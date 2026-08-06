"""
agentic_rag_v2.py — Supervisor + Subagent + RAG-as-Tool 版 agentic RAG（2026-07-26 重構）。

與 agentic_rag_nv.py 的差異：nv 版是六節點固定 state machine（檢索是節點內的純函式呼叫）。本檔把它改成
「Planner（Supervisor）→ Executor（兩個 agent）→ Replanner → loop → Synthesize」的多智能體編排，
核心變化是 **RAG 檢索變成一個 tool，讓 subagent 主動呼叫**（`rag_search`），並新增 `web_search`（Tavily）。

架構（顯式 LangGraph StateGraph，四個主節點；待辦清單是 state 的一等公民）：
  plan       ── Supervisor/Planner：一次 LLM 呼叫把問題拆成待辦清單 todos=[{id,task,status,result}]。
                「屬於 agent 系統（規劃拆解），但實作上是單次 LLM 呼叫、不是完整工具迴圈 agent」。
  execute    ── Executor = 兩個 agent 的團隊，處理「目前這一波」所有 pending 待辦（2026-07-31 改為
                分波 pipeline，見 _node_execute）：同一波內彼此獨立的子問題用 thread pool 重疊執行
                ——retrieve 仍靠 _RETRIEVE_LOCK 序列化，grade/生成這類 LLM API 呼叫才真的重疊。整波
                做完才 replan 一次（取代舊版每個 todo 各自 replan）；next-hop 待辦是下一輪 replan 才
                新增，天然落在下一波，跟這一波獨立 todos 不需要額外相依標記去分辨。單一子問題內部：
                · Agent A｜Researcher+Generator：真正的 ReAct agent（langgraph create_react_agent），
                  工具 = [rag_search, web_search]，動態決定搜尋策略、看 rerank 分數決定續搜/改關鍵字/上網。
                  **system prompt 鎖定**：收到「評估已通過」前只用工具回報事實、絕不自行生成摘要。
                · Agent B｜Grader：不是 tool-loop agent，是 Evaluator/Reflection（單次 LLM、無工具，
                  複用 _check_sufficiency），審查目前檢索池 → {sufficient, missing, new_query, relevant_ids}。
                  relevant_ids（2026-07-30 新增）：Grader 從看到的候選裡明確圈選「真正相關」的 id，
                  commit 進 collected 時優先用這份圈選結果過濾，不再是「rerank 前 k 名就全收」（見
                  [[multi-intent-agentA-is-the-leak]] 殘留雜訊案例：不相關公司的 chunk 分數夠高就混進
                  citation）。Grader 沒給圈選（欄位空/解析失敗）→ 退回舊行為，不誤濾成 0 筆。
                內部控制流（見 _run_executor）：A 純檢索 → Grader 評分 → 不夠就餵糾正訊息續搜（有界
                MAX_REWRITES）；夠了才發「解除限制、生成摘要」觸發訊息 → A 產出局部摘要 → 跳出。
  replan     ── Replanner：一次 LLM 呼叫，依已完成待辦的局部結果動態更新清單——KB 查無新聞 → 新增
                web-search 待辦；證據已足 → 提前收斂（把剩餘 pending 標 dropped）；多餘待辦 → drop。
  synthesize ── Joiner：無 agency。把跨待辦收集的 collected 全文（+ web_notes 另標）走生產生成契約
                （rq.SYSTEM_PROMPT + build_user_prompt）單次生成，過確定性 citation validator + 選配
                Reflection 幻覺稽核，最後機械式附上真實 citation 清單。

安全網哲學（沿用 nv/舊 deepagents 版的血淚教訓）：agent 的自主性只放在「工具怎麼用、清單怎麼改」；
「最終答案怎麼寫」由 Python 強制走生成契約 + validator，不交給 agent 自由發揮（避免 citation 遺失/幻覺）。
subagent 崩潰（gpt-oss-120b 的 harmony token 洩漏等）→ 降級成單次檢索+生成，保證非零產出。

與 rag_query.py 的關係：以 `import rag_query as rq` 複用 retrieve / build_user_prompt / SYSTEM_PROMPT /
call_llm / 重模型建構。無 tool-call 的呼叫（planner/grader/replanner/synthesize/reflect）走 rq.call_llm
（`_nvidia_call_llm` monkeypatch → NVIDIA）；有 tool-call 的 Agent A 用 langchain_openai.ChatOpenAI 指向
同一 NVIDIA endpoint。

⚠ 硬性約束（沿用舊版，仍成立）：
  1. 重模型（bge_m3/rerank_model/client）整個 process 只建一次、共享；local Qdrant 單寫者鎖。
  2. retrieve 用 _RETRIEVE_LOCK 序列化（重模型非執行緒安全）；run-scoped 檢索池走 contextvars（見
     _RunState/_reset_run_pool），子問題之間天生互相隔離，execute 每個待辦前換上全新一份。
  3. retrieve 內部 filter/translate 的 model_name 用 RETRIEVAL_MODEL。
  4. retrieve / subagent 會 print 大量雜訊 → redirect_stdout 靜音。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import TypedDict

from dotenv import load_dotenv

load_dotenv(override=True)

import rag_query as rq

from langgraph.graph import StateGraph, START, END

# ──────────────────────────────────────────────────────────────────────────────
# NVIDIA 版：把 rq.call_llm 換成走 NVIDIA build.nvidia.com（單一 OpenAI-compatible endpoint、
# 同一把 NVIDIA_API_KEY），取代 Groq。rq 內部（rewrite_query/translate_query_to_english 等）以裸名
# 呼叫 call_llm，重綁 rq.call_llm 會透過共用的 module __dict__ 一併生效，不需逐一改內部呼叫點。
# ──────────────────────────────────────────────────────────────────────────────
_NVIDIA_BASE_URL_RQ = "https://integrate.api.nvidia.com/v1"
_orig_call_llm = rq.call_llm


# 429/5xx 指數退避（fix 2026-07-31）：實測長跑會把 NVIDIA NIM 配額燒到 429 Too Many Requests，
# 原本裸呼叫無重試 → 一撞就永久失敗、退機械傾印（見 eval 56/100 fallback 事故）。這裡對「暫時性」
# 錯誤（429 限速 + 500/502/503/504 + 連線/逾時）做有界指數退避；honor Retry-After header；重試用盡
# 才 raise，讓上層既有 fallback 當最後保底。注意：若是「配額整個耗盡」而非瞬時 RPM 尖峰，退避只能
# 撐過短暫尖峰，救不了長時間見底——那種情況要靠降併發 / 降呼叫量 / 等配額重置。
_LLM_MAX_RETRIES   = int(os.getenv("AGENTIC_LLM_MAX_RETRIES", "5"))
_LLM_BACKOFF_BASE  = float(os.getenv("AGENTIC_LLM_BACKOFF_BASE", "2.0"))   # 秒；2,4,8,16,32...
_LLM_BACKOFF_CAP   = float(os.getenv("AGENTIC_LLM_BACKOFF_CAP", "30.0"))   # 單次退避上限


def _retry_after_seconds(exc) -> float | None:
    """從例外的 response header 取 Retry-After（秒）；取不到回 None。"""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    val = headers.get("retry-after") or headers.get("Retry-After")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _nvidia_call_llm(messages: list[dict], model_name: str, temperature: float = 0.0) -> str:
    """取代 rq.call_llm：gemini- 開頭仍走原本的 Gemini 路徑，其餘一律走 NVIDIA（而非 Groq）。
    對暫時性錯誤（429 / 5xx / 連線 / 逾時）做有界指數退避重試（見上方註解）。"""
    if model_name.startswith("gemini-"):
        return _orig_call_llm(messages, model_name, temperature)
    import random
    import openai
    nv_key = os.getenv("NVIDIA_API_KEY")
    if not nv_key:
        raise RuntimeError("agentic_rag_nv 需要 NVIDIA_API_KEY，但環境變數裡沒有。")
    client = openai.OpenAI(api_key=nv_key, base_url=_NVIDIA_BASE_URL_RQ)
    _RETRYABLE = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError)
    last_exc = None
    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model_name, messages=messages, temperature=temperature)
            return resp.choices[0].message.content
        except _RETRYABLE as e:
            # RateLimitError(429) 是 APIStatusError 的子類，必須排在下面 APIStatusError 之前先攔，
            # 否則會被當成「非 5xx status error」立刻 re-raise（實測踩過）。連線/逾時也在此重試。
            last_exc = e
        except openai.APIStatusError as e:
            # 其餘 status error：只重試暫時性 5xx；4xx（請求本身的問題）重試無益，直接 raise。
            if e.status_code not in (500, 502, 503, 504):
                raise
            last_exc = e
        if attempt >= _LLM_MAX_RETRIES:
            break
        # 優先 honor Retry-After，否則指數退避 + jitter，避免 thread pool 多 worker 同步重試撞同一波。
        delay = _retry_after_seconds(last_exc)
        if delay is None:
            delay = min(_LLM_BACKOFF_BASE * (2 ** attempt), _LLM_BACKOFF_CAP)
        delay += random.uniform(0, delay * 0.25)
        _trace(f"LLM {type(last_exc).__name__} → 退避重試 {attempt + 1}/{_LLM_MAX_RETRIES}，等 {delay:.1f}s")
        time.sleep(delay)
    raise last_exc


rq.call_llm = _nvidia_call_llm

# ──────────────────────────────────────────────────────────────────────────────
# 常數 / 模型選型（NVIDIA 目錄 id，實測見舊版 CHANGELOG 續八）
# ──────────────────────────────────────────────────────────────────────────────
# RETRIEVAL_MODEL：只做 retrieve 內部 filter/translate 這類「機械型」輕量呼叫（求快），
# 與 CHECKER_MODEL（推理）、GEN_MODEL（生成）分離。這類簡單工作直接用小模型 gpt-oss-20b
# （同 gpt-oss 家族，NVIDIA 上與 120b 同樣快且合法、JSON 輸出穩）。
RETRIEVAL_MODEL = os.getenv("AGENTIC_RETRIEVAL_MODEL", "openai/gpt-oss-20b")
# GEN_MODEL：實際寫答案的 Generator。2026-07-31 定案用 gpt-oss-120b，理由是「限速」這一維：
#   glm-5.2            單發 150~220s（NIM 上被拖），100 題不可行
#   deepseek-v4-pro    品質最好但有很低的單模型限速，eval 密集打立刻 429（實測 34 題就撞牆）
#   gpt-oss-120b       1~8s、且高負載下仍不被限速（NVIDIA 主推開放模型、額度寬）→ 唯一能撐 eval 量的
# 代價：gpt-oss-120b 偶爾吐全形【】引用，靠下游 _validate_and_fix_citations 校正（已端到端驗證）。
# 與 CHECKER_MODEL 同模型無妨（不同 prompt、不同任務）。benchmark 見 experiments/agentic/_bench_gen.txt。
GEN_MODEL       = os.getenv("AGENTIC_GEN_MODEL", "openai/gpt-oss-120b")
# CHECKER_MODEL：planner 拆解 / sufficiency 判斷 / reflection 幻覺稽核共用（要結構化 JSON 可靠 + 快）。
# gpt-oss-120b：tool-call/結構化輸出快又合法、無下架風險。
CHECKER_MODEL   = os.getenv("AGENTIC_CHECKER_MODEL", "openai/gpt-oss-120b")

RERANK_GATE_TAU   = 0.5   # rerank_score(sigmoid) 門檻：僅供 trace/參考；夠不夠的最終判斷交給 Checker。
MAX_REWRITES      = 2     # 每個子問題的補救改寫上限（防迴圈 / 省 token）。
MAX_SUBQUERIES    = 7     # planner 拆解上限。設 7 讓 Magnificent Seven 等具名集合能逐一列滿(5 會被迫
                          #   丟掉 2 家,news-13 就是這樣漏掉 NVDA);超過 7 的集合走 prompt 的「寬鬆 fallback」。
POOL_RETURN_K     = 5     # 餵給 Checker 看的候選片段數（top-k by rerank）。
COMMIT_TOP_K      = rq.DEFAULT_TOP_K   # 每個子問題 advance 時收進 collected 的 top-k。
WRITER_MAX_CHUNKS = 8     # 單發/fallback 路徑用的固定 chunk 上限（防 TPM 爆）；synthesize 改走自適應預算。
# ── 自適應生成預算（fix 2026-07-29，見 [[multi-intent-agentA-is-the-leak]]）────────
# 固定 8-cap 對多 facet 題會 8÷N 稀釋（chunk 層級 probe：軸A 佔 v2 漏失 23/40）。改成隨 facet 數放大，
# 單意圖題維持 ~base（不傷 lexical/colloquial 的 faithfulness），多 facet 才給更多名額（而非砍 facet）。
#   budget(n) = min(BASE + PER_FACET*(n-1), CAP)   例：1→5, 2→7, 3→9, 4→11, 6→15（CAP=16）
WRITER_BUDGET_BASE      = int(os.getenv("AGENTIC_WRITER_BUDGET_BASE", "5"))
WRITER_BUDGET_PER_FACET = int(os.getenv("AGENTIC_WRITER_BUDGET_PER_FACET", "2"))
WRITER_BUDGET_CAP       = int(os.getenv("AGENTIC_WRITER_BUDGET_CAP", "16"))
# ── 執行層開關（fix 2026-07-29）──────────────────────────────────────────────────
# 預設 deterministic：planner 子問題「原封不動」直接檢索，不讓 Agent A(ReAct) 自行改寫 query。
# chunk 層級 probe 證實 ReAct 執行層是 multi_intent context_recall 漏水主因（真實 v2 漏掉、單管線撈到的
# gold chunk 中 12/15 是乾淨 decomposition 撈得到的——差別只在有沒有 Agent A 亂改 query）。
# 設 AGENTIC_REACT_EXECUTOR=true 回到舊 ReAct 行為（A/B 對照用）。
USE_REACT_EXECUTOR = os.getenv("AGENTIC_REACT_EXECUTOR", "false").lower() in ("true", "1", "yes")

# ── Supervisor / Subagent 架構專屬常數 ────────────────────────────────────────
MAX_ITERS      = int(os.getenv("AGENTIC_MAX_ITERS", "8"))   # 總「子問題執行次數」上限（防 replanner 無限加待辦）。
MAX_TODOS      = MAX_SUBQUERIES   # 待辦清單總數上限（沿用 7：Magnificent Seven 逐一列滿 + replan 新增後仍守此上限）。
# 2026-07-31：execute 節點改成一次處理「一整波」pending 待辦（見 _node_execute），同一波內彼此獨立
# 的子問題用 thread pool 重疊執行——retrieve 仍靠 _RETRIEVE_LOCK 天然序列化，grade/生成這類 LLM API
# 呼叫才是真的重疊（pipeline，不是無腦全平行）。worker 數不需要很大：retrieve 本來就會在鎖上排隊，
# 多開只是讓更多子問題的「等 LLM 回應」那段時間疊在一起，2~3 個足夠打滿這個疊法的效益。
# ⚠ 預設調回 2（fix 2026-07-31）：實測 workers=3 的併發尖峰會加速把 NVIDIA NIM 打到 429（配額/RPM），
# 生成大量退機械 fallback（見 eval 56/100 事故）。搭配 _nvidia_call_llm 的退避重試，2 個 worker 兼顧
# 疊 LLM 等待與不打爆限速；配額充足時可再手動調高。
AGENTIC_PIPELINE_WORKERS = int(os.getenv("AGENTIC_PIPELINE_WORKERS", "2"))
# Agent A（Researcher+Generator，真正的 ReAct agent）用的 chat model；預設同 GEN_MODEL（中文生成流暢）。
# ⚠ 若設 gpt-oss-120b，即 CHANGELOG 續九的 tool-call harmony 洩漏來源——崩潰時 execute 有降級保證。
SUBAGENT_MODEL = os.getenv("AGENTIC_SUBAGENT_MODEL", GEN_MODEL)
SUBAGENT_RECURSION_LIMIT = int(os.getenv("AGENTIC_SUBAGENT_RECURSION", "12"))  # 單一 subagent invoke 的工具迴圈上限。
# web_search（Tavily）開關；--no-web 或此環境變數關掉。TAVILY_API_KEY 由 .env 提供，缺 key 走 graceful 降級。
ENABLE_WEB_SEARCH = os.getenv("AGENTIC_WEB_SEARCH", "true").lower() not in ("false", "0", "no")
TAVILY_MAX_RESULTS = 5
# web_search 每個待辦的硬性呼叫上限（工程層閘門，不只靠 prompt 服從性——實測弱腦會對「最近新聞」題
# 瞎猜關鍵字狂打 33 次 Tavily 仍一次都沒採用）。超過上限直接回拒、不再真的打 API。
WEB_SEARCH_MAX_CALLS = int(os.getenv("AGENTIC_WEB_SEARCH_MAX_CALLS", "3"))
# rag_search 每個待辦的硬性呼叫上限（工程層閘門）。實測 glm-5.2 會對新聞題失控搜 40+ 次、top rerank
# 早在第 4 次就到頂、後面全是白搜，卻因每次 tool call ~30s 把單題拖到 ~50 分。超過上限的 rag_search
# 直接回「停止搜尋、用已檢索內容作答」，不再真的檢索。COMMIT_TOP_K=5，4 次搜尋（每次寬召回 20+ 候選、
# 去重併池）已足以涵蓋 top-5，不傷品質。
RAG_SEARCH_MAX_CALLS = int(os.getenv("AGENTIC_RAG_SEARCH_MAX_CALLS", "4"))

# 「最近／最新」有兩種不同語意：
#   live     = 截至真實今天；KB 落後且 web 無法補齊時要揭露 cutoff。
#   snapshot = 封閉語料評測；「最新」只表示 collection 中最新可用資料，不拿 wall clock 製造缺口。
FRESHNESS_LIVE = "live"
FRESHNESS_SNAPSHOT = "snapshot"
FRESHNESS_MODES = {FRESHNESS_LIVE, FRESHNESS_SNAPSHOT}

# 預設是否開 Reflection（LLM 幻覺稽核）。eval 端 --no-validator 會把它關掉（沿用舊 CLI 參數名）。
ENABLE_REFLECTION = os.getenv("AGENTIC_REFLECTION", "true").lower() not in ("false", "0", "no")

CITATION_VALIDATOR_MAX_RETRIES = 1  # citation 不合格時丟回 Generator 重寫的上限。
REFLECTION_MAX_REVISIONS       = 1  # reflection 抓到幻覺後重生成的上限。

# 生產 citation 格式 [filename, chunk #N]：確定性 validator 用它抽引用比對 allowlist。
# 同時吃全形括號【】與全形逗號，：（gpt-oss-120b 生成中文會把 [] 轉全形，只認 ASCII 會 false-negative）。
_CITE_RE = re.compile(r"[\[【]([^\[\]【】,，]+?)[,，]\s*chunk\s*#?\s*(\d+)[\]】]", re.IGNORECASE)
_REF_PREFIX_RE = re.compile(r"^\s*reference\s*\d+\s*:\s*", re.IGNORECASE)

# [TRACE]：把每個節點的當下決策印到 stderr（retrieve 的 DEBUG 已被 _quiet() 靜音）。
_TRACE = os.getenv("AGENTIC_TRACE", "false").lower() in ("true", "1", "yes")


def _trace(msg: str) -> None:
    if _TRACE:
        import sys
        print(f"[TRACE] {msg}", file=sys.stderr, flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# 執行緒安全的「靜音 print()」：取代 contextlib.redirect_stdout(io.StringIO())。
# retrieve/call_llm 底層（sentence-transformers、tqdm…）會印大量雜訊，全檔一律靠這個抑制。
# ⚠ 不能繼續用 contextlib.redirect_stdout：它是整個 process 共用同一個 sys.stdout 做存/還原，
# 子問題 pipeline 平行執行（多執行緒同時各自 with redirect_stdout(...)）會互相踩存/還原的時機——
# 最壞情況是某個執行緒的 throwaway StringIO 被錯誤地「還原」成全 process 的 sys.stdout，之後
# 整個程式永久印不出任何東西（不是單純雜訊，是真的會壞掉）。改用 thread-local 靜音旗標：
# sys.stdout 只包一次，各執行緒各自控制「自己」要不要被吃掉，互不影響。
# ──────────────────────────────────────────────────────────────────────────────

class _ThreadLocalMuteStream:
    """包一層 sys.stdout：write() 先看目前這個執行緒有沒有被靜音，靜音就丟掉，否則照樣印。"""

    def __init__(self, real_stream) -> None:
        self._real = real_stream
        self._local = threading.local()

    def _muted(self) -> bool:
        return getattr(self._local, "muted", False)

    def write(self, s: str) -> int:
        if self._muted():
            return len(s)
        return self._real.write(s)

    def flush(self) -> None:
        self._real.flush()

    def isatty(self) -> bool:
        return False

    def set_muted(self, muted: bool) -> None:
        self._local.muted = muted


_stdout_mute: _ThreadLocalMuteStream | None = None


@contextlib.contextmanager
def _quiet():
    """取代裸的 contextlib.redirect_stdout(io.StringIO())：靜音範圍只限「呼叫這段的那個執行緒」，
    跨執行緒平行呼叫彼此不干擾，也不會有 sys.stdout 被永久錯誤還原的風險。"""
    global _stdout_mute
    import sys as _sys
    if _stdout_mute is None or _sys.stdout is not _stdout_mute:
        _stdout_mute = _ThreadLocalMuteStream(_sys.stdout)
        _sys.stdout = _stdout_mute
    _stdout_mute.set_muted(True)
    try:
        yield
    finally:
        _stdout_mute.set_muted(False)


# ──────────────────────────────────────────────────────────────────────────────
# 共享重模型（整個 process 只建一次）+ 檢索鎖
# ──────────────────────────────────────────────────────────────────────────────

_bge_m3 = None
_rerank_model = None
_client = None
_models_lock = threading.Lock()
_RETRIEVE_LOCK = threading.Lock()   # retrieve 序列化（重模型非執行緒安全 + Qdrant 單寫）

# KB coverage 只依「Agent 實際連到的 Qdrant collection」計算；不能掃 data/ 目錄，因為檔案存在
# 不代表已 ingest。cache 以 collection name 為界，eval 切 collection 時會自動重算。
_kb_coverage: dict | None = None
_kb_coverage_collection: str | None = None
_coverage_lock = threading.Lock()


def _get_models():
    """Lazy 建構 bge_m3 / rerank_model / client（mirror rag_query.main 的建構樣板），整個 process 共享。"""
    global _bge_m3, _rerank_model, _client
    if _client is not None:
        return _bge_m3, _rerank_model, _client
    with _models_lock:
        if _client is not None:
            return _bge_m3, _rerank_model, _client
        from FlagEmbedding import BGEM3FlagModel
        from sentence_transformers import CrossEncoder
        print("[agentic_rag] Loading BGE-M3 + reranker + Qdrant (one-time)...")
        _bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
        _rerank_model = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
        _client = rq.make_qdrant_client()
    return _bge_m3, _rerank_model, _client


# ──────────────────────────────────────────────────────────────────────────────
# 時間 grounding：wall clock 與 KB snapshot 是兩條獨立時間軸。
# Coverage 必須掃「實際 collection」而不是 data/；scroll 只取少量 payload、不取 vector/document。
# ──────────────────────────────────────────────────────────────────────────────

_COVERAGE_SOURCE_RE = re.compile(
    r"^(?P<ticker>[A-Z]+)_(?P<kind>10K|10Q|Fundamentals|News)_"
    r"(?P<stamp>\d{4,8})(?:_|\.|$)",
    re.IGNORECASE,
)
_RELATIVE_TIME_RE = re.compile(
    r"最近|最新|近期|目前|現在|截至今天|今日|"
    r"\b(?:latest|recent|current|today|as\s+of\s+now)\b",
    re.IGNORECASE,
)
_WEB_TODO_RE = re.compile(r"網路|上網|web\s*search|internet", re.IGNORECASE)


def _get_as_of_date() -> date:
    """live 模式的『今天』。AGENTIC_AS_OF_DATE 讓 eval/回歸測試可固定 wall clock。"""
    override = (os.getenv("AGENTIC_AS_OF_DATE") or "").strip()
    if override:
        return date.fromisoformat(override)
    return datetime.now().astimezone().date()


def _source_coverage_parts(source: str) -> tuple[str, str, str] | None:
    """從既有命名規則取 (ticker, kind, stamp)；payload 缺日期的 News/Fundamentals 靠這裡補。"""
    m = _COVERAGE_SOURCE_RE.match(source or "")
    if not m:
        return None
    kind_raw = m.group("kind").lower()
    kind = {"10q": "10-Q", "10k": "10-K",
            "fundamentals": "fundamentals", "news": "news"}[kind_raw]
    return m.group("ticker").upper(), kind, m.group("stamp")


def _coverage_sort_key(record: dict) -> str:
    """同一 doc_type 內用 YYYY / YYYYMM / YYYYMMDD 字串排序；右補 0 讓長度一致。"""
    stamp = str(record.get("report_period_code") or record.get("date")
                or record.get("fiscal_year") or "")
    digits = re.sub(r"\D", "", stamp)
    return digits.ljust(8, "0")


def _scan_kb_coverage(client, collection_name: str) -> dict:
    """掃 collection 的唯一 source，計算每個 ticker、每種文件類型的最新實際資料。"""
    coverage = {
        "available": False,
        "collection": collection_name,
        "sources_scanned": 0,
        "tickers": {},
    }
    seen_sources: set[str] = set()
    offset = None
    try:
        while True:
            points, offset = client.scroll(
                collection_name=collection_name,
                limit=256,
                offset=offset,
                with_payload=[
                    "ticker", "doc_type", "filing_type", "fiscal_year",
                    "fiscal_period", "report_period_code", "source",
                ],
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                source = str(payload.get("source") or "").strip()
                if not source or source in seen_sources:
                    continue
                seen_sources.add(source)

                source_parts = _source_coverage_parts(source)
                ticker = str(payload.get("ticker") or
                             (source_parts[0] if source_parts else "")).upper().strip()
                if not ticker:
                    continue

                filing_type = str(payload.get("filing_type") or "").upper()
                doc_type = str(payload.get("doc_type") or "").lower()
                if filing_type in ("10-Q", "10-K"):
                    kind = filing_type
                elif doc_type in ("10-q", "10-k"):
                    kind = doc_type.upper()
                elif doc_type in ("news", "fundamentals"):
                    kind = doc_type
                elif source_parts:
                    kind = source_parts[1]
                else:
                    continue

                stamp_from_source = source_parts[2] if source_parts else ""
                record = {"source": source}
                if kind in ("10-Q", "10-K"):
                    record.update({
                        "report_period_code": str(payload.get("report_period_code")
                                                  or stamp_from_source or ""),
                        "fiscal_year": str(payload.get("fiscal_year") or ""),
                        "fiscal_period": str(payload.get("fiscal_period") or ""),
                    })
                else:
                    record["date"] = stamp_from_source

                if not _coverage_sort_key(record).strip("0"):
                    continue
                ticker_cov = coverage["tickers"].setdefault(ticker, {})
                old = ticker_cov.get(kind)
                if old is None or _coverage_sort_key(record) > _coverage_sort_key(old):
                    ticker_cov[kind] = record

            if offset is None:
                break
        coverage["available"] = True
        coverage["sources_scanned"] = len(seen_sources)
    except Exception as e:
        coverage["error"] = repr(e)
        _trace(f"coverage: 掃描 collection={collection_name!r} 失敗 → {e!r}")
    return coverage


def _get_kb_coverage() -> dict:
    """Lazy coverage cache；rq.COLLECTION_NAME 改變時重算，避免 eval 誤用生產 snapshot。"""
    global _kb_coverage, _kb_coverage_collection
    collection = rq.COLLECTION_NAME
    if _kb_coverage is not None and _kb_coverage_collection == collection:
        return _kb_coverage
    with _coverage_lock:
        if _kb_coverage is not None and _kb_coverage_collection == collection:
            return _kb_coverage
        _bge, _rerank, client = _get_models()
        _kb_coverage = _scan_kb_coverage(client, collection)
        _kb_coverage_collection = collection
        _trace(f"coverage: collection={collection!r}, "
               f"sources={_kb_coverage.get('sources_scanned', 0)}, "
               f"tickers={sorted(_kb_coverage.get('tickers', {}))}")
        return _kb_coverage


def _display_date(stamp: str) -> str:
    digits = re.sub(r"\D", "", str(stamp or ""))
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return digits or "unknown"


def _coverage_record_text(kind: str, record: dict) -> str:
    if kind in ("10-Q", "10-K"):
        period = record.get("report_period_code") or record.get("fiscal_year") or "unknown"
        fiscal_period = record.get("fiscal_period") or ""
        if fiscal_period.upper() == "FY":
            fiscal_period = ""
        fiscal = " ".join(x for x in (
            f"FY{record.get('fiscal_year')}" if record.get("fiscal_year") else "",
            fiscal_period,
        ) if x)
        label = f"{kind} period {period}"
        if fiscal:
            label += f" ({fiscal})"
        return label
    return f"{kind} through {_display_date(record.get('date', ''))}"


def _format_kb_coverage(coverage: dict, tickers: set[str] | None = None) -> str:
    if not coverage.get("available"):
        return "KB Coverage Snapshot unavailable（不得因此猜測任何期間）。"
    all_tickers = coverage.get("tickers", {})
    names = sorted(tickers if tickers else all_tickers)
    lines = [f"KB Coverage Snapshot（collection={coverage.get('collection')}）:"]
    for ticker in names:
        kinds = all_tickers.get(ticker, {})
        if not kinds:
            continue
        parts = [_coverage_record_text(kind, kinds[kind])
                 for kind in ("10-Q", "10-K", "fundamentals", "news") if kind in kinds]
        lines.append(f"- {ticker}: " + "; ".join(parts))
    if len(lines) == 1:
        lines.append("- 查無對應 ticker 的 coverage；不得猜測期間。")
    return "\n".join(lines)


def _mentioned_tickers(text: str) -> set[str]:
    """找出 task 中所有已知公司；不用 rq._detect_ticker，因為它只回第一個。"""
    q = text or ""
    q_lower = q.lower()
    found: set[str] = set()
    for alias, ticker in getattr(rq, "_COMPANY_TICKER", {}).items():
        if re.search(r"[一-鿿]", alias):
            if alias in q:
                found.add(ticker)
        elif re.search(r"\b" + re.escape(alias) + r"\b", q_lower):
            found.add(ticker)
    return found


# ── ratio 題財務錨源保底（doc_type 版的 _ensure_ticker_coverage）─────────────────────
# 病灶（見 trace/probe）：毛利率/淨利率/成長率等「可從 10-K/10-Q 原始行自行計算」的指標，池中
#   同時有 ① Fundamentals（預算好的 TTM 比率，寫死值，rerank rank0）② 10-K/10-Q（原始行，可現算）
#   兩個近似平手來源。Grader 一次只圈 1 個 → 在兩來源間擲硬幣，時而圈中 10-Q（季度口徑、與 gold 的
#   Fundamentals 錨點不一致）。市值/P/E/ROE 等「單一來源」指標不受影響（想挑錯都沒得挑），故 recall 高。
# 修法：ratio 意圖時，若 commit 集合裡沒有該公司的 Fundamentals，就從池中把它補回（純確定性、零 LLM、
#   只加不減）——Grader 的語意判斷照跑，這層只接住「圈錯來源」的壞 run，消掉 run 間變異。
_RATIO_INTENT_RE = re.compile(
    r"毛利率|淨利率|净利率|利潤率|利润率|營業利益率|营业利益率|營業利潤率|獲利率|获利率|"
    r"淨利潤率|净利润率|營收成長|营收成长|成長率|成长率|營收增長|营收增长|"
    r"gross\s*margin|net\s*margin|operating\s*margin|profit\s*margin|\bmargin\b|growth\s*rate",
    re.IGNORECASE,
)


def _is_ratio_intent(task: str) -> bool:
    """子問題是否問「可從 10-K/10-Q 現算、故有雙來源」的比率/成長率指標（毛利率/淨利率/成長率…）。
    這類指標 Fundamentals 已預算好寫死值，應以 Fundamentals 為錨、繞開 10-Q 現算路徑（路由非算術）。
    單一來源指標（市值/P/E/ROE/EPS/自由現金流）不觸發：無雙來源擲硬幣風險，補了也只是多餘。"""
    return bool(_RATIO_INTENT_RE.search(task or ""))


def _ensure_ratio_source_coverage(ranked: list[dict], selected: list[dict], want_tickers) -> list[dict]:
    """ratio 題財務錨源保底：保證 want_tickers 每一家在 selected 裡至少有一個 Fundamentals chunk；
    缺的就從 ranked（完整 rerank 降序池）補上該家分數最高的 Fundamentals，回傳仍按 rerank 降序。
    照 rq._ensure_ticker_coverage 的結構（只加不減）。零/未知 ticker 時原樣回傳。
    Fundamentals 靠 source 檔名判定（payload doc_type 實測常為空，見 probe）。"""
    want = [want_tickers] if isinstance(want_tickers, str) else list(want_tickers or [])
    if not want:
        return selected
    is_fund = lambda c: "Fundamentals" in (c.get("source") or "")
    covered = {c.get("ticker", "") for c in selected if is_fund(c)}
    missing = set(want) - covered
    if not missing:
        return selected
    out = list(selected)
    seen = {(c["source"], c["chunk_index"]) for c in selected}
    for c in ranked:                       # ranked 已降序：每家第一個 Fundamentals 命中即該家最高分
        if not missing:
            break
        if not is_fund(c):
            continue
        t = c.get("ticker", "")
        key = (c["source"], c["chunk_index"])
        if t in missing and key not in seen:
            out.append(c)
            seen.add(key)
            missing.discard(t)
    out.sort(key=lambda x: x["raw_rerank_score"], reverse=True)
    return out


# ── multi_hop 第二跳依賴解析（Type B：「新聞中做了 X 的是哪家？該公司的財報指標多少」）──────────
# planner 對這種兩跳題會把 hop-2 寫成「該公司的毛利率是多少」——帶未解回指代名詞、句中沒點名公司。
# 若讓它跟 hop-1 同一波平行檢索,「該公司」永遠沒被填 → 無 ticker 的破檢索撈回隨機 chunk。
# 解法(全確定性,不動 replanner LLM)：execute 分波時延後這種依賴型 hop,等 hop-1 辨識出公司、
# 把代名詞回填成具體公司名後,下一波才檢索。
_BACKREF_RE = re.compile(r"該公司|該企業|該家公司|這家公司|此公司|上述公司|前述公司|上述那家公司")
# ticker → 可被 _mentioned_tickers / retrieve 重新偵測的正規公司名（回填第二跳用）
_TICKER_CANON = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
                 "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta", "TSLA": "Tesla"}


def _is_dependent_hop(task: str) -> bool:
    """依賴型第二跳：帶未解回指代名詞(該公司…)且句中沒點名任何具體公司。這種子問題要等
    第一跳辨識出公司、回填實體後才能正確檢索。已含具體公司名 → 不算未解(planner 已自行填好)。"""
    if not _BACKREF_RE.search(task or ""):
        return False
    return not _mentioned_tickers(task)


def _resolve_hop_entity(todos: list[dict], collected: list[dict]) -> str | None:
    """從已完成待辦推出「第一跳辨識出的公司」正規名,供第二跳回填『該公司』。純確定性:
    先看非依賴型 done 待辦的局部結果文字命中的 ticker(眾數),再退回已 commit chunk 的 owner ticker。"""
    from collections import Counter
    cnt: Counter = Counter()
    for t in todos:
        if t.get("status") == "done" and not _is_dependent_hop(t.get("task", "")):
            res = t.get("result", "") or ""
            for tk in rq._find_all_ticker_aliases(res.lower(), res):
                cnt[tk] += 1
    if not cnt:   # summary 沒明確命中 → 退回 commit chunk 的 owner ticker
        for c in collected:
            tk = c.get("ticker") or (c.get("payload") or {}).get("ticker")
            if tk:
                cnt[tk] += 1
    if not cnt:
        return None
    top_ticker = cnt.most_common(1)[0][0]
    return _TICKER_CANON.get(top_ticker, top_ticker)


def _fill_dependent_hop(task: str, entity: str) -> str:
    """把第二跳子問題裡的『該公司…』回指代名詞換成解出的具體公司名。"""
    return _BACKREF_RE.sub(entity, task)


def _build_temporal_contract(freshness_mode: str) -> str:
    coverage_text = _format_kb_coverage(_get_kb_coverage())
    if freshness_mode == FRESHNESS_SNAPSHOT:
        policy = """時間模式：snapshot（封閉知識庫評測）。
- 「最近／最新」只表示下方 KB snapshot 中最新可用文件，不表示真實世界今天。
- 只能使用 snapshot 或工具結果明確出現的期間；絕不靠模型記憶推算季度、年份或月份。
- 不要因 KB 早於 wall clock 而新增 web 待辦或在答案加入 cutoff 警語。
- 使用者明確指定期間時，必須尊重原期間，不得換成 KB 最新期間。"""
    else:
        web_state = "可用" if ENABLE_WEB_SEARCH else "停用"
        policy = f"""時間模式：live。今天是 {_get_as_of_date().isoformat()}，web search 目前{web_state}。
- 「最近／最新」表示截至今天；今天與 KB cutoff 是兩條獨立時間軸，不得混為一談。
- 只能使用 snapshot 或工具結果明確出現的期間；絕不靠模型記憶推算季度、年份或月份。
- 若新聞問題要求截至今天且 KB cutoff 較舊，先查 KB，再只針對 cutoff 後的缺口決定是否用 web。
- 使用者明確指定期間時，必須尊重原期間，不得換成 KB 最新期間。"""
    return policy + "\n\n" + coverage_text


def _build_todo_temporal_scope(task: str, freshness_mode: str) -> tuple[str, list[dict]]:
    """為單一 todo 產生精簡 coverage 與可機械判定的 live-news freshness gap。"""
    coverage = _get_kb_coverage()
    mentioned = _mentioned_tickers(task)
    scope_coverage = _format_kb_coverage(coverage, mentioned or None)
    if freshness_mode == FRESHNESS_SNAPSHOT:
        return (
            "snapshot 模式：『最近／最新』= KB 中最新可用資料；不要參照 wall clock，"
            "不要加入 cutoff 警語。\n" + scope_coverage,
            [],
        )

    today = _get_as_of_date()
    gaps: list[dict] = []
    is_current_news = bool(_RELATIVE_TIME_RE.search(task or "")) and rq.looks_like_news_query(task)
    if is_current_news and coverage.get("available"):
        all_tickers = coverage.get("tickers", {})
        targets = mentioned or set(all_tickers)
        for ticker in sorted(targets):
            news = all_tickers.get(ticker, {}).get("news")
            digits = re.sub(r"\D", "", str((news or {}).get("date") or ""))
            if len(digits) != 8:
                continue
            try:
                cutoff = date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:]}")
            except ValueError:
                continue
            if cutoff < today:
                gaps.append({"ticker": ticker, "cutoff": cutoff.isoformat(),
                             "as_of": today.isoformat(), "doc_type": "news"})

    return (
        f"live 模式：今天是 {today.isoformat()}。不得把今天誤認成 KB cutoff；"
        "不得創造未出現在下方 snapshot 或工具結果中的期間。\n" + scope_coverage,
        gaps,
    )


def _is_web_todo(task: str) -> bool:
    return bool(_WEB_TODO_RE.search(task or ""))


def _format_unresolved_freshness_notice(todos: list[dict]) -> str:
    """只對 live 且 web 沒成功補到的新聞缺口產生機械式時效聲明；snapshot 永遠沒有 gap。"""
    unique: dict[tuple[str, str, str], dict] = {}
    for todo in todos or []:
        if todo.get("status") != "done" or todo.get("web_used"):
            continue
        for gap in todo.get("freshness_gaps", []) or []:
            key = (gap.get("ticker", ""), gap.get("cutoff", ""), gap.get("as_of", ""))
            unique[key] = gap
    if not unique:
        return ""
    details = "; ".join(
        f"{ticker} 新聞資料截至 {cutoff}（查詢日 {as_of}）"
        for ticker, cutoff, as_of in sorted(unique)
    )
    return ("\n\n---\n⚠ 資料時效：" + details
            + "；Web 未提供可用補充，因此以上新聞資訊不代表涵蓋至查詢日。")


# ──────────────────────────────────────────────────────────────────────────────
# 純函式工具（片段截取 / chunk id / 池合併 / JSON 容錯）
# ──────────────────────────────────────────────────────────────────────────────

_CJK_RUN_RE = re.compile(r"[㐀-鿿]+")


def _query_terms(query: str) -> set[str]:
    r"""把 query 拆成算 window 重疊分數用的 term 集合。
    英數：抓 token（len>1）。中文：\w+ 會把「無空格中文整句」視為單一 term——除非 chunk 逐字連續
    出現整句,否則 window score 恆 0、_snippet 退回前綴截斷（P0-3,子問題本就是純繁中,對每個 >400
    字的 chunk 都咬到）。改對每段連續 CJK 抽 2-gram 當比對單位（粗但可用,不引第三方分詞;單字段落
    才退成單字元）。"""
    q = (query or "").lower()
    terms = {w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 1}
    for run in _CJK_RUN_RE.findall(q):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def _best_window_start(text_lower: str, terms: set[str], n: int) -> tuple[int, int]:
    """掃 text_lower 找 n 字視窗裡 query 詞彙命中次數最高的起點。回傳 (start, score)。"""
    if len(text_lower) <= n:
        return 0, sum(text_lower.count(t) for t in terms)
    step = max(1, n // 4)
    best_start, best_score = 0, -1
    starts = list(range(0, len(text_lower) - n + 1, step))
    if starts[-1] != len(text_lower) - n:
        starts.append(len(text_lower) - n)
    for start in starts:
        window = text_lower[start:start + n]
        score = sum(window.count(t) for t in terms)
        if score > best_score:
            best_start, best_score = start, score
    return best_start, best_score


def _snippet(text: str, query: str = "", n: int = 1200) -> str:
    """query-aware 截片段：掃全文找跟 query 詞彙重疊最高的 ~n 字為中心取，避免只看前 n 字漏掉後段重點。
    n=1200（2026-07-30 從 400 調升）：Grader 看的片段太短會漏掉關鍵數字——中文 query 對英文
    Fundamentals 內容零詞彙命中時會退回前綴截斷 t[:n]，400 字剛好切在 header/overview，把
    後段的 'Revenue (TTM): $742.78B / Gross Margin' 切掉，導致 Grader 明明有 chunk 卻誤判
    insufficient、逼執行層漂移到錯口徑（見 mh-01 診斷）。Fundamentals chunk 最長 ~1421 字，
    1200 足以讓短的 key-value chunk 全顯示。_mechanical_summary 仍顯式傳 n=200 不受影響。"""
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    terms = _query_terms(query)
    start, score = (0, -1) if not terms else _best_window_start(t.lower(), terms, n)
    if score <= 0:
        return t[:n] + "…"
    window = t[start:start + n]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + n < len(t) else ""
    return prefix + window + suffix


def _chunk_id(c: dict) -> str:
    """chunk 的可複製 id：'source#chunk_index'。"""
    return f"{c['source']}#{c['chunk_index']}"


def _merge_chunks(pool: list[dict], new: list[dict]) -> list[dict]:
    """聯集去重（(source, chunk_index) 為 key，保留 raw_rerank_score 較高的），依 rerank 降序回傳。
    讓「補救改寫重搜」只加候選、不洗掉先前找到的好 chunk（沿用舊版累積池語意，但改為顯式 state）。"""
    by_key = {(c["source"], c["chunk_index"]): c for c in (pool or [])}
    for c in new or []:
        k = (c["source"], c["chunk_index"])
        old = by_key.get(k)
        if old is None or c["raw_rerank_score"] > old["raw_rerank_score"]:
            by_key[k] = c
    return sorted(by_key.values(), key=lambda x: x["raw_rerank_score"], reverse=True)


def _fair_select(collected: list[dict], k: int) -> list[dict]:
    """跨子問題公平取 top-k 餵 Generator（P0-1）。
    collected 全域依 raw_rerank_score 排序後直接截斷有兩個病灶:① 高分子問題把其他子問題整段擠出
    WRITER_MAX_CHUNKS;② cross-encoder 原始分數是「相對當次 query」的,跨子問題不可直接比（B 的
    0.72 可能已是 B 的最佳答案,卻輸給 A 的第七名）。改成 round-robin:各子問題依自身 rerank 排序輪流
    各取一個（保底代表性）,名額用不完再由高分遞補;最終仍依 rerank 排序,給 Generator 由強到弱的穩定
    順序。單一子問題時退化成單純 top-k（collected 通常 ≤ k,直接原樣回傳）。"""
    if len(collected) <= k:
        return collected
    buckets: dict[int, list[dict]] = {}
    for c in sorted(collected, key=lambda x: x["raw_rerank_score"], reverse=True):
        buckets.setdefault(c.get("_subq", 0), []).append(c)
    order = sorted(buckets)
    pos = {i: 0 for i in order}
    picked: list[dict] = []
    while len(picked) < k:
        progressed = False
        for i in order:
            if pos[i] < len(buckets[i]):
                picked.append(buckets[i][pos[i]])
                pos[i] += 1
                progressed = True
                if len(picked) >= k:
                    break
        if not progressed:
            break
    return sorted(picked, key=lambda x: x["raw_rerank_score"], reverse=True)


def _writer_budget(n_facets: int) -> int:
    """自適應生成預算：facet 越多給越多名額（避免 8÷N 稀釋），單意圖維持 ~BASE。見上方常數說明。"""
    n = max(1, int(n_facets or 1))
    return min(WRITER_BUDGET_BASE + WRITER_BUDGET_PER_FACET * (n - 1), WRITER_BUDGET_CAP)


def _loads_json_lenient(text: str):
    """容錯 JSON 解析：剝 code fence，失敗則抓第一個 [...] 或 {...} 區塊再試。解不出回 None。
    （不同模型序列化不穩定，延續舊版對 LLM 輸出一律容錯的精神。）"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = s.find(open_c), s.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except (ValueError, TypeError):
                continue
    return None


def _coerce_bool(v) -> bool:
    """把模型回的 sufficient 值轉成 bool。**不能用 bool()**：模型常把布林序列化成字串,而
    bool('false')==True（任何非空字串都是 True）會讓判定 fail-open（P0-2:模型明明說 false 卻被讀成
    夠）。原生 bool/數字照常,字串只認明確真值詞,其餘（含 'false'/'no'/'0'）一律 False。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "夠", "足夠", "充足")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# 節點用的 LLM 呼叫（Planner / Checker / Reflection）與檢索
# ──────────────────────────────────────────────────────────────────────────────

_PLANNER_PROMPT = f"""你是美股情報 RAG 的規劃器。把使用者問題拆成「彼此獨立、各自可單獨丟去向量庫檢索」的子問題。

拆解原則:
- 單一聚焦問題(只問一家公司的一個面向) → 原樣回傳,陣列只有一個元素。
- 複合 / 跨公司比較 / 一句含多個提問 → 拆成多個原子子問題,每個只問一件事。
- 【意圖保全】一句話裡每個獨立提問都要有對應子問題——包含口語、比喻、隱含的意圖。
  例:「風頭被搶走」是一個「新聞 / 事件」意圖,和「這一季賺多少」的財報意圖是兩件不同的事,
  兩個都要各自拆出來。寧可每個意圖各切一刀,也不要整句只回應其中一半、漏掉另一個提問。
- 【隱含對照】「跟去年比」「成長多少」→ 拆出各年度 / 各實體的獨立子問題
  (例:「Apple 的 FY2025 EPS 是多少」「Apple 的 FY2024 EPS 是多少」)。
- 【集合詞】遇到群體稱呼(Magnificent Seven、FAANG、「這些公司」、「科技巨頭」、「你追蹤的 AI 股」…):
  · 能明確且完整列出全部成員、且成員數不超過上限 → 逐一列滿「每一個」成員,絕不只列一部分。
  · 成員數超過上限、或你不確定完整成員名單 → 「不要」猜一份殘缺清單;保留群體稱呼當「單一寬鬆
    子問題」,讓檢索自己 fan-out(寧可粗,也不要漏掉任何一個成員)。
- 【時間消歧】絕不依靠自身記憶推算或猜測季度、年份、月份。只有原問題明確指定、或下方
  KB Coverage Snapshot 明確列出時,才能在子問題寫入具體期間。使用者指定的期間不得被「最新期間」
  覆蓋;「最近新聞」也不得被改寫成最新財報季度。
  ·【比率指標期間中性】毛利率/淨利率/營業利益率/獲利率/成長率/YoY 等「比率或成長率」指標,若原問題用
    相對詞(目前/最新/現在/近一年/近期)問、且未自行點名某一季,子問題就**保持期間中性、絕不從 snapshot
    釘具體季度**——寫「X 目前的毛利率」而非「X FY20XX QX 的毛利率」。這類指標的權威來源是 Fundamentals 的
    TTM 預算值(無季度標記);一旦把子問題釘成某季,檢索會被季度導向 10-Q、把 TTM 來源整個濾出候選池,
    導致答錯口徑(季度 vs TTM)。唯有原問題自己明講某一季(如「FY2026 Q2 毛利率」)才照寫那一季。
- 【檢索標的檢驗】每個子問題都必須指向一個「向量庫裡撈得到的離散事實」——具體的財報數字、或一則具體的
  新聞事件/分析師動作。子問題若只是要求「解讀 / 影響 / 意義 / 背景 / 綜合看法 / 透露什麼訊息 / 反映什麼
  策略」,它**沒有自己的檢索標的**(答案要靠把上面撈到的事實做綜合,那是最後生成階段的工作),絕不可拆成獨立子問題:
  · 「這則新聞對 X 股價/處境的影響是什麼」「這透露了 X 的什麼策略訊息」→ 併進「那則新聞的內容是什麼」子問題,
    不要另外切一刀。撈的是同一個 chunk,多切只會撈回同一片段+噪音,傷準確率。
  · 「市場對 X 的綜合看法 / 整體評價是什麼」這種泛化收束句,若原問題沒有明確點名要它 → **當作杜撰,直接刪掉**。
  · 同一個新聞事件/分析師動作裡的多個面向(例:同一家投行「調升目標價」與「調升評等」)是**一則新聞**,合成一個子問題。
- 【精簡優先】在**不違反上面「意圖保全」與「集合詞逐一列滿」**的前提下,用**最少**的子問題涵蓋所有意圖:
  · 典型「財報數字 + 新聞事件」複合題就是 **2 個**子問題(一個問數字、一個問那則新聞);不要為了湊數而過度拆解。
  · **同一公司同一面向不要拆成多個近義子問題**——例如同一則新聞事件拆成兩問、或把同一個指標換句話問兩次
    (「毛利率是多少」vs「毛利率水準如何」)都算重複,合成一個即可。不同指標(毛利率 vs 淨利率)才算不同意圖。
  · 只有「集合詞需逐一列舉成員」或「多實體/多年度比較」時,才可以超過 3 個(此時以意圖保全為準,不受精簡限制)。
- 不要杜撰原問題沒有的意圖;不要拆過細。最多 {MAX_SUBQUERIES} 個。

只輸出一個 JSON 陣列(元素是繁體中文子問題字串),不要任何其他文字。
例:["Apple 的 FY2025 EPS 是多少","Apple 的 FY2024 EPS 是多少"]"""


def _plan_subqueries(query: str, freshness_mode: str) -> list[str]:
    """Planner 節點的核心：把問題拆成原子子問題。拆解失敗(解不出 JSON) → 退回單一問題,不讓規劃器失手就整個 run 掛。"""
    system_prompt = _PLANNER_PROMPT + "\n\n" + _build_temporal_contract(freshness_mode)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": query}]
    with _quiet():
        raw = rq.call_llm(messages, CHECKER_MODEL, temperature=0.0)
    data = _loads_json_lenient(raw)
    subs = [str(x).strip() for x in data if str(x).strip()] if isinstance(data, list) else []
    if not subs:
        subs = [query.strip()]
    subs = subs[:MAX_SUBQUERIES]
    _trace(f"plan: {len(subs)} sub-queries → {subs}")
    return subs


_CHECKER_PROMPT = """你是嚴格的「資訊充足度評論家」。給你一個「子問題」、可能附上的「時間與資料邊界」,
以及「檢索到的候選片段(含 rerank 分數與摘要)」,判斷這些片段**合起來**夠不夠回答子問題。

- 夠(片段裡確實有回答子問題所需的具體事實 / 數字 / 敘述) → sufficient=true。
- 不夠(離題、只沾到邊、缺關鍵數字或實體) → sufficient=false,並:
  1. 具體指出「缺什麼」(哪個數據 / 實體 / 面向沒出現)。
  2. 給一個「改寫後、更可能撈到那個缺漏資訊的新檢索 query」——換措辭或用更具體的關鍵字 / 實體 /
     財報標準術語 / 英文,**不要照抄原句**(照抄通常撈到一模一樣的東西)。
- 不論 sufficient 是 true 還是 false,都要從下面列出的候選片段中,把「跟子問題主題真正相關、可以拿來
  當作答案依據」的片段挑出來,填進 relevant_ids(用片段的 "id=" 那串,不是 [數字] 編號)。跟子問題主題
  無關或講的是別的實體/公司的候選(例如子問題問 A 公司,片段其實是 B 公司)**不要**列入——寧可少列,
  也不要為了湊數把離題片段也算進去。

⏱ KB 時間天花板(重要,避免無限空轉):「時間與資料邊界」會列出這個知識庫實際涵蓋到的最新期間
(KB Coverage Snapshot)。若候選片段已經涵蓋到「該主題在 KB 中最新可得的資料」,則**即使子問題
要求的期間比 KB 更新(那是知識庫本來就沒有的資料),也要判 sufficient=true**——收下 KB 最新可得
資料即可。**絕對不要**因為「片段不是使用者要求的那個更新日期」而持續判不足、反覆要求換 query
重搜:KB 沒有的期間,再怎麼搜都搜不到,那只會空轉撞牆。只有在片段確實離題、或缺少「coverage 範圍
內、KB 應該要有卻沒撈到」的資料時,才判 sufficient=false。

只輸出一個 JSON 物件,不要任何其他文字:
{"sufficient": true 或 false, "missing": "缺什麼(夠則空字串)", "new_query": "改寫後的新 query(夠則空字串)",
 "relevant_ids": ["真正相關的候選 id", ...]}"""


def _check_sufficiency(subquery: str, pool: list[dict], temporal_scope: str = "") -> dict:
    """Sufficiency Checker 節點的核心:一次 LLM 呼叫吐出 {sufficient, missing, new_query, relevant_ids}。
    temporal_scope(KB coverage + 時間政策)一併餵給 Grader,讓它能分辨「搜得不夠好」與「KB 天花板已到」
    ——否則對『要求比 KB 更新期間』的題(如指定未來日期的新聞題)Grader 會一路判不足、逼 Agent 空轉
    撞牆(實測:news 類多題因此跑滿 MAX_REWRITES、拖到 ~1 小時/題)。
    relevant_ids:Grader 從「這次實際看到的候選」裡圈選出來的相關 chunk id,只接受出現在 shown_ids
    的值(擋 LLM 憑空造 id)。_node_execute commit 進 collected 時用它過濾,不再是「rerank top-k 全收」
    ——修 execute[1] 誤把不相關公司的高分 chunk 一起 commit 進 citation 的殘留雜訊。"""
    if not pool:
        return {"sufficient": False, "missing": "尚未檢索到任何候選片段", "new_query": subquery, "relevant_ids": []}
    top = pool[:POOL_RETURN_K]
    shown_ids = {_chunk_id(c) for c in top}
    ctx = "\n".join(
        f"[{i}] rerank={c['rerank_score']:.3f} | id={_chunk_id(c)}\n    {_snippet(c['content'], subquery)}"
        for i, c in enumerate(top, start=1)
    )
    scope_block = f"時間與資料邊界:\n{temporal_scope}\n\n" if temporal_scope else ""
    user = f"{scope_block}子問題:{subquery}\n\n檢索到的候選片段:\n{ctx}"
    messages = [{"role": "system", "content": _CHECKER_PROMPT}, {"role": "user", "content": user}]
    with _quiet():
        raw = rq.call_llm(messages, CHECKER_MODEL, temperature=0.0)
    data = _loads_json_lenient(raw)
    if not isinstance(data, dict):
        # 解不出 → 保守當「夠」(避免無限迴圈:池非空但 checker 壞掉時,寧可收下現有候選也不空轉)
        # relevant_ids 也保守給全部(未圈選 = 無過濾訊號,退回舊行為,不誤濾成 0 筆)
        _trace(f"check[{subquery[:24]!r}] ✗ 無法解析 JSON,保守收下現有候選")
        return {"sufficient": True, "missing": "", "new_query": "", "relevant_ids": sorted(shown_ids)}
    sufficient = _coerce_bool(data.get("sufficient", False))
    missing = str(data.get("missing", "") or "").strip()
    new_query = str(data.get("new_query", "") or "").strip()
    raw_ids = data.get("relevant_ids", [])
    relevant_ids = ([str(x).strip() for x in raw_ids
                     if isinstance(x, str) and str(x).strip() in shown_ids]
                    if isinstance(raw_ids, list) else [])
    _trace(f"check[{subquery[:24]!r}] sufficient={sufficient} missing={missing[:50]!r} "
           f"new_query={new_query[:50]!r} relevant_ids={len(relevant_ids)}/{len(shown_ids)}")
    return {"sufficient": sufficient, "missing": missing, "new_query": new_query, "relevant_ids": relevant_ids}


def _retrieve_chunks(query: str) -> list[dict]:
    """Retriever 節點的核心:跑一次生產 rq.retrieve,組態固定 enable_rewrite=False + full_translate_en=True。
      · enable_rewrite=False(P0-5):Checker 已提供針對「缺什麼」的 targeted rewrite(active_query),
        不讓 rq 二次無差別改寫稀釋其意圖;且 rag_query sem-11 已證 multi-variant rewrite 對 cross-lingual 無效。
      · full_translate_en=True(2026-07-26 架構決定):中文問、中文答,但「檢索中間層一律用英文」——
        query 先整句翻成英文,dense/sparse 召回與 cross-encoder rerank 全部用該英文譯句,以消除
        cross-lingual reranker 對「中文 query × 英文 chunk」評分平坦的失真(本輪逐題診斷發現:
        news-08 正確 chunk 因中文 query rerank≈0 被壓到 rank 6、sem-15 同因排序崩成雜訊)。
        這推翻先前 translate_query_en=False 的保留結論——舊消融只量到 file-level recall(已飽和),
        沒量到 chunk 內排序這層;且舊模式是 max(原句,英譯)雙 query,這裡改為單一英文 query 更徹底。
        query 理解 hard filter 與答案生成仍用原始中文(語言無關 / 使用者要中文答)。
    補救迭代與首輪用完全相同組態,唯一差別是 active_query 已被 Checker 改寫(補救的主力施力點)。"""
    bge_m3, rerank_model, client = _get_models()
    with _RETRIEVE_LOCK:
        with _quiet():   # 靜音 retrieve 的 DEBUG print
            chunks, _note = rq.retrieve(
                query, bge_m3, rerank_model, client,
                top_k=rq.RERANK_INPUT_N,   # 多取一些進池(池會重排,advance 時只收 top-k)
                model_name=RETRIEVAL_MODEL,
                enable_rewrite=False,
                full_translate_en=True,   # 檢索中間層一律英文(見上方 docstring)
            )
    return chunks or []


# ──────────────────────────────────────────────────────────────────────────────
# Generator（無 agency）：Python 拿 collected 全文走生產生成契約,單次生成。
# 「生成」不在 agent 迴圈裡,是消除「跳過生成 / 背景知識幻覺 / 弄丟 citation」的結構性關鍵。
# ──────────────────────────────────────────────────────────────────────────────

# agentic 輸出語言硬約束(繁中產品定位):整段敘述繁體中文,但依 Rule 11 保留來源 "$X billion/million"
# 單位詞與英文術語/代號原文不改(數字後續由 rq.convert_usd_units_to_yi 雙寫成「億(＄X billion)」)。
_ZH_ANSWER_DIRECTIVE = (
    "\n\nLANGUAGE — 你的整段回答必須用【繁體中文】書寫,不得用英文段落、不得用簡體字。"
    "保留英文金融術語、股票代號(NVDA/MSFT/…)、以及來源的 \"$X billion / $Y million\" 單位詞【照原文】"
    "(依 Rule 11,不要自己換算成億);只有敘述文字要繁體中文,數字與其單位詞不改。"
)


def _write_final_answer(query: str, chunks: list[dict], model_name: str, extra_user: str = "") -> str:
    """單次 Generator 呼叫:把 chunks 全文塞進生產生成契約產出答案。extra_user 夾帶糾錯指示(重生成用)。"""
    user_prompt = rq.build_user_prompt(query, chunks) + extra_user
    messages = [
        {"role": "system", "content": rq.SYSTEM_PROMPT + _ZH_ANSWER_DIRECTIVE},
        {"role": "user", "content": user_prompt},
    ]
    with _quiet():
        return rq.call_llm(messages, model_name, temperature=rq.GEN_TEMPERATURE)


# ──────────────────────────────────────────────────────────────────────────────
# 確定性 citation validator（非 LLM,便宜的一層）：regex 抽 Writer 引用比對 allowlist,
# 抓 ① 完全沒有效引用 ② 捏造 allowlist 外的 ID。不合格→丟回 Writer 重寫(上限 1)。
# 保證「引用指向存在的 chunk」,不保證「句子忠於該 chunk」(faithfulness 交給 reflect / eval)。
# ──────────────────────────────────────────────────────────────────────────────

def _extract_citations(text: str) -> set[tuple[str, int]]:
    """從答案文字抽出 (source, chunk_index) 引用集合。容忍模型偶爾回抄的 'Reference N:' 前綴。"""
    out: set[tuple[str, int]] = set()
    for m in _CITE_RE.finditer(text or ""):
        fn = _REF_PREFIX_RE.sub("", m.group(1)).strip()
        try:
            out.add((fn, int(m.group(2))))
        except (ValueError, TypeError):
            continue
    return out


def _validate_and_fix_citations(query: str, answer: str, allowed_chunks: list[dict],
                                 model_name: str, verbose: bool = False) -> str:
    """確定性 citation 稽核 + 有界重試。回傳(盡量)合格的答案。"""
    allowed = {(c["source"], c["chunk_index"]) for c in allowed_chunks}
    if not allowed:
        return answer
    for attempt in range(CITATION_VALIDATOR_MAX_RETRIES + 1):
        cited = _extract_citations(answer)
        hallucinated = cited - allowed
        has_valid = bool(cited & allowed)
        if has_valid and not hallucinated:
            return answer
        if attempt >= CITATION_VALIDATOR_MAX_RETRIES:
            if verbose:
                print(f"--- citation validator: 重試耗盡,走機械式收尾"
                      f"（has_valid={has_valid}, hallucinated={sorted(hallucinated)}）---")
            break
        problems = []
        if not has_valid:
            problems.append("- 你的答案沒有任何有效的 [filename, chunk #N] 引用。每個事實陳述後都必須加。")
        if hallucinated:
            bad = ", ".join(f"[{s}, chunk #{i}]" for s, i in sorted(hallucinated))
            problems.append(f"- 以下引用不存在於提供的參考資料中（你捏造了 ID，禁止）：{bad}。"
                            f"只能引用上面 Reference 區塊裡真實出現的 filename + chunk #。")
        if verbose:
            print(f"--- citation validator (attempt {attempt+1}): 不合格,重寫 ---\n" + "\n".join(problems))
        suffix = ("\n\n⚠ 引用檢查未通過，請修正後重寫整份答案（內容依據不變，只改引用）：\n"
                  + "\n".join(problems))
        revised = _write_final_answer(query, allowed_chunks, model_name, extra_user=suffix)
        if revised and revised.strip():
            answer = revised
    return answer


def _format_citations(chunks: list[dict]) -> str:
    if not chunks:
        return ""
    lines = [f"  - {c['source']} #{c['chunk_index']} (rerank={c['rerank_score']:.3f})" for c in chunks]
    return "\n\n---\n📚 引用來源（Generator 實際依據的 chunk）:\n" + "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Reflection（選配,貴的一層）：LLM 忠實度稽核,找「來源無法支持的陳述(幻覺)」,有則帶問題重生成一次。
# ──────────────────────────────────────────────────────────────────────────────

_REFLECT_PROMPT = """你是嚴格的忠實度稽核員。給你「使用者問題」「答案」「答案唯一能依據的來源 chunk 全文」。
逐一檢查答案裡「每個可查證的宣稱」,特別是每個(數字＋單位＋期間＋主體)與每個具體事實(誰做了什麼、
漲/跌方向、投資或合作的對象與金額、產品名)。對每一項回到來源 chunk 全文比對,挑出下列任一種問題:

【捏造/矛盾】答案給的數字、單位、方向、主體、期間或事實,在來源中找不到,或與來源明確牴觸。
  例:來源說投資 Anthropic,答案卻說投資 OpenAI;來源毛利率 71%,答案寫 74%;來源說「下降」,答案寫「上升」。
【假性查無(反向)】答案說「來源未提及/未揭露/沒有資訊/no information」,但來源 chunk 其實有講到那件事或那個數字。

判定原則(避免誤殺):
- 判事實不判措辭。換句話說、換框架、把 billion 換算成億(如 $84.75 billion = 847.5 億),只要數字與主體對得上就不是問題。
- 期間口徑不同不算錯(答案給 FY、來源另有 TTM,只要各自標對)。
- 只挑真的對不上來源的;無關緊要的潤飾不要挑。

沒有任何問題→只回一個字:"NONE"。
有→逐條列出,每行格式:「<答案裡的原句或數字> ← 來源實際為 <來源說法，或「來源根本沒有」>」。不要輸出其他文字。"""

_REFLECT_REVISE_SUFFIX = """

⚠ 忠實度稽核發現以下問題,請重寫整份答案並修正(維持 [filename, chunk #N] 引用格式):
- 屬「捏造/矛盾」者:改成來源實際的數字/主體/方向,或直接刪除該句;
- 屬「假性查無」者:來源其實有,請補上正確事實並加引用,不要再說「未提及/未揭露」。

{issues}"""


def _reflect_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                     verbose: bool = False) -> str:
    """Reflection 節點的核心:稽核幻覺→有則帶問題重生成一次→再過 citation 稽核。無幻覺原樣回傳。"""
    if not chunks or not (answer or "").strip():
        return answer
    sources_text = "\n\n".join(f"[{c['source']} #{c['chunk_index']}]\n{c['content']}" for c in chunks)
    messages = [
        {"role": "system", "content": _REFLECT_PROMPT},
        {"role": "user", "content": f"問題:{query}\n\n答案:\n{answer}\n\n來源 chunk 全文:\n{sources_text}"},
    ]
    with _quiet():
        result = rq.call_llm(messages, model_name, temperature=0.0)
    issues = (result or "").strip()
    if not issues or issues.upper().startswith("NONE"):
        return answer
    if verbose:
        print(f"--- reflection: 發現疑似幻覺 ---\n{issues}")
    _trace(f"reflect: 發現幻覺,重生成一次 → {issues[:80]!r}")
    revised = _write_final_answer(query, chunks, model_name,
                                  extra_user=_REFLECT_REVISE_SUFFIX.format(issues=issues))
    if revised and revised.strip():
        # 重生成後再過 citation 稽核,確保修正時沒引入捏造引用
        return _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose)
    return answer


# ──────────────────────────────────────────────────────────────────────────────
# RAG-as-tool：把檢索 / 網路搜尋包成 subagent 可呼叫的 @tool
#
# run-scoped 檢索池：Agent A 每次呼叫 rag_search 就把撈到的 chunk 去重併入「目前子問題」的池，
# execute 節點在 subagent 跑完後直接讀它拿「這個待辦實際檢索到什麼」（不信任 agent 轉述——沿用舊 deepagents
# 版 _RUN_SELECTIONS 教訓）。
#
# 2026-07-30：改用 contextvars.ContextVar 裝整包 run state（取代先前的模組全域 list/set/int）。
# 動機：子問題平行/pipeline 執行時，兩個子問題可能同時「在途中」（例如 A 在等 Grader 的 LLM 回應、
# B 已經開始下一輪 retrieve）——如果狀態是單一模組全域，會被彼此互相踩到、池會混在一起。
# ⚠ 不能直接「用 query 字串分池」：同一個子問題內部的補救重試（MAX_REWRITES）每一輪 active_query
# 都不同（Grader 的 targeted rewrite），照字面 query 分 key 反而會把同一子問題自己的重試輪次拆散，
# 破壞現有「跨輪累積」的 _merge_chunks 語意。真正要隔離的單位是「子問題」，不是「這一輪用的 query」。
# 也不能用參數顯式傳遞：rag_search / web_search 是 LangChain tool，LLM 呼叫時只會帶 query 這個參數，
# 沒有管道夾帶「目前是哪個子問題」。ContextVar 是分辨這兩者的正確工具：同步單執行緒下行為與舊版
# 完全一致（只有一個 context 在跑）；未來若用 asyncio 讓子問題重疊執行，每個 asyncio.Task 建立時會
# 拷貝當下 context，之後彼此獨立、互不污染，rag_search/web_search 簽名完全不用改。
# ──────────────────────────────────────────────────────────────────────────────

import contextvars
from langchain_core.tools import tool


class _RunState:
    """一個子問題的 run-scoped 狀態：檢索池、web 筆記、Grader 圈選的相關 id、工具呼叫次數。"""
    __slots__ = ("pool", "web_notes", "relevant_ids", "rag_calls", "web_calls")

    def __init__(self) -> None:
        self.pool: list[dict] = []
        self.web_notes: list[str] = []
        self.relevant_ids: set[str] = set()
        self.rag_calls = 0
        self.web_calls = 0


_run_state_var: contextvars.ContextVar[_RunState] = contextvars.ContextVar("agentic_run_state")


def _current_run_state() -> _RunState:
    """取得目前 context 的 run state；理論上 executor 一律先呼叫 _reset_run_pool()，這裡的
    LookupError 保底只防禦「忘了先 reset」的意外情況，不讓 tool call 直接炸掉。"""
    try:
        return _run_state_var.get()
    except LookupError:
        state = _RunState()
        _run_state_var.set(state)
        return state


def _reset_run_pool() -> None:
    """開始處理一個新子問題前呼叫：換上一份全新、獨立的 run state。"""
    _run_state_var.set(_RunState())


def _tavily_search(query: str) -> str:
    """Tavily 網路搜尋，回傳幾則結果的標題/摘要/網址（每則帶 [web: url] 供生成端引用）。
    任何失敗（停用 / 缺 key / API 錯）→ 回傳明確說明字串，絕不 raise（不讓 web 成為單點故障）。"""
    if not ENABLE_WEB_SEARCH:
        return "（web search 已停用）"
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "（web search 不可用：未設定 TAVILY_API_KEY）"
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        resp = client.search(query, max_results=TAVILY_MAX_RESULTS, search_depth="basic")
        results = resp.get("results", []) if isinstance(resp, dict) else []
        if not results:
            return "（web search 查無結果）"
        lines = []
        for r in results[:TAVILY_MAX_RESULTS]:
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            content = " ".join((r.get("content") or "").split())[:300]
            lines.append(f"- {title} [web: {url}]\n  {content}")
        return "網路搜尋結果:\n" + "\n".join(lines)
    except Exception as e:
        return f"（web search 失敗：{e!r}）"


@tool
def rag_search(query: str) -> str:
    """在美股情報知識庫（10-K / 10-Q 財報、財經新聞、財務數據）做混合檢索
    （dense + sparse → RRF → cross-encoder rerank）。傳入一句檢索 query（可用更具體的關鍵字 / 實體 /
    財報標準術語 / 英文），回傳排序後的 top 候選片段，每則含 rerank 分數與可引用的 id（source#chunk）。"""
    state = _current_run_state()
    if state.rag_calls >= RAG_SEARCH_MAX_CALLS:
        _trace(f"  tool rag_search({query[:40]!r}) → 已達硬上限 {RAG_SEARCH_MAX_CALLS} 次,拒絕")
        return (f"（rag_search 已達本子問題上限 {RAG_SEARCH_MAX_CALLS} 次，停止搜尋。"
                f"目前池中已有 {len(state.pool)} 個候選、最高 rerank 已到頂——請**不要再搜尋**，"
                f"直接根據已檢索到的內容回報事實 / 作答。）")
    state.rag_calls += 1
    chunks = _retrieve_chunks(query)
    merged = _merge_chunks(state.pool, chunks)
    state.pool.clear()
    state.pool.extend(merged)
    if not merged:
        return "（知識庫查無相關片段——若問題是近期新聞/最新事件，考慮改用 web_search）"
    top = merged[:POOL_RETURN_K]
    lines = [
        f"[{i}] rerank={c['rerank_score']:.3f} | id={_chunk_id(c)}\n    {_snippet(c['content'], query)}"
        for i, c in enumerate(top, start=1)
    ]
    _trace(f"  tool rag_search({query[:40]!r}) → pool={len(merged)} top={merged[0]['rerank_score']:.3f}")
    return "檢索結果（知識庫，共 %d 筆，顯示前 %d）:\n" % (len(merged), len(top)) + "\n".join(lines)


@tool
def web_search(query: str) -> str:
    """【最後手段，非首選】只有在你已經用 rag_search 搜過、且確認知識庫『真的沒有』相關資料後，才用它。
    知識庫（本系統的主要事實來源）優先；不要一遇到「最近／最新」字樣就跳來用它。每個子問題最多用幾次，
    用完就要停、改用已檢索到的知識庫內容作答或如實說明查無。傳入搜尋 query，回傳網路結果（外部資料，
    引用請用 [web: 網址] 標註）。"""
    state = _current_run_state()
    if state.web_calls >= WEB_SEARCH_MAX_CALLS:
        _trace(f"  tool web_search({query[:40]!r}) → 已達硬上限 {WEB_SEARCH_MAX_CALLS} 次,拒絕")
        return (f"（web_search 已達本子問題上限 {WEB_SEARCH_MAX_CALLS} 次，不再搜尋。"
                f"請改用你已從 rag_search 檢索到的知識庫內容作答；若知識庫確實查無，就如實說明查無。）")
    state.web_calls += 1
    note = _tavily_search(query)
    if note and not note.startswith("（"):
        state.web_notes.append(note)
    _trace(f"  tool web_search({query[:40]!r}) [{state.web_calls}/{WEB_SEARCH_MAX_CALLS}] → {len(note)} chars")
    return note


# ──────────────────────────────────────────────────────────────────────────────
# Agent A（Researcher+Generator，真正的 ReAct agent）：整個 process 只建一次、共享。
# ──────────────────────────────────────────────────────────────────────────────

_subagent_apps: dict[tuple[str, str, bool], object] = {}


def _get_subagent(freshness_mode: str):
    # snapshot 是 closed-corpus 契約：即使 caller 忘了加 --no-web，也在工程層直接不掛載工具。
    allow_web = ENABLE_WEB_SEARCH and freshness_mode == FRESHNESS_LIVE
    key = (freshness_mode, rq.COLLECTION_NAME, allow_web)
    if key not in _subagent_apps:
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent
        llm = ChatOpenAI(
            model=SUBAGENT_MODEL, base_url=rq.NVIDIA_BASE_URL,
            api_key=os.getenv("NVIDIA_API_KEY"), temperature=rq.GEN_TEMPERATURE,
        )
        tools = [rag_search] + ([web_search] if allow_web else [])
        prompt = _RESEARCHER_LOCKED_PROMPT + "\n\n" + _build_temporal_contract(freshness_mode)
        _subagent_apps[key] = create_react_agent(llm, tools, prompt=prompt)
    return _subagent_apps[key]


_RESEARCHER_LOCKED_PROMPT = """你是美股情報的檢索研究員。你唯一的任務是用工具蒐集足以回答「當前子問題」
所需的具體事實（數字、實體、敘述）。

🏛 知識庫優先（最重要的原則）：rag_search 檢索的內部知識庫（10-K/10-Q 財報、財經新聞、財務數據）是本系統
的**主要且最可信的事實來源**，golden answer 也是根據它產生的。你的預設做法是：
- **一律先用 rag_search**。可用不同措辭 / 更具體的實體 / 財報標準術語 / 英文換 query 搜。但**一旦 top
  rerank 分數不再明顯提升（代表已撈到最相關內容），就立刻停止搜尋**——通常搜 2~4 次就夠了。**絕不要**
  為了湊數反覆換關鍵字狂搜十幾次：過度搜尋只會嚴重拖慢速度、對答案品質毫無幫助（每次搜尋成本很高）。
- 即使問題帶有「最近 / 最新 / 近期新聞」字樣，也**先假設知識庫裡就有**（它含有近期財報與新聞），
  用 rag_search 找即可；知識庫的財報數字比網路摘要更精確可靠，有就優先用。

🌐 web_search 是最後手段，不是首選：只有在你已經用 rag_search 認真搜過數次、且**確認知識庫真的沒有**相關
資料時，才動用 web_search，而且要克制（每個子問題最多幾次，用完就停）。不要為了湊「更新」而狂猜關鍵字
反覆上網——那既昂貴又常常撈到用不上的雜訊。若知識庫和有限的 web 嘗試都找不到，就如實回報「查無」，
不要瞎猜。

⏱ 時間 grounding（硬規則）：
- 今天日期與 KB coverage 是不同概念，絕對不要混為一談。
- 絕不自行創造季度、年份、月份或發布日期；只能使用 temporal_scope、Coverage Snapshot 或工具結果明確
  出現的日期。使用者已指定期間時，不得替換成另一期間。
- rag_search 是相關性排序，不是時間排序；top-1 不代表全庫最新文件。判定全庫 cutoff 只能依系統提供的
  Coverage Snapshot，不能從某一次搜尋結果自行外推。
- snapshot 模式的「最新」就是 KB 中最新文件，不補 wall-clock 資料、不加 cutoff 警語。
- live 模式若新聞 cutoff 早於今天：先查 KB；web 可用時只補 cutoff 後缺口；web 停用或失敗時如實保留
  時效限制，絕不把 KB 答案冒充成截至今天的完整資訊。

工作方式：每一輪把你這次用工具找到的關鍵事實「簡短列點」回報即可。

🚦 硬性限制：在你收到明確的「評估已通過」指令之前，**只透過工具回報你找到的事實，
絕對不要自行寫出最終的局部答案或摘要、不要下結論**。你的工作此刻只有「搜集與回報事實」。"""


def _last_ai_text(messages) -> str:
    """從 react agent 回傳的 message 串抓最後一則有內容的 AI 文字（即局部摘要）。"""
    for m in reversed(messages or []):
        if getattr(m, "type", "") == "ai":
            content = getattr(m, "content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def _mechanical_summary(task: str, chunks: list[dict]) -> str:
    """零 LLM 的機械式摘要：直接把 top chunk 的 query-aware 片段拼起來，帶真實 [source, chunk #N] 引用。
    只在「連降級呼叫都失敗」時當最後一道保底，保證這個函式永遠不會 raise、永遠有非空輸出。"""
    lines = [f"[{c['source']}, chunk #{c['chunk_index']}] {_snippet(c['content'], task, n=200)}"
             for c in chunks]
    return f"（子問題「{task}」的生成步驟發生錯誤，以下為檢索到的原始片段）\n" + "\n".join(lines)


def _fallback_local_summary(task: str, chunks: list[dict]) -> str:
    """subagent 崩潰 / 沒吐摘要時的降級：直接把池裡 top chunk 走生產生成契約產一段局部摘要。
    **這是安全網本身，不能又依賴同一個可能故障的 LLM API 而沒有退路**——包 try/except，
    若 _write_final_answer 也失敗（見實測：NVIDIA 端連續 504 時，降級呼叫一樣會炸），
    退回零 LLM 的機械式摘要，保證 _run_executor 永遠有摘要可回、不會把例外炸穿整個 graph。"""
    if not chunks:
        return f"（子問題「{task}」在知識庫中查無足夠資料）"
    try:
        return _write_final_answer(task, chunks, GEN_MODEL)
    except Exception as e:
        _trace(f"_fallback_local_summary 也失敗（{e!r}）→ 退回零 LLM 機械式摘要")
        return _mechanical_summary(task, chunks)


def _run_executor_react(task: str, temporal_scope: str, freshness_mode: str,
                        subq_index: int, verbose: bool) -> tuple[str, list[str]]:
    """[舊行為，AGENTIC_REACT_EXECUTOR=true 才用] Executor 核心：Agent A（ReAct 檢索員）⇄ Grader 的訊息
    傳遞迴圈。先純檢索（system prompt 鎖生成）→ Grader 評分 → 不夠餵糾正訊息續搜（有界 MAX_REWRITES）→
    夠了發「解除限制、生成摘要」觸發訊息 → A 產出局部摘要 → 跳出。回傳 (summary, web_notes)。
    ⚠ 2026-07-29 chunk 層級 probe 證實此路徑是 multi_intent context_recall 漏水主因（Agent A 自行改寫
    query 把 planner 已對的子問題搞砸），預設已改走 _run_executor_deterministic。"""
    from langchain_core.messages import HumanMessage

    _reset_run_pool()
    state = _current_run_state()
    summary = ""
    try:
        app = _get_subagent(freshness_mode)
        messages = [HumanMessage(content=(
            f"當前子問題：{task}\n\n時間與資料邊界：\n{temporal_scope}\n\n"
            "請開始用工具檢索、回報找到的事實（先不要生成摘要）。"))]
        for rnd in range(MAX_REWRITES + 1):
            with _quiet():
                out = app.invoke({"messages": messages},
                                 config={"recursion_limit": SUBAGENT_RECURSION_LIMIT})
            messages = out["messages"]
            verdict = _check_sufficiency(task, state.pool, temporal_scope)   # Agent B｜Grader（單次 LLM、無工具）
            state.relevant_ids.clear()
            state.relevant_ids.update(verdict.get("relevant_ids", []))
            _trace(f"execute[{subq_index}] round={rnd} pool={len(state.pool)} "
                   f"sufficient={verdict['sufficient']} missing={verdict['missing'][:40]!r}")
            if verdict["sufficient"] or rnd >= MAX_REWRITES:
                # 通過（或次數用盡強制放行）：發最後觸發訊息，解除生成鎖，要 A 產出局部摘要
                messages.append(HumanMessage(content=(
                    "評估已通過、證據充足。現在解除先前的限制，根據你剛剛用工具檢索到的所有片段，"
                    "生成一份邏輯連貫、精確的局部答案／摘要，回答上述子問題。每個關鍵事實後面標上你檢索到的"
                    " id（格式 [source, chunk #N]，新聞/網路來源用 [web: 網址]）；沒有依據的內容不要寫。")))
                with _quiet():
                    fin = app.invoke({"messages": messages},
                                     config={"recursion_limit": SUBAGENT_RECURSION_LIMIT})
                summary = _last_ai_text(fin["messages"])
                break
            # 不夠：餵糾正訊息，明令繼續搜尋、不要生成摘要（沿用 Grader 的 missing/new_query）
            missing = verdict["missing"] or "關鍵事實不足"
            new_q = verdict["new_query"] or task
            messages.append(HumanMessage(content=(
                f"目前證據不足。缺失資訊：{missing}。請依此建議重新檢索：{new_q}。"
                f"記得：繼續用工具搜尋、**不要生成摘要**。")))
    except Exception as e:
        _trace(f"execute[{subq_index}] subagent 崩潰 → 降級單次檢索+生成：{e!r}")
        if not state.pool:
            with _quiet():
                state.pool.extend(_merge_chunks([], _retrieve_chunks(task)))
        summary = _fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])

    if not (summary or "").strip():
        # react 跑完但沒吐摘要（弱腦跳過）→ 降級用池裡的 chunk 生成
        summary = _fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])
    return summary, list(state.web_notes)


def _run_executor_deterministic(task: str, temporal_scope: str, freshness_mode: str,
                                subq_index: int, verbose: bool) -> tuple[str, list[str]]:
    """[預設] Executor 核心（deterministic，2026-07-29 修）：**planner 子問題原封不動直接檢索**，不讓
    Agent A(ReAct) 自行改寫 query。流程：直接 retrieve → Grader 評分 → 不夠則用 Grader 的 targeted
    new_query 再 retrieve（併池、只加不減）→ 有界 MAX_REWRITES → 走生產契約產局部摘要。
    live 模式且 KB 補救後仍不足的近期新聞題，補一次 web_search（保留 live 時效能力）。
    設計依據 [[multi-intent-agentA-is-the-leak]]：乾淨 decomposition 的 gold-chunk 覆蓋(35/75)遠勝
    真實 ReAct v2(16/75)、也贏單管線(25/75)——差別只在拿掉 Agent A 的 query 亂改。"""
    _reset_run_pool()
    state = _current_run_state()
    web_notes: list[str] = []
    active_query = task
    verdict = {"sufficient": False, "missing": "", "new_query": ""}
    try:
        for rnd in range(MAX_REWRITES + 1):
            chunks = _retrieve_chunks(active_query)            # planner/Grader 的 query 直接檢索（無 ReAct 改寫）
            merged = _merge_chunks(list(state.pool), chunks)    # 併池：只加不減，補救輪不洗掉先前好 chunk
            state.pool.clear()
            state.pool.extend(merged)
            verdict = _check_sufficiency(task, state.pool, temporal_scope)   # Agent B｜Grader
            state.relevant_ids.clear()
            state.relevant_ids.update(verdict.get("relevant_ids", []))
            _trace(f"execute[{subq_index}] det round={rnd} q={active_query[:32]!r} pool={len(state.pool)} "
                   f"sufficient={verdict['sufficient']} missing={verdict['missing'][:40]!r}")
            if verdict["sufficient"] or rnd >= MAX_REWRITES:
                break
            active_query = verdict["new_query"] or task        # Grader 的 targeted rewrite（唯一改寫來源，deterministic temp=0）

        # live 時效補救：KB 仍不足且是近期新聞題 → 補一次 web（snapshot 不補、不製造 wall-clock 缺口）
        if (freshness_mode == FRESHNESS_LIVE and ENABLE_WEB_SEARCH and not verdict["sufficient"]
                and rq.looks_like_news_query(task) and _RELATIVE_TIME_RE.search(task or "")):
            note = _tavily_search(verdict.get("new_query") or task)
            if note and not note.startswith("（"):
                web_notes.append(note)
                _trace(f"execute[{subq_index}] det web fallback → {len(note)} chars")
    except Exception as e:
        _trace(f"execute[{subq_index}] deterministic executor 例外 → 降級：{e!r}")
        if not state.pool:
            with _quiet():
                state.pool.extend(_merge_chunks([], _retrieve_chunks(task)))

    summary = _fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])  # 走生產契約單次生成（內含 try/except 保底）
    return summary, web_notes


def _run_executor(task: str, temporal_scope: str, freshness_mode: str,
                  subq_index: int, verbose: bool) -> tuple[str, list[str]]:
    """Dispatcher：預設 deterministic（planner 子問題直接檢索）；AGENTIC_REACT_EXECUTOR=true 回舊 ReAct。"""
    if USE_REACT_EXECUTOR:
        return _run_executor_react(task, temporal_scope, freshness_mode, subq_index, verbose)
    return _run_executor_deterministic(task, temporal_scope, freshness_mode, subq_index, verbose)


# ──────────────────────────────────────────────────────────────────────────────
# LangGraph：state（待辦清單為一等公民）+ 四個主節點 + 邊
# ──────────────────────────────────────────────────────────────────────────────

class SupervisorState(TypedDict, total=False):
    query: str                 # 原始問題
    freshness_mode: str        # live=截至今天；snapshot=封閉 KB 最新資料
    enable_reflection: bool    # 是否跑 Reflection 節點
    verbose: bool
    todos: list[dict]          # 待辦清單（另含 temporal_scope/freshness_gaps/web_used）
    collected: list[dict]      # 跨待辦收集的 KB chunk 聯集（Synthesize 的 citation allowlist 來源）
    web_notes: list[str]       # web_search 的網路結果（另標，不進 chunk allowlist）
    iterations: int            # execute↔replan 已迭代幾次（防無限迴圈）
    sufficient: bool           # Replanner 判定證據已足、可提前收斂
    answer: str                # 最終答案（未附引用清單；附錄在 run_agentic 收尾加）


_REPLANNER_PROMPT = f"""你是美股情報 RAG 的動態重規劃器。給你「原始問題」與「目前待辦清單（含各自狀態與
已完成的局部結果）」。請根據目前已收集到的結果，決定如何更新待辦清單：

- 只有時間契約明確指定 live、web 可用、且某個已完成待辦的 KB cutoff 確實早於查詢日並影響原始問題時，
  才能在 add 裡新增「改用網路搜尋查…」的新待辦。snapshot 模式禁止因 wall clock 新增 web 待辦。
- 若目前已收集到的結果**已足以完整回答原始問題** → sufficient 設 true（剩餘 pending 待辦會被跳過）。
- 若某些還沒做的 pending 待辦其實已無必要 → 把它們的 id 放進 drop。
- 不要重複新增已經有的待辦；新增要克制（清單總數上限 {MAX_TODOS}）。

只輸出一個 JSON 物件，不要任何其他文字：
{{"sufficient": true 或 false, "add": ["新待辦字串", ...], "drop": [待辦id 數字, ...]}}"""


def _node_plan(state: SupervisorState) -> dict:
    freshness_mode = state.get("freshness_mode", FRESHNESS_LIVE)
    subs = _plan_subqueries(state["query"], freshness_mode)   # 沿用 nv 版拆解器
    todos = []
    for i, subquery in enumerate(subs):
        scope, gaps = _build_todo_temporal_scope(subquery, freshness_mode)
        todos.append({
            "id": i,
            "task": subquery,
            "temporal_scope": scope,
            "freshness_gaps": gaps,
            "web_used": False,
            "status": "pending",
            "result": "",
        })
    _trace(f"plan: {len(todos)} todos → {[t['task'] for t in todos]}")
    return {"todos": todos, "collected": [], "web_notes": [], "iterations": 0, "sufficient": False}


def _run_one_todo(todo: dict, freshness_mode: str, verbose: bool) -> dict:
    """在自己的 thread（因此也是自己獨立的 contextvars context）內跑完一個子問題：retrieve↔grade
    迴圈 + commit 篩選，回傳這個子問題的完整結果。不觸碰任何跨子問題共用的可變狀態，讓 wave 執行
    可以安全平行呼叫（已用 ThreadPoolExecutor 實測驗證 contextvars 在 submit() 下天生隔離）。"""
    task = todo["task"]
    summary, web_notes = _run_executor(
        task, todo.get("temporal_scope", ""), freshness_mode, todo["id"], verbose,
    )
    # executor 剛跑完、還在同一個 thread/context 內，_current_run_state() 拿到的就是這個子問題
    # 剛剛用的那份 run state。優先用 Grader 圈選的 relevant_ids 過濾（見 _check_sufficiency）：
    # 不再是「rerank 前 k 名就全收」，濾掉高分但離題的 chunk。Grader 沒給圈選訊號 → 退回舊行為。
    run_state = _current_run_state()
    if run_state.relevant_ids:
        approved = [c for c in run_state.pool if _chunk_id(c) in run_state.relevant_ids]
        picked = approved[:COMMIT_TOP_K] if approved else list(run_state.pool[:COMMIT_TOP_K])
    else:
        picked = list(run_state.pool[:COMMIT_TOP_K])
    # 每家保底覆蓋（Phase 1）：子問題點名多家公司時，保證每一家至少有一個 chunk 被 commit 進
    # collected（否則 COMMIT_TOP_K 截斷會把被比較的公司整個擠掉，見 mh-01）。deterministic、無 LLM。
    mentioned = _mentioned_tickers(task)
    if len(mentioned) > 1:
        picked = rq._ensure_ticker_coverage(run_state.pool, picked, mentioned)
    # ratio 題財務錨源保底（Phase 2）：ratio 意圖時保證每家至少有一個 Fundamentals，接住 Grader 對
    # 「Fundamentals vs 10-K/10-Q」雙來源的擲硬幣（見 _ensure_ratio_source_coverage）。deterministic、無 LLM。
    if mentioned and _is_ratio_intent(task):
        picked = _ensure_ratio_source_coverage(run_state.pool, picked, mentioned)
    for c in picked:
        c["_subq"] = todo["id"]
    return {"id": todo["id"], "summary": summary, "web_used": bool(web_notes),
            "web_notes": web_notes, "picked": picked}


def _node_execute(state: SupervisorState) -> dict:
    """處理「目前這一波」所有 pending 待辦（2026-07-31 改為分波，取代舊版每次只處理一個）。
    同一波內的子問題彼此獨立（來自 planner 一次拆解，或前一輪 replan 一次新增），用 thread pool
    重疊執行：retrieve 仍靠 _RETRIEVE_LOCK 天然序列化，grade/生成這類 LLM API 呼叫才是真的重疊
    ——等一個子問題的 retrieve 做完，下一個子問題的 retrieve 就能接著開始，不必等前一個的 grading
    也做完（pipeline，不是無腦全平行）。整波做完才 replan 一次，不再是每個 todo 各自 replan 一次
    ——次數變少也順便省了 LLM 呼叫。next-hop 待辦是下一輪 replan 才會新增，天然排在下一波，不需要
    額外的相依標記去分辨「這一波的獨立 todos」跟「replan 新增的 next-hop todos」。"""
    todos = [dict(t) for t in state["todos"]]
    freshness_mode = state.get("freshness_mode", FRESHNESS_LIVE)
    verbose = state.get("verbose", False)
    all_pending = [i for i, t in enumerate(todos) if t["status"] == "pending"]
    if not all_pending:
        return {"iterations": state.get("iterations", 0)}   # 沒有 pending（可能被 replan 全部 drop）→ 交給 route 收斂
    # multi_hop 依賴解析（Type B「辨識再查」）：帶未解「該公司」代名詞的第二跳,要等第一跳把公司辨識
    # 出來才能檢索。這一波若還有無依賴的 pending(含第一跳),就先只跑它們、把依賴型 hop 留到下一波
    # ——等 hop-1 結果進 collected 後,下一波(此時 ready 為空)才回填實體並檢索。Type A/單跳無「該公司」
    # 代名詞,ready 涵蓋全部 pending,行為與舊版完全一致。
    ready = [i for i in all_pending if not _is_dependent_hop(todos[i]["task"])]
    deferred = [i for i in all_pending if _is_dependent_hop(todos[i]["task"])]
    if ready:
        pending_idxs = ready
    else:
        # 只剩依賴型 → 第一跳已完成:把「該公司」回填成解出的具體公司,再檢索(否則無 ticker → 撈回隨機 chunk)。
        entity = _resolve_hop_entity(todos, state.get("collected", []))
        for i in deferred:
            if entity:
                filled = _fill_dependent_hop(todos[i]["task"], entity)
                if filled != todos[i]["task"]:
                    _trace(f"execute: 第二跳回填實體 {entity!r} → {filled!r}")
                    todos[i]["task"] = filled
                    sc, gp = _build_todo_temporal_scope(filled, freshness_mode)
                    todos[i]["temporal_scope"], todos[i]["freshness_gaps"] = sc, gp
            else:   # 解不出實體(理論上不該發生)→ 原樣檢索,降級但不 deadlock
                _trace(f"execute: 第二跳無法解出實體,原樣檢索(降級) → {todos[i]['task']!r}")
        pending_idxs = deferred
    for i in pending_idxs:
        todos[i]["status"] = "in_progress"

    results: dict[int, dict] = {}
    workers = min(AGENTIC_PIPELINE_WORKERS, len(pending_idxs))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_idx = {ex.submit(_run_one_todo, todos[i], freshness_mode, verbose): i
                         for i in pending_idxs}
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                # _run_executor 內部已有多層安全網，理論上不該 raise 到這裡；這層只防禦徹底沒預料到
                # 的例外，確保一個子問題爆炸不會拖垮整波其他子問題的結果。
                _trace(f"execute[{todos[i]['id']}] wave worker 未預期例外 → {e!r}")
                results[i] = {"id": todos[i]["id"],
                              "summary": f"（子問題「{todos[i]['task']}」執行時發生未預期錯誤）",
                              "web_used": False, "web_notes": [], "picked": []}

    collected = state.get("collected", [])
    web = list(state.get("web_notes", []))
    for i in pending_idxs:   # 依原始順序合併（thread 完成順序不影響結果，只影響合併時機）
        r = results[i]
        todos[i]["result"] = r["summary"]
        todos[i]["web_used"] = r["web_used"]
        todos[i]["status"] = "done"
        collected = _merge_chunks(collected, r["picked"])
        web += r["web_notes"]
    # 保留舊語意：MAX_ITERS 是「總子問題執行次數」上限，不是「總波次」上限，避免分波後這個防線變寬鬆。
    iters = state.get("iterations", 0) + len(pending_idxs)
    _trace(f"execute wave: {len(pending_idxs)} todos ({[todos[i]['id'] for i in pending_idxs]}) done → "
           f"collected={len(collected)}")
    return {"todos": todos, "collected": collected, "web_notes": web, "iterations": iters}


def _node_replan(state: SupervisorState) -> dict:
    """Replanner：依已完成待辦的局部結果動態增刪清單（新增 web 待辦 / drop 多餘 / 提前收斂）。"""
    todos = [dict(t) for t in state["todos"]]
    lines = []
    for t in todos:
        res = t.get("result", "") or ""
        res = (res[:200] + "…") if len(res) > 200 else res
        lines.append(f"- id={t['id']} [{t['status']}] {t['task']}" + (f"\n    局部結果:{res}" if res else ""))
    user = f"原始問題:{state['query']}\n\n目前待辦清單:\n" + "\n".join(lines)
    freshness_mode = state.get("freshness_mode", FRESHNESS_LIVE)
    system_prompt = _REPLANNER_PROMPT + "\n\n" + _build_temporal_contract(freshness_mode)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]
    with _quiet():
        raw = rq.call_llm(messages, CHECKER_MODEL, temperature=0.0)
    data = _loads_json_lenient(raw)

    # multi_hop 保護：還沒跑的依賴型第二跳(帶未解「該公司」代名詞)是回答原始問題的必要一跳,
    # 不能被機率性 replanner 誤 drop、也不能因 hop-1 一做完就被判 sufficient 跳過。此時強制續跑。
    has_pending_dependent = any(
        t["status"] == "pending" and _is_dependent_hop(t["task"]) for t in todos)

    sufficient = False
    if isinstance(data, dict):
        sufficient = _coerce_bool(data.get("sufficient", False))
        drop_ids = {int(x) for x in (data.get("drop", []) or []) if str(x).lstrip("-").isdigit()}
        for t in todos:
            if t["id"] in drop_ids and t["status"] == "pending" and not _is_dependent_hop(t["task"]):
                t["status"] = "dropped"
        next_id = max((t["id"] for t in todos), default=-1) + 1
        for a in (data.get("add", []) or []):
            if isinstance(a, str) and a.strip() and len(todos) < MAX_TODOS:
                task = a.strip()
                # Prompt 是機率性約束；snapshot / --no-web 再用 Python 硬擋 web todo。
                if _is_web_todo(task) and (freshness_mode == FRESHNESS_SNAPSHOT or not ENABLE_WEB_SEARCH):
                    _trace(f"replan: 拒絕不符合時間模式的 web todo → {task!r}")
                    continue
                scope, gaps = _build_todo_temporal_scope(task, freshness_mode)
                todos.append({
                    "id": next_id,
                    "task": task,
                    "temporal_scope": scope,
                    "freshness_gaps": gaps,
                    "web_used": False,
                    "status": "pending",
                    "result": "",
                })
                next_id += 1
    if has_pending_dependent:   # 第二跳未跑 → 一律不收斂,強制回 execute 把它做完
        sufficient = False
    if sufficient:
        for t in todos:
            if t["status"] == "pending":
                t["status"] = "dropped"
    _trace(f"replan: sufficient={sufficient} "
           f"todos={[(t['id'], t['status']) for t in todos]}")
    return {"todos": todos, "sufficient": sufficient}


def _route_after_replan(state: SupervisorState) -> str:
    """還有 pending 待辦且未達迭代上限 → 回 execute；否則 → synthesize。"""
    if state.get("iterations", 0) >= MAX_ITERS:
        return "synthesize"
    if any(t["status"] == "pending" for t in state.get("todos", [])):
        return "execute"
    return "synthesize"


def _node_synthesize(state: SupervisorState) -> dict:
    """Joiner（無 agency）：collected 全文（+ web_notes 另標）走生產生成契約單次生成 + validator + reflect。"""
    verbose = state.get("verbose", False)
    collected = state.get("collected", [])
    # 自適應預算：facet 數 = collected 裡實際貢獻的子問題（_subq）桶數（fix 2026-07-29，防 8÷N 稀釋）
    _n_facets = len({c.get("_subq", 0) for c in collected}) if collected else 1
    _budget = _writer_budget(_n_facets)
    chunks = _fair_select(collected, _budget)
    _trace(f"synthesize: facets={_n_facets} writer_budget={_budget} → {len(chunks)} chunks")
    web_notes = state.get("web_notes", [])
    if not chunks and not web_notes:
        _trace("synthesize: collected/web 皆空 → 拒答")
        answer = "I don't have enough information in my knowledge base to answer this."
        if state.get("freshness_mode", FRESHNESS_LIVE) == FRESHNESS_LIVE:
            answer += _format_unresolved_freshness_notice(state.get("todos", []))
        return {"answer": answer}

    extra = ""
    if web_notes:
        extra += ("\n\n=== 補充：網路搜尋結果（非知識庫；若引用請用 [web: 網址] 標註，不要當成 chunk 引用）===\n"
                  + "\n\n".join(web_notes))
    # 有待辦查無足夠佐證（局部結果是降級的「查無」標記）→ 提示 Writer 明說查無、勿臆測
    unmet = [t["task"] for t in state.get("todos", [])
             if t.get("status") == "done" and "查無足夠資料" in (t.get("result") or "")]
    if unmet:
        extra += ("\n\n⚠ 以下子問題在提供的參考資料中找不到足夠佐證，請在答案中明確說明「知識庫中查無足夠資料」，"
                  "不要用背景知識臆測補足:\n" + "\n".join(f"- {u}" for u in unmet))

    # 最終生成整段包 try/except（graph 崩潰降級保證）：這是全 run 最後一步,若 LLM API 此時不穩
    # （見實測 NVIDIA 端連續 504）,不能讓整個 graph.invoke 炸穿、零產出——退回零 LLM 的機械式摘要,
    # 至少保留真實 chunk 內容 + citation。
    try:
        answer = _write_final_answer(state["query"], chunks, GEN_MODEL, extra_user=extra)
        answer = _validate_and_fix_citations(state["query"], answer, chunks, GEN_MODEL, verbose=verbose)
        if state.get("enable_reflection", True) and not answer.startswith("I don't have enough"):
            answer = _reflect_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose)
    except Exception as e:
        _trace(f"synthesize: 最終生成失敗（{e!r}）→ 退回零 LLM 機械式摘要")
        answer = _mechanical_summary(state["query"], chunks) if chunks else \
            "I don't have enough information in my knowledge base to answer this."

    # 確定性單位換算：Rule 11 要 Writer 原樣保留 "$X billion"，這裡由純程式把 billion/million→億
    # 乘算正確（程式算不會錯、零誤報），根治 LLM 的 billion→億 音譯 10x 病（reflect 共用同盲點靠不住）。
    answer = rq.convert_usd_units_to_yi(answer)

    # 時效聲明由 Python 機械式附加，不要求 Writer 自己記得，也不讓 citation validator 把這段
    # collection metadata 誤當成無引用的回答事實。snapshot 的 todos 不會帶 freshness_gaps。
    if state.get("freshness_mode", FRESHNESS_LIVE) == FRESHNESS_LIVE:
        answer = answer.rstrip() + _format_unresolved_freshness_notice(state.get("todos", []))
    return {"answer": answer}


def build_graph():
    """建 LangGraph:plan → execute → replan →[execute | synthesize]（execute↔replan 有界迴圈）。"""
    b = StateGraph(SupervisorState)
    b.add_node("plan", _node_plan)
    b.add_node("execute", _node_execute)
    b.add_node("replan", _node_replan)
    b.add_node("synthesize", _node_synthesize)

    b.add_edge(START, "plan")
    b.add_edge("plan", "execute")
    b.add_edge("execute", "replan")
    b.add_conditional_edges("replan", _route_after_replan,
                            {"execute": "execute", "synthesize": "synthesize"})
    b.add_edge("synthesize", END)
    return b.compile()


# 整個 process 共用一張編譯好的圖(無狀態,狀態全走 invoke 的 state)。
_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


# ──────────────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────────────

def run_agentic(query: str, recursion_limit: int = 100, verbose: bool = False,
                enable_coverage_validator: bool = ENABLE_REFLECTION,
                mode: str | None = None,
                freshness_mode: str = FRESHNESS_LIVE) -> dict:
    """跑一次 LangGraph agentic RAG。回傳 {answer, chunks, sub_queries, messages}。

    參數相容舊介面(eval_agentic.py)：
      enable_coverage_validator → 對應 Reflection 節點開關(--no-validator 會傳 False)。
      mode → 已無 hybrid/summary 之分,保留參數但忽略(舊 CLI 相容)。
      freshness_mode → live=截至今天；snapshot=封閉 KB 評測（最近=collection 最新資料）。
    chunks = 全 run 收進 collected 的聯集(衡量 retrieval recall,供 eval;非 Generator 最終引用那幾個)。"""
    if freshness_mode not in FRESHNESS_MODES:
        raise ValueError(f"freshness_mode 必須是 {sorted(FRESHNESS_MODES)}，收到 {freshness_mode!r}")
    graph = _get_graph()
    init: SupervisorState = {
        "query": query,
        "freshness_mode": freshness_mode,
        "enable_reflection": enable_coverage_validator,
        "verbose": verbose,
    }
    try:
        final = graph.invoke(init, config={"recursion_limit": recursion_limit})
    except Exception as e:
        # graph 崩潰降級保證（沿用舊 deepagents 版哲學）：plan/replan 呼叫 rq.call_llm 未包 try/except
        # （沿用 nv 版既有行為),實測撞過 NVIDIA 端連續 504 把整個 invoke 炸穿——這裡是最後一道保底,
        # 確保「LLM API 暫時不穩」不會讓整支 CLI/eval 崩潰,至少走一次乾淨的生產單發查詢兜底。
        _trace(f"run_agentic: graph.invoke 崩潰（{e!r}）→ 降級走一次生產單發檢索+生成")
        bge_m3, rerank_model, client = _get_models()
        with _quiet():
            chunks_fb, _note = rq.retrieve(query, bge_m3, rerank_model, client,
                                           top_k=WRITER_MAX_CHUNKS, model_name=RETRIEVAL_MODEL,
                                           enable_rewrite=False, full_translate_en=True)
        answer_fb = _fallback_local_summary(query, chunks_fb)
        if freshness_mode == FRESHNESS_LIVE:
            _scope, gaps = _build_todo_temporal_scope(query, freshness_mode)
            answer_fb = answer_fb.rstrip() + _format_unresolved_freshness_notice([{
                "status": "done", "freshness_gaps": gaps, "web_used": False,
            }])
        return {"answer": answer_fb, "chunks": chunks_fb, "sub_queries": [query], "messages": []}

    answer = (final.get("answer") or
              "I don't have enough information in my knowledge base to answer this.")
    collected = final.get("collected", [])
    # 與 synthesize 用同一自適應預算(P0-1)：facet 數 = collected 的 _subq 桶數
    _n_facets = len({c.get("_subq", 0) for c in collected}) if collected else 1
    writer_chunks = _fair_select(collected, _writer_budget(_n_facets))
    todos = final.get("todos", [])
    # 部分拒答揭露:哪些子問題（done 但降級「查無足夠資料」）知識庫沒涵蓋。
    unmet = [t["task"] for t in todos
             if t.get("status") == "done" and "查無足夠資料" in (t.get("result") or "")]

    # 機械式附上真實引用清單(provenance 不靠模型)——拒答不附。
    if writer_chunks and not answer.startswith("I don't have enough"):
        answer = answer.rstrip() + _format_citations(writer_chunks)
        if unmet:
            answer += ("\n\n---\n⚠ 知識庫未涵蓋以下子問題,以上回答未就其提供依據:\n"
                       + "\n".join(f"  - {u}" for u in unmet))

    sub_queries = [t["task"] for t in todos]   # 對映舊回傳鍵（eval 依賴）
    if verbose:
        print(f"[run_agentic] todos={sub_queries}, collected={len(collected)} chunks, "
              f"web_notes={len(final.get('web_notes', []))}")

    return {
        "answer": answer,
        "chunks": collected,
        "sub_queries": sub_queries,
        "messages": [],   # 舊回傳鍵,本版不再有 agent message 列表,保留空 list 供相容
    }


def main() -> None:
    # Windows 主控台預設 cp950,印 emoji/部分中文會 UnicodeEncodeError,強制 UTF-8 + replace 保底。
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Agentic RAG (Supervisor + Subagent + RAG-as-tool) for US tech-stock intelligence")
    ap.add_argument("-q", "--query", help="單次查詢;不給則進互動模式")
    ap.add_argument("-v", "--verbose", action="store_true", help="印節點決策 + 中間輸出")
    ap.add_argument("--no-validator", action="store_true",
                    help="關閉 Reflection 幻覺稽核(省一次 LLM 呼叫;便宜的 regex citation 稽核仍在)")
    ap.add_argument("--no-web", action="store_true",
                    help="關閉 web_search（Tavily）工具，只用知識庫檢索")
    ap.add_argument("--freshness-mode", choices=sorted(FRESHNESS_MODES), default=FRESHNESS_LIVE,
                    help="live=『最近』截至今天；snapshot=『最近』只指 KB snapshot 最新資料")
    ap.add_argument("--trace", action="store_true",
                    help="印 [TRACE]：每個節點的決策(plan 產幾個待辦、execute 工具呼叫與評分、"
                         "replan 增刪待辦)到 stderr(等同 AGENTIC_TRACE=true)")
    args = ap.parse_args()

    global _TRACE, ENABLE_WEB_SEARCH
    if args.trace or args.verbose:
        _TRACE = True
    if args.no_web:
        ENABLE_WEB_SEARCH = False

    _get_models()  # 提前載入重模型
    validator_on = ENABLE_REFLECTION and not args.no_validator

    if args.query:
        out = run_agentic(args.query, verbose=args.verbose, enable_coverage_validator=validator_on,
                          freshness_mode=args.freshness_mode)
        print("\n" + "═" * 60)
        print("💡 Final Answer:\n")
        print(out["answer"])
        print(f"\n🧩 Sub-queries: {out['sub_queries']}")
        print(f"📚 Chunks used across run: {len(out['chunks'])}")
        print("═" * 60)
        return

    print("\n🚀 Agentic RAG (LangGraph) — 輸入問題(exit 離開)")
    while True:
        try:
            q = input("\n❓ > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("exit", "quit", ""):
            break
        out = run_agentic(q, verbose=args.verbose, enable_coverage_validator=validator_on,
                          freshness_mode=args.freshness_mode)
        print("\n💡 Final Answer:\n")
        print(out["answer"])
        print(f"\n🧩 Sub-queries: {out['sub_queries']}")
        print(f"📚 Chunks used: {len(out['chunks'])}")


if __name__ == "__main__":
    main()
