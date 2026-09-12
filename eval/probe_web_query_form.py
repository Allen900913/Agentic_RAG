"""probe_web_query_form.py — 送給 Tavily 的 query **形式**如何決定檢索品質（會連網）。

**為什麼需要它**：2026-09-02 重錄 `eval/web_fixture.json` 時，`web-01`（Apple 市值）拿回
12 筆垃圾——最高分 **0.377** 是一支 Tim Cook 影片，其餘是首頁／維基「Capitalization」詞條／
世界銀行 GDP 指標／`capitalone.com`，**一筆都沒有市值**。兩次獨立錄製**逐字相同** → 系統性。
而 `_web_query_en` 的 docstring 記著 2026-08-13 的對照：`Apple market cap` →
`stockanalysis.com/stocks/aapl/market-cap`，score **0.91**。

兩邊都是英文 → **差別不是語言，是形式**。但「關鍵詞 vs 自然語問句」與「有沒有 ticker」
在那兩筆證據裡是**混在一起**的，分不開。這支用 2×2 把它們拆開。

## 判準刻意獨立於「web-01 有沒有解除 block」

量的是 **Tavily 的檢索結果**，不是我們的答案、也不是那份 fixture。所以「改了之後 web-01 變綠」
不會是這個修法的證據——那會是 **量尺與被測物耦合**（為了讓 fixture 錄得漂亮而改受測物）。

## 三個指標，不可合併

| 指標 | 定義 |
|---|---|
| `有值` | 經**生產的** `_host_allowed` 複核後，還有幾筆內容裡真的出現被問的那個量 |
| `過濾後筆數` | 複核後剩幾筆（Tavily 的 `include_domains` 只是授權不是過濾） |
| `top 分數` | 複核後最高的 relevance score |

⚠ **`有值` 才是判準**。`過濾後筆數` 高而 `有值` 為 0 就是這次撞到的形狀：拿回一堆
**非空但無關**的頁面，而下游的 `_unfulfilled_web_route_gaps` 只看得到「非空」。

⚠ **控制題（NVDA 股價）不可省**：生產在那題是成功的。少了它，「關鍵詞形式比較好」可能只是
「市值這種問題本來就難」——兩者在只量一題時外觀相同。

用法（會連網、燒 Tavily 額度；回應錄進 probe 自己的 fixture，**不碰 eval/web_fixture.json**）：
    .venv/Scripts/python.exe -u eval/probe_web_query_form.py
    .venv/Scripts/python.exe -u eval/probe_web_query_form.py --replay   # 只讀既有 fixture
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

for _s in (sys.stdout, sys.stderr):  # ⚠ stderr 也要轉：traceback 走 stderr，只轉 stdout 的話「印到一半 crash」照樣發生（2026-09-11 閘門 H1）

    _s.reconfigure(encoding="utf-8", errors="replace")
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ⚠ **第二輪一定要換 `--fixture` 路徑**（`RAG_WEB_REPLAY` 走 argv 先解析，見下）：
#   record 模式仍然**先查 fixture 再連網**，所以沿用同一份會逐字重放第一輪的結果 →
#   量出「兩輪完全一致」而那是重放不是重複。這正是 2026-09-02 那個錯誤推論的機制版本
#   （見 docs/EVAL.md §4.7：相隔十分鐘的相同回應可能只是快取）。
_DEFAULT_FIXTURE = _ROOT / "eval" / "web_fixture_query_form.json"


def _early_fixture_path() -> Path:
    """在 import agentic_rag_version **之前**就要定案（`_web_replay` 在 import 時讀 env），
    所以這裡自己掃一次 argv，不能等 argparse。"""
    for i, a in enumerate(sys.argv):
        if a == "--fixture" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1])
        if a.startswith("--fixture="):
            return Path(a.split("=", 1)[1])
    return _DEFAULT_FIXTURE


FIXTURE = _early_fixture_path()
os.environ["RAG_WEB_REPLAY"] = str(FIXTURE)
os.environ.setdefault("RAG_WEB_REPLAY_MODE", "record")

import agentic_rag_version as ar  # noqa: E402

# 被問的那個量長什麼樣子。**刻意要求「標記 ＋ 值」同時出現在一個窗口內**：只找數字會被
# 頁面上任何一個 `4.32T`（別家公司的市值、大盤數字）滿足 → 那是在錯的證據上放行。
_TARGETS = {
    "market_cap": (re.compile(r"(?i)market\s*cap"), re.compile(r"(?i)\b\d{1,2}(?:\.\d{1,3})?\s*T\b")),
    "price":      (re.compile(r"(?i)\b(?:last|price|quote|close)\b"), re.compile(r"\b\d{2,4}\.\d{2}\b")),
}
_WINDOW = 300   # ⚠ 這個數字是判斷不是證據（同 `_MARKER_WINDOW` 的教訓）。窗口存在才是重點。

# 2×2：問句形式 × 有無 ticker。第一格 `A` 就是生產今天實際送出的東西。
CASES = [
    ("apple_mktcap", "market_cap", {
        "A 問句・無ticker（＝生產現況）": "What is Apple's current market capitalization?",
        "B 問句・有ticker":              "What is Apple (AAPL) current market capitalization?",
        "C 關鍵詞・無ticker":            "Apple market cap",
        "D 關鍵詞・有ticker":            "AAPL market cap today",
    }),
    ("msft_mktcap", "market_cap", {
        "A 問句・無ticker（＝生產現況）": "What is Microsoft's current market capitalization?",
        "B 問句・有ticker":              "What is Microsoft (MSFT) current market capitalization?",
        "C 關鍵詞・無ticker":            "Microsoft market cap",
        "D 關鍵詞・有ticker":            "MSFT market cap today",
    }),
    # 控制題：生產在這題是成功的（web-02 四輪全 PASS）。
    ("nvda_price", "price", {
        "A 問句・無ticker（＝生產現況）": "What is NVIDIA's current stock price?",
        "B 問句・有ticker":              "What is NVIDIA (NVDA) current stock price?",
        "C 關鍵詞・無ticker":            "NVIDIA stock price",
        "D 關鍵詞・有ticker":            "NVDA stock price today",
    }),
]


def _has_value(text: str, kind: str) -> bool:
    """標記與值必須落在同一個窗口內——理由見 `_TARGETS` 上方。"""
    marker, value = _TARGETS[kind]
    return any(value.search(text[m.start(): m.start() + _WINDOW]) for m in marker.finditer(text or ""))


def _probe(query: str, kind: str) -> dict:
    resp = ar._tavily_raw(query)
    rows = []
    for r in resp.get("results", []) or []:
        host = (urlparse(r.get("url") or "").hostname or "").lower()
        if not ar._host_allowed(host):          # ⚠ 用生產的複核，不抄一份
            continue
        rows.append({"host": host, "score": r.get("score") or 0.0,
                     "has_value": _has_value(f"{r.get('title','')} {r.get('content','')}", kind)})
    return {"n_raw": len(resp.get("results", []) or []), "n_allowed": len(rows),
            "n_with_value": sum(r["has_value"] for r in rows),
            "top_score": round(max((r["score"] for r in rows), default=0.0), 3),
            "hosts": sorted({r["host"] for r in rows})}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", action="store_true", help="只讀既有 fixture，絕不連網")
    ap.add_argument("--fixture", default=str(_DEFAULT_FIXTURE),
                    help="這一輪的 fixture 路徑。⚠ **時間分開的第二輪必須換一個新路徑**，"
                         "否則 record 模式會先命中既有 fixture → 量到的「一致」是重放不是重複")
    ap.add_argument("--output", default="experiments/web_query_form_probe.json")
    args = ap.parse_args()
    if args.replay:
        os.environ["RAG_WEB_REPLAY_MODE"] = "replay"
    if not args.replay and not os.getenv("TAVILY_API_KEY"):
        print("[ABORT] 沒有 TAVILY_API_KEY。")
        return 2

    print(f"fixture={FIXTURE.name}  mode={os.environ['RAG_WEB_REPLAY_MODE']}  "
          f"n={ar.TAVILY_FETCH_RESULTS}  白名單 {len(ar.WEB_ALLOWED_DOMAINS)} 個\n")
    out = []
    for name, kind, forms in CASES:
        print(f"── {name}（問的是 {kind}）")
        for label, q in forms.items():
            res = _probe(q, kind)
            out.append({"case": name, "kind": kind, "form": label, "query": q, **res})
            flag = "  <<< 一筆都沒有值" if res["n_with_value"] == 0 else ""
            print(f"   {label:<28} 有值 {res['n_with_value']:>2}/{res['n_allowed']:<2}"
                  f"（原始 {res['n_raw']}）  top={res['top_score']:<6}{flag}")
            print(f"   {'':<28} {', '.join(res['hosts'][:5]) or '(過濾後全空)'}")
        print()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("=" * 78)
    print("⚠ 判準是 `有值` 不是 `過濾後筆數`：非空但無關正是 2026-09-02 撞到的形狀。")
    print("⚠ 控制題（nvda_price）若四種形式都有值，代表形式效應是**市值題特有**，不可外推。")
    print(f"\n寫入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
