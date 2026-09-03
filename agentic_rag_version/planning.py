"""agentic_rag_version.planning — Plan 與 Grade 兩個 LLM 節點的輸入輸出契約。

⚠ **`_parse_plan_output` 要吃兩種格式**：舊 fixture 錄的 plan 是純 `list[str]`
  （加 `route` 之前），新的是 `list[dict]`。舊格式的 route 預設**必須是 `kb`** ——
  改這個預設等於讓所有既有重放行為悄悄改變（閘門⑪n 守它）。

⚠ **非法 route 必須當場炸**，不可靜默預設成 kb：那個失敗外觀與「路由真的判成 kb」
  完全相同（閘門⑪f）。

⚠ `_check_sufficiency` 是 Grader，也是 eval 會 monkeypatch 的名字之一。
  它回的 `relevant_ids` 是承接池的實際守門員（有圈選就只收圈選的），
  量尺是 `eval/probe_relevant_ids.py`。
"""
from __future__ import annotations

import json
import os
import re

import llm_replay as _replay
import rag_query as rq
from .tracing import _trace

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。
from .chunks import _chunk_id, _snippet
from .freshness import NO_REALTIME_SOURCE, REALTIME_STALE_DAYS, _classify_staleness, _get_as_of_date
from .gaps import FRESHNESS_SNAPSHOT

# CHECKER_MODEL：planner 拆解 / sufficiency 判斷 / reflection 幻覺稽核共用（要結構化 JSON 可靠 + 快）。
# gpt-oss-120b：tool-call/結構化輸出快又合法、無下架風險。
CHECKER_MODEL   = os.getenv("AGENTIC_CHECKER_MODEL", "openai/gpt-oss-120b")

MAX_SUBQUERIES    = 7     # planner 拆解上限。設 7 讓 Magnificent Seven 等具名集合能逐一列滿(5 會被迫

                          #   丟掉 2 家,news-13 就是這樣漏掉 NVDA);超過 7 的集合走 prompt 的「寬鬆 fallback」。
POOL_RETURN_K     = int(os.getenv("AGENTIC_POOL_RETURN_K", "5"))   # 餵給 Checker 看的候選片段數（top-k by rerank）。env 可覆蓋供 ablation。

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
    system_prompt = _PLANNER_PROMPT + "\n\n" + _pkg._build_temporal_contract(freshness_mode)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": query}]
    with _pkg._quiet():
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
    with _pkg._quiet():
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
