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

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agentic_rag_v2 as ar   # noqa: E402

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

    print(f"\n{'=' * 66}")
    print(f"GATE: {'PASS' if _FAIL == 0 else 'FAIL'}    PASS {_PASS}  FAIL {_FAIL}")
    for f in _FAILURES:
        print(f"  ✗ {f}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
