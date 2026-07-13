"""
hnsw_sweep.py — 掃描 HNSW 查詢時參數 ef (hnsw_ef) 對檢索品質的影響。

ef 是 HNSW 在查詢階段的搜索寬度：
  - ef 越大 → ANN 搜索越接近精確結果 → Recall 越高，但速度越慢
  - ef 越小 → 速度越快，但可能漏掉相關文件
  - ef 必須 >= limit（抓取候選數）

Qdrant 預設 ef=-1（自動，通常等於 limit）。
ef=∞ 等同 exact=True（暴力搜索，100% 精確）。

Usage:
  python eval/hnsw_sweep.py
  python eval/hnsw_sweep.py --eval-set eval/eval_set.json
"""

import argparse
import fnmatch
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

QDRANT_PATH        = os.getenv("QDRANT_PATH", "./qdrant_db")
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
COLLECTION_NAME    = "us_stock_rag"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"

# 固定 fetch/rrf 參數（使用當前 production 設定）
FETCH_N    = 30
RRF_TOP_N  = 10
EVAL_KS    = [5, 10]

# 掃描的 ef 值（-1 = Qdrant 自動預設，None = 暴力精確搜索）
EF_VALUES = [
    ("auto (default)", None, False),   # Qdrant 預設，不傳 hnsw_ef
    ("ef=64",          64,   False),
    ("ef=128",         128,  False),
    ("ef=200",         200,  False),
    ("ef=500",         500,  False),
    ("exact (brute)",  None, True),    # exact=True，100% 精確，最慢
]


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

    for label, ef, exact in EF_VALUES:
        accum      = defaultdict(list)
        latencies  = []

        for eq in encoded_queries:
            # 建立 SearchParams
            if exact:
                search_params = models.SearchParams(exact=True)
            elif ef is not None:
                search_params = models.SearchParams(hnsw_ef=ef, exact=False)
            else:
                search_params = None  # Qdrant 預設

            t0 = time.perf_counter()
            points = client.query_points(
                collection_name=COLLECTION_NAME,
                prefetch=[
                    models.Prefetch(
                        query=eq["q_dense"],
                        using=DENSE_VECTOR_NAME,
                        limit=FETCH_N,
                        params=search_params,
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=eq["s_indices"], values=eq["s_values"],
                        ),
                        using=SPARSE_VECTOR_NAME,
                        limit=FETCH_N,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=RRF_TOP_N,
                with_payload=["source"],
            ).points
            latencies.append(time.perf_counter() - t0)

            retrieved = unique_sources(points)
            relevant  = eq["relevant"]

            for k in EVAL_KS:
                accum[f"recall@{k}"].append(recall_at_k(retrieved, relevant, k))
            accum["mrr@10"].append(rr_at_k(retrieved, relevant, 10))

        row = {"label": label, "ef": ef if ef else ("brute" if exact else "auto")}
        for metric, vals in accum.items():
            row[metric] = round(sum(vals) / len(vals), 4)
        row["avg_latency_ms"] = round(sum(latencies) / len(latencies) * 1000, 2)
        results.append(row)

        r5  = row.get("recall@5",  0)
        r10 = row.get("recall@10", 0)
        mrr = row["mrr@10"]
        lat = row["avg_latency_ms"]
        print(f"  {label:<20}  R@5={r5:.4f}  R@10={r10:.4f}  MRR@10={mrr:.4f}  lat={lat:.1f}ms")

    return results


def print_table(results: list[dict]) -> None:
    print("\n" + "=" * 80)
    print("HNSW ef SWEEP RESULTS — Dense Prefetch in Hybrid Retrieval")
    print(f"  Fixed: FETCH_N={FETCH_N}, RRF_TOP_N={RRF_TOP_N}")
    print("=" * 80)
    header = f"{'ef config':<22}  {'R@5':>7}  {'R@10':>7}  {'MRR@10':>8}  {'lat(ms)':>9}"
    print(header)
    print("-" * 80)

    baseline = results[0]  # auto (default)

    for r in results:
        delta_r5 = r.get("recall@5", 0) - baseline.get("recall@5", 0)
        delta_str = f"  ({delta_r5:+.4f})" if r["label"] != baseline["label"] else "  (baseline)"
        print(
            f"{r['label']:<22}  "
            f"{r.get('recall@5',0):>7.4f}  "
            f"{r.get('recall@10',0):>7.4f}  "
            f"{r['mrr@10']:>8.4f}  "
            f"{r['avg_latency_ms']:>9.1f}"
            f"{delta_str}"
        )

    print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", default="eval/eval_set.json")
    args = parser.parse_args()

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

    print(f"[INFO] Running {len(EF_VALUES)} ef configurations...\n")

    results = run_sweep(eval_set, client, bge_m3)
    print_table(results)

    out_path = Path("eval/hnsw_sweep_results.json")
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[INFO] Results saved to {out_path}")
    print("\n[NOTE] ef 只影響查詢時的 ANN 搜索寬度，無需重建索引。")
    print("       ef_construction / M 才需要重建 collection（見 data_update.py）。")


if __name__ == "__main__":
    main()
