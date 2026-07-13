"""
compare_generation_tables.py — 並排比較（聚焦「表格內容」題型）：
同一題目分別在「舊版 us_stock_rag」與「新版 us_stock_rag_unstructured」
collection 下，檢索到的內容與 Gemini 生成的最終答案。

動機：compare_generation.py（財報數字 lexical 題）顯示兩版 pipeline 在
Fundamentals/IncomeStatement 這類小型 .txt 檔案上幾乎沒有差異 —— 因為
這些檔案根本不含 HTML 表格，新版「表格獨立成 chunk」的設計完全沒有
被觸發機會（兩邊命中的 chunk 全是 chunk_type=text）。

本腳本改為針對 10-K/10-Q HTML 檔案「裡面的表格」設計查詢 —— 這些表格
包含分部（segment）毛利率、資產負債表細項等，是 Fundamentals 摘要檔案
沒有的多欄位結構化資訊。理論上這正是新版 pipeline 「表格保留為獨立
chunk」最該發揮優勢的場景：如果表格保留得當，檢索應能撈到完整的一張
表（含多個關聯數字），LLM 才能正確抽取並引用；如果表格被打散成連續
文字 chunk，數字之間的對應關係可能會混亂或遺失。

四題鎖定的真實表格內容（已先用 Qdrant scroll 確認新版 collection 中
chunk_type=table 的內容）：
  tbl-01: NVDA_10K_2026.html #19 — Revenue/Cost of revenue/Gross profit FY2026
  tbl-02: AAPL_10K_2026.html #8,#9 — Products vs Services 毛利率分部明細
  tbl-03: NVDA_10K_2026.html #21 — 資產負債表流動資產明細
  tbl-04: MSFT_10Q_202605.html #8 — 資產負債表現金與短期投資
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import rag_query as rq

OLD_COLLECTION = "us_stock_rag"
NEW_COLLECTION = "us_stock_rag_unstructured"

# Gemini 在這次測試中遇到 503 (high demand) 且速度慢；改用 Groq
# (OpenAI-compatible endpoint) — meta-llama/llama-4-scout-17b-16e-instruct
# 在候選模型中 TPM 最高 (30K)，對於本測試「整段 HTML 表格塞進 context」
# 這種較長輸入場景較不容易撞到 token-per-minute 限制。
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"


def call_groq(messages, model=GROQ_MODEL):
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url=GROQ_BASE_URL)
    response = client.chat.completions.create(model=model, messages=messages)
    return response.choices[0].message.content

QUERIES = [
    ("tbl-01", "What was NVIDIA's revenue, cost of revenue, and gross profit for fiscal year 2026 (ended January 25, 2026), according to its 10-K income statement?"),
    ("tbl-02", "Break down Apple's gross margin between Products and Services segments for fiscal year 2025, including both dollar amounts and percentages."),
    ("tbl-03", "According to NVIDIA's balance sheet as of January 25, 2026, what were its cash and cash equivalents, marketable securities, accounts receivable, and inventories?"),
    ("tbl-04", "According to Microsoft's quarterly balance sheet as of March 31, 2026, what were its cash and cash equivalents and short-term investments?"),
]


def call_llm_with_retry(messages, max_attempts=6):
    delay = 30
    for attempt in range(1, max_attempts + 1):
        try:
            return call_groq(messages)
        except Exception as e:
            if attempt == max_attempts:
                raise
            print(f"[WARN] call_llm failed (attempt {attempt}/{max_attempts}): {e!r}; "
                  f"retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 300)


def run_one(query, collection, bge_m3, rerank_model, client):
    rq.COLLECTION_NAME = collection
    chunks, fallback_note = rq.retrieve(query, bge_m3, rerank_model, client, top_k=5)
    if not chunks:
        return "[no chunks retrieved]", []
    user_prompt = rq.build_user_prompt(query, chunks, fallback_note)
    messages = [
        {"role": "system", "content": rq.SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    answer = call_llm_with_retry(messages)
    return answer, chunks


def print_chunks(chunks):
    for c in chunks:
        print(f"  - {c['source']} | chunk #{c['chunk_index']} | type={c['chunk_type']} "
              f"| rerank={c['rerank_score']:.3f}")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    print("[INFO] Loading BGE-M3 + reranker...")
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rerank_model = CrossEncoder(rq.RERANK_MODEL)
    client = QdrantClient(path=rq.QDRANT_PATH)

    results = []
    for qid, query in QUERIES:
        print(f"\n{'='*78}\n{qid}: {query}\n{'='*78}")

        print(f"\n--- OLD ({OLD_COLLECTION}) ---")
        old_answer, old_chunks = run_one(query, OLD_COLLECTION, bge_m3, rerank_model, client)
        print(old_answer)
        print("Sources:")
        print_chunks(old_chunks)

        print(f"\n--- NEW ({NEW_COLLECTION}) ---")
        new_answer, new_chunks = run_one(query, NEW_COLLECTION, bge_m3, rerank_model, client)
        print(new_answer)
        print("Sources:")
        print_chunks(new_chunks)

        results.append({
            "id": qid,
            "query": query,
            "old": {
                "answer": old_answer,
                "sources": [
                    {"source": c["source"], "chunk_index": c["chunk_index"],
                     "chunk_type": c["chunk_type"], "rerank_score": c["rerank_score"]}
                    for c in old_chunks
                ],
            },
            "new": {
                "answer": new_answer,
                "sources": [
                    {"source": c["source"], "chunk_index": c["chunk_index"],
                     "chunk_type": c["chunk_type"], "rerank_score": c["rerank_score"]}
                    for c in new_chunks
                ],
            },
        })

    out_path = Path("eval/generation_compare_tables.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {out_path}")


if __name__ == "__main__":
    main()
