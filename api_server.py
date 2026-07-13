"""
api_server.py — FastAPI 後端，把 CLI 版 RAG（rag_query.py）包成一個長駐 HTTP 服務。

為什麼需要這個 server（架構推導，不是選擇）：
  1. 模型暖機成本極高：BGE-M3 + bge-reranker-v2-m3 載入慢、常駐約 8–10GB RAM，
     不能每個 request 重載 → 啟動時載入一次、保持暖機（見 lifespan）。
  2. Qdrant local 模式是單一持有者：QdrantClient(path=...) 會拿排他檔案鎖，
     只有一個 process 能持有 → 這個 server 是 qdrant_db 的「唯一擁有者」。
     ⚠ server 跑的時候，不能同時跑 rag_query.py / data_update.py / eval。
  3. 延遲高（CPU rerank 每題數分鐘）→ async + SSE 串流，並在出 token 前先
     回報「檢索中 / 重排中 / 生成中」狀態。

對外契約：
  GET  /health → {"status","collection","count","llm_backend","model"}
  POST /chat   → text/event-stream，事件序列：
                 status(retrieving/reranking/generating) → sources → token* → done
                 出錯則 error。
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import rag_query as rq

# ── LLM 後端選擇 ────────────────────────────────────────────────────────────────
# 預設 Gemini（沿用 rag_query.call_llm）；設 LLM_BACKEND=groq 改走 Groq（OpenAI 相容）。
# 這同時影響 retrieve() 內部 parse_query_filters() 走的 rq.call_llm —— 比照
# eval_generation_llm_judge.py 的 monkeypatch，把整個模組的 call_llm 換掉。
LLM_BACKEND      = os.getenv("LLM_BACKEND", "groq").lower()
GROQ_BASE_URL    = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
NVIDIA_BASE_URL    = "https://integrate.api.nvidia.com/v1"
DEFAULT_NVIDIA_MODEL = "meta/llama-3.3-70b-instruct"

if LLM_BACKEND == "groq":
    DEFAULT_MODEL = os.getenv("LLM_MODEL", DEFAULT_GROQ_MODEL)

    def call_llm_groq(messages: list[dict], model_name: str, temperature: float = 0.0) -> str:
        # temperature 預設 0：這個函式會被 monkeypatch 成 rq.call_llm，供 retrieve() 內部的
        # filter/rewrite/translate 使用，檢索側必須確定性（見 CLAUDE.md 溫度分工）。
        import openai

        client = openai.OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url=GROQ_BASE_URL)
        resp = client.chat.completions.create(
            model=model_name, messages=messages, temperature=temperature,
        )
        return resp.choices[0].message.content

    # 讓 retrieve() 內部的 query-filter LLM call 也走 Groq
    rq.call_llm = call_llm_groq
elif LLM_BACKEND == "nvidia":
    DEFAULT_MODEL = os.getenv("LLM_MODEL", DEFAULT_NVIDIA_MODEL)

    def call_llm_nvidia(messages: list[dict], model_name: str, temperature: float = 0.0) -> str:
        import openai

        client = openai.OpenAI(api_key=os.getenv("NVIDIA_API_KEY"), base_url=NVIDIA_BASE_URL)
        resp = client.chat.completions.create(
            model=model_name, messages=messages, temperature=temperature,
        )
        return resp.choices[0].message.content

    # 讓 retrieve() 內部的 query-filter LLM call 也走 NVIDIA NIM
    rq.call_llm = call_llm_nvidia
else:
    DEFAULT_MODEL = os.getenv("LLM_MODEL", rq.DEFAULT_MODEL)


# ── 串流 LLM 生成（依後端切換）────────────────────────────────────────────────────
def stream_llm(messages: list[dict], model_name: str):
    """逐 chunk yield 文字片段。Gemini、Groq、NVIDIA NIM 三種後端都支援串流。"""
    if LLM_BACKEND == "groq":
        import openai

        client = openai.OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url=GROQ_BASE_URL)
        stream = client.chat.completions.create(
            model=model_name, messages=messages, stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
        return

    if LLM_BACKEND == "nvidia":
        import openai

        client = openai.OpenAI(api_key=os.getenv("NVIDIA_API_KEY"), base_url=NVIDIA_BASE_URL)
        stream = client.chat.completions.create(
            model=model_name, messages=messages, stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
        return

    # Gemini
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    system_prompt = None
    conversation  = []
    for msg in messages:
        role, content = msg["role"], msg["content"]
        if role == "system":
            system_prompt = content
        elif role == "user":
            conversation.append({"role": "user",  "parts": [{"text": content}]})
        elif role == "assistant":
            conversation.append({"role": "model", "parts": [{"text": content}]})

    config = types.GenerateContentConfig(system_instruction=system_prompt) if system_prompt else None
    for chunk in client.models.generate_content_stream(
        model=model_name, contents=conversation, config=config,
    ):
        if chunk.text:
            yield chunk.text


# ── App / 模型生命週期 ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    collection = os.getenv("COLLECTION_NAME", rq.COLLECTION_NAME)
    rq.COLLECTION_NAME = collection

    print(f"[INFO] LLM backend = {LLM_BACKEND}  (model={DEFAULT_MODEL})")
    print(f"[INFO] Loading BGE-M3 (dense+sparse): {rq.EMBEDDING_MODEL}")
    app.state.bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    print(f"[INFO] Loading reranker: {rq.RERANK_MODEL}")
    app.state.rerank_model = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    print(f"[INFO] Opening Qdrant (exclusive lock): {rq.QDRANT_PATH}")
    app.state.client = QdrantClient(path=rq.QDRANT_PATH)
    # 模型/Qdrant client 非執行緒安全且每題很重 → 序列化 /chat
    app.state.lock = asyncio.Lock()
    app.state.collection = collection
    print("[INFO] Ready. ⚠ 此 server 獨佔 qdrant_db，請勿同時跑 CLI / eval。")

    yield

    app.state.client.close()


app = FastAPI(title="US Stock Intelligence RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 本機 demo；上線請收斂成 Streamlit 來源
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    query: str
    history: list[dict] = []           # [{"role":"user"/"assistant","content":...}, ...]
    top_k: int = rq.DEFAULT_TOP_K
    model: str | None = None


@app.get("/health")
def health():
    try:
        count = app.state.client.count(
            collection_name=app.state.collection, exact=True,
        ).count
        status = "ok"
    except Exception as e:
        count, status = None, f"error: {e!r}"
    return {
        "status": status,
        "collection": app.state.collection,
        "count": count,
        "llm_backend": LLM_BACKEND,
        "model": DEFAULT_MODEL,
    }


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


@app.post("/chat")
async def chat(req: ChatRequest):
    model = req.model or DEFAULT_MODEL

    async def event_gen():
        # 序列化：同一時間只跑一題（模型/Qdrant 非 thread-safe）
        async with app.state.lock:
            loop = asyncio.get_running_loop()
            try:
                yield _sse("status", {"stage": "retrieving"})

                # 檢索 + rerank 是同步且重，丟到 threadpool 避免卡住 event loop
                # enable_rewrite / translate_query_en 對齊 CLI 生產預設（2026-07-13 轉正，
                # 見 CHANGELOG）：rewrite 擴召回 + 雙 query 取最高分 rerank。
                def _retrieve():
                    return rq.retrieve(
                        req.query, app.state.bge_m3, app.state.rerank_model,
                        app.state.client, top_k=req.top_k, model_name=model,
                        enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                        translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN,
                    )

                chunks, fallback_note = await loop.run_in_executor(None, _retrieve)

                if not chunks:
                    yield _sse("sources", {"chunks": []})
                    msg = "我的知識庫裡沒有足夠資訊可以回答這個問題。"
                    yield _sse("token", {"text": msg})
                    yield _sse("done", {})
                    return

                yield _sse("status", {"stage": "reranking"})
                if fallback_note:
                    yield _sse("fallback_note", {"text": fallback_note})
                yield _sse("sources", {"chunks": [
                    {
                        "source": c["source"],
                        "chunk_index": c["chunk_index"],
                        "source_type": c["source_type"],
                        "rrf_score": round(c["rrf_score"], 6),
                        "rerank_score": round(c["rerank_score"], 4),
                        "preview": c["content"].strip()[:300],
                    }
                    for c in chunks
                ]})

                # 組 prompt（沿用 CLI 的 SYSTEM_PROMPT / build_user_prompt / 多輪截斷）
                user_prompt = rq.build_user_prompt(req.query, chunks, fallback_note)
                messages = [{"role": "system", "content": rq.SYSTEM_PROMPT}]
                messages.extend(req.history[-(rq.MAX_HISTORY * 2):])
                messages.append({"role": "user", "content": user_prompt})

                yield _sse("status", {"stage": "generating"})

                # 串流生成：把同步 generator 逐塊抽到 threadpool，避免阻塞 event loop
                gen = stream_llm(messages, model)
                sentinel = object()
                while True:
                    piece = await loop.run_in_executor(None, lambda: next(gen, sentinel))
                    if piece is sentinel:
                        break
                    yield _sse("token", {"text": piece})

                yield _sse("done", {})
            except Exception as e:
                yield _sse("error", {"message": repr(e)})

    return EventSourceResponse(event_gen())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
