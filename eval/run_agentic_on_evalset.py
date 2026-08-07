"""run_agentic_on_evalset.py — 讓 agentic RAG 產出「與 chunking 實驗可並排比較」的結果檔。

目的（2026-07-24）：把目前指定的 agentic 模組（預設 agentic_rag_v2）跑在**與 Exp0-4 完全相同的
90 題 + 相同 collection**，
輸出**與 eval_generation_llm_judge.py 相同 schema** 的 generation_judge.json（含 answer +
contexts），再丟進**原封不動的** eval_ragas_vs_rubric.py 跑同一套 RAGAS 六指標——agentic 就
變成 Exp0-4 表格裡直接可加的一欄，公平可比。

為什麼不用 eval_agentic.py：那支跑的是 eval_set_agentic.json（5 題跨公司比較題）、用舊
rubric 自製判定，**不同題也不同法**，無法跟 Exp0-4 並排。本腳本只負責「跑 agentic + 落盤
answer/contexts」，評分交給 RAGAS（standalone、在 .venv-ragas 跑），兩者職責分離。

公平性保證：
  - 同題：讀 eval/eval_set.json 的 90 題（非 agentic 專屬題庫）。
  - 同底座：--collection 預設 us_stock_rag_edgar_exp4（Exp4 最佳 chunking），agentic 與單次
    版共用同一個 Qdrant collection + 同一個 reranker，唯一差別是「編排」（decomposition）。
  - 同測量：輸出 schema 對齊 generation_judge.json，交給同一個 eval_ragas_vs_rubric.py。
  - contexts = run_agentic 回傳的 collected（全 run 各子問題撈到的 chunk 聯集，正是衡量
    retrieval recall 用的池；run_agentic docstring 已註明此用途）。

⚠ 不 import eval_generation_llm_judge：ej 在 import 當下把 rq.call_llm 換成自己的 NVIDIA
路由，agentic_rag_nv 也在 import 當下換 rq.call_llm——兩者搶同一個全域，後 import 者贏。
本腳本只需要 agentic 的路由生效，所以只 import agentic 模組，不碰 ej（評分本就在 RAGAS）。

resume：agentic 每題多輪 LLM、偶發 tool-call 崩潰（見 CHANGELOG_AGENTIC.md），因此**逐題
落盤**——重跑同一道指令會跳過已完成的題（有 answer 且無 error），只補跑失敗/未跑的。

用法：
  .venv/Scripts/python.exe eval/run_agentic_on_evalset.py
  .venv/Scripts/python.exe eval/run_agentic_on_evalset.py --limit 5           # smoke test
  .venv/Scripts/python.exe eval/run_agentic_on_evalset.py --ids mi-08 mi-12   # 只跑指定題
  # 完成後（在 .venv-ragas）：
  .venv-ragas/Scripts/python.exe eval/eval_ragas_vs_rubric.py \
      --from-results experiments/agentic/generation_judge.json \
      --output experiments/agentic/ragas_scores.json
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import rag_query as rq

EVAL_SET = Path("eval/eval_set.json")
DEFAULT_COLLECTION = "us_stock_rag_edgar_exp4"
DEFAULT_OUTPUT = Path("experiments/agentic/generation_judge.json")


def _record_from_agentic(q: dict, answer: str, chunks: list[dict],
                         sub_queries: list) -> dict:
    """把 agentic 輸出攤平成 generation_judge.json 的 record schema。
    RAGAS 只讀 id/category/query/answer/contexts；其餘欄位補上以對齊 schema、方便複查。"""
    contexts = [c["content"] for c in chunks if (c.get("content") or "").strip()]
    sources = [{"source": c.get("source"), "chunk_index": c.get("chunk_index")}
               for c in chunks]
    return {
        "id": q["id"],
        "category": q["category"],
        "query": q["query"],
        "answerable": q.get("answerable", True),
        "answer": answer,
        "raw_answer_with_evidence": None,
        "sources": sources,
        "contexts": contexts,
        # 以下是 generation_judge schema 既有欄位，agentic 這條不自算（交給 RAGAS），補 null。
        "correctness": None,
        "correctness_verdict": None,
        "is_refusal": answer.strip().startswith("I don't have enough") or answer.strip().startswith("I don’t have enough"),
        "wrongful_refusal": None,
        "context_recall_llm": None,
        "context_recall_overlap": None,
        "actionable_feedback": "",
        # agentic 專屬（額外資訊，不影響 RAGAS）。
        "agentic_sub_queries": sub_queries,
        "agentic_n_chunks": len(chunks),
    }


def _load_done(output: Path, expected_meta: dict) -> dict[str, dict]:
    """讀同設定的既有輸出。模組/模型/collection/時間模式不同時不可 resume 舊答案。"""
    if not output.exists():
        return {}
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
    except Exception:
        return {}
    old_meta = data.get("meta", {})
    compatibility_keys = (
        "module", "collection", "retrieval_model", "checker_model", "gen_model",
        "validator", "web_search", "freshness_mode", "eval_set",
    )
    mismatches = [key for key in compatibility_keys if old_meta.get(key) != expected_meta.get(key)]
    if mismatches:
        print(f"[INFO] 既有輸出設定不同（{', '.join(mismatches)}），不 resume 舊 records")
        return {}
    done = {}
    for rec in data.get("records", []):
        rid = rec.get("id")
        if rid and rec.get("answer") and not rec.get("error"):
            done[rid] = rec
    return done


def _write(output: Path, records_by_id: dict[str, dict], meta: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "records": list(records_by_id.values())}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", default="agentic_rag_v2", help="要跑的 agentic 模組")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help="Qdrant collection（預設 Exp4 最佳 chunking，與單次版對照必須同一個）")
    ap.add_argument("--eval-set", default=str(EVAL_SET))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 題（smoke test）")
    ap.add_argument("--ids", nargs="+", default=None, help="只跑指定 id")
    ap.add_argument("--category", default=None, help="只跑指定類別")
    ap.add_argument("--no-validator", action="store_true",
                    help="關閉 agentic 的 Reflection 幻覺稽核（省一次 LLM 呼叫）")
    ap.add_argument("--no-web", action="store_true",
                    help="關閉 agentic_rag_v2 的 web_search（Tavily）。eval 強烈建議加此旗標："
                         "golden answer 是根據 KB 資料生成的，且網路搜尋結果不可重現，摻入會傷 correctness/"
                         "context_recall 並讓結果無法重現比較。對沒有 web_search 的模組（如 agentic_rag_nv）無作用。")
    ap.add_argument("--freshness-mode", choices=["snapshot", "live"], default="snapshot",
                    help="時間語意；eval 預設 snapshot，『最近／最新』只指 KB 中最新可用資料，"
                         "不以執行當天重新定義題目")
    ap.add_argument("--recursion-limit", type=int, default=100)
    args = ap.parse_args()

    # 先把 collection 指到 Exp4（agentic 內部 rq.retrieve 讀 rq.COLLECTION_NAME）——
    # 必須在跑任何檢索前設定，否則會撈到預設生產 collection、和 Exp4 對照不成立。
    rq.COLLECTION_NAME = args.collection

    # 只 import agentic 模組（它會 monkeypatch rq.call_llm 成 NVIDIA 路由）；不碰 ej。
    ar = importlib.import_module(args.module)
    # eval 隔離：關掉 web_search（若該模組有此開關）。web 結果不可重現、且會偏離 KB golden answer。
    web_on = getattr(ar, "ENABLE_WEB_SEARCH", None)
    if args.no_web and web_on is not None:
        ar.ENABLE_WEB_SEARCH = False
        web_on = False
        print("[INFO] --no-web: 已關閉 web_search（Tavily），本輪只用知識庫檢索")
    effective_web_on = bool(web_on) and args.freshness_mode == "live"
    if args.freshness_mode == "snapshot" and web_on:
        print("[INFO] freshness_mode=snapshot: v2 在工程層不掛載 web_search，維持 closed-corpus eval")
    ar._get_models()  # 提前載入 BGE-M3 + reranker + client

    eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    queries = eval_set["queries"]
    if args.category:
        queries = [q for q in queries if q["category"] == args.category]
    if args.ids:
        want = set(args.ids)
        queries = [q for q in queries if q["id"] in want]
    if args.limit:
        queries = queries[: args.limit]

    meta = {
        "producer": "run_agentic_on_evalset.py",
        "module": args.module,
        "collection": args.collection,
        "retrieval_model": getattr(ar, "RETRIEVAL_MODEL", "?"),
        "checker_model": getattr(ar, "CHECKER_MODEL", "?"),
        "gen_model": getattr(ar, "GEN_MODEL", "?"),
        "validator": not args.no_validator,
        "web_search": effective_web_on,
        "freshness_mode": args.freshness_mode,
        "eval_set": args.eval_set,
    }
    output = Path(args.output)
    records_by_id = _load_done(output, meta)

    todo = [q for q in queries if q["id"] not in records_by_id]
    print(f"[INFO] module={args.module} collection={args.collection} "
          f"retrieval={meta['retrieval_model']} checker={meta['checker_model']} gen={meta['gen_model']}")
    print(f"[INFO] {len(queries)} 題選中，{len(records_by_id)} 題已完成（resume 跳過），"
          f"本輪要跑 {len(todo)} 題 → {output}")

    for i, q in enumerate(todo, start=1):
        t0 = time.time()
        print(f"\n[{i}/{len(todo)}] {q['id']} ({q['category']}): {q['query'][:55]}")
        try:
            run_kwargs = {
                "recursion_limit": args.recursion_limit,
                "enable_coverage_validator": not args.no_validator,
            }
            # runner 仍相容 agentic_rag_nv 等舊模組；只有支援 freshness_mode 的 v2 才傳入。
            if "freshness_mode" in inspect.signature(ar.run_agentic).parameters:
                run_kwargs["freshness_mode"] = args.freshness_mode
            out = ar.run_agentic(q["query"], **run_kwargs)
            rec = _record_from_agentic(q, out["answer"], out.get("chunks", []),
                                       out.get("sub_queries", []))
            records_by_id[q["id"]] = rec
            print(f"    ✓ {time.time()-t0:.0f}s | sub_queries={len(out.get('sub_queries', []))} "
                  f"| chunks={rec['agentic_n_chunks']} | answer_len={len(out['answer'])} "
                  f"| refusal={rec['is_refusal']}")
        except Exception as e:
            # agentic tool-call 偶發崩潰：記錄 error（下次重跑會重試此題），不中止整輪。
            records_by_id[q["id"]] = {
                "id": q["id"], "category": q["category"], "query": q["query"],
                "answerable": q.get("answerable", True),
                "answer": None, "contexts": [], "sources": [],
                "error": repr(e)[:400],
            }
            print(f"    ✗ ERROR {time.time()-t0:.0f}s: {repr(e)[:200]}")
        # 逐題落盤：崩潰不丟進度，重跑續補。
        _write(output, records_by_id, meta)

    done = sum(1 for r in records_by_id.values() if r.get("answer") and not r.get("error"))
    errs = [rid for rid, r in records_by_id.items() if r.get("error")]
    print(f"\n{'─'*60}")
    print(f"[DONE] 完成 {done}/{len(records_by_id)} 題落盤 → {output}")
    if errs:
        print(f"[WARN] {len(errs)} 題失敗（重跑同指令會自動重試）: {errs}")
    print(f"下一步（.venv-ragas）：\n"
          f"  .venv-ragas/Scripts/python.exe eval/eval_ragas_vs_rubric.py "
          f"--from-results {output} --output {output.parent}/ragas_scores.json")


if __name__ == "__main__":
    main()
