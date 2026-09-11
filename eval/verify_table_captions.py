"""驗收 ingest 的表格 caption 品質（零 LLM、零網路，只讀 Qdrant）。

**為什麼需要這支**：表格摘要那條路（`unstructured_components._llm_summarize_table`）曾經
**整條等於沒有產出，而且完全靜默**——2026-08-11 查 `us_stock_rag_edgar_head` 才發現
該有 LLM 摘要的 table chunk 大多被截斷成 `This table details`／`This table presents`／
`This table`（Gemini 2.5 的 reasoning token 吃掉了 `max_output_tokens`），另有一批 caption
是**純編號**——因為舊的 `_is_meaningful_caption` 只要求「有 alnum」，頁碼與註釋編號會通過，
於是 `_llm_summarize_table` **根本沒被呼叫**，最需要摘要的那些表反而拿到最沒用的 caption。

⚠ **本腳本首跑就把先前的記錄修正了一個數量級**：舊紀錄寫「4 個 chunk 的 caption 是頁碼」，
實際全庫掃過是 **95 個**（`head` 與 `exp4` 同數，兩者都早於 2026-08-11 的 caption 修法），
大多是 GOOGL 10-K 的註釋編號（`19.`／`35.`／`39.`／`85.`）而不是頁碼。先前那個 4 是窄範圍
探針的結果被當成全庫數——**這正是「只存結論不存中間產物」的代價**（同 memory
`eval-measurement-pitfalls`）。所以這支腳本掃全庫、不取樣。

這種缺陷不會讓任何東西報錯，也不會讓 RAGAS 明顯掉分（影響題數在 MDE 之下），
**只能用確定性的分類統計抓**。

## 兩個真正的閘門（其餘欄位只是分佈，不判成敗）

| 判準 | 為什麼是硬缺陷 |
|---|---|
| `numeric_caption` = 0 | 頁碼／純數字**永遠不是** caption，而且它會搶掉 LLM 的機會 |
| `missing_on_big` = 0 | 列數 >= `TABLE_SUMMARY_MIN_ROWS` 卻沒 caption ＝ LLM 那步失敗了。**這是 Groq 429 退避用盡後 `return ""` 的靜默降級出口**，重建當下最可能踩的坑 |

`stub_caption`（`This table…` 之類的截斷殘骸）也印出來並計入失敗——它是舊病的指紋，
換成 Groq + `max_completion_tokens=200` 之後不該再出現。

⚠ **`no_caption` 本身不是缺陷**：列數 < `TABLE_SUMMARY_MIN_ROWS` 的小表（封面 checkbox、
IRS 編號）不值得花 LLM call，本來就不該有 caption。把「宇宙中沒有無 caption 的表」當
閘門會讓規則**正確地不作用**被讀成失敗——同 `eval/verify_segment_split.py` 判準⑤ 與
memory `subheading-split-detaches-magnitudes` 的教訓。

⚠ **`footer_caption` 也不當閘門，但要盯著**：`_find_caption` 會撿到頁尾
（`Apple Inc. | 2025 Form 10-K | 54`）當 caption。它跟純編號同一類垃圾，只是有字母所以
`_is_meaningful_caption` 放行。**2026-08-11 全庫實測 4/165，且全部集中在 AAPL 10-K 的
Exhibit Index 區**（附錄清單，對任何查詢都沒貢獻）→ 修它零收益，列為已接受的極限。
但數字印出來當基準：**超過 4 或出現在 `notes_table` 以外的 item 就要回頭查**，因為同一個
機制若撿到真財務表格上就是實害（embedding 吃到頁碼而不是描述）。

用法：
    .venv/Scripts/python.exe eval/verify_table_captions.py --collection us_stock_rag_edgar_ground
    .venv/Scripts/python.exe eval/verify_table_captions.py -c A -c B   # 併排比較新舊
"""
from __future__ import annotations

import argparse
import os
import re
import sys
# ⚠ Windows 主控台預設 cp950，而本檔的報表帶著 ⚠／✔／① 等字元 ⇒ **印到一半就 crash**，
#   而 crash 的退出碼與「有 FAIL」外觀相同 ＝ 把量尺自己的失敗讀成系統的失敗。
#   2026-09-11 普查：eval/ 的 51 支裡有 27 支帶著這個地雷，其中兩支當天真的踩了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# chunk 文字開頭的 `[期間] `／`[section: 小標] ` 前綴（見 data_update_edgar._period_prefix）
_PREFIX = re.compile(r"^(?:\[[^\]]{0,120}\]\s*)+")
# 表格列：整行只有 -|: 空白，或以 | 開頭
_SEP_ROW = re.compile(r"^[\s|:\-]*$")
# 舊 Gemini 截斷殘骸的指紋（`This table` / `This table details` / `This table presents`）
_STUB = re.compile(r"^this table(\s+(details|presents|shows|summarizes))?\s*$", re.I)
# 頁尾被當成 caption 的指紋：`Apple Inc. | 2025 Form 10-K | 54`
_FOOTER = re.compile(r"Form\s+10-[KQ]\s*\|\s*\d+\s*$")


def _looks_like_table_row(line: str) -> bool:
    """⚠ 判準刻意**只認「以 | 開頭」**，不認「含兩個以上 |」。

    後者是第一版寫法，會把**頁尾誤判成表格列**——這個語料的頁尾長成
    `Apple Inc. | 2025 Form 10-K | 54`（用 ` | ` 當分隔），含兩個 `|` 但不是表格列。
    後果是那 4 個 chunk 被歸進 `no_caption` → 又因為列數夠大被算進 `missing_on_big`
    → 閘門吐出「最可能是 Groq 429 靜默降級」這個**完全錯誤的病因**。
    `html_table_to_markdown` 產出的表格列一律以 `|` 開頭,所以收緊是安全的。
    """
    t = line.strip()
    if not t or _SEP_ROW.match(t):
        return True
    return t.startswith("|")


def split_caption(text: str) -> tuple[str, str]:
    """把 chunk 文字拆成 (caption, 表格本體)。沒有 caption 時回 ("", 全文)。

    ingest 端組法是 `f"{caption}\\n\\n{body}"`（`build_chunk_records`），所以用第一個
    空行切;但**不能只看有沒有空行**——沒有 caption 的表格本體自己也可能含空行,
    所以還要確認第一段不是表格列。
    """
    body_all = _PREFIX.sub("", text or "")
    head, sep, rest = body_all.partition("\n\n")
    if not sep:
        return "", body_all
    if any(_looks_like_table_row(l) for l in head.splitlines()):
        return "", body_all
    return head.strip(), rest


def classify(caption: str) -> str:
    if not caption:
        return "no_caption"
    if sum(1 for ch in caption if ch.isalpha()) < 3:
        return "numeric_caption"      # 頁碼、純數字 → 硬缺陷
    if _STUB.match(caption.strip().rstrip(".:")):
        return "stub_caption"         # 舊截斷殘骸 → 硬缺陷
    if _FOOTER.search(caption):
        return "footer_caption"       # 頁尾 → 沒用但**不當硬缺陷**，見 main() 的說明
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--collection", action="append", required=True,
                    help="要檢查的 collection（可重複，多個會併排比較）")
    ap.add_argument("--show", type=int, default=6, help="每類最多印幾個實例")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    from qdrant_client import QdrantClient, models
    from unstructured_components import TABLE_SUMMARY_MIN_ROWS, _count_table_rows

    client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"),
                          api_key=os.getenv("QDRANT_API_KEY"), timeout=60)
    flt = models.Filter(must=[models.FieldCondition(
        key="chunk_type", match=models.MatchValue(value="table"))])

    failed = False
    for coll in args.collection:
        rows: list[dict] = []
        offset = None
        while True:
            pts, offset = client.scroll(coll, limit=512, with_payload=True,
                                        scroll_filter=flt, offset=offset)
            for p in pts:
                pl = p.payload or {}
                cap, body = split_caption(pl.get("document") or "")
                rows.append({"source": pl.get("source"), "idx": pl.get("chunk_index"),
                             "cap": cap, "kind": classify(cap),
                             "nrows": _count_table_rows(body)})
            if offset is None:
                break

        tally = Counter(r["kind"] for r in rows)
        # 列數夠大卻沒 caption ＝ LLM 那步失敗（429 退避用盡 → return ""）
        missing_big = [r for r in rows if r["kind"] == "no_caption"
                       and r["nrows"] >= TABLE_SUMMARY_MIN_ROWS]
        caps = [len(r["cap"]) for r in rows if r["cap"]]

        print("=" * 92)
        print(f"{coll}：{len(rows)} 個 table chunk")
        for k in ("ok", "no_caption", "numeric_caption", "stub_caption", "footer_caption"):
            print(f"  {k:18s} {tally[k]:5d}"
                  + ("   （頁尾當 caption；非閘門，基準 4，見下）" if k == "footer_caption" else ""))
        print(f"  {'missing_on_big':18s} {len(missing_big):5d}   "
              f"（no_caption 且列數 >= {TABLE_SUMMARY_MIN_ROWS}）")
        if caps:
            caps.sort()
            print(f"  caption 長度: 中位 {caps[len(caps)//2]} / 最短 {caps[0]} / 最長 {caps[-1]} 字元")

        for kind in ("numeric_caption", "stub_caption", "footer_caption"):
            for r in [x for x in rows if x["kind"] == kind][:args.show]:
                print(f"    [{kind}] {r['source']}#{r['idx']}  caption={r['cap']!r}")
        for r in missing_big[:args.show]:
            print(f"    [missing_on_big] {r['source']}#{r['idx']}  {r['nrows']} 列、無 caption")
        for r in [x for x in rows if x["kind"] == "ok"][:args.show]:
            print(f"    [ok] {r['source']}#{r['idx']}  {r['cap'][:88]!r}")

        bad = tally["numeric_caption"] + tally["stub_caption"] + len(missing_big)
        print(f"  → 硬缺陷合計 {bad}"
              f"（numeric {tally['numeric_caption']} + stub {tally['stub_caption']}"
              f" + missing_on_big {len(missing_big)}）")
        if bad:
            failed = True

    print()
    if failed:
        print("[FAIL] 有硬缺陷。numeric/stub ＝ caption 路徑選錯來源；missing_on_big ＝ "
              "LLM 摘要那步失敗（最可能是 Groq 429 退避用盡後靜默 return \"\"），"
              "**重跑 ingest 的表格階段，不要接受這個 collection**。")
        raise SystemExit(1)
    print("[OK] 無 numeric／stub caption，且列數夠大的表格都有 caption。")


if __name__ == "__main__":
    main()
