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

Usage:
  python eval/judge_regression.py
  python eval/judge_regression.py --judge-model openai/gpt-oss-120b --votes 3
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from eval.eval_generation_llm_judge import (
    evaluate_correctness_with_feedback_voted,
    compute_correctness,
)


# ── 固定案例：每題含 query/answer/rubric/hall_result + 期望結果 ─────────────
# hall_result 模擬「hallucination 指標已經跑過、且有實際 chunk 依據」的結果摘要，
# 不重新呼叫 hallucination judge（那是獨立指標，不在本套件驗證範圍）。

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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
        "hall_result": {
            "hallucination_rate": 0.33,
            "claims": [{"claim": "NVIDIA 在 AI 加速晶片市場的市佔率高達 92%", "supported": False,
                        "reason": "sources 中無此市佔率數字"}],
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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
        "hall_result": {"hallucination_rate": 0.0, "claims": []},
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
                "hall_result 標記為 supported（模擬 hallucination 指標已用真實 chunk 驗證過）。"
                "回歸目標：mn0 不應違規。",
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
        "hall_result": {
            "hallucination_rate": 0.0,
            "claims": [
                {"claim": "2025 年最後一季服務營收達 310 億美元，較前季成長 16%", "supported": True,
                 "reason": "News chunk #4 原文：Services climbed 16% to a record $31.0 billion"},
            ],
        },
        "expect": {
            "mi_hits_required": {"mi0"},
            "mn_violations_forbidden": {"mn0"},
        },
    },
    {
        "id": "sem03_true_fabrication_negative",
        "note": "反例（人工構造）：答案捏造一個沒有出處佐證的成長率數字，hall_result 標記為 unsupported。"
                "驗證判定沒有變成「看到 hallucination_rate 低就一律放行」。",
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
        "hall_result": {
            "hallucination_rate": 0.5,
            "claims": [
                {"claim": "未來三年服務營收將以 45% 複合年增長率擴張", "supported": False,
                 "reason": "chunk #80 原文無此預測數字"},
            ],
        },
        "expect": {
            "mn_violations_required": {"mn0"},
        },
    },
]


def run_case(case: dict, model: str, votes: int) -> dict:
    verdict = evaluate_correctness_with_feedback_voted(
        votes=votes,
        query=case["query"],
        answer=case["answer"],
        rubric=case["rubric"],
        hall_result=case["hall_result"],
        relevance_score=None,
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
    ap.add_argument("--judge-model", default="qwen/qwen3-32b",
                    help="與生產/主要 eval 腳本一致的 judge model（預設對齊 CLAUDE.md 記載的 judge，"
                         "2026-07-12 起為 qwen3-32b，見 CHANGELOG）")
    ap.add_argument("--votes", type=int, default=1)
    ap.add_argument("--case", default=None, help="只跑單一 case id（除錯用）")
    args = ap.parse_args()

    cases = [c for c in CASES if args.case is None or c["id"] == args.case]

    print(f"judge_regression: model={args.judge_model} votes={args.votes} cases={len(cases)}")
    print("=" * 78)

    results = []
    for case in cases:
        r = run_case(case, args.judge_model, args.votes)
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['id']}")
        if not r["passed"]:
            for f in r["failures"]:
                print(f"    - {f}")
        if r["error"]:
            print(f"    ! judge error: {r['error']}")

    n_pass = sum(1 for r in results if r["passed"])
    print("=" * 78)
    print(f"{n_pass}/{len(results)} passed")

    if n_pass < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
