"""為 RAGAS answer_correctness 生成「黃金參考答案」（gold reference answers）。

背景（重要）：RAGAS 原始 answer_correctness 的 factual F1 分量假設 ground_truth
是「一份完整、篇幅相當的理想答案」。先前用 rubric 檢查點清單（又短又碎）當
ground_truth，導致答案裡每個「清單沒明講的正確細節」都被算成 false positive、
精確率崩塌，分數假性偏低（見對話診斷）。正解是**保留原始 RAGAS 演算法、改餵
真正的參考答案**——本腳本產生這份參考答案。

作法：對每個有 rubric 的 query，從**黃金來源檔**（eval_set 的 relevant globs）撈
chunk、用 BGE-M3 dense 相似度快速選 top-K（避開 CPU cross-encoder 的 46s 瓶頸），
用 gemini-2.5-flash 生成一份 grounded、完整的英文參考答案。參考答案獨立於系統
輸出（grounded 在黃金來源，而非系統檢索結果），避免循環。

⚠️ 生成走 rq.call_llm（'gemini-' 開頭走 Google GenAI，其餘走 NVIDIA NIM）。
⚠️ 本腳本跑在專案 .venv（需要 rag_query / qdrant / FlagEmbedding）。

執行：
    .venv/Scripts/python.exe eval/gen_reference_answers.py --output eval/reference_answers.json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

import numpy as np
from qdrant_client import models

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq

EVAL_SET_PATH = Path("eval/eval_set.json")
GOLD_TOP_K = 8           # 生成參考答案時餵幾個黃金 chunk（收斂以控 token）
CHUNK_CHAR_CAP = 1500    # 每個 chunk 餵進 context 的字元上限（控 token）
GEN_MODEL = "openai/gpt-oss-120b"   # NVIDIA NIM（見 rag_query.call_llm 的 provider 路由）

REFERENCE_SYSTEM = """You are writing a GOLD REFERENCE ANSWER for evaluating a RAG system.

Given a question and authoritative source excerpts from SEC filings / news, write a
complete, accurate, self-contained answer to THE QUESTION AS ASKED. Scope the answer to
the question: cover every fact needed to fully answer it — all of its sub-parts, every
compared entity, every requested metric — and NOTHING the question did not ask for. Do
NOT volunteer extra figures the excerpts happen to contain but the question never
requested (e.g. if asked only for revenue, do not also report gross margin, current
ratio, or other periods). This reference is the ground truth other answers are scored
against, so it must match the question's scope EXACTLY — comprehensive on what is asked,
silent on what is not — yet strictly grounded: do NOT add any fact not supported by the
excerpts.

STYLE (must match the system's own answer register so answer_correctness compares fairly):
lead with a PLAIN-LANGUAGE direct answer in the first sentence — the main conclusion phrased
for a smart non-specialist, including the key headline number when the question asks for one.
Then give the supporting details using precise financial terms and EXACT numbers with their
time period. The plain-language framing is about how you LEAD and explain; never drop, round
away, or vague-out the specific figures the excerpts provide — factual precision is required.

NUMBERS — use the EXACT figures from the SOURCE EXCERPTS, never the approximate/rounded
figure phrased inside the QUESTION itself (e.g. if the question says "850 多億美元" but the
source says "$84.75 billion", write $84.75 billion — do NOT write $850 billion). Keep every
monetary figure in the source's own unit VERBATIM: write "$84.75 billion", "$131,819 million",
"$510 million" exactly as stated.

CRITICAL — NEVER write the Chinese money unit 「億」(or 「百萬」「兆」) ANYWHERE in your
answer. Do NOT hand-convert "$X billion / $Y million" into 億 yourself: that transliteration
is error-prone ("$84.75 billion" is 847.5 億 not 84.75 億; "$54.5 billion" is 545 億 not
54.5 億) and a deterministic post-processor does the 億 conversion for you. Your job is ONLY
to preserve the source's "$X billion / $Y million" token exactly as an English figure. If a
「數字+億」 appears anywhere in your output, the answer is INVALID. (Non-monetary counts such
as 「數百萬名使用者」 are fine; this rule is about money.)

Write in {lang}, in fluent prose (not bullet points). Do not mention "the excerpts"
or cite reference numbers; just state the facts as an authoritative answer.
"""


# 偵測 LLM 是否違規把金額手轉成中文「億」：數字後（容忍空白/窄空格）緊接「億」。
# 非金額計數（如「數百萬名使用者」，數前非阿拉伯數字）不會誤觸。
import re as _re
_YI_HANDCONV_RE = _re.compile(r"[0-9][0-9,\.]*[\s  ]*億")


def gen_reference_clean(msgs: list, model: str, max_retries: int = 2) -> str:
    """生成參考答案並強制金額用英文原文（$X billion）。LLM 若手轉成「億」就退回重寫，
    讓確定性 post-processor（rq.convert_usd_units_to_yi）獨佔 億 換算，根治 billion→億
    的 10x 音譯錯與巢狀雙寫（見 CHANGELOG_AGENTIC ⑩ / eval 稽核）。"""
    txt = rq.call_llm(msgs, model, temperature=0.0)
    for _ in range(max_retries):
        if not _YI_HANDCONV_RE.search(txt or ""):
            break
        retry = msgs + [
            {"role": "assistant", "content": txt},
            {"role": "user", "content":
             "你在金額用了中文「億」。請把整段重寫一次：所有『金額』一律改用來源的英文單位"
             "原文（例如 $8.0 billion、$54.5 billion、$476 million），輸出中絕對不可再出現"
             "「億」字（換算交給後處理）；其餘文字與事實維持不變。"},
        ]
        txt = rq.call_llm(retry, model, temperature=0.0)
    return txt


# ── 確定性 backstop：**定義已於 2026-09-03 提升到 `rag_query.py`** ─────────────────
# 為什麼搬：這一層原本只活在本檔，於是**生產管線完全沒有它**——agentic 只有
# `convert_usd_units_to_yi`（對 LLM 先斬後奏的「億」結構性失明），單發管線連那支都沒有。
# 現在 `rq.finalize_answer_units` 是三層的唯一組裝點，本檔改成 import 同一份實作。
# ⚠ 本檔的呼叫順序**刻意維持原樣**（repair → convert），不改成 finalize_answer_units：
#   參考答案含 24 處人工校正，換順序等於動到 gold 的產生方式。
repair_yi_against_source = rq.repair_yi_against_source
_YI_SOLO_RE = rq._YI_SOLO_RE   # 第 336 行還在用（列出「勸不動又修不掉」的殘留億）


def detect_lang(text: str) -> str:
    """粗略偵測 zh / en：CJK 佔比高於拉丁字母的 15% 就算中文。"""
    import re
    if not text:
        return "en"
    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "Traditional Chinese" if cjk > latin * 0.15 else "English"


def build_answer_lang_map(result_paths: list[str]) -> dict:
    """從結果檔建 id → 系統答案語言，讓參考答案逐題對齊答案語言（消跨語言失真）。"""
    lang_map = {}
    for path in result_paths:
        p = Path(path)
        if not p.exists():
            continue
        for rec in json.loads(p.read_text(encoding="utf-8")).get("records", []):
            if rec.get("answer") and rec["id"] not in lang_map:
                lang_map[rec["id"]] = detect_lang(rec["answer"])
    return lang_map


def snapshot_sources(client, collection: str) -> list[str]:
    """撈出 collection 內所有 distinct source 檔名（供 glob 展開）。"""
    sources: set[str] = set()
    offset = None
    while True:
        pts, offset = client.scroll(
            collection, limit=1000, offset=offset,
            with_payload=["source"], with_vectors=False,
        )
        for p in pts:
            src = p.payload.get("source")
            if src:
                sources.add(src)
        if offset is None:
            break
    return sorted(sources)


def expand_relevant(patterns: list[str], all_sources: list[str]) -> list[str]:
    out: set[str] = set()
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            out.update(s for s in all_sources if fnmatch.fnmatch(s, pat))
        elif pat in all_sources:
            out.add(pat)
    return sorted(out)


def fetch_gold_chunks(client, collection: str, sources: list[str]) -> list[dict]:
    """撈出黃金來源檔的所有 chunk（含 dense 向量與文字）。"""
    flt = models.Filter(must=[models.FieldCondition(
        key="source", match=models.MatchAny(any=sources))])
    out, offset = [], None
    while True:
        pts, offset = client.scroll(
            collection, scroll_filter=flt, limit=500, offset=offset,
            with_payload=["document", "source", "chunk_index"], with_vectors=["dense"],
        )
        for p in pts:
            dense = p.vector.get("dense") if isinstance(p.vector, dict) else None
            if dense is None:
                continue
            out.append({
                "document": p.payload.get("document", ""),
                "source": p.payload.get("source"),
                "chunk_index": p.payload.get("chunk_index"),
                "dense": np.asarray(dense, dtype=np.float32),
            })
        if offset is None:
            break
    return out


def top_k_by_dense(query_vec: np.ndarray, chunks: list[dict], k: int) -> list[dict]:
    if not chunks:
        return []
    mat = np.stack([c["dense"] for c in chunks])
    # BGE-M3 dense 已正規化；cosine ≈ dot
    sims = mat @ query_vec
    idx = np.argsort(-sims)[:k]
    return [chunks[i] for i in idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="eval/reference_answers.json")
    ap.add_argument("--top-k", type=int, default=GOLD_TOP_K)
    ap.add_argument("--gen-model", default=GEN_MODEL)
    ap.add_argument("--ids", nargs="*", default=None, help="只跑指定 query id（測試用）")
    ap.add_argument("--force", action="store_true",
                    help="無視快取強制重生成（配 --ids 用）。用途：切塊方式變了但 query 與 "
                         "gold_files 都沒變時，快取條件 ①②③ 都攔不到（③ 只在舊檔已有 "
                         "collection 欄位時才判得出來）。⚠ 不帶 --ids 就是全量重生成，"
                         "會覆蓋掉 reference_answers.json 裡的人工校正。")
    ap.add_argument("--match-lang-from", nargs="*", default=None,
                    help="結果檔清單：逐題把參考答案語言對齊該題系統答案語言（消跨語言失真）")
    args = ap.parse_args()

    lang_map = build_answer_lang_map(args.match_lang_from) if args.match_lang_from else {}

    from FlagEmbedding import BGEM3FlagModel

    eval_set = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    # 純 RAGAS 架構（2026-07-21 起）：不再要求 rubric，只要題目 answerable 且有 relevant
    # 黃金來源檔就生成 reference（RAGAS 六指標全部只需要 reference，不需要 rubric checklist）。
    queries = [q for q in eval_set["queries"]
               if q.get("answerable", True) and q.get("relevant")]
    if args.ids:
        queries = [q for q in queries if q["id"] in args.ids]

    print(f"Loading BGE-M3 and Qdrant...")
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    client = rq.make_qdrant_client()
    col = rq.COLLECTION_NAME
    all_sources = snapshot_sources(client, col)

    # 既有結果檔可續跑：讀已生成的 reference，跳過
    out_path = Path(args.output)
    existing = {}
    if out_path.exists():
        existing = {r["id"]: r for r in json.loads(out_path.read_text(encoding="utf-8"))}

    results = dict(existing)
    for i, q in enumerate(queries, 1):
        qid = q["id"]
        # query 要逐字相符才信任快取——eval_set 改版常沿用相同 id 換題目，若只看 id
        # 存在就 skip，會把舊題目的 reference 誤配到新題目上（見 2026-07-21 事故：
        # 47/90 題撈到文不對題的舊 reference，RAGAS context_recall/precision 假性腰斬）。
        gold_files = expand_relevant(q["relevant"], all_sources)
        if not gold_files:
            print(f"[{i}/{len(queries)}] {qid} — no gold sources matched {q['relevant']}, skip")
            continue
        cached = existing.get(qid)
        # 快取有效需**兩個**條件同時成立，缺一不可：
        #   ① query 逐字相符——eval_set 改版常沿用相同 id 換題目，只看 id 存在就 skip 會把
        #      舊題目的 reference 誤配到新題目（2026-07-21 事故：47/90 題文不對題，
        #      RAGAS context_recall/precision 假性腰斬）。
        #   ② gold_files 相同——語料換版時 glob 展開到不同檔案（例如 SEC 新申報讓
        #      `MSFT_10K_*.html` 從 2025 版變 2026 版），題目一個字沒改，但參考答案是照
        #      舊 filing 寫的。只驗 query 會讓這種漂移完全無聲通過，正是 memory
        #      `eval-gold-version-drift-bug` 記載的病（該次「最新一季」題假性腰斬）。
        #   ③ collection 相同（2026-08-08 加）——①② 都是**檔案層級**的比較，對「同一份
        #      檔案、切塊方式變了」完全無感。期間章節硬邊界（`_split_by_period_section`）
        #      正是這種改動：gold_files 一個字沒變，但 fetch_gold_chunks 撈到的 chunk 邊界
        #      與內容都不同了。少了這條，換 collection 重生成 gold 時受影響的題會被靜默
        #      skip、沿用舊切塊寫出來的答案——與 ② 修的是同一類病，只是換一個層級發生。
        if cached and cached.get("reference") and not args.force:
            same_q = cached.get("query", "").strip() == q["query"].strip()
            # 比對必須**順序不敏感**：expand_relevant 現在回 sorted()，但舊快取裡存的是
            # 更早版本寫下的未排序值（來源是 Qdrant scroll 的任意順序）。用 list== 比會把
            # 「同一組檔案、順序不同」誤判成語料換版 → 白白重生成一題 gold。
            # 2026-08-08 實測踩到：mh-09 的 gold 集合完全相同（GOOGL News + Fundamentals），
            # 只因順序相反就被重寫，參考答案從 356 字變成 290 字。
            same_gold = sorted(cached.get("gold_files") or []) == sorted(gold_files)
            # 舊檔沒有 collection 欄位（本次之前生成的）→ 視為相同，不強迫全量重跑
            _cached_col = cached.get("collection")
            same_col = (_cached_col is None) or (_cached_col == col)
            if same_q and same_gold and same_col:
                print(f"[{i}/{len(queries)}] {qid} — cached, skip")
                continue
            why = ("query changed" if not same_q else
                   "gold_files changed（語料換版）" if not same_gold else
                   f"collection changed（{_cached_col} → {col}，切塊可能已變）")
            print(f"[{i}/{len(queries)}] {qid} — cache STALE（{why}），regenerating")
            if not same_gold:
                _was, _now = set(cached.get("gold_files") or []), set(gold_files)
                print(f"       舊 gold: {sorted(_was - _now) or '—'} → 新 gold: {sorted(_now - _was) or '—'}")
        chunks = fetch_gold_chunks(client, col, gold_files)
        # dense 選 chunk 用「英文譯句」而非原始中文 query：黃金來源多為英文 SEC filing，
        # 中文 query × 英文 chunk 的跨語言 dense 排序會失真，把含事實那顆壓下去（實測：
        # lex-04 CUDA-2006 在 #20 卻落選、col-03 Anthropic 在 10-K #75 卻落選 → reference
        # 誤寫「來源未揭露」＝ false-negative gold）。對齊生產 full_translate_en 的作法，
        # 檢索階段一律用英文譯句；reference 生成語言仍照 lang_map（與問法語言無關）。
        retr_q = rq.translate_query_to_english(q["query"], args.gen_model)
        qvec = np.asarray(bge.encode([retr_q])["dense_vecs"][0], dtype=np.float32)
        top = top_k_by_dense(qvec, chunks, args.top_k)
        # 手動釘選 chunk（季度分部/損益表 chunk 數字密集、語意 dense 分數天生輸給敘述段，
        # dense 選擇器會漏掉真正含答案的表格 → false-negative gold；見 CHANGELOG_AGENTIC ⑨
        # eval 稽核）。pin_chunks 一律納入（優先、可超過 top_k），dense top-k 補足其餘。
        pins = q.get("pin_chunks") or []
        if pins:
            pinset = {(p["source"], p["chunk_index"]) for p in pins}
            pinned = [c for c in chunks if (c["source"], c["chunk_index"]) in pinset]
            # pin 比對不到就靜默消失＝無聲退回 dense-only 選擇，正是 pin 當初要修的
            # false-negative gold 又跑回來，而且沒有任何跡象。必須吵出來。
            # 典型成因：語料刷新後該 filing 被更新版取代（例如 SEC 新申報把 10-K_2025
            # 換成 10-K_2026），或 chunking 改版導致 chunk_index 位移。
            missing = pinset - {(c["source"], c["chunk_index"]) for c in pinned}
            if missing:
                print(f"[{i}/{len(queries)}] {qid} — ⚠ pin_chunks 有 {len(missing)}/{len(pinset)} 個"
                      f"在目前語料中找不到，該題 gold 會退回 dense-only 選擇："
                      + ", ".join(f"{s}#{ix}" for s, ix in sorted(missing)))
            seen = {(c["source"], c["chunk_index"]) for c in pinned}
            top = pinned + [c for c in top if (c["source"], c["chunk_index"]) not in seen]
            top = top[:max(args.top_k, len(pinned))]
        context = "\n\n---\n\n".join(c["document"][:CHUNK_CHAR_CAP] for c in top)
        user = f"Question:\n{q['query']}\n\nSource excerpts:\n{context}"
        # 語言決定順序：--match-lang-from 對照 > 既有 reference 的語言 > 英文。
        # 中間那層是必要的：現有 100 題全是 Traditional Chinese（見「全繁中對齊」commit），
        # 若忘了帶 --match-lang-from 就直接落到 "English"，重生成的題會安靜地變英文，
        # 與其餘題目 register 不一致——而 register 落差正是壓垮 answer_correctness 的
        # 已知主因（見 CHANGELOG_AGENTIC A1「答案風格必須與 SYSTEM_PROMPT 一致」）。
        # 2026-08-07 實測踩到：未帶參數重生成 news-01/mi-13，兩題都無聲產出英文。
        lang = lang_map.get(qid) or (cached or {}).get("reference_lang") or "English"
        if qid not in lang_map and not (cached or {}).get("reference_lang"):
            print(f"[{i}/{len(queries)}] {qid} — ⚠ 無語言對照也無既有 reference，"
                  f"預設用 English；若其餘題目是中文請補 --match-lang-from")
        msgs = [{"role": "system", "content": REFERENCE_SYSTEM.format(lang=lang)},
                {"role": "user", "content": user}]
        # rq.call_llm 依 model 名稱路由：'gemini-' 開頭走 Google GenAI，其餘走 NVIDIA NIM。
        # gen_reference_clean 內含「億」違規偵測 + 重試護欄（強制金額留英文原文）。
        reference = gen_reference_clean(msgs, args.gen_model)
        if _YI_HANDCONV_RE.search(reference or ""):
            # 勸不動就用程式修：拿 context 逐筆判定，確定是 10x 音譯錯的才改（見上方判準）。
            reference, _fixes = repair_yi_against_source(reference, context)
            for _old, _new in _fixes:
                print(f"[{i}/{len(queries)}] {qid} — ✔ backstop 修正單位：「{_old.strip()}」→「{_new}」")
            if _YI_HANDCONV_RE.search(reference or ""):
                # 仍有手轉「億」但來源判不出＝可能正確（來源是 millions 表格自行組單位）。
                # 不亂改，但要留痕，別再像先前那樣一行警告就放行。
                _left = {m.group(0).strip() for m in _YI_SOLO_RE.finditer(reference)}
                print(f"[{i}/{len(queries)}] {qid} — ⚠ 仍有無法從來源判定的手轉「億」"
                      f"（{len(_left)} 處，未修改）：{'、'.join(sorted(_left))}")
        # 確定性單位換算（與系統 synthesize 同一支 rq.convert_usd_units_to_yi）：reference prompt
        # 要 LLM 保留 "$X billion" verbatim，這裡把 billion/million→億 乘算正確並雙寫「億（$X billion）」，
        # 對齊系統答案格式 + 根治中文 reference 的 billion→億 音譯 10x 錯（見 CHANGELOG_AGENTIC ⑨）。
        reference = rq.convert_usd_units_to_yi(reference.strip())
        results[qid] = {
            "id": qid,
            "category": q["category"],
            "query": q["query"],
            "gold_files": gold_files,
            "collection": col,   # 供快取條件 ③ 判定：換 collection＝切塊可能已變，要重生成
            "gold_chunks_used": [{"source": c["source"], "chunk_index": c["chunk_index"]} for c in top],
            "reference_lang": lang,
            "reference": reference.strip(),
        }
        print(f"[{i}/{len(queries)}] {qid} — {len(gold_files)} gold files, "
              f"{len(chunks)} chunks → ref {len(reference)} chars")
        # 逐題存檔（生成貴，避免中途失敗全丟）
        out_path.write_text(
            json.dumps(list(results.values()), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nSaved {len(results)} references → {out_path}")


if __name__ == "__main__":
    main()
