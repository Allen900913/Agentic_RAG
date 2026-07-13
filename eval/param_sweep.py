"""
param_sweep.py — Grid search over FETCH_N × RRF_TOP_N for hybrid retrieval.

Tests how different prefetch sizes and RRF output sizes affect Recall@5,
Recall@10, and MRR@10 on the existing eval set.

Usage:
  python eval/param_sweep.py
  python eval/param_sweep.py --eval-set eval/eval_set.json
"""

import argparse
import fnmatch
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

QDRANT_PATH        = os.getenv("QDRANT_PATH", "./qdrant_db")
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
COLLECTION_NAME    = "us_stock_rag"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"

# ── Parameter grid ─────────────────────────────────────────────────────────────
FETCH_N_VALUES   = [10, 20, 30, 50, 100]   # per-lane prefetch candidates
RRF_TOP_N_VALUES = [5, 10, 20]             # RRF output size (must be ≤ FETCH_N)
EVAL_KS          = [5, 10]


# ── Helpers ────────────────────────────────────────────────────────────────────

def collect_all_sources(client) -> set[str]:
    sources: set[str] = set()
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME, limit=256,
            offset=next_offset, with_payload=["source"], with_vectors=False,
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
        if any(ch in pat for ch in "*?["):
            expanded |= set(fnmatch.filter(all_sources, pat))
        else:
            expanded.add(pat)
    return expanded


def unique_sources(points) -> list[str]:
    seen, seen_set = [], set()
    for p in points:
        src = (p.payload or {}).get("source")
        if src and src not in seen_set:
            seen.append(src)
            seen_set.add(src)
    return seen


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def rr_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    for rank, src in enumerate(retrieved[:k], 1):
        if src in relevant:
            return 1.0 / rank
    return 0.0


# ── Core sweep ─────────────────────────────────────────────────────────────────

def run_sweep(eval_set: dict, client, bge_m3) -> list[dict]:
    from qdrant_client import models

    queries     = eval_set["queries"]
    all_sources = collect_all_sources(client)

    # Pre-encode all queries once
    print(f"[INFO] Encoding {len(queries)} queries with BGE-M3...")
    encoded_queries = []
    for q in queries:
        enc = bge_m3.encode(
            [q["query"]], return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        q_dense   = enc["dense_vecs"][0].tolist()
        q_sparse  = enc["lexical_weights"][0]
        s_indices = [int(tok) for tok in q_sparse.keys()]
        s_values  = [float(w)  for w   in q_sparse.values()]
        relevant  = expand_relevant(q.get("relevant", []), all_sources)
        encoded_queries.append({
            "id": q["id"], "category": q["category"],
            "q_dense": q_dense, "s_indices": s_indices, "s_values": s_values,
            "relevant": relevant,
        })
    print()

    results = []

    for fetch_n in FETCH_N_VALUES:
        for rrf_top_n in RRF_TOP_N_VALUES:
            if rrf_top_n > fetch_n:
                continue  # nonsensical: can't return more than fetched

            label = f"fetch={fetch_n:3d}, rrf_top={rrf_top_n:2d}"
            accum = defaultdict(list)   # metric → list of per-query values

            for eq in encoded_queries:
                points = client.query_points(
                    collection_name=COLLECTION_NAME,
                    prefetch=[
                        models.Prefetch(
                            query=eq["q_dense"],
                            using=DENSE_VECTOR_NAME,
                            limit=fetch_n,
                        ),
                        models.Prefetch(
                            query=models.SparseVector(
                                indices=eq["s_indices"], values=eq["s_values"],
                            ),
                            using=SPARSE_VECTOR_NAME,
                            limit=fetch_n,
                        ),
                    ],
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=rrf_top_n,
                    with_payload=["source"],
                ).points

                retrieved = unique_sources(points)
                relevant  = eq["relevant"]

                for k in EVAL_KS:
                    accum[f"recall@{k}"].append(recall_at_k(retrieved, relevant, k))
                accum["mrr@10"].append(rr_at_k(retrieved, relevant, 10))

            row = {"fetch_n": fetch_n, "rrf_top_n": rrf_top_n}
            for metric, vals in accum.items():
                row[metric] = round(sum(vals) / len(vals), 4)
            results.append(row)

            r5  = row.get("recall@5",  0)
            r10 = row.get("recall@10", 0)
            mrr = row["mrr@10"]
            print(f"  {label}  |  R@5={r5:.4f}  R@10={r10:.4f}  MRR@10={mrr:.4f}")

    return results


def print_table(results: list[dict]) -> None:
    print("\n" + "═" * 70)
    print("PARAMETER SWEEP RESULTS — Hybrid Retrieval (RRF)")
    print("═" * 70)
    header = f"{'fetch_n':>8}  {'rrf_top_n':>9}  {'R@5':>7}  {'R@10':>7}  {'MRR@10':>8}"
    print(header)
    print("─" * 70)

    best_r5 = max(r.get("recall@5", 0) for r in results)

    for r in sorted(results, key=lambda x: x.get("recall@5", 0), reverse=True):
        marker = " <- best R@5" if r.get("recall@5", 0) == best_r5 else ""
        print(
            f"{r['fetch_n']:>8}  {r['rrf_top_n']:>9}  "
            f"{r.get('recall@5',0):>7.4f}  {r.get('recall@10',0):>7.4f}  "
            f"{r['mrr@10']:>8.4f}{marker}"
        )

    # Highlight current production settings
    current = next(
        (r for r in results if r["fetch_n"] == 30 and r["rrf_top_n"] == 10), None
    )
    if current:
        print("─" * 70)
        print(
            f"Current config (fetch=30, rrf_top=10):  "
            f"R@5={current.get('recall@5',0):.4f}  "
            f"R@10={current.get('recall@10',0):.4f}  "
            f"MRR@10={current['mrr@10']:.4f}"
        )
    print("═" * 70)


def main() -> None:
    global COLLECTION_NAME

    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", default="eval/eval_set.json")
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--output", default="eval/param_sweep_results.json")
    args = parser.parse_args()

    COLLECTION_NAME = args.collection

    eval_path = Path(args.eval_set)
    if not eval_path.exists():
        print(f"[ERROR] Eval set not found: {eval_path}")
        sys.exit(1)

    eval_set = json.loads(eval_path.read_text(encoding="utf-8"))
    print(f"[INFO] Loaded {len(eval_set['queries'])} queries from {eval_path}")

    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel

    print(f"[INFO] Loading BGE-M3: {EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)

    print(f"[INFO] Opening Qdrant: {QDRANT_PATH}")
    client = QdrantClient(path=QDRANT_PATH)

    grid = [(f, r) for f in FETCH_N_VALUES for r in RRF_TOP_N_VALUES if r <= f]
    print(f"[INFO] Running {len(grid)} parameter combinations...\n")

    results = run_sweep(eval_set, client, bge_m3)
    print_table(results)

    out_path = Path(args.output)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[INFO] Full results saved to {out_path}")


if __name__ == "__main__":
    main()
