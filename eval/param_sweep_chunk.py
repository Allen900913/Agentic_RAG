"""
param_sweep_chunk.py — Grid search over FETCH_N × RRF_TOP_N_PRIMARY × VARIANT_CAP
以 ckpt_R（LLM judge checkpoint coverage，精排後 top-k）為主要指標。

三個旋鈕：
  FETCH_N          — Qdrant 每路（dense / sparse）prefetch 取多少候選
  RRF_TOP_N_PRIMARY — server-side RRF 融合後保留多少給主查詢的候選池
  VARIANT_CAP      — rewrite+no-cap 模式下，每個改寫變體最多注入幾個 chunk

流程：
  Phase 1（no-rewrite）  → sweep FETCH_N × RRF_TOP_N_PRIMARY
  Phase 2（rewrite+no-cap）→ 固定 Phase 1 最佳組合，sweep VARIANT_CAP

Usage:
  python eval/param_sweep_chunk.py
  python eval/param_sweep_chunk.py --limit 10
  python eval/param_sweep_chunk.py --judge-model llama-3.1-8b-instant
  python eval/param_sweep_chunk.py --output eval/sweep_chunk_results.json
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fnmatch

import rag_query as rq
from eval_chunk_recall import (
    judge_chunk_relevance,
    judge_checkpoint_coverage,
    MAX_PASSAGE_CHARS,
)


def collect_all_sources(client, collection_name: str) -> set[str]:
    sources: set[str] = set()
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection_name, limit=256,
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

# ── Parameter grids ─────────────────────────────────────────────────────────
FETCH_N_VALUES    = [40, 60, 80, 100]
RRF_TOP_N_VALUES  = [20, 40, 60]      # must be ≤ FETCH_N
VARIANT_CAP_VALUES = [3, 5, 8, 15]   # 每個 rewrite 變體注入幾個 chunk（越大池越寬）

DEFAULT_TOP_K     = 5
DEFAULT_JUDGE     = "llama-3.1-8b-instant"
DEFAULT_QUERY_MDL = "llama-3.3-70b-versatile"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mean(lst: list) -> float:
    return sum(lst) / len(lst) if lst else 0.0


def eval_one_config(
    queries: list[dict],
    all_sources: set[str],
    bge_m3,
    rerank_model,
    client,
    *,
    fetch_n: int,
    rrf_top_n: int,
    variant_cap,        # int | None
    enable_rewrite: bool,
    judge_model: str,
    query_model: str,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """跑一組參數組合，回傳該組合的平均 ckpt_R / chunk_P / crit_miss。"""

    # 覆寫 rag_query 模組的全域常數（monkey-patch）
    rq.FETCH_N           = fetch_n
    rq.RRF_TOP_N_PRIMARY = rrf_top_n
    if variant_cap is not None:
        rq.VARIANT_CAP   = variant_cap
        # rewrite_merge_top_n=None → retrieve() 內部 fallback 到 rq.VARIANT_CAP
        # 所以只要 monkey-patch 就夠，傳 None 給 retrieve() 即可

    ckpt_rs, chunk_ps, crits = [], [], []

    for q in queries:
        query_str   = q["query"]
        relevant    = expand_relevant(q.get("relevant", []), all_sources)
        rubric      = q.get("rubric")
        checkpoints = (rubric or {}).get("must_include", []) if rubric else []
        weights     = [cp.get("weight", 1) for cp in checkpoints]
        total_w     = sum(weights) or 1

        try:
            result = rq.retrieve(
                query_str, bge_m3, rerank_model, client,
                top_k=top_k,
                model_name=query_model,
                enable_rewrite=enable_rewrite,
                rewrite_merge_top_n=variant_cap,
                rewrite_fusion=False,   # 固定 no-cap 合併方式
                return_pool=False,
            )
            chunks = result[0] if isinstance(result, tuple) else result
        except Exception as e:
            print(f"    WARN retrieve failed: {e!r}")
            chunks = []

        if not chunks:
            ckpt_rs.append(0.0)
            chunk_ps.append(0.0)
            crits.append(1.0)
            continue

        relevance = judge_chunk_relevance(query_str, chunks, judge_model)
        chunk_p   = sum(relevance) / len(relevance) if relevance else 0.0
        chunk_ps.append(chunk_p)

        if checkpoints:
            covered = judge_checkpoint_coverage(query_str, checkpoints, chunks, judge_model)
            ckpt_r  = sum(w for w, c in zip(weights, covered) if c) / total_w
            crit    = any(cp.get("is_critical") and not c
                          for cp, c in zip(checkpoints, covered))
        else:
            ckpt_r = chunk_p   # 沒有 rubric 的題目，用 chunk_P 代替
            crit   = False

        ckpt_rs.append(ckpt_r)
        crits.append(1.0 if crit else 0.0)

    return {
        "ckpt_R":    round(_mean(ckpt_rs), 4),
        "chunk_P":   round(_mean(chunk_ps), 4),
        "crit_miss": round(_mean(crits),    4),
        "n":         len(queries),
    }


def print_phase_table(title: str, results: list[dict], sort_key: str = "ckpt_R") -> None:
    print(f"\n{'═'*72}")
    print(f"  {title}")
    print(f"{'═'*72}")

    sorted_r = sorted(results, key=lambda r: r.get(sort_key, 0), reverse=True)
    best_val = sorted_r[0].get(sort_key, 0) if sorted_r else 0

    # 動態 header：根據第一列有哪些 key
    if results:
        keys = [k for k in results[0] if k not in ("ckpt_R", "chunk_P", "crit_miss", "n")]
        hdr_params = "  ".join(f"{k:>15}" for k in keys)
        print(f"  {hdr_params}  {'ckpt_R':>8}  {'chunk_P':>8}  {'crit_miss':>9}  {'n':>3}")
        print(f"{'─'*72}")
        for r in sorted_r:
            marker = " ★" if r.get(sort_key, 0) == best_val else ""
            params = "  ".join(f"{str(r.get(k,'?')):>15}" for k in keys)
            print(
                f"  {params}  "
                f"{r.get('ckpt_R',0):>8.4f}  "
                f"{r.get('chunk_P',0):>8.4f}  "
                f"{r.get('crit_miss',0):>9.4f}  "
                f"{r.get('n',0):>3}"
                f"{marker}"
            )
    print(f"{'═'*72}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set",     default="eval/eval_set.json")
    parser.add_argument("--limit",        type=int, default=None,
                        help="只跑前 N 題（預設全跑；建議先用 --limit 10 試速度）")
    parser.add_argument("--top-k",        type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--judge-model",  default=DEFAULT_JUDGE)
    parser.add_argument("--query-model",  default=DEFAULT_QUERY_MDL)
    parser.add_argument("--output",       default="eval/sweep_chunk_results.json")
    parser.add_argument("--skip-phase2",  action="store_true",
                        help="跳過 Phase 2（VARIANT_CAP sweep），只跑 Phase 1")
    args = parser.parse_args()

    eval_path = Path(args.eval_set)
    if not eval_path.exists():
        print(f"[ERROR] Eval set not found: {eval_path}")
        sys.exit(1)

    eval_set = json.loads(eval_path.read_text(encoding="utf-8"))
    queries  = eval_set["queries"]
    if args.limit:
        queries = queries[:args.limit]
    print(f"[INFO] {len(queries)} queries loaded (limit={args.limit})")

    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    print(f"[INFO] Loading BGE-M3: {rq.EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)

    print(f"[INFO] Loading reranker: {rq.RERANK_MODEL}")
    rerank_model = CrossEncoder(rq.RERANK_MODEL)

    print(f"[INFO] Opening Qdrant: {rq.QDRANT_PATH}")
    client = QdrantClient(path=rq.QDRANT_PATH)

    all_sources = collect_all_sources(client, rq.COLLECTION_NAME)

    all_results = {"phase1": [], "phase2": []}

    # ══════════════════════════════════════════════════════════════════════════
    # Phase 1：no-rewrite，sweep FETCH_N × RRF_TOP_N_PRIMARY
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "─"*60)
    print("Phase 1 — no-rewrite: sweep FETCH_N × RRF_TOP_N_PRIMARY")
    print("─"*60)

    phase1_grid = [
        (f, r) for f in FETCH_N_VALUES for r in RRF_TOP_N_VALUES if r <= f
    ]
    print(f"  {len(phase1_grid)} combos × {len(queries)} queries\n")

    for fetch_n, rrf_top_n in phase1_grid:
        label = f"fetch={fetch_n}, rrf_top={rrf_top_n}"
        print(f"  [{label}]")
        metrics = eval_one_config(
            queries, all_sources, bge_m3, rerank_model, client,
            fetch_n=fetch_n,
            rrf_top_n=rrf_top_n,
            variant_cap=None,
            enable_rewrite=False,
            judge_model=args.judge_model,
            query_model=args.query_model,
            top_k=args.top_k,
        )
        row = {"fetch_n": fetch_n, "rrf_top_n": rrf_top_n, **metrics}
        all_results["phase1"].append(row)
        print(f"    → ckpt_R={metrics['ckpt_R']:.4f}  chunk_P={metrics['chunk_P']:.4f}  "
              f"crit_miss={metrics['crit_miss']:.4f}")

    print_phase_table("Phase 1 Results — no-rewrite", all_results["phase1"])

    # 找 Phase 1 最佳組合
    best_p1 = max(all_results["phase1"], key=lambda r: r["ckpt_R"])
    best_fetch_n  = best_p1["fetch_n"]
    best_rrf_top_n = best_p1["rrf_top_n"]
    print(f"\n  ★ Phase 1 best: fetch={best_fetch_n}, rrf_top={best_rrf_top_n}  "
          f"(ckpt_R={best_p1['ckpt_R']:.4f})")

    if args.skip_phase2:
        out = Path(args.output)
        out.write_text(json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n[INFO] Saved to {out}")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # Phase 2：rewrite+no-cap，固定最佳 FETCH_N / RRF_TOP_N，sweep VARIANT_CAP
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "─"*60)
    print(f"Phase 2 — rewrite+no-cap: sweep VARIANT_CAP")
    print(f"  (fixed: fetch={best_fetch_n}, rrf_top={best_rrf_top_n})")
    print("─"*60)
    print(f"  {len(VARIANT_CAP_VALUES)} combos × {len(queries)} queries\n")

    for cap in VARIANT_CAP_VALUES:
        label = f"variant_cap={cap}"
        print(f"  [{label}]")
        metrics = eval_one_config(
            queries, all_sources, bge_m3, rerank_model, client,
            fetch_n=best_fetch_n,
            rrf_top_n=best_rrf_top_n,
            variant_cap=cap,
            enable_rewrite=True,
            judge_model=args.judge_model,
            query_model=args.query_model,
            top_k=args.top_k,
        )
        row = {"variant_cap": str(cap) if cap is not None else "None",
               "fetch_n": best_fetch_n, "rrf_top_n": best_rrf_top_n,
               **metrics}
        all_results["phase2"].append(row)
        print(f"    → ckpt_R={metrics['ckpt_R']:.4f}  chunk_P={metrics['chunk_P']:.4f}  "
              f"crit_miss={metrics['crit_miss']:.4f}")

    print_phase_table("Phase 2 Results — rewrite+no-cap (VARIANT_CAP sweep)",
                      all_results["phase2"])

    best_p2 = max(all_results["phase2"], key=lambda r: r["ckpt_R"])
    print(f"\n  ★ Phase 2 best: variant_cap={best_p2['variant_cap']}  "
          f"(ckpt_R={best_p2['ckpt_R']:.4f})")

    print(f"\n{'═'*60}")
    print(f"  最終建議組合：")
    print(f"    FETCH_N          = {best_fetch_n}")
    print(f"    RRF_TOP_N_PRIMARY = {best_rrf_top_n}")
    print(f"    VARIANT_CAP      = {best_p2['variant_cap']}")
    print(f"    ckpt_R           = {best_p2['ckpt_R']:.4f}")
    print(f"{'═'*60}")

    out = Path(args.output)
    out.write_text(json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[INFO] Saved to {out}")


if __name__ == "__main__":
    main()
