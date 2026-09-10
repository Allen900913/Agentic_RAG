"""probe_recall_layer_attribution.py — **召回失敗死在哪一層**（2026-09-09）。

## 為什麼需要這一支

`probe_chunk_gold_recall.py`（2026-09-08）量到 `gold@20 = 0.780`、**9 題召回失敗**
（gold 從沒進過候選池）。當時的判讀是「中位池正好 20 ＝ `RRF_TOP_N_PRIMARY` 上限 ⇒
池是滿的 ⇒ 去 sweep 那個常數」。**那個推論 2026-09-09 被逐題分層推翻**：
中位數 20 是 **41 題**算出來的，而**失敗的那 9 題自己的池根本沒滿**——
`lex-17` 只有 7、`col-01` 10、`sem-01` 12、`lex-07`／`sem-05` 13、`sem-03`／`lex-03` 14、
`lex-04` 15，只有 `lex-14`（19/20）撞到上限。**名額沒用完，加大名額在定義上救不了它們。**

所以真正該問的是**「gold 是在哪一層不見的」**，而 `rq.retrieve()` 的回傳是
`RRF-20 → hard filter 三層降級 → 跨期 collapse → 去重 → rerank` 之後的殘量。
少掉的 5~13 個名額**是被某一層刪掉的**，而 gold 有可能就在被刪掉的那批裡。

## 四格，四種完全不同的修法

| 格 | 判準 | 病 | 修法在哪 |
|---|---|---|---|
| ① **刪錯了** | gold **進過** RRF-20，`retrieve()` 回傳裡沒有 | collapse／filter／去重刪掉了答案 | 那幾層的規則（`verify_cross_period_collapse` 要加斷言） |
| ② **RRF 名額不夠** | 不在 RRF-20，但 `RRF_TOP_N_PRIMARY=100` 時進得來 | 融合後的名額太少 | **這時才輪到** sweep `RRF_TOP_N_PRIMARY` |
| ③ **prefetch 名額不夠** | 不在 ②，但 `FETCH_N=300` 時進得來 | 單路召回深度不夠 | `FETCH_N` |
| ④ **真的撈不到** | 三個都沒有 | dense/sparse 這個 query 就是打不到那顆 chunk | 查詢重寫／HyDE／sparse 權重（**只有這一格 HyDE 才有意義**） |

⚠ **順序不可調換**：①→④ 是「越靠近下游越先排除」。先問「是不是被刪掉的」，
  因為那一格的修法最便宜、而且它是**缺陷**（其餘三格是**取捨**）。

## 五條紀律

⚠ **「池沒滿」本身不是缺陷**——多數題的池都 < 20（去重與 hard filter 的正常結果），
  而它們的 gold 好端端在裡面。要驗的是「**被刪掉的那批裡有沒有 gold**」。
  所以本檔**一律把 28 題「沒問題」的一起量並印出來當誤報對照**：少了它，
  一個「池沒滿就報缺陷」的實作會滿分。`--only-failures` 只給臨時查看用，**不可拿來下結論**。
⚠ **gold 是聯集、偏大** → 判「有沒有進過候選池」只會**高估**（同 `probe_chunk_gold_recall`）。
  **判成 ①~③ 是硬證據；判成「沒問題」不是健康證明。**
⚠ **wide 兩臂刻意把 reranker 換成常數樁**：rerank 發生在 `query_points` **之後**，
  對本檔要記錄的候選集合零影響，但 100~200 個候選跑 cross-encoder 會慢十倍。
  ⚠ 因此 **wide 兩臂的 `post` 沒有意義、不予採用**，只讀它們的 `pre`。
⚠ **`pre` 取的是「所有 `query_points` 呼叫的聯集」**：`retrieve()` 有三層降級 filter，
  失敗的那層也會發出查詢。取聯集 ＝ 問「**檢索器到底有沒有見過這顆 chunk**」，
  偏誤方向是**讓 ① 更難成立**（聯集越大，「進過池」越容易為真 → 越容易被判成「刪錯了」……
  ⚠ **這一條方向是相反的，要小心**：聯集偏大會讓 ① **高估**。所以 ① 的每一筆都要人工複核
  是哪一層刪的，不可以只讀計數。
⚠ **英譯是 LLM**（同輸入實測 47/100 題 top-8 不同）→ 預設自動掛 `eval/replay_cache.json`
  釘死；下結論前 `--repeat 2` 以上、看逐題是否翻面。

用法：
    .venv/Scripts/python.exe eval/probe_recall_layer_attribution.py \
        --output experiments/rla_20260909.json

    # 零 Qdrant、零 LLM 的分格邏輯自測（含三條誤報對照）
    .venv/Scripts/python.exe eval/probe_recall_layer_attribution.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BUCKETS = ("dropped", "rrf_seats", "fetch_seats", "unreachable", "fine")
# 寬臂的探測上界。**刻意是絕對值**：它們回答「名額夠不夠」，不是生產組態的倍數。
# ⚠ 但凡是要印出來或拿來判斷的地方，一律引用這些名字，不要再寫一次數字。
_WIDE_RRF, _WIDER_RRF, _WIDE_FETCH = 100, 200, 300

def _labels() -> dict:
    """分格標籤。**數字一律從常數算出來，不寫死在字面裡**（2026-09-11）。

    ⚠ 生產的 `RRF_TOP_N_PRIMARY` 09-10 由 20 改成 30，而這裡原本寫著「進過 RRF-20」
      ⇒ 報表會印一個**與實際跑的組態不符**的標籤，而讀報表的人無從察覺。
      這與生產臂寫死常數是同一個病的兩半：**一個量錯、一個講錯**。
    """
    import rag_query as _rq
    return {
        "dropped":     f"① 刪錯了（進過 RRF-{_rq.RRF_TOP_N_PRIMARY}，回傳裡沒有）",
        "rrf_seats":   f"② RRF 名額不夠（RRF={_WIDE_RRF} 才進得來）",
        "fetch_seats": f"③ prefetch 名額不夠（FETCH_N={_WIDE_FETCH} 才進得來）",
        "unreachable": "④ 真的撈不到（三個候選集都沒有）",
        "fine":        "　 沒問題（gold 在 retrieve() 的回傳裡）",
    }


# ⚠ 保留 `_LABEL` 這個名字給零 Qdrant 的 selftest 用（它不該 import rq 的執行期狀態）。
_LABEL = {
    "dropped":     "① 刪錯了（進過生產 RRF 名額，回傳裡沒有）",
    "rrf_seats":   "② RRF 名額不夠",
    "fetch_seats": "③ prefetch 名額不夠",
    "unreachable": "④ 真的撈不到（三個候選集都沒有）",
    "fine":        "　 沒問題（gold 在 retrieve() 的回傳裡）",
}


def classify(in_post: bool, in_pre20: bool, in_pre100: bool, in_prewide: bool) -> str:
    """四格分類。**純函式、零 I/O**，好讓 `--selftest` 拿誤報對照餵它。

    ⚠ 順序即語意：先排除「回傳裡就有」（那不是召回失敗），再由下游往上游問。
    ⚠ 三個 `pre` 是**遞增包含**的關係（wide ⊇ 100 ⊇ 20）——但**不強制**：
      RRF 是排名融合，加大名額理論上不改變前段，實務上仍可能因 tie 而抖動。
      所以判斷一律用 `or`，不用「只在後者」。
    """
    if in_post:
        return "fine"
    if in_pre20:
        return "dropped"
    if in_pre100:
        return "rrf_seats"
    if in_prewide:
        return "fetch_seats"
    return "unreachable"


# ──────────────────────────────────────────────────────────────────────────────
# 實跑
# ──────────────────────────────────────────────────────────────────────────────
class _RecordingClient:
    """把 `query_points` 的回傳側錄下來，其餘一律轉發給真身。

    ⚠ 側錄在**client 這一層**而不是抄一份 `_query_points`：抄一份等於量尺自己重做一次
      三層降級 filter，那就變成「量尺與被測物脫鉤」——它會量到我寫的那份，不是生產那份。
    """

    def __init__(self, real):
        self._real = real
        self.seen: set = set()

    def query_points(self, *a, **kw):
        res = self._real.query_points(*a, **kw)
        for p in res.points:
            pl = p.payload or {}
            self.seen.add((str(pl.get("source", "")), str(pl.get("chunk_index", ""))))
        return res

    def __getattr__(self, name):
        return getattr(self._real, name)


class _StubReranker:
    """常數分數樁。**只用在 wide 兩臂**，理由見檔頭第三條紀律。"""

    def predict(self, pairs, **kw):
        return [0.0] * len(pairs)


def run(args) -> int:
    import rag_query as rq
    import chunk_gold as cg
    from probe_chunk_gold_recall import _eval_set, _key
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    gold_by_id = cg.by_id()
    eset = _eval_set(Path(args.eval_set))
    real_client = rq.make_qdrant_client()
    docs = cg.scan_docs(real_client, rq.COLLECTION_NAME)
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    reranker = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    stub = _StubReranker()

    print(f"[INFO] collection={rq.COLLECTION_NAME}  "
          f"RRF_TOP_N_PRIMARY={rq.RRF_TOP_N_PRIMARY}  FETCH_N={rq.FETCH_N}  "
          f"replay_cache={os.getenv('RAG_REPLAY_CACHE', '(關)')}")

    rows: dict = {}
    na: dict = defaultdict(list)
    for qid, q in eset.items():
        entry = gold_by_id.get(qid)
        if entry is None:
            na[q.get("category", "?")].append(qid)
            continue
        golds = {_key(s, ci) for s, ci in cg.gold_chunks(docs, entry)}
        if not golds:
            na[q.get("category", "?")].append(qid + "(gold未命中)")
            continue
        rows[qid] = {"category": q.get("category", "?"), "query": q["query"],
                     "n_gold": len(golds), "verdicts": [], "detail": []}
    if args.ids:
        rows = {k: v for k, v in rows.items() if k in set(args.ids)}
    if args.limit:
        rows = dict(list(rows.items())[:args.limit])

    def _one_arm(query: str, rr, rrf_n: int, fetch_n: int):
        """跑一次 retrieve，回傳 (見過的候選集合, 回傳序列)。常數用 monkeypatch 改。"""
        rec = _RecordingClient(real_client)
        o_rrf, o_fetch = rq.RRF_TOP_N_PRIMARY, rq.FETCH_N
        rq.RRF_TOP_N_PRIMARY, rq.FETCH_N = rrf_n, fetch_n
        try:
            pool, _ = rq.retrieve(query, bge, rr, rec, top_k=rq.RERANK_INPUT_N,
                                  enable_rewrite=rq.DEFAULT_ENABLE_REWRITE,
                                  translate_query_en=(not args.no_translate))
        finally:
            rq.RRF_TOP_N_PRIMARY, rq.FETCH_N = o_rrf, o_fetch
        return rec.seen, [_key(c["source"], c["chunk_index"]) for c in pool]

    for rnd in range(args.repeat):
        print(f"\n── 第 {rnd + 1}/{args.repeat} 輪 " + "─" * 52)
        for qid, r in rows.items():
            golds = {_key(s, ci) for s, ci in cg.gold_chunks(docs, gold_by_id[qid])}
            # ⚠ **生產臂的常數一定要從 `rq` 讀，不可以寫死**（2026-09-11 踩到）：
            #   這裡原本是 `_one_arm(..., 20, 60)`，而 `RRF_TOP_N_PRIMARY` 於 2026-09-10
            #   改成 30 ⇒ 這支從那一刻起量的是**沒有人在用的組態**，而外觀完全正常。
            #   同一個形狀本 repo 已踩三次（閘門① 的 `_gate()` 是抄寫、
            #   `probe_chunk_gold_recall` 把「上限 20」寫死在輸出字串）。
            #   通則：**量尺凡是要重現生產行為的地方，一律 import 不抄寫。**
            pre20, post = _one_arm(r["query"], reranker,
                                   rq.RRF_TOP_N_PRIMARY, rq.FETCH_N)
            hit_post = any(k in golds for k in post)
            hit_pre20 = bool(golds & pre20)
            # 兩條 wide 臂只在需要時才跑（省時間；不跑等於不可能翻成 ②③，
            # 而那只會讓判定更往 ④ 靠 ＝ 誤報方向偏保守）。
            hit100 = hitw = False
            if not hit_post and not hit_pre20:
                # ⚠ 寬臂**刻意保持絕對值**（100／200＋300）：它們是「名額夠不夠」的
                #   上界探測，不是生產組態的倍數。但下面印出來的標籤要用真實數字，
                #   不可以寫死在文字裡（同上）。
                pre100, _ = _one_arm(r["query"], stub, _WIDE_RRF, rq.FETCH_N)
                hit100 = bool(golds & pre100)
                if not hit100:
                    prew, _ = _one_arm(r["query"], stub, _WIDER_RRF, _WIDE_FETCH)
                    hitw = bool(golds & prew)
            v = classify(hit_post, hit_pre20, hit100, hitw)
            r["verdicts"].append(v)
            r["detail"].append({"post": len(post), "pre20": len(pre20),
                                "hit_post": hit_post, "hit_pre20": hit_pre20,
                                "hit_rrf100": hit100, "hit_wide": hitw})
            if v != "fine":
                print(f"  [{qid:<7}/{r['category']:<10}] {_labels()[v]}  "
                      f"pool={len(post)} pre20={len(pre20)}")

    _report(rows, na, args.repeat)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(
            {"_meta": {"collection": rq.COLLECTION_NAME, "repeat": args.repeat,
                       "translate": not args.no_translate,
                       "arms": {"prod": [20, 60], "rrf100": [100, 60], "wide": [200, 300]},
                       "na": dict(na)},
             "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] 寫出 {args.output}")
    return 0


def _report(rows: dict, na: dict, repeat: int) -> None:
    print("\n" + "=" * 72)
    print(f"召回失敗的層級歸因｜{len(rows)} 題 × {repeat} 輪"
          f"｜N/A {sum(len(v) for v in na.values())} 題（不併進分母）")
    print("=" * 72)
    flip = [qid for qid, r in rows.items() if len(set(r["verdicts"])) > 1]
    tally: dict = defaultdict(list)
    for qid, r in rows.items():
        # 跨輪翻面的題**不歸任何一格**：那是英譯噪音，塞進某一格會製造假的確定性。
        tally[r["verdicts"][0] if qid not in flip else "unstable"].append(qid)
    for b in BUCKETS:
        ids = sorted(tally.get(b, []))
        print(f"\n{_labels()[b]}：{len(ids)} 題")
        if ids and b != "fine":
            for qid in ids:
                print(f"    {qid:<8} {rows[qid]['category']:<10} {rows[qid]['query'][:46]}")
        elif ids:
            print(f"    {' '.join(ids)}")
    if flip:
        print(f"\n跨輪翻面（英譯噪音，不歸格）：{len(flip)} 題 {sorted(flip)}")
    print("\n  ⚠ 「沒問題」那一格是**誤報對照**：多數題的池本來就 < 20，而 gold 好端端在裡面"
          "\n    ——所以「池沒滿」本身不是缺陷，這一格證明本檔沒有把它當成缺陷。")
    print("  ⚠ ① 的每一筆都要**人工複核是哪一層刪的**：`pre` 取三層 filter 的聯集，"
          "\n    偏誤方向會讓 ① 高估。只讀計數會把「那一層本來就該刪」算成缺陷。")
    print("  ⚠ gold 是聯集、偏大 → 判成 ①~④ 是硬證據，判成「沒問題」不是健康證明。")


# ──────────────────────────────────────────────────────────────────────────────
def prod_arm_is_not_hardcoded(src: str) -> bool:
    """生產臂有沒有把常數寫死。純函式，供 `--selftest` 餵誤報對照。

    ⚠ **這條守的是一個會靜默失效的形狀**：量尺把被測物的組態抄成字面常數之後，
      改生產的那一刻兩者就脫鉤，而**量尺照樣跑、照樣印數字、外觀完全正常**。
      2026-09-11 實際發生過：生產 20 → 30，這支還在量 20。
    """
    import re as _re
    m = _re.search(r"pre20,\s*post\s*=\s*_one_arm\((.*?)\)", src, _re.S)
    if not m:
        return False
    args = m.group(1)
    return "rq.RRF_TOP_N_PRIMARY" in args and "rq.FETCH_N" in args


def _selftest() -> int:
    """⚠ 判別力全在誤報對照④⑤⑥：陽性那三條，一個「一律回 dropped」的實作也會過。"""
    ok = fail = 0

    def _a(name, cond, note=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  OK    {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {note}")

    _a("① 進過 RRF-20 但回傳沒有 → dropped",
       classify(False, True, False, False) == "dropped")
    _a("② 不在 RRF-20、RRF=100 進得來 → rrf_seats",
       classify(False, False, True, False) == "rrf_seats")
    _a("③ 只有 wide 進得來 → fetch_seats",
       classify(False, False, False, True) == "fetch_seats")
    _a("④ **誤報對照**：回傳裡有 gold → fine，即使 pre 全 True（那不是召回失敗）",
       classify(True, True, True, True) == "fine")
    _a("⑤ **誤報對照**：三個候選集都沒有 → unreachable，不可以塞給 dropped",
       classify(False, False, False, False) == "unreachable")
    _a("⑥ **誤報對照**：`in_post` 優先於一切——池沒滿／pre 沒命中都不影響",
       classify(True, False, False, False) == "fine")
    _a("⑦ 四格互斥且窮盡（16 種輸入組合各自落在恰好一格）",
       len({classify(*[bool(i >> b & 1) for b in range(4)]) for i in range(16)} - set(BUCKETS)) == 0)
    _a("⑧ **誤報對照**：wide 為 True 不會蓋掉更下游的 dropped 判定",
       classify(False, True, True, True) == "dropped")

    # ── 生產臂不可寫死常數（2026-09-11 加，判別力全在誤報對照）──────────────
    _src = Path(__file__).read_text(encoding="utf-8")
    _a("⑨ 生產臂從 `rq` 讀常數，沒有寫死（09-11 踩到：生產改 30 而這支還在量 20）",
       prod_arm_is_not_hardcoded(_src))
    _a("⑨b **誤報對照**：寫死數字的版本必須被抓到（否則這條是恆真的）",
       not prod_arm_is_not_hardcoded(
           'pre20, post = _one_arm(r["query"], reranker, 20, 60)'))
    _a("⑨c **誤報對照**：只讀對一半也算違規（抄一個、import 一個）",
       not prod_arm_is_not_hardcoded(
           'pre20, post = _one_arm(r["query"], reranker, rq.RRF_TOP_N_PRIMARY, 60)'))
    _a("⑨d **誤報對照**：整段不存在時必須回 False，不可以真空成立",
       not prod_arm_is_not_hardcoded("def unrelated(): pass"))
    _a("⑩ 標籤裡不得寫死生產的名額數字（報表會講一個與實跑不符的組態）",
       all("RRF-20" not in v and "RRF-30" not in v for v in _LABEL.values()))

    print(f"\n  PASS {ok}  FAIL {fail}")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-set", default="eval/eval_set.json")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--no-translate", action="store_true",
                    help="零 LLM 臂（**非生產組態**，只能自己跟自己比）")
    ap.add_argument("--no-replay-cache", action="store_true")
    ap.add_argument("--output")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not args.no_replay_cache and not os.getenv("RAG_REPLAY_CACHE"):
        os.environ["RAG_REPLAY_CACHE"] = "eval/replay_cache.json"
        os.environ.setdefault("RAG_REPLAY_READONLY", "1")
        print("[INFO] 自動掛上 eval/replay_cache.json（唯讀）釘死英譯")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
