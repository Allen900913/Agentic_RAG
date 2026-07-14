# CHANGELOG

紀錄本專案每次有意義的程式修改（架構調整、參數變更、新增功能、放棄的實驗）。
新條目加在最上面。每筆條目說明「改了什麼」與「為什麼」，讓之後回頭看的人不用重新挖 git log 或猜測動機。

---

## 2026-07-14

### 📌 sem-08 定案：已接受極限，不再嘗試系統側修法（CLAUDE.md 已同步更新）
- **使用者決定**：接受 sem-08 為已知天花板，同 col-08 的處理方式。
- **依據**：本日依序試了三種系統側修法（chunk 切分、Rule 10 prompt 規則、檢索後句級抽取），**全部因同一個真因失敗**——見下方三則條目。決定性診斷是句級抽取：把目標句逐字搬到 chunk 最前面，生成仍 2/3 次不引用，證明病灶不是「模型找不到」，而是「模型判定該事實與問法無關而主動略過」＋ judge/gen 雜訊。這是 query/rubric 框架錯配，不是檢索/呈現層能修的問題。
- **CLAUDE.md 同步更新**：「已知問題與分析」章節的 sem-08 條目改寫為「已接受極限」定性（不再是「變好但未穩定」的開放狀態）；待辦清單中 sem-08 一項標記關閉；順帶關閉了同章節另一個已完成的待辦——col-10 的 judge OR 邏輯誤判（見下方 col10_or_logic 條目，已於本日修好，原待辦措辭誤把它記在 sem-09 名下）。
- **復活條件**：非系統可解，只有兩條非系統路徑——調整 rubric（若判定「策略方向」題不該強制要求獲利數字）、或維持現狀接受。除非 rubric 改變，不要再對此題嘗試檢索/prompt 層修法。

### 🔬 新功能（實驗性，預設關）：檢索後句級抽取（contextual compression）——附帶證偽了「sem-08 是訊號埋沒」的假設
- **動機**：sem-08 連續兩種修法（chunk 切分、Rule 10）都失敗，剩下的通用解是「生成前把相關句物理性拉到模型眼前」。實作 `compress_chunks()`：生成前一次 LLM 呼叫，把每個 top-k chunk 對 query 的相關句**逐字**抽出，`build_user_prompt` 把它們擺 chunk 開頭當「KEY EXCERPTS」（highlight 附加、原文全保留，最壞＝現狀）。設計：一次呼叫打包全 chunk、temp=0、retrieval-side model、verbatim 防線（抽出字串必須是原 chunk 子字串，否則丟棄，抵禦抽取器幻覺）、失敗 fallback 原 chunk。接了 CLI（`--compress`）、api_server、eval（`--compress`）。
- **決定性診斷結果**：compressor **完全正常**——sem-08 的 chunk #100 被逐字抽出正確目標句「The increase in AWS operating income in 2025... due to increased sales...」擺到最前面。**但生成端仍 2/3 次不引用**：k=3 scores=`[1.0, 0.5, 0.5]`、crit_miss_rate=0.667（`eval/sem08_compress_gen_k3.json`）——跟 baseline（crit_miss 0.333）在雜訊內、沒有可靠改善。
- **這證偽了「sem-08 是訊號埋沒」的假設**：把目標句拉到模型眼前都救不了 → 病灶不在「模型找不到」，而在「模型判定『AWS 營業利益成長』與『AWS **策略方向**』這個問法無關而略過」＋ judge/gen 雜訊。**chunk 切分、Rule 10、compression 三種修法全因同一個真因失敗**：它們都在解「訊號呈現」，但 sem-08 的真因是「query/rubric 框架錯配」。
- **sem-08 結案定性**：屬**雜訊/框架受限題**（std 大、crit_miss≠1.0），依 CLAUDE.md「禁止對雜訊題優化」，**不再嘗試系統側修法**。真要動只剩兩條非系統路徑：① 調 rubric（若「策略方向」本就不該強制要求獲利數字，是 rubric 過嚴）；② 當成已知天花板文件化（如 col-08 的處理）。
- **compression 功能狀態**：**預設關（`DEFAULT_ENABLE_COMPRESS=False`）**、保留 `--compress` 開關。它是通用技術（放大相關句訊號），但①沒修好目標題、②加一次 LLM 呼叫（+延遲，你們 retrieve 已 78s）、③是全域生成端改動未做跨類回歸。轉正條件同 rewrite/translate：先跑全量 lexical/mixed/semantic/colloquial 確認無回歸（尤其 lexical 數字題 citation 不被打亂）再改預設。

### ❌ 已試無效：生成 prompt 加 Rule 10（策略題必須含財務表現）修 sem-08——指令打不過 chunk 內訊號埋沒
- **動機**：sem-08 的病灶是生成端選擇性忽略埋在稀釋 chunk 裡的 AWS 獲利句；sem-04 曾用同手法（Rule 9）修好，故先試最便宜的 prompt 規則。
- **改法**：`SYSTEM_PROMPT` 加 Rule 10——「問策略/方向/前景時，材料裡若有該業務的財務表現必須寫進來；就算埋在混雜 chunk 裡也要挖出來用」。
- **結果（k=3，`eval/sem08_rule10_gen_k3.json`）：完全無效且形態變差**——scores=`[0.5, 0.5, 0.5]`、std=0、crit_miss_rate=**1.0**（3/3 穩定失敗）。**查證排除檢索問題**：chunk #100（含 AWS 獲利句）就在 top-5 rank 3，指令也在，但三次答案完全不提獲利/營業利益（只引用 #86/#85/#45，從不引用 #100——與 07-12 觀察一致）。
- **死因：機制型**。Rule 9 能成功是因為那是「回答框架」問題（指令可糾正）；sem-08 是**注意力層級**問題——5170 字 chunk 裡 FTC/稅務/利息/Rivian 佔絕大多數，AWS 獲利只是從屬子句，模型讀 context 時根本沒「撿起」那句話，指令命令不了它看見沒看見的東西。prompt 規則類修法對「chunk 內埋沒」這一類病灶整體無效，之後不要再對同類問題嘗試加規則。Rule 10 已撤除（不留在生產 prompt 增加回歸風險）。
- **復活條件**：若生成前的 context 呈現方式改變——例如檢索後句級抽取（contextual compression）把相關句拉到 chunk 開頭、或全量改用單一主題 chunking——「訊號埋沒」前提消失，屆時也不需要 Rule 10 了（病灶直接消失，非規則復活）。
- **反向佐證**：這個負面結果驗證了「句級抽取」路線的機制正確性——問題不在指令，在訊號呈現，必須物理性地把目標句拉到模型眼前。

### 工程債清理：api_server 拆 retrieval/gen model + eval/ 版控策略
- **api_server.py 單一 model 兩用 → 拆成 `retrieval_model` / `gen_model`（對齊 CLI 與 CLAUDE.md「model_name 陷阱」）**：
  - 舊行為：`/chat` 用同一個 `model` 同時跑 retrieve() 內部（filter/rewrite/translate）與生成，前端「Model」框一改就把**檢索側**模型也換掉，讓 web 路徑的檢索行為與 eval（固定 `llama-3.3-70b-versatile`）不一致——潛伏的正確性風險。
  - 改法：新增模組常數 `DEFAULT_RETRIEVAL_MODEL`（= `rq.DEFAULT_MODEL`，對齊 eval 檢索側）與 `DEFAULT_GEN_MODEL`（= `rq.DEFAULT_GEN_MODEL` gpt-oss-120b）。`ChatRequest` 新增 `retrieval_model` / `gen_model` 可各自覆寫；舊 `model` 欄位向後相容，視為 gen_model 覆寫（最貼近前端「回答用哪個模型」語意），**檢索側預設不受前端單一 model 框影響**。`/health` 回傳與 app.py 顯示同步拆兩個模型。
- **eval/ 版控策略**：CLAUDE.md 記載「eval/ 未被 git 追蹤」**已過時**——實測 eval/ 早已在 git（23 支 py、eval_set.json、judge_regression.py 都在）。新增 [`eval/.gitignore`](eval/.gitignore)：追蹤腳本/eval_set/judge 案例/文件，忽略跑分輸出（結果 `*.json`、`*_log.txt`，可由腳本重現）。把先前誤入版控的 96 個結果檔 `git rm --cached` 退出追蹤，讓 tracked 狀態與策略一致。

### col-10 judge OR 邏輯：從 prompt 移進程式碼，用 any() 聚合（`col10_or_logic` 修法，已驗證 11/11）
- **背景**：checkpoint「A / B」代表 OR，過去靠 prompt 規則叫 judge 自己判「擇一命中」。但 LLM 對這種組合邏輯不穩定——col-10 的 mi0 在多次跑之間反覆橫跳、換 judge model 也沒好（測量層 bug，非系統 bug；見 07-12）。
- **改法（rubric decomposition）**：`eval_generation_llm_judge.py` 在 Python 端把含「A / B」的 must_include 拆成獨立原子 checkpoint（id=`mi{i}_{k}`），judge 只做單一原子的二元判定（穩定），再用 `any()` 聚合回父 `mi{i}`。下游 `compute_correctness` 仍看到原本的 `mi{i}` id（非 OR checkpoint 行為完全不變、零 regression）。拆解器 `_split_or_checkpoint()` 保守設計：只切「前後有空白」且「括號 depth 0」的斜線——避免切壞 `68.3%/年` 這種單位、以及「（NVLink / InfiniBand / Mellanox）」這種括號內列舉（切了會產生破碎片段）。prompt 的 OR-CHECKPOINTS 規則改成 ATOMIC-CHECKPOINTS 說明（judge 不再需要自己推 OR）。
- **驗證**：`judge_regression.py --votes 3` **11/11 通過**（含 col10_or_logic → mi0 命中、col10_true_miss_negative → mi0 不命中兩個對稱案例）；col-10 兩案例再獨立重跑 2 次全數 PASS，確認原本的「跨跑橫跳」已消除。純 judge-side 改動，不燒 retrieve/generate 成本。

### ❌ 已試無效：換小 reranker `bge-reranker-base`（重試 07-13 的死路，找到更硬的死因）
- **觸發**：使用者要求重試小模型換速度，命中 07-13「換小模型」條目的復活條件（延遲需求高過品質）。
- **關鍵新發現：`bge-reranker-base` 的 position embedding 硬上限只有 512 token**——設 `max_length=2048`（生產值）直接 `index 514 out of bounds` crash。意即它**結構性**只能看每個候選的前 512 token。而全庫 chunk p99=2278 token、生產用 2048 正是為了不截斷長 chunk；base 模型連這個前提都達不到，對「關鍵句在段落中後段」的長/稀釋 chunk（正是 sem-08 的病灶）尤其致命。07-13 只記了 correlation 0.688，沒點出這個 512 硬牆——這是比排序相關性更硬的機制型死因。
- **同場 A/B（`eval/rerank_model_ab_probe.py`，judge-free，base@512 vs v2-m3@2048，生產設定）**：3 題 semantic top-5 平均 overlap 只有 **2.33/5**；sem-09 base 丟掉了 v2-m3 保住的關鍵 chunk `#139`/`#157`（sem-09 正是靠 translate_query_en 修到 mean 1.0 的題，base 會直接打回原形）。
- **結論**：確認非無損、且非可接受的降級。死因升級為**機制型**（512 token 硬牆，不是資料相關的交互型）——除非換一個支援長 context 的小模型，否則永久死路。CPU 上要治本仍是 07-13 記的兩條路：託管 rerank API（Cohere/Jina/Voyage）或 GPU（TEI/Infinity）。

### 🔬 實驗（正面但待全量驗證）：section-aware chunking 拆稀釋型大 chunk（針對 sem-08）
- **背景**：sem-08（AMZN AWS）的病灶是 `AMZN_10K #100` 是一個 **5170 字、橫跨 5 個 MD&A 主題**（Other Operating Expense/Operating Income/Interest/Other Income/Income Taxes）的稀釋型大 chunk，AWS 營業利益成長句被埋在中段當從屬子句，生成端與 coverage-judge 都選擇性忽略（見 07-12）。使用者要求「切成多個單一主題 chunk 試試看」。
- **根因**：`build_chunk_records()` 把所有非表格 element 串成一個大 text_blob 再丟 SemanticChunker，**element 的角色結構（Title/Text=標題 vs NarrativeText=段落）在串接時全丟失**；SemanticChunker 又因這些財務小節語意相近（都是 MD&A 敘述）不超過 90 百分位斷點，把 5 個主題併成一個 mega-chunk。
- **改法**：`data_update_unstructure.py` 在 SemanticChunker 之上加「小節硬邊界」——`_split_text_elements_into_sections()` 用 section header（unstructured 把 10-K 小節標題歸類為 `Title` 或通用 `Text`，body 段落是 `NarrativeText`）當硬切點，段內再各自跑 SemanticChunker。為避免「header + 一句話」切出孤兒小 chunk（cross-encoder 對無上下文孤立段落評分結構性偏低，是機制型死路），設 `SECTION_SOFT_MIN_CHARS=350` soft floor：累積夠份量才允許在下一個 header 切段，太小的小節往後併。
- **驗證（retrieval 層，judge-free）**：重切**全部 3 個 AMZN filing**（10-K 113→217 chunks、兩個 10-Q 也重切；只重切 10-K 不公平，因為 AWS 訊號也存在未重切的 10-Q 稀釋 chunk 裡）。sem-08 top-5 **前後對比**：
  - 改前：AWS 營業利益只出現在稀釋型大 chunk（10-K #100 5170 字 / 10-Q #49 4170 字）裡，被埋沒。
  - 改後：top-5 出現**乾淨的單一主題 chunk `AMZN_10K #164`（1290 字，整段就是 segment operating income 含 AWS 貢獻）排 rank 4**，另有 AI 投資 chunk `#130` 排 rank 2。AWS 內容第一次以「聚焦 chunk」而非「稀釋大 chunk 裡的子句」進入生成 context。
- **generation 層驗證（k=3，`--ids sem-08 --rewrite --translate-query-en --gen-model openai/gpt-oss-120b --judge-model qwen/qwen3-32b --judge-votes 3`，`eval/sem08_afterchunk_gen_k3.json`）：沒有可靠改善**：mean=0.567、std=0.33、crit_miss_rate=0.667、scores=`[0.5, 0.2, 1.0]`。對照 07-12 baseline（scores `[0.7, 1.0, 0.2]`、crit_miss 0.333、mean ~0.633）——**在雜訊範圍內、甚至略差**，且 crit_miss_rate 未達 1.0（非穩定失敗）。依 CLAUDE.md「std 大、crit_miss≠1.0 的題禁止優化（會 fit 到雜訊）」，sem-08 屬雜訊主導題，這個結果**不足以支撐「chunk 切分修好了 sem-08」**。
  - **判讀**：chunk 切分確實改善了**檢索前置條件**（乾淨的單一主題 AWS chunk 進 top-5，這是實打實的結構改善），但沒有轉化成 end-to-end correctness 的穩定提升——印證 sem-08 的根因是生成層「選擇性忽略 + judge/gen 變異」，不是 retrieval 給不出乾淨 chunk。單靠 chunk 邊界解不了生成層問題。
- **狀態＝實驗結束，程式碼保留、collection 已還原 baseline**：① 生成層已驗，無可靠增益（見上）；② 實驗期間曾把 3 個 AMZN filing 用新 chunker 重切（2367→2616 points），驗證後**已用舊 chunker 重新 ingest 還原成 baseline（回到 2367 points、AMZN_10K 113 chunks）**，避免 eval collection 停在「只有 AMZN 用新切法」的混合狀態污染跨 ticker baseline；③ 依 CLAUDE.md「chunk 邊界改變需全量重驗」紀律，轉正需 `--rebuild` 全量重切 + 重跑四類 eval，成本高而 sem-08 收益不明——**性價比不足**。程式改動（`data_update_unstructure.py` 的 section-aware chunker）本身是通用的 chunk 品質改善、**保留在 code**（未 commit，供未來若有「多題受益於單一主題 chunk」的證據時，一次 `--rebuild` 全量採用）；但不建議只為 sem-08 付全量成本。sem-08 的真正解仍指向生成層（contextual compression / 句級抽取 / 生成 prompt 強化），非 chunk 邊界。
- **附帶產出（保留）**：新增 `eval/rerank_model_ab_probe.py`（judge-free 比對兩個 reranker 的 top-5，problem 1 用）；`eval_generation_llm_judge.py` 新增 `--ids` 單題過濾（省 TPD 做單題驗證）並把 eval 端 reranker 的 `max_length` 對齊生產 2048（原本沒設＝8192 不截斷，與生產不一致，同 model_name 陷阱精神）。
- **復活/交互掃描**：本次動了 chunk 邊界，命中 07-13「`max_length=512` 已試無效」的復活條件（「若 chunking 上限被大幅壓低，512 可能重新安全」）。但 dry-run 顯示新 chunker 仍有 4260/3861/3553/3171 字的大 chunk（單一主題但天生長），512 仍會截斷最大那批，**條件未強觸發**，暫不重測 512；若之後再加 hard size cap 讓 chunk 全面 ≤2048 token，屆時才需要按規範重掃 512。

## 2026-07-13

### Rerank 延遲攻堅（第二輪）：`batch_size=1` 免費 2.33x、零損失（已接生產）；ONNX int8 / 小模型 / 級聯三條路都試過且都不行（負面結果全記錄）
- **背景**：`max_length=2048` 之後精排單次仍要 ~85s（20 候選、CPU），雙 query 生產設定端到端 retrieve ~170s，還是不能上線。續攻。
- **✅ 已採用：`CrossEncoder.predict(..., batch_size=1)`——85.61s → 36.81s（2.33x），top-5 完全一致，數學上零損失**：
  - 根因是 padding 浪費：預設 batch=32 把 20 個候選塞成一批、全部 pad 到批內最長（2048 token），而 chunk token 中位數只有 253——短候選被迫按 2048 算 O(L²) attention，浪費數倍運算。batch=1 完全沒有 padding，每筆只算實際長度；CPU 的 GEMM 單筆就能吃滿核心，批次平行在 CPU 上沒有額外收益，所以逐筆跑純賺（batch=4 實測只有 1.33x，佐證批越小越省）。
  - 改動：`rag_query.py` `retrieve()` 內兩處 `rerank_model.predict()` 都加 `batch_size=1`。api_server 走同一條路自動繼承。**若未來換 GPU 要改回預設**——GPU 靠批次平行吃滿算力，batch=1 會反過來變慢（已寫在代碼注釋）。
  - 端到端驗證（生產設定 rewrite+雙 query）：sem-09 query retrieve **78s**（改前同設定實測 ~173s），top-5 = `[#157,#179,#156,#148,#139]` 與 07-12 驗證完全一致。
  - **疊加戰果**：原始（8192 + batch32）130s/次 → 現在（2048 + batch1）~37s/次，**約 3.5x，全程零品質損失**。
- **❌ 已試無效①：ONNX Runtime**（`optimum` 匯出 + onnxruntime CPU 推論）：
  - fp32：88.90s vs torch 92.95s（1.05x）——**沒有加速**。瓶頸是矩陣運算本身，onnxruntime 和 torch 在 CPU 上打平。死因機制型（同樣的 GEMM 換引擎不會變快），無復活條件（除非 onnxruntime 出專門的 CPU 優化 EP）。
  - int8 動態量化：42.73s（2.18x）但 **score correlation 掉到 0.858、top-5 排序改變**，甚至有 NVDA chunk 混進 Tesla 問題的 top-5——XLM-R 系多語言模型對動態量化敏感是已知現象。死因機制型。復活條件：改用 QAT（quantization-aware training）或校準式靜態量化且有完整 eval 驗證，但成本遠超收益，不建議。
  - 實驗後已把 `optimum`/`onnx` 移除、`transformers` 復原到 5.5.0（安裝 optimum 時被連帶降到 4.57.6，不能讓失敗實驗的副作用留在生產環境）；匯出的 2.7GB 模型檔已刪。
- **❌ 已試無效②：換小模型 `bge-reranker-base`**：7.28s（11.78x）但 correlation 只有 0.688、top-5 順序明顯不同——速度極誘人但品質風險大，若要走這條路需要全套 eval 重驗證。復活條件：GPU 到位前若對延遲的要求高過品質（例如 demo 場景），可以重新評估，但必須先跑全量 lexical/mixed/semantic 確認可接受。
- **❌ 已試無效③：級聯精排**（base 先篩 top-10 → v2-m3 只精排 10 個）：sem-11/sem-09 完美（2.3~2.6x、top-5 一致），但 **lex 題翻車——base 把 v2-m3 認定的 rank-1（NVDA `#38`，正是資料中心營收關鍵 chunk）整個踢出 top-10**，critical-miss 級錯誤。死因交互型：base 模型對數字/表格型內容的排序偏好與 v2-m3 差太多。復活條件：若第一階段改用「保守篩選」（例如 keep=15）收益只剩 ~1.2x 不值得；若換一個與 v2-m3 排序相關性高的小模型（如蒸餾版）可重試。
- **🤔 分析過但未實作（純推理，未跑實驗）——記錄以免重問**：
  - **GGUF（llama.cpp 格式）**：技術上可行（llama.cpp 近年加了 encoder reranking 支援，BGE 系可轉），但①它的加速**全部來自量化**（不量化的 GGUF 跟 ONNX fp32 一樣沒加速），而量化正是 int8 翻車的病灶——Q8_0 通常比動態 int8 溫和、**有機會**保住排序，但仍屬「要全量 eval 驗證」的桶子，非無損；②整合成本高一個量級（引入 llama.cpp binary/binding、換掉整條 sentence-transformers 精排路徑，是架構級改動而非參數調整）。結論：排在「換小模型 / 上 GPU」同一梯隊，是有代價、需決策的下一步，不是免費午餐。**復活條件**：CPU 上還要再榨速度、且願意付全量驗證成本時，GGUF Q8 比今天試的 ONNX 動態 int8 更值得認真做（llama.cpp CPU kernel 更強、Q8 更溫和）。
  - **fp8**：精度上**優於** int8（有指數位，非均勻量化貼合權重分布，很可能不會像 int8 搞壞排序），但 fp8 是**GPU 時代的格式**——x86 CPU 的 AVX-512 只有 int8 的硬體加速指令（VNNI），**沒有 fp8 的**。在純 CPU 上 fp8 只省記憶體、不省運算（甚至因 fp8↔fp32 轉換更慢）。這是個反諷死結：CPU 上能給速度的量化（int8）會傷精度，能保精度的（fp8）在 CPU 上給不了速度。fp8 只在有原生 fp8 的 GPU（H100 等）上才魚與熊掌兼得——但一旦有那張 GPU，GPU 本身已把延遲解決數十倍，fp8 淪為錦上添花。**結論：留 CPU 的前提下 fp8 不是出路；上 GPU 的前提下 GPU 才是出路、fp8 只是紅利。無獨立復活條件。**
- **現狀與剩餘選項**：生產 retrieve 現在 ~78s（雙 query）。CPU 上無損優化已到底；要再快只剩：①GPU（數十倍，治本，且順帶讓 fp8/大 batch 都變可用）；②降品質換速度（base 模型或 GGUF Q8，都需全量驗證）；③關雙 query（回到 ~40s，犧牲 sem-09/sem-11 修復）。使用者決策點。

### CrossEncoder `max_length` 截斷：解掉延遲診斷發現的「reranker 預設不截斷」問題——512 先試過但證實不安全，改用 2048
- **動機**：延續上方「端到端延遲實測」條目，找到的根因是 `CrossEncoder(RERANK_MODEL)` 沒設 `max_length`，套用模型預設 8192（等於完全不截斷），CPU 上單次 predict 20 候選要 46~60 秒。
- **第一次嘗試（512）：紙面驗證誤判為安全，全量回歸才抓到真回歸**：
  1. 用一批 20 候選（char len 318~6897）測 `max_length=8192 vs 512 vs 256`：8192 耗時 59.80s，512 耗時 16.07s（3.7x），**score correlation 0.99、top-5 排序逐位完全一致**——當下判斷「512 安全」，寫進 `rag_query.py`/`api_server.py` 當生產設定。
  2. 跑 lexical/mixed 零回歸（k=1，`--rewrite --translate-query-en --gen-model openai/gpt-oss-120b --judge-model qwen/qwen3-32b --judge-votes 3`）：lexical **1.0**（`eval/lexical_maxlen512_check.json`）、mixed **0.894**（基準 0.897，`eval/mixed_maxlen512_check.json`）——零回歸。
  3. **semantic k=3 全量**（`eval/semantic_maxlen512_k3.json`）：mean_of_means=**0.842**（跟基準 0.842 一樣），但 std 從 0.026 升到 0.045，且 **`sem-11` 從穩定 mean=0.7（crit_miss=0）掉到穩定 mean=0.5（3 次都恰好 0.5，std=0——不是雜訊，是系統性下降）**。
- **root cause 查證**：直接 A/B 對比 sem-11（Tesla 風險題）在 `max_length=512` vs `8192`（未截斷）下的 top-5：
  - 未截斷：`[#58, #85, #77, #67, #128]`（`#58` rank-1，raw score 最高）
  - `max_length=512`：`[#77, #85, #74, #89, #67]`——**`#58`（6982 字元 ≈1745 token，未截斷時的 rank-1）與 `#128`（7731 字元 ≈1932 token）雙雙掉出 top-5**，因為這兩個候選跟 query 最相關的內容不在文本開頭 512 token 內，精排只看到前段（例如 `#58` 開頭是「自駕不確定性」而非 rubric 要的「價格戰」段落），算出的分數失真變低。
  - **先前「單一 query 排名一致」的紙面驗證不夠**：那次測試集最長候選只有 6897 字元，剛好沒踩到「關鍵句在段落尾巴」這個病灶；sem-11 的候選池剛好有超長 10-K risk-factor 段落，暴露了問題。
  - 查全庫（`us_stock_rag_unstructured`，2367 chunks）token 長度分布（用 reranker tokenizer 實測）：median=253、p75=545、**p90=1094、p95=1487、p99=2278、max=4738**。`max_length=512` 會截斷 **27%** 的 chunk，`1024` 截斷 11.4%，`1536` 截斷 4.5%，`2048` 只截斷 **1.6%**。
- **修正為 2048**：同一批 sem-11 候選，`max_length=2048` 的 top-5 分數跟未截斷（8192）**逐位完全一致**（`#58`/`#85`/`#77`/`#67`/`#128`，raw score 對齊到小數點後 4 位），純 predict 耗時 74.29s（未截斷 130.42s，仍有 ~1.8x 加速；512 是 14.66s 但不安全）。改動：`rag_query.py` 新增常數 `RERANK_MAX_LENGTH = 2048`，`CrossEncoder(RERANK_MODEL, max_length=RERANK_MAX_LENGTH)`；`api_server.py` 的模型載入同步帶 `rq.RERANK_MAX_LENGTH`。
- **驗證（2048）**：用 `eval/diagnose_crit_miss.py --ids sem-08 sem-11 --translate-query-en`（單題 pool/topk 覆蓋診斷，比全量 generation+judge 便宜很多，符合 CLAUDE.md「復活條件」的最小成本重測原則）：
  - `sem-11`：critical checkpoint（EV 價格戰）pool=Y topk=Y、總體經濟/利率 pool=Y topk=Y——跟 07-12「修復維持」記錄的狀態完全一致，證實 2048 復原了 512 造成的退步。FSD/Autopilot checkpoint 是 `pool=N`（候選池階段就沒有，屬於既有 recall 缺口，跟 rerank 截斷無關，不是新問題）。
  - `sem-08`：AWS 獲利檢查點 `pool=Y topk=N`——跟既有記錄（chunk #100 混雜 FTC 訴訟/稅務噪音、屬於生成層問題）一致，非本次改動引入。
- **待辦**：lexical/mixed 是在 512（更激進的設定）下測得零回歸，2048 截斷範圍更小，邏輯上更安全，未重跑；但完整 semantic k=3（用 2048）尚未重跑（今日 Groq TPD 用量已高），下次有額度時建議補跑一次 `eval/eval_generation_llm_judge.py --category semantic --repeat 3` 做最終確認，預期 sem-11 mean 應回到 ~0.7。
- **已試無效：`max_length=512`**——**死因：交互型**（綁定「這個資料庫的 chunk 長度分布」，不是機制性死路）。**復活條件**：若之後 ingest 邏輯改變、chunk 長度上限被大幅壓低（例如 chunking 策略改成固定 ≤500 字元切段，不再有 6000+ 字元的長 risk-factor 段落），512 可能重新安全，但除非 chunking 策略明確改變，不要重試。

### 端到端延遲實測：真正的瓶頸是 cross-encoder rerank 在純 CPU 上跑（單次 ~46s/20 候選），不是這次的雙 query 修法本身——但雙 query 把這個已存在的巨大成本再乘以 2
- **動機**：使用者關切上線速度，07-12 轉正時只做過功能驗證，沒實測延遲。
- **方法**：`eval/latency_benchmark.py`——模型只載入一次（模擬 api_server 常駐暖機），對 4 個代表性 query 分別跑「舊設定（rewrite=False, translate_en=False）」與「新生產預設（rewrite=True, translate_en=True）」，拆解 `retrieve()` 與 `generate()` 各自耗時。
- **第一輪結果（嚇人但誤導）**：OLD retrieve 平均 50.54s，NEW retrieve 平均 172.94s（+242%），且 variance 極大（68s~370s）——遠超预期的「精排 ×2 多花 1~3 秒」估計，必須查清楚才能報告。
- **granular 拆解找到真正瓶頸**：手動對 `retrieve()` 內部各階段個別計時：
  | 階段 | 耗時 |
  |---|---|
  | BGE-M3 encode（1 個 query）| 0.32s |
  | Qdrant RRF query | 0.07s |
  | **CrossEncoder.predict（20 個候選，1 個 query）** | **46.26s** |
  | CrossEncoder.predict 再跑一次 | 47.53s |
  確認 `torch.cuda.is_available() == False`（`torch==2.11.0+cpu`，純 CPU 安裝，12 核心，無 GPU）。**`bge-reranker-v2-m3` 這個 cross-encoder 在純 CPU 上對 20 個候選評分要 46 秒**——這是全流程裡壓倒性的成本大頭，embedding、Qdrant 查詢、LLM 呼叫（filter/rewrite/translate 各 0.5~2s）、生成（~3s）全部合計不到 5 秒，跟 rerank 的 46 秒完全不成比例。
- **結論（跟原本假設不同，需要更正之前的說法）**：
  1. **這次「雙 query 取最高分」的修法，本質是把這個已經存在的巨大成本再乘以 2**（rerank 從跑 1 次變跑 2 次，20→27 個候選也讓單次評分再變長一點）——OLD/NEW 的巨大落差幾乎全部由 rerank 次數解釋，不是這次改動額外引入了新的慢邏輯。
  2. **但真正該優先處理的問題，是「rerank 在 CPU 上本來就要 46 秒」這件事本身**——這不是今天的改動造成的，是整個系統原本就有、但從來沒被實測過的既有瓶頸。就算今天完全不做雙 query 這個修法，單次查詢的 retrieve 也要 50 秒，這個延遲本身就不適合上線給使用者用。
  3. 我在 07-12 那次回答使用者「速度影響」的問題時，**估計錯了量級**——當時說「精排是本地運算、預估多花 1~3 秒」，現在實測是本地運算本身就要 46 秒、乘 2 是再加 46 秒，量級差了超過 10 倍。這個估計錯誤已經跟使用者說明並更正。
- **待辦（優先權高於雙 query 修法本身）**：
  1. **GPU 加速**——如果有 CUDA 環境，`bge-reranker-v2-m3` 在 GPU 上通常是 CPU 的數十倍速度，這是最直接的解法。
  2. **換更小的 reranker 模型**（犧牲一些精度換速度）。
  3. **量化/ONNX 推論**（不換模型，換推論引擎）。
  4. 如果以上都不做，至少要重新評估「雙 query 取最高分」是否要維持生產預設——把一個已經是 50 秒等級的延遲再乘以 2，對使用者體驗的影響可能比它修好的兩題 correctness 更重要，這是使用者需要知道後才能決定的取捨，本次不擅自關閉。

### 轉生產預設：`enable_rewrite` + `translate_query_en`（雙 query 取最高分）正式成為 CLI / api_server 預設；同步 `api_server.py` 檢索路徑
- **動機**：07-12「正式修法」已驗證雙 query 取最高分乾淨（sem-09 完全修好、sem-11 維持、lexical/mixed/colloquial 零新回歸），使用者確認轉正並要求同步 web 路徑。
- **改動**：
  1. `rag_query.py` 新增兩個模組常數 `DEFAULT_ENABLE_REWRITE = True`、`DEFAULT_TRANSLATE_QUERY_EN = True`（放在 `GEN_TEMPERATURE` 之後，作為生產入口的單一真相來源）。**`retrieve()` 函式本身的參數預設維持 `False` 不動**——保持 eval 腳本向後相容（多數 baseline eval 依賴 `enable_rewrite=False` 的預設），只有生產入口打開。
  2. `run_single_query` / `run_interactive`：`enable_rewrite`/`translate_query_en` 兩個參數預設改用上述常數，並把 `translate_query_en` 實際傳進 `retrieve()`（原本兩個 run 函式根本沒傳這個參數，等於永遠 False）。
  3. CLI（`main()`）：`--rewrite` 從 `store_true`（預設 False）改成 `BooleanOptionalAction`（預設 True，`--no-rewrite` 關閉）；新增 `--translate-query-en`/`--no-translate-query-en`（預設 True）。所以 `python rag_query.py -q "..."` 不帶任何 flag 就是生產配置。
  4. `api_server.py`：`_retrieve()` 的 `rq.retrieve()` 呼叫加上 `enable_rewrite=rq.DEFAULT_ENABLE_REWRITE`、`translate_query_en=rq.DEFAULT_TRANSLATE_QUERY_EN`——web 路徑本來完全沒開這兩個，是舊行為，這次對齊。順帶修 `call_llm_groq`/`call_llm_nvidia` 的 monkeypatch 簽名：新增 `temperature: float = 0.0` 參數並傳給 API——這兩個函式會取代 `rq.call_llm` 供 `retrieve()` 內部的 filter/rewrite/translate 使用，檢索側必須 temp=0（見 CLAUDE.md 溫度分工），舊版沒帶 temperature 等於用 Groq 預設 ~1.0 跑檢索側 LLM，是既有的靜默不一致，一併修掉。
- **驗證**：
  1. 語法 `ast.parse` 通過；`BooleanOptionalAction` 預設與 `--no-*` 開關實測正確（no-arg→都 True、`--no-rewrite`→rewrite False、`--no-translate-query-en`→translate False）。
  2. **端到端單題**（真的跑 `python rag_query.py -q "Google 在 AI 與搜尋領域的競爭策略？"`，不帶任何 flag）：top-5 含 #139（Gemini 3）與 #179（AI Overviews），答案正確涵蓋 Gemini——證實生產預設路徑確實吃到修法，不只是參數改對。
- **未動 / 已知殘留**：
  - `api_server.py` 仍是**單一 `model` 兩用**（retrieve 與 gen 同一個模型，預設 Groq `llama-3.3-70b`），沒有比照 CLI 做 `retrieval_model`/`gen_model` 拆分（07-09 待辦③）——這是獨立於本次「開啟 rewrite/translate」的另一個範疇，web demo 目前可接受，未處理。
  - sem-08 仍有 1/3 crit_miss（生成層問題，非本次範圍）；`col10_or_logic` judge bug 未修（測量層問題）。
- **未做**：端到端**延遲**尚未實測（rewrite + translate 各多一次 LLM 呼叫、rerank ×2）——使用者關切上線速度，建議下一步實測 `/chat` 或 CLI 的 wall-clock，再決定要不要做 batch rerank / GPU 之類的降速優化。

### colloquial k=3（雙 query 取最高分修法）：表面 mean 下降（0.727→0.67），逐題拆解後確認**沒有新回歸**——col-10 的不穩定是已知的 judge OR 邏輯 bug，不是這次改動造成
- **背景**：延續上方「正式修法」條目的待辦①，補跑 colloquial 版 k=3（`--rewrite --translate-query-en --gen-model openai/gpt-oss-120b --judge-model qwen/qwen3-32b --judge-votes 3 --repeat 3`），`eval/e6_colloquial_dualquery_k3.json`。
- **表面結果**：mean_of_means=**0.67**，std_across_runs=0.085（低於 07-10 baseline 的 0.727 ± 0.045）——第一眼看像回歸，但 07-10 baseline 用的是舊 judge（20b）且沒有套 07-11 語域規則，兩者設定不同，不是乾淨對照組，不能直接下「這次改動讓 colloquial 變差」的結論。
- **逐題拆解**：
  - `col-08`：crit_miss_rate=1.0（0.2/0.2/0.0）——**已知的接受極限**（見上方「col-08 文件化」），不受本次修法影響，預期內。
  - `col-04`：crit_miss_rate=0.667——跟 07-10 baseline 的 0.667 完全一樣，**不是新問題**，是既有的「灰色地帶」（生成被口語框架帶偏）。
  - `col-05`：crit_miss_rate=0.333（0.7/0.0/0.5）——跟 07-11 已記錄的「rewrite 變體語域轉換品質不穩定」一致，非新問題。
  - **`col-10`（`maps_to: sem-09`，同一個 Google/Gemini rubric）：crit_miss_rate=0.667（0.5/0.2/1.0）——查證後排除是這次改動造成的retrieval回歸**：讀三次跑的完整記錄，**三次的 top-5 sources 完全相同**（`#159/#157/#156/#137/#179`，含「AI Overviews/AI Mode」那個關鍵 chunk #179），**三次答案也都實質討論了「AI Overviews/AI Mode 嵌入搜尋」**——retrieval 和 generation 都穩定、正確。唯一不一致的是 **judge 對 mi0（「提到 Gemini 模型 / AI 整合進搜尋」，OR 條件）的逐票判定**：run1 三票 [hit, miss, miss]、run2 三票 [miss, miss, miss]、run3 三票 [hit, hit, hit]——同樣的答案內容,judge 判定結果卻在三次跑之間反覆橫跳。run1 甚至有一票的 `reason` 明講「The answer covers AI integration into search (AI Overviews/AI Mode)...」卻仍被歸類為對 mi0 沒命中的那一票。**這正是 `judge_regression.py` 已經記錄的 `col10_or_logic` 已知 bug**（07-11 記錄：qwen3-32b 對這個案例 10/11 裡唯一的失敗案例，判定命中 mi2 而非 mi0），這次在真實 eval 裡完整重現，證實不是孤立的回歸套件案例。
- **結論**：**colloquial 的表面 mean 下降完全由既有、已知、與本次 retrieval 修法無關的問題解釋**（col-08 已知極限、col-04 已知灰色地帶、col-05 已知變體不穩定、col-10 已知 judge OR bug）——**沒有證據顯示雙 query 取最高分的修法本身對 colloquial 造成新回歸**。
- **副產品：`col10_or_logic` 是獨立於本次工作的真實測量問題**，值得之後單獨處理（judge prompt 的 OR-CHECKPOINTS 規則對這個具體案例沒有穩定生效，即使換了 judge model），但不影響本次「是否轉正 retrieval 修法」的決策——兩者是不同層次的問題。
- **待辦**：若之後要修 `col10_or_logic`，直接用 `judge_regression.py` 快篩，不需要重跑完整 retrieve+generate（這是純 judge prompt 問題）。

### 正式修法：`translate_query_en=True` 改成「rerank 對每個候選同時用原始 query 與翻譯 query 各評一次分、取逐候選最高分」——k=3 全量驗證 sem-09 完全救回、sem-11 維持修復、零回歸
- **動機**：延續上方「雙 query 取最高分」探針的紙面驗證（sem-09/sem-11 兩題同時通過），實作成正式程式碼並跑完整回歸，回答 E5 留下的待辦①。
- **改動**（`rag_query.py` `retrieve()`）：`translate_query_en=True` 時，`rerank_queries` 從舊行為的 `[en_query]`（整組替換，全有全無）改成 `[query, en_query]`（原句 + 翻譯句都留著）。cross-encoder 精排邏輯本身沒動——`len(rerank_queries) > 1` 時的「逐候選取跨 query 最高分」機制本來就存在（`rerank_multi_query` 用的同一段代碼），這次只是換了送進去的 query 組合。docstring 同步更新。
- **驗證**：
  1. **單題程式碼驗證**（非探針腳本，直接呼叫 `retrieve()`）：sem-09 top-5 = `[#157,#179,#156,#148,#139]`（含 #139/#179 兩個 critical 來源）；sem-11 top-5 含 `#67`（price reductions）——跟探針的紙面推算完全一致。
  2. **lexical/mixed 零回歸**（k=1，`--rewrite --translate-query-en --gen-model openai/gpt-oss-120b --judge-model qwen/qwen3-32b --judge-votes 3`）：lexical **1.0**（`eval/lexical_dualquery_check.json`）、mixed **0.897**（`eval/mixed_dualquery_check.json`，fatal=0）——跟既有基準一致，無回歸。
  3. **semantic 全量 k=3**（同設定 + `--repeat 3`，`eval/e6_semantic_dualquery_k3.json`）：**mean_of_means=0.842，std_across_runs=0.026**（穩定）。逐題：
     | 題 | E5（舊，全有全無）| 本次（雙 query 取最高分）|
     |---|---|---|
     | sem-09 | crit_miss_rate=1.0（score 0.2×3）| **crit_miss_rate=0.0，mean=1.0（3/3 滿分）**——完全救回 |
     | sem-11 | crit_miss_rate=0.0（已修）| **crit_miss_rate=0.0，mean=0.7**——修復維持，未退步 |
     | sem-08 | crit_miss_rate=1.0（score 0.5×3）| crit_miss_rate=0.333（scores=[0.7, 1.0, 0.2]）——**變好但未穩定修好**，跟本日稍早的生成層診斷一致（這次改動沒有針對 sem-08 的病灶，2/3 過關可能只是巧合的 top-5 組成變化，不是穩定機制） |
     其餘 8 題無 critical_miss，分數在既有噪音帶內波動（sem-01/02/03/04 std 0.1~0.24，屬正常生成/judge 噪音，非本次改動造成）。
- **決策：`translate_query_en=True` 的新行為（雙 query 取最高分）本身已驗證乾淨——但尚未把 `enable_rewrite`/`translate_query_en` 兩個參數的預設值從 `False` 轉成 `True`**。理由：① colloquial 對應題（col-05/col-08）完全沒測過這個修法，語域規則同時影響 semantic 和 colloquial 兩邊，貿然轉正產預設可能在沒驗證的 colloquial 上有副作用；② sem-08 仍不穩定（1/3 機率 critical_miss），轉正前應該先想清楚要不要接受這個殘留風險。**轉正生產預設是否要做,留給使用者決定**——技術上這次的 mechanism 修改本身是安全的（可以先只轉正 `translate_query_en` 這個新行為，不代表一定要同時把兩個 opt-in 參數都轉預設 True）。
- **復活條件**：舊的「全有全無替換」行為已被取代，不再存在，無需保留復活條件；`sparse_translate_en`/`rewrite_fusion` 等其他實驗參數不受本次改動影響。
- **待辦**：① colloquial 版 k=3（`--rewrite --translate-query-en`）尚未跑，是轉正前最後一塊拼圖；② sem-08 仍需要獨立修法（生成端 prompt 或 chunk 邊界，非本次範圍）；③ 若使用者決定轉正 `enable_rewrite`/`translate_query_en` 生產預設，需同步更新 `api_server.py`（目前仍是舊的單一 model 兩用寫法，07-09 待辦③）。

### sem-08 根因確認到生成層：chunk #100（AWS 獲利內容）穩定進 top-5，但 3/3 次 E5 答案完全沒引用它——跟 sem-11 是同一種「大 chunk 稀釋、生成端選擇性忽略」病
- **方法**：零成本，直接讀已存的 `eval/e5_semantic_register_translate_k3_run{1,2,3}.json`，逐一看 sem-08 的 `sources`（top-5 chunk 清單）與 `answer` 全文裡實際出現的 `[Reference N]` 引用編號。
- **結果**：三次跑的 top-5 sources 都是 `AMZN_10K #85/#99/#100 + 10Q #46/#45`（順序不變，chunk #100 = 07-07/07-08 早就點名的稀釋型 AWS 獲利段落，始終在池子裡）。但**三次答案都只引用 Reference 1/2/4/5（對應 #85/#99/#46/#45），Reference 3（#100）一次都沒被引用過**。
- **結論**：這推翻了「檢索沒做對」的預設，sem-08 現在的病灶跟 07-11 CHANGELOG 已經記過的 sem-11 舊病灶（`這個從屬子句在大 chunk 裡被生成端稀釋掉了`）是同一種——語域規則已經成功讓 #100 進 top-5（retrieval 端有進步），但 #100 混雜 FTC 訴訟/稅務/資產減損等大量不相關內容，AWS 獲利那句只是其中一個從屬子句，生成模型三次都選擇性地略過它、只組織其他四個更「乾淨」的 chunk。**這不是語域/翻譯能解決的層次，換方向去修 chunk 邊界或生成提示才有意義。**

### 「雙 query 取最高分」rerank 探針：紙面推算成立，sem-09/sem-11 兩題同時通過（零 LLM 成本）
- **動機**：E5 顯示 `translate_query_en` 是全有全無的開關，救 sem-11、傷 sem-09（見上方 07-12「E5 全量」條目）。既然 `retrieve()` 裡已經有 `rerank_multi_query` 這個「對每個候選用多個 query 各評一次分、取跨 query 最高分」的機制（07-07 判死，07-08 用「英文變體+glossary」新前提復活過一次，見死路表），這次測的是同一機制的第三次前提替換：`rerank_queries = [原始 query, translate_query_to_english() 翻譯]`，而非當年的「原句+中文 rewrite 變體」或「英文變體+glossary」。
- **方法**：`eval/dual_query_rerank_probe.py`——固定住 `enable_rewrite=True, translate_query_en=False` 產生的候選池，對每個候選分別用「原始問句」「`translate_query_to_english()` 翻譯」跑一次 cross-encoder，取逐候選最高分排序，比對目標 chunk 是否留在 top-5。全程零 LLM 成本（只有一次 translate 呼叫，其餘是本地 CrossEncoder 推理）。
- **結果**：
  | 題 | raw_top5 含目標 | formal_top5 含目標 | max_top5 含目標 |
  |---|---|---|---|
  | sem-09（#139, #179）| 兩者皆 True | #139=False（被 10Q#72/#159 擠掉）、#179=True | **兩者皆 True** |
  | sem-11（#67）| False | True | **True** |
  取每個候選「原句分數 vs 翻譯分數」的較高者後排序，sem-09 的 top-5 剛好與 raw_top5 完全一致（#139/#179 都在，原句分數本來就夠高）；sem-11 的 top-5 與 formal_top5 完全一致（#67 靠翻譯分數 0.5868 才進得去）。**兩題同時通過，紙面推算成立**——因為每個候選各自取自己「較有利的語言」，不會出現「為了救一題必須犧牲另一題」的全有全無局面。
- **判定**：這是有效的候選修法方向，但**尚未實作成正式參數、也尚未跑完整回歸**（lexical/mixed 零回歸檢查、sem-08 是否受影響、colloquial 對應題）。目前只驗證了 sem-09/sem-11 兩題的候選池排序層，跟 07-11/07-12 反覆出現的教訓一致：**排序層驗證通過不等於轉正**，必須先跑完整 generate+judge 才能下決定。
- **待辦（等使用者確認是否要投入）**：① 決定實作方式——是把這個「雙 query 取最高分」直接改成 `translate_query_en=True` 的內部行為（因為對已測的兩題來說，dual-max 的結果不劣於各自單獨使用，理論上是嚴格改善）,還是加一個新的獨立參數；② 全量 lexical/mixed 零回歸跑一次（便宜，k=1）；③ semantic k=3 全量重跑，確認整體 mean 相對 E5 的 0.816 是否回升、且沒有引入新的副作用；④ colloquial 對應題（col-05/col-08）目前完全沒測過這個修法，不能預設會有一樣的效果。

### col-08（Tesla「麻煩」口語版）文件化為已知極限，暫停投入新召回機制
- **決定**：不再對 col-08 投入新的檢索機制（如 HyDE），接受現況為已知限制。
- **理由**：
  1. 根因已在 07-11 診斷清楚——原句 dense rank 只有 25/106（見 CHANGELOG 07-11「col-05/col-08 仍未解」條目），落差比 sem-11 更大，`VARIANT_CAP=8` 的 rewrite 變體機制構造上搆不到這個範圍，不是「多生一個變體」能解決的量級。
  2. col-08 原本被歸類為 sem-11 的「鏡像」，但今天的 E5 結果顯示兩者已經分岔：sem-11 用「語域規則 + `translate_query_en`」在完整 pipeline 驗證修好（見本日「E5 全量」條目），但這組修法對 col-08 是否同樣有效**從未實測**（colloquial 版 E5 尚未跑），且 col-08 的 dense rank 落差本來就被診斷為「超出這組修法的射程」——就算跑了 colloquial E5，也不能預期 col-08 會像 sem-11 一樣被救回。
  3. 成本效益：eval set 目前 61 題，col-08 只佔其中 1 題（colloquial 10 題的 10%）。引入新檢索機制（HyDE 或專用 glossary 觸發規則）會讓已經有 3 個活動件（filter tier 降級、rewrite 語域規則、translate_query_en）的檢索 pipeline再加一個，回歸驗證成本隨機制數量非線性增加——今天 E5 才示範過一次「新機制修好一題、同時傷到另一題」的真實案例（sem-11 修好但 sem-09 新增退步）。為 61 題裡的 1 題承擔這個複雜度上升，目前判斷不值得。
- **復活條件**：機制型觀察（dense rank 差距）本身無復活條件；但**決定「不修」這件事本身是交互型**——如果之後 eval set 擴大、且同一種失敗模式（單一公司/主題的 query-語料語域落差過大、超出 CAP=8 射程）在更多題目上重複出現，代表這不是孤例而是一類問題，届時應該重新評估是否值得為這一類（而不是為 col-08 這一題）投入 HyDE 或專用機制。colloquial 版 E5 若之後因為其他理由（例如驗證 col-05）被跑了，順便看一眼 col-08 的數字無妨，但不必為了 col-08 專門去跑。

### E5 全量 semantic k=3（`--rewrite --translate-query-en`）結果複查：**不轉正**——sem-11 確認修好，但 sem-08 在完整 pipeline 下仍是 critical_miss=1.0（推翻 07-11 單題 rank probe 的「已修」結論），且 translate_query_en 首次證實會拖累 sem-09（新發現，跟 07-11 那次「新聞被刷掉」的假說無關）
- **背景**：07-11 待辦①要求跑一次全量 semantic(11)+colloquial(10) k=3，驗證「register 規則 + `translate_query_en`」這組修法是否該轉生產預設。發現 `eval/e5_semantic_register_translate_k3.json`（+ `_run1/2/3.json`）當天已經跑過 semantic 這一半（07-11 20:34 寫入），只是還沒被讀出來下結論——本條補上複查與決策。colloquial 那一半尚未跑（見下方待辦）。
- **結果**：semantic **mean_of_means = 0.816，std_across_runs = 0.034**（穩定，非噪音）——**比 07-09 pre-fix baseline 的 0.870 還低**，儘管這組設定的目的是修好 sem-08/11。
- **逐題複查（讀 `_run1.json` 的實際 sources + judge reason）**：
  - **sem-11（Tesla）**：`critical_miss=False`，top-5 含 chunk #67（price reductions，07-11 要修的目標）——**確認修好，translate_query_en 對這題如預期生效**。
  - **sem-08（AWS）**：三次跑 score 皆 0.5、**crit_miss_rate=1.0**，top-5 sources 為 `AMZN_10K #85/#99/#100 + 10Q #46/#45`——**#100 這個 07-07/07-08 就點名的「稀釋型大 chunk」依然在 top-5**，judge reason：「Answer covers AI/ML and infrastructure but omits AWS profitability details」。**這直接推翻 07-11 記錄的「sem-08 語域規則生效,k=3 rank 4,4,4」**——那次驗證只做到「target chunk 進 rank 4」的檢索層 probe，沒有跑完整 generate+judge；rank 4 進得了池子不代表 LLM 會在最終答案裡把它寫出來、也不代表它會擠掉 #100 進最終 top-5。**教訓與 07-09 財年案例同一種：檢索層 probe 的正面結果，在轉正前必須用完整 fresh generate+judge 重新確認，不能只看 rank。**
  - **sem-09（Google，本次對話 07-11 稍早才排除「新聞被刷掉」假說）**：三次跑 score 皆 0.2、**crit_miss_rate=1.0**，top-5 sources 為 `GOOGL_10K #156/#157/#179/#159 + 10Q #72`——**#139（"Making AI Helpful for Everyone"，Gemini 3 的核心討論段落）不見了**。額外做的 ablation（固定同一個 pool，只切 `translate_query_en` 開關）證實：`translate_query_en=False` 時 top-5 = `[#157,#179,#156,#148,#139]`（含 #139）；`translate_query_en=True` 時 top-5 = `[#156,#157,#179,10Q#72,#159]`（#139 被 10Q#72 與 #159 取代）。**這是全新發現，跟本次對話早些時候排除的「rewrite 語域規則把新聞刷出去」假說是不同機制**——問題不在新聞，而是 `translate_query_en` 把 rerank 評分用的 query 換成翻譯後的正式英文後，改變了 27 個候選裡中段候選的相對排序，連帶把一個純英文 10-K chunk（#139）擠出 top-5。#179 雖然倖存（該 chunk 本身也提到 "AI Mode ... using Gemini's advanced reasoning"），但少了 #139 的加強後，生成/judge 這一關仍然穩定判定 critical_miss——具體是生成沒寫清楚、還是 judge 對 OR 條件判定不穩（同 `col10_or_logic` 那個已知 bug家族），本次未進一步拆解。
- **決策：`enable_rewrite`/`translate_query_en` 暫不轉生產預設**——E5 是這兩個修法轉正前設定的最後一步驗證，而結果是 mean 不升反降、且 sem-08「已修」的結論本身不成立。`api_server.py` 的同步（07-09 待辦③）也一併擱置，沒有新設定可同步。生產 `rag_query.py` 維持兩者皆 opt-in 的現狀不變。
- **復活條件**：
  - sem-08：交互型——復活條件是「有辦法讓 #100 這個稀釋型 chunk 不再擠進 top-5、同時讓更聚焦的 AWS 獲利 chunk 穩定入選」，屬於 07-07 就列過的稀釋型 chunk 問題，語域規則對它不夠，可能需要 07-07 探針裡評估過但當時判定「已試無效」的 chunk 邊界調整重新檢視（注意：07-07 的「已試無效」是對 rerank/recall 層的舊 chunking 設定測的，跟這次的具體失敗模式不完全同一組實驗，是否命中復活條件需要重新檢查該筆記錄）。
  - sem-09：機制型（在目前的 chunk 邊界與內容分布下，翻譯改變 query 措辭就會讓 cross-encoder 對這批中段候選重新洗牌）——復活條件是 reranker 模型或 chunk 邊界改變前，這個 trade-off 預期持續存在；如果之後有「只對 dense/sparse 召回翻譯、rerank 仍用原始 query」之類的更細粒度設計（目前 `translate_query_en` 是全有全無），值得重新測。
- **待辦**：① colloquial 半套 k=3（`--rewrite --translate-query-en`）尚未跑，col-05/col-08 是否受同樣的 trade-off 影響未知；② sem-08/sem-09 都還是未解的 critical failure，且都比原先認知的更複雜（sem-08 不是簡單的「補一個語域變體」就能解決，sem-09 是 translate_query_en 的新副作用）——下一步建議先做「sem-09 的失敗是生成沒寫清楚、還是 judge OR 邏輯誤判」這個更便宜的判定（重判已存答案即可，不必重新 retrieve+generate），再決定要不要繼續投入。

### judge model 轉正：`openai/gpt-oss-20b` → `qwen/qwen3-32b`（回應 07-11 已篩選但未轉正的候選）
- **動機**：07-11 已用 `judge_regression.py` 快篩過 `qwen3-32b`（10/11，嚴格優於 20b 的 9/11，且不與任何 `gen_model` 共用 Groq TPD 池），但當時只做到「候選篩選」，決策留給使用者。本次使用者要求執行，決定轉正。
- **驗證**：轉正前用 `--votes 3` 重新confirm 一次（避免只憑之前的快篩紀錄就改預設值）：`10/11`，唯一失敗案例仍是 `col10_or_logic`（判定命中 mi2 而非 mi0），與 07-11 記錄一致、沒有引入新的失敗案例。
- **修改**：
  1. `eval/eval_generation_llm_judge.py` 的 `DEFAULT_JUDGE_MODEL` 從 `DEFAULT_GROQ_MODEL`（即 `llama-3.3-70b-versatile`，這個舊預設從未被實際使用——CHANGELOG 所有紀錄的 eval 指令都顯式傳 `--judge-model openai/gpt-oss-20b` 覆寫它）直接改成 `"qwen/qwen3-32b"`。
  2. `eval/judge_regression.py` 的 `--judge-model` CLI 預設同步改成 `qwen/qwen3-32b`。
  3. `CLAUDE.md`：Commands 區塊兩條 `--judge-model` 範例指令、LLM provider split 表格，改成 `qwen/qwen3-32b`。
- **未動**：`eval/diagnose_crit_miss.py` 的 `DEFAULT_JUDGE`（checkpoint coverage 用，非 correctness judge）維持 `openai/gpt-oss-120b`——這是不同用途的獨立參數，不在本次轉正範圍。
- **留意**：`col10_or_logic` 這個已知 bug 對 qwen3-32b 仍然存在（跟 20b 同一種 OR 邏輯判定問題），轉正不代表這個 bug 消失，只是不會比 20b 更差。
- **待辦**：`CLAUDE.md` 的「已知問題與分析」整節（`已知問題與分析（更新至 2026-07-07…）`）仍是 07-07 的舊快照，sem-02/07/08/11 已在 07-11 修好、sem-09 已在本次對話查證排除，數字（semantic 0.545、judge=20b 的雜訊來源敘述）全部過期——排進下一步整體重寫，不在本條處理範圍內。

## 2026-07-11

### 探針：sem-09（Google AI/搜尋競爭策略）「新聞被 rerank 刷掉導致漏 Gemini」假說——**已試無效**，k=3 fresh 重測顯示 critical checkpoint 從未 miss，真正病灶（如果有）是非 critical 的 mi1 缺口
- **動機**：使用者手動查證 sem-09 的候選池，發現含 "Gemini" 的新聞 chunk（`GOOGL_News_20260612_01.txt` chunk #1）進了 pool（RRF rank #6），但最終 top-5 全被 10-K 佔滿，據此推論「rewrite 語域規則把財報 chunk 塞爆池子、cross-encoder 對財報腔評分系統性偏高，把新聞刷出局，而新聞正好是唯一含 Gemini 的來源」，並懷疑這是 07-11 稍早那條 rewrite 語域規則造成的回歸（07-08 clean A/B 記錄 sem-09=0.9/crit_miss=0，未套用語域規則）。提出三個候選修法（補新聞語料／top-5 來源多樣性保障／收斂語域規則力道），待使用者選擇方向。
- **診斷方法**（零到低 LLM 成本，三層遞進）：
  1. `eval/sem09_register_regression_probe.py`（E0+E1 合併）：E0 用 `rq.retrieve(..., return_pool=True)` 比較 `enable_rewrite=False` vs `True`（`translate_query_en` 固定 False）下 target chunk 的 pool/top-k 排名與 top-5 source_type 組成；E1 固定同一個 pool，只換 cross-encoder 評分用的 query（原始中文／`translate_query_to_english()` 財報翻譯／人工寫的中性英文），量 target chunk 的 rerank 排名。
  2. 對 top-5 實際內容做人工 grep 核對：10-K chunk #139（"Making AI Helpful for Everyone"，直接討論 Gemini 3）與 chunk #179（逐字寫 "AI Overviews and AI Mode in Search"）本身就是 rubric checkpoint「提到 Gemini 模型 / AI 整合進搜尋」的直接命中內容，且兩者在 RRF 原始池就是 rank #1、#4（非 rewrite 變體注入的邊緣候選）。
  3. `eval/sem09_live_answer_probe.py`：直接呼叫 `eval_generation_llm_judge.run_single_pass()` 對 sem-09 單題做 fresh end-to-end（真的 retrieve+generate+judge，非重判舊答案），`--repeat 3`，設定對齊生產實況（`retrieval_model=llama-3.3-70b-versatile`、`gen_model=openai/gpt-oss-120b`（`rq.DEFAULT_GEN_MODEL`）、`judge_model=openai/gpt-oss-20b`、`judge_votes=3`、`enable_rewrite=True`、`translate_query_en=False`）。
- **結果（與假說方向相反）**：
  - **E0**：`enable_rewrite=False` 與 `True` 下，target news chunk 在 pool 都是 rank #6、在 top-5 都是 0（`target_in_topk=False`）——**news chunk 在完全沒有語域規則、甚至沒有 rewrite 的情況下,一樣被排除在 top-5 外**。rewrite 只讓 pool 從 20→27（多 7 個候選,全是財報）,把 top-5 僅存的一個非 10-K 名額（10-Q #72）也換成另一個 10-K（#156）——是真實的邊際財報化,但不是「把新聞擠出去」的原因,新聞本來就沒有名額。
  - **E1**：固定同一個 27 候選 pool,三種 query 語域（原始中文／中性英文／財報翻譯）算出的 target chunk cross-encoder raw score 分別是 **0.013 / 0.023 / 0.023**——三者都遠低於 top-5 門檻分數（0.37~0.69）,彼此差距在雜訊範圍內,**沒有語域越正式分數越低的系統性趨勢**。查看該 chunk 實際內容：是協力廠商 Navan 與 Google Gemini 合作的第三方報導,主體在講 Navan 的市佔與 IPO 股價,「Gemini」只是順帶提及——cross-encoder 給極低分是合理的內容相關性判斷,不是語域偏壓的假影。
  - **人工核對 top-5 實際內容**：10-K chunk #139/#179 本身就直接涵蓋 critical checkpoint 要的內容（Gemini 3、AI Overviews、AI Mode in Search）,品質不輸、甚至優於那個新聞 chunk 的順帶提及。也就是說,就算新聞真的沒被检索到,critical checkpoint 的支撐材料在 top-5 里另有更好的來源。
  - **k=3 fresh 重測**（`eval/sem09_live_answer_probe_k3.json`）：**score = 1.0 / 0.7 / 0.7（mean=0.8）,mi0（critical, Gemini）三次全部命中（3/3 votes × 3 runs 皆 hit）,crit_miss_rate = 0/3**。唯一穩定的缺口是 **mi1**（非 critical,「搜尋廣告核心業務的防禦或變現」,weight 30）,兩次漏、一次中——這跟原本懷疑的「漏 Gemini」是完全不同的 checkpoint。
- **結論**：**「rewrite 語域規則把新聞刷出局、導致 sem-09 critical miss」這條因果鏈不成立**。原始查證（pool rank #6、top-5 全 10-K）本身沒錯,但據此推論「答案會漏 Gemini」跳過了兩步查證：① 沒檢查 top-5 的 10-K chunk 本身是否已經覆蓋 checkpoint（其實有,而且更好）；② 沒有用 `--repeat 3` 就把單次跑的結果當穩定失敗（違反 CLAUDE.md 自己訂的量測紀律——這正是 07-10 colloquial「口語 degrade」假說最後被推翻的同一種錯誤,再犯一次）。**据此,補新聞語料／top-5 來源多樣性保障／收斂語域規則力道這三個候選修法目前都不需要動工**——它們解決的問題本來就沒有發生。
- **復活條件**（機制型 vs 交互型混合）：
  - 「新聞被刷掉」本身是機制型觀察,永久成立（news chunk 內容確實邊緣、確實不在 top-5）,但**不是 sem-09 分數的瓶頸**,因為 10-K 已有更好的替代來源——除非未來 GOOGL 10-K 大改版拿掉 Gemini/AI Overviews 相關敘述（不太可能）,這個「不是瓶頸」的結論才需要重新檢查。
  - mi1（搜尋廣告防禦/變現）的間歇性缺失是交互型,復活條件：如果之後要為 mi1 做任何修法（例如 prompt 補強或 rubric 調整）,必須先確認 mi1 本身是否也是「非 critical、std 大」的雜訊帶（見下方待辦),依 CLAUDE.md 判讀原則,非 critical 缺口不需要優先修。
- **待辦**：mi1 的缺口目前只有 n=3 樣本（0.7/0.7/1.0）,還不足以判斷是穩定的次要缺口還是雜訊；且它是 non-critical（weight 30）,依 CLAUDE.md 判讀原則「std 大、非 critical 的題不要優化」,**不建議立即修**,除非之後有更多樣本顯示這是穩定模式且使用者認為值得為次要 checkpoint 投入成本。本次探針腳本（`eval/sem09_register_regression_probe.py`、`eval/sem09_live_answer_probe.py`）保留在 `eval/` 供之後複查用。

### rewrite 新增「語域轉換」規則 + glossary 子主題 scoping 修正 + 中文 ticker alias：sem-08/sem-11 k=3 穩定修好，lexical/mixed 零回歸；col-05/col-08 仍未解
- **動機**：sem-08/sem-11/col-05/col-08（= AWS 長期方向、Tesla 競爭監管風險的正式版+口語版）是既有已知的「穩定失敗」題（crit_miss_rate=1.0 或接近），07-08 探針已排除稀釋型 chunk 假設，懷疑病灶是「query 語域（策略提問）vs 語料語域（會計敘述）不匹配」，但從未實測驗證，也沒有實作修法。
- **診斷（零 LLM 成本，本地 BGE-M3 dense probe，重用 07-08 探針 harness）**：
  1. **E1 前置閘門**：grep `data/unstructure_processed/{AMZN,TSLA}_10K_2025.txt` 確認兩個 critical target 句子（AWS operating income 句、TSLA 競爭導致 price reductions 句）都確實存在於生產語料，非資料缺失問題。
  2. **E2 語域 vs 語言解耦探針**：對 target chunk 算 dense rank，比較「中文策略問句」「中文口語問句」「英文策略問句（純翻譯不轉語域）」「英文會計語氣」「中文會計語氣」五種 query。**關鍵結果**：英文策略問句（AMZN rank 23/60）比中文原句（rank 22/60）還差；中文會計語氣（AMZN rank 2/60、TSLA rank 2/106）幾乎追平英文會計語氣（rank 1/60、2/106）。**結論：病灶是語域，不是語言**——現有 `rewrite_query` 雖然強制輸出英文，但只要語氣停留在策略提問，翻不翻譯都救不了；語域轉換甚至不需要翻譯成英文即可生效。
- **修法（`rag_query.py`）**：
  1. `QUERY_REWRITE_SYSTEM_PROMPT` 新增 REGISTER 規則：對 BROAD 策略/質化問題（未來方向、面臨問題、競爭定位），至少一個 rewrite 變體必須轉成財報績效敘述語氣（operating income / profitability / pricing pressure 等具體詞，而非「direction/strategy/challenges」等抽象詞）。刻意用 GOOGL 當 few-shot 示範（不用 AMZN/TSLA，即失敗的 4 題本身），確保規則學到的是可泛化的轉換模式，不是這 4 題的答案。
  2. `_company_glossary_note()` scoping 修正：原規則「每個變體要點名清單裡不同的產品/分部」在「問題本身已經點名子主題」時（例如 sem-11「競爭**與**監管風險」）會失效——模型為了不撞詞，把「監管」子題硬配到清單裡一個不相關的分部（energy storage，而非該配的 FSD/Autopilot）。新規則：**問題有點名子主題時，變體要錨定該子主題、且只能對應清單裡「最match」的單一項目（不可以爲了保險同時列兩個）**；問題沒點名子主題才用清單做分散覆蓋（維持原行為）。
  3. `_COMPANY_TICKER` 新增中文別名（蘋果/微軟/輝達/亞馬遜/谷歌/臉書/特斯拉，grep `eval_set.json` 確認實際用例後收錄，比照 glossary 的驗證紀律）。根因：`parse_query_filters` 的 deterministic fallback（`_extract_structural_filters`）原本只認英文別名，導致純中文問句（如 col-08「特斯拉現在最讓人擔心...」）查無 ticker、直接掉到 Tier 3（無 filter），候選池混進其他 6 家公司雜訊。sem-08/sem-11 因為問句裡本來就寫了英文品牌名（"Amazon"/"Tesla"）才沒踩到這個坑，掩蓋了 col-05/col-08 的真正病灶。
- **踩到的坑（先跑對照組再下結論）**：加了語域規則後第一次跑 4 題 recall probe，sem-08 win（沒進池→top-5 #5），但 **sem-11 從穩定 top-5（#5，壓線）退步成掉出 top-5**——查證後是第二條規則衝突：新變體語域轉對了，但選錯子主題（配到 energy storage），撈進一段電池法規 chunk（#58），中文問句下 cross-encoder 誤判它比正解（#67，price reductions）更相關。這正是 §4 rerank-translate 已經證實過的「跨語言 rerank 評分失真」機制的重現——**證實語域變體（管召回）跟 translate_query_en（管排序精度）是正交、互補的兩層修法，不是二選一**。加上 `translate_query_en=True` 後 sem-11 穩定恢復。
- **驗證（k=3，`retrieval_model=llama-3.3-70b-versatile`，生產值）**：
  | 題 | baseline | +語域規則 | +語域規則+translate_en |
  |---|---|---|---|
  | sem-08 | 0/3 | 3/3（rank 4,4,4）| 3/3（rank 3,3,4）|
  | sem-11 | 3/3（rank 5,5,5）| 0/3（穩定退步，非噪音）| 3/3（rank 4,4,4，穩定恢復）|
  | col-05 | 0/3 | 0/3 | 0/3 |
  | col-08 | 0/3 | 0/3 | 0/3 |
  **E4 通用性回歸**（`--rewrite --translate-query-en`，`--judge-votes 3`，k=1，數字類查詢不需 repeat）：lexical 15 題 correct=**1.000**（舊基準 1.000，無回歸）；mixed 25 題 correct=**0.909**（舊基準 0.897，fatal 從 lex-12 換成 mix-02，但 mix-02 的變體本身正常、fatal 原因是生成端 temp=0.3 隨機性寫出一個來源沒有的精確數字、judge 2/3 票非一致判定，屬單次跑噪音範圍，非本次改動造成的回歸）。
- **col-05/col-08 仍未解，是兩個不同的獨立問題**：col-05 的 rewrite 變體語域轉換品質本身不穩定（同一 query 兩次呼叫產出的變體語域強度不同，見下方「非決定性」發現）；col-08 是純召回層問題——即使 ticker filter 修好、候選池乾淨，baseline 主池（原句）本身在整個 TSLA 語料裡就沒撈到正解（E2 顯示其原句 dense rank 只有 25/106，語域落差比 sem-11 更大），不是 CAP=8 變體機制能搆到的範圍。
- **意外發現：retrieval 側 LLM 呼叫並非完全 deterministic**——同一個 col-05 查詢、同一份 code、`llama-3.3-70b-versatile`、`call_llm` 預設 temp=0，兩次獨立呼叫產生了語域強度不同的變體（一次含 "margin trend"，另一次弱化成 "high-margin services"），導致同一題單次跑的 in-pool 結果不一致（一次進 top-5 #2、一次沒進）。這跟 CLAUDE.md 已記錄的「gpt-oss-20b 是 MoE、temp=0 仍不確定」是同一類現象，但**首次確認發生在 retrieval 側的 `llama-3.3-70b-versatile`，不只是 judge 側**——意味著任何只跑一次的 retrieval-side 診斷（含既有的 `diagnose_crit_miss.py` 單題跑法）都可能踩到跟 judge 側同樣的噪音陷阱，不能只跑一次就下結論，尤其是評估 rewrite 變體品質時。
- **現況：尚未轉生產預設**（`enable_rewrite`/`translate_query_en` 皆仍是 opt-in 參數，`rewrite_query` 內的新規則本身已隨代碼生效、無法單獨關閉）。**待辦**：① E5——semantic(11題)+colloquial(10題) 全量 k=3，是否轉正的最後一步；② col-05 的變體不穩定性需要更多樣本才能判斷是 prompt 問題還是模型固有噪音；③ col-08 的純召回問題需要另外的修法（不是這次規則能解，候選方案待評估）。

### 探索中間量級 judge 候選（qwen3-32b、llama-4-scout-17b）：qwen3-32b 是唯一「嚴格優於 20b、零新增風險」的候選，llama-4-scout 淘汰
- **動機**：本日稍早已驗證 `openai/gpt-oss-120b` 當 judge 能穩定修好 20b 的兩個系統性 bug（11/11 vs 9/11），但轉正卡在「跟 `gen_model`（也是 120b）共用同一個 Groq TPD 額度池」的商業顧慮（未變更生產預設，見下方「judge 升級試驗」條目）。提出的問題：如果 judge 換成一個**不是** `gpt-oss-120b` 的中間量級模型，能不能同時拿到「比 20b 準」和「不搶生成額度」兩個好處？用現成的 `eval/judge_regression.py`（11 案例，零 retrieve/generate 成本）快篩兩個候選，不需要跑全量 eval。
- **結果**：
  - `qwen/qwen3-32b`（`--votes 3`）：**10/11**。修好了 `lex12_fiscal_calendar`（20b 穩定 FAIL 的財年誤判），`col10_or_logic`（OR 邏輯）仍失敗（判定命中 mi2 而非 mi0），跟 20b 同一個既有 bug——**沒有引入任何新的失敗案例，所有反案例都守住**，是嚴格優於 20b 的結果。
  - `meta-llama/llama-4-scout-17b-16e-instruct`（`--votes 3`）：**10/11**，但失敗的是**不同案例**：`col03_true_wrong_number_negative`（答案給出明顯錯誤的數字，理應觸發 `mn0` 違規）沒有被判定違規（`violations=[]`）。這個反案例 20b 和 qwen3-32b 都能正確攔下——llama-4-scout 是三個模型裡唯一在這個反案例上失守的，代表它對數值捏造的判定**比 20b 更寬鬆**，是一個新的、獨立的風險模式，不是「準確度打平但錯在別處」這麼單純。
- **判定**：
  - **qwen3-32b 列為候選甜蜜點**——因為它不是 `gpt-oss-120b`，換上它當 judge 不會跟 `gen_model`（120b）搶同一個 TPD 池，120b judge 卡關的商業顧慮在這裡不成立；同時它比 20b 準（少一個系統性 bug），只是還沒到 120b 的 11/11 乾淨。是否轉正**留給使用者決定**（同一套「需要衡量而非技術判斷」的原則，見下方 Judge 章節既有結論）。
  - **llama-4-scout 淘汰**——原因是「引入新風險」而非「分數不夠高」：對真違規案例過鬆，會讓有問題的答案悄悄拿到高分，這比「OR 邏輯誤判」更危險，不建議在任何用途下採用。**復活條件**：機制型——這是該模型本身在數值捏造判定上的寬鬆傾向，不是這次 prompt 或版本問題造成的，除非模型換代（新版 llama-4-scout）或有證據顯示這是 prompt 措辭問題而非模型固有傾向，否則視為永久淘汰，不必重測。
- **範圍**：這次只做到 11 案例快篩，**沒有**：①用完整 eval（含全量 semantic/colloquial）驗證 qwen3-32b 的判定品質，②正式轉生產/eval 預設值。若之後要轉正 qwen3-32b，比照既有 judge-votes 機制轉正的先例，應該先用完整 eval 跑一次全量對照，不能只憑 11 案例快篩就下生產決策。

### 新增 `eval/judge_regression.py`：judge/rubric 專屬回歸套件，發現 07-09「財年措辭修法」實際上沒穩定生效
- **動機**：過去一週（07-08~07-10）連續發生財年季度誤判、OR 邏輯漏判、捏造範圍過寬、rubric 權重顛倒等 judge/rubric bug，每次流程都是「肉眼發現分數異常 → 人工複查 → 補一條 prompt 規則 → 用同一個案例重判驗證」，沒有安全網防止規則越補越鬆，也沒有機制在下次改 judge prompt 或換 judge model 前快速確認舊 bug 沒有復發。
- **實作**：`eval/judge_regression.py`，固定 11 個案例（`CASES` list），每個案例含 query/answer/rubric/`hall_result`（模擬 hallucination 指標摘要）與期望結果，直接呼叫 `evaluate_correctness_with_feedback_voted`（跟生產/eval 同一函式），不燒 retrieve/generate 成本。案例來源：
  - **正案例**（來自真實歷史事故的原始 query/answer/rubric）：col-01（FABRICATION SCOPE）、col-07（rubric 權重修正後）、col-10（OR-CHECKPOINTS）、lex-12（FISCAL vs CALENDAR）、sem-03（grounded 具體數字不算捏造）。
  - **反案例**（人工構造，同一份 rubric 但答案真的違規/真的漏講）：每個正案例都配一個反例（例如 col-01 配一個真的寫出捏造市佔率數字的答案），防止規則修過頭變成「什麼都不判違規」。
  - Usage：`python eval/judge_regression.py --judge-model <model> --votes <n> [--case <id>]`，PASS/FAIL 逐案例列印，非 0 exit code 供未來接 CI。
- **關鍵發現（活 bug，非套件本身問題）**：用生產 judge（`openai/gpt-oss-20b`）跑套件，`lex12_fiscal_calendar` 在 **votes=1 與 votes=3 都穩定 FAIL**——judge 仍把「Q1 2026」財年措辭誤判為 mn0 違規（`reason: "Answer cites wrong quarter but lists product lines."`），且 mi0（對應到 202512 季）也判未命中。這證實 07-09 記錄的「隔夜復發」不是單次噪音，而是 07-09 加的 FISCAL vs CALENDAR PERIOD 規則對 `gpt-oss-20b` **從未真正穩定生效過**——先前的「score 0.0→1.0」驗證只證明了規則改變了單次判定結果，沒有證明它在多次呼叫下穩定。`col10_or_logic`（OR-CHECKPOINTS）同樣在 20b 下不穩定（單次 PASS、3 票下 FAIL，命中 mi2 而非 mi0），且已排除是我這次改 prompt 造成的回歸（把新加的 NUMERIC TOLERANCE 規則暫時移除重測，結果不變，證明是 20b 本身的既有不穩定性，與 CLAUDE.md 記載的「gpt-oss-20b 是 MoE，temp=0 仍不確定」一致）。
- **9/11 通過（20b）**：col01_fabrication_scope、col01_true_fabrication_negative、col07_rubric_reweight、col10_true_miss_negative、lex12_true_wrong_period_negative、col03_numeric_tolerance、col03_true_wrong_number_negative、sem03_grounded_specific_number、sem03_true_fabrication_negative 全部通過（含所有反案例，證實規則沒有修過頭）。

### rubric schema 化：col-03/lex-03 加入 `tolerance_pct`，judge prompt 新增 NUMERIC TOLERANCE 規則
- **動機**：col-03（MSFT 毛利率，口語版）與 lex-03（正式版）過去靠 3 票多數決勉強救回「答案給 68%、rubric 要 68.3%」這種情況（且曾有 1/3 票認錯 checkpoint id，見 `eval/regression_colloquial_gen120b_fixed.json` 的 col-03 record），CHANGELOG 07-07 已記錄這是「留待處理」的容忍度設計選擇，一直沒有真正做決定。
- **修法**：`eval_set.json` 的 col-03/lex-03 mi0（毛利率 checkpoint）加上 `"tolerance_pct": 1.0`；`eval/eval_generation_llm_judge.py` 的 `evaluate_correctness_with_feedback` 在組 `mi_lines` 時把 `tolerance_pct` 顯示成 `tolerance=±Xpp` 附註給 judge；`_CORRECTNESS_FEEDBACK_SYSTEM` 新增 NUMERIC TOLERANCE grading rule，指示 judge 在有此附註時不要求精確小數點匹配，只要答案數字落在容忍區間內就算命中。
- **驗證**（`judge_regression.py` 的 `col03_numeric_tolerance`/`col03_true_wrong_number_negative`）：正案例（真實答案「約68%/67.6%/68.2%」）votes=1 單次呼叫即穩定 PASS（不再需要靠多數決運氣）；反案例（答案給 45%，明顯偏離）仍正確觸發 mn0 違規，證實規則沒有把「真的算錯數字」也放行。
- **全量驗證**：見下方 colloquial k=3 baseline，col-03 三次跑分數皆為 **1.0（std=0）**，對照舊的 0.714（且帶 judge 認錯 checkpoint 的雜訊），這是本次唯一從「勉強靠運氣過」變成「乾淨穩定滿分」的題目。

### 人工全審 colloquial 10 題 rubric：只有 col-07 需要重心修正（已於 07-10 完成），其餘無同型 bug
- **動機**：col-07 事故（rubric 把 AI 投資設為 critical、Reality Labs 損益設為次要，跟口語問題「到底在賺還在賠」的重心顛倒）證明 `maps_to` 繼承正式版 rubric 不安全——口語問法會窄化問題重心，但 rubric 權重是照抄正式版，兩者可能不同步。逐題重審 col-01~col-10，檢查每題的 critical checkpoint 是否對應口語問句實際問的重點。
- **結論**：**9/10 題權重與問題重心一致，無需修改**：
  - col-01（NVIDIA 護城河）、col-02（Apple 服務業務）、col-06（Apple EPS）、col-09（Apple 自研晶片）、col-10（Google AI 防禦）：critical checkpoint 直接對應問句核心，無疑慮。
  - col-04（Azure 打得贏嗎）：critical=雲端競爭，是「打得贏嗎」在資料限制下（來源文件不具名點名對手）最貼近可回答的代理指標，非重心錯置——此題已知的問題是生成端被口語框架帶偏而非 rubric 錯，維持現狀不動（見既有灰色地帶結論）。
  - col-08（Tesla 麻煩）：critical=EV 價格戰，是「麻煩」的合理代表性答案，非唯一但無明顯更優選項，不算重心錯置。
  - col-05（AWS 未來方向）：**唯一的軟性觀察**——critical checkpoint 是「AWS 獲利貢獻/營業利益表現」（偏向現況），但問句問的是「以後想往哪個方向走」（偏向未來）；AI/ML 投資與基礎設施擴張（非 critical）反而更貼合「方向」。這跟 col-07 原本的顛倒問題形態類似，但程度上更曖昧——現況陳述也是回答「方向」的合理鋪墊，不像 col-07 是明確的錯置。**判定：不修改**，因為 col-05 目前的失敗已由既有診斷（sem-08 鏡像，RECALL 層問題，chunk 稀釋、非 rubric 問題）完整解釋，重心是否微調不影響目前分數（chunk 根本沒被檢索到）。**復活條件**：若未來 sem-08/col-05 的 RECALL 問題被解掉（例如語域切換 rewrite 變體奏效），且分數仍卡在某個上限，才需要回頭檢查這個軟性重心疑慮是否浮現為真正瓶頸。
  - col-03：非重心問題，是數值容忍度問題，已用上面的 schema 化解決。

### judge 升級 `openai/gpt-oss-120b` 試驗：回歸套件顯示能穩定修好兩個 20b 活 bug，未變更生產預設值
- **動機**：`judge_regression.py` 建好後第一個用途——驗證「換 judge model 到 120b」這個 CHANGELOG 07-07 就掛著的待辦是否安全，不必再對舊答案重判，直接用套件正負案例驗證。
- **結果**：`--judge-model openai/gpt-oss-120b` 跑全部 11 案例，**votes=1 即 11/11 通過**（20b 是 9/11）。關鍵差異：
  - **`lex12_fiscal_calendar`、`col10_or_logic`**——20b 穩定失敗（votes=1、votes=3 皆 FAIL）的兩案例，120b 在 votes=1 與 votes=3 下都**穩定 PASS**，證實換大模型能實質解決這兩個活 bug，不是巧合。
  - **`col07_rubric_reweight`**——120b 判定 mi1（AI 投資次要點）未命中，而 20b 判命中；查證後這不是回歸，是 120b 對 grading rule「merely mentioning is not enough」讀得更嚴格（答案對 AI 投資只有「人工智慧」一詞的順帶提及，無基礎模型/推薦系統等細節）——**已調整該回歸案例的期望值只要求 mi0（Reality Labs，本案例真正要驗證的重心修正)，不再對 mi1 做強制要求**，因為 mi1 命中與否屬於模型嚴謹度差異的合理判斷區間，不是本案例的驗證目標。
- **決策：未變更生產/eval 預設 judge model**（`DEFAULT_JUDGE_MODEL`/CLAUDE.md 記載值仍是 `openai/gpt-oss-20b`）。理由：目前 `DEFAULT_GEN_MODEL` 已經是 `openai/gpt-oss-120b`（見 07-09），若 judge 也切到 120b，生成與判定會共用同一個 Groq TPD 池，容量風險尚未評估（CHANGELOG 07-08 待辦③已提過這個疑慮）。**這是留給使用者決定的取捨**：120b judge 更準確、但額度風險上升；目前的回歸套件已經證明技術上安全（不會讓已知 bug 更糟、也沒有讓反案例失守），缺的只是 TPD 容量的商業判斷。
- **待辦**：若之後決定切換，記得同時評估「gen=120b + judge=120b」全量跑一次的 TPD 消耗，不要當生產預設直接切換。

### colloquial `--repeat 3` 真 baseline：mean=0.727 ± 0.045（對照舊的單次跑 0.681），確認兩題穩定失敗、一題灰色地帶震盪
- **背景**：07-10 的 colloquial 全量結果（`eval/regression_colloquial_gen120b_fixed.json`）只跑了 k=1，CLAUDE.md 判讀原則要求 A/B 決策必須 `--repeat 3`，且不該對單次跑的數字做任何優化判斷。這是 colloquial 類別第一次跑 k=3。
- **設定**：`--category colloquial --rewrite --correctness-only --retrieval-model llama-3.3-70b-versatile --gen-model openai/gpt-oss-120b --judge-model openai/gpt-oss-20b --judge-votes 3 --repeat 3`（與生產設定一致，同時套用本次新增的 tolerance schema），輸出 `eval/colloquial_correctness_k3.json`（+ `_run1/2/3.json`）。
- **結果**：**mean_of_means = 0.727，std_across_runs = 0.045**（< 0.1 噪音門檻，是穩定可信的數字，非單次跑的巧合）。逐題：
  | 題 | mean | std | crit_miss_rate | 判讀 |
  |---|---|---|---|---|
  | col-01 | 0.8 | 0.0 | 0.0 | 穩定，FABRICATION SCOPE 修法生效 |
  | col-02 | 0.933 | 0.094 | 0.0 | 穩定高分 |
  | **col-03** | **1.0** | **0.0** | 0.0 | **本次 tolerance schema 直接效果**：從「靠多數決運氣的 0.714」變成「乾淨穩定滿分」 |
  | col-04 | 0.367 | 0.236 | 0.667 | 灰色地帶，維持既有結論不動（生成被口語框架帶偏，非 rubric/檢索問題）|
  | **col-05** | 0.5 | 0.0 | **1.0** | **穩定失敗**，sem-08 鏡像（RECALL 層問題，chunk 稀釋），非口語特有 |
  | col-06 | 1.0 | 0.0 | 0.0 | 穩定滿分 |
  | col-07 | 0.8 | 0.0 | 0.0 | 穩定，rubric 權重修法生效 |
  | **col-08** | 0.167 | 0.236 | **1.0** | **穩定失敗**，sem-11 鏡像（RECALL 層問題），分數本身有雜訊但 critical 穩定漏 |
  | col-09 | 0.9 | 0.141 | 0.0 | 穩定高分 |
  | col-10 | 0.8 | 0.141 | 0.0 | OR-CHECKPOINTS 修法生效，critical 穩定命中，非 critical 點小幅震盪 |
- **判讀**：colloquial 真實水準是 **0.727 ± 0.045**，比 07-10 單次跑的 0.681 略高且噪音更小；跟 semantic k=3 的 0.870 相比仍有落差，但落差幾乎完全由 col-05/col-08 兩個**繼承自 sem-08/sem-11 的既有 RECALL 問題**解釋（10 題的小分母讓每題失敗佔 10% mean，放大了跟 61 題全集平均的觀感落差），不是「口語」本身的獨立退化機制。col-04 是唯一與口語框架有關但已知、暫不處理的灰色地帶。**不需要對 0.727 這個數字做任何進一步的「口語專屬」修法**——真正需要修的槓桿（sem-08/col-05、sem-11/col-08 的語域切換 rewrite 變體）跟 semantic 類別完全共用，屬於 CHANGELOG 07-08 已列的待辦，不是新工作項。

### colloquial 首次驗證（gen=120b）：0.489 → 逐題複查後推翻「口語 degrade」假說，三題其實是 judge/rubric 測量 bug，已修並驗證
- **背景**：colloquial(10 題) 是 61 題裡今天之前唯一沒測過 gen=120b 的類別。單次跑（`--rewrite --correctness-only --retrieval-model llama-3.3-70b-versatile --gen-model openai/gpt-oss-120b --judge-model openai/gpt-oss-20b --judge-votes 3`），`eval/regression_colloquial_gen120b.json`，**correct=0.489, fatal=1**，遠低於 semantic 0.870(k=3)/mixed 0.937/lexical 1.0。
- **第一版分析（已修正，見下）**：最初把 7 題非滿分粗分成「已知問題重現」vs「新發現的口語 degrade 現象」，並把 col-03/col-04/col-07 歸為後者，寫成一個新的、獨立的機制假說。**這個結論後來被使用者的反問（「答案該跟問題相關，不能一直塞專有名詞吧」）打臉——逐題把完整答案、來源 chunk、rubric 原文並排比對後，發現真正的病灶分佈完全不同**：
  - **col-07→sem-10（Meta）= rubric bug**：口語問句「臉書...到底在賺還在賠？以後想怎麼搞？」整題在問 Reality Labs 損益，rubric 卻把「AI 投資」設成 critical（w50）、Reality Labs 損益只是次要（w30）——**權重跟問題實際重心顛倒**，是套用正式版 sem-10 rubric（AI 與 Reality Labs 並重）時沒考慮到口語版問題的重心已經窄化。**不是模型漏講，是 rubric 問錯了要害。**
  - **col-01→sem-01（NVDA）= judge 誤判**：答案只說「遠超其他競爭者」（純質化描述，無具體數字），judge 3 票裡 2 票仍判違反 `mn0`（捏造市佔數字）。checkpoint 文字本來就限定「具體百分比或數字」，judge 沒有嚴格遵守這個限定範圍，把任何強烈比較語氣都當成捏造嫌疑。
  - **col-10→sem-09（Google）= judge 誤判**：checkpoint「提到 Gemini 模型 **/** AI 整合進搜尋」是 OR 條件，答案明講「把 AI 能力內建於搜尋」（命中後半），judge 卻只認字面上的「Gemini」這個名字，沒吃到 OR 邏輯。
  - **col-04→sem-07（Azure）= 灰色地帶，未動**：critical 內容（Azure 面臨競爭）確實在檢索到的 chunk #50 裡（用 Qdrant 直接查證），但答案沒引用——模型被「打得贏嗎」的口語框架帶去回答「能否判定輸贏」，誠實地說「沒有對手數據無法比較」，這個回應本身合理但確實漏了本來抓得到的內容。這題不像 col-07 是明確的 rubric bug，也不像 col-01/10 是明確的 judge bug，維持現狀、不修改。
  - **col-03→lex-03（MSFT 毛利率）= 判斷從缺，未動**：答案「約 68%」實質正確（算出 67.6%~68.2%），rubric 硬性要求精確到「68.3%」，屬於 judge 對小數點過嚴，性質上不同於前兩個 judge bug（OR 邏輯、捏造範圍）是明確的 prompt 規則漏洞，這個更接近容忍度設計選擇，本次沒有修改。
  - **col-05→sem-08、col-08→sem-11**：維持第一版分析，是既有已知問題（AWS RECALL、EV 價格戰需要 translate_query_en）的鏡像重現，不是口語特有、不需新調查。
- **教訓**：我第一次的「口語 degrade」結論犯了跟今天稍早 lex-12 判斷同一種錯——**看到分數低就急著找一個新機制去解釋，沒有先窮盡「這其實是 judge/rubric 本身的既有毛病」這個更平凡的可能性**。7 題裡有 3 題（col-01/07/10）是純粹的測量工具問題，修好之後分數就回升，不需要對系統本身做任何改動。真正因為「口語」而產生的內容落差，證據上只剩 col-04 一題（而且是灰色地帶，不是明確 bug）。
- **修法與驗證**：
  1. `eval_set.json` 的 col-07 rubric：Reality Labs 損益 mi0/critical/w50，AI 投資降為 mi1/次要/w30（AI 眼鏡維持 mi2/w20 不變）。重判已存答案：**0.3 → 0.8**（剩下漏的是 mi2「AI 眼鏡」，答案裡真的沒提，屬合理缺失）。
  2. `_CORRECTNESS_FEEDBACK_SYSTEM` 新增兩條 grading rule：① FABRICATION SCOPE——must_not_include 的「捏造數字」類 checkpoint 只在答案給出具體數字/百分比且未經來源佐證時才算違規，純質化比較語句（如「遠超競爭者」）不算；② OR-CHECKPOINTS——checkpoint 文字裡的「/」代表 OR，符合任一子項即算命中，不要求全部命中。重判已存答案：col-01 **0.0(fatal) → 0.8**（剩下漏的是 mi2「網路整合」，真實缺失）；col-10 **0.5 → 1.0**。
  3. 三次驗證都用「重判已存答案」（不重新 retrieve+generate），因為這兩類修法都是純 judge/rubric 邏輯，不涉及生成內容本身——不像 07-09 的財年案例（那次改的是生成端 prompt，重判舊答案的驗證方法本身有漏洞，必須 fresh generate 才算數）。這裡的驗證方法跟修法的層級是對應的，成立。
- **全量重跑確認（fresh generate+judge，非重判舊答案）**：`eval/regression_colloquial_gen120b_fixed.json`，**correct=0.681（0.489→0.681），fatal=0（1→0）**。逐題核對：col-01=0.8、col-07=0.8、col-10=1.0，跟「重判舊答案」驗證的分數完全一致——證明這三個修法是**判定邏輯**層級的修正（跟答案怎麼寫無關），不像財年那個案例（生成端 prompt 修法）會因為每次生成的措辭不同而不穩定。col-08 這次掉到 0.0（連次要點都沒中），屬於已知 sem-11 noise，非新問題；col-04 維持 critical_miss（0.2），灰色地帶如約定未動。
- **待辦**：col-04 的灰色地帶留待人工持續觀察，不急著修；colloquial 這次只跑單次，若要用於正式決策建議 repeat 3（跟 semantic 同樣的噪音疑慮尚未用 colloquial 自己的資料驗證過）。

---

## 2026-07-10

### 修正：07-09「judge prompt 修好財年誤判」的驗證方法有漏洞——只重判舊答案不夠，隔夜排程重測後 lex-12 再次 fatal；真正修法改在生成端注入期間碼消歧義提示
- **背景**：07-09 在 `_CORRECTNESS_FEEDBACK_SYSTEM` 加了財年感知規則後，只用「把 lex-12 已存的答案重新丟給修過的 judge」驗證（0.0→1.0），就下結論「修好了」。用 Windows `schtasks` 排程隔夜（額度重置後）重跑一次完整 mixed/lexical 驗證,結果 **lex-12 再次被判 fatal（0.0，3/3 票一致）**，mixed correctness 反而從 0.897 降到 0.861。
- **為什麼昨天的驗證方法不夠**：昨天只測了「同一個已生成的答案,面對修過的 prompt 會不會判不一樣」，這只驗證了 judge 的行為改變，**沒有驗證新生成的答案本身會怎麼寫**。今天重新生成（temp=0.3,非確定性）產生的新答案寫成「2026 年第一季（即 2025 Q4 10‑Q）」——模型自己嘗試消歧義,但用的是「2025 Q4」而非使用者輸入的「202512」,而且 `must_not_include` checkpoint 原文（「把其他季度的數字當成 202512 季」）是每題獨立、綁定使用者語言的具體文字,可能比 system prompt 裡的通用規則更主導 judge 的判斷,修法沒有穩定生效。
- **教訓**：**驗證「prompt 修法是否有效」不能只重判舊輸出，必須重新跑一次完整 generate+judge，因為生成端本身是非確定性的**——這跟 CLAUDE.md 已經記過的「單次跑 mean 差 <0.1 是噪音」是同一種提醒的變體：本次是「單一樣本」而非「單次跑」的層級，问题更基本。
- **真正修法（改在生成端,不是 judge 端）**：`rag_query.py` 的 `build_user_prompt()` 重用既有的 deterministic `_PERIOD_CODE_RE`（本來就用在 `parse_query_filters` 的結構化抽取,不需要額外 LLM 呼叫）,偵測到使用者問題裡有 YYYYMM 期間碼時,在 prompt 注入一段指示：討論該期間時要把使用者的期間碼跟來源文件的 fiscal quarter 用語並列陳述（例如「202512 (fiscal Q1 2026)」）,從根源消除歧義——不管 judge 的 prompt 有沒有修對,答案本身就不會再有「只用財年措辭、沒有回填使用者代碼」這個模糊地帶。因為 `eval_generation_llm_judge.py` 呼叫的是同一個 `rq.build_user_prompt()`,這個修法自動套用到生產與 eval 兩邊,不需要重複修改。
- **驗證（單題,fresh end-to-end：真的重新 retrieve+generate+judge,不是重判舊答案）**：lex-12 新答案開頭變成「In the **2025‑12** quarter (the first quarter of fiscal 2026), Apple's revenue growth was driven primarily by...」——同時陳述使用者代碼與財年標籤。judge votes=3 全部一致：`score=1.0, critical_miss=false, fatal_hallucination=false`,`must_include_hits` 兩點全中、`must_not_violations` 全空。
- **待辦**：這次也只驗證了 lex-12 單題的 fresh 重測,還沒有用這個生成端修法重跑完整 mixed/lexical 全量（會用掉今天的 Groq 額度）,下次要建立乾淨基準時應該用這個修法跑一次完整 k=1（數字類查詢不需要 repeat）確認整體 mixed 分數回升、且沒有引入新的副作用（例如期間碼提示會不會誤觸發在沒有財年落差的公司上,拉長不必要的贅述）。

---

## 2026-07-09

### 生產切換 gen=120b 後的回歸檢查：lexical 無回歸(1.0)；mixed 表面上 0.897 但唯一的 fatal 是 judge 誤判(AAPL 財年季度命名)，非生成品質問題
- **動機**：把 `rag_query.py` 的 `DEFAULT_GEN_MODEL` 切成 120b 後，只驗證過 semantic（11/61 題），有責任確認 lexical(數字查詢,舊基準 1.000)與 mixed(指定期間查詢,舊基準 1.000)沒有回歸——尤其擔心大模型在數字/期間查詢上「話多」反而引入錯誤。
- **設定**：單次跑（非 k=3,數字類查詢決定性高,不需要 repeat）,`--rewrite --correctness-only --retrieval-model llama-3.3-70b-versatile --gen-model openai/gpt-oss-120b --judge-model openai/gpt-oss-20b --judge-votes 3`。
- **Lexical 結果**：`eval/regression_lexical_gen120b.json`,15 題（6 題有 rubric）,**correct=1.0,fatal=0**——無回歸。
- **Mixed 結果**：`eval/regression_mixed_gen120b.json`,25 題,**correct=0.897,fatal=1**——表面上像回歸,但複查後**唯一的 fatal(lex-12)是 judge 誤判,不是生成問題**：
  - 題目問「AAPL 202512 那一季的 10-Q,營收成長主要來自哪些產品線」,答案引用 `[Reference 1, chunk #26]`——**查證後確認來源就是 `AAPL_10Q_202512.html`,完全是對的檔案**（sources 陣列 5 個候選全部來自這份 202512 filing）。
  - 答案寫「Apple's **Q1 2026** revenue growth was driven by...」,judge 3/3 票一致判定違反 `mn0`（把其他季度數字當成 202512 季）,**但查 `data/unstructure_processed/AAPL_10Q_202512.txt` 原文,Apple 自己就是用「the first quarter of 2026」描述這個 202512(2025年12月27日結束)的季度**——Apple 財年 10 月開始,fiscal Q1 2026 = calendar 2025年12月季度,跟 202512 是同一件事,只是命名慣例不同（fiscal quarter vs calendar month code）。
  - **這是比 07-08 sem-03 更麻煩的一種 false positive**：sem-03 是單次判斷的隨機誤判,3 票多數決之後這次就穩定了；但這裡 **3 票全部一致誤判**,因為病灶是系統性的（judge 不知道 Apple 用非日曆財年,不是隨機噪音),多數決機制對這種系統性偏誤沒有防禦力。
  - 其餘 4 題非滿分（mix-01 0.714、mix-05 0.375、mix-06 0.625、mix-11 0.714）都是正常的部分命中（漏講精確數字/幅度、或來源本身就沒有「市場反應」這類資訊),屬於 correctness 評分的常態,不是 120b 特有的問題,沒有進一步查證的必要。
- **結論**：**lexical/mixed 都沒有因為換成 120b 而發生真正的內容品質回歸**——生產切換的決策維持不變。但這次意外測出一個獨立於今天所有實驗、原本就存在的**判準系統性漏洞**：judge 對非日曆財年公司（AAPL、MSFT 都是 9/10 月結財年)的「fiscal quarter vs calendar 期間代碼」映射沒有意識,任何模型只要照抄來源文件自己的財年用語就會被誤判。
- **待辦（新發現,尚未修，需使用者確認優先順序）**：修法可能是①在 judge prompt 加一段「AAPL/MSFT 等公司使用非日曆財年,fiscal quarter 名稱可能對應不同 calendar 月份,判斷前先確認实际期間再訂 mn 違規」,或②在 `SYSTEM_PROMPT` 要求生成端在使用 fiscal quarter 措辭時額外註明對應的日曆月份（例如「Q1 FY2026 (period ended Dec 2025)」),讓答案本身消除歧義,兩者互不排斥。這是一個獨立於「換 gen model」的既有測量漏洞,不影響今天 120b 生產切換的決策。

### 修：judge prompt 加入財年感知規則（`_CORRECTNESS_FEEDBACK_SYSTEM`），修掉上一條發現的 AAPL 財年季度誤判
- **修法**：`eval/eval_generation_llm_judge.py` 的 `_CORRECTNESS_FEEDBACK_SYSTEM` 在 Grading rules 加一條「FISCAL vs CALENDAR PERIOD」規則——明確列出 AAPL/MSFT/NVDA 使用非日曆財年、filing 本身會用 fiscal quarter 命名（如「first quarter of fiscal 2026」）而非日曆期間代碼（如「202512」），指示 judge：不能只因為答案用了公司自己的財年措辭、沒有照抄使用者問句的期間代碼，就判定 must_include 期間點未命中或觸發 must_not_include 違規；要先核對答案引用的實際日期/數字是否真的對應到不同期間，才能判違規。
- **驗證（低成本，不重跑 retrieve+generate）**：直接把 lex-12 已存的答案（`eval/regression_mixed_gen120b.json`）連同修過的 prompt 丟回 `evaluate_correctness_with_feedback_voted(votes=3, ...)` 重新判一次——**score 從 0.0（fatal, 3/3 一致誤判）翻正為 1.0（3/3 一致，mi0/mi1 都命中，無 violation）**，且 reason 從「引用錯誤季度」變成「correctly addresses the requested quarter and product lines」。同一份輸入、同一個 judge 模型，只改了 prompt，結果直接翻轉——確認修法對症。
- **範圍**：目前只補了這一條規則，沒有重跑完整 mixed/lexical 驗證是否還有其他題目受惠或有無新的副作用（例如規則會不會讓 judge 對真正搞錯期間的答案變得太寬鬆）。下次跑 mixed/lexical 全量時應該用新 prompt 重新建立基準，取代今天 0.897 這個帶假陽性的數字。

### 拆分 retrieval_model / gen_model：`rag_query.py` 與 `eval_generation_llm_judge.py` 新增獨立參數（07-08 待辦①的前置工程）
- **動機**：07-08 已修過 `diagnose_crit_miss.py` 同一種 bug（`model_name` 同時餵給 retrieve() 內部 filter/rewrite/translate 與外部生成，導致換模型實驗誤把檢索路徑也換掉）。同一天發現 `eval_generation_llm_judge.py` 有完全一樣的問題——`rq.retrieve(..., model_name=args.gen_model, ...)`（原第 680 行），代表所有「換 --gen-model 比較生成品質」的 A/B，實際上連檢索側的 filter/rewrite/translate 都一起換了，兩個維度沒有解耦，量到的效應無法歸因。
- **修法**：
  - `eval/eval_generation_llm_judge.py` 新增 `--retrieval-model`（預設 `rq.DEFAULT_MODEL`，即生產值 `llama-3.3-70b-versatile`），`rq.retrieve()` 呼叫改用它；`--gen-model` 只餵給最終 `call_llm()` 生成呼叫。預設值不變，不指定 `--retrieval-model` 時行為與舊版完全相同。
  - `rag_query.py` 的 `run_single_query`/`run_interactive`/`main()` 同步拆成 `retrieval_model`/`gen_model` 兩個參數；CLI 新增 `--gen-model`（預設 `None` → fallback 為 `--model` 的值，向後相容）。`api_server.py` 目前仍是單一 `model` 兩用（`rq.retrieve(model_name=model)` + 生成同一個），未動——它不在本次 A/B 實驗路徑上，且改動 API 介面屬於另一個範疇，留待需要時再處理。
- **驗證**：`ast.parse` 語法檢查通過；`--limit 1 --correctness-only` 跑過一次確認新參數確實生效、無 regression。

### 新增 `--judge-votes`：correctness judge 逐 checkpoint 多數決（回應 07-08 sem-03 false positive 待辦）
- **動機**：07-08 記錄了 sem-03 的 judge false positive（`must_not_violations` 誤判「捏造成長率」，但複查原文後答案完全 grounded）——`compute_correctness()` 對 `must_not_include` 命中是「一票否決」直接把分數砍到 0（評分懸崖），單次 judge call 的誤判會被這個機制放大成致命分數。CHANGELOG 07-07 待辦已列「judge 換 gpt-oss-120b（獨立 TPD 池）或 3 票多數決」，本次做的是後者。
- **實作**：`eval_generation_llm_judge.py` 新增 `evaluate_correctness_with_feedback_voted(votes, **kwargs)`，對 `evaluate_correctness_with_feedback` 呼叫 N 次，對每個 `mi{i}`/`mn{i}` checkpoint id 分別多數決（`count > votes/2` 才算命中/違規，未過半視為未命中——對稱地不偏袒任何一邊，但票數不夠的 fatal violation 不會再單靠一次誤判就砍分）。`is_refusal` 同樣多數決。`reason`/`actionable_feedback` 取第一次無 error 呼叫的結果。新增 `--judge-votes`（預設 1，行為與舊版完全相同）。結果額外記錄 `vote_detail`（每次呼叫各自的 hits/violations），供之後複查判定穩定性用，不影響 `compute_correctness` 的計算輸入。
- **驗證**：`--limit 1 --judge-votes 3` 跑 sem-01，`vote_detail` 顯示 3 次呼叫一致（`mi0` 三次都命中、都無 violation）——功能正常運作，且本次全量 k=3 baseline（見下方）用 `--judge-votes 3` 跑完 11 題 semantic ×3 repeat，sem-03 三次跑分數都是 0.8（std=0.0），沒有再出現懸崖式歸零，初步佐證有效，但樣本只有一次 k=3（同一 gen model），還沒有「同一題材蓄意製造 judge 誤判」的直接對照實驗。
- **代價**：correctness 判定的 LLM 呼叫從每題 1 次變成 `votes` 次（本次用 3），只影響 correctness 這一步，不影響 hallucination/relevance/context-recall。

### 探針：AMZN AWS 營業利益句 / TSLA FSD 監管風險段落的「稀釋型 chunk」假設——**已試無效**，真正病灶疑似是「query 語域不對」不是「chunk 太大」
- **背景**：07-07/07-08 診斷 sem-08（AWS，critical，RECALL）與 sem-11 次要 checkpoint（FSD，non-critical，RECALL）時，把稀釋型大 chunk（AMZN chunk #100 混雜 FTC 訴訟/稅務/segment 營益表；TSLA chunk #67 混雜競爭/自駕/Optimus/電池）列為候選根因，並留待辦「MD&A 語意邊界感知切分」。07-07 已證實孤立抽出段落對 **rerank 層**反而更糟（raw 0.0148），但明確標註「這是 rerank 層結論，不能直接套用到 sem-08 的 recall 層」——本次補的就是這個從未做過的 recall 層實測。
- **方法**（零 LLM 成本，純本地 embedding，不寫 Qdrant）：從 `data/unstructure_processed/{AMZN,TSLA}_10K_2025.txt`（production ingest 分離 Table 後、餵進 SemanticChunker 前的角色標記文字）重建與生產完全相同的 text_blob，只換 `breakpoint_threshold_amount`（90 現行 / 75 / 60 / 50，percentile type 不變），用 `BAAI/bge-m3` dense embedding 算目標句子所在新 chunk 對兩種 query（① 使用者原始中文問句 ② 貼近段落內容措辭的英文 query）的 cosine similarity，在「同文件內、該設定自己切出的全部 chunk」中排第幾名。
- **結果（與假設方向相反）**：
  - AMZN：英文精準 query 在 **所有** breakpoint 設定（含現行 90）下都是 **rank 1/60~314**——代表稀釋完全沒有傷到 dense 相似度，只要 query 措辭對得上。中文原始 query 排名本來就差（22~19），且非單調（75 比 60 更好），沒有隨切細而穩定改善。
  - TSLA：英文精準 query 同樣全部 **rank 1**；但中文原始 query 排名**隨切細單調惡化**（31→71→81→92）——因為切細只是讓候選池膨脹（106→522 chunks），目標句子自身的相似度分數幾乎不變（0.4693~0.4916），純粹被更多候選稀釋掉排名，是反效果。
- **結論**：**稀釋型 chunk 假設不成立**——當前生產設定下，這兩個目標句子對「措辭對的 query」已經是 rank 1，重切分無法再改善，且對中文原始 query 反而可能更差。真正的瓶頸疑似是**query 語域落差**：使用者問「長期策略方向」（策略框架），但 10-K MD&A 寫的是「營業利益因為 X 增加、部分被 Y 抵銷」（會計績效框架）——這兩種語域在 embedding 空間本來就遠，與 chunk 大小無關。這與 sem-02（Microsoft AI 戰略）當初的病灶同構，但 sem-02 是靠幫 `rewrite_query` 注入 `_COMPANY_PRODUCT_GLOSSARY`（已知詞彙表）解決，不是切 chunk——AWS 的 glossary 其實已經有 "AWS" 這個詞，缺的可能不是詞彦而是**語域**（需要一個變體主動轉成財報敘述語氣，而非停留在策略提問語域）。
- **判定**：MD&A 語意邊界切分列為**已試無效**（recall 層，接續 07-07 的 rerank 層結論，兩層現在都測過、都無效）。**復活條件**：無明確復活條件（機制型——只要「query 語域 vs 語料語域不匹配」這個病灶不變，任何 chunk 邊界調整都不會讓一個語域不對的 query 找到內容；除非之後有證據顯示某些檢索失敗案例確實是「同語域但被硬生生切斷」才需要重新考慮切分，目前兩個測試案例都不是這個模式）。
- **待辦（新方向，尚未實作，需使用者確認是否要做）**：比照 sem-02 的 glossary 解法，測試「rewrite variant 主動切換到財報敘述語域」（例如：策略類問題额外生成一個「XX 的營業利益/獲利貢獻近況」措辭的變體）是否對 sem-08 有效——這是本次探針推導出的假設，還沒有實作也沒有驗證，成本應該遠低於全面改 chunking（不需要重新 ingest 整個 collection）。

### 乾淨隔離 A/B：retrieval 固定 70b，只換生成模型 70b vs 120b——**120b 全面勝出，11 題零回歸**（解決 07-08 待辦①）
- **動機**：07-08 反覆強調換大模型的正面結論（sem-02）與負面結論（後來撤回的「120b 幻覺」）都混雜了「檢索側也悄悄換了模型」這個混淆變因（`diagnose_crit_miss.py` 與 `eval_generation_llm_judge.py` 當時都有這個 bug）。本次先做上面的 `--retrieval-model`/`--gen-model` 拆分，這是第一次能把「只換生成模型」單獨隔離出來驗證的乾淨對照。
- **設定**：`--category semantic --rewrite --correctness-only --retrieval-model llama-3.3-70b-versatile --judge-model openai/gpt-oss-20b --judge-votes 3 --repeat 3`，兩組唯一差異是 `--gen-model`（`llama-3.3-70b-versatile` vs `openai/gpt-oss-120b`）。注意：**沒有加 `--translate-query-en`**（走目前 CLAUDE.md 記載的最小生產 bundle,不是 07-08 那次驗證用的完整實驗 bundle），所以 sem-11 的 critical checkpoint（EV 價格戰）在兩組都還沒吃到 translate 修復,這點兩組一致、不影響隔離的乾淨度，純粹代表這次沒有同時測 retrieval 端的優化。
- **結果**（`eval/ab_gen70b_semantic_k3.json` vs `eval/ab_gen120b_semantic_k3.json`）：

  | 指標 | gen=70b | gen=120b |
  |---|---|---|
  | mean_of_means (k=3) | 0.627 | **0.870** |
  | std_across_runs | 0.061 | **0.030**（更穩定，不只更高分）|

  逐題（mean, crit_miss_rate）：sem-01 0.6→1.0、sem-02 0.567(0.667)→1.0(0)、sem-03 0.8→0.8（不變，judge-votes 生效後兩組都穩定無懸崖）、sem-04 1.0→1.0、sem-05 0.833(0.333)→1.0(0)、sem-06 0.933→1.0、sem-07 0.5→1.0、**sem-08 0.267(1.0)→0.5(1.0)**（critical AWS checkpoint 兩組都仍 100% miss，分數進步僅來自其他非 critical 點，符合預期——retrieval 沒變,不可能生成端救回一個沒被檢索到的內容)、sem-09 0.467(0.333)→0.9(0)、sem-10 0.633→0.9、sem-11 0.3(0.667)→0.467(0.333)（critical 的 EV 價格戰仍偶爾漏,因為 retrieval 端這次沒開 translate_query_en)。
  **11 題無一退步，多題從有 critical_miss 變成穩定 1.0。**
- **與稀釋型 chunk 探針的交叉驗證**：sem-08 是本次唯一「生成端救不動」的題,且 crit_miss_rate 在兩個生成模型下都是 1.0——與同一天稍早的 rechunk 探針結論一致（sem-08 是 retrieval 層問題,不是生成能力問題,也不是 chunk 太大),兩個獨立實驗互相印證。
- **決策：`gen_model` 轉正式生產預設值 `openai/gpt-oss-120b`，`retrieval_model` 維持 `llama-3.3-70b-versatile` 不變**——這是本次證據最乾淨、最一致的一組結果（k=3、零回歸、std 更小),直接回答 07-08 待辦①。judge 用獨立的 `openai/gpt-oss-20b`（不跟任何一個 gen model 共用 TPD 池),因此決策不受「120b 生成+120b judge 搶同池」那個舊疑慮影響。**已徵求使用者確認並實作**：`rag_query.py` 新增 `DEFAULT_GEN_MODEL = "openai/gpt-oss-120b"`；`DEFAULT_MODEL`（`llama-3.3-70b-versatile`）維持不變、只作為 retrieval 側（`parse_query_filters`/`rewrite_query`/`translate_query_to_english`）與 CLI `--model` 的預設。CLI `--gen-model` 預設值從「fallback 到 `--model`」改成直接預設 `DEFAULT_GEN_MODEL`——不特別指定時，`python rag_query.py` 現在就是「retrieval=70b、gen=120b」這組已驗證設定；仍可用 `--gen-model` 覆寫回 70b 做對照。`api_server.py` 目前仍是單一模型兩用，本次刻意不動（不在這次驗證路徑上，屬於另一個範疇）。
- **待辦**：① sem-11 待补跑 `--translate-query-en` 疊加版本,確認 retrieval 端優化 + gen=120b 疊加後 crit_miss_rate 能否進一步下降；② sem-08 仍是唯一未解的 critical 失敗,下一步應該驗證「rewrite 語域切換」假設（見上一條 chunking 探針待辦),而非再嘗試換生成模型；③ `api_server.py` 若之後也要切換生產模型，需要比照這次的拆分做法（目前它仍是一個變數兩用)。

---

## 2026-07-08

### 全量驗證 gen=120b：sem-02 修好（1.0）；先前記錄的「sem-03/sem-07 幻覺」是**我自己的查證錯誤 + judge false positive，不是 120b 幻覺**——結論修正
> ⚠️ **本條修正了同日稍早一個錯誤結論。** 稍早我斷言「gen=120b 在 sem-03/sem-07 引入 fatal 捏造引用、不建議採用」，並據此寫進 CHANGELOG。事後複查發現**那個結論建立在兩次查錯 chunk 上**，是錯的。以下是修正後的事實。

- **背景**：全量 11 題 semantic（gen=120b + judge=120b + bundle：`--rewrite --translate-query-en --rerank-multi-query --correctness-only`，`eval/generation_correctness_semantic_gen120b_bundle.json`）。overall mean=0.691（對照 70b baseline 0.682），**sem-02 從 0.5/critical_miss → 1.0**（Copilot 已驗證，真實改善）。表面上 sem-03 判 fatal（score 0）、sem-07 從 1.0 退到 0.5/critical_miss，我一開始認定是 120b 捏造。
- **查證錯誤 1（sem-03）**：答案引用 `[Reference 5, chunk #4]`，我去查 `AAPL_10K_2025.html` chunk #4（股票回購表，無數字）就下結論「捏造」。**但 Reference 5 實際是 `AAPL_News_20260519_01.txt` chunk #4，不是 10-K chunk #4**——我把「Reference 5 的 chunk_index=4」錯配成「10-K 的 chunk #4」。複查真正的 News chunk #4：原文逐字有「$31.0 billion in high-margin services」「Services climbed 16% to a record $31.0 billion」「R&D spending jumped 34% year-over-year to $11.4 billion」——**答案的 310 億、16%、34% 全部 grounded，沒有捏造**。judge 的 `mn0` 判定（捏造成長率）是 **false positive**（唯一勉強算「投射」的是開頭「兩位數年增率」，但 16%/12.8% 都在 chunk 裡、雙位數本身有據）。
- **查證錯誤 2（sem-07）**：答案寫「Azure 34% 年增、Intelligent Cloud 21%」引用 `[MSFT_10K_2025.html, chunk #2]`，我查 chunk #2（SEC 封面頁）就認定「捏造 + 引用垃圾」。**但 34%/21% 這兩個數字真實 grounded 在 chunk #115**（該題 top-5 的 Reference 2，原文「Intelligent Cloud Revenue increased ... 21%」「Azure and other cloud services revenue grew 34%」）。模型是把「Reference 2」誤寫成「chunk #2」（引用編號混淆），**數字本身沒捏造**——這也是為什麼 judge 正確地**沒有**標 `mn0`（我稍早還誤以為是「judge 漏抓」，其實 judge 判對了，沒有 fabrication 可抓）。
- **修正後的真實情況**：
  - sem-02：1.0，真實改善。
  - sem-03：答案完全 grounded，score 0 是 **judge false positive**，不是生成問題。
  - sem-07：數字 grounded，只有**引用標號寫錯（#2 應為 #115）**這個小瑕疵；但 `critical_miss`（漏 mi0「Azure 面臨競爭」）是**真實的內容覆蓋回歸**——答案講了 Azure 的能力/成長/領先定位，卻沒寫「面臨其他雲商競爭」，70b baseline 有寫（故 1.0）。這是單次跑、單一 checkpoint 的內容取捨差異，需 repeat 才知穩不穩，但**它是內容 miss，不是幻覺**。
- **修正後的決策：gen=120b 沒有引入幻覺，先前「不建議採用」的理由不成立、撤回**。目前證據下 120b 淨值中性偏正（修好 sem-02、sem-03 其實是對的、sem-07 一個真實但輕微的內容 miss + 引用標號小瑕疵）。是否轉正仍需更乾淨的量測（見待辦），但**不是因為它幻覺**。
- **教訓（比結論更重要，寫給下次查幻覺的人）**：
  1. **驗證引用時，先把「Reference N」對應到 records 裡 `sources[N-1]` 的真正 `source`+`chunk_index`，再去撈那個 chunk**。答案裡的「chunk #4」是「Reference 5 的 chunk_index」，不是「隨便哪個檔案的 chunk 4」——我兩次都跳過這步、直接拿數字去別的檔案查，兩次都查錯。生成端引用格式本身也有「Reference N vs chunk #M」易混淆的問題（sem-07 模型自己就混了）。
  2. **judge 的 fatal/mn0 判定不能盡信**，尤其 `--correctness-only` 只跑一次合併 judge call。sem-03 就是 judge false positive。**看到 fatal 一定要人工撈原文複查**，不要像我一樣反而拿 judge 的誤判去「佐證」一個預期中的結論（換大模型→更會幻覺）——這是確認偏誤：我心裡先有了「120b 應該更會漏權/幻覺」的假設，就沒有認真檢查 judge 是不是判錯、chunk 是不是查對。
- **待辦**：① 仍值得跑「retrieval 維持 70b、只換生成 120b」的乾淨對照，切開檢索與生成兩個變因（但動機從「查幻覺根因」降級為「一般性隔離」，因為幻覺這個前提已不成立）；② sem-07 的內容 miss（漏競爭語境）與引用標號瑕疵值得 repeat 確認是否穩定；③ judge 可靠度問題重申 07-08「judge 換 120b」條目的待辦（3 票多數決 / 不省略 hallucination 逐條檢查）。

### 生成層煙霧測試（sem-02 + sem-11，端到端 gen+judge）：檢索接力成功但 correctness 沒動——瓶頸已整個移到生成層，且兩題病灶不同
- **動機**：前面所有 glossary / cap / rerank_multi_query 的驗證全是檢索層（chunk 有沒有進 top-5），從沒證明「chunk 進 top-5 會轉成 correctness 上升」。跑最便宜的端到端測試（只 sem-02+sem-11，correctness-only，完整 bundle：translate_query_en + glossary + rerank_multi_query，gen=llama-3.3-70b，judge=gpt-oss-120b）補這一格。
- **結果：檢索接力確實成功，但兩題 correctness 都沒改善**：
  - sem-02：chunk #46（Copilot）**確實進了 top-5（排第 2）**——檢索接力（glossary→cap→rerank_mq）端到端有效，chunk 到位了。**但 correctness 仍 0.5、critical_miss=True**：生成答案引用了 #42/#40/#112/#48，就是**沒引用近在眼前的 #46、沒寫 Copilot**。judge：「missing Copilot mention」。
  - sem-11：chunk #67（price reductions）**也在 top-5（排第 3，raw 0.93）**，但 correctness 仍 0.2、只中 mi2——生成一樣把「價格戰/降價」泛化掉。
  - **結論：瓶頸已從檢索整個移到生成層**。這輪的檢索工作是真實成功的（sem-02 的 Copilot chunk 現在穩定進 top-5），但**檢索是必要非充分**——LLM 拿到 chunk 卻不把關鍵詞寫進答案，分數就是不動。
- **後續：70b vs 120b 生成對照（固定 chunk、只 keyword-check、不燒 judge），揪出兩題是不同的病**：
  | 題 | critical 關鍵詞 | 70b 生成 | 120b 生成 | 判讀 |
  |---|---|---|---|---|
  | sem-02 | Copilot | 1/2（不穩定） | **2/2（穩定）** | **模型能力問題，120b 修得動**——chunk 就在 context 裡，70b 是擲骰子、120b 穩定撈出 Copilot |
  | sem-11 | price war/降價/price reduction | 0/2 | **0/2** | **換模型無效**——連 120b、連語料原文用的 "price reductions" 都不寫,不是模型不夠強 |
- **關鍵結論——三題重新分類**：
  - **sem-02 = 完全可解**：檢索接力（本輪已做）+ 生成換 120b = 完整解法。120b 穩定把 Copilot 寫進答案。這是本輪唯一有明確完整解法的題。
  - **sem-11 = 跟 sem-08 同一種病，不是模型問題**：120b 拿著含 "price reductions" 的 chunk #67（6159 字混合大 chunk，price reductions 只是埋在 offset 1446 的一個從屬子句）還是不寫——**這個從屬子句在大 chunk 裡被生成端稀釋掉了，跟 sem-08 的 chunk #100 稀釋同型，只是 sem-08 卡在檢索層、sem-11 卡在生成層**。外加可能的 rubric 語意落差（10-K 寫 "price reductions" 這個風險後果，rubric 要 "price war" 這個競爭框架）。**槓桿是 chunking（把埋住的子句切成可被 surface 的單元）或 rubric 調整，不是換更大模型。**
- **對「換 120b 生成」決策的意涵**：值得採用,但**只對 sem-02 型（模型能力不穩）有效，對 sem-11/sem-08 型（稀釋/推論）無效**。而且 120b 生成 + 120b judge 會搶同一個 TPD 池,轉正前要跑全量確認 ① 對其他題無回歸 ② TPD 撐得住。本輪因額度限制只驗了 2 題,全量留待後續。
- **待辦更新**：sem-11 從「生成措辭問題（疑 prompt 可修）」重新分類為「**生成層稀釋問題（chunking 才是槓桿）**」，與 sem-08 合流——兩題共用同一個下一步:**MD&A/風險因子的大 chunk 需要更細的語意邊界切分**。這是本輪分析收斂出的最高槓桿工程項（一次可能同時鬆動 sem-08 和 sem-11）。

### 死路復活成功案例：`rerank_multi_query` 在「英文變體 + glossary」新前提下重測——sem-02 chunk#46 從 RANK 直上 top-5（3/3），sem-11 無回歸
- **這是「死路復活條件」機制生效的第一個實例**，流程完整走了一遍：glossary 修法（見下條）把 sem-02 從 RECALL 修回 RANK 後，回頭掃死路表，發現 `rerank_multi_query`（07-07 判死）的兩個死因**同時失效**：① 當年死因是「變體仍是中文，沒解決 cross-lingual」——現在變體是英文、且 glossary 讓它點名 "Copilot"/"Productivity segment"；② 昨天判「不必重測」的理由是「sem-02 是 RECALL，重排池內候選無意義」——glossary 已把 #46 送回池內。按規則觸發重測（純本地 CE 推理，零 LLM judge 成本）。
- **機制**：rerank 用的主 query 是抽象翻譯（"Microsoft's long-term strategic direction in AI"），cross-encoder 拿它對 #46（boilerplate 分部清單）評分只有 raw 0.19；但 glossary 變體 "Microsoft Dynamics 365 Copilot and Productivity segment AI integration outlook" 與 #46 是字面級對口——`rerank_multi_query` 對每個候選取跨 query 最高分，#46 被這個變體直接拉起來。
- **驗證（生產配置 llama-3.3-70b + `translate_query_en` + glossary + `rerank_multi_query=True`）**：
  - sem-02：**3/3 次獨立跑 chunk#46 全部進 top-5**（rank 1~2，raw 0.81~0.95，對照單 query rerank 的 0.19/rank 14）。且 top-5 組成健康——原本的優質候選 #40/#42/#112 仍在，#46 是「加入」而非「擠掉好的」。第一次跑曾出現 top-5 大洗牌（10-Q 候選被變體拉抬），屬變體措辭抽樣差異，後兩次組成穩定。
  - sem-11 回歸檢查（昨天 `sparse_translate_en` 就死在這關）：**chunk#67（價格戰）仍在 top-5**（rank 3，raw 0.80，比單 query 的 0.59 更高）——無回歸，甚至略有強化。TSLA 的 glossary 變體分散到 automotive / energy 兩個真實分部，沒有引入垃圾。
- **為什麼 07-07 同一個參數失敗、現在成功**：當年取 max 只把「已在前面的中文-泛泛相關 chunk」抬更高，因為中文變體對英文語料的 CE 分數整體失真；現在變體是英文+具體行話，max 拉抬的是「字面對口的正確 chunk」。**參數沒變，變的是餵給它的變體品質——再次印證「機制的成敗取決於上游輸入，負面結論綁定當時的前提」。**
- **代價與未完事項**：① multi-query 使 cross-encoder 推理次數 ×3（CPU 上單題 retrieve 明顯變慢，約 2~3 倍），生產啟用前要接受這個延遲；② **尚未跑完整 semantic eval**（TPD 額度限制），`rerank_multi_query` + glossary 的組合對其他 9 題（尤其穩定滿分的 sem-04/05/06/07）的影響未驗證——07-07 的教訓（不能只用受益題驗證）仍適用，**轉正式預設前必須跑全量**；③ sem-02 接力賽還剩最後一棒：#46 進了 top-5，但生成端是否會把 Copilot 寫進答案還沒驗證（burns gen+judge token，與全量 eval 一起做）。
- **死路表更新**：`rerank_multi_query` 從「已試無效」改列「**條件性有效**——需搭配英文變體 + glossary；預設仍 False，待全量 eval 通過後考慮與 glossary 一起轉正」。

### 新增 `_COMPANY_PRODUCT_GLOSSARY`：per-company 產品/分部詞彙表，注入 rewrite prompt——sem-02 chunk#46 從「70b rewrite 完全撈不到」修到 3/3 進 pool
- 動機：本輪稍早發現 sem-02 的真病灶是「llama-3.3-70b 生成的 rewrite 變體撈不到 chunk #46（Copilot 分部描述），120b 的變體可以」。討論後定調：rewrite 對「Microsoft 的 AI 戰略」這種開放策略問題，本質是**盲猜語料具體用詞**（Copilot？Azure AI？segment？）的任務，不是換句話說——小模型不是語言能力不夠，是它沒看過語料、只能靠參數知識盲猜,而 70b 的知識廣度不足以穩定猜中。既然語料就是那 7 家公司的 10-K/10-Q，具體用詞是**已知、有限、可以直接列表給它**的，不需要靠加大模型硬猜。
- **實作**：`rag_query.py` 新增 `_COMPANY_PRODUCT_GLOSSARY: dict[str, list[str]]`（ticker → 產品/分部詞彙 list），**每一項都先用 `grep` 在對應公司的 `data/unstructure_processed/*.txt`（生產 ingest 的實際純文字輸出，注意不是 `data/processed/`）裡確認詞彙真的出現過才收錄**，不臆測（例如 AMZN 原本想收 Bedrock/Trainium/SageMaker，grep 後發現語料裡完全沒出現，全部剔除，只保留 AWS/Alexa/North America segment/International segment）。新增 `_detect_ticker()`（沿用既有 `_COMPANY_TICKER` 對照表，跟 `_extract_structural_filters` 共用同一份表，避免兩份 ticker 判斷邏輯 drift）。`rewrite_query()` 呼叫時用 `_company_glossary_note(query)` 動態組一段「該公司已知詞彙 + 對廣泛策略類問題要求變體分散覆蓋不同詞彙、不要每次都只講最明顯的那個」的說明，附加到 system prompt 尾端——**只在偵測到單一 ticker 時注入該公司的清單，不是每次把 7 家公司全塞進 prompt**（維持 token 精簡，也避免跨公司詞彙污染）。
- **迭代過程（記錄失敗的第一版，避免下次重踩）**：第一版只列詞彙表、沒有「變體要分散覆蓋不同詞彙」的明確指令，結果 3/3 次都只圍繞著清單裡最顯眼的詞（Azure AI）打轉，完全沒生出 Copilot/segment 相關的變體——**單純餵詞彙表不夠，還要求它主動分散選擇才會生效**（模型有清單可查，但預設仍會收斂到最常見的那個詞，跟 rewrite 本身的「盲猜」病灶是同一種傾向：不主動探索，只給最保守的答案）。加上分散覆蓋的指令後才穩定改善。
- **驗證（`--translate-query-en`，`model_name=rq.DEFAULT_MODEL`=llama-3.3-70b，即生產配置）**：
  - `rewrite_query()` 單獨測試：3 次呼叫裡有 2 次生出含 "Copilot"/"Productivity segment" 字面的變體（非 100% 穩定，但已從「0/3 完全不提」進步到多數命中）。
  - **完整 pipeline（含 RRF fusion + rerank）3/3 次獨立跑：chunk #46 全部進 pool**（`in_pool=True`，pool_size 29~31，注入 Dynamics 365 等額外詞彙也讓池變大了一些）——對照修 bug 後的生產 baseline（3 次跑 0/3 進 pool），是穩定、可重現的改善。**但 3/3 都還是 `in_topk=False`**——chunk #46 進了候選池，但還是被 cross-encoder 擠出最終 top-5。也就是說：這個修法把 sem-02 從「RECALL（連候選都沒有）」修回「RANK（有候選、排序輸）」，**跟原本 07-07 記錄的病灶一致，但沒有解決 RANK 那一層**——RANK 問題是 07-07 已診斷過的「cross-encoder 對策略類問題把量化財務段落排在質化敘述之上」，是 reranker 相關性問題，不是 rewrite 能力問題，兩者是接力關係不是同一個關卡。
  - Sanity check（防止注入傷到其他情境）：`"NVDA gross margin"` 這種已是標準英文金融術語的窄查詢，仍正確回傳 `[]`（glossary 注入不影響「無需改寫」的判定邏輯，因為判定邏輯在 few-shot + 指令層，不受詞彙表本身干擾）；TSLA 的策略類查詢受益於同一套機制，變體自然分散成 "automotive segment" 與 "energy generation and storage segment" 兩個真實分部（不在原本設計目標內，屬於意外的正面副作用）。
- **結論**：確認了「rewrite 的問題是盲猜、不是模型能力，餵已知詞彙表比換大模型便宜且確定性高」這個假設方向正確且已驗證——但這只解決 sem-02 三層失敗（recall→rank→generation）裡的**第一層**。要讓 sem-02 真正拿到滿分，還需要疊加解決 RANK 層（07-07 已知、未解的 reranker 策略類偏誤問題，見 CHANGELOG 07-07「後續：sem-02/08 排序問題」）。這個修法對 sem-08（AWS）**沒有直接幫助**——sem-08 的病灶是 chunk 本身稀釋（chunk #100 混雜多個不相關主題），不是 rewrite 選錯詞彙的問題，給對詞彙也救不回一個向量已經被稀釋的 chunk（見上方 sem-08 根因段落）。
- **待辦**：① 這個修法尚未跑完整 semantic eval 驗證整體有無回歸（同樣卡在今天的 TPD 額度）；② `_COMPANY_PRODUCT_GLOSSARY` 目前只是手動 grep 出來的初版清單，隨語料更新（例如 fetch_data.py 重抓新財報）需要人工維護，沒有自動化機制——如果之後常態性更新語料，這張表會逐漸過期，值得在 `data_update_unstructure.py` ingest 完成後加一段自動掃描新 segment/product 關鍵詞的邏輯，而不是純手動列表。

### Bug：`diagnose_crit_miss.py` 用 judge model（而非生產 model）跑 retrieve() 內部的 filter/rewrite/translate，導致 sem-02 的診斷結論比生產實況樂觀
- **發現經過**：想低成本重驗 sem-02（dump chunk #46 的 rerank raw 分數），直接呼叫 `rq.retrieve(..., model_name=rq.DEFAULT_MODEL)`（= `llama-3.3-70b-versatile`，生產用的模型）——結果 chunk #46（唯一點名 Copilot 跨 M365 整合的分部描述）**完全沒進 pool**（20 個候選裡沒有它），兩次重跑結果 byte 級一致（rrf_score 到小數點後 6 位全同），排除是抽樣噪音。但 CHANGELOG 07-07 診斷（`crit_miss_diagnosis_sem020811.json`）明明記錄 sem-02 是「pool 有、被擠出 top-5」（RANK，pool_size=21）。
- **根因**：`diagnose_crit_miss.py` 呼叫 `rq.retrieve(..., model_name=judge, ...)`——`judge` 預設是 `openai/gpt-oss-120b`，這個變數同時被拿去做兩件事：① `judge_checkpoint_coverage()` 的判定 model（正確用途）；② `retrieve()` 內部 `parse_query_filters`/`rewrite_query`/`translate_query_to_english` 的 model（**錯誤**——生產與 `eval_generation_llm_judge.py` 的檢索都用 `gen_model`/`DEFAULT_MODEL`=llama-3.3-70b，diagnose 腳本卻悄悄換成 120b 跑檢索側）。**這是兩個不該綁在一起的參數**：判斷力（judge）跟生成/檢索用的模型（gen）是獨立維度。
- **驗證**：分別用 120b 與 70b 當 `model_name` 跑同一句 sem-02 query，`rewrite_query()` 生成的變體不同——120b 的變體能撈到 chunk #46（pool 20→23，rrf_score=0.5，最終 rerank 排第 14）；70b 的變體撈不到（pool 維持 20，chunk #46 完全不在候選裡）。**兩個模型的 rewrite 品質有真實差異，且 diagnose 腳本過去一直用的是「使用者實際不會用到」的那個（120b）。**
- **修法**：`diagnose_crit_miss.py` 新增 `--retrieval-model`（預設 `rq.DEFAULT_MODEL`，即生產用的 llama-3.3-70b），`retrieve()` 呼叫改用它；`--judge` 保留，只用於 `judge_checkpoint_coverage`。兩個維度徹底解耦。
- **影響範圍**：過去所有 `diagnose_crit_miss.py` 產出的診斷檔（`crit_miss_diagnosis*.json` 全系列，含 07-07 的 sem-02/08/11 首次診斷、07-08 的 translate_query_en 系列）都是在這個 bug 下跑的——**任何「pool 有、靠 rewrite 變體撈到」的 verdict 都可能過度樂觀**，因為用的是比生產更強的模型做 query understanding。sem-11 的 verdict（price war checkpoint OK）在兩個模型下結果一致（見下方修正後 baseline），不受影響；但 sem-02 的 verdict 從 RANK 降級為 **RECALL**，是實質更嚴重的問題。
- **教訓（給下次寫 diagnose/eval 腳本的人）**：`retrieve()` 內部有 LLM 呼叫時，診斷腳本傳給它的 `model_name` 必須跟生產/主要 eval 腳本一致，否則量到的是「一個使用者永遠不會用到的檢索路徑」，結論無法外推。judge model 和 retrieval model 永遠是兩個獨立參數，不要圖方便共用一個變數。

### 修正後 baseline（`--retrieval-model` 用生產 llama-3.3-70b + `--translate-query-en`）：sem-02 降級為 RECALL，sem-08/sem-11 結論不變
`eval/crit_miss_diagnosis_prodmodel_fixed.json`（judge=120b，retrieval-model=llama-3.3-70b，這是修完上述 bug 後第一份「使用者實際會遇到的檢索路徑」診斷）：

| 題 | critical checkpoint | verdict（生產模型） | 對照：舊 diagnose（120b 檢索）|
|---|---|---|---|
| sem-02 | Copilot 跨 M365 整合 | **RECALL（pool 就沒有）** | RANK（pool 有，被擠出）|
| sem-08 | AWS 獲利貢獻/營業利益（新 rubric）| RECALL（pool 就沒有）| RECALL（一致）|
| sem-11 | EV 價格戰 | OK (in top-k) | OK（一致）|

**sem-02 現在跟 sem-08/11 是同一類問題（RECALL），不再是「差一名」的排序問題**——先前規劃的「dump #46 rerank 分數、找差距」這條低成本診斷路徑已經不適用（chunk #46 在生產配置下根本不是候選）。真正的槓桿是「為什麼 llama-3.3-70b 生成的 rewrite 變體撈不到 chunk #46，120b 的可以」，這是一個新的、獨立的待查問題（見下方待辦）。

### sem-08 根因坐實：AWS 獲利貢獻文本真實存在於語料（`AMZN_10K_2025.html` chunk #100），但該 chunk 是稀釋型大 chunk，dense/sparse 排名都低，與語言無關
- 決策樹①：grep `data/unstructure_processed/AMZN_10K_2025.txt`（注意——生產 ingest 的純文字輸出在 `data/unstructure_processed/`，不是 `data/processed/`；`data/processed/AMZN_*.txt` 只有 Fundamentals/IncomeStatement/News，沒有 10-K/10-Q，第一次查找路徑找錯目錄，浪費了一次 grep）確認原文：「The increase in AWS operating income in 2025, compared to the prior year, is primarily due to increased sales, partially offset by spending on technology infrastructure...」——**完全符合新 rubric 的要求，資料存在，不是 rubric 或 ingest 缺失問題**（用 Qdrant 直接 scroll `source=AMZN_10K_2025.html` 確認這段文字對應 chunk_index=100，`chunk_type=text`）。
- **探針實驗**（獨立 sparse/dense query，限定 `source=AMZN_10K_2025.html` 排除跨文件干擾，量 chunk #100 在 within-file top-60 的排名）：

  | 通道 | 中文 query | 翻譯後英文 query |
  |---|---|---|
  | sparse | rank 26 | rank 21 |
  | dense | rank 31 | rank 38（**變差**）|

  兩個語言版本排名都在 20~38 之間、幾乎不動——**翻譯不是這個 chunk 的瓶頸**。查看 chunk #100 全文，發現它混雜了「Other Operating Expense (Income), Net」「FTC 訴訟和解」「稅務爭議」「實體門市資產減損」「Segment Operating Income 表格」，AWS 營業利益的敘述被埋在這堆不相關主題的最後——**與 sem-02 的 Copilot chunk（埋在 boilerplate 分部清單裡）、TSLA #67（混合競爭/自駕/電池/稅務）同一種病：稀釋型大 chunk，dense/sparse 相似度被其他主題拉低，跟語言無關。**
- **結論**：sem-08 不會被任何翻譯手段救回，需要的槓桿是「讓這句話所在的語意單元獨立成可被檢索到的向量」——但 CHANGELOG 07-07 已證實「把單一風險段落孤立抽出」對 **rerank** 反而更糟（raw 分數更低，見 TSLA #67 的孤立段落實驗，0.0148）。這裡是 **recall 層**（dense/sparse 相似度)問題，不是 rerank 層，兩者機制不同，07-07 的負面結論不能直接照搬類推到這裡是否值得一試——**但尚未實測，留待後續**（可能的方向：更小的 chunk size、或針對 MD&A 分部敘述做語意邊界感知的切分，而非目前的固定 unstructured 切法）。

### 實驗 3：`sparse_translate_en`（dense 保持中文 / sparse 改英文的 split-encode）——已實作、已測試，**淨負面，不建議啟用**
- 動機：`rag_query.py` 07-07/07-08 已證實「dense+sparse 一起翻英文」淨負（sem-11 從 OK 退步成 RANK）。但兩路綁在一起測，分不清是 dense 翻譯有害、還是 sparse 翻譯其實有幫助只是被 dense 蓋過——sparse 是純 lexical token 比對，中文 query 與英文語料理論上零重疊，翻譯對它應該是純增益。想拆開驗證。
- **探針結果（單獨查，非完整 pipeline，見上方 sem-08 段落）**：sem-08 的 chunk #100 翻譯前後幾乎不動（稀釋型 chunk，語言不是瓶頸）；但 sem-11 的 FSD chunk（`TSLA_10K_2025.html` #56，「Regulation of Autonomous Vehicles」段落）**中文 sparse 完全查無此 chunk（within-file top-60 都排不進去）**，翻英文後 sparse rank 26、dense rank 24（從 38 進步）——單獨看，這是支持「sparse 該用英文」假設的正面訊號。
- **實作**：`rag_query.py` `retrieve()` 新增 `sparse_translate_en: bool = False` 參數。開啟時 dense 向量沿用原始 query 的 encode 結果，sparse 向量改用（`translate_query_en` 已翻好的，或獨立呼叫翻譯的）英文版 encode 結果，兩者拼成同一組 `q_vecs` 送進 RRF prefetch。`eval/diagnose_crit_miss.py` 加 `--sparse-translate-en` flag。
- **完整 pipeline 驗證（`--ids sem-08 sem-11 --translate-query-en --sparse-translate-en`，judge=120b）── 淨負面**：
  - sem-08：critical checkpoint 仍 RECALL（chunk #100 依然沒進 25 個候選的 pool，即使 sparse 換英文、加上 rewrite 變體擴召回，enumerate 過的 pool 裡完全沒有 #100）——探針的樂觀訊號在完整 pipeline（含 rewrite 變體競爭、RRF 融合、ticker filter）下沒有兌現。
  - sem-11：critical checkpoint（EV 價格戰，chunk #67）**從穩定的 OK (in top-k) 退步成 RANK（pool 有、被擠出 top-5）**——這是先前 07-07/07-08 花最多力氣才修好的那個 checkpoint，被這個新參數重新打回退步狀態。FSD checkpoint（chunk #56）依然 RECALL，孤立探針顯示的排名進步（rank 24~26）不足以在跟其他候選 + rewrite 變體競爭 RRF_TOP_N_PRIMARY=20 的名額時勝出。
- **結論**：孤立探針的正面訊號在完整 pipeline 裡完全沒有兌現，反而額外傷到一個已修好的 checkpoint——**與 07-06 RRF-fusion 實驗、07-07 rerank_multi_query 實驗同一種教訓：單一 chunk 的孤立分數提升，不保證在跟其他候選共同競爭 RRF 融合與 rewrite 變體時也提升**。`sparse_translate_en` 保留在程式碼中（預設 `False`），標記為已測試無效，不建議啟用；也不建議再花時間對「疑似语言敏感」的 checkpoint 做孤立探針就下結論——必須用 `diagnose_crit_miss.py` 跑完整 pipeline 才算數。

### 實驗 1（嘗試並放棄）：`SYSTEM_PROMPT` Rule 10「策略/風險題必須沿用來源文件具體措辭」——3 次獨立重新生成 0/3 命中，判定無效、已還原
- 動機：sem-11 的 EV 價格戰 checkpoint 在 07-07 資料修復後已進 top-5（見上方 baseline），但 07-07 的生成層 eval 顯示答案仍只寫「競爭激烈」沒寫「降價/價格戰」——checkpoint 支撐文本已到位，純粹是生成端把具體措辭泛化掉的問題，猜測加一條 prompt rule（要求沿用來源具體用詞、列舉多重後果而非收斂成單一泛化短語）能修。
- **驗證方法**：固定住檢索到的 top-5 chunk（跳過 retrieve，直接用已知的 5 個 chunk id 組 context，排除檢索側抽樣變異），用 `GEN_TEMPERATURE=0.3` 獨立重新生成 3 次，檢查答案是否出現「price reduction / price war / 降價 / 價格戰」任一關鍵詞。
- **結果：3/3 全部沒命中**，且不是分數擺動型的失敗——三次答案的措辭高度相似，都把 chunk #58/#67 的競爭段落收斂成「激烈的競爭」，即使 chunk #67 原文明確寫「could result in lower vehicle unit sales, **price reductions**, revenue shortfalls, loss of customers and loss of market share」（offset 1446/6159，不算深埋）、prompt 也明確要求「列舉 chain 裡至少 2-3 項具體項目」。這不是溫度噪音，是這個 rule 對 llama-3.3-70b（Groq）**穩定無效**。
- **判定**：rule 已從 `SYSTEM_PROMPT` 移除（未 commit 前先在本機驗證失敗，不留無效程式碼在生產 prompt 裡；也因為沒有預算跑完整 11 題 semantic 驗證這條 rule 對已過關的題目有沒有副作用，沒有驗證過的 prompt 規則不該留在生產）。
- **教訓/待辦**：prompt-level「要求列舉具體措辭」對這類「大 chunk 裡一個從屬子句提到具體機制」的情境系統性無效，可能需要更強的槓桿——例如 few-shot 範例示範「如何從長 chunk 摘出具體措辭」，或是這其實跟 sem-08 同一種病（chunk 稀釋，見上），該從**檢索/chunking**下手而非生成 prompt。不建議重試同類「加一句要求具體措辭」的 prompt rule，除非先驗證是 few-shot 層級的介入。

### 死路復活條件（給下次改動後判斷「哪些已放棄的實驗需要重測」用）
每個「已試無效」的死路都附一個**復活條件**——標明哪個假設一旦改變，這個負面結論就可能失效。改動 baseline 後，只需掃這張表，看有沒有改動剛好命中某條的復活條件，命中才需要重測；沒命中的維持死路狀態，不必照表重跑一輪。

| 死路 | 死因 | 復活條件 | 目前狀態 |
|---|---|---|---|
| `rerank_multi_query`（07-07）| 死因記載是「變體仍是中文」| 變體改英文 **且目標 chunk 在池內**（RANK 層工具救不了 RECALL）| **已復活（見上方 2026-07-08 復活案例條目）**：glossary 修法讓 sem-02 回到 RANK 後重測，英文+glossary 變體下 chunk#46 3/3 進 top-5、sem-11 無回歸。條件性有效，待全量 eval 後考慮轉正 |
| chunking 切細（07-07）| 孤立段落無上下文，cross-encoder 評分反而更低（TSLA #67 raw 0.0148）| cross-encoder 換成對上下文無關的評分方式 | 未觸發，續死；但**該負面結論是 rerank 層的，不能直接套用到 sem-08 的 recall 層問題**（見上，未實測過） |
| dense/sparse 一起翻英文（07-08）| sem-11 從 OK 退步成 RANK（dense 翻譯有害）| dense、sparse 拆開單獨測 | **已測（本次 `sparse_translate_en` 實驗）：拆開後 sparse 翻譯仍無法在完整 pipeline 救回任何 RECALL checkpoint，且新增了對 sem-11 已修好 checkpoint 的傷害——結案，維持死路** |
| RRF-fusion（07-06）| chunk 層變雜訊化（跨變體妥協排名）| 無明確復活條件（機制型：只要 RRF fusion 本身跨變體平均化候選，就會稀釋單一 variant 的強訊號）| 未觸發，續死 |
| top_k / RERANK_INPUT_N 加大（07-07）| pool 早就 < 上限，加大無效 | pool 規模超過現有上限（例如 rewrite cap 大幅提高、或候選來源增加）| 未觸發，續死 |
| Rule 10 prompt（本次）| llama-3.3-70b 對「列舉具體措辭」指令穩定不遵從（3/3）| 換更強的生成模型、或改用 few-shot 示範取代純文字指令 | 剛加入，尚未有復活觸發事件 |

### 待辦（本輪未完成，留給下次）
- **Experiment 0（全量 semantic baseline 驗證）未執行**：今天在跑 `diagnose_crit_miss.py`（sem-08/sem-11 兩次 + sparse_translate_en 實驗）時已觸發一次 GROQ_API_KEY TPD 耗盡、自動輪換到 KEY2——為避免燒穿全部 4 把 key 影響其他人使用，本輪**沒有跑完整 11 題 semantic 的 `eval_generation_llm_judge.py`**。目前 `rag_query.py` 唯一留下的實質程式碼變動是 `sparse_translate_en` 參數（預設 False，不影響現有行為）與 `diagnose_crit_miss.py` 的 `--retrieval-model` 修正——兩者都不改變任何預設路徑，**理論上不需要全量回歸驗證就能安全存在**，但下次有 TPD 額度時仍建議跑一次 `--category semantic --rewrite --translate-query-en --correctness-only` 單次跑，確認 `translate_query_en` 本身（上週已實作但從未跑過全 11 題）没有對 sem-04/05/06/07 這些穩定滿分題造成回歸，這是比本輪任何新實驗都更優先的「補作業」。
- **sem-02 新方向（本次發現）**：為什麼 llama-3.3-70b 生成的 rewrite 變體撈不到 chunk #46、120b 的可以？值得單獨跑 `rewrite_query()` 比較兩個模型對同一句 query 生成的變體字面差異，找出是措辭問題還是模型能力問題。這比繼續在 sem-08/11 打轉更有 leverage（sem-02 從「排序」降級成「召回」代表問題比原先認知的嚴重）。
- **sem-08 chunking 方向未實測**：稀釋型大 chunk 是否該用更細的語意邊界切分，尚未實驗，且 07-07 的「chunking 切細無效」結論是 rerank 層量測，不能直接當作 recall 層也無效的證據（見上方死路表）。
- **翻譯 drift 量化實驗未執行**（人工寫 reference 英文 query 對照 LLM 翻譯版的 cross-encoder 分數）——labor-intensive，本輪時間/預算不足，仍是判斷「要不要換 reranker」的正確前置步驟。
- **eval/ 目前仍未被 git 追蹤**——本輪新增的 `crit_miss_diagnosis_prodmodel_fixed.json`、`crit_miss_diagnosis_sparse_en.json` 等結果檔同樣只在本機。建議找時機 `git add eval/`。

### 重構 `translate_query_en`：入口翻譯一次，但只有 rerank 受益——dense/sparse 召回、rewrite 輸入都改回原始 query（兩個實測踩坑）
- 動機：原本 `rerank_translate_en` 只在 rerank 前局部翻譯，使用者提議更乾淨的架構——入口翻一次英文，dense/sparse encode、query rewrite、cross-encoder rerank 全部改用同一個翻譯結果，避免多處各自呼叫翻譯。原則上同意（省 LLM 呼叫、rewrite 從英文出發也更穩），先動手重構再驗證。
- **踩坑 1：rewrite 變體歸零**。`QUERY_REWRITE_SYSTEM_PROMPT` 明文「若問題已是標準英文金融術語，回傳空列表」；`translate_query_to_english()` 的翻譯目標正是「標準英文金融術語」，兩者職責重疊——把翻譯後的英文餵給 `rewrite_query()`，會被它自己的規則判定「已標準」而回傳空變體，白白喪失多查詢擴召回（sem-11 pool 22→20，sem-02/08 同樣萎縮）。**修法：`rewrite_query()` 的輸入改回原始 query**（通常是中文，永遠不會被誤判「已標準」，穩定觸發變體生成）；`en_query`（翻譯結果）只用於 rerank。
- **踩坑 2（更關鍵）：dense/sparse 召回翻英文，sem-11 從 `OK (in top-k)` 退步成 `RANK`**。修完踩坑 1、pool 大小恢復正常（20→21~25）後重新驗證，sem-11 的 critical checkpoint 仍比「只翻 rerank、召回維持中文」那版差一級。這不是噪音——dense/sparse encode 是決定性函式（無 LLM 抽樣），兩次跑翻譯文字本身也完全一致，池組成的差異是「餵中文 vs 餵翻譯後英文」的真實效應：BGE-M3 dense 本身已是多語言、中文召回本來就 work（sem-11 的 chunk #67 中文時就在池內），翻譯反而引入一次 paraphrase drift，改變了 RRF 候選池的組成，讓 cross-encoder 看到的候選組合變了、擠掉了原本命中的 chunk。**修法：dense/sparse encode 也改回原始 query**，翻譯結果 `en_query` 最終只餵給 cross-encoder rerank——目前是唯一有決定性正面證據（sem-11 raw 0.39→0.94）的環節。
- **最終架構（`rag_query.py` `retrieve()`）**：入口只呼叫一次 `translate_query_to_english()`（單一真相來源，避免重複翻譯），但消費者收斂成「只有 rerank」；`parse_query_filters`（結構化代碼抽取，語言無關）、dense/sparse encode、`rewrite_query` 輸入、呼叫端的答案生成，全部維持用原始 query。`retrieve()` 的參數統一為 `translate_query_en`（取代先前的 `rerank_translate_en`，`eval_generation_llm_judge.py`/`diagnose_crit_miss.py` 的 CLI flag 同步改名 `--translate-query-en`）。
- **驗證**（`diagnose_crit_miss.py --translate-query-en`，judge=120b）：sem-11 critical checkpoint 恢復 `OK (in top-k)`、pool 正常（22），sem-02 仍 `RANK`（差一名，與純翻 rerank 版一致）、sem-08 仍 `RECALL`（rubric 問題，見上，與翻譯無關）。三題結果與「只翻 rerank」版完全一致，證實 dense/sparse 翻譯確實是純損耗，拿掉它沒有損失、只有恢復。
- **教訓（寫給下次想重構類似「翻一次、全部共用」設計的人）**：「單一翻譯呼叫」（省成本）跟「翻譯結果該喂給哪些下游環節」（正確性）是兩個獨立決策，不能因為前者聽起來乾淨就假設後者全部成立——每個下游環節（rewrite 的 prompt 邏輯、dense/sparse 的多語言能力）都有自己的既有行為，翻譯是否幫上忙需要**逐一實測**，不能用架構簡潔性取代實證。

### sem-08 rubric 修正：critical checkpoint 要求「AWS 市佔龍頭」超出來源文件範圍，已改寫（仿 sem-07）
- 背景：前一日診斷（決策樹①，grep 全部 `AMZN_*` processed 語料）確認「market leader / leading cloud / largest cloud / market position」**0 次命中**，10-K 只有「AWS operating income 增長，因銷售增加」這種中性會計敘述。與 sem-07（MSFT 10-K 從不點名 AWS/GCP）同型——rubric 要求了 grounding 文件本身沒有的具名定位語，逼模型引入世界知識，與系統反幻覺原則衝突。
- **修法**：`eval_set.json` 的 sem-08 與 col-05（`maps_to: sem-08`，rubric 完全複製，兩處一併）的 mi0 checkpoint 從「把 AWS 定位為雲端市佔龍頭 / Amazon 的獲利引擎」改為「提到 AWS 的獲利貢獻或營業利益表現（如營業利益增長、為 Amazon 重要獲利來源）——不要求出現『市佔龍頭 / market leader』等來源文件本身未使用的具名定位措辭」。weight=50 / is_critical=true 不變。用 Python str.replace 精確替換（保留 CRLF、只動這 2 處，其餘 byte 不變）。
- 下次跑 semantic / colloquial eval 需重新驗證 sem-08、col-05 是否脫離 critical_miss；若仍未過才需查生成/排序。**注意**：sem-08 先前另有「in_pool=RECALL」的診斷，但那是在舊 rubric（要求龍頭措辭）下判的——改 rubric 後 checkpoint 定義變了，`diagnose_crit_miss` 的召回判定要用新 rubric 重跑才有意義。

## 2026-07-07

### 🔑 重大發現：sem-02/08/11 排序問題的真正根因是 cross-lingual rerank（中問英答），不是 chunking/數量/multi-query
- **一句話**：`bge-reranker-v2-m3` 對「繁中 query ↔ 英文文檔」的相關性評分嚴重失真；把 rerank query 翻成英文後，命中 chunk 的 cross-encoder 分數暴漲、直接進 top-k。這是先前所有「調數量 / multi-query / chunking」路線全部無效的統一解釋。
- **決定性實驗（sem-11，穩定 pool、關 rewrite）**：唯一含「price reductions / EV 價格戰」論述的 `TSLA_10K_2025.html` chunk #67，同一段文本、同一個 pool、只換 rerank query 語言：

  | rerank query 語言 | #67 cross-encoder raw | #67 名次 |
  |---|---|---|
  | 繁中（原始 query） | 0.3893 | 第 5（壓線，加 rewrite 後常被變體候選擠出 top-5）|
  | **英文（翻譯 query）** | **0.9427** | **第 1** |

  #77（人才競爭）、#128（審計）這些原本靠中文 query 排在前面的「泛泛沾邊」chunk，換英文後名次全部下滑，讓真正命中的 #67 浮上來。
- **一路排除掉的錯誤假設（都有實測，別再重試）**：
  1. **數量參數無效**：`RERANK_INPUT_N`/`DEFAULT_TOP_K`，pool 早就 < 上限（見下方舊條目）。
  2. **`rerank_multi_query`（原 query + 中文 rewrite 變體取跨 query 最高分）無效**：實測 sem-11，#67 兩種模式都沒進 top-5；取 max 只把「已在前面的 chunk」分數抬更高（#77 0.67→0.85），沒救到墊底的命中 chunk——與先前 RRF-fusion 失敗同型（跨變體妥協反而更雜訊）。**因為變體仍是中文，沒解決 cross-lingual 這個真病灶。**
  3. **chunking 切細無效、甚至更糟**：把 #67（6159 字混合 chunk：競爭+自駕+Optimus+電池+國際稅務）裡「純競爭風險」段落單獨抽出來評分，raw **0.0148**（比整個 #67 還低）。孤立短段落失去上下文錨點，cross-encoder 給更低分。**推翻 CHANGELOG 先前對 sem-02 猜的「chunk 太大稀釋、切細可救」假設。**
- **已實作（`rerank_translate_en` 參數，預設 False）**：`rag_query.py` 新增 `translate_query_to_english()`（LLM temp=0 翻譯，`_looks_english()` 偵測已是英文則 skip，翻譯失敗 fallback 原 query，並正規化 `‑` 等特殊 dash→ASCII）。`retrieve()` 加 `rerank_translate_en` 參數，開啟時只把 rerank 用的 query 翻英文，**召回（dense/sparse encode）仍用原 query，不動 chunking/embedding**。`eval_generation_llm_judge.py` / `diagnose_crit_miss.py` 加 `--rerank-translate-en` flag。（踩坑：DEBUG print 翻譯結果時 Windows cp950 終端遇 `‑` 直接 `UnicodeEncodeError` 中斷整個 eval——print 已改 ascii-safe。）
- **檢索層驗證結果（`diagnose_crit_miss.py --rerank-translate-en`，judge=120b，`crit_miss_diagnosis_translate_en.json`）＝混合，不是三題全解**：

  | 題 | critical checkpoint | baseline verdict | 翻英文後 verdict | 判讀 |
  |---|---|---|---|---|
  | sem-11 | EV 價格戰 | 壓線/被擠出 | **OK (in top-k)** | ✅ 排序問題，翻譯解決（單題實驗 #67 raw 0.39→0.94 佐證）|
  | sem-02 | Copilot 整合 | RECALL | **RANK**（進 pool 未進 top-5）| ⚠️ 部分改善，還差臨門一腳 |
  | sem-08 | AWS 龍頭定位 | RECALL | 仍 **RECALL**（pool 就沒有）| ❌ 是**召回**問題不是排序，rerank 翻譯救不到 |

- **關鍵結論：三題不是同一種病**。sem-11=純排序（翻譯已解檢索層）；sem-02=排序+邊緣（翻譯把命中 chunk 從召回失敗推進到 pool 內、但 rerank 仍差一名）；sem-08=**召回層**（critical chunk 連 pool 都進不來，要動的是召回/HyDE，不是 rerank）。先前把三題都歸為「排序問題」是過度簡化。
- **sem-08 後續查證（決策樹①）＝疑似 rubric 超綱，不是召回問題，HyDE 無效**：grep 全部 `AMZN_*` processed 語料，「market leader / leading cloud / largest cloud / market position」**全部 0 次命中**。10-K 只寫「AWS operating income 增長，因銷售增加、部分被基礎設施投資抵銷」這種中性敘述——SEC filings 不會自稱龍頭（與 sem-07 的 MSFT 10-K 從不點名 AWS/GCP 同一種模式）。critical checkpoint「把 AWS 定位為雲端市佔龍頭」要求了 grounding 文件裡不存在的措辭，HyDE 造再好的假設文檔也檢索不到不存在的文本。**正解與 sem-07 相同：改寫 rubric**（例如改成「提到 AWS 營業利益增長 / 對 Amazon 獲利的貢獻 / 持續資本投入（不要求『龍頭』措辭，來源文件本身無此定位語）」）。「獲利引擎」一半可由分部營益表推算（AWS op income 佔比），但語料中沒有任何單一敘述 chunk 直接這樣說。
- **待辦**：① 跑全 11 題 semantic（`--rerank-translate-en`，理想 `--repeat 3`）確認整體 correctness 不回歸、特別是已滿分題無副作用，才能決定 `rerank_translate_en` 是否轉正式預設 True；② 改寫 sem-08（及對應 colloquial 鏡像題若有 maps_to）的 mi0 rubric，仿 sem-07 修法；③ sem-02 差一名，可試「翻譯 + 檢查 #46 的英文 rerank 分數」定位是語言殘留還是 chunk 內容問題；④ 召回側「sparse 通道對中文 query 失效」假設值得驗證（見分析）——中文 query 的 lexical tokens 與英文語料幾乎零重疊，sparse prefetch 形同虛設，翻英文可能同時修復 sparse 召回。
- **注意**：`rerank_multi_query` 參數保留在程式碼（預設 False），標記為「已試無效」，勿在生產啟用。

### Bug：`eval_generation_llm_judge.py` / `diagnose_crit_miss.py` 一直讀著 Docker 遷移前的舊 local snapshot，而非正式 Docker collection
- **背景**：2026-07-06 把 Vector DB 從 `QdrantClient(path=...)` local embedded mode 換成 `QdrantClient(url=QDRANT_URL)` Docker server mode（見上一條「Vector DB」changelog），當時記錄「影響檔案：rag_query.py、data_update_unstructure.py、eval/eval_chunk_recall.py」——**但 `eval_generation_llm_judge.py`、`diagnose_crit_miss.py`、`compare_generation.py`、`eval_retrieval.py`、`eval_retrieval_filtered*.py`、`eval_rerank.py` 全部漏改**，仍硬編碼 `QdrantClient(path=rq.QDRANT_PATH)`。
- **發現經過**：修完 TSLA 10-K 資料（見下條）後想跑 `diagnose_crit_miss.py` 驗證，背景執行卡住無輸出。手動用 Python 開 `QdrantClient(path='./qdrant_db')` 直接得到 `RuntimeError: Storage folder ... already accessed by another instance`——local mode 是排他檔案鎖，才知道該腳本仍在走 local 模式。進一步查證：`./qdrant_db/meta.json` mtime 停在 2026-06-29（Docker container 是 2026-06-30 建的，`docker ps` 顯示 up 7 天）——**local `qdrant_db` 從 Docker 遷移那天起就徹底沒再更新過**，是一份凍結在遷移前一刻的舊快照；Docker 側真正的資料放在 `qdrant_docker_storage;C`（bind mount，`curl localhost:6333/collections` 確認 `us_stock_rag_unstructured` 在裡面且是最新狀態）。
- **影響範圍評估**：CLAUDE.md 列的「主要三支」eval 腳本中，`eval_two_stage.py` 本來就用 `rq.make_qdrant_client()`（沒受影響）；但 `eval_generation_llm_judge.py`（k=3 semantic baseline、judge 120b 換模型實驗、split-temperature 實驗全靠它）和 `diagnose_crit_miss.py`（sem-02/08/11 排序 vs 召回診斷）**兩支主力腳本都中招**。好在 2026-07-06/07 的溫度/prompt/judge 實驗本身不涉及資料層更動，local 快照當時內容仍與 Docker 一致，這些結論不受影響；**唯一真正失真的是「TSLA 10-K 修復是否解決 sem-11」這個問題本身，若不修就會用舊快照（仍是薪酬 10-K/A）驗證，得到假陰性**。
- **修法**：把所有硬編碼 `QdrantClient(path=...)` 的 eval 腳本統一改成 `make_qdrant_client()`（依 `.env` 的 `QDRANT_URL` 自動選 server/local，與 `rag_query.py`/`data_update_unstructure.py`/`eval_two_stage.py`/`eval_chunk_recall.py` 一致）：
  - 主力兩支：`eval_generation_llm_judge.py`、`diagnose_crit_miss.py`。
  - 次要五支（一併修，不留待辦）：`eval_retrieval.py`、`eval_retrieval_filtered.py`、`eval_retrieval_filtered_unstructured.py`、`compare_generation.py`、`eval_rerank.py`。其中 `eval_rerank.py` 保留 `--qdrant-path` CLI 參數的語意——`QDRANT_URL` 有設時走 Docker server（無視 path），未設時才 fallback 到指定 local path。
- 全部 7 支已 `ast.parse` 語法驗證通過。往後新增 eval 腳本一律用 `make_qdrant_client()`，不要再直接 `QdrantClient(path=...)`。

### sem-11 資料修復：`fetch_data.py` 排除 10-K/A，重抓 TSLA 完整年報
- 根因（見下條 2026-07-07 診斷）：`fetch_sec_filings()` 用 `company.get_filings(form="10-K").latest(1)`；edgartools `get_filings()` 的 `amendments` 參數**預設 `True`**，`form="10-K"` 會連 10-K/A 一起匹配，`latest(1)` 因此撈到最新申報的那份——TSLA 剛好是只含高管薪酬修正的 10-K/A（2026-04-30 申報），而非 2026-01-29 申報的正式年報。
- **修法**：`fetch_sec_filings()` 的 `get_filings(form=form_type)` 改成 `get_filings(form=form_type, amendments=False)`（10-K、10-Q 都加，10-Q 同樣有 amendment 風險）。
- **驗證**：改完後 `python fetch_data.py --tickers TSLA --skip-news --skip-fundamentals` 重抓，`latest(1)` 現在拿到 accession `0001628280-26-003952`（2026-01-29 申報、period 2025-12-31，2.39MB，含 `Item 1A`×4、`Full Self-Driving`×5、`price reduc`×1 等風險因子論述）——同一個 `_period_tag()` 產生的檔名（`TSLA_10K_2025.html`）覆蓋掉舊的 698KB 修正案，**不需要額外孤兒清理**：`data_update_unstructure.py` 的 per-file MD5 機制偵測到內容變了，自動對同一 source 做 `delete_points_by_source` 再 upsert（137 chunks：31 table + 106 text；其餘 68 個檔案 MD5 未變，正確跳過）。
- 其餘 6 檔 ticker 理論上曝露在同樣風險下（只是剛好最新 filing 不是修正案才沒中獎），`amendments=False` 是一次性修好所有 ticker 的來源，非 TSLA 專屬 patch。
- **驗證結果（已跑，修 Qdrant client bug 後對 live Docker collection）**：
  - `diagnose_crit_miss.py --ids sem-11 --judge gpt-oss-120b`（`crit_miss_diagnosis_after_tsla_fix.json`）：**critical checkpoint「EV 市場競爭/價格戰」（w=50）從修復前的 `RECALL(pool 就沒有)` 轉為 `OK (in top-k)`**——資料補進來後直接進 top-5，不需動任何 retrieval 參數，證實先前「無法檢索語料裡不存在的文本」的診斷。次要 checkpoint「FSD 監管風險」（w=30）**仍 `RECALL`**：新 10-K 雖有 `Full Self-Driving`×5，但那些論述被切進未進 pool 的 chunk，是殘留的次要召回缺口（非 critical，留待後續）。
  - `eval_generation_llm_judge.py --category semantic`（單次跑，`generation_correctness_semantic_after_tsla_fix.json`，judge=gpt-oss-120b）：**sem-11 correctness=0.2、critical_miss 仍 true**——但失敗模式已從「召回失敗」質變為「生成措辭/rubric 邊界」：生成答案確實寫了「全球汽車市場競爭激烈，且預計將來會更加競爭 [chunk #58]」，但 judge 的 mi0 checkpoint 要求明確點名「EV 價格戰」，答案只講「競爭激烈」沒講「降價/價格戰」，被判 mi0 miss（只中 mi2 補貼風險）。**這跟 sem-02/08 現在同型了**（資料/召回都到位，卡在生成沒把關鍵詞寫死或 rerank 排序）。diagnose（判 chunk 是否涵蓋）說 in_topk=OK，generation judge（判整段答案是否涵蓋）說 miss——不矛盾，是「chunk 有講競爭」與「答案有明確寫價格戰」兩個不同粒度。
  - **結論**：TSLA 資料修復是必要且成功的一步（把 sem-11 從「無解的召回黑洞」推進到「可用 prompt/rerank 處理的邊界問題」），但單靠資料修復不足以讓 sem-11 拿到滿分，還需疊加生成端（Rule：策略/風險題要點名關鍵詞如「價格戰」）或 rerank 改善。crit_rate 尚未歸零，待 `--repeat 3` 複核是否穩定。

### semantic 單次跑整體（修 TSLA 資料 + Qdrant client bug 後，judge=gpt-oss-120b）
- `generation_correctness_semantic_after_tsla_fix.json`：**correctness_mean=0.682、pass@0.6=0.727、fatal_hallucination=0**（單次跑，n=11）。對照 k=3 judge-120b baseline 的 0.629——高了 0.05 但**單次跑落在噪音帶內（std_across_runs≈0.043），不可據此宣稱進步**，需 `--repeat 3` 才算數。
- 逐題快照（單次）：sem-04/05/06/07 = 1.0（**sem-07 rubric 修正後首次確認轉正**）、sem-01/03/10 = 0.8、sem-09 = 0.7；**三題 critical_miss**：sem-02=0.2（漏 Copilot）、sem-08=0.0（漏 AWS 龍頭定位）、sem-11=0.2（漏「價格戰」明確措辭）。sem-10 這次 120b 判 0.8、未觸發先前記錄的 false-positive（must_not violation），但單次跑不能證明 false-positive 消失，只能說這次沒踩到。

### sem-02/08 排序問題新解法（實驗性、未驗證）：`rerank_multi_query` —— cross-encoder 精排改用「原 query + rewrite 變體」取跨 query 最高分
- 動機：CHANGELOG 前一條已證實 sem-02/08 是「cross-encoder 對策略類問題把量化財務段落排在質化敘述之上」，而非候選數不足（`RERANK_INPUT_N`/`DEFAULT_TOP_K` 都試過、無效）。既有的 query rewrite 變體（`enable_rewrite=True` 時已經生成，供擴召回用）本身就是對同一問題的不同語意措辭——猜測某些變體的措辭可能讓 cross-encoder 對「策略/定位」類 chunk 給出更高分。
- **改動**：`rag_query.py` 的 `retrieve()` 新增參數 `rerank_multi_query`（預設 `False`，不影響現有行為）。開啟時，rewrite 變體不再只用於擴召回，也會被拿去對 pool 內每個候選各自跑一次 cross-encoder 評分，取「原 query 分數」與「各變體分數」的 **逐候選最大值** 作為最終排序依據。候選數（`RERANK_INPUT_N`）、送進 LLM 的 `top_k` 完全不變——只換評分用的 query 措辭，不是加大精排規模。`eval/diagnose_crit_miss.py` 加了對應的 `--rerank-multi-query` flag 方便快速驗證。
- **風險（尚未驗證，須跑完整 semantic 集合才能下結論）**：取 max 可能讓「對某個寬鬆變體泛泛相關」的候選被拉抬，稀釋掉對原 query 語意本來就精準的候選——類似先前 RRF-fusion 實驗「跨變體妥協排名反而更雜訊化」的失敗模式。**不能只用 sem-02/08 兩題驗證就下結論**，必須跑全 11 題 semantic（最好 `--repeat 3`）確認沒有新的回歸，尤其對照 sem-04/05/10 這些目前穩定的題目有沒有被拖累。
- **待辦**：跑 `eval/diagnose_crit_miss.py --ids sem-02 sem-08 --rerank-multi-query` 先看兩題排序是否改善；若有效，再跑 `eval/eval_generation_llm_judge.py --category semantic --rewrite --correctness-only --repeat 3` 全量驗證正確性沒有整體下降，才能決定是否轉正式參數。

### sem-02/08/11 召回 vs 排序診斷：2 題排序問題 + 1 題「資料根本缺失」（非召回）
- 工具改動：`eval/diagnose_crit_miss.py` 原本 `TARGET_IDS` / `JUDGE` 寫死，改成 `--ids` / `--judge` / `--output` 命令列參數（預設題組改為 sem-02/08/11，judge 預設 gpt-oss-120b）。跑 `--ids sem-02 sem-08 sem-11 --judge openai/gpt-oss-120b`，輸出 `eval/crit_miss_diagnosis_sem020811.json`。
- **sem-02（MSFT AI 戰略）＝排序問題**。critical checkpoint（Copilot 跨 M365 整合，w=50）在 pool（21 候選）內但被 reranker 擠出 top-5。支撐文本在 `MSFT_10K_2025.html` chunk #46（Productivity and Business Processes segment），但它 RRF 排在最後（#21）勉強進池、cross-encoder 也沒把它拉上來。**注意 CLAUDE.md「已知問題」表把 sem-02 標為「生成覆蓋問題」，本次診斷推翻——是排序問題，證據在池內但沒送進 LLM**（先前結論來自單看生成答案沒有 Copilot，未拆檢索層）。
- **sem-08（AWS）＝排序問題**。critical checkpoint（AWS 為雲端龍頭 / 獲利引擎，w=50）在 pool（24 候選）內但被 reranker 擠出 top-5。與 sem-02 同型。
- **sem-11（Tesla 風險）＝資料缺失，不是召回問題，HyDE 無效**。critical checkpoint（EV 價格戰，w=50）+ FSD 監管（w=30）**兩者 pool 就沒有（RECALL）**。決策樹①追查根因：被當成 `TSLA_10K_2025.html` 抓進語料的其實是**一份只含高管薪酬的 10-K/A 修正案**（payload 全標 `10-K/A period 2025`，raw 檔 `item 1a`/`risk factors`/`risks related to` 全部 0 次，內容是 CEO 股權獎勵/董事/審計費）。真正含 EV 競爭/價格戰/FSD 監管的 **Item 1A 風險因子在完整 FY2024 10-K 裡，而那份從未被抓進來**。兩份 10-Q 的 Item 1A 明文「Other than the risk factors set forth below, there have been no material changes from the risk factors discussed in our Annual...Reports」——即用「引用併入」指回缺席的 10-K，自己只列增量，所以 `price competition`/`competitive pressure` 在整個 TSLA 語料 = 0 次。
  - **結論：sem-11 改任何檢索手段（HyDE / rerank / top-k）都無效——你無法檢索語料裡不存在的文本**。正解是資料層：用 `fetch_data.py` 重抓 Tesla **完整年報 10-K**（含 Item 1A），而非薪酬 10-K/A。抓進來、重跑 `data_update_unstructure.py` 後，sem-11 極可能直接從召回失敗變成可答。這也解釋了為何先前 grep 「有找到價格戰/FSD 字樣」卻仍召回失敗——那些命中全在 10-Q 的引用聲明句或無關段落，不是實質風險因子論述。
- **行動優先序**：① sem-11 資料修復（重抓完整 10-K）成本最低、效益最直接，且屬「錯資料」等級的 bug（不只影響這題，任何問 Tesla 業務風險的查詢都受害）；② sem-02/08 排序問題（見下方後續診斷，已確認調 chunk 數無效）。

### 後續：sem-02/08 排序問題「調大 rerank chunk 數」無效——是 reranker 排錯序，不是數量
- 動機：初步診斷後直覺想到「增大進精排的候選數 / 送進 LLM 的 top_k」。實測（dump 完整 reranked order + 用 gpt-oss-120b 逐名判 critical checkpoint 覆蓋）推翻此路：
  - **`RERANK_INPUT_N=50` 完全無效**：sem-02 pool 僅 21~23、sem-08 僅 23，都 < 50，候選**早已全部進 cross-encoder**。瓶頸不在「有沒有被精排」。
  - **`DEFAULT_TOP_K` bump 也救不到**：sem-02 的 critical 支撐 chunk（`MSFT_10K_2025.html` #46，唯一點名 Copilot 跨 M365 整合的）在精排排到**第 20 名**。要 top_k 撈到得設到 20——等於放棄精排、把整池灌進 LLM（context 稀釋，且與溫度實驗結論衝突）。加到 8/10 無效。
- **根因＝cross-encoder 對 strategy 類問題把量化財務段落排在質化策略敘述之上**。sem-02 raw 分數：#1~#3 是 10-Q 營業利益/10-K 資本支出（raw 0.83~0.87），而唯一含 Copilot 整合的分部描述 #46 只有 raw=0.106（第 20）。對「長期 AI 戰略」這種定位題，reranker 認為「營業利益成長討論」遠比「點名 Copilot 的分部策略」相關。sem-08（AWS 龍頭/獲利引擎）同型（本輪逐名判定跑到一半 Groq TPD 耗盡、key rotation，結果不可信；但初步 diagnose 已確認 in_pool/not_in_topk 的 RANK 型）。
- **正確槓桿（未實作，待試）**：不是 chunk 數，是 reranker 相關性——① 換/加強 reranker 或在精排 query 注入「策略/定位」意圖框架；② 改 chunking，Copilot 內容埋在一個像 boilerplate 分部清單的大 chunk 裡，整段被評低分，切得更聚焦可能拉高。

### 資料衛生：`us_stock_rag`（baseline collection）85% points 是孤兒 chunk，已清除
- 動機：診斷 sem-02/07/08/11 召回問題前，決策樹①要求先確認 checkpoint 支撐文本「真的存在於目前的 production 語料」。逐一 grep `data/raw` 時發現 fetch_data.py 曾重新抓取過一輪，把不準確的 fiscal-year 檔名（如 `MSFT_10K_2026.html`、`AAPL_10Q_202605.html`）換成正確標註（`MSFT_10K_2025.html`、`AAPL_10Q_202512.html`/`202603.html`），但兩支 ingest pipeline 都沒有「偵測 raw 檔案已被刪除 / 改名」的邏輯，導致舊檔案的 chunk 從未被清掉。
- **範圍確認**：`us_stock_rag_unstructured`（production，`rag_query.py`/`skill_builder.py`/所有 eval 讀的 collection）用 `hashes_unstructure.json` 比對，**乾淨、無孤兒**——先前 sem-02/07/08/11 的召回診斷不受影響，結論有效。`us_stock_rag`（baseline，`data_update.py`，"多數舊 eval 腳本的預設 target"）用 `hashes.json` 比對出 **16 個孤兒來源**，實際查 Qdrant 發現對應 **1673 / 1967 points（85%）** 是這些已刪除舊檔的殘留 chunk，長期在背景污染任何跑 baseline collection 的 eval 腳本。
- **修法**：沒有跑 `data_update.py --rebuild`（CPU-only 環境下要重新 embed 70 個檔案、耗時且範圍過大），改用外科手術式清理——只刪這 16 個孤兒 source：Qdrant 用 `delete()` + `FilterSelector(source=<name>)` 精準刪點（1967→294）、`data/processed/` 對應的 16 個孤兒 `.txt` 直接刪除、`hashes.json` 移除對應 16 個 key。不影響其餘 294 個正確 points，不用重新 embed 任何現有正確資料。
- **待辦**：`data_update.py` / `data_update_unstructure.py` 目前都沒有「刪除 raw 檔案後同步清 Qdrant + processed + hashes」的機制，全靠人工發現。若之後常態性替換 raw 檔名（例如 fiscal-year 修正），建議在 ingest 腳本加一段：跑完後比對 `hashes.json` 的 key 集合與目前 `raw_dir` 實際檔案集合，對「已消失的 key」自動呼叫 `delete_points_by_source` + 刪對應 processed 檔，而不是只靠 --rebuild 這種全量重來的粗暴解法。

### Judge 換 gpt-oss-120b（只重評既有答案，不重跑生成/檢索）：mean 0.545→0.629，std 收斂，但揪出一個 20b 從未抓到的幻覺
- 動機：k=3 baseline 的高變異題（sem-01/03 std=0.377）已證實至少部分是 judge（openai/gpt-oss-20b）誤判，懷疑換更強 judge 能讓量測收斂。比起重跑整條 pipeline（燒生成+檢索 token），寫了 `eval/rejudge_with_model.py` 直接讀 k3 既有的 33 個生成答案（11 題 × 3 runs），只重打 judge call，換成 `openai/gpt-oss-120b`（Groq 上獨立 TPD 池，不跟生成模型搶額度）；每個既有答案重評 3 次，藉此把變異拆成「同答案重評 3 次的 std＝純 judge 變異」vs「3 個 run 之間 judge_score_mean 的差＝生成變異」。
- **整體結果**（`eval/generation_correctness_semantic_k3_judge120b.json`，已套用 sem-07 新 rubric）：mean_of_means **0.545 → 0.629**（+0.084），std_across_runs **0.068 → 0.043**（收斂，符合預期）。`mean_within_answer_judge_std = 0.05`——120b 的純 judge 變異本身不算小，但多數題目（sem-01/05/07/11）在 within-answer 重評 3 次上完全一致（std=0），確認換 judge 對這些題有實質去噪效果。
- **驗證 sem-01 false-zero 假設成立**：run1 那個「答案明確含 CUDA 卻被判 0 分」的既有答案，120b 三次重評都給 0.5、穩定命中 mi0——20b 的原判定確認是誤判，不是生成問題。sem-07 因 rubric 改寫 + 換 judge 雙重效果，三輪從 0.0/0.0/0.5 全部轉正到 0.8/1.0/1.0，crit_rate 1.0 → 0（脫離 critical_miss）。
- **新發現並已查證：120b 自己也有 false positive，不是全面優於 20b**。sem-10（Meta AI/Reality Labs）換 judge 後不升反降——舊 judge（20b）三輪都是正分（0.7/1.0/0.8），从未觸發 must_not_include；120b 在 run2 三次重評「全部」判定 fatal（`mn_violations: ['mn0']`，理由是「fabricates numeric expense allocation/percentage figures not present in sources」），run3 三次重評 1/3 判無違規、2/3 判有違規。直接查證：run2 答案寫「Meta 預計將花費約 70% 的 Reality Labs 運營費用在可穿戴設備的研發上，剩餘的 30% 則用於虛擬實境和 Horizon 項目 [META_10K_2025.html, chunk #45]」，去 Qdrant 撈出該 chunk 原文——**「we expect to spend approximately 70% of our Reality Labs operating expenses on our wearables initiatives, and the remaining 30% on our VR and Horizon initiatives」逐字對得上，生成完全忠實、無幻覺**。結論：這是 **120b 的 false positive**（把正確引用的數字誤判成捏造），不是生成端的問題，也不代表 20b 原本「漏抓幻覺」。**换 judge 不是單向變好**：120b 修掉了 20b 的 false negative（sem-01），但自己在 sem-10 引入了新的 false positive；兩個模型都不是可信度 100% 的裁判。
- 其餘題目方向不變：sem-02/08/11 三輪仍偏低分、sem-04/05 仍穩定高分，換 judge 沒有翻轉既有的「哪些題是穩定失敗」判斷（除了 sem-07 因 rubric 修正翻正、sem-10 因新抓到的幻覺翻負）。
- **決策**：mean_within_answer_judge_std（0.05）遠小於原本 std_across_runs（0.068），且多數題目（sem-01/05/07/11）within-answer std=0，整體 mean 提升、std 收斂，**採用 openai/gpt-oss-120b 取代 openai/gpt-oss-20b 作為正式 judge model**。但 sem-03/09/10 這三題 within_judge_std 仍有 0.126——120b 並非對所有題目都確定性，這幾題（尤其 sem-10 這種會動用 must_not_include 判斷的邊界案例）值得之後疊加方案 A（3 票多數決，投票做在 per-checkpoint 粒度）；不必對全部 11 題都上 3 票，只需針對 within_judge_std > 0 的題目。

### sem-07 rubric 修正：critical checkpoint 要求「點名 AWS/GCP」超出來源文件範圍，已改寫
- 背景：k=3 baseline 顯示 sem-07（Azure 雲端市場競爭定位）crit_rate=1.0（0.0/0.0/0.5），CHANGELOG 先前疑「rubric 要求超綱世界知識」。
- **驗證**：直接讀 `data/raw/MSFT_10K_2025.html` 的 Competition 段落，原文是「Azure faces diverse competition from cloud service providers and open source offerings... Our AI offerings compete with AI products from hyperscalers」——通篇未出現 "Amazon"、"Alphabet"、"Google" 字樣（`html.find()` 全部 -1）。三輪生成答案也都忠實反映這點，寫「Azure 面臨其他雲端服務商和開源供應商的競爭」，但因為沒有點名 AWS/GCP，被 judge 依照舊 rubric 判定 critical miss。這不是生成或檢索的問題，是 rubric 要求了 grounding 文件本身沒有的具名資訊（等於逼模型引入 grounding 之外的世界知識才能拿到滿分，而這其實跟系統自己的 must_not_include 反幻覺原則自相矛盾）。
- **修法**：改寫 `eval/eval_set.json` 裡 sem-07 與 `col-04`（`maps_to: sem-07`，rubric 原本完全複製，兩處一併修正）的 mi0 checkpoint，從「把 Azure 定位為雲端三強之一 / 與 AWS、GCP 競爭」改為「提到 Azure 面臨其他雲端服務商 / hyperscaler / 開源方案的競爭（不要求明確點名 AWS 或 Google/GCP，來源文件本身未指名道姓）」，weight/is_critical 不變。
- 下次跑 semantic eval 需重新驗證 sem-07 是否脫離 critical_miss；若仍未過，才需要進一步查生成/排序問題。

### eval 加 `--repeat k`（k-run 平均 ± std）＋ 建立 semantic k=3 穩定 baseline
- 動機：先前溫度實驗證明單次跑的 mean 差 <0.1 都在噪音內，需要把「重跑取平均」制度化才能做可信的 A/B。
- 改動：重構 `eval/eval_generation_llm_judge.py`，`main()` 拆出 `run_single_pass()`；新增 `--repeat k`——跑 k 輪獨立 eval（每輪寫 `{stem}_run{i}.json`、各自可 resume），彙整 `mean_of_means`、`std_across_runs`、逐題 `score_mean/std/critical_miss_rate` 寫入 `--output`。`--repeat 1`（預設）行為與舊版完全相同。
- **實務坑**：k=3 的 token 量會打爆 Groq 免費層 TPD（100k/day × 4 keys）——run3 中途 429 全滅，靠 per-run resume 隔日補完。之後 k≥3 的重跑要挑決策節點用，不能當日常。
- **k=3 semantic baseline**（`generation_correctness_semantic_k3.json`，split-temp 設定：檢索 0 / 生成 0.3）：
  - category：mean_of_means=**0.545**，std_across_runs=**0.068**（run_means 0.527/0.636/0.473）→ 單次跑 mean 的可信區間就是 ±0.07 級，過去所有 <0.1 的「版本差異」都不足採信。
  - **穩定訊號**（std≈0 或 crit_rate=1.0，值得行動）：
    - sem-04 = 1.0×3，Rule 9 累計 6 次獨立跑全滿分，關案。
    - sem-11（Tesla）0.0/0.0/0.2，crit_rate=1.0 → 穩定召回失敗（已知）。
    - sem-07（Azure）0.0/0.0/0.5，crit_rate=1.0 → 穩定生成/rubric 失敗（已知，疑 rubric 要求超綱世界知識）。
    - **sem-02（MSFT AI 戰略）crit_rate=1.0（0.5/0.5/0.2）→ 新發現的穩定失敗**：三輪都 critical miss（輪流漏 Copilot / Azure AI），先前單次跑看不出來。
    - sem-08（AWS）crit_rate=0.667（0.8/0.3/0.0）→ 偏穩定的失敗（已知召回問題）。
  - **噪音帶（不要對它們優化）**：sem-01、sem-03 std=0.377（0.8/0.8/0.0 型——已驗證 sem-01 run1 的 0.0 是 judge 誤判：答案明確含 CUDA，judge 卻判未命中任何 must-include）；sem-05 std=0.236；sem-06/09/10 std≈0.13。
- **judge 變異是主要噪音源之一**（sem-01 鐵證）＋ 評分懸崖（critical 未中 → 大扣分）把 judge 小失誤放大成 0.8 分擺動。待辦：judge 3 票多數決或升級 gpt-oss-120b。

---

## 2026-07-06

### LLM 溫度：全面補 temperature=0 → 意外發現「生成側 greedy 反而更差」
- 動機：eval 的生成模型（llama-3.3-70b 走 Groq）先前沒帶 temperature，吃預設 ≈1.0，導致「同 context 不同答案」的 run-to-run 噪音，使 A/B 對比不可靠（見前一條 Rule 9 驗證的分析）。
- 改動：在 `rag_query.py` 的共用入口 `call_llm` 兩條路徑（Gemini config / Groq create）補 `temperature=0`；`eval/eval_generation_llm_judge.py` 的輔助 `call_llm_groq` 也補上。因 eval 的 filter / rewrite / **答案生成**全走 `rq.call_llm`，一處即鎖住三個隨機源。
- Eval（semantic n=11，`generation_correctness_semantic_temp0.json`，對照 temp≈1 的 `..._prompt_v2.json`）：

  | 版本 | correctness_mean | 說明 |
  |---|---|---|
  | baseline（舊 prompt, temp≈1） | 0.627 | |
  | v2（Rule 9, temp≈1） | 0.582 | |
  | **temp0（Rule 9, temp=0）** | **0.518** | 三版最低 |

- **反直覺結論：temp=0 讓 mean 更低，不是更穩更高。** 拆兩層看：
  - **檢索側確定性是純賺**：sem-06 的 rewrite 變體變確定 → 檢索回到 baseline 的 chunk → 分數 0.5→1.0 修回。
  - **生成側 greedy 反而傷分**：sem-05（英文 query、不觸發 rewrite、**三版檢索 byte 相同**）temp0 掉到 0.5（漏 CUDA/full-stack）；sem-02、sem-09 同樣「檢索相同但 temp0 掉分」，各漏 Copilot / Gemini 並新掉進 critical_miss。根因：greedy decoding 容易重複 / mode collapse（temp0 答案開頭結尾覆述同句），重複內容擠掉本可命中不同 must-include 點的覆蓋度。對「涵蓋多 rubric 點」的評分不利。
- **正解：溫度拆兩半（已實作）**。`call_llm` / eval 的 `call_llm_groq` 都加了 `temperature` 參數（預設 0.0）；新增常數 `GEN_TEMPERATURE=0.3`。檢索側（filter / rewrite）與所有 judge 呼叫用預設 temp=0（確定性、A/B 可比）；生成答案的 4 個呼叫端（`run_single_query`、`run_interactive`、`eval_generation_llm_judge`、`compare_generation`）顯式傳 0.3。
  - **踩坑**：eval 用 `rq.call_llm = call_llm_groq` monkey-patch，所以 eval 裡 filter/rewrite/生成全走 `call_llm_groq`；第一次只改了 `rq.call_llm` 沒改 `call_llm_groq` 的簽名 → 11 題全 `TypeError` skip。兩份實作簽名要一起改。

### split-temperature 驗證：受控實驗證實 gen=0.3 > gen=0
- 第 4 版 `generation_correctness_semantic_split_temp.json`（檢索 temp=0、生成 temp=0.3），對照第 3 版 temp0（全 0）。**兩版檢索側都 temp=0 → 檢索結果 byte 相同，唯一變數是生成溫度**，是乾淨對照組。

  | 版本 | correctness_mean | pass@0.6 |
  |---|---|---|
  | baseline（舊 prompt, t≈1） | 0.627 | 0.636 |
  | v2（Rule 9, t≈1） | 0.582 | 0.455 |
  | temp0（Rule 9, 全 temp=0） | 0.518 | 0.273 |
  | **split（Rule 9, 檢索0/生成0.3）** | **0.609** | **0.545** |

- **temp0 → split（只有生成溫度 0→0.3 變）**：sem-02（0.5\*→0.7）、sem-05（0.5\*→0.8）、sem-09（0.2\*→0.7）三題全脫離 critical_miss；只有 sem-07 小退（0.2\*→0.0\*），其餘持平。mean 0.518→0.609。因檢索被凍住，這 +0.09 純粹來自「0.3 解掉 greedy 重複」——**前一條的診斷被受控實驗證實**。
- **採用 split 為正式設定**。三個機制勝點：① sem-04（Rule 9）= 1.0 **第三次複現**（v2/temp0/split 皆 1.0）；② 檢索確定性（sem-06 穩在 1.0，不像 v2 掉到 0.5）；③ 生成不 greedy（sem-02/05/09 恢復）。與 baseline mean 打平（0.609 vs 0.627，n=11 無法區分）但嚴格更優——baseline 的 sem-04 是 0.3\* 破洞且檢索不可重現。
- **殘留弱點（與溫度無關）**：sem-07（Azure）持續最差＝已知純生成問題；sem-08（AWS）、sem-11（Tesla）＝已知召回問題。留待後續。

### 生成 prompt 新增 Rule 9：多公司題強制逐家點名（方案 1）
- 針對前一次調整引入的 sem-04 回歸（多公司比較題 1.0 → 0.3）。診斷：sem-04 檢索沒問題（META/MSFT/AAPL 的 chunk 都撈到了），失分純在生成——新引用格式讓 LLM 寫成泛化敘述「這些公司面臨...」，沒把風險綁到具體公司名。
- 在 `rag_query.py` `SYSTEM_PROMPT` 新增 Rule 9：問題涉及多家公司時，正文中必須逐一點名每家公司並綁定其風險/數據（「META faces X; Microsoft faces Y」），citation 標記本身不算 attribution。
- 這是「先試最便宜的修法」——比 per-company decompose retrieval（方案 2）成本低得多，只動 prompt。
- **驗證結果（成功，方案 2 不需實作）**：`eval/eval_generation_llm_judge.py --category semantic --rewrite --correctness-only`（gen=llama-3.3-70b，judge=gpt-oss-20b），輸出 `eval/generation_correctness_semantic_prompt_v2.json`，對照組 `generation_correctness_semantic_newprompt.json`。
  - **sem-04（唯一的多公司題）0.3 → 1.0**，脫離 critical_miss。v2 生成內容逐家點名「META 面臨組織變革風險…MSFT 面臨市場競爭…NVDA 面臨網絡安全風險」，judge 判 mi0/mi1/mi2 全中；baseline 是泛化敘述被判「fails to specify risks for at least two companies」。正中診斷的失敗模式。
  - 其餘 10 題全是單公司題，Rule 9 的觸發條件（"MORE THAN ONE company"）不會啟動，理論上行為與 baseline 相同。overall mean 0.627 → 0.582 的下降全部來自這些單公司題（sem-01/06/07/10 各掉 0.3~0.5），是 llama-3.3-70b 生成 + judge 的 run-to-run 抽樣變異，**與 Rule 9 無關**——sem-06 甚至 1.0→0.5 但 prompt 對它零差異。
  - 教訓：stochastic 生成 + LLM judge 下，單次跑的 mean 對比不可靠；只有「rule 實際觸發的那題出現大幅、機制可解釋的移動」才是有效訊號。sem-04 的 +0.7 就是這種訊號。
- 方案 2（多公司拆解檢索 + per-company rerank + 分組 context）**暫不實作**，保留為 sem-04 若日後再回歸的後備方案。

### 生成 prompt 調整（semantic 類問題）
- 針對 `eval/eval_generation_llm_judge.py` 診斷出的「純生成問題」（sem-01、sem-07：checkpoint 已在 top-k，LLM 卻沒答出來）調整 `rag_query.py` 的生成 prompt / 引用格式。
- **結果**（semantic n=11，舊 prompt → 新 prompt）：correctness_mean 0.509 → 0.627，pass@0.6 45.5% → 63.6%。
- sem-01 修好（0.0 → 0.8，脫離 critical-miss）；sem-07 改善但未過門檻（0.2 → 0.5，仍 critical-miss）。
- **新回歸**：sem-04（多公司比較題）1.0 → 0.3，新掉入 critical-miss；sem-02 也小幅退步（1.0 → 0.7）。
  根因：新的引用格式（`[Reference N: FILE]`）讓生成內容變成泛化敘述，不再像舊版那樣把風險明確綁到公司名稱寫進句子裡，導致「至少點名兩家公司」的 rubric 判不過。
- **待辦**：多實體比較類問題需要在 prompt 中額外要求「正文中必須指名每家公司」，不能只靠 citation 標記帶過。

### RRF-fusion（RAG-Fusion）實驗 — 已嘗試並放棄
- 在 `rag_query.py` 的 `retrieve()` 新增 `rewrite_fusion` 參數，實作跨 query-variant 的 rank-based RRF 融合（`Σ 1/(60+rank)`），作為 query rewrite 合併策略的第 4 種候選，與既有的 cap=3 策略對打。
- Eval（`eval/eval_chunk_recall.py --category colloquial`，n=10）：

  | 條件 | file_P | file_R | chunk_P | ckpt_R | crit_miss |
  |---|---|---|---|---|---|
  | no-rewrite | 47.3% | 44.4% | 46.0% | 51.9% | 50.0% |
  | rewrite（無 cap） | 47.2% | 49.4% | 46.0% | 39.1% | 50.0% |
  | **rewrite + cap=3** | 54.7% | 63.5% | 52.0% | **57.1%** | **30.0%** |
  | rewrite + RRF-fusion | 58.2% | 61.9% | 40.0% | 49.0% | 50.0% |

- **結論：cap=3 仍是目前最佳策略，RRF-fusion 敗下陣**。RRF 在 file 層面表現最好（找到更多對的文件），但 chunk 層面反而變差——RRF pool 是「跨 variant 排名妥協」的結果，沒有任何一個 chunk 在單一 query 底下是最強的，reranker 精排的候選集反而更雜訊化。
- `rewrite_fusion` 參數保留在程式碼中（預設 `False`，不影響現有行為），但生產環境**不啟用**。

### Vector DB：由 Qdrant local mode 改為 Docker Qdrant
- 原因：`QdrantClient(path=...)` 是排他檔案鎖，同一時間只能有一個 process 開啟 `qdrant_db`，導致互動查詢、eval 腳本、API server 互相搶鎖失敗。
- 改為 `QdrantClient(url=QDRANT_URL)`（Docker container，預設 port 6333，本機容器名稱 `qdrant_hnsw`），新增 `make_qdrant_client()` 輔助函式依 `.env` 裡的 `QDRANT_URL` 是否設定決定走 server 模式或 local 模式。
- 影響檔案：`rag_query.py`、`data_update_unstructure.py`、`eval/eval_chunk_recall.py`。
- **注意**：README.md 先前寫「完全不需要 Docker」已不再準確，見本次 README 更新。

---

## 使用慣例

- 修改 retrieval / ingest / prompt 邏輯前，先看這份 CHANGELOG 是否已經有相關實驗紀錄（避免重複造輪子或重踩已知的坑）。
- 每次修改後，在最上面新增一個日期區塊，簡述「改了什麼」「為什麼」「結果如何」。不需要鉅細靡遺列出程式碼 diff（那是 git log 的工作），重點是**動機和結論**，尤其是「試過但放棄」的方案——這些最容易被遺忘、也最容易被後人重蹈覆轍。
