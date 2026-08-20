"""probe_multi_company_period.py — 「最新一季」路由在多公司題上失效，到底有沒有損害？

**背景**：[`rag_query._resolve_latest_quarter_filter`](../rag_query.py) 的第七道閘門是
`len(tickers) != 1 → 不路由`，所以兩家以上的比較題完全沒有期別保護。2026-08-14 量到
**eval_set 有 0 題受影響**（16 題通過前六道閘門的全是單一公司）→ 這個破口是推理出來的、
不是量出來的。本檔補上測資，決定要不要做（照 CLAUDE.md：機制宣稱要先有能證偽它的測試）。

**量什麼（零 LLM、確定性）**：問「最新一季」時，任何 10-Q chunk 只要期碼不是該公司
**自己的**最新期碼，就是撈錯期別。期碼從 source 檔名解（`AAPL_10Q_202603.html`），
不需要 payload、不需要 judge。指標兩個：
  ① `stale_rate` ＝ top-k 裡期別錯的 10-Q chunk 佔全部 10-Q chunk 的比例
  ② `companies_missing_latest` ＝ 題目點名、但 top-k 裡一個最新季 chunk 都沒有的公司數
     （②才是真損害：那家公司的「最新一季」根本答不出來）

**三個臂**（缺一不可）：
  `single_on`  16 題會觸發路由的單公司題，生產行為         → 陰性對照，期待 stale ≈ 0
  `single_off` 同 16 題，`RQ_LATEST_QUARTER_ROUTING=0`   → **陽性對照**，證明量尺有判別力
  `multi`      人工構造的多公司題，路由結構上不作用        → 被測項

⚠ 沒有陽性對照就不要看被測項的數字：`multi` 若量到 0，可能是真的沒事，也可能是量尺
   本身沒有判別力（CLAUDE.md：稽核腳本回傳「0 筆問題」先當壞消息查）。

用法：
    .venv/Scripts/python.exe -u eval/probe_multi_company_period.py \
        --output experiments/multi_company_period_probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

os.environ.pop("RAG_REPLAY_CACHE", None)   # 必須早於 import rag_query

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq  # noqa: E402

# source 檔名 → (ticker, 10K/10Q, 期碼)。這是 ingest 產生的固定格式，屬格式定義的封閉集合。
_SRC_RE = re.compile(r"^([A-Z]+)_(10K|10Q)_(\d+)\.", re.IGNORECASE)

# 人工構造的多公司「最新一季」比較題。刻意挑**最新期碼不同**的公司配對
# （AAPL 202606／MSFT 202603／NVDA 202604），因為那正是全域 OR（MatchAny）會漏的情境：
# 一家的最新季正好是另一家的舊季。
MULTI_QUERIES = [
    ("mc-01", "比較 Apple 和 Microsoft 最新一季的營收成長，哪一家比較快？"),
    ("mc-02", "NVIDIA 和 Tesla 最近一季的毛利率差多少？"),
    ("mc-03", "Microsoft 與 Google 最新一季的營業利益哪一家比較好？"),
    ("mc-04", "NVIDIA 跟 Apple 最近一季誰的淨利比較高？"),
    ("mc-05", "Amazon 和 Meta 最新一季的營收各是多少？"),
    ("mc-06", "比較 Apple、Microsoft、NVIDIA 最新一季的營收。"),
    ("mc-07", "Tesla 和 Amazon 目前這一季的銷售表現如何？"),
    ("mc-08", "Meta 與 Google 最新一季的營收成長差距有多大？"),
]


def _parse_source(src: str) -> tuple[str, str, str] | None:
    m = _SRC_RE.match(src or "")
    return (m.group(1).upper(), m.group(2).upper(), m.group(3)) if m else None


def _gate_report(query: str) -> tuple[bool, str]:
    """複刻 `_resolve_latest_quarter_filter` 的七道閘門，回傳 (是否只卡在公司數, 說明)。
    ⚠ 這是**抄寫不是 import**（原函式一路走到底才回傳，中途攔不下來）；改那邊要同步改這裡。"""
    q = query or ""
    for name, blocked in (
        ("無相對時間詞", not rq._LATEST_QUARTER_TIME_RE.search(q)),
        ("無季度/財報指標", not rq._QUARTER_METRIC_RE.search(q)),
        ("像新聞題", rq.looks_like_news_query(q)),
        ("是 TTM 口徑", bool(rq._detect_period_basis(q))),
        ("是年度意圖", bool(rq._ANNUAL_INTENT_RE.search(q))),
        ("自帶明確期碼", bool(rq._PERIOD_CODE_RE.search(q))),
    ):
        if blocked:
            return False, name
    n = len(rq._find_all_ticker_aliases(q.lower(), q))
    return (n != 1), (f"公司數={n}" + ("（→ 不路由）" if n != 1 else "（→ 有路由）"))


def _score(chunks: list[dict], asked: list[str], latest: dict) -> dict:
    """把一次檢索結果換算成兩個確定性指標。"""
    tenq = [p for c in chunks if (p := _parse_source(c.get("source", ""))) and p[1] == "10Q"]
    stale = [p for p in tenq if latest.get(p[0]) and p[2] != latest[p[0]]]
    got_latest = {p[0] for p in tenq if latest.get(p[0]) and p[2] == latest[p[0]]}
    missing = [t for t in asked if t in latest and t not in got_latest]
    # ⚠ 機制分類不能省：聚合的 missing_rate 說「有損害」，但**損害的成因決定該修哪裡**。
    #   A ＝ 那家公司有舊季 10-Q 卡在 top-k（同一家的新季被自己的舊季擠掉）→ 期別 filter 可修
    #   B ＝ 那家公司連一個 10-Q 都沒進 top-k（席位被別家吃光）→ 期別 filter 修不到，
    #        那是 `_ensure_ticker_coverage` 的範疇（它保底補的是最高分 chunk，
    #        不管 doc_type 也不管期別，實測補進來的是 News／Fundamentals）
    own_stale = {p[0] for p in stale}
    return {
        "n_10q": len(tenq),
        "n_stale": len(stale),
        "stale_rate": round(len(stale) / len(tenq), 4) if tenq else None,
        "stale_detail": [f"{t}@{c}(最新{latest.get(t)})" for t, _, c in stale],
        "companies_missing_latest": missing,
        "missing_kind_a": [t for t in missing if t in own_stale],
        "missing_kind_b": [t for t in missing if t not in own_stale],
        "sources": [c.get("source") for c in chunks],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-set", default="eval/eval_set.json")
    ap.add_argument("--output", default="experiments/multi_company_period_probe.json")
    ap.add_argument("--top-k", type=int, default=rq.DEFAULT_TOP_K)
    args = ap.parse_args(argv)

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    client = rq.make_qdrant_client()
    latest = rq._get_latest_10q_periods(client)
    print(f"[INFO] collection={rq.COLLECTION_NAME}  最新 10-Q 期碼={latest}")

    # 單公司對照組：直接從 eval_set 撈「通過前六道閘門且恰好一家公司」的題
    evalq = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))["queries"]
    singles = []
    for q in evalq:
        only_company, why = _gate_report(q["query"])
        if not only_company and "有路由" in why:
            singles.append((q["id"], q["query"]))
    print(f"[INFO] 單公司對照組 {len(singles)} 題：{[s[0] for s in singles]}")

    # 被測組閘門自檢：每一題都必須**只**卡在公司數這一關，否則測的不是這件事
    bad = [(i, _gate_report(s)[1]) for i, s in MULTI_QUERIES if not _gate_report(s)[0]]
    if bad:
        print(f"⚠ 這些構造題不是卡在公司數，量的不是這個破口：{bad}")
        return 2
    for i, s in MULTI_QUERIES:
        print(f"       {i} {_gate_report(s)[1]}  {s[:38]}")

    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)

    def _run(items, routing_on: bool) -> dict:
        os.environ["RQ_LATEST_QUARTER_ROUTING"] = "1" if routing_on else "0"
        out = {}
        for qid, qstr in items:
            asked = rq._find_all_ticker_aliases(qstr.lower(), qstr)
            chunks, _ = rq.retrieve(qstr, bge, reranker, client, top_k=args.top_k,
                                    enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                                    translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN)
            out[qid] = {"query": qstr, "asked": asked, **_score(chunks, asked, latest)}
            print(f"  [{qid}] 10-Q={out[qid]['n_10q']} 錯期={out[qid]['n_stale']} "
                  f"缺最新季的公司={out[qid]['companies_missing_latest']}")
        return out

    print("\n── arm: single_on（陰性對照）──")
    single_on = _run(singles, True)
    print("\n── arm: single_off（陽性對照）──")
    single_off = _run(singles, False)
    print("\n── arm: multi（被測項）──")
    multi = _run(MULTI_QUERIES, True)
    os.environ["RQ_LATEST_QUARTER_ROUTING"] = "1"

    def _agg(arm: dict) -> dict:
        n10 = sum(v["n_10q"] for v in arm.values())
        ns = sum(v["n_stale"] for v in arm.values())
        asked = sum(len([t for t in v["asked"] if t in latest]) for v in arm.values())
        miss = sum(len(v["companies_missing_latest"]) for v in arm.values())
        return {"queries": len(arm), "n_10q_chunks": n10, "n_stale": ns,
                "stale_rate": round(ns / n10, 4) if n10 else None,
                "companies_asked": asked, "companies_missing_latest": miss,
                "missing_rate": round(miss / asked, 4) if asked else None,
                "kind_a": sum(len(v["missing_kind_a"]) for v in arm.values()),
                "kind_b": sum(len(v["missing_kind_b"]) for v in arm.values())}

    aggs = {"single_on": _agg(single_on), "single_off": _agg(single_off), "multi": _agg(multi)}
    print("\n" + "=" * 82)
    print(f"{'arm':<14}{'題數':>6}{'10-Q chunks':>13}{'錯期':>7}{'錯期率':>9}"
          f"{'點名公司數':>11}{'缺最新季':>10}{'缺失率':>9}{'A/B':>9}")
    print("-" * 82)
    for k, a in aggs.items():
        print(f"{k:<14}{a['queries']:>6}{a['n_10q_chunks']:>13}{a['n_stale']:>7}"
              f"{(a['stale_rate'] if a['stale_rate'] is not None else -1):>9.3f}"
              f"{a['companies_asked']:>11}{a['companies_missing_latest']:>10}"
              f"{(a['missing_rate'] if a['missing_rate'] is not None else -1):>9.3f}"
              f"{a['kind_a']:>5}/{a['kind_b']:<3}")
    print("-" * 82)

    on, off, mu = aggs["single_on"], aggs["single_off"], aggs["multi"]
    if (off["stale_rate"] or 0) <= (on["stale_rate"] or 0) + 1e-9:
        print("⚠ 陽性對照沒有發作（關掉路由也沒有變差）→ **這個量尺沒有判別力**，")
        print("  被測項的數字不能採信。先查量尺，不要下結論。")
    elif (mu["missing_rate"] or 0) <= (on["missing_rate"] or 0) + 1e-9:
        print("判定：量尺有判別力，但**多公司題沒有量到損害** → 寫進 BACKLOG 已接受的極限。")
    elif mu["kind_a"] == 0:
        print(f"判定：多公司題**有損害**（缺失率 {mu['missing_rate']} vs 單公司 {on['missing_rate']}），")
        print(f"  但 {mu['kind_b']} 件全是 kind B（那家公司連一個 10-Q 都沒進 top-k）、kind A 為 0")
        print("  → **期別 filter（OR-of-ANDs）直接修不到任何一件**。病灶是席位競爭，")
        print("    該看的是 `_ensure_ticker_coverage`（它補的是最高分 chunk，不管 doc_type／期別）。")
        print("  ⚠ 唯一還沒排除的可能：舊季 chunk 佔走的席位若被期別 filter 釋放，遞補上來的")
        print("    會不會正好是缺席公司的最新季 10-Q——那要真的做出 filter 臂才知道，不能推論。")
    else:
        print(f"判定：多公司題量到損害，其中 kind A {mu['kind_a']} 件是同一家的舊季擠掉新季")
        print("  → 期別 filter（OR-of-ANDs per-ticker）修得到那幾件，值得做。")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(
        {"meta": {"collection": rq.COLLECTION_NAME, "top_k": args.top_k, "latest": latest},
         "agg": aggs,
         "arms": {"single_on": single_on, "single_off": single_off, "multi": multi}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"寫入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
