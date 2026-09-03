"""
兩層，方向相反但同一件事：
  · **來源資格**（原 freshness）：日期抽取／過時判定／白名單與子網域／去重／財報期別排序。
    問的是「這份**來源**夠不夠新、可不可信」。
  · **答案偵測**（零 LLM）：citation／期別／衝突／R4 並陳不裁決／R5 時點／R6 web 未採用／
    數字溯源，以及缺口與對應的揭露句。問的是「這份**答案**有沒有超出證據」。

補救（`*_check_and_fix`，會重生成）**不在這裡**，留在 `__init__`：
  ① 它們呼叫 `_write_final_answer`，那是 eval 會 monkeypatch 的名字；
  ② 偵測零 LLM、可逐條斷言，補救不是。混在一起會讓「這個模組能不能零成本重跑」
     變成要逐函式判斷。

⚠ `_is_freshness_evidence` 與 `_stale_for_realtime` **必須共用同一套資格判準**——10-K 的
  4 碼財年戳、10-Q 的 6 碼期別戳是**財報期間不是發布日**，不算過期、但也不算證據。
  混淆會讓財報替整池背書說「夠新」。

⚠ 揭露句都以 `

---
⚠` 開頭，那個邊界前綴命中 `rq.EVIDENCE_TAIL_RE`，於是每個呼叫
  `strip_evidence_tail` 的消費端都看不到它——這是它們**安全**的原因（機械式附加不會改變
  任何量尺的判定），也是閘門⑱f 唯一該驗的不變量。
"""

from __future__ import annotations

from datetime import date
from datetime import date, datetime
from urllib.parse import urlparse
import calendar
import os
import re

import rag_query as rq

import agentic_rag_version as _pkg
# ⚠ **循環 import 是刻意的**：`_pkg.<name>` 在**呼叫時**才解析，於是 eval 打在
#   套件物件上的 monkeypatch 蓋得到。子模組**不得裸用**被 patch 的名字，也不得
#   `from .x import` 它們（那會壓一份當時的物件）——守門是閘門⑬。

from .retrieval import _BACKREF_RE, _TICKER_CANON, _has_ratio_intent, _resolve_ratio_fields
from .retrieval import _COVERAGE_SOURCE_RE, _format_kb_coverage, _mentioned_tickers, _source_coverage_parts
from .tracing import _trace


# ────────────────────────────────────────────────────────────────────────────
# ── 原 freshness.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────

TAVILY_MAX_RESULTS = 5            # 最終塞進 prompt 的則數
TAVILY_FETCH_RESULTS = 12         # 先多撈,再去重／限域名／濾過期,剩下的取前 TAVILY_MAX_RESULTS 則
TAVILY_PER_DOMAIN_CAP = 2         # 單一域名最多佔幾個名額（見 _dedupe_web_results 的理由）




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
    rec = (_pkg._get_kb_coverage().get("sources") or {}).get(source)
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
    cov = _pkg._get_kb_coverage()
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


# ────────────────────────────────────────────────────────────────────────────
# ── 原 validators.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────

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


# ────────────────────────────────────────────────────────────────────────────
# ── 原 gaps.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────

FRESHNESS_SNAPSHOT = "snapshot"


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
    coverage_text = _format_kb_coverage(_pkg._get_kb_coverage())
    if freshness_mode == FRESHNESS_SNAPSHOT:
        policy = """時間模式：snapshot（封閉知識庫評測）。
- 「最近／最新」只表示下方 KB snapshot 中最新可用文件，不表示真實世界今天。
- 只能使用 snapshot 或工具結果明確出現的期間；絕不靠模型記憶推算季度、年份或月份。
- 不要因 KB 早於 wall clock 而新增 web 待辦或在答案加入 cutoff 警語。
- 使用者明確指定期間時，必須尊重原期間，不得換成 KB 最新期間。"""
    else:
        web_state = "可用" if _pkg.ENABLE_WEB_SEARCH else "停用"
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
    coverage = _pkg._get_kb_coverage()
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
    子問題需要即時資料、卻沒拿到 web 補充」）。兩種缺口併存,見 `_pkg._run_one_todo` 的接線。

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
    cov = _pkg._get_kb_coverage()
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
      **web 預算用完（`_pkg.QUERY_WEB_BUDGET`）或搜不到** → 答案回頭用財報 chunk 生成 → 零揭露。
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

    ⚠ 只在 live 才會被呼叫（見 `_pkg._run_one_todo`）。snapshot 下 `_effective_route` 已把
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
