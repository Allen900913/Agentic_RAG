"""
eval_agentic.py — 評估 agentic_rag.py 的最終答案品質，複用 eval_generation_llm_judge 的既有 scorer。

跑法（從 repo root）：
  python eval/eval_agentic.py --judge-votes 3
  python eval/eval_agentic.py --module agentic_rag_nv --baseline   # 評 NVIDIA 版 + 單 pass 對照
  python eval/eval_agentic.py --limit 2 --judge-model gemini-2.5-flash  # judge 走 Gemini（Groq TPD 用盡時）

`--module` 選要評哪一份 agentic 實作（預設 agentic_rag_mix）。注意各版模型預設不同：
agentic_rag_mix 仍指向 **已被 Groq 下架的 llama-4-scout**（2026-07-18 起呼叫必 404），
agentic_rag / agentic_rag_nv 見各自檔頭。

設計：
- 生成走 agentic_rag.run_agentic()（主 agent 拆解 → subagent 檢索 → 合成），取最終答案 + 全 run
  用到的 chunks 聯集。
- 評分「不重寫」：直接 import eval_generation_llm_judge 的 compute_correctness /
  evaluate_correctness_with_feedback_voted / evaluate_context_recall（judge 走 Groq 或 Gemini，
  與 agentic 生成的 Groq 呼叫獨立）。
- 題庫 eval/eval_set_agentic.json：full 題自帶 rubric；{"ref": "<id>"} 從 eval_set.json 撈整題復用。
- 注意：agentic 生成與單 pass baseline 都吃 Groq TPD；judge 可用 --judge-model gemini-* 改走 Gemini
  以省 Groq 額度。做 A/B 決策時加 --judge-votes 3 並看逐題 critical_miss，勿對單次 mean 過度解讀
  （見 CLAUDE.md 量測紀律）。
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import rag_query as rq

# ⚠ 在 import eval_generation_llm_judge 之前先抓住 rag_query.py **原生未被動過**的 call_llm。
# ej 與 agentic_rag_nv 都會在 import 當下 monkeypatch `rq.call_llm`（見 _load_agentic_module），
# 一旦被蓋掉就再也拿不回真正的生產路由，「生產對照組」就會量到別人的東西。
_PROD_CALL_LLM = rq.call_llm

import eval_generation_llm_judge as ej

# 要評估的 agentic 模組，由 --module 決定，在 main() 裡載入（見 _load_agentic_module 的順序警告）。
ar = None


def _load_agentic_module(name: str):
    """載入要評估的 agentic 模組（agentic_rag_mix / agentic_rag / agentic_rag_nv）。

    ⚠ **必須在 `import eval_generation_llm_judge` 之後才載入**：ej 在 import 當下就做
    `rq.call_llm = call_llm_groq`（module 級 monkeypatch，見 ej 第 174 行），而 agentic_rag_nv 同樣在
    import 當下把 `rq.call_llm` 換成 NVIDIA 路由——**兩者搶同一個全域，後 import 的才生效**。
    順序顛倒的話，agentic 側的 NVIDIA 模型 id（如 z-ai/glm-5.2）會被送去 Groq 而 404，
    而且是靜默地把整輪 eval 跑成垃圾。judge 自己走 `ej.call_judge_llm`（獨立函式、自行偵測
    provider），不吃 `rq.call_llm`，所以判分不受這個順序影響。"""
    import importlib
    return importlib.import_module(name)

ROOT = Path(__file__).resolve().parent
AGENTIC_SET = ROOT / "eval_set_agentic.json"
BASE_SET = ROOT / "eval_set.json"


def load_queries(limit: int | None = None) -> list[dict]:
    """載入 agentic 題庫；{"ref": id} 從 eval_set.json 撈整題復用。"""
    agentic = json.loads(AGENTIC_SET.read_text(encoding="utf-8"))["queries"]
    base = {q["id"]: q for q in json.loads(BASE_SET.read_text(encoding="utf-8"))["queries"]}
    out = []
    for entry in agentic:
        if "ref" in entry:
            ref = base.get(entry["ref"])
            if ref is None:
                print(f"[WARN] ref '{entry['ref']}' not found in eval_set.json, skipping")
                continue
            out.append(ref)
        else:
            out.append(entry)
    return out[:limit] if limit else out


def score_answer(q: dict, answer: str, chunks: list[dict], judge_model: str, votes: int) -> dict:
    """用既有 scorer 打分：correctness（voted）+ context_recall。"""
    rubric = q.get("rubric")
    if not rubric:
        return {"note": "null rubric（純檢索題），略過生成評分"}
    verdict = ej.evaluate_correctness_with_feedback_voted(
        votes=votes,
        query=q["query"], answer=answer, rubric=rubric,
        hall_result={}, relevance_score=None,
        ctx_recall_llm=None, ctx_recall_overlap=None,
        is_refusal_hint=None, wrongful_refusal_hint=None,
        model=judge_model,
    )
    corr = ej.compute_correctness(rubric, verdict)
    ctx = ej.evaluate_context_recall(rubric, chunks, judge_model)
    return {
        "correctness": corr["score"],
        "critical_miss": corr["critical_miss"],
        "fatal_hallucination": corr["fatal_hallucination"],
        "is_refusal": verdict.get("is_refusal", False),
        # 既有 bug（與本次 NVIDIA/judge 改動無關，git blame 顯示此行未被今天的改動碰過）：
        # evaluate_context_recall 回傳的 key 是 context_recall_score，不是 score——原本讀
        # ctx.get("score") 永遠拿 None，導致 context_recall 從未在這支 eval 裡真正算出過值。
        "context_recall": (ctx or {}).get("context_recall_score") if ctx else None,
        "must_include_hits": verdict.get("must_include_hits", []),
        "reason": verdict.get("reason", ""),
    }


def run_baseline_single_pass(query: str) -> dict:
    """對照組：現行單 pass（rq.retrieve 開 rewrite+translate → build_user_prompt → 生成）。"""
    bge_m3, rerank_model, client = ar._get_models()
    chunks, note = rq.retrieve(
        query, bge_m3, rerank_model, client, top_k=rq.DEFAULT_TOP_K,
        model_name=ar.RETRIEVAL_MODEL, enable_rewrite=True, translate_query_en=True,
    )
    if not chunks:
        return {"answer": "I don't have enough information in my knowledge base to answer this.",
                "chunks": []}
    user_prompt = rq.build_user_prompt(query, chunks, note)
    messages = [
        {"role": "system", "content": rq.SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    answer = rq.call_llm(messages, ar.GEN_MODEL, temperature=rq.GEN_TEMPERATURE)
    return {"answer": answer, "chunks": chunks}


@contextlib.contextmanager
def _production_llm_routing():
    """暫時把 `rq.call_llm` 還原成 rag_query.py 原生（Groq）路由，離開時還回去。

    非做不可的理由：`rq.retrieve` 內部的 filter/rewrite/translate 是用**裸名** `call_llm(...)`
    呼叫 module 全域，會吃到當下被 patch 的版本。`--module agentic_rag_nv` 下那個全域是 NVIDIA 路由，
    不還原的話「生產對照組」會整條偷偷跑在 NVIDIA + GLM 上，量到的根本不是使用者實際在用的東西。"""
    patched = rq.call_llm
    rq.call_llm = _PROD_CALL_LLM
    try:
        yield
    finally:
        rq.call_llm = patched


def run_production_single_pass(query: str) -> dict:
    """真·生產對照：完全照 `rag_query.py` 自己的預設跑單 pass（Groq + DEFAULT_MODEL /
    DEFAULT_GEN_MODEL），**不受 --module 影響**。

    與 `run_baseline_single_pass` 的分工（兩者回答的是不同問題，別混為一談）：
      - baseline   ：沿用被評估模組的模型 → 「模型固定、只變架構」的乾淨 A/B（agentic vs 單 pass）
      - production ：固定生產設定       → 「新做法比我現在線上跑的好嗎」
    """
    bge_m3, rerank_model, client = ar._get_models()
    with _production_llm_routing():
        chunks, note = rq.retrieve(
            query, bge_m3, rerank_model, client, top_k=rq.DEFAULT_TOP_K,
            model_name=rq.DEFAULT_MODEL, enable_rewrite=True, translate_query_en=True,
        )
        if not chunks:
            return {"answer": "I don't have enough information in my knowledge base to answer this.",
                    "chunks": []}
        user_prompt = rq.build_user_prompt(query, chunks, note)
        messages = [
            {"role": "system", "content": rq.SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        answer = rq.call_llm(messages, rq.DEFAULT_GEN_MODEL, temperature=rq.GEN_TEMPERATURE)
    return {"answer": answer, "chunks": chunks}


def main() -> None:
    ap = argparse.ArgumentParser(description="Eval agentic_rag final-answer quality (reuses existing scorers)")
    # 2026-07-18 judge 預設從 Groq 改走 Gemini，兩個實測理由（**不是**偏好問題，是 Groq 跑不動）：
    #   ① 原預設 qwen/qwen3-32b 已被 Groq 下架 → 404 model_not_found。
    #   ② 換 Groq 現存的 qwen/qwen3.6-27b 仍全滅，因為這支 eval 的 judge payload 本來就大：
    #      - correctness：agentic 答案動輒數千字 → JSON mode 下輸出被截斷 → 400 json_validate_failed
    #      - context_recall：prompt 要塞 chunk 全文，實測 ~14k tokens > Groq free tier TPM 8000
    #        → 413 Request too large（retry 無效，請求本身就太大）
    #      這是 Groq 免費層的結構性天花板（同 CHANGELOG_AGENTIC 續四/續八對 agent 側的結論），
    #      換哪個 Groq 模型都一樣，不是模型挑錯。
    # gemini-2.5-flash 實測長答案 correctness 與滿載 context_recall 皆正常。
    # ⚠ 換 judge 會讓分數與舊結果不可直接比較——做 A/B 時務必確認兩邊同一個 judge。
    ap.add_argument("--judge-model", default="gemini-2.5-flash",
                    help="判分模型（預設 Gemini；Groq 免費層 TPM 8000 吃不下本 eval 的 judge payload）")
    ap.add_argument("--module", default="agentic_rag_mix",
                    choices=["agentic_rag_mix", "agentic_rag", "agentic_rag_nv"],
                    help="要評估哪一份 agentic 實作（預設 agentic_rag_mix，維持既有行為）")
    ap.add_argument("--judge-votes", type=int, default=3, help="逐 checkpoint 多數決票數")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 題")
    ap.add_argument("--baseline", action="store_true",
                    help="同題也跑單 pass 對照（沿用被評估模組的模型＝只變架構的 A/B）")
    ap.add_argument("--prod-baseline", action="store_true",
                    help="同題也跑『生產設定』單 pass（固定 rag_query.py 預設 + Groq，不受 --module 影響）")
    ap.add_argument("--no-validator", action="store_true",
                    help="關閉 agentic 的 coverage validator（供 A/B 對照驗證它是否真的有增益）")
    ap.add_argument("--output", default=str(ROOT / "results_agentic.json"))
    args = ap.parse_args()

    global ar
    ar = _load_agentic_module(args.module)

    ar._get_models()  # 提前載入重模型
    queries = load_queries(args.limit)
    print(f"Loaded {len(queries)} agentic eval queries | module={args.module} "
          f"| brain={ar.BRAIN_MODEL} gen={ar.GEN_MODEL} "
          f"| judge={args.judge_model} votes={args.judge_votes}\n")

    results = []
    for i, q in enumerate(queries, start=1):
        print(f"[{i}/{len(queries)}] {q['id']}: {q['query'][:60]}")
        row = {"id": q["id"], "query": q["query"]}

        # ── agentic ──
        try:
            out = ar.run_agentic(q["query"], enable_coverage_validator=not args.no_validator)
            row["agentic"] = score_answer(q, out["answer"], out["chunks"], args.judge_model, args.judge_votes)
            row["agentic"]["n_chunks"] = len(out["chunks"])
            row["agentic_answer"] = out["answer"]
            print(f"    agentic: corr={row['agentic'].get('correctness')} "
                  f"crit_miss={row['agentic'].get('critical_miss')} "
                  f"ctx_recall={row['agentic'].get('context_recall')} "
                  f"chunks={row['agentic']['n_chunks']}")
        except Exception as e:
            row["agentic"] = {"error": str(e)[:300]}
            print(f"    agentic ERROR: {str(e)[:200]}")

        # ── baseline（選用）──
        if args.baseline:
            try:
                b = run_baseline_single_pass(q["query"])
                row["baseline"] = score_answer(q, b["answer"], b["chunks"], args.judge_model, args.judge_votes)
                row["baseline"]["n_chunks"] = len(b["chunks"])
                print(f"    baseline: corr={row['baseline'].get('correctness')} "
                      f"crit_miss={row['baseline'].get('critical_miss')} "
                      f"ctx_recall={row['baseline'].get('context_recall')}")
            except Exception as e:
                row["baseline"] = {"error": str(e)[:300]}
                print(f"    baseline ERROR: {str(e)[:200]}")

        # ── 生產設定對照（選用）──
        if args.prod_baseline:
            try:
                p = run_production_single_pass(q["query"])
                row["production"] = score_answer(q, p["answer"], p["chunks"], args.judge_model, args.judge_votes)
                row["production"]["n_chunks"] = len(p["chunks"])
                print(f"    production: corr={row['production'].get('correctness')} "
                      f"crit_miss={row['production'].get('critical_miss')} "
                      f"ctx_recall={row['production'].get('context_recall')}")
            except Exception as e:
                row["production"] = {"error": str(e)[:300]}
                print(f"    production ERROR: {str(e)[:200]}")

        results.append(row)

    # ── 摘要 ──
    def _mean(rows, side, key):
        vals = [r[side][key] for r in rows if side in r and isinstance(r[side].get(key), (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "n": len(results),
        "module": args.module,
        "brain_model": ar.BRAIN_MODEL,
        "gen_model": ar.GEN_MODEL,
        "judge_model": args.judge_model,
        "judge_votes": args.judge_votes,
        "agentic_mean_correctness": _mean(results, "agentic", "correctness"),
        "agentic_mean_context_recall": _mean(results, "agentic", "context_recall"),
    }
    if args.baseline:
        summary["baseline_mean_correctness"] = _mean(results, "baseline", "correctness")
        summary["baseline_mean_context_recall"] = _mean(results, "baseline", "context_recall")
    if args.prod_baseline:
        summary["production_models"] = f"{rq.DEFAULT_MODEL} / {rq.DEFAULT_GEN_MODEL}"
        summary["production_mean_correctness"] = _mean(results, "production", "correctness")
        summary["production_mean_context_recall"] = _mean(results, "production", "context_recall")

    Path(args.output).write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nSaved → {args.output}")


if __name__ == "__main__":
    main()
