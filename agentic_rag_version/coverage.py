"""agentic_rag_version.coverage — KB 有哪些檔、涵蓋到哪一期。

`_scan_kb_coverage` 直接掃 collection（不是掃 `data/`）——那是刻意的：
「碼上有哪些檔」與「collection 裡真的有什麼」是兩件事，後者才是檢索看得到的。

⚠ `_get_kb_coverage` 是 eval 會 monkeypatch 的名字之一，所以它**只能被定義在這裡、
  不能被這裡裸用**（見 verify_web_gate_isolation 閘門⑬）。同模組內要叫它一律 `_pkg.`。
"""
from __future__ import annotations

import re
import threading

from .freshness import _COVERAGE_SOURCE_RE
from .tracing import _trace
from datetime import date, datetime

import rag_query as rq

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。

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




