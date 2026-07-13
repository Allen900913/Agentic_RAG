"""
eval_retrieval.py — Retrieval Evaluation Harness

Compares four retrieval modes on the same eval set:
  1. dense   — BGE-M3 dense vector only
  2. sparse  — BGE-M3 lexical sparse only
  3. hybrid  — dense + sparse fused via Qdrant server-side RRF
  4. bm25    — classic BM25 (rank_bm25) over an in-memory corpus, with
               jieba word segmentation for bilingual (中文/English) text.
               Not a Qdrant lane — built once by scrolling the collection's
               `document` payload. Structural hard filter (--with-filter)
               is NOT applied to this mode (no per-doc metadata index).

Metrics:
  - Recall@K       (K = 5, 10, 20)
  - MRR@K          (Mean Reciprocal Rank, K = 10)
  - Per-category breakdown (semantic / lexical / mixed)

Ground truth granularity: source filename
  A retrieved chunk counts as "hit" if its `source` payload is in the
  query's `relevant` list. We aggregate hits per query by unique source.

Usage:
  python eval/eval_retrieval.py
  python eval/eval_retrieval.py --eval-set eval/eval_set.json --output eval/results.json
  python eval/eval_retrieval.py --ks 5,10,20
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

# ── Reuse config from main pipeline ────────────────────────────────────────────
QDRANT_PATH        = os.getenv("QDRANT_PATH", "./qdrant_db")
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
COLLECTION_NAME    = "us_stock_rag_unstructured"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"

DEFAULT_KS         = [5, 10, 20]
DEFAULT_FETCH      = 100   # per-lane prefetch for hybrid mode


# ══════════════════════════════════════════════════════════════════════════════
# Retrieval lanes
# ══════════════════════════════════════════════════════════════════════════════

def dense_retrieve(client, q_dense, limit, query_filter=None):
    return client.query_points(
        collection_name=COLLECTION_NAME,
        query=q_dense,
        using=DENSE_VECTOR_NAME,
        limit=limit,
        query_filter=query_filter,
        with_payload=["source"],
    ).points


def sparse_retrieve(client, q_sparse_indices, q_sparse_values, limit, query_filter=None):
    from qdrant_client import models
    return client.query_points(
        collection_name=COLLECTION_NAME,
        query=models.SparseVector(indices=q_sparse_indices, values=q_sparse_values),
        using=SPARSE_VECTOR_NAME,
        limit=limit,
        query_filter=query_filter,
        with_payload=["source"],
    ).points


def hybrid_retrieve(client, q_dense, q_sparse_indices, q_sparse_values, limit, query_filter=None):
    from qdrant_client import models
    return client.query_points(
        collection_name=COLLECTION_NAME,
        prefetch=[
            models.Prefetch(
                query=q_dense,
                using=DENSE_VECTOR_NAME,
                limit=DEFAULT_FETCH,
                filter=query_filter,
            ),
            models.Prefetch(
                query=models.SparseVector(indices=q_sparse_indices, values=q_sparse_values),
                using=SPARSE_VECTOR_NAME,
                limit=DEFAULT_FETCH,
                filter=query_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=["source"],
    ).points


def tokenize_bilingual(text: str) -> list[str]:
    """中英混合斷詞：jieba 切詞（中文詞 + 保留英文/數字 token），給 BM25 用。"""
    import re

    import jieba
    tokens = jieba.lcut(text.lower())
    return [t for t in tokens if re.search(r"[0-9a-z一-鿿]", t)]


def build_bm25_index(client):
    """Scroll the whole collection once to build an in-memory BM25 corpus.
    Returns (bm25_index, parallel_source_list) — index i in both lists refers
    to the same chunk."""
    from rank_bm25 import BM25Okapi

    print("[INFO] Scrolling collection to build BM25 corpus (document payload)...")
    sources: list[str] = []
    tokenized_docs: list[list[str]] = []
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=256,
            offset=next_offset,
            with_payload=["source", "document"],
            with_vectors=False,
        )
        for p in points:
            payload = p.payload or {}
            sources.append(payload.get("source"))
            tokenized_docs.append(tokenize_bilingual(payload.get("document", "")))
        if next_offset is None:
            break
    print(f"[INFO] BM25 corpus built: {len(tokenized_docs)} chunks.\n")
    return BM25Okapi(tokenized_docs), sources


def bm25_retrieve(bm25, corpus_sources: list[str], query_tokens: list[str], limit: int) -> list[str]:
    """Return chunk-level sources ranked by BM25 score, descending, top-`limit`."""
    import numpy as np
    scores = bm25.get_scores(query_tokens)
    top_idx = np.argsort(scores)[::-1][:limit]
    return [corpus_sources[i] for i in top_idx if scores[i] > 0]


def unique_in_order(items: list[str]) -> list[str]:
    """Collapse chunk-level sources to unique sources, preserving first-seen rank."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for s in items:
        if s and s not in seen_set:
            seen.append(s)
            seen_set.add(s)
    return seen


# ══════════════════════════════════════════════════════════════════════════════
# Ground-truth pattern expansion
# ══════════════════════════════════════════════════════════════════════════════

def _has_glob(pat: str) -> bool:
    return any(ch in pat for ch in "*?[")


def collect_all_sources(client) -> set[str]:
    """Scroll the whole collection once to gather every unique `source` payload.
    Used to expand glob patterns in eval_set.json's `relevant` field."""
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
    """Expand glob patterns (e.g. 'NVDA_News_*.txt') against the actual set of
    source filenames in the collection. Non-glob entries are kept as-is."""
    expanded: set[str] = set()
    for pat in patterns:
        if _has_glob(pat):
            matched = set(fnmatch.filter(all_sources, pat))
            expanded |= matched
        else:
            expanded.add(pat)
    return expanded


# ══════════════════════════════════════════════════════════════════════════════
# Metrics
# ══════════════════════════════════════════════════════════════════════════════

def unique_sources_in_order(points) -> list[str]:
    """Collapse chunk-level hits to source-level, preserving first-seen rank."""
    seen = []
    seen_set = set()
    for p in points:
        src = (p.payload or {}).get("source")
        if src and src not in seen_set:
            seen.append(src)
            seen_set.add(src)
    return seen


def recall_at_k(retrieved_sources: list[str], relevant: set[str], k: int) -> float:
    """How many of all relevant docs did we find in top-K? (completeness)"""
    if not relevant:
        return 0.0
    top_k = set(retrieved_sources[:k])
    hits = top_k & relevant
    return len(hits) / len(relevant)


def precision_at_k(retrieved_sources: list[str], relevant: set[str], k: int) -> float:
    """Of the top-K we returned, how many were relevant? (signal-to-noise)"""
    top_k = retrieved_sources[:k]
    if not top_k:
        return 0.0
    hits = sum(1 for s in top_k if s in relevant)
    return hits / len(top_k)


def f1_at_k(retrieved_sources: list[str], relevant: set[str], k: int) -> float:
    """Harmonic mean of P@K and R@K — balances precision and recall."""
    p = precision_at_k(retrieved_sources, relevant, k)
    r = recall_at_k(retrieved_sources, relevant, k)
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def average_precision_at_k(retrieved_sources: list[str], relevant: set[str], k: int) -> float:
    """AP@K = (1/normalizer) * Σ P@i for every position i in top-K that is a hit.
    Rewards systems that rank relevant docs HIGH — sensitive to order, unlike R@K.

    Normalizer uses min(K, |relevant|) so a query with only 2 relevant docs can
    still reach AP=1.0 when those 2 are ranked first (instead of being capped low)."""
    if not relevant:
        return 0.0
    top_k = retrieved_sources[:k]
    score = 0.0
    hits = 0
    for i, src in enumerate(top_k, start=1):
        if src in relevant:
            hits += 1
            score += hits / i  # precision at this rank
    if hits == 0:
        return 0.0
    return score / min(k, len(relevant))


def reciprocal_rank_at_k(retrieved_sources: list[str], relevant: set[str], k: int) -> float:
    """1/rank of the FIRST relevant doc in top-K. Cares only about the first hit."""
    for rank, src in enumerate(retrieved_sources[:k], start=1):
        if src in relevant:
            return 1.0 / rank
    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Evaluation runner
# ══════════════════════════════════════════════════════════════════════════════

def evaluate(eval_set: dict, client, bge_m3, ks: list[int], with_filter: bool = False) -> dict:
    from qdrant_client import models
    if with_filter:
        from rag_query import _extract_structural_filters, build_qdrant_filter

    queries = eval_set["queries"]
    modes   = ["dense", "sparse", "hybrid", "bm25"]
    max_k   = max(ks)

    # ── Snapshot every source filename currently in the collection ──────────
    #    Used to expand glob patterns (e.g. "NVDA_News_*.txt") in eval set.
    print("[INFO] Snapshotting source filenames for glob expansion...")
    all_sources = collect_all_sources(client)
    print(f"[INFO] Found {len(all_sources)} unique source files in collection.\n")

    # ── Build the BM25 corpus once (separate from Qdrant) ───────────────────
    bm25_index, bm25_corpus_sources = build_bm25_index(client)

    # Per-mode, per-category metric accumulators
    # results[mode][category][metric] = list of per-query scores
    results: dict = {
        mode: {cat: defaultdict(list) for cat in ["semantic", "lexical", "mixed", "overall"]}
        for mode in modes
    }
    # Also store per-query traces for transparency
    traces = []

    skipped_queries: list[str] = []

    for q in queries:
        qid       = q["id"]
        category  = q["category"]
        query_str = q["query"]
        raw_patterns = q["relevant"]

        # Expand globs against the actual collection's sources
        relevant = expand_relevant(raw_patterns, all_sources)

        if not relevant:
            print(f"  [{qid}] ⚠️  no relevant sources matched after glob expansion "
                  f"(patterns: {raw_patterns}) — skipping.")
            skipped_queries.append(qid)
            continue

        # Encode once: dense + sparse together (BGE-M3 single call)
        encoded = bge_m3.encode(
            [query_str],
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        q_dense  = encoded["dense_vecs"][0].tolist()
        q_sparse = encoded["lexical_weights"][0]
        s_idx    = [int(tok) for tok in q_sparse.keys()]
        s_val    = [float(w)  for w   in q_sparse.values()]

        # Run all three modes with the SAME fetch size (max_k * 3 chunks per lane,
        # then dedupe to source level). We fetch chunks generously because multiple
        # chunks can share a source and we want enough variety at the source level.
        chunk_limit = max_k * 5

        # 與 rag_query 生產路徑一致：偵測到明確結構約束時才建 hard filter
        query_filter = None
        if with_filter:
            struct_filters = _extract_structural_filters(query_str)
            query_filter   = build_qdrant_filter(struct_filters) if struct_filters else None

        dense_pts  = dense_retrieve(client, q_dense, chunk_limit, query_filter)
        sparse_pts = sparse_retrieve(client, s_idx, s_val, chunk_limit, query_filter)
        hybrid_pts = hybrid_retrieve(client, q_dense, s_idx, s_val, chunk_limit, query_filter)

        # BM25 is a separate in-memory index (no Qdrant filter support here)
        bm25_tokens = tokenize_bilingual(query_str)
        bm25_srcs   = bm25_retrieve(bm25_index, bm25_corpus_sources, bm25_tokens, chunk_limit)

        per_mode_sources = {
            "dense":  unique_sources_in_order(dense_pts),
            "sparse": unique_sources_in_order(sparse_pts),
            "hybrid": unique_sources_in_order(hybrid_pts),
            "bm25":   unique_in_order(bm25_srcs),
        }

        trace_entry = {
            "id":       qid,
            "category": category,
            "query":    query_str,
            "relevant_patterns": raw_patterns,
            "relevant_expanded": sorted(relevant),
            "retrieved": {m: per_mode_sources[m][:max_k] for m in modes},
            "metrics":  {},
        }

        for mode in modes:
            srcs = per_mode_sources[mode]
            mode_metrics = {}

            # ── Per-K metrics: precision / recall / f1 ────────────────────
            for k in ks:
                p = precision_at_k(srcs, relevant, k)
                r = recall_at_k(srcs, relevant, k)
                f = f1_at_k(srcs, relevant, k)
                for name, val in (("precision", p), ("recall", r), ("f1", f)):
                    key = f"{name}@{k}"
                    results[mode][category][key].append(val)
                    results[mode]["overall"][key].append(val)
                    mode_metrics[key] = val

            # ── Ranking-aware metrics computed at max_k horizon ───────────
            ap  = average_precision_at_k(srcs, relevant, max_k)
            mrr = reciprocal_rank_at_k(srcs, relevant, max_k)
            for name, val in ((f"ap@{max_k}", ap), (f"mrr@{max_k}", mrr)):
                results[mode][category][name].append(val)
                results[mode]["overall"][name].append(val)
                mode_metrics[name] = val

            trace_entry["metrics"][mode] = mode_metrics

        traces.append(trace_entry)
        # Pick the middle K (or max_k if only one) for the per-query progress line
        mid_k = ks[len(ks) // 2] if len(ks) > 1 else ks[0]
        print(f"  [{qid:>7}] ({category:8}) {query_str[:50]}...")
        for mode in modes:
            m = trace_entry["metrics"][mode]
            print(
                f"            {mode:<6} "
                f"P@{mid_k}={m[f'precision@{mid_k}']:.3f}  "
                f"R@{mid_k}={m[f'recall@{mid_k}']:.3f}  "
                f"F1@{mid_k}={m[f'f1@{mid_k}']:.3f}  "
                f"AP@{max_k}={m[f'ap@{max_k}']:.3f}  "
                f"MRR@{max_k}={m[f'mrr@{max_k}']:.3f}"
            )

    # ── Aggregate (mean) ─────────────────────────────────────────────────────
    summary: dict = {mode: {cat: {} for cat in ["semantic", "lexical", "mixed", "overall"]}
                     for mode in modes}
    for mode in modes:
        for cat in ["semantic", "lexical", "mixed", "overall"]:
            for metric, vals in results[mode][cat].items():
                summary[mode][cat][metric] = (
                    round(statistics.mean(vals), 4) if vals else 0.0
                )
                summary[mode][cat][f"{metric}_n"] = len(vals)

    if skipped_queries:
        print(f"\n[WARN] Skipped {len(skipped_queries)} queries with no matching "
              f"sources after glob expansion: {skipped_queries}")

    return {
        "summary":          summary,
        "traces":           traces,
        "ks":               ks,
        "skipped_queries":  skipped_queries,
        "collection_sources_count": len(all_sources),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════════

def fmt_pct(x: float) -> str:
    return f"{x*100:5.1f}%"


def print_table(summary: dict, ks: list[int]) -> None:
    """Print a category × mode × metric report.

    For each category we print one block: metrics as ROWS, modes as COLUMNS,
    so dense/sparse/hybrid are visually adjacent for easy comparison.
    """
    max_k = max(ks)
    modes = ["dense", "sparse", "hybrid", "bm25"]
    cats  = ["semantic", "lexical", "mixed", "overall"]

    # All metric keys, in display order
    metric_keys: list[str] = []
    for k in ks:
        metric_keys += [f"precision@{k}", f"recall@{k}", f"f1@{k}"]
    metric_keys += [f"ap@{max_k}", f"mrr@{max_k}"]

    # Pretty labels for display
    def label(m: str) -> str:
        if m.startswith("precision@"): return f"P@{m.split('@')[1]}"
        if m.startswith("recall@"):    return f"R@{m.split('@')[1]}"
        if m.startswith("f1@"):        return f"F1@{m.split('@')[1]}"
        if m.startswith("ap@"):        return f"MAP@{m.split('@')[1]}"  # mean over queries
        if m.startswith("mrr@"):       return f"MRR@{m.split('@')[1]}"
        return m

    width = 96
    print(f"\n{'═' * width}")
    print("RETRIEVAL EVALUATION RESULTS — Precision / Recall / F1 / MAP / MRR")
    print(f"(mean over queries per category, all metrics shown as percentages)")
    print(f"{'═' * width}\n")

    for cat in cats:
        n_key = f"{metric_keys[0]}_n"
        n = summary["dense"][cat].get(n_key, 0)
        if n == 0:
            continue
        print(f"  Category: {cat.upper()}  (n={n})")
        header = (f"    {'Metric':<10}" + "".join(f"{m:>12}" for m in modes)
                  + "    Δ(H-D)" + "  Δ(Sprs-BM25)")
        print(header)
        print(f"    {'-' * 10}" + ("-" * 12) * len(modes) + "    ------" + "  -----------")
        for m in metric_keys:
            row = f"    {label(m):<10}"
            for mode in modes:
                v = summary[mode][cat].get(m, 0.0)
                row += f"{fmt_pct(v):>12}"
            # delta (hybrid vs dense) in percentage points
            d_hd = summary["hybrid"][cat].get(m, 0.0) - summary["dense"][cat].get(m, 0.0)
            row += f"   {d_hd*100:+.1f}pp"
            # delta (BGE-M3 sparse vs classic BM25) — the question this lane exists to answer
            d_sb = summary["sparse"][cat].get(m, 0.0) - summary["bm25"][cat].get(m, 0.0)
            row += f"    {d_sb*100:+.1f}pp"
            print(row)
        print()

    # ── Headline summary ─────────────────────────────────────────────────────
    print(f"{'─' * width}")
    print("HEADLINE Δ — Hybrid vs Dense-only at K=10 (positive ⇒ hybrid wins)")
    print(f"{'─' * width}")
    headline_metrics = ["precision@10", "recall@10", "f1@10",
                        f"ap@{max_k}", f"mrr@{max_k}"]
    headline_metrics = [m for m in headline_metrics
                        if any(m in summary[modes[0]][c] for c in cats)]
    for cat in cats:
        n = summary["dense"][cat].get(f"{metric_keys[0]}_n", 0)
        if n == 0:
            continue
        parts = []
        for m in headline_metrics:
            d = summary["hybrid"][cat].get(m, 0.0) - summary["dense"][cat].get(m, 0.0)
            parts.append(f"{label(m)}: {d*100:+.1f}pp")
        print(f"  {cat.upper():<10}  " + "   ".join(parts))
    print()

    print(f"{'─' * width}")
    print("HEADLINE Δ — BGE-M3 Sparse vs classic BM25 at K=10 (positive ⇒ sparse wins)")
    print(f"{'─' * width}")
    for cat in cats:
        n = summary["dense"][cat].get(f"{metric_keys[0]}_n", 0)
        if n == 0:
            continue
        parts = []
        for m in headline_metrics:
            d = summary["sparse"][cat].get(m, 0.0) - summary["bm25"][cat].get(m, 0.0)
            parts.append(f"{label(m)}: {d*100:+.1f}pp")
        print(f"  {cat.upper():<10}  " + "   ".join(parts))
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global COLLECTION_NAME

    # Windows console codepages (e.g. cp950) can't encode some report symbols
    # (─═Δ⇒...). Force UTF-8 on stdout/stderr so printing never crashes the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Retrieval Evaluation Harness (Dense vs Sparse vs Hybrid RRF)",
    )
    parser.add_argument("--eval-set", default="eval/eval_set.json",
                        help="Path to eval set JSON (default: eval/eval_set.json)")
    parser.add_argument("--output", default="eval/results.json",
                        help="Path to write detailed results JSON (default: eval/results.json)")
    parser.add_argument("--ks", default="5,10,20",
                        help="Comma-separated K values for Recall@K (default: 5,10,20)")
    parser.add_argument("--collection", default=COLLECTION_NAME,
                        help=f"Qdrant collection to evaluate against (default: {COLLECTION_NAME})")
    parser.add_argument("--with-filter", action="store_true",
                        help="套用 structural hard filter（report_period_code/ticker）到三種模式，量測 filter 後 dense vs hybrid")
    args = parser.parse_args()

    COLLECTION_NAME = args.collection

    ks = sorted({int(k.strip()) for k in args.ks.split(",") if k.strip()})

    eval_path = Path(args.eval_set)
    if not eval_path.exists():
        print(f"[ERROR] Eval set not found: {eval_path}")
        sys.exit(1)

    with open(eval_path, "r", encoding="utf-8") as f:
        eval_set = json.load(f)
    n_queries = len(eval_set["queries"])
    print(f"[INFO] Loaded {n_queries} queries from {eval_path}")

    # ── Heavy imports ────────────────────────────────────────────────────────
    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel

    print(f"[INFO] Loading BGE-M3: {EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)

    # 依 .env 的 QDRANT_URL 自動選 Docker server / local path（與 rag_query 一致；
    # 2026-07-06 Docker 遷移後 local ./qdrant_db 已是凍結舊快照，不可再直接用 path）
    from rag_query import make_qdrant_client
    client = make_qdrant_client()

    try:
        total = client.count(collection_name=COLLECTION_NAME, exact=True).count
    except Exception as e:
        print(f"[ERROR] Cannot access collection '{COLLECTION_NAME}': {e}")
        print("        Run: python data_update.py --rebuild")
        sys.exit(1)
    if total == 0:
        print(f"[ERROR] Collection is empty. Run: python data_update.py --rebuild")
        sys.exit(1)
    print(f"[INFO] Collection size: {total} chunks\n")

    print(f"[INFO] Running evaluation across modes: dense / sparse / hybrid / bm25")
    print(f"[INFO] Hard filter: {'ON' if args.with_filter else 'OFF'}")
    print(f"[INFO] K values: {ks}\n")

    report = evaluate(eval_set, client, bge_m3, ks, with_filter=args.with_filter)

    print_table(report["summary"], ks)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[DONE] Detailed traces written to: {out_path}")


if __name__ == "__main__":
    main()
