"""量 Grader 的 `relevant_ids` 圈選（**含 LLM，非零噪音**）。

**為什麼需要這一支**：`relevant_ids` 決定「這個子問題最後把哪些 chunk 交給 Generator」
（見 `agentic_rag_version._run_one_todo`：有圈選就只收圈選的，沒圈選才退回 rerank top-k 全收）。
它是**承接池的實際守門員**——比 `new_query` 更靠近答案。而它在 2026-08-28 之前完全沒有量尺：
`grep relevant_ids eval/*.py` 的每一筆命中都是測試樁裡的 `"relevant_ids": []`。

---

## 第一版的兩個洞（留著，因為它們是這支的判讀前提）

**① 公司層的陰性對照在自然候選池裡不存在。** `_check_sufficiency` 的 prompt 明寫「講的是別的
實體/公司的候選不要列入」，但實測八題的「未點名公司 chunk 數」**全部是 0**——因為 ticker hard
filter 在上游就濾掉了。
→ **這本身是關於 `relevant_ids` 的發現**：它在「排除別家公司」這件事上的邊際價值接近零，
   真正的工作是**同一家公司內的主題相關性**。所以陰性對照改用 `MIX`（下面）**合成**混池。

**② 「gold 檔的某些 chunk 沒被圈選」不是缺陷。** `eval_set.json` 的 gold 只到**檔名**，一個檔
有幾十個 chunk，排除同檔裡離題的那些**正是這個欄位該做的事**。第一版把它當危險指標，量出
「4 題危險」——那是量尺的錯不是系統的錯。現在只留 `all_gold_dropped`（gold 檔進了候選卻**整個**
被排除）這個不含糊的形狀。

---

## 四個指標，不可合併（同 `probe_temporal_interference.py` 的教訓）

| 指標 | 意思 | 代價 |
|---|---|---|
| `all_gold_dropped` | gold 檔的 chunk 進了候選，卻**一個都沒被圈選** | **危險**：答案失去唯一依據，且是靜默的 |
| `kept_offtopic` | 合成混池裡，**別家公司**的 chunk 被圈選 | **陰性對照**：少了它，「一律全選」也會零缺陷 |
| `select_rate` | 圈選數／候選數 | **1.00 ＝ 這一題等於沒過濾**（守門員在場但沒上工） |
| `empty_rate` | 一次都沒圈選 → 退回 top-k 全收 | 外觀與「全部都相關」**完全相同** |

⚠ **合成混池是刻意的，也是有代價的**：它犯了 CLAUDE.md〈量尺不可與被測物耦合〉形狀②的邊緣
（量尺自備輸入）。緩解方式是**兩半都走生產路徑**——兩次真實 `rq.retrieve()` ＋ 生產的
`ar._merge_chunks()`，腳本只負責決定「把哪兩個查詢的結果併起來」。而混池在生產確實會發生
（`_check_sufficiency` docstring 記載的 execute[1] 殘留雜訊就是），只是**不會在有 ticker filter
的單公司子問題上**發生。判讀時要記得這一格是合成的。

⚠ **N/A 不能併進 PASS**（同 `check_number_defects.py`）：gold 檔的 chunk 根本沒進候選時，
   這一題這一輪**沒有量到東西**。`lex-03` 兩條管線都撈不到 gold，它在這裡會恆為 N/A。

⚠ **這支不是閘門**（LLM 有噪音、MoE 不固定路由）。判讀看**逐題 k/n**，`--repeat` ≥3。

用法：
    RAG_COLLECTION=us_stock_rag_edgar_multiyear \\
      .venv/Scripts/python.exe -u eval/probe_relevant_ids.py --repeat 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq          # noqa: E402
import agentic_rag_version as ar     # noqa: E402

# 預設題目：刻意涵蓋四種形態。
#   · 單公司 × 撈得到 gold（sem-01 / mix-01 / mix-03 / col-05）
#   · 單公司 × 兩條管線都撈不到 gold（lex-03）→ 應該恆 N/A，那是這支的自我檢查
#   · 口語（col-10）→ 子問題措辭與 gold 用詞距離最遠
#   · 多公司（mh-01 / mh-04）→ 候選池天生就有多家，測「會不會把該留的擠掉」
DEFAULT_IDS = ["sem-01", "mix-01", "mix-03", "lex-03", "col-10", "col-05", "mh-01", "mh-04"]

# 陰性對照用的合成混池：{題id: (拿來混的查詢, 那個查詢的公司)}。
# 選法：**同一個問題換一家公司**，讓混進來的 chunk 在主題上像、在實體上錯——那才是這個欄位
# 唯一還有工作可做的地方（純粹離題的東西 rerank 本來就不會讓它進 top-5）。
MIX = {
    "sem-01": ("Apple 的核心技術護城河是什麼？", "AAPL"),
    "mix-01": ("Amazon AWS 最新一季的營收成長率是多少？", "AMZN"),
    "col-05": ("Tesla 的毛利率表現如何？", "TSLA"),
}


def _load_cases(ids: list[str]) -> list[dict]:
    raw = json.loads((Path(__file__).parent / "eval_set.json").read_text(encoding="utf-8"))
    qs = raw.get("queries", raw)
    by_id = {q["id"]: q for q in qs}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"eval_set.json 裡找不到：{missing}")
    return [by_id[i] for i in ids]


def _kn(vals: list, bad) -> str:
    """逐輪值壓成 k/n；None 一律算 N/A（**不可併進 OK**）。"""
    na = sum(1 for v in vals if v is None)
    if na == len(vals):
        return "N/A"
    k = sum(1 for v in vals if v is not None and bad(v))
    return f"{k}/{len(vals) - na}" + (f"+{na}NA" if na else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=3, help="跑幾輪（MoE 不固定路由，≥3 才看得出穩定度）")
    ap.add_argument("--ids", nargs="*", default=None)
    args = ap.parse_args()

    cases = _load_cases(args.ids or DEFAULT_IDS)

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rr = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    cl = rq.make_qdrant_client()

    print(f"collection = {rq.COLLECTION_NAME}   CHECKER_MODEL = {ar.CHECKER_MODEL}   "
          f"repeat = {args.repeat}   POOL_RETURN_K = {ar.POOL_RETURN_K}\n")

    rows = []
    for q in cases:
        qid, query = q["id"], q["query"]
        gold_files = set(q.get("relevant") or [])

        # 檢索只做一次（同一個池餵所有輪次）：要量的是 LLM 的圈選，不是檢索的抖動。
        pool = rq.retrieve(query, bge, rr, cl, top_k=ar.POOL_RETURN_K)[0] or []
        off_ticker = None
        if qid in MIX:
            mix_q, off_ticker = MIX[qid]
            other = rq.retrieve(mix_q, bge, rr, cl, top_k=ar.POOL_RETURN_K)[0] or []
            pool = ar._merge_chunks(pool, other)      # 生產的併池函式，不自己寫一份

        shown = pool[:ar.POOL_RETURN_K]
        shown_ids = {ar._chunk_id(c) for c in shown}
        gold_ids = {ar._chunk_id(c) for c in shown if c.get("source") in gold_files}
        off_ids = ({ar._chunk_id(c) for c in shown
                    if (c.get("source") or "").upper().startswith(off_ticker + "_")}
                   if off_ticker else set())

        all_dropped, kept_off, rates, empties = [], [], [], 0
        for _ in range(args.repeat):
            scope = ar._build_todo_temporal_scope(query, ar.FRESHNESS_SNAPSHOT)
            v = ar._check_sufficiency(query, shown, scope, ar.FRESHNESS_SNAPSHOT)
            sel = {s for s in (v.get("relevant_ids") or []) if s in shown_ids}
            empties += (not sel)
            rates.append(len(sel) / len(shown_ids) if shown_ids else 0.0)
            all_dropped.append(bool(gold_ids) and not (gold_ids & sel) if gold_ids else None)
            kept_off.append(len(sel & off_ids) > 0 if off_ids else None)

        rows.append({"id": qid, "n": len(shown_ids), "gold": len(gold_ids), "off": len(off_ids),
                     "all_dropped": all_dropped, "kept_off": kept_off,
                     "empty": empties, "rate": sum(rates) / len(rates) if rates else 0.0})

    print(f"{'題':<8}{'候選':>5}{'gold':>6}{'混入他家':>9}"
          f"{'gold全滅':>10}{'誤選他家':>10}{'空圈選':>8}{'圈選率':>8}")
    print("-" * 66)
    danger = neg_fail = n_na = no_filter = 0
    for r in rows:
        d = _kn(r["all_dropped"], bool)
        o = _kn(r["kept_off"], bool)
        n_na += (d == "N/A")
        danger += (d != "N/A" and not d.startswith("0/"))
        neg_fail += (o != "N/A" and not o.startswith("0/"))
        no_filter += (r["rate"] >= 0.999)
        empty_cell = "{}/{}".format(r["empty"], args.repeat)
        print(f"{r['id']:<8}{r['n']:>5}{r['gold']:>6}{r['off']:>9}"
              f"{d:>10}{o:>10}{empty_cell:>8}{r['rate']:>8.2f}")

    print()
    print(f"⚠ 危險（gold 檔進了候選卻整個被排除）：{danger} 題")
    print(f"⚠ 陰性對照（合成混池裡誤選他家）    ：{neg_fail} 題"
          f"{'' if any(r['off'] for r in rows) else '   ⚠ 沒有任何他家 chunk 進池＝這一格沒判別力'}")
    print(f"  N/A（gold 沒進候選＝這一題沒量到東西，不是通過）：{n_na} 題")
    print(f"  圈選率 1.00 的題（＝這一題等於沒過濾）：{no_filter}/{len(rows)}"
          f"   平均圈選率 {sum(r['rate'] for r in rows) / len(rows):.2f}")
    print(f"  空圈選：{sum(r['empty'] for r in rows)}/{len(rows) * args.repeat}"
          f"（空＝退回 rerank top-k 全收，外觀與『全部都相關』相同）")
    print("\n⚠ 這支是 probe 不是閘門：看逐題 k/n，不要看總分；下結論前 --repeat ≥3。")
    print("⚠ 「gold 檔的**部分** chunk 沒被圈選」刻意不算缺陷——gold 只到檔名，"
          "排除同檔裡離題的 chunk 正是這個欄位該做的事。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
