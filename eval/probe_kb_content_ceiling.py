# -*- coding: utf-8 -*-
"""probe_kb_content_ceiling.py — **強制放行的那些子問題，是「KB 真的沒有」還是「沒撈到」**（2026-09-09）。

## 為什麼需要這一支，以及它**不是**什麼

2026-09-08／09-09 兩輪量到 `forced_pass`（Grader 連 `MAX_REWRITES+1` 輪都判不足、系統照樣
作答且零保留）穩定核心 5 題，同時 `kb_unfixable_exit` 在 **225 個子問題裡一次都沒被設起來**。
當時的判讀是「那個旗標從來不觸發，這是第一順位」——**那句話的前提是錯的**：

> `kb_unfixable` 只在 `_check_sufficiency` 的 `if _live:` 分支裡設，而且只在
> **時效改判**（`_classify_staleness`）觸發時才可能為 True。它是**時效**天花板的旗標，
> 判準是「候選池最新來源已過期，而整個 collection 的天花板也過期」。
> 題庫 65 題**全是財報題**（新聞已於 2026-08-19 移出 KB 與題庫），`realtime_need`
> 依設計就該是 `none` ⇒ **這個旗標在這個題庫上結構性不可能觸發，0 是正確行為不是缺陷。**

而兩輪 28 筆 `forced_pass_detail` 的 `missing` **沒有一筆**在講時效，全部在講**內容**
（「10-K 沒寫 CUDA 推出年份」「沒有中國市場出貨量」「沒有 PC 市佔描述」）。
**內容天花板目前完全沒有旗標**，於是它與「這次沒撈到」在系統裡是同一件事。

**這支量的就是那個分界**：把 forced_pass 的子問題各跑兩臂——

| 臂 | 組態 |
|---|---|
| `prod` | 生產的 `_retrieve_chunks` ＋ 生產的 `_check_sufficiency` |
| `wide` | `FETCH_N` 60→180、`RRF_TOP_N_PRIMARY` 20→50、`POOL_RETURN_K` 5→15 |

⚠ **`RERANK_INPUT_N` 刻意不動**：它管「重排幾顆」不是「召回幾顆」，對「KB 裡到底有沒有」
  不增加新資訊，而它是這台機器上 CPU 的成本主宰（實測 150 讓單次檢索 33s → 46s）。
⚠ **跨輪只重跑 Grader，檢索池凍住**（單次生產檢索實測 **33s**）⇒ 跨輪翻面量的是
  **Grader 的穩定性**，不是端到端穩定性（檢索側的英譯 LLM 變異被刻意排除）。

`wide` 救得起來 ⇒ **檢索問題**（名額／召回），修檢索有用。
`wide` 也救不起來 ⇒ 再看 `kb_unfixable`：

| 格 | 意思 | 系統有沒有旗標 |
|---|---|---|
| `ceiling_recency` | `kb_unfixable` 觸發 ＝ **時效**天花板 | **有**（`kb_unfixable_exit`），不是缺口 |
| `ceiling_content` | KB **內容**裡沒有這個事實 | **沒有** ← 真正的缺口，也是分母 |

⚠ **這兩格第一版被混成一格，實跑當場抓到**：三筆真正的即時題（「蘋果現在市值」
  「亞馬遜最近跟哪家 AI 公司搭上線」）因此被算進「誤殺率 3/15」，而它們是**時效旗標
  正確觸發、不是誤殺**（真實誤殺率是 **0/15**）。混在一起會讓內容旗標的分母灌水。

## ⚠ 不對稱在哪：這一格判錯的代價不對等

- **內容天花板誤判成「還有救」** ＝ 現況（白燒 `MAX_REWRITES` 輪、然後硬答）。**便宜。**
- **「其實撈得到」誤判成內容天花板** ＝ 日後若照這個旗標拒答，就是**白白拒答一個
  財報裡明明寫著的東西**，比現況更糟。**這才是要防的方向。**

⚠ **所以陰性對照不可省**（`--negatives N`）：拿生產判 `sufficient` 的子問題走**完全相同**的
  兩臂。少了它，一個「一律回 ceiling_candidate」的實作會在陽性那半滿分，而它正是最危險的
  失敗方式。陰性落進 `ceiling_candidate` 的比率就是「這個旗標會誤殺多少」。

⚠ **`ceiling_candidate` 是候選不是結論**：Grader 自己可能就判錯了（`wide` 臂只證明
  「給它 3 倍的證據它仍說不夠」，不證明 KB 裡真的沒有）。**這支不宣稱任何一題「KB 沒有」**。

⚠ **MoE 不固定專家路由 ⇒ 下結論前 `--repeat 3` 以上**，看逐題 k/n，不看單輪。
⚠ **`RAG_REPLAY_CACHE` 開著會讓每輪命中同一份決策 ＝ 假的穩定** → 本檔開跑前中止（exit 2），
  同 `probe_route_classification` 的理由。
⚠ **`wide` 臂刻意不動 hard filter**：那會把別家公司的 chunk 放進來，於是「救起來」可能只是
  Grader 被別家的數字騙了。要量 filter 的影響是另一支的事。
⚠ 每筆同時記下 `realtime_need` 與 `kb_unfixable`——**那是上面那段更正的直接證據**，
  而不是只靠讀碼推論。

用法：
    .venv/Scripts/python.exe eval/probe_kb_content_ceiling.py \\
        experiments/agentic/gj_65q_denominators_r2_20260909.json \\
        --repeat 3 --negatives 10 --output experiments/kb_ceiling_20260909.json

    .venv/Scripts/python.exe eval/probe_kb_content_ceiling.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

BUCKETS = ("prod_sufficient", "rescued_by_width", "ceiling_recency", "ceiling_content")


def classify(prod_ok: bool, wide_ok: bool, recency_flag: bool = False) -> str:
    """兩臂 ＋ `kb_unfixable` → 四格。純函式，供 `--selftest` 餵誤報對照。

    ⚠ **`prod_ok` 優先**：生產就判夠了的，不論寬臂怎麼說都不是天花板問題——
      寬臂在那種情況下的判定沒有意義（它看的是不同的候選池）。
    ⚠ **`rescued_by_width` 與兩種 ceiling 不可合併**：前者修檢索有用、後者修檢索無效。
    ⚠ **兩種 ceiling 也不可合併，這是第一版的量尺錯**（2026-09-09 實跑當場抓到）：
      · `ceiling_recency` ＝ `kb_unfixable` 觸發 ＝ **時效**天花板，**系統已經有旗標**
        （`_classify_staleness` → `kb_unfixable_exit`），不是缺口。
      · `ceiling_content` ＝ KB **內容**裡沒有這個事實，**目前完全沒有旗標** ← 真正的缺口。
      第一版把兩者混成一格，於是三筆真正的即時題（「蘋果現在市值」「亞馬遜最近跟哪家 AI
      公司搭上線」）被算進「誤殺率 3/15」——**那三筆是時效旗標正確觸發，不是誤殺**。
      混在一起會讓「要不要做內容旗標」的分母灌水。
    """
    if prod_ok:
        return "prod_sufficient"
    if wide_ok:
        return "rescued_by_width"
    return "ceiling_recency" if recency_flag else "ceiling_content"


# ── 寬臂的放大倍率。⚠ 這幾個數字是**判斷不是證據**：沒有任何斷言分得出 3 倍與 4 倍的差別
#    （同 `_MARKER_WINDOW` 的教訓）。它們只需要「明顯比生產寬」。
# ⚠ **刻意不動 `RERANK_INPUT_N`**：它管的是「重排幾顆」不是「召回幾顆」，而 cross-encoder
#   在這台機器上是 CPU 的成本主宰——實測 `RERANK_INPUT_N=150` 讓單次檢索從 33s 變 46s，
#   而它對「KB 裡到底有沒有」這個問題**不增加任何新資訊**（那 100 顆本來就在池子裡）。
_WIDE = {"FETCH_N": 180, "RRF_TOP_N_PRIMARY": 50}
_WIDE_POOL_RETURN_K = 15


def _retrieve_arm(task: str, wide: bool) -> list[dict]:
    """跑一臂的**檢索**（生產路徑，只改名額常數）。

    ⚠ **刻意與 Grader 分開**：單次生產檢索實測 **33s**（BGE-M3 ＋ cross-encoder，CPU），
      而跨輪要重跑的只有 Grader（那才是有 LLM 變異的一側）。合在一起會讓 `--repeat 3`
      把 33s 乘三次，光檢索就 1 小時以上。
    """
    import rag_query as rq
    import agentic_rag_version as ar

    saved = {k: getattr(rq, k) for k in _WIDE}
    try:
        if wide:
            for k, v in _WIDE.items():
                setattr(rq, k, v)
        return ar._retrieve_chunks(task, attributable=False)
    finally:
        for k, v0 in saved.items():
            setattr(rq, k, v0)


def _grade(task: str, pool: list[dict], scope: str, mode: str, wide: bool) -> dict:
    """跑一臂的 **Grader**（生產 `_check_sufficiency`）。"""
    import agentic_rag_version as ar

    saved_k = ar.POOL_RETURN_K
    try:
        if wide:
            ar.POOL_RETURN_K = _WIDE_POOL_RETURN_K
        v = ar._check_sufficiency(task, pool, scope, mode)
    finally:
        ar.POOL_RETURN_K = saved_k
    return {"sufficient": bool(v.get("sufficient")), "pool": len(pool),
            "missing": (v.get("missing") or "")[:200],
            "realtime_need": v.get("realtime_need", "none"),
            "kb_unfixable": bool(v.get("kb_unfixable", False))}


def _collect_tasks(path: Path, n_neg: int, seed: int) -> tuple[list, list]:
    """從結果檔取出 ① forced_pass 的子問題（陽性）② 生產判夠的子問題（陰性對照）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    pos, neg = [], []
    for r in data.get("records", []):
        es = r.get("exec_stats") or {}
        for t in (es.get("forced_pass_detail") or []):
            if t.get("task"):
                pos.append({"qid": r.get("id"), "category": r.get("category"),
                            "task": t["task"], "orig_missing": (t.get("missing") or "")[:200]})
        # ⚠ 陰性對照的來源：**同一份結果檔裡生產判夠的子問題**。刻意不自己造題——
        #   自造的題會系統性比較好答，於是「誤殺率」被低估（量尺自備輸入的老問題）。
        if es and not es.get("forced_pass") and not es.get("crashed") and es.get("sufficient"):
            for q in (r.get("agentic_sub_queries") or []):
                neg.append({"qid": r.get("id"), "category": r.get("category"), "task": q})
    random.Random(seed).shuffle(neg)
    return pos, neg[:n_neg]


def run(args) -> int:
    if os.getenv("RAG_REPLAY_CACHE"):
        print("[ABORT] `RAG_REPLAY_CACHE` 開著 → 每輪都會命中同一份 Grader 決策，"
              "量到的『穩定』是假的。取消該 env 再跑。")
        return 2

    import agentic_rag_version as ar
    mode = args.freshness_mode
    scope = ar._build_temporal_contract(mode)
    pos, neg = _collect_tasks(Path(args.results), args.negatives, args.seed)
    items = ([dict(x, arm_kind="positive") for x in pos]
             + [dict(x, arm_kind="negative") for x in neg])
    print(f"[INFO] results={args.results}  freshness={mode}  collection={ar.rq.COLLECTION_NAME}")
    print(f"[INFO] 陽性（forced_pass 子問題）{len(pos)} 筆｜陰性對照（生產判夠）{len(neg)} 筆"
          f"｜repeat={args.repeat}")
    if not neg:
        print("  ⚠ **陰性對照是空的** → 這一輪只量得到陽性那半，"
              "而『一律回 ceiling_candidate』的實作會滿分。判讀時不可略過這一行。")

    # ── 第一階段：每個 (item, arm) 各檢索**一次**（33s/次，跨輪重跑純屬浪費）。
    #    ⚠ 這表示跨輪量到的是 **Grader 的穩定性**，不是端到端穩定性——檢索側的英譯 LLM
    #      變異被刻意凍住了（同輸入實測 47/100 題 top-8 不同）。判讀時不可讀成後者。
    t0 = time.time()
    pools: dict = {}
    for i, it in enumerate(items, start=1):
        key = f"{it['qid']}::{it['task'][:60]}"
        pools[key] = (_retrieve_arm(it["task"], wide=False),
                      _retrieve_arm(it["task"], wide=True))
        print(f"  [檢索 {i}/{len(items)}] {it['qid']:8} {it['arm_kind'][:3]} "
              f"prod_pool={len(pools[key][0])} wide_pool={len(pools[key][1])}")
    print(f"[INFO] 檢索完成 {time.time() - t0:.0f}s")

    # ── 第二階段：Grader 跨輪重跑。
    rows: dict = {}
    for rnd in range(args.repeat):
        for i, it in enumerate(items, start=1):
            key = f"{it['qid']}::{it['task'][:60]}"
            pa, pb = pools[key]
            a = _grade(it["task"], pa, scope, mode, wide=False)
            b = _grade(it["task"], pb, scope, mode, wide=True)
            # ⚠ 兩臂**任一**觸發就算時效天花板：漏判的方向會把時效算進內容分母（灌水），
            #   而多判只會讓內容分母偏小＝保守。誤報方向刻意選保守的那邊。
            v = classify(a["sufficient"], b["sufficient"],
                         a["kb_unfixable"] or b["kb_unfixable"])
            row = rows.setdefault(key, {**{k: it[k] for k in
                                           ("qid", "category", "task", "arm_kind")},
                                       "verdicts": [], "detail": []})
            row["verdicts"].append(v)
            row["detail"].append({"round": rnd, "prod": a, "wide": b})
            print(f"  [r{rnd + 1} {i}/{len(items)}] {it['qid']:8} {it['arm_kind'][:3]} "
                  f"prod={a['sufficient']}({a['pool']}) wide={b['sufficient']}({b['pool']}) "
                  f"need={a['realtime_need']} → {v}")
    print(f"\n[INFO] 全部 {time.time() - t0:.0f}s")

    _report(rows, args.repeat)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(
            {"_meta": {"results": args.results, "repeat": args.repeat,
                       "freshness_mode": mode, "collection": ar.rq.COLLECTION_NAME,
                       "wide": {**_WIDE, "POOL_RETURN_K": _WIDE_POOL_RETURN_K},
                       "note": "跨輪只重跑 Grader，檢索池凍住（單次生產檢索 33s）"},
             "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] 寫出 {args.output}")
    return 0


def _majority(vs: list[str]) -> str:
    return Counter(vs).most_common(1)[0][0]


def _report(rows: dict, repeat: int) -> None:
    print("\n" + "=" * 74)
    print("forced_pass 的子問題：檢索問題，還是內容天花板？")
    print("=" * 74)
    for kind, title in (("positive", "陽性（forced_pass 的子問題）"),
                        ("negative", "**陰性對照**（生產判夠的子問題）")):
        sub = {k: r for k, r in rows.items() if r["arm_kind"] == kind}
        if not sub:
            print(f"\n{title}：0 筆")
            continue
        cnt = Counter(_majority(r["verdicts"]) for r in sub.values())
        print(f"\n{title}：{len(sub)} 筆")
        for b in BUCKETS:
            print(f"  {b:<20} {cnt.get(b, 0):3}／{len(sub)}")
        unstable = [k for k, r in sub.items() if len(set(r["verdicts"])) > 1]
        print(f"  跨輪翻面            {len(unstable):3}／{len(sub)}"
              + (f"  ← {', '.join(sorted({rows[k]['qid'] for k in unstable}))}" if unstable else ""))

    neg = {k: r for k, r in rows.items() if r["arm_kind"] == "negative"}
    # ⚠ **只有 `ceiling_content` 算誤殺**：`ceiling_recency` 是時效旗標正確觸發
    #   （那些題 KB 真的補不了，系統本來就該早退），把它算進來會讓分母灌水。
    misfire = [k for k, r in neg.items() if _majority(r["verdicts"]) == "ceiling_content"]
    print("\n" + "-" * 74)
    if neg:
        n_rec = sum(1 for r in neg.values() if _majority(r["verdicts"]) == "ceiling_recency")
        print(f"⚠ **誤殺率（這一格才是決定要不要做內容旗標的那個數字）**："
              f"{len(misfire)}／{len(neg)} 的陰性對照被判成 `ceiling_content`。")
        print(f"   （另有 {n_rec} 筆落在 `ceiling_recency` ＝ **時效旗標正確觸發，不是誤殺**——"
              "\n     那些是真的即時題，系統本來就該早退。第一版把兩者混成一格，"
              "\n     誤殺率因此從 0 被灌水成 3/15。）")
        if misfire:
            print("   誤殺的：" + " ".join(sorted({neg[k]["qid"] for k in misfire})))
        print("   ⚠ 「內容天花板誤判成還有救」＝ 現況（白燒幾輪然後硬答，便宜）；"
              "\n     「撈得到誤判成天花板」＝ 白白拒答財報裡明明寫著的東西，**比現況更糟**。")
    else:
        print("⚠ 沒有陰性對照 ⇒ **這一輪的陽性數字不可用來支持任何旗標**。")

    # ⚠ 這幾行是「`kb_unfixable` 從來不觸發是不是缺陷」的**直接證據**，不是讀碼推論。
    needs = Counter()
    unfix = 0
    for r in rows.values():
        for d in r["detail"]:
            for arm in ("prod", "wide"):
                needs[d[arm]["realtime_need"]] += 1
                unfix += bool(d[arm]["kb_unfixable"])
    print("\n" + "-" * 74)
    print(f"`realtime_need` 分布（{sum(needs.values())} 次 Grader 判定）：{dict(needs)}")
    print(f"`kb_unfixable` 為 True 的次數：{unfix}")
    flips = [k for k, r in rows.items()
             if len({d[a]["kb_unfixable"] for d in r["detail"] for a in ("prod", "wide")}) > 1]
    print("  ⚠ `kb_unfixable` 是**時效**天花板的旗標（只在 live ＋ 時效改判時才可能為 True），"
          "\n    而它**確實會觸發**——2026-09-09 實測 17 次，全在真正的即時／新聞子問題上"
          "\n    （「蘋果現在市值」「亞馬遜最近跟哪家 AI 公司搭上線」）。"
          "\n    ⚠ 所以「它在生產結果檔裡是 0」**不能**解釋成「結構上不可能觸發」——"
          "\n      那是我推導錯的第二個版本。生產為什麼是 0 目前**仍未解釋**，"
          "\n      而下面這一格是最可能的線索。")
    print(f"  ⚠ **旗標跨輪不穩定**：{len(flips)} 筆的 `kb_unfixable` 在同一份凍結的候選池上"
          f"跨輪翻面\n    （{' '.join(sorted({rows[k]['qid'] for k in flips}))}）。"
          "\n    這個旗標要在**對的那一輪**亮才會早退，閃爍等於大部分時候不亮。")
    print("  ⚠ **內容**天花板則是**完全沒有旗標**——那才是上面那張表在量的東西。")
    print("\n⚠ 跨輪只重跑 **Grader**（檢索池凍住）⇒ 這裡的『跨輪翻面』量的是 Grader 的"
          "\n  穩定性，**不是端到端穩定性**（檢索側的英譯 LLM 變異被刻意排除）。")
    print(f"⚠ repeat={repeat}"
          + ("：單輪的點估計不可引用（MoE 不固定專家路由）。" if repeat < 3
             else "：逐題看 k/n，不要只看多數決。"))


def rescore(path: str) -> int:
    """從本檔自己寫出的 JSON **重算判定**（零 LLM、零檢索）。

    ⚠ 存在的理由：判定規則改了（例如把 ceiling 拆成兩格）時，不該為了重新分格
      再燒一次 45 分鐘的 LLM＋檢索。`detail` 裡逐輪逐臂的原始欄位就足夠重算。
    ⚠ **只重算判定，不重算任何原始欄位**——那些是那次跑的事實，改了就變成偽造。
    """
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = d["rows"]
    for r in rows.values():
        r["verdicts"] = [classify(x["prod"]["sufficient"], x["wide"]["sufficient"],
                                  x["prod"]["kb_unfixable"] or x["wide"]["kb_unfixable"])
                         for x in r["detail"]]
    print(f"[INFO] 從 {path} 重算判定（零 LLM）")
    _report(rows, d.get("_meta", {}).get("repeat", len(next(iter(rows.values()))["verdicts"])))
    d.setdefault("_meta", {})["rescored_with"] = list(BUCKETS)
    Path(path).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] 更新 {path}")
    return 0


def _selftest() -> int:
    """⚠ 判別力全在誤報對照③④⑤：陽性那一條，一個「一律回 ceiling_candidate」的實作也會過。"""
    ok = fail = 0

    def _a(name, cond, note=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  OK    {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {note}")

    _a("① 兩臂都不夠、無時效旗標 → ceiling_content（真正的缺口）",
       classify(False, False, False) == "ceiling_content")
    _a("② 寬臂救起來 → rescued_by_width（檢索問題，修檢索有用）",
       classify(False, True, False) == "rescued_by_width")
    _a("③ **誤報對照**：生產就判夠 → prod_sufficient，**不可以**因為寬臂也說夠就併進 ①",
       classify(True, True, False) == "prod_sufficient")
    _a("④ **誤報對照**：生產判夠、寬臂反而說不夠 → 仍是 prod_sufficient"
       "（寬臂看的是不同的池，那個判定沒有意義）",
       classify(True, False, False) == "prod_sufficient")
    _a("⑤ **誤報對照**：`rescued_by_width` 與 ceiling 不得是同一格"
       "（合併＝把名額不夠誤診成 KB 沒有）",
       classify(False, True, False) != classify(False, False, False))
    _a("⑥ **誤報對照**：`kb_unfixable` 觸發 → `ceiling_recency`，**不可以**併進 "
       "`ceiling_content`（那是時效旗標正確觸發，系統已經有它；混進來＝內容分母灌水）",
       classify(False, False, True) == "ceiling_recency")
    _a("⑥b **誤報對照**：時效旗標不得凌駕前兩格——生產判夠／寬臂救起來時仍照原判",
       classify(True, False, True) == "prod_sufficient"
       and classify(False, True, True) == "rescued_by_width")
    _a("⑥c 四格都取得到（沒有恆不觸發的死格）",
       {classify(a, b, c) for a in (True, False) for b in (True, False)
        for c in (True, False)} == set(BUCKETS))
    _a("⑥d **回歸護欄**：`recency_flag` 預設 False ⇒ 舊的兩引數呼叫行為不變"
       "（rescore 舊 JSON 時不會憑空多出 recency）",
       classify(False, False) == "ceiling_content")

    # ⑦ 陰性對照的**取樣**：只收生產判夠、且沒有 forced_pass／crashed 的題。
    #    ⚠ 少了這條，一份「全是 forced_pass」的結果檔會產出空的陰性對照而沒有人發現。
    import tempfile
    fake = {"records": [
        {"id": "good-1", "category": "lexical", "agentic_sub_queries": ["Q1", "Q2"],
         "exec_stats": {"sufficient": 2, "forced_pass": 0, "crashed": 0,
                        "forced_pass_detail": []}},
        {"id": "bad-1", "category": "semantic", "agentic_sub_queries": ["Q3"],
         "exec_stats": {"sufficient": 0, "forced_pass": 1, "crashed": 0,
                        "forced_pass_detail": [{"task": "Q3", "missing": "m"}]}},
        {"id": "crash-1", "category": "mixed", "agentic_sub_queries": ["Q4"],
         "exec_stats": {"sufficient": 1, "forced_pass": 0, "crashed": 1,
                        "forced_pass_detail": []}},
    ]}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.json"
        p.write_text(json.dumps(fake), encoding="utf-8")
        pos, neg = _collect_tasks(p, 10, 0)
    _a("⑦ 陽性只收 forced_pass_detail 的子問題",
       [x["task"] for x in pos] == ["Q3"], f"實得 {[x['task'] for x in pos]}")
    _a("⑧ **誤報對照**：陰性對照不得收到 forced_pass 那一題的子問題",
       "Q3" not in {x["task"] for x in neg}, f"實得 {sorted(x['task'] for x in neg)}")
    _a("⑨ **誤報對照**：陰性對照不得收到**崩潰**題的子問題"
       "（那一題的 sufficient 不代表它真的跑完了）",
       "Q4" not in {x["task"] for x in neg}, f"實得 {sorted(x['task'] for x in neg)}")
    _a("⑩ 陰性對照取得到正常題的子問題", {"Q1", "Q2"} <= {x["task"] for x in neg},
       f"實得 {sorted(x['task'] for x in neg)}")
    _a("⑪ **誤報對照**：`--negatives 0` 要真的給出空清單（報表才會印那句警告）",
       _collect_tasks_len0())

    print(f"\n  PASS {ok}  FAIL {fail}")
    return 1 if fail else 0


def _collect_tasks_len0() -> bool:
    import tempfile
    fake = {"records": [{"id": "g", "category": "lexical", "agentic_sub_queries": ["Q1"],
                         "exec_stats": {"sufficient": 1, "forced_pass": 0, "crashed": 0,
                                        "forced_pass_detail": []}}]}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.json"
        p.write_text(json.dumps(fake), encoding="utf-8")
        _, neg = _collect_tasks(p, 0, 0)
    return neg == []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="?", help="agentic 結果檔（要有 exec_stats.forced_pass_detail）")
    ap.add_argument("--repeat", type=int, default=1, help="跑幾輪（下結論前 ≥3）")
    ap.add_argument("--negatives", type=int, default=10, help="陰性對照抽幾筆（0 ＝ 不抽，但會警告）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--freshness-mode", default="live", choices=("live", "snapshot"))
    ap.add_argument("--output")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rescore", metavar="JSON",
                    help="從本檔既有的輸出 JSON 重算判定（零 LLM、零檢索）")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if args.rescore:
        return rescore(args.rescore)
    if not args.results:
        ap.error("要給結果檔路徑，或用 --selftest")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
