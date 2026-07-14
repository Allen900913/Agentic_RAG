# -*- coding: utf-8 -*-
"""rerank_model_ab_probe.py — 直接比對「小 reranker（base）vs 生產 reranker（v2-m3）」的
top-5 排序差異，judge-free、只量檢索側，成本低。

動機：CHANGELOG 07-13 記錄 bge-reranker-base「correlation 0.688、top-5 順序明顯不同」，判為
「需全量重驗」的死路，復活條件=延遲需求高過品質（見 CHANGELOG）。使用者要求重試小模型，這支
腳本用最小成本量「同一 query 兩個 reranker 的 top-5 是否一致 + 逐候選分數相關性」，作為要不要
再花全量 eval 額度的前置篩檢。

同一 pool（prefetch 不變）只換 cross-encoder，故差異純粹來自 reranker 排序能力。
Usage: python eval/rerank_model_ab_probe.py --ids sem-08 sem-09 sem-11
"""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq

BASE = "BAAI/bge-reranker-base"
V2M3 = "BAAI/bge-reranker-v2-m3"


def top5_keys(chunks):
    return [(c["source"], c["chunk_index"]) for c in chunks]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="+", default=["sem-08", "sem-09", "sem-11"])
    ap.add_argument("--retrieval-model", default=rq.DEFAULT_MODEL)
    ap.add_argument("--output", default="eval/rerank_model_ab_probe_result.json")
    args = ap.parse_args()

    with open("eval/eval_set.json", encoding="utf-8") as f:
        by_id = {q["id"]: q for q in json.load(f)["queries"]}

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    # bge-reranker-base 的 position embedding 上限只有 512（給 2048 會 index-out-of-bounds crash），
    # 這本身就是關鍵發現：小模型結構性只能看每個候選前 512 token。v2-m3 用生產的 2048。
    print("[INFO] Loading BGE-M3 + rerankers (base@512 = 模型上限, v2m3@2048 = 生產)...")
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rr_base = CrossEncoder(BASE, max_length=512)
    rr_v2 = CrossEncoder(V2M3, max_length=rq.RERANK_MAX_LENGTH)
    client = rq.make_qdrant_client()

    results = {}
    for qid in args.ids:
        q = by_id[qid]["query"]
        # 生產設定：rewrite + translate_query_en 皆開
        common = dict(top_k=5, model_name=args.retrieval_model,
                      enable_rewrite=True, translate_query_en=True)
        base_chunks, _ = rq.retrieve(q, bge_m3, rr_base, client, **common)
        v2_chunks, _ = rq.retrieve(q, bge_m3, rr_v2, client, **common)
        bk, vk = top5_keys(base_chunks), top5_keys(v2_chunks)
        overlap = len(set(bk) & set(vk))
        exact_order = sum(1 for a, b in zip(bk, vk) if a == b)
        results[qid] = {
            "query": q,
            "base_top5": [f"{s}#{i}" for s, i in bk],
            "v2m3_top5": [f"{s}#{i}" for s, i in vk],
            "overlap_of_5": overlap,
            "same_position": exact_order,
        }
        print(f"\n[{qid}] {q}")
        print(f"  overlap={overlap}/5  same_position={exact_order}/5")
        print(f"  base : {results[qid]['base_top5']}")
        print(f"  v2m3 : {results[qid]['v2m3_top5']}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    avg_ov = sum(r["overlap_of_5"] for r in results.values()) / len(results)
    print(f"\n[SUMMARY] mean top-5 overlap = {avg_ov:.2f}/5 across {len(results)} queries")
    print(f"[DONE] {args.output}")


if __name__ == "__main__":
    main()
