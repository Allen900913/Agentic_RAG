"""probe_gold_funnel.py — **撈到了 → 有沒有用**（2026-09-09）。零 LLM、零檢索。

## 為什麼需要這一支

2026-09-08 量到「32% 的可量題有檢索層缺陷」（`probe_chunk_gold_recall`），
但同一天的交叉分析顯示 `forced_pass` 那 10 題**只有 1 題**是檢索問題。
於是 `BACKLOG.md` 記下一句：「**有缺陷 ≠ 修了有用**，而第二個問題沒有量尺。」

這支補那一格的**前半**：在同一次 agentic 跑分裡，把 gold 分成兩段看——

```
gold ∈ retrieved_union（檢索真的撈到過）
    →  gold ∈ sources（撐過 Grader 圈選＋COMMIT_TOP_K，進了 Generator 手上）
    →  gold ∈ cited（答案真的引用了它）
```

## ⚠ 第一段 2026-09-09 才量得到，在那之前它結構性恆為 0

結果檔的 `sources` 走的是這條鏈的**最末端**：

```
rq.retrieve() → run_state.pool → Grader 的 relevant_ids 圈選 → [:COMMIT_TOP_K]
    → picked → _merge_chunks → collected → 結果檔的 sources
```

實測 65 題 `sources` 的**中位數只有 3 顆**，而 mh-03 單題撈到 78 顆、只有 6 顆進 `collected`
（**92% 被丟掉**）。所以只有 `sources` 可讀的時候，「撈到了卻沒引用」這一格是
**結構性接近恆為 0** 的——2026-09-09 兩輪實跑都是 0，照本 repo 的規則
（**稽核回報 0 筆先當壞消息查**）追下去才發現那是量尺的性質，不是系統健康。

⚠ **這條鏈我歸因錯過兩次，兩次都寫在這裡當紀錄**：先說 `sources` 是「跨子問題的聯集」
  （錯），再說是「`_fair_select` 之後那一把」（也錯——`_fair_select` 的產出是
  `writer_chunks`，`run_agentic` 根本不回傳它）。現在這條鏈是逐行讀出來的。
  同一個形狀在 `probe_chunk_gold_recall` 的 `pool` 欄位上也發生過。

`agentic_rag_version` 於 2026-09-09 加了 **`retrieved_union`** 觀測通道（Grader 圈選
**之前**的候選池 key，逐子問題標記，閘門㉒）。**在那之前產出的結果檔沒有這一格**，
本檔對那些檔案只跑得動兩段版本，並在報表裡**分群印**——⚠ 兩群的數字不可合併，
少了第一段的那一群，`retrieved_unused` 的意義與有第一段時完全不同。

## 四條紀律

⚠ **引用是 LLM 自己宣稱的**，不是 ground truth。它會 ① 引了但其實沒用 ② 用了但漏引。
  所以 `gold@cited` 是**上界**（高估「被使用」）。**低分是硬證據，高分不是健康證明。**
⚠ **`sources` ＝ Grader 圈選＋`COMMIT_TOP_K` 截斷之後的 `collected`（實測中位數 3 顆）**，
  不是 top-k 切片。所以本檔的數字**不可以**拿去跟 `probe_chunk_gold_recall` 的
  `gold@5`／`gold@20` 並排**當成同一條漏斗的兩段**。
  **能做的是比對「哪幾題判定不同」**——那是子問題拆解的效果，不是量尺誤差。
⚠ **`retrieved_union` 也不等於 `probe_chunk_gold_recall` 的 `@20`**：前者是**跨子問題的
  聯集**（實測單題可達 78 顆），後者是**單一 query 的候選池**。並排比較會把
  「拆解出更多 query」誤讀成「檢索變好了」。
⚠ **gold 是聯集、偏大**（`chunk_gold.json` 的消費語意）→ 兩段都只會**高估**。
⚠ **引用解析一律用生產的 `_extract_citations`**，不自己寫一份 regex：
  自己寫一份 ＝ 量尺量的是我寫的那個解析器，不是系統實際認得的引用格式
  （CLAUDE.md〈量尺不可與被測物耦合〉的第二種形狀）。

用法：
    .venv/Scripts/python.exe eval/probe_gold_funnel.py \\
        experiments/agentic/gj_65q_denominators_20260908.json

    .venv/Scripts/python.exe eval/probe_gold_funnel.py --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


# 生產的 `_extract_citations` 對「一個中括號裡用 `;` 串多筆」的形狀**整段抓不到**
# （`_CITE_RE` 要求數字後面緊接右括號）。2026-09-09 在 65 題結果檔上量到 363 個引用區段裡
# **5 個（1.4%）是這個形狀，而且抓到的是 0 筆不是部分**，分屬 4 題（sem-01／mix-10／mh-01／sem-13）。
# ⚠ 本檔的處理是**前置正規化**（把 `;` 拆成獨立區段再餵生產解析器），**不是自己寫一份解析器**。
#   理由：不正規化的話這支的頭條數字會被一個與被測物無關的解析缺陷系統性壓低。
# ⚠ 差額一律單獨印出來（`semicolon_recovered`），不可以吞掉——那是生產的缺陷，不是本檔的。
_BRACKET_RE = re.compile(r"[\[【][^\[\]【】]*[\]】]")


def _cited_keys(text: str, extract) -> tuple[set, int]:
    """答案文字 → 引用集合。回傳 (集合, 靠 `;` 正規化才多撈到的筆數)。"""
    base = {(fn, ci) for fn, ci in extract(text or "")}
    norm = _BRACKET_RE.sub(
        lambda m: " ".join(f"[{part.strip()}]" for part in m.group(0)[1:-1].split(";")),
        text or "")
    wide = {(fn, ci) for fn, ci in extract(norm)}
    return wide, len(wide - base)


def _key(source, chunk_index) -> tuple:
    """(source, chunk_index) 正規化成字串對——payload 是 int、`scan_docs` 是 str。"""
    return (str(source), str(chunk_index))


def classify(in_sources: bool, in_cited: bool) -> str:
    """兩段漏斗的分格。純函式，供 `--selftest` 餵誤報對照。

    ⚠ `cited` 但不在 `sources` **不是**「更好」，是**資料不一致**（引用了一個沒撈過的
      chunk）——那是 citation validator 該抓的病，本檔單獨標出來，不可以併進 `used`。
    """
    if in_cited and in_sources:
        return "used"
    if in_cited and not in_sources:
        return "cited_not_retrieved"
    if in_sources:
        return "retrieved_unused"
    return "not_retrieved"


def classify3(in_union: bool, in_sources: bool, in_cited: bool) -> str:
    """三段漏斗的分格（`retrieved_union` 可讀時才用）。純函式，供 `--selftest` 餵誤報對照。

    ⚠ **`dropped_by_grader` 是這一支存在的理由**：撈到了、卻沒撐過 Grader 圈選／
      `COMMIT_TOP_K` 截斷 → **修檢索對它完全無效，該修的是圈選或名額**。
      在 `retrieved_union` 之前這一格結構性量不到（見檔頭）。
    ⚠ **不可以把 `dropped_by_grader` 併進 `not_retrieved`**：兩者的修法在不同層，
      合併就等於把「圈選太兇」誤診成「檢索撈不到」——正是本 repo 一再犯的那種歸因錯。
    ⚠ 順序刻意先問 `in_cited`：引用了卻不在候選池是**資料不一致**（另一種病），
      要單獨標出來，不可以因為它同時也「不在 sources」就落進別格。
    """
    if in_cited and not in_union:
        return "cited_not_retrieved"
    if in_cited:
        return "used"
    if in_sources:
        return "retrieved_unused"
    if in_union:
        return "dropped_by_grader"
    return "not_retrieved"


def run(args) -> int:
    import rag_query as rq
    import chunk_gold as cg
    from agentic_rag_version.validators import _extract_citations

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    recs = {r["id"]: r for r in data.get("records", [])}
    gold_by_id = cg.by_id()
    client = rq.make_qdrant_client()
    docs = cg.scan_docs(client, rq.COLLECTION_NAME)
    print(f"[INFO] results={args.results}  題數={len(recs)}  "
          f"collection={rq.COLLECTION_NAME}")

    rows: dict = {}
    na: dict = defaultdict(list)
    for qid, rec in recs.items():
        entry = gold_by_id.get(qid)
        cat = rec.get("category", "?")
        if entry is None:
            na[cat].append(qid)
            continue
        golds = {_key(s, ci) for s, ci in cg.gold_chunks(docs, entry)}
        if not golds:
            na[cat].append(qid + "(gold未命中)")
            continue
        srcs = {_key(s.get("source"), s.get("chunk_index"))
                for s in (rec.get("sources") or []) if isinstance(s, dict)}
        raw, recovered = _cited_keys(rec.get("answer") or "", _extract_citations)
        cited = {_key(fn, ci) for fn, ci in raw}
        # ⚠ `None` ≠ `[]`（同 ⑲l10）：`None`／缺席 ＝ 這份結果檔早於 `retrieved_union`
        #   或這一題崩潰降級沒經過 graph；`[]` ＝ 經過了但一顆都沒撈到。
        #   合併兩者會讓舊結果檔整份被判成「什麼都沒撈到」＝ 分母灌水到毫無意義。
        ru = rec.get("retrieved_union")
        if ru is None:
            v = classify(bool(golds & srcs), bool(golds & cited))
            uni = None
        else:
            uni = {_key(d.get("source"), d.get("chunk_index"))
                   for d in ru if isinstance(d, dict)}
            v = classify3(bool(golds & uni), bool(golds & srcs), bool(golds & cited))
        rows[qid] = {"category": cat, "verdict": v, "n_gold": len(golds),
                     "n_sources": len(srcs), "n_cited": len(cited),
                     "n_union": None if uni is None else len(uni),
                     "stages": 2 if uni is None else 3,
                     # 結構不變量：`sources` 是 `retrieved_union` 的下游 ⇒ 必為子集。
                     # 破了就是資料錯（或這兩格來自不同次跑），要當場看得見。
                     "union_superset_ok": None if uni is None else srcs <= uni,
                     "semicolon_recovered": recovered}

    _report(rows, na)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(
            {"_meta": {"results": args.results, "collection": rq.COLLECTION_NAME,
                       "na": dict(na)}, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n[OK] 寫出 {args.output}")
    return 0


_LABEL = {
    "used":                "　 gold 撈到了、也被引用",
    "retrieved_unused":    "① **進了 Generator 手上卻沒引用** ← 生成層，修檢索對它無效",
    "dropped_by_grader":   "② **撈到了但被 Grader 圈選／COMMIT_TOP_K 丟掉** ← 修檢索也無效，"
                           "該修的是圈選或名額",
    "not_retrieved":       "③ 檢索根本沒撈到 ← 只有這一格修檢索才有用",
    "cited_not_retrieved": "⚠ 引用了沒撈過的 chunk（資料不一致，另一種病）",
}
_ORDER = ("used", "retrieved_unused", "dropped_by_grader", "not_retrieved",
          "cited_not_retrieved")


def _report(rows: dict, na: dict) -> None:
    print("\n" + "=" * 72)
    print(f"gold 兩段漏斗｜{len(rows)} 題可量"
          f"｜N/A {sum(len(v) for v in na.values())} 題（不併進分母）")
    print("=" * 72)
    by = defaultdict(list)
    for qid, r in rows.items():
        by[r["verdict"]].append(qid)
    n = max(len(rows), 1)
    for k in _ORDER:
        ids = sorted(by.get(k, []))
        print(f"\n{_LABEL[k]}：{len(ids)} 題（{len(ids) / n:.0%}）")
        if ids:
            print("    " + " ".join(ids))
    print("\n逐類：")
    cats = sorted({r["category"] for r in rows.values()})
    for c in cats:
        sub = {q: r for q, r in rows.items() if r["category"] == c}
        # ⚠ 「撈到」在兩群裡不是同一件事：三段版 ＝ 進過候選池（含被圈選丟掉的），
        #   兩段版 ＝ 撐到了 `sources`。印成同一欄會讓兩群看起來可比，實際不可比。
        g3 = {q: r for q, r in sub.items() if r.get("stages") == 3}
        g2 = {q: r for q, r in sub.items() if r.get("stages") != 3}
        parts = []
        if g3:
            got3 = sum(1 for r in g3.values() if r["verdict"] in
                       ("used", "retrieved_unused", "dropped_by_grader"))
            gen3 = sum(1 for r in g3.values() if r["verdict"] in ("used", "retrieved_unused"))
            u3 = sum(1 for r in g3.values() if r["verdict"] == "used")
            parts.append(f"[三段] 撈到 {got3}/{len(g3)} → 進 Generator {gen3}/{len(g3)}"
                         f" → 引用 {u3}/{len(g3)}")
        if g2:
            got2 = sum(1 for r in g2.values() if r["verdict"] in ("used", "retrieved_unused"))
            u2 = sum(1 for r in g2.values() if r["verdict"] == "used")
            parts.append(f"[兩段] 進 Generator {got2}/{len(g2)} → 引用 {u2}/{len(g2)}")
        print(f"  {c:<11} " + "　｜　".join(parts))
    print("\n  ⚠ 引用是 LLM 宣稱的 → 這幾段都只會**高估**。低分是硬證據，高分不是健康證明。")
    # ⚠ 兩群的數字不可合併：少了第一段的那一群，`retrieved_unused` 的意義完全不同
    #   （它把「被 Grader 丟掉」也吸進去了）。分群印，不做加總。
    n2 = [q for q, r in rows.items() if r.get("stages") == 2]
    n3 = [q for q, r in rows.items() if r.get("stages") == 3]
    print(f"  ⚠ 三段可量 {len(n3)} 題｜**只有兩段** {len(n2)} 題"
          "（結果檔早於 `retrieved_union`，或該題崩潰降級沒經過 graph）")
    if n2:
        # ⚠ 兩段版只問「在不在 `sources`」，於是 ②（撈到了被圈選丟掉）與 ③（真的沒撈到）
        #   **一起落進 ③**。把它讀成「檢索沒撈到」就是本 repo 一再犯的那種歸因錯——
        #   mh-03 實測 78 顆撈到、只有 6 顆進 `collected`，**92% 落在 ②**。
        print("  ⚠ 兩段那一群：**② 與 ③ 分不開**（都落進 ③）；而 ① 只問「進了 Generator"
              "\n    手上卻沒引用」＝ 結構性接近恆為 0，**不是健康證明**。見檔頭。")
    if n2 and n3:
        print("  ⚠ **這兩群不可合併**：③ 在兩群裡是兩個不同的東西。")
    bad = [q for q, r in rows.items() if r.get("union_superset_ok") is False]
    if bad:
        print(f"  ✗ **結構不變量破了**：{len(bad)} 題的 `sources` 不是 `retrieved_union` 的子集"
              f"\n    （{' '.join(sorted(bad))}）——兩格來自不同次跑，或資料錯。先查這個。")
    rec_n = sum(r.get("semicolon_recovered", 0) for r in rows.values())
    if rec_n:
        ids = sorted(q for q, r in rows.items() if r.get("semicolon_recovered"))
        print(f"  ⚠ 靠 `;` 正規化才撈到的引用共 {rec_n} 筆（{len(ids)} 題：{' '.join(ids)}）"
              "\n    ——那是**生產 `_extract_citations` 的缺陷**，不是本檔的。見檔頭。")


def _selftest() -> int:
    """⚠ 判別力全在誤報對照③④⑤：陽性那一條，一個「一律回 used」的實作也會過。"""
    ok = fail = 0

    def _a(name, cond, note=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  OK    {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {note}")

    _a("① 撈到且引用 → used", classify(True, True) == "used")
    _a("② 撈到沒引用 → retrieved_unused", classify(True, False) == "retrieved_unused")
    _a("③ **誤報對照**：沒撈到就是 not_retrieved，不可以因為沒引用就併進 ①",
       classify(False, False) == "not_retrieved")
    _a("④ **誤報對照**：引用了沒撈過的 chunk **不算** used（那是資料不一致）",
       classify(False, True) == "cited_not_retrieved")
    _a("⑤ **誤報對照**：四種輸入落在四個互不相同的格",
       len({classify(a, b) for a in (True, False) for b in (True, False)}) == 4)

    # ── classify3：三段版。⚠ 判別力全在誤報對照⑨⑩⑪ ──────────────────────────
    _a("⑧ 撈到、進了 sources、也引用了 → used", classify3(True, True, True) == "used")
    _a("⑨ **誤報對照**：撈到了但沒撐過圈選 → `dropped_by_grader`，"
       "**不可以**併進 `not_retrieved`（修檢索對它無效）",
       classify3(True, False, False) == "dropped_by_grader")
    _a("⑩ **誤報對照**：進了 sources 卻沒引用 → `retrieved_unused`（生成層，另一種病）",
       classify3(True, True, False) == "retrieved_unused")
    _a("⑪ **誤報對照**：整條都沒撈到才是 `not_retrieved`",
       classify3(False, False, False) == "not_retrieved")
    _a("⑫ **誤報對照**：引用了沒撈過的 chunk 一律是資料不一致，不因 sources 而改判",
       classify3(False, False, True) == "cited_not_retrieved"
       and classify3(False, True, True) == "cited_not_retrieved")
    _a("⑬ **誤報對照**：三段版在「有 sources 就有 union」的合法輸入上，"
       "五個格子都取得到（沒有恆不觸發的死格）",
       {classify3(u, s, c) for u in (True, False) for s in (True, False)
        for c in (True, False)} == {"used", "retrieved_unused", "dropped_by_grader",
                                    "not_retrieved", "cited_not_retrieved"})
    _a("⑭ **回歸護欄**：兩段版 `classify` 逐字不動（舊結果檔的判定不得因為加了第三段而變）",
       classify(True, True) == "used" and classify(True, False) == "retrieved_unused"
       and classify(False, False) == "not_retrieved"
       and classify(False, True) == "cited_not_retrieved")

    # ⑥ 引用解析走**生產**的 `_extract_citations`，測資是真實答案逐字節錄
    #    （`gj_65q_denominators_20260908.json` 的 `sem-01`）。少了這一條，
    #    「解析器認不認得生產的引用格式」從來沒被驗過——同 ⑮e／⑲l9 的教訓。
    try:
        from agentic_rag_version.validators import _extract_citations
        real = ("- 平台上開發者與已安裝基礎的數量持續增長。[NVDA_10K_2026.html, chunk #20]  \n"
                "- 創新是其核心。[NVDA_10K_2026.html, chunk #20; NVDA_10K_2025.html, chunk #19]")
        got = _extract_citations(real)
        # ⑥ **凍結一個生產缺陷**：`;` 串接的括號生產解析器抓到 **0 筆**（不是部分）。
        #    這條刻意斷言「現況就是這樣」——哪天有人修好生產解析器，這條會 FAIL 並提醒
        #    本檔的正規化可以拿掉。實測 363 個區段裡 5 個（1.4%）是這個形狀。
        _a("⑥ **凍結**：生產 `_extract_citations` 對 `;` 串接的括號抓到 0 筆（已知缺陷）",
           ("NVDA_10K_2025.html", 19) not in got, f"got={sorted(got)}")
        wide, rec_n = _cited_keys(real, _extract_citations)
        _a("⑥b 本檔的 `;` 正規化把它救回來，且差額有被計數",
           ("NVDA_10K_2025.html", 19) in wide and rec_n == 1, f"wide={sorted(wide)} n={rec_n}")
        _a("⑥c **誤報對照**：正規化不得無中生有——單筆引用的答案差額必須是 0",
           _cited_keys("見 [NVDA_10K_2026.html, chunk #20]", _extract_citations)[1] == 0)
        _a("⑦ **誤報對照**：純敘述文字不得被解析出引用",
           _extract_citations("營收成長 18%，見第三季報告。") == set())
    except Exception as e:      # noqa: BLE001
        _a("⑥⑦ 生產解析器可 import", False, repr(e))

    print(f"\n  PASS {ok}  FAIL {fail}")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="?", help="agentic 結果檔")
    ap.add_argument("--output")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not args.results:
        ap.error("要給結果檔路徑，或用 --selftest")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
