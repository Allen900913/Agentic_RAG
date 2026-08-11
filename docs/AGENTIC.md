# Agentic 管線知識庫

> **這裡放什麼**：agentic 各節點為什麼長這樣、validator 的設計與實測、檢索層共用決定、以及架構診斷主線（哪些「更 agentic」的做法是負資產）。

**這裡不放什麼**：日期式變更記錄（→ [`CHANGELOG_AGENTIC.md`](../CHANGELOG_AGENTIC.md)）、切塊層（→ [`INGEST.md`](INGEST.md)）、量測（→ [`EVAL.md`](EVAL.md)）。

## 目錄

- [A3 Agentic 架構診斷主線（v2 價值＝確定性 planner 拆解；最 agentic 的自由迴圈是負資產）](#a3-agentic-架構診斷主線v2-價值-確定性-planner-拆解-最-agentic-的自由迴圈是負資產)
- [A2 檢索層決定（多在 `rag_query.py`，agentic 複用）](#a2-檢索層決定多在-rag_query-py-agentic-複用)
- [A5 2026-08-07 雙臂 100 題對照 + 一致性 validator（新增確定性層）](#a5-2026-08-07-雙臂-100-題對照-一致性-validator新增確定性層)
  - [A5.1 2026-08-08 改架構：LLM 抽取 + Python 判斷（取代純 regex 判定）](#a5-1-2026-08-08-改架構-llm-抽取-python-判斷取代純-regex-判定)
  - [A5.2 2026-08-08 In-Place Healing 提案的分析（結論：定位可行、裁決無解，病灶在更上游）](#a5-2-2026-08-08-in-place-healing-提案的分析結論-定位可行-裁決無解-病灶在更上游)
  - [A5.3 2026-08-08 移除 regex 降級路徑](#a5-3-2026-08-08-移除-regex-降級路徑)
  - [A5.4 2026-08-08 期間接地：讓一致性 validator 看得到原文（三個改動，踩過兩個錯的設計）](#a5-4-2026-08-08-期間接地-讓一致性-validator-看得到原文三個改動-踩過兩個錯的設計)
  - [期間接地（2026-08-08 加，`agentic_rag_v2.py` 的消費端）](#期間接地2026-08-08-加-agentic_rag_v2-py-的消費端)

---

## A3 Agentic 架構診斷主線（v2 價值＝確定性 planner 拆解；最 agentic 的自由迴圈是負資產）
- **multi_intent 退步三階診斷（逐階自我推翻）**：① 檔案層級 probe：v2 檢索**不弱反大勝**（final8 覆蓋 28/38 gold 檔 vs 單管線 12/38，嚴格超集）→推翻「多源檢索弱」。② claim/chunk 層級 probe：真實 v2 只 16/75 gold chunk、**乾淨 decomposition（無 Agent A）拿 35/75**、單管線 25/75→**煙槍：漏水點在 Agent A(ReAct) 執行層亂改 query＋亂搜，非 planner 拆解、非 doc-type filter**。③ 修法＝v2 minus ReAct（executor 改 deterministic：plan→每子問題直接 retrieve→commit）＝**v2 現狀**。
- **multi_intent recall 殘因（2026-08-06）**：真因兩機制——(A) **Grader 對雙來源擲硬幣**：毛利率/淨利率/成長率等可從 10-K/10-Q 現算的指標，池中 Fundamentals(TTM 寫死) 與 10-Q(可現算) 近似平手，Grader `relevant_ids` 一次只圈 1 個→擲硬幣，eval 那次圈中 10-Q（季度口徑 vs gold TTM）。**關鍵洞見：只有雙來源可計算指標會低 recall＝指標型別非題型**（單一來源 P/E/ROE/市值沒得挑錯故 recall 高）。(B) Plan 期間解讀分歧＋開放式子意圖答太薄。「gold 灌水」假設查證後**撤銷**（系統答太窄非 gold 太寬，動 gold＝gaming eval，違反不降級 GT，不做）。
- **multihop 依賴解析（2026-08-03，新能力）**：Type B「識別再查」第二跳帶未解代名詞→`_is_dependent_hop`/`_resolve_hop_entity`/`_fill_dependent_hop`，`_node_execute` 分波延後＋實體回填、`_node_replan` 兩道 guard，**全程無 LLM 改寫**（確定性回填不幻覺）。10 題零拒答、實體解析 5/5 命中；100 題 multi_hop faith **0.904**/recall **0.892**/corr **0.710** 全類最紮實。Type A 比較題 6 子問題（3家×2指標）是必要非過度拆。不走 Gemini 的 Plan-and-Execute 重寫（YAGNI，eval 全 2-hop）。
- **corrected verdict（2026-07-28，此語料下）**：結構化 txt＋長文 filing 混合＋中型模型下，動態編排自由度是負資產；生產維持單管線，agentic 真正舞台是多跳/開放 web/真路由工具。

## A2 檢索層決定（多在 `rag_query.py`，agentic 複用）
- **`full_translate_en`（架構定案，2026-07-27）**：中文問中文答，但 dense/sparse 召回＋rerank 一律用英文譯句，消除 cross-lingual reranker 對「中文 query × 英文 chunk」評分平坦（news-08 正解 chunk 中文 rerank≈0 壓到 rank6，英文後 rank2 跨過門檻）。全 90 題 RAGAS 六項全升，precision/nv_relevance 漲最多(+0.059/+0.062)。agentic `_retrieve_chunks` 固定開；**生產 `rag_query.py` 預設仍 False，未套用（跟進與否未決）**。
- **`translate_query_en`（舊 flag，只影響 rerank）**：`max(原句分,英譯分)` 重評、不動召回池，生產單次查詢常態預設 True（曾兩次誤植成 escalation-only，勿再犯）；agentic 主路徑已被 full_translate_en 取代。
- **`period_basis` TTM 口徑消歧義（2026-07-30）**：Fundamentals.txt 已含預算好的 TTM 比率（毛利率/淨利率/成長率/P/E）＝**路由問題非算術**，不該讓 LLM 現算。每 chunk 加 `period_basis` payload（fundamentals→TTM、10-K/Q→fiscal_year）；`rag_query._detect_period_basis` 命中 TTM 詞→`period_basis=TTM` **單向**硬 filter（fiscal_year 有標籤但不硬路由，避免回歸；開雙向前先跑全量 eval）。當初的一次性遷移腳本 `migrate_add_period_basis.py` **已不在 repo**（`4791dc2` 整理時刪掉，未納版控＝已永久遺失；要重做得重寫）。SYSTEM_PROMPT Rule 10：財務數字必帶口徑標籤、相對詞多口徑→兩個都給並標。同日去重 0508/0519 舊快照。
- **gap2 `mentioned_tickers`（2026-08-02）**：市場級新聞物理歸檔某一家但內文提及全七家，owner-ticker 硬 filter 把 gold 全排除（即使 rerank 全場最高）。news chunk 加 `mentioned_tickers` 陣列 payload、retrieve 改「陣列包含 X」；**只動 news，財報維持 owner ticker 單值**（財報提到對手 ≠ 報表是對方的）。multi_intent recall 0.593→0.638、faith +0.073、corr +0.061、**precision −0.135**（七公司 chunk 內生代價＋單跑噪音）。淨賺收下。
- **col-07 否定框架檢索限制（已知限制，決定不修）**：query「非市佔龍頭」框架 vs 語料「minority market share」框架，BGE dense+sparse+reranker 跨不過否定/框架鴻溝，連正確英譯也跨不過（只有已知答案字面＝HyDE 才撈到 rank1）。不為單題上 HyDE（全管線改、有反傷）。gold 亦同源標錯已修（24/103/110）。
- **EDGAR chunking Exp0→4**：管線越換越乾淨（edgartools Item 邊界＋RCTS 消 oversized/截斷），Faithfulness 單調升到 Exp4 新高；Answer Correctness 單調降是長度假象非真退步。生產＝Exp4（`us_stock_rag_edgar_exp4`）。

## A5 2026-08-07 雙臂 100 題對照 + 一致性 validator（新增確定性層）

**實驗設定**：同 collection（`us_stock_rag_edgar_exp4`）、同 `full_translate_en=ON`（只控制編排這一個變因）、同 gold（當日修完的 `reference_answers.json`，MD5 `ae6b165a03ba`）。結果檔 `experiments/agentic/gj_{single_ft,v2}_full100_20260807.json` → `ragas_{single_ft,v2_ft}_full100_20260807.json`。判讀一律套 **0.067 噪音底線**。

**結論（OVERALL 單管線/agentic）**：ctx_prec .790/**.864**、nv_ctx .873/**.950** 是真勝；ctx_rec .740/.748、**answer_correctness .628/.637 不可分辨**。分類看：**multi_intent 全面翻盤**（recall +.217、nv_ctx +.400、ans_rel **+.397**、corr +.095）、**multi_hop 檢索三項全過門檻**（prec +.250 最大）；代價是 **mixed/semantic 的 recall 退步**（−.123／−.094）——agentic 平均只收 3.31 chunk / 6.6k 字（單管線 4.94 / 10.7k），38 題 ≤2 chunk。∴ **ctx_prec 的漲幅有一部分是機械性的**（收得少必然精準），別當純品質提升讀。

**faithfulness −0.068 查證為量尺偏誤，非幻覺**（詳見 memory `faithfulness-penalizes-completeness`）：
- 4 個 agentic faith=0.000 逐題查證全是判官誤判或拒答退化（mix-01 的「40%」在 context 逐字、lex-08 的 `$37.01B` 逐字、col-07 是拒答）。剔除後 .801 vs .839＝**−0.038 < 噪音底線**。
- **拒答紅利**：mh-07 單管線寫「reference materials do not provide…」只剩 1 個聲明→**1.000**；agentic 兩跳都答對（`$716,924` 確實在 context 的 10-K 損益表）→**0.333**。
- **雙寫稀釋**：agentic 寫 472 個數字 vs 單管線 302；逐字命中 56% vs 79%、「換算後對得上」39% vs 17%。判官逐聲明查**字面**支持，算術正確的換算形字面不在來源 → 判 unsupported。
- 長度中介假說**被推翻**（單管線短答 .910 > 長答 .811）；footer 剝除機制沒壞（100/100 命中）。

**真錯誤只有 2 個，且都不是算術**（推翻「LLM 減法弱、該給 calculator」的直覺）：
- **mix-06**：把 `Six Months Ended 2025` 欄（25,884）當 Q2 2026 → 25,884−13,624=12,260、÷13,624=90.0%，**減法除法全對**，錯在運算元的期間欄。正解 24,768−13,624=11,144 → $11.1B／82%。
- **mix-03**：把 Intelligent Cloud **單一部門**的 +$2.7B/24% 當全公司。它自己列的三部門增幅 3,594+2,658+146=**6,398≈$6.4B**、÷32,000=**20.0%**＝正解——**運算元全在它自己的答案裡、全正確，只是選錯要報哪個**。
- ∴ calculator/code-interpreter 對兩題都零效果：mix-06 會忠實回傳同樣的錯誤答案，mix-03 根本沒觸發運算（它以為在引述）。**病灶在算術之前的「範圍/期間」選擇。**

**新增：確定性一致性 validator**（`find_numeric_conflicts` / `_consistency_check_and_fix`，接在 citation validator 之後、reflect 之前）
- 抓「同一主體同一指標、並陳兩組互斥數值卻不調和」。純 regex 零 LLM，只有觸發才花一次重生成。
- **刻意少報**：必須有模型自己端出第二組數字的措辭（`_ALSO_MARK`：另一段落／文件亦指出…）才判衝突。無此條件時「整體 vs 部門/產品線」的合法並列會大量誤殺——離線實測 4 個觸發只有 1 個是真的（mi-01 總營收 vs 資料中心、mix-02 部門 vs Windows&Devices、news-03 公司 vs iPhone 週期皆誤報）。**靜默選錯運算元（答案裡只有一組數字）確定性抓不到**，是已知盲區。
- **離線量測（先量再接，未動管線就先跑過 200 份存檔答案）**：agentic 2/100（mix-03、mix-06）、單管線 1/100（mix-02，搜尋廣告 +10%/$1.0億 vs +9%/$304M 未調和）——**3/3 全真陽性、零誤報**。單管線也中，證明此病灶非 agentic 專屬。
- **修復路徑端到端驗證**：把兩份存檔壞答案灌回修復路徑 → mix-06 改出 **$11.1B／82%**、mix-03 改出 **$6.4B／20%**，皆與 gold 一致，修復後再偵測 0 組衝突。
  > 註：live 重跑 mix-06 當場產出的是**正確**答案（LLM 隨機性，該輪無衝突、validator 未觸發）。∴ 修復路徑是用存檔壞答案定向驗證的，不是靠 live 重現。
- **「怎麼知道兩個數字在講同一件事」——它不知道，只是無法證明不同**（2026-08-08 追問後補測）：③④ 原本都是免責式比較（`a and b and a != b`，**兩邊都有標記才排除**），所以從未證明共指。真正扛住正確率的是 ②：`_ALSO_MARK` 抓的不是我對數字的語意判斷，而是**生成器自己宣告**「這是同一個指標的另一種說法」——把不可解的共指問題換成可讀的表面標記。①「指標詞相同」很粗糙（「資料中心營收」裡也有「營收」），分不出總體與分部；mi-01／mix-02／news-03 三個「整體 vs 部分」誤報全靠 ② 才沒被殺。
  - **crafted 探測找到兩個真實破口**：③ 依賴寫死的 `_CONSIST_ENTITIES`，分部名不在清單裡（如「伺服器產品」）→ `ent` 抽成空 → 放行 → 誤報；④ 期間只有單邊標記時同樣失效。
  - **修法（實測驗證後才改）**：③ 改成**對稱**比較（`a["ent"] != b["ent"]` 即排除，含兩邊皆空）——擋掉「伺服器產品」誤報，且 **3/3 真陽性全保留**（mix-06 兩句 ent 相同、mix-03 命中那組兩句 ent 皆空）。④ **維持非對稱**：mix-06 是 `period=q vs None`，改對稱就漏抓。
  - **殘餘風險（未測）**：誤報時模型被告知「有衝突」，是否可能反而刪掉本來正確的數字？重寫 prompt 已明寫「若兩組都正確必須各自標出期間或範圍」允許保留兩者，但此路徑未做定向測試。
- 附帶發現：生成端偶爾吐 **U+FFFD 壞字**（mix-06 的「營收」變「�收」），第一版偵測器因此漏抓 → `_consist_metric_in` 已容忍指標詞任一字被替換。

### A5.1 2026-08-08 改架構：LLM 抽取 + Python 判斷（取代純 regex 判定）

**動機（使用者提的，正確）**：「為什麼不先讓 verifier 抽出所有數字以及代表什麼指標，再用程式碼去判斷？」——原本的 regex 版把分界線畫錯了。「確定性 > 勸 LLM」講的是**不要把判斷交給 LLM**，不是不要用 LLM 做抽取；本管線 Plan=LLM／Execute=確定性就是同一個分工。**抽取是感知任務（LLM 強），比對是邏輯任務（Python 可靠）。**

**新結構**：`_extract_claims`（CHECKER_MODEL，temperature=0）把答案每個數值宣稱抽成 `value/unit/metric/entity/scope/period/kind/basis/quote` → `find_claim_conflicts` 跑零 LLM 規則。（初版保留 regex 為降級路徑，**2026-08-08 已移除**，見 A5.3。）

**換來 regex 結構上做不到的 R2（分部加總 vs 合併總計）**：mix-03 的輸出從「有兩個數字對不上」升級成指出正解——「回報的全公司增幅 2700 比它自己列的最大單一部門增幅 3594 還小，且與各分部加總 **6398** 對不上」。R2 判準刻意收窄成「回報的合併值比它自己列的某個分部還小」，才不會誤殺「只列兩三個部門當佐證」的合法寫法（對照組實測：正解 6400 不觸發、分部只列 2 個不觸發）。

**第一版規則太天真，量測直接打臉**：agentic 觸發 8 題只有 ~2 個站得住（regex 版是 2/2）。誤報全是抽取器的**系統性樣態**，修在規則層而不是回去勸抽取器——因為抽取本身沒錯（把「$330 提升至 $365」看成兩個 level 是正確的，錯的是我拿 level 去比）：
- **ⓐ `level` 不參與比對**（只比 change／growth_pct）：「從 X 增至 Y」抽出的兩個 level 幾乎都被填同一個 period（mi-08 637,959→716,924、mix-12 391億→752億）。
- **ⓑ `basis` 進分組 key**：YoY 92% vs QoQ 21%（mix-11）、reported 33% vs 固定匯率 29%（mix-08）是合法並列。
- **ⓒ 共用同一段 quote 者不比**：區間「EPS 增加 1 至 3 美元」(news-10) 被拆成兩筆。
- **ⓓ 百分比加總 ≈100 → 佔比不是矛盾**：sem-09 的 Reality Labs 支出 70%/30%。
- **ⓔ 同格出現 3 個以上相異值 → 列舉不是矛盾**：mix-08 一句 `Regional data ... (+29%, +39%, +40%)` 是四個地區，entity 全被填成 Meta。真正的「同一個量兩種互斥讀法」是二選一。

**最終量測（200 份存檔答案，claims 抽一次存檔後離線重評）**：

| | agentic | single | 精準度 |
|---|---|---|---|
| regex 版 | mix-03, mix-06 | mix-02 | 3/3 |
| **LLM 版（新規則）** | mix-03, mix-06 | mix-02, **col-11** | **4/4** |

**col-11 是 LLM 版獨有的真陽性、regex 版漏抓**：單管線答案自己寫「Microsoft 365 商業雲端收入…增長 **18%**【chunk #109】；**另一份報告則顯示**同一期間增長 **19%**【chunk #105】」，四個指標各有兩個互斥值全未調和。regex 版漏抓的原因就是 `_ALSO_MARK` 清單裡沒有「另一份報告則顯示」——**這正是硬編碼措辭清單的必然破口**。

**兩個量測方法教訓**：
1. 第一輪腳本用 `if claims` 判斷成敗，**空 list `[]` 也是 falsy** → 27/29 題合法的空抽取（敘述型答案本來就沒有可比較數值）被誤報成「解析失敗」。實際解析失敗率是 **0**。
2. 第一輪只存觸發數量沒存 claims，導致每改一次規則就要重跑 200 次 LLM（約 20 分鐘）。第二輪改成**抽取一次存檔、規則離線重評**，後續三輪規則迭代成本歸零。

**成本**：每份答案固定多一次 CHECKER_MODEL 呼叫（中位 5.1s，平均抽出 4.0 筆宣稱／題）；原 regex 版是零成本、只有觸發才付費。

### A5.2 2026-08-08 In-Place Healing 提案的分析（結論：定位可行、裁決無解，病灶在更上游）

**提案**（使用者提出）：不要把整段丟給 LLM 重寫，改用程式碼/輕量 NER 精準定位出錯的那一句，只抽換、抹除或標記那一個數值，周圍不動。動機是 A5.1 末尾發現的 col-11 反例——重寫把「內部矛盾」換成了「有自信的錯標」。

**拆成兩個能力來看：定位、裁決。**

**① 定位：可行，而且不需要 NER**——`_extract_claims` 已經回傳 `quote`。拿 591 條存檔 claim 量：

| | agentic | single |
|---|---|---|
| quote 逐字命中答案 | 34.3% | 52.3% |
| **正規化空白/全形後命中** | **85.1%** | **88.1%** |
| 抽取器改寫原文（定位不可能） | 14.9% | 11.9% |

逐字只有 34% 是假象：51 個百分點純粹是 ` `（窄不斷行空格）與 markdown `**` 的差異。剩下 12~15% 是抽取器真的改寫（`'Nvidia (NVDA)…上漲約 +2.22%'` 的 `…` 是它自己縮的），但比對不上就 fallback，可偵測。**再上一個 NER 去重推已經有的東西，只是多一層失敗面。**

**② 裁決：提案沒有處理，而 col-11 的病灶正好落在這裡。** 把 col-11 的衝突印出來是四組，不是一組：

```
Microsoft 365 商業雲端  18% / 19%      LinkedIn      11% / 12%
Microsoft 365 消費者雲端 29% / 33%      Dynamics 365  20% / 22%
```

回 Qdrant 對原文：`#98`／`#105` 是 19/33/12/22（**無期間標頭**），`#109` 是 18/29/20（`Nine Months Ended`）。**兩組數字都是對的**——19% 是單季、18% 是前九個月。

這直接打穿提案的操作集 `{抽換, 抹除, 標記}`：**抽換**沒有東西可以換進去、**抹除**會刪掉正確資訊、**標記**是唯一活下來的但那等於承認不確定而非修復。真正缺的是**期間限定詞**，那是要**插入**的文字，而且要知道哪個數字配哪個限定詞——必須回去讀原文。In-Place Healing 只是把「重寫整段」換成「重寫一句」，裁決能力一點都沒增加。

**③ 真正的發現在 ingest 層**：`#105`/`#98` 沒有期間標頭，是切塊時把 `Three Months Ended … Compared with …` 這行標頭切到別的 chunk 去了。**生成器不是讀錯，是資訊不在 context 裡。** → 修法與量測見 CLAUDE.md「期間章節邊界」小節（已實作於 `data_update_edgar.py`，**需重建 collection 才生效**）。與 mix-06 的「四欄表分不清哪欄」同一族病：**期間標籤在切塊時脫落**。

**「太多 Python/regex 是否正確」**（使用者同時問的）：判準不是用得多不多，是**該子任務有沒有唯一正確答案**。抽取＝開放感知→LLM；比對＝封閉邏輯→Python；定位＝給定 quote 的封閉問題→Python；**裁決＝要回讀原文→LLM，而這層現在不存在**。按這個表，Python 沒有太多，是少了一層。真正用錯地方的是 regex 降級路徑（見 A5.3）。

### A5.3 2026-08-08 移除 regex 降級路徑

刪除 `find_numeric_conflicts` 與 `_CONSIST_*` 詞表（含 `_consist_metric_in`／`_consist_money`／`_consist_period`／`_consist_close`）、env `AGENTIC_CONSISTENCY_LLM`。`_consistency_check_and_fix` 現在抽取失敗就跳過這層。

**兩個理由**：
1. **它在用字串比對做感知**——指標靠寫死詞表（「資料中心營收」裡也有「營收」）、主體靠寫死的公司/分部清單（不在清單就抽成空）、並陳靠寫死的措辭清單。每加一家公司、每換一種說法就要改表。而且已經被實測抓包：A5.1 的 col-11 漏抓，原因就是「另一份報告則顯示」不在措辭清單裡。
2. **它從來沒跑過**——觸發條件是「LLM 抽取失敗」，200 份答案的離線量測失敗率是 **0**。等於一段沒被測過、卻要永久維護的死碼。跳過檢查不會比降級偵測差（本來就沒檢查）。

**回歸驗證**：拿同一份 `claims_all.json` 離線重評，`find_claim_conflicts` 觸發結果與刪除前逐題相同（agentic: mix-03×3, mix-06×1；single: mix-02×3, col-11×4）。無衝突答案原樣回傳的路徑亦 smoke 過。

### A5.4 2026-08-08 期間接地：讓一致性 validator 看得到原文（三個改動，踩過兩個錯的設計）

**起點是使用者的質疑**：col-11 的病灶既然是「chunk 讀不出期間」，那**抽取層的欄位是不是也該更精準**？驗證後成立一半——`_extract_claims` 的 user content 只有 `f"答案:\n{answer}"`，**它看不到任何 chunk，所以無法糾正答案的錯，只能忠實複製**。而 col-11 的答案自己把單季 19% 寫成「九個月期間」，抽取層照抄 → 兩筆同期 → 必然誤報。

**改動**：① `_extract_claims` 加 `chunks` 參數並餵原文 ② 期間判準從自由文字 `period` 換成 `period_key = f"{period_months}M@{period_end}"` ③ 新增零 LLM 的 `_ground_period_from_source`（拿數字回 chunk 定位期間段並覆寫）。

**受控實驗**（答案文字固定＝重現 col-11 錯誤樣態，只換餵進去的原文）：

| 組別 | 誤報組數（3 輪） |
|---|---|
| ① 不餵原文 | 1 / 0 / 1 |
| ② 餵舊 collection 原文（無期間標籤） | 2 / 1 / 2 ← **比不餵更糟** |
| ③ 餵新 collection 原文（有標籤） | 0 / 0 / 0 |
| ④ ③的同一批 chunks，程式剝掉注入標籤 | 2 / 1 / 0 |

③ vs ④ 是乾淨對照——**同一批 chunks，唯一差別是那行 `[Three Months Ended…]` 在不在**，剝掉就退回 ② 的水準。故效果來自標籤，不是新 collection 檢索變好。② 更糟的原因：原文殘留的章節標題讓抽取器把四筆全套上同一期。**兩層缺一不可，且 ingest 必須先做**。

**中途走錯一次，記下來**：加了「舉證稽核」——要求 LLM 填 `period_evidence`，填不出原文出處就把期間降級成 `unknown`。誤報測從 3/6 降到 **0/8**，看起來成功。但補跑真衝突偵測是 **0/5**：捏造的數字必然在原文查不到 → 必然降級 → R1 永遠抓不到幻覺。**誤報歸零是因為整層被關掉了**。正確順序是「先偵測、再用原文駁回」，查不到出處要**保留答案自述的期間**。

**收斂版**（誤報 5/5 全對、真衝突 5/5 全中，且十輪零變異——判斷在 Python，無抽樣變異）：期間定位改用零 LLM 字串比對。實測讓 LLM 自己查證，8 輪只有 4 輪真的去查、其餘直接填 `answer` 照抄答案。

**回歸**：`claims_all.json` 200 份離線重放觸發數仍為 4/200（agentic 2、single 2），與改動前相同——`_period_key` 在兩欄任一缺時退回舊 `period` 字串，舊 schema 行為逐字保留，故既有的四道排除規則校準不受影響。端到端 smoke 兩題正常。

**副產物**：新 collection 下生成層**自己就把兩期分開陳述**了（「以九個月為基礎…18%」「以三個月為基礎…19%」），col-11 的矛盾根本沒產生，validator 不需介入。

**限制**：`_ground_period_from_source` 只處理百分比（金額寫法太多，誤配風險高過收益）；且依賴 chunk 帶標籤，對 `us_stock_rag_edgar_exp4` 這層等於不存在。擋不掉「查了但查錯」（evidence 抄了原文某個真實標題，但不是該數字所在段落）。

### 期間接地（2026-08-08 加，`agentic_rag_v2.py` 的消費端）
> ⚠ **依賴 chunk 帶期間標籤前綴**，只有重建過的 collection（如 `us_stock_rag_edgar_period`）才生效；對 `us_stock_rag_edgar_exp4` 這層等於不存在（`_chunk_period` 找不到標籤就直接 return，行為退回舊版）。

上面那層修的是「期間資訊存不存在」，這層修的是「一致性 validator 讀不讀得到」。三個改動，**踩過兩個錯的設計才收斂**：

| 做法 | col-11 誤報 | 真衝突偵測 | 判斷 |
|---|---|---|---|
| 原版（只餵答案給 `_extract_claims`） | 2/3 誤報 | 有 | 抽取層看不到原文,只能忠實照抄答案標錯的期間 |
| 餵原文，但 collection 無期間標籤 | **3/3 誤報**（更糟） | 有 | 原文殘留的章節標題讓它把全部套上同一期 |
| 餵原文 ＋ LLM 自己查證 | 2~3/6 誤報 | 有 | 8 輪只有 4 輪真的去查,其餘直接填 `answer` 照抄 |
| ＋ 舉證稽核（查不到就降級 unknown） | 0/8 ✅ | **0/5 ❌** | **把偵測器關掉了**——見下 |
| **＋ 零 LLM 期間接地**（現行） | **0/5** ✅ | **5/5** ✅ | 十輪完全一致（判斷在 Python,無抽樣變異） |

**「查不到出處就降級 unknown」是錯的，而且錯得隱蔽**：捏造的數字必然在原文查不到 → 必然降級 → R1 永遠抓不到幻覺，而幻覺正是要抓的東西。誤報歸零看起來像成功，其實是把整層關掉。**正確順序是「先偵測、再用原文駁回」，不是「先降級、再偵測」**；查不到出處就**保留答案自述的期間**，讓 R1 照原本方式判「答案有沒有自相矛盾」。

**`_ground_period_from_source` 為什麼是 Python 不是 LLM**：「這個數字出現在哪一段」是字串定位＝封閉邏輯（見 memory `llm-vs-python-task-split`）。保守條件是**只在該數字唯一落在一個期間段時才覆寫**，跨多段或無標籤一律不動；目前只處理百分比——金額寫法太多（`$2.6 billion`／`$2,600 million`／`26 億美元`），誤配風險高過收益。

`period_key` 取代自由文字 `period` 當同格判準：實測同一個「截至 2026/3/31 的九個月」被 LLM 寫成過 `2026YTD`／`2026Q1Q2Q3`／`2026Q3`／`unknown` 四種，而 `2026Q3` 還同時被拿去指單季與累計 → 字串比對雙向失效（真同期判成不同期＝漏抓；不同期塌縮成同期＝誤報）。`period_months`/`period_end` 直接讀原文標題就有，**不需要換算成財年季度代碼，而換算正是出錯的那一步**。
