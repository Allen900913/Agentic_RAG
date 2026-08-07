"""
experiments/phase1_edgartools_extract.py — Phase 1: 解析器替換與邊界確立(唯讀實驗)

目標:
  1. 用 edgartools 把財報三表(Income/Balance/CashFlow)各自轉成 Markdown,驗證格式不跑版、
     維持「原子 chunk」(不切斷)。
  2. 用 edgartools 的 Item 邊界(tenk.items / tenk['Item 1A'] 等)取出散文,驗證 Item 之間
     沒有互相沾染(例如 Item 1A 尾端沒有混進 Item 1B 開頭的文字,反之亦然)。

不寫入任何檔案、不動 Qdrant、不影響現有 pipeline —— 純粹印出結果供人工檢查。
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

for st in (sys.stdout, sys.stderr):
    if hasattr(st, "reconfigure"):
        st.reconfigure(encoding="utf-8", errors="replace")

from edgar import Company, set_identity

TICKER = os.environ.get("PHASE1_TICKER", "AAPL")


def get_latest_10k(ticker: str):
    set_identity(os.getenv("SEC_IDENTITY"))
    company = Company(ticker)
    filings = company.get_filings(form="10-K", amendments=False).latest(1)
    filing = filings if not hasattr(filings, "__iter__") else list(filings)[0]
    return filing.obj()


def check_three_statements(tenk):
    print("=" * 80)
    print("[STEP 1] 財報三表 → Markdown(應為單一原子 chunk,不切分)")
    print("=" * 80)
    statements = {
        "income_statement": tenk.income_statement,
        "balance_sheet": tenk.balance_sheet,
        "cash_flow_statement": tenk.cash_flow_statement,
    }
    results = {}
    for name, stmt in statements.items():
        md = stmt.to_markdown()
        ctx = stmt.to_context()
        results[name] = {"markdown": md, "context": ctx}
        print(f"\n--- {name} ---")
        print(f"markdown 長度: {len(md)} 字元 (~{len(md)//4} tok)")
        print(f"context  長度: {len(ctx)} 字元")
        # 跑版檢查:markdown table 每一列的 '|' 數量應該一致
        lines = [l for l in md.splitlines() if l.strip().startswith("|")]
        pipe_counts = {l.count("|") for l in lines}
        ok = len(pipe_counts) <= 1
        print(f"跑版檢查(每列 '|' 數量應一致): {'PASS' if ok else 'FAIL — pipe_counts=' + str(pipe_counts)}")
        print("--- markdown 前 400 字元預覽 ---")
        print(md[:400])
        print("--- context 全文預覽 ---")
        print(ctx)
    return results


def check_item_boundaries(tenk):
    print("\n" + "=" * 80)
    print("[STEP 2] Item 邊界萃取 → 驗證 Item 之間沒有沾染")
    print("=" * 80)
    items = tenk.items
    print(f"\n共 {len(items)} 個 Item: {items}")

    item_texts = {}
    for item_name in items:
        try:
            text = tenk[item_name]
        except Exception as e:
            print(f"  [WARN] 取 {item_name} 失敗: {e!r}")
            continue
        item_texts[item_name] = text
        print(f"  {item_name:<12} {len(text):>7} 字元")

    # 邊界沾染檢查:取 Item 1A 尾端 200 字元、Item 1B(或下一個 item)開頭 200 字元,
    # 人工比對有無重疊/重複句子。用「連續 8 字元以上重疊」當簡易訊號。
    ordered = list(item_texts.keys())
    print("\n--- 邊界沾染抽查(相鄰 Item 交界處) ---")
    for i in range(len(ordered) - 1):
        cur, nxt = ordered[i], ordered[i + 1]
        cur_tail = item_texts[cur][-200:].strip()
        nxt_head = item_texts[nxt][:200].strip()
        print(f"\n[{cur}] 尾端 200 字元:\n...{cur_tail}")
        print(f"[{nxt}] 開頭 200 字元:\n{nxt_head}...")

    return item_texts


if __name__ == "__main__":
    print(f"[INFO] Ticker = {TICKER}")
    tenk = get_latest_10k(TICKER)
    print(f"[INFO] Filing: {tenk}")

    check_three_statements(tenk)
    check_item_boundaries(tenk)

    print("\n[DONE] Phase 1 萃取完成。請人工檢查上方輸出:")
    print("  1. 三表 markdown 是否跑版(pipe_counts 是否 PASS)")
    print("  2. Item 交界處是否有句子重複/截斷/混入下一段標題文字")
