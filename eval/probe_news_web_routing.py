"""量「冷凍的 37 題 news／multi_intent 會不會走到 web」（**含 LLM，零 Tavily、零網路**）。

2026-08-19 KB 拔除新聞時講好的下一步是「驗 live web 能不能取代 KB 新聞」，而那件事有
**兩個可以分開問、成本差一個數量級**的子問題：

| 子問題 | 誰決定 | 成本 | 量尺 |
|---|---|---|---|
| ① 這題**會不會**被送去 web | Planner ＋ Grader（LLM） | 只燒 LLM | **本檔** |
| ② web 拿回來的東西**夠不夠**回答它 | Tavily 回應品質 | 連網、燒 Tavily 額度 | `record_web_fixture.py` ＋ `check_web_claims.py` |

**① 是 ② 的必要條件**：一題若根本不會觸發 web，錄再多 fixture 也救不了它——那是路由問題
不是內容問題。所以先用零 Tavily 的方式把 ① 量完，再決定要為哪些題花錢錄 fixture。

**怎麼做到零網路**：把 `agentic_rag_v2._tavily_search` 換成計數樁（回空字串），其餘管線
**原封不動**跑真的 Plan／檢索／Grade。這樣量到的「會不會打 web」與生產判斷式是同一條路，
不是另外抄一份判斷（抄寫版本會漂移，見 `verify_web_gate_isolation.py` 閘門① 的教訓）。
⚠ 樁回空字串 ＝ 模擬「web 有打、但什麼都沒撈到」。所以本檔**不量答案品質**，只量路由。

**判讀看不對稱的錯誤**（同 `probe_realtime_need.py`）：
  · news 題 **0 次 web** → KB 已經沒有新聞了，這題只會拿財報硬答或拒答，**且零揭露**（危險）
  · 財報題 **>0 次 web** → 白燒一次 Tavily（只花錢）

⚠ **陰性對照臂不可省**。少了它，一個「一律打 web」的路由也會在 news 那一臂拿滿分。
   對照題取自 `eval/eval_set.json`（純財報、無任何時效語意），斷言是 **web 呼叫 0 次**。

⚠ **這支不是閘門**（含 LLM、MoE 不固定路由）。它是 probe：量到、寫下來，必要時 `--repeat`。

用法（生產 .venv）：
    .venv/Scripts/python.exe -u eval/probe_news_web_routing.py --output experiments/news_web_routing.json
    #   --ids / --category 只跑一部分；--controls-only 只跑陰性對照（便宜的健檢）
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

NEWS_SET = _ROOT / "eval" / "eval_set_news.json"
MAIN_SET = _ROOT / "eval" / "eval_set.json"

# 陰性對照分兩級，**不可合併**（2026-08-19 第一版把兩級混在一起，得到一個假的「浪費 2 題」）：
#
#   strict —— 期別被明確指定（FY2026／10-K／某一季），wall clock 完全不影響答案。
#             斷言 web 呼叫 **必須是 0**；打了就是白花錢。
#   loose  —— 措辭帶「最近一年／最近十二個月（TTM）」。TTM 是**會計口徑**不是 wall clock，
#             但它離「最近」只有一個字，Grader 判成需要即時**未必是錯的**。
#             只觀察、**不斷言**。
#
# ⚠ **「現在市值／現在股價」不可以當陰性對照**。第一版把 col-08（NVIDIA 現在值多少錢）
#   放進 strict，結果它打了 3 次 web 被記成「浪費」——**錯的是對照不是系統**：
#   市值在 KB 裡是 2026-06-12 的快照，而 eval/web_claims.json 的 web-01 對同型問題的斷言
#   正是 `web_calls_gte: 1`。同一件事在兩支腳本裡有相反的期望，等於量尺自相矛盾。
CONTROL_STRICT = ["lex-03", "lex-07", "sem-01", "sem-06", "mix-01", "mix-11", "col-10"]
CONTROL_LOOSE = ["mh-03"]
CONTROL_IDS = CONTROL_STRICT + CONTROL_LOOSE


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["queries"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", default="2026-08-19",
                    help="鎖死的『今天』。不釘死的話跨日不可重現（KB 天花板與它比）")
    ap.add_argument("--ids", nargs="*", default=None)
    ap.add_argument("--category", default=None)
    ap.add_argument("--controls-only", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--output", default="experiments/news_web_routing.json")
    args = ap.parse_args(argv)

    os.environ["AGENTIC_AS_OF_DATE"] = args.as_of
    os.environ.setdefault("AGENTIC_WEB_SEARCH", "true")   # 路由要開才量得到；Tavily 由樁攔住

    import rag_query as rq                      # noqa: E402
    import agentic_rag_v2 as ar                 # noqa: E402

    if not ar.ENABLE_WEB_SEARCH:
        print("[ABORT] ENABLE_WEB_SEARCH 是關的 → 每一題都會是 0 次，量尺沒有判別力。")
        return 2

    # ── 零網路的關鍵：換掉 Tavily，其餘管線原封不動 ─────────────────────────────
    calls: list[str] = []
    _real = ar._tavily_search

    def _stub(query: str, need: str = "none") -> str:
        calls.append(f"[{need}] {query}")
        return ""
    ar._tavily_search = _stub

    news = [q for q in _load(NEWS_SET)]
    ctrl = [q for q in _load(MAIN_SET) if q["id"] in set(CONTROL_IDS)]
    missing = set(CONTROL_IDS) - {q["id"] for q in ctrl}
    if missing:   # 對照題被改名／搬走而靜默消失 ＝ 陰性對照悄悄縮水
        print(f"[ABORT] 陰性對照有 {len(missing)} 題在 eval_set.json 裡找不到：{sorted(missing)}")
        return 2
    for q in ctrl:
        q["_arm"] = "control" if q["id"] in CONTROL_STRICT else "control_loose"
    for q in news:
        q["_arm"] = "news_set"

    items = ctrl if args.controls_only else news + ctrl
    if args.category:
        items = [q for q in items if q["category"] == args.category]
    if args.ids:
        items = [q for q in items if q["id"] in set(args.ids)]

    print(f"as_of={args.as_of}  collection={rq.COLLECTION_NAME}  題數={len(items)}"
          f"（news_set {sum(1 for q in items if q['_arm'] == 'news_set')}／"
          f"control {sum(1 for q in items if q['_arm'] == 'control')}）  repeat={args.repeat}")
    print(f"CHECKER={ar.CHECKER_MODEL}  預算 QUERY_WEB_BUDGET={ar.QUERY_WEB_BUDGET}\n")

    records: list[dict] = []
    for rnd in range(args.repeat):
        for i, q in enumerate(items, 1):
            calls.clear()
            try:
                out = ar.run_agentic(q["query"], freshness_mode=ar.FRESHNESS_LIVE)
                err = ""
            except Exception as e:
                out, err = {}, repr(e)
            ans = out.get("answer", "")
            rec = {"round": rnd, "id": q["id"], "arm": q["_arm"], "category": q["category"],
                   "query": q["query"], "n_web": len(calls), "web_queries": list(calls),
                   "sub_queries": out.get("sub_queries", []),
                   "answer_len": len(ans), "answer_head": ans[:200], "error": err}
            records.append(rec)
            flag = ""
            if q["_arm"] == "news_set" and not calls:
                flag = "  ❌ 不叫 web（KB 已無新聞 → 只能硬答或拒答）"
            if q["_arm"] == "control" and calls:
                flag = "  ⚠ 白叫 web"
            if q["_arm"] == "control_loose" and calls:
                flag = "  ·  loose 對照有打 web（觀察，不判錯）"
            print(f"[{rnd}:{i}/{len(items)}] {q['id']:8s} {q['category']:12s} "
                  f"web×{len(calls)}{flag}{'  ERR ' + err if err else ''}")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(
        {"meta": {"as_of": args.as_of, "collection": rq.COLLECTION_NAME,
                  "checker_model": ar.CHECKER_MODEL, "repeat": args.repeat,
                  "control_ids": CONTROL_IDS,
                  "note": "web 呼叫由計數樁攔截，未連網；本檔只量路由，不量答案品質"},
         "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 判讀：分臂、分類別，不合併 ─────────────────────────────────────────────
    print("\n" + "=" * 88)
    by = collections.defaultdict(lambda: [0, 0])      # (arm, category) → [有打 web 的題數, 總題數]
    for r in records:
        k = (r["arm"], r["category"])
        by[k][0] += 1 if r["n_web"] else 0
        by[k][1] += 1
    print(f"{'臂':<10}{'類別':<14}{'觸發 web':<12}比例")
    for (arm, cat), (fired, tot) in sorted(by.items()):
        print(f"{arm:<10}{cat:<14}{fired}/{tot:<10}{fired / tot:.0%}")

    silent = [r["id"] for r in records if r["arm"] == "news_set" and not r["n_web"]]
    wasted = [r["id"] for r in records if r["arm"] == "control" and r["n_web"]]
    loose = [r["id"] for r in records if r["arm"] == "control_loose" and r["n_web"]]
    print(f"\n❌ 危險（news_set 題不叫 web）：{len(silent)} 題 {sorted(set(silent))}")
    print(f"⚠ 浪費（strict 陰性對照白叫 web）：{len(wasted)} 題 {sorted(set(wasted))}")
    print(f"·  loose 對照有打 web（TTM／最近一年措辭，觀察不判錯）：{len(loose)} 題 {sorted(set(loose))}")
    if not any(r["web_queries"] for r in records) and not args.controls_only:
        # ⚠ --controls-only 時全 0 正是預期結果,不該報警(否則護欄變成狼來了)
        print("[!] 全程 0 次 web 呼叫 → 先當壞消息查：樁是否真的裝上、ENABLE_WEB_SEARCH 是否為真。")
    dup = sum(1 for r in records if len(r["web_queries"]) > len(set(r["web_queries"])))
    print(f"[i] 同題送出**逐字相同** web query 的：{dup} 題（預算 {ar.QUERY_WEB_BUDGET} 次）")
    print("    ⚠ 這只抓逐字重複。實測近重複（差幾個字、語意同一件事）抓不到——"
          "要量那個得比語意，這支刻意不做（那會把一個 LLM 判斷塞進零 LLM 的統計裡）。")
    print(f"→ {args.output}")
    ar._tavily_search = _real
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
