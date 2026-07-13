"""
app.py — Streamlit 聊天前端，串接 api_server.py 的 SSE 後端。

設計重點：這個前端「完全不碰 Qdrant、不 import rag_query」，純粹是 HTTP client，
徹底避開 Qdrant 的排他檔案鎖（模型與向量庫都在 FastAPI 那一端）。每次互動 Streamlit
會重跑整個 script，但因為這裡只持有 requests session，不受影響；對話歷史存在
st.session_state。

啟動：
  uvicorn api_server:app --port 8000   # 視窗 A：後端
  streamlit run app.py                  # 視窗 B：前端
"""

import json

import requests
import streamlit as st

st.set_page_config(page_title="US Stock RAG", page_icon="📈", layout="centered")

# ── Sidebar：設定 + 健康狀態 ─────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 設定")
    api_url = st.text_input("API base URL", value="http://localhost:8000")
    top_k   = st.slider("Top-k（送進 LLM 的引用數）", 1, 10, 5)
    model   = st.text_input("Model（留空用後端預設）", value="")

    st.divider()
    if st.button("🩺 檢查後端"):
        try:
            h = requests.get(f"{api_url}/health", timeout=10).json()
            if h.get("status") == "ok":
                st.success(
                    f"✅ collection=`{h['collection']}`，{h['count']} chunks\n\n"
                    f"LLM：{h['llm_backend']} / `{h['model']}`"
                )
            else:
                st.error(f"後端有問題：{h.get('status')}")
        except Exception as e:
            st.error(f"連不上後端：{e}")

    if st.button("🧹 清除對話"):
        st.session_state.messages = []
        st.rerun()

st.title("📈 US Stock Intelligence RAG")
st.caption("NVDA · MSFT · AAPL — Hybrid 檢索（Dense+Sparse → RRF → Rerank）+ 引用生成")

if "messages" not in st.session_state:
    st.session_state.messages = []   # [{"role","content","sources"(optional)}]


def _render_sources(chunks: list[dict]) -> None:
    with st.expander(f"📚 引用來源（{len(chunks)} 筆）"):
        for i, c in enumerate(chunks, start=1):
            st.markdown(
                f"**[{i}] `{c['source']}`** · chunk #{c['chunk_index']} · "
                f"`{c['source_type']}`  \n"
                f"rrf={c['rrf_score']:.6f} · rerank={c['rerank_score']:.4f}"
            )
            st.caption(c["preview"])


def _stream_chat(query: str):
    """POST /chat 並逐行解析 SSE。回傳 generator：先 yield ('sources', chunks)，
    再 yield ('status', stage) 與 ('token', text)，結束時 yield ('done', None)；
    出錯 yield ('error', message)。"""
    # 只送純文字 history 給後端（不含 sources 欄位）
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages
    ]
    payload = {"query": query, "history": history, "top_k": top_k}
    if model.strip():
        payload["model"] = model.strip()

    resp = requests.post(
        f"{api_url}/chat", json=payload, stream=True,
        headers={"Accept": "text/event-stream"}, timeout=900,
    )
    resp.raise_for_status()

    event = None
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None or raw == "":
            event = None
            continue
        if raw.startswith("event:"):
            event = raw[len("event:"):].strip()
        elif raw.startswith("data:"):
            data = json.loads(raw[len("data:"):].strip())
            yield event, data


# ── 重播歷史 ────────────────────────────────────────────────────────────────────
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("sources"):
            _render_sources(msg["sources"])


# ── 主輸入 ──────────────────────────────────────────────────────────────────────
if query := st.chat_input("問點什麼，例如：NVDA 最新一季毛利率是多少？"):
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        status_box = st.status("⏳ 準備中…", expanded=False)
        sources_holder = st.empty()
        answer_holder  = st.empty()

        sources: list[dict] = []
        answer = ""
        error  = None
        stage_label = {"retrieving": "🔍 檢索中…", "reranking": "🎯 重排中…",
                       "generating": "🤖 生成中…"}

        try:
            for event, data in _stream_chat(query):
                if event == "status":
                    status_box.update(label=stage_label.get(data["stage"], data["stage"]))
                elif event == "sources":
                    sources = data["chunks"]
                elif event == "token":
                    answer += data["text"]
                    answer_holder.markdown(answer + "▌")
                elif event == "error":
                    error = data["message"]
                    break
                elif event == "done":
                    break
        except Exception as e:
            error = repr(e)

        if error:
            status_box.update(label="❌ 發生錯誤", state="error")
            answer_holder.error(f"後端錯誤：{error}")
            answer = answer or f"(錯誤：{error})"
        else:
            status_box.update(label="✅ 完成", state="complete")
            answer_holder.markdown(answer)
            if sources:
                with sources_holder.container():
                    _render_sources(sources)

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "sources": sources}
    )
