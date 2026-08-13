# 評測與量測知識庫

> **這裡放什麼**：怎麼量、量到的數字能不能相信、每個指標的噪音與可追空間、量尺自己壞掉的歷史、以及**已試無效**的清單。

**跑任何 A/B 之前必讀「量測噪音與重放快取」與「已試無效總表」。**

**這裡不放什麼**：eval 腳本清單（→ [`CLAUDE.md`](../CLAUDE.md) 檔案地圖）、待辦（→ [`BACKLOG.md`](../BACKLOG.md)）。

## 目錄

- [已試無效總表（改動前先查這裡，避免重踩）](#已試無效總表改動前先查這裡-避免重踩)
  - [量尺飽和：切塊這條路已經到頂（2026-08-13，**提任何 ingest 實驗前必讀**）](#量尺飽和切塊這條路已經到頂2026-08-13-提任何-ingest-實驗前必讀)
  - [量測噪音與重放快取（2026-08-09，**跑任何 A/B 之前必讀**）](#量測噪音與重放快取2026-08-09-跑任何-a-b-之前必讀)
  - [數字缺陷指標的四次失效（2026-08-10，**拿它當閘門前必讀**）](#數字缺陷指標的四次失效2026-08-10-拿它當閘門前必讀)
- [A1 Eval 量尺與方法論（最大宗；不懂這層會把量尺 bug 當系統缺陷追）](#a1-eval-量尺與方法論最大宗-不懂這層會把量尺-bug-當系統缺陷追)
  - [A5.5 2026-08-09 量測基礎建設：先讓實驗可被相信，再談優化](#a5-5-2026-08-09-量測基礎建設-先讓實驗可被相信-再談優化)
- [反覆出現的方法論教訓](#反覆出現的方法論教訓)

---

## 已試無效總表（改動前先查這裡，避免重踩）

| 項目 | 死因 | 復活條件 | 日期 |
|---|---|---|---|
| RRF-fusion（跨 query-variant 排名融合） | pool 是妥協排名，候選集更雜訊化 | 無（機制型） | 07-06 |
| chunking 切細救 rerank 分數 | 孤立段落無上下文，cross-encoder 評分反更低 | 換對上下文無關的評分方式 | 07-07 |
| dense+sparse 一起翻英文 | dense 翻譯本身有害，sem-11 退步 | 已拆開測試（見下條），仍死 | 07-08 |
| `sparse_translate_en`（只翻 sparse） | 救不回任何 RECALL checkpoint，還傷 sem-11 | 無 | 07-08 |
| Rule 10 prompt（策略題強制含財務數字） | 注意力層級問題，指令命令不動埋沒的訊號 | 已被句級抽取證偽同因 | 07-14 |
| section-aware chunking（拆稀釋型大 chunk） | retrieval 層有改善但 end-to-end 無可靠增益 | 若多題受益證據出現，全量重估 | 07-14 |
| 檢索後句級抽取（contextual compression） | 目標句已搬到最前面，生成仍 2/3 不引用——病灶是模型主動略過，非訊號埋沒 | 無（決定性診斷） | 07-14 |
| `bge-reranker-base`（小 reranker 換速度） | position embedding 上限 512 token，長 chunk 中後段看不到 | 換支援長 context 的小 reranker | 07-13/14 |
| ONNX Runtime fp32 | 無加速（1.05x） | 無 | 07-13 |
| ONNX int8 動態量化 | 2.18x 加速但 correlation 掉到 0.858，排序改變 | QAT/校準式靜態量化+完整驗證 | 07-13 |
| 級聯精排（base 篩 top-10 → v2-m3 精排） | lexical 翻車，critical chunk 被踢出 | 保守篩選收益不值得，或換模型 | 07-13 |
| `max_length=512`（reranker 截斷） | sem-11 退步，關鍵句在段尾被截斷 | chunk 長度上限大幅壓低 | 07-13 |
| col-08 投入新召回機制（HyDE 等） | dense rank 落差(25/106)超出射程，成本不值得 | eval set 擴大、同類失敗多題重現 | 07-12 |
| RAGAS ground_truth 用 rubric 清單合成 | 篇幅錯配，F1 精確率崩塌，分數假性極低(~0.31) | 已解決——改用完整黃金參考答案 | 07-15 |
| GGUF/fp8（reranker 量化，僅分析未跑） | 加速靠量化（同 int8 風險）+ 整合成本高；fp8 純 CPU 無加速 | CPU 再榨速度且願付驗證成本時 | 07-13 |
| few-shot 修 `parse_query_filters` | 範例共現模式被模仿成新錯誤，8b few-shot 80.0% < 8b zero-shot 85.0% | 換避開該共現的範例組合 | 07-15 |
| `z-ai/glm-5.2` 當 agentic GEN_MODEL | NVIDIA NIM 上單發 127~217s，跑 eval 不可行 | 該模型端上加速 | 08-01 |
| `deepseek-ai/deepseek-v4-pro` 當 agentic GEN_MODEL | 品質/中文最佳但 per-model 429 硬牆，100 題必團滅（實測 34 題 22 fallback） | 該模型放寬單模型限速 | 08-01 |
| **繼續改切塊／ingest 求 RAGAS 分數** | **量尺飽和**：六指標五個已達或超過 gold 上限；檢索全修好只值 +0.024，低於噪音 | 換沒飽和的量尺，或改生成層 | 08-13 |
| **改用「最簡單」的 collection（`period`，無小標層）** | 零噪音的 `check_number_defects` 判得出來：**mix-03 FAIL**（分部 24%／27 億冒充公司整體，答案拼自 3 個 chunk） | 無（已被同源對照證偽） | 08-13 |

**復活成功案例**：`rerank_multi_query`（07-07 判死，變體仍中文）在「英文變體+glossary」新前提下（07-08）復活，現已併入生產雙 query 精排機制。

---

### 量尺飽和：切塊這條路已經到頂（2026-08-13，**提任何 ingest 實驗前必讀**）

**結論：RAGAS 的檢索類指標已經量不到切塊改動了，`answer_correctness` 的剩餘空間在生成層不在檢索層。**

#### (1) 六個指標有五個已達或超過 gold 上限

上限常數＝把 `reference_answers.json` 原文當成系統答案餵進去評的分數（`eval_ragas_vs_rubric.py` 的 `GOLD_BASELINE`）。它回答「這個指標還有沒有可追空間」：

| metric | gold 上限 | `mdna` 實測 | 判讀 |
|---|---|---|---|
| `context_recall` | 0.766 | **0.770** | 已超過 |
| `context_precision` | 0.822 | **0.831** | 已超過 |
| `nv_context_relevance` | 0.965 | 0.963 | 到頂 |
| `faithfulness` | 0.656 | **0.808** | 已超過（負空間，見〈已接受的極限〉） |
| `answer_relevancy` | 0.842 | 0.831 | 剩 0.011 |
| `answer_correctness` | 0.989 | 0.660 | **唯一有空間的（+0.329）** |

切塊／ingest 改動只能影響前三個檢索指標，而它們全部飽和。**再改也量不到，不是「改動沒效果」而是「尺沒有刻度了」。**

#### (2) 檢索已經不是瓶頸——把 100 題按檢索成敗分桶

| 分桶 | 題數 | 平均 `answer_correctness` |
|---|---|---|
| `context_recall` = 1.0（檢索完全成功） | 44 | 0.684 |
| 0.5 ~ 0.99 | 42 | 0.669 |
| < 0.5（檢索失敗） | 12 | 0.543 |

檢索完美與檢索全失敗只差 **0.141**。就算把 100 題的檢索全修到完美，correctness 也只從 0.660 推到 ~0.684＝**+0.024，低於 0.067 的噪音底線**。而且 44 題檢索完美的題裡只有 5 題 correctness < 0.5。

#### (3) 歷史軌跡佐證

2026-08-06 之後的十二次全量跑分，`context_recall` 全部落在 **0.73~0.77**、`answer_correctness` 全部落在 **0.62~0.66**。整個離散範圍就是噪音寬度。真正的躍升發生在 08-02 → 08-06（correctness 0.585 → 0.626），之後每一次都在同一個平台上擺盪。

#### (4) 剩下那 0.329 有多少是真的？部分不是

`lex-07`（Apple FY2025 Diluted EPS）系統答 **$7.46**、`lex-14`（Meta）答 **$23.49**——**兩題數字都完全正確、引用也對**，`answer_correctness` 卻只有 0.415／0.424。扣分來自參考答案多寫了佐證陳述（分子分母），RAGAS 的 statement-level F1 把它們算成 false negative。**它罰的是陳述集合的差異，不是對錯。**

⚠ 但不能因此說整個 gap 都是假的：gold 自己拿 0.989（85/100 滿分），代表指標**給得出滿分**。量過 `log(答案長度比)` 與 correctness 的相關係數只有 **+0.125**（弱相關），所以也不是單純的冗長度問題。目前沒有把「真的答錯」與「陳述顆粒度不同」分開的工具。

#### (5) 所以還能往哪走

1. **改生成層**——讓 Generator 補齊佐證陳述。唯一還能動 `answer_correctness` 的槓桿，但要先想清楚要的是「答案更好」還是「分數更好」（`lex-07` 現在的答案對使用者已經夠好）。
2. **換量尺**——`check_number_defects` 那種確定性斷言是零噪音的，只是覆蓋率僅 6 題。要數字驅動開發就得擴充它。
3. **接受現況收工。**

---

### 量測噪音與重放快取（2026-08-09，**跑任何 A/B 之前必讀**）

**核心事實：這個專案的量測解析度比多數改動的效果粗一個數量級。**

| 噪音層 | 實測 | 量法 |
|---|---|---|
| RAGAS judge | `context_precision` **0.046**（兩次獨立樣本 0.0459／0.0452 複現）、`context_recall` 0.002～0.013 | 同一份結果檔重評兩次（`--from-results` 指同一檔、`--output` 換名） |
| Plan 節點 | **12/25 題（48%）子問題不同** | 同輸入連呼叫 `_plan_subqueries` 兩次 |
| query 英譯 → 檢索 | **47/100 題 top-8 不同** | 同組態完整重跑兩次 |

`temperature=0` **不等於**確定性：`gpt-oss-120b` 是 MoE，溫度只固定取樣、不固定專家路由。
**MDE（95%, n=100）**：recall 0.029／precision 0.036／faithfulness 0.028／correctness 0.019 —— 換算成「要幾題從 0 修到 0.5」是 **4~7 題**。ingest 層那種「動 2~5 題」的改動在原理上就量不出來。

**跑 A/B 的正確做法**：設 `RAG_REPLAY_CACHE=eval/replay_cache.json`（見 [`llm_replay.py`](../llm_replay.py)），兩臂共用同一份 plan／英譯／Grader 決策，差異才只剩你改的那一項。**同一份 fixture 內別再重生成**——它跟 `reference_answers.json` 一樣，不需要唯一正確，只需要固定且對所有組態一視同仁。

**但共用一份 fixture 只是 K=1 個 block**：結論條件於那一次 plan 抽樣，分不出「B 真的較好」與「B 在這個 plan 下剛好較好」。要多 block **不需改碼**——`RAG_REPLAY_CACHE` 是路徑，一個 block 一個檔，block 內兩臂共用、跨 block 重抽（同 block 內**必須先後跑**，並行＝兩臂都 miss＝等於沒 block）。⚠ **blocking 是必要條件不是充分條件**：擋不住 Grader（跨 collection 必然 miss，而那是對的）、生成層 `GEN_TEMPERATURE=0.3`、RAGAS judge，所以「已經 blocked」≠「差異可解讀」。旗標語法與其餘細節見 [`llm_replay.py`](../llm_replay.py) docstring。

**指標的可追空間**（把 `reference_answers.json` 原文當答案餵回去評分，n=100，判讀護欄會自動印）：

| metric | 系統 | gold 當答案 | 判讀 |
|---|---|---|---|
| answer_correctness | 0.651 | **0.989** | **唯一有空間的指標** |
| faithfulness | 0.820 | **0.656** | **負空間**——gold 自己 61/100 題輸給系統，停止追 |
| answer_relevancy | 0.823 | 0.842 | 幾乎無空間 |
| context_precision | 0.867 | 0.822 | 噪音 0.046，單次差異不可解讀 |
| nv_context_relevance | 0.968 | 0.965 | 已飽和 |

> ⚠ **2026-08-09 起 `strip_citation_footer` 也剝句末 inline `【檔名, chunk #N】`**（佔答案本文 25% 字元）→ **當日之後的數字與之前的結果檔不可比**。剝除的理由是「像比像」不是「分數變好」——實測 correctness 反而 -0.041。
> ⚠ **「確定性指標」不會自動變確定**：量測路徑上只要還有一次 LLM 呼叫（例如 `full_translate_en` 的英譯），它就跟 RAGAS 一樣髒。用它驗收前先跑一次同組態重複量噪音。
> ⚠ **不要拿落在噪音裡的數字反推機制**——機制聽起來越合理越危險。2026-08-09 一天內提出三個機制假設、三個都被自己的確定性測試推翻（詳見 [`CHANGELOG_AGENTIC.md`](../CHANGELOG_AGENTIC.md) A5.5）。

**目前最大的未動槓桿**：`_check_sufficiency` 的 `relevant_ids` 只留下 **49.3%** 的候選（18/58 題最後只剩 1 個 chunk，全 100 題中位數 3 個），而 `context_recall ↔ correctness` 相關 0.53 是六指標最強、`precision ↔ correctness` 只有 0.14。等於每題用一次 LLM 判斷丟掉一半證據，換一個跟答對與否幾乎無關的指標。
> ⚠ **但「單純放寬候選數」已經量過，是零效果**——2026-08-05 的 `gj_widen_pool12_full100`：證據字元 +32%（6285→8305）、correctness **0.626→0.625**、precision **−0.053**、nv_relevance −0.028。多出來的席位裝的是冗餘。**要動的是「同樣 5 個席位裝 5 份不同證據」（Grader 前確定性去重），不是席位數。** 這條 null result 當時沒進 changelog，導致 2026-08-11 又提了同一個實驗跑到 40% 才被抓到——**提檢索層實驗前先 `ls experiments/`**，詳見 [`CHANGELOG_AGENTIC.md`](../CHANGELOG_AGENTIC.md) (6) 的 08-11 補記。

### 數字缺陷指標的四次失效（2026-08-10，**拿它當閘門前必讀**）

`check_number_defects.py` 原本用「答案的第一個百分比 vs gold 的第一個百分比」。**四種比法被實測推翻，一天內兩輪**：

| 比法 | mix-03（確診答錯：答 24%，合併實為 20%） | 副作用 |
|---|---|---|
| 比**第一個** | 抓到 ✅ | col-11／mi-04／mi-08 **誤報**——頭條挑了 Azure 40%／LTM 12.8%，內容其實對 |
| 比**任一個** | **漏抓 ❌** | 答案的 21%（Productivity 分部）落在 gold 20% 的 ±1pt 內 |
| 接地後**先往後找、後往前找** | 抓到 ✅ | mi-04 **誤報**：head 寫「**16.6%（YoY）**，若以 LTM 計算則為 **12.8%**」→ 值在 anchor **前**，往後撈到別的主張 |
| 接地後**取最近（雙向）** | 抓到 ✅ | col-11 **誤報 1~2 個 run**：「Azure 40%，整體 Microsoft Cloud 則 29%」→ 最近的是 40% |

→ 兩層教訓。**第一層：不接地不行**——答案裡有 4~8 個百分比、容差 ±1pt，任何值幾乎都能湊到。**第二層：接地後「挑哪一個值」也猜不出來**——值在 anchor 前或後都是合法中文寫法，方向與距離都會在某個 run 上誤報。現行做法是問**集合成員關係**（`anchored_pcts`：anchor ±N 內的百分比集合，正解在不在／禁止值在不在），與措辭方向無關，實測 ±40／±60／±80 三個窗口結果相同。
> ⚠ **判別力由 `forbid_pct` 承擔，不是 `expect_pct`**：只問「正解在附近嗎」時，答案把正解與干擾值並陳也會 PASS。所以 `known_defect` 必須填 forbid（`_validate_claims` 啟動時強制），`regression_guard` 可以只填 expect。
> ⚠ **forbid 只在「錯值不會與正解正當並存」時可用**：col-11 的 Azure 40% 與 Cloud 29% 正當並存於鄰近，把 40 填成 forbid 會誤報兩個正確的 run。

> ⚠ **兩個記帳坑，都害過事**：
> ① **「某個 run 沒給百分比」被算成衝突** → 兩檔並列得到 10、單檔卻是 6／4／5。當時據此把閘門門檻設成「衝突 8 → ≤5」，**門檻與被比較的數字不可比，等於這道閘門沒有判定力**。現在分成獨立的「無法比對」桶，且**每個 run 各自算**。
> ② 稽核腳本第一版回傳「0 筆假象」不是好消息，是 gold 欄位名寫錯（`reference_answer` vs 正確的 `reference`）。同 memory `eval-measurement-pitfalls`。
> ⚠ `number_claims.json` 的每一條都要**人工驗證過才登錄**（`verified` 欄位寫怎麼驗的）。`status` 兩種：`known_defect`（現在會 FAIL，修好要變 PASS）／`regression_guard`（現在 PASS，防日後退步）。

## A1 Eval 量尺與方法論（最大宗；不懂這層會把量尺 bug 當系統缺陷追）
- **`answer_correctness` 是長度假象、對 retrieval 免疫**：factual-F1(0.75)＋語意(0.25)，主要由「系統答案 vs 固定 reference 的詳略對齊」決定。lex-07 答「$7.46」完全正確只拿 0.40（reference 塞 6 事實，F1 recall=1/6）。**別當 chunking/系統成敗閘門**，改用 recall/precision/faithfulness。修正：類別級「長度單調」在逐題級站不住（Pearson≈−0.10）——低分尾巴真因是①檢索失敗②false-negative gold。語言錯配假說也已 A/B 推翻（EN 0.475 vs ZH 0.494，沒升反微跌）。
- **false-negative gold（五批，全 eval 側非系統）**：reference 誤寫「來源未揭露」但語料白紙黑字有，系統答對甚至比 gold 準，被打 recall=0。兩機制：(a) `eval_set.relevant`/gold_files 範圍太窄漏標 10-Q/10-K；(b) `gen_reference_answers.py` 檔內選錯 chunk 行/錯期別。修法全在 eval 側（補 relevant、`pin_chunks`、gen_reference 改英文譯句 dense＋`GOLD_TOP_K` 5→8）。特例：mi-04 是 FY vs TTM 真口徑歧義（非乾淨 false-negative），硬加 10-K 反雙降→已 revert。**鐵律：判系統幻覺前先 grep 語料驗事實在不在；「系統 vs gold 差 10x」先讀原文 billion/億 對一次（gold 也會犯單位錯，如 mi-11 誤寫 $380B→$38B）；反向也要驗（grep 抓不到 ≠ 語料沒有，數字常在 MD&A 敘述段）。**
- **gold 全量稽核（2026-08-07，13 題 23 處錯，全部已修）**：上一條的單位鐵律先前只修過 mi-11 單題，**從未全掃**；這次把 100 題 reference 逐一對回它自己的 gold chunk，結果：
  - **機械層 100% 乾淨**：relevant glob 0 個落空、`pin_chunks` 50 個在 exp4 全解析、reference↔eval_set 的 qid/query/gold_files 逐字對齊、無 stale cache、無空答案。→ **量尺壞掉的地方全在語意層，機械檢查給不出警訊**。
  - **10 倍單位錯 6 題 14 處**（mi-02/07/09/11/13、mh-07）：病灶單一——來源 `Fundamentals`/`IncomeStatement`/新聞寫 `$253.49B`，reference 直接抄成「253.49 億美元」（正解 2,534.9 億）。**對照組證明這不是隨機**：來源是 10-K/10-Q `(In millions)` 表格時換算全對（mix-10/11/14、col-12 共 15 處 0 錯）——**錯的只有「B 結尾」那一種字面**。
  - **句內自我矛盾 2 題**：mix-06 開頭「11.1 億」vs 內文「111.44 億（$11,144 百萬）」；mix-14 開頭「42.19 億」vs 內文「4.219 億」。→ 同一句話裡對的錯的並存，抽樣看必漏。
  - **期間錯置 2 題**：mix-03 把 10-Q 的**九個月累計** `$20.4B/22%` 當成單季（單季實為 `$6.4B/20%`，同章節分屬 `Three Months Ended`／`Nine Months Ended` 兩段）；mix-06/mix-14 把 6/30 那季標成「第三季」（曆年制＝Q2）。
  - **抄錯數字 1 題**：mix-11 `$75,246M` 寫成 751.46 億（應 752.46）。
  - **false-negative gold 1 題**：sem-05 寫「文件中未提及與 Anthropic 的具體合作細節」，但 `AMZN_10K_2025.html` 有 6 個 chunk 詳載（$5.3B 可轉債、轉換 $2.3B 重分類利得、$7.2B 上調、2025 年底特別股 $14.8B／可轉債公允價值 $45.8B、2026 年將再認列 $12B）。答對的系統會被判錯。
  - **衝擊面**：**multi_intent 15 題裡 5 題**（mi-02/07/09/11/13）帶 10x 錯，全部集中在「Fundamentals 數值」子意圖——這正是 A3「multi_intent recall 殘因」一路追的題類。**先前 multi_intent 的 answer_correctness 有一部分是被錯 gold 壓的，不是系統答錯**；那批分數需重跑才有效。
  - **根因＝防護「有印警告但不修」**：生成端其實早有兩道（prompt CRITICAL 規則禁寫「億」＋ `gen_reference_clean` 重試 2 次），2026-08-03 `3417ad0` 就在，gold 是 08-06 生成的——**防護當時在，照樣漏**。因為重試耗盡後只 `print("⚠ 重試後仍含手轉「億」，需人工檢查")` 就放行，100 題輸出裡那行捲過去，沒有人檢查。`rq.convert_usd_units_to_yi` 也救不了：它只認「數字+英文單位詞」，LLM 一旦先斬後奏寫成「253.49 億美元」，該函式明文「不碰已經是億的值」→ 結構性失明（覆蓋率：agentic 答案 186 雙寫:21 solo＝90%；gold 29:50＝**37%**，同一支轉換器，差在 Writer 交出的原料）。
  - **修法＝勸不動就用程式修**（`repair_yi_against_source`，[`eval/gen_reference_answers.py`](../eval/gen_reference_answers.py)）：拿該題 context 逐筆判定，三條件同時成立才動手——①來源有 `$N billion` ②來源沒有 `$N/10 billion` ③來源沒有 `$N*100 million`（②③排掉「答案其實對、來源另有數字長得像」）。**回測**（套在修正前的 `.bak`）：14/14 全抓、0 危險誤報；套在修正後檔案殘留 0。判不出的一律不改，但改印出清單留痕。
  - **寫 guard 時自己踩的兩個坑**（都被回測抓出來）：①幣別標記寫成**可選**→「10 億**使用者**」「25 億部裝置」被當金額，來源剛好有 `$10 billion` 就會改成「100 億美元（$10 billion）使用者」。**漏修無害、改壞有害，一律從嚴要求 `美元/歐元`**。②「後接括號就當已雙寫而跳過」太粗→ mi-09「84.75 億美元（其中…」是敘述性括號，被誤跳過漏修；改成只認「括號內以數字/`$` 開頭」。
  - **`mix-06` 順帶校正到來源揭露值**：10-Q MD&A 明文 `Google Cloud revenues increased $11.1 billion ... or 82%`，故 gold 用 **111 億美元（$11.1 billion）**，而非從分部表自行相減的 111.44 億（24,768−13,624）。**系統當時答的就是 111 億＝對的，被舊 gold 的 11.1 億扣分。**
  - **同一種病在生成腳本裡共四處**（全是「偵測到問題→印一行警告→照樣放行」）：①`pin_chunks` 解析失敗靜默退回 dense-only ②手轉「億」重試耗盡只印「需人工檢查」③**快取只比對 `query`、不比對 `gold_files`**——語料換版後 glob 展開到不同檔案（SEC 新申報讓 `MSFT_10K_*.html` 從 2025 版變 2026 版），題目一字未改就照樣 `cached, skip`，正是 `eval-gold-version-drift-bug` 的形狀，2026-07-21 的修法只堵了 query 漂移這半邊 ④**`lang` 預設 English**——忘帶 `--match-lang-from` 就無聲產出英文 gold（2026-08-07 實測踩到：重生成 news-01/mi-13 兩題都變英文），而 register 落差正是壓垮 answer_correctness 的已知主因。③④ 已修：快取改雙條件並印新舊 gold diff；語言改「`--match-lang-from` > 既有 `reference_lang` > English」。實測偽造語料換版後正確報 STALE 並保持繁中。
  - **`eval/reference_answers.json` 先前被 `*.json` 一併 gitignore**（白名單只列了 `eval_set*.json`）——它是 RAGAS 的 ground truth，跟 eval_set 同級的「標準答案」，且不是腳本能免費重現的東西（要一次 LLM 全量生成，且含 24 處人工校正）。本專案已發生過未追蹤 eval 檔被刪、git 完全救不回的事故。已加 `!reference_answers.json`。
  - **eval/ 目錄清理**：6 個過期產物（`generation_judge*.json` 三支、`generation_judge_multiintent_new.json`、`ragas_vs_rubric.json`、以及**沒有任何程式讀取**的 `answer_shape_labels.json`）移入 `eval/_archive_stale/`（沿用 `data/raw/_archive_stale/` 慣例，未硬刪——它們未納版控，刪掉即永久遺失）。清理後 `eval/` 只剩 `eval_set.json`／`eval_set_agentic.json`／`reference_answers.json` 三個版控中的輸入／標準答案。⚠ 兩支腳本的 `--output` 預設仍寫回 `eval/`（`eval/generation_judge.json`、`eval/ragas_vs_rubric.json`），下次跑會再把輸出混進來。
  - **`eval_ragas_vs_rubric.py` 加 `--ids`**：RAGAS 每題獨立評分，只改少數題 reference 時可只重跑那幾題再拼接回原結果，避免整份重跑多吃一輪 judge 噪音。
  - **方法論**：判準要**自足**。「reference 的 N 億 vs 來源的 $N billion」單看會大量誤報（來源同時有 `$2.5B` 與 `$250 million` 就會撞），有效判準是 ①雙寫括號內外自洽（`N 億美元（$M billion）`→ N==M×10，全庫 0 錯）②沒雙寫的才逐筆查來源字面單位。稽核腳本自身的 regex 也會騙人：`(?![\w])` 抓不到 `$60.46B` 的尾隨 `B`，害 lex-03/lex-14/mh-01/mh-10 全被誤標「數字無出處」，逐筆看才發現都是對的。
- **footer 毒害 faithfulness**：agentic 答案尾端「📚 引用來源…rerank=0.730」footer 是 metadata、context 裡不存在→RAGAS faithfulness 逐 claim 判 unsupported，短答案佔比高被砸最重（解釋 lexical 悖論：檢索近乎完美但 faith 全類最低）。已於 [`eval/eval_ragas_vs_rubric.py`](../eval/eval_ragas_vs_rubric.py) `load_answers` 加 `strip_citation_footer()`。全 100 重跑 faith 0.763→**0.815**、lexical 0.672→**0.889**。
- **RAGAS `context_precision` 對多 chunk 比較題判定器不穩**：同一份 rec=1.0/faith=1.0/corr=0.89 正確答案，precision 在 0/0.333/1.0 亂跳（re-roll 就變）。**比較題（multi_hop）別信 precision 欄**，非 gold/系統可修。
- **gold 版本漂移**：`eval_set.json` 精確檔名鎖 10-Q 版本、新聞用 glob；語料刷新加了 202606 新季報但 gold 沒同步→撈到最新的系統被判 0、撈到舊的反得高分。修：6 題 gold 202603→202606；`_meta` 應宣告 collection＋snapshot cutoff。**改 `eval_set.json` 用 `open('wb')` 保 LF，別讓 CRLF 假爆 diff。**
- **eval 改造三定案**：①答案風格＝白話結論先行＋精確數字，SYSTEM_PROMPT 與 reference register 必須一致（否則 correctness 因 register 落差崩）；②RAGAS 跑獨立 `.venv-ragas`（ragas 0.2.x 綁 langchain 0.3，會扯壞生產 `.venv` 的 langchain 1.x／SemanticChunker）；③multi_intent GT **不降級**（不改 news-only 粉飾、不加「含財報字就不觸發 news filter」guard）。
- **eval 改版拆分（2026-07-29，90→100 題）**：multi_intent 收斂為嚴格「1 財務指標＋1 新聞事實」平行雙意圖（15 題）；新增 multi_hop 10 題（hop-2 key 待 hop-1 才成形）＝5 題比較→屬性(A)＋5 題新聞→實體→財務(B)。戰略：**multi_intent 本不該 agentic 贏（平行、單管線拆解即可），multi_hop 才是 plan→execute→replan 正當戰場**。

### A5.5 2026-08-09 量測基礎建設：先讓實驗可被相信，再談優化

這一節不是功能改動，是**把「為什麼指標優化不動」查到底**的結果。結論是：**量測的解析度比改動的效果粗一個數量級，而且量測路徑自己也在抖。**

#### (1) 三層噪音，全部大於訊號

| 層 | 實測 | 量法 |
|---|---|---|
| RAGAS judge | `context_precision` 均值移動 **0.046**（兩次獨立樣本 0.0459 / 0.0452 複現）；`context_recall` 0.002 / 0.013 | 同一份結果檔重評兩次 |
| Plan 節點 | **12/25 題（48%）子問題不同** | 同輸入、`temperature=0` 連呼叫兩次 |
| query 英譯 → 檢索 | **47/100 題 top-8 不同**（重複對 149 vs 151） | 同組態完整重跑兩次 |

`temperature=0` 不等於確定性——`gpt-oss-120b` 是 MoE，溫度只固定取樣、不固定專家路由。

**MDE（95%, n=100）**：recall 0.029 / precision 0.036 / faithfulness 0.028 / correctness 0.019 → 換算成「要幾題從 0 修到 0.5」是 **4~7 題**。而 ingest 層的改動（期間邊界 11 個 chunk、影響 2 題）**在原理上就量不出來**。這不是運氣，是設計問題。

#### (2) `llm_replay.py`：檢索前 LLM 中間產物的重放快取

接三個點：`_plan_subqueries`、`translate_query_to_english`、`_check_sufficiency`。未設 `RAG_REPLAY_CACHE` 時**完全 no-op，生產路徑不受影響**。

**key 的兩個相反設計，各有理由**：
- plan / translate 的 key **不含 system prompt** —— planner 的 prompt 內嵌隨 collection 變動的 KB Coverage Snapshot，納入 key 會讓跨 collection A/B 全部 miss，正好毀掉唯一用途。副作用：改 planner prompt 時快取不會自動失效，要手動刪檔。
- checker 的 key **含這次看到的候選 id** —— 候選變了是**合法 miss**，那正是被測改動造成的差異，用舊決策蓋掉會把訊號洗掉。

**它買到什麼、買不到什麼**：買到 A/B 兩臂共用同一份 plan；**買不到生產端穩定**（MoE 路由不確定不打算解）。代價是固定下來的是「某一次抽樣」不是「正確答案」——跟 `reference_answers.json` 同一種東西，**定版後別再重生成**。

fixture：`eval/replay_cache.json`（plan 100 / translate_en 181 / check 206）。

#### (3) 判讀護欄：讓分數無法被誤讀

`eval_ragas_vs_rubric.py` 每次跑完在 OVERALL 底下印出每個指標的**噪音門檻**與 **gold 上限**。gold 上限來自把 `reference_answers.json` 原文當成系統答案餵回去評分（n=100）：

| metric | 系統 | gold 當答案 | 判讀 |
|---|---|---|---|
| answer_correctness | 0.651 | **0.989**（85/100 滿分） | 唯一有空間的指標，距上限 0.338 |
| faithfulness | 0.820 | **0.656**（61/100 輸給系統） | **負空間**，停止追 |
| answer_relevancy | 0.823 | 0.842 | 幾乎無空間 |
| context_precision | 0.867 | 0.822 | 噪音 0.046，量不出來 |
| nv_context_relevance | 0.968 | 0.965 | 已飽和 |

faithfulness 是負空間的原因：gold 依 `gold_files` 生成，與 agentic 實際撈到的 contexts 不同源。**繼續往上推等於要求系統答得比標準答案還保守。**

**推翻的舊結論**：`recall==1.0` 的 44 題，系統 correctness 0.724、gold 0.983 —— 證據全到位仍差 0.26，**瓶頸在生成層不在檢索層**。（先前我用 0.724 論證「correctness 有天花板」是錯的，gold 拿得到 0.989。）

#### (4) 剝 inline 引用標記：留下，但理由不是分數

`strip_citation_footer` 現在同時剝句末 `【檔名, chunk #N】`（佔答案本文 **25.0% 字元**）。**實測分數反而變差**：judge 重評 28 題 correctness **-0.041**；而零 LLM 的確定性測試顯示相似度分量只變 **+0.0011**（cosine 0.9105→0.9148），**「檔名雜訊稀釋 embedding」的假說證偽**。殘差來自 0.75 權重的 statement F1，未查清。

保留是方法論選擇（引用是 metadata、reference 一個都沒有，剝掉才 like-for-like）；照「分數變低就退回」做就是在 gaming 量尺。⚠ **這使 2026-08-09 之後的數字與之前的結果檔不可比。**

#### (5) 兩個 ingest/檢索層改動改成預設關閉

| 改動 | 量測 | 處置 |
|---|---|---|
| `_strip_tabular_blocks`（壓平表格去重） | 六指標無明確勝方；核心指標 precision 的差落在噪音內 | `--strip-dup-tables`，**預設關閉** |
| `_suppress_near_duplicates`（檢索層近重複抑制） | top-8 重複對 149→139（噪音區間 149~151，訊號約噪音 5 倍，**有效但很小**）；而最終 contexts 的重複本來就只有 18 對 / 11 題，**Grader 已清掉 88%** | `RAG_SUPPRESS_NEAR_DUP=1`，**預設關閉** |

#### (6) 尚未動的最大槓桿：Grader 保留率

`_check_sufficiency` 的 `relevant_ids` 只留下 **49.3%** 的候選（58 題單一子問題、池固定 5 → 143/290），**18/58 題最後只剩 1 個 chunk**，全 100 題最終 chunk 數中位數 = **3**。而 `context_recall ↔ correctness` 相關 **0.53**（六指標最強），`precision ↔ correctness` 只有 0.14。

**這條管線每一題都在用一次 LLM 判斷丟掉一半證據，換來一個跟答對與否幾乎無關的 precision。** 這是唯一每題都作用、因此唯一有機會超過 MDE 的系統性改動。正確的消融是**把確定性去重放在 Grader 之前**（讓 5 個席位裝 5 份不同證據），而不是單純放寬保留率。

##### 補記（2026-08-11）：「單純放寬」已經被量過了，是零效果

上面那句「不是單純放寬保留率」當時是推論。**2026-08-05 其實已經跑過完整 100 題**，只是結果檔沒進 changelog（`gj_widen_pool12_full100` / `gj_pool10_clean`，都在 `exp4` 上）——於是 2026-08-11 我又提了同一個實驗，跑到 40% 才被使用者一句「上次 grader 放寬不是反而會帶來更多雜訊嗎」問住，回頭翻檔案才發現。

| | 基準 `gj_v2_full100_20260806` | `pool10` | **`widen_pool12`** |
|---|---|---|---|
| 平均 chunk 數/題 | 3.04 | 3.29 | 3.94 |
| **平均證據字元/題** | 6285 | 6532 | **8305（+32%）** |
| **answer_correctness** | 0.626 | 0.625 | **0.625** |
| context_precision | 0.835 | 0.828 | **0.782（−0.053）** |
| nv_context_relevance | 0.958 | 0.925 | **0.930（−0.028）** |

**+32% 證據量 → correctness 動 0.001，precision 掉 0.053。** 多出來的席位裝的是冗餘與不相關。

**連帶推翻一個當天才提出的機制**：head collection 每題證據字元比 period 少 23%（8434→6525，chunk 數不變、chunk 變短），我據此推論那是 head correctness −0.032 的成因。但 **+32% 都動不了 correctness，−23% 就不可能是原因**。head 非 AAPL 那 42 題的退步目前**沒有已證實的解釋**。

**紀律補一條**（`verify-mechanism-before-claiming` 的前置步驟）：提實驗之前先 `ls experiments/` 找同名干預——**能證偽假設的測試可能已經跑過了**，而未寫進 changelog 的 null result 等於下次一定重跑。

#### 方法論教訓（當天犯了三次同一個錯）

| 我的機制假設 | 推翻它的確定性證據 |
|---|---|
| 「移除冗餘剝奪 chunk 自足性」（precision -0.0205） | judge 噪音 0.046，兩次複現 |
| 「correctness 有天花板」（perfect recall 只有 0.724） | gold 當答案拿 0.989 |
| 「inline 標記稀釋 embedding」 | 確定性 cosine 差 +0.0043 → 影響 +0.0011 |

共同模式：**先看到數字，再編一個合理的機制，然後沒去驗那個機制。** 定為紀律——**任何機制宣稱都要先有一個能證偽它的確定性測試**（零 LLM、可重跑、無抽樣變異）。另外：「確定性指標」不會自動變確定，只要量測路徑上還有一次 LLM 呼叫，它就跟 RAGAS 一樣髒。

## 反覆出現的方法論教訓
繞過 brain 測 ≠ 真實 graph／先排除工具環境毛病再懷疑系統／單次 LLM-judge 不能論因果、要確定性消融／
別過度外推（file-level 無效 ≠ chunk-level 無效、edgar_exp4 無效 ≠ 全面無用）／prompt 是機率不是保證，
grounding/citation 要靠 Python 強制不靠拜託模型。

---

---

## 操作備忘（文件重整時從 CLAUDE.md 收攏過來）

**標準跑法**（三步；reference 只需生成一次可重用）
```bash
# ① 生成 ground truth（已存在就不必再跑；--force 不帶 --ids 會洗掉人工校正）
.venv/Scripts/python.exe eval/gen_reference_answers.py

# ② 產生結果檔：agentic 版
.venv/Scripts/python.exe eval/run_agentic_on_evalset.py --module agentic_rag_v2 \
    --freshness-mode snapshot --output experiments/agentic/<名稱>.json
#    單管線版
.venv/Scripts/python.exe eval/eval_generation_llm_judge.py --output experiments/<名稱>.json

# ③ 切 .venv-ragas 算六指標
.venv-ragas/Scripts/python.exe eval/eval_ragas_vs_rubric.py \
    --from-results experiments/agentic/<名稱>.json \
    --reference-file eval/reference_answers.json --output experiments/agentic/ragas_<名稱>.json
```

- **只改動少數題的 reference 時不必重跑全部 RAGAS**：RAGAS 每題獨立評分，用 `--ids <題號…>` 只評改動題，再把新分數拼接回原結果。全部重跑只是多疊一層 judge 噪音。
- ⚠ **`eval_generation_llm_judge.py` 與 `eval_ragas_vs_rubric.py` 的 `--output` 預設值仍指向 `eval/`**，跑的時候要自己帶 `--output experiments/...`。`eval/` 只放輸入與標準答案；過期產物在 `eval/_archive_stale/`（未納版控，刪掉即永久遺失，所以只歸檔不硬刪）。
- **新機器還原評測環境**：`python -m venv .venv-ragas` → `pip install -r requirements-ragas.txt`（2026-08-07 用 `pip install --dry-run` 驗過：解析出 97 個套件，與當時環境的數量與關鍵版本一致）。兩個環境為何不能合併見 [`../README.md`](../README.md) §4-1。
- ⚠ **`answer_correctness` 偏低常是量尺問題**（長度對齊／false-negative gold），不是系統答錯——下結論前先讀 memory `ragas-correctness-length-artifact`、`eval-false-negative-gold`，以及本檔的〈判讀護欄〉。
