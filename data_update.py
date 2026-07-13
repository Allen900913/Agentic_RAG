"""
data_update.py — US Stock RAG Data Pipeline (Qdrant Hybrid)

從 data/raw/ 讀取原始資料，執行清理、Semantic Chunking，
使用 BGE-M3 同時產生 Dense + Sparse 向量，並寫入 Qdrant：
  - Qdrant collection 設計兩個 named vectors:
      "dense"  → BGE-M3 dense (1024 維, cosine)
      "sparse" → BGE-M3 lexical_weights (token_id → weight)
  - 每筆 chunk 一筆 point，同時帶 dense + sparse + payload

支援：
  --rebuild   全量重建（清空 data/processed/、刪除 collection）
  預設         增量更新（以 MD5 hash 判斷是否變動）

Usage:
  python data_update.py              # 增量更新
  python data_update.py --rebuild    # 全量重建
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
PROCESSED_DIR    = Path("data/processed")
HASHES_FILE      = Path("hashes.json")
QDRANT_PATH      = os.getenv("QDRANT_PATH", "./qdrant_db")
EMBEDDING_MODEL  = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
COLLECTION_NAME  = "us_stock_rag"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"
DENSE_DIM        = 1024  # BGE-M3 dense dimension
SUPPORTED_EXTS   = {".txt", ".md", ".pdf", ".html", ".htm"}


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
# Filing metadata extraction (filing_type / fiscal_year / fiscal_period)
#
# 從 SEC filing 開頭的 XBRL dei: cover-page tags 讀取「這份文件本身是哪一期」，
# 寫進每個 chunk 的 payload，讓 rag_query.py 可以用 hard payload filter
# 排除/篩選期間錯誤的候選（例如使用者問 FY2025，就不該撈到 10-Q 的表格）。
# 檔名規則（如 AAPL_10K_2026.html）作為 dei: 標記缺漏時的備援。
# ══════════════════════════════════════════════════════════════════════════════

DEI_FIELD_MAP = {
    "dei:documenttype":              "filing_type",
    "dei:documentfiscalyearfocus":   "fiscal_year",
    "dei:documentfiscalperiodfocus": "fiscal_period",
}
_IX_NONNUMERIC_RE = re.compile(r"ix:nonnumeric", re.I)
_FILENAME_RE = re.compile(r"^(?P<ticker>[A-Z]+)_(?P<filing>10K|10Q)_(?P<yyyymm>\d{4,6})")


def extract_filing_metadata(path: Path) -> dict:
    """回傳 {"filing_type": "10-K", "fiscal_year": "2025", "fiscal_period": "FY"}。
    優先讀 XBRL dei: cover-page tags，缺漏的欄位用檔名規則補齊
    （例如 NVDA_10K_2026.html -> filing_type=10-K, fiscal_year=2026）。"""
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

    return meta


# ══════════════════════════════════════════════════════════════════════════════
# Text extraction
# ══════════════════════════════════════════════════════════════════════════════

def extract_text(path: Path) -> Optional[str]:
    ext = path.suffix.lower()

    if ext in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")

    if ext in {".html", ".htm"}:
        try:
            from bs4 import BeautifulSoup
            raw = path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "ix:header"]):
                tag.decompose()
            for tag in soup.find_all(
                style=lambda value: value and 'display:none' in value.replace(' ', '')
            ):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            return re.sub(r"<[^>]+>", " ", raw)

    if ext == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)
        except ImportError:
            pass
        try:
            import PyPDF2
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                return "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError:
            pass
        print(f"  [WARN] PDF support not available; install pdfplumber or PyPDF2")
        return None

    return None


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
        # Payload indexes for filing-period hard filtering (rag_query.py query_filter)
        for field_name in ("filing_type", "fiscal_year", "fiscal_period"):
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field_name,
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
        description="US Stock RAG — Data Update Pipeline (Qdrant Hybrid)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python data_update.py              # 增量更新（只處理有變動的檔案）
  python data_update.py --rebuild    # 全量重建
  python data_update.py --raw-dir ./my_data
        """,
    )
    parser.add_argument("--rebuild", action="store_true",
                        help="Clear data/processed/ and recreate the Qdrant collection from scratch")
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

    # ── Qdrant client (local persistent, no Docker) ──────────────────────────
    client = QdrantClient(path=QDRANT_PATH)

    if args.rebuild:
        print("[INFO] --rebuild: clearing data/processed/ + recreating collection...")
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
        # filter 能匹配到這些來源，否則這些檔案會被 must-filter 整批排除。
        if "ticker" not in filing_meta:
            m_ticker = re.match(r"^([A-Z]+)_", filepath.stem)
            if m_ticker:
                filing_meta["ticker"] = m_ticker.group(1)

        raw_text = extract_text(filepath)
        if not raw_text:
            print(f"  [SKIP] Could not extract text")
            continue

        clean = clean_text(raw_text)
        proc_path = processed_dir / (filepath.stem + ".txt")
        proc_path.write_text(clean, encoding="utf-8")

        # ── Semantic Chunking ────────────────────────────────────────────
        docs = semantic_chunker.create_documents([clean])
        chunks = [doc.page_content for doc in docs if len(doc.page_content.strip()) > 20]
        total_chunks += len(chunks)
        print(f"  → {len(chunks)} semantic chunks  |  processed: {proc_path.name}")

        if not chunks:
            continue

        # ── Replace existing points for this source ──────────────────────
        if not args.rebuild:
            try:
                delete_points_by_source(client, filepath.name)
            except Exception:
                pass

        # ── Encode: BGE-M3 returns dense + sparse in one shot ────────────
        encoded = bge_m3.encode(
            chunks, batch_size=8,
            return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        dense_vecs   = encoded["dense_vecs"]
        lex_weights  = encoded["lexical_weights"]  # list[dict[str, float]]

        # ── Build Qdrant points (dense + sparse + payload) ───────────────
        points = []
        for i, (chunk_text, dense_vec, weights) in enumerate(
            zip(chunks, dense_vecs, lex_weights)
        ):
            chunk_id_str = f"{filepath.stem}_c{i}"
            indices = [int(tok) for tok in weights.keys()]
            values  = [float(w) for w in weights.values()]

            payload = {
                "chunk_id":    chunk_id_str,
                "source":      filepath.name,
                "stem":        filepath.stem,
                "chunk_index": i,
                "document":    chunk_text,
            }
            payload.update(filing_meta)  # filing_type / fiscal_year / fiscal_period (HTML filings only)

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
    print(f"[DONE] Chunks this run   : {total_chunks}")
    print(f"[DONE] Total in Qdrant   : {total_in_db} (each point holds dense + sparse)")
    print(f"[DONE] Qdrant path       : {QDRANT_PATH}")
    print(f"[DONE] Collection        : {COLLECTION_NAME}")


if __name__ == "__main__":
    main()
