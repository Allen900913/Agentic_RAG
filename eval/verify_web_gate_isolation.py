"""驗收 web_search 的兩件事：**eval 隔離**與**資料真的進得了答案**。零 LLM、零網路、零 Qdrant，秒級。

閘門① eval 隔離：生產打得到 web、eval 一次都不漏（見下方 SCENARIOS）。
閘門② 重生成保留：`web_extra` 必須跟著每一次 validator 重寫走，且 reflect 的稽核來源要含它。
      2026-08-13 實測：接管線之前，Tavily 三次都帶回 `$5.27T`，最終答案三次都退回 6 月快照的
      `$4,962.16B`——因為重生成時 `extra_user` 被換成糾錯指示，web 區塊整段消失。
閘門③ 生成契約：**無 web 時 system message 逐字不變**（＝eval 基準不被動到的證明），
      有 web 時修訂條款與 as-of 日期真的進得去。
閘門④ 來源白名單：`include_domains` 真的傳給 Tavily，且濾空時不靜默退回全網。
閘門⑤ Grader 時效判準：snapshot 的 Checker prompt 逐字不變；日期算術真值表（零 LLM）。


**這支在守什麼**：`agentic_rag_version._run_executor_deterministic` 裡那個 web 補救判斷式，
必須滿足「生產打得到、eval 一次都不漏」。兩者是不同的護欄，很容易被誤當成同一個：

| 條件 | 角色 |
|---|---|
| `freshness_mode == LIVE` | **eval 隔離**。`run_agentic_on_evalset.py` 的 `--freshness-mode` 預設就是 `snapshot` |
| `ENABLE_WEB_SEARCH` | **eval 隔離**。`--no-web` 設成 False |
| `not verdict["sufficient"]` | **生產端擋濫用**。KB 答得出來就不上網 |
| ~~`looks_like_news_query`~~ | 2026-08-13 移除。它守的是相關性，不是隔離，卻讓非新聞措辭的即時題永遠打不到 web |

**前兩個各自都足以擋住**（defense in depth）——所以第二個情境「eval 忘了帶 `--no-web`」
也必須是 0 次呼叫，那是這支測試最重要的一格。

⚠ 生產列刻意假設 Grader 對每一題都判「不足」（對隔離而言是最壞情況，最容易觸發 web）。
真實跑分不會這樣：實測 `Microsoft 最新一季的 Azure 成長多少` 的 Grader 判 `sufficient=True`，
被 `not verdict["sufficient"]` 擋下、不會上網。**本檔不驗生產端的濫用率**，那要看實跑 trace。

跑法：`.venv/Scripts/python.exe eval/verify_web_gate_isolation.py`
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import agentic_rag_version as ar  # noqa: E402

LIVE = ar.FRESHNESS_LIVE
SNAP = ar.FRESHNESS_SNAPSHOT

_calls: list[str] = []
# ⚠ 閘門① 把 `_tavily_search` 整個換成 stub（絕不連網）；閘門④ 要測的正是真身，
# 所以**先留一份原始參考**。少了這行，閘門④ 會量到 stub 而全數誤報 FAIL（2026-08-13 實際踩到）。
_REAL_TAVILY_SEARCH = ar._tavily_search
ar._tavily_search = lambda q: _calls.append(q) or "（stub，絕不連網）"


def _gate(freshness: str, web_enabled: bool, sufficient: bool, task: str) -> bool:
    """必須與 `agentic_rag_version._run_executor_deterministic` 的 web 補救判斷式**逐字一致**。

    ⚠ 這裡是抄寫、不是 import——判斷式寫在函式中段，無法單獨取用。
    兩份漂移的話這支會安靜地驗錯的東西，所以改動那一行時**務必同步改這裡**。

    2026-08-13：`task` 已不再參與判斷（兩道詞表閘門都拿掉了），保留參數是為了讓 SCENARIOS
    的逐題表格仍讀得出「哪一題會打 web」。
    """
    return freshness == LIVE and web_enabled and not sufficient


# 四個實測過的決策邊界（2026-08-13 生產實跑）：必須上網／不該上網／邊界／即時數字
QUERIES = {
    "Q1 8月新聞": "NVIDIA 在 2026 年 8 月有哪些最新消息？",
    "Q2 Azure": "Microsoft 最新一季的 Azure 與雲端服務成長多少？",
    "Q3 TeslaFSD": "Tesla 最近在自動駕駛（FSD／Robotaxi）上有什麼進展？",
    "Q4 股價": "NVIDIA 目前的股價和市值大約是多少？",
}

SCENARIOS = [
    ("eval 帶 --no-web", SNAP, False, 0),
    ("eval 忘了帶 --no-web", SNAP, True, 0),      # ← 最重要的一格：預設 snapshot 單獨就要擋住
    ("生產（live + web 開）", LIVE, True, None),   # None = 不設上限，只要求 > 0
]


WEB_MARK = "網路搜尋結果"          # synthesize 組出來的 web 區塊標頭
WEB_FACT = "$5.27T-CANARY"        # 只出現在 web 區塊裡的哨兵值


def _check_regeneration_keeps_web() -> int:
    """閘門②：強制觸發每一條重生成路徑，斷言 web 區塊有跟著進到 prompt。

    做法：把 `rq.call_llm` 換成腳本化 stub 並錄下每一次收到的 prompt（零 LLM、零網路）。
    """
    import rag_query as rq

    web_extra = f"\n\n=== 補充：{WEB_MARK}（非知識庫）===\nNVDA 市值 {WEB_FACT}（as of 2026-08-11）"
    chunks = [{"source": "NVDA_10K_2026.html", "chunk_index": 1,
               "content": "NVIDIA Corporation revenue discussion.", "rerank_score": 0.7}]
    prompts: list[str] = []
    replies: list[str] = []

    def fake_call_llm(messages, model_name=None, temperature=None, **kw):
        prompts.append("\n".join(m.get("content", "") for m in messages))
        return replies.pop(0) if replies else "改寫後的答案【NVDA_10K_2026.html, chunk #1】"

    orig = rq.call_llm
    rq.call_llm = fake_call_llm
    try:
        results = []

        # ① citation validator：答案沒有任何有效引用 → 必定重寫一次
        prompts.clear(); replies.clear()
        ar._validate_and_fix_citations("NVIDIA 股價？", "市值很高，沒有引用。", chunks,
                                       "stub-model", web_extra=web_extra)
        results.append(("citation validator 重寫", any(WEB_FACT in p for p in prompts)))

        # ② reflect：先回一個非 NONE 的稽核結果 → 觸發重生成
        prompts.clear(); replies.clear()
        replies.append("發現疑似幻覺：市值數字無來源")          # 稽核回覆
        replies.append("修正後答案【NVDA_10K_2026.html, chunk #1】")  # 重生成
        ar._reflect_and_fix("NVIDIA 股價？", "市值 $5.27T【NVDA_10K_2026.html, chunk #1】",
                            chunks, "stub-model", web_extra=web_extra)
        results.append(("reflect 稽核來源含 web", WEB_FACT in prompts[0] if prompts else False))
        results.append(("reflect 重生成", any(WEB_FACT in p for p in prompts[1:])))
    finally:
        rq.call_llm = orig

    print()
    print(f"  {'重生成路徑':<28}{'web 資料是否保留':>20}")
    print("  " + "-" * 50)
    fail = 0
    for name, ok in results:
        fail += 0 if ok else 1
        print(f"  {name:<28}{('保留 OK' if ok else '**遺失 FAIL**'):>20}")
    return fail


def _check_system_prompt_amendment() -> int:
    """閘門③：**沒有 web 時，system message 必須與加修訂條款以前逐字相同**。

    這是 eval 基準不被動到的證明。eval 的 `web_notes` 恆為空（freshness 預設 snapshot），
    所以只要「web_extra 空 → system 逐字不變」成立，既有跑分就一格都不會被影響。
    另一半驗有 web 時修訂條款與 as-of 日期真的進得去（否則等於沒改）。
    """
    import rag_query as rq

    seen: list[str] = []

    def fake_call_llm(messages, model_name=None, temperature=None, **kw):
        seen.append(next((m["content"] for m in messages if m.get("role") == "system"), ""))
        return "答案【NVDA_10K_2026.html, chunk #1】"

    chunks = [{"source": "NVDA_10K_2026.html", "chunk_index": 1,
               "content": "NVIDIA revenue discussion.", "rerank_score": 0.7}]
    web_extra = f"\n\n=== 網路搜尋結果 ===\nNVDA 市值 {WEB_FACT}"

    orig = rq.call_llm
    rq.call_llm = fake_call_llm
    try:
        ar._write_final_answer("Q", chunks, "stub-model")
        ar._write_final_answer("Q", chunks, "stub-model", web_extra=web_extra)
    finally:
        rq.call_llm = orig

    baseline = rq.SYSTEM_PROMPT + ar._ZH_ANSWER_DIRECTIVE
    today = ar._get_as_of_date().isoformat()
    results = [
        ("無 web → system 逐字不變", seen[0] == baseline),
        ("有 web → 帶引用修訂條款", "規則修訂" in seen[1] and "[web: 網址]" in seen[1]),
        ("有 web → 帶今天日期", today in seen[1]),
        ("有 web → 保留原契約", seen[1].startswith(baseline)),
    ]
    print()
    print(f"  {'生成契約（system message）':<28}{'判定':>20}")
    print("  " + "-" * 50)
    fail = 0
    for name, ok in results:
        fail += 0 if ok else 1
        print(f"  {name:<28}{('OK' if ok else '**FAIL**'):>20}")
    return fail


def _check_domain_allowlist() -> int:
    """閘門④：白名單真的傳給 Tavily，且**濾空時不退回全網**。

    做法：把 `tavily` 模組換成 stub 記下 kwargs（零網路）。第二格是重點——靜默 fallback
    到全網等於白名單沒生效卻沒人知道。
    """
    import types

    captured: dict = {}

    class _FakeClient:
        def __init__(self, api_key=None):
            pass

        def search(self, query, **kw):
            captured.update(kw)
            return {"results": []}          # 模擬白名單濾空

    fake_mod = types.ModuleType("tavily")
    fake_mod.TavilyClient = _FakeClient
    orig_mod = sys.modules.get("tavily")
    orig_key = os.environ.get("TAVILY_API_KEY")
    sys.modules["tavily"] = fake_mod
    os.environ["TAVILY_API_KEY"] = "stub-key"
    try:
        out = _REAL_TAVILY_SEARCH("NVIDIA latest market cap")
    finally:
        if orig_mod is None:
            sys.modules.pop("tavily", None)
        else:
            sys.modules["tavily"] = orig_mod
        if orig_key is None:
            os.environ.pop("TAVILY_API_KEY", None)
        else:
            os.environ["TAVILY_API_KEY"] = orig_key

    allow = captured.get("include_domains") or []

    # 2026-08-13 事故的常駐回歸：清單寫 `apple.com`／`microsoft.com`（本意是 IR）時，Tavily
    # 連子網域一起收 → 「蘋果的即時市值」35 筆結果有 33 筆是這些消費端主機，零筆財經資料。
    # 這些字串全部取自當時的實跑 trace，不是想像出來的。
    MUST_REJECT = ["apps.apple.com", "support.apple.com", "podcasts.apple.com",
                   "support.microsoft.com", "learn.microsoft.com"]
    MUST_ADMIT = ["www.sec.gov", "stockanalysis.com", "finance.yahoo.com",
                  "www.macrotrends.net", "www.reuters.com", "www.cnbc.com"]
    leaked = [h for h in MUST_REJECT if any(ar._domain_admits(e, h) for e in allow)]
    missed = [h for h in MUST_ADMIT if not any(ar._domain_admits(e, h) for e in allow)]

    # **清單必須公司無關（O(1)）**：拿 rq 的公司別名表當測資，所以未來新增追蹤公司時，
    # 這道斷言會自動涵蓋新名字——不需要有人記得回來改測試。
    # 只查長度 ≥4 的別名（短別名會在正常域名裡誤命中）。
    #
    # ⚠ **已知限制**：靠「域名含公司名」偵測，抓不到字面上與公司無關的 IR 域名。
    #    實測負向控制 4/5——`abc.xyz`（Alphabet 的 IR）漏掉，故另列 `_IR_NO_NAME` 補齊。
    #    這一格擋得住「順手加一筆 investor.xxx.com」這種常見手滑，擋不住刻意用冷門域名。
    _IR_NO_NAME = {"abc.xyz"}
    import rag_query as rq
    aliases = {a.lower() for a in getattr(rq, "_COMPANY_TICKER", {}) if len(a) >= 4}
    company_specific = [e for e in allow
                        if any(a in e.lower() for a in aliases)
                        or e.lower().startswith(("investor.", "ir."))
                        or e.lower() in _IR_NO_NAME]

    results = [
        ("白名單有傳給 Tavily", allow == ar.WEB_ALLOWED_DOMAINS and len(allow) > 0),
        ("濾空不退回全網", "未退回全網" in out),
        ("清單含原始揭露方", "sec.gov" in allow),
        (f"消費端子網域不得放行{('：'+leaked[0]) if leaked else ''}", not leaked),
        (f"財經來源必須放行{('：缺 '+missed[0]) if missed else ''}", not missed),
        (f"清單必須公司無關{('：'+company_specific[0]) if company_specific else ''}",
         not company_specific),
    ]
    print()
    print(f"  {'web 來源白名單':<28}{'判定':>20}")
    print("  " + "-" * 50)
    fail = 0
    for name, ok in results:
        fail += 0 if ok else 1
        print(f"  {name:<28}{('OK' if ok else '**FAIL**'):>20}")
    return fail


def _check_recency_gate() -> int:
    """閘門⑤：Grader 時效判準。① snapshot 的 Checker prompt 逐字不變 ② 日期算術的真值表。

    ② 是零 LLM 零網路的純算術，是這條規則唯一可靠的回歸保護——真實跑分要花 LLM 且不可重現。
    案例全部取自 2026-08-13 實跑的缺陷：`AAPL_Fundamentals_20260612`（兩個月前）被當成
    「現在的本益比」、`TSLA_News_20260721`（三週前）被當成「今天股價」。
    """
    as_of = ar.date(2026, 8, 13)
    F = [{"source": "AAPL_Fundamentals_20260612.txt"}]      # 62 天前
    N = [{"source": "TSLA_News_20260721_01.txt"}]           # 23 天前
    K = [{"source": "MSFT_10Q_202603.html"}]                # 財報期間 → 月底 2026-03-31
    Y = [{"source": "NVDA_10K_2026.html"}]                  # 財年 → 2026-12-31（**未來日期**）
    FRESH = [{"source": "AAPL_News_20260812_01.txt"}]       # 1 天前，真實日曆日期
    cases = [
        ("即時市值 vs 兩個月前基本面", "intraday", F, True),
        ("今天股價 vs 三週前新聞", "intraday", N, True),
        ("最近進展 vs 三週前新聞", "days", N, True),
        ("季度數字 vs 兩個月前基本面", "none", F, False),      # none → 永不過期
        ("季度數字 vs 10-K", "none", Y, False),                # 同上：財報數字不因 wall clock 過期
        ("最近消息 vs 10-Q 期間", "days", K, True),
        # ── 2026-08-19：以下四條是**推翻舊行為**後的新語意，見 `_stale_for_realtime` docstring ──
        # 舊行為把「財報永不過期」誤用成「財報能證明池子夠新」。KB 有新聞時無害（池裡有真日期
        # 的新聞壓著）；拔除新聞後財報成了唯一日期來源，於是 intraday 題也判不出過期。
        ("即時 vs 10-K：財報不得證明池子夠新", "intraday", Y, True),
        ("來源算不出日期 → 證明不了夠新 → 判過期", "intraday", [{"source": "weird.txt"}], True),
        ("無候選 → 證明不了夠新", "intraday", [], True),
        # ⚠ **這一條是這個 bug 的回歸鎖**：未來日期的 10-K 不得蓋過真實日期的 Fundamentals。
        #   舊碼 max(2026-12-31, 2026-06-12) = 未來 → 判不過期；新碼只認 8 碼真實日期 → 62 天。
        ("未來日期的 10-K 不得蓋過真實日期來源", "days", Y + F, True),
        # ⚠ **誤報對照**：證明修法不是「一律判過期」。池裡有 1 天前的真實日期來源 → 夠新。
        ("池裡有 1 天前的真實日期來源 → 不判過期", "intraday", Y + FRESH, False),
    ]
    print()
    print(f"  {'Grader 時效判準':<34}{'判定':>16}")
    print("  " + "-" * 50)
    fail = 0
    snap = ar._CHECKER_PROMPT
    ok_prompt = "realtime_need" not in snap and "realtime_need" in ar._CHECKER_LIVE_RECENCY_BLOCK
    fail += 0 if ok_prompt else 1
    print(f"  {'snapshot prompt 不含新欄位':<34}{('OK' if ok_prompt else '**FAIL**'):>16}")
    for name, need, chunks, expect_stale in cases:
        got = ar._stale_for_realtime(need, chunks, as_of) is not None
        ok = got == expect_stale
        fail += 0 if ok else 1
        print(f"  {name:<34}{('OK' if ok else '**FAIL**'):>16}")

    # ── ③ `kb_unfixable` 不得誤殺（2026-08-14 追加）────────────────────────────
    # 缺陷：原本只要「候選池最新那筆過期」就標 kb_unfixable=True 並跳過剩餘改寫。
    # 但候選池是**語意檢索的結果**——池子裡最新是 62 天前,不代表 collection 沒有 3 天前的。
    # 那種情況改寫真的有救,跳過就是誤殺。修法是拿整個 collection 的天花板再比一次。
    stale_cov = {"available": True, "tickers": {
        "AAPL": {"fundamentals": {"source": "AAPL_Fundamentals_20260612.txt"}}}}
    fresh_cov = {"available": True, "tickers": {
        "AAPL": {"fundamentals": {"source": "AAPL_Fundamentals_20260612.txt"},
                 "news": {"source": "AAPL_News_20260812_01.txt"}}}}   # 1 天前,天花板夠新
    pool = [{"source": "AAPL_Fundamentals_20260612.txt", "ticker": "AAPL"}]
    unfix_cases = [
        # (名稱, coverage, chunks, need, 期望 stale, 期望 unfixable)
        ("天花板也過期 → 真的沒救", stale_cov, pool, "intraday", True, True),
        ("天花板還夠新 → 是檢索沒撈到", fresh_cov, pool, "intraday", True, False),
        ("coverage 掃不到 → 保守當沒救", {"available": False}, pool, "intraday", True, True),
        ("chunk 無 ticker → 算不出天花板", fresh_cov,
         [{"source": "AAPL_Fundamentals_20260612.txt"}], "intraday", True, True),
        ("池子本來就夠新 → 不改判", fresh_cov,
         [{"source": "AAPL_News_20260812_01.txt", "ticker": "AAPL"}], "intraday", False, False),
        ("need=none → 永不觸發", stale_cov, pool, "none", False, False),
    ]
    real_cov = ar._get_kb_coverage
    try:
        for name, cov, chunks, need, exp_stale, exp_unfix in unfix_cases:
            ar._get_kb_coverage = lambda _c=cov: _c
            stale_days, unfixable = ar._classify_staleness(need, chunks, as_of)
            ok = (stale_days is not None) == exp_stale and unfixable == exp_unfix
            fail += 0 if ok else 1
            print(f"  {name:<34}{('OK' if ok else '**FAIL**'):>16}")
    finally:
        ar._get_kb_coverage = real_cov
    return fail


def _check_web_result_hygiene() -> int:
    """閘門⑥：web 結果的四道**確定性**整理（零 LLM、零網路）——2026-08-14 五個缺陷的常駐回歸。

    案例全部取自 2026-08-14「蘋果的即時市值是多少？」的實跑 trace，不是想像出來的：
      D1 摘要截斷 300 字 → macrotrends 的 `$4572.79B` 被切成 `Apple market cap as of Augus`
      D2 `companiesmarketcap.com` 用五種幣別變體吃掉全部 5 個名額，五則都不含 Apple 數字
      D3 `cnbc.com/2020/08/19/apple-reaches-2-trillion` 與今天的值並列進答案
      D5 同一頁的 `/amp/`、`new.` 子網域、`http://` 變體各佔一個名額
      D4 時效不足 KB 補不了 → replanner 生出 7 個同義待辦，各自再跑一次 web
    """
    import types

    print()
    print(f"  {'web 結果整理':<34}{'判定':>16}")
    print("  " + "-" * 50)
    fail = 0

    def _assert(name: str, ok: bool) -> None:
        nonlocal fail
        fail += 0 if ok else 1
        print(f"  {name:<34}{('OK' if ok else '**FAIL**'):>16}")

    # ── D3 網址日期抽取（確定性、公司無關）
    D = ar._url_published_date
    _assert("網址日期 /2020/08/19/", D("https://www.cnbc.com/2020/08/19/apple-2t.html") == ar.date(2020, 8, 19))
    _assert("網址日期 -07-28-2026", D("https://www.wsj.com/livecoverage/stock-market-today-07-28-2026") == ar.date(2026, 7, 28))
    _assert("網址日期 /2026/07", D("https://reuters.com/2026/07/apple") == ar.date(2026, 7, 1))
    # Reuters／AP 式：日期在網址**結尾**。上一版樣式要求年份前必須是 `/`，整類都抽不到。
    _assert("網址日期 …-2026-08-11/", D(
        "https://www.reuters.com/business/apple-briefly-tops-5-trillion-2026-08-11/") == ar.date(2026, 8, 11))
    _assert("數據頁無日期 → None", D("https://stockanalysis.com/stocks/aapl/market-cap") is None)
    _assert("不合法日期 → None", D("https://x.com/2020/13/45/a") is None)
    # `published_date` 優先於網址（將來若改用 topic="news" 不必再動這裡）
    _assert("published_date 優先", ar._web_result_date(
        {"published_date": "Tue, 11 Aug 2026 17:00:00 GMT",
         "url": "https://www.cnbc.com/2020/08/19/a.html"}) == ar.date(2026, 8, 11))

    # ── D6 本地端白名單複核：地區子網域拿到的是**別的市場**的報價，必須擋掉。
    # 這些主機全部取自實跑：`ca.finance.yahoo.com/quote/TSLA.NE`（加拿大 NEO）、
    # `finance.yahoo.com/quote/TL0.SG`（新加坡）、`cn.wsj.com`（給出多年前的市值對比）。
    for h in ("ca.finance.yahoo.com", "hk.finance.yahoo.com", "cn.wsj.com",
              "apps.apple.com", "new.macrotrends.net"):
        _assert(f"擋掉子網域 {h}", not ar._host_allowed(h))
    for h in ("finance.yahoo.com", "www.reuters.com", "sec.gov", "www.sec.gov",
              "stockanalysis.com", "www.cnbc.com"):
        _assert(f"放行 {h}", ar._host_allowed(h))

    # ── D9 衍生性商品合約頁：拿到的是**別的標的**，價格是權利金不是股價。
    # 實測「特斯拉今天股價」一次跑分有 3 個名額被 OCC 選擇權頁佔走。
    for u in ("https://finance.yahoo.com/quote/TSLA260814C00257500",
              "https://finance.yahoo.com/quote/AAPL261218P00150000/",
              "https://finance.yahoo.com/quote/NVDA260814C00610000?p=x"):
        _assert(f"擋掉選擇權頁 {u[-22:]}", ar._is_derivative_page(u))
    # 負向控制：一般報價／資料頁**不得**被誤殺
    for u in ("https://finance.yahoo.com/quote/TSLA", "https://finance.yahoo.com/quote/AAPL/key-statistics",
              "https://stockanalysis.com/stocks/aapl/market-cap", "https://www.cnbc.com/quotes/TSLA",
              "https://www.cnbc.com/2026/07/28/apple-touches-5-trillion-market-cap.html"):
        _assert(f"不得誤殺 {u[-26:]}", not ar._is_derivative_page(u))

    # ── D5 同頁去重的等價類
    N = ar._normalize_url
    _assert("amp 與原頁同鍵", N("https://www.cnbc.com/amp/2020/08/19/a.html") == N("https://www.cnbc.com/2020/08/19/a.html"))
    _assert("new. 子網域與主站同鍵", N("https://new.macrotrends.net/x/y") == N("http://www.macrotrends.net/x/y"))
    _assert("不同頁不得同鍵", N("https://stockanalysis.com/stocks/aapl/") != N("https://stockanalysis.com/stocks/msft/"))

    # ── D2/D3 過濾行為
    as_of = ar.date(2026, 8, 14)
    raw = [
        {"url": "https://companiesmarketcap.com/apple/marketcap", "content": "a"},
        {"url": "https://companiesmarketcap.com/aud/apple/marketcap", "content": "b"},
        {"url": "https://companiesmarketcap.com/cad/apple/marketcap", "content": "c"},   # 超過單域名上限
        {"url": "https://www.cnbc.com/2020/08/19/apple-2t.html", "content": "d"},        # 六年前
        {"url": "https://www.macrotrends.net/stocks/charts/AAPL/apple/market-cap", "content": "e"},
        {"url": "https://new.macrotrends.net/stocks/charts/AAPL/apple/market-cap", "content": "f"},  # 同頁
        {"url": "https://stockanalysis.com/stocks/aapl/market-cap", "content": "g"},
    ]
    kept, stats = ar._dedupe_web_results([dict(r) for r in raw], "intraday", as_of)
    doms = [ar._domain_of(r["url"]) for r in kept]
    _assert("單域名不得超過上限", doms.count("companiesmarketcap.com") <= ar.TAVILY_PER_DOMAIN_CAP)
    _assert("同頁變體只佔一個名額", doms.count("macrotrends.net") == 1)
    _assert("六年前的文章被濾掉", not any("cnbc.com" in d for d in doms) and stats["stale"] == 1)
    _assert("無日期的數據頁保留", any("stockanalysis.com" in d for d in doms))
    _assert("不超過 TAVILY_MAX_RESULTS", len(kept) <= ar.TAVILY_MAX_RESULTS)
    # 三週前的報導：問「今天股價」時必須擋掉，問「近期發展」時必須留下。
    # 實測（2026-08-14）：門檻設 90 天時，一則 22 天前的 WSJ 報導（$319.69 / −14.52%）被模型
    # 當成「最新可得」寫進「特斯拉今天股價」的答案，反把未標日期的即時行情頁降為次要。
    three_wk = [{"url": "https://www.wsj.com/livecoverage/stock-market-today-07-23-2026/card/tesla-slides",
                 "content": "x"}]
    k_intra, _ = ar._dedupe_web_results([dict(r) for r in three_wk], "intraday", as_of)
    k_days, _ = ar._dedupe_web_results([dict(r) for r in three_wk], "days", as_of)
    _assert("三週前報導：intraday 擋掉", not k_intra)
    _assert("三週前報導：days 保留", len(k_days) == 1)

    # 負向控制：need="none"（問財報期間）時歷史文章**不該**被濾掉
    kept_none, stats_none = ar._dedupe_web_results([dict(r) for r in raw], "none", as_of)
    _assert("need=none 不濾歷史文章", stats_none["stale"] == 0
            and any("cnbc.com" in ar._domain_of(r["url"]) for r in kept_none))

    # ── D1 摘要不得再被截掉數字（**這是 2026-08-14 的主因，必須端到端驗**）
    # 造一則「數字排在站台樣板文字之後」的結果——正是 macrotrends 的真實形狀。
    MARKER = "$4572.79B"
    padded = ("Market capitalization is the most commonly used method of measuring the size of a "
              "publicly traded company. " * 8) + f" Apple market cap as of August 2026 is {MARKER}."
    assert padded.index(MARKER) > 300, "測資本身要讓數字落在 300 字之後，否則這格驗不到東西"

    class _FakeClient:
        def __init__(self, api_key=None):
            pass

        def search(self, query, **kw):
            return {"results": [{"url": "https://www.macrotrends.net/stocks/charts/AAPL/apple/market-cap",
                                 "title": "Apple Market Cap", "content": padded}]}

    fake_mod = types.ModuleType("tavily")
    fake_mod.TavilyClient = _FakeClient
    orig_mod, orig_key = sys.modules.get("tavily"), os.environ.get("TAVILY_API_KEY")
    sys.modules["tavily"] = fake_mod
    os.environ["TAVILY_API_KEY"] = "stub-key"
    try:
        note = _REAL_TAVILY_SEARCH("Apple market cap", need="intraday")
    finally:
        sys.modules.pop("tavily", None) if orig_mod is None else sys.modules.__setitem__("tavily", orig_mod)
        os.environ.pop("TAVILY_API_KEY", None) if orig_key is None else os.environ.__setitem__("TAVILY_API_KEY", orig_key)
    _assert("摘要不得截掉深處的數字", MARKER in note)
    _assert("無日期時標「未標示日期」", "未標示日期" in note)

    # ── D8 時效警語不得與答案自相矛盾。
    # 實測：Azure 那題主體引用了 CNBC 與 sec.gov 兩個 web 來源，底下卻印「Web 未提供可用補充」。
    gap = [{"ticker": "MSFT", "cutoff": "2026-06-12", "as_of": "2026-08-14"}]
    mixed = [{"status": "done", "web_used": False, "freshness_gaps": gap},
             {"status": "done", "web_used": True, "freshness_gaps": []}]
    none_web = [{"status": "done", "web_used": False, "freshness_gaps": gap}]
    n_mixed = ar._format_unresolved_freshness_notice(mixed)
    n_none = ar._format_unresolved_freshness_notice(none_web)
    _assert("有用 web 時不得說「未提供」", "未提供可用補充" not in n_mixed and "部分子問題" in n_mixed)
    _assert("完全沒用 web 時照舊", "未提供可用補充" in n_none)
    _assert("無缺口 → 不印警語", ar._format_unresolved_freshness_notice(
        [{"status": "done", "web_used": False, "freshness_gaps": []}]) == "")

    # ── D4 後半：整個 query 的 web 預算是**跨子問題**的硬上限。
    # ⚠ 舊的 `WEB_SEARCH_MAX_CALLS` 記在 `_RunState`，而 `_RunState` 每個子問題歸零，
    #   所以它的真實語意是「每個子問題 N 次」，總量無上界——實跑量到 7 次。這一格量的是總量。
    ar._reset_query_web_budget()
    taken = sum(1 for _ in range(ar.QUERY_WEB_BUDGET + 5) if ar._take_query_web_budget())
    _assert(f"query 級 web 預算封頂（實得 {taken}）", taken == ar.QUERY_WEB_BUDGET)
    ar._reset_query_web_budget()
    _assert("歸零後恢復額度", ar._take_query_web_budget())
    # 負向控制：預算不得由 executor 歸零——那正是舊計數器失效的原因。
    ar._reset_query_web_budget()
    for _ in range(ar.QUERY_WEB_BUDGET):
        ar._take_query_web_budget()
    ar._reset_run_pool()          # 模擬「換下一個子問題」
    _assert("換子問題不得重置預算", not ar._take_query_web_budget())
    ar._reset_query_web_budget()

    # ── D4 前半：KB 補不了的不足 → 執行層必須**立刻**跳出改寫迴圈，不是燒完 MAX_REWRITES。
    # 全程零 LLM 零網路：把 Grader／檢索／摘要／web 都換成 stub，只量「檢索被呼叫幾次」。
    n_retrieve = {"n": 0}

    # ⚠ 必須吃 `attributable`（2026-08-28 起 `_retrieve_chunks` 的必填 keyword-only 參數）。
    #   這支只數呼叫次數，歸因與否不影響它要量的東西；「每個呼叫點都傳了」由
    #   `verify_answer_validators.py` 的 ⑮f2 守。
    def _fake_retrieve(q, *, attributable=True):
        n_retrieve["n"] += 1
        return [{"source": "AAPL_Fundamentals_20260612.txt", "chunk_index": 0, "text": "x",
                 "rerank_score": 1.0, "raw_rerank_score": 1.0}]

    def _fake_check(subquery, pool, temporal_scope="", freshness_mode=ar.FRESHNESS_SNAPSHOT):
        return {"sufficient": False, "missing": "太舊", "new_query": subquery,
                "relevant_ids": [], "realtime_need": "intraday", "kb_unfixable": True}

    saved = (ar._retrieve_chunks, ar._check_sufficiency, ar._fallback_local_summary, ar._tavily_search)
    ar._retrieve_chunks, ar._check_sufficiency = _fake_retrieve, _fake_check
    ar._fallback_local_summary = lambda task, chunks: "stub"
    ar._tavily_search = lambda q, need="none": "（stub）"
    try:
        ar._run_executor_deterministic("蘋果的即時市值", "", ar.FRESHNESS_LIVE, 0, False,
                                       attributable=True)
    finally:
        (ar._retrieve_chunks, ar._check_sufficiency,
         ar._fallback_local_summary, ar._tavily_search) = saved
    _assert(f"KB 補不了 → 只檢索 1 次（實得 {n_retrieve['n']}）", n_retrieve["n"] == 1)

    # 負向控制：一般的「不夠」仍然要用滿改寫次數，否則等於把補救能力一起關掉。
    n_retrieve["n"] = 0

    def _fake_check_plain(subquery, pool, temporal_scope="", freshness_mode=ar.FRESHNESS_SNAPSHOT):
        return {"sufficient": False, "missing": "離題", "new_query": subquery,
                "relevant_ids": [], "realtime_need": "none", "kb_unfixable": False}

    saved = (ar._retrieve_chunks, ar._check_sufficiency, ar._fallback_local_summary, ar._tavily_search)
    ar._retrieve_chunks, ar._check_sufficiency = _fake_retrieve, _fake_check_plain
    ar._fallback_local_summary = lambda task, chunks: "stub"
    ar._tavily_search = lambda q, need="none": "（stub）"
    try:
        ar._run_executor_deterministic("蘋果的營收", "", ar.FRESHNESS_SNAPSHOT, 0, False,
                                       attributable=True)
    finally:
        (ar._retrieve_chunks, ar._check_sufficiency,
         ar._fallback_local_summary, ar._tavily_search) = saved
    _assert(f"一般不足仍跑滿改寫（實得 {n_retrieve['n']}）", n_retrieve["n"] == ar.MAX_REWRITES + 1)
    return fail


def _check_replay_miss_is_loud() -> int:
    """閘門⑦：replay 的 miss 不准被任何 `except Exception` 吞掉（2026-08-28）。

    **為什麼是結構性而非契約**：舊版靠「呼叫端記得 re-raise」。`_tavily_search` 記得了，
    但上一層 `_run_executor_deterministic` 的 `except Exception` 照樣接走 → 印一行
    「降級」就繼續跑 → 量尺把「fixture 沒涵蓋」報成「系統沒打 web」。全碼庫 16 個
    `except Exception`，只要有一個沒加 re-raise 就破功。

    ⚠ **誤報對照（⑦d）不可省**：只驗「FixtureMiss 會傳播」的話，一個把整個 try/except
      拿掉的實作也會通過——那等於把 executor 的容錯關掉。所以要同時驗「一般例外仍然被降級接住」。
    """
    import llm_replay as _lr
    import web_replay as _wr

    results: list[tuple[str, bool, str]] = []

    # ── ⑦a/⑦b 型別：不在 Exception 階層裡，但仍是 BaseException ────────────────
    for nm, exc in (("web_replay.FixtureMiss", _wr.FixtureMiss),
                    ("web_replay.RecordError", _wr.RecordError),
                    ("llm_replay.ReplayCacheMiss", _lr.ReplayCacheMiss)):
        results.append((f"⑦a {nm} 不是 Exception 子類", not issubclass(exc, Exception),
                        "`except Exception` 抓不到" if not issubclass(exc, Exception)
                        else "**會被通用處理吞掉**"))
        results.append((f"⑦b {nm} 仍是 BaseException 子類", issubclass(exc, BaseException), ""))

    # ── ⑦c 行為：executor 內部丟 FixtureMiss 必須傳播出來 ─────────────────────
    def _mk_stubs(boom):
        calls = {"n": 0}

        def _fake_retrieve(q, *, attributable=True):
            calls["n"] += 1
            if calls["n"] == 1 and boom is not None:
                raise boom("stub")
            return [{"source": "AAPL_Fundamentals_20260612.txt", "chunk_index": 0, "text": "x",
                     "rerank_score": 1.0, "raw_rerank_score": 1.0}]

        def _fake_check(subquery, pool, temporal_scope="", freshness_mode=ar.FRESHNESS_SNAPSHOT):
            return {"sufficient": True, "missing": "", "new_query": "",
                    "relevant_ids": [], "realtime_need": "none", "kb_unfixable": False}
        return _fake_retrieve, _fake_check

    def _run(boom):
        """回傳 (傳播出來的例外型別 or None)。"""
        fr, fc = _mk_stubs(boom)
        saved = (ar._retrieve_chunks, ar._check_sufficiency,
                 ar._fallback_local_summary, ar._tavily_search)
        ar._retrieve_chunks, ar._check_sufficiency = fr, fc
        ar._fallback_local_summary = lambda task, chunks, period_note="": "stub"
        ar._tavily_search = lambda q, need="none": "（stub）"
        try:
            # ⑦c/⑦d 測的是例外傳播，跟歸因無關；給 True 是為了與生產最常見的路徑一致。
            ar._run_executor_deterministic("蘋果的營收", "", ar.FRESHNESS_SNAPSHOT, 0, False,
                                           attributable=True)
            return None
        except BaseException as e:          # noqa: BLE001 - 要的就是抓到什麼型別
            return type(e)
        finally:
            (ar._retrieve_chunks, ar._check_sufficiency,
             ar._fallback_local_summary, ar._tavily_search) = saved

    got = _run(_wr.FixtureMiss)
    results.append(("⑦c FixtureMiss 從 executor 傳播出來（不被降級吞掉）",
                    got is _wr.FixtureMiss, f"實得 {got}"))

    got = _run(RuntimeError)
    results.append(("⑦d 誤報對照：一般例外仍然被降級接住（容錯沒被關掉）",
                    got is None, f"實得 {got}"))

    # ── ⑦e `_tavily_search` 這一層（原始契約仍然成立）──────────────────────────
    # ⚠ 兩個都不能省，兩個都是自己踩出來的：
    #   ① 必須用 `_REAL_TAVILY_SEARCH`——本檔在 import 時就把 `ar._tavily_search` 換成
    #      「絕不連網」的 stub，直接叫它只會拿到 stub 的回傳（這正是本檔 L48 註解記的
    #      2026-08-13 那個坑，閘門④ 已經踩過一次）。
    #   ② 必須自己開 `ENABLE_WEB_SEARCH`——真身第一行就是 `if not ENABLE_WEB_SEARCH: return`。
    _saved_raw, _saved_en = ar._tavily_raw, ar.ENABLE_WEB_SEARCH
    try:
        def _boom(q):
            raise _wr.FixtureMiss("stub")
        ar._tavily_raw, ar.ENABLE_WEB_SEARCH = _boom, True
        try:
            _REAL_TAVILY_SEARCH("q")
            got = None
        except BaseException as e:          # noqa: BLE001
            got = type(e)
    finally:
        ar._tavily_raw, ar.ENABLE_WEB_SEARCH = _saved_raw, _saved_en
    results.append(("⑦e _tavily_search 不把 FixtureMiss 降級成「查無結果」",
                    got is _wr.FixtureMiss, f"實得 {got}"))

    print()
    print(f"  {'replay miss 必須大聲炸':<52}{'判定':>8}")
    print("  " + "-" * 68)
    fail = 0
    for name, ok, note in results:
        fail += 0 if ok else 1
        print(f"  {name:<52}{('OK' if ok else 'FAIL'):>8}  {'' if ok else note}")
    return fail


def _check_replan_is_replayed() -> int:
    """閘門⑧：replan 的決策要進重放快取，且 key 不含自由文字結果（2026-08-29）。

    **為什麼**：`_node_replan` 曾是全碼庫**唯一沒被錄下來的 LLM 呼叫**。它每輪重抽 → 生出
    措辭不同的 todo → 那個 todo 的 `check` key 是新的 → `new_query` 新 → 英譯新 →
    Tavily key 新 → `FixtureMiss`。實測 web-04 單輪 `hit=6 miss=7`，而重錄 fixture 治不好
    （key 空間本來就無界，源頭沒釘死就會一直生新的）。

    ⚠ **判別力全在誤報對照**（⑧d~⑧g）：只驗「⑧c 快取會命中」的話，一個「key 是常數」的
      實作也會滿分——而那會讓**所有** replan 決策互相蓋掉。所以每一個「應該要 miss」的
      維度都要各有一條：task 變、freshness_mode 變、query 變、status 變。
    """
    import ast
    import inspect
    import llm_replay as _lr

    results: list[tuple[str, bool, str]] = []

    results.append(("⑧a 'replan' 已註冊進 llm_replay._KNOWN_KINDS",
                    "replan" in _lr._KNOWN_KINDS,
                    "沒註冊 → RAG_REPLAY_MODE=strict:replan 會被判成拼錯,strict 靜默失效"))

    # ── ⑧b AST：get 與 put 都在 _node_replan 裡，kind 字面必須是 'replan' ──────────
    tree = ast.parse(inspect.getsource(ar._node_replan))
    seen = {"get": set(), "put": set()}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in seen and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_replay" and node.args
                and isinstance(node.args[0], ast.Constant)):
            seen[node.func.attr].add(node.args[0].value)
    results.append(("⑧b _node_replan 同時有 _replay.get/put('replan')",
                    seen["get"] == {"replan"} and seen["put"] == {"replan"},
                    f"實得 get={sorted(seen['get'])} put={sorted(seen['put'])}"))

    # ── 行為測試共用：跑兩次 _node_replan，數第二次還有沒有真的叫 LLM ────────────────
    def _todo(tid, task, status="done", result=""):
        return {"id": tid, "task": task, "status": status, "result": result,
                "temporal_scope": "", "attributable": True, "freshness_gaps": [],
                "period_notes": [], "web_used": False}

    def _twice(state_a, state_b):
        calls = {"n": 0}

        def _fake_llm(messages, model_name, temperature=0.0):
            calls["n"] += 1
            return '{"sufficient": false, "add": [], "drop": []}'

        saved = (_lr._CACHE, _lr.enabled, _lr._DIRTY,
                 ar.rq.call_llm, ar._build_temporal_contract)
        # 純記憶體：_PATH 仍是 None → _flush() 的守衛會擋掉落盤，這支不碰檔案系統。
        _lr._CACHE, _lr.enabled = {}, (lambda: True)
        ar.rq.call_llm = _fake_llm
        ar._build_temporal_contract = lambda mode: "(stub)"
        try:
            ar._node_replan(state_a)
            n1 = calls["n"]
            ar._node_replan(state_b)
            return n1, calls["n"] - n1
        finally:
            (_lr._CACHE, _lr.enabled, _lr._DIRTY,
             ar.rq.call_llm, ar._build_temporal_contract) = saved

    BASE = [_todo(0, "Tesla 最近的重大新聞是什麼")]

    def _st(todos, q="Tesla 最近有什麼重要消息？", mode=ar.FRESHNESS_LIVE):
        return {"query": q, "freshness_mode": mode, "todos": todos, "collected": []}

    # ⑧c 只有 result 不同 → 必須命中（result 是 executor 生成的摘要，每輪都不一樣）
    n1, n2 = _twice(_st([_todo(0, "Tesla 最近的重大新聞是什麼", result="摘要甲 …")]),
                    _st([_todo(0, "Tesla 最近的重大新聞是什麼", result="完全不同的摘要乙 …")]))
    results.append(("⑧c 只有 result 不同 → 第二次命中快取（不再叫 LLM）",
                    n1 == 1 and n2 == 0,
                    f"第一次 {n1} 次、第二次 {n2} 次 LLM（納入 result 會讓快取永不命中）"))

    # ── ⑧d~⑧g 誤報對照：每一個「應該要 miss」的維度各一條 ──────────────────────
    for tag, sa, sb, why in (
        ("⑧d task 不同 → 必須 miss", _st(BASE),
         _st([_todo(0, "Tesla 的營收是多少")]), "不同待辦清單共用決策"),
        ("⑧e freshness_mode 不同 → 必須 miss", _st(BASE),
         _st(BASE, mode=ar.FRESHNESS_SNAPSHOT), "snapshot 與 live 的決策互蓋"),
        ("⑧f 原始問題不同 → 必須 miss", _st(BASE),
         _st(BASE, q="Apple 最近有什麼消息？"), "不同問題共用決策"),
        ("⑧g status 不同 → 必須 miss", _st(BASE),
         _st([_todo(0, "Tesla 最近的重大新聞是什麼", status="pending")]),
         "「做完了沒」這個層級的進展分不出來"),
    ):
        n1, n2 = _twice(sa, sb)
        results.append((tag, n1 == 1 and n2 == 1, f"第二次 {n2} 次 LLM（key 太粗＝{why}）"))

    # ⑧h web_used 是第五個「合法改變決策」的維度（2026-08-29 起它會改變 prompt 內容）
    n1, n2 = _twice(_st([_todo(0, "Tesla 最近的重大新聞是什麼")]),
                    _st([dict(_todo(0, "Tesla 最近的重大新聞是什麼"), web_used=True)]))
    results.append(("⑧h web_used 不同 → 必須 miss",
                    n1 == 1 and n2 == 1,
                    f"第二次 {n2} 次 LLM（「已打過 web」與「還沒打」共用決策"
                    f"＝把 _REPLANNER_LIVE_BLOCK 的修法抵銷掉）"))

    # ── ⑧i snapshot 的 replanner system prompt 逐字不變（＝65 題基準沒被動到的證明）──
    #    同閘門③ 對 Generator system message 的作法：live 專屬的段落只准出現在 live。
    def _sys_prompt_for(mode):
        got = {}

        def _cap_llm(messages, model_name, temperature=0.0):
            got["sys"] = messages[0]["content"]
            return '{"sufficient": false, "add": [], "drop": []}'

        saved = (_lr._CACHE, _lr.enabled, ar.rq.call_llm, ar._build_temporal_contract)
        _lr._CACHE, _lr.enabled = {}, (lambda: False)   # 關快取,強制走真正的 prompt 組裝
        ar.rq.call_llm = _cap_llm
        ar._build_temporal_contract = lambda m: "(contract stub)"
        try:
            ar._node_replan(_st([_todo(0, "任意待辦", result="任意結果")], mode=mode))
            return got.get("sys", "")
        finally:
            (_lr._CACHE, _lr.enabled, ar.rq.call_llm, ar._build_temporal_contract) = saved

    _snap, _live_p = _sys_prompt_for(ar.FRESHNESS_SNAPSHOT), _sys_prompt_for(ar.FRESHNESS_LIVE)
    results.append(("⑧i snapshot 的 replanner prompt 不含 live 專屬段（基準不動）",
                    ar._REPLANNER_LIVE_BLOCK not in _snap
                    and _snap.startswith(ar._REPLANNER_PROMPT)
                    and _snap.endswith("(contract stub)")
                    # 長度剛好＝原 prompt ＋ 兩個換行 ＋ contract：中間塞不下任何東西
                    and len(_snap) == len(ar._REPLANNER_PROMPT) + len("(contract stub)") + 2,
                    "snapshot 的 prompt 被動到 → 65 題基準跨這次改動不可比"))
    results.append(("⑧j 誤報對照：live 的 replanner prompt 必須含那一段",
                    ar._REPLANNER_LIVE_BLOCK in _live_p,
                    "live 也沒加＝這個修法根本沒生效,而 ⑧i 照樣會綠"))

    # ── ⑧k~⑧o 徒勞的 web 重試要被擋，而**新聞題不可以被擋**（2026-08-29）────────────
    def _td(**kv):
        base = {"id": 0, "task": "t", "status": "done", "result": "", "web_used": False,
                "realtime_need": "none"}
        base.update(kv)
        return base

    for tag, todos_, want, why in (
        ("⑧k intraday ＋ 已打過 web → 判定徒勞", [_td(web_used=True, realtime_need="intraday")],
         True, "實測 12/12 個這種待辦貢獻 0，664 秒全白燒"),
        ("⑧l 誤報對照：days（新聞）＋ 已打過 web → **不可**判徒勞",
         [_td(web_used=True, realtime_need="days")], False,
         "第一版用 prompt 寫『搜過就收斂』，把新聞題 4 個有貢獻的待辦一起殺掉了"),
        ("⑧m 誤報對照：intraday 但還沒打過 web → **不可**判徒勞",
         [_td(web_used=False, realtime_need="intraday")], False, "第一次都不准搜＝時效能力歸零"),
        ("⑧n 誤報對照：none（財報題）→ 不可判徒勞",
         [_td(web_used=True, realtime_need="none")], False, ""),
    ):
        got = ar._web_retry_is_pointless(todos_)
        results.append((tag, got is want, f"實得 {got}（{why}）"))

    # ⑧o 接線：光有判準不夠，`_node_replan` 要真的據此拒絕（值測試，不是驗「有呼叫」）
    def _replan_adds(todos_, add_list):
        saved = (_lr._CACHE, _lr.enabled, ar.rq.call_llm, ar._build_temporal_contract)
        _lr._CACHE, _lr.enabled = {}, (lambda: False)
        ar.rq.call_llm = lambda m, mn, temperature=0.0: json.dumps(
            {"sufficient": False, "add": add_list, "drop": []})
        ar._build_temporal_contract = lambda mode: "(stub)"
        try:
            out = ar._node_replan({"query": "q", "freshness_mode": ar.FRESHNESS_LIVE,
                                   "todos": todos_, "collected": []})
            return [t["task"] for t in out["todos"] if t["id"] != 0]
        finally:
            (_lr._CACHE, _lr.enabled, ar.rq.call_llm, ar._build_temporal_contract) = saved

    _saved_web = ar.ENABLE_WEB_SEARCH
    ar.ENABLE_WEB_SEARCH = True
    try:
        blocked = _replan_adds([_td(web_used=True, realtime_need="intraday")],
                               ["改用網路搜尋查 NVIDIA 現在的股價"])
        # ⚠ 這一格是 2026-08-29 實測抓到的漏洞：`_WEB_TODO_RE` 是 `網路|上網|web search|internet`，
        #   而 replan 實際生出的是「在 Yahoo Finance 上查詢…」「使用 NASDAQ 官方網站…」
        #   ——**一個都不匹配**。所以守衛不可以用待辦文字當前置條件。
        blocked_site = _replan_adds([_td(web_used=True, realtime_need="intraday")],
                                    ["在 Yahoo Finance 上查詢 NVDA 當前股價",
                                     "使用NASDAQ官方網站或API查詢NVDA即時股價"])
        kept = _replan_adds([_td(web_used=True, realtime_need="days")],
                            ["改用網路搜尋查 Tesla 最近的重大新聞"])
        kept2 = _replan_adds([_td(web_used=False, realtime_need="intraday")],
                             ["改用網路搜尋查 NVIDIA 現在的股價"])
    finally:
        ar.ENABLE_WEB_SEARCH = _saved_web
    results.append(("⑧o 接線：intraday 的 web 重試真的被 _node_replan 拒絕",
                    blocked == [], f"實得 {blocked}（判準存在但沒接上＝完全沒作用）"))
    results.append(("⑧p **不含「網路」二字**的站名措辭也要被拒（詞表漏洞）",
                    blocked_site == [], f"實得 {blocked_site}（_WEB_TODO_RE 匹配不到站名）"))
    results.append(("⑧q 誤報對照：新聞題（days）的 web 待辦仍然加得進去",
                    len(kept) == 1, f"實得 {kept}（連新聞題一起擋＝把功能關掉冒充修好）"))
    results.append(("⑧r 誤報對照：intraday 但還沒搜過 → 第一次仍加得進去",
                    len(kept2) == 1, f"實得 {kept2}（第一次都不准搜＝時效能力歸零）"))

    # ── ⑧s~⑧v：replan 建的 todo 也帶 route（2026-09-02，計畫第 5 步）──────────────
    # ⚠ 這一組取代了 `_WEB_TODO_RE`：**snapshot 的 web-todo 拒絕判準從「待辦文字裡有沒有
    #   『網路』二字」換成「route 欄位是什麼」**。那個詞表匹配不到「在 Yahoo Finance 上查詢」
    #   （⑧p 逐字凍結了六個），而路由現在是欄位，不需要從中文句子反推。
    def _replan_todos(todos_, add_list, fm=LIVE):
        saved = (_lr._CACHE, _lr.enabled, ar.rq.call_llm, ar._build_temporal_contract)
        _lr._CACHE, _lr.enabled = {}, (lambda: False)
        ar.rq.call_llm = lambda m, mn, temperature=0.0: json.dumps(
            {"sufficient": False, "add": add_list, "drop": []})
        ar._build_temporal_contract = lambda mode: "(stub)"
        try:
            out = ar._node_replan({"query": "q", "freshness_mode": fm,
                                   "todos": todos_, "collected": []})
            return [(t["task"], t.get("route")) for t in out["todos"] if t["id"] != 0]
        finally:
            (_lr._CACHE, _lr.enabled, ar.rq.call_llm, ar._build_temporal_contract) = saved

    _saved_web2 = ar.ENABLE_WEB_SEARCH
    ar.ENABLE_WEB_SEARCH = True
    try:
        _new_fmt = _replan_todos([_td()], [{"task": "Tesla 最近的新聞", "route": "web"},
                                           {"task": "Tesla 上季營收", "route": "kb"}])
        _legacy = _replan_todos([_td()], ["改用網路搜尋查 Tesla 最近的新聞"])
        _snap_web = _replan_todos([_td()], [{"task": "Tesla 最近的新聞", "route": "web"}],
                                  fm=SNAP)
        _snap_kb = _replan_todos([_td()], [{"task": "Tesla 上季營收", "route": "kb"}], fm=SNAP)
    finally:
        ar.ENABLE_WEB_SEARCH = _saved_web2

    results.append(("⑧s replan 建的 todo 帶 route，且值是 LLM 給的那個",
                    _new_fmt == [("Tesla 最近的新聞", "web"), ("Tesla 上季營收", "kb")],
                    f"實得 {_new_fmt}；沒有 route ＝ 落到預設 kb ＝ 新聞題又去撈財報"))
    results.append(("⑧t 舊格式（純字串）的 add 仍吃得下，route 落到 kb",
                    _legacy == [("改用網路搜尋查 Tesla 最近的新聞", "kb")],
                    f"實得 {_legacy}；既有 replay 快取錄的全是字串,不吃就是全部失效"))
    results.append(("⑧u snapshot 下 route=web 的追加待辦被拒（判準已換成 route,不是詞表）",
                    _snap_web == [], f"實得 {_snap_web}"))
    results.append(("⑧v **誤報對照**：snapshot 下 route=kb 的追加待辦仍加得進去",
                    _snap_kb == [("Tesla 上季營收", "kb")],
                    f"實得 {_snap_kb}（一起擋掉＝把 replan 關掉冒充修好）"))

    # ⑧w route 必須是重放 key 的一個維度：它會改變 replan 的決策輸入。
    _seen_keys = []
    _o = (_lr._CACHE, _lr.enabled, _lr.put, ar.rq.call_llm, ar._build_temporal_contract)
    _lr._CACHE, _lr.enabled = {}, (lambda: True)
    _lr.put = lambda kind, key, val: _seen_keys.append(key)
    ar.rq.call_llm = lambda m, mn, temperature=0.0: json.dumps(
        {"sufficient": False, "add": [], "drop": []})
    ar._build_temporal_contract = lambda mode: "(stub)"
    try:
        for _r in ("kb", "web"):
            ar._node_replan({"query": "q", "freshness_mode": LIVE, "collected": [],
                             "todos": [dict(_td(), route=_r)]})
    finally:
        (_lr._CACHE, _lr.enabled, _lr.put, ar.rq.call_llm, ar._build_temporal_contract) = _o
    results.append(("⑧w **誤報對照**：todo 的 route 不同 → 重放 key 必須不同",
                    len(_seen_keys) == 2 and _seen_keys[0] != _seen_keys[1],
                    f"實得 {_seen_keys}；key 不含 route ＝ 兩種路由的 replan 決策互相蓋掉"))

    print()
    print(f"  {'replan 必須可重放（key 不含自由文字結果）':<52}{'判定':>8}")
    print("  " + "-" * 68)
    fail = 0
    for name, ok, note in results:
        fail += 0 if ok else 1
        print(f"  {name:<52}{('OK' if ok else 'FAIL'):>8}  {'' if ok else note}")
    return fail


def _check_fixture_binding_is_preserved() -> int:
    """閘門⑨：fixture 的環境綁定不准被 replay 改寫，且開跑前要被檢查（2026-08-29）。

    **被一次三週的靜默失效逼出來的。** `note_meta()` 舊版無條件寫 `_meta`，於是**每一次
    replay 都把綁定改成當下的環境**——那一格記的不再是「錄在什麼條件下」而是「上次誰跑過」。
    後果不是欄位不準，是**證據自己抹掉自己**：2026-08-27 生產從 `..._mdna` 換到
    `..._multiyear` 之後，fixture 每跑一次就自動宣稱綁在新 collection 上，於是
    「fixture 對不上 collection」三週都沒有任何跡象，外觀只是「LLM 不穩」。

    ⚠ **誤報對照（⑨b）不可省**：只驗「replay 不寫」的話，一個 `note_meta` 直接 `return` 的
      實作也會通過——那樣連 record 都不寫，fixture 從此沒有綁定可查。
    """
    import web_replay as _wr

    results: list[tuple[str, bool, str]] = []

    def _note_with(mode: str) -> dict:
        saved = (_wr._CACHE, _wr._DIRTY, os.environ.get("RAG_WEB_REPLAY_MODE"),
                 os.environ.get("RAG_WEB_REPLAY"))
        _wr._CACHE = {"responses": {}, "_meta": {"collection": "ORIGINAL"}}
        os.environ["RAG_WEB_REPLAY_MODE"] = mode
        os.environ["RAG_WEB_REPLAY"] = "(記憶體,_PATH 是 None 所以不落盤)"
        try:
            _wr.note_meta(collection="OVERWRITTEN")
            return dict(_wr._CACHE.get("_meta") or {})
        finally:
            _wr._CACHE, _wr._DIRTY = saved[0], saved[1]
            for k, v in (("RAG_WEB_REPLAY_MODE", saved[2]), ("RAG_WEB_REPLAY", saved[3])):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    got = _note_with("replay")
    results.append(("⑨a replay 模式不得改寫 fixture 的 _meta",
                    got.get("collection") == "ORIGINAL",
                    f"實得 {got.get('collection')!r}（綁定被 replay 覆寫＝證據自我抹除）"))
    got = _note_with("record")
    results.append(("⑨b 誤報對照：record 模式仍然要寫得進去",
                    got.get("collection") == "OVERWRITTEN",
                    f"實得 {got.get('collection')!r}（連 record 都不寫＝從此沒有綁定可查）"))

    # ⑨c 開跑前真的有比對這一格（讀 record_web_fixture 的原始碼，零執行）
    src = (Path(__file__).parent / "record_web_fixture.py").read_text(encoding="utf-8")
    has_check = ('_meta.get("collection")' in src or '.get("collection")' in src)         and "ABORT" in src and "COLLECTION_NAME" in src
    results.append(("⑨c record_web_fixture 開跑前比對 fixture 綁的 collection",
                    has_check, "沒有比對 → _meta 記了但沒人讀＝記錄不等於檢查"))
    results.append(("⑨d 同上，as-of 也要比對（它決定 kb_unfixable）",
                    'as_of' in src and 'args.as_of' in src and "ABORT" in src, ""))

    print()
    print(f"  {'fixture 綁定不可自我抹除':<52}{'判定':>8}")
    print("  " + "-" * 68)
    fail = 0
    for name, ok, note in results:
        fail += 0 if ok else 1
        print(f"  {name:<52}{('OK' if ok else 'FAIL'):>8}  {'' if ok else note}")
    return fail


def _check_route_dispatch() -> int:
    """閘門⑪：**路由是 todo 上的欄位，分派是確定性的**（2026-09-01 先寫斷言、後改碼）。

    **為什麼**：現在的路由散在三個地方，沒有一個是為它設計的——
    ① Grader 的 `realtime_need`（三分類 LLM 欄位，一個措辭族實測 0/3）
    ② Replanner 把「改用網路搜尋查…」寫成**自由文字**塞進 todo（實測 news-11 一題 6 個近義串）
    ③ `_WEB_TODO_RE` 用 regex 把那句中文**再解析回來**（匹配不到「在 Yahoo Finance 上查詢」）。
    改動的本質是：**別再把路由決定編碼成一句中文再叫 regex 解析回來**，讓它變成欄位。

    ⚠ **這支現在會 FAIL，那是對的**：`_dispatch_todo`／`_escalate_route`／`_parse_plan_output`
      都還不存在。斷言先寫，是為了讓「改完到底算不算對」在動工前就定義好。
      計畫見 [`BACKLOG.md`](../BACKLOG.md)〈把「路由」改成 todo 上的 `route`〉。

    ⚠ **判別力集中在三條誤報對照**，少了它們一個「一律 kb」或「一律上網」的實作也會滿分：
      · **⑪f** 非法 route 必須在 dispatch **當場炸**——靜默預設成 kb 的話，Plan 打錯一個字
        就會讓新聞題全部退回「拿財報硬答」，而那個失敗的外觀與「路由判成 kb」**完全相同**。
      · **⑪i** `kb` ＋ 不足但**非** `kb_unfixable` → **仍然是 kb**。少了這條，
        一個「不足就升級上網」的實作會全綠，而那正是現在這條路（`not sufficient` 就打 web）
        ——等於改了個寂寞，還把 record 那半的過度路由做得更嚴重。
      · **⑪n** 舊格式 `list[str]` 的 route 預設必須是 **kb**。既有 fixture 錄的 plan 值全是
        字串陣列；預設成 web 或 both 會讓所有既有重放**憑空多打網路**＝基準不再可比。

    ⚠ **⑪n 與 ⑪q/⑪r 的預設值刻意不同，那不是筆誤**——它們回答的是不同的問題：
      · ⑪n（舊格式字串）管**可重現性**：那些 fixture 是在「KB 先撈、web 當 fallback」的世界
        錄的，落到 `kb` 才不會生出新的 Tavily 呼叫與新的 fixture key。
      · ⑪q/⑪r（新格式缺值／非法值）管**代價不對稱**，判準沿用 `realtime_need` 那條
        「判不出來填 days 不填 none」：誤判成 `kb` 會讓新聞題**拿財報冒充新聞且零揭露**
        （`check_news_routing.py` 量的正是這個，而它看不見）；誤判成 `both` 只是多打一次網路。
    """
    import ast
    import inspect

    results: list[tuple[str, bool, str]] = []

    def _has(name: str):
        return getattr(ar, name, None)

    VALID = _has("VALID_ROUTES")
    results.append(("⑪a ar.VALID_ROUTES 是宣告好的封閉集合 {kb, web, both}",
                    VALID is not None and set(VALID) == {"kb", "web", "both"},
                    f"實得 {VALID!r}（封閉集合要列清單,不要散在各處的字串比對）"))

    dispatch = _has("_dispatch_todo")
    results.append(("⑪b ar._dispatch_todo 存在且可呼叫", callable(dispatch),
                    "還沒實作（計畫 A4）"))

    # ── 行為測試：把兩個 tool 都換成計數樁，零網路零 Qdrant ────────────────────────
    def _run(route):
        """回 (kb 次數, web 次數, 例外)。兩個 tool 都換成樁——**不要只換一個**，
        只換 web 的話「web 分支其實也撈了 KB」這個誤報就看不見。"""
        n = {"kb": 0, "web": 0}
        saved = (ar._retrieve_chunks, ar._tavily_search)
        ar._retrieve_chunks = lambda q, **kw: (n.__setitem__("kb", n["kb"] + 1) or [])
        ar._tavily_search = lambda q, need="none": (
            n.__setitem__("web", n["web"] + 1) or "（stub）")
        try:
            dispatch(route, "某個子問題")
            return n["kb"], n["web"], None
        except Exception as e:                    # noqa: BLE001 - 要把例外當成結果之一
            return n["kb"], n["web"], e
        finally:
            ar._retrieve_chunks, ar._tavily_search = saved

    if callable(dispatch):
        kb_k, kb_w, kb_e = _run("kb")
        results.append(("⑪c route=kb → 撈 KB、**一次都不打 web**",
                        kb_e is None and kb_k >= 1 and kb_w == 0,
                        f"kb={kb_k} web={kb_w} err={kb_e!r}"))
        wb_k, wb_w, wb_e = _run("web")
        results.append(("⑪d route=web → 打 web、**不撈 KB**（這一刀才是修好新聞題的那一刀）",
                        wb_e is None and wb_w >= 1 and wb_k == 0,
                        f"kb={wb_k} web={wb_w} err={wb_e!r}"))
        bo_k, bo_w, bo_e = _run("both")
        results.append(("⑪e route=both → 兩個都做（並陳題：KB 有舊值、web 有新值）",
                        bo_e is None and bo_k >= 1 and bo_w >= 1,
                        f"kb={bo_k} web={bo_w} err={bo_e!r}"))
        bad = [_run(r) for r in ("", None, "news", "KB")]
        results.append(("⑪f **誤報對照**：非法 route 必須當場炸，不可靜默預設",
                        all(e is not None for _, _, e in bad),
                        "靜默預設 → Plan 打錯一個字,新聞題全退回拿財報硬答,"
                        "而外觀與「路由判成 kb」完全相同"))
        # ⑪g web 分支必須走既有的 _tavily_search（那裡面有白名單複核／去重／過時過濾／截斷）
        try:
            src = inspect.getsource(dispatch)
            calls = {n.func.id for n in ast.walk(ast.parse(src))
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            calls |= {n.func.attr for n in ast.walk(ast.parse(src))
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        except (OSError, SyntaxError):
            calls = set()
        results.append(("⑪g web 分支走既有 _tavily_search **與** _web_query_en",
                        "_tavily_search" in calls and "_web_query_en" in calls,
                        f"實得呼叫 {sorted(calls)}；直接叫 TavilyClient ＝白名單複核／地區子網域／"
                        "去重／過時過濾／截斷全部繞掉；少了 _web_query_en ＝ 送中文 query 給 "
                        "Tavily（變異測試 M5 逃脫過一次，就是因為這條只驗了前半）"))
    else:
        for tag in ("⑪c", "⑪d", "⑪e", "⑪f", "⑪g"):
            results.append((f"{tag} （_dispatch_todo 不存在,跳過）", False, "還沒實作"))

    # ── 升級規則：kb 走不下去時怎麼辦（純 Python，不是 LLM）──────────────────────
    esc = _has("_escalate_route")
    results.append(("⑪h ar._escalate_route 存在且可呼叫", callable(esc), "還沒實作（計畫 A5）"))
    if callable(esc):
        def _e(route, sufficient, unfixable):
            try:
                return esc(route, {"sufficient": sufficient, "kb_unfixable": unfixable})
            except Exception as ex:               # noqa: BLE001
                return f"ERR {ex!r}"
        results.append(("⑪i **誤報對照**：kb ＋ 不足但非 kb_unfixable → **仍然 kb**",
                        _e("kb", False, False) == "kb",
                        f"實得 {_e('kb', False, False)!r}；判成 both ＝ 退回現在的"
                        "「不足就上網」,改了個寂寞且加重 record 那半的過度路由"))
        # ⚠ **這條原本寫 both，被既有的閘門⑤當場推翻**（「KB 補不了 → 只檢索 1 次」實得 2）。
        #   `kb_unfixable` ＝ KB 結構上補不了，而 kb 那一半已經在池子裡（_merge_chunks 只加不減）
        #   → 再撈一次照定義不可能有用，只是把 2026-08-14「21 次檢索原地打轉」換個寫法請回來。
        #   **先寫的斷言不等於對的斷言**；這一格是舊閘門修正新規格的實例。
        results.append(("⑪j kb ＋ kb_unfixable → **web**（kb 那半已在池裡，再撈不可能有用）",
                        _e("kb", False, True) == "web", f"實得 {_e('kb', False, True)!r}"))
        results.append(("⑪k kb ＋ 已足夠 → 不升級",
                        _e("kb", True, False) == "kb", f"實得 {_e('kb', True, False)!r}"))
        results.append(("⑪l **誤報對照**：web／both 不得被降級回 kb",
                        _e("web", False, True) in ("web", "both")
                        and _e("both", True, False) == "both",
                        f"實得 web→{_e('web', False, True)!r} both→{_e('both', True, False)!r}"))
    else:
        for tag in ("⑪i", "⑪j", "⑪k", "⑪l"):
            results.append((f"{tag} （_escalate_route 不存在,跳過）", False, "還沒實作"))

    # ── Plan 輸出解析：新舊兩種格式都要吃得下 ────────────────────────────────────
    parse = _has("_parse_plan_output")
    results.append(("⑪m ar._parse_plan_output 存在且可呼叫", callable(parse),
                    "還沒實作（計畫 A2）"))
    if callable(parse):
        def _p(raw):
            try:
                return parse(raw)
            except Exception as ex:               # noqa: BLE001
                return f"ERR {ex!r}"
        old = _p(["A 的營收", "B 的新聞"])
        results.append(("⑪n **誤報對照**：舊格式 list[str] 的 route 預設必須是 kb",
                        isinstance(old, list) and len(old) == 2
                        and all(t.get("route") == "kb" for t in old)
                        and [t.get("task") for t in old] == ["A 的營收", "B 的新聞"],
                        f"實得 {old!r}；預設成 web/both 會讓**所有既有 fixture 重放**的行為"
                        "悄悄改變＝跨日基準不再可比"))
        new = _p([{"task": "X 的新聞", "route": "web"}])
        results.append(("⑪o 新格式 list[dict] 原樣保留 route",
                        isinstance(new, list) and new[:1] and new[0].get("route") == "web",
                        f"實得 {new!r}"))
        mixed = _p(["純字串", {"task": "帶路由", "route": "both"}])
        results.append(("⑪p 新舊混雜也要吃（LLM 不保證整批同格式）",
                        isinstance(mixed, list) and len(mixed) == 2
                        and mixed[0].get("route") == "kb" and mixed[1].get("route") == "both",
                        f"實得 {mixed!r}"))
        noroute = _p([{"task": "缺 route"}])
        results.append(("⑪q dict 缺 route → 補 **both**，且**不可 KeyError 讓整個 plan 掛掉**",
                        isinstance(noroute, list) and noroute[:1]
                        and noroute[0].get("route") == "both", f"實得 {noroute!r}"))
        illegal = _p([{"task": "亂填", "route": "news"}])
        results.append(("⑪r 非法 route 在**解析時**正規化成 both（不是原樣傳給 dispatch）",
                        isinstance(illegal, list) and illegal[:1]
                        and illegal[0].get("route") == "both",
                        f"實得 {illegal!r}；原樣傳下去會延後到 ⑪f 才炸,"
                        "一次 LLM 亂填就毀掉整個 query"))
        results.append(("⑪s 解析結果每一格的 route 都在 VALID_ROUTES 裡",
                        VALID is not None and all(
                            isinstance(r, list) and all(t.get("route") in set(VALID) for t in r)
                            for r in (old, new, mixed, noroute, illegal)
                            if isinstance(r, list)), ""))
    else:
        for tag in ("⑪n", "⑪o", "⑪p", "⑪q", "⑪r", "⑪s"):
            results.append((f"{tag} （_parse_plan_output 不存在,跳過）", False, "還沒實作"))

    # ── eval 隔離：**route 之後，隔離的單一實作點是 `_effective_route`** ────────────
    # ⚠ **這一組是變異測試逼出來的**：把 `_effective_route` 的隔離整段拿掉（snapshot 也照
    #   route 走 → eval 直接連網），**當時兩支閘門一條都沒響**。原因是閘門① 的 `_gate()` 是
    #   生產判斷式的**抄寫不是 import**（該處自己的 ⚠ 就寫著這件事），抄的還是加 route 之前
    #   的條件。所以隔離必須在**它現在真正住的地方**再被驗一次。
    eff = _has("_effective_route")
    results.append(("⑪t ar._effective_route 存在且可呼叫", callable(eff), "還沒實作"))
    if callable(eff):
        _saved_flag = ar.ENABLE_WEB_SEARCH
        try:
            ar.ENABLE_WEB_SEARCH = True
            snap = [eff(r, SNAP) for r in ("kb", "web", "both")]
            live = [eff(r, LIVE) for r in ("kb", "web", "both")]
            ar.ENABLE_WEB_SEARCH = False
            off = [eff(r, LIVE) for r in ("kb", "web", "both")]
        finally:
            ar.ENABLE_WEB_SEARCH = _saved_flag
        results.append(("⑪u **snapshot（eval 走的路）一律降級成 kb**，不論 route 是什麼",
                        snap == ["kb", "kb", "kb"],
                        f"實得 {snap}；不是 kb ＝ **eval 直接連網**，這是本檔存在的理由"))
        results.append(("⑪v ENABLE_WEB_SEARCH=False 時也一律 kb（兩個條件各自都足夠）",
                        off == ["kb", "kb", "kb"], f"實得 {off}"))
        results.append(("⑪w **誤報對照**：live ＋ web 開啟時必須原樣放行，不可一律 kb",
                        live == ["kb", "web", "both"],
                        f"實得 {live}；全 kb ＝ 隔離做過頭,live 時效能力整個失效"))

    # ⑪x/⑪y 端到端接線：純函式對不代表迴圈真的用了它（同 ⑮b→⑮e 的教訓）。
    if callable(dispatch):
        def _e2e(fm):
            n = {"kb": 0, "web": 0}
            saved = (ar._retrieve_chunks, ar._check_sufficiency,
                     ar._tavily_search, ar._fallback_local_summary, ar._web_query_en)
            ar._retrieve_chunks = lambda q, **kw: (n.__setitem__("kb", n["kb"] + 1) or [])
            ar._tavily_search = lambda q, need="none": (
                n.__setitem__("web", n["web"] + 1) or "（stub）")
            ar._web_query_en = lambda q: q
            ar._check_sufficiency = lambda *a, **k: {
                "sufficient": True, "missing": "", "new_query": "",
                "relevant_ids": [], "realtime_need": "none", "kb_unfixable": False}
            ar._fallback_local_summary = lambda t, c: "stub"
            try:
                ar._reset_run_pool()
                ar._reset_query_web_budget()
                ar._run_executor_deterministic("某個新聞子問題", "", fm, 0, False,
                                               attributable=True, route="web")
            finally:
                (ar._retrieve_chunks, ar._check_sufficiency, ar._tavily_search,
                 ar._fallback_local_summary, ar._web_query_en) = saved
            return n
        _snap_n, _live_n = _e2e(SNAP), _e2e(LIVE)
        results.append(("⑪x 接線：route=web 的 todo 在 **snapshot** 下 web 呼叫 0 次、改撈 KB",
                        _snap_n["web"] == 0 and _snap_n["kb"] >= 1, f"實得 {_snap_n}"))
        results.append(("⑪y **誤報對照**：同一個 todo 在 live 下必須真的打到 web",
                        _live_n["web"] >= 1, f"實得 {_live_n}；0 次 ＝ 隔離做過頭"))

    # ── web 預算用完時，`route=web` **不可以退回 KB**（2026-09-02）────────────────
    # ⚠ **這一組是 after 臂量出來的**：`news-10`／`news-13` 兩題沒有任何 replan todo，
    #   財報引用卻不是 0——成因是預算用完後把 route 降級成 kb，於是又去撈財報。
    #   `route=web` 的意思就是「KB 依設計沒有這種資料」，降級的結果是**拿財報回答新聞題**，
    #   而那正是這整個改動要治的病。`_unmet_realtime_gaps` 的 docstring 早就點名這個情境
    #   （「web 預算用完 → 答案回頭用財報 chunk 生成 → 零揭露」），只是那條路依賴
    #   `realtime_need` 這個 BACKLOG 記著會失手的 LLM 欄位。這裡改用**確定性**判準：
    #   「這個 todo 被路由到 web，而它一次 web 都沒拿到」本身就是缺口。
    if callable(dispatch):
        def _budget_probe(route, fm, budget_left):
            """把預算調到 `budget_left` 再跑一個 todo，回 (kb 次數, web 次數, gaps)。"""
            n = {"kb": 0, "web": 0}
            saved = (ar._retrieve_chunks, ar._check_sufficiency, ar._tavily_search,
                     ar._fallback_local_summary, ar._web_query_en, ar.QUERY_WEB_BUDGET)
            ar._retrieve_chunks = lambda q, **kw: (
                n.__setitem__("kb", n["kb"] + 1)
                or [{"source": "AAPL_10K_2025.html", "chunk_index": 0, "content": "stub",
                     "ticker": "AAPL", "raw_rerank_score": 0.9}])
            # ⚠ 樁的回傳**不可以用「（」開頭**：生產約定那是「查無結果」的標記，
            #   `_dispatch_todo` 會把它濾掉 → web_notes 空 → 缺口恆為真 → ⑪z4 誤報。
            #   （被 ⑪z4 當場抓出來的，正是 CLAUDE.md 說的「樁要長得像生產的產物」。）
            ar._tavily_search = lambda q, need="none": (
                n.__setitem__("web", n["web"] + 1)
                or "網路搜尋結果: - Stub（發布日 2026-09-01）[web: https://reuters.com/x]")
            ar._web_query_en = lambda q: q
            ar._check_sufficiency = lambda *a, **k: {
                "sufficient": True, "missing": "", "new_query": "",
                "relevant_ids": [], "realtime_need": "none", "kb_unfixable": False}
            ar._fallback_local_summary = lambda t, c: "stub"
            ar.QUERY_WEB_BUDGET = budget_left
            try:
                ar._reset_run_pool()
                ar._reset_query_web_budget()
                out = ar._run_one_todo(
                    {"id": 0, "task": "Apple 最近有什麼新聞", "temporal_scope": "",
                     "attributable": True, "route": route, "freshness_gaps": [],
                     "period_notes": [], "web_used": False, "status": "pending", "result": ""},
                    fm, False)
                return n, out.get("freshness_gaps") or []
            finally:
                (ar._retrieve_chunks, ar._check_sufficiency, ar._tavily_search,
                 ar._fallback_local_summary, ar._web_query_en,
                 ar.QUERY_WEB_BUDGET) = saved

        _n0, _g0 = _budget_probe("web", LIVE, 0)     # 預算用完
        results.append(("⑪z1 route=web ＋ 預算用完 → **一次 KB 都不撈**（不可退回財報）",
                        _n0["kb"] == 0 and _n0["web"] == 0,
                        f"實得 {_n0}；撈了 KB ＝ 拿財報回答新聞題,正是本次改動要治的病"))
        results.append(("⑪z2 同上，必須留下時效缺口（否則就是靜默降級）",
                        len(_g0) >= 1, f"實得 gaps={_g0}"))
        _n1, _g1 = _budget_probe("both", LIVE, 0)
        results.append(("⑪z3 **誤報對照**：route=both ＋ 預算用完 → KB 那半照撈",
                        _n1["kb"] >= 1 and _n1["web"] == 0,
                        f"實得 {_n1}；both 的 KB 半本來就該撈,一起擋掉是矯枉過正"))
        _n2, _g2 = _budget_probe("web", LIVE, 3)     # 有預算
        results.append(("⑪z4 **誤報對照**：route=web ＋ 有預算且拿到 web → **不得**留缺口",
                        _n2["web"] >= 1 and not _g2,
                        f"實得 {_n2} gaps={_g2}；恆留缺口 ＝ 警語永遠印,這條斷言沒有判別力"))
        _n3, _g3 = _budget_probe("web", SNAP, 0)     # eval
        results.append(("⑪z5 **誤報對照**：snapshot 下（已降級成 kb）不得憑空多出缺口",
                        not _g3, f"實得 gaps={_g3}；eval 基準會因此位移"))

    print()
    print(f"  {'路由欄位與確定性分派（先寫斷言、後改碼）':<52}{'判定':>8}")
    print("  " + "-" * 68)
    fail = 0
    for name, ok, note in results:
        fail += 0 if ok else 1
        print(f"  {name:<52}{('OK' if ok else 'FAIL'):>8}  {'' if ok else note}")
    return fail


def _check_content_date_extraction() -> int:
    """閘門⑩：web 結果的日期要抽得到，**而且不可以抽錯成太新的**（2026-08-29）。

    測資是 **2026-09-01 一次真 Tavily 呼叫的逐字內容**（`What is NVIDIA's current share price?`）。
    那次呼叫的發現：**12 則結果的 `published_date` 全部是 None**、網址是 `/quote/NVDA` 也推不出
    日期 → 每一則都印「（未標示日期）」，而內容裡明明寫著 `REAL TIME 11:49 AM EDT 08/31/26`。
    過時過濾（`_dedupe_web_results` 第③道）因此**整個失效**。

    ⚠ **危險方向是不對稱的**：抽到**太新**的日期會讓過期頁冒充新鮮並替整池背書；抽不到只是
      維持現狀。所以誤報對照（⑩e~⑩i）比陽性斷言重要——同一頁裡有分析師評等日、**未來的**
      財報日與除息日、歷史表格列，全都不是發布日。
    """
    AS_OF = date(2026, 9, 1)

    # ── 逐字凍結：2026-09-01 真實回應 ────────────────────────────────────────
    YAHOO_QUOTE = ("Selected editionUS English 220.78 +3.23 (+1.48%) At close: August 31 at "
                   "4:00:01 PM EDT 220.16 -0.62 (-0.28%) Overnight: 9:53:29 PM EDT")
    WSJ = ("NVIDIA Corp.NVDA (U.S.: Nasdaq) AT CLOSE 4:00 PM EDT 08/28/26 217.55USD "
           "-10.43-4.57% Volume195,116,417 ...Read more")
    YAHOO_STATS = ("NasdaqGS - Nasdaq Real Time Price USD # NVIDIA Corporation (NVDA) "
                   "217.55 -10.43 (-4.57%) At close: August 28 at 4:00:00 PM EDT "
                   "217.89 +0.34 (+0.16%) After hours: August 28 at 7:59:59 PM EDT")
    YAHOO_HIST = ("NasdaqGS - Nasdaq Real Time Price USD # NVIDIA Corporation (NVDA) "
                  "219.74 -5.27 (-2.34%) At close: August 18 at 4:00:00 PM EDT "
                  "221.20 +1.46 (+0.66%) Pre-Market: 9:06:53 AM EDT [...] | Dec 8, 2025 |")
    ANALYST = ("## Analyst Insights ### Analyst Price Targets 180.00 323.42 Average 220.78 "
               "Latest Rating Date 8/25/2026 Analyst Raymond James Rating Action Maintains "
               "Earnings Date Nov 17, 2026 Ex-Dividend Date Sep 10, 2026")
    OLD_TABLE = ("| Date | Open | High | Low | Close | Adj Close | Volume | "
                 "| Dec 1, 2018 | 4.32 | 4.37 | 3.11 | 3.34 | 3.31")

    cases = [
        ("⑩a yahoo 報價頁『At close: August 31』（省略年份）", YAHOO_QUOTE, date(2026, 8, 31)),
        ("⑩b wsj『AT CLOSE 4:00 PM EDT 08/28/26』（兩位數年）", WSJ, date(2026, 8, 28)),
        ("⑩c 同頁多個標記 → 取最新（After hours 也是 8/28）", YAHOO_STATS, date(2026, 8, 28)),
        ("⑩d 標記日期 8/18 勝過表格裡的 Dec 8, 2025（後者無標記）", YAHOO_HIST, date(2026, 8, 18)),
        ("⑩e 誤報對照：評等日／**未來的**財報日除息日 → 一個都不可抽", ANALYST, None),
        ("⑩f 誤報對照：歷史表格列（無標記）→ 不可抽", OLD_TABLE, None),
        ("⑩g 誤報對照：完全沒有標記 → None（維持加這層之前的行為）",
         "NVDA 219.52 Previous Close 217.55 Volume 195,116,417", None),
        ("⑩h 誤報對照：標記旁邊就是未來日期 → 仍然丟棄",
         "As of Nov 17, 2026 the company will report earnings.", None),
        ("⑩i 省略年份且落在未來 → 補成去年同日，**絕不外推到未來**",
         "At close: December 30 at 4:00:00 PM EDT 219.52", date(2025, 12, 30)),
        # ⑩m 新聞頁的形狀：Published 早於 Updated → 必須取 Updated（max 不是 min）。
        # 沒有這一條，一個「取最舊」的實作在 ⑩a~⑩i 上會全綠——那會讓改過的新聞被當成舊的丟掉。
        ("⑩m 新聞頁 Published/Updated 並存 → 取較新的那個",
         "Published on Aug 20, 2026 by staff. Last updated Aug 30, 2026 with new details.",
         date(2026, 8, 30)),
        # ⑩n **危險方向**：沒有日期的標記（`Pre-Market:` 後面只有時間）不可以收編隔壁表格裡
        #    **比較新**的日期。這一條是變異測試逼出來的——原本 48 字元的窗會讓它收編，而
        #    ⑩d 之所以還是綠的只是因為誤收的那個剛好比較舊、被 max 蓋過去（僥倖不是保證）。
        ("⑩n 危險方向：無日期的標記不可收編隔壁表格中**更新**的日期",
         "At close: August 18 at 4:00:00 PM EDT 221.20 Pre-Market: 9:06:53 AM EDT "
         "| Aug 25, 2026 | 230.00 |", date(2026, 8, 18)),
        # ⑩o 沒有區段邊界可截時，窗口是唯一的護欄：標記後面隔著一整段散文的日期不算它的。
        # ⚠ 這一條測的是「窗口存在」，**不是** 48 這個數值——48 vs 24 沒有任何斷言分得出來
        #   （變異測試實測），所以那個數值是判斷不是量出來的，不要當成有證據支持。
        ("⑩o 沒有邊界可截時，隔著一整段散文的日期不算這個標記的",
         "As of the date of this report the company has continued to expand its "
         "operations across several regions and on Aug 25, 2026 announced a new plan.",
         None),
    ]
    results: list[tuple[str, bool, str]] = []
    for name, content, want in cases:
        got = ar._content_published_date(content, AS_OF)
        results.append((name, got == want, f"預期 {want} 實得 {got}"))

    # ── ⑩j/⑩k 接線：光抽得到不夠，過時過濾要真的用得到它（值測試）────────────
    def _kept(need):
        rs = [{"url": "https://finance.yahoo.com/quote/NVDA/history", "title": "t",
               "content": YAHOO_HIST, "published_date": None}]
        kept, stats = ar._dedupe_web_results(rs, need, AS_OF)
        return kept, stats

    kept_i, stats_i = _kept("intraday")     # limit=7 天，8/18 距 9/1 是 14 天 → 該被濾掉
    kept_d, stats_d = _kept("days")         # limit=180 天 → 不該被濾掉
    results.append(("⑩j 接線：intraday 下 8/18 的頁被過時濾掉（14 天 > 7）",
                    kept_i == [] and stats_i["stale"] == 1,
                    f"實得 kept={len(kept_i)} stale={stats_i['stale']}（抽得到但沒接上＝白做）"))
    results.append(("⑩k 誤報對照：同一則在 need=days（180 天）不可被濾掉",
                    len(kept_d) == 1 and stats_d["stale"] == 0,
                    f"實得 kept={len(kept_d)} stale={stats_d['stale']}（濾過頭＝新聞題沒東西可用）"))
    results.append(("⑩l 接線：_pub_date 真的被寫進結果（prompt 的『發布日』靠它）",
                    bool(kept_d) and kept_d[0].get("_pub_date") == date(2026, 8, 18),
                    f"實得 {kept_d[0].get('_pub_date') if kept_d else None}"))

    print()
    print(f"  {'web 結果的日期抽取（危險方向＝抽到太新的）':<52}{'判定':>8}")
    print("  " + "-" * 68)
    fail = 0
    for name, ok, note in results:
        fail += 0 if ok else 1
        print(f"  {name:<52}{('OK' if ok else 'FAIL'):>8}  {'' if ok else note}")
    return fail


def _check_replay_readonly() -> int:
    """閘門⑫：唯讀模式真的不寫，而且 miss 不准隱形（2026-09-02）。

    **守的是什麼**：`llm_replay` 的 `atexit` 無條件回寫 → A/B 共用一份 fixture 時，
    **先跑那臂的 miss 會變成後跑那臂的 hit**（實測 hit 23→32，兩臂不可比）。這條污染是
    **單向**的：永遠偏袒後跑的那一方，而且兩臂外觀都「掛了 fixture」，看不出異常。

    ⚠ **判別力集中在三條，其餘陽性一個「唯讀就整個 no-op」的實作也會通過**：
      · **⑫e**（單向污染的重現＋消失）——先證明非唯讀真的會污染，再證明唯讀讓它消失。
        少了前半，「唯讀下 armB miss」可能只是這個測試沒建立污染條件。
      · **⑫i** 唯讀時**檔案裡已有的** key 照樣 hit——擋掉「唯讀 ＝ 把模組關掉」的實作，
        那樣 A/B 兩臂會一起失去 fixture，比污染更糟且更難察覺。
      · **⑫f** 唯讀時 hit/miss 照樣印出來——非唯讀唯一的出口是 `_flush()` 那行
        `[replay] wrote`，唯讀不寫檔就沒有那行 → **miss 完全隱形**，外觀與「系統沒走那條路」相同。
    """
    import contextlib
    import tempfile
    import llm_replay as _lr

    results: list[tuple[str, bool, str]] = []
    _saved_env = {k: os.environ.get(k) for k in
                  ("RAG_REPLAY_CACHE", "RAG_REPLAY_READONLY", "RAG_REPLAY_MODE")}
    _saved_globals = (_lr._CACHE, _lr._PATH, _lr._DIRTY, dict(_lr._STATS))

    def _reset(path, *, ro, mode=None):
        """把模組狀態歸零並重設 env。**必須清 `_CACHE`**：它是 process 層快取，
        不清的話第二個情境讀到的是第一個情境的 dict，測到的東西就不是磁碟上的事實。"""
        os.environ["RAG_REPLAY_CACHE"] = str(path)
        os.environ.pop("RAG_REPLAY_READONLY", None)
        os.environ.pop("RAG_REPLAY_MODE", None)
        if ro:
            os.environ["RAG_REPLAY_READONLY"] = "1"
        if mode:
            os.environ["RAG_REPLAY_MODE"] = mode
        _lr._CACHE, _lr._PATH, _lr._DIRTY = None, None, False
        _lr._STATS.update({"hit": 0, "miss": 0, "skipped_write": 0})

    try:
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "rc.json"

            # ── ⑫a 預設（未設 READONLY）＝既有三個流程依賴的行為，必須逐字不變 ──────
            _reset(fp, ro=False)
            _lr.put("plan", "k1", ["a"])
            _lr._flush()
            wrote = fp.exists() and json.loads(fp.read_text(encoding="utf-8")) == {"plan": {"k1": ["a"]}}
            results.append(("⑫a 預設仍然回寫（record_web_fixture／兩支 period probe 靠它）",
                            wrote, f"檔案={fp.exists()}"))

            _before = fp.read_bytes()

            # ── ⑫b/⑫c/⑫d 唯讀：put 不進 cache、不設 _DIRTY、磁碟逐字不變 ─────────
            _reset(fp, ro=True)
            _lr.put("plan", "k2", ["b"])
            results.append(("⑫b 唯讀時 put 之後 get 仍然 miss（沒進 cache）",
                            _lr.get("plan", "k2") is _lr.MISS, "put 竟然生效了"))
            results.append(("⑫c 唯讀時 `_DIRTY` 保持 False（守入口不是守出口）",
                            _lr._DIRTY is False, f"_DIRTY={_lr._DIRTY}"))
            _lr._flush()
            results.append(("⑫d 唯讀時磁碟內容逐字不變", fp.read_bytes() == _before,
                            "檔案被改了"))

            # ── ⑫i 唯讀**不是**把模組關掉：既有的 key 照樣 hit ────────────────────
            results.append(("⑫i 唯讀時檔案裡已有的 key 照樣 hit（不是整個 no-op）",
                            _lr.get("plan", "k1") == ["a"], f"實得 {_lr.get('plan', 'k1')!r}"))

            # ── ⑫f miss 不准隱形 ─────────────────────────────────────────────────
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _lr._flush()
            out = buf.getvalue()
            results.append(("⑫f 唯讀時 `_flush` 照樣印出 hit/miss（否則 miss 完全隱形）",
                            "read-only" in out and "miss=" in out, f"實得 {out.strip()!r}"))

            # ── ⑫e **單向污染**：先重現，再證明唯讀讓它消失 ──────────────────────
            #    兩臂共用同一份 fixture，armA 先跑（miss → 算出結果 → put），armB 後跑。
            def _two_arms(readonly: bool) -> bool:
                fp2 = Path(td) / f"ab_{int(readonly)}.json"
                fp2.write_text("{}", encoding="utf-8")
                _reset(fp2, ro=readonly)
                if _lr.get("plan", "shared") is _lr.MISS:      # armA miss → 現場算 → 回寫
                    _lr.put("plan", "shared", ["armA 算出來的"])
                _lr._flush()
                _reset(fp2, ro=readonly)                        # armB：新 process 的模擬
                return _lr.get("plan", "shared") is not _lr.MISS

            polluted = _two_arms(readonly=False)
            clean = _two_arms(readonly=True)
            results.append(("⑫e1 **先重現**：非唯讀下 armA 的 miss 變成 armB 的 hit",
                            polluted is True, "污染沒重現 → 這個測試沒有前提"))
            results.append(("⑫e2 唯讀下同一個 key 在 armB 仍然 miss（污染消失）",
                            clean is False, "唯讀沒擋住回寫"))

            # ── ⑫g readonly() 每次重讀 env（不可快取成模組層常數）────────────────
            _reset(fp, ro=False)
            was = _lr.readonly()
            os.environ["RAG_REPLAY_READONLY"] = "1"
            now = _lr.readonly()
            results.append(("⑫g `readonly()` 每次重讀 env（不是 import 時定死）",
                            was is False and now is True, f"{was} → {now}"))

            # ── ⑫h 與 strict 正交：唯讀不得讓 strict 靜默失效 ────────────────────
            _reset(fp, ro=True, mode="strict:plan")
            try:
                _lr.get("plan", "不存在的 key")
                got = None
            except _lr.ReplayCacheMiss:
                got = _lr.ReplayCacheMiss
            results.append(("⑫h 唯讀 ＋ strict 正交：miss 照樣拋 ReplayCacheMiss",
                            got is _lr.ReplayCacheMiss, f"實得 {got}"))
    finally:
        for k, v in _saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        _lr._CACHE, _lr._PATH, _lr._DIRTY = _saved_globals[:3]
        _lr._STATS.update(_saved_globals[3])

    print()
    print(f"  {'replay 唯讀模式（A/B 單向污染）':<52}{'判定':>8}")
    print("  " + "-" * 68)
    fail = 0
    for name, ok, note in results:
        fail += 0 if ok else 1
        print(f"  {name:<52}{('OK' if ok else 'FAIL'):>8}  {'' if ok else note}")
    return fail


def _check_monkeypatch_reaches_callers() -> int:
    """閘門⑬：套件化之後，打在套件上的 monkeypatch 仍然攔得住呼叫端（2026-09-03）。

    **守的是什麼**：`agentic_rag_v2.py`（單一 4549 行檔）拆成 `agentic_rag_version/` 套件時，
    最危險的失效**不會有任何外觀差異**。eval 全靠 `ar.<name> = stub` 攔截，而那是在**套件物件**
    上改綁定；一旦呼叫端寫成 `from .webtools import _tavily_search`，那個名字就綁死在
    呼叫端模組的 globals 裡，**stub 再也蓋不到它** → `verify_web_gate_isolation` 保證的
    「eval 絕不連網」會**真的連網**，而閘門本身照樣全綠（它只數自己那個 stub 被叫幾次）。

    同樣的病也吃常數：`ENABLE_WEB_SEARCH` / `QUERY_WEB_BUDGET` 被 eval 直接改寫，
    子模組若各自 `from .config import ENABLE_WEB_SEARCH`，改的就是另一份。

    ⚠ **判別力全在 ⑬e，其餘四條今天是空跑**：套件目前只有 `__init__`，沒有子模組，
      所以 ⑬b~⑬d 是**真空成立**（vacuously true）——它們現在回報 OK **不代表有判別力**，
      只代表還沒有東西可以違反。⑬e 拿一個**故意造出來的違規**餵同一套檢查，證明它抓得到；
      沒有 ⑬e，這道閘門就是這個專案犯過六次的「量尺與被測物耦合」的第七次。

    ⚠ **⑬a 刻意從 eval 腳本反推而不是只讀凍結清單**：漏掉一個 patch 站點的失敗方式，
      與「那個站點本來就不存在」外觀相同。所以凍結清單少於實際 patch 的名字時要當場炸。
    """
    import ast as _ast
    import pkgutil as _pkgutil
    import re as _re
    import sys as _sys
    from pathlib import Path as _P

    results: list[tuple[str, bool, str]] = []

    def _ck(name: str, ok: bool, note: str = "") -> None:
        results.append((name, bool(ok), note))

    # ── ⑬a 凍結清單 vs eval 實際 patch 的名字 ──────────────────────────────────
    FROZEN = {
        "ENABLE_WEB_SEARCH", "QUERY_WEB_BUDGET", "_build_temporal_contract",
        "_check_sufficiency", "_fallback_local_summary", "_get_kb_coverage",
        "_get_models", "_retrieve_chunks", "_run_executor", "_run_one_todo",
        "_tavily_search", "_web_query_en", "_write_final_answer",
    }
    _assign = _re.compile(r"\b_?ar\.([A-Za-z_][A-Za-z_0-9]*)\s*=(?!=)")
    found: set[str] = set()
    for p in sorted(_P(__file__).parent.glob("*.py")):
        found |= set(_assign.findall(p.read_text(encoding="utf-8")))
    missing = found - FROZEN
    _ck("⑬a 凍結清單涵蓋 eval 實際 patch 的每一個名字", not missing,
        f"清單漏了 {sorted(missing)}——拆分時不會被保護")

    pkg = _sys.modules[ar.__name__]
    submods = []
    if hasattr(pkg, "__path__"):
        for mi in _pkgutil.iter_modules(list(pkg.__path__)):
            m = _sys.modules.get(f"{ar.__name__}.{mi.name}")
            if m is not None and getattr(m, "__file__", None):
                submods.append(m)

    # ── ⑬b 沒有任何子模組把被 patch 的名字綁成自己的 module-level global ──────────
    def _shadowers(names: set[str], mods) -> list[str]:
        bad = []
        for m in mods:
            for n in names:
                if n in vars(m):
                    bad.append(f"{m.__name__}.{n}")
        return bad

    shadow = _shadowers(FROZEN, submods)
    _ck(f"⑬b 子模組（{len(submods)} 個）都沒有遮蔽被 patch 的名字", not shadow,
        f"遮蔽：{shadow[:4]}")

    # ── ⑬c 子模組裡沒有「直接呼叫全域名字」的呼叫點（必須走套件物件）───────────────
    def _direct_calls(names: set[str], mods) -> list[str]:
        bad = []
        for m in mods:
            try:
                tree = _ast.parse(_P(m.__file__).read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in _ast.walk(tree):
                if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)
                        and node.func.id in names):
                    bad.append(f"{m.__name__}:{node.lineno} {node.func.id}()")
        return bad

    direct = _direct_calls(FROZEN, submods)
    _ck("⑬c 子模組沒有直接呼叫被 patch 的名字（要走套件物件）", not direct,
        f"直呼：{direct[:4]}")

    # ── ⑬d 動態版：真的 patch 下去，沒有任何子模組還握著舊物件 ───────────────────
    sentinel = object()
    victim = "_tavily_search"
    saved = getattr(ar, victim)
    try:
        setattr(ar, victim, sentinel)
        stale = [f"{m.__name__}.{victim}" for m in submods
                 if victim in vars(m) and vars(m)[victim] is not sentinel]
    finally:
        setattr(ar, victim, saved)
    _ck(f"⑬d patch `ar.{victim}` 之後沒有子模組握著舊物件", not stale, f"舊物件：{stale}")

    # ── ⑬e 誤報對照：故意造一個違規的假子模組，上面三條必須抓到 ────────────────────
    #   ⚠ 這是本道**唯一**有判別力的斷言（⑬b~⑬d 在還沒有子模組時是真空成立）。
    import types as _types
    fake = _types.ModuleType(f"{ar.__name__}._fake_violation")
    fake.__file__ = str(_P(__file__).parent / "_fake_violation_probe.py")
    fake._tavily_search = saved                     # ← 遮蔽：from .webtools import _tavily_search
    fake.ENABLE_WEB_SEARCH = True                   # ← 常數也一樣會被遮蔽
    caught_b = _shadowers(FROZEN, [fake])
    _ck("⑬e1 誤報對照：遮蔽了就必須被 ⑬b 抓到", len(caught_b) == 2, f"只抓到 {caught_b}")

    src = ("def go(q):\n"
           "    _tavily_search(q)\n"
           "    return ENABLE_WEB_SEARCH\n")
    probe = _P(fake.__file__)
    try:
        probe.write_text(src, encoding="utf-8")
        caught_c = _direct_calls(FROZEN, [fake])
        _ck("⑬e2 誤報對照：直接呼叫全域名字必須被 ⑬c 抓到", len(caught_c) == 1,
            f"抓到 {caught_c}")
    finally:
        probe.unlink(missing_ok=True)

    setattr(ar, victim, sentinel)
    try:
        caught_d = [f"{fake.__name__}.{victim}"] if vars(fake)[victim] is not sentinel else []
    finally:
        setattr(ar, victim, saved)
    _ck("⑬e3 誤報對照：握著舊物件必須被 ⑬d 抓到", len(caught_d) == 1, f"抓到 {caught_d}")

    print()
    print(f"  {'套件化之後 monkeypatch 仍攔得住':<52}{'判定':>8}")
    print("  " + "-" * 68)
    fail = 0
    for name, ok, note in results:
        fail += 0 if ok else 1
        print(f"  {name:<52}{('OK' if ok else 'FAIL'):>8}  {'' if ok else note}")
    return fail


def main() -> int:
    print(f"  {'情境':<24}{'Q1':>6}{'Q2':>6}{'Q3':>6}{'Q4':>6}{'呼叫':>6}   判定")
    print("  " + "-" * 68)
    fail = 0
    for name, freshness, web_enabled, expect in SCENARIOS:
        _calls.clear()
        cells = []
        for q in QUERIES.values():
            fired = _gate(freshness, web_enabled, False, q)   # 最壞情況：Grader 全判不足
            if fired:
                ar._tavily_search(q)
            cells.append("呼叫" if fired else "—")
        n = len(_calls)
        if expect is None:
            ok, note = n > 0, "生產打得到" if n > 0 else "生產打不到 → 時效能力失效"
        else:
            ok, note = n == expect, "eval 隔離成立" if n == expect else "**eval 漏出網路**"
        fail += 0 if ok else 1
        print(f"  {name:<24}" + "".join(f"{c:>6}" for c in cells)
              + f"{n:>6}   {'OK  ' if ok else 'FAIL'} {note}")

    print("  " + "-" * 68)
    fail += _check_regeneration_keeps_web()
    fail += _check_system_prompt_amendment()
    fail += _check_domain_allowlist()
    fail += _check_recency_gate()
    fail += _check_web_result_hygiene()
    fail += _check_replay_miss_is_loud()
    fail += _check_replan_is_replayed()
    fail += _check_fixture_binding_is_preserved()
    fail += _check_content_date_extraction()
    fail += _check_route_dispatch()
    fail += _check_replay_readonly()
    fail += _check_monkeypatch_reaches_callers()
    print()
    print("GATE:", "PASS" if fail == 0 else f"FAIL（{fail} 項不符）")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
