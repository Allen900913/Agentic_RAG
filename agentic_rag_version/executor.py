"""agentic_rag_version.executor — 單一子問題怎麼被執行（route 分派 ＋ 兩種 executor）。

`_run_executor_deterministic` 是現役路徑：確定性檢索 → Grader → 不夠就改寫（有界）。
`_run_executor_react` 是 ReAct 版，`AGENTIC_REACT_EXECUTOR` 才走。

⚠ **`_effective_route` 是 eval 隔離現在真正住的地方**：`verify_web_gate_isolation`
  閘門① 的 `_gate()` 是生產判斷式的**抄寫不是 import**，抄的還是加 route 之前的條件——
  變異測試實測把這裡的隔離整段拿掉，當時兩支閘門一條都沒響。所以閘門⑪t~⑪y 在
  **它現在住的地方**又驗了一次。

⚠ 非法 route 必須當場炸，不可靜默預設成 kb（失敗外觀與「真的判成 kb」完全相同）。
"""
from __future__ import annotations

import json
import os

import rag_query as rq
from .tracing import _trace

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。
from .chunks import _merge_chunks, _snippet
from .planning import FRESHNESS_LIVE, VALID_ROUTES
from .webtools import _current_run_state, _reset_run_pool, _subagent_apps, _take_query_web_budget, rag_search, web_search

# GEN_MODEL：實際寫答案的 Generator。2026-07-31 定案用 gpt-oss-120b，理由是「限速」這一維：
#   glm-5.2            單發 150~220s（NIM 上被拖），100 題不可行
#   deepseek-v4-pro    品質最好但有很低的單模型限速，eval 密集打立刻 429（實測 34 題就撞牆）
#   gpt-oss-120b       1~8s、且高負載下仍不被限速（NVIDIA 主推開放模型、額度寬）→ 唯一能撐 eval 量的
# 代價：gpt-oss-120b 偶爾吐全形【】引用，靠下游 _validate_and_fix_citations 校正（已端到端驗證）。
# 與 CHECKER_MODEL 同模型無妨（不同 prompt、不同任務）。benchmark 見 experiments/agentic/_bench_gen.txt。
GEN_MODEL       = os.getenv("AGENTIC_GEN_MODEL", "openai/gpt-oss-120b")

MAX_REWRITES      = 2     # 每個子問題的補救改寫上限（防迴圈 / 省 token）。

WRITER_MAX_CHUNKS = 8     # 單發/fallback 路徑用的固定 chunk 上限（防 TPM 爆）；synthesize 改走自適應預算。

# ── 執行層開關（fix 2026-07-29）──────────────────────────────────────────────────
# 預設 deterministic：planner 子問題「原封不動」直接檢索，不讓 Agent A(ReAct) 自行改寫 query。
# chunk 層級 probe 證實 ReAct 執行層是 multi_intent context_recall 漏水主因（真實 v2 漏掉、單管線撈到的
# gold chunk 中 12/15 是乾淨 decomposition 撈得到的——差別只在有沒有 Agent A 亂改 query）。
# 設 AGENTIC_REACT_EXECUTOR=true 回到舊 ReAct 行為（A/B 對照用）。
USE_REACT_EXECUTOR = os.getenv("AGENTIC_REACT_EXECUTOR", "false").lower() in ("true", "1", "yes")

# Agent A（Researcher+Generator，真正的 ReAct agent）用的 chat model；預設同 GEN_MODEL（中文生成流暢）。
# ⚠ 若設 gpt-oss-120b，即 CHANGELOG 續九的 tool-call harmony 洩漏來源——崩潰時 execute 有降級保證。
SUBAGENT_MODEL = os.getenv("AGENTIC_SUBAGENT_MODEL", GEN_MODEL)

SUBAGENT_RECURSION_LIMIT = int(os.getenv("AGENTIC_SUBAGENT_RECURSION", "12"))  # 單一 subagent invoke 的工具迴圈上限。


def _get_subagent(freshness_mode: str):
    # snapshot 是 closed-corpus 契約：即使 caller 忘了加 --no-web，也在工程層直接不掛載工具。
    allow_web = _pkg.ENABLE_WEB_SEARCH and freshness_mode == FRESHNESS_LIVE
    key = (freshness_mode, rq.COLLECTION_NAME, allow_web)
    if key not in _subagent_apps:
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent
        llm = ChatOpenAI(
            model=SUBAGENT_MODEL, base_url=rq.NVIDIA_BASE_URL,
            api_key=os.getenv("NVIDIA_API_KEY"), temperature=rq.GEN_TEMPERATURE,
        )
        tools = [rag_search] + ([web_search] if allow_web else [])
        prompt = _RESEARCHER_LOCKED_PROMPT + "\n\n" + _pkg._build_temporal_contract(freshness_mode)
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
    若 _pkg._write_final_answer 也失敗（見實測：NVIDIA 端連續 504 時，降級呼叫一樣會炸），
    退回零 LLM 的機械式摘要，保證 _pkg._run_executor 永遠有摘要可回、不會把例外炸穿整個 graph。"""
    if not chunks:
        return f"（子問題「{task}」在知識庫中查無足夠資料）"
    try:
        return _pkg._write_final_answer(task, chunks, GEN_MODEL, period_note=period_note)
    except Exception as e:
        _trace(f"_pkg._fallback_local_summary 也失敗（{e!r}）→ 退回零 LLM 機械式摘要")
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
            with _pkg._quiet():
                out = app.invoke({"messages": messages},
                                 config={"recursion_limit": SUBAGENT_RECURSION_LIMIT})
            messages = out["messages"]
            verdict = _pkg._check_sufficiency(task, state.pool, temporal_scope,
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
                with _pkg._quiet():
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
            with _pkg._quiet():
                state.pool.extend(_merge_chunks([], _pkg._retrieve_chunks(task, attributable=attributable)))
        summary = _pkg._fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])

    if not (summary or "").strip():
        # react 跑完但沒吐摘要（弱腦跳過）→ 降級用池裡的 chunk 生成
        summary = _pkg._fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])
    return (summary, list(state.web_notes), (verdict or {}).get("realtime_need", "none"),
            list(state.period_notes))


def _effective_route(route: str, freshness_mode: str) -> str:
    """套上 **eval 隔離**之後的實際 route。純函式、零副作用。

    ⚠ snapshot（＝eval 走的路）或 `_pkg.ENABLE_WEB_SEARCH` 關閉時**一律降級成 `kb`**。兩層理由：
      ① **這就是 eval 隔離**。CLAUDE.md：隔離只靠 `freshness_mode == LIVE` 與
         `_pkg.ENABLE_WEB_SEARCH` 兩個獨立條件，任何「相關性詞表」都不是隔離機制。把這兩個條件
         收在**一個純函式**裡，才寫得出真值表（閘門⑪t~⑪w）。
      ② **降級成 `kb` 而不是「什麼都不撈」**：snapshot 下每個 todo 本來就只撈 KB，
         降級成 kb 才能讓 **eval 的行為與加 route 之前逐字相同**——回空會讓 65 題基準整批位移。

    ⚠ 這裡**不做**非法值正規化（那是 `_parse_plan_output` 的事）：讓被改壞的 route 一路走到
      `_dispatch_todo` 當場炸，比在這裡默默吞掉好。live 之外吞不吞都沒有差別（碰不到 web）。
    """
    if freshness_mode != FRESHNESS_LIVE or not _pkg.ENABLE_WEB_SEARCH:
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
        chunks = _pkg._retrieve_chunks(query, attributable=attributable)
    if route in ("web", "both"):
        # ⚠ 一定要走 `_pkg._tavily_search`（白名單複核／地區子網域／去重／過時過濾／截斷都在裡面）
        #   與 `_pkg._web_query_en`（Tavily 對英文 query 明顯較好）。自己直接叫 TavilyClient
        #   會把那六道一起繞掉。閘門⑪g 用 AST 守這一條。
        note = _pkg._tavily_search(_pkg._web_query_en(query), need=need)
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
                    _trace(f"execute[{subq_index}] web 預算已用完（{_pkg.QUERY_WEB_BUDGET} 次）"
                           f"→ 這一輪只跑 KB")
                    _round_route = "kb"
                else:
                    # ⚠ **route=web 拿不到額度時絕不退回 KB。** 這個 todo 之所以是 web，
                    #   就是因為 KB 依設計沒有這種資料（`RAW_EXCLUDE_DIRS` 排除 News/）；
                    #   退回去撈只會拿到財報，然後被當成新聞引用、零揭露——那正是這整個
                    #   改動要治的病。實測 after 臂 `news-10`／`news-13` 就是這樣來的。
                    #   預算是 query 級的，這一輪拿不到，之後也不會有 → 直接收工，
                    #   由下面的 `_unfulfilled_web_route_gaps` 留下可揭露的缺口。
                    _trace(f"execute[{subq_index}] web 預算已用完（{_pkg.QUERY_WEB_BUDGET} 次）"
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
            verdict = _pkg._check_sufficiency(task, state.pool, temporal_scope, freshness_mode)  # Agent B｜Grader
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
            with _pkg._quiet():
                state.pool.extend(_merge_chunks([], _pkg._retrieve_chunks(task, attributable=attributable)))

    summary = _pkg._fallback_local_summary(task, state.pool[:WRITER_MAX_CHUNKS])  # 走生產契約單次生成（內含 try/except 保底）
    return summary, web_notes, verdict.get("realtime_need", "none"), list(state.period_notes)


def _run_executor(task: str, temporal_scope: str, freshness_mode: str,
                  subq_index: int, verbose: bool, *,
                  attributable: bool,
                  route: str = "kb") -> tuple[str, list[str], str, list[str]]:
    """Dispatcher：預設 deterministic（planner 子問題直接檢索）；AGENTIC_REACT_EXECUTOR=true 回舊 ReAct。

    第三個回傳值是 Grader 最後一次的 `realtime_need`——`_pkg._run_one_todo` 要靠它判「這個子問題
    需不需要即時資料」，那是時效警語的新判準（見 `_unmet_realtime_gaps`）。
    第四個是這個子問題檢索時發生的**期間降級揭露**（見 `_pkg._retrieve_chunks`），最終由 Synthesize
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
