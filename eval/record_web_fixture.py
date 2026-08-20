"""record_web_fixture.py — 錄製 live 路徑的 fixture（**會連網、會燒 Tavily 與 LLM 額度**）。

live web 沒辦法定 gold（資料每天在變），但把外部世界錄下來、把「今天」鎖死之後，那幾題
的背景就變成靜態的 → 可以寫確定性斷言。這支就是產生那份 fixture 的唯一入口。

**必須同時錄兩份快取，缺一不可**：
  · `RAG_WEB_REPLAY`   — Tavily 原始回應（見 [`web_replay.py`](../web_replay.py)）
  · `RAG_REPLAY_CACHE` — plan／translate_en／check（見 [`llm_replay.py`](../llm_replay.py)）
只錄 web 會**在重放時 miss**：pipeline 送給 Tavily 的 query 是 Planner 與 Grader 的 LLM
輸出決定的，而 `gpt-oss-120b` 是 MoE、temp=0 也不固定專家路由（實測 plan 48% 重跑不同）
→ 重放時會生出不同的 web query、打不到 fixture。把 plan/check 一起釘死才閉環。

**題目選擇**（刻意涵蓋四種形態 ＋ 一個陰性對照）：
  · intraday 報價／市值 —— KB 結構上不可能有，必走 web
  · KB 有舊值、web 有新值 —— 這是 `find_authority_conflicts` 對 web 全盲的那個缺口
  · 新聞時效
  · **陰性對照**：純歷史財報題，斷言它**一次 web 都不該打**（web 沒有被無差別觸發的證明）

⚠ `_meta` 會記下 as-of 日期與 collection。兩者都影響 `kb_unfixable`（它拿 KB 天花板跟
   今天比），所以 fixture 不是只有 web 回應，是**綁定一組環境**。重建 collection 後這份
   fixture 的斷言要重新確認。

用法（在生產 .venv）：
    .venv/Scripts/python.exe -u eval/record_web_fixture.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 錄製題庫。id 前綴 web- 以免與 eval_set.json 的 100 題混淆——**這些不進 eval_set**
# （加題會動到 100 題基準與含 24 處人工校正的 reference_answers.json，代價遠大於這件事）。
QUERIES = [
    ("web-01", "Apple 目前的市值是多少？", "intraday；KB 結構上沒有即時市值"),
    ("web-02", "NVIDIA 現在的股價是多少？", "intraday；純即時報價"),
    ("web-03", "Microsoft 最新一季的 Azure 營收成長率是多少？", "KB 有舊值、web 可能更新 → 權威衝突"),
    ("web-04", "Tesla 最近有什麼重要消息？", "新聞時效"),
    ("web-05", "Apple 在 FY2025 全年的總營收是多少？", "陰性對照：純歷史財報，不該打 web"),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", default="eval/web_fixture.json")
    ap.add_argument("--replay-cache", default="eval/web_replay_llm.json",
                    help="plan/translate_en/check 的快取。**與 100 題用的 "
                         "eval/replay_cache.json 分開**，避免這幾題的決策污染主 fixture")
    ap.add_argument("--as-of", default=date.today().isoformat(),
                    help="鎖死的『今天』。錄完就不要再改——改了 kb_unfixable 的判斷會跟著變")
    ap.add_argument("--output", default="experiments/web_fixture_answers.json")
    ap.add_argument("--ids", nargs="*", default=None)
    ap.add_argument("--from-eval-set", default=None,
                    help="改從 eval set 檔（{queries:[{id,query,category}]}）取題，取代上面 5 題。"
                         "⚠ **fixture 路徑要一起換**：既有的 eval/web_fixture.json 綁 as-of 2026-08-15 "
                         "與 eval/web_claims.json 的斷言，混錄進去會把那道閘門弄壞")
    ap.add_argument("--mode", choices=("record", "replay"), default="record",
                    help="record＝連網並寫入 fixture；replay＝絕不連網，miss 直接報錯。"
                         "驗收一律用 replay")
    args = ap.parse_args(argv)

    # ⚠ 這四個必須早於 import agentic_rag_v2
    os.environ["RAG_WEB_REPLAY"] = args.fixture
    os.environ["RAG_WEB_REPLAY_MODE"] = args.mode
    os.environ["RAG_REPLAY_CACHE"] = args.replay_cache
    os.environ["AGENTIC_AS_OF_DATE"] = args.as_of

    sys.path.insert(0, str(_ROOT))
    import rag_query as rq                      # noqa: E402
    import web_replay as _wr                    # noqa: E402
    import agentic_rag_v2 as ar                 # noqa: E402

    if args.mode == "record" and not os.getenv("TAVILY_API_KEY"):
        print("[ABORT] 沒有 TAVILY_API_KEY，錄不到任何 web 回應。")
        return 2
    if not ar.ENABLE_WEB_SEARCH:
        print("[ABORT] ENABLE_WEB_SEARCH 是關的，這樣錄出來的是空 fixture。")
        return 2

    if args.from_eval_set:
        src = json.loads(Path(args.from_eval_set).read_text(encoding="utf-8"))["queries"]
        pool = [(q["id"], q["query"], q.get("category", "")) for q in src]
        if Path(args.fixture).resolve() == (_ROOT / "eval" / "web_fixture.json").resolve():
            print("[ABORT] --from-eval-set 不可寫進 eval/web_fixture.json"
                  "（那份綁既有 5 題的斷言與 as-of）。請另給 --fixture。")
            return 2
    else:
        pool = QUERIES
    items = [q for q in pool if not args.ids or q[0] in set(args.ids)]
    _wr.note_meta(as_of=args.as_of, collection=rq.COLLECTION_NAME,
                  allowed_domains=sorted(ar.WEB_ALLOWED_DOMAINS),
                  fetch_results=ar.TAVILY_FETCH_RESULTS,
                  retrieval_model=ar.RETRIEVAL_MODEL, gen_model=ar.GEN_MODEL)
    print(f"as-of={args.as_of}  collection={rq.COLLECTION_NAME}  題數={len(items)}")

    def _web_calls() -> int:
        """record 模式算 recorded、replay 模式算 hit——兩邊都要能數出「這題打了幾次 web」，
        因為陰性對照的斷言就是 `web_calls == 0`。"""
        s = _wr.stats()
        return s["recorded"] + s["hit"]

    records = []
    for qid, query, why in items:
        before = _web_calls()
        print(f"\n──[{qid}] {query}\n   （{why}）")
        try:
            out = ar.run_agentic(query, freshness_mode=ar.FRESHNESS_LIVE)
        except Exception as e:
            print(f"   [FAIL] {e!r}")
            records.append({"id": qid, "query": query, "why": why, "error": repr(e)})
            continue
        n_web = _web_calls() - before
        ans = out.get("answer", "")
        records.append({
            "id": qid, "query": query, "why": why, "answer": ans,
            "n_web_calls": n_web, "n_web_calls_recorded": n_web,
            "sub_queries": out.get("sub_queries", []),
            "sources": [{"source": c.get("source"), "chunk_index": c.get("chunk_index")}
                        for c in (out.get("chunks") or [])],
        })
        print(f"   web 呼叫錄到 {n_web} 次｜答案 {len(ans)} 字")
        print(f"   {ans[:220].replace(chr(10), ' ')}")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(
        {"meta": {"as_of": args.as_of, "collection": rq.COLLECTION_NAME,
                  "fixture": args.fixture, "replay_cache": args.replay_cache},
         "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nfixture → {args.fixture}（web 回應 {_wr.stats()['recorded']} 筆）")
    print(f"答案 → {args.output}")
    print("下一步：讀答案，把『這題該成立什麼』寫成斷言檔，再用 replay 模式驗。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
