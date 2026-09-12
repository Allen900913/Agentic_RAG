"""驗收 **chunk 層**的幅度接地（零 LLM、零網路，只讀 Qdrant ＋ reranker tokenizer）。

## 為什麼 `verify_segment_split.py` 判準⑤ 不夠

判準⑤ 量的是 **section 層**——它刻意不跑 SemanticChunker（那需要 embedding），才能在別的
實驗跑的時候安全執行。代價是它**看不到 SemanticChunker 之後的邊界**。

2026-08-12 這件事就是這個盲點造成的：`_merge_unquantified_sections` 把 AAPL
`Segment Operating Performance` 併成 2251 字元的 section（判準⑤ 因此回報 46→0、全綠），
但 SemanticChunker 隨後把它切成 1030 + 593——幅度表格留在前半、
`Greater China net sales increased …` 落在後半。**判準⑤ 全綠，缺陷卻還在**：
實測 `us_stock_rag_edgar_ground` 沒有任何一個 chunk 同時含那句話與 `18,816`，
mix-09 三個 run 只有 1 個 PASS（還輸給完全沒有小標層的 `period` 的 2/3）。

→ **「規則生效」與「缺陷消失」是兩個判準，前者過關不代表後者。** 這支腳本量後者。

## 三態，不要把「規則正確地不作用」讀成失敗

| 桶 | 意思 | 算不算 FAIL |
|---|---|---|
| `groundable_not_grounded` | 前一個 chunk（同一硬邊界內）帶幅度數字，**併得起來卻沒併** | **是，唯一的閘門** |
| `unreachable` | 前一個 chunk 也沒有幅度數字 → 沒有母節可接 | 否（硬併只會黏不相關內容）|
| `blocked_by_cap` | 併起來會超過 `RCTS_THRESHOLD` token → 會被 RCTS 再切開，併了白做 | 否（`_merge_small_chunks` 刻意不併）|

把閘門寫成「宇宙中沒有孤兒 chunk」會讓後兩桶被讀成失敗——同 memory
`subheading-split-detaches-magnitudes` 與 `number-defect-metric-needs-claim-anchoring`。

用法：
    .venv/Scripts/python.exe eval/verify_chunk_grounding.py -c us_stock_rag_edgar_ground
    .venv/Scripts/python.exe eval/verify_chunk_grounding.py -c A -c B   # 併排比較新舊
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

from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 與 data_update_edgar 同一組判準；**刻意就地重寫而不 import 該模組**——import 它會拉進
# unstructured/embedding 相依（實測超過 120s），而本腳本的價值就在於便宜到可以隨手跑。
# ⚠ 兩邊漂移就會量錯：改 data_update_edgar 的這三個常數時要同步這裡（下方有一致性斷言）。
MDNA_ITEMS = {"Item 7", "Part I, Item 2"}
CHANGE_STMT_RE = re.compile(r"\b(increased|decreased|grew|declined|rose|fell)\b", re.I)
MAGNITUDE_RE = re.compile(r"\d+(?:\.\d+)?\s*%|\$\s*[\d,]+|\b\d{1,3},\d{3}\b")
ORPHAN_MAX_CHARS = 1200
RCTS_THRESHOLD = 1200

_PREFIX = re.compile(r"^(?:\[[^\]]{0,120}\]\s*)+")


def _assert_in_sync() -> None:
    """把上面幾個常數與 `data_update_edgar.py` 的**原始碼文字**比對，不一致就中止。

    為什麼用讀文字而不 import：import 會拉進 unstructured/embedding（>120s），本腳本
    「便宜到可以隨手跑」就沒了。為什麼要比對：常數在兩個檔各一份，**漂移的話這支腳本會
    安靜地量錯東西並回報全綠**——那比沒有閘門更糟（同 CLAUDE.md「稽核腳本回傳 0 筆問題
    先當壞消息查」）。
    """
    src = (Path(__file__).resolve().parent.parent / "data_update_edgar.py").read_text(encoding="utf-8")

    def grab(pat: str, what: str) -> str:
        # re.S 是必要的：`_CHANGE_STMT_RE = re.compile(\n    r"…")` 定義跨兩行
        mo = re.search(pat, src, re.M | re.S)
        if not mo:
            raise SystemExit(f"[SYNC] 在 data_update_edgar.py 找不到 {what}——"
                             f"它可能被改名或刪了，本腳本的判準已不可信，請先對號。")
        return mo.group(1)

    checks = [
        ("_CHANGE_STMT_RE", grab(r'^_CHANGE_STMT_RE = re\.compile\(\s*r"(.+?)"', "_CHANGE_STMT_RE"),
         CHANGE_STMT_RE.pattern),
        ("_MAGNITUDE_RE", grab(r'^_MAGNITUDE_RE = re\.compile\(\s*r"(.+?)"\)', "_MAGNITUDE_RE"),
         MAGNITUDE_RE.pattern),
        ("_ORPHAN_MAX_CHARS", grab(r"^_ORPHAN_MAX_CHARS = (\d+)", "_ORPHAN_MAX_CHARS"),
         str(ORPHAN_MAX_CHARS)),
        ("RCTS_THRESHOLD", grab(r"^RCTS_THRESHOLD\s*=\s*(\d+)", "RCTS_THRESHOLD"),
         str(RCTS_THRESHOLD)),
        ("_MDNA_ITEMS", grab(r"^_MDNA_ITEMS = \{(.+?)\}", "_MDNA_ITEMS"),
         ", ".join(f'"{x}"' for x in ["Item 7", "Part I, Item 2"])),
    ]
    bad = [(n, a, b) for n, a, b in checks if a != b]
    if bad:
        lines = "\n".join(f"    {n}: ingest={a!r}  本腳本={b!r}" for n, a, b in bad)
        raise SystemExit(f"[SYNC] 判準與 data_update_edgar.py 不一致，量出來的東西不可信：\n{lines}")


def _norm_item(raw: str) -> str:
    """payload 的 item_id 用底線（`Part_I_Item_2`），還原成 MDNA_ITEMS 的寫法。"""
    return (raw or "").replace("_", " ").replace("Part I Item 2", "Part I, Item 2")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--collection", action="append", required=True)
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()

    _assert_in_sync()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    from qdrant_client import QdrantClient, models
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"))
    ntok = lambda t: len(tok.encode(t, add_special_tokens=False))
    client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"),
                          api_key=os.getenv("QDRANT_API_KEY"), timeout=60)
    flt = models.Filter(must=[models.FieldCondition(
        key="chunk_type", match=models.MatchValue(value="text"))])

    failed = False
    for coll in args.collection:
        # 逐 source 收集，之後按 chunk_index 排序才能問「前一個 chunk 是誰」
        by_src: dict[str, list[dict]] = defaultdict(list)
        offset = None
        while True:
            pts, offset = client.scroll(coll, limit=1024, with_payload=True,
                                        scroll_filter=flt, offset=offset)
            for p in pts:
                pl = p.payload or {}
                if _norm_item(pl.get("item_id")) not in MDNA_ITEMS:
                    continue
                by_src[pl.get("source") or "?"].append({
                    "idx": pl.get("chunk_index"), "ticker": pl.get("ticker") or "?",
                    "period": pl.get("period_context"), "head": pl.get("heading_context"),
                    "text": _PREFIX.sub("", pl.get("document") or "")})
            if offset is None:
                break

        n_change = 0
        buckets: Counter[str] = Counter()
        by_ticker: Counter[str] = Counter()
        examples: list[str] = []
        for src, rows in by_src.items():
            rows.sort(key=lambda r: (r["idx"] if r["idx"] is not None else -1))
            for i, r in enumerate(rows):
                t = r["text"]
                if not CHANGE_STMT_RE.search(t):
                    continue
                n_change += 1
                if len(t) > ORPHAN_MAX_CHARS or MAGNITUDE_RE.search(t):
                    continue                        # 自足，不是孤兒
                prev = rows[i - 1] if i > 0 else None
                # 前一個必須在**同一個硬邊界內**（同期間、同小標），否則本來就不該併
                same_bd = (prev is not None and prev["period"] == r["period"]
                           and prev["head"] == r["head"])
                if not same_bd or not MAGNITUDE_RE.search(prev["text"]):
                    buckets["unreachable"] += 1
                elif ntok(prev["text"] + "\n\n" + t) > RCTS_THRESHOLD:
                    buckets["blocked_by_cap"] += 1
                else:
                    buckets["groundable_not_grounded"] += 1
                    by_ticker[r["ticker"]] += 1
                    if len(examples) < args.show:
                        examples.append(f"    [!!] {src}#{r['idx']} ({len(t)} 字元, "
                                        f"head={r['head']!r}) 前一個 #{prev['idx']} 帶幅度數字，"
                                        f"併起來 {ntok(prev['text'] + t)} token <= {RCTS_THRESHOLD}")

        n_orph = sum(buckets.values())
        print("=" * 92)
        print(f"{coll}：MD&A 含漲跌陳述的 text chunk {n_change} 個，其中無幅度孤兒 "
              f"{n_orph}（{n_orph / n_change * 100 if n_change else 0:.1f}%）")
        for k in ("groundable_not_grounded", "unreachable", "blocked_by_cap"):
            mark = "  ← 唯一的閘門" if k == "groundable_not_grounded" else ""
            print(f"  {k:26s} {buckets[k]:4d}{mark}")
        if by_ticker:
            print(f"  閘門項分公司: {dict(sorted(by_ticker.items(), key=lambda x: -x[1]))}")
        for e in examples:
            print(e)
        if buckets["groundable_not_grounded"]:
            failed = True

    print()
    if failed:
        print("[FAIL] 有「併得起來卻沒併」的 chunk ＝ 幅度接地在 chunk 層沒生效。"
              "檢查 `_merge_small_chunks` 是否收到 token_len_fn/max_tokens，"
              "以及 `_is_orphan_explainer` 的上界是否卡到這些段。")
        raise SystemExit(1)
    print("[OK] 所有無幅度孤兒 chunk 都是「無母節可接」或「併了會被 RCTS 再切開」＝ "
          "規則正確地不作用，不是漏接。")


if __name__ == "__main__":
    main()
