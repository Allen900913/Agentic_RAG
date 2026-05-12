"""
rag_query.py — US Stock Intelligence RAG Query Interface

使用 ChromaDB 向量檢索 + OpenAI-compatible API 生成回答，每次回答都附帶引用來源。

Usage:
  python rag_query.py                                  # 互動式多輪對話
  python rag_query.py --query "NVIDIA 毛利率多少？"    # 單次查詢
  python rag_query.py --query "..." --top-k 3
  python rag_query.py --query "..." --model gpt-oss:20b
  python rag_query.py --help
"""

import argparse
import math
import os
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

# ── Config ─────────────────────────────────────────────────────────────────────
CHROMA_PERSIST  = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL",
                             "paraphrase-multilingual-MiniLM-L12-v2")
RERANK_MODEL    = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base") 
COLLECTION_NAME = "us_stock_rag"
DEFAULT_TOP_K   = 5
FETCH_N         = 10
DEFAULT_MODEL   = "gemini-3-flash-preview"
MAX_HISTORY     = 3   # 保留最近 N 輪對話歷史

SYSTEM_PROMPT = """\
You are a professional US stock market analyst and financial expert specializing \
in NVIDIA (NVDA), Microsoft (MSFT), and Apple (AAPL).

Rules:
1. Answer ONLY based on the provided reference materials.
2. After EVERY factual claim, add a citation: [filename, chunk #N]
3. If the answer is not in the materials, state "I don't have enough information \
in my knowledge base to answer this."
4. Be concise, precise, and data-driven.
5. When citing numbers (revenue, margins, etc.), always specify the time period.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Retrieval helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_collection(client):
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def infer_source_type(source: str) -> str:
    source_upper = source.upper()
    if "_NEWS_" in source_upper:
        return "news"
    if "_10K_" in source_upper or "10-K" in source_upper:
        return "10-K"
    if "_10Q_" in source_upper or "10-Q" in source_upper:
        return "10-Q"
    if "FUNDAMENTALS" in source_upper:
        return "fundamentals"
    if "INCOMESTATEMENT" in source_upper:
        return "income_statement"
    return "other"


def looks_like_news_query(query: str) -> bool:
    query_lower = query.lower()
    keywords = (
        "news", "headline", "headlines", "favorable", "unfavorable",
        "新聞", "消息", "利多", "利空", "有利", "不利",
    )
    return any(keyword in query_lower for keyword in keywords)


def retrieve(
    query: str,
    embed_model,
    rerank_model,
    client,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict]:
    """Retrieve candidates, score them with a Cross-Encoder, and return top-k."""
    collection = get_collection(client)
    total      = collection.count()

    if total == 0:
        return []

    # 1. 初篩 (Initial Retrieval)：撈出 FETCH_N 筆
    q_vec = embed_model.encode([query])[0].tolist()
    n     = min(FETCH_N, total)

    results = collection.query(
        query_embeddings=[q_vec],
        n_results=n,
        include=["documents", "metadatas", "distances"],
    )

    docs, metas, dists = (
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    )

    # 2. 準備 Rerank 的輸入配對： [(query, doc1), (query, doc2), ...]
    cross_input = [[query, doc] for doc in docs]

    # 3. 進行精篩打分 (使用 Sigmoid 轉換為 0~1 的機率值)
    raw_scores = rerank_model.predict(cross_input)
    raw_scores = raw_scores.tolist() if hasattr(raw_scores, "tolist") else list(raw_scores)

    # 4. 將資料打包並根據 rerank_score 降冪排序
    scored_chunks = []
    for doc, meta, dist, raw_score in zip(docs, metas, dists, raw_scores):
        source = meta.get("source", "unknown")
        scored_chunks.append({
            "content": doc,
            "source": source,
            "source_type": infer_source_type(source),
            "chunk_index": meta.get("chunk_index", 0),
            "vector_score": round(1.0 - dist, 4),
            "raw_rerank_score": float(raw_score),
            "rerank_score": float(1 / (1 + math.exp(-raw_score))),
        })

    # 依照 rerank_score 由大到小排序
    scored_chunks.sort(
        key=lambda x: (x["raw_rerank_score"], x["vector_score"]),
        reverse=True,
    )

    if raw_scores:
        min_score = min(raw_scores)
        max_score = max(raw_scores)
        span = max_score - min_score
        print(
            "DEBUG - Rerank raw score range: "
            f"min={min_score:.6f}, max={max_score:.6f}, span={span:.6f}"
        )
        if span < 0.05:
            print(
                "WARN  - Reranker sees these candidates as almost equally relevant. "
                "The rerank signal is weak."
            )

    if looks_like_news_query(query) and not any(c["source_type"] == "news" for c in scored_chunks):
        print(
            "WARN  - This looks like a news query, but no news chunks were retrieved. "
            "Reranking can only reorder filings/fundamentals."
        )

    print("DEBUG - Top reranked candidates:")
    for i, chunk in enumerate(scored_chunks[:top_k], start=1):
        print(
            f"  [{i}] {chunk['source']} | chunk #{chunk['chunk_index']} | "
            f"type={chunk['source_type']} | vector={chunk['vector_score']:.4f} | "
            f"raw={chunk['raw_rerank_score']:.6f} | sigmoid={chunk['rerank_score']:.6f}"
        )

    return scored_chunks[:top_k]


# ══════════════════════════════════════════════════════════════════════════════
# Prompt assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_user_prompt(query: str, chunks: list[dict]) -> str:
    context_parts = []
    for i, c in enumerate(chunks):
        context_parts.append(
            f"[Reference {i+1}: {c['source']}, chunk #{c['chunk_index']}]\n"
            f"{c['content'].strip()}"
        )
    context = "\n\n".join(context_parts)

    return (
        f"=== Reference Materials ===\n"
        f"{context}\n\n"
        f"=== Question ===\n"
        f"{query}\n\n"
        f"Please answer based solely on the reference materials above. "
        f"Cite every factual claim using [filename, chunk #N] format."
    )


def format_sources(chunks: list[dict]) -> str:
    lines = ["\n📚 Referenced Sources (Re-ranked):"]
    for i, c in enumerate(chunks):
        lines.append(
            f"  [{i+1}] {c['source']}  |  chunk #{c['chunk_index']}"
            f"  |  Raw: {c['raw_rerank_score']:.4f}"
            f"  |  Sigmoid: {c['rerank_score']:.4f}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# LLM call
# ══════════════════════════════════════════════════════════════════════════════

def call_llm(messages: list[dict], model_name: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    # 分離 system prompt 和對話歷史
    system_prompt = None
    conversation  = []
    for msg in messages:
        role    = msg["role"]
        content = msg["content"]
        if role == "system":
            system_prompt = content          # system → system_instruction
        elif role == "user":
            conversation.append({"role": "user",  "parts": [{"text": content}]})
        elif role == "assistant":
            conversation.append({"role": "model", "parts": [{"text": content}]})
            # Google genai 用 "model" 而非 "assistant"

    config = None
    if system_prompt:
        config = types.GenerateContentConfig(system_instruction=system_prompt)

    response = client.models.generate_content(
        model=model_name,
        contents=conversation,
        config=config,
    )
    return response.text


# ══════════════════════════════════════════════════════════════════════════════
# Query modes
# ══════════════════════════════════════════════════════════════════════════════

def run_single_query(
    query: str,
    model: str,
    top_k: int,
    embed_model,
    rerank_model,
    client,
) -> None:
    print(f"\n🔍 Query: {query}")
    print("⏳ Retrieving chunks...")

    chunks = retrieve(query, embed_model, rerank_model, client, top_k)
    if not chunks:
        print("❌ No chunks found. Run: python data_update.py --rebuild")
        return

    user_prompt = build_user_prompt(query, chunks)
    messages    = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]

    print(f"🤖 Calling {model}...")
    answer = call_llm(messages, model)

    print(f"\n{'═'*60}")
    print("💡 Answer:")
    print(answer)
    print(format_sources(chunks))
    print("═" * 60)


def run_interactive(
    model: str,
    top_k: int,
    embed_model,
    rerank_model,
    client,
) -> None:
    print("\n🚀 US Stock Intelligence RAG — Interactive Mode")
    print(f"   Model: {model}  |  Top-k: {top_k}")
    print("   Type your question, or 'quit' / 'exit' to exit.")
    print("═" * 60)

    history: list[dict] = []   # stores {"role": ..., "content": ...}

    while True:
        try:
            query = input("\n❓ Question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye! 👋")
            break

        if not query:
            continue
        if query.lower() in {"quit", "exit", "q", "bye"}:
            print("Goodbye! 👋")
            break

        chunks = retrieve(query, embed_model, rerank_model, client, top_k)
        if not chunks:
            print("❌ No chunks found. Run: python data_update.py --rebuild")
            continue

        user_prompt = build_user_prompt(query, chunks)

        # Build message list: system + trimmed history + current
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Keep last MAX_HISTORY rounds (each round = 1 user + 1 assistant)
        trimmed  = history[-(MAX_HISTORY * 2):]
        messages.extend(trimmed)
        messages.append({"role": "user", "content": user_prompt})

        print(f"🤖 Generating answer ({model})...")
        answer = call_llm(messages, model)

        print(f"\n{'─'*60}")
        print("💡 Answer:")
        print(answer)
        print(format_sources(chunks))
        print("─" * 60)

        # Update history (store plain query for context, not the full prompt)
        history.append({"role": "user",      "content": query})
        history.append({"role": "assistant", "content": answer})


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="US Stock Intelligence — RAG Query Interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python rag_query.py                                  # 互動式對話模式
  python rag_query.py --query "NVDA revenue growth?"   # 單次查詢
  python rag_query.py --query "..." --top-k 3          # 只取 top 3 chunks
  python rag_query.py --query "..." --model gpt-oss:20b
        """,
    )
    parser.add_argument(
        "--query", "-q", type=str, default=None,
        help="Direct query string (single-query mode)",
    )
    parser.add_argument(
        "--top-k", "-k", type=int, default=DEFAULT_TOP_K,
        help=f"Number of chunks to retrieve (default: {DEFAULT_TOP_K})",
    )
    parser.add_argument(
        "--model", "-m", type=str, default=DEFAULT_MODEL,
        help=f"LLM model name (default: {DEFAULT_MODEL}). Available: gpt-oss:20b",
    )
    args = parser.parse_args()

    # ── Load heavy deps ────────────────────────────────────────────────────────
    import chromadb
    from sentence_transformers import SentenceTransformer, CrossEncoder

    print(f"[INFO] Loading embedding model: {EMBEDDING_MODEL}")
    embed_model = SentenceTransformer(EMBEDDING_MODEL)

    print(f"[INFO] Loading rerank model: {RERANK_MODEL}")
    rerank_model = CrossEncoder(RERANK_MODEL)

    client      = chromadb.PersistentClient(path=CHROMA_PERSIST)

    # ── Dispatch ───────────────────────────────────────────────────────────────
    if args.query:
        run_single_query(args.query, args.model, args.top_k, embed_model, rerank_model, client)
    else:
        run_interactive(args.model, args.top_k, embed_model, rerank_model, client)


if __name__ == "__main__":
    main()
