"""就地把既有 Fundamentals `.txt` 的比率欄位從小數改寫成百分比（零網路、確定性）。

**為什麼不重跑 `fetch_data.py`**：那會去 yfinance 抓**新的快照**，數值全變 → `reference_answers.json`
（gold）立刻失效，而 gold 有 24 處人工校正、不是腳本能免費重現的。這支只改**寫法**不改**數值**，
所以 gold 完全不受影響——這是本次改動能單獨當一個實驗變數的前提。

改的欄位與理由見 `fetch_data.py::_pct`。刻意**不動** `Debt/Equity`(79.548)、`Current Ratio`(1.07)、
P/E、Price/Sales、Price/Book、EV/EBITDA、EPS——它們不是百分比。`Dividend Yield` 只加 `%` 不乘 100
（yfinance 已回百分點：AAPL 0.37 = 0.37%，乘 100 會錯 100 倍）。

⚠ **必須連 gold 一起改，不能只改 .txt**：實測 **19 題**的 `reference_answers.json` 內文直接引用了
小數字面值（mi-04「來源列示為 0.166 的年度成長率」、mh-02 引了三個…）。只改語料的話，那些 gold
敘述會變成「contexts 裡查不到」→ `context_recall` 假性下降，而系統其實沒退步。這正是 memory
`eval-gold-version-drift-bug` 記的同一類坑：**語料改寫必須同步 gold**。所以本腳本兩段一起做，
`--dry-run` 會把兩邊要改的都列出來。

用法：
    python eval/migrate_fundamentals_pct.py --dry-run     # 先看會改什麼（語料 + gold）
    python eval/migrate_fundamentals_pct.py               # 實際改寫（會先備份到 *.bak_pct）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
import sys
# ⚠ Windows 主控台預設 cp950，而本檔的報表帶著 ⚠／✔／① 等字元 ⇒ **印到一半就 crash**，
#   而 crash 的退出碼與「有 FAIL」外觀相同 ＝ 把量尺自己的失敗讀成系統的失敗。
#   2026-09-11 普查：eval/ 的 51 支裡有 27 支帶著這個地雷，其中兩支當天真的踩了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass


FUND_DIR = Path("data/raw/Fundamentals")

# 欄位標籤 → 是否已經是百分點（True 就只加 %，不乘 100）
RATIO_FIELDS: dict[str, bool] = {
    "Profit Margin": False,
    "Operating Margin": False,
    "Gross Margin": False,
    "Revenue Growth (YoY)": False,
    "Earnings Growth": False,
    "Return on Equity": False,
    "Return on Assets": False,
    "Dividend Yield": True,
}


def convert_line(line: str) -> tuple[str, bool]:
    """回傳 (新行, 是否改過)。已經是百分比或非數值一律原樣返回（本腳本可重複執行）。"""
    for label, already_pct in RATIO_FIELDS.items():
        m = re.match(rf"^({re.escape(label)}\s*:\s*)(-?\d+(?:\.\d+)?)\s*$", line)
        if not m:
            continue
        head, raw = m.group(1), m.group(2)
        v = float(raw)
        return f"{head}{v if already_pct else v * 100:.2f}%", True
    return line, False


GOLD_PATH = Path("eval/reference_answers.json")


def collect_decimal_map(prefer_backup: bool = True) -> dict[str, str]:
    """建立「小數字面值 → 百分比字串」對照表（只含要被改寫的欄位）。

    ⚠ **一定要讀改寫前的內容**。第一版寫成「先改 .txt、再掃 .txt 建表」——改完之後小數已經
    不在檔案裡，表是空的，gold 一題都沒修到（2026-08-09 實際踩到）。所以：若同目錄有
    `*.bak_pct` 備份就優先讀它，否則讀 .txt（尚未改寫時的正常情況）。
    `Dividend Yield` 不進表：它只加 `%` 不乘 100，字面值不變，gold 不需要跟著改。
    """
    out: dict[str, str] = {}
    labels = "|".join(re.escape(k) for k, already in RATIO_FIELDS.items() if not already)
    pat = re.compile(rf"^({labels})\s*:\s*(-?\d+\.\d+)\s*$")
    for path in sorted(FUND_DIR.glob("*_Fundamentals_*.txt")):
        bak = path.with_suffix(path.suffix + ".bak_pct")
        src = bak if (prefer_backup and bak.exists()) else path
        for line in src.read_text(encoding="utf-8").splitlines():
            m = pat.match(line.strip())
            if m:
                out[m.group(2)] = f"{float(m.group(2)) * 100:.2f}%"
    return out


def patch_gold(dec_map: dict[str, str], dry_run: bool) -> int:
    """把 gold 內文引用的小數字面值換成百分比。**不動任何數值語意**，只換寫法。
    長字面值先替換（避免 `0.166` 誤中 `0.1667` 之類的前綴重疊）。"""
    if not GOLD_PATH.exists():
        print(f"  [WARN] {GOLD_PATH} 不存在，跳過 gold 修補")
        return 0
    # 用 read_bytes 不用 read_text：text 模式的 universal newlines 會把 CRLF 就地轉成 \n，
    # 於是「原檔是不是 CRLF」永遠偵測不到、寫回時整檔行尾被翻掉（2026-08-09 踩到：4265 個
    # CRLF 全變 LF、git diff 假性爆量）。memory `eval-gold-version-drift-bug` 記的是鏡像方向。
    raw_bytes = GOLD_PATH.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))
    order = sorted(dec_map, key=len, reverse=True)
    n_items = 0
    for item in data:
        ref = item.get("reference") or ""
        new = ref
        hits = []
        for d in order:
            if d in new:
                hits.append((d, dec_map[d]))
                new = new.replace(d, dec_map[d])
        if hits:
            n_items += 1
            print(f"  {'~' if dry_run else '+'} gold {item['id']:8s} " +
                  "、".join(f"{a}→{b}" for a, b in hits))
            item["reference"] = new
    if not dry_run and n_items:
        shutil.copy2(GOLD_PATH, GOLD_PATH.with_suffix(GOLD_PATH.suffix + ".bak_pct"))
        # 保 LF：這個檔 HEAD 是 CRLF，用 newline="" 原樣寫回 dumps 的 \n 會變 LF-only，
        # 造成整檔假性 diff。先 dumps 再自行換行以維持原始行尾。
        nl = "\r\n" if b"\r\n" in raw_bytes else "\n"
        body = json.dumps(data, ensure_ascii=False, indent=2)
        GOLD_PATH.write_bytes(body.replace("\n", nl).encode("utf-8"))
    return n_items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只列出會改的行，不寫檔")
    ap.add_argument("--skip-gold", action="store_true",
                    help="只改語料不改 gold。⚠ 幾乎一定是錯的（見檔頭），只在 gold 已另外處理時用")
    args = ap.parse_args()

    files = sorted(FUND_DIR.glob("*_Fundamentals_*.txt"))
    if not files:
        raise SystemExit(f"{FUND_DIR} 找不到 *_Fundamentals_*.txt")

    # 先建表再改檔——順序反了表會是空的（見 collect_decimal_map 的警告）
    dec_map = collect_decimal_map()

    total = 0
    for path in files:
        # 逐行處理並保留原始行尾（避免整檔 LF↔CRLF 翻轉造成假性 diff）
        raw_b = path.read_bytes()                      # bytes：同 patch_gold 的理由
        newline = "\r\n" if b"\r\n" in raw_b else "\n"
        out_lines, changed = [], []
        for line in raw_b.decode("utf-8").replace("\r\n", "\n").split("\n"):
            new, hit = convert_line(line)
            if hit and new != line:
                changed.append((line, new))
            out_lines.append(new)
        if not changed:
            print(f"  = {path.name}（無需改動）")
            continue
        total += len(changed)
        print(f"  {'~' if args.dry_run else '+'} {path.name}  {len(changed)} 行")
        for old, new in changed:
            print(f"      {old.strip()}   ->   {new.strip()}")
        if not args.dry_run:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak_pct"))
            path.write_bytes(newline.join(out_lines).encode("utf-8"))
    print(f"\n語料：{'（dry-run）' if args.dry_run else ''}共 {total} 行、{len(files)} 個檔")

    if args.skip_gold:
        print("--skip-gold：未修補 gold（⚠ 若 gold 引用了小數字面值，context_recall 會假性下降）")
    else:
        print("\ngold 同步（見檔頭：只改寫法不改數值）")
        n = patch_gold(dec_map, args.dry_run)
        print(f"gold：{'（dry-run）' if args.dry_run else ''}共 {n} 題")

    if not args.dry_run:
        print("\n備份在 *.bak_pct。接著重新 ingest 這批 .txt：")
        print("  python data_update_edgar.py --skip-filings --collection us_stock_rag_edgar_period")


if __name__ == "__main__":
    main()
