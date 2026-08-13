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
    results = [
        ("白名單有傳給 Tavily", allow == ar.WEB_ALLOWED_DOMAINS and len(allow) > 0),
        ("濾空不退回全網", "未退回全網" in out),
        ("清單含原始揭露方", "sec.gov" in allow),
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
    Y = [{"source": "NVDA_10K_2026.html"}]                  # 財年 → 2026-12-31，永不過期
    cases = [
        ("即時市值 vs 兩個月前基本面", "intraday", F, True),
        ("今天股價 vs 三週前新聞", "intraday", N, True),
        ("最近進展 vs 三週前新聞", "days", N, True),
        ("季度數字 vs 兩個月前基本面", "none", F, False),      # none → 永不過期
        ("即時 vs 10-K（財年期間非發布日）", "intraday", Y, False),
        ("最近消息 vs 10-Q 期間", "days", K, True),
        ("來源算不出日期 → 不主張過期", "intraday", [{"source": "weird.txt"}], False),
        ("無候選 → 不主張過期", "intraday", [], False),
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
    print()
    print("GATE:", "PASS" if fail == 0 else f"FAIL（{fail} 項不符）")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
