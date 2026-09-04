"""verify_answer_validators.py — Synthesize 端確定性 validator 的驗收（零 LLM、零網路，只讀 Qdrant coverage）。

**這支存在的理由**：2026-08-15 加的三件事都是「機制宣稱」，照 CLAUDE.md 的規矩，宣稱要先有能
證偽它的確定性測試，否則就是又一個「聽起來合理」的假設。

  ① `find_stale_period_claims`：答案自稱「最新一季」卻引用了較舊的期別 → 抓出來重生成。
     病灶是 web-03：「Microsoft 最新一季的 Azure 營收成長率」答 40%【MSFT_10Q_202603】,
     而 `MSFT_10K_2026 #129`（Azure grew 41%）就在同一份證據集合裡沒被用。
  ② `_news_freshness_gaps`：時效警語改由「這次實際用了誰的新聞」決定,不再由兩個硬編碼詞表
     （`_RELATIVE_TIME_RE` × `looks_like_news_query`）猜問法。
  ③ `find_untraceable_numbers`：答案裡「不可能被換算」形態的數字必須溯得回來源。
     ⚠ **這一項沒有實測正例**——它原本要封的 `web-02` 後來查明是量尺誤報。留著的理由是結構性的
     （reflect 自己的重生成之後只剩 citation validator），所以閘門⑦ 的陽性全是**變異注入**,
     而真正有價值的是那一組**誤報對照**：100 題乾跑誤報 0 題，靠的就是那兩條排除規則。

⚠ 檔名 2026-08-15 從 `verify_period_validator`（舊名）改過來：加了第三種 validator 之後舊名已不符實。

⚠ **每一道都要有陰性對照**。只證明「該抓的抓得到」等於沒證明——還要證明「不該抓的不會抓」,
  否則一個永遠回 True 的函式也能全綠。閘門③④ 就是那些陰性對照,少了它們這支沒有判別力。

⚠ **陽性案例逐字凍結在本檔（`_WEB03_PRE_FIX_ANSWER`）,不讀 `experiments/`**。第一版是去讀
  結果檔的——那份會被修好,一修好陽性就消失,等於閘門隨著修法生效而自己失去判別力。
  「現況是乾淨的」是相反方向的斷言,由閘門⑥ 另外負責。

跑法：
    .venv/Scripts/python.exe -u eval/verify_answer_validators.py
"""
from __future__ import annotations

import inspect
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 本檔輸出含 ↔／⚠／全形，Windows 預設 cp950 會在 gate8 當場炸

import agentic_rag_version as ar   # noqa: E402

_PASS = _FAIL = 0
_FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {label}")
    else:
        _FAIL += 1
        _FAILURES.append(label)
        print(f"  [FAIL] {label}" + (f"\n         └ {detail}" if detail else ""))


def _chunks(*sources: str) -> list[dict]:
    """把 source 清單做成 validator 吃的 chunk dict（只需要 source/ticker/chunk_index）。"""
    return [{"source": s, "chunk_index": i, "ticker": s.split("_")[0]}
            for i, s in enumerate(sources)]


def _cite(*sources: str) -> str:
    return " ".join(f"【{s}, chunk #{i}】" for i, s in enumerate(sources))


# ══════════════════════════════════════════════════════════════════════════════
def gate1_fiscal_rank() -> None:
    """① 期別排序真值表。**跨 10-K/10-Q 的排序是整套機制的地基**,錯了下游全錯。"""
    print("\n① _fiscal_rank：(fiscal_year, 期別序) 的排序")
    r = ar._fiscal_rank
    msft_k, msft_q3, msft_q2 = r("MSFT_10K_2026.html"), r("MSFT_10Q_202603.html"), r("MSFT_10Q_202512.html")
    nvda_k, nvda_q = r("NVDA_10K_2026.html"), r("NVDA_10Q_202604.html")
    aapl_k, aapl_q = r("AAPL_10K_2025.html"), r("AAPL_10Q_202606.html")

    _assert("所有受測 filing 都解得出 rank",
            all(x is not None for x in (msft_k, msft_q3, msft_q2, nvda_k, nvda_q, aapl_k, aapl_q)),
            f"msft_k={msft_k} msft_q3={msft_q3} nvda_k={nvda_k} nvda_q={nvda_q}")
    _assert("FY 排在同財年的 Q3 之後（10-K 涵蓋整個財年）", msft_k > msft_q3, f"{msft_k} vs {msft_q3}")
    _assert("同財年 Q3 > Q2", msft_q3 > msft_q2, f"{msft_q3} vs {msft_q2}")
    # ⚠ 這條是整支最重要的陰性對照：`_source_newest_date()` 把 10-K 一律算成該年 12/31,
    #   NVDA 財年 1 月底就結束 → 用它排序會把 10K_2026 判得比真正更新的 10Q(FY2027 Q1) 還新。
    #   換句話說,**如果哪天有人「順手」把 _fiscal_rank 換回 _source_newest_date,這條會 FAIL**。
    _assert("NVDA：10-K FY2026 < 10-Q FY2027 Q1（財年非曆年的反轉陷阱）",
            nvda_k < nvda_q, f"10K={nvda_k} 10Q={nvda_q}")
    _assert("AAPL：10-K FY2025 < 10-Q FY2026 Q3", aapl_k < aapl_q, f"{aapl_k} vs {aapl_q}")
    _assert("非財報檔回 None（不參與比較）",
            r("AAPL_News_20260612_04.txt") is None and r("AAPL_Fundamentals_20260612.txt") is None)
    _assert("不存在的 source 回 None（不炸、不誤報）", r("NOPE_10K_2099.html") is None)


# web-03 **修好之前**的真實答案，逐字凍結在這裡。
# ⚠ 陽性案例**不能讀 `experiments/web_replay_answers.json`**：那份會被修好,一修好陽性就消失,
#   等於這道閘門會隨著修法生效而自己失去判別力（第一版就是這樣寫的）。凍結才測得到「這個病
#   如果復發,抓不抓得到」。回歸（現況必須是乾淨的）另外由閘門⑥ 負責,兩者方向相反、缺一不可。
_WEB03_PRE_FIX_ANSWER = (
    "Microsoft 在最新一季（截至 2026 年 3 月 31 日的三個月）Azure 及其他雲端服務營收成長率為 "
    "**40%**【MSFT_10Q_202603.html, chunk #69】。\n"
    "此 40% 的增幅同樣在該季的概覽中被指出，說明 Azure 與其他雲端服務收入較去年同期提升了 "
    "40%【MSFT_10Q_202603.html, chunk #49】。"
)
_WEB03_PRE_FIX_SOURCES = ("MSFT_10Q_202603.html", "MSFT_10K_2026.html", "MSFT_10Q_202512.html")


def gate2_stale_period_positive() -> None:
    """② 陽性：這個病復發時抓不抓得到。"""
    print("\n② find_stale_period_claims：陽性（凍結的 web-03 病灶）")
    probs = ar.find_stale_period_claims(_WEB03_PRE_FIX_ANSWER, _chunks(*_WEB03_PRE_FIX_SOURCES))
    _assert("web-03 修好前的真實答案：抓到 MSFT 引了 10-Q 而非更新的 10-K",
            len(probs) == 1 and "MSFT" in probs[0] and "MSFT_10K_2026" in probs[0],
            f"probs={probs}")

    # 構造版：同樣的形狀必須抓到。
    ans = "Microsoft 最新一季 Azure 成長率為 40%" + _cite("MSFT_10Q_202603.html")
    probs2 = ar.find_stale_period_claims(ans, _chunks("MSFT_10Q_202603.html", "MSFT_10K_2026.html"))
    _assert("構造版：引 10-Q、證據有更新的 10-K → 抓到", len(probs2) == 1, f"probs={probs2}")

    # ⚠ 這條的來源是 (a) 修好之後**重生成的答案自己暴露的殘留缺口**（2026-08-15 實跑）：
    #   照指示引了 10-K 並說明「全年 41%、未拆單季」,卻拿 FY2026 Q2 當「最新單季」,
    #   而 FY2026 Q3 就在同一份證據集合裡。只有 (a) 的話這一版是全綠的。
    ans3 = ("最新單季 Azure 成長 39%" + _cite("MSFT_10Q_202512.html") +
            " 全年 41% 未拆單季" + _cite("MSFT_10K_2026.html"))
    probs3 = ar.find_stale_period_claims(
        ans3, _chunks("MSFT_10Q_202512.html", "MSFT_10Q_202603.html", "MSFT_10K_2026.html"))
    _assert("引到最新的 10-K 但 10-Q 挑了舊的一季 → 仍要抓（(a) 過關不代表季別選對）",
            len(probs3) == 1 and "單季" in probs3[0] and "MSFT_10Q_202603" in probs3[0],
            f"probs={probs3}")

    # 宣稱詞的措辭覆蓋。⚠ 前兩個是 2026-08-15 實跑中 Generator 真的寫出來、而第一版詞表漏掉的措辭
    #   ——窮舉清單漏掉自己造成的新問題,這條就是那次的回歸鎖。
    for phrase in ("最新單季 Azure 成長", "最新公布的季報顯示", "最新一季", "最近一季",
                   "目前可得的最新單季", "latest quarter", "most recent fiscal year"):
        _assert(f"宣稱詞辨識：{phrase!r}",
                bool(ar._LATEST_CLAIM_RE.search(phrase)))


def gate3_stale_period_negative() -> None:
    """③ 陰性對照。**沒有這一道,一個永遠回 True 的實作也會全綠。**"""
    print("\n③ find_stale_period_claims：陰性（不該抓的都不能抓）")
    f = ar.find_stale_period_claims

    _assert("答案沒有『最新』宣稱 → 不觸發（使用者問的是指定期間）",
            f("Microsoft FY2026 Q3 的 Azure 成長率為 40%" + _cite("MSFT_10Q_202603.html"),
              _chunks("MSFT_10Q_202603.html", "MSFT_10K_2026.html")) == [])
    _assert("引用的就是最新期別 → 不觸發",
            f("Microsoft 最新一季 Azure 成長率為 41%" + _cite("MSFT_10K_2026.html"),
              _chunks("MSFT_10Q_202603.html", "MSFT_10K_2026.html")) == [])
    # (b) 的陰性面：只比得到 10-K 時不能因為「沒引 10-Q」就誤報——(b) 的範圍內沒有引用就跳過。
    _assert("只引 10-K、證據裡沒有更新的 10-Q → 不觸發",
            f("Microsoft 最新一季 Azure 成長率為 41%" + _cite("MSFT_10K_2026.html"),
              _chunks("MSFT_10K_2026.html")) == [])
    _assert("10-K 與最新 10-Q 都引到了 → 不觸發",
            f("最新單季 40%" + _cite("MSFT_10Q_202603.html") + " 全年 41%" + _cite("MSFT_10K_2026.html"),
              _chunks("MSFT_10Q_202512.html", "MSFT_10Q_202603.html", "MSFT_10K_2026.html")) == [])
    # 反轉陷阱的端到端版：用 _source_newest_date 排序的實作會在這裡誤報。
    _assert("NVDA：引 10-Q(FY2027 Q1)、證據有 10-K(FY2026) → 不觸發",
            f("NVIDIA 最新一季營收" + _cite("NVDA_10Q_202604.html"),
              _chunks("NVDA_10Q_202604.html", "NVDA_10K_2026.html")) == [])
    _assert("證據集合只有被引用的那一份 → 不觸發",
            f("Microsoft 最新一季 Azure 成長率為 40%" + _cite("MSFT_10Q_202603.html"),
              _chunks("MSFT_10Q_202603.html")) == [])
    _assert("只引用新聞／Fundamentals（沒有期別可比）→ 不觸發",
            f("Apple 最新一季表現" + _cite("AAPL_News_20260612_04.txt"),
              _chunks("AAPL_News_20260612_04.txt", "AAPL_10K_2025.html")) == [])
    _assert("『最新的 2026 年財報』是絕對期間指稱 → 不觸發（中間有數字，錨詞不算數）",
            f("Microsoft 最新的 2026 年財報顯示成長 40%" + _cite("MSFT_10Q_202603.html"),
              _chunks("MSFT_10Q_202603.html", "MSFT_10K_2026.html")) == [])
    _assert("答案沒有任何引用 → 不觸發",
            f("Microsoft 最新一季 Azure 成長率為 40%",
              _chunks("MSFT_10Q_202603.html", "MSFT_10K_2026.html")) == [])
    # 跨公司不比期別：AAPL 的 FY2026 Q3 與 MSFT 的 FY2026 FY 不可比,只能各自跟自己比。
    ans = ("Apple 最新一季營收" + _cite("AAPL_10Q_202606.html") +
           " Microsoft 最新一季 Azure 成長 40%" + _cite("MSFT_10Q_202603.html"))
    probs = f(ans, _chunks("AAPL_10Q_202606.html", "AAPL_10K_2025.html",
                           "MSFT_10Q_202603.html", "MSFT_10K_2026.html"))
    _assert("多公司：只抓 MSFT,不誤抓已引用最新期別的 AAPL",
            len(probs) == 1 and probs[0].startswith("MSFT"), f"probs={probs}")


def gate4_news_gaps() -> None:
    """④ 時效缺口：由**實際用到的 chunk** 決定,不由問法詞表決定。

    ⚠ **2026-08-19 起這道閘門自帶合成 coverage**。原本 cutoff 讀真實 KB 的 coverage,但 KB
    已不收新聞（見 docs/EVAL.md〈KB 拔除新聞〉）→ cutoff 恆為空 → 三條陽性斷言全部測不出
    東西。那是**量尺失去判別力**,不是機制壞掉。把 coverage 釘死在測試裡,這道閘門就回到
    「新聞若復活,警語機制必須立刻正確」的守門角色,且不再與 KB 當下的內容耦合。
    """
    print("\n④ _news_freshness_gaps：看結果不看問法")
    as_of = date(2026, 8, 15)
    g = ar._news_freshness_gaps
    # 合成 coverage：_news_freshness_gaps 只走 `tickers[X]["news"]["source"]` 這一條路徑
    _fake_cov = {"available": True, "tickers": {
        "AAPL": {"news": {"source": "AAPL_News_20260612_04.txt"}},
        "TSLA": {"news": {"source": "TSLA_News_20260721_01.txt"}}}}
    _orig_cov = ar._get_kb_coverage
    ar._get_kb_coverage = lambda: _fake_cov

    gaps = g(_chunks("AAPL_News_20260612_04.txt", "AAPL_10K_2025.html"), as_of)
    _assert("用到 AAPL 新聞且 cutoff < today → 一筆缺口",
            len(gaps) == 1 and gaps[0]["ticker"] == "AAPL" and gaps[0]["doc_type"] == "news",
            f"gaps={gaps}")
    _assert("cutoff 取 KB coverage 該 ticker 最新的一則（不是這次剛好引到的那則）",
            gaps and gaps[0]["cutoff"] == "2026-06-12", f"gaps={gaps}")
    # ⚠ 這一條是這道閘門的判別力所在：舊版只要問法像新聞題就印警語,不管答案有沒有用到新聞。
    _assert("純財報 chunk（完全沒用到新聞）→ 零筆缺口",
            g(_chunks("MSFT_10Q_202603.html", "MSFT_10K_2026.html"), as_of) == [])
    _assert("as_of 早於 cutoff → 零筆（不製造未來的缺口）",
            g(_chunks("AAPL_News_20260612_04.txt"), date(2026, 1, 1)) == [])
    multi = g(_chunks("AAPL_News_20260612_04.txt", "TSLA_News_20260721_01.txt",
                      "AAPL_News_20260612_04.txt"), as_of)
    _assert("多家新聞 → 每家一筆、同家去重",
            len(multi) == 2 and {x["ticker"] for x in multi} == {"AAPL", "TSLA"}, f"gaps={multi}")
    _assert("空 chunk → 零筆（不炸）", g([], as_of) == [])
    ar._get_kb_coverage = _orig_cov          # 還原，別汙染後面的閘門


def gate5_notice_wiring() -> None:
    """⑤ 缺口 → 警語的接線沒被改壞（`_format_unresolved_freshness_notice` 的既有語意）。"""
    print("\n⑤ 缺口 → 警語")
    gap = [{"ticker": "AAPL", "cutoff": "2026-06-12", "as_of": "2026-08-15", "doc_type": "news"}]
    n_none = ar._format_unresolved_freshness_notice(
        [{"status": "done", "web_used": False, "freshness_gaps": gap}])
    n_mixed = ar._format_unresolved_freshness_notice(
        [{"status": "done", "web_used": False, "freshness_gaps": gap},
         {"status": "done", "web_used": True, "freshness_gaps": []}])
    _assert("無缺口 → 不印警語",
            ar._format_unresolved_freshness_notice(
                [{"status": "done", "web_used": False, "freshness_gaps": []}]) == "")
    _assert("有缺口且全程沒用 web → 印『Web 未提供可用補充』", "Web 未提供可用補充" in n_none)
    _assert("有缺口但這次有用到 web → 改印逐段口徑（警語不得與有出處的數字互相矛盾）",
            "Web 未提供可用補充" not in n_mixed and "未標註" in n_mixed)

    # ══════════════════════════════════════════════════════════════════════════
    # 2026-08-19：`realtime` 缺口。**這一組是回歸鎖**——KB 拔除新聞後，唯一的缺口來源
    # `_news_freshness_gaps` 沒有指涉對象、恆回空集合，於是時效警語**整個死掉**（實測）。
    # 風險沒消失只是換位置：即時題 → Grader 判不足 → 打 web → 預算用完／搜不到 →
    # 回頭用財報 chunk 生成答案 → 零揭露。以下斷言就是為了讓那個狀態不能再靜默發生。
    # ══════════════════════════════════════════════════════════════════════════
    print()
    print("⑤b `realtime` 缺口 → 警語（KB 無新聞後唯一還會觸發的那種）")
    KB = [{"source": "MSFT_10K_2026.html", "ticker": "MSFT"},
          {"source": "MSFT_10Q_202603.html", "ticker": "MSFT"}]
    AS_OF = date(2026, 8, 19)
    _fake_cov = {"available": True, "tickers": {
        "MSFT": {"fundamentals": {"source": "MSFT_Fundamentals_20260612.txt"}}}}
    _orig = ar._get_kb_coverage
    ar._get_kb_coverage = lambda: _fake_cov
    try:
        g_intra = ar._unmet_realtime_gaps("微軟現在的股價是多少？", "intraday", KB, AS_OF)
        g_days = ar._unmet_realtime_gaps("微軟最近有什麼進展？", "days", KB, AS_OF)
        g_none = ar._unmet_realtime_gaps("微軟最新一季營收成長率？", "none", KB, AS_OF)
        _assert("intraday → 一筆缺口，doc_type=realtime",
                len(g_intra) == 1 and g_intra[0]["doc_type"] == "realtime", f"{g_intra}")
        _assert("days → 一筆缺口", len(g_days) == 1)
        # ⚠ 陰性對照：沒有它，「一律產生缺口」也會全綠，警語就會出現在財報題底下
        _assert("none（財報題）→ 零缺口", g_none == [])
        # ⚠ 用 `[:1]` 取值而不是 `[0]`：機制壞掉時要**乾淨地 FAIL**，不是丟 IndexError。
        #   變異測試（把 `_unmet_realtime_gaps` 改成恆回空）第一版就是炸 traceback——
        #   崩潰雖然也算偵測到，但它會蓋掉後面所有斷言、看不出到底壞了幾條。
        _assert("缺口帶 ticker 與 KB 天花板（cutoff 取 8 碼真實日期來源）",
                [(g["ticker"], g["cutoff"]) for g in g_intra[:1]] == [("MSFT", "2026-06-12")],
                f"{g_intra}")
        _assert("候選為空 → 仍記缺口，但 cutoff 留白（不編造日期）",
                [g["cutoff"] for g in
                 ar._unmet_realtime_gaps("x 現在股價？", "intraday", [], AS_OF)[:1]] == [""])
        # ── 接線：缺口 → 警語 ────────────────────────────────────────────────
        n_rt = ar._format_unresolved_freshness_notice(
            [{"status": "done", "web_used": False, "freshness_gaps": g_intra}])
        _assert("realtime 缺口且沒拿到 web → **必須**印警語（這條就是回歸鎖）",
                "資料時效" in n_rt and "Web 未提供可用補充" in n_rt, f"{n_rt!r}")
        _assert("警語措辭講的是『知識庫沒有這種資料』，不是『新聞有點舊』",
                "需要即時／近期資料" in n_rt and "新聞資料截至" not in n_rt, f"{n_rt!r}")
        _assert("同一題有拿到 web → 不印（答案已有 [web:] 出處，警語會自相矛盾）",
                ar._format_unresolved_freshness_notice(
                    [{"status": "done", "web_used": True, "freshness_gaps": g_intra}]) == "")
        _assert("財報題（無缺口）→ 不印",
                ar._format_unresolved_freshness_notice(
                    [{"status": "done", "web_used": False, "freshness_gaps": g_none}]) == "")
        # news 缺口與 realtime 缺口不得互相蓋掉（doc_type 有入 dedup key）
        g_news = [{"ticker": "MSFT", "cutoff": "2026-06-12",
                   "as_of": AS_OF.isoformat(), "doc_type": "news"}]
        n_both = ar._format_unresolved_freshness_notice(
            [{"status": "done", "web_used": False, "freshness_gaps": g_intra + g_news}])
        _assert("news 與 realtime 兩種缺口並存 → 兩句都在（doc_type 有進 dedup key）",
                "需要即時／近期資料" in n_both and "新聞資料截至" in n_both, f"{n_both!r}")
    finally:
        ar._get_kb_coverage = _orig
    _assert("未完成的待辦不計入缺口",
            ar._format_unresolved_freshness_notice(
                [{"status": "pending", "web_used": False, "freshness_gaps": gap}]) == "")


def gate8_web_authority() -> None:
    """⑧ R4 `find_unreconciled_web_conflicts`：web↔財報衝突要求**並陳**而非裁決（零 LLM）。

    ⚠ **這道閘門的重點在誤報對照那組**，跟 ⑦ 一樣：R4 的動作是「兩個都講」，那在
      「同指標不同期間」的誤報下**本來就是正確行為**——所以真正會出事的方向不是漏抓，
      是**話太多**（每個財報題底下都掛一段要求並陳）。下面的陰性對照守的是那個方向。
    """
    print()
    print("⑧ R4：web ↔ 財報並陳（不裁決）")
    f = ar.find_unreconciled_web_conflicts

    def claim(v, src, quote, metric="revenue_growth", unit="percent"):
        return {"metric": metric, "entity": "MSFT", "scope": None, "kind": None,
                "unit": unit, "value": v, "src_types": src, "quote": quote}

    # ── 陽性：web 40%（無出處）vs 10-Q 29%（有出處）→ 要求並陳 ──────────────
    pos = f([claim(40.0, ["web"], "市場報導雲端成長 40%"),
             claim(29.0, ["10-Q"], "Microsoft Cloud 成長 29% [MSFT_10Q_202603.html, chunk #75]")])
    _assert("web 值與財報值互斥且 web 沒標出處 → 發話", len(pos) == 1, f"{pos}")
    _assert("訊息要求**兩個都保留**，不是判誰對",
            bool(pos) and "兩者都要保留" in pos[0] and "當錯的刪掉" in pos[0], f"{pos}")

    # ── 誤報對照（這組才是重點）────────────────────────────────────────────
    _assert("兩邊都已標出處 → 沉默（模型已經並陳了，不要再囉嗦）",
            f([claim(40.0, ["web"], "雲端成長 40% [web: https://reuters.com/x]"),
               claim(29.0, ["10-Q"], "成長 29% [MSFT_10Q_202603.html, chunk #75]")]) == [])
    _assert("值相同 → 不是衝突",
            f([claim(29.0, ["web"], "成長 29%"),
               claim(29.0, ["10-Q"], "成長 29% [a.html, chunk #1]")]) == [])
    _assert("差 2% 以內（四捨五入）→ 不發話",
            f([claim(29.4, ["web"], "成長 29.4%"),
               claim(29.0, ["10-Q"], "成長 29% [a.html, chunk #1]")]) == [])
    _assert("兩邊都是財報 → 不歸 R4 管（那是 R1/R2 的事）",
            f([claim(40.0, ["10-K"], "成長 40%"),
               claim(29.0, ["10-Q"], "成長 29% [a.html, chunk #1]")]) == [])
    _assert("只有 web、沒有財報對照值 → 不發話（不能憑空要求並陳）",
            f([claim(40.0, ["web"], "成長 40%")]) == [])
    _assert("同一個數字 web 與財報都有（src_types 兩者皆備）→ 不是衝突",
            f([claim(40.0, ["web", "10-Q"], "成長 40%"),
               claim(29.0, ["10-Q"], "成長 29% [a.html, chunk #1]")]) == [])
    _assert("不同 metric 不同桶 → 不比",
            f([claim(40.0, ["web"], "雲端成長 40%", metric="cloud_growth"),
               claim(29.0, ["10-Q"], "總營收成長 29% [a.html, chunk #1]")]) == [])
    _assert("沒有 src_types（數字定位不到任何來源）→ 跳過，不猜",
            f([claim(40.0, [], "成長 40%"),
               claim(29.0, ["10-Q"], "成長 29% [a.html, chunk #1]")]) == [])
    _assert("空輸入", f([]) == [])

    # ── R3 不得因為 web 進來而開始對 web 觸發（兩條規則的界線）──────────────
    r3 = ar.find_authority_conflicts(
        [claim(40.0, ["web"], "成長 40%"),
         claim(29.0, ["10-Q"], "成長 29% [a.html, chunk #1]")], [])
    _assert("R3 仍只認 news，不對 web 觸發（它判『錯』，套到 web 是錯的）", r3 == [], f"{r3}")

    # ── _ground_source_type 看得到 web_extra（R4 的前提）────────────────────
    cs = [{"metric": "m", "entity": "E", "unit": "percent", "value": 40.0, "quote": "q"}]
    ar._ground_source_type(cs, [], web_extra="市場報導雲端成長 40% [web: https://reuters.com/x]")
    _assert("web_extra 裡的數字會被標成 src_types=['web']", cs[0].get("src_types") == ["web"],
            f"{cs[0].get('src_types')}")
    # ⚠ 這條測的是「不變量」不是「新行為」：web_extra 為空時，grounding 的結果必須與
    #   2026-08-19 以前逐字相同。第一版斷言寫成 `chunks=[]` 期望 `src_types == []`，
    #   但兩者都空本來就會早退、`src_types` 根本不會被設定——**那是舊行為，不該改**。
    #   改成餵真實 chunk 才測得到「空 web_extra 不製造假來源」。
    kb = [{"source": "MSFT_10Q_202603.html", "content": "Cloud revenue increased 40%"}]
    cs2 = [{"metric": "m", "entity": "E", "unit": "percent", "value": 40.0, "quote": "q"}]
    ar._ground_source_type(cs2, kb, web_extra="")
    _assert("web_extra 為空 → 只認 chunk 的來源型別，不製造 'web'",
            cs2[0].get("src_types") == ["10-Q"], f"{cs2[0].get('src_types')}")
    cs3 = [{"metric": "m", "entity": "E", "unit": "percent", "value": 40.0, "quote": "q"}]
    ar._ground_source_type(cs3, kb, web_extra="無關內容，沒有這個數字")
    _assert("web_extra 有內容但不含該數字 → 不得標成 web",
            cs3[0].get("src_types") == ["10-Q"], f"{cs3[0].get('src_types')}")


def gate6_end_to_end_regression() -> None:
    """⑥ 端到端回歸：現行結果檔裡**每一題**的最終答案都不得留下期別落差。

    與閘門② 方向相反：② 測「病復發抓不抓得到」（凍結的舊答案），這裡測「現況是乾淨的」。
    只有兩個都在，才同時擋住「機制失效」與「機制退步」。
    """
    print("\n⑥ 端到端回歸：現行結果檔不得留下期別落差")
    p = Path("experiments/web_replay_answers.json")
    if not p.exists():
        _assert("結果檔存在", False, f"{p} 不存在 → 先跑 eval/record_web_fixture.py --mode replay")
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    for rec in data["records"]:
        if rec.get("error"):
            continue
        probs = ar.find_stale_period_claims(
            rec.get("answer") or "", _chunks(*[s["source"] for s in rec.get("sources", [])]))
        _assert(f"{rec['id']}：最終答案無期別落差", not probs, f"probs={probs}")


def gate7_untraceable_numbers() -> None:
    """⑦ 數字溯源。陽性靠變異注入（沒有實測正例）；重點在**誤報對照**。"""
    print("\n⑦ find_untraceable_numbers：數字必須溯得回來源")
    f = ar.find_untraceable_numbers
    web = ("| Year | Average Stock Price | Year Close |\n"
           "| 2026 | 196.8165 | 224.0900 |\n| 2025 | 153.6958 | 186.4200 |\n"
           "NVDA quote 225.57 as of today")
    kb = [{"content": "P/E Ratio Trailing: 31.325687\nMarket Cap : $4962.16B\n"
                      "Greater China revenue 15,369 vs 18,816"}]

    _assert("變異注入：憑空的 999.99 → 抓到", len(f("平均價為 999.99", [], web)) == 1)
    _assert("變異注入：鄰近但錯的 196.92 → 抓到（捨入容忍不是放水）",
            len(f("平均價為 196.92", [], web)) == 1)
    # ↓ 以下每一條都是實跑量到的誤報形狀。少了它們，這層放進主線會擾動 100 題基準。
    _assert("逐字命中 225.57 → 不誤報", f("股價 225.57", [], web) == [])
    _assert("來源小數位較多（196.8165 → 196.82）是正確四捨五入 → 不誤報",
            f("2026 年平均股價 196.82", [], web) == [])
    _assert("P/E 31.325687 → 答案寫 31.33 → 不誤報", f("trailing P/E 為 31.33", kb, "") == [])
    _assert("億換算（15,369 → 153.69 億美元）→ 不誤報（換算值本來就不會逐字出現）",
            f("營收從 153.69 億美元 提升至 188.16 億美元", kb, "") == [])
    _assert("兆換算 → 不誤報", f("市值約 4.96 兆美元", kb, "") == [])
    _assert("百分比不受檢（13.90% 不是報價形態）", f("成長 13.90%", [], web) == [])
    _assert("千分位寫法差異（來源 $4962.16B / 答案 4,962.16）→ 不誤報",
            f("市值 4,962.16 百萬美元", kb, "") == [])
    _assert("空答案／空來源 → 不炸", f("", [], "") == [] and f("股價 225.57", [], "") != [])


# 修法前的真實序號引用，**逐字凍結在這裡，刻意不讀 `experiments/`**。
# ⚠ 這是本檔閘門② 早就寫下的教訓：讀結果檔的話，**一修好陽性就消失、閘門會隨修法生效
#   而自己失去判別力**。2026-08-20 我在同一個 session 內重踩了一次——⑨ 的端到端斷言
#   原本讀 `experiments/..._news37_all.json`，把修好的 replay 併回去之後那條當場變成
#   `dirty=0` 而 FAIL：**不是機制壞了，是量尺沒東西可量**。
# 來源：experiments/web_fixture_answers_news37.json（2026-08-20 錄製，修法前）。
_FROZEN_BARE_REFS = [
    ('news-03',
     ['【Reference 7, chunk #15】'],
     [('AAPL_10Q_202606.html', 22), ('AAPL_10Q_202606.html', 21), ('AAPL_10Q_202606.html', 20), ('AAPL_10Q_202606.html', 16), ('AAPL_10Q_202606.html', 26), ('AAPL_10Q_202606.html', 1), ('AAPL_10Q_202606.html', 15), ('AAPL_10K_2025.html', 17), ('AAPL_10K_2025.html', 16), ('AAPL_10Q_202603.html', 20), ('AAPL_10Q_202603.html', 19), ('AAPL_10Q_202603.html', 15), ('AAPL_10Q_202603.html', 1)]),
    ('news-07',
     ['【Reference 1, chunk #112】', '【Reference 4, chunk #184】', '【Reference 1, chunk #112】', '【Reference 4, chunk #184】', '【Reference 5, chunk #16】', '【Reference 7, chunk #75】', '【Reference 8, chunk #75】'],
     [('MSFT_10K_2026.html', 112), ('MSFT_10Q_202603.html', 50), ('MSFT_10Q_202512.html', 52), ('MSFT_10K_2026.html', 184), ('MSFT_10K_2026.html', 16), ('MSFT_10K_2026.html', 33), ('MSFT_10K_2026.html', 60), ('MSFT_10K_2026.html', 75), ('MSFT_10K_2026.html', 110), ('MSFT_10K_2026.html', 32), ('MSFT_10K_2026.html', 82)]),
    ('news-13',
     ['【Reference 6, chunk #0】', '【Reference 8, chunk #0】', '【Reference 9, chunk #0】', '【Reference 14, chunk #0】'],
     [('AAPL_10Q_202606.html', 1), ('GOOGL_10Q_202606.html', 1), ('NVDA_10K_2026.html', 118), ('AAPL_10Q_202606.html', 0), ('AAPL_10Q_202606.html', 20), ('AAPL_10Q_202606.html', 10), ('AAPL_10Q_202606.html', 2), ('GOOGL_Fundamentals_20260612.txt', 0), ('GOOGL_10Q_202606.html', 8), ('META_Fundamentals_20260612.txt', 0), ('GOOGL_10Q_202603.html', 8), ('GOOGL_10Q_202606.html', 0), ('NVDA_Fundamentals_20260612.txt', 0), ('MSFT_10K_2026.html', 1), ('META_10Q_202606.html', 1), ('MSFT_Fundamentals_20260612.txt', 0), ('MSFT_10K_2026.html', 2), ('MSFT_10K_2026.html', 218), ('NVDA_10K_2026.html', 10), ('MSFT_10Q_202603.html', 1), ('NVDA_10Q_202510.html', 103), ('NVDA_10K_2026.html', 181), ('META_10Q_202606.html', 15), ('META_10Q_202603.html', 14), ('META_Fundamentals_20260612.txt', 1), ('TSLA_Fundamentals_20260612.txt', 0), ('AMZN_10Q_202606.html', 135), ('AMZN_10Q_202603.html', 43)]),
    ('mi-02',
     ['【Reference 1, chunk #0】', '【Reference 1, chunk #0】', '【Reference 6, chunk #86】', '【Reference 6, chunk #86】', '【Reference 6, chunk #86】', '【Reference 3, chunk #85】'],
     [('NVDA_Fundamentals_20260612.txt', 0), ('NVDA_10Q_202510.html', 96), ('NVDA_10K_2026.html', 85), ('NVDA_10K_2026.html', 123), ('NVDA_10K_2026.html', 88), ('NVDA_10K_2026.html', 86), ('NVDA_10K_2026.html', 34)]),
    ('mi-07',
     ['【Reference 1, chunk #0】', '【Reference 1, chunk #0】'],
     [('AMZN_Fundamentals_20260612.txt', 0), ('AMZN_10Q_202606.html', 94), ('AMZN_10Q_202606.html', 2), ('AMZN_10Q_202606.html', 26), ('AMZN_10Q_202606.html', 43), ('AMZN_10Q_202606.html', 24), ('AMZN_10K_2025.html', 134), ('AMZN_10Q_202603.html', 93), ('AMZN_10Q_202606.html', 35), ('AMZN_10Q_202606.html', 74), ('AMZN_10K_2025.html', 135), ('AMZN_10Q_202606.html', 59)]),
]

# ══════════════════════════════════════════════════════════════════════════════
def gate9_reference_citation_repair() -> None:
    """⑨ `_repair_reference_citations`：把只有序號的引用確定性還原成檔名（零 LLM）。

    **為什麼要有這個機制**（2026-08-20 實測）：live 路徑 5/37 題吐出 `【Reference 7, chunk #15】`
    ——不是檔名、不是網址，**指向不存在的東西**；snapshot 路徑 0/100。而那 5 題的**有效 KB 引用是 0**,
    整份答案沒有任何可追溯的財報出處,答案裡卻有大量財報數字。
    舊行為：validator 判它捏造 → 丟回重寫 → 重試上限 1 用完 → **原樣出貨**。

    ⚠ **這道閘門的重點是那兩組守門條件的誤報對照**。修補的失敗方向不是「漏修」（漏修＝維持現狀,
      不會更糟）,而是**把「無法追溯」變成「看起來可追溯的錯引用」**——那比現況糟得多,
      因為讀者會相信一個指向錯 chunk 的出處。所以下面兩條陰性對照才是主角：
        ① `N` 超出候選範圍 → 不可以硬湊
        ② 引用裡的 `chunk #M` 與第 N 筆的 `chunk_index` 不一致 → **不知道模型指的是哪一個** → 不可以猜
      ⚠ 誤報對照③（`Reference 0`）看起來像邊界瑣事,其實是三條裡**最危險的一條**：Python 的
        `[-1]` 是合法索引,少了守門① 它會**安靜地指到最後一筆候選**（其他越界值是當場 IndexError,
        反而吵）。變異測試實測：拿掉守門① 時 `Reference 9` 直接炸,而 `Reference 0` 是靠守門② 才擋下的。
      條件② 同時讓「修補的排序假設」不必被證明：兩半一致時,那筆修補是被資料自己確認過的。
    """
    print()
    print("⑨ Reference 序號引用的確定性還原")
    ch = [{"source": "AAPL_10Q_202606.html", "chunk_index": 22},
          {"source": "MSFT_10K_2026.html", "chunk_index": 112},
          {"source": "NVDA_Fundamentals_20260612.txt", "chunk_index": 0}]
    r = ar._repair_reference_citations

    # ── 陽性：兩半自洽就該還原 ────────────────────────────────────────────────
    out, fixed, skipped = r("營收成長 18%【Reference 2, chunk #112】", ch)
    _assert("⑨ 自洽的 Reference 序號 → 還原成真檔名",
            "【MSFT_10K_2026.html, chunk #112】" in out and (fixed, skipped) == (1, 0), out)
    out, fixed, _ = r("[Reference 3, chunk #0]", ch)
    _assert("⑨ 半形括號也吃（模型中英夾雜時會出現）",
            "[NVDA_Fundamentals_20260612.txt, chunk #0]" == out and fixed == 1, out)
    out, fixed, _ = r("a【Reference 1, chunk #22】b【Reference 2, chunk #112】c", ch)
    _assert("⑨ 一句裡多筆都要還原", fixed == 2 and "Reference" not in out, out)

    # ── 誤報對照（主角）：不確定就不要動 ──────────────────────────────────────
    out, fixed, skipped = r("【Reference 9, chunk #5】", ch)
    _assert("⑨ 誤報對照①：N 超出候選範圍 → 逐字不動、計入 skipped",
            out == "【Reference 9, chunk #5】" and (fixed, skipped) == (0, 1), out)
    out, fixed, skipped = r("【Reference 2, chunk #999】", ch)
    _assert("⑨ 誤報對照②：chunk# 與第 N 筆不一致 → 逐字不動（不知道指哪個就不要猜）",
            out == "【Reference 2, chunk #999】" and (fixed, skipped) == (0, 1), out)
    out, fixed, skipped = r("【Reference 0, chunk #22】", ch)
    _assert("⑨ 誤報對照③：序號 0（1-based 邊界）→ 不動",
            out == "【Reference 0, chunk #22】" and (fixed, skipped) == (0, 1), out)

    # ── 不該被碰到的東西 ──────────────────────────────────────────────────────
    ok = "【AAPL_10Q_202606.html, chunk #22】"
    _assert("⑨ 已經合法的引用逐字不動", r(ok, ch)[0] == ok)
    web = "【web: https://finance.yahoo.com/quote/AAPL】"
    _assert("⑨ web 引用逐字不動（它沒有 chunk #N，不該進這條路）", r(web, ch)[0] == web)
    pre = "【Reference 1: AAPL_10Q_202606.html, chunk #22】"
    _assert("⑨ 帶檔名的 `Reference N:` 前綴不動（_extract_citations 已經吃得下）",
            r(pre, ch)[0] == pre)
    _assert("⑨ 空候選清單 → 不動、不炸", r("【Reference 1, chunk #22】", [])[0]
            == "【Reference 1, chunk #22】")
    _assert("⑨ 空答案 → 不炸", r("", ch)[0] == "")

    # ── 端到端：真實答案上的修補率（**用凍結樣本，不讀 experiments/**）──────────
    # ⚠ 凍結樣本的 `chunks` 取自結果檔的 `sources`＝**跨子問題的聯集**，與 Generator 實際看到的
    #   清單不同 → 量到的是**下界**。實測帶 AGENTIC_TRACE 的重放，用真正的 allowed_chunks 時
    #   守門條件 **0/15 擋下**（代理排序下會擋掉 4 筆）。所以下面的門檻刻意鬆：
    #   它守的是「機制有沒有在真實輸入上作用」，不是「修補率有多高」。
    tot_fix = clean_after = 0
    for _qid, _cites, _chunks in _FROZEN_BARE_REFS:
        cs = [{"source": s, "chunk_index": i} for s, i in _chunks]
        out, f, _k = r(" ".join(_cites), cs)
        tot_fix += f
        if not ar._BARE_REF_CITE_RE.search(out):
            clean_after += 1
    _assert(f"⑨ 端到端（凍結樣本 {len(_FROZEN_BARE_REFS)} 題／"
            f"{sum(len(c) for _, c, _ in _FROZEN_BARE_REFS)} 筆序號引用）："
            f"還原 {tot_fix} 筆、{clean_after} 題完全清乾淨",
            len(_FROZEN_BARE_REFS) == 5 and tot_fix >= 10 and clean_after >= 3,
            f"fixed={tot_fix} clean={clean_after}")


# ══════════════════════════════════════════════════════════════════════════════
def gate10_ratio_source_field() -> None:
    """⑩ `_ensure_ratio_source_coverage` 必須補「含被問欄位」的 Fundamentals，不是分數最高的。

    **實測根因（2026-08-20，推翻 BACKLOG 掛了 9 天的「未驗證」猜測）**：
      · `MSFT_Fundamentals #0`（698 字元）含 Revenue Growth／Gross／Operating／Profit Margin
        ——**每一個比率都在這裡**；`#1`（2045 字元）只有 Total Cash／Total Debt，**一個都沒有**。
      · cross-encoder 對「Microsoft 的毛利率是多少？」把 **#1 排在 #0 前面**（0.928 vs 0.918）。
      · 保底機制原本補「分數最高的 Fundamentals」→ 補進零比率的 #1 → Generator 沒有 TTM 值可引
        → 退回 10-K/10-Q 的財年／單季數字（口徑不同、數值接近、**無揭露**）。
      舊記載猜「#1 長三倍造成 rerank 偏好」——方向對了，但傷害不在 rerank，
      **在保底用「分數最高」而不是「有沒有那個欄位」挑**。

    ⚠ 誤報方向：認不出欄位時**必須退回舊行為**（補分數最高的），不可以什麼都不補——
      那會讓 ratio 題完全沒有 Fundamentals 錨源，比原本更糟。
    """
    print()
    print("⑩ ratio 保底要補「含該欄位」的 Fundamentals")
    F = ar._ensure_ratio_source_coverage

    def _c(src, idx, score, content):
        return {"source": src, "chunk_index": idx, "ticker": src.split("_")[0],
                "raw_rerank_score": score, "content": content}

    # 逐字取自 us_stock_rag_edgar_mdna 的 MSFT_Fundamentals_20260612.txt（2026-08-20）
    C0 = _c("MSFT_Fundamentals_20260612.txt", 0, 0.918,
            "Market Cap : $2899.62B Revenue Growth (YoY): 18.30% "
            "Gross Margin : 68.31% Operating Margin: 46.33% Profit Margin : 39.34%")
    C1 = _c("MSFT_Fundamentals_20260612.txt", 1, 0.928,
            "Total Cash : $78.23B Total Debt : $125.43B Debt-Equity ... Current Ratio ...")
    FILING = _c("MSFT_10K_2026.html", 124, 0.90, "revenue increased 18% or $50.1 billion")
    ranked = [C1, C0, FILING]

    got = F(ranked, [C1, FILING], ["MSFT"], "Microsoft 的毛利率是多少？")
    _assert("⑩ selected 只有零比率的 #1 → 必須把含 Gross Margin 的 #0 補進來",
            any(c["chunk_index"] == 0 for c in got if "Fundamentals" in c["source"]),
            [(c["source"], c["chunk_index"]) for c in got])

    got = F(ranked, [C0, FILING], ["MSFT"], "Microsoft 的毛利率是多少？")
    _assert("⑩ 誤報對照①：已經有含該欄位的 #0 → 不重複補、不動",
            len(got) == 2 and sum(1 for c in got if "Fundamentals" in c["source"]) == 1,
            [(c["source"], c["chunk_index"]) for c in got])

    # ⚠ 這條的第一版寫成「selected=[C1, FILING] → 結果要含 Fundamentals」，**沒有判別力**：
    #   C1 本來就在 selected 裡，函式什麼都不做也會通過（變異測試實測沒 FAIL 才發現）。
    #   要測「認不出欄位時會不會退回舊行為」，selected 就**不能先放 Fundamentals**。
    got = F(ranked, [FILING], ["MSFT"], "Microsoft 的自由現金流是多少？")
    _assert("⑩ 誤報對照②：認不出欄位（自由現金流不在對照表）→ 退回舊行為補分數最高的，"
            "**不可以什麼都不補**",
            any("Fundamentals" in c["source"] for c in got),
            [(c["source"], c["chunk_index"]) for c in got])

    _assert("⑩ 誤報對照③：want_tickers 為空 → 原樣回傳",
            F(ranked, [C1], [], "Microsoft 的毛利率是多少？") == [C1])
    _assert("⑩ 市值題不是 ratio 意圖（單一來源，補了只是多餘）",
            not ar._is_ratio_intent("NVIDIA 目前的市值是多少？"))
    _assert("⑩ 成長率／毛利率／淨利率各自對到不同欄位",
            ar._wanted_ratio_fields("營收成長率") == ["Revenue Growth"]
            and ar._wanted_ratio_fields("毛利率") == ["Gross Margin"]
            and ar._wanted_ratio_fields("淨利率") == ["Profit Margin"])

    # ── 活體對照：欄位名是不是還長這樣（ingest 改了格式，這條會先叫）─────────────
    try:
        from qdrant_client import models as _qm
        _cl = ar.rq.make_qdrant_client()
        _pts, _ = _cl.scroll(ar.rq.COLLECTION_NAME, limit=20, with_payload=True, with_vectors=False,
                             scroll_filter=_qm.Filter(must=[_qm.FieldCondition(
                                 key="source", match=_qm.MatchValue(
                                     value="MSFT_Fundamentals_20260612.txt"))]))
        _by = {p.payload.get("chunk_index"): (p.payload.get("document") or "") for p in _pts}
        _assert("⑩ 活體：真實 Fundamentals #0 仍含全部四個欄位名（ingest 改格式時這條先叫）",
                all(f.lower() in _by.get(0, "").lower()
                    for f in ("Revenue Growth", "Gross Margin", "Operating Margin", "Profit Margin")),
                f"#0 長度={len(_by.get(0, ''))}")
        _assert("⑩ 活體：#1 仍然一個比率欄位都沒有（＝這個機制針對的形狀還在）",
                not any(f.lower() in _by.get(1, "").lower()
                        for f in ("Revenue Growth", "Gross Margin", "Profit Margin")),
                f"#1 長度={len(_by.get(1, ''))}")
    except Exception as e:
        _assert("⑩ 活體對照可執行（掃不到就等於這道閘門只測了合成資料）", False, repr(e))

    # ── ⑩b 確定性補撈：池裡沒有含該欄位的 Fundamentals 時，直接查 Qdrant ──────────
    # ⚠ **為什麼需要這一組**（2026-08-21）：上面那幾條測的是「池裡有 #0 時會不會挑對」，
    #   而實測的生產失敗是「**池裡根本沒有 #0**」——英譯 query 下 #0 連 RRF 候選名單都沒進。
    #   也就是說 ⑩ 原本整組斷言測的形狀，在生產上不成立；量尺看得見的病和實際的病不是同一個。
    # ⚠ reranker 用樁替代（不載模型）：要測的是**選哪一個 chunk**（確定性），不是分數本身。
    #   樁的回傳值同時當成分數溯源的探針——見下方最後一條。
    class _StubRR:
        def __init__(self): self.calls = 0
        def predict(self, pairs, batch_size=1):
            self.calls += 1
            return [0.4242 + i * 0.01 for i in range(len(pairs))]

    _stub = _StubRR()
    _orig_get_models = ar._get_models
    try:
        _cl2 = ar.rq.make_qdrant_client()
        ar._get_models = lambda: (None, _stub, _cl2)

        _got = ar._fetch_fundamentals_with_field("MSFT", ["Revenue Growth"], "營收成長率")
        _assert("⑩b 池裡沒有 → 直接從 Qdrant 撈到含 Revenue Growth 的那一個（實測是 #0）",
                _got is not None and "Fundamentals" in _got["source"]
                and "revenue growth" in (_got["content"] or "").lower(),
                None if _got is None else (_got["source"], _got["chunk_index"]))
        _assert("⑩b 補撈的 chunk 走生產建構子 → 期別／口徑欄位齊全（下游 validator 靠它們）",
                _got is not None and _got.get("period_basis") == "TTM"
                and "fiscal_rank" in _got and _got.get("ticker") == "MSFT",
                None if _got is None else sorted(_got))
        _assert("⑩b 分數是**真的算出來的**，不是捏造的常數"
                "（這個數字會印在引用區塊給使用者看）",
                _got is not None and abs(_got["raw_rerank_score"] - 0.4242) < 1e-9
                and _stub.calls == 1,
                None if _got is None else (_got["raw_rerank_score"], _stub.calls))

        _assert("⑩b 誤報對照①：欄位在庫裡不存在 → 回 None，**不可以退而求其次補一個隨便的 Fundamentals**"
                "（那正是原本的病）",
                ar._fetch_fundamentals_with_field("MSFT", ["Zzz Nonexistent Field"], "x") is None)
        _assert("⑩b 誤報對照②：沒有 ticker／沒有欄位 → 回 None，不查庫",
                ar._fetch_fundamentals_with_field("", ["Revenue Growth"], "x") is None
                and ar._fetch_fundamentals_with_field("MSFT", [], "x") is None)

        # 端到端：ranked 裡**一個 Fundamentals 都沒有**（＝實測的生產池形狀）
        _e2e = F([FILING], [FILING], ["MSFT"], "Microsoft 的營收成長率表現如何？")
        _assert("⑩b 端到端：池裡零個 Fundamentals（實測的生產形狀）→ 保底仍要補到含該欄位的 chunk",
                any("Fundamentals" in c["source"] and "revenue growth" in (c["content"] or "").lower()
                    for c in _e2e),
                [(c["source"], c["chunk_index"]) for c in _e2e])
        _assert("⑩b 端到端誤報對照：不是 ratio 欄位時不會憑空補（自由現金流→池裡沒有就沒有）",
                not any("Fundamentals" in c["source"]
                        for c in F([FILING], [FILING], ["MSFT"], "Microsoft 的自由現金流？")),
                None)
    except Exception as e:
        _assert("⑩b 可執行（跑不起來就等於這一組沒測到東西）", False, repr(e))
    finally:
        ar._get_models = _orig_get_models


# ══════════════════════════════════════════════════════════════════════════════
def gate11_basis_disclosure() -> None:
    """⑪ `_basis_disclosure_notice`：答案只有財報期間口徑時必須揭露（零 LLM）。

    治的病（lex-17 實測）：問「營收成長率」沒指定口徑 → 答案給 10-K 的財年 18%，
    而 gold 是 Fundamentals 的 TTM 18.30%。**數字不是假的，錯的是口徑，且零揭露**——
    兩個值差 0.3pt，讀者無從分辨拿到的是哪一種。

    ⚠ **這道閘門的重點是三條沉默對照**，理由同 ⑧⑩：警語的失敗方向**不是漏印，是話太多**。
      每個財報題底下都掛一段「這不是 TTM」會讓警語變成背景噪音，等到真的需要時沒人看
      ——2026-08-14 的時效警語已經踩過一次同樣的坑。
    ⚠ 口徑判定用 payload 的 `period_basis`（全庫只有 TTM／fiscal_year 兩個值），
      **不是檔名也不是 doc_type**——換 collection 或改 ingest 時，活體那條會先叫。
    """
    print()
    print("⑪ 口徑揭露（TTM vs 財報期間）")
    N = ar._basis_disclosure_notice
    FY = {"period_basis": "fiscal_year"}
    TTM = {"period_basis": "TTM"}

    _assert("⑪ 陽性：沒指定口徑的成長率題、只引到財年 chunk → 必須揭露",
            ar._BASIS_NOTICE_MARK in N("Microsoft 的營收成長率表現如何？", [FY]))
    _assert("⑪ 陽性：毛利率題同樣成立（不是只認成長率）",
            ar._BASIS_NOTICE_MARK in N("Apple 的毛利率是多少？", [FY]))

    _assert("⑪ 沉默對照①（退路）：引到 TTM chunk 但解不出欄位值 → 維持沉默，"
            "不在看不懂的情況下多話",
            N("Microsoft 的營收成長率表現如何？", [FY, TTM]) == "")
    _assert("⑪ 沉默對照②：問題自己指定了財年（2025 財年／FY2026）→ 財報口徑正是要的，講了是雜訊",
            N("Apple 2025 財年的毛利率是多少？", [FY]) == ""
            and N("Apple FY2026 gross margin", [FY]) == "")
    _assert("⑪ 沉默對照③：問題帶 yyyymm 期碼 → 同上",
            N("Microsoft 202603 的毛利率", [FY]) == "")
    _assert("⑪ 沉默對照④：市值／EPS 這類單一來源指標沒有口徑歧義 → 不觸發",
            N("NVIDIA 目前的市值是多少？", [FY]) == ""
            and N("Apple 的 Diluted EPS 是多少？", [FY]) == "")
    _assert("⑪ 沉默對照⑤：沒引到任何財報期間 chunk（純 web 答案）→ 不是這條的守備範圍",
            N("Microsoft 的毛利率", []) == "")

    _assert("⑪ 措辭只陳述事實、不宣稱原因（KB 可能有 TTM 只是沒被引用，"
            "斷言一個查不到的原因＝製造新的不可信內容）",
            "缺乏" not in N("Apple 的毛利率是多少？", [FY])
            and "TTM" in N("Apple 的毛利率是多少？", [FY]))

    # ── 活體對照：period_basis 這個欄位還在不在、值還是不是那兩個 ──────────────
    try:
        import collections as _c
        _cl = ar.rq.make_qdrant_client()
        _seen, _off = _c.Counter(), None
        while True:
            _pts, _off = _cl.scroll(ar.rq.COLLECTION_NAME, limit=1000, offset=_off,
                                    with_payload=["period_basis"], with_vectors=False)
            for _p in _pts:
                _seen[_p.payload.get("period_basis")] += 1
            if _off is None:
                break
        _assert(f"⑪ 活體：period_basis 仍只有 TTM／fiscal_year 兩個值（實測 {dict(_seen)}）",
                set(_seen) == {"TTM", "fiscal_year"} and _seen["TTM"] > 0, str(dict(_seen)))
    except Exception as e:
        _assert("⑪ 活體對照可執行（掃不到就等於這道閘門只測了合成資料）", False, repr(e))

    # ── ⑪c 沉默條件③：看**值有沒有講出來**，不是看有沒有引到 TTM chunk ──────────
    # ⚠ **這一組是回歸鎖，來自一次「護欄被自己要抓的行為解除武裝」**（2026-08-21，lex-17）：
    #   補撈修好之後，答案確實引到了 MSFT_Fundamentals #0（眼前就是 Revenue Growth 18.30%），
    #   卻寫成「全年與最近的 TTM 都在約 18% 左右【…chunk #0】」——把 TTM 四捨五入成 18%，
    #   再與 10-K 的財年 18% 併成同一個說法。引用是真的、數字看起來也對，兩個口徑就這樣消失。
    #   而舊條件③「引用裡有 TTM chunk 就沉默」→ **正好在該叫的那一刻把警語關掉**。
    # ⚠ 陽性答案**逐字凍結在這裡，不讀 experiments/**（同閘門② 的理由）：讀結果檔的話，
    #   一修好陽性就消失，這道閘門會隨修法生效而自己失去判別力。
    _TTM0 = {"source": "MSFT_Fundamentals_20260612.txt", "chunk_index": 0, "period_basis": "TTM",
             "content": "Market Cap : $2899.62B Revenue Growth (YoY): 18.30% "
                        "Gross Margin : 68.31% Operating Margin: 46.33% Profit Margin : 39.34%"}
    _Q = "Microsoft 的營收成長率表現如何？"
    # 逐字取自 experiments/agentic/gj_mdna_65q_after2.json 的 lex-17（2026-08-21）
    _ANS_BAD = ("Microsoft 的營收持續以兩位數的高速成長，全年與最近的 TTM 都在約 18% 左右的 "
                "YoY 增長率【MSFT_Fundamentals_20260612.txt, chunk #0】。")
    _ANS_GOOD = "TTM（截至 2026-06-12）：營收年增率 **18.30%**（YoY）【MSFT_Fundamentals_20260612.txt, chunk #0】"

    _n = N(_Q, [FY, _TTM0], _ANS_BAD)
    _assert("⑪c 陽性（回歸鎖）：引了 TTM chunk 卻把 18.30% 講成「約 18%」→ 必須揭露"
            "（舊條件③ 在這裡會沉默＝護欄被自己要抓的行為關掉）",
            ar._BASIS_NOTICE_MARK in _n, repr(_n[:60]))
    _assert("⑪c 警語要把**值**講出來，不是只說「這不是 TTM」"
            "（值逐字取自答案自己引用的那個 chunk，仍可追溯）",
            "18.30" in _n, repr(_n[:120]))

    _assert("⑪c 沉默對照①：答案真的講出 18.30% → 不必再講",
            N(_Q, [FY, _TTM0], _ANS_GOOD) == "")
    _assert("⑪c 沉默對照②：講成 18.3%（去掉尾零）視為同一個值 → 仍沉默",
            N(_Q, [FY, _TTM0], "營收年增率 18.3%") == "")
    _assert("⑪c 誤報對照：118.3 不算講出了 18.30（前後要有邊界，否則警語會被無關數字關掉）",
            ar._BASIS_NOTICE_MARK in N(_Q, [FY, _TTM0], "營收 118.3 億美元"))
    _assert("⑪c 沉默對照③：不是 ratio 題 → 不管引了什麼都不觸發",
            N("NVIDIA 目前的市值是多少？", [FY, _TTM0], "市值 2.9 兆") == "")
    _assert("⑪c 多欄位：只有其中一個值沒講出來 → 只講那一個",
            "68.31" in N("Microsoft 的毛利率與營收成長率？", [FY, _TTM0], "營收年增率 18.30%")
            and "18.30" not in N("Microsoft 的毛利率與營收成長率？", [FY, _TTM0], "營收年增率 18.30%"))

    # ── ⑪b 生產建構子對照：validator 讀的欄位，retrieve() 到底供不供得出來 ──────────
    # ⚠ **這一組是補一個真實的漏洞**（2026-08-21）：上面每一條都拿測試自己造的
    #   `{"period_basis": ...}` 餵進去，於是全綠——而生產的 `rq.retrieve()`
    #   **根本沒把 `period_basis` 放進 chunk dict**，這個 validator 在線上結構性永遠不觸發。
    #   「payload 裡有這個欄位」（上面那條活體）和「chunk dict 裡有這個欄位」是兩件事，
    #   中間隔著一個建構子。量尺與被測物耦合的同型錯誤，這是第五次。
    #   → 所以這裡拿**真實 payload 餵生產建構子** `rq._payload_to_chunk`，不自己造 dict。
    try:
        from qdrant_client import models as _qm2
        _cl3 = ar.rq.make_qdrant_client()

        def _one(**match):
            _pts, _ = _cl3.scroll(ar.rq.COLLECTION_NAME, limit=1, with_payload=True,
                                  with_vectors=False,
                                  scroll_filter=_qm2.Filter(must=[
                                      _qm2.FieldCondition(key=k, match=_qm2.MatchValue(value=v))
                                      for k, v in match.items()]))
            return (_pts[0].payload or {}) if _pts else None

        _fund_pl = _one(ticker="MSFT", period_basis="TTM")
        _fil_pl = _one(ticker="MSFT", period_basis="fiscal_year")
        _fund = ar.rq._payload_to_chunk(_fund_pl or {}, 0.0, 0.0)
        _fil = ar.rq._payload_to_chunk(_fil_pl or {}, 0.0, 0.0)

        _assert("⑪b 生產建構子把 period_basis 帶進 chunk dict（少了它，這道閘門上面全部是假綠）",
                "period_basis" in _fund and "period_basis" in _fil,
                sorted(_fund))
        _assert("⑪b Fundamentals → TTM、filing → fiscal_year（值也要對，不是有 key 就好）",
                _fund.get("period_basis") == "TTM" and _fil.get("period_basis") == "fiscal_year",
                (_fund.get("period_basis"), _fil.get("period_basis")))
        _assert("⑪b 端到端：真實 filing chunk 直接餵 validator → 會揭露"
                "（上面的陽性用的是合成 dict，這條用的是庫裡真的那一筆）",
                ar._BASIS_NOTICE_MARK in N("Microsoft 的營收成長率表現如何？", [_fil]),
                _fil.get("period_basis"))
        _assert("⑪b 端到端沉默對照：真實 Fundamentals chunk 一起引 → 不揭露",
                N("Microsoft 的營收成長率表現如何？", [_fil, _fund]) == "")
    except Exception as e:
        _assert("⑪b 生產建構子對照可執行（跑不起來＝這道閘門仍然只測合成資料）", False, repr(e))


def gate12_ratio_intent_llm() -> None:
    """⑫ ratio 意圖改由 LLM 判定之後，**詞表必須真的退位**（零 LLM、零網路）。

    治的病（BACKLOG 殘留①，2026-08-25）：`_is_ratio_intent` 是硬編碼詞表，Planner 只要把
    子問題寫成口語的「營收成長得快不快？」，整條 ratio 保底就**靜默繞過**——不是補撈失敗，
    是根本沒進到補撈。`col-11` 連跑四輪，兩輪這樣、兩輪不是，看起來像隨機退步。

    ⚠ **這道閘門真正的判別力在「LLM 說空」那幾條**，不在口語陽性那條：
      把覆寫寫成 `if fields:`（而不是 `if fields is not None:`）會讓「LLM 判定不是 ratio 題」
      的空 list 掉回詞表 → 詞表仍然是實際做決定的人，整次改動只是裝飾，而**端到端跑分看不
      出任何差別**（詞表判對的題本來就會過）。空 list 與 None 是兩件事，這裡逐條釘死。
    ⚠ fallback（None → 詞表）是刻意保留的：LLM 掛掉時行為要與舊碼**逐字相同**，
      而不是把 ratio 保底整個關掉。代價是它會遮住 LLM 的失手 → 分類準確度不能從端到端推，
      要用 `eval/probe_ratio_intent.py` 直接量。
    """
    print()
    print("⑫ ratio 意圖：LLM 判定壓過詞表")
    H, R, U = ar._has_ratio_intent, ar._resolve_ratio_fields, ar._todos_ratio_fields

    # 被修的病灶本身：詞表看不懂的口語措辭
    # ⚠ 這句**逐字取自 BACKLOG 記的 col-11 三輪實測子問題**，不要自己改寫措辭：
    #   「營收成長得快不快」含「營收成長」→ 詞表認得，拿它當陽性就測不到病灶了
    #   （第一版斷言就是這樣寫的，當場 FAIL——那是量尺錯不是系統壞）。
    _assert("⑫ 陽性：詞表判 False 的口語措辭，LLM 表過態就要成立（col-11 的形狀）",
            ar._is_ratio_intent("微軟的雲端服務最近成長得快不快？") is False
            and H(["Revenue Growth"], "微軟的雲端服務最近成長得快不快？") is True)

    # ── 誤報對照：這幾條才是判別力來源 ─────────────────────────────────────────
    _assert("⑫ 誤報對照①：LLM 判定不是 ratio 題（空 list）→ 詞表不得復活",
            H([], "Apple 的毛利率是多少？") is False
            and ar._is_ratio_intent("Apple 的毛利率是多少？") is True)
    _assert("⑫ 誤報對照②：欄位同樣以 LLM 為準，空就是空",
            R([], "Apple 的毛利率是多少？") == [])
    _assert("⑫ 誤報對照③：LLM 挑到 enum 以外的欄位一律丟掉（不可自創欄位名）",
            R(["Free Cash Flow", "Gross Margin"], "") == ["Gross Margin"]
            and R(["revenue growth"], "") == [])

    # ── fallback：None ＝ LLM 沒表態，行為必須與舊碼逐字相同 ────────────────────
    _assert("⑫ fallback：None → 退回詞表，與舊碼同行為",
            H(None, "Apple 的毛利率是多少？") is True
            and R(None, "Apple 的毛利率是多少？") == ["Gross Margin"]
            and H(None, "微軟的雲端服務最近成長得快不快？") is False)

    # ── todos 聯集 ────────────────────────────────────────────────────────────
    _assert("⑫ 聯集：沒有任何 todo 表過態 → None（＝呼叫端退回詞表）",
            U([]) is None and U([{"ratio_fields": None}, {}]) is None)
    _assert("⑫ 聯集邊界：有 todo 表態但聯集是空的 → 回 []，**不是 None**"
            "（回 None 會讓詞表在 Synthesize 端復活）",
            U([{"ratio_fields": []}]) == [])
    _assert("⑫ 聯集：去重且順序穩定（照 enum 序，方便斷言與 trace 比對）",
            U([{"ratio_fields": ["Profit Margin"]},
               {"ratio_fields": ["Gross Margin", "Profit Margin"]},
               {"ratio_fields": None}]) == ["Gross Margin", "Profit Margin"])

    # ── 端到端：兩個生產呼叫點都要吃到覆寫 ─────────────────────────────────────
    def _c(src, idx, score, content):
        return {"source": src, "chunk_index": idx, "ticker": src.split("_")[0],
                "raw_rerank_score": score, "content": content}

    C0 = _c("MSFT_Fundamentals_20260612.txt", 0, 0.918,
            "Market Cap : $2899.62B Revenue Growth (YoY): 18.30% "
            "Gross Margin : 68.31% Operating Margin: 46.33% Profit Margin : 39.34%")
    C1 = _c("MSFT_Fundamentals_20260612.txt", 1, 0.928,
            "Total Cash : $78.23B Total Debt : $125.43B Debt-Equity ... Current Ratio ...")
    FILING = _c("MSFT_10K_2026.html", 124, 0.90, "revenue increased 18% or $50.1 billion")
    got = ar._ensure_ratio_source_coverage([C1, C0, FILING], [C1, FILING], ["MSFT"],
                                           "微軟的雲端服務最近成長得快不快？",
                                           ["Revenue Growth"])
    _assert("⑫ 端到端①：口語 task ＋ LLM 覆寫 → 補撈仍認得出要哪個欄位（詞表在此解不出）",
            any(c["chunk_index"] == 0 for c in got if "Fundamentals" in c["source"]),
            [(c["source"], c["chunk_index"]) for c in got])

    N, FY = ar._basis_disclosure_notice, {"period_basis": "fiscal_year"}
    _assert("⑫ 端到端②：口語 task ＋ LLM 覆寫 → 口徑揭露要觸發（舊碼在此沉默）",
            ar._BASIS_NOTICE_MARK in N("微軟的雲端服務最近成長得快不快？", [FY], "",
                                       ["Revenue Growth"]))
    _assert("⑫ 端到端③（誤報對照）：LLM 說不是 ratio 題 → 即使問句寫著「毛利率」也不揭露",
            N("Apple 的毛利率是多少？", [FY], "", []) == "")

    # ── 解析防護：不打真的 LLM，只測 `_classify_ratio_fields` 的三種輸出 ────────
    _orig_call = ar.rq.call_llm
    try:
        ar.rq.call_llm = lambda *a, **k: '[["Revenue Growth"],[],["Bogus","Gross Margin"]]'
        _assert("⑫ 解析：合法輸出照收，且第三題的 enum 外欄位被丟掉",
                ar._classify_ratio_fields(["a", "b", "c"]) ==
                [["Revenue Growth"], [], ["Gross Margin"]])
        ar.rq.call_llm = lambda *a, **k: '[["Revenue Growth"]]'
        _assert("⑫ 解析誤報對照①：長度對不上 → 整批回 None（退回詞表），"
                "**不可**只對得上的那幾題套用（會把答案錯位到別的子問題）",
                ar._classify_ratio_fields(["a", "b"]) == [None, None])
        ar.rq.call_llm = lambda *a, **k: "抱歉，我不確定。"
        _assert("⑫ 解析誤報對照②：解不出 JSON → 回 None 退回詞表，不是回空 list",
                ar._classify_ratio_fields(["a"]) == [None])
    finally:
        ar.rq.call_llm = _orig_call


# ══════════════════════════════════════════════════════════════════════════════
# 閘門⑬：拒答不得附上「📚 引用來源」（`_compose_answer_tail`）
#
# 病灶：那段尾巴宣稱「Generator 實際依據的 chunk」,印在一份剛說自己沒有依據的答案底下就是
# **假的宣稱**。舊守門是 `answer.startswith("I don't have enough")`——只擋得住 graph 崩潰時
# 那句英文預設值。實測既有結果檔 4175 份答案／59 份拒答,**21 份帶著引用尾巴出貨**。
#
# ⚠ 判別力**不在陽性那幾條**（一個「一律不附」的實作也會全過），在 ⑬b 的**誤報對照**：
#   拒答判過頭 = 把真的有依據的答案的 provenance 砍掉,那才是危險方向。三條各鎖一種近似形狀：
#   ①「其中一項未揭露」的部分作答 ②**寫得長的誠實答案**（「只有 2023–2025、未包含 2022」,
#     這正是多年語料 before 臂 18/18 的形狀）③ 完全正常的答案。
# ⚠ ⑬a 最後一條是**回歸鎖**：舊守門唯一擋得住的那句英文預設值,改用新判準後仍然要擋得住。
# ══════════════════════════════════════════════════════════════════════════════

_REFUSAL_ZH = "我沒有足夠的資訊來回答這個問題。"
# 逐字取自 experiments/agentic/gj_hist_before_mdna.json 的 bh-02（單年 KB 對歷史題的實際拒答）。
_REFUSAL_ZH_CITED = ("很抱歉，根據提供的參考資料，未列出 Tesla 2022 年的總營收數字，"
                     "無法回答此問題。【TSLA_10K_2025.html, chunk #0】")
_REFUSAL_EN = "I don't have enough information in my knowledge base to answer this."
# 逐字取自 gj_hist_before_mdna.json 的 bh-05：**寫得長的誠實答案**,有實質作答（列出 KB 真的
# 有哪幾年）,必須留住引用清單。
_HONEST_LONG = (
    "根據提供的參考資料，Alphabet 的 10-K 僅列示 2023、2024 與 2025 年的營收金額"
    "（分別為 $307,394 百萬、$350,018 百萬與 $394,986 百萬）【GOOGL_10K_2025.html, chunk #12】，"
    "未包含 2022 年的營收資料，因此無法從這份文件得知該年度的總營收數字。"
    "若要取得 2022 年數字，需要 FY2022 或 FY2023 的年報，而目前的知識庫沒有收錄那幾份。"
)
_PARTIAL = (
    "Tesla 2026 財年第二季總營收為 $22,496 百萬【TSLA_10Q_202606.html, chunk #3】，"
    "毛利率為 17.2%【TSLA_10Q_202606.html, chunk #5】。至於分車型的交付均價，"
    "參考資料中未揭露，因此這一項無法提供。"
)
_NORMAL = "NVIDIA FY2026 資料中心營收為 $115,186 百萬【NVDA_10K_2026.html, chunk #21】。"


def gate13_refusal_no_citation_tail() -> None:
    print("\n[⑬] 拒答不得附引用清單（_compose_answer_tail）")
    chunks = [dict(c, rerank_score=0.9) for c in _chunks("TSLA_10Q_202606.html", "TSLA_10K_2025.html")]
    unmet = ["Tesla 2022 年總營收"]

    def tail(ans: str, cs=None, um=()) -> str:
        return ar._compose_answer_tail(ans, chunks if cs is None else cs, list(um))

    # ⑬a 陽性：各種拒答形狀都不得附尾巴
    _assert("⑬a 中文拒答 → 無尾巴", tail(_REFUSAL_ZH) == "", repr(tail(_REFUSAL_ZH))[:120])
    _assert("⑬a 中文拒答（自帶 inline 引用）→ 無尾巴",
            tail(_REFUSAL_ZH_CITED) == "", repr(tail(_REFUSAL_ZH_CITED))[:120])
    _assert("⑬a 回歸鎖：英文預設拒答 → 無尾巴（舊守門唯一擋得住的那句）",
            tail(_REFUSAL_EN) == "", repr(tail(_REFUSAL_EN))[:120])
    _assert("⑬a 拒答時 unmet 揭露也不附（維持舊行為）",
            tail(_REFUSAL_ZH, um=unmet) == "", repr(tail(_REFUSAL_ZH, um=unmet))[:120])
    _assert("⑬a 沒有 writer_chunks → 無尾巴", tail(_NORMAL, cs=[]) == "")

    # ⑬b 誤報對照（判別力在這裡）：有依據的答案必須留住 provenance
    for label, ans in (("完全正常的答案", _NORMAL),
                       ("部分作答＋其中一項未揭露", _PARTIAL),
                       ("寫得長的誠實答案（只有 2023–2025、未包含 2022）", _HONEST_LONG)):
        t = tail(ans)
        _assert(f"⑬b 誤報對照：{label} → 仍附引用清單",
                t.startswith("\n\n---\n📚 引用來源") and "TSLA_10Q_202606.html #0" in t,
                repr(t)[:160])

    # ⑬b unmet 揭露只在非拒答時接上,且接在引用清單之後
    t = tail(_NORMAL, um=unmet)
    _assert("⑬b 非拒答＋unmet → 引用清單在前、未涵蓋揭露在後",
            t.count("\n\n---\n") == 2 and t.index("📚") < t.index("⚠ 知識庫未涵蓋"), repr(t)[:200])

    # ⑬c 尾巴必須是 `rq.strip_evidence_tail` 認得的形狀——否則下游每個消費端都會把
    # metadata 當成答案本體算進去（`looks_like_refusal` 的長度閘就是這樣少算 8 筆的）。
    # （比 `.strip()` 後的本體：尾巴以 `\n\n---\n` 開頭而正則只吃掉一個 `\n`，會餘一個換行；
    #   所有消費端本來就 strip，這裡要鎖的是「兩塊 metadata 都被切掉、答案本體一個字不少」。）
    full = _NORMAL.rstrip() + tail(_NORMAL, um=unmet)
    _assert("⑬c 產生的尾巴切得掉（producer/consumer 同一個定義）",
            ar.rq.strip_evidence_tail(full).strip() == _NORMAL,
            repr(ar.rq.strip_evidence_tail(full))[:160])

    # ⑬d 單一定義：`eval` 端與生產端不可各有一份拒答判準
    import eval.eval_generation_llm_judge as _ejg
    _assert("⑬d eval 端的 looks_like_refusal 就是 rq 的那一個（不是複本）",
            _ejg.looks_like_refusal is ar.rq.looks_like_refusal)

    # ⑬e **拒答仍然會帶尾巴 → `looks_like_refusal` 的切尾巴不可以拿掉。**
    # ⑬a 之後很容易得出「拒答已經不附尾巴了，那個 strip 是多餘的」——**錯**。
    # `_compose_answer_tail` 只管 📚 那一塊；`_node_synthesize` 在 collected/web 皆空時走的是
    # 另一條路：拒答 ＋ `_format_unresolved_freshness_notice`（live 才有），**不經過它**。
    # 那個 ⚠ 尾巴是**該留的**（「答不出來，因為 KB 只到 X」是有用的揭露），所以要留的是尾巴，
    # 該留的也是 strip。這裡拿生產那兩個函式現場組一次，不是抄字串。
    _live_todos = [{"status": "done", "web_used": False,
                    "freshness_gaps": [{"ticker": "NVDA", "cutoff": "2026-06-12",
                                        "as_of": "2026-08-28", "doc_type": "realtime"}]}]
    _refusal = "I don't have enough information in my knowledge base to answer this."
    _notice = ar._format_unresolved_freshness_notice(_live_todos)
    _assert("⑬e 前提：live 拒答確實會被接上時效警語（沒有這個，下面兩條就沒在測東西）",
            _notice.startswith("\n\n---\n⚠"), repr(_notice[:60]))
    _assert("⑬e 拒答＋時效警語 → 仍判為拒答（＝strip_evidence_tail 有在作用）",
            ar.rq.looks_like_refusal(_refusal + _notice))
    _assert("⑬e 反向證明：不切尾巴就會漏判（長度閘被 metadata 撐爆）",
            len(ar.rq._CITE_MARK.sub("", _refusal + _notice).strip()) >= ar.rq.REFUSAL_MAX_CHARS)


# ══════════════════════════════════════════════════════════════════════════════
# 閘門⑭：Synthesize 的四道 validator 守門也要用同一個拒答判準
#
# 病灶（2026-08-28）：⑬ 修的是引用尾巴,但 `_node_synthesize` 裡**還有四道守門**寫著
# `answer.startswith("I don't have enough")`——一致性／期別／reflect／數字溯源。那是英文字面、
# 只認開頭,而 Writer 講中文 → 中文拒答整句穿得過去,於是一份剛說自己沒有依據的答案還會
# 照樣付 `_extract_claims` ＋ `_reflect_and_fix` **至少兩次 LLM 呼叫**去稽核它,而稽核對象裡
# 沒有任何可稽核的東西。⑬ 那次只改了尾巴、這四道漏改——**同一個守門寫錯,兩個消費端**。
#
# ⚠ 判別力分佈與 ⑬ 一樣但後果不同：
#   · ⑭a 是**接線鎖**,而且必須逐個 validator 驗。改三道漏一道的話,⑭b~⑭e 全部照樣綠
#     （它們測的是判準本身,不是誰在用它）。所以這裡用 AST 走訪、不是字串 grep。
#   · ⑭d 是**誤報對照＝危險方向**：判過頭 ＝ 真的有依據的答案被當成拒答 → 四道 validator
#     一次全部跳過 → 一致性／期別／數字溯源的保護同時消失。這比漏判嚴重得多。
# ══════════════════════════════════════════════════════════════════════════════

_GUARDED_VALIDATORS = ("_consistency_check_and_fix", "_period_check_and_fix",
                       "_reflect_and_fix", "_number_check_and_fix")


def _pkg_funcdefs() -> dict:
    """套件裡**每一個模組**的 top-level 函式 AST，name -> FunctionDef。

    ⚠ **為什麼不是 `ast.parse(Path(ar.__file__).read_text())`**（2026-09-03 套件化時改）：
      `agentic_rag_v2.py` 還是單一檔時，`ar.__file__` 就是全部原始碼；套件化之後它只是
      `__init__.py`，**任何被拆進子模組的函式都會查無此人**。而這兩道閘門的前提斷言
      （⑭a／⑮a）寫的是「找不到的話下面全部是假性通過」——也就是說，重構會讓它們
      **大聲失敗**（這是好的），但修法不該是「把函式搬回 __init__ 遷就量尺」，
      那是量尺與被測物耦合。改成掃整個套件，之後怎麼拆都不用再動這裡。

    ⚠ 同名函式跨模組重複時**保留先掃到的並記在 `_DUP`**：靜默覆蓋會讓斷言驗到另一個
      同名函式而完全看不出來。
    """
    import ast as _a
    import pkgutil as _pk
    import sys as _s
    from pathlib import Path as _Pa

    paths = [_Pa(ar.__file__)]
    pkg = _s.modules[ar.__name__]
    if hasattr(pkg, "__path__"):
        for mi in _pk.iter_modules(list(pkg.__path__)):
            m = _s.modules.get(f"{ar.__name__}.{mi.name}")
            f = getattr(m, "__file__", None)
            if f and _Pa(f) not in paths:
                paths.append(_Pa(f))

    out: dict = {}
    _pkg_funcdefs._DUP = []
    _pkg_funcdefs._TREES = []
    for p in paths:
        try:
            tree = _a.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        _pkg_funcdefs._TREES.append(tree)
        # ⚠ 只收 **top-level** 函式，不用 `ast.walk`：walk 會把類別的 method 與巢狀
        #   helper 一起撈進來，於是每個定義了 `__init__` 的類別都會被記成「跨模組同名」。
        #   （這條是加了重複檢查之後**當場**被誤報出來的，量尺錯不是系統錯。）
        for n in tree.body:
            if isinstance(n, _a.FunctionDef):
                if n.name in out:
                    _pkg_funcdefs._DUP.append(f"{n.name}@{p.name}")
                else:
                    out[n.name] = n
    _pkg_funcdefs._PATHS = paths
    return out


def gate14_synthesize_refusal_guards() -> None:
    print("\n[⑭] Synthesize 四道 validator 的拒答守門")
    import ast
    from pathlib import Path

    _funcs = _pkg_funcdefs()
    # ⚠ 跨模組同名函式：`_pkg_funcdefs` 保留先掃到的那個，於是後面每一條斷言都可能
    #   驗到**另一個**同名函式而完全看不出來。拆分期間這是真實風險，所以當場擋掉。
    _assert(f"⑭a 前提：套件裡沒有跨模組同名函式（掃了 {len(_pkg_funcdefs._PATHS)} 個模組）",
            not _pkg_funcdefs._DUP, f"重複：{_pkg_funcdefs._DUP[:5]}")
    fn = _funcs.get("_node_synthesize")
    _assert("⑭a 前提：找得到 `_node_synthesize`（找不到的話下面全部是假性通過）", fn is not None)
    if fn is None:
        return

    fn_src = ast.unparse(fn)
    _assert("⑭a 舊的英文字面守門已從 `_node_synthesize` 移除",
            "startswith('I don" not in fn_src and 'startswith("I don' not in fn_src)

    # 逐個 validator 驗它**確實**被 looks_like_refusal 守著。改三道漏一道要在這裡叫。
    guarded: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            nm = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if nm in _GUARDED_VALIDATORS:
                called.add(nm)
        if isinstance(node, ast.If) and "looks_like_refusal" in ast.unparse(node.test):
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(sub, ast.Call):
                    nm = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                    if nm in _GUARDED_VALIDATORS:
                        guarded.add(nm)
    _assert(f"⑭a 前提：四道 validator 都還在被呼叫（實際 {sorted(called)}）",
            called == set(_GUARDED_VALIDATORS), f"缺 {sorted(set(_GUARDED_VALIDATORS) - called)}")
    for nm in _GUARDED_VALIDATORS:
        _assert(f"⑭a `{nm}` 由 `rq.looks_like_refusal` 守門", nm in guarded,
                "這一道沒有被守到 → 拒答仍會付它的 LLM 呼叫")

    # ⑭b 回歸鎖：舊守門唯一擋得住的那句,新判準也要擋得住（同 ⑬a 末條）
    _assert("⑭b 回歸鎖：英文預設拒答仍判為拒答", ar.rq.looks_like_refusal(_REFUSAL_EN))

    # ⑭c 真陽性：舊守門漏、新守門擋得住。**兩個方向都要斷言**才證明這次修法真的改了行為
    for label, ans in (("中文拒答", _REFUSAL_ZH),
                       ("中文拒答（自帶 inline 引用）", _REFUSAL_ZH_CITED)):
        _assert(f"⑭c 前提：{label} 是舊守門漏掉的（不然這條沒在測修法）",
                not ans.startswith("I don't have enough"))
        _assert(f"⑭c {label} → 新守門擋得住（省掉至少兩次 LLM 呼叫）",
                ar.rq.looks_like_refusal(ans))

    # ⑭d **誤報對照＝危險方向**：有依據的答案被判成拒答 → 四道 validator 一次全部跳過
    for label, ans in (("完全正常的答案", _NORMAL),
                       ("部分作答＋其中一項未揭露", _PARTIAL),
                       ("寫得長的誠實答案（只有 2023–2025、未包含 2022）", _HONEST_LONG)):
        _assert(f"⑭d 誤報對照：{label} → **不**判為拒答（validator 必須照跑）",
                not ar.rq.looks_like_refusal(ans), repr(ans)[:120])

    # ⑭e 新舊判準唯一的分歧方向,鎖住它是刻意的：舊守門只認開頭,一份「以拒答句開頭、後面
    #    寫了實質內容」的答案舊版會**跳過四道 validator**,新版會照跑。方向是更安全那一邊。
    #    ⚠ 這裡的「實質內容」必須真的超過 `REFUSAL_MAX_CHARS`。第一版寫得太短（本體約 130 字）
    #      而**它本來就該被判成拒答**——那是量尺錯不是系統壞（同 CLAUDE.md「FAIL 先問是不是
    #      量尺錯」）。短的混合答案新舊判準一致,分歧只出現在**長**的那種。
    _mixed = (_REFUSAL_EN + " 不過根據提供的參考資料仍可回答其中一部分：Tesla 2026 財年第二季"
              "總營收為 $22,496 百萬【TSLA_10Q_202606.html, chunk #3】，較去年同期成長，"
              "毛利率為 17.2%【TSLA_10Q_202606.html, chunk #5】。能源儲存部署量與汽車部門的"
              "交付結構在該季報的 MD&A 中亦有揭露，可據以判斷毛利率變化的主要來源。"
              "至於分車型的交付均價，這份文件沒有拆分到那個層級，需要另外查閱投資人簡報。")
    _assert("⑭e 前提：這種答案舊守門會判成拒答（＝舊版會跳過四道 validator）",
            _mixed.startswith("I don't have enough"))
    _assert("⑭e 前提：而且它真的是實質答案（本體超過 REFUSAL_MAX_CHARS）",
            len(ar.rq._CITE_MARK.sub("", _mixed).strip()) >= ar.rq.REFUSAL_MAX_CHARS,
            f"本體長度={len(ar.rq._CITE_MARK.sub('', _mixed).strip())}")
    _assert("⑭e 以拒答句開頭但有實質內容 → 新守門**不**判為拒答,validator 照跑",
            not ar.rq.looks_like_refusal(_mixed))


# ══════════════════════════════════════════════════════════════════════════════
# 閘門⑮：期間降級揭露（Tier 2 fallback）必須從檢索層一路走到 Generator
#
# 病灶（2026-08-28 修）：`_retrieve_chunks` 寫的是 `chunks, _note = rq.retrieve(...)`——
# `rq.retrieve` 在 Tier 1 嚴格 filter 落空、降級 Tier 2 時產生的那句「知識庫沒有 X 在所詢問
# 財年（Y）的資料，以下回答改用最接近的可得期間（Z）」**被直接丟掉**。單管線兩個消費端都有
# （CLI print／SSE 顯示 ＋ 注入 generator prompt），agentic 兩半都沒有 → 「系統會不會揭露」
# 在所有 agentic 評測上**結構性恆為 0**，量到的 0 是在覆述一行程式碼。
#
# ⚠ 判別力全在**接線**，不在揭露句本身（那句話是 `rq._build_fallback_note` 產的，單管線早就
#   在用、也早就被閘門測過）。所以這一道刻意做成三段各自獨立的鎖：
#   · ⑮a 產生點：`_retrieve_chunks` 不准再把第二個回傳值丟掉（AST，抓得到 `_note` 那個形狀）
#   · ⑮b 消費點：`_write_final_answer` 的**每一個**呼叫點都要帶 `period_note=`——validator 的
#     重生成會換掉 `extra_user`，漏一處就等於「揭露只活在第一次生成」（web_extra 踩過這個坑）
#   · ⑮d/⑮e 活體：state → synthesize → Generator 真的把**值**帶過去，不是只寫了關鍵字
# ⚠ **⑮c 是誤報對照＝這次改動不該動到的東西**：period_note 為空時 user prompt 與 system
#   message 都必須逐字不變，否則 65 題既有基準會整批漂移（而且是無聲的）。
# ══════════════════════════════════════════════════════════════════════════════

_PERIOD_NOTE_CONSUMERS = ("_validate_and_fix_citations", "_consistency_check_and_fix",
                          "_period_check_and_fix", "_reflect_and_fix", "_number_check_and_fix")


def gate15_period_fallback_disclosure() -> None:
    print("\n[⑮] 期間降級揭露：檢索層 → state → Generator 的接線")
    import ast
    from pathlib import Path as _Path

    funcs = _pkg_funcdefs()

    # ── ⑮a 產生點：`_retrieve_chunks` 不准丟掉 `rq.retrieve` 的第二個回傳值
    rc = funcs.get("_retrieve_chunks")
    _assert("⑮a 前提：找得到 `_retrieve_chunks`（找不到的話下面全是假性通過）", rc is not None)
    if rc is None:
        return
    note_names: list[str] = []
    for node in ast.walk(rc):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fname = ast.unparse(node.value.func)
            if fname.endswith("retrieve") and isinstance(node.targets[0], ast.Tuple):
                elts = node.targets[0].elts
                if len(elts) >= 2 and isinstance(elts[1], ast.Name):
                    note_names.append(elts[1].id)
    _assert("⑮a 前提：`_retrieve_chunks` 裡有解包 `rq.retrieve` 的兩個回傳值",
            len(note_names) == 1, f"找到 {note_names}")
    if len(note_names) == 1:
        nm = note_names[0]
        # 舊病灶的字面形狀：拋棄名（`_` / `_note`）。這一條就是在抓那一行。
        _assert("⑮a 第二個回傳值不是拋棄名（舊碼是 `chunks, _note = ...`）",
                not nm.startswith("_"), f"目前是 {nm!r}")
        # 光是「有名字」不夠——名字沒被用到跟丟掉是同一件事。
        used = sum(1 for n in ast.walk(rc) if isinstance(n, ast.Name)
                   and n.id == nm and isinstance(n.ctx, ast.Load))
        _assert(f"⑮a 第二個回傳值 {nm!r} 在函式體裡真的被使用（不是命名了就不管）",
                used >= 1, f"Load 次數={used}")
        _assert("⑮a 它被收進 run state（五個呼叫點共用一個收集點，逐個回傳會有人漏接）",
                "period_notes" in ast.unparse(rc))

    # ── ⑮b 消費點：每一個 `_write_final_answer` 呼叫都要帶 period_note
    #    ⚠ 這裡不能只驗 synthesize 那一個：validator 的重生成各自呼叫一次，漏一處的症狀是
    #      「揭露只活在第一次生成」——只要 validator 觸發過一次就整段消失，而且不會報錯。
    missing: list[str] = []
    seen_callers: set[str] = set()
    for fname, fn in funcs.items():
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) != "_write_final_answer":
                continue
            seen_callers.add(fname)
            if not any(kw.arg == "period_note" for kw in node.keywords):
                missing.append(f"{fname}:{node.lineno}")
    _assert("⑮b 前提：五道 validator ＋ synthesize 都還在呼叫 `_write_final_answer`",
            set(_PERIOD_NOTE_CONSUMERS + ("_node_synthesize",)) <= seen_callers,
            f"缺 {sorted(set(_PERIOD_NOTE_CONSUMERS + ('_node_synthesize',)) - seen_callers)}")
    _assert("⑮b 每一個 `_write_final_answer` 呼叫都帶了 `period_note=`（漏一處＝揭露只活到第一次重寫）",
            not missing, f"沒帶的：{missing}")
    # 五道 validator 內部還會再過一次 citation 稽核，那一條路也要帶（同一個理由）
    revalidate_missing = [
        f"{fname}:{node.lineno}"
        for fname in _PERIOD_NOTE_CONSUMERS[1:] for node in ast.walk(funcs[fname])
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "_validate_and_fix_citations"
        and not any(kw.arg == "period_note" for kw in node.keywords)
    ]
    _assert("⑮b validator 內部回頭呼叫 citation 稽核時也帶著 `period_note=`",
            not revalidate_missing, f"沒帶的：{revalidate_missing}")

    # ── ⑮c 誤報對照：沒有揭露時，prompt 必須逐字不變（否則 65 題基準會無聲漂移）
    _c = [{"source": "MSFT_10K_2026.html", "chunk_index": 3, "content": "Revenue increased 18%."}]
    _assert("⑮c 空揭露 → user prompt 與「完全不傳這個參數」逐字相同",
            ar.rq.build_user_prompt("Q", _c, "") == ar.rq.build_user_prompt("Q", _c))

    seen: dict[str, str] = {}
    _orig_llm = ar.rq.call_llm

    def _spy(messages, model_name, temperature=0.0, **kw):
        seen["system"], seen["user"] = messages[0]["content"], messages[1]["content"]
        return "測試答案【MSFT_10K_2026.html, chunk #3】"

    note = ar.rq._build_fallback_note(          # ⚠ 用**生產的建構路徑**產這句話，不是自己編一句
        [{"field": "fiscal_year", "polarity": "include", "value": "2021"},
         {"field": "ticker", "polarity": "include", "value": "MSFT"}],
        [type("P", (), {"payload": {"fiscal_year": "2026", "report_period_code": "2026"}})()],
    )
    _assert("⑮c 前提：生產的 `_build_fallback_note` 對「問了 KB 沒有的財年」真的產得出揭露句",
            bool(note), f"note={note!r}")
    try:
        ar.rq.call_llm = _spy
        ar._write_final_answer("Q", _c, "m", period_note=note)
        with_sys, with_user = seen["system"], seen["user"]
        ar._write_final_answer("Q", _c, "m")
        without_sys, without_user = seen["system"], seen["user"]
    finally:
        ar.rq.call_llm = _orig_llm
    _assert("⑮c system message 逐字不變（揭露是這一題的檢索事實，不是契約修訂）",
            with_sys == without_sys)
    _assert("⑮d 揭露句真的出現在送給 Generator 的 user prompt 裡",
            bool(note) and note in with_user)
    _assert("⑮d 且走的是 `build_user_prompt` 的既有模板（與單管線同一個位置、同一句指示）",
            "資料期間提示" in with_user and "資料期間提示" not in without_user)

    # ── ⑮e 端到端接線（值真的從 state 流到 Generator，不是只寫了關鍵字）
    #    ⚠ 這條與 ⑮b 不可互相取代：⑮b 驗「呼叫點寫了 period_note=」，它照樣可能傳一個永遠是空的
    #      區域變數；⑮e 驗「state 裡的值真的到得了」。⑧g／⑭a 是同一個教訓。
    _N1, _N2 = "揭露句甲", "揭露句乙"
    _orig = {k: getattr(ar, k) for k in
             ("_write_final_answer", "_validate_and_fix_citations", "_consistency_check_and_fix",
              "_period_check_and_fix", "_reflect_and_fix", "_number_check_and_fix", "_run_one_todo")}
    got: dict[str, str] = {}
    try:
        def _fake_write(query, chunks, model_name, extra_user="", web_extra="", period_note=""):
            got["period_note"] = period_note
            return "答案【MSFT_10K_2026.html, chunk #3】"
        ar._write_final_answer = _fake_write
        for _k in _PERIOD_NOTE_CONSUMERS:
            setattr(ar, _k, lambda *a, **k: a[1])
        out = ar._node_synthesize({"query": "Q", "collected": _c, "web_notes": [],
                                   "period_notes": [_N1, _N2], "todos": [],
                                   "freshness_mode": ar.FRESHNESS_SNAPSHOT})
        _assert("⑮e synthesize 把 state['period_notes'] 全部交給 Generator",
                got.get("period_note") == f"{_N1}\n{_N2}", f"實際={got.get('period_note')!r}")
        _assert("⑮e 揭露**不**塞進答案文字（顯示由呼叫端負責，答案文字不動＝既有結果檔可比）",
                _N1 not in out.get("answer", ""))

        # execute 這一段：`_run_one_todo` 的 period_notes 要併進 state，且跨子問題去重保序
        def _fake_todo(todo, freshness_mode, verbose):
            return {"id": todo["id"], "summary": "s", "web_used": False, "web_notes": [],
                    "picked": [], "freshness_gaps": [],
                    "period_notes": [_N1] if todo["id"] == 0 else [_N1, _N2]}
        ar._run_one_todo = _fake_todo
        st = ar._node_execute({
            "query": "Q", "freshness_mode": ar.FRESHNESS_SNAPSHOT, "collected": [],
            "web_notes": [], "period_notes": [], "iterations": 0,
            "todos": [{"id": i, "task": f"子問題{i}", "temporal_scope": "", "status": "pending",
                       "result": "", "web_used": False, "freshness_gaps": [], "period_notes": []}
                      for i in (0, 1)],
        })
        _assert("⑮e execute 把子問題的 period_notes 併進 state，且跨子問題去重保序",
                st.get("period_notes") == [_N1, _N2], f"實際={st.get('period_notes')!r}")
        _assert("⑮e 每個 todo 也各自留著自己的 period_notes（供逐子問題診斷）",
                [t.get("period_notes") for t in st["todos"]] == [[_N1], [_N1, _N2]])
    finally:
        for k, v in _orig.items():
            setattr(ar, k, v)

    # ── ⑮f 歸因：揭露句只能來自「承載使用者意圖」的 query（2026-08-28，col-10 實測）
    #    ⚠ 這是 ⑮ 裡唯一在問「這句話**該不該說**」的一格，⑮a~⑮e 全部只問「有沒有接上」。
    #      沒有它，一個把 Grader 補救改寫的假前提照樣往外送的實作會全綠——而實測就是那樣上線的：
    #      使用者問「微軟每年能自由運用的現金大概有多少？」、Planner 分解成「Microsoft 每年的自由
    #      現金流大概是多少」（**兩者都沒有年份、沒有 10-K**），揭露句卻寫「知識庫沒有「MSFT 10-K」
    #      在所詢問財年（2022）的資料」。那個 2022 只可能來自 Grader 的 targeted rewrite。
    _kwonly = [a.arg for a in rc.args.kwonlyargs]
    _nodefault = [a.arg for a, d in zip(rc.args.kwonlyargs, rc.args.kw_defaults) if d is None]
    _assert("⑮f1 `_retrieve_chunks` 有 keyword-only 參數 `attributable`",
            "attributable" in _kwonly, f"kwonly={_kwonly}")
    _assert("⑮f1 且它**沒有預設值**（有預設＝日後新增的呼叫點會靜默沿用可歸因，col-10 復活）",
            "attributable" in _nodefault, f"無預設的={_nodefault}")

    # ⑮f2 逐個呼叫點驗（同 ⑮b／⑭a 的形狀：改三處漏一處要叫得出來）
    # ⚠ 2026-09-01：確定性補救迴圈改成經由 `_dispatch_todo` 叫檢索器（route 分派），
    #   所以「帶 attributable 的呼叫點」現在**兩種函式都算**。少了 `_dispatch_todo`
    #   這一半，⑮f3 會找不到迴圈那一處而變成前提失敗——那是錨點過期不是系統壞掉。
    # ⚠ 2026-09-03 套件化：呼叫點可能落在**任何**子模組，所以掃整個套件而不是單一 tree。
    _calls = [n for _t in _pkg_funcdefs._TREES for n in ast.walk(_t) if isinstance(n, ast.Call)
              and (ast.unparse(n.func).endswith("_retrieve_chunks")
                   or ast.unparse(n.func).endswith("_dispatch_todo"))]
    _assert("⑮f2 前提：找得到 `_retrieve_chunks` 的呼叫點（找不到＝下面全是假性通過）",
            len(_calls) >= 4, f"共 {len(_calls)} 處")
    _no_kw = [ast.unparse(c)[:70] for c in _calls
              if not any(k.arg == "attributable" for k in c.keywords)]
    _assert("⑮f2 每一個呼叫點都明確傳了 `attributable=`（漏一處＝那條路仍會吐假前提）",
            not _no_kw, f"漏掉：{_no_kw}")

    # ⑮f3 判別力所在：補救迴圈那一處**不可以是常數**。
    #     `attributable=True` 寫死在迴圈裡，f1/f2/f4 照樣全綠，而 col-10 原封不動地回來。
    # `_dispatch_todo(route, active_query, …)` → active_query 是**第二個**位置引數。
    _loop = [c for c in _calls
             if any(ast.unparse(a) == "active_query" for a in c.args[:2])]
    _assert("⑮f3 前提：找得到確定性補救迴圈的檢索呼叫（引數裡有 active_query）",
            len(_loop) == 1, f"找到 {len(_loop)} 處")
    if len(_loop) == 1:
        _v = next(k.value for k in _loop[0].keywords if k.arg == "attributable")
        _assert("⑮f3 補救迴圈的 `attributable` 是輪次條件而非常數（寫死 True＝col-10 復活）",
                not isinstance(_v, ast.Constant), f"實際={ast.unparse(_v)}")
        _assert("⑮f3 且那個條件讀的是輪次變數 `rnd`",
                "rnd" in ast.unparse(_v), f"實際={ast.unparse(_v)}")

    # ⑮f4 行為（不是接線）：只驗接線的話，一個「一律丟棄」的實作也會全綠 → 兩個方向都要驗。
    #     `_get_models` 一併換樁：這支閘門要維持秒級，不能為了兩條斷言載入 BGE-M3 ＋ reranker。
    _o4 = {"retrieve": ar.rq.retrieve, "get_models": ar._get_models}

    def _stub4(query, *a, **kw):
        return ([], f"知識庫沒有「T」在所詢問財年（{query}）的資料，以下回答改用最接近的可得期間（2026）。")

    try:
        ar.rq.retrieve = _stub4
        ar._get_models = lambda: (None, None, None)
        ar._reset_run_pool()
        ar._retrieve_chunks("UNATTR", attributable=False)
        _unattr = list(ar._current_run_state().period_notes)
        ar._retrieve_chunks("ATTR", attributable=True)
        _attr = list(ar._current_run_state().period_notes)
    finally:
        ar.rq.retrieve = _o4["retrieve"]
        ar._get_models = _o4["get_models"]
    _assert("⑮f4 不可歸因的揭露句不得進 run state（col-10 的直接斷言）",
            _unattr == [], f"實際={_unattr}")
    _assert("⑮f4 誤報對照：可歸因的揭露句仍然進得去（否則是把功能關掉，不是修好）",
            len(_attr) == 1 and "ATTR" in _attr[0], f"實際={_attr}")

    # ⑮f5 端到端（零 LLM、零 Qdrant）：跑**真的** `_run_executor_deterministic`，只換掉 LLM 與檢索。
    #     ⚠ 與 f3／f4 不可互相取代：f4 直接呼叫 `_retrieve_chunks`，證不到「補救迴圈真的傳了 False」；
    #       f3 只讀 AST，看不出 `rnd` 的語意被改掉（例如迴圈改成從 1 起算）。f5 是唯一同時踩到兩者的。
    _o5 = {k: getattr(ar, k) for k in ("_check_sufficiency", "_fallback_local_summary", "_get_models")}
    _o5r = ar.rq.retrieve
    _seen: list[str] = []

    def _stub5(query, *a, **kw):
        _seen.append(query)
        return ([], f"NOTE::{query}")

    def _stub_check(subquery, pool, temporal_scope="", freshness_mode="", *a, **kw):
        # 第一輪判不足，並給一個「憑空加了年份與 filing type」的 targeted rewrite＝col-10 的形狀
        if len(_seen) <= 1:
            return {"sufficient": False, "missing": "缺年份",
                    "new_query": "Microsoft FY2022 free cash flow 10-K",
                    "relevant_ids": [], "realtime_need": "none"}
        return {"sufficient": True, "missing": "", "new_query": "",
                "relevant_ids": [], "realtime_need": "none"}

    _task = "Microsoft 每年的自由現金流大概是多少"
    try:
        ar.rq.retrieve = _stub5
        ar._get_models = lambda: (None, None, None)
        ar._check_sufficiency = _stub_check
        ar._fallback_local_summary = lambda task, chunks, period_note="": "摘要"
        _pn = ar._run_executor_deterministic(_task, "", ar.FRESHNESS_SNAPSHOT, 0, False,
                                            attributable=True)[3]
    finally:
        ar.rq.retrieve = _o5r
        for k, v in _o5.items():
            setattr(ar, k, v)
    _assert("⑮f5 前提：樁真的驅動了兩輪（第一輪 Planner 原句、第二輪 Grader 改寫）",
            len(_seen) == 2 and "FY2022" in _seen[1], f"實際={_seen}")
    _assert("⑮f5 只有 Planner 子問題那一輪的揭露句逸出，Grader 改寫那輪的被丟掉",
            _pn == [f"NOTE::{_task}"], f"實際={_pn}")

    # ── ⑮g 歸因掛在 todo 的出身上，不是從輪次推（2026-08-28）
    #    ⚠ ⑮f 只擋得住「Grader 在同一個子問題內改寫」。**replanner 另外加一個 todo** 時，那個
    #      todo 的 rnd 0 照樣是「第一輪」→ `attributable=(rnd == 0)` 為真 → col-10 的假前提從
    #      另一扇門原封不動地回來，而 ⑮f 全綠（它驗的條件確實還在）。這一格就是那扇門。
    #      實測 replanner 會生出「改用網路搜尋查…」→「即時網路搜尋…」這種同義待辦串，
    #      裡面夾帶的年份／filing type 都是機器腦補的（見 agentic_rag_version/__init__.py `_node_replan`）。
    _appends = []          # (所在函式, dict 節點)
    for _fname in ("_node_plan", "_node_replan"):
        _fn = funcs.get(_fname)
        if _fn is None:
            continue
        for _n in ast.walk(_fn):
            if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
                    and _n.func.attr == "append" and _n.args
                    and isinstance(_n.args[0], ast.Dict)):
                _appends.append((_fname, _n.args[0]))
    _assert("⑮g 前提：`_node_plan`／`_node_replan` 各找得到一個建 todo 的 dict",
            sorted(f for f, _ in _appends) == ["_node_plan", "_node_replan"],
            f"找到 {[f for f, _ in _appends]}")

    def _dict_const(d, key):
        for k, v in zip(d.keys, d.values):
            if isinstance(k, ast.Constant) and k.value == key:
                return v
        return None

    _vals = {}
    for _fname, _d in _appends:
        _v = _dict_const(_d, "attributable")
        _vals[_fname] = (_v.value if isinstance(_v, ast.Constant) else
                         (ast.unparse(_v) if _v is not None else None))
    _assert("⑮g1 Planner 建的 todo 標 `attributable: True`（分解是使用者意圖的重述）",
            _vals.get("_node_plan") is True, f"實際={_vals.get('_node_plan')!r}")
    _assert("⑮g2 Replanner 建的 todo 標 `attributable: False`（機器對 missing 的反應）",
            _vals.get("_node_replan") is False, f"實際={_vals.get('_node_replan')!r}")

    # ⑮g3 三個 executor 入口的 `attributable` 都必須是**必填** keyword-only。
    #     ⚠ 只要有一個給了預設值，那條路就會靜默沿用它，而 g1/g2 照樣全綠。
    for _fname in ("_run_executor", "_run_executor_react", "_run_executor_deterministic"):
        _fn = funcs.get(_fname)
        _names = [a.arg for a in _fn.args.kwonlyargs] if _fn else []
        _nodef = [a.arg for a, d in zip(_fn.args.kwonlyargs, _fn.args.kw_defaults)
                  if d is None] if _fn else []
        _assert(f"⑮g3 `{_fname}` 的 `attributable` 是必填 keyword-only（無預設）",
                "attributable" in _nodef, f"kwonly={_names} 無預設={_nodef}")

    # ⑮g4 `_run_one_todo` 必須用下標讀，不能 `.get(..., True)`——給預設＝新的 todo 建立點
    #     會靜默沿用「可歸因」，那正是這一格要防的東西。
    _rot = funcs.get("_run_one_todo")
    _rot_src = ast.unparse(_rot) if _rot else ""
    _assert("⑮g4 `_run_one_todo` 用 `todo['attributable']` 下標讀（漏設要當場 KeyError）",
            "todo['attributable']" in _rot_src and "'attributable'," not in _rot_src.replace(
                "todo['attributable']", ""),
            f"片段={[l for l in _rot_src.splitlines() if 'attributable' in l]}")

    # ⑮g5 值流：不是「寫了關鍵字」而是「值真的從 todo 走到 executor」（同 ⑮e／⑧g 的教訓）
    _seen_attr: list = []
    _seen_route: list = []
    _o6 = {"_run_executor": ar._run_executor}

    def _todo(flag=True, **extra):
        d = {"id": 0, "task": "某個沒有公司名的子問題", "temporal_scope": "",
             "attributable": flag, "freshness_gaps": [], "period_notes": [],
             "web_used": False, "status": "pending", "result": ""}
        d.update(extra)
        return d

    try:
        ar._run_executor = (lambda task, scope, fm, idx, verbose, *, attributable, route="kb":
                            (_seen_attr.append(attributable), _seen_route.append(route),
                             ("摘要", [], "none", []))[2])
        for _flag in (True, False):
            ar._reset_run_pool()
            ar._run_one_todo(_todo(_flag), ar.FRESHNESS_SNAPSHOT, False)
        _attr_flow = list(_seen_attr)          # ⚠ 先凍結：下面的 route 測試會再跑幾次 _run_one_todo
        # ⑮g6 **值流（route）**：同 ⑮e／⑮g5 的教訓——「呼叫點有沒有寫 `route=`」與「值有沒有
        #     真的從 todo 走到 executor」是兩件事。少了這條，一個 `route="kb"` 寫死的呼叫點
        #     照樣全綠，而 Planner 判的路由**一格都不會生效**。
        _seen_route.clear()
        for _r in ("web", "both", "kb"):
            ar._reset_run_pool()
            ar._run_one_todo(_todo(True, route=_r), ar.FRESHNESS_SNAPSHOT, False)
        _route_flow = list(_seen_route)
        # ⑮g7 誤報對照：**沒有 route 的舊 todo**（`_node_replan` 這一版還沒補）必須落到 "kb"
        #     ＝與加 route 之前逐字相同的行為，而不是 KeyError 把整個子問題打掉。
        _seen_route.clear()
        ar._reset_run_pool()
        ar._run_one_todo(_todo(True), ar.FRESHNESS_SNAPSHOT, False)
        _route_missing = list(_seen_route)
    finally:
        for k, v in _o6.items():
            setattr(ar, k, v)
    _assert("⑮g5 todo 的 `attributable` 真的流到 executor（不是只寫了關鍵字）",
            _attr_flow == [True, False], f"實際={_attr_flow}")
    _assert(f"⑮g6 todo 的 route **值**真的流到 executor（實得 {_route_flow}）",
            _route_flow == ["web", "both", "kb"])
    _assert(f"⑮g7 誤報對照：todo 沒有 route 時退回 'kb'（實得 {_route_missing}）",
            _route_missing == ["kb"])

    # ⑮g6 行為 ＋ 誤報對照：replan 出身的 todo，連第一輪的揭露句都不得逸出；
    #     Planner 出身的仍然要逸出（否則就是把功能關掉而不是修好）。
    _o7 = {k: getattr(ar, k) for k in
           ("_check_sufficiency", "_fallback_local_summary", "_get_models")}
    _o7r = ar.rq.retrieve
    _out: dict = {}
    try:
        ar.rq.retrieve = lambda q, *a, **kw: ([], f"NOTE::{q}")
        ar._get_models = lambda: (None, None, None)
        ar._check_sufficiency = lambda sq, pool, ts="", fm="", *a, **kw: {
            "sufficient": True, "missing": "", "new_query": "",
            "relevant_ids": [], "realtime_need": "none"}
        ar._fallback_local_summary = lambda task, chunks, period_note="": "摘要"
        for _flag in (True, False):
            _out[_flag] = ar._run_executor_deterministic(
                "某個子問題", "", ar.FRESHNESS_SNAPSHOT, 0, False, attributable=_flag)[3]
    finally:
        ar.rq.retrieve = _o7r
        for k, v in _o7.items():
            setattr(ar, k, v)
    _assert("⑮g6 replan 出身的 todo：第一輪的揭露句也不得逸出",
            _out.get(False) == [], f"實際={_out.get(False)}")
    _assert("⑮g6 誤報對照：Planner 出身的 todo 第一輪仍然逸出（沒有被整個關掉）",
            _out.get(True) == ["NOTE::某個子問題"], f"實際={_out.get(True)}")


# ══════════════════════════════════════════════════════════════════════════════
def gate16_round0_query_is_verbatim() -> None:
    """閘門⑯：**rnd 0 檢索用的 query 與子問題逐字相同，且只有 rnd 0 算可歸因**。

    **為什麼是斷言而不是註解**：`attributable` 的整個保證建立在「rnd 0 問的就是使用者問的」
    這個不變量上——`_retrieve_chunks(active_query, attributable=(attributable and rnd == 0))`。
    閘門⑮f/⑮g 驗的是「揭露句該不該說『所詢問財年』」，而**那個判斷的前提就是這裡**：
    只要有誰讓 rnd 0 先改寫一次 query，⑮ 全綠而前提已經崩了。

    **這支現在是綠的，寫它是為了接下來的重構**（路由改成 todo 的 `route` 欄位，
    見 [`BACKLOG.md`](../BACKLOG.md)）：那次會動 `_run_executor_deterministic` 的迴圈頭，
    而「rnd 0 逐字」是那次改動**最容易順手弄丟**的一格。

    ⚠ **刻意是行為測試不是 AST**：AST 只看得到「有沒有寫 `active_query = task`」，
      看不到「第一次真的送出去的是哪個字串」。實測相近的坑：⑮b 只驗了呼叫點寫了
      `period_note=`，而那照樣可能傳一個永遠是空的變數（⑮e 才補上）。

    ⚠ **判別力在 ⑯c/⑯d 兩條誤報對照**：只驗「第一次是 task」的話，一個「**每一輪都用 task**、
      完全不理 Grader 改寫」的實作也會通過——而那會讓 MAX_REWRITES 整個變成空轉。
    """
    print("\n[gate16] rnd 0 的 query 逐字 ＋ attributable 只在 rnd 0")

    def _trace_rounds(attributable: bool, task: str = "Apple FY2025 的營收是多少"):
        """跑真實的 `_run_executor_deterministic`，側錄每一輪送進檢索器的 (query, attributable)。
        兩個 tool 與摘要都換成樁 → 零 LLM、零網路、零 Qdrant。"""
        seen: list[tuple[str, bool]] = []
        saved = (ar._retrieve_chunks, ar._check_sufficiency,
                 ar._tavily_search, ar._fallback_local_summary)

        def _fake_retrieve(q, *, attributable):
            seen.append((q, attributable))
            # ⚠ `raw_rerank_score` 不可省：`_merge_chunks` 拿它排序,少了會 KeyError,
            #   而那個例外會被 `_run_executor_deterministic` 的 `except Exception` **吞掉**
            #   → 迴圈只跑一輪就走降級路徑,⑯c/⑯d 量到的是降級行為不是受測行為。
            #   （這正是 CLAUDE.md 說的「量尺自備輸入」那個坑：樁要長得像生產的產物。）
            return [{"source": "AAPL_10K_2025.html", "chunk_index": 0,
                     "content": "stub", "ticker": "AAPL", "raw_rerank_score": 0.9}]

        ar._retrieve_chunks = _fake_retrieve
        # 永遠判不足 → 迴圈會跑滿 MAX_REWRITES+1 輪,才看得到「第二輪換了沒」。
        ar._check_sufficiency = lambda *a, **k: {
            "sufficient": False, "missing": "缺 FY2025", "new_query": "REWRITTEN_BY_GRADER",
            "relevant_ids": [], "realtime_need": "none", "kb_unfixable": False}
        ar._tavily_search = lambda q, need="none": "（stub）"
        ar._fallback_local_summary = lambda task_, chunks: "（stub 摘要）"
        try:
            ar._run_executor_deterministic(
                task, "", ar.FRESHNESS_SNAPSHOT, 0, False, attributable=attributable)
        finally:
            (ar._retrieve_chunks, ar._check_sufficiency,
             ar._tavily_search, ar._fallback_local_summary) = saved
        return seen

    TASK = "Apple FY2025 的營收是多少"
    rounds = _trace_rounds(True, TASK)

    _assert("⑯a 至少跑了 2 輪（否則後面的誤報對照全部沒有判別力）",
            len(rounds) >= 2, f"實得 {len(rounds)} 輪：{rounds!r}")
    _assert("⑯b rnd 0 送進檢索器的 query 與子問題**逐字**相同",
            bool(rounds) and rounds[0][0] == TASK,
            f"實得 {rounds[0][0]!r} != {TASK!r}（rnd 0 一旦被改寫,⑮ 的『所詢問財年』就成了假前提）")
    _assert("⑯c **誤報對照**：rnd 1 必須換成 Grader 的 new_query",
            len(rounds) >= 2 and rounds[1][0] == "REWRITTEN_BY_GRADER",
            f"實得 {(rounds[1][0] if len(rounds) >= 2 else '（沒有第二輪）')!r}；"
            "每輪都用 task ＝ MAX_REWRITES 整個空轉,而 ⑯b 照樣全綠")
    _assert("⑯d rnd 0 是 attributable=True，其餘輪一律 False",
            bool(rounds) and rounds[0][1] is True
            and all(a is False for _, a in rounds[1:]),
            f"實得 {[a for _, a in rounds]}")

    rounds_no = _trace_rounds(False, TASK)
    _assert("⑯e **誤報對照**：todo 本身不可歸因時，連 rnd 0 也必須是 False",
            bool(rounds_no) and all(a is False for _, a in rounds_no),
            f"實得 {[a for _, a in rounds_no]}；只看 `rnd == 0` 而漏掉 `attributable and` "
            "＝ replan 造的 todo 也會宣稱『所詢問財年』（⑮g 治的正是這個病）")
    _assert("⑯f 不可歸因時 rnd 0 的 query 仍然逐字（歸因與改寫是兩件事）",
            bool(rounds_no) and rounds_no[0][0] == TASK, f"實得 {rounds_no[0][0]!r}")


def gate17_dual_source_timepoints() -> None:
    """閘門⑰：R5「並陳必須帶時點」（`find_undated_dual_sourcing`）。

    **它治的病**（2026-09-02 實測）：`web-01` 同一份 fixture 三輪重放 PASS／**FAIL**／PASS，
    失敗那輪引用了 web 的 $4.75 兆卻**完全沒給時點**，還寫「兩者皆屬於同一時間段的不同來源」
    ——六月的 KB 快照與九月的 web 值不是同一時間段。R4 抓不到它，因為 R4 的 bucket key 含
    `unit`，而 `$4.75 兆` 與 `$4342.02 billion` 落到不同桶。

    ⚠ **判別力幾乎全在誤報對照**（⑰c~⑰g），陽性那幾條一個「一律發話」的實作也會通過。
      三條是**乾跑實際踩出來的**，各自逐字凍結：
        · **⑰c** U+2011 不換行連字號：`2026‑09‑01` 看起來與 ASCII 版一樣但 `[-/年]` 匹配不到。
          第一版在 281 份答案裡的 4 個觸發有 **3 個是這個字元造成的誤報**。
        · **⑰f** 新聞敘述引用：第一版只要求「同時引用 web 與財報就要有時點」，觸發率
          **34/123（28%）**，絕大多數是這一型。現在要求兩邊都出現**可比較的金額**。
        · **⑰g** 量級差 >10 倍 ＝ 不是同一個量（$25.56B 的自由現金流 vs $4.34T 的市值）。
    """
    print("\n── 閘門⑰：R5 並陳必須帶時點 ─────────────────────────────")
    f = ar.find_undated_dual_sourcing

    # ⑰a 真陽性：web_replay_r6.json 的 web-01，**逐字**。
    R6 = ("Apple 目前的市值約為 **47,500 億美元（$4.75 trillion）**（即 47,500 億美元"
          "（$4.75 兆））【web: https://stockanalysis.com/stocks/aapl/market-cap】。\n\n"
          "作為參考，Apple 在 2026\u2011 06\u201112 的公司基本面資料中列示的市值為 "
          "**43,420.2 億美元（$4342.02 billion）**【AAPL_Fundamentals_20260612.txt, chunk #0】。")
    _assert("⑰a 真陽性：web 金額與財報金額並列，web 那句沒有任何日期 → 發話",
            any("網路那一邊" in p for p in f(R6)), f"實得 {f(R6)}")

    # ⑰b 誤報對照：同一題答對的那一輪（r5），兩邊都有時點 → 沉默。
    R5 = ("Apple 目前的市值約為 **47,500 億美元（$4.75 兆）**，此數據來自 2026 年 9 月 1 日的"
          "最新報告【web: https://stockanalysis.com/stocks/aapl/market-cap】。\n\n"
          "（供參考：根據 2026-06-12 的公司基本面資料，Apple 的市值為 43,420.2 億美元"
          "（$4342.02 billion）【AAPL_Fundamentals_20260612.txt, chunk #0】。）")
    _assert("⑰b 誤報對照：兩邊都標了時點 → 沉默", f(R5) == [], f"實得 {f(R5)}")

    # ⑰c 誤報對照（**乾跑踩出來的**）：日期用 U+2011 不換行連字號寫，必須照樣算數。
    NB = ("Apple 目前的市值大約是 44,600 億美元（$4.46 trillion），依據 StockAnalysis.com，"
          "Apple 在 2026\u201108\u201114 的市值為 **44,600 億美元（$4.46 trillion）**"
          "【web: https://stockanalysis.com/stocks/aapl/market-cap】。\n"
          "內部基本面資料（截至 2026\u201106\u201112）列出市值 **43,420.2 億美元"
          "（$4,342.02 billion）**【AAPL_Fundamentals_20260612.txt, chunk #0】。")
    _assert("⑰c 誤報對照：U+2011 連字號寫的日期照樣算時點（第一版 3/4 的誤報來自這個字元）",
            f(NB) == [], f"實得 {f(NB)}")

    # ⑰d/⑰e 誤報對照：沒有並陳就沒有意見。
    WEB_ONLY = ("Apple 目前的市值約為 **47,500 億美元（$4.75 trillion）**"
                "【web: https://stockanalysis.com/stocks/aapl/market-cap】。")
    KB_ONLY = ("Apple 的市值為 **43,420.2 億美元（$4342.02 billion）**"
               "【AAPL_Fundamentals_20260612.txt, chunk #0】。")
    _assert("⑰d 誤報對照：只有 web 引用（沒有財報）→ 沉默", f(WEB_ONLY) == [], f"實得 {f(WEB_ONLY)}")
    _assert("⑰e 誤報對照：只有財報引用（沒有 web）→ 沉默", f(KB_ONLY) == [], f"實得 {f(KB_ONLY)}")

    # ⑰f 誤報對照（**乾跑踩出來的**）：新聞敘述型引用。第一版在這裡誤報，觸發率 28%。
    NEWS = ("多篇報導指出，儘管股價近期下跌，華爾街仍維持「Strong Buy」共識"
            "【web: https://finance.yahoo.com/markets/stocks/articles/meta-stock-sinks-32.html】。\n"
            "為支撐 AI 需求，META 正在裁減約 8,000 個職位、凍結約 6,000 個新職缺"
            "【web: https://finance.yahoo.com/markets/stocks/articles/meta-ai.html】。\n"
            "公司的自由現金流達 255.6 億美元（$25.56 billion）"
            "【META_Fundamentals_20260612.txt, chunk #1】。")
    _assert("⑰f 誤報對照：web 那半是新聞敘述、沒有可比較的金額 → 沉默（第一版在這裡 28% 誤報）",
            f(NEWS) == [], f"實得 {f(NEWS)}")

    # ⑰g 誤報對照：兩個金額量級差超過 10 倍 ＝ 不是同一個量。
    FAR = ("Apple 的市值約為 **47,500 億美元（$4.75 trillion）**"
           "【web: https://stockanalysis.com/stocks/aapl/market-cap】。\n"
           "其季度研發費用為 **86 億美元（$8.6 billion）**"
           "【AAPL_10Q_202606.html, chunk #12】。")
    _assert("⑰g 誤報對照：兩個金額差超過 10 倍（市值 vs 研發費用）→ 不是同一個量 → 沉默",
            f(FAR) == [], f"實得 {f(FAR)}")

    # ⑰h 同句規則：日期在**別的句子**不算——那正是要抓的病（KB 的日期替 web 值背書）。
    CROSS = ("截至 2026-06-12 的資料如下。\n"
             "Apple 目前的市值約為 **47,500 億美元（$4.75 trillion）**"
             "【web: https://stockanalysis.com/stocks/aapl/market-cap】。\n"
             "財報值為 **43,420.2 億美元（$4342.02 billion）**"
             "【AAPL_Fundamentals_20260612.txt, chunk #0】。")
    _assert("⑰h 同句規則：日期在別句不算（整篇比對會讓一個日期替所有來源背書）",
            any("網路那一邊" in p for p in f(CROSS)), f"實得 {f(CROSS)}")

    # ⑰i KB 那半可以靠**檔名的期別戳**滿足——不然這條規則會在幾乎每份答案上觸發＝變成常數。
    # ⚠ **這條的測資必須讓財報那句「只有檔名戳、沒有日曆日期」**：第一版拿 ⑰a 的 R6 來測，
    #   而 R6 的財報句裡本來就寫著 `2026-06-12` → 它是靠日曆日期通過的，檔名戳那條路
    #   **從來沒被測到**。變異測試當場證實：把 `_FISCAL_MARK_RE` 從 KB 判斷拿掉，全綠。
    STAMP_ONLY = ("Apple 目前的市值約為 **47,500 億美元（$4.75 trillion）**（截至 2026-09-01）"
                  "【web: https://stockanalysis.com/stocks/aapl/market-cap】。\n"
                  "財報列示的市值為 **43,420.2 億美元（$4342.02 billion）**"
                  "【AAPL_Fundamentals_20260612.txt, chunk #0】。")
    _assert("⑰i 財報那半只有檔名期別戳（`_20260612`）、沒有日曆日期 → 仍算交代了期間",
            f(STAMP_ONLY) == [], f"實得 {f(STAMP_ONLY)}")

    # ⑰j 接線：真的被 `_node_synthesize` 呼叫，而且**在數字溯源之後**（管線最末）。
    #    ⚠ 順序不是裝飾：時點是措辭，前面每一道的重生成都可能改掉它。
    src = inspect.getsource(ar._node_synthesize)
    i_num = src.find("_number_check_and_fix")
    i_dual = src.find("_dual_source_check_and_fix")
    _assert("⑰j 接線：`_node_synthesize` 呼叫了 `_dual_source_check_and_fix`", i_dual > 0)
    _assert("⑰k 順序：R5 排在數字溯源**之後**（前面每一道重生成都可能改掉時點）",
            i_num > 0 and i_dual > i_num, f"num@{i_num} dual@{i_dual}")

    # ⑰l 守門一致：拒答時要跳過（同其他四道，見閘門⑭）。
    _assert("⑰l 守門：拒答時跳過（與其他四道共用 `rq.looks_like_refusal`）",
            "looks_like_refusal" in src[max(0, i_dual - 200):i_dual])


def gate18_web_fetched_but_uncited() -> None:
    """閘門⑱：R6「抓到 web 卻一個都沒引用」→ 揭露（`web_fetched_but_uncited_notice`）。

    **它治的病**：`web-01` 第 10 輪 `n_web_calls=1`、fixture 裡就有 $4.75 兆，答案卻只有
    「Apple 目前的市值約為 43,420.2 億美元【AAPL_Fundamentals_20260612.txt, chunk #0】」
    ——82 天前的快照當「目前」，零 web 引用、零揭露，五道 validator 一道都不響。

    ⚠ **這是揭露不是重生成**，因為誤報方向決定：web 真的回垃圾時「網路結果未採用」字面為真。
    ⚠ **頻率極低**（現行架構 116 題×輪出現 1 次），所以**驗收只能靠這道閘門**，
      不要拿端到端跑分當證據。
    ⚠ **⑱f 是這道閘門最重要的一條**：在答案後面加字**可能翻掉 eval 側分類器的判定**
      （`check_news_routing.classify` 讀的就是答案文字）。任何「機械式附加」都要驗這件事，
      否則量尺會被受測物的輸出改變＝CLAUDE.md〈量尺不可與被測物耦合〉的第三種形狀。
    """
    print("\n── 閘門⑱：R6 抓到 web 卻沒引用 → 揭露 ────────────────────")
    f = ar.web_fetched_but_uncited_notice
    WEB_EXTRA = "\n=== 網路搜尋結果 ===\n[web: https://stockanalysis.com/stocks/aapl/market-cap] ..."

    # ⑱a 陽性：web_replay_r10 的 web-01，**逐字**。
    R10 = ("Apple 目前的市值約為 **43,420.2 億美元（$4,342.02 billion）**（即約 43,400 億美元"
           "（$4.34 trillion））【AAPL_Fundamentals_20260612.txt, chunk #0】.")
    _assert("⑱a 陽性：web 有內容、答案零 web 引用 → 出揭露句",
            ar._WEB_UNCITED_MARK in f(R10, WEB_EXTRA), f"實得 {f(R10, WEB_EXTRA)!r}")

    # ⑱b 誤報對照：引用了 web 就沒事——本規則對「引用比例」沒有意見。
    CITED = ("Apple 目前的市值約為 **47,500 億美元（$4.75 兆）**（截至 2026-09-01）"
             "【web: https://stockanalysis.com/stocks/aapl/market-cap】。\n"
             "財報值為 43,420.2 億美元【AAPL_Fundamentals_20260612.txt, chunk #0】。")
    _assert("⑱b 誤報對照：答案有 web 引用 → 沉默", f(CITED, WEB_EXTRA) == "", f"實得 {f(CITED, WEB_EXTRA)!r}")

    # ⑱c 誤報對照：web 根本沒拿到內容 → 沉默。那是 `_unfulfilled_web_route_gaps` 的守備範圍，
    #    在這裡再講一次會變成同一件事講兩句。
    _assert("⑱c 誤報對照：web_extra 為空（沒抓到內容）→ 沉默（避免與時效缺口重複揭露）",
            f(R10, "") == "" and f(R10, "   ") == "", f"實得 {f(R10, '')!r}")

    # ⑱d 全形／半形 web 標記都要認。⚠ 生產的 `_WEB_MARK` 只認半形，而實測答案全是全形——
    #    只認半形會讓這條規則在**每一份**有 web 引用的答案上誤報（R4 就是這樣壞掉的）。
    FULL = "市值為 4.75 兆【web: https://stockanalysis.com/x】。"
    HALF = "市值為 4.75 兆[web: https://stockanalysis.com/x]。"
    _assert("⑱d 全形【web:】與半形 [web:] 都算引用（只認半形＝R4 那個壞法）",
            f(FULL, WEB_EXTRA) == "" and f(HALF, WEB_EXTRA) == "",
            f"全形={f(FULL, WEB_EXTRA)!r} 半形={f(HALF, WEB_EXTRA)!r}")

    # ⑱e 接線：掛在 LIVE 分支，而且**拒答判定取在附加任何聲明之前**。
    #    ⚠ `looks_like_refusal` 有 150 字上限：先附時效聲明再判，真拒答會因為變長而不再像
    #      拒答 → 守門靜默失效。
    src = inspect.getsource(ar._node_synthesize)
    i_flag = src.find("_was_refusal = rq.looks_like_refusal")
    # ⚠ **要找 `i_flag` 之後**的那一個：`_format_unresolved_freshness_notice` 在
    #   `_node_synthesize` 裡出現兩次，第一次在「collected/web 皆空 → 拒答」的早退分支。
    #   拿第一次來比順序會永遠 FAIL，而那是斷言寫錯不是接線錯。
    #   （早退分支不需要 R6：它的條件是 `not chunks and not web_notes` → web_extra 必為空。）
    i_notice = src.find("_format_unresolved_freshness_notice", i_flag)
    i_r6 = src.find("web_fetched_but_uncited_notice")
    _assert("⑱e1 接線：`_node_synthesize` 呼叫了 `web_fetched_but_uncited_notice`", i_r6 > 0)
    _assert("⑱e2 拒答判定取在**附加任何聲明之前**（150 字上限會讓後判失效）",
            0 < i_flag < i_notice < i_r6, f"flag@{i_flag} notice@{i_notice} r6@{i_r6}")
    _assert("⑱e3 只在 LIVE 分支（snapshot 的 eval 基準一格不動）",
            "FRESHNESS_LIVE" in src[max(0, i_flag - 300):i_flag])

    # ⑱f **跨量尺安全性：驗那個「讓它安全」的不變量，不是驗「結果剛好沒變」。**
    #
    # ⚠ 第一版寫的是「附上揭露句後 `check_news_routing.classify` 的判定不變」——**恆真、
    #   零判別力**。因為揭露句以 `\n\n---\n⚠` 開頭，正好命中 `rq.EVIDENCE_TAIL_RE`
    #   （`\n-{3,}\n(?=\s*(?:📚|⚠))`）→ 每一個呼叫 `strip_evidence_tail` 的消費端**根本看不到
    #   它**。變異測試當場證實：把措辭改成含承認詞的「本次無法取得可用的網路新聞來源」
    #   （`_ADMIT_RE` ＋ `_FLOW_SCOPE_RE` 同句命中）**照樣全綠**。
    # → 所以真正該驗的是**邊界前綴**：揭露句必須落在證據尾巴的界線**之後**。措辭因此可以
    #   自由改；一旦有人把前綴拿掉，它就會漏進答案本體並且**能夠**翻掉分類器（⑱f2 證明這件事）。
    notice = f(R10, WEB_EXTRA)
    # ⚠ 比對前 rstrip：`EVIDENCE_TAIL_RE` 的樣式是「換行 ＋ 三個以上減號 ＋ 換行」，
    #   切完會把界線**前面**那個換行留在本體尾巴。
    _assert("⑱f1 揭露句落在證據尾巴界線之後（`strip_evidence_tail` 之後逐字等於原答案）",
            ar.rq.strip_evidence_tail(R10 + notice).rstrip() == R10.rstrip(),
            f"實得 {ar.rq.strip_evidence_tail(R10 + notice)[-60:]!r}")

    # ⑱f2 **誤報對照**：證明那個前綴是承重的，不是裝飾。少了它，同一段文字會漏進答案本體
    #     ——而且足以把 eval 側分類器從 `kb_only`（危險）翻成 `admits_gap`（誠實）。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import check_news_routing as cnr
        leaked = "。本次無法取得可用的網路新聞來源"      # 拿掉前綴、且措辭含承認詞
        before = cnr.classify(R10)[0]
        after_safe = cnr.classify(R10 + notice)[0]
        after_leak = cnr.classify(R10 + leaked)[0]
        _assert("⑱f2 誤報對照：**沒有**邊界前綴時，同一段文字會把 kb_only 翻成 admits_gap",
                before == "kb_only" and after_leak == "admits_gap",
                f"before={before} leak={after_leak}（若沒翻面，這條斷言就沒有前提）")
        _assert("⑱f3 有邊界前綴時判定不變（⑱f2 證明了這不是恆真）",
                after_safe == before, f"{before} → {after_safe}")
    except ImportError as e:
        _assert("⑱f2/⑱f3 跨量尺誤報對照", False, f"import 失敗 {e!r}（不要靜默跳過斷言）")



def gate19_unit_finalization():
    """⑲ 金額單位三層後處理（`rq.finalize_answer_units`）。零 LLM、零網路、零 Qdrant。

    ⚠ **這道的價值幾乎全在誤報對照與接線斷言**：陽性那三條，一個「一律把億剝掉」的
      粗暴實作也會過；真正會出事的是它**動到不該動的東西**（非金額計數、敘述性括號）、
      或**擾動既有基準**（LLM 照 Rule 11 寫的正常路徑必須逐字不變）。
    ⚠ **⑲g 是這道最重要的一條**：三層的正確性完全建立在「順序」上，而順序是註解裡的
      宣稱。⑲g 把順序調換、證明它當場壞掉——沒有它，那個宣稱無法被證偽。
      ⚠ 這條的**症狀是我第一版猜錯的**：原本斷言「調換會吐巢狀」，實跑發現 `repair_paired_yi`
        會把巢狀自己拆掉、於是**乾淨地留下 LLM 那個錯值**（84.75 而非 847.5）。
        後者危險得多——外觀完全正常。所以現在斷言的是**值**，不是格式。
    """
    print()
    print("⑲ finalize_answer_units：金額單位三層後處理")
    F = ar.rq.finalize_answer_units
    KB = [{"content": "Total revenue was $84.75 billion for the quarter."}]

    # ── 陽性：三個逐字驗證過的真實形狀（2026-09-03）
    _assert("⑲a ① million 換算差 10 倍 → 修成 828.86 億（原答案寫 82.9）",
            F("營收為 $82,886 百萬美元（約 $82.9 億美元）") ==
            "營收為 828.86 億美元（$82,886 百萬）")
    _assert("⑲b ② 同形狀（716,924 百萬 → 7,169.24 億，原答案寫 716.924）",
            F("總營收為 716,924 百萬美元（即 716.924 億美元）") ==
            "總營收為 7,169.24 億美元（$716,924 百萬）")
    _assert("⑲c ③ LLM 自寫雙寫：值改對且**不巢狀**（舊版會吐 `84.75 億美元（847.5 億美元（…））`）",
            F("營收為 84.75 億美元（$84.75 billion）") == "營收為 847.5 億美元（$84.75 billion）")

    # ── 誤報對照①：正常路徑必須**逐字**與舊版相同 ＝ 既有基準不被這層動到的證明
    for txt in ("營收為 $84.75 billion，較去年成長",
                "淨利為 58,321 百萬美元",
                "市值 $4962.16B"):
        _assert(f"⑲d 正常路徑逐字等於單獨跑 convert_usd_units_to_yi：{txt[:18]}",
                F(txt) == ar.rq.convert_usd_units_to_yi(txt),
                f"新 {F(txt)!r} vs 舊 {ar.rq.convert_usd_units_to_yi(txt)!r}")

    # ── 誤報對照②：非金額計數。回測踩過 news-10 / mi-11，幣別標記是承重條件
    NON_MONEY = "月活躍使用者達 10 億人，裝置 25 億部，流通股數 2.57 億股"
    _assert("⑲e 非金額計數（10 億使用者/25 億部/2.57 億股）一個字都不能動",
            F(NON_MONEY, KB) == NON_MONEY, f"實得 {F(NON_MONEY, KB)!r}")

    # ── 誤報對照③：敘述性括號不是單位雙寫，不可誤刪
    NARR = "營收為 $84.75 billion（其中雲端佔大宗）"
    _assert("⑲f 敘述性括號保留（只有『括號內是換算成億』才剝）",
            "（其中雲端佔大宗）" in F(NARR), f"實得 {F(NARR)!r}")

    # ── ⑲g **順序承重的證明**：調換 → 巢狀當場重現。少了這條，「順序不可調換」只是註解。
    wrong_order = ar.rq.repair_paired_yi(
        ar.rq.convert_usd_units_to_yi("營收為 84.75 億美元（$84.75 billion）"))[0]
    _right = F("營收為 84.75 億美元（$84.75 billion）")
    _assert("⑲g 誤報對照：順序調換 → 錯值**靜默留著**（證明順序是承重的）",
            wrong_order != _right and "847.5" not in wrong_order,
            f"調換順序實得 {wrong_order!r} vs 正確 {_right!r}（若相同，這條沒有判別力）")

    # ── ⑲h 孤立的億：沒有來源時不准動（判準需要來源，無來源＝無從定罪）
    SOLO = "營收為 84.75 億美元，年增 12%"
    _assert("⑲h 無來源時孤立的億不動", F(SOLO) == SOLO, f"實得 {F(SOLO)!r}")
    _assert("⑲h2 有來源且可定罪時才修（證明 ⑲h 不是因為這層根本沒接上）",
            F(SOLO, KB) == "營收為 847.5 億美元（$84.75 billion），年增 12%",
            f"實得 {F(SOLO, KB)!r}")

    # ── ⑲k 證據 B：② 自己的產出當權威（2026-09-04 端到端跑出來才補的路）
    #     背景：⑲h/⑲h2 走的是證據 A（來源有 "$N billion"），而 SEC 的數字幾乎都住在
    #     markdown 表格裡（`| Revenue | $82,886 |`，"In millions" 只在表頭）→ `_src_has`
    #     要求單位詞緊貼數字，對表格來源**兩個方向都是 False**（實測）。於是這個真實形狀
    #     三層全漏：`828.86 億美元（$82,886 百萬），相當於 $82.886 億美元`——同句自相矛盾、
    #     後半差 10 倍。⑲k 驗的是換掉證據源之後它會被接住。
    TBL = [{"content": "| Revenue | $82,886 | $70,066 |\n(In millions, except per share amounts)"}]
    E2E = "總營收為 $82,886 百萬美元，相當於 $82.886 億美元。"
    _assert("⑲k 表格來源：② 產出的億值可當證據，孤立的 82.886 億被修成 828.86",
            F(E2E, TBL) == "總營收為 828.86 億美元（$82,886 百萬），相當於 828.86 億美元。",
            f"實得 {F(E2E, TBL)!r}")
    _assert("⑲k2 前提：證據 A 對這份表格來源確實兩個方向都失明（否則 ⑲k 測到的是別條路）",
            not ar.rq._src_has(TBL[0]["content"], 82.886, r"billion|B\b")
            and not ar.rq._src_has(TBL[0]["content"], 82886, r"million|M\b"))
    _assert("⑲k3 證據 B **不補 `（$N billion）`**：那字串不在來源裡，補了會被判無法溯源",
            "billion" not in F(E2E, TBL))
    # 誤報對照①：兩個**真的**差 10 倍的獨立金額 → 都由 ② 產出、都帶括號雙寫 → 一個都不准動。
    TWO = "雲端部門為 $8,288.6 百萬美元，總營收為 $82,886 百萬美元。"
    _assert("⑲k4 誤報對照：真的差 10 倍的兩筆獨立金額不得被改（`_DUAL_PAREN_RE` 擋）",
            F(TWO, TBL) == "雲端部門為 82.886 億美元（$8,288.6 百萬），總營收為 828.86 億美元（$82,886 百萬）。",
            f"實得 {F(TWO, TBL)!r}")
    # 誤報對照②：② 一個億值都沒產出時，證據 B 必須整條靜默（不能退化成「看到億就乘 10」）。
    _assert("⑲k5 誤報對照：② 無產出時證據 B 靜默（無來源）",
            F("營收為 82.886 億美元。") == "營收為 82.886 億美元。")
    _assert("⑲k6 誤報對照：② 無產出且來源也定不了罪 → 不動",
            F("營收為 82.886 億美元。", TBL) == "營收為 82.886 億美元。")
    # 誤報對照③：非金額計數仍不得動——即使 ② 剛好產出了它的 10 倍值。
    CNT = "該平台有 10 億使用者。總營收為 $10,000 百萬美元。"
    _assert("⑲k7 誤報對照：非金額計數不因 ② 產出 100 億而被改",
            "10 億使用者" in F(CNT, TBL), f"實得 {F(CNT, TBL)!r}")
    # ⑲k8 尾綴裸「元」：LLM 寫 `$X 百萬元` 時，「元」不可被留成孤兒。
    _assert("⑲k8 尾綴裸「元」被吃掉（舊版吐 `1,197.96 億美元（$119,796 百萬）元`）",
            F("總營收為 $119,796 百萬元。") == "總營收為 1,197.96 億美元（$119,796 百萬）。",
            f"實得 {F('總營收為 $119,796 百萬元。')!r}")

    # ── ⑲i 接線：兩條生產路徑都必須走 finalize，且**不得**再裸呼叫 convert
    #     （⑮e 的教訓：呼叫點寫了名字不代表值真的流過去 → 上面 ⑲a~⑲h 已經是行為測試，
    #      這裡補的是「有沒有別的路徑繞過這層」，那是 AST 才看得到的）
    import ast as _ast
    for path, want in (("rag_query.py", "finalize_answer_units"),
                       ("agentic_rag_version/graph.py", "finalize_answer_units"),
                       ("api_server.py", "finalize_answer_units")):   # ← 產品線走的那條
        src = Path(path).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)]
        def _name(n):
            f = n.func
            return f.attr if isinstance(f, _ast.Attribute) else getattr(f, "id", "")
        n_final = sum(1 for c in calls if _name(c) == want)
        n_bare = sum(1 for c in calls if _name(c) == "convert_usd_units_to_yi")
        _assert(f"⑲i {path} 呼叫 {want}", n_final >= 1, f"實得 {n_final} 次")
        allowed = 1 if path == "rag_query.py" else 0   # rq 只准 finalize 內部那一次
        _assert(f"⑲i2 {path} 裸呼叫 convert_usd_units_to_yi 次數 == {allowed}",
                n_bare == allowed,
                f"實得 {n_bare} 次（繞過三層組裝＝那條路徑重新失明）")

    # ── ⑲j 拒答不受影響（拒答沒有數字，但這層跑在所有路徑上）
    REF = "I don't have enough information in my knowledge base to answer this."
    _assert("⑲j 拒答文字逐字不變", F(REF, KB) == REF)


def main() -> int:
    print(f"collection={ar.rq.COLLECTION_NAME}")
    cov = ar._get_kb_coverage()
    if not cov.get("available"):
        print(f"[ABORT] KB coverage 掃不到（{cov.get('error')}）→ 這支的所有斷言都失去意義")
        return 2
    if not cov.get("sources"):
        print("[ABORT] coverage 沒有 sources 期別表 → _fiscal_rank 一律回 None,全部斷言會假性通過")
        return 2

    gate1_fiscal_rank()
    gate2_stale_period_positive()
    gate3_stale_period_negative()
    gate4_news_gaps()
    gate5_notice_wiring()
    gate6_end_to_end_regression()
    gate7_untraceable_numbers()
    gate8_web_authority()
    gate9_reference_citation_repair()
    gate10_ratio_source_field()
    gate11_basis_disclosure()
    gate12_ratio_intent_llm()
    gate13_refusal_no_citation_tail()
    gate14_synthesize_refusal_guards()
    gate15_period_fallback_disclosure()
    gate16_round0_query_is_verbatim()
    gate17_dual_source_timepoints()
    gate18_web_fetched_but_uncited()
    gate19_unit_finalization()

    print(f"\n{'=' * 66}")
    print(f"GATE: {'PASS' if _FAIL == 0 else 'FAIL'}    PASS {_PASS}  FAIL {_FAIL}")
    for f in _FAILURES:
        print(f"  ✗ {f}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
