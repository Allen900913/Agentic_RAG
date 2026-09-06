"""
plan → execute → replan → [execute | synthesize]，加上單一子問題怎麼被執行
（route 分派 ＋ 確定性 executor／ReAct executor）。

⚠ **`_effective_route` 是 eval 隔離現在真正住的地方**：`verify_web_gate_isolation` 閘門①
  的 `_gate()` 是生產判斷式的**抄寫不是 import**，抄的還是加 route 之前的條件——變異測試
  實測把這裡的隔離整段拿掉，當時兩支閘門一條都沒響。所以閘門⑪t~⑪y 在**它現在住的
  地方**又驗了一次。

⚠ **非法 route 必須當場炸**，不可靜默預設成 kb：那個失敗外觀與「路由真的判成 kb」完全相同。

⚠ **Synthesize 的四道 validator 守門共用 `rq.looks_like_refusal`**，不要退回英文字面比對
  （中文拒答會穿過去、白燒兩次 LLM 稽核）。閘門⑭a 用 AST 逐個驗。

⚠ **rnd 0 送出去的 query 必須是 todo 的 task 逐字**，且**歸因掛在 todo 的出身**
  （`_node_plan` 建的標 `attributable=True`、`_node_replan` 建的 False）——⑮f／⑮g／⑯
  三道的前提全在這兩件事上。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict
import json
import os
import re

from langgraph.graph import StateGraph, START, END
import llm_replay as _replay
import rag_query as rq

import agentic_rag_version as _pkg
# ⚠ **循環 import 是刻意的**：`_pkg.<name>` 在**呼叫時**才解析，於是 eval 打在
#   套件物件上的 monkeypatch 蓋得到。子模組**不得裸用**被 patch 的名字，也不得
#   `from .x import` 它們（那會壓一份當時的物件）——守門是閘門⑬。

from .retrieval import _chunk_id, _fair_select, _merge_chunks, _writer_budget
from .retrieval import _chunk_id, _snippet
from .retrieval import _classify_ratio_fields, _ensure_ratio_source_coverage, _has_ratio_intent, _todos_ratio_fields
from .retrieval import _mentioned_tickers
from .retrieval import _merge_chunks, _snippet
from .tools import _current_run_state
from .tools import _current_run_state, _reset_run_pool, _subagent_apps, _take_query_web_budget, rag_search, web_search
from .tracing import _trace
from .validators import FRESHNESS_SNAPSHOT
from .validators import FRESHNESS_SNAPSHOT, _basis_disclosure_notice, _build_todo_temporal_scope, _fill_dependent_hop, _format_unresolved_freshness_notice, _has_unresolved_anchor, _is_dependent_hop, _news_freshness_gaps, _resolve_hop_entity, _unfulfilled_web_route_gaps, _unmet_realtime_gaps
from .validators import NO_REALTIME_SOURCE, REALTIME_STALE_DAYS, _classify_staleness, _get_as_of_date
from .validators import _extract_citations, web_fetched_but_uncited_notice
from .validators import _get_as_of_date


# ────────────────────────────────────────────────────────────────────────────
# ── 原 planning.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────


MAX_SUBQUERIES    = 7     # planner 拆解上限。設 7 讓 Magnificent Seven 等具名集合能逐一列滿(5 會被迫


# 「最近／最新」有兩種不同語意：
#   live     = 截至真實今天；KB 落後且 web 無法補齊時要揭露 cutoff。
#   snapshot = 封閉語料評測；「最新」只表示 collection 中最新可用資料，不拿 wall clock 製造缺口。
FRESHNESS_LIVE = "live"

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
- 【依賴 depends_on】絕大多數子問題**彼此獨立,填 null**。只有「必須先知道前一個子問題的答案
  才寫得出來」的那種第二跳才填——典型是「市值最高的是哪一家」→「**那一家**的毛利率是多少」。
  · 填的是**排在前面**的那個子問題的索引(0 起算);只能往前指,不能指自己、也不能指後面的。
  · 這種子問題的文字裡,把待解的公司寫成 **#索引** 佔位符,不要寫「該公司」這類代名詞:
    寫「#0 的毛利率是多少」,不要寫「該公司的毛利率是多少」。執行時 #0 會被換成第 0 個
    子問題解出的公司名。
  · ⚠ **能自己寫清楚就不要建依賴**:原問題已經點名公司時直接寫公司名,填 null。
    多建一個依賴會讓那個子問題晚一輪才檢索。
- 不要杜撰原問題沒有的意圖;不要拆過細。最多 {MAX_SUBQUERIES} 個。

只輸出一個 JSON 陣列,元素是物件
{{"task": 繁體中文子問題, "route": "kb"|"web"|"both", "depends_on": 索引或 null}},
不要任何其他文字。
例:[{{"task":"Apple 的 FY2025 EPS 是多少","route":"kb","depends_on":null}},
    {{"task":"Apple 最近有什麼跟 Siri 有關的消息","route":"web","depends_on":null}},
    {{"task":"Magnificent Seven 裡市值最高的是哪一家","route":"both","depends_on":null}},
    {{"task":"#2 的毛利率是多少","route":"kb","depends_on":2}}]"""


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
        dep, declared = None, False
        if isinstance(item, str):
            task, route = item.strip(), _ROUTE_LEGACY_DEFAULT
        elif isinstance(item, dict):
            task = str(item.get("task") or "").strip()
            raw = str(item.get("route") or "").strip().lower()
            route = raw if raw in VALID_ROUTES else _ROUTE_UNCERTAIN_DEFAULT
            # ⚠ **「說沒有」與「沒說」必須分得開**（2026-09-06）：`depends_on: null` 是
            #   Planner **宣告**這一跳不依賴任何人，而整個 key 不存在（舊格式、既有 fixture、
            #   replan 建的 todo）是**沒有資訊**。分不開的話，`_BACKREF_RE` 那張詞表仍然是
            #   實際做決定的人，而端到端跑分看不出任何差別——`_RATIO_INTENT_RE` 就是這個形狀
            #   （見 CLAUDE.md〈LLM 與 Python 的分工〉）。閘門⑮a/⑮c 是這條的兩個方向。
            if "depends_on" in item:
                declared = True
                _d = item.get("depends_on")
                # 值壞掉（字串、bool、浮點）→ 正規化成 None，但 `declared` 仍是 True：
                # 「宣告了但寫壞了」由驗證層修剪並計數，不在解析層靜默吃掉（同 route 的
                # 「解析寬鬆、分派嚴格」）。⚠ `bool` 要先擋：Python 的 True 是 int 的子類。
                if isinstance(_d, int) and not isinstance(_d, bool):
                    dep = _d
        else:
            continue                      # 數字/None/巢狀陣列 → 丟掉,不要 str() 成垃圾子問題
        if task:
            out.append({"task": task, "route": route,
                        "depends_on": dep, "deps_declared": declared})
    return out


def validate_plan_dependencies(todos: list[dict]) -> tuple[list[dict], dict]:
    """Plan 的 DAG 結構檢驗 ＋ 修剪。**純函式、零 LLM、零網路。** 回傳 `(todos, stats)`。

    業界（A.DOT／Maestro／structured-planning 三篇一致）的第二段：執行前做 variable hygiene
    （無懸空引用）與無環檢查，**懸空引用是 prune ＋ warning 不是靜默**。

    ⚠ **不需要 Kahn／DFS**：todo 的 `id` 是依清單位置指派的，所以「只能依賴排在自己前面的」
      （`0 <= depends_on < id`）**同時**保證無懸空、無自環、無環，是 O(n) 的全序檢查。
      副產品：`id 0` 的 `depends_on` 恆被修剪成 `None` ⇒ **永遠至少有一個非依賴型 todo，
      `_node_execute` 不會 deadlock**（閘門⑮i）。

    ⚠ **`deps_disagreed` 不是缺陷計數，是分母**：Planner 宣告「沒有依賴」、而子問題字面上
      帶著未解錨點（`#N` 或回指代名詞且句中沒點名任何公司）時記一筆。那種情況 `_is_dependent_hop`
      **仍然會延後**（見該函式），這裡只負責把分歧數出來——日後要不要拿掉那個例外，靠這個數字。
    """
    stats = {"n_todos": len(todos), "deps_declared": False, "deps_count": 0,
             "deps_pruned": 0, "deps_pruned_detail": [], "deps_disagreed": 0}
    for i, t in enumerate(todos):
        if t.get("deps_declared"):
            stats["deps_declared"] = True
        dep = t.get("depends_on")
        tid = t.get("id", i)
        if dep is not None:
            if not (isinstance(dep, int) and 0 <= dep < tid):
                stats["deps_pruned"] += 1
                stats["deps_pruned_detail"].append(
                    f"id={tid} depends_on={dep!r}（只能依賴排在自己前面的 todo）")
                _trace(f"plan: 依賴引用非法 → 修剪 id={tid} depends_on={dep!r}")
                t["depends_on"] = None
            else:
                stats["deps_count"] += 1
        # 分歧：宣告了「沒有依賴」，但這句話單獨檢索時沒有主詞。
        if t.get("deps_declared") and t.get("depends_on") is None and _has_unresolved_anchor(
                t.get("task", "")):
            stats["deps_disagreed"] += 1
            _trace(f"plan: Planner 說無依賴、但子問題沒有主詞 → 仍延後 id={tid} {t.get('task')!r}")
    return todos, stats


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
    system_prompt = _PLANNER_PROMPT + "\n\n" + _pkg._build_temporal_contract(freshness_mode)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": query}]
    with _pkg._quiet():
        raw = rq.call_llm(messages, _pkg.CHECKER_MODEL, temperature=0.0)
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
    top = pool[:_pkg.POOL_RETURN_K]
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
    with _pkg._quiet():
        raw = rq.call_llm(messages, _pkg.CHECKER_MODEL, temperature=0.0)
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


# ────────────────────────────────────────────────────────────────────────────
# ── 原 executor.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────

# GEN_MODEL：實際寫答案的 Generator。2026-07-31 定案用 gpt-oss-120b，理由是「限速」這一維：
#   glm-5.2            單發 150~220s（NIM 上被拖），100 題不可行
#   deepseek-v4-pro    品質最好但有很低的單模型限速，eval 密集打立刻 429（實測 34 題就撞牆）
#   gpt-oss-120b       1~8s、且高負載下仍不被限速（NVIDIA 主推開放模型、額度寬）→ 唯一能撐 eval 量的
# 代價：gpt-oss-120b 偶爾吐全形【】引用，靠下游 _validate_and_fix_citations 校正（已端到端驗證）。
# 與 _pkg.CHECKER_MODEL 同模型無妨（不同 prompt、不同任務）。benchmark 見 experiments/agentic/_bench_gen.txt。
GEN_MODEL       = os.getenv("AGENTIC_GEN_MODEL", "nvidia/nemotron-3-super-120b-a12b")  # ⚠ 2026-09-03 換：gpt-oss-120b 被 NVIDIA 退役（410 Gone）。選型見 experiments/_model_bakeoff_20260903.log

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
                    _trace(f"execute[{subq_index}] web 預算已用完（{_pkg.QUERY_WEB_BUDGET} 次）"
                           f"→ 這一輪只跑 KB")
                    _round_route = "kb"
                else:
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


# ────────────────────────────────────────────────────────────────────────────
# ── 原 nodes.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────

COMMIT_TOP_K      = int(os.getenv("AGENTIC_COMMIT_TOP_K", str(rq.DEFAULT_TOP_K)))   # 每個子問題 advance 時收進 collected 的 top-k。env 可覆蓋供 ablation。

# ── Supervisor / Subagent 架構專屬常數 ────────────────────────────────────────
# 總「子問題執行次數」上限。⚠ **在現行常數下這條咬不到，別以為它是那道保險**（2026-09-06 量的）：
# todo 的狀態是單向的（pending → in_progress → done，不會回頭），所以總執行次數**恆等於**曾經
# 建立過的 todo 數 ≤ `MAX_TODOS`；`MAX_TODOS(7) < MAX_ITERS(8)` ⇒ 永遠先撞到前者。
# 它也不是死碼——**把 `MAX_TODOS` 調到 ≥ 這個值的那一刻它就活過來**，而那時會有 todo 被建立卻
# 永遠不執行，且完全靜默（`_route_after_replan` 直接跳 synthesize，不留任何痕跡）。
# 兩個常數要一起調，`verify_web_gate_isolation` 閘門⑭a 守這個不變量。
MAX_ITERS      = int(os.getenv("AGENTIC_MAX_ITERS", "8"))

# 待辦清單總數上限（沿用 7：Magnificent Seven 逐一列滿 + replan 新增後仍守此上限）。
# ⚠ **Planner 與 Replanner 共用這一個額度、先到先得**，而 Planner 一定先跑：實測 planner 拆 7 個
#   時 Replanner 的預算是 **0**（拆 5 個 → 2、拆 1 個 → 6）。也就是說「拆得最細的題」＝
#   「Replanner 完全沒有預算的題」，恰好是 multi_hop 那一類。
#   額度要不要拆開是**待決的**，判準是 `replan_stats.refused_budget` 的實際發生率——
#   在量到之前不要調高（子問題爆炸級聯有前科，見 `QUERY_WEB_BUDGET` 上方）。見 BACKLOG。
MAX_TODOS      = MAX_SUBQUERIES

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
    unit_stats: dict           # `rq.finalize_answer_units` 這一次剝掉／修掉了幾處億（見那支的 stats 說明）
    # Replanner 的待辦額度統計（見 `_node_replan`）。⚠ **沒有宣告在這裡的 key，LangGraph 會直接
    # 丟掉**——閘門⑭g 守這條。⚠ 這一格是**累加**的：LangGraph 對沒有 reducer 的 key 是取代語意，
    # 所以 `_node_replan` 自己讀舊值再合併（⑭i 守這條），否則只會剩下最後一輪。
    replan_stats: dict
    # Plan 的依賴宣告品質（見 `validate_plan_dependencies`）：宣告了幾個、修剪了幾個、
    # 以及「Planner 說沒依賴但句子沒有主詞」的分歧數。同 `unit_stats`／`replan_stats`
    # 的角色——是**分母**不是判定。
    plan_stats: dict
    # 重生成回歸守衛的計數（見 `_accept_revision`）：accepted／rejected／empty ＋ 逐筆明細。
    # 是**分母**不是判定——「六道修補鏈的重生成到底破壞了什麼」在此之前完全沒有量尺。
    revision_stats: dict


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
            # **2026-09-06 起由 Planner 宣告**（見 `_PLANNER_PROMPT` 的【依賴 depends_on】），
            # 取代原本用 `_BACKREF_RE` 回指詞表反推的作法。`deps_declared` 分辨「Planner 說
            # 沒有」與「Planner 沒說」——少了它詞表仍然是實際做決定的人（見 `_parse_plan_output`）。
            "depends_on": sub.get("depends_on"),
            "deps_declared": bool(sub.get("deps_declared")),
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
    # 業界那三段的第二段：執行前跑確定性結構檢驗（無懸空引用／無環），非法的 prune ＋ trace。
    todos, plan_stats = validate_plan_dependencies(todos)
    _trace(f"plan: {len(todos)} todos → "
           f"{[(t['task'], t['route'], t['depends_on']) for t in todos]} stats={plan_stats}")
    return {"todos": todos, "collected": [], "web_notes": [], "period_notes": [],
            "iterations": 0, "sufficient": False, "plan_stats": plan_stats}


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
    # ⚠ 傳整個 todo 而不是 `todos[i]["task"]`：判定要讀 `depends_on`／`deps_declared`
    #   兩個欄位（見 `_is_dependent_hop`）。只傳 task 的話，Planner 的宣告等於沒接上。
    ready = [i for i in all_pending if not _is_dependent_hop(todos[i])]
    deferred = [i for i in all_pending if _is_dependent_hop(todos[i])]
    if ready:
        pending_idxs = ready
    else:
        # 只剩依賴型 → 第一跳已完成:把「該公司」回填成解出的具體公司,再檢索(否則無 ticker → 撈回隨機 chunk)。
        for i in deferred:
            # 實體逐 todo 解析：Planner 宣告了 `depends_on` 就只看那一個父 todo，
            # 沒宣告才退回「所有 done todo 的 ticker 眾數」（舊行為）。見 `_resolve_hop_entity`。
            entity = _resolve_hop_entity(todos, state.get("collected", []),
                                         parent_id=todos[i].get("depends_on"))
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
            raw = rq.call_llm(messages, _pkg.CHECKER_MODEL, temperature=0.0)
        data = _loads_json_lenient(raw)
        # 解析失敗（data is None）也照錄：那代表「這一輪 replan 什麼都沒做」,是個穩定的結果,
        # 重放時要重現同一件事。不錄的話一次暫時性的解析失敗會讓整輪不可重現。
        _replay.put("replan", _rk, data)

    # multi_hop 保護：還沒跑的依賴型第二跳(帶未解「該公司」代名詞)是回答原始問題的必要一跳,
    # 不能被機率性 replanner 誤 drop、也不能因 hop-1 一做完就被判 sufficient 跳過。此時強制續跑。
    has_pending_dependent = any(
        t["status"] == "pending" and _is_dependent_hop(t) for t in todos)

    # 待辦額度統計。⚠ **自己讀舊值再合併**：LangGraph 對沒有 reducer 的 key 是**取代**語意，
    #   直接回一份新的會讓前面幾輪 replan 的計數整個消失（⑭i 守這條）。
    # ⚠ 三個計數一律**在場**，沒發生就是 0 而不是缺席——消費端要分得出「replanner 不想加」
    #   與「想加但額度滿了」，缺席讓兩者外觀相同（同閘門⑲l4 的教訓）。
    _prev = state.get("replan_stats") or {}
    _rstats = {
        "rounds": int(_prev.get("rounds", 0)) + 1,
        "added": int(_prev.get("added", 0)),
        "refused_budget": int(_prev.get("refused_budget", 0)),
        "refused_tasks": list(_prev.get("refused_tasks", [])),
    }

    sufficient = False
    if isinstance(data, dict):
        sufficient = _coerce_bool(data.get("sufficient", False))
        drop_ids = {int(x) for x in (data.get("drop", []) or []) if str(x).lstrip("-").isdigit()}
        for t in todos:
            if t["id"] in drop_ids and t["status"] == "pending" and not _is_dependent_hop(t):
                t["status"] = "dropped"
        next_id = max((t["id"] for t in todos), default=-1) + 1
        # ⚠ 用**同一個** `_parse_plan_output` 正規化（唯一定義點）：它同時吃舊格式的
        #   `["字串"]`（既有 replay 快取錄的全是這種 → 落到 kb）與新格式的
        #   `[{"task":…, "route":…}]`。理由與 Planner 那邊逐字相同，見該函式 docstring。
        for a in _parse_plan_output(data.get("add", []) or []):
            # ⚠ **額度用完的拒絕原本是這個 `if` 的隱含 else：沒有 trace、沒有計數**
            #   （2026-09-06 改）。另外兩個拒絕分支都有 trace，只有最常發生的這個沒有，於是
            #   「replanner 想加 3 個但額度滿了」與「replanner 什麼都不想加」在結果檔裡**外觀
            #   完全相同** → `probe_replan_contribution` 量到的「Replanner 貢獻 0」在拆得細的
            #   題上讀不出來。改成早退 ＋ 計數（行為與舊版逐字相同，閘門⑭f 是那條回歸護欄）。
            #   ⚠ 計數**刻意與另外兩個拒絕分支分開**：三種原因是三種不同的病，合併成一個
            #   數字就再也分不出來（⑭e 守這條）。
            if len(todos) >= MAX_TODOS:
                _rstats["refused_budget"] += 1
                _rstats["refused_tasks"].append(a["task"])
                _trace(f"replan: 待辦額度已滿（MAX_TODOS={MAX_TODOS}，Planner 先用掉大部分）"
                       f"→ 拒絕 {a['task']!r}")
                continue
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
                "attributable": False,
                "route": a_route,
                # replan 建的 todo **刻意標成「沒宣告」**：Replanner 的 prompt 沒有教它填這個
                # 欄位，標 True 會把「說沒有」這個語意套到根本沒被問過的東西上。沒宣告 →
                # `_is_dependent_hop` 退回詞表 ＝ 與 2026-09-06 之前逐字相同。
                "depends_on": None,
                "deps_declared": False,
                "freshness_gaps": [],   # 同上：執行完由 _node_execute 回填
                "period_notes": [],
                "web_used": False,
                "status": "pending",
                "result": "",
            })
            next_id += 1
            _rstats["added"] += 1
    if has_pending_dependent:   # 第二跳未跑 → 一律不收斂,強制回 execute 把它做完
        sufficient = False
    if sufficient:
        for t in todos:
            if t["status"] == "pending":
                t["status"] = "dropped"
    _trace(f"replan: sufficient={sufficient} "
           f"todos={[(t['id'], t['status']) for t in todos]} stats={_rstats}")
    return {"todos": todos, "sufficient": sufficient, "replan_stats": _rstats}


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
    # 回歸守衛的計數：五道會重生成的 validator 共用同一份（見 `_accept_revision`）。
    _rev_stats: dict = {}
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
            answer = _pkg._consistency_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose, rev_stats=_rev_stats,
                                                web_extra=web_extra, period_note=period_note)
        # 確定性期別稽核（零 LLM 偵測）：答案自稱「最新一季」卻引用了較舊的期別 → 重生成一次。
        # 放在一致性之後、reflect 之前：期別改對可能連帶換掉數字，要讓 reflect 稽核最終版本。
        if not rq.looks_like_refusal(answer):
            answer = _pkg._period_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose, rev_stats=_rev_stats,
                                           web_extra=web_extra, period_note=period_note)
        if state.get("enable_reflection", True) and not rq.looks_like_refusal(answer):
            answer = _pkg._reflect_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose, rev_stats=_rev_stats,
                                      web_extra=web_extra, period_note=period_note)
        # 數字溯源（零 LLM 偵測）**放最後**：這是唯一會看 reflect 重生成結果的檢查。
        # 100 題乾跑誤報 0 題（見 find_untraceable_numbers 的兩條排除規則），所以放進主線不會
        # 擾動既有基準；真的觸發才花一次重生成。
        if not rq.looks_like_refusal(answer):
            answer = _pkg._number_check_and_fix(state["query"], answer, chunks, GEN_MODEL, verbose=verbose, rev_stats=_rev_stats,
                                           web_extra=web_extra, period_note=period_note)
        # R5 並陳時點（零 LLM 偵測）**放最末**：時點是措辭問題，而上面每一道的重生成都可能
        # 把時點改掉；放中間等於只驗了一個會被後面推翻的版本。281 份既有答案乾跑觸發 1 次
        # 且是真陽性（見 find_undated_dual_sourcing），所以不會擾動既有基準。
        if not rq.looks_like_refusal(answer):
            answer = _pkg._dual_source_check_and_fix(state["query"], answer, chunks, GEN_MODEL, rev_stats=_rev_stats,
                                                verbose=verbose, web_extra=web_extra,
                                                period_note=period_note)
    except Exception as e:
        _trace(f"synthesize: 最終生成失敗（{e!r}）→ 退回零 LLM 機械式摘要")
        answer = _mechanical_summary(state["query"], chunks) if chunks else \
            "I don't have enough information in my knowledge base to answer this."

    # 確定性單位換算：Rule 11 要 Writer 原樣保留 "$X billion"，這裡由純程式把 billion/million→億
    # 乘算正確（程式算不會錯、零誤報），根治 LLM 的 billion→億 音譯 10x 病（reflect 共用同盲點靠不住）。
    # ⚠ 2026-09-03 改走 `finalize_answer_units`：`convert_usd_units_to_yi` 單獨用有兩個洞——
    #   LLM 先斬後奏寫「億」時它明文不碰（結構性失明），而它自己又不冪等 → LLM 寫的雙寫
    #   會被它吐成巢狀。新入口在它前面先剝、後面再依來源補，順序由那支保證。
    # `unit_stats` 是 BACKLOG〈「億」發生頻率〉缺的分母：① 與 ③ 觸發的每一筆都代表
    # LLM 違反 Rule 11 自己寫了億。**只在這裡收集、原樣往上傳，不在這裡下判定**。
    _unit_stats: dict = {}
    answer = rq.finalize_answer_units(answer, chunks, web_extra=web_extra, stats=_unit_stats)

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
    return {"answer": answer, "unit_stats": _unit_stats,
            "revision_stats": _rev_stats}


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
