"""agentic_rag_version.validators — 答案的**確定性偵測器**（零 LLM）。

這裡只有「看得出不對」，沒有「把它改對」。補救（`*_check_and_fix`，會重生成）
留在 `__init__`——那是刻意的接縫，理由有二：
  ① 四個補救函式都呼叫 `_write_final_answer`，而那是 eval 會 monkeypatch 的名字，
     依本次重構的規則必須留在 `__init__`（見 verify_web_gate_isolation 閘門⑬）。
  ② 偵測是零 LLM、可逐條斷言的；補救不是。把兩者放同一個檔會讓「這個模組能不能
     零成本重跑」這件事變得要逐函式判斷。

住在這裡的偵測器（都被 verify_answer_validators 的 239 項直接吃）：
  · citation      `_extract_citations` / `_repair_reference_citations`
  · 期別          `_chunk_period` / `_ground_period_from_source` / `find_stale_period_claims`
  · 衝突          `find_claim_conflicts` / `find_authority_conflicts`
  · R4 web↔財報   `find_unreconciled_web_conflicts`（**要求並陳不裁決**）
  · R5 並陳時點   `find_undated_dual_sourcing`
  · R6 web 未採用 `web_fetched_but_uncited_notice`（**是揭露不是重生成**）
  · 數字溯源      `find_untraceable_numbers`

⚠ **本模組不得定義任何被 eval monkeypatch 的名字**。唯一用到的是 `_get_kb_coverage`，
  走 `_pkg.` 在呼叫時解析。
"""
from __future__ import annotations

import re

import rag_query as rq

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 呼叫時才解析，
                                     #   eval 打在套件上的 monkeypatch 才蓋得到。
from .freshness import _fiscal_label, _fiscal_rank


# 生產 citation 格式 [filename, chunk #N]：確定性 validator 用它抽引用比對 allowlist。
# 同時吃全形括號【】與全形逗號，：（gpt-oss-120b 生成中文會把 [] 轉全形，只認 ASCII 會 false-negative）。
_CITE_RE = re.compile(r"[\[【]([^\[\]【】,，]+?)[,，]\s*chunk\s*#?\s*(\d+)[\]】]", re.IGNORECASE)
_REF_PREFIX_RE = re.compile(r"^\s*reference\s*\d+\s*:\s*", re.IGNORECASE)
# 「只有序號、沒有檔名」的引用：`【Reference 7, chunk #15】`。序號是**我們自己給的**
# （`rq.build_user_prompt` 產生 `[Reference i+1: source, chunk #idx]`），所以它可以被
# 確定性地還原成檔名——見 `_repair_reference_citations`。
_BARE_REF_CITE_RE = re.compile(
    r"([\[【])\s*reference\s*(\d+)\s*[,，]\s*chunk\s*#?\s*(\d+)\s*([\]】])", re.IGNORECASE)


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
    return str(((_pkg._get_kb_coverage().get("sources") or {}).get(source) or {}).get("kind") or "")


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
