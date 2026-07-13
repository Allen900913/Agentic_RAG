"""
build_and_compare_table_summary_meta.py — 實驗：在表格摘要 prompt 中注入結構化
filing metadata（filing_type / fiscal_year / fiscal_period），看能否改善摘要的
期間消歧能力，進而修正 tbl-02 這類「抓到型態對、期間錯的表格」問題。

動機：build_and_compare_table_summary.py（純 LLM 摘要去檢索）的結果顯示，
LLM 自己從表格內容推斷出的摘要雖然「有時」會提到期間（例如 "from its 10-K
filing"、"for fiscal years 2023-2025"），但這個訊號不夠穩定、不夠精確
（10-Q 表格只看內容常常看不出是哪一季），導致 embedding 仍然選到主題相同
但期間錯誤的表格。

本實驗的做法：
  1. 用 BeautifulSoup 解析每份 filing 開頭的 XBRL dei: 標記
     （dei:DocumentType / dei:DocumentFiscalYearFocus /
       dei:DocumentFiscalPeriodFocus），抽出 filing_type / fiscal_year /
       fiscal_period（檔名解析作為備援）
  2. 把這組 metadata 連同表格內容一起放進摘要 prompt，明確要求 LLM 在摘要
     開頭準確點出公司、filing 類型與精確的會計期間（而不是讓它自己用猜的）
  3. 其餘流程（embedding、generation、評分）與
     build_and_compare_table_summary.py 完全相同，方便三方比較

寫入新 collection us_stock_rag_table_summary_meta，與 us_stock_rag /
us_stock_rag_table_summary 並存。

Usage:
  python eval/build_and_compare_table_summary_meta.py
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import data_update_unstructure as du
import rag_query as rq

META_COLLECTION    = "us_stock_rag_table_summary_meta"
OLD_COLLECTION     = "us_stock_rag"
SUMMARY_COLLECTION = "us_stock_rag_table_summary"

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


# ══════════════════════════════════════════════════════════════════════════════
# Filing metadata extraction：優先讀 XBRL dei: 標記，檔名解析作為備援
# ══════════════════════════════════════════════════════════════════════════════

DEI_FIELD_MAP = {
    "dei:documenttype":               "filing_type",
    "dei:documentfiscalyearfocus":    "fiscal_year",
    "dei:documentfiscalperiodfocus":  "fiscal_period",
}
_IX_NONNUMERIC_RE = re.compile(r"ix:nonnumeric", re.I)
_FILENAME_RE = re.compile(r"^(?P<ticker>[A-Z]+)_(?P<filing>10K|10Q)_(?P<yyyymm>\d{4,6})")


def extract_filing_metadata(path: Path) -> dict:
    """回傳 {"filing_type": "10-K", "fiscal_year": "2025", "fiscal_period": "FY"}。
    缺漏的欄位用檔名規則補齊（例如 NVDA_10K_2026 -> 10-K / 2026）。"""
    from bs4 import BeautifulSoup

    meta: dict = {}
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(_IX_NONNUMERIC_RE):
            key = DEI_FIELD_MAP.get((tag.get("name") or "").lower())
            if key and key not in meta:
                value = tag.get_text(strip=True)
                if value:
                    meta[key] = value
    except Exception as e:
        print(f"    [WARN] dei: extraction failed for {path.name}: {e!r}")

    m = _FILENAME_RE.match(path.stem)
    if m:
        meta.setdefault("filing_type", "10-K" if m.group("filing") == "10K" else "10-Q")
        meta.setdefault("fiscal_year", m.group("yyyymm")[:4])

    return meta


def describe_period(meta: dict) -> str:
    """把 metadata 轉成人類易讀的期間描述，例如 'FY2025 Annual (10-K)' 或
    'FY2026 Q2 (10-Q)'，注入 prompt 給 LLM 當作明確依據（不用它自己猜）。"""
    filing_type  = meta.get("filing_type", "unknown filing type")
    fiscal_year  = meta.get("fiscal_year", "unknown fiscal year")
    fiscal_period = meta.get("fiscal_period", "")
    period_label = "Annual" if fiscal_period == "FY" else (fiscal_period or "unknown period")
    return f"FY{fiscal_year} {period_label} ({filing_type})"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1: build us_stock_rag_table_summary_meta collection
# ══════════════════════════════════════════════════════════════════════════════

SUMMARY_SYSTEM_PROMPT_META = (
    "You are given a financial table extracted from a company's SEC filing, "
    "the source filename, and the filing's OFFICIAL metadata (filing type, "
    "fiscal year, fiscal period) taken directly from its XBRL cover-page tags — "
    "treat this metadata as ground truth and do NOT guess a different period from "
    "the table content. "
    "Write ONE short sentence (max 40 words) describing what this table contains: "
    "the company, the EXACT filing type and fiscal period from the metadata "
    "provided (e.g. '10-K for fiscal year 2025 (annual)' or '10-Q for fiscal year "
    "2026 Q2'), what kind of financial data it is (e.g. income statement, balance "
    "sheet, segment breakdown, gross margin by segment, percentages vs dollar "
    "amounts), and the unit if shown. "
    "Output ONLY the sentence, no preamble, no quotes."
)


def summarize_table_with_meta(markdown_table: str, source: str, meta: dict) -> str:
    period_desc = describe_period(meta)
    user_content = (
        f"Source file: {source}\n"
        f"Official filing metadata: filing_type={meta.get('filing_type', 'N/A')}, "
        f"fiscal_year={meta.get('fiscal_year', 'N/A')}, "
        f"fiscal_period={meta.get('fiscal_period', 'N/A')} "
        f"(i.e. this filing covers {period_desc})\n\n"
        f"Table (Markdown):\n{markdown_table[:3000]}"
    )
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT_META},
        {"role": "user", "content": user_content},
    ]
    summary = call_llm_with_retry(messages)
    return summary.strip().strip('"')


def build_records_with_meta_summary(elements, semantic_chunker, source_name, meta):
    from unstructured.documents.elements import Table

    table_elements = [el for el in elements if isinstance(el, Table)]
    text_elements  = [el for el in elements if not isinstance(el, Table)]

    records: list[dict] = []

    for idx, el in enumerate(table_elements):
        markdown = du.table_element_to_text(el).strip()
        if not markdown:
            continue
        try:
            summary = summarize_table_with_meta(markdown, source_name, meta)
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


def build_meta_collection(bge_m3, client):
    from qdrant_client import models

    du.COLLECTION_NAME = META_COLLECTION
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

        if filepath.suffix.lower() in {".html", ".htm"}:
            meta = extract_filing_metadata(filepath)
            print(f"  [meta] {meta}  -> {describe_period(meta)}")
        else:
            meta = {}

        elements = du.partition_and_clean(filepath)
        if not elements:
            print("  [SKIP] could not partition")
            continue

        records = build_records_with_meta_summary(elements, semantic_chunker, filepath.name, meta)
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
        client.upsert(collection_name=META_COLLECTION, points=points)

    print(f"\n[DONE] Indexed {total_table} table chunks (metadata-aware LLM summaries) "
          f"+ {total_text} text chunks into '{META_COLLECTION}'")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2: re-run the 4 table-focused queries — OLD vs SUMMARY vs META
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


def chunk_summary(chunks):
    return [
        {"source": c["source"], "chunk_index": c["chunk_index"],
         "chunk_type": c["chunk_type"], "rerank_score": c["rerank_score"]}
        for c in chunks
    ]


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

    print(f"\n{'='*78}\nPhase 1: building '{META_COLLECTION}' (metadata-aware LLM table-summary embeddings)\n{'='*78}")
    build_meta_collection(bge_m3, client)

    print(f"\n{'='*78}\nPhase 2: re-running 4 table-focused queries — OLD vs SUMMARY vs META\n{'='*78}")
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

        print(f"\n--- META ({META_COLLECTION}) ---")
        meta_answer, meta_chunks = run_one(query, META_COLLECTION, bge_m3, rerank_model, client)
        print(meta_answer)
        print("Sources:")
        print_chunks(meta_chunks)

        results.append({
            "id": qid,
            "query": query,
            "old":     {"answer": old_answer,  "sources": chunk_summary(old_chunks)},
            "summary": {"answer": sum_answer,  "sources": chunk_summary(sum_chunks)},
            "meta":    {"answer": meta_answer, "sources": chunk_summary(meta_chunks)},
        })

    out_path = Path("eval/generation_compare_table_summary_meta.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {out_path}")


if __name__ == "__main__":
    main()
