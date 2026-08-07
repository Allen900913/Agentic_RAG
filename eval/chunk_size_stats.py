"""
chunk_size_stats.py — 量測目前 Qdrant collection 裡每個 chunk 的「reranker token 數」分布。

為什麼用 reranker tokenizer 當尺（不是 BGE-M3、不是字元）：
  真正會造成資訊遺失的約束是 cross-encoder rerank 的 RERANK_MAX_LENGTH=2048 截斷
  （rag_query.py）。一個 chunk 有沒有「過大」，唯一有意義的定義就是「送進 reranker 時
  會不會被截掉尾巴」，所以必須用 reranker（BAAI/bge-reranker-v2-m3 = XLM-RoBERTa）自己
  的 tokenizer 來數 token。用別的尺量到的「1200/2048」都對不上真正的截斷點。

量測對象是 payload["document"]（含 metadata header 的完整文字）——那正是 retrieve() 餵給
cross-encoder 的字串（rag_query.retrieve: docs = [p.payload["document"] ...]），不是原始
chunk text，也不是被 embed 的向量。

輸出：
  - 全體 + 分組（chunk_type=table/text、doc_type）的 P50/P90/P95/max、mean
  - >1200 token 與 >2048 token（= 實際 rerank 截斷率）的 chunk 數與比例
  - JSON 存到 experiments/exp0_baseline/chunk_size_stats.json（供跨實驗對照）

用法：
  .venv/Scripts/python.exe eval/chunk_size_stats.py
  .venv/Scripts/python.exe eval/chunk_size_stats.py --collection us_stock_rag_unstructured
"""

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

# 讓 `import rag_query` 能在 eval/ 底下跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import rag_query as rq

RERANK_TRUNCATE = rq.RERANK_MAX_LENGTH   # 2048：實際 rerank 截斷點
SOFT_LIMIT      = 1200                    # chunking 決策的 soft 上限（>此值 Exp3 會觸發 RCTS）


def _percentile(sorted_vals: list[int], pct: float) -> int:
    """nearest-rank percentile（sorted_vals 需已排序、非空）。"""
    if not sorted_vals:
        return 0
    k = max(0, min(len(sorted_vals) - 1, int(round(pct / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def _summarize(token_counts: list[int]) -> dict:
    if not token_counts:
        return {"n": 0}
    s = sorted(token_counts)
    n = len(s)
    over_soft = sum(1 for t in s if t > SOFT_LIMIT)
    over_trunc = sum(1 for t in s if t > RERANK_TRUNCATE)
    return {
        "n": n,
        "mean": round(statistics.mean(s), 1),
        "p50": _percentile(s, 50),
        "p90": _percentile(s, 90),
        "p95": _percentile(s, 95),
        "p99": _percentile(s, 99),
        "max": s[-1],
        f"n_over_{SOFT_LIMIT}": over_soft,
        f"pct_over_{SOFT_LIMIT}": round(100.0 * over_soft / n, 2),
        f"n_over_{RERANK_TRUNCATE}_truncated": over_trunc,
        f"pct_over_{RERANK_TRUNCATE}_truncated": round(100.0 * over_trunc / n, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", default=os.getenv("COLLECTION_NAME", rq.COLLECTION_NAME))
    ap.add_argument("--out", default="experiments/exp0_baseline/chunk_size_stats.json")
    ap.add_argument("--payload-field", default="document",
                    help="要量測 token 數的 payload 欄位（預設 document = 實際送進 reranker 的文字）")
    args = ap.parse_args()

    print(f"[INFO] Reranker tokenizer : {rq.RERANK_MODEL}")
    print(f"[INFO] Truncation limit   : {RERANK_TRUNCATE} tokens (rag_query.RERANK_MAX_LENGTH)")
    print(f"[INFO] Soft limit         : {SOFT_LIMIT} tokens")
    print(f"[INFO] Collection         : {args.collection}")
    print(f"[INFO] Measuring field    : payload['{args.payload_field}']")

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(rq.RERANK_MODEL)

    rq.COLLECTION_NAME = args.collection
    client = rq.make_qdrant_client()

    # scroll 全部 points，只取 payload（不要向量，省頻寬）
    all_counts: list[int] = []
    by_chunk_type: dict[str, list[int]] = defaultdict(list)
    by_doc_type: dict[str, list[int]] = defaultdict(list)
    oversized_examples: list[dict] = []

    offset = None
    scanned = 0
    while True:
        points, offset = client.scroll(
            collection_name=args.collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        texts = [(p.payload or {}).get(args.payload_field, "") or "" for p in points]
        # 一次 batch tokenize，不加特殊 token（純算內容長度）
        encoded = tok(texts, add_special_tokens=False, truncation=False)["input_ids"]
        for p, ids in zip(points, encoded):
            n_tok = len(ids)
            payload = p.payload or {}
            all_counts.append(n_tok)
            by_chunk_type[payload.get("chunk_type", "n/a")].append(n_tok)
            by_doc_type[payload.get("doc_type", "n/a")].append(n_tok)
            if n_tok > RERANK_TRUNCATE:
                oversized_examples.append({
                    "source": payload.get("source"),
                    "chunk_index": payload.get("chunk_index"),
                    "chunk_type": payload.get("chunk_type"),
                    "tokens": n_tok,
                })
        scanned += len(points)
        print(f"\r[SCAN] {scanned} chunks...", end="", flush=True)
        if offset is None:
            break
    print()

    result = {
        "collection": args.collection,
        "payload_field": args.payload_field,
        "reranker_model": rq.RERANK_MODEL,
        "truncate_limit": RERANK_TRUNCATE,
        "soft_limit": SOFT_LIMIT,
        "overall": _summarize(all_counts),
        "by_chunk_type": {k: _summarize(v) for k, v in sorted(by_chunk_type.items())},
        "by_doc_type": {k: _summarize(v) for k, v in sorted(by_doc_type.items())},
        "oversized_examples": sorted(oversized_examples, key=lambda x: -x["tokens"])[:30],
    }

    # ── 印出人看的摘要 ────────────────────────────────────────────────────────
    def _print_row(label: str, s: dict) -> None:
        if s.get("n", 0) == 0:
            print(f"  {label:16} (empty)")
            return
        print(f"  {label:16} n={s['n']:>5}  p50={s['p50']:>5}  p90={s['p90']:>5}  "
              f"p95={s['p95']:>5}  max={s['max']:>5}  "
              f">{SOFT_LIMIT}={s[f'pct_over_{SOFT_LIMIT}']:>5}%  "
              f">{RERANK_TRUNCATE}(截斷)={s[f'pct_over_{RERANK_TRUNCATE}_truncated']:>5}%")

    print("\n" + "=" * 100)
    print("CHUNK TOKEN 分布（reranker tokenizer）")
    print("=" * 100)
    _print_row("ALL", result["overall"])
    print("\n-- by chunk_type --")
    for k, s in result["by_chunk_type"].items():
        _print_row(k, s)
    print("\n-- by doc_type --")
    for k, s in result["by_doc_type"].items():
        _print_row(k, s)

    if oversized_examples:
        print(f"\n-- 被 rerank 截斷（>{RERANK_TRUNCATE} token）的 chunk（前 10 大）--")
        for ex in result["oversized_examples"][:10]:
            print(f"  {ex['tokens']:>5} tok  {ex['source']} #{ex['chunk_index']} ({ex['chunk_type']})")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[SAVED] {out_path}")


if __name__ == "__main__":
    main()
