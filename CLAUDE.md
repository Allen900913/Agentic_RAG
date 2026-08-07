# US Stock RAG — 檔案地圖（reference：用到哪塊才去看那個檔）

> **維護規則（改任何檔案前必讀）**：這份檔案地圖是本專案的 reference。**只要你要改動任何檔案，就要先檢查這份地圖是否因此過時**——例如新增/刪除/更名檔案、改變某檔用途或角色（生產⇄棄用）、換 LLM/collection、改變資料流或路徑（如把 `data/raw` 分成子目錄）。**只要地圖有任何一格對不上實況，就要在同一次改動裡把它重新生成/更新到一致**，別讓程式碼與地圖漂移。

**生產 collection**：`us_stock_rag_edgar_exp4`（EDGAR 抽取 → chunk → BGE-M3 dense+sparse hybrid → RRF → cross-encoder rerank）。
**預設 LLM**：NVIDIA NIM `openai/gpt-oss-120b`；模型名 `gemini-*` 開頭走 Gemini（`rq.call_llm` 依名稱路由）。
> 一次性診斷腳本（`eval/_*.py`、`*_probe*`、`* copy.py`）不列於此表；那些是拋棄式實驗，別當生產碼。

## 生產／查詢
| 檔案 | 用途 | LLM |
|---|---|---|
| [`rag_query.py`](rag_query.py) | CLI 查詢入口（`python rag_query.py -q "..."`）。核心 `retrieve()`（hybrid+rerank，支援 `full_translate_en` 檢索中間層全英文）、`call_llm()`、`build_user_prompt()` 也被其他腳本 `import rag_query as rq` 共用。`COLLECTION_NAME` 在此定義 | NVIDIA NIM `gpt-oss-120b`；`gemini-*` 走 Gemini |
| [`api_server.py`](api_server.py) | FastAPI 後端，包住 `rag_query.py`（`/chat` SSE 串流） | 沿用 `rq.call_llm` |
| [`app.py`](app.py) | Streamlit 聊天前端，串接 `api_server.py` 的 SSE | — |

## Agentic（LangGraph 多節點版，生產級 agentic 入口）
| 檔案 | 用途 | 模型分層 |
|---|---|---|
| [`agentic_rag_v2.py`](agentic_rag_v2.py) | **現役** agentic RAG（2026-07-26 重構）。LangGraph Supervisor 管線：Plan（拆平行子問題）→ Execute（**確定性** `_retrieve_chunks`，無 Agent A LLM）→ Grade（`_check_sufficiency` 判足夠+targeted 改寫）→ Synthesize（單次生成 + citation validator + reflect）。時間感知走雙軸 `freshness_mode`：`snapshot`（eval，「最新」= KB coverage 掃出的最新資料，不看 wall clock）／`live`（prod，看 `_get_as_of_date`，可用 `AGENTIC_AS_OF_DATE` 固定） | RETRIEVAL=`gpt-oss-20b`、CHECKER/GEN=`gpt-oss-120b`（皆 NVIDIA，env `AGENTIC_*_MODEL` 可覆蓋） |
| [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md) | agentic 變更史＋知識庫附錄（eval 量尺、檢索層決策、診斷結論） | — |

## Ingest／Migration（要重建語料或改 payload 時才看）
| 檔案 | 用途 |
|---|---|
| [`data_update_edgar.py`](data_update_edgar.py) | **唯一 ingest 執行入口**：edgartools 抽 SEC filing → chunk → 寫入獨立 EDGAR collection（`--collection`，生產為 `us_stock_rag_edgar_exp4`；chunking 世代演進見 memory `edgar-chunking-pipeline-experiments`）。四種 doc_type 都由本檔處理：10-K/10-Q 走 edgartools，News/Fundamentals/IncomeStatement 的 `.txt` 借用 `unstructured_components` 的組件。**增量策略三軌**：① `.txt` MD5 快取（`hashes_edgar.json`，per-collection 記帳，未變則跳過 embed；`--force-txt` 繞過）② SEC filing 不快取（申報後內容不變、且要 API 抓回才算得出 hash），維持 delete-by-source 重寫 ③ Fundamentals/IncomeStatement keep-latest：只 ingest 最新快照，且**主動刪除過期快照的殘留 chunk**（保證庫裡只有一份；靜態財報 10-K/10-Q 則允許多期並存供查歷史） |
| [`unstructured_components.py`](unstructured_components.py) | **函式庫，非執行入口**（前身 `data_update_unstructure.py`，2026-08-07 剝除 CLI/Qdrant 寫入/hash）。提供 unstructured 解析與 chunking 組件：`partition_and_clean`、`build_chunk_records`、表格處理（`table_element_to_text`／`_find_caption`／`_llm_summarize_table`）、`extract_filing_metadata`、`BGEM3DenseEmbeddings`。只被 `data_update_edgar.py` import |
| [`fetch_data.py`](fetch_data.py) | 抓原始資料（新聞/財報數據）供 ingest 使用 |
| [`data_update.py`](data_update.py) | 最舊的 ingest 管線（MD5 增量＋`hashes.json`），已被 EDGAR 版取代，保留對照 |

### 重建生產 collection 的正確指令（2026-08-07 補記）
```bash
.venv/Scripts/python.exe data_update_edgar.py --rebuild --rcts-fallback --collection us_stock_rag_edgar_exp4
```
**`--rcts-fallback` 不可省，但預設是關的**——這是最容易踩、且先前沒有任何文件記載的坑。漏掉它會得到一個實質劣化的索引：實測 3272 vs 3673 chunks（少 12%），且 **7.4%** 的 text chunk 超過 RCTS 的 1200-token 補切門檻（開啟時僅 0.1%），超出部分在 rerank 階段會被 `RERANK_MAX_LENGTH=2048` 截斷、內容等於看不到。

判定 exp4 當初有開的依據是 **token 分佈**：exp4 的 text chunk 最大 1208 token、>1200 者只有 4 個，精準卡在門檻上，是 RCTS 開啟的明確特徵。
> ⚠ **別用字元數推 token 數**：exp4 有 6898 字元的 chunk，直覺會換算成 ~1700 token 而誤判成「沒開 RCTS」，實測卻只有 1208 token（BGE-M3 多語 tokenizer 對英文約 **5.7 字元/token**）。2026-08-07 就是這樣推錯一次、用錯旗標跑掉一次完整重建。要判斷就載 `BAAI/bge-reranker-v2-m3` 的 tokenizer 實際數。

### 語料目錄結構（2026-08-07 重整）
`data/raw/` 已分成三類子目錄，**根目錄不再有任何檔案**：

| 目錄 | 內容 | ingest 行為 |
|---|---|---|
| `Filings/` | 10-K/10-Q `.html` | 舊管線遺留；EDGAR 版改由 edgartools 呼叫 SEC API 取得，**不讀這裡** |
| `Fundamentals/` | Fundamentals + IncomeStatement `.txt` | 讀取，套 keep-latest |
| `News/` | News `.txt` | 讀取，不去重（時間序列） |
| `_archive_stale/` | 過期快照（0508/0519） | **整個目錄排除**（`RAW_EXCLUDE_DIRS`） |

掃描必須**遞迴**（`RAW_DIR.rglob`）：改回非遞迴的 `iterdir()` 會掃到 0 個檔案，而且**不報錯**，只是安靜地少 ingest 38 個 `.txt`（新聞＋基本面全滅）。傾印輸出 `data/edgar_processed/` 鏡像同樣三分類（`_category_for`）。

### 重建不會逐字重現舊 collection——兩個正當差異
1. **SEC 新申報**：edgar 每次抓「最新 10-K ＋ 最近 2 份 10-Q」，SEC 一有新申報就換版（2026-08-07 實測換掉 4 份：AAPL/AMZN/META 的 10-Q、MSFT 的 10-K）。
2. **表格抽取有跑次間變異（約 2~3 個 chunk/filing）**：兩個成因——① 無 caption 的大表格由 `_llm_summarize_table` 走 Gemini 生成，未固定 temperature 且 `max_output_tokens=60` 會截斷 ② **連表格本體都會變**（實測 GOOGL Exhibit Index 等行政表格，表頭列有無、長度差 5~17 字元），推測來自 unstructured 對原始 HTML 的表格偵測與 `_find_caption` 回溯。**text chunk 完全不受影響**。
   > 這是變異不是 bug 的證據：2026-08-07 三方交叉比對同一份 `GOOGL_10K_2025`（127 個 table chunk），exp4↔HEAD＝2、HEAD↔新碼＝2、exp4↔新碼＝3——**兩個都不含新改動的版本彼此就差 2**，沒有任何一方是離群值。要判斷某次改動有沒有動到切塊，比 text chunk（確定性）而不是 table。

扣除這兩項後，2026-08-07 驗證結果：`.txt` 三類（news 98／fundamentals 22／income_statement 59）**逐字全等**；排除換版文件後 filing chunk 數 2832 vs 2832 相同，其中 2355 個 text chunk **逐字全等**。→ 判斷重建是否正常，看的是「扣掉換版文件後 **text chunk** 是否逐字相同」，不是總 chunk 數，也不要拿 table 當判準。

## Eval
| 檔案 | 用途 | 跑在哪 |
|---|---|---|
| [`eval/eval_set.json`](eval/eval_set.json) | **100 題**題庫（news/multi_intent/semantic/mixed/lexical/colloquial 各 15 + **multi_hop 10**），只含 query/relevant/answerable，不含 rubric | — |
| [`eval/gen_reference_answers.py`](eval/gen_reference_answers.py) | 從 `relevant` 黃金來源檔生成完整參考答案 → `eval/reference_answers.json`（RAGAS 的 ground truth）。dense 選 chunk 用**英文譯句**（2026-08-01 修：中文 query 跨語言 dense 會漏事實 chunk→ false-negative gold），`GOLD_TOP_K=8` | 專案 `.venv`，`rq.call_llm` |
| [`eval/run_agentic_on_evalset.py`](eval/run_agentic_on_evalset.py) | 把 agentic 模組（`--module`，預設 `agentic_rag_v2`）跑在 eval_set → generation_judge schema 結果檔（含 `contexts`，供 RAGAS）。`--freshness-mode` 預設 `snapshot` | 專案 `.venv`，NVIDIA |
| [`eval/eval_generation_llm_judge.py`](eval/eval_generation_llm_judge.py) | **單管線**（rag_query）版：真實 retrieve+generate + 3 維判定（Correctness/Refusal/Context Recall）→ 同 generation_judge schema | 專案 `.venv`，NVIDIA（`--gen-model` 指定 NVIDIA 名） |
| [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) | 讀結果檔 + `reference_answers.json` → RAGAS 六指標（context_recall/precision、nv_context_relevance、faithfulness、answer_relevancy、answer_correctness）。`--from-results` 可指任一結果檔、`--output` 自訂 | **獨立 `.venv-ragas`**（`ragas==0.2.15` 需 `langchain<0.4`，會弄壞生產 `.venv`）；NVIDIA |

**標準跑法**（三步，reference 只需生成一次可重用）：
1. `gen_reference_answers.py` → `reference_answers.json`
2. 產生結果檔：agentic 版跑 `run_agentic_on_evalset.py`；單管線版跑 `eval_generation_llm_judge.py`
3. 切 `.venv-ragas` 跑 `eval_ragas_vs_rubric.py --from-results <結果檔> --reference-file eval/reference_answers.json --output <輸出>`

> 只改動少數題的 reference 時**不必重跑全部 RAGAS**：RAGAS 每題獨立評分，把改動題的新分數拼接回原結果、其餘沿用即可（重跑只多出 judge 噪音）。
> `answer_correctness` 偏低常是量尺問題（長度對齊／false-negative gold），非系統答錯——先讀 memory `ragas-correctness-length-artifact`、`eval-false-negative-gold` 再下結論。
