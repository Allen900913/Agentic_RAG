"""
compare_generation.py — 並排比較：同一題目分別在「舊版 us_stock_rag」與
「新版 us_stock_rag_unstructured」collection 下，檢索到的內容與 Gemini 生成的最終答案。

動機：retrieval eval（eval_retrieval.py）的 ground truth 標記在「來源檔案」層級，
量不出「同一份檔案中，撈到的 chunk 內容本身夠不夠好」這件事 —— 而這正是新版
pipeline（表格獨立成 chunk，保留列/欄結構）想改善的目標。本腳本直接比較最終
生成的答案，看 retrieval 數字打平/略降是否真的反映在回答品質上。

聚焦 lexical 類別（財報數字題）：retrieval eval 顯示新版在這個類別反而退步，
理論上「表格保留」最該在這裡發揮優勢，所以最值得直接看生成結果。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import rag_query as rq

OLD_COLLECTION = "us_stock_rag"
NEW_COLLECTION = "us_stock_rag_unstructured"

QUERIES = [
    ("lex-01", "NVDA P/E ratio"),
    ("lex-02", "AAPL EPS"),
    ("lex-03", "MSFT gross margin"),
    ("lex-06", "NVDA income statement 2026"),
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
    answer = rq.call_llm(messages, rq.DEFAULT_MODEL, temperature=rq.GEN_TEMPERATURE)
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
    # 依 .env 的 QDRANT_URL 自動選 Docker server / local path（Docker 遷移後 local
    # ./qdrant_db 已是凍結舊快照，見 CHANGELOG 2026-07-07 Qdrant client bug）
    client = rq.make_qdrant_client()

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

    out_path = Path("eval/generation_compare.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {out_path}")


if __name__ == "__main__":
    main()
