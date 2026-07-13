"""
build_and_compare_table_summary.py — 實驗：「摘要去檢索、原文去生成」對表格的效果

動機：compare_generation_tables.py 的結果顯示，無論是原始 HTML 表格或轉成
Markdown 表格語法（generation_compare_tables.json），表格 chunk 的 embedding
訊號都太弱/太模糊：
  - tbl-02：相關表格從未進入 top-5（撈不到）
  - tbl-01：撈到「主題像但內容不對」的表格（百分比表 vs 金額表，撈錯）

本實驗改用「摘要去檢索、原文去生成」（multi-vector / parent-document 模式）：
  1. 用 Groq LLM 為每張表格生成一句話摘要（描述公司、期間、這張表是什麼、
     包含哪些欄位/數字類型——例如「NVIDIA FY2026 損益表：營收、營收成本、
     毛利的金額（單位：百萬美元）」）
  2. 把這句「自然語言摘要」拿去 embedding（檢索用、語意訊號強、貼近使用者問法）
  3. payload["document"] 仍存放完整的 Markdown 表格內容（生成用、供 LLM 精確抽取數字）

寫入新 collection us_stock_rag_table_summary，與 us_stock_rag /
us_stock_rag_unstructured 並存，方便三方比較。

Usage:
  python eval/build_and_compare_table_summary.py
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import data_update_unstructure as du
import rag_query as rq

SUMMARY_COLLECTION = "us_stock_rag_table_summary"
OLD_COLLECTION     = "us_stock_rag"
NEW_COLLECTION     = "us_stock_rag_unstructured"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"


def call_groq(messages, model=GROQ_MODEL):
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url=GROQ_BASE_URL)
    response = client.chat.completions.create(model=model, messages=messages, temperature=0)
    return response.choices[0].message.content


def call_llm_with_retry(messages, max_attempts=6):
    delay = 20
    for attempt in range(1, max_attempts + 1):
        try:
            return call_groq(messages)
        except Exception as e:
            if attempt == max_attempts:
                raise
            print(f"  [WARN] LLM call failed (attempt {attempt}/{max_attempts}): {e!r}; retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 180)


SUMMARY_SYSTEM_PROMPT = (
    "You are given a financial table extracted from a company's SEC filing "
    "(10-K or 10-Q), in Markdown format, along with the source filename. "
    "Write ONE short sentence (max 30 words) describing what this table contains: "
    "the company, the fiscal period if shown, what kind of financial data it is "
    "(e.g. income statement, balance sheet, segment breakdown, gross margin by "
    "segment, percentages vs dollar amounts), and the unit if shown. "
    "Output ONLY the sentence, no preamble, no quotes."
)


def summarize_table(markdown_table: str, source: str) -> str:
    user_content = f"Source file: {source}\n\nTable (Markdown):\n{markdown_table[:3000]}"
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    summary = call_llm_with_retry(messages)
    return summary.strip().strip('"')


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1: build us_stock_rag_table_summary collection
# ══════════════════════════════════════════════════════════════════════════════

def build_records_with_summary(elements, semantic_chunker, source_name):
    """回傳 [{"embed_text": ..., "document": ..., "chunk_type": ...}, ...]

    - Table : embed_text = LLM 生成的一句話摘要；document = Markdown 表格原文
    - 其餘  : embed_text == document == 語意切割後的文字（與原 pipeline 相同）
    """
    from unstructured.documents.elements import Table

    table_elements = [el for el in elements if isinstance(el, Table)]
    text_elements  = [el for el in elements if not isinstance(el, Table)]

    records: list[dict] = []

    for idx, el in enumerate(table_elements):
        markdown = du.table_element_to_text(el).strip()
        if not markdown:
            continue
        try:
            summary = summarize_table(markdown, source_name)
        except Exception as e:
            print(f"    [WARN] summarize failed for table #{idx}: {e!r}; falling back to markdown content")
            summary = markdown[:200]
        print(f"    [table summary] {summary}")
        records.append({"embed_text": summary, "document": markdown, "chunk_type": "table"})

    text_blob = "\n\n".join(
        el.text.strip() for el in text_elements if el.text and el.text.strip()
    )
    if text_blob.strip():
        docs = semantic_chunker.create_documents([text_blob])
        for doc in docs:
            content = doc.page_content.strip()
            if len(content) > 20:
                records.append({"embed_text": content, "document": content, "chunk_type": "text"})

    return records


def build_summary_collection(bge_m3, client):
    from qdrant_client import models

    du.COLLECTION_NAME = SUMMARY_COLLECTION
    du.ensure_collection(client, recreate=True)

    embed_adapter = du.BGEM3DenseEmbeddings(bge_m3)
    from langchain_experimental.text_splitter import SemanticChunker
    semantic_chunker = SemanticChunker(
        embed_adapter, breakpoint_threshold_type="percentile", breakpoint_threshold_amount=90,
    )

    raw_files = sorted(
        f for f in du.RAW_DIR.iterdir() if f.suffix.lower() in du.SUPPORTED_EXTS
    )
    print(f"[INFO] Found {len(raw_files)} file(s)\n")

    total_table = total_text = 0

    for filepath in raw_files:
        print(f"[PROC] {filepath.name}")
        elements = du.partition_and_clean(filepath)
        if not elements:
            print("  [SKIP] could not partition")
            continue

        records = build_records_with_summary(elements, semantic_chunker, filepath.name)
        n_table = sum(1 for r in records if r["chunk_type"] == "table")
        n_text  = sum(1 for r in records if r["chunk_type"] == "text")
        total_table += n_table
        total_text  += n_text
        print(f"  -> {len(records)} chunks ({n_table} table + {n_text} text)")
        if not records:
            continue

        embed_texts = [r["embed_text"] for r in records]
        encoded = bge_m3.encode(
            embed_texts, batch_size=8,
            return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        dense_vecs  = encoded["dense_vecs"]
        lex_weights = encoded["lexical_weights"]

        points = []
        for i, (record, dense_vec, weights) in enumerate(zip(records, dense_vecs, lex_weights)):
            chunk_id_str = f"{filepath.stem}_c{i}"
            indices = [int(tok) for tok in weights.keys()]
            values  = [float(w) for w in weights.values()]
            points.append(models.PointStruct(
                id=du.deterministic_uuid(chunk_id_str),
                vector={
                    du.DENSE_VECTOR_NAME:  dense_vec.tolist(),
                    du.SPARSE_VECTOR_NAME: models.SparseVector(indices=indices, values=values),
                },
                payload={
                    "chunk_id":    chunk_id_str,
                    "source":      filepath.name,
                    "stem":        filepath.stem,
                    "chunk_index": i,
                    "chunk_type":  record["chunk_type"],
                    "document":    record["document"],
                    "embed_text":  record["embed_text"],
                },
            ))
        client.upsert(collection_name=SUMMARY_COLLECTION, points=points)

    print(f"\n[DONE] Indexed {total_table} table chunks (LLM-summarized) "
          f"+ {total_text} text chunks into '{SUMMARY_COLLECTION}'")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2: re-run the 4 table-focused queries against OLD vs SUMMARY collection
# ══════════════════════════════════════════════════════════════════════════════

QUERIES = [
    ("tbl-01", "What was NVIDIA's revenue, cost of revenue, and gross profit for fiscal year 2026 (ended January 25, 2026), according to its 10-K income statement?"),
    ("tbl-02", "Break down Apple's gross margin between Products and Services segments for fiscal year 2025, including both dollar amounts and percentages."),
    ("tbl-03", "According to NVIDIA's balance sheet as of January 25, 2026, what were its cash and cash equivalents, marketable securities, accounts receivable, and inventories?"),
    ("tbl-04", "According to Microsoft's quarterly balance sheet as of March 31, 2026, what were its cash and cash equivalents and short-term investments?"),
]


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

    print(f"\n{'='*78}\nPhase 1: building '{SUMMARY_COLLECTION}' (LLM table-summary embeddings)\n{'='*78}")
    build_summary_collection(bge_m3, client)

    print(f"\n{'='*78}\nPhase 2: re-running 4 table-focused queries — OLD vs SUMMARY\n{'='*78}")
    results = []
    for qid, query in QUERIES:
        print(f"\n{'='*78}\n{qid}: {query}\n{'='*78}")

        print(f"\n--- OLD ({OLD_COLLECTION}) ---")
        old_answer, old_chunks = run_one(query, OLD_COLLECTION, bge_m3, rerank_model, client)
        print(old_answer)
        print("Sources:")
        print_chunks(old_chunks)

        print(f"\n--- SUMMARY ({SUMMARY_COLLECTION}) ---")
        sum_answer, sum_chunks = run_one(query, SUMMARY_COLLECTION, bge_m3, rerank_model, client)
        print(sum_answer)
        print("Sources:")
        print_chunks(sum_chunks)

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
            "summary": {
                "answer": sum_answer,
                "sources": [
                    {"source": c["source"], "chunk_index": c["chunk_index"],
                     "chunk_type": c["chunk_type"], "rerank_score": c["rerank_score"]}
                    for c in sum_chunks
                ],
            },
        })

    out_path = Path("eval/generation_compare_table_summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {out_path}")


if __name__ == "__main__":
    main()
