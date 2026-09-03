"""
eval_chunk_recall.py — Filter vs No-filter:file-level + chunk-level recall/precision

對照「query-understanding hard filter」（rag_query.parse_query_filters）開／關
對檢索品質的影響，分兩個粒度量：

  - file-level（粗）：沿用 eval_retrieval.py 的 source-level precision/recall——
    retrieve() 吐出的 top-k chunk 去重成來源檔名，跟 eval_set.json 的 relevant
    比對。只看「有沒有撈到對的檔案」。

  - chunk-level（細，用 LLM judge）：
      * chunk precision —— 對 retrieve() 實際吐出的每個 chunk，問 LLM「這段有沒
        有幫助回答這題」，算 relevant 比例。
      * checkpoint recall —— 對 eval_set.json 既有的 rubric.must_include
        checkpoints，問 LLM「這些 retrieved chunks 合起來有沒有提供足夠證據」，
        算（依 weight 加權的）覆蓋率。這是 file-level recall 量不到的東西：
        即使選對了檔案，檔案裡 86 個 chunk 也只有幾個真正答得了問題。

為什麼不用靜態 chunk-level ground truth（手動標哪些 chunk index 相關）：
  corpus 一旦 re-chunk / re-ingest，chunk id/index 全部改變，靠 index 標記的
  ground truth 立刻失效，維護成本隨檔案數線性增加。LLM judge 在 eval 當下才
  判斷，對 re-ingest 免疫，而且直接重用 eval_set.json 既有的 rubric，不需要
  額外標註——這是 RAGAS / ARES 等業界 RAG eval 框架（Context Precision /
  Context Recall）的標準做法。

只有 rubric != null 的題目才算 checkpoint recall（純檔名 fetch 題，如
lex-13~21，沒有 checkpoint，只看 file-level + chunk precision）。

Usage:
  python eval/eval_chunk_recall.py
  python eval/eval_chunk_recall.py --limit 10              # 先跑前 10 題試算 LLM 成本
  python eval/eval_chunk_recall.py --judge-model openai/gpt-oss-120b
"""

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq
from eval_retrieval import collect_all_sources, expand_relevant

CONDITIONS = ["nofilter", "filter"]
MAX_PASSAGE_CHARS = 800   # 截斷每段送進 judge 的長度，控制 token 成本


# ══════════════════════════════════════════════════════════════════════════════
# LLM judges
# ══════════════════════════════════════════════════════════════════════════════

CHUNK_RELEVANCE_JUDGE_PROMPT = """\
You judge whether each retrieved passage is useful for answering a financial question \
about a US public company (10-K/10-Q filings, fundamentals, or news).

For EACH numbered passage, decide if it contains information that helps answer the \
question — even partially (a related figure, the right company/period, or relevant \
qualitative content counts as relevant).

Output ONLY a JSON object: {"relevant": [true, false, ...]} with exactly one boolean \
per passage, in the same order as given. No markdown, no explanation.
"""

CHECKPOINT_COVERAGE_JUDGE_PROMPT = """\
You judge whether a set of retrieved passages collectively provide enough evidence to \
satisfy each checkpoint of an answer rubric for a financial question.

For EACH numbered checkpoint, decide if the passages contain sufficient evidence (a \
specific figure, fact, or statement) to satisfy it — partial / approximate matches count \
as covered.

Output ONLY a JSON object: {"covered": [true, false, ...]} with exactly one boolean per \
checkpoint, in the same order as given. No markdown, no explanation.
"""


def _parse_json_list(raw: str, key: str, n: int) -> list[bool]:
    # qwen3-32b（等推理模型）在 content 裡夾帶 <think>...</think> 推理過程，JSON 才接在後面；
    # 不先剝掉會讓 json.loads 卡在第一個字元（"Expecting value: line 1 column 1"）。
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    parsed = json.loads(raw)
    vals = parsed.get(key, []) if isinstance(parsed, dict) else []
    vals = (list(vals) + [False] * n)[:n]
    return [bool(v) for v in vals]


def judge_chunk_relevance(query: str, chunks: list[dict], model_name: str) -> list[bool]:
    if not chunks:
        return []
    passages = "\n\n".join(
        f"[{i + 1}] {c['content'][:MAX_PASSAGE_CHARS]}" for i, c in enumerate(chunks)
    )
    user = f"Question: {query}\n\nPassages:\n{passages}"
    try:
        raw = rq.call_llm(
            [{"role": "system", "content": CHUNK_RELEVANCE_JUDGE_PROMPT},
             {"role": "user", "content": user}],
            model_name,
        )
        return _parse_json_list(raw, "relevant", len(chunks))
    except Exception as e:
        print(f"    [WARN] chunk relevance judge failed: {e!r}")
        return [False] * len(chunks)


def judge_checkpoint_coverage(query: str, checkpoints: list[dict], chunks: list[dict],
                               model_name: str) -> list[bool]:
    if not checkpoints or not chunks:
        return [False] * len(checkpoints)
    context = "\n\n".join(
        f"--- passage {i + 1} ---\n{c['content'][:MAX_PASSAGE_CHARS]}"
        for i, c in enumerate(chunks)
    )
    cps = "\n".join(f"[{i + 1}] {cp['checkpoint']}" for i, cp in enumerate(checkpoints))
    user = f"Question: {query}\n\nCheckpoints:\n{cps}\n\nPassages:\n{context}"
    try:
        raw = rq.call_llm(
            [{"role": "system", "content": CHECKPOINT_COVERAGE_JUDGE_PROMPT},
             {"role": "user", "content": user}],
            model_name,
        )
        return _parse_json_list(raw, "covered", len(checkpoints))
    except Exception as e:
        print(f"    [WARN] checkpoint coverage judge failed: {e!r}")
        return [False] * len(checkpoints)


# ══════════════════════════════════════════════════════════════════════════════
# Evaluation runner
# ══════════════════════════════════════════════════════════════════════════════

def evaluate(eval_set: dict, client, bge_m3, rerank_model, top_k: int,
             judge_model: str, query_model: str, limit: int | None = None,
             enable_rewrite: bool = False, category: str | None = None,
             rewrite_merge_top_n: int | None = None,
             rewrite_fusion: bool = False,
             translate_query_en: bool = False) -> dict:
    queries = eval_set["queries"]
    if category:
        queries = [q for q in queries if q["category"] == category]
    if limit:
        queries = queries[:limit]

    print("[INFO] Snapshotting source filenames for glob expansion...")
    all_sources = collect_all_sources(client)
    print(f"[INFO] Found {len(all_sources)} unique source files in collection.\n")

    file_acc  = {c: defaultdict(list) for c in CONDITIONS}
    chunk_acc = {c: defaultdict(list) for c in CONDITIONS}
    traces    = []
    over_filter_hits = []   # queries where filter hurt recall vs nofilter

    for q in queries:
        qid, category, query_str, raw_patterns = q["id"], q["category"], q["query"], q["relevant"]
        relevant = expand_relevant(raw_patterns, all_sources)
        rubric = q.get("rubric")
        checkpoints = (rubric or {}).get("must_include", []) if rubric else []

        entry = {"id": qid, "category": category, "query": query_str, "conditions": {}}
        print(f"  [{qid:>7}] ({category:8}) {query_str[:55]}")

        for cond in CONDITIONS:
            chunks, fallback_note, pool = rq.retrieve(
                query_str, bge_m3, rerank_model, client,
                top_k=top_k, model_name=query_model,
                disable_filter=(cond == "nofilter"),
                enable_rewrite=enable_rewrite,
                rewrite_merge_top_n=rewrite_merge_top_n,
                rewrite_fusion=rewrite_fusion,
                translate_query_en=translate_query_en,
                return_pool=True,
            )

            # Stage 1：精排前候選池的 file recall
            pool_sources = {(p.payload or {}).get("source", "") for p in pool}
            pool_file_recall = (len(pool_sources & relevant) / len(relevant)
                                if relevant else 0.0)

            unique_srcs, seen = [], set()
            for c in chunks:
                if c["source"] not in seen:
                    unique_srcs.append(c["source"])
                    seen.add(c["source"])

            file_precision = (sum(1 for s in unique_srcs if s in relevant) / len(unique_srcs)
                               if unique_srcs else 0.0)
            file_recall    = (len(set(unique_srcs) & relevant) / len(relevant)
                               if relevant else 0.0)

            chunk_relevant  = judge_chunk_relevance(query_str, chunks, judge_model)
            chunk_precision = sum(chunk_relevant) / len(chunk_relevant) if chunk_relevant else 0.0

            checkpoint_recall = None
            critical_miss     = None
            if checkpoints:
                covered = judge_checkpoint_coverage(query_str, checkpoints, chunks, judge_model)
                weights = [cp.get("weight", 1) for cp in checkpoints]
                total_w = sum(weights) or 1
                checkpoint_recall = sum(w for w, c in zip(weights, covered) if c) / total_w
                critical_miss = any(cp.get("is_critical") and not c
                                     for cp, c in zip(checkpoints, covered))

            entry["conditions"][cond] = {
                "sources":           unique_srcs,
                "pool_size":         len(pool),
                "pool_file_recall":  round(pool_file_recall, 4),
                "file_precision":    round(file_precision, 4),
                "file_recall":       round(file_recall, 4),
                "chunk_precision":   round(chunk_precision, 4),
                "checkpoint_recall": round(checkpoint_recall, 4) if checkpoint_recall is not None else None,
                "critical_miss":     critical_miss,
                "fallback_note":     fallback_note,
            }

            file_acc[cond]["pool_file_recall"].append(pool_file_recall)
            file_acc[cond]["precision"].append(file_precision)
            file_acc[cond]["recall"].append(file_recall)
            chunk_acc[cond]["chunk_precision"].append(chunk_precision)
            if checkpoint_recall is not None:
                chunk_acc[cond]["checkpoint_recall"].append(checkpoint_recall)
                chunk_acc[cond]["critical_miss_rate"].append(1.0 if critical_miss else 0.0)

            print(f"            {cond:8} poolR={pool_file_recall:.2f}(n={len(pool):>2})  "
                  f"file_P={file_precision:.2f} file_R={file_recall:.2f}  "
                  f"chunk_P={chunk_precision:.2f}  "
                  + (f"ckpt_R={checkpoint_recall:.2f}" + (" ⚠CRIT-MISS" if critical_miss else "")
                     if checkpoint_recall is not None else "ckpt_R=n/a"))

        nf, ft = entry["conditions"]["nofilter"], entry["conditions"]["filter"]
        recall_drop = nf["file_recall"] - ft["file_recall"]
        ckpt_drop   = (None if nf["checkpoint_recall"] is None or ft["checkpoint_recall"] is None
                        else nf["checkpoint_recall"] - ft["checkpoint_recall"])
        if recall_drop > 0.01 or (ckpt_drop is not None and ckpt_drop > 0.01):
            over_filter_hits.append({"id": qid, "query": query_str,
                                      "file_recall_drop": round(recall_drop, 4),
                                      "checkpoint_recall_drop": round(ckpt_drop, 4) if ckpt_drop is not None else None})

        traces.append(entry)

    def _mean(xs):
        return round(statistics.mean(xs), 4) if xs else None

    summary = {
        cond: {
            "pool_file_recall":    _mean(file_acc[cond]["pool_file_recall"]),
            "file_precision":      _mean(file_acc[cond]["precision"]),
            "file_recall":         _mean(file_acc[cond]["recall"]),
            "chunk_precision":     _mean(chunk_acc[cond]["chunk_precision"]),
            "checkpoint_recall":   _mean(chunk_acc[cond]["checkpoint_recall"]),
            "critical_miss_rate":  _mean(chunk_acc[cond]["critical_miss_rate"]),
            "n_queries":           len(file_acc[cond]["precision"]),
            "n_with_checkpoints":  len(chunk_acc[cond]["checkpoint_recall"]),
        }
        for cond in CONDITIONS
    }

    return {
        "summary":          summary,
        "traces":           traces,
        "over_filter_hits": over_filter_hits,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════════

def print_report(report: dict) -> None:
    summary = report["summary"]
    width = 78
    print(f"\n{'═' * width}")
    print("FILTER vs NO-FILTER — file-level (粗) + chunk-level LLM-judge (細)")
    print(f"{'═' * width}\n")

    nf, ft = summary["nofilter"], summary["filter"]
    rows = [
        ("pool_file_recall",   "Pool file recall [Stage 1: 精排前候選池]"),
        ("file_precision",     "File precision   [Stage 2: top-k 來源中對的比例]"),
        ("file_recall",        "File recall      [Stage 2: relevant 檔案有撈到的比例]"),
        ("chunk_precision",    "Chunk precision  [Stage 2: chunk 中 judge=relevant 比例]"),
        ("checkpoint_recall",  "Checkpoint recall [Stage 2: rubric 加權覆蓋率]"),
        ("critical_miss_rate", "Critical-miss rate (越低越好)"),
    ]
    print(f"  {'Metric':<48}{'nofilter':>10}{'filter':>10}{'Δ':>8}")
    print(f"  {'-' * 48}{'-' * 10}{'-' * 10}{'-' * 8}")
    for key, label in rows:
        a, b = nf.get(key), ft.get(key)
        if a is None or b is None:
            print(f"  {label:<48}{'n/a':>10}{'n/a':>10}{'':>8}")
            continue
        d = b - a
        print(f"  {label:<48}{a*100:9.1f}%{b*100:9.1f}%{d*100:+7.1f}pp")
    print(f"\n  n_queries={nf['n_queries']}  n_with_checkpoints={nf['n_with_checkpoints']}\n")

    over_filter = report["over_filter_hits"]
    if over_filter:
        print(f"{'─' * width}")
        print(f"⚠ OVER-FILTER WARNING — {len(over_filter)} 題 filter 後 recall 反而比 nofilter 差")
        print("(代表 hard filter 猜錯/太嚴，把含答案的 chunk 排除掉了——比 retriever 選型重要得多)")
        print(f"{'─' * width}")
        for h in over_filter:
            ckpt = f", checkpoint_recall_drop={h['checkpoint_recall_drop']:+.2f}" if h["checkpoint_recall_drop"] is not None else ""
            print(f"  [{h['id']}] {h['query'][:50]}  file_recall_drop={h['file_recall_drop']:+.2f}{ckpt}")
        print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Filter vs no-filter: file-level + chunk-level (LLM judge) recall/precision",
    )
    parser.add_argument("--eval-set", default="eval/eval_set.json")
    parser.add_argument("--output", default="eval/results_chunk_recall.json")
    parser.add_argument("--top-k", type=int, default=rq.DEFAULT_TOP_K)
    parser.add_argument("--judge-model", default=rq.DEFAULT_MODEL,
                        help="LLM used for chunk-relevance / checkpoint-coverage judging")
    parser.add_argument("--query-model", default=rq.DEFAULT_MODEL,
                        help="LLM used inside rag_query.retrieve() for query-understanding filter")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only run the first N queries (cost control while iterating)")
    parser.add_argument("--rewrite", action="store_true",
                        help="Enable rag_query's query-rewrite recall expansion (enable_rewrite=True)")
    parser.add_argument("--category", default=None,
                        help="Only run queries of this category (e.g. colloquial)")
    parser.add_argument("--rewrite-merge-top-n", type=int, default=None,
                        help="Cap how many candidates each rewrite variant merges into the pool")
    parser.add_argument("--rewrite-fusion", action="store_true",
                        help="跨變體 RRF-fusion（RAG-Fusion）合併，取代 max-score 合併；與 --rewrite 併用")
    parser.add_argument("--translate-query-en", action="store_true",
                        help="rerank 對每個候選同時用原句與英譯各評一次分、取逐候選最高分（見 rag_query.retrieve 說明）")
    args = parser.parse_args()

    eval_path = Path(args.eval_set)
    with open(eval_path, "r", encoding="utf-8") as f:
        eval_set = json.load(f)
    print(f"[INFO] Loaded {len(eval_set['queries'])} queries from {eval_path}")

    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    print(f"[INFO] Loading BGE-M3: {rq.EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)

    print(f"[INFO] Loading reranker: {rq.RERANK_MODEL}")
    rerank_model = CrossEncoder(rq.RERANK_MODEL)

    client = rq.make_qdrant_client()

    try:
        total = client.count(collection_name=rq.COLLECTION_NAME, exact=True).count
    except Exception as e:
        print(f"[ERROR] Cannot access collection '{rq.COLLECTION_NAME}': {e}")
        sys.exit(1)
    if total == 0:
        print(f"[ERROR] Collection is empty.")
        sys.exit(1)
    print(f"[INFO] Collection size: {total} chunks")
    print(f"[INFO] top_k={args.top_k}  judge_model={args.judge_model}  query_model={args.query_model}  "
          f"rewrite={args.rewrite}  translate_query_en={args.translate_query_en}\n")

    report = evaluate(eval_set, client, bge_m3, rerank_model, args.top_k,
                       args.judge_model, args.query_model, limit=args.limit,
                       enable_rewrite=args.rewrite, category=args.category,
                       rewrite_merge_top_n=args.rewrite_merge_top_n,
                       rewrite_fusion=args.rewrite_fusion,
                       translate_query_en=args.translate_query_en)

    print_report(report)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[DONE] Detailed traces written to: {out_path}")


if __name__ == "__main__":
    main()
