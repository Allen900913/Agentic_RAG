"""
eval_two_stage.py — 兩階段對照：候選池 recall（精排前）vs top-k 指標（精排後）

核心問題：多路召回在哪個階段貢獻增益、又在哪個階段流失？

  Stage 1（精排前）：答案 chunk 有沒有進候選池？
    pool_size       — union 後的候選池大小
    pool_file_R     — 相關來源在池中的比例（file 粒度 recall，越高越好）
    pool_ckpt_R     — LLM judge：rubric checkpoints 在「整個候選池」裡的加權覆蓋率
                      （chunk 粒度 recall——答案 chunk 有沒有進 union，這是旋鈕
                      「加大 FETCH_N / 變體擴召回」效果真正現形的地方）。
                      判整池成本高，用 --pool-judge-cap 限制送 judge 的段數。

  Stage 2（精排後，top-k）：reranker 有沒有把答案排進 top-k？
    file_P / file_R — 來源精度 / 召回（沿用 eval_chunk_recall 的定義）
    chunk_P         — LLM judge：top-k chunk 中有幾個真正有用？
    ckpt_R          — LLM judge：rubric checkpoints 加權覆蓋率
    crit_miss       — 有沒有 critical checkpoint 沒被 cover？

對照四個條件（每題都跑全部）：
  1. no-rewrite         — 原始 query，無改寫
  2. rewrite+cap=3      — query rewrite + 每變體只貢獻 top-3
  3. rewrite+no-cap     — query rewrite + 變體不設 cap（全 union）
  4. rewrite+RRF-fusion — query rewrite + 跨路 RRF 投票

「Stage 1 ↑ Stage 2 ↓」= 瓶頸在 reranker/budget，不在召回策略。
「Stage 1 ↓」= 答案根本沒進候選池，調 reranker 沒用，要調召回。

Usage:
  python eval/eval_two_stage.py
  python eval/eval_two_stage.py --limit 10            # 前 10 題試跑
  python eval/eval_two_stage.py --no-llm-judge        # 跳過 LLM judge（只量 file 層）
  python eval/eval_two_stage.py --judge-model llama-3.3-70b-versatile
  python eval/eval_two_stage.py --category semantic   # 只跑特定 category
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
from eval_chunk_recall import (
    judge_chunk_relevance,
    judge_checkpoint_coverage,
    MAX_PASSAGE_CHARS,
)

# ── 四個對照條件 ──────────────────────────────────────────────────────────────
CONDITIONS = [
    {
        "name":               "no-rewrite",
        "enable_rewrite":     False,
        "rewrite_merge_top_n": None,
        "rewrite_fusion":     False,
    },
    {
        "name":               "rewrite+cap=3",
        "enable_rewrite":     True,
        "rewrite_merge_top_n": 3,
        "rewrite_fusion":     False,
    },
    {
        "name":               "rewrite+no-cap",
        "enable_rewrite":     True,
        "rewrite_merge_top_n": None,
        "rewrite_fusion":     False,
    },
    {
        "name":               "rewrite+RRF-fusion",
        "enable_rewrite":     True,
        "rewrite_merge_top_n": None,
        "rewrite_fusion":     True,
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1：候選池 recall（精排前）
# ══════════════════════════════════════════════════════════════════════════════

def pool_file_recall(pool: list, relevant: set) -> tuple[float, int]:
    """回傳 (recall, pool_size)。
    pool 是 retrieve(..., return_pool=True) 回傳的第三個元素（ScoredPoint list）。
    recall = 池中獨立 source 與 relevant 的交集比例。"""
    if not relevant:
        return 0.0, len(pool)
    pool_sources = {(p.payload or {}).get("source", "") for p in pool}
    matched = pool_sources & relevant
    return len(matched) / len(relevant), len(pool)


# ══════════════════════════════════════════════════════════════════════════════
# 主 evaluate loop
# ══════════════════════════════════════════════════════════════════════════════

def evaluate(eval_set: dict, client, bge_m3, rerank_model,
             top_k: int, judge_model: str, query_model: str,
             limit: int | None = None, use_llm_judge: bool = True,
             category: str | None = None,
             conditions: list[str] | None = None) -> dict:

    queries = eval_set["queries"]
    if category:
        queries = [q for q in queries if q["category"] == category]
    if limit:
        queries = queries[:limit]

    print("[INFO] Snapshotting source filenames for glob expansion...")
    all_sources = collect_all_sources(client)
    print(f"[INFO] Found {len(all_sources)} unique source files in collection.\n")

    active_conds = [c for c in CONDITIONS if conditions is None or c["name"] in conditions]
    cond_names = [c["name"] for c in active_conds]

    # 累計器：stage1 + stage2
    s1_acc = {n: {"pool_file_R": [], "pool_size": []} for n in cond_names}
    s2_acc = {n: {
        "file_P": [], "file_R": [],
        "chunk_P": [], "ckpt_R": [], "crit_miss": [],
    } for n in cond_names}

    traces = []

    for q in queries:
        qid      = q["id"]
        cat      = q["category"]
        query_str = q["query"]
        relevant  = expand_relevant(q["relevant"], all_sources)
        rubric    = q.get("rubric")
        checkpoints = (rubric or {}).get("must_include", []) if rubric else []
        # rubric 權重每題算一次，Stage 1（pool）與 Stage 2（top-k）共用
        weights = [cp.get("weight", 1) for cp in checkpoints]
        total_w = sum(weights) or 1

        print(f"\n  [{qid:>7}] ({cat:8}) {query_str[:55]}")
        entry = {"id": qid, "category": cat, "query": query_str, "conditions": {}}

        for cond in active_conds:
            cname = cond["name"]

            result = rq.retrieve(
                query_str, bge_m3, rerank_model, client,
                top_k=top_k,
                model_name=query_model,
                enable_rewrite=cond["enable_rewrite"],
                rewrite_merge_top_n=cond["rewrite_merge_top_n"],
                rewrite_fusion=cond["rewrite_fusion"],
                return_pool=True,
            )
            chunks, fallback_note, pool = result

            # ── Stage 1：候選池（精排前）────────────────────────────────────
            p_recall, p_size = pool_file_recall(pool, relevant)
            s1_acc[cname]["pool_file_R"].append(p_recall)
            s1_acc[cname]["pool_size"].append(p_size)

            # ── Stage 2：top-k（精排後）─────────────────────────────────────
            unique_srcs = list({c["source"] for c in chunks})
            file_p = (sum(1 for s in unique_srcs if s in relevant) / len(unique_srcs)
                      if unique_srcs else 0.0)
            file_r = (len(set(unique_srcs) & relevant) / len(relevant)
                      if relevant else 0.0)

            chunk_p = None
            ckpt_r  = None
            crit    = None

            if use_llm_judge:
                relevance = judge_chunk_relevance(query_str, chunks, judge_model)
                chunk_p = sum(relevance) / len(relevance) if relevance else 0.0

                if checkpoints:
                    covered = judge_checkpoint_coverage(query_str, checkpoints, chunks, judge_model)
                    ckpt_r  = sum(w for w, c in zip(weights, covered) if c) / total_w
                    crit    = any(cp.get("is_critical") and not c
                                  for cp, c in zip(checkpoints, covered))

            s2_acc[cname]["file_P"].append(file_p)
            s2_acc[cname]["file_R"].append(file_r)
            if chunk_p is not None:
                s2_acc[cname]["chunk_P"].append(chunk_p)
            if ckpt_r is not None:
                s2_acc[cname]["ckpt_R"].append(ckpt_r)
            if crit is not None:
                s2_acc[cname]["crit_miss"].append(1.0 if crit else 0.0)

            entry["conditions"][cname] = {
                "pool_size":    p_size,
                "pool_file_R":  round(p_recall, 4),
                "file_P":       round(file_p, 4),
                "file_R":       round(file_r, 4),
                "chunk_P":      round(chunk_p, 4)  if chunk_p is not None else None,
                "ckpt_R":       round(ckpt_r, 4)   if ckpt_r  is not None else None,
                "crit_miss":    crit,
                "fallback_note": fallback_note,
            }

            # 每題每條件的單行進度
            stage2_str = (
                f"file_P={file_p:.2f} file_R={file_r:.2f}"
                + (f" chunk_P={chunk_p:.2f}" if chunk_p is not None else "")
                + (f" ckpt_R={ckpt_r:.2f}"   if ckpt_r  is not None else "")
                + (" ⚠CRIT" if crit else "")
            )
            print(f"    {cname:<22} pool={p_size:>3} poolR={p_recall:.2f}  {stage2_str}")

        traces.append(entry)

    def _mean(xs):
        return round(statistics.mean(xs), 4) if xs else None

    summary = {}
    for cname in cond_names:
        summary[cname] = {
            # Stage 1
            "pool_file_R":  _mean(s1_acc[cname]["pool_file_R"]),
            "avg_pool_size": _mean(s1_acc[cname]["pool_size"]),
            # Stage 2
            "file_P":       _mean(s2_acc[cname]["file_P"]),
            "file_R":       _mean(s2_acc[cname]["file_R"]),
            "chunk_P":      _mean(s2_acc[cname]["chunk_P"])    if s2_acc[cname]["chunk_P"]    else None,
            "ckpt_R":       _mean(s2_acc[cname]["ckpt_R"])     if s2_acc[cname]["ckpt_R"]     else None,
            "crit_miss":    _mean(s2_acc[cname]["crit_miss"])  if s2_acc[cname]["crit_miss"]  else None,
            "n_queries":    len(s1_acc[cname]["pool_file_R"]),
            "n_with_ckpt":  len(s2_acc[cname]["ckpt_R"]),
        }

    return {"summary": summary, "traces": traces}


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def print_report(report: dict, use_llm_judge: bool) -> None:
    summary = report["summary"]
    cond_names = list(summary.keys())
    width = 100

    print(f"\n{'═' * width}")
    print("兩階段對照：候選池 recall（精排前）vs top-k 指標（精排後）")
    print(f"{'═' * width}\n")

    # Header
    col_w = 22
    h1 = f"  {'Condition':<{col_w}}"
    h2 = f"  {'Condition':<{col_w}}"
    sep = f"  {'-' * col_w}"

    stage1_cols = [("pool_file_R", "poolR"), ("avg_pool_size", "pool_n")]
    stage2_cols = [("file_P", "file_P"), ("file_R", "file_R"),
                   ("chunk_P", "chunk_P"), ("ckpt_R", "ckpt_R"), ("crit_miss", "crit_miss")]
    if not use_llm_judge:
        stage2_cols = [("file_P", "file_P"), ("file_R", "file_R")]

    all_cols = stage1_cols + stage2_cols
    for _, label in all_cols:
        h1 += f" {'':>9}"
        h2 += f" {label:>9}"
        sep += f" {'-' * 9}"

    # Stage 1 / Stage 2 span labels
    s1_span = "── Stage 1: pre-rerank pool ──"
    s2_span = "── Stage 2: post-rerank top-k ──"
    print(f"  {'':<{col_w}} {s1_span:>{len(stage1_cols)*10}} {s2_span:>{len(stage2_cols)*10}}")
    print(h2)
    print(sep)

    for cname in cond_names:
        s = summary[cname]
        row = f"  {cname:<{col_w}}"
        for key, _ in all_cols:
            val = s.get(key)
            if val is None:
                row += f" {'n/a':>9}"
            elif key in ("pool_file_R", "file_P", "file_R", "chunk_P", "ckpt_R", "crit_miss"):
                row += f" {val*100:8.1f}%"
            else:
                row += f" {val:9.1f}"
        print(row)

    print(sep)
    n = summary[cond_names[0]]["n_queries"]
    n_ckpt = summary[cond_names[0]]["n_with_ckpt"]
    print(f"\n  n_queries={n}  n_with_checkpoints={n_ckpt}\n")

    # 自動分析：哪個條件最好、Stage1→Stage2 的轉化損失
    print(f"{'─' * width}")
    print("分析提示")
    print(f"{'─' * width}")
    print("  pool_file_R（Stage 1）= 答案來源有沒有進候選池（file 粒度）。這個高代表召回策略正確。")
    print("  ckpt_R     （Stage 2）= 精排後 top-k 是否還保留足夠 rubric 證據。")
    print("  Stage1 ↑ Stage2 ↓ → 瓶頸在 reranker 或 top-k budget，不在召回。")
    print("  Stage1 ↓           → 答案根本沒進候選池，調 reranker 沒用，要調 FETCH_N / 召回策略。")

    # 找出 Stage1 vs Stage2 落差最大的條件（file 粒度 + chunk 粒度）
    print()
    for cname in cond_names:
        s = summary[cname]
        pr, fr = s.get("pool_file_R"), s.get("file_R")
        if pr is not None and fr is not None:
            print(f"  {cname:<22} file_R gap = {(pr-fr)*100:+.1f}pp  "
                  f"(pool={pr*100:.1f}% → top-k={fr*100:.1f}%)")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="兩階段 eval：候選池 recall（精排前）vs top-k 指標（精排後）",
    )
    parser.add_argument("--eval-set",    default="eval/eval_set.json")
    parser.add_argument("--output",      default="eval/results_two_stage.json")
    parser.add_argument("--top-k",       type=int, default=rq.DEFAULT_TOP_K)
    parser.add_argument("--judge-model", default=rq.DEFAULT_MODEL,
                        help="LLM used for chunk-relevance / checkpoint-coverage judging")
    parser.add_argument("--query-model", default=rq.DEFAULT_MODEL,
                        help="LLM used inside rag_query.retrieve() for query-filter parsing")
    parser.add_argument("--limit",       type=int, default=None,
                        help="Only run the first N queries")
    parser.add_argument("--category",    default=None,
                        help="Only run queries of this category")
    parser.add_argument("--no-llm-judge", action="store_true",
                        help="跳過 LLM judge（chunk_P / ckpt_R）——只量 file 層，省 API 成本")
    parser.add_argument("--conditions", nargs="+", default=None,
                        help="只跑指定條件，例如 --conditions rewrite+no-cap no-rewrite")
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
        print("[ERROR] Collection is empty. Run: python data_update.py --rebuild")
        sys.exit(1)
    print(f"[INFO] Collection size: {total} chunks")
    print(f"[INFO] top_k={args.top_k}  judge_model={args.judge_model}  "
          f"query_model={args.query_model}  llm_judge={not args.no_llm_judge}\n")
    active = args.conditions
    shown = active if active else [c["name"] for c in CONDITIONS]
    print(f"[INFO] Conditions: {shown}\n")

    report = evaluate(
        eval_set, client, bge_m3, rerank_model,
        top_k=args.top_k,
        judge_model=args.judge_model,
        query_model=args.query_model,
        limit=args.limit,
        use_llm_judge=not args.no_llm_judge,
        category=args.category,
        conditions=active,
    )

    print_report(report, use_llm_judge=not args.no_llm_judge)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[DONE] Detailed traces → {out_path}")


if __name__ == "__main__":
    main()
