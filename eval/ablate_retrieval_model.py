"""ablate_retrieval_model.py — 檢索側 LLM（gpt-oss-20b vs 120b）的零噪音對照。

⚠ **2026-09-06 起這支跑不起來**：`MODEL_BIG`＝`gpt-oss-120b` 已被 NVIDIA 退役（410 Gone）。
  保留作為**已完成實驗的記錄**，理由與重跑要改哪些地方寫在 `MODEL_BIG` 上方。

**動機**：單發管線的檢索側模型預設是 `rq.DEFAULT_MODEL`＝`gpt-oss-120b`，agentic 則
刻意分層用 `gpt-oss-20b`。翻遍 CHANGELOG 找不到任何對照——那個 120b 不是選型結論，
是沒人動過的預設。而它同時是 2026-08-14「單發 vs agentic」對照裡的一個未控制變因。

**為什麼可以零噪音量**：檢索堆疊本身沒有 LLM（BGE-M3 dense+sparse → Qdrant → RRF →
cross-encoder rerank），檢索側模型只驅動三個 query-understanding 呼叫，其中兩個輸出
是結構化的、可逐題機械比對：

  ① `parse_query_filters()`        → JSON filter 清單（欄位/值/極性，完全可比）
  ② `translate_query_to_english()` → 英文字串（先比字串；不同的那幾題才需要比 rank）

第三個 `rewrite_query()` 生產預設關閉（`DEFAULT_ENABLE_REWRITE=False`），不在此量。

**噪音底線**：temp=0 不等於 host 端完全確定性，所以每個模型各跑 `--repeat` 次，
先量**同模型自我不一致率**——跨模型差異要大過它才算數。這是本檔唯一的「量尺」，
沒有任何聚合指標、沒有 judge。

⚠ **一定要清掉 `RAG_REPLAY_CACHE`**：`translate_en` 的快取 key 不含 model_name，
   開著快取時兩個模型會拿到同一筆記錄，量出來的「無差異」是假的。本檔在 import
   `rag_query` 之前就把它從 env 移除。

**兩個 stage**（stage 2 一定要跑）：

  stage `understand`（免 Qdrant／免 GPU）：錄下兩個模型對 100 題的 filter 與翻譯輸出。
  stage `rank`（要 Qdrant ＋ BGE-M3 ＋ reranker，**但零 LLM**）：把 stage 1 錄下來的
      輸出釘死（monkeypatch `parse_query_filters` / `translate_query_to_english`），
      各自送進真實檢索器，比**最終 top-k chunk 與 gold 命中**。

  為什麼 stage 2 不能省：實測翻譯的字面**同一個模型跑兩次就不一樣**（news-01：
  "recent legal actions or settlements" vs "recent lawsuits or settlements"），
  所以「字串不同」這個量尺對 translate 沒有判別力——唯一有意義的判準是
  「送進檢索器之後，撈回來的 chunk 有沒有變」。stage 2 用同模型的兩次跑
  （120b#0 vs 120b#1）當噪音底線，跨模型差異要大過它才算數。

用法：
    .venv/Scripts/python.exe -u eval/ablate_retrieval_model.py \
        --output experiments/retrieval_model_ab.json
    .venv/Scripts/python.exe -u eval/ablate_retrieval_model.py --stage rank \
        --from-stage1 experiments/retrieval_model_ab.json \
        --output experiments/retrieval_model_ab_rank.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ⚠ 必須早於 import rag_query：llm_replay 在 import 期讀 env 決定是否啟用。
os.environ.pop("RAG_REPLAY_CACHE", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq  # noqa: E402

# ⚠ **這支目前跑不起來，而且刻意不修**（2026-09-06 標記）。
#   `MODEL_BIG` 於 2026-09-03T08:00Z 被 NVIDIA 退役（410 Gone），stage `understand` 一開跑就炸。
#   **不把它偷偷指向活的模型**：這兩個常數是一次**已完成實驗的臂定義**，換掉之後檔裡的結論、
#   `experiments/retrieval_model_ab.json` 的 meta、以及第 257 行寫死的臂標籤（"120b#0" /
#   "120b#1" / "20b#0"）就會與實際跑過的東西對不上——那是把舊結論掛到沒跑過的組態上。
#   **要重跑得改三個地方**：這兩個常數、那三個臂標籤、本檔 docstring 的「20b vs 120b」敘述；
#   而且新的兩臂**都必須是活的**（同模型跑兩次當噪音底線這件事，兩邊都要做得到）。
#   原本那次的結論見 CHANGELOG（檢索側換模型的零噪音對照）。
MODEL_BIG = "openai/gpt-oss-120b"     # ⚠ 已退役（410）
MODEL_SMALL = "openai/gpt-oss-20b"


def _norm_filters(filters: list[dict]) -> tuple:
    """把 filter 清單正規化成可比對的鍵：順序無關、值大小寫無關。"""
    return tuple(sorted(
        (str(f.get("field")), str(f.get("value")).strip().lower(), str(f.get("polarity")))
        for f in (filters or [])
    ))


def _norm_en(text: str) -> str:
    """翻譯字串的正規化：只吃掉大小寫與空白差異，其餘一律算不同。"""
    return " ".join((text or "").split()).strip().lower()


def _run_one(task: tuple) -> dict:
    """跑單一 (題目, 模型, pass) 的兩個 query-understanding 呼叫。"""
    qid, query, model, rep = task
    t0 = time.time()
    try:
        filters = rq.parse_query_filters(query, model)
    except Exception as e:                      # 呼叫層已有 fallback，這裡只記錄
        filters, ferr = [], repr(e)
    else:
        ferr = None
    try:
        en = rq.translate_query_to_english(query, model)
    except Exception as e:
        en, eerr = query, repr(e)
    else:
        eerr = None
    return {"id": qid, "model": model, "rep": rep, "filters": filters,
            "en": en, "filter_err": ferr, "en_err": eerr,
            "secs": round(time.time() - t0, 2)}


def stage_understand(args) -> int:
    queries = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))["queries"]
    if args.ids:
        queries = [q for q in queries if q["id"] in set(args.ids)]

    # 先算兩道效率 gate 的分佈：gate 擋掉的題根本不呼叫 LLM → 那些題「無差異」
    # 是結構上必然的，不能當成模型等價的證據。
    gated_filter = [q["id"] for q in queries if not rq._FILING_HINT_RE.search(q["query"])]
    gated_en = [q["id"] for q in queries if rq._looks_english(q["query"])]

    tasks = [(q["id"], q["query"], m, r)
             for q in queries
             for m in (MODEL_BIG, MODEL_SMALL)
             for r in range(args.repeat)]
    print(f"題數 {len(queries)}｜模型 2｜repeat {args.repeat} → {len(tasks)} 個 task")
    print(f"gate 擋掉不呼叫 LLM：parse_query_filters {len(gated_filter)} 題、"
          f"translate_en {len(gated_en)} 題")

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(_run_one, tasks):
            rows.append(row)
            done += 1
            if done % 20 == 0:
                print(f"  ... {done}/{len(tasks)}")

    # index: (id, model, rep) → row
    idx = {(r["id"], r["model"], r["rep"]): r for r in rows}
    qids = [q["id"] for q in queries]

    def _self_inconsistent(model: str, key: str, norm) -> list[str]:
        """同一模型、同一題跑 repeat 次結果不一致的題 ＝ 本測的噪音底線。"""
        bad = []
        for qid in qids:
            vals = {norm(idx[(qid, model, r)][key]) for r in range(args.repeat)}
            if len(vals) > 1:
                bad.append(qid)
        return bad

    noise = {
        "filters": {m: _self_inconsistent(m, "filters", _norm_filters)
                    for m in (MODEL_BIG, MODEL_SMALL)},
        "en": {m: _self_inconsistent(m, "en", _norm_en)
               for m in (MODEL_BIG, MODEL_SMALL)},
    }

    def _cross_diff(key: str, norm) -> list[dict]:
        """跨模型差異：用 rep=0 比對；若該題自己就不穩，另外標記。"""
        out = []
        for qid in qids:
            a, b = idx[(qid, MODEL_BIG, 0)], idx[(qid, MODEL_SMALL, 0)]
            if norm(a[key]) != norm(b[key]):
                out.append({"id": qid,
                            "big": a[key], "small": b[key],
                            "unstable": qid in noise[key][MODEL_BIG]
                                        or qid in noise[key][MODEL_SMALL]})
        return out

    diff_filters = _cross_diff("filters", _norm_filters)
    diff_en = _cross_diff("en", _norm_en)

    errs = [r for r in rows if r["filter_err"] or r["en_err"]]
    lat = {m: round(sum(r["secs"] for r in rows if r["model"] == m)
                    / max(1, sum(1 for r in rows if r["model"] == m)), 2)
           for m in (MODEL_BIG, MODEL_SMALL)}

    print("\n" + "=" * 78)
    print(f"{'項目':<26}{'120b':>12}{'20b':>12}")
    print("-" * 78)
    print(f"{'自我不一致題數 (filters)':<24}"
          f"{len(noise['filters'][MODEL_BIG]):>12}{len(noise['filters'][MODEL_SMALL]):>12}")
    print(f"{'自我不一致題數 (translate)':<23}"
          f"{len(noise['en'][MODEL_BIG]):>12}{len(noise['en'][MODEL_SMALL]):>12}")
    print(f"{'平均單次耗時 (秒)':<25}{lat[MODEL_BIG]:>12}{lat[MODEL_SMALL]:>12}")
    print("-" * 78)
    print(f"跨模型不同：parse_query_filters {len(diff_filters)}/{len(qids)} 題"
          f"（其中 {sum(1 for d in diff_filters if d['unstable'])} 題本身就不穩）")
    print(f"跨模型不同：translate_en        {len(diff_en)}/{len(qids)} 題"
          f"（其中 {sum(1 for d in diff_en if d['unstable'])} 題本身就不穩）")
    if errs:
        print(f"⚠ 呼叫例外 {len(errs)} 次（已 fallback，但代表該模型在這條路上會掉）")
        for r in errs[:10]:
            print(f"   {r['id']:<12} {r['model']:<22} {r['filter_err'] or r['en_err']}")

    if diff_filters:
        print("\n── parse_query_filters 逐題差異（誰對要人工看） ──")
        for d in diff_filters:
            print(f"  {d['id']:<12}{'  [不穩]' if d['unstable'] else ''}")
            print(f"     120b: {d['big']}")
            print(f"      20b: {d['small']}")

    cat = Counter(q["category"] for q in queries
                  if q["id"] in {d["id"] for d in diff_en})
    if cat:
        print(f"\ntranslate_en 差異的類別分佈：{dict(cat)}")

    out = {
        "meta": {"models": [MODEL_BIG, MODEL_SMALL], "repeat": args.repeat,
                 "n": len(qids),
                 "gated_no_llm": {"parse_query_filters": gated_filter,
                                  "translate_en": gated_en},
                 "avg_secs": lat},
        "self_inconsistent": noise,
        "diff_filters": diff_filters,
        "diff_en": diff_en,
        "rows": rows,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    print(f"\n寫入 {args.output}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# stage rank：把 stage 1 錄下的 query-understanding 輸出釘死，比真實檢索結果
#
# 這一段**零 LLM**——所有 LLM 輸出都來自 stage 1 的記錄，所以同一份 stage 1 檔重跑
# 幾次結果都一樣（檢索堆疊本身是確定性的）。這是本測能宣稱「零噪音」的原因。
# ══════════════════════════════════════════════════════════════════════════════

def _chunk_key(c: dict) -> str:
    return f"{c.get('source')}#{c.get('chunk_index')}"


def _collect_sources(client, collection: str) -> set[str]:
    """掃整個 collection 的 source 檔名，供 eval_set 的 glob 展開。
    ⚠ 不用 `eval_retrieval.collect_all_sources`——那個把 collection 名寫死在自己的
       模組常數（`us_stock_rag_unstructured`，早就不存在），會 404。"""
    sources, offset = set(), None
    while True:
        points, offset = client.scroll(collection_name=collection, limit=256,
                                       offset=offset, with_payload=["source"],
                                       with_vectors=False)
        for p in points:
            if src := (p.payload or {}).get("source"):
                sources.add(src)
        if offset is None:
            return sources


def stage_rank(args) -> int:
    import fnmatch

    stage1 = json.loads(Path(args.from_stage1).read_text(encoding="utf-8"))
    rec = {(r["id"], r["model"], r["rep"]): r for r in stage1["rows"]}
    repeat = stage1["meta"]["repeat"]
    if repeat < 2:
        print("⚠ stage 1 的 --repeat < 2，沒有同模型兩次跑 → 量不出噪音底線")
        return 2

    queries = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))["queries"]
    if args.ids:
        queries = [q for q in queries if q["id"] in set(args.ids)]
    queries = [q for q in queries if (q["id"], MODEL_BIG, 0) in rec]

    # 三臂：120b#0 是基準，120b#1 只跟基準差在「同模型的第二次跑」＝噪音底線，
    # 20b#0 才是被測的那一項。
    arms = [("120b#0", MODEL_BIG, 0), ("120b#1", MODEL_BIG, 1), ("20b#0", MODEL_SMALL, 0)]

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    client = rq.make_qdrant_client()
    print(f"[INFO] collection = {rq.COLLECTION_NAME}")
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    all_sources = _collect_sources(client, rq.COLLECTION_NAME)
    print(f"[INFO] {len(all_sources)} 個 source 檔名")

    orig_pf, orig_tr = rq.parse_query_filters, rq.translate_query_to_english
    results: dict[str, dict[str, dict]] = {a[0]: {} for a in arms}
    try:
        for i, q in enumerate(queries, 1):
            qid, qstr = q["id"], q["query"]
            gold = set()
            for pat in q.get("relevant", []):
                gold |= (set(fnmatch.filter(all_sources, pat))
                         if any(ch in pat for ch in "*?[") else {pat})
            for label, model, repi in arms:
                r = rec[(qid, model, repi)]
                # 釘死這一臂的 query-understanding 輸出：下游檢索完全不再呼叫 LLM
                rq.parse_query_filters = (
                    lambda _q, _m=None, _f=r["filters"]: [dict(x) for x in _f])
                rq.translate_query_to_english = lambda _q, _m=None, _e=r["en"]: _e
                chunks, _note = rq.retrieve(
                    qstr, bge, reranker, client, top_k=args.top_k,
                    model_name=model,
                    enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                    translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN,
                )
                results[label][qid] = {
                    "keys": [_chunk_key(c) for c in chunks],
                    "gold_hits": sum(1 for c in chunks if c.get("source") in gold),
                    "n_gold": len(gold),
                }
            print(f"  [{i}/{len(queries)}] {qid} done")
    finally:
        rq.parse_query_filters, rq.translate_query_to_english = orig_pf, orig_tr

    qids = [q["id"] for q in queries]

    def _pair(a: str, b: str) -> dict:
        same_order = sum(1 for i in qids if results[a][i]["keys"] == results[b][i]["keys"])
        jac, changed = [], []
        for i in qids:
            sa, sb = set(results[a][i]["keys"]), set(results[b][i]["keys"])
            j = len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0
            jac.append(j)
            if sa != sb:
                changed.append(i)
        return {"same_order": same_order, "same_set": len(qids) - len(changed),
                "mean_jaccard": round(sum(jac) / len(jac), 4), "changed": changed}

    noise = _pair("120b#0", "120b#1")     # 同模型兩次跑 ＝ 噪音底線
    signal = _pair("120b#0", "20b#0")     # 跨模型 ＝ 被測項
    signal2 = _pair("120b#1", "20b#0")

    def _recall(label: str) -> float:
        vals = [min(1.0, results[label][i]["gold_hits"] / max(1, results[label][i]["n_gold"]))
                for i in qids]
        return round(sum(vals) / len(vals), 4)

    def _anyhit(label: str) -> float:
        return round(sum(1 for i in qids if results[label][i]["gold_hits"] > 0) / len(qids), 4)

    print("\n" + "=" * 78)
    print(f"n = {len(qids)}｜top_k = {args.top_k}｜collection = {rq.COLLECTION_NAME}")
    print("-" * 78)
    print(f"{'配對':<22}{'chunk 集合相同':>16}{'順序也相同':>14}{'平均 Jaccard':>16}")
    for name, p in (("120b#0 vs 120b#1 (噪音)", noise),
                    ("120b#0 vs 20b#0  (訊號)", signal),
                    ("120b#1 vs 20b#0  (訊號)", signal2)):
        print(f"{name:<22}{p['same_set']:>13}/{len(qids)}{p['same_order']:>11}/{len(qids)}"
              f"{p['mean_jaccard']:>16.4f}")
    print("-" * 78)
    print(f"{'gold 檔命中率':<22}" + "".join(f"{a[0]:>12}" for a in arms))
    print(f"{'  recall@k（比例）':<21}" + "".join(f"{_recall(a[0]):>12.4f}" for a in arms))
    print(f"{'  至少命中一個':<20}" + "".join(f"{_anyhit(a[0]):>12.4f}" for a in arms))
    print("-" * 78)
    verdict = ("20b 與 120b 的差異沒有大過同模型自跑兩次的差異 → 檢索側等價"
               if signal["mean_jaccard"] >= noise["mean_jaccard"] - 1e-9
               else "20b 造成的候選變動大於噪音底線 → 要逐題看 gold 有沒有掉")
    print(f"判定：{verdict}")

    out = {"meta": {"n": len(qids), "top_k": args.top_k,
                    "collection": rq.COLLECTION_NAME, "arms": [a[0] for a in arms],
                    "from_stage1": args.from_stage1},
           "pairs": {"noise_120b0_vs_120b1": noise,
                     "signal_120b0_vs_20b0": signal,
                     "signal_120b1_vs_20b0": signal2},
           "gold": {a[0]: {"recall": _recall(a[0]), "any_hit": _anyhit(a[0])} for a in arms},
           "per_query": {i: {a[0]: results[a[0]][i] for a in arms} for i in qids}}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    print(f"寫入 {args.output}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("understand", "rank"), default="understand")
    ap.add_argument("--eval-set", default="eval/eval_set.json")
    ap.add_argument("--output", default="experiments/retrieval_model_ab.json",
                    help="跑分輸出一律寫 experiments/（eval/ 只放輸入與標準答案）")
    ap.add_argument("--repeat", type=int, default=2,
                    help="每個模型跑幾次（用來量同模型自我不一致率＝本測的噪音底線）")
    ap.add_argument("--ids", nargs="*", default=None, help="只跑這些題（除錯用）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--from-stage1", default="experiments/retrieval_model_ab.json",
                    help="stage rank 用：讀 stage understand 的輸出")
    ap.add_argument("--top-k", type=int, default=rq.DEFAULT_TOP_K)
    args = ap.parse_args(argv)
    return stage_rank(args) if args.stage == "rank" else stage_understand(args)


if __name__ == "__main__":
    raise SystemExit(main())
