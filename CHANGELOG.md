# CHANGELOG

紀錄本專案每次有意義的程式修改（架構調整、參數變更、新增功能、放棄的實驗）。
新條目加在最上面。每筆條目只留「改了什麼、關鍵數字、結論」，診斷過程與推導細節見 git log，不重述。

> `agentic_rag.py`（deepagents 多智能體實驗性入口）的修改紀錄獨立在 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md)。

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

**復活成功案例**：`rerank_multi_query`（07-07 判死，變體仍中文）在「英文變體+glossary」新前提下（07-08）復活，現已併入生產雙 query 精排機制。

---

## 已知問題 / 已接受的極限（不要再嘗試系統側修法）

- **sem-08**（AMZN AWS 策略方向）——三種系統側修法皆因同一真因失敗：生成模型判定「獲利數字」與「策略方向」問法無關而主動略過，非訊號埋沒。復活條件：僅剩調整 rubric 或維持現狀。
- **col-08**（Tesla 口語版）——dense rank 25/106，落差超出 rewrite 射程，不再投入新召回機制。
- **col-04**（Azure 口語版）——關鍵內容在 top-5，但生成被口語框架帶偏、選擇誠實拒答。非 bug，觀察中。
- **col-05**（AWS 口語版，sem-08 鏡像）——RECALL 層問題 + rewrite 變體品質不穩定。
- **ckpt_R = 0.7307**（chunk recall, n=20）——約 27% checkpoint 在候選池階段沒被撈到，多非 critical，暫不優先處理。

---

## 2026-08-02

> 本日改動集中在**生成契約**（`rag_query.py`，單管線與 agentic 共用此契約）。agentic 端的 synthesize
> 接入、忠實度稽核升級與逐題 smoke 驗證見 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md) ⑧。

- **`SYSTEM_PROMPT` 新增 Rule 11-13**（`rag_query.py`）。逐題讀錯誤分出三族「生成讀壞」，各補一條通用讀法紀律：**Rule 11 單位**（來源 `$X billion` 原樣保留、別轉億——billion→億 是 ×10）、**Rule 12 方向**（引用來源方向詞原文、保留符號、兩期數值靠日期定先後，禁臆測「在改善」）、**Rule 13 per-intent**（多意圖某腿有相關 chunk 卻假性拒答「無資訊」→ 禁止）。evidence-first 變體舊 Rule 11 順移 14、內文 `rules 1-10`→`1-13`。
- **確定性單位換算 `convert_usd_units_to_yi()`**（`rag_query.py` 純文字後處理）：billion→億 是 LLM 翻譯層 token 習慣，prompt 只能隨機壓（news-09「叫它×10」兩跑一對一全錯）。正解＝Rule 11 要 Writer 原樣保留 `$X billion`，再由**純程式**乘算成正確億（billion×10 / million×0.01 / trillion×10000），**零誤報**（`X billion` render 成 `X 億` 100% 是錯）。任何呼叫端可套用；邊界安全（%、EPS `$1 to $3`、GPU 台數、已是億的值都不動）。
- **eval 側 gold**：mi-11 單位錯修正（源文 `$380 billion` 誤寫「380 億」）；確認 col-03（源文真有 $15B OpenAI Series C）/mi-02（74.9%Q1 vs 71.1%TTM 口徑差）/mi-10（FY vs TTM）為 false-negative gold 或口徑差，非系統錯 → 先前 bincorr「~26 真 error」被高估、真實正確率高於 0.645。⚠ `reference_answers.json` 為 gitignore 生成物，gold 修正不進版控。**未動**檢索/filter/模型/agentic 結構。

---

## 2026-08-01

- **Agentic v2 GEN_MODEL 定案 `openai/gpt-oss-120b`**：候選 benchmark 只量了速度/品質、漏了「per-model 限速」這維，踩了兩個坑——`glm-5.2` 單發 127~217s（不可行）、`deepseek-v4-pro` 品質最佳但有低 per-model 429 硬牆（100 題必團滅）。gpt-oss-120b 快（~8s）、不限速、col-01 端到端驗證乾淨，定案。
- **`_nvidia_call_llm` 加 429/5xx 指數退避重試**：先前無重試，長 run 撞 NVIDIA 日配額 → glm 那輪 56/100 題掉進機械 fallback、汙染生成分數。修法：`RateLimitError`/連線類重試（含 `Retry-After`），5xx 才重試、4xx 立即拋；`AGENTIC_PIPELINE_WORKERS` 併發 3→2 降壓。
- **100 題乾淨 RAGAS（v2 pipeline · gpt-oss-120b · 0 fallback/error/refuse）**，_overall：context_recall 0.639、context_precision 0.790、nv_context_relevance 0.890、faithfulness 0.794、answer_relevancy 0.812、answer_correctness 0.552。
  - **關鍵**：同管線同題庫，對照被 429 汙染作廢的 glm 輪，生成兩項暴漲（answer_relevancy 0.490→0.812、answer_correctness 0.376→0.552），檢索三項守住（precision +0.064、relevance +0.030、faithfulness +0.018）。證實 glm 輪低分是 429 fallback 假象，非真實生成品質；這份才是 v2 生成真值。
  - 軟肋：`lexical` recall 0.528（各類最低，精確關鍵字題該撈未撈滿）；`answer_correctness` 偏低多為長度對齊假象（見 07-15 條），別過度解讀。

---

## 2026-07-22

- **LLM backend 統一改 NVIDIA NIM（取代 Groq）**：`rag_query.py` `call_llm()` 非 `gemini-` 分支改走 NVIDIA NIM（單一 key，無 Groq 的 TPD 硬牆）；`DEFAULT_MODEL`/`DEFAULT_GEN_MODEL` 統一為 `openai/gpt-oss-120b`（`meta/llama-3.3-70b-instruct` 在 NVIDIA 上會 timeout，不可用）。`api_server.py` 移除重複的雙份呼叫邏輯，直接沿用 `rq.call_llm`。CLI 實測通過。`eval/`、`agentic_rag*.py` 不動，仍各自獨立走 Groq。

- **RAGAS eval 重建**：`eval_set.json` 改版為 90 題（6 類 × 15），不再需要 rubric，改用 `gen_reference_answers.py` 生成的完整參考答案驅動 RAGAS 六指標。
  - **Bug（重大）**：`gen_reference_answers.py` 續跑快取只認 id 存在，不查 query 是否還是同一題——eval_set 改版沿用舊 id 但換了題目，導致 47/90 題撈到文不對題的舊 reference（如 NVIDIA 的題配到 Microsoft 的答案）。已修：快取判斷加 query 逐字比對，不符即視為 stale 強制重生。修好後 context_recall 0.345→0.679、context_precision 0.402→0.777、answer_correctness 0.352→0.613（不依賴 reference 的 faithfulness/answer_relevancy 幾乎不動，印證問題出在 reference）。
  - 順手修 `eval_ragas_vs_rubric.py`：Groq TPD 燒穿（4 key 同 org 共用額度）→ 加 `EVAL_LLM_PROVIDER=nvidia`；`main()` 缺 UTF-8 reconfigure 導致印 `↔` 崩潰、分數存檔前遺失→已補上，並加 `--timeout`/`--max-retries`/`--nvidia-passes`。
  - **最終結果**（90 題 OVERALL）：context_recall 0.679、context_precision 0.777、nv_context_relevance 0.739、faithfulness 0.784、answer_relevancy 0.738、answer_correctness 0.613。`multi_intent` 類明顯最弱（財報+新聞雙意圖單一 query 難同時撈齊），是後續 Agentic RAG 驗證目標。

---

## 2026-07-21

**新聞 hard filter**：偵測到新聞意圖（`looks_like_news_query`，先前只印 WARN 未路由）→ 硬篩 `doc_type=news`。三處改動：ingest 幫每個 chunk 補 `doc_type` payload（`infer_source_type()` 從檔名推導）；`retrieve()` 疊加 ticker/期別 filter；新檔 `migrate_add_doc_type.py` 用 `set_payload` 補現有 2367 points（不重算 embedding）。驗證：新聞題 Tier1 strict hit、財報題零影響。已知取捨：財報+新聞混合題會被硬篩成只回新聞（待後續量化評估）。

---

## 2026-07-20

- **rewrite × translate 2×2 拆解**：兩者是**負交互**——各自單開都有小增益（rewrite +0.014、translate +0.018），疊加反而全數抵銷、低於 baseline（0.795 < 0.806）。translate-alone 是本 regime（`gpt-oss-20b` retrieval）最佳單一設定。⚠ 此結論確立於 Groq TPD 燒乾後的 `gpt-oss-20b` regime，若生產仍用 `llama-3.3-70b-versatile` 需重跑才能定案。
- **方案 A「evidence-first 生成」實驗**：機制成功（Evidence Log 確實逼模型引用已檢索證據）但分數不動——證明 sem-08 殘餘失敗在 judge/框架層（措辭認定不穩），不在檢索/引用層。col-08（RECALL 受限題）反而因「窮盡覆蓋」被鎖死在錯誤召回上，變差。不轉正、不跑全量回歸，保留為 opt-in 診斷工具（`SYSTEM_PROMPT_EVIDENCE_FIRST` + `--evidence-first`）。
- **Rewrite A/B 定案跑分**（`--repeat 3 --judge-votes 3`）：overall +0.014，弱但一致的訊號（3-5 倍 std）。意外發現：逐題 run-to-run std 普遍 0.2~0.5，遠高於 overall std——过去多次「單跑一次定案」不可靠，col-08/col-04 舊定案因此存疑（待重跑，未做）。

---

## 2026-07-19

- **Judge 模型換代**：`qwen/qwen3-32b` 被 Groq 下架（404），改預設 `openai/gpt-oss-20b`（`judge_regression.py` 11 案例 10/11，優於 `gpt-oss-120b` 的 9/11 且無自評偏誤）。新增 `EVAL_LLM_PROVIDER=nvidia` 開關。
- **修復「靜默降級污染結果檔」的結構性漏洞**（第三次踩同一坑）：`rewrite_query`/`parse_query_filters`/`translate_query_to_english` 對 LLM 例外一律靜默降級，TPD 耗盡時題目照樣跑完、分數照樣算——量到的其實是「沒開 rewrite」。舊的兩份 100 題 rewrite 結果作廢（`n_with_rubric` 實際只剩 69/36，摘要卻照印 100）。修法：新增降級事件記錄，確定失敗就整輪中止（`exit 2`，已完成題目落盤可 resume）。
- **Rewrite A/B 乾淨重跑**（單次，NVIDIA）：overall +0.009，在雜訊範圍內（已知 std_across_runs=0.030）。col-08 反被弄壞（已知極限題）。

---

## 2026-07-16

- **100 題 baseline 首跑**：overall correctness 0.776（與舊 61 題基準不可比較，題集已變）。
- **修復 2 題 rubric 缺陷**（sem-18/col-17 Tesla 能源分部錯設 critical）；**新發現 3 類 bug**：judge 中文「億」單位換算誤判（sem-23）、生成層「億/billion」誤譯（sem-17，新類型）、2 個真實 RECALL 缺口（年度加總句未進池）。
- **eval_set 全量 gold 驗證 + 擴充至 100 題**（各類別 25 題）：發現 2 題整題超綱（sem-06/col-09「Apple Silicon」語料 0 命中，已替換）、3 題快照漂移缺陷（`*_Fundamentals_*` glob 匹配多份快照、數值不同，已放寬 rubric）。逐題證據見 [`eval/eval_set_evidence.md`](eval/eval_set_evidence.md)。

---

## 2026-07-15

- **RAGAS answer_correctness vs 自製 rubric 對照**：ground_truth 若用 rubric 清單合成，篇幅錯配導致 F1 精確率崩塌（~0.31）。修法：新增 `gen_reference_answers.py` 生成完整參考答案，回升到 ~0.51。剩餘差距是結構性的（rubric=加權 recall，RAGAS=F1），不能拿絕對值互相參照。standalone 跑在獨立 `.venv-ragas`（`ragas==0.2.15` 需要 `langchain<0.4`，會弄壞生產 `.venv` 的 `langchain-experimental`）。
- **`parse_query_filters` few-shot 探針**：k=3 確認無效（已寫入死路表）。意外發現 8b zero-shot 在 filter 任務上不輸 70b，但驗證 rewrite/translate 品質的對照組被 Groq TPD 耗盡阻斷，數據作廢。
- 順手修：`eval_chunk_recall.py` 用 `qwen3-32b` 當 judge 時 `<think>` 區塊沒剝除，JSON 解析 100% 失敗、靜默 fallback 成 `ckpt_R=0.00`。

---

## 2026-07-14

- **sem-08 定案為已接受極限**；col-08/col-04/col-05 現狀維持（見上方清單）。
- **工程債清理**：`api_server.py` 拆 `retrieval_model`/`gen_model` 為獨立參數（避免前端換模型悄悄改變檢索行為）；新增 `eval/.gitignore`，96 個誤入版控的結果檔 `git rm --cached`。
- **`col10_or_logic`**：judge 的 OR 邏輯從 prompt 移進程式碼、用 `any()` 聚合，11/11 通過（過去靠 LLM 自己判斷「A/B」擇一命中很不穩）。

---

## 2026-07-13

- **Rerank 延遲攻堅**：根因是 `bge-reranker-v2-m3` 在純 CPU 上評分 20 候選要 ~46s，是全流程壓倒性瓶頸。`batch_size=1` 免費 2.33x 加速零損失（消除 padding 浪費，**已採用**）；`max_length=2048`（全庫僅 1.6% chunk 超標，與未截斷結果逐位一致，**已採用生產值**）。疊加後 130s→37s（3.5x）。ONNX/小模型/級聯三條路都失敗（見死路表）。
- **轉生產預設**：`enable_rewrite` + `translate_query_en`（雙 query 取最高分）正式成為 CLI/api_server 預設。lexical/mixed 零回歸；colloquial 表面分數低但逐題拆解無新回歸（皆可歸因已知問題）。
- **正式修法**：`translate_query_en` 從「整組替換」改成「rerank 對每個候選同時用原句與翻譯句評分、取逐候選最高分」——sem-09/sem-11 同時修好（舊版全有全無會顧此失彼）。

---

## 2026-07-12

- E5 全量驗證：舊版 `translate_query_en`（全有全無替換）暫不轉正——sem-08 未真修好、sem-09 新退步（後由 07-13「雙 query 取最高分」解決）。
- sem-08 根因確認到生成層：目標 chunk 穩定進 top-5，但因混雜大量不相關內容、AWS 獲利只是從屬子句，生成模型 3/3 次選擇性略過。
- Judge 轉正 `qwen/qwen3-32b`（11 案例 10/11，優於 20b 的 9/11）。col-08 文件化為已知極限。

---

## 2026-07-11

- **rewrite 新增「語域轉換」規則** + glossary scoping 修正 + 中文 ticker 別名：根因是「策略提問語域」與「會計敘述語域」不匹配。sem-08/sem-11 k=3 穩定修好，lexical/mixed 零回歸。
- `eval/judge_regression.py` 建立：11 案例回歸套件，發現財年措辭修法未穩定生效。
- colloquial 首次驗證推翻「口語 degrade」假說：7 題非滿分裡 3 題是純測量 bug（judge/rubric），非系統退步。**教訓：分數低不要急著找新機制，先排除判定工具本身的既有毛病。**

---

## 2026-07-10

**財年措辭誤判**：07-09 只重判舊答案的驗證方法有漏洞（未驗證新生成內容本身）。真正修法改在生成端——`build_user_prompt()` 偵測 YYYYMM 期間碼，注入指示要求答案並列使用者代碼與 fiscal quarter 用語，從根源消除歧義。

---

## 2026-07-09

- **乾淨隔離 A/B**：retrieval 固定 70b、只換生成模型——`gpt-oss-120b` 全面勝出（mean 0.627→0.870，11 題零回歸）。**決策：`DEFAULT_GEN_MODEL` 轉正為 `openai/gpt-oss-120b`**，`retrieval_model` 維持 70b。
- 拆分 `retrieval_model`/`gen_model` 為獨立參數（解掉換模型 A/B 時檢索側被悄悄一起換掉的混淆變因）。
- 新增 `--judge-votes` 多數決；judge prompt 加 FISCAL vs CALENDAR 規則修財年措辭誤判。
- 溫度拆分（見 07-06）延伸驗證：檢索側 temp=0 純賺，生成側需保留隨機性。

---

## 2026-07-08

- gen=120b 全量驗證：sem-02 真實修好；先前「sem-03/07 幻覺」結論是自己查錯 chunk，已撤回。
- **sem-02 三層失敗（RECALL→RANK→生成）逐層修好**：新增 `_COMPANY_PRODUCT_GLOSSARY`（先 grep 語料確認才收錄）+ `rerank_multi_query` 英文變體版 + gen=120b，四層修法缺一不可。
- **教訓寫入 CLAUDE.md**：judge model 與 retrieval/gen model 是獨立參數，不可圖方便共用同一變數（`diagnose_crit_miss.py` 曾誤用導致診斷結論過於樂觀）。
- sem-11 資料修復：`fetch_data.py` 預設含修正案 10-K/A（無風險因子章節），改 `amendments=False` 重抓後直接修好，對全部 7 檔有效。
- Bug：7 支 eval 腳本硬編碼舊 local Qdrant path，Docker 遷移時漏改，統一改用 `make_qdrant_client()`。

---

## 2026-07-07

**🔑 重大發現：sem-02/08/11 排序問題根因是 cross-lingual rerank**（中文問句 vs 英文文檔評分嚴重失真）。翻成英文後命中分數暴漲直接進 top-k，是先前所有「調數量/multi-query/chunking」路線全部無效的統一解釋。三題不是同一種病：sem-11=純排序（翻譯已解）、sem-02=排序+邊緣（後續 glossary 解決）、sem-08=召回層（翻譯救不到，稀釋型 chunk）。

新增 `--repeat k`，建立「單次跑 mean 差 <0.1 是噪音」的量測紀律。

---

## 2026-07-06

- 溫度拆分：檢索側/judge 用 temp=0，生成答案用 temp=0.3（`GEN_TEMPERATURE`，**已採用**）——生成側 greedy 反而更差（mode collapse，擠掉內容多樣性）。
- 生成 prompt 加 Rule 9：多公司比較題強制逐家點名（sem-04 從 1.0 掉到 0.3 後修好）。
- Vector DB：Qdrant local mode（排他檔案鎖）→ Docker server mode，依 `.env` 的 `QDRANT_URL` 自動選模式。

---

## 使用慣例

- 修改 retrieval / ingest / prompt 邏輯前，先查上方「已試無效總表」。
- 每次修改後在最上面新增日期區塊，只寫「改了什麼、關鍵數字、結論」——診斷過程見 git log。
- 「已試無效」的項目附復活條件，寫進總表而非分散在各日期區塊。
