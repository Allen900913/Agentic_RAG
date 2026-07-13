"""
data_update_unstructure.py — US Stock RAG Data Pipeline (unstructured + Qdrant Hybrid)

與 data_update.py 的差異：改用 unstructured 解析/清理原始檔案，並把表格與純文字分開處理：
  - partition_html / partition_text → 取得帶語意角色的 elements
                                       (Title / NarrativeText / Text / Table / ListItem ...)
  - 清理：對每個 element 套用 clean_extra_whitespace
  - 清理後檔案輸出到 data/unstructure_processed/（角色標記格式，方便肉眼檢查與舊版對照）
  - Chunking 策略（依元素類型分流）：
      • Table          → 每個表格獨立成一個 chunk（text_as_html 轉成 Markdown 表格語法）
      • 其餘文字 elements → 串接後交給 SemanticChunker 做語意切割
  - 寫入 Qdrant 時於 payload 多帶一個 chunk_type 欄位（"table" / "text"）以便之後篩選

為避免覆蓋既有的 us_stock_rag collection（其資料是用舊版 pipeline 切的），
本腳本寫入另一個獨立 collection us_stock_rag_unstructured，方便兩者並存比較。

Usage:
  python data_update_unstructure.py              # 增量更新
  python data_update_unstructure.py --rebuild    # 全量重建
"""

import argparse
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
RAW_DIR          = Path("data/raw")
PROCESSED_DIR    = Path("data/unstructure_processed")
HASHES_FILE      = Path("hashes_unstructure.json")
QDRANT_PATH      = os.getenv("QDRANT_PATH", "./qdrant_db")
QDRANT_URL       = os.getenv("QDRANT_URL", "")   # 若設定則走 server mode（Docker）
EMBEDDING_MODEL  = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
COLLECTION_NAME  = "us_stock_rag_unstructured"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"
DENSE_DIM        = 1024  # BGE-M3 dense dimension
SUPPORTED_EXTS   = {".txt", ".md", ".html", ".htm"}


# ══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ══════════════════════════════════════════════════════════════════════════════

def compute_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def load_hashes() -> dict:
    if HASHES_FILE.exists():
        with open(HASHES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_hashes(hashes: dict) -> None:
    with open(HASHES_FILE, "w", encoding="utf-8") as f:
        json.dump(hashes, f, indent=2)


def deterministic_uuid(name: str) -> str:
    """Qdrant point IDs must be int or UUID. Deterministic UUID lets us replay
    --rebuild and get the same IDs for the same chunk_id string."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


# ══════════════════════════════════════════════════════════════════════════════
# Filing metadata extraction (filing_type / fiscal_year / fiscal_period / report_label_year)
#
# 與 data_update.py 的 extract_filing_metadata() 邏輯相同：優先讀 XBRL dei:
# cover-page tags 取得文件「自己宣稱」的會計期間，缺漏的欄位用檔名規則補齊。
#
# report_label_year 額外永遠從檔名抽取（不受 dei: 是否存在影響）。動機：檔名
# 年份（例如 AAPL_10K_2026.html）代表「使用者口語/資料集收錄時間點」語意，
# 跟 XBRL 的 fiscal_year（AAPL 該檔案實際是 FY2025）是兩個不同座標系——
# AAPL/META 的會計年度結束月跟日曆年度不同調，兩者常常差 1。query-filter
# 從使用者問題抽出來的年份通常對應檔名語意，因此額外保留這個欄位讓
# rag_query.py 的 hard filter 可以同時比對兩種年份語意。
# ══════════════════════════════════════════════════════════════════════════════

DEI_FIELD_MAP = {
    "dei:documenttype":              "filing_type",
    "dei:documentfiscalyearfocus":   "fiscal_year",
    "dei:documentfiscalperiodfocus": "fiscal_period",
}
_IX_NONNUMERIC_RE = re.compile(r"ix:nonnumeric", re.I)
_FILENAME_RE = re.compile(r"^(?P<ticker>[A-Z]+)_(?P<filing>10K|10Q)_(?P<yyyymm>\d{4,6})")


def extract_filing_metadata(path: Path) -> dict:
    """回傳 {"filing_type": "10-K", "fiscal_year": "2025", "fiscal_period": "FY",
    "report_label_year": "2026"}。優先讀 XBRL dei: cover-page tags，缺漏的欄位
    用檔名規則補齊（例如 NVDA_10K_2026.html -> filing_type=10-K, fiscal_year=2026）。
    report_label_year 永遠取自檔名（與 fiscal_year 是兩個獨立座標系，見上方說明）。"""
    meta: dict = {}
    try:
        from bs4 import BeautifulSoup
        html = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(_IX_NONNUMERIC_RE):
            key = DEI_FIELD_MAP.get((tag.get("name") or "").lower())
            if key and key not in meta:
                value = tag.get_text(strip=True)
                if value:
                    meta[key] = value
    except Exception as e:
        print(f"  [WARN] dei: extraction failed for {path.name}: {e!r}")

    m = _FILENAME_RE.match(path.stem)
    if m:
        meta.setdefault("filing_type", "10-K" if m.group("filing") == "10K" else "10-Q")
        meta.setdefault("fiscal_year", m.group("yyyymm")[:4])
        meta["report_label_year"]  = m.group("yyyymm")[:4]
        meta["report_period_code"] = m.group("yyyymm")        # 完整期間碼（e.g. "202605"）
        meta["ticker"]             = m.group("ticker")         # e.g. "AAPL", "NVDA"

    return meta


def build_metadata_header(filing_meta: dict) -> str:
    """把 ticker / 期別前綴進被 embed 的文字。

    動機：fiscal_year / report_period_code 等期別資訊只存在 payload，從不出現在
    chunk 原文裡（例如 10-Q 正文不會寫「這是 202512 那一期」）——導致同一 ticker
    不同期的 chunk 文字幾乎相同，dense/sparse/BM25 任何文字檢索都無法區分期別，
    只能仰賴 query-understanding 的 hard filter。把這些 metadata 前綴進文字後，
    檢索本身也具備期別感知，hard filter 失準或未觸發時仍有救。"""
    parts = []
    if filing_meta.get("ticker"):
        parts.append(filing_meta["ticker"])
    if filing_meta.get("filing_type"):
        parts.append(filing_meta["filing_type"])
    period_code = filing_meta.get("report_period_code")
    if period_code:
        parts.append(f"period {period_code}")
    elif filing_meta.get("fiscal_year"):
        period_str = f"FY{filing_meta['fiscal_year']}"
        if filing_meta.get("fiscal_period"):
            period_str += f" {filing_meta['fiscal_period']}"
        parts.append(period_str)
    if not parts:
        return ""
    return "[" + " ".join(parts) + "] "


def html_table_to_markdown(html: str) -> Optional[str]:
    """把 unstructured 提供的 text_as_html 轉成 Markdown 表格語法。

    動機：原始 <table><tr><td> 標記對 embedding 模型來說雜訊很重（一堆標籤、
    樣式屬性，數字訊號被稀釋），Markdown 表格語法（| a | b |）保留相同的
    列/欄結構，但更精簡、更接近模型訓練語料中常見的純文字形式，embedding
    訊號應該更乾淨。轉換失敗時回傳 None，由呼叫端退回原始內容。
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            return None

        rows = []
        for tr in table.find_all("tr"):
            cells = [
                " ".join(cell.get_text(separator=" ", strip=True).split())
                for cell in tr.find_all(["td", "th"])
            ]
            if any(cells):
                rows.append(cells)

        if not rows:
            return None

        n_cols = max(len(r) for r in rows)
        rows = [r + [""] * (n_cols - len(r)) for r in rows]

        def fmt_row(cells):
            return "| " + " | ".join(c.replace("|", "/") for c in cells) + " |"

        lines = [fmt_row(rows[0]), fmt_row(["---"] * n_cols)]
        lines += [fmt_row(r) for r in rows[1:]]
        return "\n".join(lines)
    except Exception:
        return None


def table_element_to_text(el) -> str:
    """取得表格元素的最終文字內容：優先把 text_as_html 轉成 Markdown，
    轉換失敗則退回原始 text_as_html，再不行就退回 unstructured 的 el.text。"""
    html = getattr(el.metadata, "text_as_html", None)
    if html:
        md = html_table_to_markdown(html)
        if md:
            return md
        return html
    return el.text


# ══════════════════════════════════════════════════════════════════════════════
# unstructured: partition + clean
# ══════════════════════════════════════════════════════════════════════════════

def partition_and_clean(path: Path) -> Optional[list]:
    """用 unstructured 把檔案解析成帶角色的 elements，並逐一清理空白字元。"""
    from unstructured.partition.html import partition_html
    from unstructured.partition.text import partition_text
    from unstructured.cleaners.core import clean_extra_whitespace

    ext = path.suffix.lower()
    try:
        if ext in {".html", ".htm"}:
            elements = partition_html(filename=str(path))
        elif ext in {".txt", ".md"}:
            elements = partition_text(filename=str(path))
        else:
            return None
    except Exception as e:
        print(f"  [WARN] unstructured partition failed: {e}")
        return None

    for el in elements:
        el.apply(clean_extra_whitespace)

    return elements


def render_processed(elements: list) -> str:
    """把 elements 轉成角色標記格式的純文字，寫進 unstructure_processed/，
    方便人眼檢查與舊版 processed/*.txt（純文字流，無角色資訊）對照。"""
    from unstructured.documents.elements import Table

    blocks = []
    for el in elements:
        role = type(el).__name__
        if isinstance(el, Table):
            body = table_element_to_text(el).strip()
            blocks.append(f"[Table]\n{body}")
        else:
            text = el.text.strip()
            if text:
                blocks.append(f"[{role}] {text}")
    return "\n\n".join(blocks)


# ══════════════════════════════════════════════════════════════════════════════
# Chunking：Table 獨立成 chunk、其餘文字走語意切割
# ══════════════════════════════════════════════════════════════════════════════

# 把表格前最近的標題/敘述當 caption prepend 進 table chunk 時，最多取多少字元。
# 限制長度是為了補回脈絡的同時，不讓一大段敘述稀釋掉表格本身的數字訊號。
TABLE_CAPTION_MAXLEN = 240

# LLM 摘要的觸發門檻：表格至少要有這麼多列才值得摘要。
# 列數 < 閾值的通常是封面 checkbox / IRS ID 之類的行政雜訊，不值得花 LLM call。
TABLE_SUMMARY_MIN_ROWS = 4


def _is_meaningful_caption(text: str) -> bool:
    """判斷一段前文能不能當表格 caption：要有實際文字內容，排除 unstructured 在
    封面表格周圍吐出的純標點/空白雜訊（例如 ', ,' 或孤立的 'OR'）。"""
    stripped = text.strip()
    return len(stripped) >= 3 and any(ch.isalnum() for ch in stripped)


# 往表格前方最多回溯幾個 element 找 caption。
# 設為 2 是為了同時抓到「Title + NarrativeText → Table」這種兩層結構，
# 又不會跨越章節邊界拿到不相關的遠端文字。
TABLE_CAPTION_LOOKBACK = 2


def _count_table_rows(markdown: str) -> int:
    """從 Markdown 表格字串數出資料列數（排除 header 和分隔線）。"""
    lines = [l for l in markdown.splitlines()
             if l.strip().startswith("|") and not set(l.replace("|", "").replace("-", "").replace(" ", "")) == set()]
    # 第一列是 header，第二列是 ---, 所以資料列 = max(0, 總列數 - 2)
    return max(0, len(lines) - 2)


_TABLE_SUMMARY_SYSTEM = (
    "You are a financial document analyst. "
    "Given a Markdown table from a SEC filing, write ONE concise sentence "
    "(max 30 words) describing what the table contains: the type of data, "
    "company/segment if apparent, and time period if visible. "
    "Do NOT repeat specific numbers. Output only the sentence, no preamble."
)


def _llm_summarize_table(markdown: str) -> str:
    """用 Gemini 為找不到 caption 的大型表格生成一行摘要。
    摘要只描述「這是什麼表、涵蓋哪些指標」，不重複表格數字（數字已在 body 裡）。
    LLM call 失敗時靜默回傳空字串，表格仍以純 Markdown 寫入。
    走 GEMINI_API_KEY（與 rag_query.py 的 Gemini 路徑一致），而非 LiteLLM（額度/認證
    不穩定，曾在批量 ingest 時整批 401 失敗）。"""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        config = types.GenerateContentConfig(
            system_instruction=_TABLE_SUMMARY_SYSTEM,
            max_output_tokens=60,
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=markdown[:1500],
            config=config,
        )
        return (resp.text or "").strip()
    except Exception as e:
        print(f"  [WARN] LLM table summary failed: {e!r}")
        return ""


def _find_caption(elements: list, table_idx: int) -> str:
    """往前回溯最多 TABLE_CAPTION_LOOKBACK 個 element 找 caption。

    停止條件（任一成立即停止，不繼續往前）：
    - 遇到另一張 Table（代表跨表格了，不同區塊）
    - 已回溯超過 TABLE_CAPTION_LOOKBACK 個 element

    回傳第一個（最近的）通過 _is_meaningful_caption() 的文字；
    若找不到則回傳空字串（= 不加 caption）。
    """
    from unstructured.documents.elements import Table

    lookback = 0
    for j in range(table_idx - 1, -1, -1):
        if lookback >= TABLE_CAPTION_LOOKBACK:
            break
        prev = elements[j]
        if isinstance(prev, Table):
            break  # 跨過另一張表，停止
        txt = (prev.text or "").strip()
        lookback += 1
        if _is_meaningful_caption(txt):
            return txt[:TABLE_CAPTION_MAXLEN]
    return ""


def build_chunk_records(elements: list, semantic_chunker) -> list[dict]:
    """回傳 [{"text": ..., "chunk_type": "table" | "text"}, ...]

    Table 的 caption 取得策略（依序嘗試）：
      1. 向前回溯最多 TABLE_CAPTION_LOOKBACK 個 element，取最近的有意義前文。
      2. 若找不到（回傳 ""）且表格列數 >= TABLE_SUMMARY_MIN_ROWS，
         呼叫 LLM 生成一行摘要（只描述「這是什麼表」，不重複數字）。
      3. 兩者都失敗（太小的表格或 LLM call 失敗），直接用純 Markdown，不加 caption。

    閾值設計理由：
    - 列數 < TABLE_SUMMARY_MIN_ROWS 的表格通常是封面 checkbox / IRS 編號之類的行政
      雜訊，對任何 RAG query 都沒有貢獻，不值得花 LLM call。
    - 列數 >= 閾值 且找不到 caption 才是「真正的財務表格但結構上沒有前文」的情況，
      這裡 LLM 才能發揮作用。
    """
    from unstructured.documents.elements import Table

    records: list[dict] = []
    text_elements: list = []

    for i, el in enumerate(elements):
        if isinstance(el, Table):
            body = table_element_to_text(el).strip()
            if not body:
                continue
            caption = _find_caption(elements, i)
            if not caption and _count_table_rows(body) >= TABLE_SUMMARY_MIN_ROWS:
                caption = _llm_summarize_table(body)
            text = f"{caption}\n\n{body}" if caption else body
            records.append({"text": text, "chunk_type": "table"})
        else:
            txt = (el.text or "").strip()
            if txt:
                text_elements.append(el)

    text_blob = "\n\n".join(
        el.text.strip() for el in text_elements if el.text and el.text.strip()
    )
    if text_blob.strip():
        docs = semantic_chunker.create_documents([text_blob])
        for doc in docs:
            content = doc.page_content.strip()
            if len(content) > 20:
                records.append({"text": content, "chunk_type": "text"})

    return records


# ══════════════════════════════════════════════════════════════════════════════
# BGE-M3 adapter for SemanticChunker (LangChain Embeddings interface)
# ══════════════════════════════════════════════════════════════════════════════

class BGEM3DenseEmbeddings:
    def __init__(self, model):
        self.model = model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out = self.model.encode(
            texts, batch_size=8,
            return_dense=True, return_sparse=False, return_colbert_vecs=False,
        )
        return [vec.tolist() for vec in out["dense_vecs"]]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


# ══════════════════════════════════════════════════════════════════════════════
# Qdrant collection setup
# ══════════════════════════════════════════════════════════════════════════════

def ensure_collection(client, recreate: bool = False) -> None:
    """Create the hybrid collection if missing, or drop+recreate on --rebuild."""
    from qdrant_client import models

    exists = client.collection_exists(COLLECTION_NAME)

    if exists and recreate:
        client.delete_collection(COLLECTION_NAME)
        print(f"[INFO] Deleted collection '{COLLECTION_NAME}'")
        exists = False

    if not exists:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=DENSE_DIM,
                    distance=models.Distance.COSINE,
                ),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(
                    index=models.SparseIndexParams(on_disk=False),
                ),
            },
        )
        # Payload index so we can efficiently filter by source for replace-on-update
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="source",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
        print(f"[INFO] Created collection '{COLLECTION_NAME}' "
              f"(dense={DENSE_DIM}D cosine, sparse=BGE-M3 lexical)")


def delete_points_by_source(client, source: str) -> None:
    """Idempotency: remove all points belonging to a given source file."""
    from qdrant_client import models
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(
                    key="source",
                    match=models.MatchValue(value=source),
                )]
            )
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="US Stock RAG — Data Update Pipeline (unstructured + Qdrant Hybrid)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python data_update_unstructure.py              # 增量更新（只處理有變動的檔案）
  python data_update_unstructure.py --rebuild    # 全量重建
  python data_update_unstructure.py --raw-dir ./my_data
        """,
    )
    parser.add_argument("--rebuild", action="store_true",
                        help="Clear data/unstructure_processed/ and recreate the Qdrant collection from scratch")
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--processed-dir", default=str(PROCESSED_DIR))

    args = parser.parse_args()

    raw_dir       = Path(args.raw_dir)
    processed_dir = Path(args.processed_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    # ── Imports requiring installed packages ─────────────────────────────────
    from qdrant_client import QdrantClient, models
    from FlagEmbedding import BGEM3FlagModel
    from langchain_experimental.text_splitter import SemanticChunker

    # ── Load BGE-M3 ──────────────────────────────────────────────────────────
    print(f"[INFO] Embedding model : {EMBEDDING_MODEL}")
    print(f"[INFO] Qdrant path     : {QDRANT_PATH}")
    print(f"[INFO] Collection      : {COLLECTION_NAME}")
    print("[INFO] Loading BGE-M3 (first run downloads ~2.3GB)...")

    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)

    embed_adapter = BGEM3DenseEmbeddings(bge_m3)
    semantic_chunker = SemanticChunker(
        embed_adapter,
        breakpoint_threshold_type="percentile",
        breakpoint_threshold_amount=90,
    )

    # ── Qdrant client ────────────────────────────────────────────────────────
    if QDRANT_URL:
        print(f"[INFO] Opening Qdrant server: {QDRANT_URL}")
        client = QdrantClient(url=QDRANT_URL)
    else:
        print(f"[INFO] Opening Qdrant local: {QDRANT_PATH}")
        client = QdrantClient(path=QDRANT_PATH)

    if args.rebuild:
        print("[INFO] --rebuild: clearing data/unstructure_processed/ + recreating collection...")
        for f in processed_dir.glob("*.txt"):
            f.unlink()
        if HASHES_FILE.exists():
            HASHES_FILE.unlink()

    ensure_collection(client, recreate=args.rebuild)

    hashes     = load_hashes()
    new_hashes: dict = {}

    raw_files = sorted(
        f for f in raw_dir.iterdir()
        if f.suffix.lower() in SUPPORTED_EXTS
    )
    if not raw_files:
        print(f"[WARN] No supported files found in {raw_dir}")
        return

    print(f"[INFO] Found {len(raw_files)} file(s) in {raw_dir}\n")

    new_count    = 0
    skip_count   = 0
    total_chunks = 0
    total_table_chunks = 0
    total_text_chunks  = 0

    for filepath in raw_files:
        current_hash             = compute_md5(filepath)
        new_hashes[str(filepath)] = current_hash

        if not args.rebuild and hashes.get(str(filepath)) == current_hash:
            skip_count += 1
            continue

        print(f"[PROC] {filepath.name}")

        filing_meta = {}
        if filepath.suffix.lower() in {".html", ".htm"}:
            filing_meta = extract_filing_metadata(filepath)
            if filing_meta:
                print(f"  [meta] {filing_meta}")

        # Fundamentals / IncomeStatement / News 等 .txt 檔沒有 filing_type/fiscal_year
        # 語意，但檔名仍以 TICKER_ 開頭；補上 ticker 讓 rag_query.py 的 ticker hard
        # filter 能匹配到這些來源，否則這些檔案會被 must-filter 整批排除（見
        # eval/eval_generation_llm_judge.py 跑批發現：AMZN EPS 等財報數字題因此
        # 永遠撈不到 Fundamentals/IncomeStatement chunk）。
        if "ticker" not in filing_meta:
            m_ticker = re.match(r"^([A-Z]+)_", filepath.stem)
            if m_ticker:
                filing_meta["ticker"] = m_ticker.group(1)

        elements = partition_and_clean(filepath)
        if not elements:
            print(f"  [SKIP] Could not partition file")
            continue

        proc_path = processed_dir / (filepath.stem + ".txt")
        proc_path.write_text(render_processed(elements), encoding="utf-8")

        # ── Chunking：Table 獨立成 chunk，其餘文字走語意切割 ──────────────
        records = build_chunk_records(elements, semantic_chunker)
        n_table = sum(1 for r in records if r["chunk_type"] == "table")
        n_text  = sum(1 for r in records if r["chunk_type"] == "text")
        total_chunks       += len(records)
        total_table_chunks += n_table
        total_text_chunks  += n_text
        print(f"  → {len(records)} chunks ({n_table} table + {n_text} text)  |  "
              f"processed: {proc_path.name}")

        if not records:
            continue

        # ── Replace existing points for this source ──────────────────────
        if not args.rebuild:
            try:
                delete_points_by_source(client, filepath.name)
            except Exception:
                pass

        # ── Encode: BGE-M3 returns dense + sparse in one shot ────────────
        # 前綴 ticker/期別 metadata header，讓 dense/sparse 也能感知期別
        # （見 build_metadata_header 註解：期別原文不在 chunk 文字裡）
        meta_header = build_metadata_header(filing_meta)
        chunk_texts = [meta_header + r["text"] for r in records]
        encoded = bge_m3.encode(
            chunk_texts, batch_size=8,
            return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        dense_vecs   = encoded["dense_vecs"]
        lex_weights  = encoded["lexical_weights"]  # list[dict[str, float]]

        # ── Build Qdrant points (dense + sparse + payload) ───────────────
        points = []
        for i, (record, dense_vec, weights) in enumerate(
            zip(records, dense_vecs, lex_weights)
        ):
            chunk_id_str = f"{filepath.stem}_c{i}"
            indices = [int(tok) for tok in weights.keys()]
            values  = [float(w) for w in weights.values()]

            payload = {
                "chunk_id":    chunk_id_str,
                "source":      filepath.name,
                "stem":        filepath.stem,
                "chunk_index": i,
                "chunk_type":  record["chunk_type"],   # "table" | "text"
                # 與被 embed 的文字一致（含 metadata header），讓 reranker 跟最終
                # 餵給 LLM 的 context 也看得到期別，不只是 embedding 階段。
                "document":    meta_header + record["text"],
            }
            payload.update(filing_meta)  # filing_type / fiscal_year / fiscal_period / report_label_year (HTML filings only)

            points.append(models.PointStruct(
                id=deterministic_uuid(chunk_id_str),
                vector={
                    DENSE_VECTOR_NAME:  dense_vec.tolist(),
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=indices, values=values,
                    ),
                },
                payload=payload,
            ))

        client.upsert(collection_name=COLLECTION_NAME, points=points)
        new_count += 1

    save_hashes(new_hashes)

    total_in_db = client.count(collection_name=COLLECTION_NAME, exact=True).count
    print(f"\n{'─'*50}")
    print(f"[DONE] Files processed   : {new_count}")
    print(f"[DONE] Files skipped     : {skip_count} (unchanged)")
    print(f"[DONE] Chunks this run   : {total_chunks} "
          f"({total_table_chunks} table + {total_text_chunks} text)")
    print(f"[DONE] Total in Qdrant   : {total_in_db} (each point holds dense + sparse)")
    print(f"[DONE] Qdrant path       : {QDRANT_PATH}")
    print(f"[DONE] Collection        : {COLLECTION_NAME}")


if __name__ == "__main__":
    main()
