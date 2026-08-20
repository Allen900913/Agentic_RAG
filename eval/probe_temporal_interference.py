"""probe_temporal_interference.py — 加入舊年度 filing 之後，檢索的期別損害有多大？

**背景**：KB 目前只有**一年**（每家 1 份 10-K ＋ 2 份 10-Q，共 21 份）。這代表「答錯期別」
在現有資料上結構性地幾乎不可能發生，而**現有的所有評測分數都是在這個保護傘下拿到的**。
`BACKLOG.md` 把這件事記成「現在不可證偽」，復活條件是「KB 納入第二個年度的 filing 時」。
本檔就是那個復活條件要用的量尺。

⚠ **順序不可顛倒**：必須先在**乾淨的單年 collection** 上跑一次取得 before 基準。資料一旦
灌進去就再也回不到單年狀態，順序錯了就永遠量不到 delta。

**量什麼（三種互相獨立的損害，不可合併成一個數字）**——這是 `probe_multi_company_period.py`
那次「kind A vs kind B」教訓的直接套用：只看聚合率會把 F1 誤診成 F2，而兩者該修的地方不同。

  F1 期別排擠 `crowding`      top-k 被同一節內容的多個年份佔滿 → 席位浪費，答案可能還對
  F2 期別錯選 `wrong_period`  對的期別沒進 top-k、錯的期別進來了 → **答錯且看起來有根據**
  ——  連續量 `gold_rank`      gold chunk 在完整精排序列裡的名次；不被 top-k 截斷藏住，最靈敏

另外報一個**純資料層**的量 `sibling_density`：gold 所屬的 (ticker, filing_type, item_id) 這一節
在 collection 裡有幾份「不同 filing」的兄弟。它與檢索無關，是**競爭密度本身**——加了兩年舊資料
之後它必然上升，F1/F2 有沒有跟著上升才是這次要回答的問題。

**分組鍵是 `(ticker, filing_type, item_id)`**，不是檔名前綴：payload 有 `item_id`（實查），
所以「同一節、不同年份」可以確定性地認出來，不必用字串相似度猜（那條路 2026-08-14 實測
分離度是負的，見 docs/AGENTIC.md A7）。同一份 filing 的同一節本來就會有多個 chunk
（`item_chunk_index`），所以**排擠只算「來自不同 filing 的席位」**，不然會把正常的多 chunk
誤判成排擠。

**兩個臂**：
  `filter_on`   生產行為                              → 被測項
  `filter_off`  `RQ_LATEST_QUARTER_ROUTING=0`         → **陽性對照**，證明量尺有判別力
⚠ 沒有陽性對照就不要看被測項的數字（CLAUDE.md：稽核腳本回傳「0 筆問題」先當壞消息查）。
只有「路由真的會觸發」的題才跑第二臂（直接呼叫 `rq._resolve_latest_quarter_filter` 判定，
**是 import 不是抄寫**）；其餘題兩臂結果相同，直接沿用，不白燒一次 rerank。

**gold 怎麼展開**：對照 `eval/period_probe_baseline.json`（灌舊資料前的 21 份快照），
不是對照當下的 manifest。理由見那份檔案的 `_meta`——eval_set 有 4 題的 gold 是萬用字元，
照當下 manifest 展開會讓它們「撈到哪一年都算命中」。**eval_set.json 一個字都不用改。**

**LLM 與噪音**：檢索路徑上唯一的 LLM 是 `translate_query_to_english`（實測同輸入重跑
68/100 不同）。預設掛 `RAG_REPLAY_CACHE` 把它釘死，讓 before/after 兩次跑的差異只剩語料。
⚠ `llm_replay.py` 的 docstring 警告「A/B 是加了新文件的 collection 時 fixture 有方向不明的
偏誤」——**那條警告針對的是 `plan`**（planner 的 system prompt 內嵌 coverage snapshot，
換 collection 就換 prompt）。本檔只用 `translate_en`，它的 key 是 query 文字、與語料無關，
所以那個偏誤在這裡不成立。

用法：
    # before（乾淨的單年 collection）
    .venv/Scripts/python.exe -u eval/probe_temporal_interference.py \
        --output experiments/temporal_probe_baseline.json
    # after（灌了舊年度的新 collection）
    RAG_COLLECTION=us_stock_rag_edgar_multiyear .venv/Scripts/python.exe -u \
        eval/probe_temporal_interference.py --output experiments/temporal_probe_multiyear.json
    # 對照兩份
    .venv/Scripts/python.exe eval/probe_temporal_interference.py --compare A.json B.json
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 預設掛上重放快取（見上方 docstring）。要關掉跑純 live 用 --no-replay-cache，
# 但那樣 before/after 兩次跑的英譯會不同，delta 裡會混進與語料無關的變動。
if "--no-replay-cache" not in sys.argv and not os.getenv("RAG_REPLAY_CACHE"):
    os.environ["RAG_REPLAY_CACHE"] = str(_ROOT / "eval" / "replay_cache.json")

import llm_replay  # noqa: E402
import rag_query as rq  # noqa: E402

# ── 期間意圖分類（確定性，零 LLM）────────────────────────────────────────────
# 為什麼要分類再報數字：「Apple 最新一季營收」與「Apple FY2024 營收」壞掉的成因不同、
# 修法也不同（前者是相對指稱解析錯，後者是絕對期間 filter 沒生效）。合併成一個數字會
# 把兩件事攪在一起。第三類 `none`（問題根本沒提期間）**本來就沒有唯一正確答案**，
# 它的 gold miss 不代表系統壞掉——報出來但不併進判定。
_YEAR_RE = re.compile(r"\b20\d{2}\b|\bFY\s?20\d{2}\b|20\d{2}\s*年", re.IGNORECASE)


def classify_period_intent(q: str) -> str:
    if rq._PERIOD_CODE_RE.search(q) or _YEAR_RE.search(q):
        return "explicit"
    if rq._LATEST_QUARTER_TIME_RE.search(q):
        return "relative"
    return "none"


def build_chunk_meta(client) -> dict:
    """掃一次 collection，建 {(source, chunk_index): payload 子集}。

    存在理由：`rq.retrieve()` 回傳的 chunk dict 只帶 source/chunk_index/ticker/chunk_type，
    **沒有 item_id 也沒有 report_period_code**，而這支探針的分組鍵正好需要它們。
    （agentic 的 `_scan_kb_coverage` 因為同一個理由也自己掃了一次。）"""
    meta: dict = {}
    offset = None
    while True:
        pts, offset = client.scroll(
            collection_name=rq.COLLECTION_NAME, limit=512, offset=offset,
            with_payload=["source", "chunk_index", "ticker", "filing_type",
                          "item_id", "report_period_code", "doc_type"],
            with_vectors=False)
        for p in pts:
            pl = p.payload or {}
            meta[(pl.get("source", ""), pl.get("chunk_index", 0))] = {
                "ticker": (pl.get("ticker") or "").upper(),
                "filing_type": (pl.get("filing_type") or "").upper(),
                "item_id": pl.get("item_id") or "",
                "period_code": str(pl.get("report_period_code") or ""),
                "doc_type": (pl.get("doc_type") or "").lower(),
            }
        if offset is None:
            break
    print(f"[INFO] chunk meta：{len(meta)} chunks")
    return meta


def _section_key(m) -> tuple | None:
    """(ticker, filing_type, item_id) — 只有 filing 有 item_id；News/Fundamentals 回 None
    （它們沒有期別競爭的概念，硬塞進來只會製造假的排擠訊號）。"""
    if not m or not m["item_id"] or m["filing_type"] not in ("10-K", "10-Q"):
        return None
    return (m["ticker"], m["filing_type"], m["item_id"])


def gold_sections_of(gold: set, meta: dict) -> set:
    """gold 這幾份 filing 涵蓋到的所有 (ticker, filing_type, item_id)。"""
    return {sk for (src, _ci), m in meta.items()
            if src in gold and (sk := _section_key(m)) is not None}


def score_one(pool: list, gold: set, meta: dict, k: int, gold_sections: set) -> dict:
    """把一次檢索結果換算成三個確定性指標。pool 需為完整精排降序。"""
    topk = pool[:k]

    # ── gold 的名次（連續量，不被 top-k 截斷藏住）──────────────────────────
    gold_rank = next((i for i, c in enumerate(pool, 1) if c["source"] in gold), None)

    # ── gold 這一節在 collection 裡有幾份「不同 filing」的兄弟（純資料層）──
    sib = defaultdict(set)
    for (src, _ci), m in meta.items():
        sk = _section_key(m)
        if sk is not None and sk in gold_sections:
            sib[sk].add(src)
    sibling_density = max((len(v) for v in sib.values()), default=0)

    # ── F1 期別排擠：同一節、來自不同 filing 的席位就是浪費 ────────────────
    groups: dict = defaultdict(list)
    for i, c in enumerate(topk):
        sk = _section_key(meta.get((c["source"], c["chunk_index"])))
        if sk is not None:
            groups[sk].append(i)
    crowding_seats, crowding_detail = 0, []
    for sk, idxs in groups.items():
        srcs = [topk[i]["source"] for i in idxs]
        keep = srcs[0]                       # 名次最高的那份 filing 是「正主」
        wasted = [s for s in srcs if s != keep]
        if wasted:
            crowding_seats += len(wasted)
            crowding_detail.append(
                f"{sk[0]} {sk[2]}: 留 {keep}，被 {sorted(set(wasted))} 佔 {len(wasted)} 席")

    # ── F2 期別錯選：gold 沒進 top-k，但「同一節的別份 filing」進來了 ───────
    gold_in_topk = any(c["source"] in gold for c in topk)
    intruders = sorted({c["source"] for c in topk
                        if _section_key(meta.get((c["source"], c["chunk_index"]))) in gold_sections
                        and c["source"] not in gold})
    return {
        "gold_rank": gold_rank,
        "gold_in_topk": gold_in_topk,
        "sibling_density": sibling_density,
        "crowding_seats": crowding_seats,
        "crowding_detail": crowding_detail,
        "wrong_period": (not gold_in_topk) and bool(intruders),
        "wrong_period_intruders": intruders,
        "topk_sources": [f"{c['source']}#{c['chunk_index']}" for c in topk],
    }


def score_trend(pool: list, ticker: str, meta: dict, k: int) -> dict:
    """趨勢題（陰性對照）的指標：top-k 裡屬於該公司、來自 filing 的**相異期碼**個數。

    ⚠ 為什麼另外寫一個指標，不沿用 crowding：**collapse 的規則與 crowding 的定義是同一個**
    （同一 `(ticker, filing_type, item_id)` 只留一份 filing）→ collapse 一開，crowding 必然
    歸零。那是套套邏輯不是證據。真正要問的是「它弄壞了什麼」，而那個方向只有這個指標量得到。
    """
    topk = pool[:k]
    periods, per_kind = set(), {}
    for c in topk:
        m = meta.get((c["source"], c["chunk_index"]))
        if not m or m["ticker"] != ticker or m["filing_type"] not in ("10-K", "10-Q"):
            continue
        periods.add(m["period_code"])
        per_kind.setdefault(m["filing_type"], set()).add(m["period_code"])
    return {
        "periods_covered": len(periods),
        "periods": sorted(periods),
        "by_kind": {k2: sorted(v) for k2, v in per_kind.items()},
        "own_filing_seats": sum(
            1 for c in topk
            if (m := meta.get((c["source"], c["chunk_index"])))
            and m["ticker"] == ticker and m["filing_type"] in ("10-K", "10-Q")),
        "topk_sources": [f"{c['source']}#{c['chunk_index']}" for c in topk],
    }


def _agg(arm: dict, intents: dict, k: int) -> dict:
    """分意圖類別聚合。⚠ `none` 類單獨報、不併進判定（見 classify_period_intent）。"""
    out = {}
    for label in ("explicit", "relative", "none", "ALL"):
        rows = [v for qid, v in arm.items() if label == "ALL" or intents[qid] == label]
        if not rows:
            continue
        ranks = sorted(r["gold_rank"] for r in rows if r["gold_rank"] is not None)
        out[label] = {
            "queries": len(rows),
            "gold_in_topk": sum(r["gold_in_topk"] for r in rows),
            "gold_recall": round(sum(r["gold_in_topk"] for r in rows) / len(rows), 4),
            "gold_rank_median": (ranks[len(ranks) // 2] if ranks else None),
            "gold_never_ranked": sum(r["gold_rank"] is None for r in rows),
            "crowding_seats": sum(r["crowding_seats"] for r in rows),
            "crowding_rate": round(sum(r["crowding_seats"] for r in rows) / (len(rows) * k), 4),
            "wrong_period": sum(r["wrong_period"] for r in rows),
            "wrong_period_rate": round(sum(r["wrong_period"] for r in rows) / len(rows), 4),
            "sibling_density_max": max(r["sibling_density"] for r in rows),
            "sibling_density_mean": round(
                sum(r["sibling_density"] for r in rows) / len(rows), 2),
        }
    return out


def _print_table(title: str, aggs: dict) -> None:
    print("\n" + "=" * 96)
    print(title)
    print(f"{'arm / intent':<26}{'題數':>6}{'gold@k':>9}{'rank中位':>10}{'池外':>6}"
          f"{'排擠席位':>10}{'排擠率':>9}{'錯期':>7}{'錯期率':>9}{'兄弟密度':>10}")
    print("-" * 96)
    for arm, per in aggs.items():
        for label, a in per.items():
            med = a["gold_rank_median"] if a["gold_rank_median"] is not None else -1
            print(f"{arm + ' / ' + label:<26}{a['queries']:>6}{a['gold_recall']:>9.3f}"
                  f"{med:>10}{a['gold_never_ranked']:>6}{a['crowding_seats']:>10}"
                  f"{a['crowding_rate']:>9.3f}{a['wrong_period']:>7}"
                  f"{a['wrong_period_rate']:>9.3f}{a['sibling_density_mean']:>10.2f}")
    print("-" * 96)
    print("「池外」＝ gold 檔連候選池都沒進去。⚠ 那**不是期別損害**——實測 mi-01／mi-14 是")
    print("  multi-intent 題被 `looks_like_news_query` 硬路由到 doc_type=news，filing gold 在")
    print("  候選池階段就被排除。它只拖低 gold@k；排擠與錯期兩個指標都需要同節 chunk 在池裡，")
    print("  所以不受污染，且 before/after 同樣被排除 → Δ 仍然有效。")


def cmd_compare(before_p: str, after_p: str) -> int:
    b = json.loads(Path(before_p).read_text(encoding="utf-8"))
    a = json.loads(Path(after_p).read_text(encoding="utf-8"))
    print(f"before: {b['meta']['collection']}   after: {a['meta']['collection']}")
    _print_table("before", b["agg"])
    _print_table("after", a["agg"])
    print("\nΔ（after − before，filter_on 臂）")
    print(f"{'intent':<14}{'Δgold@k':>10}{'Δ排擠率':>10}{'Δ錯期率':>10}{'Δ兄弟密度':>12}")
    print("-" * 56)
    for label in ("explicit", "relative", "none", "ALL"):
        bb = b["agg"].get("filter_on", {}).get(label)
        aa = a["agg"].get("filter_on", {}).get(label)
        if not bb or not aa:
            continue
        print(f"{label:<14}{aa['gold_recall'] - bb['gold_recall']:>+10.3f}"
              f"{aa['crowding_rate'] - bb['crowding_rate']:>+10.3f}"
              f"{aa['wrong_period_rate'] - bb['wrong_period_rate']:>+10.3f}"
              f"{aa['sibling_density_mean'] - bb['sibling_density_mean']:>+12.2f}")
    print("-" * 56)
    print("判讀：兄弟密度必然上升（那是資料層事實）。要看的是**排擠率與錯期率有沒有跟著上升**——")
    print("  沒跟著上升 ⇒ 檢索扛得住，可以考慮升生產；跟著上升 ⇒ 照 F1/F2 的比例決定修哪一條。")
    return 0


def cmd_collapse_sweep(args) -> int:
    """一次檢索、掃出 collapse 的多個 keep 值。

    **為什麼一次就夠**：`_collapse_cross_period_sections` 是**純粹的 rerank 後處理**，
    沒有任何回饋進檢索（不改 query、不改 filter、不改候選池）。所以拿一次完整精排序列，
    在探針這邊逐一套用不同 keep 值，結果與「每個 keep 各跑一次 retrieve」**逐位元相同**，
    但省掉 N−1 次 embedding＋rerank（實測一輪 49 題約 1.2 小時）。
    ⚠ 前提是 retrieve 那邊 collapse 必須**關著**，否則會 collapse 兩次。
    """
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))["filings"]
    evalq = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))["queries"]
    items = []
    for q in evalq:
        pats = [r for r in (q.get("relevant") or []) if r.endswith(".html")]
        if not pats:
            continue
        gold = {f for p in pats for f in baseline if fnmatch.fnmatch(f, p)}
        if gold:
            items.append((q["id"], q["query"], gold))
    if args.limit:
        items = items[:args.limit]
    intents = {qid: classify_period_intent(qs) for qid, qs, _ in items}

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    client = rq.make_qdrant_client()
    os.environ["RAG_CROSS_PERIOD_COLLAPSE"] = "0"   # 見上方 docstring：retrieve 端必須關著
    print(f"[INFO] collection={rq.COLLECTION_NAME}  {len(items)} 題  top_k={args.top_k}")
    meta = build_chunk_meta(client)
    gsec = {qid: gold_sections_of(gold, meta) for qid, _q, gold in items}
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)

    # (臂名, keep, prefer)。`prefer` 這一維是實測逼出來的，不是設計時想到的：k1/rank 會把
    # gold 刪掉（sem-03／sem-04 的 gold 在組內不是 rerank 最高 → 留下錯的年份、gold_rank
    # 從 3 變成 None）。見 `rq._collapse_cross_period_sections` 的 prefer 註解。
    ARMS = [("collapse_off", 0, "rank"),
            ("k1_rank", 1, "rank"), ("k1_newest", 1, "newest"),
            ("k2_rank", 2, "rank"), ("k2_newest", 2, "newest")]
    arms: dict = {name: {} for name, _, _ in ARMS}
    pools: dict = {}
    for qid, qstr, gold in items:
        pool, _ = rq.retrieve(qstr, bge, reranker, client, top_k=rq.RERANK_INPUT_N,
                              enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                              translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN)
        # ⚠ 把完整候選池存下來：每加一個臂就重跑一次 1.2 小時的檢索是純浪費，而且
        #   「只存結論不存中間產物」正是 memory `eval-measurement-pitfalls` 記載的坑
        #   （這一輪就因為臨時要加 prefer 這一維而重跑過一次）。
        pools[qid] = [{kk: c.get(kk) for kk in
                       ("source", "chunk_index", "item_id", "filing_type",
                        "period_code", "fiscal_rank", "ticker")} for c in pool]
        line = []
        for name, k, prefer in ARMS:
            collapsed = pool if k == 0 else \
                rq._collapse_cross_period_sections(pool, k, prefer=prefer)[0]
            r = score_one(collapsed, gold, meta, args.top_k, gsec[qid])
            r.update({"query": qstr, "intent": intents[qid], "gold": sorted(gold)})
            arms[name][qid] = r
            line.append(f"{name}:{r['gold_rank']}/{r['crowding_seats']}")
        print(f"  [{qid}/{intents[qid]}] " + " ".join(line))

    aggs = {name: _agg(arm, intents, args.top_k) for name, arm in arms.items()}
    _print_table(f"collapse sweep — {rq.COLLECTION_NAME}", aggs)
    print("\n⚠ **排擠率不是收益證據**：collapse 的規則與排擠席位的定義是同一個，開了必然下降，")
    print("  那是套套邏輯。收益看 **gold@k**（讓出來的席位換到了什麼），損害看 --trend。")
    print(f"\n{'intent':<12}" + "".join(f"{('gold@k ' + n):>16}" for n in aggs))
    for label in ("explicit", "relative", "none", "ALL"):
        row = f"{label:<12}"
        for n in aggs:
            a = aggs[n].get(label)
            row += f"{(a['gold_recall'] if a else float('nan')):>16.3f}"
        print(row)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(
        {"meta": {"collection": rq.COLLECTION_NAME, "top_k": args.top_k,
                  "arms": [a[0] for a in ARMS], "queries": len(items)},
         "agg": aggs, "arms": arms, "pools": pools},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"寫入 {args.output}")
    return 0


def cmd_trend(args) -> int:
    """跨期 collapse 的陰性對照：本來就需要多個期別的題，collapse 開了會掉多少期。"""
    qs = json.loads(Path(args.trend_set).read_text(encoding="utf-8"))["queries"]
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    client = rq.make_qdrant_client()
    print(f"[INFO] collection={rq.COLLECTION_NAME}  top_k={args.top_k}  趨勢題 {len(qs)} 題")
    meta = build_chunk_meta(client)
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)

    # 與 cmd_collapse_sweep 同一個技巧：collapse 是純 rerank 後處理，一次檢索就能掃全部臂。
    os.environ["RAG_CROSS_PERIOD_COLLAPSE"] = "0"       # retrieve 端必須關著，否則 collapse 兩次
    ARMS = [("collapse_off", 0, "rank"),
            ("k1_rank", 1, "rank"), ("k1_newest", 1, "newest"),
            ("k2_rank", 2, "rank"), ("k2_newest", 2, "newest")]
    out: dict = {name: {} for name, _, _ in ARMS}
    for q in qs:
        pool, _ = rq.retrieve(q["query"], bge, reranker, client, top_k=rq.RERANK_INPUT_N,
                              enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                              translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN)
        line = []
        for name, k, prefer in ARMS:
            collapsed = pool if k == 0 else \
                rq._collapse_cross_period_sections(pool, k, prefer=prefer)[0]
            r = score_trend(collapsed, q["ticker"], meta, args.top_k)
            r["query"] = q["query"]
            out[name][q["id"]] = r
            line.append(f"{name}:{r['periods_covered']}")
        print(f"  [{q['id']}/{q['ticker']}] " + " ".join(line))
    os.environ.pop("RAG_CROSS_PERIOD_COLLAPSE", None)

    print("\n" + "=" * 78)
    print(f"{'id':<8}" + "".join(f"{n:>14}" for n, _, _ in ARMS))
    print("-" * 78)
    tot = {n: 0 for n, _, _ in ARMS}
    for qid in out["collapse_off"]:
        row = f"{qid:<8}"
        for n, _, _ in ARMS:
            v = out[n][qid]["periods_covered"]
            tot[n] += v
            row += f"{v:>14}"
        print(row)
    print("-" * 78)
    base = tot["collapse_off"]
    print(f"{'合計':<8}" + "".join(f"{tot[n]:>14}" for n, _, _ in ARMS))
    print(f"{'Δ%':<8}" + "".join(
        f"{((tot[n] - base) / base * 100 if base else 0):>13.0f}%" for n, _, _ in ARMS))
    print("\n判讀：這是**損害的上界**不是實害——單一份 10-K 的 MD&A 本來就含跨年比較，")
    print("  少一個期碼不等於答不出來。要與收益（cmd_collapse_sweep 的 gold@k）一起看：")
    print("  同一個臂在兩張表上的位置決定它值不值得。")

    out_p = args.output.replace(".json", "_trend.json") if args.output.endswith(".json") \
        else args.output + "_trend.json"
    Path(out_p).parent.mkdir(parents=True, exist_ok=True)
    Path(out_p).write_text(json.dumps(
        {"meta": {"collection": rq.COLLECTION_NAME, "top_k": args.top_k},
         "arms": {"collapse_off": off, "collapse_on": on}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"寫入 {out_p}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-set", default=str(_ROOT / "eval" / "eval_set.json"))
    ap.add_argument("--baseline", default=str(_ROOT / "eval" / "period_probe_baseline.json"))
    ap.add_argument("--output", default="experiments/temporal_probe.json")
    ap.add_argument("--top-k", type=int, default=rq.DEFAULT_TOP_K)
    ap.add_argument("--no-replay-cache", action="store_true", help="不掛重放快取（英譯會抖）")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--report", metavar="RESULT_JSON",
                    help="只把既有結果檔的表格重印一次（不跑檢索、不載模型）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 題（冒煙測試用）")
    ap.add_argument("--trend", action="store_true",
                    help="只跑跨期陰性對照（趨勢題，8 題，快）。量 periods_covered@k，"
                         "collapse on/off 兩臂")
    ap.add_argument("--trend-set", default=str(_ROOT / "eval" / "period_probe_trend_queries.json"))
    ap.add_argument("--collapse-sweep", action="store_true",
                    help="一次檢索掃出 collapse 的多個 keep 值（見 cmd_collapse_sweep）")
    args = ap.parse_args(argv)

    if args.compare:
        return cmd_compare(*args.compare)
    if args.report:
        d = json.loads(Path(args.report).read_text(encoding="utf-8"))
        _print_table(f"collection = {d['meta']['collection']}"
                     f"（{d['meta']['queries']} 題，意圖 {d['meta']['intents']}）", d["agg"])
        return 0

    if args.trend:
        return cmd_trend(args)
    if args.collapse_sweep:
        return cmd_collapse_sweep(args)

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))["filings"]
    evalq = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))["queries"]

    items = []
    for q in evalq:
        pats = [r for r in (q.get("relevant") or []) if r.endswith(".html")]
        if not pats:
            continue
        gold = {f for p in pats for f in baseline if fnmatch.fnmatch(f, p)}
        if not gold:
            print(f"⚠ {q['id']} 的 gold 在 baseline 快照裡展開成空集合，跳過：{pats}")
            continue
        items.append((q["id"], q["query"], gold))
    if args.limit:
        items = items[:args.limit]
    intents = {qid: classify_period_intent(qs) for qid, qs, _ in items}
    dist = {lab: sum(v == lab for v in intents.values())
            for lab in ("explicit", "relative", "none")}
    print(f"[INFO] filing 類題目 {len(items)} 題；期間意圖分佈：{dist}")

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    client = rq.make_qdrant_client()
    print(f"[INFO] collection={rq.COLLECTION_NAME}  top_k={args.top_k}  "
          f"replay_cache={os.getenv('RAG_REPLAY_CACHE', '(關)')}")
    meta = build_chunk_meta(client)
    gsec = {qid: gold_sections_of(gold, meta) for qid, _qs, gold in items}
    empty = [qid for qid, s in gsec.items() if not s]
    if empty:
        print(f"⚠ 這些題的 gold 檔在 collection 裡找不到任何帶 item_id 的 chunk："
              f"{empty} → 它們的排擠/錯期恆為 0，是量尺盲區不是系統很好")

    # 哪些題「最新一季」路由真的會觸發 → 只有這些題的 filter_off 臂才有意義。
    # ⚠ 這是 import 生產判斷式，不是抄寫。
    routed = {qid for qid, qs, _ in items if rq._resolve_latest_quarter_filter(qs, client)}
    print(f"[INFO] 會觸發最新一季路由的題 {len(routed)} 題：{sorted(routed)}")

    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)

    def _run(subset, routing_on: bool) -> dict:
        os.environ["RQ_LATEST_QUARTER_ROUTING"] = "1" if routing_on else "0"
        out = {}
        for qid, qstr, gold in subset:
            # top_k=RERANK_INPUT_N：拿**完整精排序列**而不是 top-5，才量得到 gold_rank
            # 這個連續量。top-k 指標自己從 pool[:k] 切。⚠ 副作用：`_ensure_ticker_coverage`
            # 在 top_k 這麼大時等於不作用，所以多公司題的 pool[:k] 會與生產 top-k 略有
            # 出入——filing 類題目絕大多數是單一公司（`want<=1` 本來就原樣回傳），
            # 且本檔量的是期別不是公司覆蓋，這個出入不影響結論。
            chunks, _note = rq.retrieve(
                qstr, bge, reranker, client, top_k=rq.RERANK_INPUT_N,
                enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN)
            r = score_one(chunks, gold, meta, args.top_k, gsec[qid])
            r.update({"query": qstr, "intent": intents[qid], "gold": sorted(gold)})
            out[qid] = r
            print(f"  [{qid}/{intents[qid]}] gold_rank={r['gold_rank']} "
                  f"排擠={r['crowding_seats']} 錯期={r['wrong_period']} "
                  f"兄弟={r['sibling_density']}")
        return out

    print("\n── arm: filter_on（被測項）──")
    filter_on = _run(items, True)
    print(f"\n── arm: filter_off（陽性對照，只跑會觸發路由的 {len(routed)} 題）──")
    off_subset = [it for it in items if it[0] in routed]
    filter_off = dict(filter_on)                       # 不觸發路由的題兩臂必然相同
    filter_off.update(_run(off_subset, False))
    os.environ["RQ_LATEST_QUARTER_ROUTING"] = "1"

    aggs = {"filter_on": _agg(filter_on, intents, args.top_k),
            "filter_off": _agg(filter_off, intents, args.top_k)}
    _print_table(f"collection = {rq.COLLECTION_NAME}", aggs)

    # ── 量尺自檢：陽性對照沒發作就不要看被測項的數字 ────────────────────────
    routed_intents = {intents[q] for q in routed}
    probe_label = "relative" if routed_intents == {"relative"} else "ALL"
    on_a = aggs["filter_on"].get(probe_label, {})
    off_a = aggs["filter_off"].get(probe_label, {})
    if not routed:
        print("⚠ 一題都沒有觸發最新一季路由 → **沒有陽性對照**，本次數字只能當描述，不能當判定。")
    elif (off_a.get("wrong_period_rate", 0) <= on_a.get("wrong_period_rate", 0)
          and off_a.get("gold_recall", 1) >= on_a.get("gold_recall", 1)):
        print(f"⚠ 陽性對照沒有發作（{probe_label} 類關掉路由也沒有變差）→ **這個量尺在目前語料上"
              "沒有判別力**。")
        print("  單年資料下期別競爭本來就稀薄，這是預期的；但這代表 before 這一輪只能當作")
        print("  『競爭密度基準』，判定要等 after 那一輪。**不要因此宣稱系統很好。**")
    else:
        print(f"✅ 陽性對照有發作（{probe_label} 類：關路由後 gold@k "
              f"{on_a.get('gold_recall')} → {off_a.get('gold_recall')}，錯期率 "
              f"{on_a.get('wrong_period_rate')} → {off_a.get('wrong_period_rate')}）→ 量尺有判別力。")
    if llm_replay.enabled():
        print(f"[INFO] replay cache: {llm_replay.stats()}")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(
        {"meta": {"collection": rq.COLLECTION_NAME, "top_k": args.top_k,
                  "baseline_filings": len(baseline), "queries": len(items),
                  "routed": sorted(routed), "intents": dist},
         "agg": aggs,
         "arms": {"filter_on": filter_on, "filter_off": filter_off}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"寫入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
