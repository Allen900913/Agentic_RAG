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
# RETRIEVAL_MODEL：只做 retrieve 內部 filter/translate 這類「機械型」輕量呼叫，
# 與 CHECKER_MODEL（推理）、GEN_MODEL（生成）分離。
# 2026-08-14 從 gpt-oss-20b 改為 120b。原本的理由是「機械型輕量呼叫，用小模型求快」，
# 而 `eval/ablate_retrieval_model.py`（100 題零噪音對照）把那個前提推翻了：
#   · 速度：**20b 在 NIM 上慢一倍**（3.57s vs 1.76s，n=200/模型）——理由本身是反的。
#   · 品質：等價。gold recall 0.8459(20b) vs 0.8479(120b)，差 0.0020，而同一個模型跑
#     兩次的噪音就有 0.0014；完全撈不到 gold 的四題（col-07/lex-03/lex-07/lex-14）三臂
#     一模一樣；候選池雖然會換（Jaccard 0.897 vs 噪音 0.947）但換掉的都是無關 chunk。
# ⚠ 這會讓本次之後的 agentic 跑分與 2026-08-14 以前的前處理不同（雖已量到無作用）。
#   要重現舊跑分請設 env `AGENTIC_RETRIEVAL_MODEL=openai/gpt-oss-20b`。
RETRIEVAL_MODEL = os.getenv("AGENTIC_RETRIEVAL_MODEL", "openai/gpt-oss-120b")
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
POOL_RETURN_K     = int(os.getenv("AGENTIC_POOL_RETURN_K", "5"))   # 餵給 Checker 看的候選片段數（top-k by rerank）。env 可覆蓋供 ablation。
COMMIT_TOP_K      = int(os.getenv("AGENTIC_COMMIT_TOP_K", str(rq.DEFAULT_TOP_K)))   # 每個子問題 advance 時收進 collected 的 top-k。env 可覆蓋供 ablation。
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
TAVILY_MAX_RESULTS = 5            # 最終塞進 prompt 的則數
TAVILY_FETCH_RESULTS = 12         # 先多撈,再去重／限域名／濾過期,剩下的取前 TAVILY_MAX_RESULTS 則
TAVILY_PER_DOMAIN_CAP = 2         # 單一域名最多佔幾個名額（見 _dedupe_web_results 的理由）
# 每則保留的摘要字數。
# ⚠ **原本是 300,那是 2026-08-13 「即時市值答不出來」的真正主因**（比日期把關、比白名單都嚴重）：
#    Tavily 的 content 實測 596~1982 字,而數據頁的**數字排在站台樣板文字後面**,300 字正好切在
#    數字前一個字——trace 裡看到的 `Apple market cap as of Augus` 就是被切斷的 `$4572.79B`。
#    「資料撈到了卻被自己截掉」與「根本沒撈到」在舊 trace 裡長得一模一樣,是加了逐則印摘要才看見的。
WEB_CONTENT_CHARS = int(os.getenv("AGENTIC_WEB_CONTENT_CHARS", "1200"))
# web 來源白名單（2026-08-13 新增）。**這是授權清單，不是感知用的詞表**——它不試圖理解內容，
# 只決定「哪些來源准進來」，性質同 VALID_*_ITEMS，列清單正當（見 CLAUDE.md〈LLM 與 Python 的分工〉）。
#
# 為什麼非做不可：在此之前 `_tavily_search` 是全網無過濾，而同一批改動放行了「web 數字可被引用」。
# 「可引用」＋「來源不設限」才是真正危險的組合；先前只是因為 web 資料根本進不了答案而被遮住。
#
# 選法：只收「原始揭露方」與「有編輯流程的財經媒體」，不收論壇、內容農場、個人分析（Seeking Alpha
# 一類意見文與事實混排，只看 `WEB_CONTENT_CHARS` 那段摘要分不出來）。分兩類純粹是為了讓後人知道各自在守什麼。
# ⚠⚠ **這份清單必須完全公司無關（O(1)）**，不得再出現任何一家公司的 IR 主機名。
#
# 為什麼（2026-08-13 兩次事故，都出在「某家公司專屬的條目」）：
#   ① 寫 `apple.com`／`microsoft.com`（本意是 IR）→ Tavily 連子網域一起收 → 「蘋果的即時市值」
#      35 筆結果有 33 筆是 apps.apple.com 的《股市》App 頁、support.apple.com 的「在 iPhone 上
#      查看股市」、podcasts.apple.com 的節目，**零筆財經資料**，5 個名額全被佔滿。
#   ② 改成精確主機名（investor.apple.com…）雖然修掉①，但清單隨公司數 O(n) 成長，
#      **每加一家就是一次重複①的機會**（granularity 猜錯就靜默壞掉，且白名單仍會回 ~2KB
#      看起來很健康——這是「稽核回傳有東西也可能是壞消息」的一例）。
#
# 統一入口是 `sec.gov`：它涵蓋所有上市公司的 10-K/10-Q/8-K，而重大新聞稿本來就以 8-K 的
# EX-99 附件形式在裡面。實測佐證——`ir.tesla.com` 那次命中的網址路徑裡就有 `/sec/`，
# 那本來就是一份 SEC 文件、只是鏡像在 IR 站上。
# 代價：失去活動行事曆與未以 8-K 提交的新聞稿。若日後實測確認缺這類內容，再**帶著證據**單獨加回。
#
# ⚠ web fallback **不負責抓最新 10-Q**。filing 一律走 fetch_data.py → data_update_edgar.py
# 的 ingest 管線（見 CLAUDE.md〈抓取與處理分離〉）；讓 web 撈財報會繞過切塊六層、期間標籤與
# chunk 引用。KB 落後一季的正解是重跑 ingest。
_WEB_DOMAINS_PRIMARY = [        # 原始揭露／交易所級數據（全市場，非單一公司）
    "sec.gov", "nasdaq.com", "nyse.com",
    "stockanalysis.com", "companiesmarketcap.com", "macrotrends.net",
]
_WEB_DOMAINS_PRESS = [          # 有編輯流程的財經新聞
    "reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "ft.com",
    "cnbc.com", "barrons.com", "finance.yahoo.com", "marketwatch.com",
]
WEB_ALLOWED_DOMAINS = _WEB_DOMAINS_PRIMARY + _WEB_DOMAINS_PRESS


def _domain_admits(entry: str, host: str) -> bool:
    """Tavily `include_domains` 的比對語意：**一筆 entry 會連子網域一起收**。

    把這條語意寫成可執行的形式,是因為 2026-08-13 的白名單事故就出在它:憑直覺以為
    `apple.com` 只收官網,實際上 `apps.apple.com`／`support.apple.com` 全被收進來。
    `eval/verify_web_gate_isolation.py` 用它斷言那些消費端主機**不得**被放行。
    ⚠ 這是對 Tavily 行為的**建模**,不是 Tavily 的實作;若哪天它改了比對規則,這裡要跟著改。
    """
    host = (host or "").lower().strip().lstrip(".")
    entry = (entry or "").lower().strip().lstrip(".")
    if not host or not entry:
        return False
    return host == entry or host.endswith("." + entry)
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
# 「只有序號、沒有檔名」的引用：`【Reference 7, chunk #15】`。序號是**我們自己給的**
# （`rq.build_user_prompt` 產生 `[Reference i+1: source, chunk #idx]`），所以它可以被
# 確定性地還原成檔名——見 `_repair_reference_citations`。
_BARE_REF_CITE_RE = re.compile(
    r"([\[【])\s*reference\s*(\d+)\s*[,，]\s*chunk\s*#?\s*(\d+)\s*([\]】])", re.IGNORECASE)

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

# 路由：這個子問題該去哪裡拿資料。**封閉集合**——CLAUDE.md 說硬編碼詞表是警訊，
# 而「格式定義的封閉集合」是那條規則明列的正當例外（同 `VALID_*_ITEMS`）。
VALID_ROUTES = ("kb", "web", "both")

# ⚠ **兩個預設值刻意不同，因為它們回答的是不同的問題**（判準見 `_parse_plan_output`）：
#   · 舊格式的純字串 → `kb`：管的是**可重現性**。既有 fixture 是在「KB 先撈、web 當
#     fallback」的世界錄的；落到 web/both 會讓每一份既有重放都憑空多打網路 ＝ 基準不再可比。
#   · 新格式缺 route／填了非法值 → `both`：管的是**代價不對稱**（同 `realtime_need` 那條
#     「判不出來填 days 不填 none」）。誤判成 kb 會讓新聞題**拿財報冒充新聞且零揭露**，
#     那個失敗看不見；誤判成 both 只是多打一次網路。
_ROUTE_LEGACY_DEFAULT = "kb"
_ROUTE_UNCERTAIN_DEFAULT = "both"

# 時效需求分級 → 容忍幾天（2026-08-13）。**分工**：「這題要多新」沒有唯一機械答案 → 交給 Grader
# 判（`realtime_need` 欄位，只在 live 模式問）；「來源多舊」是確定性的 → 交給 Python 從檔名算。
# 單一天數門檻行不通：「今天股價漲跌」容忍 1 天，「最近有什麼進展」容忍一週，兩者差一個數量級。
REALTIME_STALE_DAYS = {
    "intraday": int(os.getenv("AGENTIC_STALE_DAYS_INTRADAY", "1")),   # 即時報價、今日漲跌、當前市值
    "days": int(os.getenv("AGENTIC_STALE_DAYS_RECENT", "7")),         # 近期新聞、最新進展
    "none": None,                                                     # 財報期間數字 → 永不過期
}


def _domain_of(url: str) -> str:
    """從網址取域名（去掉 www.）。純顯示用，失敗回原字串前段——絕不因為 log 而讓查詢炸掉。"""
    try:
        host = urlparse(url or "").netloc.lower()
        return host[4:] if host.startswith("www.") else (host or (url or "")[:28])
    except Exception:
        return (url or "")[:28]


# web 結果的「明顯過時」門檻（天）。**刻意比 REALTIME_STALE_DAYS 寬得多**,兩者做的是不同的事:
#   REALTIME_STALE_DAYS 判「KB 夠不夠新到可以不上網」——嚴格,寧可多上網一次。
#   WEB_STALE_DAYS     判「這則網頁是不是歷史文章」——寬鬆,因為即時題也需要幾天內的脈絡報導,
#                       用 1 天砍會把有用的近期報導一起砍光,只剩沒有日期的數據頁。
# 實測要擋的是 `cnbc.com/2020/08/19/apple-reaches-2-trillion-market-cap`（六年前）被當成現值,
# 那種東西超出任何合理門檻,不需要把門檻壓到天級。抽不出日期的一律**保留**(數據頁沒有日期,
# 而數據頁正是即時題最需要的)——濾掉「日期不明」等於濾掉正確答案。
WEB_STALE_DAYS = {
    # ⚠ intraday 原本設 90,實測太寬：「特斯拉今天股價」放進一則 **22 天前**的 WSJ 報導
    #   （$319.69 / −14.52%）,模型就把它當成「最新可得」寫進答案,反而把未標日期的即時
    #   行情頁（$327.51）降為次要。問當下數值時,**任何有日期的舊報導都不可能是答案**,
    #   留著只會製造更好聽的錯誤。7 天與 REALTIME_STALE_DAYS["days"] 同級,仍容得下脈絡報導。
    "intraday": int(os.getenv("AGENTIC_WEB_STALE_INTRADAY", "7")),
    "days": int(os.getenv("AGENTIC_WEB_STALE_RECENT", "180")),
    "none": None,   # 問特定財報期間 → 歷史文章本來就正當,不濾
}

# 網址裡的日期。**確定性、公司無關、格式定義**——屬於「比對／定位給 Python」那一側。
_URL_DATE_PATTERNS = (
    # `/2020/08/19/`（CNBC 式）與 `-2026-08-11/`（Reuters／AP 式，日期在網址結尾）都收
    (re.compile(r"[/-](20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$)"), (1, 2, 3)),
    (re.compile(r"[/-](\d{1,2})-(\d{1,2})-(20\d{2})(?:[/-]|$)"), (3, 1, 2)),      # -07-28-2026（wsj livecoverage）
    (re.compile(r"[/-](20\d{2})(\d{2})(\d{2})(?:[/-]|$)"), (1, 2, 3)),            # -20260728-
    (re.compile(r"/(20\d{2})[/-](\d{1,2})(?:[/-]|$)"), (1, 2, 0)),                # /2020/08/ → 當月 1 日
)


def _url_published_date(url: str) -> date | None:
    """從網址推發布日。抽不到回 None（**不是缺陷**：數據頁本來就沒有日期）。
    取第一個成功的樣式；日期不合法（月份 13、日 32）就當抽不到,不 raise。"""
    for pat, (yi, mi, di) in _URL_DATE_PATTERNS:
        m = pat.search(url or "")
        if not m:
            continue
        try:
            return date(int(m.group(yi)), int(m.group(mi)), int(m.group(di)) if di else 1)
        except (ValueError, IndexError):
            continue
    return None


# 內容裡的日期**只在發布／報價時間標記旁邊**才算數（2026-08-29）。
#
# 為什麼需要這一層：2026-09-01 實跑一次真 Tavily（`What is NVIDIA's current share price?`）：
#   **12 則結果的 `published_date` 全部是 None**，網址是 `/quote/NVDA` 也推不出日期
#   → 每一則都印「（未標示日期）」，而同一行內容寫著 `REAL TIME 11:49 AM EDT 08/31/26`。
#   後果是連鎖的：`_dedupe_web_results` 的過時過濾整個失效（抽不出日期一律保留），
#   同一個池子裡 08/18、08/28、08/31 三個不同日期的價格沒有任何東西替它們排序。
#
# ⚠ **為什麼不是「找出任何日期」**：同一頁裡有大量**不是發布日**的日期——分析師評等日
#   （`Latest Rating Date 8/25/2026`）、**未來的**財報日與除息日（`Nov 17, 2026`／`Sep 10, 2026`）、
#   歷史表格列（`Dec 1, 2018`）。抓錯的方向是不對稱的：抓到**太新**的日期會讓過期頁冒充新鮮
#   並替整池背書，那正是 CLAUDE.md〈只有真實日曆日期能證明候選池夠新〉在防的事。
#   所以判準是**標記相鄰**＋**未來日期一律丟棄**，而且**不確定就回 None**（維持現狀，不猜）。
_CONTENT_DATE_MARKER = re.compile(
    r"(?:at\s+close|after\s+hours|pre[- ]?market|real\s?time|as\s+of|published(?:\s+on)?|"
    r"updated(?:\s+on)?|last\s+updated|posted(?:\s+on)?)\b", re.IGNORECASE)
# ⚠ 判準是**在第一個表格／區段邊界處截斷**，不是「窗口夠窄」。這是變異測試逼出來的：
#   `Pre-Market: 9:06:53 AM EDT [...] | Dec 8, 2025 |` 的 `Pre-Market` 後面**沒有日期**，
#   沒有邊界截斷就會收編隔壁表格的日期。那個例子剛好無害（誤收的比較舊，被 max 蓋掉），
#   但誤收到比較**新**的就是危險方向——頁面會冒充新鮮（閘門 ⑩n 就是那個形狀）。
# ⚠ **窗口不可以太窄**：24 會把 `| Aug 25, 2026` 截成 `| Aug 2` 而**合成出一個不存在的日期**
#   （Aug 2）。截半個 token 比截掉整個 token 危險。48 ＋ 邊界截斷則不會截在 token 中間。
#   實際需要的距離很短：`At close: August 31`＝1、`AT CLOSE 4:00 PM EDT 08/28/26`＝13。
_MARKER_WINDOW = 48
_SECTION_BREAK = re.compile(r"[|\[\]#]")   # 表格欄位／`[...]` 省略段／標題，日期跨過去就不是這個標記的

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_CONTENT_DATE_PATTERNS = (
    # `August 31 at 4:00` / `Aug 28, 2026`：**年份可省**（報價頁最常見的形狀）
    re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
               r"(?:\s*,?\s*(20\d{2}))?\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|20\d{2})\b"),      # 08/31/26、08/31/2026
    re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"),                # ISO
)


def _content_published_date(content: str, as_of: date) -> date | None:
    """從 web 結果的內容抽發布／報價日。**只認標記相鄰的日期，且丟棄未來日期。**

    年份省略時（`At close: August 31`）補上「**不晚於 as_of 的最近一次**」——絕不外推到未來。
    多個候選取 **max**：報價頁把最新一次收盤排在最前面，新聞頁的 `Updated` 也晚於 `Published`。
    一個都不合格 → None（與加這層之前完全相同的行為）。
    """
    text = content or ""
    cands: list[date] = []
    for m in _CONTENT_DATE_MARKER.finditer(text):
        window = text[m.end():m.end() + _MARKER_WINDOW]
        brk = _SECTION_BREAK.search(window)     # 跨過表格／區段邊界的日期不算這個標記的
        if brk:
            window = window[:brk.start()]
        for pat in _CONTENT_DATE_PATTERNS:
            hit = pat.search(window)
            if not hit:
                continue
            g = hit.groups()
            try:
                if pat is _CONTENT_DATE_PATTERNS[0]:
                    mo, day = _MONTHS[g[0][:3].lower()], int(g[1])
                    yr = int(g[2]) if g[2] else as_of.year
                    d = date(yr, mo, day)
                    if not g[2] and d > as_of:      # 沒寫年份且落在未來 → 是去年的同一天
                        d = date(yr - 1, mo, day)
                elif pat is _CONTENT_DATE_PATTERNS[1]:
                    yr = int(g[2])
                    d = date(yr + 2000 if yr < 100 else yr, int(g[0]), int(g[1]))
                else:
                    d = date(int(g[0]), int(g[1]), int(g[2]))
            except (ValueError, KeyError):
                continue
            if d <= as_of:          # ⚠ 未來日期一律丟棄：財報日／除息日不是發布日
                cands.append(d)
            break                   # 這個標記已經有解，換下一個標記
    return max(cands) if cands else None


def _web_result_date(r: dict, as_of: date | None = None) -> date | None:
    """一則 web 結果的日期：`published_date` → 網址推斷 → **內容裡的標記相鄰日期**。
    ⚠ 實測（2026-08-13）：只有 `topic="news"` 會回 `published_date`,而 news 模式**拿不到數據頁**
      （macrotrends／stockanalysis／companiesmarketcap 全消失）,即時報價題要的正是數據頁。
      所以生產走預設 topic ＋ 網址推斷；這裡仍先讀 `published_date`,將來若改 topic 不必再動這裡。
    ⚠ 第三段是 2026-08-29 補的,理由見 `_content_published_date` 上方——在那之前
      真 Tavily 的報價題**12/12 全部印「未標示日期」**,過時過濾等於沒有。"""
    raw = (r.get("published_date") or "").strip()
    if raw:
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", raw)   # 任何帶 ISO 日期的變體
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return (_url_published_date(r.get("url") or "")
            or _content_published_date(r.get("content") or "", as_of or _get_as_of_date()))


def _normalize_url(url: str) -> str:
    """去重用的正規化鍵：拿掉協定／www.／amp 路徑段／query／結尾斜線,子網域前綴 `new.`／`m.` 也去掉。
    要擋的是同一頁的多種寫法佔掉多個名額——實測同一次搜尋同時回了
    `cnbc.com/2020/…` 與 `cnbc.com/amp/2020/…`、`www.macrotrends.net/…` 與 `new.macrotrends.net/…`。"""
    try:
        p = urlparse(url or "")
        host = (p.netloc or "").lower()
        for pre in ("www.", "new.", "m.", "amp."):
            if host.startswith(pre):
                host = host[len(pre):]
        path = re.sub(r"/amp(?=/|$)", "", (p.path or "").lower()).rstrip("/")
        return host + path
    except Exception:
        return (url or "").lower()


# 衍生性商品頁的網址樣式。**OCC 選擇權代號是標準化格式**（`{代號}{YYMMDD}{C|P}{8 位履約價}`,
# 例 `TSLA260814C00257500` ＝ 2026-08-14 到期、履約價 $257.50 的買權），屬於格式定義的封閉集合,
# 用樣式排除正當（同 CLAUDE.md 對 `VALID_*_ITEMS` 的例外）。
# 為什麼要擋：它與「外國掛牌」同一類——**拿到的是別的標的**。選擇權頁上的價格是權利金,
# 被當成股價就是數量級的錯。實測「特斯拉今天股價」一次跑分有 3 個名額被選擇權合約頁佔走。
_DERIVATIVE_URL_RE = re.compile(r"/[A-Z]{1,6}\d{6}[CP]\d{6,8}(?:[/?]|$)")


def _is_derivative_page(url: str) -> bool:
    """網址看起來是選擇權／衍生性商品合約頁？只認 OCC 標準格式,認不出就回 False（保守側）。"""
    return bool(_DERIVATIVE_URL_RE.search(url or ""))


def _host_allowed(host: str) -> bool:
    """**本地端**的白名單複核：只認 entry 本身或它的 `www.` 形式，**不認任意子網域**。

    為什麼要在 Tavily 的 `include_domains` 之外再擋一層：Tavily 的比對是子網域包含式的
    （`_domain_admits` 模型化了這件事），而**地區子網域拿到的是別的市場的報價**——
      `ca.finance.yahoo.com/quote/TSLA.NE`（加拿大 NEO）、`finance.yahoo.com/quote/TL0.SG`（新加坡）、
      `cn.wsj.com`（實測給出多年前的 Apple／MSFT 市值對比,直接害答案說市值「下降」）。
    這與 2026-08-13 `apple.com` 收進 `apps.apple.com` 是同一個缺陷類,只是換一家。
    白名單本身已是精確主機名,所以「exact ＋ www」不會誤殺（`www.sec.gov`／`www.reuters.com` 都過）。
    ⚠ 這擋的是**子網域**；同一主機下的外國掛牌路徑（`stockanalysis.com/quote/bvl/AAPL`）擋不到,
      見 BACKLOG〈web 外國掛牌〉。"""
    host = (host or "").lower().strip().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host in {e.lower().lstrip(".") for e in WEB_ALLOWED_DOMAINS}


def _dedupe_web_results(results: list[dict], need: str, as_of: date) -> tuple[list[dict], dict]:
    """對 Tavily 原始結果做三道**確定性**過濾,回傳 (保留的結果, 統計)。順序即 Tavily 的相關性排序。
      ⓪ 本地端白名單複核（`_host_allowed`，擋地區子網域）＋ 衍生性商品頁排除（`_is_derivative_page`）
      ① 同頁去重（`_normalize_url`）
      ② 單域名上限 `TAVILY_PER_DOMAIN_CAP`——實測 `companiesmarketcap.com` 用五種幣別變體
         （$USD／A$AUD／C$CAD／€EUR…）**吃掉全部 5 個名額**,五則內容一模一樣且都不含 Apple 數字,
         等於整次 web 搜尋作廢。這是通用現象（同站多變體頁),不是某一家的問題。
      ③ 明顯過時（`WEB_STALE_DAYS`）；抽不出日期一律保留。
    三道都不看內容、不看公司名,純結構。"""
    limit = WEB_STALE_DAYS.get(need)
    kept: list[dict] = []
    seen: set[str] = set()
    per_domain: dict[str, int] = {}
    stats = {"dup": 0, "domain_cap": 0, "stale": 0, "dated": 0, "off_host": 0, "derivative": 0}
    for r in results:
        url = (r.get("url") or "").strip()
        key = _normalize_url(url)
        if not url or key in seen:
            stats["dup"] += 1
            continue
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""
        if not _host_allowed(host):
            stats["off_host"] += 1
            _trace(f"    web ✗ 非白名單主機（多為地區子網域）{host} {url[:60]}")
            continue
        if _is_derivative_page(url):
            stats["derivative"] += 1
            _trace(f"    web ✗ 衍生性商品合約頁（非該股票）{url[:70]}")
            continue
        dom = _domain_of(url)
        if per_domain.get(dom, 0) >= TAVILY_PER_DOMAIN_CAP:
            stats["domain_cap"] += 1
            continue
        d = _web_result_date(r, as_of)
        if d:
            stats["dated"] += 1
            if limit is not None and (as_of - d).days > limit:
                stats["stale"] += 1
                _trace(f"    web ✗ 過時 {(as_of - d).days} 天（>{limit}）{_domain_of(url)} {url[:60]}")
                continue
        seen.add(key)
        per_domain[dom] = per_domain.get(dom, 0) + 1
        r["_pub_date"] = d
        kept.append(r)
        if len(kept) >= TAVILY_MAX_RESULTS:
            break
    return kept, stats


def _source_newest_date(source: str) -> date | None:
    """從來源檔名推它「最新可能」的日期。`TICKER_KIND_STAMP`，stamp 為 4/6/8 碼。

    刻意取**該期間的最後一天**（4 碼→12/31、6 碼→月底、8 碼→當天）：寧可高估新鮮度也不要誤判過期。
    誤判過期會叫 web 白跑一趟，誤判新鮮只是維持現狀，前者才是我們要避免的成本。
    ⚠ 10-K/10-Q 的 stamp 是**財報期間**不是發布日，所以 `NVDA_10K_2026` 會被算成 2026-12-31＝
    永遠不過期。這是刻意的：財報數字本來就不該因為 wall clock 走了就被判為需要上網補。
    """
    m = _COVERAGE_SOURCE_RE.match(source or "")
    if not m:
        return None
    stamp = m.group("stamp")
    try:
        if len(stamp) == 8:
            return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        if len(stamp) == 6:
            y, mo = int(stamp[:4]), int(stamp[4:6])
            return date(y, mo, calendar.monthrange(y, mo)[1])
        if len(stamp) == 4:
            return date(int(stamp), 12, 31)
    except ValueError:
        return None
    return None


# 「這一筆來源能不能證明候選池夠新」＝ stamp 是不是**真實日曆日期**（8 碼）。
# 10-K 的 4 碼／10-Q 的 6 碼是**財報期間**不是發布日：`MSFT_10K_2026` 會被
# `_source_newest_date` 算成 2026-12-31，那是未來。
NO_REALTIME_SOURCE = -1        # `_stale_for_realtime` 的哨符，見該函式 docstring


def _is_freshness_evidence(source: str) -> bool:
    """這一筆來源可否作為「候選池夠新」的證據（只有 8 碼真實日期算數）。"""
    m = _COVERAGE_SOURCE_RE.match(source or "")
    return bool(m) and len(m.group("stamp")) == 8


def _stale_for_realtime(need: str, chunks: list[dict], as_of: date) -> int | None:
    """候選裡**最新**的來源距今幾天算過期？

    回傳：天數（過期）／`NO_REALTIME_SOURCE`(-1，池裡沒有任何能證明新鮮度的來源)／
    None（夠新／`need="none"` 無此需求）。

    用最新那一筆而非全部：只要池裡有一筆夠新就不該叫 web。

    ⚠ **2026-08-19 推翻了一個先前刻意的決定**，理由是 KB 拔除新聞後成本算式反轉了：
      舊行為是「財報永不過期」＋ 全池取 `max(dates)`。那讓財報不只是**棄權**，而是
      **替整個候選池背書說夠新**——「永不過期」與「能證明夠新」被混成同一件事。
      新聞還在時無害（池裡有真日期的新聞 chunk 壓著）；新聞拔掉後財報成了唯一日期來源，
      於是 `MSFT_10K_2026`(→2026-12-31，未來) 讓「微軟現在的股價是多少？」**判不出過期、
      web 不會被叫**。實測 6 題有 4 題如此，含兩題 intraday。
      ⚠ 其中 3 題**與拔除新聞無關**（從來沒被 `looks_like_news_query` 攔過）＝ 既有的洞。
      舊註解寫「誤判過期只是叫 web 白跑一趟，誤判新鮮只是維持現狀，前者才是要避免的」——
      **那句話在 KB 有新聞時成立**。現在「維持現狀」＝ 拿 10-K 回答今天股價，成本大得多。
    """
    limit = REALTIME_STALE_DAYS.get(need)
    if limit is None:
        return None            # need="none"：財報數字不因 wall clock 走了就要上網補
    dates = [d for d in (_source_newest_date(c.get("source") or "") for c in chunks
                         if _is_freshness_evidence(c.get("source") or "")) if d and d <= as_of]
    if not dates:
        # 池裡沒有任何真實日期來源（現在的 KB：只有 Fundamentals 有）→ 無法證明夠新。
        # 對 intraday／days 來說「證明不了夠新」就該上網，不是維持原判。
        return NO_REALTIME_SOURCE
    age = (as_of - max(dates)).days
    return age if age > limit else None


# ── 財報期別排序（跨 10-K/10-Q，只在同一家公司內比較）────────────────────────────
#
# ⚠ **不能用 `_source_newest_date()` 做這件事**。它把 `TICKER_10K_YYYY` 一律算成該年 12/31
#   （刻意高估新鮮度，見它的 docstring），而各家財年結束月份不同：
#     NVDA 財年 1 月底結束 → `NVDA_10K_2026`(→2026-12-31) 會被算得比真正更新的
#     `NVDA_10Q_202604`(FY2027 Q1) 還新，**排序直接反轉**。
#   payload 的 (fiscal_year, fiscal_period) 才是精確的（2026-08-15 實測）：
#     MSFT_10K_2026 = (2026, FY) > MSFT_10Q_202603 = (2026, Q3)   ← web-03 要抓的就是這組
#     NVDA_10K_2026 = (2026, FY) < NVDA_10Q_202604 = (2027, Q1)   ← 不能誤報成這組
#
# FY 排在 Q4 之後：10-K 涵蓋整個財年，是那一年的最後一期。
# Q1~Q4/FY 是**格式定義的封閉集合**（SEC 就這幾種），列清單正當——見 CLAUDE.md〈硬編碼詞表〉的例外。
_FISCAL_PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}
_FISCAL_PERIOD_LABEL = {v: k for k, v in _FISCAL_PERIOD_ORDER.items()}


def _fiscal_rank(source: str) -> tuple[int, int] | None:
    """source → (fiscal_year, 期別序)，可直接比大小。

    非財報檔、或期別欄位缺漏 → 回 None，代表**不參與比較**（寧可漏判也不製造誤報）。
    """
    rec = (_get_kb_coverage().get("sources") or {}).get(source)
    if not rec:
        return None
    order = _FISCAL_PERIOD_ORDER.get(str(rec.get("fiscal_period") or "").upper().strip())
    year = re.sub(r"\D", "", str(rec.get("fiscal_year") or ""))
    if order is None or len(year) != 4:
        return None
    return int(year), order


def _fiscal_label(rank: tuple[int, int]) -> str:
    return f"FY{rank[0]} {_FISCAL_PERIOD_LABEL.get(rank[1], '?')}"


def _kb_ceiling_date(chunks: list[dict]) -> date | None:
    """這些 ticker 在**整個 collection** 裡最新能提供到哪一天（與本次檢索撈到什麼無關）。

    ⚠ 存在理由：`_stale_for_realtime()` 量的是**候選池**裡最新那筆,而候選池是語意檢索的結果
      ——池子裡最新是 60 天前,不代表 collection 沒有 3 天前的,很可能只是這輪措辭沒命中。
      兩者一比就能把兩種不足分開：**檢索沒撈到**（改寫有救）vs **KB 根本沒有**（改寫沒救）。
      少了這個天花板,前者會被誤判成後者,白白跳過還有救的改寫。

    刻意與 `_stale_for_realtime()` 共用 `_source_newest_date()`：兩邊的日期必須是同一套算法,
    否則比出來的大小沒有意義。
    """
    cov = _get_kb_coverage()
    if not cov.get("available"):
        return None                      # 掃不到 → 無法證明「還有更新的」,不主張可修
    tickers = {str(c.get("ticker") or "").upper().strip() for c in chunks}
    tickers.discard("")
    # ⚠ 與 `_stale_for_realtime` 共用**同一套資格判準**（`_is_freshness_evidence`）：
    #   兩邊若一邊算財報、一邊不算，比出來的大小沒有意義（見本函式 docstring）。
    dates = [d
             for t in tickers
             for record in (cov.get("tickers", {}).get(t) or {}).values()
             for src in (str(record.get("source") or ""),)
             if _is_freshness_evidence(src)
             for d in (_source_newest_date(src),)
             if d]
    return max(dates) if dates else None


def _classify_staleness(need: str, chunks: list[dict],
                        as_of: date) -> tuple[int | None, bool]:
    """回傳 (候選池最新來源過期幾天 or None, 這種不足 KB 補不補得了)。

    抽成獨立函式是為了**讓閘門能零 LLM 測到這個判斷**——它原本內嵌在 `_check_sufficiency()`
    的 LLM 回傳處理裡,測不到就等於沒有被證偽過。
    """
    stale_days = _stale_for_realtime(need, chunks, as_of)
    if stale_days is None:
        return None, False
    limit = REALTIME_STALE_DAYS.get(need)
    ceiling = _kb_ceiling_date(chunks)
    # 天花板本身也過期 → 再怎麼改寫都是同一批檔案,沒救；天花板夠新 → 是這次沒撈到,還有救
    unfixable = ceiling is None or limit is None or (as_of - ceiling).days > limit
    return stale_days, unfixable


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
        # source → 該檔的財報期別（只收 10-K/10-Q）。**沿用同一次 scroll**，不多掃一遍。
        # 存在理由：`rq.retrieve()` 回傳的 chunk dict 只帶 source/chunk_index/ticker/chunk_type，
        # **沒有 fiscal_year／fiscal_period**（2026-08-15 實測），而期別排序非有它不可
        # （見 `_fiscal_rank`）。要嘛在這裡建表，要嘛去改全專案共用的 retrieve 投影——選前者。
        "sources": {},
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
                if kind in ("10-Q", "10-K"):
                    coverage["sources"][source] = {
                        "ticker": ticker, "kind": kind,
                        "fiscal_year": record["fiscal_year"],
                        "fiscal_period": record["fiscal_period"],
                    }
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


# ratio 意圖 → Fundamentals 檔裡的**欄位名**。這是**格式定義的封閉集合**（欄位名由本專案自己的
# ingest 產生，見 data/edgar_processed/Fundamentals/*.txt），不是拿字串比對做感知，
# 屬於 CLAUDE.md〈硬編碼詞表是警訊〉明列的正當例外。
#
# **為什麼需要它**（2026-08-20 實測，推翻了 BACKLOG 掛了 9 天的「未驗證」假設）：
#   MSFT_Fundamentals #0（698 字元）含 Revenue Growth (YoY) 18.30%、Gross Margin 68.31%、
#   Operating Margin 46.33%、Profit Margin 39.34% ——**每一個比率都在這裡**；
#   #1（2045 字元）只有 Total Cash／Total Debt，**一個比率都沒有**。
#   而 cross-encoder 對「Microsoft 的毛利率是多少？」把 **#1 排在 #0 前面**（0.928 vs 0.918）。
#   → `_ensure_ratio_source_coverage` 原本補「該家分數最高的 Fundamentals」，補進來的正是
#     那個一個比率都沒有的 #1；Generator 因此沒有 TTM 值可引，退回 10-K/10-Q 的財年／單季數字
#     （口徑不同、數值接近、無揭露）。這就是 mi-05／lex-17 的真因。
#   舊記載猜的是「#1 長三倍造成 rerank 偏好」——**方向對了，但真正的傷害不在 rerank 本身，
#   而在保底機制用「分數最高」而不是「有沒有那個欄位」來挑**。
_RATIO_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    (r"毛利率|gross\s*margin", "Gross Margin"),
    (r"營業利益率|营业利益率|營業利潤率|operating\s*margin", "Operating Margin"),
    (r"淨利率|净利率|淨利潤率|净利润率|net\s*margin|profit\s*margin", "Profit Margin"),
    (r"營收成長|营收成长|營收增長|营收增长|成長率|成长率|growth\s*rate", "Revenue Growth"),
)


def _wanted_ratio_fields(task: str) -> list[str]:
    """這個子問題問的是 Fundamentals 的哪幾個欄位。認不出來就回空 list ＝ 退回舊行為（挑分數最高）。"""
    return [f for pat, f in _RATIO_FIELD_HINTS if re.search(pat, task or "", re.IGNORECASE)]


def _is_ratio_intent(task: str) -> bool:
    """子問題是否問「可從 10-K/10-Q 現算、故有雙來源」的比率/成長率指標（毛利率/淨利率/成長率…）。
    這類指標 Fundamentals 已預算好寫死值，應以 Fundamentals 為錨、繞開 10-Q 現算路徑（路由非算術）。
    單一來源指標（市值/P/E/ROE/EPS/自由現金流）不觸發：無雙來源擲硬幣風險，補了也只是多餘。"""
    return bool(_RATIO_INTENT_RE.search(task or ""))


# ── ratio 意圖：LLM 判定（詞表退位成 fallback）────────────────────────────────
# **為什麼要改**（2026-08-25，BACKLOG 殘留①）：上面那條 `_RATIO_INTENT_RE` 是拿字串比對
#   做感知，正是 CLAUDE.md〈硬編碼詞表是警訊〉講的形狀。實測 `col-11` 連跑四輪，Planner
#   把子問題寫成「Microsoft 的營收成長得快不快？」時整條 ratio 機制**靜默繞過**——不是
#   補撈失敗，是根本沒進到補撈。往詞表加「快不快」只會換一個措辭再漏一次。
# **判準**（CLAUDE.md〈LLM 與 Python 的分工〉）：「這句話想知道哪個量」沒有唯一機械答案
#   → LLM；「那個量在 Fundamentals 叫什麼欄位名」是封閉集合 → 由 enum 收斂。所以 LLM 只
#   被允許從 `_RATIO_FIELD_ENUM` 裡挑，挑到集合外的一律丟掉。
# ⚠ **為什麼不擴 `_PLANNER_PROMPT` 而是另外一次 call**：planner prompt 一動，子問題拆解
#   本身就會漂（輸出格式從 `["字串"]` 變成物件陣列），**65 題每一題的檢索池都會跟著變**，
#   等於把被測項和基準一起搬走。多付一次輕量 call 換 planner prompt 逐字不變，是這裡唯一
#   划算的交易（同 `verify_web_gate_isolation` 對 Checker prompt 的 byte-identical 斷言）。
# ⚠ **fallback 會遮住 LLM 的失手**：解析失敗回 None → 退回詞表，行為與舊碼完全相同。
#   所以「LLM 判得準不準」不能從端到端結果推，要用 `eval/probe_ratio_intent.py` 直接量。
_RATIO_FIELD_ENUM: tuple[str, ...] = tuple(f for _, f in _RATIO_FIELD_HINTS)

_RATIO_INTENT_PROMPT = """你是財務問句的欄位分類器。給你一組編號的「子問題」，逐一判斷它想知道的
是不是本知識庫 Fundamentals 檔裡那四個**預先算好的比率欄位**之一。

可選欄位（只能從這四個挑，不可自創、不可改寫）：
- "Gross Margin"      毛利率
- "Operating Margin"  營業利益率／營業利潤率
- "Profit Margin"     淨利率／純益率／利潤率／獲利率
- "Revenue Growth"    營收成長率（YoY）

判準是**問題想知道的那個量**，不是它用了哪些字——口語、比喻、反問、間接問法都要照樣判：
「營收成長得快不快」問的就是 Revenue Growth；「每賺一塊錢留下多少」問的就是 Profit Margin。

問到多個就都列。問的**不是**這四個比率時回空陣列，例如：
- 市值／股價／本益比／ROE／EPS／自由現金流／現金與負債 → []
- 營收「金額」是多少、獲利「金額」多少 → []（那是金額不是比率／成長率）
- 風險、策略、競爭、產品、業務描述 → []

只輸出一個 JSON 陣列，長度與子問題數完全相同，第 i 個元素是第 i 題的欄位陣列。
不要輸出任何其他文字。例（輸入兩題）：[["Revenue Growth"],[]]"""


def _classify_ratio_fields(tasks: list[str]) -> list[list[str] | None]:
    """一次 LLM call 判定每個子問題問的是哪幾個 Fundamentals 比率欄位。

    回傳與 `tasks` 等長的清單，每格是 `list[str]`（**空 list 是有效答案＝判定不是 ratio 題**）
    或 `None`（＝LLM 沒表態，呼叫端須退回詞表）。**這兩者不可混為一談**：空 list 要能壓過
    詞表，否則詞表仍然是實際做決定的人，這次改動就只是裝飾。"""
    if not tasks:
        return []
    _rk = "\n".join(tasks)
    _hit = _replay.get("ratio", _rk)
    if _hit is not _replay.MISS:
        return [None if x is None else list(x) for x in _hit]
    user = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(tasks))
    try:
        with _quiet():
            raw = rq.call_llm(
                [{"role": "system", "content": _RATIO_INTENT_PROMPT},
                 {"role": "user", "content": user}],
                RETRIEVAL_MODEL, temperature=0.0,
            )
        data = _loads_json_lenient(raw)
        if not isinstance(data, list) or len(data) != len(tasks):
            raise ValueError(f"shape mismatch: {data!r}")
        out: list[list[str] | None] = []
        for item in data:
            if not isinstance(item, list):
                raise ValueError(f"element not a list: {item!r}")
            out.append([f for f in _RATIO_FIELD_ENUM if f in item])   # enum 外一律丟掉
    except Exception as exc:
        _trace(f"ratio-intent: LLM 判定失敗 {exc!r} -> 退回詞表")
        return [None] * len(tasks)
    _trace(f"ratio-intent: {list(zip(tasks, out))}")
    _replay.put("ratio", _rk, out)
    return out


def _resolve_ratio_fields(fields: list[str] | None, task: str) -> list[str]:
    """要撈的 Fundamentals 欄位：LLM 表態過就以它為準（**含空 list**），沒表態才退回詞表。"""
    if fields is not None:
        return [f for f in _RATIO_FIELD_ENUM if f in fields]
    return _wanted_ratio_fields(task)


def _has_ratio_intent(fields: list[str] | None, task: str) -> bool:
    """ratio 意圖：LLM 表態過就是「有沒有挑到欄位」，沒表態才退回詞表。

    ⚠ 這裡把「意圖」與「欄位」**合成同一個訊號**，是刻意的：舊碼允許 `_is_ratio_intent`
      為真而 `_wanted_ratio_fields` 為空（兩份詞表不同步，例如「獲利率」在前者不在後者），
      而那個組合會讓 `_ensure_ratio_source_coverage` 退回「挑分數最高的 Fundamentals」——
      那正是 mi-05／lex-17 的真因。走 LLM 這條路時不再有那個縫。"""
    if fields is not None:
        return bool(_resolve_ratio_fields(fields, task))
    return _is_ratio_intent(task)


def _todos_ratio_fields(todos: list[dict] | None) -> list[str] | None:
    """把各子問題的欄位判定收成一個聯集，供 Synthesize 端的揭露 validator 用。

    ⚠ 回 `None` 的條件是「**沒有任何一個** todo 帶著判定」（＝ LLM 整批失手，或這是一份
      舊格式的 state）——那時呼叫端會退回詞表、行為同舊碼。只要有一個 todo 表過態，就以
      LLM 的判定為準，即使聯集是空的：Synthesize 看的是原始問句，而原始問句正是詞表最會
      誤判的地方（「營收多少」被 `成長率` 以外的字樣掃到）。"""
    seen = False
    out: list[str] = []
    for t in todos or []:
        f = t.get("ratio_fields")
        if f is None:
            continue
        seen = True
        for x in f:
            if x in _RATIO_FIELD_ENUM and x not in out:
                out.append(x)
    if not seen:
        return None
    return [f for f in _RATIO_FIELD_ENUM if f in out]     # 順序穩定，方便斷言


def _fetch_fundamentals_with_field(ticker: str, fields: list[str], task: str) -> dict | None:
    """直接從 Qdrant 撈該公司「含指定欄位」的 Fundamentals chunk（零 LLM、只讀 payload）。

    ⚠ **為什麼保底不能只掃池**（2026-08-21 實測，這是 lex-17 的第二層真因）：
      `_ensure_ratio_source_coverage` 原本只在 `run_state.pool` 裡找，而 pool ＝ Qdrant
      server-side RRF 回傳的 `RRF_TOP_N_PRIMARY`(=20) 個候選。實測「Microsoft 的營收成長率
      表現如何」在生產組態（`full_translate_en=True`，英譯句
      "How is Microsoft's revenue growth rate performing?"）下，**`MSFT_Fundamentals #0`
      連候選名單都沒進**（進來的是零比率的 #1，rank 5）。也就是說：名字叫「保底」，
      實作卻是「希望它剛好在池裡」——池裡沒有的時候，它什麼也做不到。
      （中文原句反而撈得到 #0，rank 7。這不是 recall 調參能一勞永逸的方向，
       且 `RRF_TOP_N_PRIMARY` 碼上註明 sweep 過 20 > 40，不該為一題去動它。）

    所以補撈走**確定性查詢**而不是相似度：「這家公司的哪個 Fundamentals chunk 含
    Revenue Growth 欄位」有唯一正確答案 → 照 CLAUDE.md〈LLM 與 Python 的分工〉交給 Python。
    篩選鍵用 `period_basis == "TTM"`（全庫只有 Fundamentals 是 TTM，見
    `data_update_edgar._period_basis_for`，22 筆，且該欄位有 payload 索引）。

    ⚠ 分數用**真實的 cross-encoder 重算**，不要捏造：這個 `raw_rerank_score` 會被印在
      答案底下的引用區塊給使用者看，塞一個假數字等於出貨一個看起來可追溯的謊。
      （量尺與被測物耦合之外的另一種同型錯誤：讓「無法追溯」變成「看起來可追溯」。）
    """
    if not ticker or not fields:
        return None
    try:
        from qdrant_client import models as qmodels
        _bge, reranker, client = _get_models()
        pts, _ = client.scroll(
            collection_name=rq.COLLECTION_NAME,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="ticker", match=qmodels.MatchValue(value=ticker)),
                qmodels.FieldCondition(key="period_basis", match=qmodels.MatchValue(value="TTM")),
            ]),
            limit=64, with_payload=True, with_vectors=False,
        )
    except Exception as exc:                       # Qdrant 不可用時退回舊行為，不要讓保底變成故障點
        _trace(f"ratio-coverage: Fundamentals 補撈失敗 {exc!r}")
        return None

    cands = []
    for pt in pts:
        pl = pt.payload or {}
        if "Fundamentals" not in (pl.get("source") or ""):
            continue
        txt = pl.get("document") or ""
        if any(f.lower() in txt.lower() for f in fields):
            cands.append(pl)
    if not cands:
        return None
    scores = reranker.predict([[task, (pl.get("document") or "")] for pl in cands], batch_size=1)
    scores = scores.tolist() if hasattr(scores, "tolist") else list(scores)
    best = max(range(len(cands)), key=lambda i: scores[i])
    chunk = rq._payload_to_chunk(cands[best], 0.0, float(scores[best]))
    _trace(f"ratio-coverage: 補撈 {chunk['source']}#{chunk['chunk_index']} "
           f"（欄位 {fields}，rerank={chunk['raw_rerank_score']:.4f}）")
    return chunk


def _ensure_ratio_source_coverage(ranked: list[dict], selected: list[dict], want_tickers,
                                  task: str = "",
                                  ratio_fields: list[str] | None = None) -> list[dict]:
    """ratio 題財務錨源保底：保證 want_tickers 每一家在 selected 裡至少有一個 Fundamentals chunk；
    缺的就從 ranked（完整 rerank 降序池）補上該家分數最高的 Fundamentals，回傳仍按 rerank 降序。
    照 rq._ensure_ticker_coverage 的結構（只加不減）。零/未知 ticker 時原樣回傳。
    Fundamentals 靠 source 檔名判定（payload doc_type 實測常為空，見 probe）。"""
    want = [want_tickers] if isinstance(want_tickers, str) else list(want_tickers or [])
    if not want:
        return selected
    is_fund = lambda c: "Fundamentals" in (c.get("source") or "")
    # 要補的不是「隨便一個 Fundamentals」，是**含被問欄位的那一個**（見 _RATIO_FIELD_HINTS 上方的實測）。
    fields = _resolve_ratio_fields(ratio_fields, task)

    def _has_field(c: dict) -> bool:
        if not fields:
            return True                      # 認不出欄位 → 退回舊行為，不要更糟
        txt = c.get("content") or ""
        return any(f.lower() in txt.lower() for f in fields)

    # 已經有「含該欄位的 Fundamentals」才算 covered——原本只看有沒有 Fundamentals，
    # 於是 #1（Balance Sheet，零比率）也會被算成已覆蓋，保底機制當場失效。
    covered = {c.get("ticker", "") for c in selected if is_fund(c) and _has_field(c)}
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
        if t in missing and key not in seen and _has_field(c):
            out.append(c)
            seen.add(key)
            missing.discard(t)
    # 池裡沒有「含該欄位的 Fundamentals」→ 確定性補撈，讓保底真的是保底
    # （為什麼掃池不夠，見 _fetch_fundamentals_with_field 的 docstring）
    for t in sorted(missing):
        c = _fetch_fundamentals_with_field(t, fields, task)
        if not c:
            continue
        key = (c["source"], c["chunk_index"])
        if key in seen:
            continue
        out.append(c)
        seen.add(key)
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


def _build_todo_temporal_scope(task: str, freshness_mode: str) -> str:
    """為單一 todo 產生精簡的 coverage 說明（餵給 Grader 的 `temporal_scope`）。

    ⚠ 2026-08-15 起**不再回傳 freshness gap**。缺口改由執行完的實際結果算（`_news_freshness_gaps`），
      理由見那支的 docstring。這裡回傳單一字串而不是留一個永遠是空 list 的第二欄——
      留著等於是給下一個人一個會漂移的死欄位。
    """
    coverage = _get_kb_coverage()
    scope_coverage = _format_kb_coverage(coverage, _mentioned_tickers(task) or None)
    if freshness_mode == FRESHNESS_SNAPSHOT:
        return ("snapshot 模式：『最近／最新』= KB 中最新可用資料；不要參照 wall clock，"
                "不要加入 cutoff 警語。\n" + scope_coverage)
    return (f"live 模式：今天是 {_get_as_of_date().isoformat()}。不得把今天誤認成 KB cutoff；"
            "不得創造未出現在下方 snapshot 或工具結果中的期間。\n" + scope_coverage)


def _news_freshness_gaps(chunks: list[dict], as_of: date) -> list[dict]:
    """從**實際採用的 chunk** 算新聞時效缺口：用到了某家的 news、而該家 news cutoff 早於今天 → 一筆。

    ⚠ **2026-08-19 起對 KB 恆回空集合**（KB 已不收新聞,沒有 `doc_type=news` 的 chunk）。
    保留函式與 `eval/verify_answer_validators.py` 閘門④⑤ 的斷言,是為了「新聞復活時警語
    立刻回來」——那些斷言餵的是合成 chunk,不讀 KB,所以現在仍有判別力。
    ⚠ **但時效警語本身沒有跟著死**：它現在由 `_unmet_realtime_gaps` 供應（判準改成「這個
    子問題需要即時資料、卻沒拿到 web 補充」）。兩種缺口併存,見 `_run_one_todo` 的接線。

    ⚠ 2026-08-15 從「看問法」改成「看結果」。舊判準是兩個硬編碼詞表串聯：
          `bool(_RELATIVE_TIME_RE.search(task)) and rq.looks_like_news_query(task)`
      這正是 CLAUDE.md 明列的反模式（用字串比對做感知），而**漏網的代價是零揭露**：
      「Microsoft 最新一季的 Azure 營收成長率」過得了 `_RELATIVE_TIME_RE`（有「最新」）卻過不了
      `looks_like_news_query`（不是新聞措辭）→ 一句時效警語都不會印。
      （2026-08-13 拿掉的是 **web 觸發**那兩道同名閘門；警語這道當時漏改，文件卻已寫成「全數移除」。）

    改看結果之後判準是純比對、零詞表，而且更準：問法像新聞、答案其實全靠財報時，舊版會印一句
    無關的新聞 cutoff 警語，新版不會。cutoff 取 **KB coverage 裡該 ticker 最新的一則新聞**
    （警語講的是「KB 的新聞收到哪天」，不是「這次剛好引到哪一則」）。
    """
    if not chunks:
        return []
    cov = _get_kb_coverage()
    if not cov.get("available"):
        return []
    used = {parts[0] for c in chunks
            for parts in (_source_coverage_parts(str(c.get("source") or "")),)
            if parts and parts[1] == "news"}
    gaps: list[dict] = []
    for ticker in sorted(used):
        cutoff = _source_newest_date(
            str(((cov.get("tickers", {}).get(ticker) or {}).get("news") or {}).get("source") or ""))
        if cutoff and cutoff < as_of:
            gaps.append({"ticker": ticker, "cutoff": cutoff.isoformat(),
                         "as_of": as_of.isoformat(), "doc_type": "news"})
    return gaps


def _unmet_realtime_gaps(task: str, need: str, chunks: list[dict], as_of: date) -> list[dict]:
    """這個子問題**需要即時資料**（Grader 判 `realtime_need != none`）→ 記一筆時效缺口。

    ⚠ **2026-08-19 新增，補上 KB 拔除新聞後死掉的那道揭露。**
      舊的唯一缺口來源 `_news_freshness_gaps` 的判準是「用了 KB 新聞、而那則新聞過期」。
      KB 不收新聞之後那個判準**沒有指涉對象 → 恆回空集合 → 時效警語永遠不印**（實測）。
      但風險沒有消失，只是換了位置：即時題 → Grader 正確判不足 → 去打 web →
      **web 預算用完（`QUERY_WEB_BUDGET`）或搜不到** → 答案回頭用財報 chunk 生成 → 零揭露。
      那正是 CLAUDE.md 記著的「把三週前的數字講成『今天股價』」，只是來源從新聞換成了 10-K。

    判準刻意**只看 Grader 的 `realtime_need`**，不看問法、不看候選內容：
      · 「需不需要即時資料」有判斷成分 → LLM（已在 Grader 內，不多花一次呼叫）
      · 「這次有沒有拿到 web」是確定性的 → `_format_unresolved_freshness_notice` 用 `web_used` 篩
    兩者分屬 CLAUDE.md〈LLM 與 Python 的分工〉的兩邊，這裡不重複判斷。

    ⚠ 本函式**不檢查 `web_used`**：那個過濾統一在 `_format_unresolved_freshness_notice`
      裡做（它已經有「這個待辦用了 web 就跳過」的邏輯）。兩邊都做會在未來漂移。
    """
    if need not in ("intraday", "days"):
        return []
    ticker = next(iter(sorted(_mentioned_tickers(task))), "")
    ceiling = _kb_ceiling_date(chunks) if chunks else None
    return [{"ticker": ticker, "cutoff": ceiling.isoformat() if ceiling else "",
             "as_of": as_of.isoformat(), "doc_type": "realtime", "need": need}]


def _unfulfilled_web_route_gaps(route: str, web_notes: list[str], chunks: list[dict],
                                as_of: date) -> list[dict]:
    """**被路由到 web 的待辦卻一次 web 都沒拿到** → 記一筆時效缺口。確定性，零 LLM。

    ⚠ **刻意不看 `realtime_need`**（`_unmet_realtime_gaps` 走的是那條路）。那個欄位是
      Grader 的三分類 LLM 輸出，BACKLOG 記著它在一個措辭族上實測 0/3；而且它要 Grader
      跑過才有值——`route=web` ＋ 預算用完時我們根本沒跑到 Grader，那條路必然沉默。
      這裡的判準是**結構性**的：Planner 說這題要上網，而網路一次都沒查到。零判斷成分。

    ⚠ 只在 live 才會被呼叫（見 `_run_one_todo`）。snapshot 下 `_effective_route` 已把
      route 降級成 kb，這裡也不會有陽性——**eval 基準因此一格不動**（閘門⑪z5）。
    """
    if route not in ("web", "both") or web_notes:
        return []
    ceiling = _kb_ceiling_date(chunks) if chunks else None
    return [{"ticker": "", "cutoff": ceiling.isoformat() if ceiling else "",
             "as_of": as_of.isoformat(), "doc_type": "realtime", "need": "days"}]


# ── 口徑揭露 validator（確定性，零 LLM）─────────────────────────────────────────
# 治的病（2026-08-20 實測 lex-17）：使用者問「營收成長率」沒指定口徑 → 答案給 10-K 的財年 18%，
# 而 gold 是 Fundamentals 的 TTM 18.30%。**數字不是假的，錯的是口徑，而且沒有任何揭露**——
# 兩個值差 0.3pt，讀者無從分辨自己拿到的是哪一種。
#
# ⚠ **刻意做成 validator 而不是 prompt 指令**。理由是本檔已經寫過一次的教訓：
#   「Prompt 是機率性約束；snapshot / --no-web 再用 Python 硬擋」。而這裡要判的兩件事
#   **都是確定性的**——「答案引了哪些 chunk」是 regex，「那些 chunk 是什麼口徑」是 payload
#   的 `period_basis` 欄位（實測全庫只有兩個值：fundamentals=TTM 22 筆／其餘 fiscal_year 3805 筆）。
#   照 CLAUDE.md〈LLM 與 Python 的分工〉，這一半不該交給 LLM 去記得。
#
# ⚠ 措辭刻意**只陳述事實、不宣稱原因**。Gemini 版的建議是「由於缺乏最新 TTM 數據」，
#   但那個因果**可能是假的**：KB 裡可能有 TTM chunk，只是這次沒被引用。斷言一個查不到的原因
#   就是在製造新的不可信內容——同 R4 選「並陳」不選「裁決」的理由。
_BASIS_NOTICE_MARK = "⚠ 口徑說明："
# 「問題自己講明了絕對期別」＝ 使用者要的就是財報期間，這時講 TTM 是雜訊（話太多方向）。
# 只認**格式化的字面訊號**（yyyymm 期碼、年份＋財年字樣），不做語意判斷。
_EXPLICIT_FY_RE = re.compile(r"(?:19|20)\d{2}\s*(?:財年|财年|會計年度|会计年度|年度)"
                             r"|(?:fiscal\s*year|FY)\s*(?:19|20)?\d{2}", re.IGNORECASE)


# Fundamentals chunk 裡「欄位名 : 12.34%」的取值。⚠ 這不是感知，是**解析本專案自己 ingest
# 產生的固定格式**（見 data/edgar_processed/Fundamentals/*.txt），屬於格式定義的封閉集合。
_FUND_PCT_TMPL = r"{field}\s*(?:\([^)]*\))?\s*[:：]\s*(-?\d+(?:\.\d+)?)\s*%"


def _ttm_field_values(cited_chunks: list[dict], fields: list[str]) -> dict[str, str]:
    """引用到的 TTM chunk 裡，被問欄位各自的值（`{"Revenue Growth": "18.30"}`）。認不出就不放。"""
    out: dict[str, str] = {}
    for c in cited_chunks or []:
        if (c.get("period_basis") or "") != "TTM":
            continue
        txt = c.get("content") or ""
        for f in fields:
            m = re.search(_FUND_PCT_TMPL.format(field=re.escape(f)), txt, re.IGNORECASE)
            if m:
                out.setdefault(f, m.group(1))
    return out


def _value_stated(answer: str, val: str) -> bool:
    """答案裡有沒有真的講出這個值。`18.30` 與 `18.3` 視為同一個；`118.3` 不算（前後要有邊界）。"""
    trimmed = val.rstrip("0").rstrip(".") if "." in val else val
    return re.search(rf"(?<![\d.]){re.escape(trimmed)}0*(?![\d])", answer or "") is not None


def _basis_disclosure_notice(task: str, cited_chunks: list[dict], answer: str = "",
                             ratio_fields: list[str] | None = None) -> str:
    """答案只引到財報期間口徑的數字、卻是在回答一個沒指定口徑的比率題 → 回傳揭露警語。

    沉默條件（全部是「話太多」方向的誤報對照——這道護欄的失敗方向不是漏印，是變成背景噪音）：
      ① 不是比率／成長率題（市值、EPS 這類單一來源指標沒有口徑歧義）
      ② 問題自己指定了絕對期別（`2025 財年`、`FY2026`、yyyymm 期碼）——那時財報口徑正是要的
      ③ **答案裡真的講出了那個 TTM 值**

    ⚠ ③ 原本寫的是「引用裡有 TTM chunk」，**那是錯的，而且是被自己要抓的行為解除武裝**
      （2026-08-21 實測，lex-17）：補撈修好之後，答案確實引到 `MSFT_Fundamentals #0`，
      眼前就是 `Revenue Growth (YoY): 18.30%`，它卻寫成
      「全年與最近的 **TTM** 都在約 **18%** 左右【…chunk #0】」——**把 TTM 四捨五入成 18%，
      再與 10-K 的財年 18% 併成同一個說法**。引用是真的、數字看起來也對，兩個口徑就這樣消失了。
      而舊條件③ 看到「有 TTM chunk 被引用」就沉默 → **護欄正好在該叫的那一刻關掉**。
      → 判準改成看**值有沒有出現在答案裡**（確定性字串比對，見 `_value_stated`）。

    ⚠ 有值的時候警語就**把值講出來**，不是只講「這不是 TTM」：值逐字取自**答案自己引用的
      那個 chunk**，所以仍然可追溯；這是 R4 那條「要求並陳不裁決」的同一個做法。
    """
    if not _has_ratio_intent(ratio_fields, task):
        return ""
    if _EXPLICIT_FY_RE.search(task or "") or rq._PERIOD_CODE_RE.search(task or ""):
        return ""

    fields = _resolve_ratio_fields(ratio_fields, task)
    ttm_vals = _ttm_field_values(cited_chunks, fields)
    if ttm_vals:
        missing = {f: v for f, v in ttm_vals.items() if not _value_stated(answer, v)}
        if not missing:
            return ""                  # 答案真的給了 TTM 值 → 不需要這段
        detail = "、".join(f"{f} {v}%" for f, v in sorted(missing.items()))
        return (chr(10) + chr(10) + "---" + chr(10) + _BASIS_NOTICE_MARK
                + f"上文引用的 TTM（最近十二個月）口徑數值為 **{detail}**，"
                  "與文中的財報期間（財年／單季）數字不是同一個口徑——"
                  "兩者數值可能接近但不可互換。")

    basis = {(c.get("period_basis") or "") for c in (cited_chunks or [])}
    if "TTM" in basis:
        return ""                      # 引到 TTM chunk 但認不出欄位值 → 維持沉默，不在看不懂時多話
    if "fiscal_year" not in basis:
        return ""                      # 沒引到任何財報期間 chunk（例如純 web 答案）→ 不是這條的守備範圍
    return (chr(10) + chr(10) + "---" + chr(10) + _BASIS_NOTICE_MARK
            + "以上比率／成長率取自財報期間口徑（財年或單季），"
              "**不是最近十二個月（TTM）**。同一指標的兩種口徑數值可能接近但不可互換。")


def _format_unresolved_freshness_notice(todos: list[dict]) -> str:
    """只對 live 且 web 沒成功補到的新聞缺口產生機械式時效聲明；snapshot 永遠沒有 gap。

    ⚠ 缺口是**逐待辦**算的，但這段警語**整篇答案只印一次**——所以措辭不能講成整篇的結論。
      2026-08-14 實測：「Azure 最新一季成長率」的答案主體引用了 CNBC 與 sec.gov 兩個 web 來源，
      底下卻印出「Web 未提供可用補充」，因為另外幾個沒查網的待辦各自帶著 gap。警語與答案互相矛盾
      比沒有警語更糟（它會讓讀者不信任明明有出處的數字），故依「這一次跑分到底有沒有用到 web」分岔。"""
    unique: dict[tuple[str, str, str], dict] = {}
    any_web = False
    for todo in todos or []:
        if todo.get("status") != "done":
            continue
        if todo.get("web_used"):
            any_web = True
            continue
        for gap in todo.get("freshness_gaps", []) or []:
            # doc_type 入 key：news 缺口與 realtime 缺口措辭不同，混在一起會互相蓋掉
            key = (gap.get("ticker", ""), gap.get("cutoff", ""),
                   gap.get("as_of", ""), gap.get("doc_type", "news"))
            unique[key] = gap
    if not unique:
        return ""
    def _phrase(ticker: str, cutoff: str, as_of: str, doc_type: str) -> str:
        who = ticker or "本次查詢"
        if doc_type == "realtime":
            # KB 只有財報 → 要講「知識庫本來就沒有這種資料」，不是「資料有點舊」
            span = f"，知識庫最新期別截至 {cutoff}" if cutoff else "，知識庫僅含 SEC 財報與基本面"
            return f"{who} 需要即時／近期資料{span}（查詢日 {as_of}）"
        return f"{who} 新聞資料截至 {cutoff}（查詢日 {as_of}）"

    details = "; ".join(_phrase(*k) for k in sorted(unique))
    tail = ("；本次有部分子問題未經網路補充，**未標註 [web:] 出處的內容**不代表涵蓋至查詢日。"
            if any_web else
            "；Web 未提供可用補充，因此以上內容不代表涵蓋至查詢日。")
    return "\n\n---\n⚠ 資料時效：" + details + tail


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
- 【子問題文字保持來源中性】**來源由下面的 route 欄位決定,不要寫進子問題的文字裡**。
  絕不在 task 裡自行加上「在新聞中 / 相關新聞內容 / 在財報中」等來源限定詞——照原問法寫
  (「X 如何做 Y」「X 最近有什麼消息」),讓 route 去說它該往哪裡查。
- 【路由 route】每個子問題除了 task,還要判一個 route:
  · "kb"   ── 答案在 10-K / 10-Q / Fundamentals 裡:財報數字、比率、營收/獲利/成長率、
              業務與策略與競爭與風險敘述、指定財報期間的事實、跨公司財務比較。
              ⚠ 「最新一季」「上一季」「最近一個財年」屬於這一類——那是**財報期別**(由 filing
              定義),不是 wall clock。
              ⚠ **定性題也在這裡**:問「如何 / 為何 / 靠什麼 / 競爭優勢 / 護城河 / 商業模式 /
              面臨哪些風險」的題目,答案寫在 10-K 的 business、competition、risk factors 與
              MD&A 段落,**不是新聞**。**「監管」「反壟斷」「訴訟」「地緣政治」「供應鏈風險」
              這些字本身不代表要上網**——公司自己在 10-K Item 1A 就逐條列著這些風險。
              只有問句帶了時間動態(「最近有什麼進展」「最新裁決」「這週的消息」)才算 web。
  · "web"  ── 知識庫**結構上沒有這種資料**:新聞、報導、分析師看法/目標價/評等、市場情緒與風評、
              公司公告與事件。⚠ 向量庫裡**沒有任何新聞 chunk**,這類子問題送 kb 等於撈不到,
              而模型會拿舊財報硬答且不會交代——那比答不出來更糟。
  · "both" ── 兩邊都有而且都要講:知識庫有帶期間的舊值、外面有更新的值。典型是「目前的市值 /
              現在的股價 / 現在的本益比」——Fundamentals 有快照值(帶日期),web 有當日值,
              正確答案是**兩個都給並各標時點**,不是挑一個。
  判不出來時填 "both",不要填 "kb":兩種錯的代價不對稱——填錯 both 只是多打一次網路,
  填錯 kb 會讓答案拿舊資料冒充現況且零揭露。
- 【檢索標的檢驗】每個子問題都必須指向一個「向量庫裡撈得到的離散事實」——具體的財報數字、
  Fundamentals 的估值/比率欄位、或 10-K/10-Q 裡的一段具體業務/策略敘述。(向量庫裡沒有新聞。)子問題若只是要求「解讀 / 影響 / 意義 / 背景 /
  綜合看法 / 透露什麼訊息 / 反映什麼
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

只輸出一個 JSON 陣列,元素是物件 {{"task": 繁體中文子問題, "route": "kb"|"web"|"both"}},
不要任何其他文字。
例:[{{"task":"Apple 的 FY2025 EPS 是多少","route":"kb"}},
    {{"task":"Apple 最近有什麼跟 Siri 有關的消息","route":"web"}},
    {{"task":"Apple 目前的市值是多少","route":"both"}}]"""


def _parse_plan_output(data) -> list[dict]:
    """把 Planner 的原始輸出正規化成 `[{"task": str, "route": str}]`。**純函式、零 LLM、零網路。**

    **兩種格式都要吃得下**：
      · `["子問題", …]`                       ← 2026-09-01 之前的格式，**既有 fixture 錄的全是這種**
      · `[{"task": …, "route": …}, …]`        ← 新格式

    ⚠ **相容舊格式不是好心，是必要條件**：`llm_replay` 的 `plan` 快取裡錄的全是字串陣列，
      不吃就是所有既有重放當場失效（而 web fixture 的 key 是從 plan 一路推導出來的）。

    ⚠ **兩個預設值刻意不同**（常數在 `_ROUTE_LEGACY_DEFAULT` / `_ROUTE_UNCERTAIN_DEFAULT`
      上方有完整理由）：舊格式字串 → `kb`（可重現性）；新格式缺值／非法值 → `both`（代價不對稱）。

    ⚠ **解析寬鬆、分派嚴格**：非法值在這裡就正規化掉，**不要原樣傳下去**——
      `_dispatch_todo` 對非法 route 是當場炸的（閘門⑪f），一次 LLM 亂填會毀掉整個 query。
    """
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if isinstance(item, str):
            task, route = item.strip(), _ROUTE_LEGACY_DEFAULT
        elif isinstance(item, dict):
            task = str(item.get("task") or "").strip()
            raw = str(item.get("route") or "").strip().lower()
            route = raw if raw in VALID_ROUTES else _ROUTE_UNCERTAIN_DEFAULT
        else:
            continue                      # 數字/None/巢狀陣列 → 丟掉,不要 str() 成垃圾子問題
        if task:
            out.append({"task": task, "route": route})
    return out


def _plan_subqueries(query: str, freshness_mode: str) -> list[dict]:
    """Planner 節點的核心：把問題拆成原子子問題。拆解失敗(解不出 JSON) → 退回單一問題,不讓規劃器失手就整個 run 掛。"""
    # 重放快取（見 llm_replay）：未設 RAG_REPLAY_CACHE 時完全 no-op。key 刻意不含
    # system_prompt——它內嵌隨 collection 變動的 KB Coverage Snapshot，納入 key 會讓
    # 跨 collection A/B 全部 miss，正好毀掉這個快取唯一的用途。
    _rk = f"{freshness_mode}|{query}"
    _hit = _replay.get("plan", _rk)
    if _hit is not _replay.MISS:
        # ⚠ 命中也要走同一條正規化：舊快取錄的是 `list[str]`,直接回傳會讓下游拿到字串而不是
        #   dict。**這條是既有 fixture 能不能繼續重放的唯一關口**（閘門⑪n）。
        subs = _parse_plan_output(_hit)
        _trace(f"plan(replay): {len(subs)} sub-queries → {subs}")
        return subs
    system_prompt = _PLANNER_PROMPT + "\n\n" + _build_temporal_contract(freshness_mode)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": query}]
    with _quiet():
        raw = rq.call_llm(messages, CHECKER_MODEL, temperature=0.0)
    data = _loads_json_lenient(raw)
    subs = _parse_plan_output(data)
    if not subs:
        # 拆解失敗 → 退回單一問題。route 用 uncertain 的那個預設（拆都拆不出來時，
        # 更沒有理由相信「這題 KB 一定有」）。
        subs = [{"task": query.strip(), "route": _ROUTE_UNCERTAIN_DEFAULT}]
    subs = subs[:MAX_SUBQUERIES]
    _trace(f"plan: {len(subs)} sub-queries → {subs}")
    _replay.put("plan", _rk, subs)
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

# ⚠ 只在 **live** 模式附加（2026-08-13）。snapshot（＝eval 走的路）的 Checker prompt 因此**逐字不變**,
# 既有跑分基準一格都不動；由 `eval/verify_web_gate_isolation.py` 的 byte-identical 斷言長期把關。
#
# 為什麼要多這個欄位：實測 Grader 只問「有沒有這個欄位」,不問「這個值夠不夠新」。
# 「Apple 現在的本益比」撈到 6/12 的 Fundamentals 就判 sufficient=true,結果答案把兩個月前的
# 35.83 講成「現在的本益比」,完全沒有揭露時點。同一個病讓「蘋果的即時市值」「特斯拉今天股價」
# 都拿三週到兩個月前的數字當即時值答。
#
# 只問「需要多新」,**不要**問「候選夠不夠新」——後者要比對日期,那是 Python 的活(見 _stale_for_realtime)。
_CHECKER_LIVE_RECENCY_BLOCK = """

⏱ 額外欄位 realtime_need（**只判斷子問題本身需要多新的資料**,不要去看候選片段的日期）:
- "intraday"：問即時／當下的市場數值——股價、今天漲跌、當前市值、即時本益比、盤中報價。
- "days"：問近期動態——最新消息、最近進展、近期發表,可容忍數天內的資料。
- "none"：問特定財報期間或不隨時間變動的事實——某季營收、財報風險因素、跨公司比較、歷史數字。
  子問題已明確指定期間(FY2026、2026 年第三季、10-K 提到…)一律填 "none"。

⚠ 這個欄位**只看子問題在問什麼**,與「候選片段裡有什麼」無關(2026-08-28 補,實測見下):
- 候選片段全是 10-K／10-Q,**不構成**填 "none" 的理由。「財報答不答得了這個問題」與「這個問題
  需要多新的資料」是兩件不同的事——「這家公司最近怎麼樣」用去年的年報回答,那個答案是**錯的**,
  不只是舊的。
- 判準是「使用者期待的時間點」:問句指向**當下或最近一段時間**(而不是某個財報期間)→ 至少 "days"。
  **不要因為找不到那麼新的資料就改填 "none"**——「找不到」是 sufficient 要處理的事,不是這一欄。
- ⚠ 例外(不要判過頭):「最新一季」「上一季」「最近一個財年」指的是**財報期別**(由 filing 定義),
  不是 wall clock → 仍然填 "none"。
- 判不出來時填 "days" 而不是 "none"。兩種錯的代價不對稱:填錯 "days" 只是多搜一次;填錯 "none"
  會讓答案**拿舊資料冒充現況且零揭露**。

JSON 因此多一個欄位:
{"sufficient": …, "missing": …, "new_query": …, "relevant_ids": […], "realtime_need": "intraday"|"days"|"none"}"""


def _check_sufficiency(subquery: str, pool: list[dict], temporal_scope: str = "",
                       freshness_mode: str = FRESHNESS_SNAPSHOT) -> dict:
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
    # 重放快取：key = 子問題 + 這次實際看到的候選 id（順序敏感）。候選變了就是合法 miss
    # ——那正是被測改動造成的差異，不該用舊決策蓋掉。temporal_scope 不入 key（同 plan 的
    # 理由：它隨 collection 變，納入會讓跨 collection A/B 全部 miss）。
    # live 多問一個欄位、且會套時效改判 → key 必須分流,否則 live 的決策會蓋掉 snapshot 的快取
    # （反之亦然）。snapshot 的 key 因此與 2026-08-13 以前逐字相同,既有 fixture 全部照舊命中。
    _live = freshness_mode == FRESHNESS_LIVE
    _rk = subquery + " || " + " ".join(_chunk_id(c) for c in top) + (" || live" if _live else "")
    _hit = _replay.get("check", _rk)
    if _hit is not _replay.MISS:
        _trace(f"check(replay)[{subquery[:24]!r}] sufficient={_hit.get('sufficient')}")
        return dict(_hit)
    ctx = "\n".join(
        f"[{i}] rerank={c['rerank_score']:.3f} | id={_chunk_id(c)}\n    {_snippet(c['content'], subquery)}"
        for i, c in enumerate(top, start=1)
    )
    scope_block = f"時間與資料邊界:\n{temporal_scope}\n\n" if temporal_scope else ""
    user = f"{scope_block}子問題:{subquery}\n\n檢索到的候選片段:\n{ctx}"
    system = _CHECKER_PROMPT + (_CHECKER_LIVE_RECENCY_BLOCK if _live else "")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
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
    # live 時效改判：Grader 說「有這個欄位就夠」,但欄位可能是兩個月前的快照。只降不升
    # ——只把 true 改成 false,絕不把 false 改成 true(不繞過 Grader 原本的離題判斷)。
    need, kb_unfixable = "none", False
    if _live:
        need = str(data.get("realtime_need", "none") or "none").strip().lower()
        if need not in REALTIME_STALE_DAYS:
            need = "none"
        stale_days, kb_unfixable = _classify_staleness(need, top, _get_as_of_date())
        if stale_days is not None and sufficient:
            sufficient = False
            # ⚠ `kb_unfixable` 只在**整個 collection 的天花板**也過期時才成立（見 _classify_staleness）。
            #   若天花板還夠新,代表是這次檢索沒撈到最新那筆 → 保留改寫機會,別跳過。
            missing = ((f"候選中沒有任何能證明時效的來源（KB 只有財報期間資料，"
                        f"財報期間不是發布日）,而本題需要 {need} 等級的即時性"
                        f"（原判定：{missing or '足夠'}）")
                       if stale_days == NO_REALTIME_SOURCE else
                       (f"候選中最新來源已是 {stale_days} 天前的資料,而本題需要 {need} 等級的即時性"
                        f"（原判定：{missing or '足夠'}）"))
            new_query = new_query or subquery
            _trace(f"check[{subquery[:24]!r}] 時效改判 sufficient=True→False "
                   f"(need={need}, 最新來源 {stale_days} 天前, "
                   f"{'KB 補不了' if kb_unfixable else 'KB 還有更新的 → 保留改寫'})")
        else:
            kb_unfixable = False   # 沒觸發改判就不帶旗標,避免污染執行層的早退判斷
    _trace(f"check[{subquery[:24]!r}] sufficient={sufficient} missing={missing[:50]!r} "
           f"new_query={new_query[:50]!r} relevant_ids={len(relevant_ids)}/{len(shown_ids)}")
    out = {"sufficient": sufficient, "missing": missing, "new_query": new_query,
           "relevant_ids": relevant_ids, "realtime_need": need, "kb_unfixable": kb_unfixable}
    _replay.put("check", _rk, out)
    return out


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


def _repair_reference_citations(answer: str, allowed_chunks: list[dict]) -> tuple[str, int, int]:
    """把 `【Reference 7, chunk #15】` 這種**只有序號沒有檔名**的引用還原成真檔名。

    **為什麼可以確定性還原**：那個序號是我們自己編的——`rq.build_user_prompt` 把候選排成
    `[Reference i+1: {source}, chunk #{chunk_index}]` 餵給 Generator。所以 `N` 就是
    `allowed_chunks[N-1]`，屬於 CLAUDE.md〈LLM 與 Python 的分工〉的 Python 那一半，不必再問 LLM。

    **為什麼需要它**（2026-08-20 實測）：live 路徑 5/37 題吐出這種標記，snapshot 路徑 0/100。
    而那 5 題的**有效 KB 引用是 0**——不是多一個壞引用，是整份答案沒有任何可追溯的財報出處，
    答案裡卻有大量財報數字。舊行為是：validator 判它捏造 → 丟回重寫 → `CITATION_VALIDATOR_MAX_RETRIES`
    用完 → **原樣出貨**。（舊 trace 寫「走機械式收尾」，但程式裡從來沒有任何機械收尾。）

    ⚠ **兩個守門條件，缺一不可**——寧可不修，也不要把「無法追溯」變成「看起來可追溯的錯引用」：
      ① `N` 必須落在 `1..len(allowed_chunks)`；超出範圍 → **不動**（維持被判捏造、走原本的重寫路徑）。
      ② 引用裡的 `chunk #M` 必須等於第 N 筆的 `chunk_index`；**兩半不一致就不知道模型指的是哪一個**
         → **不動**。這一條讓修補在套用時是**被資料自己確認過的**，而不是靠「排序假設」。
    回傳 (修好的答案, 修補筆數, 因守門條件而略過的筆數)。"""
    if not allowed_chunks or not answer:
        return answer, 0, 0
    fixed = skipped = 0

    def _sub(m: re.Match) -> str:
        nonlocal fixed, skipped
        n, want_idx = int(m.group(2)), int(m.group(3))
        if not (1 <= n <= len(allowed_chunks)):        # 守門①
            skipped += 1
            return m.group(0)
        c = allowed_chunks[n - 1]
        if c.get("chunk_index") != want_idx:           # 守門②
            skipped += 1
            return m.group(0)
        fixed += 1
        return f"{m.group(1)}{c['source']}, chunk #{want_idx}{m.group(4)}"

    return _BARE_REF_CITE_RE.sub(_sub, answer), fixed, skipped


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


def _parse_period_months(v) -> int | None:
    """期間長度只收 3/6/9/12。其餘（None、"unknown"、自創值）一律 None。"""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n in (3, 6, 9, 12) else None


_PERIOD_END_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _parse_period_end(v) -> str | None:
    s = str(v or "").strip()
    return s if _PERIOD_END_RE.match(s) else None


def _period_key(c: dict) -> str:
    """同格判準用的期間鍵。

    優先用 (period_months, period_end)——這兩個值直接讀原文標題就有（"Three Months Ended
    March 31, 2026"），**不需要換算成財年季度代碼,而換算正是抽取器出錯的地方**：實測同一個
    「截至 2026/3/31 的九個月」被寫成過 2026YTD / 2026Q1Q2Q3 / 2026Q3 / unknown 四種,
    而 "2026Q3" 還同時被拿去指單季與累計。R1 拿 period 做字串相等比對,命名一飄就雙向失效
    （真同期判成不同期 → 漏抓;不同期塌縮成同期 → 誤報）。

    兩欄任一缺就退回舊的 period 字串,行為與加這層之前逐字相同（保住既有的離線校準）。
    """
    m, e = c.get("period_months"), c.get("period_end")
    if m and e:
        return f"{m}M@{e}"
    return c.get("period") or "unknown"


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
_MONTH_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}
_MONTH_NUM = {"january": "01", "february": "02", "march": "03", "april": "04",
              "may": "05", "june": "06", "july": "07", "august": "08",
              "september": "09", "october": "10", "november": "11", "december": "12"}
# ingest 期注入到 chunk 開頭的期間標籤（見 data_update_edgar._split_by_period_section）
_PERIOD_TAG_RE = re.compile(
    r"\[(Three|Six|Nine|Twelve)\s+Months\s+Ended\s+([A-Z][a-z]+)\.?\s+\d{1,2},\s*(\d{4})[^\]]*\]",
    re.I)


def _chunk_period(content: str) -> tuple[int, str] | None:
    """讀 chunk 開頭 ingest 注入的期間標籤 → (月數, "YYYY-MM")。沒有標籤回 None。"""
    m = _PERIOD_TAG_RE.search(content or "")
    if not m:
        return None
    months = _MONTH_WORDS.get(m.group(1).lower())
    mon = _MONTH_NUM.get(m.group(2).lower())
    return (months, f"{m.group(3)}-{mon}") if months and mon else None


def _ground_period_from_source(claims: list[dict], chunks: list[dict]) -> None:
    """**零 LLM** 的期間接地：拿每筆宣稱的數字回原文定位,用它所在 chunk 的期間標籤覆寫期間。

    為什麼不問 LLM：「這個數字出現在哪一段」是字串定位,是封閉邏輯（見 memory
    llm-vs-python-task-split）。實測讓 LLM 自己查證,8 輪只有 4 輪真的去查、其餘直接照抄答案。

    為什麼**不能**把查不到出處的數字降級成 unknown（上一版就是這樣寫,實測真衝突偵測率 0/5）：
    捏造的數字必然在原文查不到,降級等於讓 R1 永遠抓不到幻覺——而幻覺正是要抓的。
    所以查不到就**保留答案自述的期間**,R1 照原本的方式判「答案自己有沒有前後矛盾」。
    原文只用來**駁回**誤報（col-11：答案把單季 19% 說成九個月,定位到單季段就拆開了）。

    保守條件：只在該數字唯一落在**一個**期間段時才覆寫;跨多段或無標籤段一律不動。
    目前只處理百分比（col-11 的樣態）——金額寫法太多（$2.6 billion / $2,600 million /
    26 億美元）,誤配的風險高過收益,留給答案自述。
    """
    tagged: list[tuple[tuple[int, str], str]] = []
    for ch in chunks:
        body = ch.get("content") or ""
        p = _chunk_period(body)
        if p:
            tagged.append((p, body))
    if not tagged:
        return
    for c in claims:
        if c.get("unit") != "percent":
            continue
        v = c.get("value")
        pats = [f"{v:g}%", f"{v:g} percent", f"{v:g} percentage points"]
        hits = {p for p, body in tagged if any(t in body for t in pats)}
        if len(hits) == 1:
            months, end = hits.pop()
            c["period_months"], c["period_end"] = months, end
            c["period_grounded"] = True


AUTHORITATIVE_TYPES = ("10-K", "10-Q", "income_statement", "fundamentals")


def _ground_source_type(claims: list[dict], chunks: list[dict], web_extra: str = "") -> None:
    """**零 LLM**：拿每筆宣稱的數字回 chunk 定位,記下它出現在哪些來源類型（`src_types`）。

    為什麼需要：實測 mi-04 的 contexts 裡同時有新聞的「revenue growth of 12.8% LTM」與
    Fundamentals 的「Revenue Growth (YoY): 0.166」,**兩個都在 context 裡**,答案挑了新聞那個
    （gold 是 16.6%）。mix-09 更直接——同一個 chunk 裡新聞寫「Greater China grew 28%」、
    10-Q 寫 22%,答案挑了 28%。所以病不在檢索,在「同一個指標有多個來源時沒有優先順序」。

    定位方式與 `_ground_period_from_source` 同一路（字串比對＝封閉邏輯,不問 LLM）。除了
    百分比的三種寫法,額外比對**小數表示**：Fundamentals 把比率寫成 `0.166` 而不是 `16.6%`,
    這正是它輸給新聞「12.8%」的原因之一（新聞那個看起來更像答案）。
    """
    # ⚠ 早退條件必須同時看 web_extra：只有 chunks 的舊寫法會讓「候選池空、只有 web」
    #   這個**正是 live 路徑的常見形狀**整個跳過 grounding（閘門⑧ 抓到的）。
    if not chunks and not (web_extra or "").strip():
        return
    typed = [(rq.infer_source_type(ch.get("source") or ""), ch.get("content") or "")
             for ch in chunks]
    # ⚠ 2026-08-19：web 內容走 `web_extra` **字串**、不是 chunk，所以 `chunks` 看不到它。
    #   不併進來的話，只出現在 web 的數字會拿到空的 `src_types` → R3／R4 一律跳過 ＝ 靜默漏判。
    #   來源型別記 `"web"`，**刻意不列入 `AUTHORITATIVE_TYPES`**（web 沒有揭露義務）。
    if (web_extra or "").strip():
        typed = typed + [("web", web_extra)]
    for c in claims:
        v = c.get("value")
        if not isinstance(v, (int, float)):
            continue
        if c.get("unit") == "percent":
            pats = [f"{v:g}%", f"{v:g} percent", f"{v:g} percentage points"]
            # 小數表示（0.166 = 16.6%）**只在 Fundamentals 比對**。2026-08-09 全庫實測：
            #   fundamentals     12/22 chunk 有 `0.xx` 比率、`NN%` 0 個  ← 只有它寫小數
            #   income_statement  0/59 有 `0.xx`
            #   10-K / 10-Q      87 / 106 處 `0.xx`,但全是**債券票面利率**（"0.875% Notes"）
            #                    與**每股金額**（"2.40 | 0.77"），不是比率
            #   news             20 處,全是**股價漲跌**（"GOOG +0.92%"）← 2026-08-19 後 KB 已無此類
            # 所以在財報／新聞裡比對 `0.22` 會配到完全無關的東西 → 把只在新聞出現的宣稱
            # 誤標成「權威來源也有」→ R3 該抓的反而不抓。範圍限定在 fundamentals 才安全。
            dec = f"{v / 100:g}"
            hits = {t for t, body in typed
                    if any(pt in body for pt in pats)
                    or (t == "fundamentals" and dec in body)}
        elif c.get("unit") == "USD_M":
            # 百萬美元：原文可能寫 16,621 / 16621 / $16.62 billion,只比前兩種（確定性高）
            pats = [f"{v:,.0f}", f"{v:.0f}"]
            hits = {t for t, body in typed if any(pt in body for pt in pats)}
        else:
            continue
        if hits:
            c["src_types"] = sorted(hits)


_WEB_MARK = "[web:"
_KB_MARK_RE = re.compile(r"chunk\s*#\s*\d+")


# web／KB 引用標記。⚠ **兩種括號都要收**：`_WEB_MARK` 是半形 `[web:`，而實測 40 份答案的
# web 引用**全部是全形**【web: …】（生成端跟著中文標點走）。只認半形的後果是 R4 的
# 「已經並陳就閉嘴」分支永遠為 False（見 BACKLOG）。新規則不重蹈那一步。
_WEB_CITE_ANY_RE = re.compile(r"[\[【]\s*web\s*[:：]", re.IGNORECASE)
_KB_CITE_ANY_RE = re.compile(r"[\[【][^\]】]*chunk\s*#\s*\d+[^\[【]*?[\]】]", re.IGNORECASE)

# 「這句話帶了時點嗎」。日曆日期：ISO／中文／英文月份三種寫法都收——實測**同一份 fixture
# 三輪就寫出三種形式**（`（截至 2026-09-01）`／`此數據來自 2026 年 9 月 1 日的最新報告`／
# `依據 2026 年 6 月 12 日的…`）。只認一種寫法的量尺第二輪就誤報，那個錯已經犯過兩次。
_CAL_DATE_RE = re.compile(
    r"\b20\d{2}\s*[-/年]\s*(0?[1-9]|1[0-2])\s*[-/月]"
    r"|\b(0?[1-9]|1[0-2])\s*/\s*(0?[1-9]|[12]\d|3[01])\s*/\s*\d{2,4}\b"
    r"|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}\b",
    re.IGNORECASE)
# 財報期間：日曆日期以外，財年／季別／檔名裡的期別戳都算 KB 那半的時點。
# ⚠ **檔名戳一定要算**：`【AAPL_10K_2025.html, chunk #115】` 本身就交代了期別。不算的話
#   這條規則會在幾乎每一份有引用的答案上觸發＝把它變成一個常數，那種規則不帶任何資訊。
_FISCAL_MARK_RE = re.compile(
    r"FY\s*20\d{2}|20\d{2}\s*(財政年度|財年|會計年度)|第\s*[1-4一二三四]\s*季"
    r"|_10[KQ]_\d{4,6}|_\d{8}\b|Q[1-4]\s*(FY)?\s*20\d{2}|20\d{2}\s*Q[1-4]", re.IGNORECASE)

_DUAL_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")

# ⚠ **比對日期之前一定要正規化連字號。** 生成端寫的是 U+2011（不換行連字號）：`2026‑09‑01`
#   看起來與 `2026-09-01` 一模一樣，但 `[-/年]` 匹配不到。實測乾跑時這一個字元製造了
#   **3/4 的誤報**（三份答案明明每一句都標了日期）。`eval/check_web_claims._norm` 早就在做
#   這件事，這裡沒抄到那一課——**凡是拿 regex 讀 LLM 寫出來的日期，都要先過這一層**。
_DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\uff0d\u2212"
_DASH_TRANS = {ord(c): "-" for c in _DASH_CHARS}

# 句子裡的**金額**（不是任意數字）。單位一律正規化成百萬美元，因為要判的是「這兩個值可不可
# 比」——實測失敗案例寫的是 `$4.75 trillion` 與 `$4342.02 billion`，字面完全不同、量級相同。
_MONEY_RE = re.compile(
    r"(?:US)?\$\s*([\d,]+(?:\.\d+)?)\s*(trillion|billion|million|兆|億)?"
    r"|([\d,]+(?:\.\d+)?)\s*(兆美元|億美元|百萬美元)",
    re.IGNORECASE)
_MONEY_SCALE = {"trillion": 1_000_000.0, "兆": 1_000_000.0, "兆美元": 1_000_000.0,
                "billion": 1_000.0, "億": 100.0, "億美元": 100.0,
                "million": 1.0, "百萬美元": 1.0}


def _money_in_millions(text: str) -> list[float]:
    """句子裡所有金額，正規化成百萬美元。⚠ 沒有單位詞的裸 `$1,234` 一律忽略——猜單位會
    製造假的可比性，而這條規則的整個判別力就建立在「這兩個值是同一個量」上面。"""
    out = []
    for m in _MONEY_RE.finditer(text or ""):
        raw, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if not unit:
            continue
        try:
            out.append(float(raw.replace(",", "")) * _MONEY_SCALE[unit.lower()])
        except (ValueError, KeyError):
            continue
    return out


def find_undated_dual_sourcing(answer: str) -> list[str]:
    """R5 **並陳必須帶時點**（零 LLM）：web 與財報給出**同一個量的兩個值**時，兩邊都要交代時點。

    **被一次實測逼出來的（2026-09-02）**：`web-01`（Apple 市值）同一份 fixture 三輪重放
    PASS／**FAIL**／PASS。失敗那輪引用了 web 的 $4.75 兆卻**完全沒給時點**，還寫
    「兩者皆屬於同一時間段的不同來源」——六月的 KB 快照與九月的 web 值**不是**同一時間段，
    那是一句錯的話。讀者看到的是兩個差 9% 的數字被宣告成同期。

    **為什麼 R4 抓不到**：`find_unreconciled_web_conflicts` 的 bucket key 含 `unit`，而答案寫的是
    `$4.75 兆` 與 `$4342.02 billion` → **兩個單位落到不同桶**，規則從頭到尾沒觸發。本規則
    把金額正規化成百萬美元再比，繞開那個坑；也不需要 LLM 抽出來的 `claims`。

    **觸發條件收得很窄，而那是量出來的**：第一版只要求「同時引用 web 與財報就要有時點」，
    在 281 份既有答案上**觸發 34 次（並陳答案的 28%）**，絕大多數是新聞敘述引用
    （「華爾街仍維持 Strong Buy 共識」），要求那種句子標查得日期不是這個病。
    現在要求**兩邊句子都出現可比較的金額**（量級差在 10 倍以內、值差超過 2%）——
    那才是「同一個量的兩個值」的確定性代理。

    **判準是同句共現**（沿用 `check_news_routing.admits_flow_gap` 的教訓）：整篇比對會讓答案裡
    任何一個日期替所有來源背書——而那正是要抓的病（KB 的 6 月日期替 web 值背書）。

    ⚠ **誤報下的動作是安全的**（選這個動作而不是裁決的主要理由，同 R4）：要求模型做的事是
      「把每個值的時點寫出來」，那對任何答案都是正確行為。裁決型規則沒有這個性質。

    ⚠ **能力上界①**：只看得到答案自己寫出來的東西。答案只講其中一邊、或把 web 值寫成沒有引用
      的敘述，這裡看不到——那是 citation validator 的守備範圍。

    ⚠ **能力上界②（2026-09-02 量出來的，別再試著收緊）**：金額量級配對（10 倍內、差 >2%）
      是「同一個量」的**代理**，不是它本身。實測它會把不同的量配成一對：
        · `news-14`：KB「九個月服務收入 917.28 億」 vs web「FY2025 全年 1,092 億」（不同期間）
        · `news-03`：KB「九個月營收 3,136.95 億」   vs web「盈餘 1,120.1 億」（不同指標）
      **財報那側接受檔名期別戳（`_FISCAL_MARK_RE`）正是在吸收這個不精確**——它是承重的，
      不是讓步。試過把 KB 側收緊成「web 有日曆日期時 KB 也必須有」：179 份並陳答案的觸發從
      2 變成 9，而**新增的 7 筆全部是上面那種假配對**。
      → 因此 `eval/web_claims.json` 的 `web-01` 斷言**比本規則嚴格是正確的**：那一題經人工確認
        兩個值就是同一個量，而 validator 沒有這個知識。兩者不該對齊。
      **真正的解法**是讓配對精確而不是讓判準更嚴：把 `find_unreconciled_web_conflicts`（R4）的
      bucket key 做**單位正規化**（它有 LLM 抽出來的 `metric`／`entity`，只差 `unit` 沒normalise）。
      那要動 R4 的行為，需要它自己的乾跑與閘門。
    """
    body = rq.strip_evidence_tail(answer or "").translate(_DASH_TRANS)
    if not body.strip():
        return []
    sents = [x for x in _DUAL_SENT_SPLIT_RE.split(body) if x.strip()]
    web_sents = [x for x in sents if _WEB_CITE_ANY_RE.search(x)]
    kb_sents = [x for x in sents if _KB_CITE_ANY_RE.search(x)]
    if not web_sents or not kb_sents:
        return []          # 沒有並陳 → 這條規則沒有意見

    # 有沒有「同一個量的兩個值」：量級可比（10 倍內）且真的不同（>2%，同 R4 的門檻）。
    pairs = [(w, k) for ws in web_sents for w in _money_in_millions(ws)
             for ks in kb_sents for k in _money_in_millions(ks)
             if w > 0 and k > 0 and max(w, k) / min(w, k) <= 10
             and abs(w - k) / max(w, k) > 0.02]
    if not pairs:
        return []

    problems: list[str] = []
    if not any(_CAL_DATE_RE.search(x) for x in web_sents):
        problems.append(
            "答案把網路來源的金額與財報金額並列，但**網路那一邊沒有交代時點**。"
            "網路值請在同一句裡註明查得日期（例如「截至 2026-09-01」），"
            "不要讓讀者以為它與財報值屬於同一個時間段。")
    if not any((_CAL_DATE_RE.search(x) or _FISCAL_MARK_RE.search(x)) for x in kb_sents):
        problems.append(
            "答案把網路來源的金額與財報金額並列，但**財報那一邊沒有交代期間**。"
            "財報值請在同一句裡註明所屬財報期間或資料日期。")
    return problems


_WEB_UNCITED_MARK = "⚠ 網路結果未採用："


def web_fetched_but_uncited_notice(answer: str, web_extra: str) -> str:
    """R6 **抓到了 web 卻一個都沒引用** → 附一句揭露（確定性，零 LLM，**不重生成**）。

    **治的病（2026-09-02 實測）**：`web-01` 第 10 輪 `n_web_calls=1`、fixture 裡就有
    `stockanalysis.com` 的 $4.75 兆，而答案只有一句
    「Apple 目前的市值約為 43,420.2 億美元【AAPL_Fundamentals_20260612.txt, chunk #0】」
    ——**82 天前的快照當「目前」，零 web 引用、零時點揭露**。同一份 fixture 的第 8、9 輪都
    引用了它，所以成因是 Generator 的抽樣，不是輸入。五道 validator 一道都不會響
    （chunk 是真的、數字溯源得到、期別也對、citation 合法）。

    ⚠ **為什麼掛在 Synthesize 而不是 todo 層**：`_format_unresolved_freshness_notice` 對
      `todo["web_used"]` 為真的待辦直接 `continue`。而 `web_used` 問的是「**檢索**有沒有拿到
      web」，這裡的病是「**答案**沒有引用 web」——r10 那一輪 `web_used` 是 True，整個待辦
      被跳過，一句警語都不會出。只有在 Synthesize 才看得到答案文字。

    ⚠ **為什麼是「揭露」不是「重生成」**：誤報方向決定的。web 真的回垃圾時（實測 2026-09-02
      上午 Apple 市值那次，12 筆全是首頁與維基詞條），「網路結果未採用」**字面上就是真的**；
      而重生成會白燒一輪 LLM，還可能把對的答案改壞。同 R4／R5 選「並陳」與「補時點」而不是
      「裁決」的理由：**誤報下要求做的事本來就是正確行為**。

    ⚠ **能力上界（寫清楚免得被當成保證）**：它分不出「web 有可用內容卻沒被引用」與
      「web 回的東西根本答不了這題」——兩者在結構層完全相同，要分辨得看內容層有沒有被問的
      那個量＝感知不是規則。所以措辭刻意**只陳述事實、不宣稱原因**（同 `_basis_disclosure_notice`
      那條：斷言一個查不到的原因就是在製造新的不可信內容）。

    ⚠ **頻率很低，不要拿端到端跑分驗收它**：現行架構上 116 個題×輪只出現 1 次（≈0.9%，
      `newsroute_after2` 23 ＋ `mixed_prod_r1~r3` 63 ＋ `web-01` 重放 30）。它是**靜默失效上的
      一層字面為真的揭露**，驗收只能靠確定性閘門⑱。
    """
    if not (web_extra or "").strip() or not (answer or "").strip():
        return ""
    if _WEB_CITE_ANY_RE.search(rq.strip_evidence_tail(answer)):
        return ""          # 引用了就沒事——本規則對比例沒有意見
    return ("\n\n---\n" + _WEB_UNCITED_MARK +
            "本次已取得網路搜尋結果，但最終答案未引用其中任何來源；"
            "以上內容僅根據知識庫的 SEC 財報與基本面資料，**不代表涵蓋至查詢日**。")


def find_unreconciled_web_conflicts(claims: list[dict]) -> list[str]:
    """R4 **web ↔ 財報並陳**（零 LLM）：同一格出現互斥數值、一邊只在 web、另一邊在財報 →
    要求答案**兩個都講、各自標出處與時點**。

    ⚠ **刻意不是「判 web 那個為錯」**——那是 R3 對 KB 新聞的形狀，套到 web 是錯的：
      web 可以**合法地比任何 filing 都新**（盤中報價、8-K 事件、財報已發布但 10-Q 未申報）。
      宣告「財報永遠對」會讓系統**系統性地報舊數字**，正是 CLAUDE.md 那句
      「把三週前的數字講成『今天股價』」的鏡像。

    **為什麼「並陳」在誤報下是安全的**（這條是選這個動作而不是裁決的主要理由）：
      本規則最大的誤報來源是「同一指標、不同期間」——`period` 刻意不入 bucket key
      （沿用 R3 的理由：新聞／web 幾乎不標精確期間，納入會讓兩邊永遠落到不同桶、規則永不觸發）。
      而誤報時要求模型做的事是「把兩個值連同各自的時點講清楚」，**那對不同期間的兩個值本來
      就是正確行為**。裁決型規則沒有這個性質：判錯一次就是把對的數字刪掉。

    ⚠ **能力上界（寫清楚免得被當成保證）**：claims 是從**答案**抽的，所以本規則只看得到
      「答案自己講了兩個互斥的值」。答案只講其中一個、而另一個來源有不同值的情況，
      這裡**看不到**——那需要從來源側反向抽取指標，不是確定性能做的事。

    只在「同一格、互斥值、一邊只有 web、另一邊有權威來源、且至少一邊沒標出處」時發話。
    兩邊都已標出處 ＝ 模型已經並陳了，保持沉默。
    """
    problems: list[str] = []
    buckets: dict[tuple, list[dict]] = {}
    for c in claims:
        if not c.get("metric") or not c.get("src_types"):
            continue
        buckets.setdefault((c["metric"], c.get("entity"), c.get("scope"),
                            c.get("kind"), c.get("unit")), []).append(c)
    for key, group in buckets.items():
        web_only = [g for g in group if g["src_types"] == ["web"]]
        authoritative = [g for g in group
                         if any(ty in AUTHORITATIVE_TYPES for ty in g["src_types"])]
        if not web_only or not authoritative:
            continue
        for w in web_only:
            for a in authoritative:
                hi = max(abs(w["value"]), abs(a["value"]))
                if hi <= 0 or abs(w["value"] - a["value"]) / hi <= 0.02:
                    continue
                w_cited = _WEB_MARK in (w.get("quote") or "")
                a_cited = bool(_KB_MARK_RE.search(a.get("quote") or ""))
                if w_cited and a_cited:
                    continue          # 已經並陳且各有出處 → 不囉嗦
                metric, entity = key[0], key[1]
                problems.append(
                    f"「{entity or '（未指明主體）'}」的 {metric} 同時出現網路值 "
                    f"{w['value']:g}（{w['quote']}）與財報值 {a['value']:g}（{a['quote']}）。"
                    f"**兩者都要保留**：網路值標 [web: 網址] 並註明查得時點，財報值標 "
                    f"[檔名, chunk #N] 並註明所屬財報期間；不要只挑一個講，也不要把其中一個當錯的刪掉。")
    return problems


def find_authority_conflicts(claims: list[dict], chunks: list[dict],
                             web_extra: str = "") -> list[str]:
    """R3 **財報優先**（零 LLM）：同一格出現互斥數值,且其中一個只在新聞裡、另一個在財報／
    Fundamentals 裡 → 判新聞那個為錯。

    ⚠ **2026-08-19 起這條規則在 KB 上永久不觸發**,而那是正確的而非退化：它治的病是
    「KB 裡的新聞數字壓過財報數字」（實測 mi-04 用了新聞的 12.8% LTM 而非 Fundamentals 的
    16.6%、mix-09 用了新聞的 28% 而非 10-Q 的 22%）。KB 已不收新聞 → `src_types == ["news"]`
    這個桶永遠是空的 → 病源消失,治療自然閒置。**刻意保留不刪**：新聞若復活它要立刻生效。

    ⚠ **新的同類風險沒有被這條規則涵蓋**：live web 的內容走 `web_extra` 字串、不是 chunk,
    `_ground_source_type` 看不到它,所以「web 說 X、10-Q 說 Y」不會被抓。那需要另一條規則
    與它自己的確定性測試,記在 BACKLOG〈web 數值與財報衝突無人看守〉。

    只在「同一格、互斥值、來源類型分屬新聞 vs 權威」時才發話,其餘一律沉默。
    """
    _ground_source_type(claims, chunks, web_extra)
    problems: list[str] = []
    buckets: dict[tuple, list[dict]] = {}
    for c in claims:
        if not c.get("metric") or not c.get("src_types"):
            continue
        # period 刻意**不入 key**：新聞幾乎不標精確期間（「LTM」「近一年」），把期間納入
        # 比對會讓新聞那筆永遠落到別的桶、R3 永遠不觸發。代價是可能拿不同期間的值互比，
        # 由 metric+entity+scope+kind+unit 五項全等來控制誤報。
        buckets.setdefault((c["metric"], c.get("entity"), c.get("scope"),
                            c.get("kind"), c.get("unit")), []).append(c)
    for key, group in buckets.items():
        news_only = [g for g in group if g["src_types"] == ["news"]]
        authoritative = [g for g in group
                         if any(t in AUTHORITATIVE_TYPES for t in g["src_types"])]
        if not news_only or not authoritative:
            continue
        for n in news_only:
            for a in authoritative:
                hi = max(abs(n["value"]), abs(a["value"]))
                if hi > 0 and abs(n["value"] - a["value"]) / hi > 0.02:
                    metric, entity = key[0], key[1]
                    problems.append(
                        f"「{entity or '（未指明主體）'}」的 {metric} 用了新聞的數值 "
                        f"{n['value']:g}（{n['quote']}）,但財報／Fundamentals 給的是 "
                        f"{a['value']:g}（{a['quote']}）——同一指標以財報為準,請改用後者。")
                    break
    return problems


def find_claim_conflicts(claims: list[dict]) -> list[str]:
    """對結構化宣稱跑確定性規則。**這裡完全不呼叫 LLM**——判斷是邏輯,交給程式。

    R1 同一格（metric,entity,scope,period,kind,unit）出現互斥數值 → 衝突。
    R2 同一 (metric,period,kind=change) 下,segment 們的加總對不上 consolidated → 衝突。
       這條是 regex 版結構上做不到的：mix-03 的三個部門增幅合計 6,398 才是正解,
       它卻報了單一部門的 2,700。
    """
    problems: list[str] = []

    # R1：同格互斥。以下四道排除全是 2026-08-08 離線量測（200 份存檔答案）打出來的——
    # 第一版 R1 在 agentic 新抓 6 題只有 1 個站得住,病因全在抽取器的系統性樣態,
    # 用確定性規則擋掉比回去勸抽取器可靠。
    buckets: dict[tuple, list[dict]] = {}
    for c in claims:
        pkey = c.get("period_key") or c["period"]
        if pkey == "unknown" or not c["metric"]:
            continue          # 期間不明就不比,避免拿不同期間的數字互撞
        # ⓐ level 不比：「從 X 增至 Y」是一句話裡的兩個 level,抽取器幾乎都給同一個 period,
        #    比下去必然誤報（實測 mi-08 637,959→716,924、mix-12 391億→752億 都是這樣炸的）。
        #    真正該比的是變化量與成長率。
        if c["kind"] not in ("change", "growth_pct"):
            continue
        # 期間用 _period_key（客觀的 長度@截止月）而非自由文字 period,見 _period_key docstring。
        buckets.setdefault((c["metric"], c["entity"], c["scope"], pkey,
                            c["kind"], c["unit"], c["basis"]), []).append(c)
        # ⓑ basis 進 key：YoY 92% 與 QoQ 21%（mix-11）、reported 33% 與固定匯率 29%（mix-08）
        #    是不同基準的合法並列,不是矛盾。
    for key, group in buckets.items():
        # ⓒ 同一句話拆出來的多筆不比：區間「EPS 增加 1 至 3 美元」(news-10) 會被拆成兩筆,
        #    它們共用同一段 quote。
        if len({g["quote"] for g in group}) < 2:
            continue
        vals = [g["value"] for g in group]
        if len(vals) < 2:
            continue
        # ⓓ 百分比加總 ≈ 100 → 是佔比分配不是互斥值（sem-09 的 Reality Labs 支出 70%/30%）。
        if key[5] == "percent" and abs(sum(vals) - 100) <= 1:
            continue
        # ⓔ 同一格出現 3 個以上相異值 → 幾乎都是抽取器把列舉壓成同一格,不是矛盾
        #   （mix-08：一句 "Regional data ... (+29%, +39%, +40%)" 是四個地區,entity 全被填成 Meta）。
        #   真正的「同一個量兩種互斥讀法」是二選一,不會有三個候選。
        if len({round(v, 4) for v in vals}) > 2:
            continue
        lo, hi = min(vals), max(vals)
        if hi > 0 and (hi - lo) / hi > 0.02:
            metric, entity, scope, _pkey, kind = key[:5]
            # 訊息給人看,用可讀的 period 字串;判斷才用 _pkey（見 _period_key）
            period = group[0].get("period") or _pkey
            detail = " / ".join(f"{g['value']:g}（{g['quote']}）" for g in group)
            problems.append(f"「{entity or '（未指明主體）'}」的 {metric}（{period}、{scope}、{kind}）"
                            f"出現互斥數值：{detail}")

    # R2：分部加總 vs 合併總計。不依賴 period（實測分部常被抽成 period=unknown），
    # 只依賴 scope + metric——mix-03 的分部是用 level 對（「從 X 增至 Y」）寫的，不是 change，
    # 所以要先從 level 對推導增幅。
    by_metric: dict[str, list[dict]] = {}
    for c in claims:
        if c["unit"] == "USD_M" and c["metric"]:
            by_metric.setdefault(c["metric"], []).append(c)
    for metric, group in by_metric.items():
        seg_changes: dict[str, float] = {}
        for ent in {g["entity"] for g in group if g["scope"] == "segment" and g["entity"]}:
            mine = [g for g in group if g["scope"] == "segment" and g["entity"] == ent]
            direct = [g["value"] for g in mine if g["kind"] == "change"]
            if direct:
                seg_changes[ent] = direct[0]
                continue
            levels = sorted(g["value"] for g in mine if g["kind"] == "level")
            if len(levels) == 2:
                seg_changes[ent] = levels[1] - levels[0]   # 兩個水準值 → 增幅
        cons = [g["value"] for g in group if g["scope"] == "consolidated" and g["kind"] == "change"]
        if len(seg_changes) < 2 or not cons:
            continue
        seg_sum, biggest = sum(seg_changes.values()), max(seg_changes.values())
        if any(abs(t - seg_sum) / max(abs(seg_sum), 1e-9) <= 0.02 for t in cons):
            continue          # 有一個合併值對得上加總 → 正常
        # 只在「回報的合併總計比它自己列的某個分部還小」時才判——這是 mix-03 的簽名
        # （報 2,700 卻列了一個 +3,594 的部門）。分部沒列全的正常情況下,合併值必 ≥ 任一分部,
        # 因此這道門檻能擋掉「只列兩三個部門當佐證」的合法寫法。
        if all(v > 0 for v in seg_changes.values()) and min(cons) < biggest * 0.98:
            names = ", ".join(f"{k} +{v:g}" for k, v in sorted(seg_changes.items(), key=lambda x: -x[1]))
            problems.append(
                f"{metric}：回報的全公司增幅 {min(cons):g} 比它自己列的最大單一部門增幅 {biggest:g} 還小，"
                f"且與各分部加總 {seg_sum:g} 對不上（{names}）——很可能把單一部門當成了全公司總計")
    return problems


_CONSIST_REVISE_SUFFIX = """

⚠ 一致性檢查未通過：你的答案對「同一個主體的同一個指標」給了兩組互斥的數值,而且沒有說明
它們為何不同。請重寫整份答案（維持 [filename, chunk #N] 引用格式）,並做到:
1. 回到來源表格確認每個數字的「欄位」——多期間並排表常見 `Three Months Ended` 與
   `Six/Nine Months Ended` 相鄰,分部門表也常把單一部門與合併總計並列。確認你取的是
   使用者問的那個期間與範圍（問單季就不要拿累計欄,問全公司就不要拿單一部門）。
2. 只保留正確的那一組;若兩組都正確（例如一組是單季、另一組是累計），必須各自明確標出
   期間或範圍,不要讓它們看起來在講同一件事。

以下是偵測到的衝突:
{issues}"""


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


# ──────────────────────────────────────────────────────────────────────────────
# 期別稽核：抓「答案自稱『最新一季／最新一年』，但引用的不是證據集合裡最新的那一期」。
#
# 2026-08-15 web-03 實測到的病灶。「Microsoft 最新一季的 Azure 營收成長率」答成
#   40%【MSFT_10Q_202603.html】並標「截至 2026 年 3 月 31 日的三個月」
# ——數字沒錯、期間也標了，但 MSFT 最新一季是 Q4 FY2026（4–6 月，**沒有獨立 10-Q，包在 10-K 裡**），
# 而 `MSFT_10K_2026.html #129`（"Azure and other cloud services revenue grew 41%"）**就在同一份
# 證據集合裡、rerank rank 4**，完全沒被用到。KB 一點都不缺資料，缺的是「哪一份才是最新期別」。
#
# **為什麼判準看答案而不是看問題**：要判「使用者是不是在問最新一期」得對問句做語意判斷，
#   那要嘛加詞表（換個措辭就漏，CLAUDE.md 明列的反模式）、要嘛動 Grader prompt
#   （會破壞 snapshot prompt 逐字不變的 eval 隔離，`verify_web_gate_isolation.py` 有常駐斷言）。
#   但**答案寫出「最新一季」時，它就是在做一個宣稱**，而「你引的是不是證據裡最新的一期」是純比對
#   ——這是驗證答案自己的宣稱，不是猜使用者意圖。使用者問 FY2025 時答案本來就不會寫「最新一季」，
#   誤報風險因此很低；而漏判的代價只是維持現狀（跟沒有這層一樣）。方向刻意選在安全側。
#
# ⚠ 這層**不會**因為「KB 沒有更新的期別」而觸發——那是另一件事（該不該上網），判準完全不同，
#   而且目前 KB 裡沒有任何一家的 filing 天花板超出申報週期，等於無從測起。見 BACKLOG。
# 「最新／最近」＋ 5 個字以內的期別詞。⚠ 第一版寫成窮舉（`最新一季|最新季度|最新財季|…`），
# **同一次迭代內就漏了兩個**：修正後重生成的答案自己寫的是「最新**單季**」與「最新公布的**季報**」，
# 兩個都不在清單裡 → validator 對自己造成的新問題視而不見。窮舉措辭必漏，改成「錨詞 + 鄰近期別詞」。
# 中間不許出現數字，讓「最新的 2026 年財報」這種**絕對期間**不誤觸（它不是相對指稱）。
# 這條的失敗方向刻意釘在漏判側：漏判＝維持現狀，誤判＝多燒一次生成。
_LATEST_CLAIM_RE = re.compile(
    r"(?:最新|最近)[^\d。，,、\n]{0,5}[季年期]|"
    r"(?:latest|most\s+recent)(?:\s+\w+){0,2}\s+(?:quarter|year)",
    re.IGNORECASE,
)


def _filing_kind(source: str) -> str:
    return str(((_get_kb_coverage().get("sources") or {}).get(source) or {}).get("kind") or "")


def find_stale_period_claims(answer: str, chunks: list[dict]) -> list[str]:
    """零 LLM、零網路：只做 (fiscal_year, fiscal_period) 的比大小，且**只在同一家公司內比**
    （跨公司比期別沒有意義——AAPL 的 FY2026 Q3 與 NVDA 的 FY2027 Q1 不可比）。

    每家比**兩輪**，缺一不可：
      (a) 全期別：引到的最新一份 vs 證據裡最新一份。抓 web-03 的原病（只引 10-Q、更新的 10-K 沒用）。
      (b) 只比 10-Q：引到的最新一份 10-Q vs 證據裡最新一份 10-Q。
    ⚠ (b) 是 2026-08-15 補的，來源是 (a) 修好之後**重生成的答案自己暴露的殘留缺口**：
      它照指示引了 10-K 並說明「全年 41%、未拆單季」，卻拿 `MSFT_10Q_202512`（FY2026 Q2, 39%）
      當「最新單季」，而 `MSFT_10Q_202603`（FY2026 Q3）就在同一份證據集合裡。
      只有 (a) 的話這一版是**全綠**的——引到的最新一份就是 10-K，確實等於證據裡最新一份。
      「最新一季」問的是最新的**季**，10-K 過關不代表季別選對了。
    """
    if not answer or not chunks or not _LATEST_CLAIM_RE.search(answer):
        return []
    ticker_of: dict[str, str] = {}
    for c in chunks:
        src = str(c.get("source") or "").strip()
        tk = str(c.get("ticker") or "").upper().strip()
        if src and tk:
            ticker_of.setdefault(src, tk)
    cited = {s for s, _ in _extract_citations(answer)}
    problems: list[str] = []
    for tk in sorted({ticker_of[s] for s in cited if s in ticker_of}):
        mine = {s: r for s, t in ticker_of.items() if t == tk
                for r in (_fiscal_rank(s),) if r is not None}
        for scope, ranked in (("", mine),
                              ("單季（10-Q）", {s: r for s, r in mine.items()
                                                if _filing_kind(s) == "10-Q"})):
            cited_ranked = {s: r for s, r in ranked.items() if s in cited}
            if not cited_ranked:
                continue     # 這家（在這個範圍內）沒被引用任何財報 → 沒有期別可比
            best_src, best = max(cited_ranked.items(), key=lambda kv: kv[1])
            newest_src, newest = max(ranked.items(), key=lambda kv: kv[1])
            if newest > best:
                where = f"的{scope}" if scope else ""
                problems.append(
                    f"{tk}：答案自稱在講「最新」期別，但引用到{where}最新一份是 {best_src}"
                    f"（{_fiscal_label(best)}）；參考資料裡 {tk} 還有更新的 {newest_src}"
                    f"（{_fiscal_label(newest)}）完全沒有被引用")
    return problems


_PERIOD_REVISE_SUFFIX = """

⚠ 期別檢查未通過：你的答案自稱在講「最新」的期別,但你引用的並不是參考資料裡最新的那一期。
請重寫整份答案（維持 [filename, chunk #N] 引用格式）,並做到:
1. 以**參考資料裡最新的那一期**為準回答。
2. 若最新那一期沒有使用者要的那個數字（常見情況：10-K 只揭露整個財年、不單獨列最後一季）,
   **必須明說這件事**,再給出有揭露的最近一期並標明它的期間——不可以直接把較舊的一期
   講成「最新一季」。
3. 不要臆測參考資料裡沒有出現的期間或數字。

以下是偵測到的期別落差:
{issues}"""


# ──────────────────────────────────────────────────────────────────────────────
# 數字溯源稽核：答案裡「不可能被換算」形態的數字，必須在來源原文裡逐字出現。
#
# ⚠ **這是一道防線，不是對已觀測缺陷的修補。目前沒有任何實測正例。**
#   它原本是為了封住 `web-02` 的「$196.82 憑空生成」而寫的，但那個 FAIL 後來查明是**量尺誤報**
#   （fixture 的 macrotrends 表裡就有 `196.8165`，答案是正確四捨五入）。留下它的理由改成結構性的：
#
#   `_validate_and_fix_citations` 的 `_CITE_RE` 要求 `, chunk #N`，**`[web: url]` 不算引用**；
#   而 `reflect` 雖然會把 `web_extra` 併進稽核來源（所以 web 數字不是零驗證），**它自己的重生成
#   之後就只剩 citation validator 了**。也就是說：任何 validator 的重生成引入的數字問題，
#   只有下一個 validator 看得到，最後一個之後沒人看。這一層補的是那個位置。
#
# 判準原本只活在 [`eval/check_web_claims.py`](eval/check_web_claims.py) 的
# `numbers_must_be_in_fixture`。生產時 `web_notes` 與 chunk 全文都在手上，**不需要 fixture**。
#
# ⚠ **形態必須收窄到「不可能被換算」的**。換算過的值（億／兆／百分比）本來就不會逐字出現在來源，
#   拿來斷言只會製造假 FAIL。所以只收「兩位小數 ＋ 至少兩位整數（或帶千分位）」這種報價／市值形態，
#   且排除後面接 `%` 的。漏判的代價是維持現狀，誤判的代價是白燒一次重生成——刻意選在漏判側。
#   ⚠ 後面接 `億／兆／萬` 的一律排除——那**就是**換算值。實測 100 題乾跑：不排除會誤報 11 題，
#     而且 11 題全是同一種形狀（`153.69 億美元（$15,369 million）`，來源寫的是 `15,369`）。
#     這種「答案自己把來源數字換算成中文單位」是生產契約要求的寫法，不是幻覺。
_TRACEABLE_NUM_RE = re.compile(
    r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d{2,})\.\d{2}(?![\d%])(?!\s*[億兆萬])")


def find_untraceable_numbers(answer: str, chunks: list[dict], web_extra: str = "") -> list[str]:
    """零 LLM、零網路：答案裡每個受測形態的數字，都必須在 chunk 全文或 web 結果裡逐字出現。

    乾草堆同時含 KB chunk 與 web 結果——**不按引用歸屬拆開**：要判「這個數字是不是編的」，
    只需要問「我們到底有沒有看過它」，不需要先解出它掛在哪個來源名下（那才是 LLM 的活）。
    比對時兩邊都去掉千分位逗號（來源寫 `4342.02`、答案寫 `4,342.02` 是同一個數）。
    """
    if not (answer or "").strip():
        return []
    found = set(_TRACEABLE_NUM_RE.findall(answer))
    if not found:
        return []
    hay = "\n".join([(c.get("content") or "") for c in (chunks or [])] + [web_extra or ""])
    hay_flat = hay.replace(",", "")
    # 來源小數位數常多於答案（`P/E Ratio Trailing: 31.325687` → 答案寫 `31.33`）。四捨五入到兩位
    # 相等就算找得到——**這是正確的引用行為，不是幻覺**。100 題乾跑時它是最後一個誤報。
    hay_rounded = set()
    for tok in re.findall(r"\d[\d,]*\.\d+", hay):
        try:
            hay_rounded.add(round(float(tok.replace(",", "")), 2))
        except ValueError:
            continue

    def _seen(n: str) -> bool:
        if n in hay or n.replace(",", "") in hay_flat:
            return True
        try:
            return round(float(n.replace(",", "")), 2) in hay_rounded
        except ValueError:
            return True          # 解不出來就不主張是幻覺
    return [f"數字 {n} 在提供的參考資料與網路結果裡都找不到（＝憑空生成或自行推算）"
            for n in sorted(found) if not _seen(n)]


_NUMBER_REVISE_SUFFIX = """

⚠ 數字溯源檢查未通過：你的答案裡有數字**在提供的參考資料與網路結果裡都找不到**。
請重寫整份答案（維持 [filename, chunk #N] 與 [web: 網址] 的引用格式）,並做到:
1. **刪掉**這些找不到出處的數字,或改用來源裡真實出現的數值。
2. **不要自己推算**（平均、換算、年化、與其他數字相減得出的差額都不行）。來源沒寫的就說沒有。
3. 其餘正確的內容照舊,不要因為刪掉一個數字就重寫整段結論。

以下是找不到出處的數字:
{issues}"""


_DUAL_SOURCE_REVISE_SUFFIX = """

⚠ 時點檢查未通過：你的答案把**網路來源的金額**與**財報金額**並列，但其中一邊沒有交代它是
什麼時候的數字。讀者會以為兩個值屬於同一個時間段——而它們通常差好幾個月。
請重寫整份答案（維持 [filename, chunk #N] 與 [web: 網址] 的引用格式）,並做到:
1. **每個金額都在它自己那一句裡帶上時點**：網路值寫查得日期（如「截至 2026-09-01」）,
   財報值寫所屬期間或資料日期（如「2026-06-12 的基本面資料」「FY2025」）。
2. **兩個值都要留著**,不要挑一個講,也不要把其中一個當成錯的刪掉——它們可以都是對的,
   只是時點不同。
3. **不要宣稱它們屬於同一個時間段**,除非來源真的這樣寫。
4. 其餘正確的內容照舊。

以下是問題:
{issues}"""


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
