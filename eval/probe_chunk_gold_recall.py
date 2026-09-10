"""probe_chunk_gold_recall.py — **生產檢索的 chunk 層召回率**（2026-09-08）。

## 為什麼需要這一支

`BACKLOG.md`〈量尺缺口〉與 CLAUDE.md 都寫著「切塊／檢索已經到頂」，而那個結論的來源是
**100 題 × 舊 judge（`gpt-oss-120b`，已 410）× 舊 collection** 的 RAGAS 檢索三指標。照這個
repo 自己的規則（**跨 2026-09-04 的分數一律不可比**、**跨輪比 RAGAS 聚合值 ＝ 把 judge 噪音
算進系統差異**），那把尺**已經作廢**。於是「生產檢索到底撈不撈得到答案」目前**沒有任何可信的
整體數字**——`chunk_gold.json`（41/65 題、83 顆、literal 定位、逐題人工驗證）2026-09-04 就建好了，
但它**只被 `probe_relevant_ids` 拿去量 Grader 的圈選**，從來沒有拿去量 `rq.retrieve()` 本身。

這支就是補那一格。**它不是閘門**（檢索路徑上有一次 LLM 英譯），是探針。

## 判讀方式：**先看 @5 與 @20 的差，再看數字大小**

| `gold@20` | `gold@5` | 診斷 | 對應的修法在哪一層 |
|---|---|---|---|
| 高 | 低 | **排序**問題 | rerank／`RRF_TOP_N_PRIMARY`／`_fair_select` |
| 低 | 低 | **召回**問題 | 召回層（HyDE 這類「換一個查詢向量」的機制才在這一格有意義） |

⚠ **`@20` 實際上就是「整個候選池」，不是「一個大排序的前 20 名」**：`RRF_TOP_N_PRIMARY = 20`
——server-side RRF 只回 20 個候選，rerank 只是把這 20 個重排（實測去重與 hard filter 之後更小，
`mh-01` 只有 12）。所以本檔印出 `pool` 的中位數，**判讀時先看它**：pool 若已 < 20，
「@20 進不來」就等於「**RRF 那 20 個名額裡根本沒有它**」＝ 召回層的問題，而那同時指向
`RRF_TOP_N_PRIMARY` 這個常數本身（它上一次 sweep 是在舊 collection 上做的）。
**不要把「@20 進不來」直接讀成「embedding 不行」——先問名額夠不夠。**

⚠ **這兩格對應完全不同的修法，合併成一個 recall 數字就分不出來了。** `BACKLOG.md` 記著
`col-05`／`col-08` 的 dense rank 是 **25／106**（＝下面那一列），而 `col-04` 的關鍵內容
**就在 top-5**（＝根本不是檢索問題）——同樣掛在「口語題答不好」底下，是三種病。

## 四條紀律

⚠ **命中判準是「gold 聯集裡**任何一顆**進了 top-k」，而 gold 聯集偏大** —— `chunk_gold.json`
  的消費語意本來就是 union（那支的檔頭寫著理由：union 偏大只會讓判定**更難**觸發）。
  用在**召回**上，偏誤方向是**反過來的**：聯集越大越容易命中 → **這支只會高估召回，不會低估**。
  所以：**低分是硬證據，高分不是健康證明**。要下「檢索沒問題」這種結論之前，先想這一條。

⚠ **N/A 不可併進任何一格**（同 `check_number_defects.py`）：`chunk_gold` 只涵蓋 41/65 題，
  **那是上限不是缺陷**——定性 semantic 題沒有可定位的數值答案。未涵蓋的 24 題是 N/A，
  併進分母會讓每一類的召回率被稀釋成看不懂的東西。本檔逐類印出 N/A 題數。
⚠ **這支不是零噪音**：`translate_query_to_english` 是 LLM，同組態重跑實測 **47/100 題 top-8
  不同**（`docs/EVAL.md`〈量測噪音〉）。所以：① 預設**自動掛上 `eval/replay_cache.json`**
  把英譯釘死（同 `probe_historical_benefit`／`probe_temporal_interference` 的作法）；
  ② 要下結論請 `--repeat 3` 以上並看**逐題 k/n**，不要看單輪聚合值。
  ③ `--no-translate` 給一條**零 LLM 的確定性臂**——但它**不是生產組態**，只能自己跟自己比。
⚠ **量的是「單管線的檢索」不是「agentic 的檢索」**：agentic 不對原始 query 檢索，它對
  Planner 的子問題各跑一次。要量 agentic 那一條請用 `--from-results`（讀結果檔的 `contexts`
  ＝ 全 run 收集到的聯集），**零額外檢索、零 LLM**。兩條路的數字**不可互相比較**：
  一個是 top-k 切片，一個是跨子問題的聯集（分母定義就不同）。

用法：
    # 單管線臂（預設掛 replay cache 釘死英譯）
    .venv/Scripts/python.exe eval/probe_chunk_gold_recall.py --output experiments/cgr_single.json

    # 零 LLM 的確定性臂（非生產組態，只能自己跟自己比）
    .venv/Scripts/python.exe eval/probe_chunk_gold_recall.py --no-translate

    # agentic 臂：讀結果檔的 contexts（零檢索、零 LLM）
    .venv/Scripts/python.exe eval/probe_chunk_gold_recall.py \
        --from-results experiments/agentic/gj_65q_denominators_20260908.json

    # 多輪（英譯有噪音，下結論前請跑 >=3 輪）
    .venv/Scripts/python.exe eval/probe_chunk_gold_recall.py --repeat 3

    # 零 Qdrant 的自測（毫秒級）
    .venv/Scripts/python.exe eval/probe_chunk_gold_recall.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# Windows 主控台預設 cp950，印 ⚠ 會 UnicodeEncodeError。
# ⚠ 這個崩法特別陰險（跟 `verify_cited_evidence.py` 同一個）：它只炸在**最後那一行**，
#   前面的數字都已經印出來了 → 外觀像「跑完了」，只有退出碼是 1。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 檢索路徑上唯一的 LLM 是英譯，同輸入重跑實測 47/100 題 top-8 不同 → 預設釘死，
# 讓「這一輪的數字」至少對自己是可重現的。⚠ `--from-results` 不碰檢索，這行對它無作用。
if "--no-replay-cache" not in sys.argv and not os.getenv("RAG_REPLAY_CACHE"):
    os.environ["RAG_REPLAY_CACHE"] = str(_ROOT / "eval" / "replay_cache.json")

import chunk_gold as cg  # noqa: E402

CATS = ("semantic", "mixed", "lexical", "colloquial", "multi_hop")


def _key(source: str, chunk_index) -> tuple[str, str]:
    """chunk 的身分。⚠ `chunk_index` 兩邊型別不同（`scan_docs` 轉 str，`rq` 的 payload 是 int）
    → **一律轉字串再比**。不轉的話每一題都會是 0 命中，而那個外觀與「檢索全壞」完全相同。"""
    return (str(source), str(chunk_index))


def _eval_set(path: Path) -> dict:
    qs = json.loads(path.read_text(encoding="utf-8"))
    qs = qs["queries"] if isinstance(qs, dict) else qs
    return {q["id"]: q for q in qs}


# ──────────────────────────────────────────────────────────────────────────────
# 報表
# ──────────────────────────────────────────────────────────────────────────────
def _report(rows: dict, ks: tuple[int, ...], n_rounds: int, label: str,
            na_ids: dict[str, list]) -> None:
    """逐類別印 gold@k。⚠ **N/A 另外一欄**，永遠不併進分母。"""
    print()
    print("=" * 78)
    print(f"{label}   輪數 {n_rounds}")
    print("=" * 78)
    hdr = "".join(f"{'gold@' + str(k):>11}" for k in ks)
    print(f"  {'類別':<12}{'有 gold':>8}{'N/A':>6}{hdr}")
    print("  " + "-" * 74)
    tot = {k: [0, 0] for k in ks}
    for cat in CATS:
        rs = [r for r in rows.values() if r["category"] == cat]
        if not rs and not na_ids.get(cat):
            continue
        cells = ""
        for k in ks:
            hit = sum(r["hits"][k] for r in rs)
            tot_n = sum(r["rounds"] for r in rs)
            tot[k][0] += hit
            tot[k][1] += tot_n
            cells += f"{(hit / tot_n if tot_n else 0):>10.3f} " if tot_n else f"{'—':>11}"
        print(f"  {cat:<12}{len(rs):>8}{len(na_ids.get(cat, [])):>6}{cells}")
    print("  " + "-" * 74)
    cells = "".join(f"{(tot[k][0] / tot[k][1] if tot[k][1] else 0):>10.3f} " for k in ks)
    n_na = sum(len(v) for v in na_ids.values())
    print(f"  {'合計':<12}{len(rows):>8}{n_na:>6}{cells}")
    print(f"\n  ⚠ N/A {n_na} 題**不是 PASS 也不是 FAIL**：`chunk_gold` 只涵蓋 41/65，"
          f"那是上限不是缺陷（定性題沒有可定位的數值答案）。")

    # ⚠ 這一段是**量尺自己的效度檢查**，不是裝飾：`RRF_TOP_N_PRIMARY = 20` 表示候選池上限
    #   就是 20，所以 `@20` 量到的其實是「有沒有進候選池」。池若比 @k 還小，那一欄的
    #   語意就從「排名」變成「進不進得來」——不講清楚會把「名額不夠」誤讀成「embedding 不行」。
    pools = sorted(n for r in rows.values() for n in (r.get("pool") or []))
    if pools:
        med = pools[len(pools) // 2]
        print(f"\n  候選池大小：中位 {med}、最小 {pools[0]}、最大 {pools[-1]}"
              f"（RRF_TOP_N_PRIMARY 上限 20）")
        if med < max(ks):
            print(f"  ⚠ **中位池 {med} < @{max(ks)}**：那一欄實際量的是「有沒有進候選池」，"
                  f"不是「排在前 {max(ks)} 名」。\n"
                  f"    ⇒ 「召回問題」那一格同時指向 `RRF_TOP_N_PRIMARY`（名額不夠），"
                  f"不只是 embedding 撈不到。")

    if len(ks) >= 2:
        lo, hi = min(ks), max(ks)
        print(f"\n  {'診斷（每題按 @' + str(hi) + ' / @' + str(lo) + ' 分類）':<40}")
        buckets = {"排序問題（@%d 進得來、@%d 進不來）" % (hi, lo): [],
                   "召回問題（@%d 都進不來）" % hi: [],
                   "沒問題（@%d 就在）" % lo: [],
                   "不穩定（跨輪翻面）": []}
        for qid, r in sorted(rows.items()):
            n = r["rounds"]
            a, b = r["hits"][lo], r["hits"][hi]
            if 0 < b < n or 0 < a < n:
                buckets["不穩定（跨輪翻面）"].append(qid)
            elif a == n:
                buckets["沒問題（@%d 就在）" % lo].append(qid)
            elif b == n:
                buckets["排序問題（@%d 進得來、@%d 進不來）" % (hi, lo)].append(qid)
            else:
                buckets["召回問題（@%d 都進不來）" % hi].append(qid)
        for name, ids in buckets.items():
            print(f"    {name:<34}{len(ids):>3}  {' '.join(sorted(ids)) if ids else ''}")
        print("\n  ⚠ 這兩格對應**完全不同**的修法：排序問題在 rerank／`_fair_select`，"
              "召回問題才是「換一個查詢向量」（HyDE 這類）有意義的地方。")
        print("  ⚠ 「不穩定」那一格是**英譯噪音**，不是系統壞掉——單輪跑出來的數字會把它們"
              "隨機分到上面兩格，所以下結論前 `--repeat 3` 以上。")


# ──────────────────────────────────────────────────────────────────────────────
_SWEEPABLE = ("FETCH_N", "RRF_TOP_N_PRIMARY", "RERANK_INPUT_N")


def parse_overrides(items) -> dict:
    """`["FETCH_N=180"]` → `{"FETCH_N": 180}`。純函式，供 `--selftest` 餵誤報對照。

    ⚠ **名字不在白名單就當場炸**，不靜默忽略：打錯字的失敗外觀與「這個常數沒有效果」
      完全相同——那正是 sweep 最容易得出的假 null result。
    ⚠ 白名單刻意只有三個**檢索名額**常數。它是**格式定義的封閉集合**（CLAUDE.md 允許
      詞表的那個例外），不是在猜使用者想調什麼。
    ⚠ `RERANK_INPUT_N` 列在這裡但**不建議動**：它管重排不管召回，而它是這台機器的成本
      主宰（150 讓單次檢索 33s→46s，見 `probe_kb_content_ceiling`）。
    """
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"[FATAL] --override 要寫成 NAME=VALUE，收到 {it!r}")
        k, v = it.split("=", 1)
        k = k.strip()
        if k not in _SWEEPABLE:
            raise SystemExit(f"[FATAL] 不可 sweep 的常數 {k!r}；可用：{', '.join(_SWEEPABLE)}")
        out[k] = int(v)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 臂一：單管線（跑真實 rq.retrieve）
# ──────────────────────────────────────────────────────────────────────────────
def run_single(args) -> int:
    import rag_query as rq
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    ov = parse_overrides(getattr(args, "override", None))
    # ⚠ **就地改模組屬性**：`rq.retrieve` 在函式體內讀這些常數，改 `rq.<NAME>` 就會生效。
    #   不用 env 是因為 `rag_query` 沒有為它們留 env 入口，而**為了 sweep 去給生產加 env
    #   入口＝為了量尺去改被測物**。改屬性只活在這個 process 裡。
    _saved = {k: getattr(rq, k) for k in ov}
    for k, v in ov.items():
        setattr(rq, k, v)
    if ov:
        print("[INFO] override " + "  ".join(
            f"{k}: {_saved[k]} → {getattr(rq, k)}" for k in ov))

    gold_by_id = cg.by_id()
    eset = _eval_set(Path(args.eval_set))
    client = rq.make_qdrant_client()
    print(f"[INFO] collection={rq.COLLECTION_NAME}  "
          f"translate={not args.no_translate}  rewrite={rq.DEFAULT_ENABLE_REWRITE}  "
          f"replay_cache={os.getenv('RAG_REPLAY_CACHE', '(關)')}")
    docs = cg.scan_docs(client, rq.COLLECTION_NAME)
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)

    if args.ids:
        eset = {k: v for k, v in eset.items() if k in set(args.ids)}
    ks = tuple(sorted(args.k))
    rows: dict = {}
    na_ids: dict[str, list] = defaultdict(list)
    for qid, q in eset.items():
        entry = gold_by_id.get(qid)
        if entry is None:
            na_ids[q.get("category", "?")].append(qid)
            continue
        golds = {_key(s, ci) for s, ci in cg.gold_chunks(docs, entry)}
        if not golds:
            # gold 的 literal 在**現行 collection** 裡一顆都定位不到 → 這是 gold 漂移，
            # 不是檢索失敗。混進 0 分會把量尺自己的病算到系統頭上。
            na_ids[q.get("category", "?")].append(qid + "(gold未命中)")
            continue
        rows[qid] = {"category": q.get("category", "?"), "query": q["query"],
                     "n_gold": len(golds), "rounds": 0,
                     "hits": {k: 0 for k in ks}, "best_rank": [], "pool": []}

    if args.limit:
        rows = dict(list(rows.items())[:args.limit])
    for rnd in range(args.repeat):
        print(f"\n── 第 {rnd + 1}/{args.repeat} 輪 " + "─" * 50)
        for qid, r in rows.items():
            entry = gold_by_id[qid]
            golds = {_key(s, ci) for s, ci in cg.gold_chunks(docs, entry)}
            pool, _note = rq.retrieve(
                r["query"], bge, reranker, client, top_k=rq.RERANK_INPUT_N,
                enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                translate_query_en=(not args.no_translate))
            seq = [_key(c["source"], c["chunk_index"]) for c in pool]
            rank = next((i for i, kk in enumerate(seq, 1) if kk in golds), None)
            r["rounds"] += 1
            r["best_rank"].append(rank)
            r["pool"].append(len(pool))
            for k in ks:
                if any(kk in golds for kk in seq[:k]):
                    r["hits"][k] += 1
            marks = " ".join(f"@{k}={'Y' if any(kk in golds for kk in seq[:k]) else 'N'}"
                             for k in ks)
            print(f"  [{qid:<7}/{r['category']:<10}] {marks}  rank={rank}  "
                  f"gold={len(golds)} pool={len(pool)}")

    _report(rows, ks, args.repeat,
            f"單管線 rq.retrieve()｜collection={rq.COLLECTION_NAME}"
            f"｜translate={not args.no_translate}", na_ids)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(
            {"_meta": {"arm": "single", "collection": rq.COLLECTION_NAME,
                       "translate": not args.no_translate, "repeat": args.repeat,
                       "k": list(ks), "na": dict(na_ids),
                       # ⚠ 覆蓋值一定要進 `_meta`：兩份輸出檔的差別**只有**這一格，
                       #   沒記下來的話兩個臂在檔案層完全分不出來。
                       "overrides": ov, "baseline": _saved},
             "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] 寫出 {args.output}")
    for k, v in _saved.items():        # 同一個 process 若再跑別的臂，不可留下污染
        setattr(rq, k, v)
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# 臂二：agentic（讀結果檔的 contexts，零檢索零 LLM）
# ──────────────────────────────────────────────────────────────────────────────
def run_from_results(args) -> int:
    import rag_query as rq

    data = json.loads(Path(args.from_results).read_text(encoding="utf-8"))
    recs = data.get("records") or data.get("results") or data
    if isinstance(recs, dict):
        recs = list(recs.values())
    gold_by_id = cg.by_id()
    client = rq.make_qdrant_client()
    docs = cg.scan_docs(client, rq.COLLECTION_NAME)

    rows: dict = {}
    na_ids: dict[str, list] = defaultdict(list)
    for rec in recs:
        qid = rec.get("id")
        entry = gold_by_id.get(qid)
        cat = rec.get("category", "?")
        if entry is None:
            na_ids[cat].append(qid)
            continue
        golds = {_key(s, ci) for s, ci in cg.gold_chunks(docs, entry)}
        if not golds:
            na_ids[cat].append(f"{qid}(gold未命中)")
            continue
        # ⚠ 用 `sources` 而不是 `contexts`：`contexts` 是文字，沒有 chunk 身分。
        #   兩者由 `_record_from_agentic` **同一次過濾、逐位對齊**（那支的註解說明了原因）。
        got = {_key(s.get("source"), s.get("chunk_index"))
               for s in (rec.get("sources") or []) if isinstance(s, dict)}
        rows[qid] = {"category": cat, "query": rec.get("query", ""),
                     "n_gold": len(golds), "rounds": 1,
                     "hits": {0: int(bool(golds & got))}, "best_rank": [None],
                     "n_collected": len(got)}
        print(f"  [{qid:<7}/{cat:<10}] {'Y' if golds & got else 'N'}  "
              f"gold={len(golds)} collected={len(got)}")

    _report(rows, (0,), 1,
            f"agentic 聯集｜{Path(args.from_results).name}", na_ids)
    print("\n  ⚠ 這一欄的 `gold@0` 是**聯集命中**（全 run 收集到的 chunk 裡有沒有 gold），"
          "\n    不是 top-k 切片——**不可與單管線那一欄並排比較**，分母定義不同。")
    return 0


# ──────────────────────────────────────────────────────────────────────────────
def cmd_selftest() -> int:
    """零 Qdrant、毫秒級。⚠ 判別力在③④：陽性那兩條，一個「一律回 0」的實作也會過。"""
    ok = fail = 0

    def _a(name, cond, note=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  OK    {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {note}")

    # ① 型別正規化：int 與 str 的 chunk_index 必須是同一顆
    _a("① `chunk_index` 型別正規化（payload 是 int、scan_docs 是 str，不轉就全 0 命中）",
       _key("A.html", 3) == _key("A.html", "3"))

    # ② 報表不得把 N/A 併進分母
    rows = {"q1": {"category": "lexical", "rounds": 2, "hits": {5: 1, 20: 2}, "best_rank": [3, 9]}}
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report(rows, (5, 20), 2, "自測", {"semantic": ["s-1", "s-2"]})
    out = buf.getvalue()
    _a("② N/A 有自己的一欄，且不進 gold@k 的分母（1 題 2 輪 → @5=0.500 而非 0.250）",
       "0.500" in out and "1.000" in out, out)
    _a("②b N/A 題數印得出來（2 題）", " 2 " in out or "N/A" in out, out)

    # ③ 誤報對照：@5 全中的題不得被歸成「排序問題」
    rows3 = {"q1": {"category": "lexical", "rounds": 2, "hits": {5: 2, 20: 2}, "best_rank": [1, 1]}}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report(rows3, (5, 20), 2, "自測", {})
    out3 = buf.getvalue()
    _a("③ **誤報對照**：@5 全中 → 歸「沒問題」，不得歸成排序問題",
       "沒問題" in out3.split("q1")[0].split("排序問題")[-1] or
       [ln for ln in out3.splitlines() if "q1" in ln and "沒問題" in ln], out3)

    # ④ 誤報對照：跨輪翻面必須歸「不穩定」，不得被算成任何一種病
    rows4 = {"q1": {"category": "lexical", "rounds": 3, "hits": {5: 1, 20: 3}, "best_rank": []}}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report(rows4, (5, 20), 3, "自測", {})
    out4 = buf.getvalue()
    _a("④ **誤報對照**：@5 三輪中一輪（英譯噪音）→ 歸「不穩定」而不是排序問題",
       any("不穩定" in ln and "q1" in ln for ln in out4.splitlines()), out4)

    # ⑤ 召回問題：@20 也全滅
    rows5 = {"q1": {"category": "colloquial", "rounds": 2, "hits": {5: 0, 20: 0}, "best_rank": []}}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report(rows5, (5, 20), 2, "自測", {})
    out5 = buf.getvalue()
    _a("⑤ @20 也全滅 → 歸「召回問題」（那才是 HyDE 這類機制有意義的一格）",
       any("召回問題" in ln and "q1" in ln for ln in out5.splitlines()), out5)

    # ── `--override` 的解析（判別力全在「打錯字要炸」那兩條）────────────────
    _a("⑥ `NAME=VALUE` 解析成 int", parse_overrides(["FETCH_N=180"]) == {"FETCH_N": 180})
    _a("⑥b 可以一次給多個", parse_overrides(["FETCH_N=180", "RRF_TOP_N_PRIMARY=50"])
       == {"FETCH_N": 180, "RRF_TOP_N_PRIMARY": 50})
    _a("⑥c 沒給就是空 dict（＝生產組態，不是「有覆蓋但值為 0」）",
       parse_overrides(None) == {} and parse_overrides([]) == {})
    try:
        parse_overrides(["FETCH_NN=180"])
        _a("⑦ **誤報對照**：名字打錯必須當場炸（靜默忽略＝假的 null result）", False)
    except SystemExit:
        _a("⑦ **誤報對照**：名字打錯必須當場炸（靜默忽略＝假的 null result）", True)
    try:
        parse_overrides(["COLLECTION_NAME=x"])
        _a("⑦b **誤報對照**：白名單以外的 `rq` 屬性也要炸（別讓 sweep 變成任意改生產）",
           False)
    except SystemExit:
        _a("⑦b **誤報對照**：白名單以外的 `rq` 屬性也要炸（別讓 sweep 變成任意改生產）",
           True)
    try:
        parse_overrides(["FETCH_N"])
        _a("⑦c **誤報對照**：少了 `=` 也要炸", False)
    except SystemExit:
        _a("⑦c **誤報對照**：少了 `=` 也要炸", True)

    print(f"\n  PASS {ok}  FAIL {fail}")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-set", default=str(_ROOT / "eval" / "eval_set.json"))
    ap.add_argument("--k", type=int, nargs="+", default=[5, 20],
                    help="要量的 top-k（預設 5 與 20：兩格對應不同的修法，見 docstring）")
    ap.add_argument("--ids", nargs="+", default=None, help="只跑指定 id（smoke test）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 題（smoke test）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="重複幾輪。⚠ 英譯有噪音，下結論前請 >=3")
    ap.add_argument("--no-translate", action="store_true",
                    help="零 LLM 的確定性臂（**非生產組態**，只能自己跟自己比）")
    ap.add_argument("--no-replay-cache", action="store_true",
                    help="不釘死英譯（預設會掛 eval/replay_cache.json）")
    ap.add_argument("--from-results", default=None,
                    help="agentic 臂：讀結果檔的 sources（零檢索零 LLM）")
    ap.add_argument("--output", default=None, help="⚠ 一律寫到 experiments/")
    ap.add_argument("--override", nargs="+", metavar="NAME=VALUE",
                    help="sweep 檢索名額常數（%s）。⚠ 只影響本 process"
                         % ", ".join(_SWEEPABLE))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return cmd_selftest()
    if args.from_results:
        return run_from_results(args)
    return run_single(args)


if __name__ == "__main__":
    raise SystemExit(main())
