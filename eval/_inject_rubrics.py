# -*- coding: utf-8 -*-
"""一次性腳本：把 rubric（評分得分點）與 answerable 旗標注入 eval_set.json。

rubric schema（每題）：
  answerable: bool                      該題在知識庫中是否可答（拒答率用）
  rubric:
    must_include:    [{checkpoint, weight, is_critical}]  正面得分點 → Correctness
    must_not_include:[{checkpoint, is_critical}]          負面致命點 → Faithfulness

weight 慣例：critical 必中=50、high=30、mid=20。
Correctness = 命中 must_include 權重和 / must_include 權重總和；
若漏掉 is_critical=true 的 must_include，或命中任一 must_not_include，整題視為嚴重失分。
數值得分點的數字全部來自 data/processed 對應檔（2026-06-12 generated）。
"""
import json
from pathlib import Path

P = Path("eval/eval_set.json")
data = json.loads(P.read_text(encoding="utf-8"))


def mi(cp, w, crit=False):
    return {"checkpoint": cp, "weight": w, "is_critical": crit}


def mn(cp):
    return {"checkpoint": cp, "is_critical": True}


# id -> (answerable, must_include, must_not_include)
R = {
    # ── SEMANTIC ─────────────────────────────────────────────────────────────
    "sem-01": (True, [
        mi("提到 CUDA 軟體生態 / 開發者鎖定 / 軟硬體全端平台", 50, True),
        mi("提到資料中心 GPU 的效能領先或加速運算平台", 30),
        mi("提到網路整合（NVLink / InfiniBand / Mellanox）或系統級整合", 20),
    ], [mn("捏造未在來源出現的具體市佔率百分比或營收數字")]),

    "sem-02": (True, [
        mi("提到 Copilot 跨 Microsoft 365 / 產品線的 AI 整合", 50, True),
        mi("提到 Azure AI / 雲端 AI 基礎設施作為成長引擎", 30),
        mi("提到與 OpenAI 的合作或自研 AI 模型投資", 20),
    ], [mn("捏造未在來源出現的具體 AI 營收數字")]),

    "sem-03": (True, [
        mi("提到服務業務營收成長或其高毛利特性", 50, True),
        mi("提到訂閱服務生態（App Store / iCloud / Music / TV+ 等）", 30),
        mi("提到龐大裝置安裝基數帶動服務變現", 20),
    ], [mn("捏造未在來源出現的服務業務具體成長率百分比")]),

    "sem-04": (True, [
        mi("具體點出至少兩家公司的風險，而非泛泛而談", 50, True),
        mi("涵蓋競爭 / 監管 / 供應鏈 / 總體經濟 其中數項具體風險", 30),
        mi("風險陳述有對應到正確的公司", 20),
    ], [mn("把某公司的風險錯誤歸屬到另一家公司")]),

    "sem-05": (True, [
        mi("Mentions full-stack platform / CUDA software ecosystem", 50, True),
        mi("Mentions performance leadership or accelerated computing", 30),
        mi("Mentions networking (NVLink / InfiniBand / Mellanox) or system integration", 20),
    ], [mn("Fabricates a market-share percentage or revenue figure not in the sources")]),

    "sem-06": (True, [
        mi("提到垂直整合 / 自研晶片讓 Apple 掌控軟硬體", 50, True),
        mi("提到效能或能效優勢", 30),
        mi("提到降低對第三方（如 Intel）的依賴", 20),
    ], [mn("捏造未在來源出現的晶片效能具體數字")]),

    "sem-07": (True, [
        mi("把 Azure 定位為雲端三強之一 / 與 AWS、GCP 競爭", 50, True),
        mi("提到 AI / Copilot 帶動 Azure 成長", 30),
        mi("提到企業 / 混合雲 / 既有客戶優勢", 20),
    ], [mn("捏造未在來源出現的 Azure 市佔率或營收數字")]),

    "sem-08": (True, [
        mi("把 AWS 定位為雲端市佔龍頭 / Amazon 的獲利引擎", 50, True),
        mi("提到 AI / ML 服務投資（如 Bedrock、自研晶片）", 30),
        mi("提到基礎設施擴張 / 資本支出", 20),
    ], [mn("把 Amazon 整體營收當成 AWS 營收")]),

    "sem-09": (True, [
        mi("提到 Gemini 模型 / AI 整合進搜尋", 50, True),
        mi("提到搜尋廣告核心業務的防禦或變現", 30),
        mi("提到 Google Cloud 或自研 TPU", 20),
    ], [mn("捏造未在來源出現的搜尋市佔或 AI 營收數字")]),

    "sem-10": (True, [
        mi("提到 AI 投資（基礎模型 / 推薦系統 / Meta AI）", 50, True),
        mi("提到 Reality Labs 的 VR/AR 長期投入與虧損", 30),
        mi("提到 AI 眼鏡（Ray-Ban Meta）等穿戴裝置", 20),
    ], [mn("捏造未在來源出現的 Reality Labs 具體虧損或營收數字")]),

    "sem-11": (True, [
        mi("提到 EV 市場競爭加劇 / 價格戰", 50, True),
        mi("提到自動駕駛（FSD / Autopilot）的監管風險", 30),
        mi("提到總體經濟 / 利率 / 補貼政策風險", 20),
    ], [mn("捏造未在來源出現的具體召回或訴訟金額")]),

    # ── LEXICAL（財報數字題）─────────────────────────────────────────────────
    "lex-01": (True, [
        mi("給出 P/E 數值，trailing 約 31.3（forward 約 16.1 亦可）", 50, True),
        mi("標明是 trailing 或 forward，或註明資料期間", 20),
    ], [mn("給出與來源（trailing≈31.3）明顯不符的本益比")]),

    "lex-02": (True, [
        mi("給出 EPS 數值（trailing 約 8.25，或 FY2025 diluted 7.46）", 50, True),
        mi("標明 trailing / 年度 / 對應期間", 20),
    ], [mn("給出與來源明顯不符的 EPS 數字")]),

    "lex-03": (True, [
        mi("給出毛利率約 68.3%（0.683）", 50, True),
        mi("標明資料期間或來源", 20),
    ], [mn("給出與來源（約 68%）明顯不符的毛利率")]),

    "lex-06": (True, [
        mi("給出 FY2026 總營收 $215.94B", 50, True),
        mi("給出 FY2026 毛利 $153.46B 或淨利 $120.07B 或 diluted EPS $4.90", 30),
    ], [mn("把 FY2025 的數字（營收 $130.50B 等）當成 FY2026")]),

    "lex-08": (True, [
        mi("給出 EPS 數值（trailing 約 7.63，或 FY2025 diluted 7.17）", 50, True),
        mi("標明 trailing / 年度 / 對應期間", 20),
    ], [mn("給出與來源明顯不符的 EPS 數字")]),

    "lex-09": (True, [
        mi("給出毛利率約 60.4%（0.604）", 50, True),
        mi("標明資料期間或來源", 20),
    ], [mn("給出與來源（約 60%）明顯不符的毛利率")]),

    # ── MIXED ────────────────────────────────────────────────────────────────
    "mix-01": (True, [
        mi("給出營收 YoY 成長率（約 85%）或引用具體季度成長", 50, True),
        mi("引用營收絕對值或對應期間", 20),
    ], [mn("捏造與來源不符的營收成長率")]),

    "mix-02": (True, [
        mi("指出 iPhone 銷售 / 營收的趨勢方向（成長或下滑）", 50, True),
        mi("引用財報或新聞作為依據", 20),
    ], [mn("給出未在來源出現的精確 iPhone 銷量數字")]),

    "mix-03": (True, [
        mi("提到 Azure / Intelligent Cloud 的成長表現", 50, True),
        mi("引用季度營收或成長率", 20),
    ], [mn("把其他季度的數字當成最新季度")]),

    "mix-04": (True, [
        mi("指出至少一項具體商業動向（新品 / 合作 / 訂單）", 50, True),
        mi("動向來自新聞來源", 20),
    ], [mn("捏造未在新聞中出現的事件")]),

    "mix-05": (True, [
        mi("指出服務業務的高毛利特性", 50, True),
        mi("指出服務業務的成長趨勢", 30),
    ], [mn("在來源未提供時捏造服務毛利率的具體百分比")]),

    "mix-06": (True, [
        mi("給出 EPS（trailing 約 16.78，或 FY2025 diluted 13.64）", 50, True),
        mi("給出年增率（盈餘成長約 23%，或 EPS YoY）", 30),
    ], [mn("捏造與來源不符的 EPS 或年增率")]),

    "mix-07": (True, [
        mi("指出資料中心 / AI 晶片營收強勁成長", 50, True),
        mi("引用營收或成長數字", 20),
    ], [mn("錯置財報期間或捏造數字")]),

    "mix-08": (True, [
        mi("指出 AWS 營收成長表現", 50, True),
        mi("引用季度數字或成長率", 20),
    ], [mn("把 Amazon 整體營收當成 AWS 營收")]),

    "mix-09": (True, [
        mi("指出 Google Cloud 的成長或獲利表現", 50, True),
        mi("引用數字", 20),
    ], [mn("把其他季度或整體營收當成 Google Cloud 最新季度")]),

    "mix-10": (True, [
        mi("指出廣告業務營收成長", 50, True),
        mi("指出營業利益率 / 盈利能力（營業利益率約 40.6%）", 30),
    ], [mn("捏造與來源不符的盈利率或成長率")]),

    "mix-11": (True, [
        mi("指出財報重點（營收 / 獲利 / 毛利率約 19%）", 50, True),
        mi("結合新聞或市場反應", 20),
    ], [mn("捏造未在來源出現的市場反應或股價變動")]),

    "mix-12": (True, [
        mi("給出毛利率數值（TTM 約 74.1%，或該季 GAAP 毛利率）", 50, True),
        mi("指出毛利率的變化方向或與前期比較", 30),
    ], [mn("捏造與來源不符的毛利率")]),

    "mix-13": (True, [
        mi("明確對應到 2026 年 3 月底結束的季度（202603）", 50, True),
        mi("引用該季的營收數字", 30),
    ], [mn("把 202512 季的數字當成 202603 季")]),

    "mix-14": (True, [
        mi("明確對應到 2025 年 12 月底結束的季度（202512）", 50, True),
        mi("引用該季的營收數字", 30),
    ], [mn("把 202603 季的數字當成 202512 季")]),

    "mix-15": (True, [
        mi("明確對應到 202510 那一季", 50, True),
        mi("引用該季的資料中心營收數字", 30),
    ], [mn("把 202604 季的數字當成 202510 季")]),

    "mix-16": (True, [
        mi("明確對應到 202604 那一季", 50, True),
        mi("引用該季的資料中心營收數字", 30),
    ], [mn("把 202510 季的數字當成 202604 季")]),
}

count = 0
for q in data["queries"]:
    spec = R.get(q["id"])
    if not spec:
        continue
    answerable, must_inc, must_not = spec
    q["answerable"] = answerable
    q["rubric"] = {"must_include": must_inc, "must_not_include": must_not}
    count += 1

# 沒有 rubric 的純檢索題：標記 answerable 但留空 rubric（generation 不計分）
for q in data["queries"]:
    if "answerable" not in q:
        q["answerable"] = True
        q["rubric"] = None

data["_meta"]["rubric_schema"] = {
    "answerable": "bool — 知識庫中是否可答；用於拒答率（answerable=true 卻拒答=錯誤拒答）",
    "rubric.must_include": "正面得分點 [{checkpoint, weight, is_critical}] → Correctness",
    "rubric.must_not_include": "負面致命點 [{checkpoint, is_critical}] → Faithfulness（命中即幻覺）",
    "scoring": "Correctness = 命中 must_include 權重和 / 總權重；漏 is_critical 或命中 must_not_include 視為嚴重失分",
    "null_rubric": "rubric=null 表示純檢索題，不做 generation 評分",
}

P.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"injected rubric into {count} queries; total {len(data['queries'])}")
