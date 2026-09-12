"""驗收跨期 field collapsing 的行為（零 LLM、零網路、零 Qdrant，毫秒級）。

**為什麼需要這支**：[`rag_query._collapse_cross_period_sections`](../rag_query.py) 是一個機制
宣稱（「同一節只讓一份 filing 佔位，同一份 filing 的多個 chunk 全留」），照 CLAUDE.md
「機制宣稱要先有能證偽它的確定性測試」必須先有這個。

⚠ **這支測的是規則本身，不是它有沒有用。**
`_collapse_cross_period_sections` 的規則與探針 `crowding_seats` 的定義**是同一個** → collapse
一開，排擠席位必然歸零。那是套套邏輯，不是收益證據。收益與損害要另外量：
  · 收益：`eval/probe_temporal_interference.py` 的 `gold@k`（讓出來的席位換到了什麼）
  · 損害：`eval/probe_temporal_interference.py --trend`（本來就需要多期的題掉了幾個期別）

**兩條最容易寫錯、也最容易在重構時被破壞的界線**（各有專屬斷言）：
  ① **同一份 filing 的同一節有多個 chunk 時全部保留**——`item_chunk_index` 0/1/2 是互補內容
     不是重複。寫成「同組只留一個 chunk」會把單一份 filing 的長章節砍成只剩開頭。
  ② **別份 filing 插隊之後，原本那份的後續 chunk 仍要留**（測項④）。用「看到第 N 個就停」
     這種寫法會在這裡出錯。

用法：
    .venv/Scripts/python.exe eval/verify_cross_period_collapse.py
"""
from __future__ import annotations

import sys
# ⚠ Windows 主控台預設 cp950，而本檔的報表帶著 ⚠／✔／① 等字元 ⇒ **印到一半就 crash**，
#   而 crash 的退出碼與「有 FAIL」外觀相同 ＝ 把量尺自己的失敗讀成系統的失敗。
#   2026-09-11 普查：eval/ 的 51 支裡有 27 支帶著這個地雷，其中兩支當天真的踩了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq  # noqa: E402


def _c(src, ci, item, ft="10-K", tk="MSFT", rank=None):
    """一個 filing chunk（只帶 collapse 會看的欄位）。`rank` ＝ `(fiscal_year, 期別序)`。"""
    return {"source": src, "chunk_index": ci, "item_id": item, "filing_type": ft,
            "ticker": tk, "fiscal_rank": rank, "raw_rerank_score": 0.0}


def _n(src, ci):
    """News／Fundamentals：沒有 item_id，不該進任何組。"""
    return {"source": src, "chunk_index": ci, "item_id": "", "filing_type": "", "ticker": "MSFT"}


def main() -> int:
    col = rq._collapse_cross_period_sections
    passed = failed = 0

    def ck(name, got, want):
        nonlocal passed, failed
        if got == want:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}\n         got ={got}\n         want={want}")

    # ── 界線①：同一份 filing 的同節多 chunk 是互補內容，全留 ──────────────
    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"), _c("MSFT_10K_2026.html", 2, "Item_1"),
                _c("MSFT_10K_2026.html", 3, "Item_1")])
    ck("同檔同節多 chunk 全留", (len(r), d), (3, 0))

    # ── 核心行為：同節跨檔只留 rerank 最高那份 ────────────────────────────
    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"), _c("MSFT_10K_2025.html", 9, "Item_1"),
                _c("MSFT_10K_2024.html", 7, "Item_1")])
    ck("同節跨檔只留 rerank 最高那份", ([c["source"] for c in r], d),
       (["MSFT_10K_2026.html"], 2))

    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"), _c("MSFT_10K_2025.html", 9, "Item_1"),
                _c("MSFT_10K_2024.html", 7, "Item_1")], keep_per_group=2)
    ck("keep_per_group=2 留兩份", (len(r), d), (2, 1))

    # ── 界線②：別份 filing 插隊後，原本那份的後續 chunk 仍要留 ─────────────
    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"), _c("MSFT_10K_2025.html", 9, "Item_1"),
                _c("MSFT_10K_2026.html", 2, "Item_1")])
    ck("同檔多 chunk 不因別檔插隊而被砍",
       ([f'{c["source"]}#{c["chunk_index"]}' for c in r], d),
       (["MSFT_10K_2026.html#1", "MSFT_10K_2026.html#2"], 1))

    # ── 分組鍵的三個維度各自要能分開組 ────────────────────────────────────
    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"), _c("MSFT_10K_2025.html", 9, "Item_7")])
    ck("不同 item_id 互不影響", (len(r), d), (2, 0))

    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"),
                _c("AAPL_10K_2025.html", 1, "Item_1", tk="AAPL")])
    ck("不同 ticker 不同組", (len(r), d), (2, 0))

    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"),
                _c("MSFT_10Q_202603.html", 1, "Item_1", ft="10-Q")])
    ck("10-K 與 10-Q 不同組", (len(r), d), (2, 0))

    # ── News／Fundamentals 完全不受影響（它們沒有跨期競爭的概念）───────────
    r, d = col([_n("MSFT_News_a.txt", 0), _n("MSFT_News_b.txt", 0), _n("MSFT_Fundamentals.txt", 0)])
    ck("無 item_id 的一律放行", (len(r), d), (3, 0))

    # ── 順序必須保持（下游 `enriched[:top_k]` 依賴 rerank 降序）────────────
    r, _ = col([_c("A_10K_2026.html", 1, "Item_1", tk="A"), _n("x.txt", 0),
                _c("A_10K_2025.html", 1, "Item_1", tk="A"),
                _c("A_10K_2026.html", 2, "Item_1", tk="A")])
    ck("順序保持、非組員留在原位",
       [f'{c["source"]}#{c["chunk_index"]}' for c in r],
       ["A_10K_2026.html#1", "x.txt#0", "A_10K_2026.html#2"])

    # ── 逃生口：keep<1 視為不作用（而不是全砍）────────────────────────────
    r, d = col([_c("MSFT_10K_2026.html", 1, "Item_1"), _c("MSFT_10K_2025.html", 9, "Item_1")],
               keep_per_group=0)
    ck("keep_per_group<1 不作用", (len(r), d), (2, 0))

    # ── 空輸入 ────────────────────────────────────────────────────────────
    ck("空輸入", col([]), ([], 0))

    # ══════════════════════════════════════════════════════════════════════
    # prefer="newest"：組內留**財年序最新**的，而不是 rerank 最高的。
    # 這一維是實測逼出來的——`prefer="rank"` 在 sem-03／sem-04 上把 gold
    # （`MSFT_10K_2026`）整個刪掉，因為它在組內不是 rerank 最高的那一份。
    # ══════════════════════════════════════════════════════════════════════
    FY26, FY25, FY24 = (2026, 5), (2025, 5), (2024, 5)

    # rerank 順序是 2024 → 2026，但 2026 才是新的
    seq = [_c("MSFT_10K_2024.html", 7, "Item_1", rank=FY24),
           _c("MSFT_10K_2026.html", 1, "Item_1", rank=FY26),
           _c("MSFT_10K_2025.html", 9, "Item_1", rank=FY25)]
    r, d = col(seq, prefer="rank")
    ck("prefer=rank 留 rerank 最高（此例是最舊的 2024）",
       ([c["source"] for c in r], d), (["MSFT_10K_2024.html"], 2))
    r, d = col(seq, prefer="newest")
    ck("prefer=newest 留財年最新（2026），與 rerank 名次無關",
       ([c["source"] for c in r], d), (["MSFT_10K_2026.html"], 2))

    # newest 也必須保留同檔的多 chunk
    r, d = col([_c("MSFT_10K_2024.html", 7, "Item_1", rank=FY24),
                _c("MSFT_10K_2026.html", 1, "Item_1", rank=FY26),
                _c("MSFT_10K_2026.html", 2, "Item_1", rank=FY26)], prefer="newest")
    ck("newest：贏家那份的多 chunk 全留",
       ([f'{c["source"]}#{c["chunk_index"]}' for c in r], d),
       (["MSFT_10K_2026.html#1", "MSFT_10K_2026.html#2"], 1))

    # newest 的輸出仍須維持原順序（下游依賴 rerank 降序）
    r, _ = col([_c("A_10K_2024.html", 1, "Item_1", tk="A", rank=FY24),
                _c("A_10K_2026.html", 5, "Item_1", tk="A", rank=FY26),
                _n("x.txt", 0),
                _c("A_10K_2026.html", 6, "Item_1", tk="A", rank=FY26)], prefer="newest")
    ck("newest：輸出仍是原順序",
       [f'{c["source"]}#{c["chunk_index"]}' for c in r],
       ["A_10K_2026.html#5", "x.txt#0", "A_10K_2026.html#6"])

    # keep=2 + newest：留最新的兩份
    r, d = col(seq, keep_per_group=2, prefer="newest")
    ck("newest keep=2 留最新的兩份",
       (sorted({c["source"] for c in r}), d),
       (["MSFT_10K_2025.html", "MSFT_10K_2026.html"], 1))

    # fiscal_rank 缺漏（None）→ 排在有 rank 的後面，但不會爆
    r, d = col([_c("MSFT_10K_X.html", 1, "Item_1", rank=None),
                _c("MSFT_10K_2026.html", 9, "Item_1", rank=FY26)], prefer="newest")
    ck("newest：fiscal_rank 缺漏者讓位給有 rank 的",
       ([c["source"] for c in r], d), (["MSFT_10K_2026.html"], 1))

    print(f"\nPASS {passed}  FAIL {failed}")
    print("GATE: " + ("PASS" if failed == 0 else "FAIL"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
