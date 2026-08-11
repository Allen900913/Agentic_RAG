"""驗收 ingest 的「期間章節 + 通用小標」兩層硬邊界（零網路、零 embedding、確定性）。

**為什麼需要獨立的驗收腳本**：這兩層都是 ingest 期改動，只有重建 collection 才生效，而
重建要一小時、且表格抽取本身有跑次間變異（見 CLAUDE.md「重建不會逐字重現舊 collection」）。
所以正確的驗收順序是**先在原文上驗切段邏輯，再決定要不要付重建的成本**——本腳本只讀本機
`.nc` 取 Item 原文（`data_update_edgar._filings_from_manifest`，零網路），直接呼叫
`_split_by_period_section` / `_split_by_subheading`，不碰 Qdrant 也不碰 GPU，
因此可以在別的實驗正在跑的時候安全執行。

四個判準（全部確定性，無抽樣變異）：
  ① **該切的切到**：散文型 Item 應切出真標題（`--show` 看標籤內容）。特別是 MSFT 的
     MD&A 期間章節必須切出 Productivity / Intelligent Cloud / More Personal Computing。
  ② **不該動的不動**：表格為主體的 Item（`_TABLE_DOMINATED_ITEMS`）必須 0 段。
  ③ **表格沒被切開**：標題名同時是表格列標籤，切段前後每個標籤在該章節的出現次數必須
     完全相同。任何不符會印 `[!!]` 並以 exit 1 結束。
  ④ **目標句歸屬正確**：mix-03 的病灶句 'Operating income increased $2.7 billion or 24%'
     必須落進 Intelligent Cloud 段（它是分部數字，不是全公司總計）。

另外印出**通用化前後的覆蓋率對照**：帶標籤的散文區段佔比。2026-08-09 通用化前只有
MSFT 拿得到標籤（全庫 2.8%），因為只有 MSFT 寫 `X Compared with Y` 期間標題。

用法：
    .venv/Scripts/python.exe eval/verify_segment_split.py
    .venv/Scripts/python.exe eval/verify_segment_split.py --tickers MSFT --show
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_update_edgar as du

# mix-03 的兩個關鍵句：前者是 Intelligent Cloud 分部、後者才是合併總計（用合併損益表
# 驗算過：38,398-32,000=+6,398(+20%) 是合併，13,753-11,095=+2,658(+24%) 是 Intelligent Cloud）
TARGETS = [
    "Operating income increased $2.7 billion or 24%",
    "Operating income increased $6.4 billion or 20%",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+",
                    default=["MSFT", "NVDA", "AAPL", "GOOGL", "AMZN", "META", "TSLA"])
    ap.add_argument("--show", action="store_true", help="逐段列印標籤與長度")
    args = ap.parse_args()

    du._enable_local_storage()
    n_bad = 0
    n_table_item_split = 0
    labelled_chars = total_chars = 0
    per_filing: dict[str, int] = {}
    label_pool: Counter[str] = Counter()
    orphan_before: Counter[str] = Counter()   # ⑤ 併之前「說了漲跌卻不帶幅度」的段數
    orphan_after: Counter[str] = Counter()    # ⑤ 併之後（必須是 0）
    for tk in args.tickers:
        for form, filing in du._filings_from_manifest(tk):
            obj = filing.obj()
            valid = du.VALID_10K_ITEMS if form == du.SEC_FORM_10K else du.VALID_10Q_ITEMS
            item_texts = {}
            for name in dict.fromkeys(obj.items):
                if name not in valid:
                    continue
                try:
                    text = (obj[name] or "").strip()
                except Exception as e:                       # noqa: BLE001
                    print(f"  [WARN] {tk} {form} item {name}: {e!r}")
                    continue
                if len(text) >= 20:
                    item_texts[name] = text
            tag = f"{tk} {form} {filing.accession_no}"
            n_sections = 0
            for item_name, item_text in item_texts.items():
                # 鏡射生產程式的 gate（見 _TABLE_DOMINATED_ITEMS）
                use_heading = item_name not in du._TABLE_DOMINATED_ITEMS
                for plabel, sect in du._split_by_period_section(item_text):
                    subs = (du._split_by_subheading(sect) if use_heading
                            else [(None, sect)])
                    # ⑤ 幅度接地（鏡射生產程式的 gate，見 _merge_unquantified_sections）
                    raw_subs = subs
                    if use_heading and item_name in du._MDNA_ITEMS and len(subs) > 1:
                        subs = du._merge_unquantified_sections(subs)
                    if item_name in du._MDNA_ITEMS:
                        orphan_before[tk] += sum(
                            1 for _, b in raw_subs if du._is_orphan_explainer(b))
                        # 閘門問的是「**還能接地卻沒接**的段」，不是「宇宙中沒有孤兒句」：
                        # 前一段本身沒有數字時無處可接，硬併只會貼錯標（實測 MSFT
                        # `Interest and dividends income increased…` 就是章節首段）。
                        for i, (_, b) in enumerate(subs):
                            if du._is_orphan_explainer(b) and i > 0 \
                                    and du._MAGNITUDE_RE.search(subs[i - 1][1]):
                                orphan_after[tk] += 1
                                print(f"  [!!] {tag} {item_name}: 可接地卻沒接 "
                                      f"({len(b)} 字元) {b.strip()[:70]!r}")
                    total_chars += len(sect)
                    labelled_chars += sum(len(b) for s, b in subs if s or plabel)
                    if len(subs) > 1:
                        n_sections += 1
                        if not use_heading:
                            n_table_item_split += 1     # ② 不該發生
                        if args.show:
                            print(f"{tag} | {item_name} | period={plabel}")
                        for slabel, body in subs:
                            if slabel:
                                label_pool[slabel] += 1
                            hit = [t for t in TARGETS if t in body]
                            if args.show or hit:
                                mark = ("   <<< " + "; ".join(hit)) if hit else ""
                                print(f"      {len(body):7d} chars  "
                                      f"section={slabel or '(章節層)'}{mark}")
                    # ③ 表格對帳
                    names = {s for s, _ in subs if s}
                    before = {s: sect.count(s) for s in names}
                    after = {s: sum(b.count(s) for _, b in subs) for s in names}
                    if before != after:
                        n_bad += 1
                        print(f"  [!!] {tag} {item_name}: 切段改變了字串出現次數 "
                              f"{before} -> {after}")
            per_filing[tag] = n_sections
    print("\n── 每份 filing 有小標切段的章節數 ──")
    for tag, n in per_filing.items():
        print(f"  {'*' if n else ' '} {tag:44s} {n}")
    print(f"\n覆蓋率：帶標籤（期間或小標）的散文字元 "
          f"{labelled_chars}/{total_chars} = {labelled_chars/max(1,total_chars):.1%}"
          f"（通用化前全庫 chunk 層級是 2.8%，且全部是 MSFT）")
    print(f"抓到的小標共 {len(label_pool)} 種，出現最多的 15 種：")
    for lbl, n in label_pool.most_common(15):
        print(f"    {n:3d}  {lbl}")
    print("\n── 判準⑤ 幅度接地：MD&A 裡「短句解釋段卻不帶幅度」的段 ──")
    for tk in sorted(set(orphan_before) | set(orphan_after),
                     key=lambda t: -orphan_before[t]):
        print(f"    {tk:6} 併之前 {orphan_before[tk]:3} → 併之後仍可接地卻沒接 "
              f"{orphan_after[tk]:3}")
    n_orphan = sum(orphan_after.values())
    print(f"    合計 {sum(orphan_before.values())} → {n_orphan}")
    print(f"\n判準②：表格型 Item 被切段次數 = {n_table_item_split}（必須是 0）")
    print(f"判準③：[!!] 共 {n_bad} 筆（必須是 0）")
    print(f"判準⑤：併之後仍不自足的段 = {n_orphan}（必須是 0）")
    if n_bad or n_table_item_split or n_orphan:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
