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
from .tracing import _TRACE, _trace   # noqa: E402
from .freshness import (   # noqa: E402
    TAVILY_MAX_RESULTS,
    TAVILY_FETCH_RESULTS,
    TAVILY_PER_DOMAIN_CAP,
    _COVERAGE_SOURCE_RE,
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
)
from .validators import (   # noqa: E402
    _CITE_RE,
    _REF_PREFIX_RE,
    _BARE_REF_CITE_RE,
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
)
from .planning import (   # noqa: E402
    CHECKER_MODEL,
    MAX_SUBQUERIES,
    POOL_RETURN_K,
    FRESHNESS_LIVE,
    VALID_ROUTES,
    _ROUTE_LEGACY_DEFAULT,
    _ROUTE_UNCERTAIN_DEFAULT,
    _loads_json_lenient,
    _coerce_bool,
    _PLANNER_PROMPT,
    _parse_plan_output,
    _plan_subqueries,
    _CHECKER_PROMPT,
    _CHECKER_LIVE_RECENCY_BLOCK,
    _check_sufficiency,
)
from .gaps import (   # noqa: E402
    FRESHNESS_SNAPSHOT,
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
from .chunks import (   # noqa: E402
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
)
from .ratio import (   # noqa: E402
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
    _TICKER_CANON,
)
from .coverage import (   # noqa: E402
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

# GEN_MODEL：實際寫答案的 Generator。2026-07-31 定案用 gpt-oss-120b，理由是「限速」這一維：
#   glm-5.2            單發 150~220s（NIM 上被拖），100 題不可行
#   deepseek-v4-pro    品質最好但有很低的單模型限速，eval 密集打立刻 429（實測 34 題就撞牆）
#   gpt-oss-120b       1~8s、且高負載下仍不被限速（NVIDIA 主推開放模型、額度寬）→ 唯一能撐 eval 量的
# 代價：gpt-oss-120b 偶爾吐全形【】引用，靠下游 _validate_and_fix_citations 校正（已端到端驗證）。
# 與 CHECKER_MODEL 同模型無妨（不同 prompt、不同任務）。benchmark 見 experiments/agentic/_bench_gen.txt。
GEN_MODEL       = os.getenv("AGENTIC_GEN_MODEL", "openai/gpt-oss-120b")

RERANK_GATE_TAU   = 0.5   # rerank_score(sigmoid) 門檻：僅供 trace/參考；夠不夠的最終判斷交給 Checker。
MAX_REWRITES      = 2     # 每個子問題的補救改寫上限（防迴圈 / 省 token）。
COMMIT_TOP_K      = int(os.getenv("AGENTIC_COMMIT_TOP_K", str(rq.DEFAULT_TOP_K)))   # 每個子問題 advance 時收進 collected 的 top-k。env 可覆蓋供 ablation。
WRITER_MAX_CHUNKS = 8     # 單發/fallback 路徑用的固定 chunk 上限（防 TPM 爆）；synthesize 改走自適應預算。
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
# 每則保留的摘要字數。
# ⚠ **原本是 300,那是 2026-08-13 「即時市值答不出來」的真正主因**（比日期把關、比白名單都嚴重）：
#    Tavily 的 content 實測 596~1982 字,而數據頁的**數字排在站台樣板文字後面**,300 字正好切在
#    數字前一個字——trace 裡看到的 `Apple market cap as of Augus` 就是被切斷的 `$4572.79B`。
#    「資料撈到了卻被自己截掉」與「根本沒撈到」在舊 trace 裡長得一模一樣,是加了逐則印摘要才看見的。
WEB_CONTENT_CHARS = int(os.getenv("AGENTIC_WEB_CONTENT_CHARS", "1200"))
# web_search 每個待辦的硬性呼叫上限（工程層閘門，不只靠 prompt 服從性——實測弱腦會對「最近新聞」題
# 瞎猜關鍵字狂打 33 次 Tavily 仍一次都沒採用）。超過上限直接回拒、不再真的打 API。
WEB_SEARCH_MAX_CALLS = int(os.getenv("AGENTIC_WEB_SEARCH_MAX_CALLS", "3"))
# rag_search 每個待辦的硬性呼叫上限（工程層閘門）。實測 glm-5.2 會對新聞題失控搜 40+ 次、top rerank
# 早在第 4 次就到頂、後面全是白搜，卻因每次 tool call ~30s 把單題拖到 ~50 分。超過上限的 rag_search
# 直接回「停止搜尋、用已檢索內容作答」，不再真的檢索。COMMIT_TOP_K=5，4 次搜尋（每次寬召回 20+ 候選、
# 去重併池）已足以涵蓋 top-5，不傷品質。
RAG_SEARCH_MAX_CALLS = int(os.getenv("AGENTIC_RAG_SEARCH_MAX_CALLS", "4"))

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
_ZH_ANSWER_DIRECTIVE = (
    "\n\nLANGUAGE — 你的整段回答必須用【繁體中文】書寫,不得用英文段落、不得用簡體字。"
    "保留英文金融術語、股票代號(NVDA/MSFT/…)、以及來源的 \"$X billion / $Y million\" 單位詞【照原文】"
    "(依 Rule 11,不要自己換算成億);只有敘述文字要繁體中文,數字與其單位詞不改。"
)


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


def _consistency_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                               verbose: bool = False, web_extra: str = "", period_note: str = "") -> str:
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
        return _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
    return answer




def _dual_source_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                               verbose: bool = False, web_extra: str = "", period_note: str = "") -> str:
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
        return _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
    return answer


def _number_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                          verbose: bool = False, web_extra: str = "", period_note: str = "") -> str:
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
        return _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
    return answer


def _period_check_and_fix(query: str, answer: str, chunks: list[dict], model_name: str,
                          verbose: bool = False, web_extra: str = "", period_note: str = "") -> str:
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
        return _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
    return answer


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
                     verbose: bool = False, web_extra: str = "", period_note: str = "") -> str:
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
        return _validate_and_fix_citations(query, revised, chunks, model_name, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
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
    """一個子問題的 run-scoped 狀態：檢索池、web 筆記、期間降級揭露、Grader 圈選的相關 id、工具呼叫次數。

    `period_notes` 與 `web_notes` 是同一個形狀、同一個理由：兩者都是「只有檢索層知道、
    Generator 看不到」的事實，而產生它的地方（`_retrieve_chunks` / `_tavily_search`）分散在
    ReAct 工具、確定性迴圈與兩處例外降級路徑上——收在 run state 才不會有哪個呼叫點漏接。"""
    __slots__ = ("pool", "web_notes", "period_notes", "relevant_ids", "rag_calls", "web_calls")

    def __init__(self) -> None:
        self.pool: list[dict] = []
        self.web_notes: list[str] = []
        self.period_notes: list[str] = []
        self.relevant_ids: set[str] = set()
        self.rag_calls = 0
        self.web_calls = 0


_run_state_var: contextvars.ContextVar[_RunState] = contextvars.ContextVar("agentic_run_state")

# ── 整個 query 共用的 web 預算（跨子問題、跨 replan 輪次）
#
# ⚠ **`WEB_SEARCH_MAX_CALLS` 擋不住這件事**，2026-08-14 才發現：它記在 `_RunState.web_calls`，
#   而 `_RunState` 是**每個子問題各自歸零**的（`_reset_run_pool()` 在 executor 開頭呼叫），
#   所以「上限 3 次」的真實語意是「每個子問題 3 次」；而且確定性執行層的 web fallback 直接
#   呼叫 `_tavily_search`，連那個計數器都沒經過。實測「蘋果的即時市值是多少？」跑出 7 次 web。
#
# 為什麼會生出那麼多子問題：時效改判讓子問題恆為 sufficient=False，replanner 每輪就再加一個
# 措辭更強調「請上網」的同義待辦（'改用網路搜尋查…' → '即時網路搜尋…' → '使用即時金融網站…'）。
# **不要用字串相似度去認這些同義句**——實測分離度是負的：正向最低 jaccard 0.04、負向最高 0.50，
# 而最糟的負向正是「即時市值」vs「即時本益比」這種字面幾乎一樣、需求卻不同的配對。那等於
# 用字串比對做語意感知，就是被拿掉的 `_RELATIVE_TIME_RE` 換個外觀（見 CLAUDE.md〈LLM 與 Python〉）。
# 這裡改成**不判斷語意、只封成本**：web 是最後手段，一個 query 用掉 N 次就不再花錢。
QUERY_WEB_BUDGET = int(os.getenv("AGENTIC_QUERY_WEB_BUDGET", "3"))
_query_web_budget_lock = threading.Lock()
_query_web_calls = 0


def _reset_query_web_budget() -> None:
    """每個新 query 開始時歸零（在 graph 入口呼叫，不是 executor 入口——那正是舊計數器的 bug）。"""
    global _query_web_calls
    with _query_web_budget_lock:
        _query_web_calls = 0


def _take_query_web_budget() -> bool:
    """取用一次 query 級 web 額度；用完回 False。子問題並行跑，所以要上鎖。"""
    global _query_web_calls
    with _query_web_budget_lock:
        if _query_web_calls >= QUERY_WEB_BUDGET:
            return False
        _query_web_calls += 1
        return True


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


def _web_query_en(query: str) -> str:
    """送給 Tavily 之前把 query 翻成英文（複用 `rq.translate_query_to_english`：已英文則原樣、
    有重放快取、翻譯失敗降級回原句，不會成為單點故障）。

    為什麼（2026-08-13 實測，同一次比較）：
      中文「改用即時網路搜尋查蘋果市值」→ `cn.wsj.com` 的多年前舊文（Apple $2.48T vs MSFT $1.76T），
        模型把舊文的「上週收盤」當本週，還推論出「市值下降了」。
      英文 `Apple market cap`          → `stockanalysis.com/stocks/aapl/market-cap`，score 0.91。
    順帶解掉地區子網域問題：英文 query 本來就不會撈到 `cn.wsj.com`／`hk.finance.yahoo.com`，
    不必維護一份地區站台排除清單（那又會變成 O(n)）。
    ⚠ 這只改善命中品質，**不是日期把關**——舊英文文章一樣進得來，那要另外解。
    """
    try:
        en = rq.translate_query_to_english(query, CHECKER_MODEL)
    except Exception as e:
        _trace(f"  web query 英譯失敗（{e!r}）→ 用原句")
        return query
    if en and en.strip() and en.strip() != (query or "").strip():
        _trace(f"  web query 英譯：{query[:32]!r} → {en[:48]!r}")
    return en or query


def _tavily_raw(query: str) -> dict:
    """真正打 Tavily 的那一層，**錄／放就在這裡**（見 `web_replay` 模組 docstring）。

    ⚠ 刻意錄「原始回應」而不是 `_tavily_search` 的回傳字串：後者是跑完 `_host_allowed`
      複核 → `_normalize_url` 去重 → 單域名上限 → `_url_published_date` 抽日期 → 過時過濾
      → 摘要截斷之後的成品。錄在那裡等於把要測的六道處理一起 mock 掉＝測了個寂寞。
    replay 命中時**不需要 TAVILY_API_KEY、不 import tavily**——fixture 就是全部的輸入。"""
    key = _web_replay.make_key(query, TAVILY_FETCH_RESULTS, "advanced", WEB_ALLOWED_DOMAINS)
    hit = _web_replay.get(key)          # replay 模式 miss 會在這裡直接報錯，不會偷偷連網
    if hit is not _web_replay.MISS:
        _trace(f"  web_search(replay) {query[:40]!r}")
        return hit
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise _NoTavilyKey()
    from tavily import TavilyClient
    resp = TavilyClient(api_key=api_key).search(
        query, max_results=TAVILY_FETCH_RESULTS, search_depth="advanced",
        include_domains=WEB_ALLOWED_DOMAINS)
    _web_replay.put(key, resp)
    return resp


class _NoTavilyKey(Exception):
    """缺 key 與「API 打壞了」要分開：前者是組態問題，回傳的說明字串不同。"""


def _tavily_search(query: str, need: str = "none") -> str:
    """Tavily 網路搜尋，回傳幾則結果的標題/發布日/摘要/網址（每則帶 [web: url] 供生成端引用）。
    `need` 是 Grader 的 `realtime_need`（intraday／days／none），只用來決定過時門檻 `WEB_STALE_DAYS`。
    任何失敗（停用 / 缺 key / API 錯）→ 回傳明確說明字串，絕不 raise（不讓 web 成為單點故障）。

    ⚠ 2026-08-13 起限定 `WEB_ALLOWED_DOMAINS`。**白名單濾空時明確回報查無，不退回全網**——
    靜默 fallback 等於白名單沒生效卻沒人知道（同 CLAUDE.md「稽核回傳 0 筆先當壞消息查」的道理）。
    濾空會走 _trace，讓它在 trace 看得見而不是安靜消失。

    ⚠ 2026-08-14 起 `search_depth="advanced"`（原 basic）。實測同一 query：basic 的 content 596~1296 字、
    advanced 820~1982 字，且 advanced 把數字段落排到前面（macrotrends 從 3 個數字變 6 個）。
    差別對「即時報價／市值」這種**值在頁面深處**的題是決定性的。
    """
    if not ENABLE_WEB_SEARCH:
        return "（web search 已停用）"
    try:
        resp = _tavily_raw(query)
        results = resp.get("results", []) if isinstance(resp, dict) else []
        if not results:
            _trace(f"  web_search({query[:40]!r}) → 白名單內查無結果（不退回全網）")
            return "（web search 查無結果：白名單來源內找不到，未退回全網搜尋）"
        kept, stats = _dedupe_web_results(results, need, _get_as_of_date())
        _trace(f"  web_search({query[:40]!r}) 原始 {len(results)} → 保留 {len(kept)}"
               f"（非白名單主機 {stats['off_host']}、衍生性商品頁 {stats['derivative']}、"
               f"同頁重複 {stats['dup']}、同域名超額 {stats['domain_cap']}、"
               f"過時 {stats['stale']}、可判日期 {stats['dated']}；need={need}）")
        if not kept:
            return "（web search 查無結果：白名單來源內的結果全部過時或重複）"
        lines = []
        for r in kept:
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            content = " ".join((r.get("content") or "").split())[:WEB_CONTENT_CHARS]
            # 日期直接標進 prompt：**讓生成端能自己排序新舊**,而不是把「哪個比較新」留給它猜。
            # 標不出日期的寫「未標示日期」而非省略——省略會被讀成「就是今天」。
            d = r.get("_pub_date")
            stamp = f"（發布日 {d.isoformat()}）" if d else "（未標示日期）"
            lines.append(f"- {title} {stamp} [web: {url}]\n  {content}")
            # 逐則印出網址/發布日/標題/摘要全文（--trace 才有，平時 no-op）。
            # ⚠ 沒有這一段就分不出「Tavily 根本沒撈到那個數字」與「Generator 拿到了卻不用」
            #   ——2026-08-13 的「蘋果即時市值」就是卡在這裡無法診斷：7 次 web、18 次時效改判，
            #   答案仍用 6/12 快照，而 trace 只有 `→ 2163 chars` 這種字數，什麼也推不出來。
            # 印摘要全文（已截到 WEB_CONTENT_CHARS）是刻意的：只印前 80 字很可能正好切掉要找的數字
            #   ——2026-08-14 就是靠這一段才看見 `Apple market cap as of Augus` 是被自己截斷的。
            # 印完整網址：上一輪診斷就是因為只印域名，無法判斷日期抽取為何失效（Reuters 的
            # 日期在網址結尾而非開頭），只好另外打 API 去問。觀測成本比再跑一次便宜太多。
            _trace(f"    web ← {stamp} {url}")
            _trace(f"      {title[:70]}")
            _trace(f"      {content}")
        return "網路搜尋結果:\n" + "\n".join(lines)
    except (_web_replay.FixtureMiss, _web_replay.RecordError):
        # 絕不降級：fixture 沒涵蓋／寫不進去都要大聲炸，否則會被下面的通用處理吞成
        # 「查無結果」，整輪 replay 實驗在無 fixture 下跑完、或錄出一份安靜少幾筆的 fixture，
        # 兩種都是事後分不出來的失敗。
        # ⚠ 2026-08-28 起這兩個型別已移出 `Exception` 階層，所以這一段**在語意上是多餘的**。
        #   保留是刻意的：它讓「這裡不准降級」在讀碼時看得見，而且萬一哪天基底類別被改回去，
        #   最重要的這一個呼叫點仍然守得住。真正的保證在型別本身 ＋ 閘門⑦。
        raise
    except _NoTavilyKey:
        return "（web search 不可用：未設定 TAVILY_API_KEY）"
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
    chunks = _retrieve_chunks(query, attributable=False)   # Agent A 自行改寫的 query,非使用者意圖
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
        _trace(f"  tool web_search({query[:40]!r}) → 已達本子問題上限 {WEB_SEARCH_MAX_CALLS} 次,拒絕")
        return (f"（web_search 已達本子問題上限 {WEB_SEARCH_MAX_CALLS} 次，不再搜尋。"
                f"請改用你已從 rag_search 檢索到的知識庫內容作答；若知識庫確實查無，就如實說明查無。）")
    # 兩層上限缺一不可：`web_calls` 管單一子問題內的濫用，`_take_query_web_budget()` 管整個 query
    # 的總成本——子問題數量本身會被 replanner 撐大，只有前者時總量無上界（見 QUERY_WEB_BUDGET 註解）。
    if not _take_query_web_budget():
        _trace(f"  tool web_search({query[:40]!r}) → 整個 query 的 web 預算已用完,拒絕")
        return (f"（本次查詢的網路搜尋額度已用完（{QUERY_WEB_BUDGET} 次）。"
                f"請改用已檢索到的知識庫內容作答；若確實查無，就如實說明查無。）")
    state.web_calls += 1
    # ⚠ 這條路（ReAct executor，`AGENTIC_REACT_EXECUTOR=true` 才啟用，非預設）沒有 Grader 的
    #   `realtime_need`，所以 `need` 只能用預設 "none" ＝**不做過時過濾**。刻意選保守側（寧可
    #   多留也不誤刪）；要補的話得先讓 ReAct 那條路也產出時效判斷。
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


def _fallback_local_summary(task: str, chunks: list[dict], period_note: str = "") -> str:
    """subagent 崩潰 / 沒吐摘要時的降級：直接把池裡 top chunk 走生產生成契約產一段局部摘要。
    **這是安全網本身，不能又依賴同一個可能故障的 LLM API 而沒有退路**——包 try/except，
    若 _write_final_answer 也失敗（見實測：NVIDIA 端連續 504 時，降級呼叫一樣會炸），
    退回零 LLM 的機械式摘要，保證 _run_executor 永遠有摘要可回、不會把例外炸穿整個 graph。"""
    if not chunks:
        return f"（子問題「{task}」在知識庫中查無足夠資料）"
    try:
        return _write_final_answer(task, chunks, GEN_MODEL, period_note=period_note)
    except Exception as e:
        _trace(f"_fallback_local_summary 也失敗（{e!r}）→ 退回零 LLM 機械式摘要")
        return _mechanical_summary(task, chunks)


def _run_executor_react(task: str, temporal_scope: str, freshness_mode: str,
                        subq_index: int, verbose: bool, *,
                        attributable: bool) -> tuple[str, list[str], str, list[str]]:
    """[舊行為，AGENTIC_REACT_EXECUTOR=true 才用] Executor 核心：Agent A（ReAct 檢索員）⇄ Grader 的訊息
    傳遞迴圈。先純檢索（system prompt 鎖生成）→ Grader 評分 → 不夠餵糾正訊息續搜（有界 MAX_REWRITES）→
    夠了發「解除限制、生成摘要」觸發訊息 → A 產出局部摘要 → 跳出。回傳 (summary, web_notes)。
    ⚠ 2026-07-29 chunk 層級 probe 證實此路徑是 multi_intent context_recall 漏水主因（Agent A 自行改寫
    query 把 planner 已對的子問題搞砸），預設已改走 _run_executor_deterministic。"""
    from langchain_core.messages import HumanMessage

    verdict: dict = {}          # 迴圈可能一次都沒跑（例外／弱腦）→ 先給空的
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
            verdict = _check_sufficiency(task, state.pool, temporal_scope,
                                         freshness_mode)   # Agent B｜Grader（單次 LLM、無工具）
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
                state.pool.extend(_merge_chunks([], _retrieve_chunks(task, attributable=attributable)))
        summary = _fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])

    if not (summary or "").strip():
        # react 跑完但沒吐摘要（弱腦跳過）→ 降級用池裡的 chunk 生成
        summary = _fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])
    return (summary, list(state.web_notes), (verdict or {}).get("realtime_need", "none"),
            list(state.period_notes))


def _effective_route(route: str, freshness_mode: str) -> str:
    """套上 **eval 隔離**之後的實際 route。純函式、零副作用。

    ⚠ snapshot（＝eval 走的路）或 `ENABLE_WEB_SEARCH` 關閉時**一律降級成 `kb`**。兩層理由：
      ① **這就是 eval 隔離**。CLAUDE.md：隔離只靠 `freshness_mode == LIVE` 與
         `ENABLE_WEB_SEARCH` 兩個獨立條件，任何「相關性詞表」都不是隔離機制。把這兩個條件
         收在**一個純函式**裡，才寫得出真值表（閘門⑪t~⑪w）。
      ② **降級成 `kb` 而不是「什麼都不撈」**：snapshot 下每個 todo 本來就只撈 KB，
         降級成 kb 才能讓 **eval 的行為與加 route 之前逐字相同**——回空會讓 65 題基準整批位移。

    ⚠ 這裡**不做**非法值正規化（那是 `_parse_plan_output` 的事）：讓被改壞的 route 一路走到
      `_dispatch_todo` 當場炸，比在這裡默默吞掉好。live 之外吞不吞都沒有差別（碰不到 web）。
    """
    if freshness_mode != FRESHNESS_LIVE or not ENABLE_WEB_SEARCH:
        return "kb"
    return route


def _escalate_route(route: str, verdict: dict) -> str:
    """KB 走不下去時要不要升級到 web。純函式、零 LLM。

    ⚠ **只有 `kb_unfixable` 才升級，「不足」本身不算**。這條看起來保守，但它正是這次改動的
      重點：現行行為是 `not verdict["sufficient"]` 就打 web，於是純財報題也會上網
      （實測 before 臂 `mix-01`「Azure **最新一季**營收成長率」打了 web 並引用它）。
      把「不足」當升級條件 ＝ 換個寫法回到原地，還加重過度路由。誤報對照見閘門⑪i。

    ⚠ **升級成 `web` 而不是 `both`**，這一格被既有的閘門⑤當場抓過：`kb_unfixable` 的意思就是
      「KB 結構上補不了」，而 kb 那一半**已經在池子裡**（`_merge_chunks` 只加不減）——再撈一次
      照定義不可能有用，只是把 2026-08-14 那個「21 次檢索原地打轉」的病換個寫法請回來。
      閘門⑤ 的斷言是「KB 補不了 → 只檢索 1 次」，`both` 會讓它變成 2 次。
    """
    if route == "kb" and not verdict.get("sufficient") and verdict.get("kb_unfixable"):
        return "web"
    return route


def _dispatch_todo(route: str, query: str, *, need: str = "none",
                   attributable: bool = True) -> tuple[list[dict], list[str]]:
    """依 route 叫對應的 tool。**純 Python、零 LLM 判斷**——它不決定要不要上網，只執行。

    回 `(chunks, web_notes)`。policy（eval 隔離、web 預算、升級）全部在呼叫端，
    這裡只有「route 說什麼就做什麼」。這個分工是刻意的：**分派給 Python、裁決給 LLM**
    （CLAUDE.md），而 policy 各自有自己的純函式與真值表。

    ⚠ **非法 route 當場炸，不可靜默預設**：預設成 kb 的話，Plan 打錯一個字就讓新聞題
      全退回「拿財報硬答」，而那個失敗的外觀與「路由判成 kb」**完全相同**（閘門⑪f）。
      寬鬆正規化是 `_parse_plan_output` 的職責——**解析寬鬆、分派嚴格**。
    """
    if route not in VALID_ROUTES:
        raise ValueError(f"未知的 route {route!r}；合法值是 {VALID_ROUTES}")
    chunks: list[dict] = []
    web_notes: list[str] = []
    if route in ("kb", "both"):
        chunks = _retrieve_chunks(query, attributable=attributable)
    if route in ("web", "both"):
        # ⚠ 一定要走 `_tavily_search`（白名單複核／地區子網域／去重／過時過濾／截斷都在裡面）
        #   與 `_web_query_en`（Tavily 對英文 query 明顯較好）。自己直接叫 TavilyClient
        #   會把那六道一起繞掉。閘門⑪g 用 AST 守這一條。
        note = _tavily_search(_web_query_en(query), need=need)
        if note and not note.startswith("（"):
            web_notes.append(note)
    return chunks, web_notes


def _run_executor_deterministic(task: str, temporal_scope: str, freshness_mode: str,
                                subq_index: int, verbose: bool, *,
                                attributable: bool,
                                route: str = "kb") -> tuple[str, list[str], str, list[str]]:
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
    # eval 隔離在這裡一次套用：snapshot／web 關閉 → 一律 kb（見 `_effective_route`）。
    eff_route = _effective_route(route, freshness_mode)
    verdict = {"sufficient": False, "missing": "", "new_query": ""}
    try:
        for rnd in range(MAX_REWRITES + 1):
            # web 預算是 **query 級**的（子問題會被 replanner 撐大，只靠子問題級上限總量無界）。
            # 只在這一輪真的會打 web 時才取用，取不到就這一輪降級成純 KB。
            _round_route = eff_route
            if _round_route in ("web", "both") and not _take_query_web_budget():
                if _round_route == "both":
                    # both 的 KB 那一半本來就該撈，只是這一輪沒有 web 可加。
                    _trace(f"execute[{subq_index}] web 預算已用完（{QUERY_WEB_BUDGET} 次）"
                           f"→ 這一輪只跑 KB")
                    _round_route = "kb"
                else:
                    # ⚠ **route=web 拿不到額度時絕不退回 KB。** 這個 todo 之所以是 web，
                    #   就是因為 KB 依設計沒有這種資料（`RAW_EXCLUDE_DIRS` 排除 News/）；
                    #   退回去撈只會拿到財報，然後被當成新聞引用、零揭露——那正是這整個
                    #   改動要治的病。實測 after 臂 `news-10`／`news-13` 就是這樣來的。
                    #   預算是 query 級的，這一輪拿不到，之後也不會有 → 直接收工，
                    #   由下面的 `_unfulfilled_web_route_gaps` 留下可揭露的缺口。
                    _trace(f"execute[{subq_index}] web 預算已用完（{QUERY_WEB_BUDGET} 次）"
                           f"且 route=web → 不退回 KB，本子問題留空並記缺口")
                    break
            # 兩個條件都要成立：① 這個 todo 本身出自 Planner（不是 replan 加的）
            # ② 還沒被 Grader 改寫過（rnd 0）。任何一個不成立，這一輪產生的揭露句都不該
            # 說「所詢問」——它問的不是使用者問的東西。
            chunks, _notes = _dispatch_todo(
                _round_route, active_query,
                need=verdict.get("realtime_need", "none"),
                attributable=(attributable and rnd == 0))
            web_notes.extend(_notes)
            merged = _merge_chunks(list(state.pool), chunks)    # 併池：只加不減，補救輪不洗掉先前好 chunk
            state.pool.clear()
            state.pool.extend(merged)
            verdict = _check_sufficiency(task, state.pool, temporal_scope, freshness_mode)  # Agent B｜Grader
            state.relevant_ids.clear()
            state.relevant_ids.update(verdict.get("relevant_ids", []))
            _trace(f"execute[{subq_index}] det round={rnd} route={_round_route} "
                   f"q={active_query[:32]!r} pool={len(state.pool)} "
                   f"sufficient={verdict['sufficient']} missing={verdict['missing'][:40]!r}")
            if verdict["sufficient"] or rnd >= MAX_REWRITES:
                break
            # 升級：只有「KB 結構上補不了」才從 kb 升成 both（見 `_escalate_route`）。
            _next_route = _escalate_route(eff_route, verdict)
            # 防空轉：升級**沒改變任何東西**時再多跑一輪也沒有意義。
            # ⚠ 2026-08-14 實測「蘋果的即時市值」：時效改判每輪都判 False，而**任何 KB 檢索都不可能
            #   讓 KB 變新**，於是 7 個子問題 × 3 輪 = 21 次檢索全在原地打轉。這是結構性死迴圈，
            #   不是那一題的巧合——只要「不足的原因是資料不在 KB 裡」就會發生。
            if verdict.get("kb_unfixable") and _next_route == eff_route:
                _trace(f"execute[{subq_index}] 不足原因 KB 補不了（時效）且路由已無可升級 → "
                       f"跳過剩餘 {MAX_REWRITES - rnd} 次改寫")
                break
            # ⚠ **已知成本**：升級成 web 之後還會再 grade 一次，而那一次的池子與這一次相同
            #   （web_notes 不是 chunk），所以判定必然一樣 → **多燒一次 Grader 呼叫**。
            #   刻意接受：把它省掉要在迴圈裡插一段 inline dispatch＋break，而這個迴圈已經
            #   為了「省一輪」踩過兩次坑。一次 LLM 呼叫換迴圈可讀性，划算。
            eff_route = _next_route
            active_query = verdict["new_query"] or task        # Grader 的 targeted rewrite（唯一改寫來源，deterministic temp=0）

        # ⚠ **這裡原本有一段「KB 仍不足 → 補一次 web」的 fallback，2026-09-01 拿掉了。**
        #   它的觸發條件是 `not verdict["sufficient"]` ——「答不出來就上網」，而那正是
        #   before 臂量到的過度路由來源（`mix-01`「Azure 最新一季營收成長率」是純財報題，
        #   卻打了 web 並引用它）。現在改由**兩件事**接手，各自有純函式與真值表：
        #     · 該不該上網 → Planner 判的 `route`（＋ `_effective_route` 的 eval 隔離）
        #     · KB 走不下去要不要升級 → `_escalate_route`（只認 `kb_unfixable`，不認「不足」）
        #   兩道詞表閘門（`rq.looks_like_news_query` / `_RELATIVE_TIME_RE`）更早之前就拿掉了，
        #   理由相同：用硬編碼詞表做感知，換個措辭就漏。常駐證明見
        #   [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py)。
    except Exception as e:
        _trace(f"execute[{subq_index}] deterministic executor 例外 → 降級：{e!r}")
        if not state.pool:
            with _quiet():
                state.pool.extend(_merge_chunks([], _retrieve_chunks(task, attributable=attributable)))

    summary = _fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])  # 走生產契約單次生成（內含 try/except 保底）
    return summary, web_notes, verdict.get("realtime_need", "none"), list(state.period_notes)


def _run_executor(task: str, temporal_scope: str, freshness_mode: str,
                  subq_index: int, verbose: bool, *,
                  attributable: bool,
                  route: str = "kb") -> tuple[str, list[str], str, list[str]]:
    """Dispatcher：預設 deterministic（planner 子問題直接檢索）；AGENTIC_REACT_EXECUTOR=true 回舊 ReAct。

    第三個回傳值是 Grader 最後一次的 `realtime_need`——`_run_one_todo` 要靠它判「這個子問題
    需不需要即時資料」，那是時效警語的新判準（見 `_unmet_realtime_gaps`）。
    第四個是這個子問題檢索時發生的**期間降級揭露**（見 `_retrieve_chunks`），最終由 Synthesize
    注入 Generator prompt——與單管線 `run_single_query` 的 `fallback_note` 是同一個東西。"""
    if USE_REACT_EXECUTOR:
        # ⚠ ReAct 這條路**不吃 route**：它是已退役的對照組（gold-chunk 覆蓋 16/75 vs 35/75），
        #   自己決定何時停、自己選 tool。要讓它支援 route 得先把那些 agency 收回來，
        #   不在這次範圍內。用它跑 ＝ 回到加 route 之前的行為。
        return _run_executor_react(task, temporal_scope, freshness_mode, subq_index, verbose,
                                   attributable=attributable)
    return _run_executor_deterministic(task, temporal_scope, freshness_mode, subq_index, verbose,
                                      attributable=attributable, route=route)


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
    period_notes: list[str]    # 期間降級揭露（Tier 2 fallback；注入 Generator prompt，見 _retrieve_chunks）
    iterations: int            # execute↔replan 已迭代幾次（防無限迴圈）
    sufficient: bool           # Replanner 判定證據已足、可提前收斂
    answer: str                # 最終答案（未附引用清單；附錄在 run_agentic 收尾加）


_REPLANNER_PROMPT = f"""你是美股情報 RAG 的動態重規劃器。給你「原始問題」與「目前待辦清單（含各自狀態與
已完成的局部結果）」。請根據目前已收集到的結果，決定如何更新待辦清單：

- 【路由 route】每個新增的待辦都要帶 route（"kb"｜"web"｜"both"），判準與 Planner 相同：
  向量庫裡只有 10-K／10-Q 與 Fundamentals，**沒有任何新聞**。問「外面現在怎麼樣」（新聞、報導、
  分析師看法、即時報價）填 "web"；問財報數字／業務敘述填 "kb"；兩邊都有而且都要講填 "both"。
  ⚠ **不要把來源寫進 task 文字**——不要寫「改用網路搜尋查…」「使用即時金融網站…」，
  那件事現在由 route 欄位表達。task 只寫「要查什麼」。
- 只有時間契約明確指定 live、web 可用、且某個已完成待辦的 KB cutoff 確實早於查詢日並影響原始問題時，
  才能新增 route="web" 的待辦。snapshot 模式禁止因 wall clock 新增 web 待辦。
- 若目前已收集到的結果**已足以完整回答原始問題** → sufficient 設 true（剩餘 pending 待辦會被跳過）。
- 若某些還沒做的 pending 待辦其實已無必要 → 把它們的 id 放進 drop。
- 不要重複新增已經有的待辦；新增要克制（清單總數上限 {MAX_TODOS}）。

只輸出一個 JSON 物件，不要任何其他文字：
{{"sufficient": true 或 false,
  "add": [{{"task": "新待辦", "route": "kb"|"web"|"both"}}, ...],
  "drop": [待辦id 數字, ...]}}"""

# live 專屬追加段（2026-08-29）。**刻意只在 live 追加**：snapshot 的 replanner prompt 因此
# 逐字不變，65 題基準不被動到（同 `_CHECKER_LIVE_RECENCY_BLOCK` 的作法）。
#
# 為什麼加這一段：`web_used` **一直就在 todo dict 上**（`_node_execute` 回填），只是從來沒有
# 放進 prompt——replan 因此不知道「這個待辦已經打過 web 了」，於是針對同一個資訊需求再加一個
# 同義的 web 待辦。`probe_replan_contribution.py`（6 題 × 2 輪）量到的代價：
#   · intraday 兩題：**12 個 replan 待辦、664 秒、獨有且被引用的 chunk = 0**（12/12 全零）。
#     實際生出來的是 `改用網路搜尋查 NVIDIA 現在的股價` →`使用網路搜尋取得…最新股價`
#     →`…今日股價` →`…（例如 Yahoo Finance）` →`…於 Bloomberg` →`…（例如 MarketWatch）`
#     ——**它在列舉網站**。web-02 單輪 431.9 秒，而答案引用 0 個 chunk。
#   · 新聞題（web-04）：4 個 replan 待辦貢獻 6 個獨有且被引用的 chunk，**比 planner 自己的 4 個多**。
# → 所以不是關掉 replan，是**只擋「同義的 web 重試」這一種**。
_REPLANNER_LIVE_BLOCK = """
⚠ 待辦若標了 [已用過網路搜尋]，代表它**已經打過網路搜尋並拿到結果了**。**指定不同網站**
（「…例如 Yahoo Finance」「…於 Bloomberg」「…例如 MarketWatch」）**不算新待辦**——搜尋引擎
已經跨站搜過了，換一個網站名只是同一個搜尋的換句話說。
仍然可以加**真正不同**的待辦：換一個資訊來源類型（例如改去某份 10-Q／10-K 找）、補一個還沒
問過的面向、或加上原本沒有的具體時間範圍。"""


def _web_retry_is_pointless(todos: list[dict]) -> bool:
    """已經有待辦判定需要 **intraday** 資料且**已經打過 web** → 再加 web 待辦是必然徒勞。

    **為什麼這一條交給 Python 不交給 prompt**（CLAUDE.md〈LLM 與 Python 的分工〉：比對／定位
    給 Python）：「這個子問題已經打過 web 了嗎」「Grader 判它要多新」都是**查表**，沒有判斷成分。

    **為什麼判準是 `intraday` 而不是「打過 web 就不准再打」**：2026-08-29 實測（6 題×2 輪，
    `probe_replan_contribution.py`）兩者差很多：
      · intraday（web-01／web-02）：**12 個 replan 待辦、664 秒、獨有且被引用的 chunk ＝ 0**。
        KB 結構上不可能有即時報價,再搜幾次都一樣——它實際生出的是在**列舉網站**。
      · 新聞（web-04）：**4 個 replan 待辦貢獻 6 個獨有且被引用的 chunk**，比 planner 自己的 4 個多。
        新聞題的 KB 裡真的還有東西可找,改寫 query 會撈到不同 chunk。
    ⚠ **第一版用 prompt 寫「已經搜過就 sufficient: true 收斂」，結果 replan 在 12 輪裡全部
      不出手——連新聞題那 4 個有貢獻的也一起殺掉了**。那是「把功能關掉冒充修好」，
      由 `probe_replan_contribution.py` 的新聞那一列當場抓到。所以判準必須窄到 `intraday`。

    ⚠ **第二版在呼叫端加了 `_is_web_todo(task)` 前置條件，被詞表漏掉。** web-02 實測生出
      「使用NASDAQ官方**網站**或API查詢NVDA即時股價」「在 **Yahoo Finance** 上查詢 NVDA 當前股價」
      「在 **Bloomberg** 上查詢…」「在 **MarketWatch** 上…」「在 **Reuters** 上…」「在 **CNBC** 上…」
      ——六個待辦、503 秒、答案引用 0 個 chunk，而 `_WEB_TODO_RE`（`網路|上網|web search|internet`）
      **一個都不匹配**（「網站」不是「網路」，站名更不用說）。這正是 CLAUDE.md〈硬編碼詞表是警訊〉
      說的形狀：用字串比對做感知，換個措辭就漏。
      → 所以呼叫端**不再看待辦文字**：`intraday` ＋ 已搜過 web 時，追加**任何**待辦都拒絕。
        依據是 before ＋ after2 兩臂合計 **18 個 intraday replan 待辦、獨有且被引用的 chunk ＝ 0**。
      ⚠ **2026-09-02 起 `_WEB_TODO_RE` 整個沒了**：那條 snapshot 拒絕路徑改讀 `route` 欄位，
        不是隔離問題——隔離只靠 `freshness_mode` 與 `ENABLE_WEB_SEARCH`）。見 BACKLOG。
    """
    return any(t.get("web_used") and t.get("realtime_need") == "intraday" for t in todos)


def _node_plan(state: SupervisorState) -> dict:
    freshness_mode = state.get("freshness_mode", FRESHNESS_LIVE)
    subs = _plan_subqueries(state["query"], freshness_mode)   # 沿用 nv 版拆解器
    # ratio 意圖交 LLM（見 _classify_ratio_fields）。整批一次 call，且**在這裡**判：
    # 子問題是這條路的實際輸入，原始問句不是（「Microsoft 的營收成長率」可能被拆成
    # 口語的「成長得快不快」，詞表在那一刻就漏了）。
    rfields = _classify_ratio_fields([sub["task"] for sub in subs])
    todos = []
    for i, sub in enumerate(subs):
        subquery = sub["task"]
        scope = _build_todo_temporal_scope(subquery, freshness_mode)
        todos.append({
            "id": i,
            "task": subquery,
            # 路由：這個子問題該去哪裡拿資料。**由 Planner 判、寫成欄位**，不再靠
            # 「改用網路搜尋查…」這種自由文字 ＋ `_WEB_TODO_RE` 反向解析（那條路匹配不到
            # 「在 Yahoo Finance 上查詢」，見閘門⑧p）。
            "route": sub["route"],
            # 依賴：這個子問題要等哪個 todo 的結果才寫得出來（mh-07 的「該公司」）。
            # **這一版只留欄位不解析**——先讓 route 站穩，見 BACKLOG 的〈明確不做的〉。
            "depends_on": None,
            "temporal_scope": scope,
            # Planner 的分解是**使用者意圖的重述** → 由它產生的期間降級揭露可以說「所詢問財年」。
            # 對照 `_node_replan` 那一個（見該處註解）。判準與 `_retrieve_chunks` 的 docstring 同一條。
            "attributable": True,
            "ratio_fields": rfields[i] if i < len(rfields) else None,
            "freshness_gaps": [],      # 執行完才知道用了誰的新聞 → 由 _node_execute 回填
            "period_notes": [],        # 同上：檢索降級到 Tier 2 才會有，由 _node_execute 回填
            "web_used": False,
            "status": "pending",
            "result": "",
        })
    _trace(f"plan: {len(todos)} todos → "
           f"{[(t['task'], t['route']) for t in todos]}")
    return {"todos": todos, "collected": [], "web_notes": [], "period_notes": [],
            "iterations": 0, "sufficient": False}


def _run_one_todo(todo: dict, freshness_mode: str, verbose: bool) -> dict:
    """在自己的 thread（因此也是自己獨立的 contextvars context）內跑完一個子問題：retrieve↔grade
    迴圈 + commit 篩選，回傳這個子問題的完整結果。不觸碰任何跨子問題共用的可變狀態，讓 wave 執行
    可以安全平行呼叫（已用 ThreadPoolExecutor 實測驗證 contextvars 在 submit() 下天生隔離）。"""
    task = todo["task"]
    summary, web_notes, realtime_need, period_notes = _run_executor(
        task, todo.get("temporal_scope", ""), freshness_mode, todo["id"], verbose,
        # ⚠ `.get(..., "kb")` 而不是下標：`_node_replan` 建的 todo 這一版還沒有 route
        #   （第 5 步才收窄 replan）。預設 kb ＝ 與加 route 之前逐字相同的行為。
        route=todo.get("route", "kb"),
        # ⚠ 刻意用 `todo["attributable"]` 而不是 `.get(..., True)`：漏設要當場 KeyError。
        #   給預設值＝新的 todo 建立點會靜默沿用「可歸因」，那正是這次要防的東西。
        #   兩個建立點都設了這個欄位，由 `verify_answer_validators.py` 閘門 ⑮g 把關。
        attributable=todo["attributable"],
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
    _rf = todo.get("ratio_fields")
    if mentioned and _has_ratio_intent(_rf, task):
        picked = _ensure_ratio_source_coverage(run_state.pool, picked, mentioned, task, _rf)
    for c in picked:
        c["_subq"] = todo["id"]
    # 時效缺口在這裡算,不在 plan 時算：判準是「這個子問題**實際用到了**誰的新聞」,
    # 不是「問法看起來像不像新聞題」（見 _news_freshness_gaps 的 docstring）。
    if freshness_mode == FRESHNESS_LIVE:
        _now = _get_as_of_date()
        # 兩種缺口併存：① 用了過期的 KB 新聞（新聞若復活才會有）② 需要即時資料卻沒拿到 web
        gaps = (_news_freshness_gaps(picked, _now)
                + _unmet_realtime_gaps(task, realtime_need, picked, _now)
                # 第三個來源：路由說要上網、卻一次 web 都沒拿到（預算用完／搜不到）。
                # 用 `_effective_route` 而不是 todo 的原始 route——eval 隔離已經把它降級的
                # 情況不該算缺口（閘門⑪z5）。
                + _unfulfilled_web_route_gaps(
                    _effective_route(todo.get("route", "kb"), freshness_mode),
                    web_notes, picked, _now))
    else:
        gaps = []
    return {"id": todo["id"], "summary": summary, "web_used": bool(web_notes),
            "web_notes": web_notes, "picked": picked, "freshness_gaps": gaps,
            # ⚠ 2026-08-29 帶出來：`_node_replan` 要靠它擋掉「intraday 且已打過 web」之後的
            #   同義 web 重試（見那裡的 `_web_retry_is_pointless`）。在此之前它算完就被丟掉。
            "realtime_need": realtime_need,
            "period_notes": period_notes}


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
                    todos[i]["temporal_scope"] = _build_todo_temporal_scope(filled, freshness_mode)
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
                              "web_used": False, "web_notes": [], "picked": [],
                              "freshness_gaps": [], "period_notes": []}

    collected = state.get("collected", [])
    web = list(state.get("web_notes", []))
    periods = list(state.get("period_notes", []))
    for i in pending_idxs:   # 依原始順序合併（thread 完成順序不影響結果，只影響合併時機）
        r = results[i]
        todos[i]["result"] = r["summary"]
        todos[i]["web_used"] = r["web_used"]
        todos[i]["realtime_need"] = r.get("realtime_need", "none")
        todos[i]["freshness_gaps"] = r.get("freshness_gaps", [])
        todos[i]["period_notes"] = r.get("period_notes", [])
        todos[i]["status"] = "done"
        collected = _merge_chunks(collected, r["picked"])
        web += r["web_notes"]
        # 去重跨子問題：多個子問題問同一家同一年，`rq._build_fallback_note` 會產生逐字相同的句子，
        # 而這句話最終要整篇只講一次（同 `_format_unresolved_freshness_notice` 的教訓：
        # 逐待辦算、整篇印一次的東西，措辭與去重都要在合併這一層處理好）。
        for n in r.get("period_notes", []):
            if n not in periods:
                periods.append(n)
    # 保留舊語意：MAX_ITERS 是「總子問題執行次數」上限，不是「總波次」上限，避免分波後這個防線變寬鬆。
    iters = state.get("iterations", 0) + len(pending_idxs)
    _trace(f"execute wave: {len(pending_idxs)} todos ({[todos[i]['id'] for i in pending_idxs]}) done → "
           f"collected={len(collected)} period_notes={len(periods)}")
    return {"todos": todos, "collected": collected, "web_notes": web,
            "period_notes": periods, "iterations": iters}


def _node_replan(state: SupervisorState) -> dict:
    """Replanner：依已完成待辦的局部結果動態增刪清單（新增 web 待辦 / drop 多餘 / 提前收斂）。"""
    todos = [dict(t) for t in state["todos"]]
    freshness_mode = state.get("freshness_mode", FRESHNESS_LIVE)
    _live = freshness_mode == FRESHNESS_LIVE
    lines = []
    for t in todos:
        res = t.get("result", "") or ""
        res = (res[:200] + "…") if len(res) > 200 else res
        # ⚠ `web_used` 一直就在 todo dict 上，只是從來沒進過 prompt（理由與代價見
        #   `_REPLANNER_LIVE_BLOCK` 上方）。snapshot 不會有 web_used=True,所以那邊的
        #   prompt bytes 不受影響。
        flag = " [已用過網路搜尋]" if (_live and t.get("web_used")) else ""
        lines.append(f"- id={t['id']} [{t['status']}]{flag} {t['task']}"
                     + (f"\n    局部結果:{res}" if res else ""))
    user = f"原始問題:{state['query']}\n\n目前待辦清單:\n" + "\n".join(lines)
    # 重放快取（見 llm_replay）：未設 RAG_REPLAY_CACHE 時完全 no-op。
    # ⚠ **2026-08-29 補上——這是全碼庫唯一沒被錄下來的 LLM 呼叫**，而它正是「web fixture 的
    #   key 無界」那條鏈的源頭：replan 每輪重抽 → 生出措辭不同的 todo → 那個 todo 的 `check`
    #   key 是新的 → `new_query` 是新的 → 英譯是新的 → Tavily key 是新的 → `FixtureMiss`。
    #   實測 web-04 單輪 `hit=6 miss=7`,而且在非 strict 下是**靜默**掉回真 LLM。
    #   重錄 fixture 不會解決這件事——key 空間本來就無界,源頭沒釘死就會一直生出新的。
    # key 刻意**不含**兩樣東西：
    #   ① system prompt——它內嵌 `_build_temporal_contract()` 的 KB Coverage Snapshot,隨
    #      collection 變動,納入會讓跨 collection A/B 全部 miss（與 `_plan_subqueries` 同一個理由）。
    #   ② 各待辦的 `result` 自由文字——那是 executor 生成的摘要,每輪都不一樣,納入等於快取
    #      永不命中（＝這個修法歸零）。
    # ⚠ **代價要講清楚**：②意味著「同一份待辦清單、不同的局部結果」會共用同一個決策。
    #   這是 **fixture 用的重放**,不是通用函式快取——目的就是把待辦清單釘死好讓 web fixture
    #   打得到（同 llm_replay docstring：「固定下來的是某一次抽樣的結果,不是正確答案」）。
    #   `status` 有入 key,所以「做完了沒」這個層級的進展仍然分得開。
    # ⚠ `web_used` **必須入 key**：它現在會改變送進 prompt 的內容（見 `_REPLANNER_LIVE_BLOCK`）,
    #   不入 key 就會讓「已打過 web」與「還沒打」共用同一個決策——那正好把這個修法抵銷掉。
    #   原則同 `check` 的 key 含候選 chunk id：**凡是合法改變決策的東西都要入 key**。
    _rk = "|".join([freshness_mode, state["query"]]
                   + [f"{t['id']}:{t['status']}:{int(bool(t.get('web_used')))}"
                        f":{t.get('route', 'kb')}:{t['task']}"
                      for t in todos])
    _hit = _replay.get("replan", _rk)
    if _hit is not _replay.MISS:
        data = _hit
        _trace(f"replan(replay): sufficient={(data or {}).get('sufficient')} "
               f"add={len((data or {}).get('add') or [])} drop={(data or {}).get('drop') or []}")
    else:
        # live 才追加那一段；snapshot 的 replanner prompt 因此**逐字不變**（65 題基準不動）。
        system_prompt = (_REPLANNER_PROMPT + (_REPLANNER_LIVE_BLOCK if _live else "")
                         + "\n\n" + _build_temporal_contract(freshness_mode))
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]
        with _quiet():
            raw = rq.call_llm(messages, CHECKER_MODEL, temperature=0.0)
        data = _loads_json_lenient(raw)
        # 解析失敗（data is None）也照錄：那代表「這一輪 replan 什麼都沒做」,是個穩定的結果,
        # 重放時要重現同一件事。不錄的話一次暫時性的解析失敗會讓整輪不可重現。
        _replay.put("replan", _rk, data)

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
        # ⚠ 用**同一個** `_parse_plan_output` 正規化（唯一定義點）：它同時吃舊格式的
        #   `["字串"]`（既有 replay 快取錄的全是這種 → 落到 kb）與新格式的
        #   `[{"task":…, "route":…}]`。理由與 Planner 那邊逐字相同，見該函式 docstring。
        for a in _parse_plan_output(data.get("add", []) or []):
            if len(todos) < MAX_TODOS:
                task, a_route = a["task"], a["route"]
                # Prompt 是機率性約束；snapshot / --no-web 再用 Python 硬擋 web todo。
                # ⚠ **判準從 `_is_web_todo(task)` 換成 route 欄位**（2026-09-02）：那個詞表
                #   （`網路|上網|web search|internet`）匹配不到「在 Yahoo Finance 上查詢」
                #   「使用NASDAQ官方網站」——六個真實措辭逐字凍結在閘門⑧p。現在路由是欄位，
                #   不需要再從中文句子反推。
                if a_route in ("web", "both") and (freshness_mode == FRESHNESS_SNAPSHOT
                                                   or not ENABLE_WEB_SEARCH):
                    _trace(f"replan: 拒絕不符合時間模式的 web todo → {task!r}")
                    continue
                # intraday 且已打過 web → 追加任何待辦都必然徒勞（實測 18/18 零貢獻）。
                # ⚠ **刻意不加「這是不是 web 待辦」的前置條件**：intraday 且已搜過 web 之後，
                #   追加**任何**待辦都徒勞，不只 web 的。第二版曾用 `_is_web_todo(task)` 當前置
                #   條件而被詞表漏掉（六個措辭凍結在閘門⑧p）；那個函式已於 2026-09-02 刪除，
                #   但這裡的判準不變——**不要**改成 `a_route in ("web","both")`，那會讓
                #   「查一下 10-Q 有沒有提到」這種徒勞的 kb 待辦重新溜進來。
                if _web_retry_is_pointless(todos):
                    _trace(f"replan: 拒絕徒勞的追加待辦（intraday 且已搜過 web）→ {task!r}")
                    continue
                scope = _build_todo_temporal_scope(task, freshness_mode)
                todos.append({
                    "id": next_id,
                    "task": task,
                    "temporal_scope": scope,
                    # ⚠ **False，且這一格是刻意與 `_node_plan` 相反的**（2026-08-28）：replan 加的
                    # 待辦是機器對 Grader `missing` 的反應，不是使用者說過的話。實測 replanner 會生出
                    # 「改用網路搜尋查…」→「即時網路搜尋…」→「使用即時金融網站…」這種同義待辦串，
                    # 裡面夾帶的年份／filing type 都是腦補的。若標成 True，col-10 的假前提會換一扇門回來
                    # ——而閘門 ⑮f 抓不到（它驗的是 `rnd == 0` 這個條件還在，條件確實還在）。
                    "attributable": False,
                    # 路由：與 `_node_plan` 同一個欄位。舊 replay 快取錄的是純字串 →
                    # `_parse_plan_output` 把它們落到 "kb" ＝ 與加 route 之前相同的行為。
                    "route": a_route,
                    "depends_on": None,
                    "freshness_gaps": [],   # 同上：執行完由 _node_execute 回填
                    "period_notes": [],
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

    # web 區塊獨立成 web_extra：它要跟著**每一次重生成**走（validator 重寫時 extra_user 會被換掉），
    # 且 reflect 的稽核來源也要含它。只放進 extra 的話，web 資料只活在第一次生成。
    # ⚠ 標頭措辭與 `_WEB_SOURCE_AMENDMENT` 是一組的：修訂條款靠「網路搜尋結果」這個字串指認要修訂
    # 哪一段，兩邊改動要同步。舊版寫「非知識庫」會與修訂條款的 Rule 1 修訂互相矛盾，已改掉。
    web_extra = ""
    if web_notes:
        web_extra = ("\n\n=== 網路搜尋結果（即時檢索，已通過來源白名單；引用請用 [web: 網址]）===\n"
                     + "\n\n".join(web_notes))
    # 期間降級揭露：`rq.retrieve` 在 Tier 1 落空、降級 Tier 2 時產生（見 `_retrieve_chunks`）。
    # 走 `rq.build_user_prompt` 的第三參數 → 與單管線**同一個模板、同一個位置**，不另立措辭。
    # ⚠ 整篇只講一次，所以在 `_node_execute` 合併時就已跨子問題去重；這裡只負責串接。
    period_note = "\n".join(state.get("period_notes", []) or [])
    if period_note:
        _trace(f"synthesize: 期間降級揭露 {len(state.get('period_notes', []))} 筆 → 注入 Generator")
    extra = ""
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
        answer = _write_final_answer(state["query"], chunks, GEN_MODEL, extra_user=extra,
                                     web_extra=web_extra, period_note=period_note)
        answer = _validate_and_fix_citations(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                             web_extra=web_extra, period_note=period_note)
        # ⚠ 以下四道守門共用同一個判準 `rq.looks_like_refusal`（2026-08-28 從
        #   `answer.startswith("I don't have enough")` 換過來）。舊守門是**英文字面、只認開頭**,
        #   而 Writer 講中文——「知識庫中查無足夠資料…」整句穿得過去,於是一份剛說自己沒有依據
        #   的答案還會照樣付 `_extract_claims` ＋ `_reflect_and_fix` 至少兩次 LLM 呼叫去稽核它,
        #   而稽核對象裡沒有任何可稽核的東西。這與引用尾巴那次（`_compose_answer_tail`）是**同一個
        #   守門寫錯**,那次只改了尾巴、這四道漏改。
        #   ⚠ 危險方向不是漏判而是**判過頭**：真的有依據的答案被當成拒答 → 四道 validator 全部
        #   跳過 → 一致性/期別/數字溯源的保護一次消失。`looks_like_refusal` 的 150 字上限就是
        #   擋這件事的（長的誠實答案不算拒答）,誤報對照見 verify_answer_validators 閘門⑬b/⑭d。
        # 確定性一致性稽核（零 LLM 成本的偵測,只有真的抓到才花一次重生成）。放在 reflect 之前,
        # 讓 reflect 稽核的是已調和過的版本。
        if not rq.looks_like_refusal(answer):
            answer = _consistency_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                                web_extra=web_extra, period_note=period_note)
        # 確定性期別稽核（零 LLM 偵測）：答案自稱「最新一季」卻引用了較舊的期別 → 重生成一次。
        # 放在一致性之後、reflect 之前：期別改對可能連帶換掉數字，要讓 reflect 稽核最終版本。
        if not rq.looks_like_refusal(answer):
            answer = _period_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
        if state.get("enable_reflection", True) and not rq.looks_like_refusal(answer):
            answer = _reflect_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                      web_extra=web_extra, period_note=period_note)
        # 數字溯源（零 LLM 偵測）**放最後**：這是唯一會看 reflect 重生成結果的檢查。
        # 100 題乾跑誤報 0 題（見 find_untraceable_numbers 的兩條排除規則），所以放進主線不會
        # 擾動既有基準；真的觸發才花一次重生成。
        if not rq.looks_like_refusal(answer):
            answer = _number_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
        # R5 並陳時點（零 LLM 偵測）**放最末**：時點是措辭問題，而上面每一道的重生成都可能
        # 把時點改掉；放中間等於只驗了一個會被後面推翻的版本。281 份既有答案乾跑觸發 1 次
        # 且是真陽性（見 find_undated_dual_sourcing），所以不會擾動既有基準。
        if not rq.looks_like_refusal(answer):
            answer = _dual_source_check_and_fix(state["query"], answer, chunks, GEN_MODEL,
                                                verbose=verbose, web_extra=web_extra,
                                                period_note=period_note)
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
        # ⚠ **拒答判定要在附加任何聲明之前取**：`rq.looks_like_refusal` 帶 150 字上限，
        #   附完時效聲明再判，一份真正的拒答會因為變長而不再像拒答（守門靜默失效）。
        _was_refusal = rq.looks_like_refusal(answer)
        answer = answer.rstrip() + _format_unresolved_freshness_notice(state.get("todos", []))
        # R6（見 `web_fetched_but_uncited_notice`）：抓到了 web 卻一個都沒引用 → 揭露。
        # ⚠ 必須在這裡而不是 todo 層：`_format_unresolved_freshness_notice` 對 `web_used` 為真的
        #   待辦直接跳過，而這裡的病正好是「web_used 為真、答案卻沒引用」。
        if not _was_refusal:
            answer = answer.rstrip() + web_fetched_but_uncited_notice(answer, web_extra)
    # 口徑揭露：與時效聲明同一個位置、同一個理由（機械式附加，不要求 Writer 自己記得）。
    # ⚠ 判的是**答案實際引用**的 chunk，不是候選池——池裡有 TTM 但答案沒用，讀者一樣拿不到。
    _cited = _extract_citations(answer)
    _used = [c for c in (state.get("collected") or [])
             if (c.get("source"), c.get("chunk_index")) in _cited]
    answer = answer.rstrip() + _basis_disclosure_notice(
        state.get("query", ""), _used, answer, _todos_ratio_fields(state.get("todos")))
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
                "period_notes": [note_fb] if note_fb else []}

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
