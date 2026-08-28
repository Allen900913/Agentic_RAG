"""驗收 web_search 的兩件事：**eval 隔離**與**資料真的進得了答案**。零 LLM、零網路、零 Qdrant，秒級。

閘門① eval 隔離：生產打得到 web、eval 一次都不漏（見下方 SCENARIOS）。
閘門② 重生成保留：`web_extra` 必須跟著每一次 validator 重寫走，且 reflect 的稽核來源要含它。
      2026-08-13 實測：接管線之前，Tavily 三次都帶回 `$5.27T`，最終答案三次都退回 6 月快照的
      `$4,962.16B`——因為重生成時 `extra_user` 被換成糾錯指示，web 區塊整段消失。
閘門③ 生成契約：**無 web 時 system message 逐字不變**（＝eval 基準不被動到的證明），
      有 web 時修訂條款與 as-of 日期真的進得去。
閘門④ 來源白名單：`include_domains` 真的傳給 Tavily，且濾空時不靜默退回全網。
閘門⑤ Grader 時效判準：snapshot 的 Checker prompt 逐字不變；日期算術真值表（零 LLM）。


**這支在守什麼**：`agentic_rag_v2._run_executor_deterministic` 裡那個 web 補救判斷式，
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
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import agentic_rag_v2 as ar  # noqa: E402

LIVE = ar.FRESHNESS_LIVE
SNAP = ar.FRESHNESS_SNAPSHOT

_calls: list[str] = []
# ⚠ 閘門① 把 `_tavily_search` 整個換成 stub（絕不連網）；閘門④ 要測的正是真身，
# 所以**先留一份原始參考**。少了這行，閘門④ 會量到 stub 而全數誤報 FAIL（2026-08-13 實際踩到）。
_REAL_TAVILY_SEARCH = ar._tavily_search
ar._tavily_search = lambda q: _calls.append(q) or "（stub，絕不連網）"


def _gate(freshness: str, web_enabled: bool, sufficient: bool, task: str) -> bool:
    """必須與 `agentic_rag_v2._run_executor_deterministic` 的 web 補救判斷式**逐字一致**。

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
    print()
    print("GATE:", "PASS" if fail == 0 else f"FAIL（{fail} 項不符）")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
