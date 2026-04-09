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
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
CHROMA_PERSIST  = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL",
                             "paraphrase-multilingual-MiniLM-L12-v2")
COLLECTION_NAME = "us_stock_rag"
DEFAULT_TOP_K   = 5
DEFAULT_MODEL   = "gpt-oss:20b"
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


def retrieve(
    query: str,
    embed_model,
    client,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict]:
    """Embed query and return top-k chunks with metadata."""
    collection = get_collection(client)
    total      = collection.count()

    if total == 0:
        return []

    q_vec = embed_model.encode([query])[0].tolist()
    n     = min(top_k, total)

    results = collection.query(
        query_embeddings=[q_vec],
        n_results=n,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    docs, metas, dists = (
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    )
    for doc, meta, dist in zip(docs, metas, dists):
        chunks.append({
            "content":     doc,
            "source":      meta.get("source", "unknown"),
            "chunk_index": meta.get("chunk_index", 0),
            "similarity":  round(1.0 - dist, 4),   # cosine dist → similarity
        })

    return chunks


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
    lines = ["\n📚 Referenced Sources:"]
    for i, c in enumerate(chunks):
        sim_pct = f"{c['similarity']*100:.1f}%"
        lines.append(
            f"  [{i+1}] {c['source']}  |  chunk #{c['chunk_index']}"
            f"  |  similarity {sim_pct}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# LLM call
# ══════════════════════════════════════════════════════════════════════════════

def call_llm(messages: list[dict], model_name: str) -> str:
    import openai

    client = openai.OpenAI(
        api_key=os.getenv("LITELLM_API_KEY"),
        base_url=os.getenv("LITELLM_BASE_URL", "https://litellm.netdb.csie.ncku.edu.tw"),
    )
    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
    )
    return response.choices[0].message.content


# ══════════════════════════════════════════════════════════════════════════════
# Query modes
# ══════════════════════════════════════════════════════════════════════════════

def run_single_query(
    query: str,
    model: str,
    top_k: int,
    embed_model,
    client,
) -> None:
    print(f"\n🔍 Query: {query}")
    print("⏳ Retrieving chunks...")

    chunks = retrieve(query, embed_model, client, top_k)
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

        chunks = retrieve(query, embed_model, client, top_k)
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
    from sentence_transformers import SentenceTransformer

    print(f"[INFO] Loading embedding model: {EMBEDDING_MODEL}")
    embed_model = SentenceTransformer(EMBEDDING_MODEL)
    client      = chromadb.PersistentClient(path=CHROMA_PERSIST)

    # ── Dispatch ───────────────────────────────────────────────────────────────
    if args.query:
        run_single_query(args.query, args.model, args.top_k, embed_model, client)
    else:
        run_interactive(args.model, args.top_k, embed_model, client)


if __name__ == "__main__":
    main()
