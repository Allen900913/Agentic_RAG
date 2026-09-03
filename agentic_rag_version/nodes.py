"""agentic_rag_version.nodes — LangGraph 的四個節點與圖的組裝。

plan → execute → replan → [execute | synthesize]

⚠ **Synthesize 的四道 validator 守門共用 `rq.looks_like_refusal`**，不要退回英文字面
  比對（中文拒答會穿過去、白燒兩次 LLM 稽核）。閘門⑭a 用 AST 逐個驗，改三道漏一道
  要叫得出來。

⚠ **rnd 0 送出去的 query 必須是 todo 的 task 逐字**：⑮f／⑮g 判斷「該不該說『所詢問
  財年』」的前提就是這件事——誰讓 rnd 0 先改寫一次，⑮ 全綠而前提已經崩了（閘門⑯）。

⚠ **歸因掛在 todo 的出身**：`_node_plan` 建的標 `attributable=True`、`_node_replan`
  建的標 False。少了這一格，replanner 加的 todo 其 rnd 0 照樣算「第一輪」，
  假前提會從另一扇門回來（閘門⑮g）。
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict

import rag_query as rq
import llm_replay as _replay
from .tracing import _trace
from langgraph.graph import StateGraph, START, END

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。
from .chunks import _chunk_id, _fair_select, _merge_chunks, _writer_budget
from .coverage import _mentioned_tickers
from .executor import GEN_MODEL, _effective_route, _mechanical_summary
from .freshness import _get_as_of_date
from .gaps import FRESHNESS_SNAPSHOT, _basis_disclosure_notice, _build_todo_temporal_scope, _fill_dependent_hop, _format_unresolved_freshness_notice, _is_dependent_hop, _news_freshness_gaps, _resolve_hop_entity, _unfulfilled_web_route_gaps, _unmet_realtime_gaps
from .planning import CHECKER_MODEL, FRESHNESS_LIVE, MAX_SUBQUERIES, _coerce_bool, _loads_json_lenient, _parse_plan_output, _plan_subqueries
from .ratio import _classify_ratio_fields, _ensure_ratio_source_coverage, _has_ratio_intent, _todos_ratio_fields
from .validators import _extract_citations, web_fetched_but_uncited_notice
from .webtools import _current_run_state

COMMIT_TOP_K      = int(os.getenv("AGENTIC_COMMIT_TOP_K", str(rq.DEFAULT_TOP_K)))   # 每個子問題 advance 時收進 collected 的 top-k。env 可覆蓋供 ablation。

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


class SupervisorState(TypedDict, total=False):
    query: str                 # 原始問題
    freshness_mode: str        # live=截至今天；snapshot=封閉 KB 最新資料
    enable_reflection: bool    # 是否跑 Reflection 節點
    verbose: bool
    todos: list[dict]          # 待辦清單（另含 temporal_scope/freshness_gaps/web_used）
    collected: list[dict]      # 跨待辦收集的 KB chunk 聯集（Synthesize 的 citation allowlist 來源）
    web_notes: list[str]       # web_search 的網路結果（另標，不進 chunk allowlist）
    period_notes: list[str]    # 期間降級揭露（Tier 2 fallback；注入 Generator prompt，見 _pkg._retrieve_chunks）
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
        不是隔離問題——隔離只靠 `freshness_mode` 與 `_pkg.ENABLE_WEB_SEARCH`）。見 BACKLOG。
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
            # 對照 `_node_replan` 那一個（見該處註解）。判準與 `_pkg._retrieve_chunks` 的 docstring 同一條。
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
    summary, web_notes, realtime_need, period_notes = _pkg._run_executor(
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
    # 剛剛用的那份 run state。優先用 Grader 圈選的 relevant_ids 過濾（見 _pkg._check_sufficiency）：
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
        future_to_idx = {ex.submit(_pkg._run_one_todo, todos[i], freshness_mode, verbose): i
                         for i in pending_idxs}
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                # _pkg._run_executor 內部已有多層安全網，理論上不該 raise 到這裡；這層只防禦徹底沒預料到
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
    #   ① system prompt——它內嵌 `_pkg._build_temporal_contract()` 的 KB Coverage Snapshot,隨
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
                         + "\n\n" + _pkg._build_temporal_contract(freshness_mode))
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]
        with _pkg._quiet():
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
                                                   or not _pkg.ENABLE_WEB_SEARCH):
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
    # 期間降級揭露：`rq.retrieve` 在 Tier 1 落空、降級 Tier 2 時產生（見 `_pkg._retrieve_chunks`）。
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
        answer = _pkg._write_final_answer(state["query"], chunks, GEN_MODEL, extra_user=extra,
                                     web_extra=web_extra, period_note=period_note)
        answer = _pkg._validate_and_fix_citations(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                             web_extra=web_extra, period_note=period_note)
        # ⚠ 以下四道守門共用同一個判準 `rq.looks_like_refusal`（2026-08-28 從
        #   `answer.startswith("I don't have enough")` 換過來）。舊守門是**英文字面、只認開頭**,
        #   而 Writer 講中文——「知識庫中查無足夠資料…」整句穿得過去,於是一份剛說自己沒有依據
        #   的答案還會照樣付 `_extract_claims` ＋ `_pkg._reflect_and_fix` 至少兩次 LLM 呼叫去稽核它,
        #   而稽核對象裡沒有任何可稽核的東西。這與引用尾巴那次（`_compose_answer_tail`）是**同一個
        #   守門寫錯**,那次只改了尾巴、這四道漏改。
        #   ⚠ 危險方向不是漏判而是**判過頭**：真的有依據的答案被當成拒答 → 四道 validator 全部
        #   跳過 → 一致性/期別/數字溯源的保護一次消失。`looks_like_refusal` 的 150 字上限就是
        #   擋這件事的（長的誠實答案不算拒答）,誤報對照見 verify_answer_validators 閘門⑬b/⑭d。
        # 確定性一致性稽核（零 LLM 成本的偵測,只有真的抓到才花一次重生成）。放在 reflect 之前,
        # 讓 reflect 稽核的是已調和過的版本。
        if not rq.looks_like_refusal(answer):
            answer = _pkg._consistency_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                                web_extra=web_extra, period_note=period_note)
        # 確定性期別稽核（零 LLM 偵測）：答案自稱「最新一季」卻引用了較舊的期別 → 重生成一次。
        # 放在一致性之後、reflect 之前：期別改對可能連帶換掉數字，要讓 reflect 稽核最終版本。
        if not rq.looks_like_refusal(answer):
            answer = _pkg._period_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
        if state.get("enable_reflection", True) and not rq.looks_like_refusal(answer):
            answer = _pkg._reflect_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                      web_extra=web_extra, period_note=period_note)
        # 數字溯源（零 LLM 偵測）**放最後**：這是唯一會看 reflect 重生成結果的檢查。
        # 100 題乾跑誤報 0 題（見 find_untraceable_numbers 的兩條排除規則），所以放進主線不會
        # 擾動既有基準；真的觸發才花一次重生成。
        if not rq.looks_like_refusal(answer):
            answer = _pkg._number_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose,
                                           web_extra=web_extra, period_note=period_note)
        # R5 並陳時點（零 LLM 偵測）**放最末**：時點是措辭問題，而上面每一道的重生成都可能
        # 把時點改掉；放中間等於只驗了一個會被後面推翻的版本。281 份既有答案乾跑觸發 1 次
        # 且是真陽性（見 find_undated_dual_sourcing），所以不會擾動既有基準。
        if not rq.looks_like_refusal(answer):
            answer = _pkg._dual_source_check_and_fix(state["query"], answer, chunks, GEN_MODEL,
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
