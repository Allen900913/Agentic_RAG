"""agentic_rag_version.freshness — 「這份來源夠不夠新、可不可信」。

這一群管的是**外部來源的資格判定**，四件事其實是同一個問題的四面：
  · 日期抽取   `_url_published_date` / `_content_published_date` / `_web_result_date`
  · 過時判定   `_stale_for_realtime` / `_classify_staleness` / `_kb_ceiling_date`
  · 來源資格   `WEB_ALLOWED_DOMAINS` / `_domain_admits` / `_host_allowed`
  · 結果整理   `_dedupe_web_results`（去重／單域名上限／濾過期／截斷）

⚠ **`_is_freshness_evidence` 與 `_stale_for_realtime` 必須共用同一套資格判準**——
  10-K 的 4 碼財年戳、10-Q 的 6 碼期別戳是**財報期間不是發布日**，
  它們**不算過期、但也不算證據**。混淆這兩件事會讓財報替整池背書說「夠新」。

⚠ **本模組不得定義任何被 eval monkeypatch 的名字**（見 verify_web_gate_isolation 閘門⑬）。
  唯一用到的那個是 `_get_kb_coverage`，所以它走 `_pkg.` 在**呼叫時**解析。
"""
from __future__ import annotations

import calendar
import os
import re
from datetime import date, datetime
from urllib.parse import urlparse

import agentic_rag_version as _pkg   # ⚠ 循環 import 是刻意的：`_pkg.<name>` 在**呼叫時**
                                     #   才解析，於是 eval 打在套件上的 monkeypatch 蓋得到。
from .tracing import _trace


TAVILY_MAX_RESULTS = 5            # 最終塞進 prompt 的則數
TAVILY_FETCH_RESULTS = 12         # 先多撈,再去重／限域名／濾過期,剩下的取前 TAVILY_MAX_RESULTS 則
TAVILY_PER_DOMAIN_CAP = 2         # 單一域名最多佔幾個名額（見 _dedupe_web_results 的理由）


_COVERAGE_SOURCE_RE = re.compile(
    r"^(?P<ticker>[A-Z]+)_(?P<kind>10K|10Q|Fundamentals|News)_"
    r"(?P<stamp>\d{4,8})(?:_|\.|$)",
    re.IGNORECASE,
)


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
