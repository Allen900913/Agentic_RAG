"""
judge_regression.py — correctness judge 專屬回歸測試套件

動機（見 CHANGELOG 2026-07-08~07-10）：過去一週連續發生財年季度誤判、OR 邏輯漏判、
捏造範圍過寬、rubric 權重顛倒等 judge/rubric bug，每次都是「肉眼發現分數異常 → 人工複查
→ 補一條 prompt 規則 → 用同一個案例重判驗證」。這個流程本身沒有安全網：新規則會不會讓
真正的違規也被放過（規則越補越鬆），從來沒有系統性驗證過。

這支腳本把「已知會踩雷的案例」+「刻意構造的反例（真正該被判違規/未命中的案例）」固定
下來，每次修改 _CORRECTNESS_FEEDBACK_SYSTEM 或更換 judge model 前後都跑一次：
- 正案例（來自真實歷史事故的 query/answer/rubric）：judge 不該再誤判。
- 反案例（人工構造，同一個 rubric 但答案真的違規/真的漏講）：judge 仍然要抓到，
  防止規則修過頭變成「什麼都不判違規」。

呼叫的是 evaluate_correctness_with_feedback_voted（跟生產/eval 用同一個函式），
不重新實作判定邏輯，只是餵固定輸入、不燒 retrieve/generate 的成本。

⚠ **這支不是零噪音的閘門，是逐題的 k/n 量尺**。judge 是 MoE，單輪總分完全不可比——
2026-08-27 修好後同一份碼、同一個模型（gpt-oss-20b）連跑三個單輪拿到 **10/11、9/11、11/11**，
而同一天跑了兩輪 `--repeat 3`，**兩輪之間也不一致**——`col01_true_fabrication_negative`
第一輪 1/3、第二輪 3/3。把當天全部 9 次觀測合起來才看得出誰真的有問題：

    9/9   8 題
    7/9   col01_true_fabrication_negative   （雜訊等級，與 lex12 同量級）
    7/9   lex12_fiscal_calendar             （財年措辭，已知系統性 bug 家族）
    1/9   sem03_true_fabrication_negative   ← **只有這一題是真的**，見該題 known_limitation

出貨當下的 `--repeat 3` 是：全過 9、全掛 0、時好時壞 1（lex12 2/3）、已知極限 1（sem03 0/3）。
**單輪總分會把上面這張表壓成一個沒有意義的數字**，所以 `--repeat` 報逐題 k/n、
**退出碼只認「全掛」**（k=0）。n=3 都還會誤導，要下結論就多跑幾輪把 k/n 加起來。

⚠ 三態 PASS／FAIL／FLAKY 之外還有 **N/A，N/A 不能併進 PASS**（CLAUDE.md 全域規則）。
帶 `known_limitation` 的案例是**當初拿 judge 現在已經拿不到的輸入寫的**——不是 judge 判錯，
是題目的前提沒了。它哪天變成 PASS 是好消息，該回頭撤掉標記。

Usage:
  python eval/judge_regression.py --dry-run       # 零 LLM 接線檢查，先跑這個
  python eval/judge_regression.py --repeat 3      # 要下結論就用這個（逐題 k/3）
  python eval/judge_regression.py --judge-model openai/gpt-oss-120b --votes 3
"""

import argparse
import ast
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from eval.eval_generation_llm_judge import (
    DEFAULT_JUDGE_MODEL,
    evaluate_correctness_with_feedback,
    evaluate_correctness_with_feedback_voted,
    compute_correctness,
)


# ── 固定案例：每題含 query/answer/rubric + 期望結果 ────────────────────────
# ⚠ 2026-08-27：原本每題還帶一個 `hall_result`（模擬自製 Hallucination Rate 的結果摘要）
# 一起餵進 judge。那個指標 **2026-07-23 就已經移除**（與 RAGAS Faithfulness 相關性太低，見
# `eval_generation_llm_judge.py` 檔頭），`evaluate_correctness_with_feedback` 的簽章跟著少了
# `hall_result`／`relevance_score` 兩個參數——本檔沒跟上，於是**一跑就 TypeError、一個月沒人發現**。
# 現在 `fatal_hallucination` 改由 must_not_include 命中在 Python 端推導（`compute_correctness`），
# judge 只看「問題＋答案＋rubric＋context_recall/refusal 提示」。那些 `hall_result` 字典已是死資料，
# 一併刪除（留著必然與簽章再次漂移）。判「捏造」現在完全靠 mn checkpoint，反例照樣要抓得到。

CASES = [
    # ── col-01 / 對應 sem-01：FABRICATION SCOPE ──────────────────────────
    {
        "id": "col01_fabrication_scope",
        "note": "真實案例（07-10 前）：純質化比較語句「遠超其他競爭者」被判捏造市佔數字，"
                "已用 FABRICATION SCOPE 規則修正。回歸目標：mn0 不應違規。",
        "query": "為什麼大家做 AI 都要買輝達的卡，別家一直追不上它？",
        "answer": (
            "NVIDIA 之所以成為 AI 開發者的首選，是因為它在硬體性能、完整軟體生態與市場佔有率上"
            "遠超其他競爭者，提供了「一次投資、全方位支援」的獨特價值。\n"
            "- GPU 核心與並行運算能力：NVIDIA 的 GPU 具備數萬個運算核心與 Tensor Core，"
            "成為 AI 訓練與推論的基礎硬體【NVDA_10K_2026.html, chunk #38】。\n"
            "- 平台化策略：公司提供從 CUDA 開發平台、AI 模型庫到雲端服務的完整堆疊，"
            "讓開發者在同一生態系內快速建置、部署 AI 應用【NVDA_10K_2026.html, chunk #38】。\n"
            "- 競爭者資源與技術差距：雖然 AMD、Intel 等也在研發加速器，但在整合軟硬體平台的"
            "深度上仍落後於 NVIDIA【NVDA_10K_2026.html, chunk #51】。"
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "提到 CUDA 軟體生態 / 開發者鎖定 / 軟硬體全端平台", "weight": 50, "is_critical": True},
                {"checkpoint": "提到資料中心 GPU 的效能領先或加速運算平台", "weight": 30, "is_critical": False},
                {"checkpoint": "提到網路整合（NVLink / InfiniBand / Mellanox）或系統級整合", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "捏造未在來源出現的具體市佔率百分比或營收數字", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_required": {"mi0"},
            "mn_violations_forbidden": {"mn0"},
        },
    },
    {
        "id": "col01_true_fabrication_negative",
        "note": "反例（人工構造）：同一 rubric，答案改成明確捏造一個具體市佔率數字。"
                "驗證 FABRICATION SCOPE 規則沒有把「真的有具體數字」也一併放過。",
        "query": "為什麼大家做 AI 都要買輝達的卡，別家一直追不上它？",
        "answer": (
            "NVIDIA 目前在 AI 加速晶片市場的市佔率高達 92%，這個具體數字遠遠超越所有競爭者。\n"
            "- 公司透過 CUDA 開發平台鎖定開發者，形成軟硬體全端生態【NVDA_10K_2026.html, chunk #38】。\n"
            "- 資料中心 GPU 效能領先業界【NVDA_10K_2026.html, chunk #38】。"
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "提到 CUDA 軟體生態 / 開發者鎖定 / 軟硬體全端平台", "weight": 50, "is_critical": True},
                {"checkpoint": "提到資料中心 GPU 的效能領先或加速運算平台", "weight": 30, "is_critical": False},
                {"checkpoint": "提到網路整合（NVLink / InfiniBand / Mellanox）或系統級整合", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "捏造未在來源出現的具體市佔率百分比或營收數字", "is_critical": True},
            ],
        },
        "expect": {
            "mn_violations_required": {"mn0"},
        },
    },

    # ── col-07 / 對應 sem-10：rubric 權重（已修正版本）─────────────────────
    {
        "id": "col07_rubric_reweight",
        "note": "真實案例（07-10 前）：舊 rubric 把 AI 投資設為 critical、Reality Labs 損益降為次要，"
                "跟口語問題「到底在賺還在賠」的重心顛倒。這裡用修正後的 rubric（Reality Labs=critical）"
                "測試同一份真實答案，答案本身完整涵蓋 Reality Labs 虧損數字。"
                "mi1（AI 投資）只要求 mi0 命中——答案對 AI 只有『人工智慧』一詞的順帶提及，"
                "沒有基礎模型/推薦系統等細節，是否算命中屬於模型嚴謹度差異的合理判斷區間"
                "（120b 判未中、20b 判中，兩者都在 grading rule『merely mentioning is not enough』的"
                "合理解讀範圍內），不是本案例要驗證的重點，故不強制要求。",
        "query": "臉書一直砸大錢做頭戴裝置跟虛擬世界，到底在賺還在賠？以後想怎麼搞？",
        "answer": (
            "Meta 的 Reality Labs 仍在虧損，但虧損幅度正逐年縮小——2025 全年虧損約 191.9 億美元，"
            "較 2024 年的 177.3 億美元略有擴大；2026 年第一季虧損約 40.3 億美元【META_10K_2025.html, chunk #4】。\n"
            "未來策略：Meta 明確將元宇宙定位為下一代社交科技，持續投入 VR/AR 研發、人工智慧、"
            "可穿戴設備與新介面，以打造沉浸式體驗【META_10K_2025.html, chunk #67】。"
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "提到 Reality Labs 的 VR/AR 長期投入與虧損（問題本身在問「賺還是賠」，這是直接對應的答案）", "weight": 50, "is_critical": True},
                {"checkpoint": "提到 AI 投資（基礎模型 / 推薦系統 / Meta AI）作為「以後想怎麼搞」的一部分", "weight": 30, "is_critical": False},
                {"checkpoint": "提到 AI 眼鏡（Ray-Ban Meta）等穿戴裝置", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "捏造未在來源出現的 Reality Labs 具體虧損或營收數字", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_required": {"mi0"},
            "mn_violations_forbidden": {"mn0"},
        },
    },

    # ── col-10 / 對應 sem-09：OR-CHECKPOINTS ────────────────────────────
    {
        "id": "col10_or_logic",
        "note": "真實案例（07-10 前）：checkpoint「提到 Gemini 模型 / AI 整合進搜尋」是 OR 條件，"
                "答案明講「把 AI 能力內建於搜尋」但沒點名 Gemini，舊 judge 只認字面 Gemini、判未命中。"
                "回歸目標：mi0 應該命中（OR 邏輯）。",
        "query": "谷歌會不會怕 AI 搶走它網路搜尋的生意？它打算怎麼接招？",
        "answer": (
            "Google 已經認識到 AI 可能會侵蝕其搜尋業務，正以大幅投資自家 AI、將 AI 深度嵌入"
            "所有產品與服務的方式積極應對【GOOGL_10K_2025.html, chunk #157】。\n"
            "公司正開發前沿的生成式 AI，並把 AI 能力內建於搜尋、雲端與其他產品，以先行提供"
            "使用者體驗，再考慮變現方式【GOOGL_10K_2025.html, chunk #179】。\n"
            "為支撐 AI 計算需求，Google 正大幅提升資本支出，並持續優化 AI 專用 TPU、GPU 供應鏈"
            "【GOOGL_10K_2025.html, chunk #179】。"
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "提到 Gemini 模型 / AI 整合進搜尋", "weight": 50, "is_critical": True},
                {"checkpoint": "提到搜尋廣告核心業務的防禦或變現", "weight": 30, "is_critical": False},
                {"checkpoint": "提到 Google Cloud 或自研 TPU", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "捏造未在來源出現的搜尋市佔或 AI 營收數字", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_required": {"mi0"},
        },
    },
    {
        "id": "col10_true_miss_negative",
        "note": "反例（人工構造）：同一 rubric，答案完全不提 Gemini 也不提 AI 整合進搜尋這件事，"
                "只講廣告與雲端基礎設施。驗證 OR-CHECKPOINTS 規則沒有把「兩個子項都真的沒提到」也放行。",
        "query": "谷歌會不會怕 AI 搶走它網路搜尋的生意？它打算怎麼接招？",
        "answer": (
            "Google 的廣告業務持續成長，公司透過提升資料中心效率與資本支出管理來維持利潤率"
            "【GOOGL_10K_2025.html, chunk #156】。Google Cloud 也貢獻了顯著的營收成長，"
            "並持續投資自研 TPU 以降低算力成本【GOOGL_10K_2025.html, chunk #139】。"
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "提到 Gemini 模型 / AI 整合進搜尋", "weight": 50, "is_critical": True},
                {"checkpoint": "提到搜尋廣告核心業務的防禦或變現", "weight": 30, "is_critical": False},
                {"checkpoint": "提到 Google Cloud 或自研 TPU", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "捏造未在來源出現的搜尋市佔或 AI 營收數字", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_forbidden": {"mi0"},
        },
    },

    # ── lex-12：FISCAL vs CALENDAR PERIOD ───────────────────────────────
    {
        "id": "lex12_fiscal_calendar",
        "note": "真實案例（07-09）：AAPL 用非日曆財年，答案正確引用 202512 那一季的來源"
                "（chunk #26，已查證），只是用「Q1 2026」財年措辭而非日曆代碼，"
                "3 票judge 曾一致誤判為捏造/錯期。回歸目標：mn0 不應違規，mi0 應命中。",
        "query": "AAPL 202512 那一季的 10-Q 裡，營收成長主要來自哪些產品線？",
        "answer": (
            "Apple's Q1 2026 revenue growth was driven almost entirely by higher iPhone sales "
            "and stronger Services sales, with iPad also adding modest upside while Mac and "
            "Wearables declined. The 10-Q notes that iPhone net sales rose year-over-year on "
            "stronger Pro-model demand, Services net sales grew on advertising, App Store and "
            "cloud-service gains, and iPad net sales increased on higher iPad and iPad Pro "
            "shipments; by contrast, Mac and Wearables-Home-Accessories sales fell "
            "[AAPL_10Q_202512.html, chunk #26]."
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "明確對應到 202512 那一季", "weight": 50, "is_critical": True},
                {"checkpoint": "指出至少一項驅動營收成長的產品線或業務", "weight": 30, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "把其他季度的數字當成 202512 季", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_required": {"mi0", "mi1"},
            "mn_violations_forbidden": {"mn0"},
        },
    },
    {
        "id": "lex12_true_wrong_period_negative",
        "note": "反例（人工構造）：答案明確討論的是另一季（202509）的數字，卻聲稱是在回答 202512 那一季。"
                "驗證 FISCAL vs CALENDAR 規則沒有連「真的引用錯季度」都放過。",
        "query": "AAPL 202512 那一季的 10-Q 裡，營收成長主要來自哪些產品線？",
        "answer": (
            "In the quarter ended June 2025 (fiscal Q3 2025), Apple's revenue growth was driven "
            "by strong Mac sales following the M-series refresh and a rebound in Greater China "
            "iPhone demand [AAPL_10Q_202509.html, chunk #12]."
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "明確對應到 202512 那一季", "weight": 50, "is_critical": True},
                {"checkpoint": "指出至少一項驅動營收成長的產品線或業務", "weight": 30, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "把其他季度的數字當成 202512 季", "is_critical": True},
            ],
        },
        "expect": {
            "mn_violations_required": {"mn0"},
        },
    },

    # ── col-03 / 對應 lex-03：NUMERIC TOLERANCE ─────────────────────────
    {
        "id": "col03_numeric_tolerance",
        "note": "真實案例：rubric 要求毛利率約 68.3%，答案算出 67.6%~68.2%（未精確命中 68.3%），"
                "過去只能靠 3 票多數決勉強救回（且曾有 1 票認錯 checkpoint id）。"
                "加上 tolerance_pct=1.0 後，回歸目標：單次 judge call（votes=1）就應穩定命中 mi0，不必依賴多數決運氣。",
        "query": "微軟賣東西，扣掉成本之後大概還能留下幾成？",
        "answer": (
            "Microsoft's gross margin is roughly 68% of its revenue (about $56 billion gross "
            "profit on $83 billion of sales, i.e., ≈ 67.6%)【MSFT_10Q_202603.html, chunk #14】. "
            "The same pattern holds for the nine-month period, where gross margin was "
            "$164,983M on revenue of $241,832M, yielding about 68.2% gross margin "
            "【MSFT_10Q_202603.html, chunk #14】."
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "給出毛利率約 68.3%（0.683）", "weight": 50, "is_critical": True, "tolerance_pct": 1.0},
                {"checkpoint": "標明資料期間或來源", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "給出與來源（約 68%）明顯不符的毛利率", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_required": {"mi0", "mi1"},
            "mn_violations_forbidden": {"mn0"},
        },
    },
    {
        "id": "col03_true_wrong_number_negative",
        "note": "反例（人工構造）：答案給出的毛利率明顯偏離來源（45% vs 實際 ~68%），"
                "遠超 tolerance=±1.0pp。驗證 NUMERIC TOLERANCE 規則沒有把「真的算錯/引用錯數字」也放行。",
        "query": "微軟賣東西，扣掉成本之後大概還能留下幾成？",
        "answer": (
            "Microsoft's gross margin is approximately 45% of its revenue, reflecting the cost "
            "structure of its hardware and cloud infrastructure businesses 【MSFT_10Q_202603.html, chunk #14】."
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "給出毛利率約 68.3%（0.683）", "weight": 50, "is_critical": True, "tolerance_pct": 1.0},
                {"checkpoint": "標明資料期間或來源", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "給出與來源（約 68%）明顯不符的毛利率", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_forbidden": {"mi0"},
            "mn_violations_required": {"mn0"},
        },
    },

    # ── sem-03：grounded 具體數字不等於捏造 ─────────────────────────────
    {
        "id": "sem03_grounded_specific_number",
        "note": "真實案例（07-08）：答案引用「服務營收 310 億美元、成長 16%」，已用 Qdrant 查證"
                "grounded 在 News chunk #4 原文。judge 曾把「具體成長率數字」本身當成捏造嫌疑判 fatal。"
                "回歸目標：mn0 不應違規。⚠ 這一題的『已查證 grounded』是**當時人工用 Qdrant 查的**，"
                "不是 judge 看得到的資訊——judge 從頭到尾只拿到問題／答案／rubric，這正是本案例要測的"
                "（沒有佐證資訊時，具體數字本身不構成捏造）。答案引的 News chunk 2026-08-19 已不在 KB，"
                "不影響本案例：judge 不做檢索。",
        "query": "Apple 的服務業務未來成長潛力如何？",
        "answer": (
            "Apple 的服務業務未來成長潛力被視為強勁，預計將持續以兩位數的年增率擴張。"
            "服務佔 Apple 總營收約 28%，2025 年最後一季服務營收達 310 億美元，較前季成長 16%"
            "【AAPL_News_20260519_01.txt, chunk #4】。服務毛利在 2026 年第一季與第二季均上升，"
            "且毛利率因服務組合變化而提升【AAPL_10Q_202512.html, chunk #26】。"
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "提到服務業務營收成長或其高毛利特性", "weight": 50, "is_critical": True},
                {"checkpoint": "提到訂閱服務生態（App Store / iCloud / Music / TV+ 等）", "weight": 30, "is_critical": False},
                {"checkpoint": "提到龐大裝置安裝基數帶動服務變現", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "捏造未在來源出現的服務業務具體成長率百分比", "is_critical": True},
            ],
        },
        "expect": {
            "mi_hits_required": {"mi0"},
            "mn_violations_forbidden": {"mn0"},
        },
    },
    {
        "id": "sem03_true_fabrication_negative",
        "note": "反例（人工構造）：答案捏造一個「未來三年 45% 複合年增長率」的預測數字。"
                "它是上一題的**誤報對照**——上一題要求「具體數字不等於捏造」放行，這一題要求"
                "真的捏造的仍然要抓到。少了這一題，一個「mn 永遠不違規」的 judge 也會滿分。"
                "⚠ **它的前提在 2026-07-23 就沒了**（見下方 known_limitation），"
                "2026-08-27 實測 gpt-oss-20b **九次觀測只中 1 次**，`--votes 5` 的多數決也是 0 violation。",
        "known_limitation":
            "judge **從頭到尾拿不到來源 chunk**——`evaluate_correctness_with_feedback` 沒有這個參數，"
            "2026-07-23 移除自製 hallucination 指標後連 `hall_result` 這個間接管道也沒了。"
            "而 FABRICATION SCOPE 規則寫的是「a SPECIFIC number ... that is not grounded in **the sources**」，"
            "於是這條規則實際上只能靠**合理性**判，而「管理層預期 45% CAGR」讀起來是合理的。"
            "⚠ 同族的 `col01_true_fabrication_negative`（市佔率 92%）九次觀測 **7 中**——"
            "跟一般雜訊同量級，**沒有**跟著掉下來。所以「合理性」這個解釋是有判別力的："
            "92% 的市佔率離譜到光看答案就站不住，45% 的成長率預測不會。"
            "（⚠ 這一段是**改過的**：只看某一輪 `--repeat 3` 時 col01 是 1/3，我一度據此寫成"
            "「兩個反例方向一致」——九次觀測攤開才知道那是雜訊。n=3 不夠。）"
            "**復活條件**：judge 哪天拿得到來源，或這題的 mn checkpoint 改寫成「judge 從問題＋答案就判得出來」"
            "的形狀。在那之前 groundedness 這個軸由 RAGAS faithfulness 負責（那正是當初移除的理由）。",
        "query": "Apple 的服務業務未來成長潛力如何？",
        "answer": (
            "Apple 的服務業務展現強勁成長動能，管理層預期未來三年服務營收將以驚人的 45% "
            "複合年增長率擴張，遠超過公司歷史平均水準【AAPL_10K_2025.html, chunk #80】。"
        ),
        "rubric": {
            "must_include": [
                {"checkpoint": "提到服務業務營收成長或其高毛利特性", "weight": 50, "is_critical": True},
                {"checkpoint": "提到訂閱服務生態（App Store / iCloud / Music / TV+ 等）", "weight": 30, "is_critical": False},
                {"checkpoint": "提到龐大裝置安裝基數帶動服務變現", "weight": 20, "is_critical": False},
            ],
            "must_not_include": [
                {"checkpoint": "捏造未在來源出現的服務業務具體成長率百分比", "is_critical": True},
            ],
        },
        "expect": {
            "mn_violations_required": {"mn0"},
        },
    },
]


# ── 零 LLM 接線檢查（`--dry-run`）──────────────────────────────────────────────
# **這支存在的理由就是本檔自己踩的坑**：judge 的簽章 2026-07-23 少了兩個參數，本檔沒跟上，
# 於是一跑就 TypeError——而因為它「要燒 LLM」平常不跑，一個月沒人發現，judge 這段期間
# **完全沒有回歸保護**。零成本的接線檢查才會被順手跑進閘門巡檢，昂貴的判分不會。
#
# ⚠ kwargs 清單**用 AST 從 `run_case` 自己的原始碼讀**，不另外抄一份常數——抄寫必然漂移，
#   而漂移正是這次的病灶（見 `verify_web_gate_isolation.py` 閘門① 的同一個教訓）。
# ⚠ 第二項（expect 的 id 不得越界）鎖的是**量尺無聲死掉**的方向：`mi_hits_forbidden`／
#   `mn_violations_forbidden` 寫一個不存在的 id，那條斷言永遠成立，看起來全綠。


def _sent_kwargs() -> set[str]:
    """AST 讀出 `run_case` 實際送給 judge 的關鍵字參數名。"""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_case")
    for call in ast.walk(fn):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "evaluate_correctness_with_feedback_voted"):
            return {kw.arg for kw in call.keywords if kw.arg}
    raise AssertionError("run_case 裡找不到 evaluate_correctness_with_feedback_voted 的呼叫")


def check_wiring() -> bool:
    """零 LLM、零網路：確認這支跟 judge 的介面還對得上、且每個案例的斷言真的會被檢查。"""
    problems: list[str] = []

    # ① 簽章：送出去的 kwargs 必須恰好對得上 judge 的參數（多、少都要叫）
    accepted = set(inspect.signature(evaluate_correctness_with_feedback).parameters) | {"votes"}
    sent = _sent_kwargs()
    for extra in sorted(sent - accepted):
        problems.append(f"送了 judge 不收的參數 `{extra}=`（judge 簽章變過而本檔沒跟上）")
    for missing in sorted(accepted - sent):
        problems.append(f"漏送 judge 需要的參數 `{missing}=`")

    # ② 每個案例：rubric 結構完整、expect 非空、expect 的 id 不得越界
    _EXPECT_KEYS = {"mi_hits_required", "mi_hits_forbidden",
                    "mn_violations_required", "mn_violations_forbidden"}
    seen_required = seen_forbidden = False
    ids = [c["id"] for c in CASES]
    if len(set(ids)) != len(ids):
        problems.append("case id 有重複（`--case` 會挑到不只一題）")
    for c in CASES:
        cid, rub, exp = c["id"], c.get("rubric") or {}, c.get("expect") or {}
        n_mi, n_mn = len(rub.get("must_include", [])), len(rub.get("must_not_include", []))
        for i, p in enumerate(rub.get("must_include", [])):
            if not {"checkpoint", "weight", "is_critical"} <= set(p):
                problems.append(f"{cid}: must_include[{i}] 缺 checkpoint/weight/is_critical")
        if not exp:
            problems.append(f"{cid}: expect 是空的 → 這題永遠 PASS")
        for k in exp:
            if k not in _EXPECT_KEYS:
                problems.append(f"{cid}: expect 有不認得的鍵 `{k}` → 永遠不會被檢查")
        for k, n, pre in (("mi_hits_required", n_mi, "mi"), ("mi_hits_forbidden", n_mi, "mi"),
                          ("mn_violations_required", n_mn, "mn"),
                          ("mn_violations_forbidden", n_mn, "mn")):
            for cpid in exp.get(k, ()):
                if not (cpid.startswith(pre) and cpid[len(pre):].isdigit()
                        and int(cpid[len(pre):]) < n):
                    problems.append(f"{cid}: expect.{k} 的 `{cpid}` 不在 rubric 範圍內"
                                    f"（{pre}0..{pre}{n - 1}）→ 這條斷言測不到東西")
            if exp.get(k):
                if k.endswith("_required"):
                    seen_required = True
                else:
                    seen_forbidden = True

    # ③ 判別力：整個套件要同時有正例與反例，否則一個「什麼都不判違規」的 judge 也能滿分
    if not seen_required:
        problems.append("整套沒有任何 *_required 斷言 → 只證明了『不誤判』，沒證明『抓得到』")
    if not seen_forbidden:
        problems.append("整套沒有任何 *_forbidden 斷言 → 一個「一律判違規」的 judge 也會滿分")

    # ④ 下游算分（純 Python，可零 LLM 直接驗）：fatal 歸零、critical_miss 封頂 0.5
    _rub = {"must_include": [{"checkpoint": "a", "weight": 50, "is_critical": True},
                             {"checkpoint": "b", "weight": 50, "is_critical": False}],
            "must_not_include": [{"checkpoint": "x", "is_critical": True}]}
    _cc = compute_correctness(_rub, {"must_include_hits": ["mi0", "mi1"], "must_not_violations": []})
    if _cc["score"] != 1.0:
        problems.append(f"compute_correctness 全中應為 1.0，得到 {_cc}")
    _cc = compute_correctness(_rub, {"must_include_hits": ["mi1"], "must_not_violations": []})
    if not (_cc["score"] == 0.5 and _cc["critical_miss"]):
        problems.append(f"compute_correctness 漏 critical 應封頂 0.5，得到 {_cc}")
    _cc = compute_correctness(_rub, {"must_include_hits": ["mi0", "mi1"], "must_not_violations": ["mn0"]})
    if not (_cc["score"] == 0.0 and _cc["fatal_hallucination"]):
        problems.append(f"compute_correctness 命中 mn 應歸零，得到 {_cc}")

    if problems:
        print(f"[WIRING FAIL] {len(problems)} 項：")
        for p in problems:
            print(f"  ✗ {p}")
        return False
    n_kl = sum(1 for c in CASES if c.get("known_limitation"))
    print(f"[wiring OK] {len(CASES)} 案例、judge 簽章 {sorted(sent)} 對得上、"
          f"expect id 全在範圍內、compute_correctness 三態正確")
    if n_kl:
        # ⚠ 刻意印在這裡：`known_limitation` 是**唯一能讓失敗不算失敗**的旗標，
        #   它的數量必須每次跑都被看見，否則它會變成安靜消音失敗的地方。
        print(f"[wiring] ⚠ {n_kl}/{len(CASES)} 題帶 known_limitation（失敗時記 N/A 不記 FAIL）："
              + "、".join(c["id"] for c in CASES if c.get("known_limitation")))
    return True


def run_case(case: dict, model: str, votes: int) -> dict:
    verdict = evaluate_correctness_with_feedback_voted(
        votes=votes,
        query=case["query"],
        answer=case["answer"],
        rubric=case["rubric"],
        ctx_recall_llm=None,
        ctx_recall_overlap=None,
        is_refusal_hint=False,
        wrongful_refusal_hint=False,
        model=model,
    )
    hits = set(verdict.get("must_include_hits", []))
    violations = set(verdict.get("must_not_violations", []))
    expect = case["expect"]

    failures = []
    for cid in expect.get("mi_hits_required", set()):
        if cid not in hits:
            failures.append(f"expected mi hit {cid}, got hits={sorted(hits)}")
    for cid in expect.get("mi_hits_forbidden", set()):
        if cid in hits:
            failures.append(f"expected mi {cid} NOT hit, got hits={sorted(hits)}")
    for cid in expect.get("mn_violations_required", set()):
        if cid not in violations:
            failures.append(f"expected mn violation {cid}, got violations={sorted(violations)}")
    for cid in expect.get("mn_violations_forbidden", set()):
        if cid in violations:
            failures.append(f"expected mn {cid} NOT violated, got violations={sorted(violations)}")

    correctness = compute_correctness(case["rubric"], verdict)
    return {
        "id": case["id"],
        "passed": not failures,
        "failures": failures,
        "hits": sorted(hits),
        "violations": sorted(violations),
        "correctness": correctness,
        "reason": verdict.get("reason"),
        "error": verdict.get("error"),
    }


def main():
    ap = argparse.ArgumentParser()
    # ⚠ 預設**不要**在這裡再寫一次模型名。舊版硬編碼 `qwen/qwen3-32b`，而那支
    # 2026-07-12 就被 Groq 下架了——本檔跑不起來，這個過期預設也就沒人發現。
    # 一律跟著 `eval_generation_llm_judge.DEFAULT_JUDGE_MODEL`（選型證據寫在那裡）。
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                    help=f"judge model，預設跟隨 eval_generation_llm_judge.DEFAULT_JUDGE_MODEL"
                         f"（目前 {DEFAULT_JUDGE_MODEL}）")
    ap.add_argument("--votes", type=int, default=1,
                    help="單次判定內的多數決票數（judge 端）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每題重複判定幾輪，報逐題 k/n（判 judge 穩不穩定；MoE 單輪不可信）")
    ap.add_argument("--case", default=None, help="只跑單一 case id（除錯用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="零 LLM 接線檢查（見 check_wiring）；不判分，只確認這支還跑得動")
    args = ap.parse_args()

    if not check_wiring():
        sys.exit(2)
    if args.dry_run:
        return

    cases = [c for c in CASES if args.case is None or c["id"] == args.case]

    print(f"judge_regression: model={args.judge_model} votes={args.votes} cases={len(cases)}")
    print("=" * 78)

    n = args.repeat
    rates: list[tuple[str, int, list[dict]]] = []
    for case in cases:
        runs = [run_case(case, args.judge_model, args.votes) for _ in range(n)]
        k = sum(1 for r in runs if r["passed"])
        if k == n:
            tag = "PASS"
        elif case.get("known_limitation"):
            tag = "N/A"          # ⚠ N/A 不能併進 PASS（CLAUDE.md 全域規則）
        elif k == 0:
            tag = "FAIL"
        else:
            tag = "FLAKY"
        print(f"[{tag} {k}/{n}] {case['id']}")
        if tag == "N/A":
            print(f"    · 已知極限（不計入 PASS）：{case['known_limitation']}")
        for f in sorted({f for r in runs for f in r["failures"]}):
            print(f"    - {f}")
        for e in sorted({r["error"] for r in runs if r["error"]}):
            print(f"    ! judge error: {e}")
        rates.append((case["id"], k, tag))

    n_always = sum(1 for _, _, t in rates if t == "PASS")
    n_never = sum(1 for _, _, t in rates if t == "FAIL")
    n_flaky = sum(1 for _, _, t in rates if t == "FLAKY")
    n_na = sum(1 for _, _, t in rates if t == "N/A")
    print("=" * 78)
    print(f"每題跑 {n} 輪：全過 {n_always}  全掛 {n_never}  時好時壞 {n_flaky}  已知極限 {n_na}"
          f"   （共 {len(rates)} 案例）")
    if n_na:
        print(f"⚠ 「已知極限」**不是通過**：那 {n_na} 題當初是拿 judge 現在**已經拿不到的輸入**"
              f"寫的（見各題 known_limitation）。它們哪天變成 PASS 是好消息，該回頭撤掉標記。")
    if n_flaky:
        print("⚠ 「時好時壞」**不是通過**——但也不是回歸。judge 是 MoE：同一份碼、同一個模型，"
              "三個單輪拿到 10/11、9/11、11/11，而 `--repeat 3` 的逐題表跟那三個單輪並不一致"
              "（`col01_true_fabrication_negative` 三個單輪全過、repeat 3 只中 1）。"
              "要拿這支做 A/B，比的是**逐題 k/n**，不是單輪總分；n=1 的結論一律不可信。")
    # ⚠ 退出碼只認「全掛」：那才是確定的回歸。時好時壞交給人看 k/n，用退出碼把噪音
    #   變成紅燈，這支就會被當成壞掉而沒人再跑——本檔壞了一個月正是這樣來的。
    if n_never:
        sys.exit(1)


if __name__ == "__main__":
    main()
