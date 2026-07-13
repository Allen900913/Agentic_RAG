"""
eval_retrieval_filtered_unstructured.py — Hard-filter ablation on us_stock_rag_unstructured

針對 data_update_unstructure.py 新增的 filing metadata（fiscal_year /
report_label_year / filing_type / fiscal_period）做 A/B/C 比較：

  1. "nofilter"   — 不套用任何 payload 篩選（baseline）
  2. "fy_only"    — include 條件只比對 fiscal_year 本身（+ IsEmptyCondition
                     讓沒有 filing metadata 的 News/Fundamentals/IncomeStatement
                     chunk 不被誤殺），不比對 report_label_year
  3. "fy_label"   — rag_query.py 目前的完整版：fiscal_year 額外比對
                     report_label_year（檔名年份），同時保留 IsEmptyCondition

用來驗證兩個修法各自的貢獻：
  - fy_only  vs nofilter  → IsEmptyCondition（非filing文件不被誤殺）的效果
  - fy_label vs fy_only   → report_label_year 別名（年份座標系落差）的效果

Usage:
  python eval/eval_retrieval_filtered_unstructured.py
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
from rag_query import (  # noqa: E402
    build_qdrant_filter as build_qdrant_filter_v2,
    QUERY_FILTER_SYSTEM_PROMPT,
    FILTERABLE_FIELDS,
)

from eval.eval_retrieval import (  # noqa: E402
    _has_glob,
    precision_at_k, recall_at_k, f1_at_k, average_precision_at_k,
    reciprocal_rank_at_k, fmt_pct,
)

QDRANT_PATH        = os.getenv("QDRANT_PATH", "./qdrant_db")
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
COLLECTION_NAME    = "us_stock_rag_unstructured"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"

GROQ_MODEL    = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

DEFAULT_KS    = [5, 10, 20]
DEFAULT_FETCH = 100

MODES = ["nofilter", "fy_only", "fy_label"]


# ══════════════════════════════════════════════════════════════════════════════
# Filter variants
# ══════════════════════════════════════════════════════════════════════════════

def build_qdrant_filter_v1(filters: list[dict]):
    """include -> should(field == value, IsEmpty(field))；不比對 report_label_year。
    exclude -> 單純 must_not(field == value)。"""
    from qdrant_client import models

    must, must_not = [], []
    for f in filters:
        field, value = f["field"], f["value"]
        condition = models.FieldCondition(key=field, match=models.MatchValue(value=value))

        if f["polarity"] == "exclude":
            must_not.append(condition)
            continue

        should = [condition, models.IsEmptyCondition(is_empty=models.PayloadField(key=field))]
        must.append(models.Filter(should=should))

    if not must and not must_not:
        return None
    return models.Filter(must=must or None, must_not=must_not or None)


FILTER_BUILDERS = {
    "nofilter": None,
    "fy_only":  build_qdrant_filter_v1,
    "fy_label": build_qdrant_filter_v2,
}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers (collection-scoped copies — eval_retrieval.py's collect_all_sources etc.
# close over its own module-level COLLECTION_NAME, so we keep local copies here)
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


def unique_sources_in_order(points) -> list[str]:
    seen, seen_set = [], set()
    for p in points:
        src = (p.payload or {}).get("source")
        if src and src not in seen_set:
            seen.append(src)
            seen_set.add(src)
    return seen


def parse_query_filters_groq(query: str, groq_client) -> list[dict]:
    """跟 rag_query.parse_query_filters 邏輯相同，但改用 Groq (OpenAI-compatible) 呼叫。"""
    import re as _re

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": QUERY_FILTER_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            temperature=0,
        )
        raw = response.choices[0].message.content
        raw = _re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=_re.MULTILINE).strip()
        parsed = json.loads(raw)
    except Exception as e:
        print(f"WARN  - query-filter parsing failed ({e!r}); proceeding without filter")
        return []

    filters = parsed.get("filters", []) if isinstance(parsed, dict) else []
    cleaned = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        field    = f.get("field")
        value    = f.get("value")
        polarity = f.get("polarity", "include")
        if field not in FILTERABLE_FIELDS or not value:
            continue
        if polarity not in ("include", "exclude"):
            polarity = "include"
        cleaned.append({"field": field, "value": str(value), "polarity": polarity})
    return cleaned


def hybrid_retrieve_filtered(client, q_dense, q_sparse_indices, q_sparse_values, limit, active_filter):
    from qdrant_client import models

    def _run(f):
        return client.query_points(
            collection_name=COLLECTION_NAME,
            prefetch=[
                models.Prefetch(
                    query=q_dense, using=DENSE_VECTOR_NAME,
                    limit=DEFAULT_FETCH, filter=f,
                ),
                models.Prefetch(
                    query=models.SparseVector(indices=q_sparse_indices, values=q_sparse_values),
                    using=SPARSE_VECTOR_NAME,
                    limit=DEFAULT_FETCH, filter=f,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=["source"],
        ).points

    points = _run(active_filter)
    if not points and active_filter is not None:
        points = _run(None)
    return points


# ══════════════════════════════════════════════════════════════════════════════
# Evaluation
# ══════════════════════════════════════════════════════════════════════════════

def evaluate(eval_set: dict, client, bge_m3, groq_client, ks: list[int]) -> dict:
    queries = eval_set["queries"]
    max_k   = max(ks)

    print("[INFO] Snapshotting source filenames for glob expansion...")
    all_sources = collect_all_sources(client)
    print(f"[INFO] Found {len(all_sources)} unique source files in collection.\n")

    results: dict = {
        mode: {cat: defaultdict(list) for cat in ["semantic", "lexical", "mixed", "overall"]}
        for mode in MODES
    }
    traces = []
    skipped_queries: list[str] = []

    for q in queries:
        qid       = q["id"]
        category  = q["category"]
        query_str = q["query"]
        raw_patterns = q["relevant"]

        relevant = expand_relevant(raw_patterns, all_sources)
        if not relevant:
            print(f"  [{qid}] [SKIP] no relevant sources matched after glob expansion "
                  f"(patterns: {raw_patterns}) — skipping.")
            skipped_queries.append(qid)
            continue

        detected_filters = parse_query_filters_groq(query_str, groq_client)

        encoded = bge_m3.encode(
            [query_str], return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        q_dense  = encoded["dense_vecs"][0].tolist()
        q_sparse = encoded["lexical_weights"][0]
        s_idx    = [int(tok) for tok in q_sparse.keys()]
        s_val    = [float(w)  for w   in q_sparse.values()]

        chunk_limit = max_k * 5

        per_mode_sources = {}
        for mode in MODES:
            builder = FILTER_BUILDERS[mode]
            active_filter = builder(detected_filters) if builder else None
            pts = hybrid_retrieve_filtered(client, q_dense, s_idx, s_val, chunk_limit, active_filter)
            per_mode_sources[mode] = unique_sources_in_order(pts)

        trace_entry = {
            "id":       qid,
            "category": category,
            "query":    query_str,
            "detected_filters": detected_filters,
            "relevant_patterns": raw_patterns,
            "relevant_expanded": sorted(relevant),
            "retrieved": {m: per_mode_sources[m][:max_k] for m in MODES},
            "metrics":  {},
        }

        for mode in MODES:
            srcs = per_mode_sources[mode]
            mode_metrics = {}
            for k in ks:
                p = precision_at_k(srcs, relevant, k)
                r = recall_at_k(srcs, relevant, k)
                f = f1_at_k(srcs, relevant, k)
                for name, val in (("precision", p), ("recall", r), ("f1", f)):
                    key = f"{name}@{k}"
                    results[mode][category][key].append(val)
                    results[mode]["overall"][key].append(val)
                    mode_metrics[key] = val
            ap  = average_precision_at_k(srcs, relevant, max_k)
            mrr = reciprocal_rank_at_k(srcs, relevant, max_k)
            for name, val in ((f"ap@{max_k}", ap), (f"mrr@{max_k}", mrr)):
                results[mode][category][name].append(val)
                results[mode]["overall"][name].append(val)
                mode_metrics[name] = val
            trace_entry["metrics"][mode] = mode_metrics

        traces.append(trace_entry)
        mid_k = ks[len(ks) // 2] if len(ks) > 1 else ks[0]
        filt_str = detected_filters if detected_filters else "-"
        print(f"  [{qid:>7}] ({category:8}) {query_str[:40]:<40} filters={filt_str}")
        for mode in MODES:
            m = trace_entry["metrics"][mode]
            print(
                f"            {mode:<9} "
                f"P@{mid_k}={m[f'precision@{mid_k}']:.3f}  "
                f"R@{mid_k}={m[f'recall@{mid_k}']:.3f}  "
                f"F1@{mid_k}={m[f'f1@{mid_k}']:.3f}  "
                f"AP@{max_k}={m[f'ap@{max_k}']:.3f}  "
                f"MRR@{max_k}={m[f'mrr@{max_k}']:.3f}"
            )

    summary: dict = {mode: {cat: {} for cat in ["semantic", "lexical", "mixed", "overall"]}
                     for mode in MODES}
    for mode in MODES:
        for cat in ["semantic", "lexical", "mixed", "overall"]:
            for metric, vals in results[mode][cat].items():
                summary[mode][cat][metric] = (
                    round(statistics.mean(vals), 4) if vals else 0.0
                )
                summary[mode][cat][f"{metric}_n"] = len(vals)

    n_filtered = sum(1 for t in traces if t["detected_filters"])
    print(f"\n[INFO] Queries with a detected hard filter: {n_filtered} / {len(traces)}")

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


def print_table(summary: dict, ks: list[int]) -> None:
    max_k = max(ks)
    cats  = ["semantic", "lexical", "mixed", "overall"]

    metric_keys: list[str] = []
    for k in ks:
        metric_keys += [f"precision@{k}", f"recall@{k}", f"f1@{k}"]
    metric_keys += [f"ap@{max_k}", f"mrr@{max_k}"]

    def label(m: str) -> str:
        if m.startswith("precision@"): return f"P@{m.split('@')[1]}"
        if m.startswith("recall@"):    return f"R@{m.split('@')[1]}"
        if m.startswith("f1@"):        return f"F1@{m.split('@')[1]}"
        if m.startswith("ap@"):        return f"MAP@{m.split('@')[1]}"
        if m.startswith("mrr@"):       return f"MRR@{m.split('@')[1]}"
        return m

    width = 96
    print(f"\n{'=' * width}")
    print("HARD-FILTER ABLATION (us_stock_rag_unstructured) — nofilter / fy_only / fy_label")
    print(f"(mean over queries per category, all metrics shown as percentages)")
    print(f"{'=' * width}\n")

    for cat in cats:
        n_key = f"{metric_keys[0]}_n"
        n = summary["nofilter"][cat].get(n_key, 0)
        if n == 0:
            continue
        print(f"  Category: {cat.upper()}  (n={n})")
        header = f"    {'Metric':<10}{'nofilter':>12}{'fy_only':>12}{'fy_label':>12}    fy_only-Δ   fy_label-Δ"
        print(header)
        print(f"    {'-' * 10}" + ("-" * 12) * 3 + "    ---------   ----------")
        for m in metric_keys:
            row = f"    {label(m):<10}"
            v0 = summary["nofilter"][cat].get(m, 0.0)
            v1 = summary["fy_only"][cat].get(m, 0.0)
            v2 = summary["fy_label"][cat].get(m, 0.0)
            row += f"{fmt_pct(v0):>12}{fmt_pct(v1):>12}{fmt_pct(v2):>12}"
            d1, d2 = v1 - v0, v2 - v0
            row += f"   {'+' if d1>=0 else ''}{d1*100:+.1f}pp     {'+' if d2>=0 else ''}{d2*100:+.1f}pp"
            print(row)
        print()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Hard-filter ablation (nofilter / fy_only / fy_label) on us_stock_rag_unstructured",
    )
    parser.add_argument("--eval-set", default="eval/eval_set.json")
    parser.add_argument("--output", default="eval/results_unstructured_filtered.json")
    parser.add_argument("--ks", default="5,10,20")
    args = parser.parse_args()

    ks = sorted({int(k.strip()) for k in args.ks.split(",") if k.strip()})

    eval_path = Path(args.eval_set)
    if not eval_path.exists():
        print(f"[ERROR] Eval set not found: {eval_path}")
        sys.exit(1)

    with open(eval_path, "r", encoding="utf-8") as f:
        eval_set = json.load(f)
    n_queries = len(eval_set["queries"])
    print(f"[INFO] Loaded {n_queries} queries from {eval_path}")

    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel
    from openai import OpenAI

    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        print("[ERROR] GROQ_API_KEY not set in .env")
        sys.exit(1)
    groq_client = OpenAI(api_key=groq_api_key, base_url=GROQ_BASE_URL)
    print(f"[INFO] Using Groq model for query-filter detection: {GROQ_MODEL}")

    print(f"[INFO] Loading BGE-M3: {EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)

    # 依 .env 的 QDRANT_URL 自動選 Docker server / local path（Docker 遷移後 local
    # ./qdrant_db 已是凍結舊快照，見 CHANGELOG 2026-07-07 Qdrant client bug）
    from rag_query import make_qdrant_client
    client = make_qdrant_client()

    try:
        total = client.count(collection_name=COLLECTION_NAME, exact=True).count
    except Exception as e:
        print(f"[ERROR] Cannot access collection '{COLLECTION_NAME}': {e}")
        sys.exit(1)
    if total == 0:
        print(f"[ERROR] Collection is empty.")
        sys.exit(1)
    print(f"[INFO] Collection size: {total} chunks\n")

    report = evaluate(eval_set, client, bge_m3, groq_client, ks)
    print_table(report["summary"], ks)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[DONE] Detailed traces written to: {out_path}")


if __name__ == "__main__":
    main()
