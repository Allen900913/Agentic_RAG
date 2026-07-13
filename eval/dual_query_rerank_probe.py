# -*- coding: utf-8 -*-
"""驗證「rerank 對每個候選同時用原始 query 與 translate_query_to_english() 翻譯後
的 query 各評一次分、取兩者較高分」是否能同時保住 sem-11（需要 translate 才進
top-5）跟 sem-09（translate 反而把 chunk #139 擠出 top-5）。

背景：見 CHANGELOG 2026-07-12「E5 全量」——`translate_query_en=True` 是全有全無
的開關，救 sem-11、傷 sem-09。這裡測的是 `retrieve()` 裡本來就有的
`rerank_multi_query` 逐 query 取最高分機制（`rag_query.py:865-873`），只是這次
餵的 query 組合是「原句 + 英文翻譯」而非當年判死的「原句 + 中文 rewrite 變體」
——CHANGELOG 07-08 已有一次同機制换前提後復活的先例（rerank_multi_query 對
sem-02 從死路復活成條件性有效），這次是同一機制的另一次前提替換，非重踩死路。

零 LLM 成本（只呼叫一次 translate_query_to_english，其餘都是本地 CrossEncoder
推理）。

Usage:
  python eval/dual_query_rerank_probe.py
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq

RETRIEVAL_MODEL = "llama-3.3-70b-versatile"

CASES = [
    {
        "id": "sem-09",
        "query": "Google 在 AI 與搜尋領域的競爭策略？",
        # critical checkpoint 的直接命中來源；#139 是本次對話 07-11 已證實會被
        # translate_query_en=True 擠出 top-5 的關鍵 chunk
        "targets": [("GOOGL_10K_2025.html", 139), ("GOOGL_10K_2025.html", 179)],
    },
    {
        "id": "sem-11",
        "query": "Tesla 面臨哪些主要競爭與監管風險？",
        # CHANGELOG 07-11/07-12 認定的 critical checkpoint 來源（price reductions）
        "targets": [("TSLA_10K_2025.html", 67)],
    },
]


def main():
    print("[INFO] Loading BGE-M3 + reranker...")
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rerank_model = CrossEncoder(rq.RERANK_MODEL)
    client = rq.make_qdrant_client()

    results = {}
    for case in CASES:
        qid, query, targets = case["id"], case["query"], case["targets"]
        print(f"\n{'='*80}\n[{qid}] {query}\n{'='*80}")

        _, _, pool = rq.retrieve(
            query, bge_m3, rerank_model, client, top_k=rq.DEFAULT_TOP_K,
            model_name=RETRIEVAL_MODEL, enable_rewrite=True,
            translate_query_en=False, return_pool=True,
        )
        formal_en = rq.translate_query_to_english(query, RETRIEVAL_MODEL)
        print(f"formal_en: {formal_en!r}")

        docs = [(p.payload or {}).get("document", "") for p in pool]
        meta = [(p.payload or {}) for p in pool]

        def rerank(q_text):
            cross_input = [[q_text, d] for d in docs]
            scores = rerank_model.predict(cross_input)
            return scores.tolist() if hasattr(scores, "tolist") else list(scores)

        raw_scores = rerank(query)
        formal_scores = rerank(formal_en)
        max_scores = [max(a, b) for a, b in zip(raw_scores, formal_scores)]

        def top5(scores):
            ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:5]
            return [(meta[i].get("source"), meta[i].get("chunk_index"), round(scores[i], 4)) for i in ranked]

        raw_top5 = top5(raw_scores)
        formal_top5 = top5(formal_scores)
        max_top5 = top5(max_scores)

        def targets_in(top5_list):
            present = {(s, c) for s, c, _ in top5_list}
            return {f"{s}#{c}": (s, c) in present for s, c in targets}

        case_result = {
            "query": query, "formal_en": formal_en,
            "raw_top5": raw_top5, "formal_top5": formal_top5, "max_top5": max_top5,
            "targets_in_raw": targets_in(raw_top5),
            "targets_in_formal": targets_in(formal_top5),
            "targets_in_max": targets_in(max_top5),
        }
        results[qid] = case_result

        print(f"raw_top5   : {raw_top5}")
        print(f"formal_top5: {formal_top5}")
        print(f"max_top5   : {max_top5}")
        print(f"targets_in_raw   : {case_result['targets_in_raw']}")
        print(f"targets_in_formal: {case_result['targets_in_formal']}")
        print(f"targets_in_max   : {case_result['targets_in_max']}")

    out = Path("eval/dual_query_rerank_probe_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {out}")

    print(f"\n{'='*80}\nSUMMARY\n{'='*80}")
    for qid, r in results.items():
        all_targets_ok = all(r["targets_in_max"].values())
        print(f"{qid}: targets_in_max={r['targets_in_max']} -> {'PASS' if all_targets_ok else 'FAIL'}")


if __name__ == "__main__":
    main()
