"""agentic_rag_version.webtools — Tavily 呼叫與兩個給 subagent 用的 tool。

⚠ **白名單只是授權，不是過濾**：Tavily 的 `include_domains` 是**子網域包含式**比對，
  一筆 `finance.yahoo.com` 會連 `ca.`／`hk.` 一起收，而那些是**別的市場的報價**
  （同一天差 12%）。收到結果後還要用 `_host_allowed` 自己複核。這個坑踩過兩次。

⚠ **錄／放刻意錄在 `TavilyClient.search()` 的原始回應**，不是 `_tavily_search()` 的
  回傳字串——後者已跑完白名單複核／去重／抽日期／過時過濾／截斷，錄那裡等於把要測的
  六道一起 mock 掉。

⚠ 本模組裡 `_tavily_search`／`_web_query_en` 的**呼叫**一律走 `_pkg.`：
  eval 有 14 處 monkeypatch 打在套件上，裸用會讓「eval 絕不連網」失效（閘門⑬）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import contextvars
from contextvars import ContextVar

from langchain_core.tools import tool

import rag_query as rq
import web_replay as _web_replay
from .tracing import _trace

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。
from .chunks import _chunk_id, _merge_chunks, _snippet
from .freshness import TAVILY_FETCH_RESULTS, WEB_ALLOWED_DOMAINS, _dedupe_web_results, _get_as_of_date
from .planning import CHECKER_MODEL, POOL_RETURN_K

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


class _RunState:
    """一個子問題的 run-scoped 狀態：檢索池、web 筆記、期間降級揭露、Grader 圈選的相關 id、工具呼叫次數。

    `period_notes` 與 `web_notes` 是同一個形狀、同一個理由：兩者都是「只有檢索層知道、
    Generator 看不到」的事實，而產生它的地方（`_pkg._retrieve_chunks` / `_pkg._tavily_search`）分散在
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
#   呼叫 `_pkg._tavily_search`，連那個計數器都沒經過。實測「蘋果的即時市值是多少？」跑出 7 次 web。
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
        if _query_web_calls >= _pkg.QUERY_WEB_BUDGET:
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

    ⚠ 刻意錄「原始回應」而不是 `_pkg._tavily_search` 的回傳字串：後者是跑完 `_host_allowed`
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
    if not _pkg.ENABLE_WEB_SEARCH:
        return "（web search 已停用）"
    try:
        resp = _pkg._tavily_raw(query)
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
    chunks = _pkg._retrieve_chunks(query, attributable=False)   # Agent A 自行改寫的 query,非使用者意圖
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
    # 的總成本——子問題數量本身會被 replanner 撐大，只有前者時總量無上界（見 _pkg.QUERY_WEB_BUDGET 註解）。
    if not _take_query_web_budget():
        _trace(f"  tool web_search({query[:40]!r}) → 整個 query 的 web 預算已用完,拒絕")
        return (f"（本次查詢的網路搜尋額度已用完（{_pkg.QUERY_WEB_BUDGET} 次）。"
                f"請改用已檢索到的知識庫內容作答；若確實查無，就如實說明查無。）")
    state.web_calls += 1
    # ⚠ 這條路（ReAct executor，`AGENTIC_REACT_EXECUTOR=true` 才啟用，非預設）沒有 Grader 的
    #   `realtime_need`，所以 `need` 只能用預設 "none" ＝**不做過時過濾**。刻意選保守側（寧可
    #   多留也不誤刪）；要補的話得先讓 ReAct 那條路也產出時效判斷。
    note = _pkg._tavily_search(query)
    if note and not note.startswith("（"):
        state.web_notes.append(note)
    _trace(f"  tool web_search({query[:40]!r}) [{state.web_calls}/{WEB_SEARCH_MAX_CALLS}] → {len(note)} chars")
    return note


# ──────────────────────────────────────────────────────────────────────────────
# Agent A（Researcher+Generator，真正的 ReAct agent）：整個 process 只建一次、共享。
# ──────────────────────────────────────────────────────────────────────────────

_subagent_apps: dict[tuple[str, str, bool], object] = {}
