"""ablate_translate_rerank.py — 隔離 translate_query_en 對「rerank 排序品質」的確定性消融。

動機（2026-07-25）：agentic P0 修復把 retrieve 組態對齊生產（translate_query_en 常開），
單次 RAGAS A/B 顯示 colloquial 退步，但 RAGAS 六指標全是 LLM judge、單次跑、n=15，判官噪音
無法排除；且「檢索結果變了」≠「變差了」。這支只用**比對 gold `relevant` 的確定性指標**（無任何
LLM judge），乾淨回答一件事：translate ON vs OFF，對「把 gold 檔/chunk 排到前面」有沒有幫助。

translate_query_en 只動 rerank（見 rag_query.retrieve）：dense/sparse 召回用原始 query，故
pool_file_recall 兩邊必相同（sanity check）；差異只會出現在 rerank 後的 top-k。

跑法：
  .venv/Scripts/python.exe eval/ablate_translate_rerank.py --category colloquial
  .venv/Scripts/python.exe eval/ablate_translate_rerank.py --category multi_intent
  .venv/Scripts/python.exe eval/ablate_translate_rerank.py            # 全部有 gold 的題
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq
from eval_retrieval import collect_all_sources, expand_relevant
from eval_chunk_recall import judge_chunk_relevance


def _first_gold_rank(chunks: list[dict], relevant: set[str]) -> int | None:
    """第一個命中 gold 檔的 chunk 在 top-k 裡的名次（1-indexed）；沒命中回 None。"""
    for i, c in enumerate(chunks, start=1):
        if c["source"] in relevant:
            return i
    return None


def _run_condition(query: str, bge_m3, rerank_model, client, relevant: set[str],
                   top_k: int, model_name: str, translate: bool,
                   judge_model: str | None = None) -> dict:
    with contextlib.redirect_stdout(io.StringIO()):   # 靜音 retrieve 的 DEBUG
        chunks, _note, pool = rq.retrieve(
            query, bge_m3, rerank_model, client,
            top_k=top_k, model_name=model_name,
            enable_rewrite=False, translate_query_en=translate,
            return_pool=True,
        )
    pool_sources = {(p.payload or {}).get("source", "") for p in pool}
    top_sources = []
    seen = set()
    for c in chunks:
        if c["source"] not in seen:
            top_sources.append(c["source"]); seen.add(c["source"])
    # 檔內 chunk 品質（LLM judge）——sem-11 那種「對的檔已撈到、但對的 chunk 排序」效果，file-level
    # 量不到（file_recall 常飽和），只有 chunk 層級看得見。judge_model=None 時跳過（純確定性模式）。
    chunk_precision = None
    chunk_ids = [f"{c['source']}#{c['chunk_index']}" for c in chunks]
    if judge_model:
        with contextlib.redirect_stdout(io.StringIO()):
            rel = judge_chunk_relevance(query, chunks, judge_model)
        chunk_precision = sum(rel) / len(rel) if rel else 0.0
    return {
        "pool_file_recall": len(pool_sources & relevant) / len(relevant) if relevant else 0.0,
        "file_recall": len(set(top_sources) & relevant) / len(relevant) if relevant else 0.0,
        "file_precision": sum(1 for s in top_sources if s in relevant) / len(top_sources) if top_sources else 0.0,
        "first_gold_rank": _first_gold_rank(chunks, relevant),
        "top_sources": top_sources,
        "chunk_ids": chunk_ids,
        "chunk_precision": chunk_precision,
    }


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-set", default="eval/eval_set.json")
    ap.add_argument("--collection", default="us_stock_rag_edgar_exp4")
    ap.add_argument("--category", default=None, help="只跑指定類別（colloquial / multi_intent / ...）")
    ap.add_argument("--top-k", type=int, default=rq.DEFAULT_TOP_K)
    ap.add_argument("--query-model", default=rq.DEFAULT_MODEL)
    ap.add_argument("--judge", action="store_true",
                    help="開 chunk-level LLM judge（chunk_precision）——看 sem-11 那種檔內 chunk 排序效果")
    ap.add_argument("--judge-model", default=rq.DEFAULT_MODEL)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    judge_model = args.judge_model if args.judge else None

    rq.COLLECTION_NAME = args.collection

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    print(f"[INFO] collection={args.collection} top_k={args.top_k} query_model={args.query_model}")
    print("[INFO] Loading BGE-M3 + reranker + Qdrant ...")
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rerank_model = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    client = rq.make_qdrant_client()

    eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    queries = eval_set["queries"]
    if args.category:
        queries = [q for q in queries if q["category"] == args.category]
    queries = [q for q in queries if q.get("relevant")]   # 只跑有 gold 的題

    all_sources = collect_all_sources(client)
    print(f"[INFO] {len(all_sources)} source files; {len(queries)} queries with gold\n")

    acc = {"off": [], "on": []}
    print(f"{'id':>7}  {'poolR_off/on':>14}  {'fileR_off/on':>14}  {'fileP_off/on':>14}  {'rank_off/on':>12}  changed")
    traces = []
    for q in queries:
        relevant = expand_relevant(q["relevant"], all_sources)
        off = _run_condition(q["query"], bge_m3, rerank_model, client, relevant, args.top_k, args.query_model, False, judge_model)
        on  = _run_condition(q["query"], bge_m3, rerank_model, client, relevant, args.top_k, args.query_model, True, judge_model)
        acc["off"].append(off); acc["on"].append(on)
        changed = "SAME" if off["top_sources"] == on["top_sources"] else "DIFF"
        ro = off["first_gold_rank"]; rn = on["first_gold_rank"]
        cp = ""
        if judge_model:
            cp = f"  chunkP {off['chunk_precision']:.2f}/{on['chunk_precision']:.2f}"
        print(f"{q['id']:>7}  {off['pool_file_recall']:.2f}/{on['pool_file_recall']:.2f}          "
              f"{off['file_recall']:.2f}/{on['file_recall']:.2f}          "
              f"{off['file_precision']:.2f}/{on['file_precision']:.2f}          "
              f"{str(ro):>4}/{str(rn):<4}   {changed}{cp}")
        traces.append({"id": q["id"], "off": off, "on": on})

    def _mean(key, cond):
        xs = [r[key] for r in acc[cond]]
        return statistics.mean(xs) if xs else 0.0

    def _mean_rank(cond):
        xs = [r["first_gold_rank"] for r in acc[cond] if r["first_gold_rank"] is not None]
        miss = sum(1 for r in acc[cond] if r["first_gold_rank"] is None)
        return (statistics.mean(xs) if xs else None), miss

    print(f"\n{'='*70}\n{'metric':<24}{'OFF':>10}{'ON':>10}{'Δ (on-off)':>14}")
    for key in ("pool_file_recall", "file_recall", "file_precision"):
        a, b = _mean(key, "off"), _mean(key, "on")
        print(f"{key:<24}{a:>10.3f}{b:>10.3f}{b-a:>+14.3f}")
    (ra, ma), (rb, mb) = _mean_rank("off"), _mean_rank("on")
    print(f"{'mean_first_gold_rank':<24}{ra if ra else float('nan'):>10.2f}{rb if rb else float('nan'):>10.2f}"
          f"   (miss off={ma} on={mb})  越小越好")
    if judge_model:
        cpo = statistics.mean([r["chunk_precision"] for r in acc["off"]])
        cpn = statistics.mean([r["chunk_precision"] for r in acc["on"]])
        print(f"{'chunk_precision (judge)':<24}{cpo:>10.3f}{cpn:>10.3f}{cpn-cpo:>+14.3f}  ← sem-11 顆粒度在此")
    n_diff = sum(1 for t in traces if t["off"]["top_sources"] != t["on"]["top_sources"])
    print(f"\ntop-5 來源集合改變的題數：{n_diff}/{len(traces)}")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[DONE] traces → {args.output}")


if __name__ == "__main__":
    main()
