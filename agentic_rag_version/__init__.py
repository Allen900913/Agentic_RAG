"""
agentic_rag_version — Supervisor + Subagent + RAG-as-Tool 版 agentic RAG。

⚠ 2026-09-03 套件化：本檔原本是單一檔案 `agentic_rag_v2.py`（4549 行），已整個搬成
`agentic_rag_version/__init__.py`，正在逐群拆成子模組。**搬移那一步刻意零語意變更**
（同一個模組命名空間、同一份 globals），這樣四道閘門的 512 項就是搬移正確性的證明。

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
import calendar
import contextlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import TypedDict
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(override=True)

import llm_replay as _replay
import web_replay as _web_replay      # Tavily 原始回應的錄／放（未設 env 時 no-op）
import rag_query as rq

# ── 已拆出去的子模組（2026-09-03 套件化）──────────────────────────────────────
# ⚠ 這裡刻意用**逐名 import** 而不是 `import *`：底線開頭的名字 `*` 不會帶出來，
#   而這個模組的公開介面幾乎全是底線名（eval 與閘門直接吃 `ar._xxx`）。
# CHECKER_MODEL：planner 拆解 / sufficiency 判斷 / reflection 幻覺稽核共用（要結構化 JSON 可靠 + 快）。
# gpt-oss-120b：tool-call/結構化輸出快又合法、無下架風險。
CHECKER_MODEL   = os.getenv("AGENTIC_CHECKER_MODEL", "nvidia/nemotron-3-super-120b-a12b")  # ⚠ 2026-09-03 換：gpt-oss-120b 被 NVIDIA 退役（410 Gone）。選型見 experiments/_model_bakeoff_20260903.log

POOL_RETURN_K     = int(os.getenv("AGENTIC_POOL_RETURN_K", "5"))   # 餵給 Checker 看的候選片段數（top-k by rerank）。env 可覆蓋供 ablation。

# ⚠ `_TRACE` 這份是 **import 當下的快照**，留著只為了閘門⑬f（子模組的 top-level 名字全部要
#   re-export 得到）。**任何地方都不准讀它、更不准重綁它**：要判斷 trace 開沒開讀
#   `tracing._TRACE`，要開關一律呼叫 `set_trace_enabled()`。理由見 `tracing.set_trace_enabled`
#   的 docstring（重綁這一份 ＝ 旗標靜默失效）。守門在閘門㉓g。
from .tracing import _TRACE, _trace, set_trace_enabled   # noqa: E402
from .retrieval import (   # noqa: E402
    _COVERAGE_SOURCE_RE,
    WRITER_BUDGET_BASE,
    WRITER_BUDGET_PER_FACET,
    WRITER_BUDGET_CAP,
    _CJK_RUN_RE,
    _query_terms,
    _best_window_start,
    _snippet,
    _chunk_id,
    _merge_chunks,
    _fair_select,
    _writer_budget,
    _coverage_lock,
    _kb_coverage,
    _kb_coverage_collection,
    _source_coverage_parts,
    _coverage_sort_key,
    _scan_kb_coverage,
    _get_kb_coverage,
    _display_date,
    _coverage_record_text,
    _format_kb_coverage,
    _mentioned_tickers,
    RETRIEVAL_MODEL,
    _RATIO_INTENT_RE,
    _RATIO_FIELD_HINTS,
    _wanted_ratio_fields,
    _is_ratio_intent,
    _RATIO_FIELD_ENUM,
    _RATIO_INTENT_PROMPT,
    _classify_ratio_fields,
    _resolve_ratio_fields,
    _has_ratio_intent,
    _todos_ratio_fields,
    _fetch_fundamentals_with_field,
    _ensure_ratio_source_coverage,
    _BACKREF_RE,
    _DEP_PLACEHOLDER_RE,
    _TICKER_CANON,
)
from .validators import (   # noqa: E402
    TAVILY_MAX_RESULTS,
    TAVILY_FETCH_RESULTS,
    TAVILY_PER_DOMAIN_CAP,
    _WEB_DOMAINS_PRIMARY,
    _WEB_DOMAINS_PRESS,
    WEB_ALLOWED_DOMAINS,
    _domain_admits,
    REALTIME_STALE_DAYS,
    _domain_of,
    WEB_STALE_DAYS,
    _URL_DATE_PATTERNS,
    _url_published_date,
    _CONTENT_DATE_MARKER,
    _MARKER_WINDOW,
    _SECTION_BREAK,
    _MONTHS,
    _CONTENT_DATE_PATTERNS,
    _content_published_date,
    _web_result_date,
    _normalize_url,
    _DERIVATIVE_URL_RE,
    _is_derivative_page,
    _host_allowed,
    _dedupe_web_results,
    _source_newest_date,
    NO_REALTIME_SOURCE,
    _is_freshness_evidence,
    _stale_for_realtime,
    _FISCAL_PERIOD_ORDER,
    _FISCAL_PERIOD_LABEL,
    _fiscal_rank,
    _fiscal_label,
    _kb_ceiling_date,
    _classify_staleness,
    _get_as_of_date,
    _CITE_RE,
    _REF_PREFIX_RE,
    _BARE_REF_CITE_RE,
    _deterministic_defects,
    _extract_citations,
    _repair_reference_citations,
    _parse_period_months,
    _PERIOD_END_RE,
    _parse_period_end,
    _period_key,
    _MONTH_WORDS,
    _MONTH_NUM,
    _PERIOD_TAG_RE,
    _chunk_period,
    _ground_period_from_source,
    AUTHORITATIVE_TYPES,
    _ground_source_type,
    _WEB_MARK,
    _KB_MARK_RE,
    _WEB_CITE_ANY_RE,
    _KB_CITE_ANY_RE,
    _CAL_DATE_RE,
    _FISCAL_MARK_RE,
    _DUAL_SENT_SPLIT_RE,
    _DASH_CHARS,
    _DASH_TRANS,
    _MONEY_RE,
    _MONEY_SCALE,
    _money_in_millions,
    find_undated_dual_sourcing,
    _WEB_UNCITED_MARK,
    web_fetched_but_uncited_notice,
    find_unreconciled_web_conflicts,
    find_authority_conflicts,
    find_claim_conflicts,
    _CONSIST_REVISE_SUFFIX,
    _LATEST_CLAIM_RE,
    _filing_kind,
    find_stale_period_claims,
    _PERIOD_REVISE_SUFFIX,
    _TRACEABLE_NUM_RE,
    find_untraceable_numbers,
    _NUMBER_REVISE_SUFFIX,
    _DUAL_SOURCE_REVISE_SUFFIX,
    FRESHNESS_SNAPSHOT,
    _has_unresolved_anchor,
    _is_dependent_hop,
    _resolve_hop_entity,
    _fill_dependent_hop,
    _build_temporal_contract,
    _build_todo_temporal_scope,
    _news_freshness_gaps,
    _unmet_realtime_gaps,
    _unfulfilled_web_route_gaps,
    _BASIS_NOTICE_MARK,
    _EXPLICIT_FY_RE,
    _FUND_PCT_TMPL,
    _ttm_field_values,
    _value_stated,
    _basis_disclosure_notice,
    _format_unresolved_freshness_notice,
)
from .tools import (   # noqa: E402
    WEB_CONTENT_CHARS,
    WEB_SEARCH_MAX_CALLS,
    RAG_SEARCH_MAX_CALLS,
    _RunState,
    _run_state_var,
    QUERY_WEB_BUDGET,
    _query_web_budget_lock,
    _query_web_calls,
    _reset_query_web_budget,
    _take_query_web_budget,
    _current_run_state,
    _reset_run_pool,
    _web_query_en,
    _tavily_raw,
    _NoTavilyKey,
    _tavily_search,
    rag_search,
    web_search,
    _subagent_apps,
)
from .graph import (   # noqa: E402
    MAX_SUBQUERIES,
    FRESHNESS_LIVE,
    VALID_ROUTES,
    _ROUTE_LEGACY_DEFAULT,
    _ROUTE_UNCERTAIN_DEFAULT,
    _loads_json_lenient,
    _coerce_bool,
    _PLANNER_PROMPT,
    _parse_plan_output,
    validate_plan_dependencies,
    _plan_subqueries,
    _CHECKER_PROMPT,
    _CHECKER_LIVE_RECENCY_BLOCK,
    _check_sufficiency,
    GEN_MODEL,
    MAX_REWRITES,
    WRITER_MAX_CHUNKS,
    USE_REACT_EXECUTOR,
    SUBAGENT_MODEL,
    SUBAGENT_RECURSION_LIMIT,
    _get_subagent,
    _RESEARCHER_LOCKED_PROMPT,
    _last_ai_text,
    _mechanical_summary,
    _fallback_local_summary,
    _run_executor_react,
    _effective_route,
    _escalate_route,
    _dispatch_todo,
    _EXEC_OUTCOMES,
    _note_exec_outcome,
    _merge_exec_stats,
    _merge_pool_keys,
    _run_executor_deterministic,
    _run_executor,
    COMMIT_TOP_K,
    MAX_ITERS,
    MAX_TODOS,
    AGENTIC_PIPELINE_WORKERS,
    SupervisorState,
    _REPLANNER_PROMPT,
    _REPLANNER_LIVE_BLOCK,
    _web_retry_is_pointless,
    _node_plan,
    _run_one_todo,
    _node_execute,
    _node_replan,
    _route_after_replan,
    _node_synthesize,
    build_graph,
    _GRAPH,
    _get_graph,
)


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


RERANK_GATE_TAU   = 0.5   # rerank_score(sigmoid) 門檻：僅供 trace/參考；夠不夠的最終判斷交給 Checker。

# web_search（Tavily）開關；--no-web 或此環境變數關掉。TAVILY_API_KEY 由 .env 提供，缺 key 走 graceful 降級。
ENABLE_WEB_SEARCH = os.getenv("AGENTIC_WEB_SEARCH", "true").lower() not in ("false", "0", "no")

FRESHNESS_MODES = {FRESHNESS_LIVE, FRESHNESS_SNAPSHOT}

# 預設是否開 Reflection（LLM 幻覺稽核）。eval 端 --no-validator 會把它關掉（沿用舊 CLI 參數名）。
ENABLE_REFLECTION = os.getenv("AGENTIC_REFLECTION", "true").lower() not in ("false", "0", "no")

CITATION_VALIDATOR_MAX_RETRIES = 1  # citation 不合格時丟回 Generator 重寫的上限。
REFLECTION_MAX_REVISIONS       = 1  # reflection 抓到幻覺後重生成的上限。




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

# ⚠ `_RELATIVE_TIME_RE` 已於 2026-08-15 **從碼上刪除**（2026-08-13 只從 web 觸發拿掉，時效警語那條
#   漏改，文件卻已寫成「全數移除」——漂移了兩天）。留這段是因為它在多處註解／CHANGELOG 被引用：
#   它曾是 `最近|最新|近期|目前|現在|截至今天|今日|latest|recent|current|today` 的詞表，
#   實測 18 個真實時效措辭漏 10 個。要判「這題要不要即時資料」請看 Grader 的 `realtime_need`，
#   要判「這次答案有沒有用到新聞」請看 `_news_freshness_gaps`——兩者都不靠字串比對。
# ⚠ `_WEB_TODO_RE` / `_is_web_todo` 已於 2026-09-02 **從碼上刪除**（路由改成 todo 的 `route`
#   欄位之後，最後一個呼叫端——`_node_replan` 的 snapshot web-todo 拒絕——換成讀 route）。
#   留這段是因為它在多處註解／CHANGELOG／閘門⑧p 被引用：它曾是
#   `網路|上網|web search|internet` 的詞表，而 replan 實際生出的是「在 Yahoo Finance 上查詢」
#   「使用NASDAQ官方網站」——**一個都不匹配**（六個真實措辭逐字凍結在閘門⑧p）。
#   它是「用硬編碼詞表做感知」的第三個死者（前兩個：`rq.looks_like_news_query`、
#   `_RELATIVE_TIME_RE`）。要判「這個待辦該不該上網」請讀 `route` 欄位。



# 時效需求分級 → 容忍幾天（2026-08-13）。**分工**：「這題要多新」沒有唯一機械答案 → 交給 Grader
# 判（`realtime_need` 欄位，只在 live 模式問）；「來源多舊」是確定性的 → 交給 Python 從檔名算。
# 單一天數門檻行不通：「今天股價漲跌」容忍 1 天，「最近有什麼進展」容忍一週，兩者差一個數量級。













def _retrieve_chunks(query: str, *, attributable: bool) -> list[dict]:
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
    補救迭代與首輪用完全相同組態,唯一差別是 active_query 已被 Checker 改寫(補救的主力施力點)。

    ⚠ **第二個回傳值(期間降級揭露)必須收下,不可以丟掉**(2026-08-28 修)：`rq.retrieve` 在 Tier 1
    嚴格 filter 落空、降級到 Tier 2 時會產生一句「知識庫沒有 X 在所詢問財年(Y)的資料,以下回答改用
    最接近的可得期間(Z)」。單管線兩個消費端都有(CLI `print` / SSE 顯示 ＋ 注入 generator prompt),
    而這裡舊寫法是 `chunks, _note = ...` **直接丟棄** → agentic 這條路上「系統會不會揭露」
    結構性恆為 0,量到的 0 是在覆述一行程式碼。收進 run state 的理由見 `_RunState` docstring:
    這個函式有五個呼叫點(ReAct 工具、確定性迴圈、兩處例外降級),逐個回傳會有人漏接。

    ⚠ **`attributable` 是必填的,不給預設值**(2026-08-28 修 col-10)：揭露句的字面是「知識庫沒有
    X 在**所詢問**財年(Y)的資料」,而那個 Y 是從**這一次 retrieve 的 query**解析出來的 filter。
    在單管線那永遠成立(query 就是使用者原句),在 agentic 不成立——query 可能已被機器改寫兩次。
    實測 col-10:使用者問「微軟每年能自由運用的現金大概有多少?」、Planner 分解成「Microsoft 每年的
    自由現金流大概是多少」,**兩者都沒有年份也沒有 10-K**,而揭露句卻說「MSFT 10-K 在所詢問財年
    (2022)」——那個 2022 是 Grader 補救改寫加進去的。
    分界線不是輪次而是**意圖歸屬**:Planner 的分解是使用者意圖的重述(可歸因);Grader 的 targeted
    rewrite 被 `_CHECKER_PROMPT` **明令**去加使用者沒說過的具體詞(「換措辭或用更具體的關鍵字 /
    實體 / 財報標準術語」),ReAct 的 `rag_search` 同理(不可歸因)。
    不可歸因的 note 直接丟棄而不是改措辭:那個期間約束本來就不是使用者問的,講出來只是噪音。"""
    bge_m3, rerank_model, client = _get_models()
    with _RETRIEVE_LOCK:
        with _quiet():   # 靜音 retrieve 的 DEBUG print
            chunks, period_note = rq.retrieve(
                query, bge_m3, rerank_model, client,
                top_k=rq.RERANK_INPUT_N,   # 多取一些進池(池會重排,advance 時只收 top-k)
                model_name=RETRIEVAL_MODEL,
                enable_rewrite=False,
                full_translate_en=True,   # 檢索中間層一律英文(見上方 docstring)
            )
    if period_note and not attributable:
        # 期間約束來自機器改寫、不是使用者問的 → 丟棄(理由見 docstring)。仍然印 trace:
        # 「揭露句被產出但被丟掉」與「根本沒產出」是兩件事,診斷時要分得開。
        _trace(f"retrieve: 期間降級揭露(不可歸因,丟棄) → {period_note}")
    elif period_note:
        notes = _current_run_state().period_notes
        if period_note not in notes:   # 同一子問題的補救輪次會重複產生同一句,去重但保序
            notes.append(period_note)
            _trace(f"retrieve: 期間降級揭露 → {period_note}")
    return chunks or []


# ──────────────────────────────────────────────────────────────────────────────
# Generator（無 agency）：Python 拿 collected 全文走生產生成契約,單次生成。
# 「生成」不在 agent 迴圈裡,是消除「跳過生成 / 背景知識幻覺 / 弄丟 citation」的結構性關鍵。
# ──────────────────────────────────────────────────────────────────────────────

# agentic 輸出語言硬約束(繁中產品定位):整段敘述繁體中文,但依 Rule 11 保留來源 "$X billion/million"
# 單位詞與英文術語/代號原文不改(數字後續由 rq.convert_usd_units_to_yi 雙寫成「億(＄X billion)」)。
# ⚠ 2026-09-03 起**唯一定義點在 `rq.ZH_ANSWER_DIRECTIVE`**：單發管線也需要同一段文字
#   （它原本沒附 → 換模型後整篇英文）。這裡保留這個名字是因為 eval 直接讀它
#   （`verify_web_gate_isolation` 閘門③ 的 baseline ＝ `rq.SYSTEM_PROMPT + ar._ZH_ANSWER_DIRECTIVE`）。
_ZH_ANSWER_DIRECTIVE = rq.ZH_ANSWER_DIRECTIVE


# 只在**真的有 web 結果時**才附加到 system message 的修訂條款（2026-08-13）。
#
# 為什麼一定要在 system 而不是 user：`rq.SYSTEM_PROMPT` 的 Rule 1「Answer ONLY based on the provided
# reference materials」、Rule 2「後接 [filename, chunk #N]」、Rule 8「Every number you state must be
# traceable to a cited chunk」都在 system message。在 user message 寫「web 內容可引用」是拿 user turn
# 去赦免 system rule，權重較低——**實測失敗過**：舊版 web 區塊已寫「若引用請用 [web: 網址] 標註」，
# 三次跑分仍全數退回 6 月快照的 $4,962.16B，甚至寧可拿舊市值除股數捏造「每股 $204」（違反 Rule 8
# 前半句「Never invent」）也不碰那個不可引用的數字。所以缺的從來不是格式，是**許可**。
#
# 同時補上時間契約：Planner(_PLANNER_PROMPT)、Replanner、Researcher 都掛了 _build_temporal_contract，
# **唯獨 Generator 漏接**。不是「LLM 沒有時鐘」，是這個系統造好了契約卻少接一個呼叫點。
_WEB_SOURCE_AMENDMENT = """

=== 本次生成的規則修訂（僅適用於下方標示為「網路搜尋結果」的區塊）===
使用者訊息中的「網路搜尋結果」區塊來自即時網路檢索，已通過來源白名單過濾。針對該區塊：
- Rule 1 修訂：該區塊**屬於** provided reference materials，可以據以作答。
- Rule 2／Rule 8 修訂：該區塊的內容用 `[web: 網址]` 標註出處即**視為已滿足引用要求**，
  不需要也不應該替它編造 `[filename, chunk #N]`。「traceable to a cited chunk」對該區塊
  等價於「traceable to a cited web URL」。
- 每則結果標了「（發布日 YYYY-MM-DD）」或「（未標示日期）」。⚠ 不要因為某則被放進
  「網路搜尋結果」就當它是今天的——網頁可能是好幾年前的報導。日期的用法分兩種情況：
  - 問**當下的數值**（今天股價、即時市值、現在的本益比）：**「（未標示日期）」的行情／統計頁
    才是當前值**，優先採用；標了日期的報導講的是**那一天**發生的事，即使只差幾天也不能拿來
    當今天的數值，只能當背景並註明日期。
  - 問**近期發展或歷史事件**：以標示日期**最新**的來源為準，並在答案裡寫出那個日期。
- 只有「（未標示日期）」可以當當前值；但**不得**拿它當某個歷史里程碑事件的日期依據。
- 與財報 chunk 數字不一致時，那通常是時間點不同，不是矛盾——請並列說明兩者各自的時點。
- Rule 8 的「Never invent」不變：仍然不得自行推算或估計任何來源沒有明說的數字。"""


def _write_final_answer(query: str, chunks: list[dict], model_name: str, extra_user: str = "",
                        web_extra: str = "", period_note: str = "") -> str:
    """單次 Generator 呼叫:把 chunks 全文塞進生產生成契約產出答案。extra_user 夾帶糾錯指示(重生成用)。

    `web_extra`(網路搜尋區塊)刻意獨立於 `extra_user`:它要同時進 **system**(修訂引用契約 + 補時間契約)
    與 **user**(內容本身),且必須跟著**每一次重生成**走。web_extra 為空時 system message 與 2026-08-13
    以前**逐字相同**——eval 的 web_notes 恆為空,所以既有基準一個都不會動到(由
    `eval/verify_web_gate_isolation.py` 的 byte-identical 斷言長期把關)。

    `period_note`(期間降級揭露)沿用 `rq.build_user_prompt` 的第三參數,**與單管線同一個模板、
    同一個位置**——它不是新的措辭,是把單管線早就有的東西接到 agentic 上(見 `_retrieve_chunks`)。
    ⚠ 它跟 `web_extra` 一樣**必須跟著每一次重生成走**：validator 的重生成會換掉 `extra_user`,
    寫在那裡的話揭露只活在第一次生成,任何一次重寫都會讓它消失(web_extra 踩過這個坑)。
    ⚠ 只進 **user** 不進 system:它是這一題的檢索事實,不是契約修訂——所以 system message 逐字不變。
    period_note 為空時 `build_user_prompt` 的 `note_section` 是空字串 → user prompt 也逐字不變。
    """
    system = rq.SYSTEM_PROMPT + _ZH_ANSWER_DIRECTIVE
    if web_extra:
        # 用 _get_as_of_date() 而非 date.today():AGENTIC_AS_OF_DATE 要能固定 wall clock 供回歸測試。
        system += _WEB_SOURCE_AMENDMENT + "\n\n" + _build_temporal_contract(FRESHNESS_LIVE)
    user_prompt = rq.build_user_prompt(query, chunks, period_note) + web_extra + extra_user
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    with _quiet():
        return rq.call_llm(messages, model_name, temperature=rq.GEN_TEMPERATURE)


# ──────────────────────────────────────────────────────────────────────────────
# 確定性 citation validator（非 LLM,便宜的一層）：regex 抽 Writer 引用比對 allowlist,
# 抓 ① 完全沒有效引用 ② 捏造 allowlist 外的 ID。不合格→丟回 Writer 重寫(上限 1)。
# 保證「引用指向存在的 chunk」,不保證「句子忠於該 chunk」(faithfulness 交給 reflect / eval)。
# ──────────────────────────────────────────────────────────────────────────────



def _validate_and_fix_citations(query: str, answer: str, allowed_chunks: list[dict],
                                 model_name: str, verbose: bool = False,
                                 web_extra: str = "", period_note: str = "") -> str:
    """確定性 citation 稽核 + 有界重試。回傳(盡量)合格的答案。

    `web_extra`＝synthesize 組出來的網路搜尋補充區塊（含 `[web: 網址]` 標註規則）。
    ⚠ 2026-08-13 新增：**重生成時必須把它一起帶回去**，否則 web 拿到的即時資料只活在第一次
    生成，任何一次 validator 重寫都會讓它消失（實測 Q4「NVIDIA 目前股價與市值」三次跑分，
    Tavily 每次都帶回 `$5.27T`，最終答案卻三次都退回 6 月快照的 `$4,962.16B`）。
    `[web: 網址]` 本身不會被 `_CITE_RE` 當成引用（它要求 `, chunk #N`），所以不會誤判成捏造 ID。
    """
    allowed = {(c["source"], c["chunk_index"]) for c in allowed_chunks}
    if not allowed:
        return answer
    for attempt in range(CITATION_VALIDATOR_MAX_RETRIES + 1):
        # 確定性修補先跑：能還原的就不必浪費一次 LLM 重寫（也不必賭重寫會變好）。
        answer, _n_fix, _n_skip = _repair_reference_citations(answer, allowed_chunks)
        if _n_fix or _n_skip:
            _trace(f"citation repair: 還原 {_n_fix} 筆 Reference 序號引用"
                   f"（守門條件擋下 {_n_skip} 筆，維持原樣走重寫）")
        cited = _extract_citations(answer)
        hallucinated = cited - allowed
        has_valid = bool(cited & allowed)
        if has_valid and not hallucinated:
            return answer
        if attempt >= CITATION_VALIDATOR_MAX_RETRIES:
            if verbose:
                # ⚠ 這裡**沒有**任何機械式收尾：修不掉的引用維持原樣出貨。
                #   2026-08-20 之前這行寫的是「走機械式收尾」，而程式裡從來沒有——
                #   註解描述一個不存在的行為比沒有註解更糟，所以改成照實說。
                print(f"--- citation validator: 重試耗盡,維持現狀出貨"
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
        revised = _write_final_answer(query, allowed_chunks, model_name,
                                      extra_user=suffix, web_extra=web_extra,
                                      period_note=period_note)
        if revised and revised.strip():
            answer = revised
    return answer


# ──────────────────────────────────────────────────────────────────────────────
# 一致性稽核：抓「同一主體同一指標,答案裡並陳兩組互斥數值卻不調和」。
# 2026-08-07 雙臂 100 題實測到的真實病灶——Writer 抓錯運算元的「範圍/期間」,而且它自己
# 往往同時把正確那組也寫進答案:
#   mix-06 拿「Six Months 2025」欄當 Q2 2026 算出 +$12,260M,同一段又抄了來源的「+$11.1B」;
#   mix-03 把 Intelligent Cloud 單一部門的 +$2.7B/24% 當成全公司,又提了九個月的 +$20.4B/22%。
# 靜默選錯運算元、答案裡只有一組數字的情形,這層抓不到,交給 reflect / eval。
#
# **分工＝LLM 抽取、Python 判斷**：`_extract_claims` 把每個數值宣稱抽成結構化欄位,
# `find_claim_conflicts` 跑零 LLM 的確定性規則。分界線畫在能力邊界上——抽取是感知任務,
# 比對是邏輯任務（本管線 Plan=LLM／Execute=確定性 就是同一個分工）。
#
# 2026-08-08 移除了 regex 降級路徑（`find_numeric_conflicts` 與 `_CONSIST_*` 詞表）。
# 它是用字串比對做**感知**：指標靠寫死的詞表（「資料中心營收」裡也有「營收」）、主體靠
# 寫死的公司/分部清單（不在清單就抽成空）、並陳靠寫死的措辭清單——每加一家公司、每換一種
# 說法就要改表。它已經被實測抓包過：col-11 漏抓的原因就是「另一份報告則顯示」不在措辭清單裡。
# 而它作為 fallback 的觸發條件是「LLM 抽取失敗」,離線量測 200 份答案的失敗率是 **0**——
# 等於一段從未在生產跑過、卻要永久維護的降級碼。現在抽取失敗就跳過一致性檢查（本來就沒
# 檢查,不會更差),少一條沒被測過的路徑。
# ──────────────────────────────────────────────────────────────────────────────


# ── 結構化宣稱抽取（LLM 只做抽取,判斷全交給下面的確定性規則）─────────────────────
# 抽成結構化欄位後多拿到一個「逐句 regex」結構上做不到的檢查：**分部加總 vs 合併總計**（R2）。
_CLAIM_EXTRACT_PROMPT = """你是財務數據抽取器。把「答案」裡每一個可比較的數值宣稱抽成 JSON 陣列。
只抽答案裡真的出現的數字,不要自己推算、不要補充答案沒寫的東西。

每個元素的欄位:
- "value": 數值。金額一律換算成**百萬美元**（1 億美元 = 100；$1 billion = 1000）；百分比就給百分數本身（82 不是 0.82）
- "unit": "USD_M" 或 "percent"
- "metric": 指標,用答案的用詞（如 "營收"、"營業利益"、"淨利"、"毛利率"）
- "entity": 這個數字屬於誰,照答案寫法（如 "Microsoft"、"Google Cloud"、"Intelligent Cloud"）。答案沒指明就填 ""
- "scope": "consolidated"=全公司/合併總計, "segment"=單一部門或產品線, "unknown"=看不出來
- "period_months": 期間**長度**的月數,只能填 3 / 6 / 9 / 12 其中之一。
  原文 "Three Months Ended"→3、"Six Months"→6、"Nine Months"→9、"Twelve Months"或全年→12。
  看不出來填 null。**不要自己換算成財年季度代碼**——那是最容易錯的一步。
- "period_end": 期間**截止**的年月,格式 "YYYY-MM"（如 "Ended March 31, 2026" → "2026-03"）。看不出來填 null
- "period": 期間的人類可讀寫法,用 "2026Q2"/"2026H1"/"2026YTD"/"FY2025"/"TTM" 這類;看不出來填 "unknown"。
  **這欄只供人閱讀,判斷同不同期間是用上面兩欄**,所以上面兩欄填得出來時務必填
- "kind": "level"=水準值(某期間的金額), "change"=變化量(增加/減少了多少), "growth_pct"=成長率
- "basis": 比較基準。"yoy"=年增(較去年同期), "qoq"=季增/環比, "cc"=固定匯率, "other"=其他或不適用
- "quote": 答案裡對應的原句片段（30 字內,供人工複查）。**同一句話拆出的多筆宣稱要填同一段 quote**
- "period_evidence": 你憑什麼填上面那個 period_months/period_end。附了原文時,**直接抄原文裡那句期間標題**
  （如 "Three Months Ended March 31, 2026 Compared with Three Months Ended March 31, 2025"）。
  找不到原文出處就填 "answer"（代表只依答案的說法）。沒有附原文時一律填 "answer"

判定要點:
- 「從 A 增至 B,增加 C」要抽成三筆：兩筆 level（各自的 period 不同）+ 一筆 change。
- 分辨 scope 很重要：「整體/公司/合併」是 consolidated;「XX 部門/業務/產品線」是 segment。
- 分辨 period 很重要：單季（三個月）和累計（六個月/九個月/YTD/全年）**不是同一個期間**。

附了「原文」段落時,**期間一律以原文為準,不以答案的說法為準**。答案很可能標錯期間——
10-Q 把單季與累計寫成相鄰兩節,期間只寫在節標題,寫答案的模型常把兩節的數字混用並套上同一個期間。
所以**不要相信答案括號裡的期間**,一定要逐個數字執行：

1. 拿這個數字回原文搜,找出它出現在哪一段。
2. 讀那一段自己的期間標題（"[Three Months Ended March 31, 2026 vs …]"、
   "Nine Months Ended … Compared with …"）,照它填 period_months / period_end,
   並把該標題原文抄進 period_evidence。
3. 原文標的期間與答案括號裡寫的不一致時 → **以原文為準**,並在 quote 開頭加 "[答案期間有誤] "。
4. 原文搜不到這個數字 → period_months / period_end 填 null、period 填 "unknown"、
   period_evidence 填 "answer"。不要拿鄰近段落的期間硬套。

注意：同一段答案文字裡連續出現的兩個數字,**很可能來自原文的不同期間段**（一個單季一個累計）。
逐個查,不要因為它們寫在一起就給同一個期間。
原文**只用來查證**,不要從原文抽出答案沒提到的新數字。

只輸出 JSON 陣列,不要任何其他文字。抽不到任何數值宣稱就輸出 []。"""




def _extract_claims(answer: str, model_name: str,
                    chunks: list[dict] | None = None) -> list[dict] | None:
    """呼叫 LLM 把答案抽成結構化宣稱。解析失敗回 None（呼叫端據此跳過一致性檢查）。

    chunks 有給就一併餵原文,period/scope 改以原文標示為準。
    只看答案是有天花板的：答案讀不出期間時只能填 unknown,答案標錯期間時只會忠實照抄
    （col-11 實測：19% 其實是單季,答案寫成「九個月期間」,抽取層照抄成兩筆同期 → 誤報互斥）。
    要糾正必須回讀原文,而原文得先在 ingest 期保住期間標籤（見 CLAUDE.md「期間章節邊界」）。
    """
    if chunks:
        src = "\n\n".join(
            f"[{c['source']} #{c['chunk_index']}]\n{(c.get('content') or '').strip()}"
            for c in chunks)
        user = f"原文:\n{src}\n\n答案:\n{answer}"
    else:
        user = f"答案:\n{answer}"
    messages = [
        {"role": "system", "content": _CLAIM_EXTRACT_PROMPT},
        {"role": "user", "content": user},
    ]
    with _quiet():
        raw = rq.call_llm(messages, model_name, temperature=0.0)
    data = _loads_json_lenient(raw)
    if not isinstance(data, list):
        return None
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        try:
            v = float(str(d.get("value", "")).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            continue
        c = {"value": v, "unit": d.get("unit") or "USD_M",
             "metric": (d.get("metric") or "").strip(),
             "entity": (d.get("entity") or "").strip(),
             "scope": (d.get("scope") or "unknown").strip(),
             "period": (d.get("period") or "unknown").strip(),
             "period_months": _parse_period_months(d.get("period_months")),
             "period_end": _parse_period_end(d.get("period_end")),
             "kind": (d.get("kind") or "").strip(),
             "basis": (d.get("basis") or "other").strip(),
             "quote": (d.get("quote") or "").strip()[:60],
             "period_evidence": (d.get("period_evidence") or "answer").strip()[:120]}
        out.append(c)
    if chunks:
        _ground_period_from_source(out, chunks)
    for c in out:
        c["period_key"] = _period_key(c)
    return out


_WS_RE = re.compile(r"\s+")


def _accept_revision(original: str, revised: str, chunks: list[dict], web_extra: str,
                     *, tag: str, stats: dict | None = None) -> str:
    """重生成的**回歸守衛**：零 LLM 重算缺陷指紋，**任何一格變差就退回原答案**。

    **為什麼是單向守衛而不是 DeCRIM 那種 critique↔refine 迴圈**：迴圈的每一輪 refine 都要一次
    LLM 呼叫，而本管線一題已經燒 50~60 次；守衛則是**零成本**（critique 側本來就是零 LLM 的
    偵測器）。取捨是：守衛保證「不會更糟」，但不會把剩下的缺陷修掉——那本來就是下一道
    validator 的工作，而它們在同一條鏈上排在後面。

    ⚠ **判準是「任何一格變差」不是「總數變多」**：四格是四種不同的病，加總會讓
      「修好一個期別錯、引入一個捏造數字」看起來持平——而那兩者的嚴重度不對稱。
    ⚠ **相等就接受**：重生成常常只改措辭。以「沒變好就退回」當判準會把整條修補鏈關掉
      （閘門㉑c/㉑d 是那兩條誤報對照）。
    ⚠ **空的重生成不算退回**（`empty`）：那是 LLM 呼叫失敗，與「改壞了」是兩種病，
      合併成一個數字就再也分不出來（同 ⑲l4／⑭d 的形狀）。
    ⚠ `stats` 走 out-param 不改回傳型別：五個呼叫端，改成 tuple 會讓漏改的那個安靜地
      把 tuple 當字串接（`rq.finalize_answer_units` 的 `stats` 就是這個理由）。
    """
    if stats is not None:
        for k, v in (("accepted", 0), ("rejected", 0), ("empty", 0), ("rejected_detail", [])):
            stats.setdefault(k, v)
    if not (revised or "").strip():
        if stats is not None:
            stats["empty"] += 1
        return original
    before = _deterministic_defects(original, chunks, web_extra)
    after = _deterministic_defects(revised, chunks, web_extra)
    worse = [k for k in after if after[k] > before[k]]
    if worse:
        detail = f"{tag}: " + "、".join(f"{k} {before[k]}→{after[k]}" for k in worse)
        _trace(f"revision-guard: {detail} → 退回重生成前的答案")
        if stats is not None:
            stats["rejected"] += 1
            stats["rejected_detail"].append(detail)
        return original
    if stats is not None:
        stats["accepted"] += 1
    return revised


def _consistency_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                               verbose: bool = False, web_extra: str = "", period_note: str = "",
                     rev_stats: dict | None = None) -> str:
    """一致性稽核 → 有衝突則帶問題重生成一次 → 再過 citation 稽核。無衝突原樣回傳。

    `web_extra` 只在**重生成**時帶回（見 `_validate_and_fix_citations` 的說明）。
    ⚠ **2026-08-19 修正了這段原本的說明**：舊版寫「把 web 併進去會改變 R3 的語意，故不在此改」
    ——前半對、後半不完整。R3 的語意**確實不能動**（它判「新聞那個為錯」，套到 web 是錯的：
    web 可以合法地比 filing 新）。正確做法是**新增一條動作不同的規則**而不是擴充 R3：
    `find_unreconciled_web_conflicts`（R4）要求**並陳**而不是裁決。
    `_ground_source_type` 現在會吃 `web_extra`（來源型別 `"web"`，非權威），R3 因此**仍然不會**
    對 web 觸發（它只看 `src_types == ["news"]`），R4 才會。
    """
    if not chunks or not (answer or "").strip():
        return answer
    # LLM 抽結構化宣稱 → 確定性規則判。抽取失敗（API 掛／JSON 解不出）就跳過這層,
    # 不做降級偵測（見上方註解：舊的 regex 降級路徑實測從未被觸發過）。
    claims = _extract_claims(answer, CHECKER_MODEL, chunks)
    if claims is None:
        _trace("consistency: 宣稱抽取失敗 → 跳過一致性檢查")
        return answer
    found = find_claim_conflicts(claims)
    # R3 財報優先（零 LLM,見 find_authority_conflicts）：同格衝突且一邊只在新聞裡時,
    # 判新聞那個為錯。與 R1/R2 併入同一份 issues,共用既有的重生成路徑。
    # ⚠ R3 與 R4 共用 `_ground_source_type` 打的 `src_types`,所以只呼叫一次（在 R3 內）。
    found = found + find_authority_conflicts(claims, chunks, web_extra)
    # R4 web↔財報並陳（零 LLM,見 find_unreconciled_web_conflicts）：不判誰對,要求兩個都講。
    found = found + find_unreconciled_web_conflicts(claims)
    if not found:
        return answer
    issues = "\n".join(f"- {p}" for p in found)
    if verbose:
        print(f"--- consistency validator: 發現互斥數值 ---\n{issues}")
    _trace(f"consistency: 發現 {len(found)} 組互斥數值,重生成一次")
    revised = _write_final_answer(query, chunks, model_name,
                                  extra_user=_CONSIST_REVISE_SUFFIX.format(issues=issues),
                                  web_extra=web_extra, period_note=period_note)
    if revised and revised.strip():
        revised = _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                              web_extra=web_extra, period_note=period_note)
    # 回歸守衛（零 LLM）：重生成把別的確定性檢查弄差了就退回原答案。見 `_accept_revision`。
    return _accept_revision(answer, revised, chunks, web_extra, tag="consistency", stats=rev_stats)




def _dual_source_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                               verbose: bool = False, web_extra: str = "", period_note: str = "",
                     rev_stats: dict | None = None) -> str:
    """R5 並陳時點稽核 → 有缺時點則帶問題重生成一次 → 再過 citation 稽核。

    **放在數字溯源之後**（管線最末）：時點是**措辭**問題，而前面每一道 validator 的重生成都
    可能把時點改掉。放在中間等於只驗了一個會被後面推翻的版本——同 `_number_check_and_fix`
    當初被移到 reflect 之後的理由。

    **經濟性**：偵測零成本；281 份既有答案乾跑**只觸發 1 次，而那 1 次是真陽性**
    （`web_replay_r6` 的 web-01），所以放進主線不會擾動既有基準。
    """
    if not (answer or "").strip():
        return answer
    found = find_undated_dual_sourcing(answer)
    if not found:
        return answer
    issues = "\n".join(f"- {p}" for p in found)
    if verbose:
        print(f"--- dual-source validator: 並陳缺時點 ---\n{issues}")
    _trace(f"dual_source: {len(found)} 個時點缺口,重生成一次")
    revised = _write_final_answer(query, chunks, model_name,
                                  extra_user=_DUAL_SOURCE_REVISE_SUFFIX.format(issues=issues),
                                  web_extra=web_extra, period_note=period_note)
    if revised and revised.strip():
        revised = _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                              web_extra=web_extra, period_note=period_note)
    # 回歸守衛（零 LLM）：重生成把別的確定性檢查弄差了就退回原答案。見 `_accept_revision`。
    return _accept_revision(answer, revised, chunks, web_extra, tag="dual_source", stats=rev_stats)


def _number_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                          verbose: bool = False, web_extra: str = "", period_note: str = "",
                     rev_stats: dict | None = None) -> str:
    """數字溯源稽核 → 有找不到出處的數字則帶問題重生成一次 → 再過 citation 稽核。

    **放在 reflect 之後**：那正是缺口所在（reflect 的重生成引入的幻覺沒有任何人再看）。
    """
    if not (answer or "").strip():
        return answer
    found = find_untraceable_numbers(answer, chunks, web_extra)
    if not found:
        return answer
    issues = "\n".join(f"- {p}" for p in found)
    if verbose:
        print(f"--- number validator: 數字找不到出處 ---\n{issues}")
    _trace(f"number: {len(found)} 個數字找不到出處,重生成一次")
    revised = _write_final_answer(query, chunks, model_name,
                                  extra_user=_NUMBER_REVISE_SUFFIX.format(issues=issues),
                                  web_extra=web_extra, period_note=period_note)
    if revised and revised.strip():
        revised = _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                              web_extra=web_extra, period_note=period_note)
    # 回歸守衛（零 LLM）：重生成把別的確定性檢查弄差了就退回原答案。見 `_accept_revision`。
    return _accept_revision(answer, revised, chunks, web_extra, tag="number", stats=rev_stats)


def _period_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                          verbose: bool = False, web_extra: str = "", period_note: str = "",
                     rev_stats: dict | None = None) -> str:
    """期別稽核 → 有落差則帶問題重生成一次 → 再過 citation 稽核。無落差原樣回傳。

    與 `_consistency_check_and_fix` 分開而不合併成一次重生成：兩者的修訂指示語意完全不同
    （一個是「兩組互斥數值要調和」，一個是「你引錯期別了」），塞在同一段會互相稀釋。
    偵測本身零成本，只有真的抓到才花那一次生成，經濟性與一致性稽核相同。
    """
    if not chunks or not (answer or "").strip():
        return answer
    found = find_stale_period_claims(answer, chunks)
    if not found:
        return answer
    issues = "\n".join(f"- {p}" for p in found)
    if verbose:
        print(f"--- period validator: 引用的不是最新期別 ---\n{issues}")
    _trace(f"period: 發現 {len(found)} 組期別落差,重生成一次")
    revised = _write_final_answer(query, chunks, model_name,
                                  extra_user=_PERIOD_REVISE_SUFFIX.format(issues=issues),
                                  web_extra=web_extra, period_note=period_note)
    if revised and revised.strip():
        revised = _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                              web_extra=web_extra, period_note=period_note)
    # 回歸守衛（零 LLM）：重生成把別的確定性檢查弄差了就退回原答案。見 `_accept_revision`。
    return _accept_revision(answer, revised, chunks, web_extra, tag="period", stats=rev_stats)


def _format_citations(chunks: list[dict]) -> str:
    if not chunks:
        return ""
    lines = [f"  - {c['source']} #{c['chunk_index']} (rerank={c['rerank_score']:.3f})" for c in chunks]
    return "\n\n---\n📚 引用來源（Generator 實際依據的 chunk）:\n" + "\n".join(lines)


def _compose_answer_tail(answer: str, writer_chunks: list[dict], unmet: list[str]) -> str:
    """決定答案尾端要接哪些 metadata（機械式引用清單 ＋ 未涵蓋子問題揭露）。拒答一律不附。

    ⚠ **拒答不附引用清單**：「📚 引用來源（Generator 實際依據的 chunk）」在一份剛宣告
    自己沒有依據的答案底下是**假的宣稱**。舊守門寫成 `answer.startswith("I don't have enough")`,
    只擋得住 graph 崩潰時那句英文預設值,擋不住模型自己用中文寫的拒答（「我沒有足夠的資訊
    來回答…」）——實測既有結果檔 4175 份答案／59 份拒答,**21 份是帶著引用尾巴出貨的**。
    判準改用 `rq.looks_like_refusal`（唯一定義,見 rag_query.py）,不在這裡再寫一份。

    ⚠ 抽成純函式不是為了好看,是為了讓 `eval/verify_answer_validators.py` 閘門⑬ 能**零 LLM
    直接測這條生產判斷**;去 inspect `run_agentic` 的原始碼字串等於又一次抄寫（見
    `verify_web_gate_isolation.py` 閘門① 的教訓）。

    ⚠ `unmet` 跟著引用清單一起被拒答關掉,是**維持舊行為**（兩者本來就在同一個 if 底下）,
    不是新的決定。拒答時那份清單只是把答案已經講過的話再講一次。
    """
    if not writer_chunks or rq.looks_like_refusal(answer):
        return ""
    tail = _format_citations(writer_chunks)
    if unmet:
        tail += ("\n\n---\n⚠ 知識庫未涵蓋以下子問題,以上回答未就其提供依據:\n"
                 + "\n".join(f"  - {u}" for u in unmet))
    return tail


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
                     verbose: bool = False, web_extra: str = "", period_note: str = "",
                     rev_stats: dict | None = None) -> str:
    """Reflection 節點的核心:稽核幻覺→有則帶問題重生成一次→再過 citation 稽核。無幻覺原樣回傳。

    ⚠ 2026-08-13：`web_extra` 要進 **`sources_text`**，不只進重生成。這一層是拿「答案」對
    「來源全文」比對判幻覺——來源若少了網路結果，任何從 web 得到的即時數字都會被判成幻覺
    而觸發重寫，等於 web 資料永遠活不到最終答案。
    """
    if not chunks or not (answer or "").strip():
        return answer
    sources_text = "\n\n".join(f"[{c['source']} #{c['chunk_index']}]\n{c['content']}" for c in chunks)
    if web_extra:
        sources_text += "\n\n" + web_extra
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
                                  extra_user=_REFLECT_REVISE_SUFFIX.format(issues=issues),
                                  web_extra=web_extra, period_note=period_note)
    if revised and revised.strip():
        # 重生成後再過 citation 稽核,確保修正時沒引入捏造引用
        revised = _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                              web_extra=web_extra, period_note=period_note)
    # 回歸守衛（零 LLM）：重生成把別的確定性檢查弄差了就退回原答案。見 `_accept_revision`。
    return _accept_revision(answer, revised, chunks, web_extra, tag="reflect", stats=rev_stats)


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
    _reset_query_web_budget()   # 每個 query 一份預算；⚠ 這裡是唯一的歸零點，不要移進 executor
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
        # 降級的成因要**進結果檔**，不能只有 `_trace`（非 verbose 完全不輸出，而降級記錄
        # 外觀完全正常——有 answer、無 error、resume 還會跳過它）。2026-09-08 三題、
        # 09-09 九題降級，成因在加這一格之前全是猜的。
        # ⚠ 要帶**最深一層的檔:行**，不能只有 `repr(e)`：後者的資訊量與上面那行 `_trace`
        #   完全相同，而現行的病正是「印了也查不出是哪個節點炸的」（閘門㉒d）。
        import traceback as _tb_mod
        _frames = _tb_mod.extract_tb(e.__traceback__)
        _loc = (f"{os.path.basename(_frames[-1].filename)}:{_frames[-1].lineno}"
                f" in {_frames[-1].name}") if _frames else "?"
        _degraded_reason = f"{type(e).__name__}: {e} @ {_loc}"[:400]
        bge_m3, rerank_model, client = _get_models()
        with _quiet():
            chunks_fb, note_fb = rq.retrieve(query, bge_m3, rerank_model, client,
                                             top_k=WRITER_MAX_CHUNKS, model_name=RETRIEVAL_MODEL,
                                             enable_rewrite=False, full_translate_en=True)
        # 降級路徑不經過 graph，期間降級揭露要在這裡自己接（否則這條路又回到「無揭露」）。
        answer_fb = _fallback_local_summary(query, chunks_fb, period_note=note_fb)
        if freshness_mode == FRESHNESS_LIVE:
            # 降級路徑同樣看**實際用到的 chunk**（`chunks_fb` 就是這次的全部依據），
            # 不再用詞表猜問法——與正常路徑同一套判準。
            answer_fb = answer_fb.rstrip() + _format_unresolved_freshness_notice([{
                "status": "done", "web_used": False,
                "freshness_gaps": _news_freshness_gaps(chunks_fb, _get_as_of_date()),
            }])
        return {"answer": answer_fb, "chunks": chunks_fb, "sub_queries": [query], "messages": [],
                "period_notes": [note_fb] if note_fb else [],
                # ⚠ 這個鍵**只在降級時出現**：缺席＝沒降級（閘門㉒b）。填 None／"" 會讓
                #   消費端要多維護一套「什麼算空」的判準，而那正是 `None` ≠ `{}` 的教訓。
                #   ⚠ 同理這條路**不帶** `retrieved_union`（沒經過 graph，閘門㉒f）。
                "degraded_reason": _degraded_reason}

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

    answer = answer.rstrip() + _compose_answer_tail(answer, writer_chunks, unmet)

    sub_queries = [t["task"] for t in todos]   # 對映舊回傳鍵（eval 依賴）
    if verbose:
        print(f"[run_agentic] todos={sub_queries}, collected={len(collected)} chunks, "
              f"web_notes={len(final.get('web_notes', []))}")

    return {
        "answer": answer,
        "chunks": collected,
        "sub_queries": sub_queries,
        "messages": [],   # 舊回傳鍵,本版不再有 agent message 列表,保留空 list 供相容
        # 期間降級揭露：**已注入 Generator prompt**（見 `_write_final_answer`），這裡另外回傳一份
        # 供呼叫端**顯示給使用者**——與單管線 `run_single_query` 的 `print(f"⚠️  {fallback_note}")`
        # 是同一個消費端。⚠ 刻意不併進 `answer` 字串：那會動到既有結果檔的答案文字，而顯示這件事
        # 由呼叫端負責（CLI 印、SSE 送事件），跟「注入生成」是兩個獨立的通道。
        "period_notes": final.get("period_notes", []),
        # 金額單位後處理的計數與明細（見 `rq.finalize_answer_units` 的 stats）。
        # ⚠ 降級路徑（graph.invoke 崩潰那條）**不會有這一格**——它走 `_fallback_local_summary`，
        #   根本沒經過 `finalize_answer_units`。消費端要把「缺這格」與「這格是 0」分開讀，
        #   否則崩潰降級的題會被算進「LLM 這次沒寫億」的分母裡。
        "unit_stats": final.get("unit_stats") or {},
        # Replanner 的待辦額度統計（見 `_node_replan`）。`refused_budget` 每一筆都代表
        # **Replanner 想加一個待辦、但額度被 Planner 用光了**——那是 `MAX_TODOS` 該不該
        # 拆成兩份額度的分母（見該常數上方與 BACKLOG）。
        # ⚠ 與 `unit_stats` 同樣的 `None` ≠ `{}` 語意：崩潰降級那條路不經過 graph，
        #   **整個 key 不會出現**（不是 0），消費端要把兩者分開讀。
        "replan_stats": final.get("replan_stats") or {},
        # Plan 的依賴宣告品質（見 `validate_plan_dependencies`）。`deps_declared`
        # 為 False ＝ 這一題走的是舊格式 plan（重放快取命中舊 fixture），
        # 那時依賴仍由 `_BACKREF_RE` 詞表判——彙總時要把這兩群分開。
        "plan_stats": final.get("plan_stats") or {},
        # 重生成回歸守衛（見 `_accept_revision`）。`rejected` 每一筆都代表
        # **某一道 validator 的重生成讓另一道已經通過的檢查倒退了**。
        "revision_stats": final.get("revision_stats") or {},
        # executor 的出場方式（見 `_merge_exec_stats`）。`forced_pass` 每一筆都代表
        # **Grader 連 MAX_REWRITES+1 輪都判證據不足，而系統照樣拿它作答且零保留**——
        # 「要不要揭露／拒答」的分母就是這一格。⚠ 另外三種出場（`kb_unfixable_exit`／
        # `web_budget_exit`／`crashed`）**不是同一種病**，彙總時不可合併（閘門⑯d/e/l）。
        "exec_stats": final.get("exec_stats") or {},
        # 各子問題檢索候選池（`rq.retrieve` 產出、Grader 圈選**之前**）的 key 聯集。
        # ⚠ **純觀測**：沒有任何節點讀它，行為與加它之前逐字相同。
        # 為什麼需要：`chunks`／`sources` 是 pool → Grader 圈選 → `COMMIT_TOP_K` 截斷
        # 之後的**末端**（實測 65 題中位數 3 顆），於是「撈到了卻沒被用」在結果檔裡
        # **結構性地量不到**——`probe_gold_funnel` 兩輪都是 0，那是量尺的性質不是系統健康。
        # ⚠ 崩潰降級那條路**整個 key 不會出現**（沒經過 graph），同 `unit_stats` 的
        #   `None` ≠ `{}`：消費端要把「缺席」與「空」分開讀，否則降級題會被算進分母。
        "retrieved_union": final.get("retrieved_union") or [],
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

    global ENABLE_WEB_SEARCH
    if args.trace or args.verbose:
        # ⚠ 這裡**不可以**寫 `global _TRACE; _TRACE = True`（2026-09-12 之前就是那樣，整條
        #   `--trace`／`--verbose` 靜默無效）：重綁本模組的 `_TRACE` 動不到 `_trace()` 實際
        #   讀的 `tracing._TRACE`。唯一寫入點是 `set_trace_enabled()`，理由見它的 docstring。
        set_trace_enabled(True)
    if args.no_web:
        ENABLE_WEB_SEARCH = False

    _get_models()  # 提前載入重模型
    validator_on = ENABLE_REFLECTION and not args.no_validator

    if args.query:
        out = run_agentic(args.query, verbose=args.verbose, enable_coverage_validator=validator_on,
                          freshness_mode=args.freshness_mode)
        print("\n" + "═" * 60)
        # 期間降級揭露：與單管線 `run_single_query` 的 `print(f"⚠️  {fallback_note}")` 同一個通道。
        # 它已經注入過 Generator prompt，這裡是**顯示**那一半——舊版 agentic 兩半都沒有。
        for note in out.get("period_notes", []):
            print(f"⚠️  {note}")
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
        for note in out.get("period_notes", []):
            print(f"\n⚠️  {note}")
        print("\n💡 Final Answer:\n")
        print(out["answer"])
        print(f"\n🧩 Sub-queries: {out['sub_queries']}")
        print(f"📚 Chunks used: {len(out['chunks'])}")


if __name__ == "__main__":
    main()
