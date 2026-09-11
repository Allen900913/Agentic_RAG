"""probe_route_classification.py — Planner 判的 `route` 準不準（含 LLM、非零噪音；零網路）。

`verify_web_gate_isolation.py` 閘門⑪ 驗的是「route 這個欄位有沒有被正確地**接線**」——真值表、
分派、升級、eval 隔離。它**完全不問**「Planner 判得對不對」。這支補的就是那一格。

**為什麼需要它（被一次真實的翻面逼出來的）**：2026-09-02 的 after / after2 兩輪之間，
`sem-06`「Amazon 在 AWS、Prime 與電商業務上面臨哪些反壟斷或監管風險？」——一題純 10-K
Item 1A 的題目——**從全 `kb` 翻成全 `web`**。同一份碼、同一題、相反的答案（`gpt-oss-120b`
是 MoE，temp=0 只固定取樣、不固定專家路由）。把路由從自由文字搬進欄位讓它**可稽核**，
**不代表它準**；那是兩件事，而在這支之前後者沒有任何量尺。

---

## 三個指標，不可合併（判準是**代價不對稱**，不是整體準確率）

| 指標 | 定義 | 為什麼要分開看 |
|---|---|---|
| `危險誤判` | **flow 題**被判成純 `kb` | KB **依設計沒有新聞**（`RAW_EXCLUDE_DIRS`）→ 拿舊財報回答「最近有什麼消息」，**有引用、有數字、零揭露**，五道 validator 一道都不會響 |
| `浪費誤判` | **record 題**被判成 `web`／`both` | 多燒一次 Tavily；更重要的是 `route=web` **不撈 KB** → 丟掉期別 metadata 與引用鏈，答案變得不可驗證 |
| `不穩定` | 同一題跨輪的 route 集合不一致 | 兩輪都對也可能只是運氣。**這一格與前兩格正交**：可以「每輪都錯」（穩定地錯）也可以「時對時錯」 |

⚠ **`不穩定` 不可併進誤判**：after2 那次剛好只有 1 題誤判、達標了事前寫死的判準，
   但**換了一題**（after 是 `mix-01` 對、`sem-06` 錯；再測 3 輪反而是 `sem-01` 錯）。
   達標是運氣不是穩定性——只看誤判數會把這件事整個蓋掉。

## 陰性對照

`record` 那 7 題**就是**陰性對照：少了它，一個「一律回 web」的 Planner 會在 flow 那半滿分。
`mixed` 另外報但**判準較弱**（只要求「至少一個 web-ish ＋ 至少一個 kb-ish，或有 both」），
因為「這半該不該上網」在一句話裡混著兩個提問時沒有唯一正解。

## ⚠ 兩個會讓這支靜默失效的陷阱

1. **`RAG_REPLAY_CACHE` 開著會讓每一輪都命中同一份快取** → 報出「完全穩定」，
   而那是重放不是量測。本檔**開跑前檢查並中止**（exit 2），不是印個警告了事。
2. `_build_temporal_contract` 會嵌入 KB Coverage Snapshot（讀 Qdrant）。那是 prompt 的一部分，
   **必須是真的**——用假的 coverage 量出來的路由不是生產會做的決定。所以這支**讀 Qdrant**，
   不是純離線的。

用法：
    RAG_COLLECTION=us_stock_rag_edgar_multiyear \\
      .venv/Scripts/python.exe -u eval/probe_route_classification.py --repeat 3
    ... --only flow,record          # 預設；加上 mixed 會多 22 題
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):  # ⚠ stderr 也要轉：traceback 走 stderr，只轉 stdout 的話「印到一半 crash」照樣發生（2026-09-11 閘門 H1）

    _s.reconfigure(encoding="utf-8", errors="replace")
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import agentic_rag_version as ar  # noqa: E402

QUESTION_SET = _ROOT / "eval" / "news_routing_questions.json"
_WEBBY = ("web", "both")


def _load_questions(kinds_wanted: set[str]) -> list[dict]:
    """題目分類讀 `news_routing_questions.json`，題目文字回頭到兩個 eval set 取。

    ⚠ 刻意不把 query 抄一份進分類檔：抄了就會漂移，而漂移的方向是「探針量的題目
      與 eval 跑的題目不是同一句」——那種失效沒有任何跡象。"""
    qs = json.loads(QUESTION_SET.read_text(encoding="utf-8"))["questions"]
    pool: dict[str, str] = {}
    for name in ("eval_set.json", "eval_set_news.json"):
        for q in json.loads((_ROOT / "eval" / name).read_text(encoding="utf-8"))["queries"]:
            pool[q["id"]] = q["query"]
    out, missing = [], []
    for q in qs:
        if q["kind"] not in kinds_wanted:
            continue
        # `query` 就地寫的是 **probe 專用題**（`probe_only`）：它們刻意**不進任何 eval set**
        # ——加進去會改動分母，而跨日期的分數就再也比不了（CLAUDE.md）。
        text = q.get("query") or pool.get(q["id"])
        if not text:
            missing.append(q["id"])
            continue
        out.append({"id": q["id"], "kind": q["kind"], "query": text,
                    "probe_only": bool(q.get("probe_only"))})
    if missing:
        print(f"⚠ 這些 id 在兩個 eval set 裡都找不到，已略過：{missing}")
    return out


def _verdict(kind: str, routes: set[str]) -> str:
    """一輪的判定。回 'ok' / 'danger' / 'waste' / 'weak'（mixed 用）。"""
    webby = bool(routes & set(_WEBBY))
    kbby = "kb" in routes or "both" in routes
    if kind == "flow":
        return "ok" if webby else "danger"
    if kind == "record":
        return "waste" if webby else "ok"
    # mixed：判準較弱，只要求兩邊都有代表（或出現 both）
    return "ok" if (webby and kbby) else "weak"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=3,
                    help="≥3。MoE 不固定專家路由，單輪結論不可用（CLAUDE.md）")
    ap.add_argument("--only", default="flow,record",
                    help="要跑哪些 kind，逗號分隔。預設 flow,record（mixed 判準較弱且多 22 題）")
    ap.add_argument("--as-of", default=None,
                    help="鎖死的『今天』。⚠ **要與被比較的那次跑分一致**：as-of 會進 "
                         "`_build_temporal_contract`＝Planner prompt 的一部分，不同的 as-of "
                         "量的不是同一個 prompt（第一版漏了這個，探針與真實跑分對不上）")
    ap.add_argument("--output", default="experiments/route_classification_probe.json")
    args = ap.parse_args()
    if args.as_of:
        os.environ["AGENTIC_AS_OF_DATE"] = args.as_of

    # ⚠ 見 docstring 的陷阱①：重放快取會讓每一輪命中同一份結果 → 「完全穩定」是假的。
    if os.getenv("RAG_REPLAY_CACHE"):
        print(f"[ABORT] RAG_REPLAY_CACHE={os.getenv('RAG_REPLAY_CACHE')!r} 開著。"
              f"這支量的是 LLM 的跨輪變異，重放會讓每一輪回同一個答案 ＝ 假的穩定。")
        return 2
    if args.repeat < 3:
        print(f"⚠ --repeat={args.repeat} < 3：MoE 不固定專家路由，這個輪數下不了結論。")

    kinds = {k.strip() for k in args.only.split(",") if k.strip()}
    qs = _load_questions(kinds)
    print(f"collection={ar.rq.COLLECTION_NAME}  CHECKER={ar.CHECKER_MODEL}  "
          f"repeat={args.repeat}  題數={len(qs)}（{Counter(q['kind'] for q in qs)}）\n")

    rows = []
    for q in qs:
        rounds = []
        for _ in range(args.repeat):
            subs = ar._plan_subqueries(q["query"], ar.FRESHNESS_LIVE)
            rounds.append({"routes": sorted({t["route"] for t in subs}),
                           "n_sub": len(subs),
                           "tasks": [t["task"] for t in subs]})
        verdicts = [_verdict(q["kind"], set(r["routes"])) for r in rounds]
        unstable = len({tuple(r["routes"]) for r in rounds}) > 1
        rows.append({**q, "rounds": rounds, "verdicts": verdicts, "unstable": unstable})
        bad = [v for v in verdicts if v != "ok"]
        mark = ""
        if bad:
            mark += f"  <<< {bad[0]} {len(bad)}/{args.repeat}"
        if unstable:
            mark += "  <<< 跨輪不一致"
        print(f"  {q['id']:<8}{q['kind']:<7}"
              + " ".join("/".join(r["routes"]) for r in rounds) + mark)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                 encoding="utf-8")

    print("\n" + "=" * 78)
    for kind in ("flow", "record", "mixed"):
        sub = [r for r in rows if r["kind"] == kind]
        if not sub:
            continue
        n_q, n_r = len(sub), len(sub) * args.repeat
        bad_key = {"flow": "danger", "record": "waste", "mixed": "weak"}[kind]
        bad_rounds = sum(v == bad_key for r in sub for v in r["verdicts"])
        bad_qs = sum(any(v == bad_key for v in r["verdicts"]) for r in sub)
        unstable = sum(r["unstable"] for r in sub)
        label = {"danger": "**危險誤判**（flow → 純 kb）",
                 "waste": "**浪費誤判**（record → web/both）",
                 "weak": "弱判準未達（mixed 缺一邊）"}[bad_key]
        print(f"\n{kind}（{n_q} 題 × {args.repeat} 輪 = {n_r} 次判定）")
        print(f"  {label:<34}{bad_rounds}/{n_r} 次   涉及 {bad_qs}/{n_q} 題")
        print(f"  {'跨輪不一致':<34}{unstable}/{n_q} 題")
        cnt = Counter(x for r in sub for rd in r["rounds"] for x in rd["routes"])
        print(f"  route 出現次數：{dict(cnt)}")

    print("\n⚠ 這支是 probe 不是閘門：LLM 有噪音，判讀看逐題 k/n，不要看總平均。")
    print("⚠ `跨輪不一致` 與誤判數**正交**，不可合併——兩輪都對也可能只是運氣，"
          "而『穩定地錯』與『時對時錯』要用不同的方法修。")
    print("⚠ record 那 7 題是陰性對照：少了它，一個「一律回 web」的 Planner 在 flow 那半會滿分。")
    print(f"\n寫入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
