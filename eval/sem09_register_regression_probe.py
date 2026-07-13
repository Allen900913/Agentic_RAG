# -*- coding: utf-8 -*-
"""E0+E1 合併探針：sem-09（Google AI/搜尋競爭策略）news chunk 排序回歸診斷。

背景：07-08 clean A/B（gen=120b, retrieval=70b, no translate_en）sem-09 = 0.9,
crit_miss_rate=0。07-11 加入 rewrite 語域規則後，使用者手動查證同一題的候選池，
發現含 "Gemini" 的新聞 chunk（GOOGL_News_20260612_01.txt, chunk_index=1）已進入
candidate pool（RRF rank #6），但最終 rerank top-5 全被 10-K 佔滿——news chunk
在 rerank 層被刷掉。本探針要回答兩個問題：

  E0（歸因）：pool 組成本身有沒有變？是 rewrite 語域變體把更多財報 chunk 塞進
    候選池、稀釋掉 news 的排名機會（pool-dilution），還是 pool 本身沒變、純粹
    是 rerank 分數的問題？→ 用同一個 query，比較 enable_rewrite=False/True
    （translate_query_en 固定 False，隔離 rewrite 這一個變因）。

  E1（rerank 語域）：固定同一個 pool（用 rewrite=True 產生的、最大的候選集），
    只換 cross-encoder 評分用的 query 語域（原始中文 / 財報翻譯 / 中性英文），
    看 target chunk 的 rerank 排名怎麼變。→ 隔離「rerank query 語域」這個變因，
    與 translate_query_en=True/False 的官方開關直接對應。

Usage:
  python eval/sem09_register_regression_probe.py --retrieval-model llama-3.3-70b-versatile
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq

TARGET_SOURCE = "GOOGL_News_20260612_01.txt"
TARGET_CHUNK_INDEX = 1  # 含 "Gemini" ×2 的段落（人工查證，見 CHANGELOG 2026-07-11）

QUERY = "Google 在 AI 與搜尋領域的競爭策略？"

# E1 用的三種 rerank query 語域（原始中文 / 財報腔翻譯 / 中性口語英文，人工撰寫的對照組）
REGISTER_VARIANTS = {
    "raw_zh":       QUERY,
    "plain_en":     "What is Google's competitive strategy in AI and search?",
    # formal_en 用 rq.translate_query_to_english() 動態產生，見下方
}


def find_target_in_pool(points):
    """pool 是 ScoredPoint list（見 rq.retrieve return_pool）。"""
    for i, p in enumerate(points, start=1):
        pl = p.payload or {}
        if pl.get("source") == TARGET_SOURCE and pl.get("chunk_index") == TARGET_CHUNK_INDEX:
            return True, i, p
    return False, None, None


def find_target_in_topk(chunks):
    """chunks 是 rq.retrieve() 回傳的 enriched dict list（已含 'source'/'chunk_index' key）。"""
    for i, c in enumerate(chunks, start=1):
        if c.get("source") == TARGET_SOURCE and c.get("chunk_index") == TARGET_CHUNK_INDEX:
            return True, i, c
    return False, None, None


def source_type_breakdown(chunks):
    from collections import Counter
    return dict(Counter(c["source_type"] for c in chunks))


def main():
    parser = argparse.ArgumentParser(description="sem-09 news chunk 排序回歸：E0(pool 組成) + E1(rerank 語域)")
    parser.add_argument("--retrieval-model", default=rq.DEFAULT_MODEL,
                         help="retrieve() 內部 filter/rewrite/translate 呼叫用的 model，"
                              "須與生產/主要 eval 腳本一致（見 CLAUDE.md model_name 陷阱）")
    parser.add_argument("--output", default="eval/sem09_register_regression_probe_result.json")
    args = parser.parse_args()

    print("[INFO] Loading BGE-M3 + reranker...")
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rerank_model = CrossEncoder(rq.RERANK_MODEL)
    client = rq.make_qdrant_client()

    results = {"query": QUERY, "target": f"{TARGET_SOURCE}#{TARGET_CHUNK_INDEX}"}

    # ══════════════════════════════════════════════════════════════════
    # E0: pool 組成 attribution — enable_rewrite False vs True（translate_query_en 固定 False）
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}\n[E0] Pool-composition attribution (translate_query_en=False)\n{'='*80}")
    e0_results = {}
    for label, enable_rewrite in [("rewrite_off", False), ("rewrite_on", True)]:
        chunks, _note, pool = rq.retrieve(
            QUERY, bge_m3, rerank_model, client,
            top_k=rq.DEFAULT_TOP_K, model_name=args.retrieval_model,
            enable_rewrite=enable_rewrite, translate_query_en=False,
            return_pool=True,
        )
        pool_found, pool_rank, _ = find_target_in_pool(pool)
        topk_found, topk_rank, _ = find_target_in_topk(chunks)
        breakdown = source_type_breakdown(chunks)
        e0_results[label] = {
            "pool_size": len(pool),
            "topk_size": len(chunks),
            "target_in_pool": pool_found,
            "target_pool_rank": pool_rank,
            "target_in_topk": topk_found,
            "target_topk_rank": topk_rank,
            "topk_source_type_breakdown": breakdown,
        }
        print(f"\n  [{label}] pool_size={len(pool)} target_in_pool={pool_found}(rank={pool_rank}) "
              f"target_in_topk={topk_found}(rank={topk_rank}) topk_breakdown={breakdown}")
    results["E0_pool_attribution"] = e0_results

    # ══════════════════════════════════════════════════════════════════
    # E1: rerank 語域 probe — 固定 rewrite_on 的 pool，只換 rerank 評分 query
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}\n[E1] Rerank-register probe (fixed pool from rewrite_on)\n{'='*80}")
    _, _, pool_for_e1 = rq.retrieve(
        QUERY, bge_m3, rerank_model, client,
        top_k=rq.DEFAULT_TOP_K, model_name=args.retrieval_model,
        enable_rewrite=True, translate_query_en=False,
        return_pool=True,
    )
    formal_en = rq.translate_query_to_english(QUERY, args.retrieval_model)
    variants = dict(REGISTER_VARIANTS)
    variants["formal_en_translate"] = formal_en

    docs = [(p.payload or {}).get("document", "") for p in pool_for_e1]
    pool_meta = [(p.payload or {}) for p in pool_for_e1]

    e1_results = {}
    for label, q_text in variants.items():
        cross_input = [[q_text, doc] for doc in docs]
        scores = rerank_model.predict(cross_input)
        scores = scores.tolist() if hasattr(scores, "tolist") else list(scores)
        # 依分數排序，找 target 排名
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        target_rank = None
        target_score = None
        for rank, idx in enumerate(ranked, start=1):
            pl = pool_meta[idx]
            if pl.get("source") == TARGET_SOURCE and pl.get("chunk_index") == TARGET_CHUNK_INDEX:
                target_rank = rank
                target_score = scores[idx]
                break
        # top-5 依這個 query 語域排出來的 source_type 組成
        top5_sources = [pool_meta[idx].get("source", "?") for idx in ranked[:rq.DEFAULT_TOP_K]]
        top5_types = [rq.infer_source_type(s) for s in top5_sources]
        e1_results[label] = {
            "query_text": q_text,
            "target_rerank_rank": target_rank,
            "target_rerank_score": target_score,
            "in_top5": target_rank is not None and target_rank <= rq.DEFAULT_TOP_K,
            "top5_source_types": top5_types,
        }
        print(f"\n  [{label}] query={q_text!r}\n"
              f"    target_rank={target_rank} score={target_score} in_top5={e1_results[label]['in_top5']}\n"
              f"    top5_types={top5_types}")

    results["E1_rerank_register"] = e1_results
    results["pool_size_for_E1"] = len(pool_for_e1)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {args.output}")


if __name__ == "__main__":
    main()
