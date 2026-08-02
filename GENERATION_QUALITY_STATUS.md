# 生成品質改善狀態（2026-08-02）

本輪聚焦「**生成層**」的讀法紀律，不動檢索 / filter / 模型 / agentic 節點結構。核心洞察：
`answer_correctness` 長期偏低是**兩件事疊加**——(1) RAGAS claim-F1 措辭天花板（改系統無用），
(2) 少數**生成讀壞**的真 bug（被 run 噪音藏住）。本輪處理 (2) 裡「prompt/程式治得動」的部分。

---

## 一、已落地的機制

### 1. 生成 prompt 規則（`rag_query.py` `SYSTEM_PROMPT`，單管線＋agentic 兩路共用）

| 規則 | 內容 | 修的病 |
|---|---|---|
| **Rule 10 期間口徑** | 每個財務數字必帶 FY/TTM/Q 標籤；相對詞（最新/近一年）多口徑不同值 → 兩個都答並標 | FY vs TTM 混淆 |
| **Rule 11 單位** | **來源寫 `billion` 就原樣保留、別轉億**（billion≠億，轉億=×10） | 億/billion 差 10x |
| **Rule 12 方向** | 引用來源方向詞原文、保留符號、兩期數值靠日期定先後，**禁止臆測「在改善」** | 漲跌反轉、符號脫落 |
| **Rule 13 per-intent** | 多意圖某腿只要有相關 chunk 就得答，**禁止假性「無資訊」** | 第二意圖假拒答 |

> evidence-first 變體的舊 Rule 11 順移為 14，內文 `rules 1-10` → `1-13`。

### 2. 確定性單位換算（`rag_query.convert_usd_units_to_yi()`）

- **動機**：billion→億 是 LLM 的**翻譯層 token 習慣**，prompt（Rule 11）只能隨機壓、reflect 也共用同盲點。
  實測 news-09「叫它 ×10 算對」兩跑一對一全錯。
- **正解**：Rule 11 要 Writer 原樣保留 `$X billion` → 純程式把 `billion`(×10)/`million`(×0.01)/`trillion`(×10000)
  換算成正確的億。程式算不會錯、**零誤報**（`X billion` render 成 `X 億` 100% 是錯）。
- **接入點**：`agentic_rag_v2.py` synthesize，reflect 之後、freshness 之前。

### 3. 忠實度稽核升級（`agentic_rag_v2._REFLECT_PROMPT`）

- 從模糊「找幻覺」→ **結構化逐 claim 蘊含 + 雙向**：
  - 【捏造/矛盾】數字/單位/方向/主體/期間對不上或牴觸來源
  - 【假性查無（反向）】答案說「未提及」但來源其實有
- 護欄：判事實不判措辭；換句話說 / billion 換算億 / FY-vs-TTM 口徑差 **不算錯**（避免誤殺）。
- 同模型 `gpt-oss-120b`、溫度 0（照決議**不換模型**；gpt-oss-20b 比 generator 弱不適合，Gemini 留必要時才上）。

---

## 二、驗證結果（agentic snapshot smoke，逐題對 gold）

| 測點 | 病 | 結果 |
|---|---|---|
| news-09 | 單位 | ✅ end-to-end 六數字全正確億（847.5/347.5/400/100/1,850/915 億美元） |
| mi-14 | 方向 | ✅ 舊「improved」→ 新「下降 17.2→16.8」，反轉消失 |
| mix-02 | 符號 | ✅ 舊「OEM 上升 2%」→ 新「fell -2%」 |
| mh-10 | 漏答 | ✅ 舊「net income not disclosed」→ 新 `$60.46B`（=gold） |
| mi-12 | 漏答 | ✅ Rule 13 **正確不捏造**（該 chunk 真的沒 SpaceX 拋售；病灶是檢索，見下） |
| col-14 | 幻覺 | ✅ reflect 移除捏造的「GPU vesting 條件」 |
| mi-10 | 幻覺 | ✅ reflect 移除捏造的「15 個產品全用 Gemini」 |
| col-03 | （非幻覺） | ✅ reflect **正確地沒 flag**——原文真有 `$15B OpenAI Series C`，系統對且比 gold 完整 |
| mi-02 | （非誤） | ✅ reflect **正確地沒 flag**——74.9%(Q1) vs 71.1%(TTM) 口徑差，系統已標 |

**語意稽核淨評**：0 誤殺、移除 2 個真 embellishment、正確保留 2 個 grounded 事實 → 有判別力，非橡皮圖章。

---

## 三、順帶修正 / 發現的 eval 側問題（非系統缺陷）

- **mi-11 gold 修正**：原文 `$380 billion`，gold 誤寫「380 億」($38B) → 改為「3,800 億美元（約 $380 billion）」。**單位型 false-negative gold**。
- **col-03 / mi-02 / mi-10 = false-negative gold 或口徑差**：先前 bincorr「~26 真 error」清單**被高估**，這幾題其實系統對。
  真實正確率比先前估的 0.645 更高。
- ⚠ **`eval/reference_answers.json` 是 gitignore 的生成產物** → 上述 gold 修正**不進版控**（repo 既有設計）。

---

## 四、尚未處理（本輪範圍外）

1. **檢索側缺口**（非生成規則能修）：
   - mi-12 跨公司市場新聞（hedge-fund/SpaceX 只在 TSLA/NVDA/AAPL/MSFT 的 news 檔，`META_News` 沒有）→ Meta-scoped 檢索撈不到。
   - 「最新一季」未 deterministic 路由（單管線 mix-06/col-13 撈到舊季度）。
2. **單管線 SSE 串流**未接單位換算（`api_server.py` token 串流，post-hoc 轉換較麻煩；Rule 11 的 verbatim billion 本身可讀且正確）。
3. **全量 eval（agentic 100 + RAGAS）尚未重跑**——拿真實 before/after；需連 gold artifact 一起看才準。
4. col-03 gold 補完（加 `AMZN_10Q_202603.html` 到 relevant 並重生成）— 選配 eval hygiene。

---

## 相關記憶（`.claude/.../memory/`）
- `eval-false-negative-gold.md` — mi-11 單位型、col-03 grounded 的登記
- `period-basis-ttm-disambiguation.md` — Rule 10-13 與單位換算的決策脈絡
