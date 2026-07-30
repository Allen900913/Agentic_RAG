"""
migrate_add_period_basis.py — 一次性 migration（2026-07-30）：對生產 collection
（rag_query.COLLECTION_NAME，預設 us_stock_rag_edgar_exp4）做兩件 in-place 修改，
**不重新 embed、不重打 SEC API**：

  Item 3｜補 period_basis 口徑標籤（set_payload，依 doc_type）：
    - doc_type=fundamentals             → period_basis="TTM"
    - doc_type in (10-K, 10-Q, income_statement) → period_basis="fiscal_year"
    - news / other                      → 不標
    供 rag_query._detect_period_basis 的 TTM 硬 filter 把「最近十二個月」題導向 Fundamentals。

  Item 2｜語料去重（delete points）：
    Fundamentals / IncomeStatement 每個 ticker 只保留「最新一份快照」（如 0508/0519/0612
    → 只留 0612），其餘舊快照的 points 整批刪除。News 不動（時間序列）。

另建 period_basis 的 keyword payload index。

與 data_update_edgar.py 的程式碼修改一致：未來 `--rebuild` 會重現同樣的（去重 + 標籤）狀態，
本 migration 只是讓「現有 live collection」立即對齊，省一次全量重建。

Usage:
  .venv/Scripts/python.exe migrate_add_period_basis.py            # dry-run（只印計畫，不寫）
  .venv/Scripts/python.exe migrate_add_period_basis.py --apply    # 實際套用
"""

import argparse
import re

import rag_query as rq


def _period_basis_for(doc_type: str) -> str | None:
    dt = (doc_type or "").lower()
    if dt == "fundamentals":
        return "TTM"
    if dt in ("10-k", "10-q", "income_statement"):
        return "fiscal_year"
    return None


_STAMP_RE = re.compile(r"_(\d{8})(?=[_.]|$)")


def _stamp(stem: str) -> str:
    m = _STAMP_RE.search(stem)
    return m.group(1) if m else ""


def scan(client, collection: str):
    """回傳 (sources: dict[source]->{'doc_type','ticker','count'}, doc_type_counts)。"""
    sources: dict[str, dict] = {}
    doc_type_counts: dict[str, int] = {}
    offset = None
    while True:
        pts, offset = client.scroll(collection, limit=512, offset=offset,
                                    with_payload=True, with_vectors=False)
        for p in pts:
            pl = p.payload or {}
            src = str(pl.get("source") or "")
            dt = str(pl.get("doc_type") or "")
            doc_type_counts[dt] = doc_type_counts.get(dt, 0) + 1
            if src not in sources:
                sources[src] = {
                    "doc_type": dt,
                    "ticker": str(pl.get("ticker") or (re.match(r"^([A-Z]+)_", src).group(1)
                                                       if re.match(r"^([A-Z]+)_", src) else "")),
                    "count": 0,
                }
            sources[src]["count"] += 1
        if offset is None:
            break
    return sources, doc_type_counts


def compute_stale(sources: dict[str, dict]) -> list[str]:
    """Fundamentals / IncomeStatement 每個 (ticker, doc_type) 只留最新 stamp，其餘為 stale。"""
    latest: dict[tuple[str, str], str] = {}
    for src, info in sources.items():
        if info["doc_type"].lower() not in ("fundamentals", "income_statement"):
            continue
        key = (info["ticker"], info["doc_type"].lower())
        st = _stamp(src.rsplit(".", 1)[0])
        if st and (key not in latest or st > latest[key]):
            latest[key] = st
    stale = []
    for src, info in sources.items():
        if info["doc_type"].lower() not in ("fundamentals", "income_statement"):
            continue
        key = (info["ticker"], info["doc_type"].lower())
        st = _stamp(src.rsplit(".", 1)[0])
        if st and st != latest.get(key):
            stale.append(src)
    return sorted(stale)


def main() -> None:
    ap = argparse.ArgumentParser(description="Add period_basis + dedup stale snapshots (in-place)")
    ap.add_argument("--apply", action="store_true", help="實際套用（不加則 dry-run）")
    ap.add_argument("--collection", default=rq.COLLECTION_NAME)
    args = ap.parse_args()

    from qdrant_client import models

    client = rq.make_qdrant_client()
    col = args.collection
    before = client.count(col, exact=True).count
    print(f"[INFO] Collection: {col}  (points={before})")

    sources, dtc = scan(client, col)
    print(f"[INFO] doc_type counts: {dtc}")
    stale = compute_stale(sources)

    # 計畫摘要
    ttm_dt = ["fundamentals"]
    fy_dt = ["10-K", "10-Q", "income_statement"]
    print("\n=== 計畫 ===")
    print(f"period_basis=TTM        ← doc_type in {ttm_dt}")
    print(f"period_basis=fiscal_year← doc_type in {fy_dt}")
    print(f"\nstale 快照（將刪除 {len(stale)} 個 source 的 points）:")
    for s in stale:
        print(f"  - {s}  ({sources[s]['count']} pts)")
    if not stale:
        print("  （無 stale，可能已去重過）")

    if not args.apply:
        print("\n[DRY-RUN] 未寫入。加 --apply 實際套用。")
        return

    # ── 1. set_payload period_basis（依 doc_type filter，一次一種值）────────────
    def _set_basis(doc_types: list[str], basis: str):
        flt = models.Filter(should=[
            models.FieldCondition(key="doc_type", match=models.MatchValue(value=dv))
            for dv in doc_types
        ])
        client.set_payload(collection_name=col, payload={"period_basis": basis}, points=flt)

    _set_basis(ttm_dt, "TTM")
    _set_basis(fy_dt, "fiscal_year")
    print("\n[APPLY] period_basis 已補（TTM / fiscal_year）。")

    # ── 2. 刪除 stale 快照 points（依 source match）─────────────────────────────
    for s in stale:
        client.delete(collection_name=col, points_selector=models.FilterSelector(
            filter=models.Filter(must=[
                models.FieldCondition(key="source", match=models.MatchValue(value=s))])))
    print(f"[APPLY] 已刪除 {len(stale)} 個 stale source 的 points。")

    # ── 3. period_basis keyword index（已存在則忽略）────────────────────────────
    try:
        client.create_payload_index(collection_name=col, field_name="period_basis",
                                    field_schema=models.PayloadSchemaType.KEYWORD)
        print("[APPLY] period_basis payload index 已建立。")
    except Exception as e:
        print(f"[APPLY] period_basis index 已存在或建立略過：{e!r}")

    after = client.count(col, exact=True).count
    print(f"\n[DONE] points {before} → {after}（刪除 {before - after}）")

    # 驗證：抽樣印各 doc_type 的 period_basis
    print("\n=== 驗證抽樣 ===")
    for dv in ("fundamentals", "income_statement", "10-K", "news"):
        pts, _ = client.scroll(col, limit=1, with_payload=True, with_vectors=False,
                               scroll_filter=models.Filter(must=[
                                   models.FieldCondition(key="doc_type", match=models.MatchValue(value=dv))]))
        if pts:
            pl = pts[0].payload or {}
            print(f"  doc_type={dv:16s} → period_basis={pl.get('period_basis', '(none)')!r} | {pl.get('source')}")


if __name__ == "__main__":
    main()
