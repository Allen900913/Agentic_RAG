"""probe_historical_benefit.py — 多年語料**買到了什麼**？（零 LLM 判定、只讀 Qdrant ＋ reranker）

**為什麼有這支**：多年壓測（`probe_temporal_interference.py`、docs/EVAL.md〈多年語料的期別
干擾〉）量到的每一格都是**損害**——45 題掉 2 題、錯期率 0.000 → 0.044。**沒有任何量尺在量
收益**，於是「要不要把多年語料升生產」只有一半證據。這支補的是另一半。

**量什麼**：gold 只存在於舊年度 filing 的歷史題。單年 KB 結構性答不出來，多年 KB **有機會**
答得出來——「有機會」與「真的做到」中間隔著期別干擾，而那個差就是這裡唯一有資訊量的數字。

  · 單年臂的 `gold@k = 0` 是**定義使然不是量測**（檔案根本不在 collection 裡），不要拿它邀功。
  · 多年臂的 `gold@k` 才是量測：**語料裡有 ≠ 撈得到**。
  · 兩臂共同的「同節別期席位」＝ top-k 裡「同 ticker 同 filing_type 同 item_id 但不是 gold」
    的席位。單年臂上它就是「拿別的年份頂上去」的量——那是**有引用、看起來很有根據的錯答**，
    比空手而回更危險。

**gold 不寫死檔名**，由題庫的 `literal` 在 collection 裡確定性定位（見題庫 `_meta`）。這樣
「gold 是哪幾份」與前提檢查「這個事實在單年 KB 到底在不在」**是同一個操作**，不會各自漂移。

**前提檢查（這把尺的自我驗證）**三態，任何一格不符就大聲印出來：

  · `historical` 單年臂必須 absent、多年臂必須 present（多年臂 absent ＝ 題目無效 `void`）
  · `control`   兩臂都必須 present（少了它，「一律把舊年份排前面」也會在 historical 滿分）
  · 兩臂都 absent ＝ 這個事實根本不在語料裡，**不能算多年語料的收益**

⚠ **這支量的是檢索不是生成**。「單年 KB 遇到歷史題會拒答，還是拿別年份硬答」是生成端的
問題，要花 LLM ＋ 標準答案。拆法同 `probe_news_web_routing.py` 的 ①／②：便宜的必要條件先量
（本檔），貴的內容半另外做（見 BACKLOG）。

用法：

    # 單年臂（現生產）
    .venv/Scripts/python.exe -u eval/probe_historical_benefit.py
        --output experiments/benefit_probe_mdna.json

    # 多年臂（升生產的組態：修法 A 開）
    RAG_COLLECTION=us_stock_rag_edgar_multiyear RQ_PERIOD_INTENT_LLM=1
        .venv/Scripts/python.exe -u eval/probe_historical_benefit.py
        --output experiments/benefit_probe_multiyear_A.json

    # 對照兩份（同節別期席位是在這裡才算得出來的，見 cmd_compare）
    .venv/Scripts/python.exe eval/probe_historical_benefit.py --compare BEFORE.json AFTER.json

    # 只自測 literal 比對器（零 Qdrant、毫秒級）
    .venv/Scripts/python.exe eval/probe_historical_benefit.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 與 probe_temporal_interference.py 同一個理由：檢索路徑上唯一的 LLM 是英譯，
# 同輸入重跑實測 68/100 不同 → 釘死，讓兩臂的差異只剩 collection。
if "--no-replay-cache" not in sys.argv and not os.getenv("RAG_REPLAY_CACHE"):
    os.environ["RAG_REPLAY_CACHE"] = str(_ROOT / "eval" / "replay_cache.json")

# `literal_matcher` 2026-09-04 搬到 `eval/chunk_gold.py` 當唯一定義點——本檔與那支用的是
# 同一個判準（「literal 在 collection 裡落在哪」），維護兩份逐字相同的正則遲早會漂移。
# ⚠ 下面 `cmd_selftest` 的 10 條案例**刻意留在這裡**：它們現在測的是共用實作，
#   等於多一個獨立的呼叫端在守同一個零件（chunk_gold.py 自己也有一組，形狀不同）。
from chunk_gold import literal_matcher  # noqa: E402


def scan_docs(client, collection: str) -> list[dict]:
    """掃一次 collection。`rq.retrieve()` 的 chunk dict 沒有 item_id、也沒有 document 全文，
    而 gold 定位與分節鍵兩件事都需要它們（`build_chunk_meta` 因為同一個理由也掃一次）。"""
    out, off = [], None
    while True:
        pts, off = client.scroll(
            collection_name=collection, limit=1024, offset=off, with_vectors=False,
            with_payload=["document", "source", "chunk_index", "ticker",
                          "filing_type", "item_id", "report_period_code"])
        for p in pts:
            pl = p.payload or {}
            out.append({
                "document": pl.get("document") or "",
                "source": pl.get("source") or "",
                "chunk_index": pl.get("chunk_index") or 0,
                "ticker": (pl.get("ticker") or "").upper(),
                "filing_type": (pl.get("filing_type") or "").upper(),
                "item_id": pl.get("item_id") or "",
                "period_code": str(pl.get("report_period_code") or ""),
            })
        if off is None:
            break
    print(f"[INFO] 掃過 {len(out)} chunks")
    return out


def _section(d):
    """(ticker, filing_type, item_id)。同 `probe_temporal_interference._section_key`：
    只有 filing 有期別競爭的概念，News/Fundamentals 回 None。"""
    if not d or not d["item_id"] or d["filing_type"] not in ("10-K", "10-Q"):
        return None
    return (d["ticker"], d["filing_type"], d["item_id"])


def locate_gold(docs: list[dict], ticker: str, lit: str) -> tuple[list[str], list[list]]:
    """literal 在這個 collection 裡落在哪幾份 filing、哪幾節。⚠ 限定同 ticker：
    別家公司剛好有同一個數字是巧合，不是 gold。"""
    hit = literal_matcher(lit)
    srcs, secs = set(), set()
    for d in docs:
        if d["ticker"] == ticker and hit(d["document"]):
            srcs.add(d["source"])
            sk = _section(d)
            if sk is not None:
                secs.add(sk)
    return sorted(srcs), sorted(list(s) for s in secs)


def _cell(v, width=14):
    return f"{'—' if v is None else v:>{width}}"


def _load_queries(path: str) -> tuple[list, list]:
    """回 (有效題, 被標 void 的題)。

    ⚠ `void` 是 2026-08-27 加的，起因是**前提檢查漏掉重述**：它比對的是「某個值」在不在單年
    KB，而 10-K 會重述前一年的數字（改分部結構／會計政策）→ 同一個事實在新年報裡是**另一個
    值**，於是「原始值查無」卻「事實查得到」。bh-07／bh-16 就是這樣混進來的。
    **抓到它們的不是這支，是生成端那一輪**（`check_historical_generation.py`：單年臂把它們
    答出來了）→ 新增收益題之後**兩支都要跑**，值比對的前提檢查一個人擋不住。"""
    qs_all = json.loads(Path(path).read_text(encoding="utf-8"))["queries"]
    return [q for q in qs_all if not q.get("void")], [q for q in qs_all if q.get("void")]


def run_arm(args) -> int:
    qs, void = _load_queries(args.query_set)
    if void:
        print(f"⚠ 題庫標了 {len(void)} 題 `void`，排除不計："
              f"{[q['id'] for q in void]}（原因見題庫）")
    if args.limit:
        qs = qs[:args.limit]

    import llm_replay
    import rag_query as rq
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    client = rq.make_qdrant_client()
    print(f"[INFO] collection={rq.COLLECTION_NAME}  top_k={args.top_k}  題 {len(qs)}  "
          f"period_intent_llm={os.getenv('RQ_PERIOD_INTENT_LLM', '0')}  "
          f"replay_cache={os.getenv('RAG_REPLAY_CACHE', '(關)')}")
    docs = scan_docs(client, rq.COLLECTION_NAME)
    by_key = {(d["source"], d["chunk_index"]): d for d in docs}

    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)

    rows: dict = {}
    for q in qs:
        gold, gsec = locate_gold(docs, q["ticker"], q["literal"])
        # top_k=RERANK_INPUT_N：拿**完整精排序列**才量得到 gold_rank 這個連續量；
        # top-k 指標自己從 pool[:k] 切（同 probe_temporal_interference 的作法）。
        pool, _note = rq.retrieve(
            q["query"], bge, reranker, client, top_k=rq.RERANK_INPUT_N,
            enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
            translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN)
        goldset = set(gold)
        rank = next((i for i, c in enumerate(pool, 1) if c["source"] in goldset), None)
        topk = []
        for c in pool[:args.top_k]:
            d = by_key.get((c["source"], c["chunk_index"]))
            sk = _section(d)
            topk.append({
                "source": c["source"], "chunk_index": c["chunk_index"],
                "section": list(sk) if sk else None,
                "period_code": d["period_code"] if d else "",
            })
        rows[q["id"]] = {
            "category": q["category"], "ticker": q["ticker"], "query": q["query"],
            "literal": q["literal"], "kb_absent": not gold, "gold": gold,
            "gold_sections": gsec, "gold_rank": rank,
            "gold_in_topk": any(t["source"] in goldset for t in topk),
            "pool_size": len(pool), "topk": topk,
        }
        r = rows[q["id"]]
        mark = "KB 無此事實" if r["kb_absent"] else (
            f"gold@{args.top_k}={'Y' if r['gold_in_topk'] else 'N'} rank={rank} gold={gold}")
        print(f"  [{q['id']}/{q['category']:<10}] {mark}")

    # ── 前提檢查：這把尺的自我驗證（見 docstring）────────────────────────────
    hist = [r for r in rows.values() if r["category"] == "historical"]
    ctrl = [r for r in rows.values() if r["category"] == "control"]
    present_h = sum(not r["kb_absent"] for r in hist)
    present_c = sum(not r["kb_absent"] for r in ctrl)
    print("\n" + "=" * 72)
    print(f"collection = {rq.COLLECTION_NAME}")
    print(f"  historical {len(hist)} 題：KB 裡有這個事實 {present_h} 題")
    print(f"  control    {len(ctrl)} 題：KB 裡有這個事實 {present_c} 題")
    bad = [qid for qid, r in rows.items() if r["category"] == "control" and r["kb_absent"]]
    if bad:
        print(f"⚠ **control 題在這個 collection 上 KB 就沒有事實**：{bad} → 陰性對照失效，"
              "本輪數字不能當判定（先查題庫的 literal，再查 collection）。")
    if present_h:
        got = sum(r["gold_in_topk"] for r in hist if not r["kb_absent"])
        rk = [r["gold_rank"] for r in hist if not r["kb_absent"] and r["gold_rank"]]
        print(f"  historical 之中 KB 有事實的那 {present_h} 題：gold 進 top-{args.top_k} "
              f"{got} 題（{got / present_h:.3f}），gold_rank 中位 "
              f"{statistics.median(rk) if rk else '—'}")
        print("  ⚠ 這一格才是收益的量測。**語料裡有 ≠ 撈得到**——差額就是期別干擾吃掉的部分。")
    else:
        print(f"  historical 全部 KB 無此事實 → gold@{args.top_k} 恆為 0，"
              "那是**定義使然不是量測**。")
        print("  這一臂真正的資訊在 --compare 的「同節別期席位」那一列：它交回了什麼。")
    if llm_replay.enabled():
        print(f"[INFO] replay cache: {llm_replay.stats()}")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(
        {"meta": {"collection": rq.COLLECTION_NAME, "top_k": args.top_k,
                  "period_intent_llm": os.getenv("RQ_PERIOD_INTENT_LLM", "0"),
                  "queries": len(qs), "chunks": len(docs)},
         "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"寫入 {args.output}")
    return 0


def cmd_compare(before_p: str, after_p: str) -> int:
    """兩臂對照。

    ⚠ 「同節別期席位」**只能在這裡算**：它需要 gold 所在的那幾節，而單年臂上 gold 檔根本
    不存在 → 那一臂自己算必然是 0，那是量尺失效不是系統很好（`gold_sections_of` 在
    `probe_temporal_interference` 裡吃過同一個結構的虧）。所以「節」的定義一律取自**有
    gold 的那一臂**，兩臂共用。"""
    b = json.loads(Path(before_p).read_text(encoding="utf-8"))
    a = json.loads(Path(after_p).read_text(encoding="utf-8"))
    # 被標 void 的題一律排除——**既有結果檔裡還留著它們**，所以要在這裡濾，
    # 否則舊檔會繼續用失效的分母報數字（見 `_load_queries` 的說明）。
    _, voided = _load_queries(str(_ROOT / "eval" / "period_probe_benefit_queries.json"))
    vids = {q["id"] for q in voided}
    if vids:
        print(f"⚠ 排除 {len(vids)} 題 `void`：{sorted(vids)}（原因見題庫）")
    ids = [i for i in a["rows"] if i in b["rows"] and i not in vids]
    if len(ids) != len(a["rows"]) or len(ids) != len(b["rows"]):
        print(f"⚠ 兩份結果檔的題目不同（共同 {len(ids)} 題）→ 只比共同集；"
              "**分母變動過的兩份不可直接比**。")

    def seats(row, sections) -> int:
        secs = {tuple(s) for s in sections}
        goldset = set(row["gold"])
        return sum(1 for t in row["topk"]
                   if t["section"] and tuple(t["section"]) in secs
                   and t["source"] not in goldset)

    print("=" * 78)
    print(f"before = {b['meta']['collection']}   after = {a['meta']['collection']}"
          f"（修法 A={a['meta'].get('period_intent_llm', '?')}）")
    print("=" * 78)
    out, void = {}, []
    for cat in ("historical", "control"):
        sub = [i for i in ids if a["rows"][i]["category"] == cat]
        if not sub:
            continue
        agg = {"n": len(sub)}
        for tag, d in (("before", b), ("after", a)):
            rs = [d["rows"][i] for i in sub]
            have = [r for r in rs if not r["kb_absent"]]
            rk = [r["gold_rank"] for r in have if r["gold_rank"]]
            agg[f"{tag}_kb_present"] = len(have)
            agg[f"{tag}_gold_topk"] = sum(r["gold_in_topk"] for r in have)
            agg[f"{tag}_gold_rank_median"] = statistics.median(rk) if rk else None
            agg[f"{tag}_other_period_seats"] = sum(
                seats(d["rows"][i],
                      a["rows"][i]["gold_sections"] or b["rows"][i]["gold_sections"])
                for i in sub)
        out[cat] = agg
        if cat == "historical":
            void = [i for i in sub if a["rows"][i]["kb_absent"]]

        print(f"\n{cat}（{agg['n']} 題）".ljust(28) + _cell("before") + _cell("after"))
        print("-" * 78)
        for lab, key in (("KB 裡有這個事實", "kb_present"),
                         ("gold 進 top-k", "gold_topk"),
                         ("gold_rank 中位", "gold_rank_median"),
                         ("同節別期席位 合計", "other_period_seats")):
            print(f"{lab:<24}" + _cell(agg[f"before_{key}"]) + _cell(agg[f"after_{key}"]))

    h, c = out.get("historical", {}), out.get("control", {})
    print("\n" + "=" * 78)
    if void:
        print(f"⚠ **題目無效**：{void} 在 after 臂的 KB 裡也找不到事實 → 這幾題證明不了任何"
              "收益，先修題庫再看下面的數字。")
    if h.get("before_kb_present"):
        print(f"⚠ historical 有 {h['before_kb_present']} 題在 before 臂就找得到事實 → "
              "那些題**不是收益題**（單年 KB 本來就答得出來），會憑空灌水。")
    if c and c.get("after_gold_topk", 0) < c.get("before_gold_topk", 0):
        print(f"⚠ **陰性對照退步**：control 的 gold@k {c['before_gold_topk']} → "
              f"{c['after_gold_topk']} → 歷史題的收益是拿當期題換來的，**那是交換不是收益**。")
    elif c:
        print(f"✅ 陰性對照沒退步（control gold@k {c['before_gold_topk']} → "
              f"{c['after_gold_topk']}）→ 下面的收益不是拿當期題換來的。")
    if h.get("after_kb_present"):
        realized = h["after_gold_topk"] / h["after_kb_present"]
        print(f"\n收益（after 臂）：{h['after_kb_present']} 題語料裡有事實，其中 "
              f"{h['after_gold_topk']} 題 gold 真的進了 top-k → **兌現率 {realized:.3f}**。")
        print(f"before 臂不是空手：同節別期席位合計 {h['before_other_period_seats']} 席 → "
              "單年 KB 對這些題交回的是**同一節的別的年份**。")
        print("  ⚠ **不要把這一格讀成「所以答案是錯的」**：2026-08-27 的生成端量測"
              "（check_historical_generation.py）實測單年臂 **18/18 誠實承認、0 題硬答**。")
        print("  檢索把別年份端上來 ≠ 生成器會拿它充數。這裡量的是檢索層的風險，不是實害。")
    print("⚠ 以上全是檢索層。生成端會不會拒答、會不會拿別年份硬答，這支量不到（見 docstring）。")
    return 0


def cmd_selftest() -> int:
    """literal 比對器的雙向自測（零 Qdrant）。這是本檔唯一不平凡的確定性零件：
    gold 是哪幾份、前提檢查說不說得準，全靠它。"""
    cases = [
        ("1,353", "termination cost of $1,353 million", True, "數字型：正例"),
        ("1,353", "revenue was $21,353 million", False, "數字型：**左邊界**，21,353 不算命中"),
        ("1,353", "$1,3530", False, "數字型：右邊界"),
        ("1,353", "|$1,353|", True, "數字型：夾在表格分隔符之間仍算"),
        ("3.19", "DAP was 3.19 billion", True, "小數：正例"),
        ("3.19", "was 13.19 billion", False, "小數：左邊界"),
        ("3.19", "was 3.190 billion", False, "小數：右邊界（3.190 是別的數字）"),
        ("394,328", "Net sales | $416,161 | $391,035", False, "數字型：不在就是不在"),
        ("reduction in our workforce",
         "charges associated with the Reduction In Our Workforce.", True,
         "片語型：大小寫無關"),
        ("reduction in our workforce", "a reduction in headcount", False, "片語型：負例"),
    ]
    ok = 0
    for lit, text, want, why in cases:
        got = literal_matcher(lit)(text)
        ok += got == want
        print(f"[{'PASS' if got == want else 'FAIL'}] {why}  literal={lit!r} -> {got}")
    print(f"\n{ok}/{len(cases)} PASS")
    return 0 if ok == len(cases) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query-set",
                    default=str(_ROOT / "eval" / "period_probe_benefit_queries.json"))
    ap.add_argument("--output", default="experiments/benefit_probe.json")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 題（冒煙測試用）")
    ap.add_argument("--no-replay-cache", action="store_true", help="不掛重放快取（英譯會抖）")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return cmd_selftest()
    if args.compare:
        return cmd_compare(*args.compare)
    return run_arm(args)


if __name__ == "__main__":
    raise SystemExit(main())
