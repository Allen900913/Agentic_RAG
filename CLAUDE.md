# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 修改規範（重要）

**每次對 retrieval / ingest / prompt / eval 邏輯做出有意義的修改後，必須把「改了什麼、為什麼、結果如何」寫進 [`CHANGELOG.md`](CHANGELOG.md)**（新條目加在檔案最上面）。這包含：
- 參數調整（例如 `FETCH_N`、`RRF_TOP_N_PRIMARY`、`VARIANT_CAP` 等常數變更）
- 新增或修改 prompt（生成 prompt、query rewrite prompt、query filter prompt）
- 架構調整（例如換 Vector DB 連線模式、換 embedding/rerank 模型）
- **嘗試過但放棄的方案**，尤其要記錄——這些最容易被遺忘，也最容易被後人重新嘗試一次而重踩已知的坑
- 若有 eval 數據佐證修改效果（前後對比），附上關鍵數字

不需要寫成 git commit message 等級的細節（那是 `git log` 的工作），重點是動機與結論。修改前也應該先查 `CHANGELOG.md` 有沒有相關的既有實驗紀錄。

### 「已試無效」條目要附「復活條件」

每筆「嘗試過但放棄的方案」除了記錄死因，還要補一行**復活條件**：哪個假設一旦改變，這個負面結論就可能失效。區分兩種死因：
- **機制型**（與系統其他部分無關，例如「孤立段落沒有上下文，cross-encoder 評分結構性偏低」）→ 通常沒有復活條件，永久死路。
- **交互型**（死因綁定某個特定元件的當時行為，例如「rewrite 變體當時是中文，沒解決 cross-lingual 問題」）→ 復活條件就是那個元件的行為。

**每次對 retrieval/ingest/prompt 做出會改變其他元件行為的修改後**，掃一遍 CHANGELOG 裡「已試無效」清單的復活條件：命中的才需要用最小成本重測（單題 `diagnose_crit_miss.py`，不必跑全量 eval）；沒命中的維持死路狀態，不必照表重跑。同一原則反向適用於「已採用」的正面結論——它們同樣綁定當時的 baseline，baseline 大幅變動後也該視情況重驗，不能預設繼續有效。

### 診斷/eval 腳本的 `model_name` 陷阱

`retrieve()` 內部有 LLM 呼叫（`parse_query_filters` / `rewrite_query` / `translate_query_to_english`），這些呼叫用的 `model_name` **必須跟生產（`rag_query.DEFAULT_MODEL`）或主要 eval 腳本的 `--gen-model` 一致**，否則量到的是「使用者實際不會走到」的檢索路徑，診斷結論無法外推到生產行為（見 CHANGELOG 2026-07-08 `diagnose_crit_miss.py` 的 bug：曾誤用 judge model 跑檢索側，讓 sem-02 的診斷比生產實況樂觀）。judge model（評分用）與 retrieval/gen model（檢索與生成用）永遠是兩個獨立參數，新增診斷腳本時不要圖方便共用一個變數。

## What this is

A personal RAG system for US tech-stock intelligence (AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA).

Pipeline overview:
```
fetch_data.py          → data/raw/          (原始資料爬取)
data_update_unstructure.py → qdrant_db/    (生產用 ingest：表格/文字分離)
data_update.py         → qdrant_db/        (baseline ingest：純文字 SemanticChunker)
rag_query.py           ← 生產查詢介面      (retrieve + generate)
skill_builder.py       → skill.md          (4 個全局 RAG 查詢 → 摘要)
api_server.py / app.py ← Web API / UI      (選用)
```

`README.md` 是設計決策的權威文件，修改 retrieval 或 ingest 前必讀。

---

## 目前使用的關鍵參數（rag_query.py）

```python
COLLECTION_NAME   = "us_stock_rag_unstructured"   # 生產 collection
FETCH_N           = 60      # 每路 prefetch 候選數（送進 server-side RRF）
RRF_TOP_N_PRIMARY = 20      # RRF 回傳候選數（新參數，舊值=40）
VARIANT_CAP       = 8       # query rewrite 每個變體最多注入幾個新候選（新參數，舊值=5）
RERANK_INPUT_N    = 50      # cross-encoder 輸入上限（只在 rewrite_fusion=True 這條實驗路徑生效；
                             # 預設 cap-based merge 路徑池子上限 20~33，永遠到不了 50）
DEFAULT_TOP_K     = 5       # 最終送進 LLM 的 chunk 數
GEN_TEMPERATURE   = 0.3     # 生成答案溫度；filter/rewrite/judge 一律 temp=0（call_llm 預設）
DEFAULT_ENABLE_REWRITE     = True   # 生產入口（CLI/api_server）預設開 rewrite（2026-07-13 轉正）
DEFAULT_TRANSLATE_QUERY_EN = True   # 生產入口預設開「雙 query 取最高分」rerank（同上）
```
> `retrieve()` 函式本身的 `enable_rewrite`/`translate_query_en` 參數預設仍是 `False`（eval 腳本向後相容）；上面兩個常數只套用在生產入口。

溫度分工（2026-07 實驗定案，見 CHANGELOG）：檢索側（filter/rewrite）與 judge 必須 temp=0（確定性、A/B 可比）；生成答案用 0.3——**不可用 0**，greedy 會重複/mode collapse 而漏 rubric 點。

Pool 機制：`pool = primary_20 ∪ (每個 rewrite 變體 top-8，去重)` → pool 實際大小 20~33 不等（完全重疊時不增加）。

---

## Commands

環境初始化（從 repo root 執行）：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # 填入 API keys
```

### 核心 pipeline

```powershell
# 生產資料 ingest（表格/文字分離，us_stock_rag_unstructured）
python data_update_unstructure.py --rebuild

# 增量更新（只重跑 MD5 變更的檔案）
python data_update_unstructure.py

# 互動查詢
python rag_query.py
python rag_query.py -q "NVDA gross margin?" -k 5 -m gemini-2.5-flash

# 生成 skill.md
python skill_builder.py -o skill.md -m gpt-oss:20b

# 擴充原始資料
python fetch_data.py --tickers NVDA --news-count 3 --skip-sec
```

### Eval（從 repo root 執行）

```powershell
# 主要三支：目前實際使用的 eval pipeline
python eval/eval_generation_llm_judge.py --limit 20 --rewrite --correctness-only \
    --gen-model llama-3.3-70b-versatile --judge-model qwen/qwen3-32b \
    --output eval/generation_correctness_new_params.json

# 做 A/B 決策時務必加 --repeat 3（k-run 平均 ± std；單次跑 mean 差 <0.1 都是噪音）。
# 注意：k=3 會吃掉整天的 Groq TPD 額度（100k/day × 4 keys），挑決策節點用
python eval/eval_generation_llm_judge.py --category semantic --rewrite --correctness-only \
    --gen-model llama-3.3-70b-versatile --judge-model qwen/qwen3-32b \
    --repeat 3 --output eval/generation_correctness_semantic_k3.json

python eval/eval_two_stage.py --rewrite \
    --output eval/results_new_params.json

python eval/diagnose_crit_miss.py   # 逐 checkpoint 拆「召回 vs 排序」問題
```

---

## Eval

主要三支腳本：`eval_generation_llm_judge.py`、`eval_two_stage.py`、`diagnose_crit_miss.py`。
腳本清單、結果檔說明、標準答案位置見 [`eval/EVAL_GUIDE.md`](eval/EVAL_GUIDE.md)。

---

## Architecture

### Single-model dense+sparse embedding（核心設計）
`BAAI/bge-m3`（via `FlagEmbedding.BGEM3FlagModel`）一次 `encode()` 同時輸出 1024-D dense vector 和 sparse lexical-weight dict（`token_id → weight`）。這是整個系統用單一 embedding model 做 hybrid retrieval、不另接 BM25 index 的原因。首次下載 ~2.3GB，之後離線。

### Qdrant collection layout（local embedded mode，無 Docker）
`QdrantClient(path="./qdrant_db")`。每個 chunk 是一個 point，帶兩個 named vector（`dense` cosine、`sparse`）加 payload（`source`, `chunk_index`, `document`，HTML filings 另有 `filing_type`/`fiscal_year`/`fiscal_period`/`chunk_type`）。

Retrieval 流程：
1. 兩路 Prefetch（dense / sparse），server-side `FusionQuery(Fusion.RRF)` 融合 → **pool**（精排前 20~33 個）
2. `BAAI/bge-reranker-v2-m3` CrossEncoder 精排 → **top-k**（送進 LLM 的 5 個）

### Two parallel collections

| Collection | Builder | 用途 |
|---|---|---|
| `us_stock_rag_unstructured` | `data_update_unstructure.py` | **生產**：用 `unstructured` 分離表格（`chunk_type="table"`）與敘述（`chunk_type="text"`）。`rag_query.py` / `skill_builder.py` 讀這個。|
| `us_stock_rag` | `data_update.py` | Baseline：SemanticChunker 純文字，無表格分離。多數舊 eval 腳本的預設 target。|

`DENSE_VECTOR_NAME` / `SPARSE_VECTOR_NAME` 常數在 `data_update*.py`、`rag_query.py`、`skill_builder.py` 中重複定義，必須保持一致。

### LLM provider split（重要陷阱）

| 用途 | Provider | 預設模型 | Key |
|---|---|---|---|
| `rag_query.py` 生成 | Google GenAI SDK | `gemini-2.5-flash` | `GEMINI_API_KEY` |
| `skill_builder.py` 生成 | LiteLLM endpoint（課程用） | `gpt-oss:20b` | `LITELLM_API_KEY` / `LITELLM_BASE_URL` |
| Eval gen/judge | Groq（4 key rotation） | `llama-3.3-70b-versatile` / `qwen/qwen3-32b` | `GROQ_API_KEY` ~ `GROQ_API_KEY4` |

Groq 可用模型（2026-07 實測）：`llama-3.1-8b-instant`, `llama-3.3-70b-versatile`, `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3-32b`, `meta-llama/llama-4-scout-17b-16e-instruct`。**`mixtral-8x7b-32768` 已下架，勿使用。**

### Groq 4-key rotation（eval 腳本內建）
`eval_generation_llm_judge.py` 和 `eval_chunk_recall.py` 偵測到 TPD exhausted 錯誤時自動輪換 `GROQ_API_KEY` → `GROQ_API_KEY2` → `GROQ_API_KEY3` → `GROQ_API_KEY4`。

### Query-understanding hard filter（`rag_query.py` 專有）
Retrieval 前，`parse_query_filters()` 呼叫 LLM 萃取 filing 條件（fiscal year/period/type/ticker，含 include/exclude 極性）→ Qdrant `must`/`must_not`。零結果時自動 fallback 退 tier 重試（strict → 寬鬆 → 無 filter）。這是防止錯期 chunk 污染答案的核心機制。

### Query Rewrite + Translate-EN rerank（2026-07-13 已轉生產預設）
`enable_rewrite` 對非標準查詢生成 2 個語意變體，各取 top-`VARIANT_CAP`(=8) 個候選後去重合入主池（英文 / ticker 格式的查詢通常不生成變體，"query already standard"）。`translate_query_en` 讓 cross-encoder rerank 對每個候選**同時用原句與英文翻譯各評一次分、取逐候選最高分**（解 cross-lingual rerank 失真，見下方「已知問題」sem-09/sem-11）。

**生產預設**：`DEFAULT_ENABLE_REWRITE = True` / `DEFAULT_TRANSLATE_QUERY_EN = True`（rag_query.py 模組常數）。CLI 不帶 flag 即開啟，用 `--no-rewrite` / `--no-translate-query-en` 關閉；`api_server.py` 也已對齊。**注意 `retrieve()` 函式本身的參數預設仍是 `False`**（保持 eval 腳本向後相容）——只有生產入口（CLI / api_server）預設打開，eval 腳本仍需顯式傳 `--rewrite --translate-query-en`。

### Idempotency
`data_update*.py` 以 per-file MD5（`hashes.json` / `hashes_unstructure.json`）決定是否重跑。Point ID 為確定性 `uuid5(NAMESPACE_DNS, "<stem>_c<i>")`，re-ingest 同 chunk 產生同 ID。Changed file 先 `delete_points_by_source` 再 upsert。`--rebuild` 清空 `data/processed/`、hashes 檔、collection。

---

## 已知問題與分析（更新至 2026-07-12）

判讀原則：**crit_rate=1.0（k 次全 critical miss）才是穩定失敗、值得修**；std 大的題是 judge/生成噪音，**禁止對它們優化**（會 fit 到噪音）。做 A/B 必須 `--repeat 3` 且看逐題 crit_rate 而非單次 mean（單次跑 mean 差 <0.1 一律視為噪音）。

### semantic（11 題）

07-11 加入 rewrite 語域轉換規則；07-12 先用 E5 驗證「全有全無替換」版的 `translate_query_en` 發現 sem-09 退步（見下方「E5」腳注），接著把 `translate_query_en=True` 的實作改成「rerank 對每個候選同時用原始 query 與翻譯 query 各評一次分、取逐候選最高分」（而非整組替換），重新跑 k=3 全量驗證：
- `sem-09`（Google AI/搜尋策略）：**完全修好**——crit_miss_rate=0.0，mean=1.0（3/3 滿分）。舊版全有全無替換會把關鍵 10-K chunk（#139）擠出 top-5，新版讓每個候選各自取較高分後兩個關鍵 chunk（#139/#179）都留住。
- `sem-11`（Tesla）：**修復維持**——crit_miss_rate=0.0，mean=0.7（price-reduction chunk #67 穩定進最終答案），未因換了 translate_query_en 的實作而退步。
- `sem-08`（AWS）：**變好但未穩定**——crit_miss_rate 從 1.0 降到 0.333（scores=[0.7, 1.0, 0.2]）。根因已於 07-12 查明是**生成層問題**，非語言/語域問題：讀 E5 的三次已存答案發現，含 AWS 獲利內容的 chunk（AMZN #100）**穩定進 top-5，但三次答案都完全沒引用它**（只引用其他四個更「乾淨」的 chunk）——#100 混雜 FTC 訴訟/稅務等大量不相關內容，目標句子只是其中一個從屬子句，生成模型選擇性忽略。這次改動沒有針對這個病灶，2/3 過關的改善可能只是候選組成變化的副作用，不能當作穩定修好。
- `sem-04` 已修（Rule 9，穩定滿分），`sem-02`/`sem-07` 已由 rubric/生成端修法解決。

**`enable_rewrite`/`translate_query_en` 已於 2026-07-13 轉生產預設**（CLI 與 api_server 都開啟，見上方「Query Rewrite + Translate-EN rerank」與 CHANGELOG 07-13）。驗證乾淨：lexical 1.0、mixed 0.897、semantic 0.842±0.026、colloquial 表面 0.67（逐題拆解無新回歸）。**殘留風險**：sem-08 仍有 1/3 crit_miss（生成層問題，非本次修法能解），這是轉正時接受的已知殘留，不是新引入的問題。

### colloquial（10 題）

已測過新版 `translate_query_en`（k=3，`eval/e6_colloquial_dualquery_k3.json`）：表面 mean 0.67（低於 07-10 baseline 0.727，但兩者 judge model 與是否套語域規則都不同，非乾淨對照）。逐題拆解後**確認沒有新回歸**：
- `col-08`（Tesla 鏡像）：crit_miss_rate=1.0——已知接受極限（見 07-12「col-08 文件化」），不受本次修法影響。
- `col-04`：crit_miss_rate=0.667，跟 07-10 baseline 一樣，既有灰色地帶，非新問題。
- `col-05`（AWS 鏡像）：crit_miss_rate=0.333，跟 07-11 已記錄的 rewrite 變體不穩定一致，非新問題。
- `col-10`（`maps_to: sem-09`）：crit_miss_rate=0.667，**查證後是已知的 judge OR 邏輯 bug（`col10_or_logic`）**——三次跑的 retrieval 結果與答案內容完全一致（都正確涵蓋「AI Overviews/AI Mode 嵌入搜尋」），只有 judge 對 mi0 的逐票判定在三次跑之間反覆橫跳，是測量問題不是系統問題（詳見 CHANGELOG 07-12）。

### 評分基準（各自最近一次全量測得，設定不完全一致，見各自 CHANGELOG 條目）

| 指標 | 值 | 條件 |
|---|---|---|
| lexical correctness | 1.000 | k=1，`--rewrite --translate-query-en`（新版），數字查詢全對 |
| mixed correctness | 0.897 | k=1，同上設定，fatal=0，無回歸 |
| semantic correctness | 0.842 ± 0.026 | k=3，同上設定（新版 `translate_query_en`）；sem-09 完全修好、sem-11 維持、sem-08 改善但未穩定 |
| colloquial correctness | 0.67 ± 0.085 | k=3，同上設定；表面低於舊 baseline，但逐題拆解後無新回歸（見上） |
| ckpt_R (chunk recall) | 0.7307 | rewrite+no-cap，n=20，單次跑 |

> **量測紀律**：semantic 單次跑的 mean 在 0.47~0.64 間晃，噪音來源：生成抽樣（temp=0.3，已知代價，split-temp 設計下的必要成本）＋ judge 變異＋評分懸崖放大（critical 未中 → 自由落體）。judge 已於 07-12 從 `openai/gpt-oss-20b` 換成 `qwen/qwen3-32b`（回歸套件 10/11，優於 20b 的 9/11，且不與 gen_model 共用 TPD 池），`--judge-votes 3` 已是標準做法。
>
> **待辦**：① sem-08 的稀釋型 chunk 問題需要語域規則以外的修法（chunk 邊界或更精準的變體）；② sem-09 的 `translate_query_en` 副作用需要判定是生成沒寫清楚還是 judge OR 邏輯誤判（重判已存答案即可，成本低）；③ colloquial 版 E5 尚未跑。

---

## Conventions

- `data/raw/` 檔名編碼 metadata：`TICKER_TYPE_DATE`（如 `NVDA_News_20260519_01.txt`、`AAPL_10Q_202605.html`）。`infer_source_type()` 解析 type；`extract_filing_metadata()` 優先讀 XBRL `dei:` tags、fallback 到 filename regex。`eval_set.json` 的 `relevant_sources` 用 glob pattern（如 `NVDA_News_*.txt`），新增日期版本不需修改 eval set。
- **Bilingual**：docstrings 和 comments 用繁體中文，identifiers 和 log messages 用英文。修改時維持這個慣例。
- eval 腳本全部從 **repo root** 執行（腳本內用相對路徑 `eval/eval_set.json`）。
- `eval/` 目前**未被 git 追蹤**（`git ls-files eval/` 為空）。腳本和標準答案若重要，應 `git add eval/` 納入版控。
