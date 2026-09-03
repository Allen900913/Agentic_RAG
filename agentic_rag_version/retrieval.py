"""
三件事，都是「拿到候選之後怎麼處理」而不是「怎麼問 LLM」：
  · chunk 操作   `_merge_chunks` / `_fair_select` / `_snippet` / `_chunk_id`
  · KB 涵蓋      `_scan_kb_coverage`（掃 collection 不掃 `data/`——「碼上有哪些檔」與
                 「collection 裡真的有什麼」是兩件事，後者才是檢索看得到的）
  · ratio 意圖   欄位分類與財務錨源保底

⚠ `_merge_chunks` / `_fair_select` 是**承接池的形狀**由誰決定的地方：Grader 有圈選就只收
  圈選的，沒圈選才退回 rerank top-k 全收（量尺 `eval/probe_relevant_ids.py`）。

⚠ ratio 的判別力來自「LLM 說不是就必須不是」：判不出來會退回詞表 `_RATIO_INTENT_RE`，
  於是詞表仍是實際做決定的人，而端到端跑分看不出差別。量它的是 `eval/probe_ratio_intent.py`。

⚠ `_snippet` 的視窗要**對齊查詢詞**而不是從頭截：數據頁的數字常排在站台樣板文字後面，
  從頭截會正好切在數字前面——「撈到了卻被自己截掉」與「根本沒撈到」外觀相同。
"""

from __future__ import annotations

from datetime import date, datetime
import json
import os
import re
import threading

import llm_replay as _replay
import rag_query as rq

import agentic_rag_version as _pkg
# ⚠ **循環 import 是刻意的**：`_pkg.<name>` 在**呼叫時**才解析，於是 eval 打在
#   套件物件上的 monkeypatch 蓋得到。子模組**不得裸用**被 patch 的名字，也不得
#   `from .x import` 它們（那會壓一份當時的物件）——守門是閘門⑬。

from .tracing import _trace


_COVERAGE_SOURCE_RE = re.compile(
    r"^(?P<ticker>[A-Z]+)_(?P<kind>10K|10Q|Fundamentals|News)_"
    r"(?P<stamp>\d{4,8})(?:_|\.|$)",
    re.IGNORECASE,
)

# ────────────────────────────────────────────────────────────────────────────
# ── 原 chunks.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────


# ── 自適應生成預算（fix 2026-07-29，見 [[multi-intent-agentA-is-the-leak]]）────────
# 固定 8-cap 對多 facet 題會 8÷N 稀釋（chunk 層級 probe：軸A 佔 v2 漏失 23/40）。改成隨 facet 數放大，
# 單意圖題維持 ~base（不傷 lexical/colloquial 的 faithfulness），多 facet 才給更多名額（而非砍 facet）。
#   budget(n) = min(BASE + PER_FACET*(n-1), CAP)   例：1→5, 2→7, 3→9, 4→11, 6→15（CAP=16）
WRITER_BUDGET_BASE      = int(os.getenv("AGENTIC_WRITER_BUDGET_BASE", "5"))

WRITER_BUDGET_PER_FACET = int(os.getenv("AGENTIC_WRITER_BUDGET_PER_FACET", "2"))

WRITER_BUDGET_CAP       = int(os.getenv("AGENTIC_WRITER_BUDGET_CAP", "16"))

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


# ────────────────────────────────────────────────────────────────────────────
# ── 原 coverage.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────


_coverage_lock = threading.Lock()

# KB coverage 只依「Agent 實際連到的 Qdrant collection」計算；不能掃 data/ 目錄，因為檔案存在
# 不代表已 ingest。cache 以 collection name 為界，eval 切 collection 時會自動重算。
_kb_coverage: dict | None = None

_kb_coverage_collection: str | None = None


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
        _bge, _rerank, client = _pkg._get_models()
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






# ────────────────────────────────────────────────────────────────────────────
# ── 原 ratio.py（2026-09-03 合併）
# ────────────────────────────────────────────────────────────────────────────


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
        with _pkg._quiet():
            raw = rq.call_llm(
                [{"role": "system", "content": _RATIO_INTENT_PROMPT},
                 {"role": "user", "content": user}],
                RETRIEVAL_MODEL, temperature=0.0,
            )
        data = _pkg._loads_json_lenient(raw)
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
        _bge, reranker, client = _pkg._get_models()
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
