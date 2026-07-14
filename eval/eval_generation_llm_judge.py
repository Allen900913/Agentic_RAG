"""
eval_generation_llm_judge.py — 五維評估 RAG 生成品質（重構版）

指標體系（全部改用二元判斷 + Python 算比例，不叫 LLM 直接吐刻度分）：

1. Correctness  (0-1)   — 命中 rubric must_include 得分點的加權比例；is_critical 未命中封頂 0.5；
                          must_not_include 命中（幻覺）直接歸 0。Python 算，LLM 只回傳 hit list。
2. Hallucination Rate   — 先讓 LLM 把回答拆成原子主張（Atomic Claims），再逐條判 Supported/Unsupported；
                          hallucination_rate = Unsupported / Total。LLM 回傳二元 list，Python 算比例。
3. Answer Relevance     — 讓 LLM「反推」該回答在解決什麼問題，再用 BGE-M3 embedding 計算
                          cosine_sim(原始問題, 推測問題)。LLM 不吐分數，只吐一個字串。
4. Refusal              — is_refusal bool；wrongful_refusal = answerable 題卻拒答。
5. Context Recall       — LLM 逐條判斷「retrieved chunks 能否推導 rubric 每個得分點」；
                          context_recall_score = 能推導的數 / 總 rubric 數。
                          無 rubric 的題 fallback 到來源檔名 overlap（與 eval_retrieval.py 對齊）。

所有 judge LLM 呼叫走 Groq（response_format=json_object, temperature=0），
RAG 生成走相同 endpoint 但不強制 JSON format（以免破壞 parse_query_filters 的輸出）。

Usage:
  python eval/eval_generation_llm_judge.py
  python eval/eval_generation_llm_judge.py --limit 10 --category mixed
  python eval/eval_generation_llm_judge.py --collection us_stock_rag_unstructured
"""

import argparse
import fnmatch
import json
import os
import re
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import rag_query as rq

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
# 2026-07-12：judge 預設從 gpt-oss-20b 換成 qwen3-32b（見 CHANGELOG）——回歸套件
# 10/11（20b 是 9/11），且不與任何 gen_model 共用 Groq TPD 池。
DEFAULT_JUDGE_MODEL = "qwen/qwen3-32b"

REFUSAL_MARKERS = [
    "don't have enough information",
    "do not have enough information",
    "知識庫", "無法回答", "沒有足夠",
]

# Groq key rotation：TPD 用完自動切換到備用 key
_GROQ_KEYS: list[str] = []
_groq_key_idx = 0

def _get_groq_key() -> str:
    """取得當前使用的 Groq API key。"""
    global _GROQ_KEYS
    if not _GROQ_KEYS:
        _GROQ_KEYS = [k for k in [
            os.getenv("GROQ_API_KEY"),
            os.getenv("GROQ_API_KEY2"),
            os.getenv("GROQ_API_KEY3"),
            os.getenv("GROQ_API_KEY4"),
        ] if k]
    return _GROQ_KEYS[_groq_key_idx % len(_GROQ_KEYS)]

def _rotate_groq_key(err_str: str) -> bool:
    """若是 TPD 耗盡錯誤，嘗試換下一個 key；回傳是否成功換 key。"""
    global _groq_key_idx
    if "tokens per day" not in err_str and "TPD" not in err_str:
        return False
    next_idx = _groq_key_idx + 1
    if next_idx >= len(_GROQ_KEYS):
        return False
    _groq_key_idx = next_idx
    print(f"  [KEY ROTATION] TPD exhausted, switching to GROQ_API_KEY{_groq_key_idx + 1}")
    return True


# ── LLM 呼叫（生成用 / judge 用，judge 自動偵測 provider）──────────────────

def call_llm_groq(messages: list[dict], model_name: str, temperature: float = 0.0) -> str:
    """生成用：model_name 以 'gemini-' 開頭走 Google GenAI SDK，其餘走 Groq（支援 key rotation）。
    temperature 預設 0.0（供 monkey-patch 後的 filter / rewrite 用，要確定性）；
    生成答案的呼叫端顯式傳 rq.GEN_TEMPERATURE(=0.3)——temp=0 的 greedy 生成會漏 rubric 點。"""
    import time
    for attempt in range(4):
        try:
            if model_name.startswith("gemini-"):
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                system_parts = [m["content"] for m in messages if m["role"] == "system"]
                user_parts   = [m["content"] for m in messages if m["role"] != "system"]
                system_text  = "\n\n".join(system_parts) if system_parts else None
                contents     = [types.Content(role="user", parts=[types.Part(text="\n\n".join(user_parts))])]
                config = types.GenerateContentConfig(
                    temperature=temperature,
                    **({"system_instruction": system_text} if system_text else {}),
                )
                resp = client.models.generate_content(model=model_name, contents=contents, config=config)
                return resp.text
            import openai
            client = openai.OpenAI(api_key=_get_groq_key(), base_url=GROQ_BASE_URL)
            resp = client.chat.completions.create(model=model_name, messages=messages, temperature=temperature)
            return resp.choices[0].message.content
        except Exception as e:
            err_str = str(e)
            if _rotate_groq_key(err_str):
                continue
            retryable = any(code in err_str for code in ("429", "503", "529", "UNAVAILABLE", "rate_limit"))
            if retryable and attempt < 3:
                wait = 15 * (2 ** attempt)
                print(f"  [GEN RETRY {attempt+1}/3] {err_str[:80]} — waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


def call_judge_llm(messages: list[dict], model_name: str, _retries: int = 4) -> str:
    """Judge 用：temperature=0，JSON mode，自動 retry + key rotation。
    - model_name 以 'gemini-' 開頭 → Google GenAI SDK（GEMINI_API_KEY）
    - 其餘 → Groq OpenAI-compatible（GROQ_API_KEY / GROQ_API_KEY2）
    """
    import time

    for attempt in range(_retries):
        try:
            if model_name.startswith("gemini-"):
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                system_parts = [m["content"] for m in messages if m["role"] == "system"]
                user_parts   = [m["content"] for m in messages if m["role"] != "system"]
                system_text  = "\n\n".join(system_parts) if system_parts else None
                contents     = [types.Content(role="user", parts=[types.Part(text="\n\n".join(user_parts))])]
                config = types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    **({"system_instruction": system_text} if system_text else {}),
                )
                resp = client.models.generate_content(model=model_name, contents=contents, config=config)
                return resp.text
            else:
                import openai
                client = openai.OpenAI(api_key=_get_groq_key(), base_url=GROQ_BASE_URL)
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0,
                )
                return resp.choices[0].message.content

        except Exception as e:
            err_str = str(e)
            if _rotate_groq_key(err_str):
                continue  # 立刻用新 key 重試，不 sleep
            retryable = any(code in err_str for code in ("429", "503", "529", "UNAVAILABLE", "rate_limit"))
            if retryable and attempt < _retries - 1:
                wait = 15 * (2 ** attempt)  # 15s → 30s → 60s → 120s
                print(f"  [RETRY {attempt+1}/{_retries-1}] {err_str[:120]} — waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


# 生成走 Groq，patch rq.call_llm。
rq.call_llm = call_llm_groq


def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    return json.loads(cleaned)


# ── 指標 2：Hallucination Rate（原子主張拆解 + 支撐驗證）──────────────────

_HALLUCINATION_SYSTEM = """\
You are evaluating whether an AI answer's factual claims are supported by retrieved context chunks.

Task:
1. Extract every distinct, atomic factual claim from the answer (numbers, names, percentages, events, rankings).
2. For each claim, judge whether it is directly supported by the retrieved chunks.
3. Determine if the answer is a refusal (says it lacks information / cannot answer).

Output ONLY a JSON object:
{
  "is_refusal": <true|false>,
  "claims": [
    {"claim": "<one atomic factual statement>", "supported": <true|false>, "reason": "<one short sentence>"},
    ...
  ]
}

Rules:
- A claim is "supported" ONLY if the chunks explicitly state or clearly imply it.
- If is_refusal is true, set claims to [].
- Do NOT include vague or hedging phrases as claims (e.g. "results may vary").
- Be strict on numbers: a different number means unsupported.
"""


def evaluate_hallucination(answer: str, chunks: list[dict], model: str) -> dict:
    """把回答拆成原子主張，逐條判 Supported/Unsupported → hallucination_rate。"""
    parts = [f"[Chunk {i+1}: {c['source']}]\n{c['content'].strip()}"
             for i, c in enumerate(chunks)]
    context = "\n\n".join(parts) if parts else "(no chunks retrieved)"

    messages = [
        {"role": "system", "content": _HALLUCINATION_SYSTEM},
        {"role": "user", "content": (
            f"=== Retrieved Chunks ===\n{context}\n\n"
            f"=== Answer to Evaluate ===\n{answer}\n\n"
            "Extract all atomic claims and verify each. Output ONLY the JSON object."
        )},
    ]
    raw = call_judge_llm(messages, model)
    try:
        p = _parse_json(raw)
        claims = p.get("claims", [])
        total = len(claims)
        unsupported = sum(1 for c in claims if not c.get("supported", True))
        return {
            "is_refusal": bool(p.get("is_refusal", False)),
            "claims": claims,
            "total_claims": total,
            "unsupported_claims": unsupported,
            "hallucination_rate": round(unsupported / total, 3) if total > 0 else 0.0,
            "error": None,
        }
    except Exception as e:
        return {
            "is_refusal": None, "claims": [], "total_claims": 0,
            "unsupported_claims": 0, "hallucination_rate": None,
            "error": f"{e!r} | raw={raw[:300]!r}",
        }


# ── 指標 3：Answer Relevance（反推問題 + Embedding cosine sim）──────────────

_REVERSE_QUESTION_SYSTEM = """\
You are given an AI-generated answer about financial data. Infer the single question this answer was written to address.

Output ONLY a JSON object:
{"generated_question": "<the question this answer is addressing>"}

Be concise and specific. Do not include any other text.
"""


def generate_reverse_question(answer: str, model: str) -> str:
    messages = [
        {"role": "system", "content": _REVERSE_QUESTION_SYSTEM},
        {"role": "user", "content": f"=== Answer ===\n{answer}\n\nWhat question does this answer address?"},
    ]
    raw = call_judge_llm(messages, model)
    try:
        return _parse_json(raw).get("generated_question", "")
    except Exception:
        return ""


def compute_relevance_score(original_q: str, generated_q: str, bge_m3) -> float | None:
    """cosine_sim(原始問題 embedding, 反推問題 embedding)，用已載入的 BGE-M3。"""
    if not generated_q:
        return None
    vecs = bge_m3.encode([original_q, generated_q], batch_size=2, max_length=512)["dense_vecs"]
    v1, v2 = vecs[0], vecs[1]
    cos_sim = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-10))
    return round(cos_sim, 4)


# ── 指標 1：Correctness + Actionable Feedback（合併單次 LLM call）───────────

_CORRECTNESS_FEEDBACK_SYSTEM = """\
You are a strict rubric evaluator AND diagnostic expert for a financial RAG system.

You will receive:
1. The user's question and the system's answer
2. A grading RUBRIC with must_include and must_not_include lists
3. Pre-computed metric results: hallucination_rate, relevance_score, context_recall

TASK A — Grade the answer against the rubric.
TASK B — Based on ALL metrics (including your grading), output a 1-3 sentence Chinese diagnostic.

Output ONLY a JSON object:
{
  "is_refusal": <true|false>,
  "must_include_hits": ["<id>", ...],
  "must_not_violations": ["<id>", ...],
  "reason": "<one short English sentence>",
  "actionable_feedback": "<1-3 句中文建議，點出最嚴重問題在哪一層（Retriever/LLM/System Prompt），以及具體改善方向>"
}

Grading rules (TASK A):
- A must_include point is a HIT only if the answer genuinely and correctly states it (correct numbers/facts).
  Merely mentioning the topic is NOT enough. Be strict on numbers.
- List a must_not_include id ONLY if the answer actually commits that specific error.
- is_refusal: true if the answer declines to answer or says it lacks information.
- FISCAL vs CALENDAR PERIOD: some companies (e.g. AAPL, MSFT, NVDA) use a non-calendar fiscal year, so
  their own filings label periods with fiscal-quarter names (e.g. "first quarter of fiscal 2026") that
  do NOT lexically match the calendar-month period code used in the question/rubric (e.g. "202512").
  Do NOT judge a must_include period checkpoint as unmet, or flag a must_not_include "wrong period"
  violation, merely because the answer uses the company's own fiscal-quarter phrasing instead of the
  calendar code. Before flagging a period mismatch, check whether the answer's cited dates/figures
  (e.g. "three months ended December 27, 2025") actually correspond to a different real-world period
  than requested — only flag if the underlying reported period is genuinely wrong, not just differently named.
- FABRICATION SCOPE: a must_not_include "fabricated number/percentage/market-share" checkpoint is violated
  ONLY when the answer states a SPECIFIC number/percentage/dollar figure that is not grounded in the
  sources. A purely qualitative comparative claim (e.g. "far exceeds competitors", "leads the market",
  "significantly higher margin") with NO specific figure attached is NOT a fabrication, even if it sounds
  like a strong claim — do not flag it.
- ATOMIC CHECKPOINTS: each must_include line is a single atomic concept — judge it independently and
  literally. OR conditions ("A / B", satisfied by any one concept) are already split into separate
  ids (e.g. "mi0_0", "mi0_1") and re-aggregated with any() in code, so you never need to reason about
  OR yourself; just decide HIT/MISS for the exact concept on each line.
- NUMERIC TOLERANCE: when a must_include checkpoint is annotated with "tolerance=±Xpp" (percentage points)
  next to a target number, it is a HIT whenever the answer's stated figure is within that tolerance of the
  target — do NOT require an exact decimal match. E.g. a checkpoint "給出毛利率約 68.3%" with
  "tolerance=±1.0pp" is satisfied by an answer saying "about 68%" or "67.6%~68.2%". Only miss the
  checkpoint if the answer's figure falls outside the tolerance band or is absent entirely.

Diagnostic rules (TASK B, 依優先序):
1. context_recall_score 低（< 0.5）→ 問題在 Retriever 沒撈到正確 Chunks，與 LLM 無關。
2. hallucination_rate 高（> 0.2）且 context_recall 尚可 → 問題在 LLM 動用預訓練記憶捏造。
3. correctness_score 低但 hallucination 低 → 回答雖忠實但漏講關鍵得分點。
4. wrongful_refusal = true → 知識庫有資料但 LLM 卻拒答，檢查 System Prompt 或 top-k。
5. relevance_score 低（< 0.7）→ 回答答非所問，可能 query understanding 有誤。
"""


# ── OR-checkpoint 拆解（col10_or_logic 修法，見 CHANGELOG 2026-07-14）─────────────────
# 背景：checkpoint 文字用「A / B」表示 OR（滿足任一即命中）。過去靠 prompt 規則叫 judge 自己
# 判 OR，但 LLM 對「A 或 B 擇一命中」這種組合邏輯不穩定（col-10 的 mi0 在多次跑之間反覆橫跳、
# 換 judge model 也沒好，是 LLM-as-judge 的已知痛點）。改法（rubric decomposition）：在 Python
# 端把 OR 拆成獨立原子 checkpoint，judge 只做單一原子的二元判定（穩定），再用 any() 聚合回原
# mi{i}。這是把組合邏輯從 prompt 移進程式碼。
_PAREN_OPEN = "（(【[「『｛{"
_PAREN_CLOSE = "）)】]」』｝}"


def _split_or_checkpoint(text: str) -> list[str]:
    """把「A / B」形式的 OR checkpoint 拆成原子概念清單。切割條件（保守，寧可不切也不亂切）：
      1. 斜線前後皆為空白（人工撰寫頂層 OR 的慣例）→ 不切 '68.3%/年'、日期、URL 這類無空白斜線；
      2. 斜線位於括號 depth 0 → 括號內的斜線是列舉範例（如「（NVLink / InfiniBand / Mellanox）」），
         切了會產生破碎片段，故不切。
    無可切處回傳單元素清單（原字串），非 OR checkpoint 行為完全不變。"""
    parts, depth, last = [], 0, 0
    for i, ch in enumerate(text):
        if ch in _PAREN_OPEN:
            depth += 1
        elif ch in _PAREN_CLOSE:
            depth = max(0, depth - 1)
        elif ch in "/／" and depth == 0 \
                and i > 0 and text[i - 1].isspace() \
                and i + 1 < len(text) and text[i + 1].isspace():
            parts.append(text[last:i].strip())
            last = i + 1
    parts.append(text[last:].strip())
    parts = [p for p in parts if p]
    return parts if len(parts) >= 2 else [text]


def evaluate_correctness_with_feedback(
    query: str,
    answer: str,
    rubric: dict,
    hall_result: dict,
    relevance_score: float | None,
    ctx_recall_llm: dict | None,
    ctx_recall_overlap: float | None,
    is_refusal_hint: bool | None,
    wrongful_refusal_hint: bool | None,
    model: str,
) -> dict:
    """Correctness + Actionable Feedback 合併單次 LLM call。"""
    # OR-checkpoint 拆解：含「A / B」的 must_include 拆成獨立原子 line（id=mi{i}_{k}），judge 對每個
    # 原子概念各自二元判定；非 OR checkpoint 維持 id=mi{i}（無 regression）。judge 回傳的原子命中
    # 由 atom_to_parent 用 any() 聚合回父 mi{i}，下游 compute_correctness 仍看到原本的 mi{i} id。
    atom_to_parent: dict[str, str] = {}
    _mi_line_list: list[str] = []
    for i, cp in enumerate(rubric["must_include"]):
        meta = (f'weight={cp["weight"]}, critical={cp["is_critical"]}'
                + (f', tolerance=±{cp["tolerance_pct"]}pp' if cp.get("tolerance_pct") is not None else ''))
        concepts = _split_or_checkpoint(cp["checkpoint"])
        if len(concepts) == 1:
            atom_to_parent[f"mi{i}"] = f"mi{i}"
            _mi_line_list.append(f'  id="mi{i}" ({meta}): {cp["checkpoint"]}')
        else:
            for k, concept in enumerate(concepts):
                lid = f"mi{i}_{k}"
                atom_to_parent[lid] = f"mi{i}"
                _mi_line_list.append(f'  id="{lid}" ({meta}): {concept}')
    mi_lines = "\n".join(_mi_line_list)
    mn_lines = "\n".join(
        f'  id="mn{i}": {p["checkpoint"]}'
        for i, p in enumerate(rubric["must_not_include"])
    ) or "  (none)"

    unsupported = [c["claim"] for c in hall_result.get("claims", []) if not c.get("supported", True)]
    uncovered_ctx = [item.get("id", "?") for item in (ctx_recall_llm or {}).get("coverage", [])
                     if not item.get("covered", True)]

    metrics_summary = (
        f"hallucination_rate: {hall_result.get('hallucination_rate')}\n"
        f"unsupported_claims: {unsupported or '（無）'}\n"
        f"relevance_score: {relevance_score}\n"
        f"context_recall_llm: {ctx_recall_llm.get('context_recall_score') if ctx_recall_llm else 'N/A'}\n"
        f"context_recall_overlap: {ctx_recall_overlap}\n"
        f"uncovered_rubric_in_chunks: {uncovered_ctx or '（無）'}\n"
        f"is_refusal: {is_refusal_hint}\n"
        f"wrongful_refusal: {wrongful_refusal_hint}"
    )

    messages = [
        {"role": "system", "content": _CORRECTNESS_FEEDBACK_SYSTEM},
        {"role": "user", "content": (
            f"=== User Question ===\n{query}\n\n"
            f"=== System Answer ===\n{answer}\n\n"
            f"=== Grading Rubric ===\n"
            f"must_include:\n{mi_lines}\n"
            f"must_not_include:\n{mn_lines}\n\n"
            f"=== Pre-computed Metrics ===\n{metrics_summary}\n\n"
            "Complete TASK A and TASK B. Output ONLY the JSON object."
        )},
    ]
    raw = call_judge_llm(messages, model)
    try:
        p = _parse_json(raw)
        # 原子命中 → any() 聚合回父 mi{i}（任一原子命中即父 checkpoint 命中）。未知 id 忽略。
        parent_hits = sorted({atom_to_parent[h] for h in p.get("must_include_hits", [])
                              if h in atom_to_parent})
        return {
            "is_refusal": bool(p.get("is_refusal", False)),
            "must_include_hits": parent_hits,
            "must_not_violations": list(p.get("must_not_violations", [])),
            "reason": str(p.get("reason", "")),
            "actionable_feedback": str(p.get("actionable_feedback", "")),
            "error": None,
        }
    except Exception as e:
        return {
            "is_refusal": None,
            "must_include_hits": [], "must_not_violations": [],
            "reason": None, "actionable_feedback": "",
            "error": f"{e!r} | raw={raw[:300]!r}",
        }


def evaluate_correctness_with_feedback_voted(*, votes: int, **kwargs) -> dict:
    """對 evaluate_correctness_with_feedback 做 N 次獨立呼叫、逐 checkpoint 多數決。

    動機（見 CHANGELOG 2026-07-08 sem-03 false positive）：單次 judge call 對
    must_not_include 的誤判會讓 compute_correctness() 把分數砍到 0（評分懸崖），
    但 judge 本身在 temp=0 下對同一組 checkpoint 的逐項判定並非完全穩定
    （gpt-oss-120b 是 MoE）。votes=1 時行為與舊版單次呼叫完全相同（無 regression）；
    votes>1 時對每個 mi{i}/mn{i} id 分別在 N 次結果中做嚴格多數決
    （count > votes/2 才算數，未過半視為未命中/未違規——偏向不誤判 fatal，
    對稱地也偏向不誤判 must_include 命中），is_refusal 同樣多數決。
    reason/actionable_feedback 為自由文字，取第一次無 error 的呼叫結果。
    """
    if votes <= 1:
        return evaluate_correctness_with_feedback(**kwargs)

    calls = [evaluate_correctness_with_feedback(**kwargs) for _ in range(votes)]

    def _majority(get_ids) -> list[str]:
        counts: dict[str, int] = {}
        for c in calls:
            for cid in get_ids(c):
                counts[cid] = counts.get(cid, 0) + 1
        return [cid for cid, n in counts.items() if n > votes / 2]

    refusal_votes = sum(1 for c in calls if c.get("is_refusal"))
    first_ok = next((c for c in calls if not c.get("error")), calls[0])

    return {
        "is_refusal": refusal_votes > votes / 2,
        "must_include_hits": _majority(lambda c: c.get("must_include_hits", [])),
        "must_not_violations": _majority(lambda c: c.get("must_not_violations", [])),
        "reason": first_ok.get("reason", ""),
        "actionable_feedback": first_ok.get("actionable_feedback", ""),
        "error": None if any(not c.get("error") for c in calls) else calls[0].get("error"),
        "vote_detail": {
            "votes": votes,
            "must_include_hits_per_call": [c.get("must_include_hits", []) for c in calls],
            "must_not_violations_per_call": [c.get("must_not_violations", []) for c in calls],
        },
    }


# 保留舊函數供無 rubric 題目的 fallback feedback 使用
def evaluate_correctness(query: str, answer: str, rubric: dict, model: str) -> dict:
    """舊版單獨 correctness call（無 rubric 時不呼叫，保留相容性）。"""
    return evaluate_correctness_with_feedback(
        query, answer, rubric, {}, None, None, None, None, None, model
    )


def compute_correctness(rubric: dict, verdict: dict) -> dict | None:
    """依 weight 加權算 Correctness（0-1），純 Python 決定論計算。

    - 命中任一 must_not_include → fatal，score = 0
    - 漏掉任一 is_critical must_include → critical_miss，score 封頂 0.5
    """
    if not rubric:
        return None
    must_inc = rubric["must_include"]
    total_w = sum(p["weight"] for p in must_inc) or 1
    hits = set(verdict["must_include_hits"])
    got_w = sum(p["weight"] for i, p in enumerate(must_inc) if f"mi{i}" in hits)
    score = got_w / total_w

    fatal = len(verdict["must_not_violations"]) > 0
    critical_miss = any(
        p["is_critical"] and f"mi{i}" not in hits
        for i, p in enumerate(must_inc)
    )
    if fatal:
        score = 0.0
    elif critical_miss:
        score = min(score, 0.5)
    return {
        "score": round(score, 3),
        "critical_miss": critical_miss,
        "fatal_hallucination": fatal,
    }


# ── 指標 5：Context Recall（Chunks 能推導幾個 Rubric 點）────────────────────

_CONTEXT_RECALL_SYSTEM = """\
You are checking whether retrieved context chunks contain sufficient information to support each rubric point.

For each rubric point, judge whether the chunks provide enough evidence to derive or confirm it.

Output ONLY a JSON object:
{
  "coverage": [
    {"id": "<id>", "covered": <true|false>, "reason": "<one short sentence>"},
    ...
  ]
}

A point is "covered" if the answer to it can be directly inferred from the chunks, even if not word-for-word.
"""


def evaluate_context_recall(rubric: dict, chunks: list[dict], model: str) -> dict | None:
    """LLM 逐條判斷 chunks 能否推導出各個 rubric 得分點。"""
    if not rubric:
        return None
    parts = [f"[Chunk {i+1}: {c['source']}]\n{c['content'].strip()}"
             for i, c in enumerate(chunks)]
    context = "\n\n".join(parts) if parts else "(no chunks retrieved)"
    mi_lines = "\n".join(
        f'  id="mi{i}": {p["checkpoint"]}'
        for i, p in enumerate(rubric["must_include"])
    )
    messages = [
        {"role": "system", "content": _CONTEXT_RECALL_SYSTEM},
        {"role": "user", "content": (
            f"=== Retrieved Chunks ===\n{context}\n\n"
            f"=== Rubric Points to Check ===\n{mi_lines}\n\n"
            "Output ONLY the JSON object."
        )},
    ]
    raw = call_judge_llm(messages, model)
    try:
        p = _parse_json(raw)
        coverage = p.get("coverage", [])
        total = len(coverage)
        covered = sum(1 for c in coverage if c.get("covered", False))
        return {
            "coverage": coverage,
            "context_recall_score": round(covered / total, 3) if total > 0 else None,
            "error": None,
        }
    except Exception as e:
        return {
            "coverage": [], "context_recall_score": None,
            "error": f"{e!r} | raw={raw[:300]!r}",
        }


# ── 診斷建議：Actionable Feedback ────────────────────────────────────────────

_FEEDBACK_SYSTEM = """\
你是一個 RAG 系統的品質診斷專家。根據以下評估指標的結果，輸出一段簡潔的可操作診斷建議。

輸出 ONLY 一個 JSON 物件：
{
  "actionable_feedback": "<1-3 句中文建議，點出最嚴重的問題在哪一層（Retriever/Chunks/System Prompt/LLM 本身），以及具體改善方向>"
}

診斷邏輯（依優先序）：
1. context_recall_score 低（< 0.5）→ 問題在 Retriever 沒撈到正確 Chunks，與 LLM 無關。
2. hallucination_rate 高（> 0.2）且 context_recall 尚可 → 問題在 LLM 動用預訓練記憶捏造。
3. correctness_score 低但 hallucination 低 → 問題在回答雖然忠實，但漏講關鍵得分點。
4. wrongful_refusal = true → 知識庫有資料但 LLM 卻拒答，檢查 System Prompt 或 top-k 設定。
5. relevance_score 低（< 0.7）→ 回答答非所問，可能 query understanding 有誤。
可以同時提多個問題，但最多 3 句，每句對應一個具體問題與建議動作。
"""


def generate_actionable_feedback(
    query: str,
    rubric: dict | None,
    correctness: dict | None,
    correctness_verdict: dict | None,
    hall_result: dict,
    relevance_score: float | None,
    ctx_recall_llm: dict | None,
    ctx_recall_overlap: float | None,
    is_refusal: bool | None,
    wrongful_refusal: bool | None,
    model: str,
) -> str:
    """綜合所有指標結果，讓 LLM 輸出一段可操作的診斷建議。"""
    # 整理缺漏的 rubric 得分點（給 LLM 具體名稱）
    missed_points = []
    if rubric and correctness_verdict:
        hits = set(correctness_verdict.get("must_include_hits", []))
        for i, p in enumerate(rubric["must_include"]):
            if f"mi{i}" not in hits:
                missed_points.append(f"mi{i}（{p['checkpoint']}）")

    uncovered_ctx = []
    if ctx_recall_llm:
        for item in ctx_recall_llm.get("coverage", []):
            if not item.get("covered", True):
                uncovered_ctx.append(item.get("id", "?"))

    unsupported_claims = [
        c["claim"] for c in hall_result.get("claims", [])
        if not c.get("supported", True)
    ]

    metrics_summary = (
        f"hallucination_rate: {hall_result.get('hallucination_rate')}\n"
        f"correctness_score: {correctness['score'] if correctness else 'N/A'}\n"
        f"critical_miss: {correctness.get('critical_miss') if correctness else 'N/A'}\n"
        f"fatal_hallucination: {correctness.get('fatal_hallucination') if correctness else 'N/A'}\n"
        f"relevance_score: {relevance_score}\n"
        f"context_recall_llm: {ctx_recall_llm.get('context_recall_score') if ctx_recall_llm else 'N/A'}\n"
        f"context_recall_overlap: {ctx_recall_overlap}\n"
        f"is_refusal: {is_refusal}\n"
        f"wrongful_refusal: {wrongful_refusal}\n"
        f"missed_rubric_points: {missed_points or '（無）'}\n"
        f"uncovered_in_chunks: {uncovered_ctx or '（無）'}\n"
        f"unsupported_claims: {unsupported_claims or '（無）'}"
    )

    messages = [
        {"role": "system", "content": _FEEDBACK_SYSTEM},
        {"role": "user", "content": (
            f"=== 問題 ===\n{query}\n\n"
            f"=== 評估指標結果 ===\n{metrics_summary}\n\n"
            "根據以上結果，輸出 ONLY 診斷建議 JSON 物件。"
        )},
    ]
    raw = call_judge_llm(messages, model)
    try:
        return _parse_json(raw).get("actionable_feedback", "")
    except Exception:
        return ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def looks_like_refusal(answer: str) -> bool:
    a = (answer or "").lower()
    return any(m.lower() in a for m in REFUSAL_MARKERS)


def snapshot_sources(client, collection):
    """掃整個 collection 的 distinct payload['source']，供 glob 展開算 context recall fallback。"""
    sources = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection, limit=512, offset=offset,
            with_payload=["source"], with_vectors=False,
        )
        for pt in points:
            s = (pt.payload or {}).get("source")
            if s:
                sources.add(s)
        if offset is None:
            break
    return sources


def expand_relevant(patterns, all_sources):
    out = set()
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            out |= {s for s in all_sources if fnmatch.fnmatch(s, pat)}
        elif pat in all_sources:
            out.add(pat)
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def agg(rows):
    hall = [r["hallucination_rate"] for r in rows if r["hallucination_rate"] is not None]
    corr = [r["correctness"]["score"] for r in rows if r["correctness"] is not None]
    rel  = [r["relevance_score"] for r in rows if r["relevance_score"] is not None]
    ctx_llm = [
        r["context_recall_llm"]["context_recall_score"]
        for r in rows
        if r.get("context_recall_llm") and
           r["context_recall_llm"].get("context_recall_score") is not None
    ]
    ctx_ovlp = [r["context_recall_overlap"] for r in rows
                if r.get("context_recall_overlap") is not None]
    wref  = [r["wrongful_refusal"] for r in rows if r["wrongful_refusal"] is not None]
    fatal = [r for r in rows if r["correctness"] and r["correctness"].get("fatal_hallucination")]
    return {
        "n": len(rows),
        "hallucination_rate_mean":    round(statistics.mean(hall), 3) if hall else None,
        "correctness_mean":           round(statistics.mean(corr), 3) if corr else None,
        "n_with_rubric":              len(corr),
        "relevance_mean":             round(statistics.mean(rel), 3) if rel else None,
        "context_recall_llm_mean":    round(statistics.mean(ctx_llm), 3) if ctx_llm else None,
        "context_recall_overlap_mean":round(statistics.mean(ctx_ovlp), 3) if ctx_ovlp else None,
        "wrongful_refusal_rate":      round(sum(wref) / len(wref), 3) if wref else None,
        "fatal_hallucination_count":  len(fatal),
        # Pass@ thresholds
        "hallucination_pass@0.1":     round(sum(1 for s in hall if s <= 0.1) / len(hall), 3) if hall else None,
        "correctness_pass@0.6":       round(sum(1 for s in corr if s >= 0.6) / len(corr), 3) if corr else None,
        "relevance_pass@0.8":         round(sum(1 for s in rel  if s >= 0.8) / len(rel),  3) if rel  else None,
    }


def print_summary_table(summary: dict) -> None:
    print(f"\n{'═'*82}\nLLM-AS-JUDGE SUMMARY (5 metrics)\n{'═'*82}")
    hdr = (f"  {'CAT':<9}{'n':>3}  {'hall↓':>6} {'correct':>8} {'relev':>7} "
           f"{'ctxLLM':>7} {'wRefus':>7} {'fatal':>6}")
    print(hdr)
    for cat, s in summary.items():
        print(f"  {cat.upper():<9}{s['n']:>3}  "
              f"{str(s['hallucination_rate_mean']):>6} {str(s['correctness_mean']):>8} "
              f"{str(s['relevance_mean']):>7} {str(s['context_recall_llm_mean']):>7} "
              f"{str(s['wrongful_refusal_rate']):>7} {str(s['fatal_hallucination_count']):>6}")


def run_single_pass(queries, all_sources, bge_m3, rerank_model, client, args, out_path: Path):
    """跑一輪完整 eval（每題 retrieve+generate+judge），寫入 out_path，回傳 (records, summary)。
    支援 per-run resume（沿用既有邏輯：檔案已存在且該題已有結果就跳過），
    多輪重跑（--repeat）時每輪各自獨立 resume，互不干擾。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 載入已完成的題目，跳過 hallucination_rate 不為 None 的
    existing_records: dict = {}
    if out_path.exists():
        try:
            existing_data = json.loads(out_path.read_text(encoding="utf-8"))
            for r in existing_data.get("records", []):
                # skip if any metric was computed OR an answer was generated (no-rubric queries);
                # re-run only if answer is None (ERROR-skipped due to quota exhaustion)
                if (r.get("hallucination_rate") is not None
                        or r.get("correctness") is not None
                        or r.get("answer") is not None):
                    existing_records[r["id"]] = r
            if existing_records:
                print(f"[INFO] Resuming: {len(existing_records)} questions already done, will skip them.")
        except Exception:
            pass

    records = []
    for q in queries:
        qid, category, query_str = q["id"], q["category"], q["query"]
        rubric = q.get("rubric")
        answerable = q.get("answerable", True)
        relevant = expand_relevant(q.get("relevant", []), all_sources)

        if qid in existing_records:
            print(f"\n[{qid}] ({category}) SKIP (already done)")
            records.append(existing_records[qid])
            continue

        print(f"\n[{qid}] ({category}) {query_str}")

        try:
            # ── Retrieve + Generate ──────────────────────────────────────
            chunks, fallback_note = rq.retrieve(query_str, bge_m3, rerank_model, client,
                                               top_k=args.top_k, model_name=args.retrieval_model,
                                               enable_rewrite=args.rewrite,
                                               translate_query_en=args.translate_query_en,
                                               rerank_multi_query=args.rerank_multi_query)
            if not chunks:
                answer = "I don't have enough information in my knowledge base to answer this."
            else:
                # 檢索後句級抽取（見 CHANGELOG 2026-07-14）：用 retrieval_model（檢索側，
                # 遵守 model_name 紀律）。預設關（--compress 才開），維持既有 baseline 向後相容。
                if args.compress:
                    chunks = rq.compress_chunks(query_str, chunks, args.retrieval_model)
                user_prompt = rq.build_user_prompt(query_str, chunks, fallback_note)
                answer = rq.call_llm(
                    [{"role": "system", "content": rq.SYSTEM_PROMPT},
                     {"role": "user", "content": user_prompt}],
                    args.gen_model, temperature=rq.GEN_TEMPERATURE)

            # correctness-only 模式：跳過幻覺/相關性/context-recall，省一半 LLM 額度
            co = args.correctness_only

            # ── 指標 2：Hallucination Rate（原子主張，每題都跑）────────
            if co:
                hall_result = {"claims": [], "total_claims": 0, "unsupported_claims": 0,
                               "hallucination_rate": None, "is_refusal": None, "error": None}
            else:
                hall_result = evaluate_hallucination(answer, chunks, args.judge_model)

            # ── 指標 3：Answer Relevance（反推問題 + cosine sim，每題都跑）
            if co:
                generated_q = ""
                relevance_score = None
            else:
                generated_q = generate_reverse_question(answer, args.judge_model)
                relevance_score = compute_relevance_score(query_str, generated_q, bge_m3)

            # ── 指標 5：Context Recall（只有 rubric 題才跑）──────────
            ctx_recall_llm = None
            if rubric and not co:
                ctx_recall_llm = evaluate_context_recall(rubric, chunks, args.judge_model)
            # Fallback：來源檔名 overlap（與 eval_retrieval.py 計算口徑一致）
            retrieved_sources = {c["source"] for c in chunks}
            ctx_hits = retrieved_sources & relevant
            ctx_recall_overlap = (len(ctx_hits) / len(relevant)) if relevant else None

            # ── 先推算初步 refusal（給合併 call 參考）────────────────
            is_refusal_hint = hall_result.get("is_refusal") or looks_like_refusal(answer)
            wrongful_refusal_hint = bool(is_refusal_hint and answerable)

            # ── 指標 1 + 診斷建議（合併單次 LLM call，rubric 題）─────
            correctness_verdict = None
            correctness = None
            actionable_feedback = ""
            if rubric:
                combined = evaluate_correctness_with_feedback_voted(
                    votes=args.judge_votes,
                    query=query_str,
                    answer=answer,
                    rubric=rubric,
                    hall_result=hall_result,
                    relevance_score=relevance_score,
                    ctx_recall_llm=ctx_recall_llm,
                    ctx_recall_overlap=ctx_recall_overlap,
                    is_refusal_hint=is_refusal_hint,
                    wrongful_refusal_hint=wrongful_refusal_hint,
                    model=args.judge_model,
                )
                actionable_feedback = combined.pop("actionable_feedback", "")
                correctness_verdict = combined
                correctness = compute_correctness(rubric, correctness_verdict)
            elif not co:
                # 無 rubric 題：單獨呼叫 feedback（correctness-only 模式跳過，無 correctness 可算）
                actionable_feedback = generate_actionable_feedback(
                    query=query_str,
                    rubric=rubric,
                    correctness=None,
                    correctness_verdict=None,
                    hall_result=hall_result,
                    relevance_score=relevance_score,
                    ctx_recall_llm=ctx_recall_llm,
                    ctx_recall_overlap=ctx_recall_overlap,
                    is_refusal=is_refusal_hint,
                    wrongful_refusal=wrongful_refusal_hint,
                    model=args.judge_model,
                )

            # ── 指標 4：Refusal（最終版，優先用 correctness_verdict）──
            is_refusal = (
                (correctness_verdict or {}).get("is_refusal")
                or is_refusal_hint
            )
            wrongful_refusal = bool(is_refusal and answerable)

            # ── 印進度 ────────────────────────────────────────────────
            ctx_display = (ctx_recall_llm or {}).get("context_recall_score", ctx_recall_overlap)
            print(f"  hall={hall_result['hallucination_rate']} "
                  f"correct={correctness['score'] if correctness else '-'} "
                  f"relev={relevance_score} "
                  f"refusal={is_refusal} "
                  f"ctx={None if ctx_display is None else round(ctx_display, 2)}")
            if actionable_feedback:
                print(f"  feedback: {actionable_feedback}")
            for key in ("error",):
                if hall_result.get(key):
                    print(f"  [WARN] hallucination parse error: {hall_result[key]}")
                if correctness_verdict and correctness_verdict.get(key):
                    print(f"  [WARN] correctness parse error: {correctness_verdict[key]}")
                if ctx_recall_llm and ctx_recall_llm.get(key):
                    print(f"  [WARN] context recall parse error: {ctx_recall_llm[key]}")

        except Exception as e:
            print(f"  [ERROR] skipping query: {e!r}")
            chunks, answer = [], None
            hall_result = {"claims": [], "total_claims": 0, "unsupported_claims": 0,
                           "hallucination_rate": None, "is_refusal": None, "error": repr(e)}
            generated_q = ""
            relevance_score = None
            correctness_verdict = correctness = None
            ctx_recall_llm = None
            ctx_recall_overlap = None
            is_refusal = wrongful_refusal = None
            actionable_feedback = ""

        records.append({
            "id": qid, "category": category, "query": query_str,
            "answerable": answerable, "answer": answer,
            "sources": [{"source": c["source"], "chunk_index": c["chunk_index"]}
                        for c in chunks],
            # 指標 1
            "correctness": correctness,
            "correctness_verdict": correctness_verdict,
            # 指標 2
            "hallucination_rate": hall_result.get("hallucination_rate"),
            "hallucination_claims": hall_result.get("claims", []),
            # 指標 3
            "relevance_score": relevance_score,
            "relevance_generated_question": generated_q,
            # 指標 4
            "is_refusal": is_refusal,
            "wrongful_refusal": wrongful_refusal,
            # 指標 5
            "context_recall_llm": ctx_recall_llm,
            "context_recall_overlap": ctx_recall_overlap,
            # 診斷建議
            "actionable_feedback": actionable_feedback,
        })

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"summary": {}, "records": records}, f, indent=2, ensure_ascii=False)

        # 每題之間稍等，避免打到 Gemini RPM 上限（免費 10 req/min）
        import time as _time
        _time.sleep(8)

    # ── Aggregate（此輪）──────────────────────────────────────────────────
    cats = sorted({r["category"] for r in records}) + ["overall"]
    summary = {
        cat: agg([r for r in records if cat == "overall" or r["category"] == cat])
        for cat in cats
    }
    print_summary_table(summary)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "records": records}, f, indent=2, ensure_ascii=False)
    print(f"\n[DONE] Written to {out_path}")

    return records, summary


def aggregate_across_runs(all_run_records: list, all_run_summaries: list, k: int) -> dict:
    """把 k 次獨立跑的結果整理成「跨 run 平均 ± 標準差」，取代只看單次 mean 下結論
    （單次跑的 variance 會淹沒小幅度的真訊號，見 CHANGELOG 2026-07-06 溫度實驗）。

    category_summary：每個 category 在 k 次跑各自的 correctness_mean，取平均與 std
      （std_across_runs 直接量化「這個 category 的 mean 有多容易因抽樣噪音擺動」）。
    per_question：每題在 k 次跑的 correctness score 清單、平均、std、critical_miss 命中率
      （用來判斷某題的分數變化是穩定訊號還是噪音）。"""
    by_qid: dict = {}
    for run_records in all_run_records:
        for r in run_records:
            by_qid.setdefault(r["id"], []).append(r)

    per_question = {}
    for qid, rs in by_qid.items():
        scores = [r["correctness"]["score"] for r in rs if r.get("correctness") is not None]
        crit = [bool(r["correctness"]["critical_miss"]) for r in rs if r.get("correctness") is not None]
        per_question[qid] = {
            "category": rs[0]["category"],
            "query": rs[0]["query"],
            "n_runs": len(rs),
            "scores": scores,
            "score_mean": round(statistics.mean(scores), 3) if scores else None,
            "score_std": round(statistics.pstdev(scores), 3) if len(scores) > 1 else (0.0 if scores else None),
            "critical_miss_rate": round(sum(crit) / len(crit), 3) if crit else None,
        }

    cats = sorted({r["category"] for run in all_run_records for r in run}) + ["overall"]
    category_summary = {}
    for cat in cats:
        run_means = [
            s[cat]["correctness_mean"] for s in all_run_summaries
            if cat in s and s[cat]["correctness_mean"] is not None
        ]
        category_summary[cat] = {
            "run_means": run_means,
            "mean_of_means": round(statistics.mean(run_means), 3) if run_means else None,
            "std_across_runs": round(statistics.pstdev(run_means), 3) if len(run_means) > 1 else (0.0 if run_means else None),
        }

    return {
        "k_runs": k,
        "category_summary": category_summary,
        "per_question": per_question,
    }


def print_repeat_summary(agg_result: dict) -> None:
    print(f"\n{'═'*82}\nK-RUN AVERAGE (k={agg_result['k_runs']})\n{'═'*82}")
    print(f"  {'CAT':<9}{'mean_of_means':>14}{'std_across_runs':>17}   run_means")
    for cat, s in agg_result["category_summary"].items():
        print(f"  {cat.upper():<9}{str(s['mean_of_means']):>14}{str(s['std_across_runs']):>17}   {s['run_means']}")
    print("\n  Per-question (correctness score across runs):")
    for qid, s in agg_result["per_question"].items():
        print(f"    {qid:<8} mean={s['score_mean']}  std={s['score_std']}  "
              f"crit_miss_rate={s['critical_miss_rate']}  scores={s['scores']}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Five-metric LLM-as-judge: correctness / hallucination / "
                    "relevance / refusal / context-recall",
    )
    parser.add_argument("--eval-set", default="eval/eval_set.json")
    parser.add_argument("--output", default="eval/generation_judge.json")
    parser.add_argument("--collection", default=rq.COLLECTION_NAME)
    parser.add_argument("--top-k", type=int, default=rq.DEFAULT_TOP_K)
    parser.add_argument("--gen-model", default=DEFAULT_GROQ_MODEL)
    parser.add_argument("--retrieval-model", default=rq.DEFAULT_MODEL,
                        help="retrieve() 內部 filter/rewrite/translate 呼叫用的模型，"
                             "與 --gen-model 獨立（見 CHANGELOG 2026-07-08 diagnose_crit_miss.py "
                             "bug：同一變數餵兩個維度會讓 gen-model A/B 混入檢索路徑差異）。"
                             "預設與生產 rag_query.DEFAULT_MODEL 一致，不指定時行為不變。")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--judge-votes", type=int, default=1,
                        help="correctness judge 對 must_include/must_not_include 逐項多數決的獨立呼叫次數"
                             "（見 CHANGELOG 2026-07-08 sem-03 false positive 待辦：3 票多數決）。"
                             "預設 1（與舊版單次呼叫行為完全相同）；>1 時每題多花 (votes-1) 次 judge call，"
                             "只影響 correctness 判定，不影響 hallucination/relevance/context-recall。")
    parser.add_argument("--category", default=None)
    parser.add_argument("--ids", nargs="+", default=None,
                        help="只評指定 query id（如 sem-08），對單題除錯/驗證省 TPD。可與 --category 疊用。")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--correctness-only", action="store_true",
                        help="只跑 generate + Correctness 評分，跳過 hallucination / "
                             "relevance / context-recall（每題 2 次 LLM call，省一半額度）")
    parser.add_argument("--rewrite", action="store_true",
                        help="Enable rag_query's query-rewrite recall expansion (enable_rewrite=True)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="重跑 k 次取平均 ± std，解決單次跑的 run-to-run 抽樣噪音。"
                             "k>1 時每輪寫入 {output_stem}_run{i}.json，最終彙整（mean/std）寫入 --output。")
    parser.add_argument("--translate-query-en", action="store_true",
                        help="入口把 query 翻成英文一次，dense/sparse/rewrite/rerank 全部改用（解 cross-lingual 失真，見 CHANGELOG 2026-07-08）")
    parser.add_argument("--compress", action="store_true",
                        help="檢索後句級抽取：生成前把每個 chunk 的相關句逐字抽出擺前面（見 CHANGELOG "
                             "2026-07-14）。預設關，維持既有 baseline 向後相容。")
    parser.add_argument("--rerank-multi-query", action="store_true",
                        help="cross-encoder 精排改用「原 query + rewrite 變體」逐一評分取跨 query 最高分"
                             "（需搭配 --rewrite；見 CHANGELOG 2026-07-08 復活案例：英文+glossary 變體下對 sem-02 有效）")
    args = parser.parse_args()

    rq.COLLECTION_NAME = args.collection

    eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    queries = eval_set["queries"]
    if args.category:
        queries = [q for q in queries if q["category"] == args.category]
    if args.ids:
        queries = [q for q in queries if q["id"] in set(args.ids)]
    if args.limit:
        queries = queries[: args.limit]
    print(f"[INFO] Evaluating {len(queries)} queries against '{rq.COLLECTION_NAME}' "
          f"(retrieval={args.retrieval_model}, gen={args.gen_model}, judge={args.judge_model}, "
          f"judge_votes={args.judge_votes}, rewrite={args.rewrite}, repeat={args.repeat})")

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    print("[INFO] Loading BGE-M3 + reranker...")
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    # max_length 對齊生產（rag_query 用 RERANK_MAX_LENGTH=2048）；未帶會套模型預設 8192＝不截斷，
    # 讓 eval 的 rerank 看到比生產更多的 token、排序可能與生產不一致（同 model_name 陷阱精神）。
    rerank_model = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    client = rq.make_qdrant_client()

    print("[INFO] Snapshotting collection sources for context-recall fallback...")
    all_sources = snapshot_sources(client, rq.COLLECTION_NAME)

    if args.repeat <= 1:
        run_single_pass(queries, all_sources, bge_m3, rerank_model, client, args, Path(args.output))
        return

    out_path = Path(args.output)
    all_run_records, all_run_summaries = [], []
    for i in range(1, args.repeat + 1):
        run_out = out_path.parent / f"{out_path.stem}_run{i}{out_path.suffix}"
        print(f"\n{'#'*82}\n[REPEAT {i}/{args.repeat}] -> {run_out}\n{'#'*82}")
        records, summary = run_single_pass(queries, all_sources, bge_m3, rerank_model, client, args, run_out)
        all_run_records.append(records)
        all_run_summaries.append(summary)

    agg_result = aggregate_across_runs(all_run_records, all_run_summaries, args.repeat)
    print_repeat_summary(agg_result)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(agg_result, f, indent=2, ensure_ascii=False)
    print(f"\n[DONE] k={args.repeat}-run average written to {out_path}")


if __name__ == "__main__":
    main()
