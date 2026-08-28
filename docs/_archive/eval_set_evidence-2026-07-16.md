# eval_set.json Gold 驗證紀錄（2026-07-16）— **已封存**

> ⚠ **這是歷史記錄，不要拿它當現況。** 它描述的是 **100 題**時代的題庫（`lex-25`~`lex-34`、`col-14`~`col-25`、`sem-16`~`sem-25` 等題號在現行 **65 題**裡已不存在），語料路徑 `data/unstructure_processed/` 也已改成 `data/edgar_processed/`，行號全部漂移。
>
> **仍然適用的維護規則已收進 [`../../eval/README.md`](../../eval/README.md) §3**（快照漂移、10 倍單位錯、false-negative gold、概念型舉例要 grep 有源、跑分前先掃污染）。保留本檔是因為它記載了**當初怎麼查證**，那個方法本身還有參考價值。

本文件記錄 eval set 從 61 題擴充到 100 題（lexical/mixed/semantic/colloquial 各 25 題）時的逐題查證：每個 checkpoint 都以 grep / 逐字比對確認在 `data/unstructure_processed/` 語料中真實存在（labeling granularity 與 eval 一致：source file 層級）。驗證方法：數字型 checkpoint 逐字核對 Fundamentals / IncomeStatement / 10-Q 原文；概念型 checkpoint 用關鍵詞 grep 確認至少一個 relevant 檔案含有對應內容。

## 一、既有 61 題驗證結果

### 發現並修復的缺陷（5 項）

| 題目 | 缺陷 | 修法 |
|---|---|---|
| **sem-06 / col-09** | **整題超綱**：全語料唯一的 "silicon" 是 10-K 人才段落的 "Silicon Valley"；`\bIntel\b`、自研晶片相關敘述 0 命中。原題（Apple Silicon 垂直整合）語料完全答不出來，高分答案只可能來自模型世界知識＝獎勵幻覺 | 整題替換為 Apple Intelligence 題（實據：`AAPL_News_20260612_02`「Siri AI, which rides on Apple Intelligence」＋ PCC 跑 Nvidia 晶片/Google Cloud；`AAPL_News_20260612_05`「As Apple Intelligence features proliferate across devices, we expect multi-year upgrades, improved monetization, and expanded recurring revenue」） |
| **lex-01** | **快照漂移**：`NVDA_Fundamentals_*` glob 同時匹配三份快照，trailing P/E 分別為 43.08（0508）/ 45.46（0519）/ 31.33（0612），rubric 只認 31.3——檢索若撈到舊快照，grounded 正確答案會被判死 | rubric 放寬為「任一快照值皆可」，must_not 改為「與所有快照皆不符」 |
| **mix-01** | 同類漂移：營收 YoY 舊快照 TTM=73.2%、新快照 85.2%、10-Q 202604 原文 "up 85%" | checkpoint 註明三個有源數值 |
| **mix-12** | 同類漂移：毛利率舊快照 71.07%、新快照 74.14% | checkpoint 註明兩者皆有源，加 tolerance_pct=1.5 |
| **sem-08 / col-05** | +30 checkpoint 括號舉例「（如 Bedrock、自研晶片）」——Bedrock/Trainium/SageMaker/Graviton 全語料 **0 命中**（CHANGELOG 07-08 glossary 建立時已知，rubric 漏改） | 舉例改為語料真實用詞（artificial intelligence and machine learning technologies / technology infrastructure） |

### 通過驗證的關鍵抽查（節錄）

- **lex-02/col-06**：AAPL EPS trailing 8.25（三份快照一致）；FY2025 diluted 7.46（IncomeStatement）✓
- **lex-03/col-03**：MSFT Gross Margin 0.68309（三份快照一致）；10-K FY2025 193.89/281.72=68.8% 在 tol 內 ✓
- **lex-06**：NVDA FY2026 營收 215.94B / 毛利 153.46B / 淨利 120.07B / dEPS 4.90；FY2025 130.50B ✓（IncomeStatement 逐字）
- **lex-08**：AMZN EPS trailing 7.63 / FY2025 diluted 7.17 ✓
- **lex-09**：GOOGL GM 0.60368 ✓；**mix-06**：MSFT EPS 16.78（16.77~16.81 跨快照）/13.64、growth 0.234 ✓
- **mix-10**：META operating margin 0.40617 ✓；**mix-11**：TSLA GM 0.19065 ✓
- **lex-05/mix-03**：MSFT 10-Q 202603「Azure and other cloud services revenue increased 40%」✓
- **lex-07**：AAPL 10-Q 202603 iPhone（Pro 帶動）與 Services（advertising/App Store/cloud）敘述 ✓
- **lex-11**：TSLA 10-Q 202603 automotive GM 21.1%（前期 16.2%）✓
- **lex-12**：AAPL 10-Q 202512 iPhone Pro/Services/iPad 成長、Mac/Wearables 下滑 ✓
- **lex-22**：MSFT 10-Q 202512 毛利 $55,295M、Microsoft Cloud GM 67% ✓
- **mix-15**：NVDA 10-Q 202510「Data Center revenue was $51.2 billion, up 66%」✓
- **mix-01 主值**：NVDA 10-Q 202604「Revenue was $81.6 billion, up 85%」✓
- 概念型：CUDA（NVDA_10K×5）、NVLink/InfiniBand/Mellanox（3 檔×13）、OpenAI（MSFT 3 檔×32）、Gemini/AI Overviews（GOOGL 5 檔）、TPU/Ironwood（GOOGL_10K）、Reality Labs/Ray-Ban/metaverse（META 4 檔×53）、price reduction（TSLA_10K chunk，即已知 #67）、Autopilot/FSD（TSLA 3 檔×16）、installed base（AAPL 10-K+News）、Jensen Huang / Tim Cook / Vision Pro / Blackwell / H100 ✓

### 已知殘留（不修）

- lex-13~lex-21 為 retrieval-only（rubric=null），僅驗證 relevant 檔案存在，皆通過。
- 三份日期快照並存是刻意設計（測時間性），但**未來新增數字型題目時，rubric 必須枚舉所有快照值**（本次 lex-01/mix-01/mix-12 的教訓）。

## 二、新增 39 題的證據（逐題）

新題原則：先找到語料原文、再反推題目與 rubric，因此 checkpoint 逐字有源。以下記錄每題的證據位置（行號為 `data/unstructure_processed/*.txt` 內行號，會隨資料更新漂移，僅供人工複查起點）。

### lexical（lex-25 ~ lex-34）

| id | 證據 |
|---|---|
| lex-25 | `GOOGL_IncomeStatement`: FY2025 Total Revenue $402.84B、FY2024 $350.02B；`GOOGL_10K` L1113「Revenues were $402.8 billion, an increase of 15%」 |
| lex-26 | `GOOGL_Fundamentals_0612`: EPS Trailing 13.09；IncomeStatement FY2025 Diluted EPS $10.81 |
| lex-27 | `META_Fundamentals_0612`: EPS Trailing 27.51；IncomeStatement FY2025 Diluted EPS $23.49 |
| lex-28 / col-25 | `META_Fundamentals_0612`: Gross Margin 0.81941 |
| lex-29 | `AMZN_IncomeStatement`: FY2025 Total Revenue $716.92B、FY2024 $637.96B |
| lex-30 | `TSLA_Fundamentals_0612`: EPS Trailing 1.08；IncomeStatement FY2025 Diluted 1.08 / Basic 1.18（FY2024 2.04） |
| lex-31 / col-23 | `TSLA_IncomeStatement`: FY2025 $94.83B < FY2024 $97.69B（營收下滑，must_not 抓「聲稱成長」） |
| lex-32 | `AMZN_Fundamentals_0612`: Operating Margin 0.1314 |
| lex-33 | `MSFT_IncomeStatement`: FY2025 $281.72B、FY2024 $245.12B |
| lex-34 | `META_10K` L729「our investments in Reality Labs reduced our 2025 overall operating profit by approximately $19.19 billion, and we expect our 2026 Reality Labs operating losses to remain similar to 2025」 |

### semantic（sem-12 ~ sem-25）

| id | 證據 |
|---|---|
| sem-12 / col-11 | `GOOGL_10K` L614-626：反壟斷/監管程序、私人與集體訴訟、「awards of monetary damages and remedies that could harm our business」 |
| sem-13 / col-12 | `GOOGL_10K` L1113/1213/1330：Cloud +36%/+$15.5B、GCP infrastructure and platform services 驅動、operating income +$7.8B；`GOOGL_10Q_202603` L661/760：Q1 +63%/+$7.8B |
| sem-14 / col-13 | `META_10K` L725-729：AI/RL 投資「have the effect of reducing our operating margin and profitability」＋ $19.19B ＋ infrastructure and headcount |
| sem-15 / col-14 | `META_Fundamentals_0612` Business Summary：Ray-Ban Meta、Oakley Meta、Meta Ray-Ban Display、Neural Band（electromyography）、RL 分部涵蓋 Quest 與 wearables |
| sem-16 / col-15 | `AMZN_Fundamentals_0612`／`AMZN_10K`：「three segments: North America, International, and Amazon Web Services (AWS)」＋各分部內容 |
| sem-17 / col-16 | `AMZN_News_20260612_01`：€10B 歐洲投資、fulfillment 現代化、Proteus/STARK 機器人、25,000 jobs、Amazon Now 擴展 Manchester/Birmingham |
| sem-18 / col-17 | `TSLA_10K` L145/159/165：兩大分部、Megapack（Autobidder）、Powerwall（Powerhub）、solar |
| sem-19 / col-18 | `TSLA_IncomeStatement`：FY2025 淨利 3.79B < FY2024 7.13B；`TSLA_10Q_202603` L579：auto GM 16.2%→21.1%；L526：regulatory credits 380 vs 595（-36%） |
| sem-20 / col-19 | `MSFT_10Q_202512` L598-600：「Microsoft Cloud gross margin percentage decreased to 67% driven by continued investments in AI infrastructure and growing AI product usage, offset in part by efficiency gains」；Azure +39%/+40% |
| sem-21 / col-20 | `NVDA_Fundamentals`／`NVDA_10K`：Compute & Networking + Graphics 兩分部；gaming/professional visualization/data center/automotive 市場 |
| sem-22 | `AAPL_News_20260612_04` L39/43/45/165：mix shift 向高毛利服務、2.5B+ 裝置、Services GM 74.2%、services growth >14%；`AAPL_10Q_202603`：Services net sales increased |
| sem-23 / col-22 | `GOOGL_10Q_202603` L729：YouTube ads +$956M、direct response 領先 brand advertising、advertiser spending 增加 |
| sem-24 / col-21 | `AMZN_10K` L806（net sales 107,556→128,725）、L928（op income 39,834→45,606）、L937（增長主因 increased sales，部分被 technology infrastructure 投資抵銷） |
| sem-25 / col-24 | `NVDA_10Q_202510` L532/570：Blackwell 為 DC 營收主力、Blackwell Ultra leading、three platform shifts（accelerated computing / AI models / agentic applications）、$51.2B +66%、H20 insignificant |

### colloquial（col-11 ~ col-25）

全部為鏡像題（口語問法迴避財務術語），rubric 與 relevant 完全沿用 `maps_to` 目標題（同既有 col-01~col-10 慣例），證據同上表。

## 四、100 題 baseline 首跑（2026-07-16，原始路徑無 rewrite/translate，gen=120b，單次）

`eval/generation_correctness_100q_baseline.json`：overall correctness 0.776（lexical 0.891 / mixed 0.962 / semantic 0.712 / colloquial 0.565）。**分類基準不可與舊 61 題版本（semantic 0.842 / colloquial 0.67）直接比較**——題目集合、部分 rubric 已變。

新增 39 題單獨統計：mean 0.637，11/39 critical_miss。逐一查證後分類如下（方法：先看完整答案原文，比對 judge verdict 是否站得住腳，同 07-11 慣例「先排除判定工具的毛病」）：

### 確認是 rubric 缺陷，已修復
- **sem-18 / col-17**（Tesla 能源事業）：critical checkpoint 原要求「提到能源生成與儲能是與汽車並列的報告分部」——但驗證發現 `TSLA_Fundamentals` chunk #1 原文就寫著「operates in two segments, Automotive; and Energy Generation and Storage」，且**兩份答案都引用了這個 chunk**，只是沒有把「報告分部」這個行政框架講出來，業務內容本身（Megapack/Powerwall/Autobidder/Powerhub）答得完整詳實。判定：把一個行政分類事實錯放成 critical，真正的核心內容（能源事業做什麼）才該是 critical。已調整權重（產品內容→critical 50、軟體平台→30、分部框架→降為 non-critical 20 且放寬措辭要求）。

### 確認是 judge 誤判，不改 eval_set（記錄供未來 judge prompt 修法參考）
- **sem-23**（YouTube 廣告）：答案寫「9.56 億美元」，換算 9.56×1億=$956,000,000=$956M，與 rubric 要求的 $956M **完全一致**，judge 卻判定「無意中多加了一個零」而判 critical_miss。這是中文「億」單位換算的 judge bug，屬於 CLAUDE.md 已知的「judge 對財年/數字措辭系統性誤判」同類問題，新增一個子類型（中文大數單位換算）。**不修 eval_set**（rubric 本身沒錯），留待未來 judge prompt 加中文數字單位換算規則時一併處理。

### 確認是真實系統發現（RECALL 缺口 / 生成層 bug），不算 eval_set 缺陷
- **sem-24 / col-21**（AWS 營收規模）：語料裡 `AMZN_10K_2025.html` 有完整 FY2025 AWS 分部表（營收 $128,725M、營業利益 $45,606M，見 evidence §二 sem-24 證據），但檢索只撈到季度 10-Q 數據（Q1 2026 $37.6B 或 9M2025 $78.8B），年度分部表 chunk 沒進 top-5。真實 RECALL 缺口。
- **lex-34**（Meta Reality Labs 虧損）：語料裡同一份 10-K 對同一件事有兩種框架的數字——風險因子敘述句「reduced our 2025 overall operating profit by approximately $19.19 billion」（年度總影響）與分部表「9 個月 $13.2B、全年 YoY +8%」（分期 + 變動率）。兩者換算一致（不矛盾），但檢索撈到的是分部表 chunk、沒撈到風險因子那句話。真實 RECALL 缺口，非 rubric 錯誤（$19.19B 已在既有 evidence §二 grep 驗證過）。
- **sem-19 / col-18**（Tesla 獲利能力變化）：`TSLA_IncomeStatement`（有乾淨的 FY2025 vs FY2024 年度淨利對比）沒被撈到，模型改用 `TSLA_Fundamentals` 的 TTM 欄位（`Earnings Growth: 0.083`，即 +8.3%）與 `TSLA_10Q_202509` 的季度數據作答，導致答案結論「盈利能力提升」與年度真實情況（淨利 $3.79B < $7.13B，下滑 47%）相反。這裡有兩層值得注意：① 真實 RECALL 缺口（年度 IncomeStatement 沒進池）；② **`Earnings Growth` 這個 Yahoo Finance 欄位本身的計算基準不明**（可能是分析師預估或非 GAAP 年度口徑），與 IncomeStatement 的年度淨利對比方向相反——之後若處理這題，需連同判斷「這個欄位是否該被視為可信來源」一併考慮，不能只當成單純的召回排序問題。
- **sem-17**（Amazon 歐洲投資）：**生成層新發現的 bug**——原文「€10 billion」被 120b 翻成中文「超過 €10億」，但中文「10億」=1,000,000,000=１ billion，是原文 €10 billion 的 **1/10**，屬於中翻英大數單位換算錯誤。judge 正確抓到這個落差（「underreporting actual €10B figure」），是一個 judge 判對、生成錯的案例，與 sem-23（judge 判錯、生成對）恰成對照。CLAUDE.md 尚未記錄過這個 bug 類型（生成層中文「億」單位換算），值得之後複測是否穩定重現。

## 五、TPD 污染事故記錄（2026-07-16~17）——與 CHANGELOG 07-15「rewrite/70b vs 8b 對照失效」同型

跑完 baseline 後緊接著跑 `--rewrite --translate-query-en` 版本（`eval/generation_correctness_100q_rewrite.json`），事後逐題查證發現**兩組跑分污染程度差很多，不能直接拿來做 A/B**：

| | baseline（無 rewrite） | rewrite+translate（第一次） |
|---|---|---|
| 真正因 429 而空白的題（非設計上 rubric=null 的 lex-13~21） | 2（col-02, col-06） | **22**（semantic 11 + colloquial 11） |
| 污染分布 | 隨機、可忽略 | **集中在最後跑到的 semantic/colloquial**，且落在最想驗證的兩個類別 |

根因：`--rewrite --translate-query-en` 每題多燒 retrieval 側（filter+rewrite+translate）LLM 呼叫，4 把 key 合計 800k tokens/day（`openai/gpt-oss-120b`）的額度撐不到 100 題跑完；4-key 自動輪換有觸發（3 次），但輪完 4 把 key 後額度仍不夠，之後的題目全部靜默留空（`answer_len=0`, `score=None`），且**未被排除出分母**——category summary 仍顯示 n=25，只是 `n_with_rubric` 悄悄降到 14，若不追查會誤把「倖存題目的平均」當成「25 題的真實平均」，重演 07-15 的教訓。

**处理方式**：不修改 eval_set（這是額度問題非 gold 缺陷）；跨日重跑一次乾淨版本（`eval/generation_correctness_100q_rewrite_v2.json`），並在**分析任何 correctness 輸出前，先檢查 `n_with_rubric` 是否等於該類別應有的滿額題數**，此後成為固定檢查步驟。baseline 版本的 2 題污染（col-02/col-06）數字太小、不影響已回報結論，予以保留不重跑，但註記在案。

**新增規則寫入維護原則**：分析任何 correctness JSON 前，先跑污染掃描（比對 `records` 裡 `correctness.score is None` 的 id，扣掉設計上 `rubric=null` 的題目後看剩餘數量），非零就代表這組數字有 TPD 或其他錯誤污染，不可直接用於 A/B 或當作最終結論。

## 六、維護原則

1. **新增數字型題目時，先確認該指標是否跨快照漂移**；會漂移的（P/E、市值、YoY、TTM margin）必須枚舉所有快照值或直接 pin 單一日期檔案。
2. **概念型 checkpoint 的括號舉例必須 grep 有源**——舉例詞彙若語料沒有，judge 可能拿舉例當唯一判準（sem-08 Bedrock 教訓）。
3. 資料 refresh（`fetch_data.py`）後應重跑本文件的抽查——行號會漂移，數值可能換代。
