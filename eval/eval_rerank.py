"""
eval_rerank.py — 兩段式檢索評估（Retrieval Recall → Rerank Recall/MRR）

評估 production pipeline（us_stock_rag_unstructured）的兩個階段，專注在 Recall 與 MRR：

  Stage 1 — Retrieval (Qdrant hybrid + server-side RRF)
    ─ 衡量「相關文件有沒有被撈進候選池」→ 以 Recall@{5,10,20} 為主。
      這是整條 pipeline 的天花板：reranker 永遠無法救回 RRF 沒撈到的文件。

  Stage 2 — Rerank (BGE-reranker-v2-m3 CrossEncoder)
    ─ 對「同一個」RRF 候選池重排，衡量「有沒有把相關文件往前推」
      → Recall@{1,3,5,10} + MRR。
      關鍵：rerank 不改變候選集合、只改順序，所以它的貢獻只會顯現在
      小 k 的 Recall 與 MRR 上；Recall@(pool size) 在重排前後必然相同。

輸出兩張表：
  (A) RETRIEVAL — RRF 階段的 Recall 天花板
  (B) RERANK    — RRF 順序 vs CrossEncoder 順序，並排比較 Recall/MRR + Δ

Ground-truth 粒度與其餘 eval 一致：source 檔名（任一 chunk 命中該檔即算 hit），
glob pattern（NVDA_News_*.txt）依當前 collection 的實際 source 展開。

Usage:
  python eval/eval_rerank.py
  python eval/eval_rerank.py --pool 50              # 每題 rerank 多少 chunk（候選池大小）
  python eval/eval_rerank.py --collection us_stock_rag_unstructured
  python eval/eval_rerank.py --output eval/results_rerank.json
"""

import argparse
import fnmatch
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 純函式（不依賴任何 collection 名稱）直接沿用主 eval harness，確保指標定義一致
from eval.eval_retrieval import (  # noqa: E402
    _has_glob,
    recall_at_k,
    precision_at_k,
    reciprocal_rank_at_k,
    fmt_pct,
)
from rag_query import _extract_structural_filters, build_qdrant_filter, rewrite_query  # noqa: E402

# ── Config ──────────────────────────────────────────────────────────────────────
QDRANT_PATH        = os.getenv("QDRANT_PATH", "./qdrant_db")
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
RERANK_MODEL       = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
COLLECTION_NAME    = "us_stock_rag_unstructured"   # production collection（rag_query.py 讀的那個）
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"

PER_LANE_FETCH     = 100   # 每路（dense / sparse）prefetch 取多少 chunk 進 RRF
DEFAULT_POOL       = 50    # RRF 融合後保留多少 chunk 當作候選池（= rerank 的輸入量）

# Recall 評估的 k 值。小 k（1/3/5）凸顯 rerank 的貢獻；大 k（20）逼近 retrieval 天花板。
RECALL_KS_RETRIEVAL = [5, 10, 20]
RECALL_KS_RERANK    = [1, 3, 5, 10]
MRR_HORIZONS        = [5, 10]   # MRR@5 貼近 top-k 使用情境；MRR@10 對齊業界標準

CATEGORIES = ["semantic", "lexical", "mixed", "overall"]
STAGES     = ["rrf", "rerank"]


# ══════════════════════════════════════════════════════════════════════════════
# Collection-scoped helpers（eval_retrieval 的版本 close over 它自己的 COLLECTION_NAME，
# 故這裡保留本地副本，綁定本檔的 COLLECTION_NAME）
# ══════════════════════════════════════════════════════════════════════════════

def collect_all_sources(client) -> set[str]:
    sources: set[str] = set()
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=256,
            offset=next_offset,
            with_payload=["source"],
            with_vectors=False,
        )
        for p in points:
            src = (p.payload or {}).get("source")
            if src:
                sources.add(src)
        if next_offset is None:
            break
    return sources


def expand_relevant(patterns: list[str], all_sources: set[str]) -> set[str]:
    expanded: set[str] = set()
    for pat in patterns:
        if _has_glob(pat):
            expanded |= set(fnmatch.filter(all_sources, pat))
        else:
            expanded.add(pat)
    return expanded


def unique_sources_in_order(points_or_payloads) -> list[str]:
    """把 chunk 層級命中收斂成 source 層級，保留首次出現的排名。
    接受 Qdrant point（有 .payload）或直接是 payload dict 的串列。"""
    seen, seen_set = [], set()
    for item in points_or_payloads:
        payload = getattr(item, "payload", item) or {}
        src = payload.get("source")
        if src and src not in seen_set:
            seen.append(src)
            seen_set.add(src)
    return seen


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1 — Hybrid retrieval（server-side RRF），回傳帶 source + document 的候選池
# ══════════════════════════════════════════════════════════════════════════════

def hybrid_rrf_pool(client, q_dense, s_idx, s_val, pool: int, query_filter=None):
    from qdrant_client import models
    return client.query_points(
        collection_name=COLLECTION_NAME,
        prefetch=[
            models.Prefetch(query=q_dense, using=DENSE_VECTOR_NAME, limit=PER_LANE_FETCH,
                            filter=query_filter),
            models.Prefetch(
                query=models.SparseVector(indices=s_idx, values=s_val),
                using=SPARSE_VECTOR_NAME, limit=PER_LANE_FETCH,
                filter=query_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=pool,
        with_payload=["source", "document"],   # document 供 reranker 打分用
    ).points


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — CrossEncoder rerank（與 rag_query.retrieve 完全相同的打分/排序邏輯）
# ══════════════════════════════════════════════════════════════════════════════

def rerank_pool(rerank_model, query: str, rrf_points, rerank_query: str | None = None) -> list[dict]:
    """對 RRF 候選池逐一用 cross-encoder 打分，回傳依分數重排後的 payload 串列。
    rerank_query 若提供則用它做 rerank（可以是改寫後的術語），否則用原始 query。"""
    q = rerank_query if rerank_query else query
    docs = [(p.payload or {}).get("document", "") for p in rrf_points]
    cross_input = [[q, d] for d in docs]
    raw_scores = rerank_model.predict(cross_input)
    raw_scores = raw_scores.tolist() if hasattr(raw_scores, "tolist") else list(raw_scores)

    scored = list(zip(rrf_points, raw_scores))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(p.payload or {}) for p, _ in scored]


# ══════════════════════════════════════════════════════════════════════════════
# Evaluation
# ══════════════════════════════════════════════════════════════════════════════

def evaluate(eval_set, client, bge_m3, rerank_model, pool: int, with_filter: bool = False,
             enable_rewrite: bool = False, model_name: str = "gemini-2.5-flash") -> dict:
    queries = eval_set["queries"]

    print("[INFO] Snapshotting source filenames for glob expansion...")
    all_sources = collect_all_sources(client)
    print(f"[INFO] Found {len(all_sources)} unique source files in collection.\n")

    # results[stage][category][metric] = list of per-query scores
    # 用 defaultdict 讓未知 category（如 colloquial）自動建立
    results = {
        stage: defaultdict(lambda: defaultdict(list))
        for stage in STAGES
    }
    for stage in STAGES:
        for cat in CATEGORIES:
            _ = results[stage][cat]  # pre-initialize known categories
    traces = []
    skipped_queries: list[str] = []

    all_ks = sorted(set(RECALL_KS_RETRIEVAL) | set(RECALL_KS_RERANK))

    for q in queries:
        qid       = q["id"]
        category  = q["category"]
        query_str = q["query"]
        raw_patterns = q["relevant"]

        relevant = expand_relevant(raw_patterns, all_sources)
        if not relevant:
            print(f"  [{qid}] [SKIP] no relevant sources after glob expansion "
                  f"(patterns: {raw_patterns})")
            skipped_queries.append(qid)
            continue

        # ── 編碼 query → dense + sparse（BGE-M3 一次出兩種向量）──────────────
        encoded = bge_m3.encode(
            [query_str], return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        q_dense = encoded["dense_vecs"][0].tolist()
        q_sp    = encoded["lexical_weights"][0]
        s_idx   = [int(tok) for tok in q_sp.keys()]
        s_val   = [float(w)  for w   in q_sp.values()]

        # ── Stage 1: RRF 候選池 ──────────────────────────────────────────────
        query_filter = None
        if with_filter:
            struct_filters = _extract_structural_filters(query_str)
            query_filter   = build_qdrant_filter(struct_filters) if struct_filters else None
        rrf_points    = hybrid_rrf_pool(client, q_dense, s_idx, s_val, pool, query_filter)
        rrf_sources   = unique_sources_in_order(rrf_points)

        # ── Stage 2: 對同一候選池 rerank ─────────────────────────────────────
        rq = query_str
        if enable_rewrite:
            variants = rewrite_query(query_str, model_name)
            if variants:
                rq = variants[0]
                print(f"  [{qid}] rerank_query: {rq!r}")
        reranked_payloads = rerank_pool(rerank_model, query_str, rrf_points, rerank_query=rq)
        rerank_sources    = unique_sources_in_order(reranked_payloads)

        stage_sources = {"rrf": rrf_sources, "rerank": rerank_sources}

        trace_entry = {
            "id": qid, "category": category, "query": query_str,
            "relevant_patterns": raw_patterns,
            "relevant_expanded": sorted(relevant),
            "pool_chunks": len(rrf_points),
            "retrieved": {s: stage_sources[s][:max(all_ks)] for s in STAGES},
            "metrics": {},
        }

        for stage in STAGES:
            srcs = stage_sources[stage]
            m = {}
            for k in all_ks:
                r = recall_at_k(srcs, relevant, k)
                # Hit Rate@k：top-k 至少命中 1 篇相關文件即記 1（最寬鬆，貼近「LLM
                # 能不能 ground 這題」）。等價於 reciprocal_rank_at_k(...,k) > 0。
                hr = 1.0 if (set(srcs[:k]) & relevant) else 0.0
                results[stage][category][f"recall@{k}"].append(r)
                results[stage]["overall"][f"recall@{k}"].append(r)
                results[stage][category][f"hit_rate@{k}"].append(hr)
                results[stage]["overall"][f"hit_rate@{k}"].append(hr)
                m[f"recall@{k}"] = r
                m[f"hit_rate@{k}"] = hr
            for h in MRR_HORIZONS:
                mrr = reciprocal_rank_at_k(srcs, relevant, h)
                results[stage][category][f"mrr@{h}"].append(mrr)
                results[stage]["overall"][f"mrr@{h}"].append(mrr)
                m[f"mrr@{h}"] = mrr
            trace_entry["metrics"][stage] = m

        traces.append(trace_entry)

        rrf_m, rr_m = trace_entry["metrics"]["rrf"], trace_entry["metrics"]["rerank"]
        print(f"  [{qid:>7}] ({category:8}) {query_str[:42]:<42}")
        mrr_str = "  ".join(f"MRR@{h}={rrf_m[f'mrr@{h}']:.3f}" for h in MRR_HORIZONS)
        print(f"            RRF     R@5={rrf_m['recall@5']:.3f}  R@10={rrf_m['recall@10']:.3f}  "
              f"R@20={rrf_m['recall@20']:.3f}  {mrr_str}")
        mrr_str_rr = "  ".join(f"MRR@{h}={rr_m[f'mrr@{h}']:.3f}" for h in MRR_HORIZONS)
        print(f"            Rerank  R@1={rr_m['recall@1']:.3f}  R@3={rr_m['recall@3']:.3f}  "
              f"R@5={rr_m['recall@5']:.3f}  {mrr_str_rr}")

    # ── 聚合（每類別取平均）───────────────────────────────────────────────────
    all_cats = list(dict.fromkeys(list(CATEGORIES) + list(results[STAGES[0]].keys())))
    summary = {stage: {cat: {} for cat in all_cats} for stage in STAGES}
    for stage in STAGES:
        for cat in all_cats:
            for metric, vals in results[stage][cat].items():
                summary[stage][cat][metric] = round(statistics.mean(vals), 4) if vals else 0.0
                summary[stage][cat][f"{metric}_n"] = len(vals)

    if skipped_queries:
        print(f"\n[WARN] Skipped {len(skipped_queries)} queries: {skipped_queries}")

    return {
        "summary": summary,
        "traces": traces,
        "pool": pool,
        "per_lane_fetch": PER_LANE_FETCH,
        "skipped_queries": skipped_queries,
        "collection_sources_count": len(all_sources),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════════

def _n_for(summary, cat) -> int:
    return summary["rrf"][cat].get(f"recall@{RECALL_KS_RETRIEVAL[0]}_n", 0)


def _metric_label(m: str) -> str:
    return (m.replace("hit_rate@", "HitRate@")
             .replace("recall@", "Recall@")
             .replace("mrr@", "MRR@"))


def print_retrieval_table(summary) -> None:
    """表 A：Stage 1（RRF）的 Hit Rate / Recall 天花板。"""
    metrics = (
        [f"hit_rate@{k}" for k in RECALL_KS_RETRIEVAL]
        + [f"recall@{k}" for k in RECALL_KS_RETRIEVAL]
        + [f"mrr@{h}" for h in MRR_HORIZONS]
    )
    width = 64
    print(f"\n{'=' * width}")
    print("(A) RETRIEVAL — Qdrant Hybrid + Server-side RRF  [recall ceiling]")
    print("    HitRate＝有沒有撈到≥1篇；Recall＝撈齊幾分之幾。RRF 是 reranker 上限。")
    print(f"{'=' * width}\n")
    for cat in summary["rrf"]:
        n = _n_for(summary, cat)
        if n == 0:
            continue
        print(f"  Category: {cat.upper()}  (n={n})")
        for m in metrics:
            print(f"    {_metric_label(m):<12}{fmt_pct(summary['rrf'][cat].get(m, 0.0)):>10}")
        print()


def print_rerank_table(summary) -> None:
    """表 B：Stage 2（rerank）—— RRF 順序 vs CrossEncoder 順序並排 + Δ。"""
    metrics = (
        [f"hit_rate@{k}" for k in RECALL_KS_RERANK]
        + [f"recall@{k}" for k in RECALL_KS_RERANK]
        + [f"mrr@{h}" for h in MRR_HORIZONS]
    )
    width = 78
    print(f"\n{'=' * width}")
    print("(B) RERANK — RRF order  →  BGE-reranker-v2-m3 (CrossEncoder)")
    print("    同一候選池、只改順序。rerank 的價值體現在小 k 的 HitRate/Recall 與 MRR。")
    print("    Δ > 0 ⇒ rerank 把相關文件往前推（贏）。")
    print(f"{'=' * width}\n")
    for cat in summary["rrf"]:
        n = _n_for(summary, cat)
        if n == 0:
            continue
        print(f"  Category: {cat.upper()}  (n={n})")
        print(f"    {'Metric':<12}{'RRF':>10}{'Reranked':>12}{'Δ (pp)':>12}")
        print(f"    {'-'*12}{'-'*10}{'-'*12}{'-'*12}")
        for m in metrics:
            label = _metric_label(m)
            v_rrf = summary["rrf"][cat].get(m, 0.0)
            v_rr  = summary["rerank"][cat].get(m, 0.0)
            d = (v_rr - v_rrf) * 100
            sign = "+" if d >= 0 else ""
            print(f"    {label:<12}{fmt_pct(v_rrf):>10}{fmt_pct(v_rr):>12}{sign}{d:>10.1f}pp")
        print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global COLLECTION_NAME

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Two-stage retrieval eval: Retrieval Recall → Rerank Recall/MRR",
    )
    parser.add_argument("--eval-set", default="eval/eval_set.json")
    parser.add_argument("--output", default="eval/results_rerank.json")
    parser.add_argument("--collection", default=COLLECTION_NAME,
                        help=f"Qdrant collection (default: {COLLECTION_NAME})")
    parser.add_argument("--qdrant-path", default=QDRANT_PATH,
                        help=f"Qdrant local path (default: {QDRANT_PATH}). "
                             f"指向快照副本可在 api_server 佔用 qdrant_db 時跑 eval。"
                             f"（CLI 參數不受 load_dotenv 影響，比 env var 可靠）")
    parser.add_argument("--pool", type=int, default=DEFAULT_POOL,
                        help=f"RRF 候選池大小 = 每題 rerank 的 chunk 數 (default: {DEFAULT_POOL})")
    parser.add_argument("--with-filter", action="store_true",
                        help="套用 ticker + report_period_code structural hard filter（測試過濾效果）")
    parser.add_argument("--rewrite", action="store_true",
                        help="對 reranker 使用改寫後的術語 query（rewrite variant[0]）而非原始 query")
    parser.add_argument("--model", default="gemini-2.5-flash",
                        help="query rewrite 使用的 LLM（預設 gemini-2.5-flash）")
    args = parser.parse_args()

    COLLECTION_NAME = args.collection
    qdrant_path = args.qdrant_path
    pool = args.pool

    eval_path = Path(args.eval_set)
    if not eval_path.exists():
        print(f"[ERROR] Eval set not found: {eval_path}")
        sys.exit(1)
    with open(eval_path, "r", encoding="utf-8") as f:
        eval_set = json.load(f)
    print(f"[INFO] Loaded {len(eval_set['queries'])} queries from {eval_path}")

    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    print(f"[INFO] Loading BGE-M3 (dense+sparse): {EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)
    print(f"[INFO] Loading reranker: {RERANK_MODEL}")
    rerank_model = CrossEncoder(RERANK_MODEL)

    # 依 .env 的 QDRANT_URL 自動選 Docker server / local path。QDRANT_URL 未設時才
    # fallback 到 --qdrant-path 指定的 local 路徑（Docker 遷移後 local ./qdrant_db
    # 已是凍結舊快照，見 CHANGELOG 2026-07-07 Qdrant client bug）
    from rag_query import make_qdrant_client, QDRANT_URL
    if QDRANT_URL:
        print(f"[INFO] Opening Qdrant server: {QDRANT_URL}")
        client = make_qdrant_client()
    else:
        print(f"[INFO] Opening Qdrant local: {qdrant_path}")
        client = QdrantClient(path=qdrant_path)

    try:
        total = client.count(collection_name=COLLECTION_NAME, exact=True).count
    except Exception as e:
        print(f"[ERROR] Cannot access collection '{COLLECTION_NAME}': {e}")
        print("        Run: python data_update_unstructure.py --rebuild")
        sys.exit(1)
    if total == 0:
        print(f"[ERROR] Collection '{COLLECTION_NAME}' is empty.")
        sys.exit(1)
    print(f"[INFO] Collection: {COLLECTION_NAME}  ({total} chunks)")
    print(f"[INFO] Per-lane prefetch: {PER_LANE_FETCH}  |  RRF pool (rerank input): {pool}\n")

    report = evaluate(eval_set, client, bge_m3, rerank_model, pool,
                      with_filter=args.with_filter,
                      enable_rewrite=args.rewrite, model_name=args.model)

    print_retrieval_table(report["summary"])
    print_rerank_table(report["summary"])

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[DONE] Detailed traces written to: {out_path}")


if __name__ == "__main__":
    main()
