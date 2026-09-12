"""probe_ticker_resolution.py — 量產品／子公司名 → 母公司 ticker 的 LLM 實體解析
（含 LLM、非零噪音、**不是閘門**）。

**這支存在的理由**：2026-08-28 起「問題提到的產品是誰家的」由 `rq.resolve_tickers_llm` 判，
而它**只在 `_COMPANY_TICKER` 這張 regex 表沉默時才被叫**。兩個後果：

  ① `eval/eval_set.json` 的 **65 題全部由 regex 解出 ticker**（實測），所以這條路在既有題庫上
     **一次都不會觸發** → 端到端跑分對這個修法完全無感。**準確度不能從結果檔推，只有這一支在量。**
  ② 接線正不正確（誰在什麼時候被叫、輸出怎麼過封閉集合）是零 LLM 可測的，那在
     [`eval/verify_period_intent_routing.py`](verify_period_intent_routing.py) 閘門⑦。
     **本檔只量準確度，不重測接線。**

⚠ **判讀看不對稱的錯誤**（兩種錯的代價差很多）：
  · **解不出來**（回 `[]`）＝ 退回本修法之前的行為：沒有 ticker hard filter、Tier 3 跨公司污染。
    **只是沒有改善**，不比今天糟。
  · **解成錯的公司** ＝ hard filter 鎖到**別家的財報**，答案會帶著看起來正當的引用一起錯。
    **比今天更糟。** 所以陰性對照那一臂的「誤指」是這支最重要的數字，不是陽性臂的命中率。

⚠ **8 題陰性對照不可省**。少了它們，一個「一律回最像的那一家」也會在陽性臂拿高分。
  對照題涵蓋四種該回空的情形：KB 以外的上市公司（AMD／Intel／Salesforce／台積電）、
  別家的產品（Snapdragon）、產業題、總體題、名詞定義題。

⚠ **刻意沒有收錄的一類**：「OpenAI 的營收模式」「Anthropic 的模型」。那些公司與 MSFT／AMZN／
  GOOGL 有投資關係，「這題是不是在問 MSFT」本身就沒有一致答案 → 拿它當對照會讓量尺變噪音。
  要驗那一類得先決定產品的正確答案是什麼，那是題庫問題不是模型問題。

⚠ **前提檢查**：每一題都必須是 regex 抽不到 ticker 的（否則生產路徑上 LLM 根本不會被叫，
  那一題就沒在測東西）。不成立會直接 ABORT，不靜默縮水。

⚠ MoE 不固定路由，單輪看不出穩定度。下結論請 `--repeat 3` 以上，看**逐題 k/n**。

跑法：
    .venv/Scripts/python.exe eval/probe_ticker_resolution.py --selftest
    .venv/Scripts/python.exe eval/probe_ticker_resolution.py --repeat 3 \\
        --output experiments/ticker_resolution_probe.json
"""
from __future__ import annotations

import argparse
import json
import sys
# ⚠ Windows 主控台預設 cp950，而本檔的報表帶著 ⚠／✔／① 等字元 ⇒ **印到一半就 crash**，
#   而 crash 的退出碼與「有 FAIL」外觀相同 ＝ 把量尺自己的失敗讀成系統的失敗。
#   2026-09-11 普查：eval/ 的 51 支裡有 27 支帶著這個地雷，其中兩支當天真的踩了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(override=True)

import rag_query as rq  # noqa: E402

# ── 題庫 ──────────────────────────────────────────────────────────────────────
# 陽性：一般人真的會這樣問，而 `_COMPANY_TICKER` 全部抓不到。
POSITIVE: list[tuple[str, str, list[str]]] = [
    ("pos-01", "AWS 在 2022 年的淨銷售額是多少？",              ["AMZN"]),
    ("pos-02", "Azure 這一季的營收成長率？",                    ["MSFT"]),
    ("pos-03", "iPhone 佔總營收的比重？",                       ["AAPL"]),
    ("pos-04", "YouTube 廣告營收成長得快嗎？",                  ["GOOGL"]),
    ("pos-05", "Instagram 的變現進度如何？",                    ["META"]),
    ("pos-06", "Reality Labs 去年虧了多少？",                   ["META"]),
    ("pos-07", "CUDA 生態系構成的護城河有多深？",               ["NVDA"]),
    ("pos-08", "Model Y 這一季交付了多少台？",                  ["TSLA"]),
    ("pos-09", "Xbox 的營收貢獻多少？",                         ["MSFT"]),
    ("pos-10", "LinkedIn 對整體營收的貢獻？",                   ["MSFT"]),
    ("pos-11", "GeForce 顯示卡的毛利率？",                      ["NVDA"]),
    ("pos-12", "Prime 會員訂閱收入有多少？",                    ["AMZN"]),
    ("pos-13", "WhatsApp 的商業化做到哪了？",                   ["META"]),
    ("pos-14", "App Store 抽成貢獻多少服務營收？",              ["AAPL"]),
    ("pos-15", "Waymo 投入了多少資源？",                        ["GOOGL"]),
    ("pos-16", "超級充電站網路帶來多少營收？",                  ["TSLA"]),
    ("pos-17", "Kindle 的銷售狀況？",                           ["AMZN"]),
    ("pos-18", "Bing 搜尋的市佔有起來嗎？",                     ["MSFT"]),
    # 多公司：ticker filter 要能圈出兩家（`build_qdrant_filter` 走 MatchAny）
    ("pos-19", "AWS 跟 Azure 誰成長比較快？",                   ["AMZN", "MSFT"]),
    ("pos-20", "iPhone 跟 Model Y，哪個產品線的營收比較大？",   ["AAPL", "TSLA"]),
]

# 難題臂：**10-K 裡真正的分部名稱**（六個都用 grep 在 `data/edgar_processed/Filings/` 驗過
# 確實逐字出現）。與陽性臂分開報，理由是**陽性臂太簡單**——AWS→Amazon 是常識，一個大模型
# 本來就會；分部名才是「不查財報就答不出來」的那一類，也是這支量尺真正的難度所在。
# ⚠ 這一臂表現差**不算回歸**（它本來就不在原本的病灶範圍內），但它會告訴你這條路的天花板。
HARD: list[tuple[str, str, list[str]]] = [
    ("hard-01", "Other Bets 這個部門虧了多少？",              ["GOOGL"]),
    ("hard-02", "Intelligent Cloud 分部的營運利益？",          ["MSFT"]),
    ("hard-03", "Compute & Networking 分部的營收成長？",       ["NVDA"]),
    ("hard-04", "Family of Apps 的營收佔比？",                 ["META"]),
    # ⚠ hard-05 是這支目前**唯一失手的一題**（2026-08-28 實測 0/3，一律回空）。
    #   它照 prompt 寫的做了正確的事：不確定就回空，**不猜**。所以它的失敗方向是安全的那一邊
    #   ——退回沒有 ticker filter，而不是鎖到別家。這一題留著當這條路的天花板標記。
    ("hard-05", "Wearables, Home and Accessories 賣得如何？",  ["AAPL"]),
    ("hard-06", "能源生成與儲存業務的毛利率？",                ["TSLA"]),
]

# 陰性對照：**不可省**。少了它們，「一律猜最像的那一家」也會在陽性臂拿高分。
CONTROL: list[tuple[str, str, list[str]]] = [
    ("neg-01", "AMD 的資料中心營收成長多少？",                  []),   # KB 外的上市公司
    ("neg-02", "Intel 的晶圓代工進度如何？",                    []),
    ("neg-03", "Salesforce 的訂閱營收？",                       []),
    ("neg-04", "台積電的先進製程良率如何？",                    []),
    ("neg-05", "Snapdragon 的效能贏過對手嗎？",                 []),   # 別家的產品名
    ("neg-06", "這一季整體科技股的景氣如何？",                  []),   # 產業題
    ("neg-07", "聯準會升息對股市有什麼影響？",                  []),   # 總體題
    ("neg-08", "本益比是什麼意思？",                            []),   # 名詞定義
]

CASES = POSITIVE + HARD + CONTROL


# ── 判定（零 LLM、純集合比對）────────────────────────────────────────────────
def verdict(want: list[str], got: list[str]) -> str:
    """三態。⚠ `partial` 與 `wrong` 一定要分開：前者是漏抓（退回今天），
    後者是**抓到別家**（比今天糟）。合併成一個「不正確率」就看不出危險的那一半。"""
    w, g = set(want), set(got)
    if w == g:
        return "exact"
    if g - w:                 # 出現了不該出現的公司 → 危險
        return "wrong"
    return "partial"          # g ⊊ w（含 g 為空）→ 只是沒解出來


def _preflight() -> list[str]:
    """每題都必須是 regex 抽不到 ticker 的，否則那一題在生產路徑上根本走不到 LLM。"""
    bad = []
    for cid, q, _ in CASES:
        hit = rq._find_all_ticker_aliases(q.lower(), q)
        if hit:
            bad.append(f"{cid}: regex 已抽到 {hit} → 生產上 LLM 不會被叫，這題沒在測東西（{q}）")
    return bad


def selftest() -> int:
    """判定函式的雙向自測（零 LLM、零網路）。"""
    cases = [
        ("完全命中",              ["AMZN"],         ["AMZN"],         "exact"),
        ("順序不同仍算命中",      ["AMZN", "MSFT"], ["MSFT", "AMZN"], "exact"),
        ("空對空（陰性對照命中）", [],               [],               "exact"),
        ("漏抓 → partial",        ["AMZN"],         [],               "partial"),
        ("多家只抓到一家 → partial", ["AMZN", "MSFT"], ["AMZN"],      "partial"),
        ("抓到別家 → wrong",      ["AMZN"],         ["MSFT"],         "wrong"),
        # ⚠ 這條是判定的重點：對了一半但**多抓一家別的**，必須算 wrong 不算 partial
        #   ——那一家會進 hard filter，把別人的財報拉進候選池。
        ("對一半 ＋ 多抓一家 → wrong", ["AMZN", "MSFT"], ["AMZN", "MSFT", "TSLA"], "wrong"),
        ("陰性對照被誤指 → wrong", [],               ["MSFT"],         "wrong"),
    ]
    ok = 0
    for label, want, got, expect in cases:
        got_v = verdict(want, got)
        mark = "PASS" if got_v == expect else "FAIL"
        ok += got_v == expect
        print(f"  [{mark}] {label}: want={want} got={got} → {got_v}（預期 {expect}）")
    print(f"\n{ok}/{len(cases)} PASS")
    return 0 if ok == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1,
                    help="每題重複判幾輪，報逐題 k/n（MoE 單輪不可信）")
    ap.add_argument("--model", default=rq.DEFAULT_MODEL)
    ap.add_argument("--output", default=None, help="結果檔（請寫到 experiments/）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    bad = _preflight()
    if bad:
        print("[ABORT] 前提不成立——這些題目 regex 就抓得到，量它們等於沒在量 LLM：")
        for b in bad:
            print(f"  ✗ {b}")
        return 2

    n = args.repeat
    rows = []
    for cid, q, want in CASES:
        runs = [rq.resolve_tickers_llm(q, args.model) for _ in range(n)]
        vs = [verdict(want, g) for g in runs]
        c = Counter(vs)
        rows.append({"id": cid, "query": q, "want": want,
                     "runs": runs, "verdicts": vs,
                     "exact": c["exact"], "partial": c["partial"], "wrong": c["wrong"]})
        worst = "wrong" if c["wrong"] else ("partial" if c["partial"] else "exact")
        print(f"[{worst.upper():7s} {c['exact']}/{n}] {cid}  want={want or '[]'}  "
              f"got={runs[0] if runs else []}"
              + ("" if len(set(map(tuple, runs))) == 1 else f"  ⚠ 輪間不一致: {runs}"))

    def _tally(subset):
        t = Counter()
        for r in subset:
            for v in r["verdicts"]:
                t[v] += 1
        return t

    by_id = {r["id"]: r for r in rows}
    pos = _tally([by_id[c[0]] for c in POSITIVE])
    hard = _tally([by_id[c[0]] for c in HARD])
    neg = _tally([by_id[c[0]] for c in CONTROL])
    print("\n" + "=" * 74)
    print(f"陽性臂・知名產品（{len(POSITIVE)} 題 × {n} 輪 = {sum(pos.values())} 次判定）: "
          f"命中 {pos['exact']}  漏抓 {pos['partial']}  **指錯公司 {pos['wrong']}**")
    print(f"陽性臂・10-K 分部名（{len(HARD)} 題 × {n} 輪 = {sum(hard.values())} 次判定）: "
          f"命中 {hard['exact']}  漏抓 {hard['partial']}  **指錯公司 {hard['wrong']}**")
    print(f"陰性對照（{len(CONTROL)} 題 × {n} 輪 = {sum(neg.values())} 次判定）: "
          f"正確回空 {neg['exact']}  **誤指 {neg['wrong']}**")
    print("⚠ 判讀：漏抓＝退回本修法之前（沒有 ticker filter、跨公司污染），只是沒改善；")
    print("        指錯／誤指＝hard filter 鎖到別家財報，**比修法之前更糟**——看這一欄。")
    if n == 1:
        print("⚠ n=1：MoE 不固定路由，這一輪的數字不足以下結論，請 --repeat 3 以上。")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"meta": {"model": args.model, "repeat": n,
                      "collection": rq.COLLECTION_NAME,
                      "n_positive": len(POSITIVE), "n_control": len(CONTROL)},
             "positive": dict(pos), "control": dict(neg), "rows": rows},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n寫出 {out}")
    # 退出碼只認「陰性對照被誤指」——那是唯一比修法前更糟的方向。
    return 1 if neg["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
