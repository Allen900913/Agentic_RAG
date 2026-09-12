# -*- coding: utf-8 -*-
"""端到端延遲實測：模型只載入一次（模擬 api_server 常駐暖機），對同一批代表性
query 分別跑「舊設定（rewrite=False, translate_query_en=False）」與「新生產預設
（rewrite=True, translate_query_en=True）」，拆解 retrieve() 與 generate() 各自
耗時，量化這次「雙 query 取最高分」到底讓查詢慢多少。

Usage:
  python eval/latency_benchmark.py
"""
import statistics
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):  # ⚠ stderr 也要轉：traceback 走 stderr，只轉 stdout 的話「印到一半 crash」照樣發生（2026-09-11 閘門 H1）

    _s.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq

QUERIES = [
    "Google 在 AI 與搜尋領域的競爭策略？",
    "Tesla 面臨哪些主要競爭與監管風險？",
    "NVIDIA 核心技術上的優勢是什麼？",
    "Apple 的服務業務未來成長性如何？",
]

RETRIEVAL_MODEL = rq.DEFAULT_MODEL
GEN_MODEL = rq.DEFAULT_GEN_MODEL


def time_call(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - t0


def main():
    print("[INFO] Loading BGE-M3 + reranker (one-time warm-up, not counted in per-query timing)...")
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rerank_model = CrossEncoder(rq.RERANK_MODEL)
    client = rq.make_qdrant_client()

    configs = [
        ("OLD（rewrite=False, translate_en=False）", dict(enable_rewrite=False, translate_query_en=False)),
        ("NEW（rewrite=True,  translate_en=True） ", dict(enable_rewrite=True, translate_query_en=True)),
    ]

    results = {label: {"retrieve": [], "generate": [], "total": []} for label, _ in configs}

    for label, kwargs in configs:
        print(f"\n{'='*80}\n[{label}]\n{'='*80}")
        for q in QUERIES:
            t_total0 = time.perf_counter()
            (chunks, fallback_note), t_retrieve = time_call(
                rq.retrieve, q, bge_m3, rerank_model, client, top_k=rq.DEFAULT_TOP_K,
                model_name=RETRIEVAL_MODEL, **kwargs,
            )
            user_prompt = rq.build_user_prompt(q, chunks, fallback_note)
            messages = [
                {"role": "system", "content": rq.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            _answer, t_generate = time_call(
                rq.call_llm, messages, GEN_MODEL, temperature=rq.GEN_TEMPERATURE,
            )
            t_total = time.perf_counter() - t_total0

            results[label]["retrieve"].append(t_retrieve)
            results[label]["generate"].append(t_generate)
            results[label]["total"].append(t_total)
            print(f"  {q[:30]:30s}  retrieve={t_retrieve:6.2f}s  generate={t_generate:6.2f}s  total={t_total:6.2f}s")

    print(f"\n{'='*80}\nSUMMARY (mean over {len(QUERIES)} queries)\n{'='*80}")
    for label, _ in configs:
        r = results[label]
        print(f"\n[{label}]")
        print(f"  retrieve : mean={statistics.mean(r['retrieve']):.2f}s  "
              f"min={min(r['retrieve']):.2f}s  max={max(r['retrieve']):.2f}s")
        print(f"  generate : mean={statistics.mean(r['generate']):.2f}s")
        print(f"  total    : mean={statistics.mean(r['total']):.2f}s")

    old_retrieve = statistics.mean(results[configs[0][0]]["retrieve"])
    new_retrieve = statistics.mean(results[configs[1][0]]["retrieve"])
    old_total = statistics.mean(results[configs[0][0]]["total"])
    new_total = statistics.mean(results[configs[1][0]]["total"])
    print(f"\n[DELTA] retrieve: {new_retrieve - old_retrieve:+.2f}s "
          f"({(new_retrieve/old_retrieve - 1)*100:+.1f}%)")
    print(f"[DELTA] total   : {new_total - old_total:+.2f}s "
          f"({(new_total/old_total - 1)*100:+.1f}%)")


if __name__ == "__main__":
    main()
