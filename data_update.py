"""
data_update.py — US Stock RAG Data Pipeline

從 data/raw/ 讀取原始資料，執行清理、Chunking、Embedding，並寫入 ChromaDB。

支援：
  --rebuild   全量重建（清空 data/processed/ 並重建索引）
  預設         增量更新（以 MD5 hash 判斷是否變動）

Usage:
  python data_update.py              # 增量更新
  python data_update.py --rebuild    # 全量重建
  python data_update.py --help       # 顯示說明
"""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
RAW_DIR          = Path("data/raw")
PROCESSED_DIR    = Path("data/processed")
HASHES_FILE      = Path("hashes.json")
CHROMA_PERSIST   = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
EMBEDDING_MODEL  = os.getenv("EMBEDDING_MODEL",
                              "paraphrase-multilingual-MiniLM-L12-v2")
COLLECTION_NAME  = "us_stock_rag"
CHUNK_SIZE       = 500
CHUNK_OVERLAP    = 50
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


# ══════════════════════════════════════════════════════════════════════════════
# Text extraction
# ══════════════════════════════════════════════════════════════════════════════

def extract_text(path: Path) -> Optional[str]:
    """Extract raw text from supported file types."""
    ext = path.suffix.lower()

    if ext in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")

    if ext in {".html", ".htm"}:
        # 用 BeautifulSoup 抜除 HTML 標籤，取出純文字
        try:
            from bs4 import BeautifulSoup
            raw = path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(raw, "html.parser")
            
            # 移除一般不需萃取的標籤與 SEC 的 iXBRL metadata 標籤
            for tag in soup(["script", "style", "nav", "footer", "header", "ix:header"]):
                tag.decompose()
            
            # 移除設定為隱藏的元素（例如 display: none）以過濾掉多餘的 SEC 標籤
            for tag in soup.find_all(style=lambda value: value and 'display:none' in value.replace(' ', '')):
                tag.decompose()
                
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            # bs4 未安裝，用 re 簡單去標籤
            import re
            raw = path.read_text(encoding="utf-8", errors="ignore")
            return re.sub(r"<[^>]+>", " ", raw)

    if ext == ".pdf":
        # Try pdfplumber first, fall back to PyPDF2
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


# ══════════════════════════════════════════════════════════════════════════════
# Text cleaning
# ══════════════════════════════════════════════════════════════════════════════

def clean_text(text: str) -> str:
    """Remove HTML tags, normalise whitespace, strip leading/trailing."""
    text = re.sub(r"<[^>]+>", " ", text)           # HTML tags
    text = re.sub(r"&[a-zA-Z]+;", " ", text)       # HTML entities
    text = re.sub(r"\r\n", "\n", text)              # CRLF → LF
    text = re.sub(r"[ \t]+", " ", text)             # multiple spaces/tabs
    text = re.sub(r"\n{3,}", "\n\n", text)          # 3+ blank lines → 2
    return text.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Chunking
# ══════════════════════════════════════════════════════════════════════════════

def chunk_text(
    text: str,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """
    固定長度 + Overlap 切塊策略。
    每塊 `size` 字元，相鄰塊重疊 `overlap` 字元，避免語意斷裂。
    """
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunk = text[start : start + size]
        if len(chunk.strip()) > 20:     # 過濾過短的片段
            chunks.append(chunk)
        start += size - overlap
    return chunks


# ══════════════════════════════════════════════════════════════════════════════
# ChromaDB helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_collection(client):
    """Get or create ChromaDB collection with cosine similarity."""
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="US Stock RAG — Data Update Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python data_update.py              # 增量更新（只處理有變動的檔案）
  python data_update.py --rebuild    # 全量重建（清空並重建整個索引）
  python data_update.py --raw-dir ./my_data  # 指定原始資料目錄
        """,
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Clear data/processed/ and rebuild the vector index from scratch",
    )
    parser.add_argument(
        "--raw-dir", default=str(RAW_DIR),
        help=f"Raw data directory (default: {RAW_DIR})",
    )
    parser.add_argument(
        "--processed-dir", default=str(PROCESSED_DIR),
        help=f"Processed text output directory (default: {PROCESSED_DIR})",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=CHUNK_SIZE,
        help=f"Characters per chunk (default: {CHUNK_SIZE})",
    )
    parser.add_argument(
        "--overlap", type=int, default=CHUNK_OVERLAP,
        help=f"Overlap characters between chunks (default: {CHUNK_OVERLAP})",
    )
    args = parser.parse_args()

    raw_dir       = Path(args.raw_dir)
    processed_dir = Path(args.processed_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    # ── Imports that require installed packages ──────────────────────────────
    import chromadb
    from sentence_transformers import SentenceTransformer

    # ── Load model ────────────────────────────────────────────────────────────
    print(f"[INFO] Embedding model : {EMBEDDING_MODEL}")
    print(f"[INFO] ChromaDB path   : {CHROMA_PERSIST}")
    embed_model = SentenceTransformer(EMBEDDING_MODEL)

    # ── ChromaDB client ───────────────────────────────────────────────────────
    client = chromadb.PersistentClient(path=CHROMA_PERSIST)

    # ── Rebuild: clear everything ─────────────────────────────────────────────
    if args.rebuild:
        print("[INFO] --rebuild: clearing data/processed/ and vector DB...")
        # Clear processed files
        for f in processed_dir.glob("*.txt"):
            f.unlink()
        # Drop collection
        try:
            client.delete_collection(COLLECTION_NAME)
            print(f"[INFO] Deleted collection '{COLLECTION_NAME}'")
        except Exception:
            pass
        # Clear hash state
        if HASHES_FILE.exists():
            HASHES_FILE.unlink()
        print("[INFO] Rebuild ready. Starting fresh indexing...\n")

    collection = get_collection(client)

    # ── Load existing hashes ──────────────────────────────────────────────────
    hashes     = load_hashes()
    new_hashes: dict = {}

    # ── Collect raw files ─────────────────────────────────────────────────────
    raw_files = sorted(
        f for f in raw_dir.iterdir()
        if f.suffix.lower() in SUPPORTED_EXTS
    )

    if not raw_files:
        print(f"[WARN] No supported files found in {raw_dir}")
        print(f"       Expected extensions: {', '.join(SUPPORTED_EXTS)}")
        return

    print(f"[INFO] Found {len(raw_files)} file(s) in {raw_dir}\n")

    new_count  = 0
    skip_count = 0
    total_chunks = 0

    for filepath in raw_files:
        current_hash             = compute_md5(filepath)
        new_hashes[str(filepath)] = current_hash

        # ── Incremental: skip unchanged ───────────────────────────────────
        if not args.rebuild and hashes.get(str(filepath)) == current_hash:
            skip_count += 1
            continue

        print(f"[PROC] {filepath.name}")

        # ── Extract ───────────────────────────────────────────────────────
        raw_text = extract_text(filepath)
        if not raw_text:
            print(f"  [SKIP] Could not extract text")
            continue

        # ── Clean ─────────────────────────────────────────────────────────
        clean = clean_text(raw_text)

        # ── Save to data/processed/ ───────────────────────────────────────
        proc_path = processed_dir / (filepath.stem + ".txt")
        proc_path.write_text(clean, encoding="utf-8")

        # ── Chunk ─────────────────────────────────────────────────────────
        chunks = chunk_text(clean, args.chunk_size, args.overlap)
        total_chunks += len(chunks)
        print(f"  → {len(chunks)} chunks  |  processed: {proc_path.name}")

        if not chunks:
            continue

        # ── Remove old vectors for this source (idempotency) ──────────────
        try:
            existing = collection.get(where={"source": filepath.name})
            if existing["ids"]:
                collection.delete(ids=existing["ids"])
        except Exception:
            pass

        # ── Embed ─────────────────────────────────────────────────────────
        embeddings = embed_model.encode(
            chunks, batch_size=32, show_progress_bar=False
        ).tolist()

        # ── Store ─────────────────────────────────────────────────────────
        ids       = [f"{filepath.stem}_c{i}" for i in range(len(chunks))]
        metadatas = [
            {"source": filepath.name, "chunk_index": i, "stem": filepath.stem}
            for i in range(len(chunks))
        ]
        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
        )
        new_count += 1

    # ── Persist hashes ────────────────────────────────────────────────────────
    save_hashes(new_hashes)

    total_in_db = collection.count()
    print(f"\n{'─'*50}")
    print(f"[DONE] Files processed  : {new_count}")
    print(f"[DONE] Files skipped    : {skip_count} (unchanged)")
    print(f"[DONE] Chunks this run  : {total_chunks}")
    print(f"[DONE] Total in DB      : {total_in_db} chunk(s)")
    print(f"[DONE] ChromaDB path    : {CHROMA_PERSIST}")


if __name__ == "__main__":
    main()
