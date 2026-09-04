"""
api_server.py — FastAPI 後端，把 CLI 版 RAG（rag_query.py）包成一個長駐 HTTP 服務。

為什麼需要這個 server（架構推導，不是選擇）：
  1. 模型暖機成本極高：BGE-M3 + bge-reranker-v2-m3 載入慢、常駐約 8–10GB RAM，
     不能每個 request 重載 → 啟動時載入一次、保持暖機（見 lifespan）。
  2. Qdrant 連線一律走 `rq.make_qdrant_client()`（依 `QDRANT_URL` 決定 server／local）。
     ⚠ **不要在這裡自己 `QdrantClient(path=...)`**——2026-08-14 前這裡就是這樣寫死 local
     模式、忽略 `QDRANT_URL`，而 `.env` 早已切到 Docker server：於是 server 會在不存在的
     路徑上開一個**空的**本機 DB，`/health` 查 collection 報錯、`/chat` 每題都回
     「知識庫裡沒有足夠資訊」——**沉默地壞**。全 repo 只有這一支漏改。
     local 模式時 `QdrantClient(path=...)` 會拿排他檔案鎖（只有一個 process 能持有），
     所以走 local 時 server 跑著就不能同時跑 rag_query.py / data_update.py / eval。
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

# ── LLM 後端 ────────────────────────────────────────────────────────────────
# 2026-07-21 統一改走 NVIDIA NIM：rq.call_llm 本身已經是「'gemini-' 開頭走 Gemini，其餘走
# NVIDIA」的路由（見 rag_query.py call_llm），這裡不需要再 monkeypatch、也不需要重複維護一份
# Groq/NVIDIA 呼叫邏輯 —— 直接沿用 rq.call_llm 供 retrieve() 內部 filter/rewrite/translate 用，
# stream_llm 只需跟它走一樣的路由規則。
NVIDIA_BASE_URL = rq.NVIDIA_BASE_URL

# retrieval_model 與 gen_model 是兩個獨立維度（對齊 CLI 的 --model / --gen-model，
# 以及 CLAUDE.md「model_name 陷阱」）：
#   - retrieval_model：retrieve() 內部 filter/rewrite/translate 用，必須跟 eval 的
#     retrieval side（rq.DEFAULT_MODEL）一致，否則 web 路徑量到的檢索行為會與 eval 不符。
#   - gen_model：生成答案用，預設 rq.DEFAULT_GEN_MODEL（模型名不在這裡複述——2026-09-03 換過一次）。
DEFAULT_RETRIEVAL_MODEL = os.getenv("LLM_MODEL", rq.DEFAULT_MODEL)
DEFAULT_GEN_MODEL       = os.getenv("LLM_GEN_MODEL", rq.DEFAULT_GEN_MODEL)


# ── 串流 LLM 生成 ────────────────────────────────────────────────────────────
def stream_llm(messages: list[dict], model_name: str):
    """逐 chunk yield 文字片段。跟 rq.call_llm 同一套路由規則：'gemini-' 開頭走 Gemini
    串流，其餘走 NVIDIA NIM 串流。"""
    if not model_name.startswith("gemini-"):
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

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    collection = os.getenv("COLLECTION_NAME", rq.COLLECTION_NAME)
    rq.COLLECTION_NAME = collection

    _backend = "gemini" if DEFAULT_GEN_MODEL.startswith("gemini-") else "nvidia"
    print(f"[INFO] LLM backend = {_backend}  "
          f"(retrieval={DEFAULT_RETRIEVAL_MODEL}, gen={DEFAULT_GEN_MODEL})")
    print(f"[INFO] Loading BGE-M3 (dense+sparse): {rq.EMBEDDING_MODEL}")
    app.state.bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    print(f"[INFO] Loading reranker: {rq.RERANK_MODEL}")
    app.state.rerank_model = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    # 依 QDRANT_URL 決定 server／local（見檔頭②）。不要改回寫死 path。
    app.state.client = rq.make_qdrant_client()
    try:
        _n = app.state.client.count(collection_name=collection, exact=True).count
    except Exception as e:
        # 開錯 Qdrant 是沉默故障（server 起得來、每題都答「沒有足夠資訊」）→ 啟動就吵。
        raise RuntimeError(
            f"collection '{collection}' 在這個 Qdrant 連線上不存在（{e!r}）。"
            f"檢查 .env 的 QDRANT_URL／QDRANT_PATH 與 COLLECTION_NAME 是否一致。") from e
    print(f"[INFO] Qdrant ready: collection={collection}  chunks={_n}")
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
    # retrieval_model / gen_model 各自可覆寫（對齊 CLI）；留空用後端預設。
    retrieval_model: str | None = None
    gen_model: str | None = None
    # 舊欄位：前端（app.py）只有單一「model」框 → 視為 gen_model 覆寫（最貼近使用者感知的
    # 「回答用哪個模型」）。retrieval_model 保持預設以維持與 eval 一致的檢索行為。
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
        "llm_backend": "gemini" if DEFAULT_GEN_MODEL.startswith("gemini-") else "nvidia",
        "retrieval_model": DEFAULT_RETRIEVAL_MODEL,
        "gen_model": DEFAULT_GEN_MODEL,
        # 舊欄位保留（前端 health 顯示）：對外仍以 gen_model 為「當前模型」。
        "model": DEFAULT_GEN_MODEL,
    }


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


@app.post("/chat")
async def chat(req: ChatRequest):
    # retrieval 側必須跟 eval 對齊（CLAUDE.md model_name 陷阱）→ 預設不受前端單一 model 框影響；
    # 只有明確帶 retrieval_model 才覆寫。gen 側可由 gen_model 或舊 model 欄位覆寫。
    retrieval_model = req.retrieval_model or DEFAULT_RETRIEVAL_MODEL
    gen_model       = req.gen_model or req.model or DEFAULT_GEN_MODEL

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
                        app.state.client, top_k=req.top_k, model_name=retrieval_model,
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

                # 檢索後句級抽取（生成前把相關句逐字抽出擺 chunk 前面；見 CHANGELOG 2026-07-14）。
                # 用 retrieval_model（檢索側，遵守 model_name 紀律）。放在 sources 之前，讓
                # sources 事件回報的也是含摘錄的 chunk。
                if rq.DEFAULT_ENABLE_COMPRESS:
                    chunks = await loop.run_in_executor(
                        None, lambda: rq.compress_chunks(req.query, chunks, retrieval_model))

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
                messages = [{"role": "system", "content": rq.SYSTEM_PROMPT + rq.ZH_ANSWER_DIRECTIVE}]
                messages.extend(req.history[-(rq.MAX_HISTORY * 2):])
                messages.append({"role": "user", "content": user_prompt})

                yield _sse("status", {"stage": "generating"})

                # 串流生成：把同步 generator 逐塊抽到 threadpool，避免阻塞 event loop
                gen = stream_llm(messages, gen_model)
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
