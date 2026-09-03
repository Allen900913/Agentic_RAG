"""量「Replanner 加的待辦到底貢獻了什麼」（**含 LLM，非零噪音；零網路**）。

**為什麼是現在才做得出來**：2026-08-28 給每個 todo 加上 `attributable`
（`_node_plan` 建的 True／`_node_replan` 建的 False）之後，「哪些待辦是機器自己造的」
**第一次變成可查詢的屬性**。在那之前只能從 trace 的字面猜。

**為什麼要量**：replan 是 execute↔replan 迴圈裡唯一有 agency 的節點，而它的已知副作用是
子問題爆炸——實測會生出 `改用網路搜尋查…最新新聞` →`…的重要消息` →`使用即時金融網站…`
這種近義待辦串（`eval/web_replay_llm.json` 裡逐字錄著五個），代價是 7 次 web／近一小時一題。
它拿不到的資訊是決定性的：`_node_replan` 的 prompt 只給 `id / status / task / result[:200]`，
**`web_used` 就在同一個 dict 上卻沒放進去**，而 `kb_unfixable`、Grader 試過哪些改寫、燒了幾輪，
在 `_run_executor_deterministic` return 的時候就整個丟掉了。

---

## 四個指標，不可合併

| 指標 | 意思 | 判讀 |
|---|---|---|
| `n_new` | 這個待辦 commit 的 chunk 裡，**先前待辦沒撈過**的有幾個 | 0 ＝ 純重複 |
| `n_new_cited` | 那些「只有它撈到」的 chunk，**最終答案真的引用**了幾個 | **這一格才是「有沒有用」** |
| `web` | 它打了幾次 web | 成本 |
| `secs` | 它花了多久 | 成本 |

⚠ **`n_new` 高不等於有用**：撈到別人沒撈到的東西，跟那東西被寫進答案，是兩件事。
   歷史上「多撈一點」的改動就是這樣看起來有效而實際上沒有（見 docs/EVAL.md 已試無效總表）。

⚠ **`n_new_cited` 高也不等於有用——這一格在「流資訊題」上會量到反面**（2026-09-01 訂正）。
   它量的是「這個 chunk **有沒有被引用**」，不是「引用它**對不對**」。實測拿
   `web-04`（「Tesla 最近有什麼重要消息」）跑，commit 進答案的是
   `TSLA_10K_2023#59`／`TSLA_10K_2024#63`／`TSLA_10Q_202506#83`——**一則新聞都沒有**
   （KB 依設計不收新聞），而這一格照樣記成「引用貢獻＝3」。
   在那種題目上，被引用**正是缺陷本身**：拿舊財報冒充最近的消息。
   → **判讀本表時，flow 題（`web-04` 這種）的 `n_new_cited` 一律要配
     [`check_news_routing.py`](check_news_routing.py) 一起看**，單看這一格會把傷害讀成貢獻。
     `web-01`／`web-02`／`web-03` 與對照組是財報題，這一格在它們身上的原意成立。

## ⚠ 誤報對照（沒有它這支毫無意義）

**Planner 的待辦用完全相同的方式量**。如果 planner 待辦的 `n_new_cited` 也常常是 0，
那「replan 待辦 n_new_cited=0」就**不是**關於 replan 的證據，只是這個指標沒有判別力。
輸出一律把兩邊並排印出來。

⚠ 第二個誤報對照是**順序**：replan 的待辦天生跑在後面，而「先前待辦沒撈過」對後跑的人
本來就比較難。所以 planner 那一欄也要看**同一波之內的後段待辦**——輸出附上逐待辦明細供人工看。

## 零網路怎麼做到

把 `ar._tavily_search` 換成計數樁，**其餘管線原封不動**（同 `probe_news_web_routing.py`）。
⚠ **樁一定要回傳「像樣的非空內容」**：回空字串等於**先驗地判定 web 待辦沒有貢獻**，
   那會讓這支量出想要的答案而不是真的答案。所以樁回一則格式與 `_tavily_search` 相同、
   帶發布日與白名單網域的假結果。

⚠ 這支**不是閘門**（LLM 有噪音、MoE 不固定路由、一題 50~60 次呼叫）。`--repeat` ≥2，
   判讀看逐題明細不要看總平均。

用法：
    RAG_COLLECTION=us_stock_rag_edgar_multiyear \\
      .venv/Scripts/python.exe -u eval/probe_replan_contribution.py --repeat 2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agentic_rag_version as ar     # noqa: E402

# 題目：兩組，刻意讓 replan 有機會出手／刻意讓它沒事可做。
#   · LIVE 組＝已知會走 live 補救路徑的四題（取自 record_web_fixture.QUERIES）
#   · 對照組＝純財報題，replan 理論上不該加東西（若它照樣加，那本身就是發現）
LIVE_QS = [
    ("web-01", "Apple 目前的市值是多少？"),
    ("web-02", "NVIDIA 現在的股價是多少？"),
    ("web-03", "Microsoft 最新一季的 Azure 營收成長率是多少？"),
    ("web-04", "Tesla 最近有什麼重要消息？"),
]
CTRL_QS = [
    ("web-05", "Apple 在 FY2025 全年的總營收是多少？"),
    ("mh-03", "在 NVIDIA、Meta、Alphabet 這三家公司中，最近一年營收年增率（YoY revenue growth）"
              "最高的是哪一家？該公司最近十二個月（TTM）的淨利（net income）是多少？"),
]

# 樁回傳的假 web 結果：格式與 `_tavily_search` 的真回傳一致（含發布日與白名單網域），
# 內容刻意「像樣且可引用」——回空字串會先驗地判定 web 待辦沒貢獻，那是在作弊。
_STUB_WEB = (
    "網路搜尋結果:\n"
    "- Stub Market Data — quote snapshot（發布日 2026-08-28）"
    "[web: https://stockanalysis.com/stocks/stub/]\n"
    "  Shares last traded at $123.45, up 1.2% on the session; market capitalisation stood at "
    "$1.23 trillion as of the close on August 28, 2026. Volume was 41.2 million shares.\n"
    "- Stub Newswire — company update（發布日 2026-08-27）[web: https://reuters.com/stub/]\n"
    "  The company announced on August 27, 2026 that quarterly deliveries reached 512,000 units, "
    "and reaffirmed full-year guidance."
)


def _run_once(query: str) -> dict:
    """跑一次生產 run_agentic（live），側錄每個待辦的出身與貢獻。不改變任何行為。"""
    seen: list[dict] = []
    _orig_todo = ar._run_one_todo
    _orig_web = ar._tavily_search
    n_web = {"n": 0}

    def _spy(todo, freshness_mode, verbose):
        t0 = time.time()
        r = _orig_todo(todo, freshness_mode, verbose)
        seen.append({
            "id": todo["id"],
            "task": todo["task"],
            # ⚠ 下標讀不是 .get(..., True)：漏設要當場炸,同 `_run_one_todo` 的理由
            "attributable": todo["attributable"],
            "chunks": [ar._chunk_id(c) for c in r["picked"]],
            "web": 1 if r["web_notes"] else 0,
            "secs": round(time.time() - t0, 1),
        })
        return r

    def _stub(query_, need="none"):
        n_web["n"] += 1
        return _STUB_WEB

    ar._run_one_todo, ar._tavily_search = _spy, _stub
    try:
        out = ar.run_agentic(query, freshness_mode=ar.FRESHNESS_LIVE)
    finally:
        ar._run_one_todo, ar._tavily_search = _orig_todo, _orig_web

    answer = out.get("answer", "") or ""
    cited = {f"{s}#{i}" for s, i in ar._extract_citations(answer)}

    # 逐待辦算貢獻：seen 是執行順序，所以「先前待辦沒撈過」直接用前綴聯集。
    earlier: set[str] = set()
    for rec in seen:
        new = [c for c in rec["chunks"] if c not in earlier]
        rec["n_picked"] = len(rec["chunks"])
        rec["n_new"] = len(new)
        rec["n_new_cited"] = sum(1 for c in new if c in cited)
        earlier.update(rec["chunks"])
    return {"todos": seen, "n_web": n_web["n"], "answer_len": len(answer),
            "n_cited": len(cited)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--only", choices=("live", "ctrl", "all"), default="all")
    ap.add_argument("--output", default="experiments/replan_contribution.json")
    args = ap.parse_args()

    qs = (LIVE_QS if args.only == "live" else
          CTRL_QS if args.only == "ctrl" else LIVE_QS + CTRL_QS)
    print(f"collection={ar.rq.COLLECTION_NAME}  CHECKER={ar.CHECKER_MODEL}  "
          f"repeat={args.repeat}  題數={len(qs)}   （web 走計數樁，零網路）\n")

    runs = []
    for qid, q in qs:
        for k in range(args.repeat):
            t0 = time.time()
            try:
                r = _run_once(q)
                r.update({"id": qid, "round": k, "query": q, "secs": round(time.time() - t0, 1)})
            except Exception as e:                       # 一題炸掉不拖垮整批
                r = {"id": qid, "round": k, "query": q, "error": repr(e), "todos": [],
                     "n_web": 0, "secs": round(time.time() - t0, 1)}
            runs.append(r)
            nplan = sum(1 for t in r["todos"] if t.get("attributable"))
            nrep = sum(1 for t in r["todos"] if not t.get("attributable"))
            print(f"  {qid} r{k}: {r['secs']:>6}s  待辦 plan={nplan} replan={nrep}  "
                  f"web={r['n_web']}" + (f"  ERR {r['error'][:60]}" if r.get("error") else ""))

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 彙總：Planner vs Replanner 並排（誤報對照就是這個並排本身）────────────────
    def _agg(flag):
        ts = [t for r in runs for t in r["todos"] if bool(t.get("attributable")) is flag]
        if not ts:
            return None
        return {
            "n": len(ts),
            "picked": sum(t["n_picked"] for t in ts),
            "new": sum(t["n_new"] for t in ts),
            "new_cited": sum(t["n_new_cited"] for t in ts),
            "zero_new": sum(1 for t in ts if t["n_new"] == 0),
            "zero_cited": sum(1 for t in ts if t["n_new_cited"] == 0),
            "web": sum(t["web"] for t in ts),
            "secs": round(sum(t["secs"] for t in ts), 1),
        }

    print()
    print(f"{'出身':<12}{'待辦數':>7}{'commit':>8}{'獨有':>7}{'獨有且被引用':>14}"
          f"{'獨有=0':>9}{'被引用=0':>10}{'web':>6}{'秒':>9}")
    print("-" * 84)
    for flag, name in ((True, "Planner"), (False, "Replanner")):
        a = _agg(flag)
        if a is None:
            print(f"{name:<12}{'（這批沒有出現）':>7}")
            continue
        print(f"{name:<12}{a['n']:>7}{a['picked']:>8}{a['new']:>7}{a['new_cited']:>14}"
              f"{a['zero_new']:>9}{a['zero_cited']:>10}{a['web']:>6}{a['secs']:>9}")

    print()
    print("逐待辦明細（順序＝執行順序；⚠ replan 天生跑在後面，判讀要跟 planner 的後段比）")
    print("-" * 84)
    for r in runs:
        if r.get("error"):
            print(f"  {r['id']} r{r['round']}: ERROR {r['error'][:70]}")
            continue
        print(f"  {r['id']} r{r['round']}  (答案引用 {r['n_cited']} 個 chunk)")
        for t in r["todos"]:
            src = "plan  " if t["attributable"] else "REPLAN"
            print(f"     [{src}] commit={t['n_picked']} 獨有={t['n_new']} "
                  f"獨有且被引用={t['n_new_cited']} web={t['web']} {t['secs']}s  "
                  f"{t['task'][:44]}")

    print()
    print("⚠ 這支是 probe 不是閘門：LLM 有噪音，判讀看逐待辦明細，不要看總平均。")
    print("⚠ 誤報對照＝上表的 Planner 那一列。若它的『獨有且被引用』也接近 0，")
    print("  那麼 Replanner 的 0 就不是關於 replan 的證據，是這個指標沒有判別力。")
    print(f"\n寫入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
