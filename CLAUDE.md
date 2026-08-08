# US Stock RAG — 檔案地圖（reference：用到哪塊才去看那個檔）

> **維護規則（改任何檔案前必讀）**：這份檔案地圖是本專案的 reference。**只要你要改動任何檔案，就要先檢查這份地圖是否因此過時**——例如新增/刪除/更名檔案、改變某檔用途或角色（生產⇄棄用）、換 LLM/collection、改變資料流或路徑（如把 `data/raw` 分成子目錄）。**只要地圖有任何一格對不上實況，就要在同一次改動裡把它重新生成/更新到一致**，別讓程式碼與地圖漂移。

**生產 collection**：`us_stock_rag_edgar_exp4`（EDGAR 抽取 → chunk → BGE-M3 dense+sparse hybrid → RRF → cross-encoder rerank）。
**預設 LLM**：NVIDIA NIM `openai/gpt-oss-120b`；模型名 `gemini-*` 開頭走 Gemini（`rq.call_llm` 依名稱路由）。
> 一次性診斷腳本（`eval/_*.py`、`*_probe*`、`* copy.py`）不列於此表；那些是拋棄式實驗，別當生產碼。

## 生產／查詢
| 檔案 | 用途 | LLM |
|---|---|---|
| [`rag_query.py`](rag_query.py) | CLI 查詢入口（`python rag_query.py -q "..."`）。核心 `retrieve()`（hybrid+rerank，支援 `full_translate_en` 檢索中間層全英文）、`call_llm()`、`build_user_prompt()` 也被其他腳本 `import rag_query as rq` 共用。`COLLECTION_NAME` 在此定義，**可用 env `RAG_COLLECTION` 覆蓋**（2026-08-08 加）——gold 生成／eval／agentic 全都 import 這個常數，跑 collection A/B 時設 env 即可，不必改碼（改碼跑完忘了改回來是實際風險） | NVIDIA NIM `gpt-oss-120b`；`gemini-*` 走 Gemini |
| [`api_server.py`](api_server.py) | FastAPI 後端，包住 `rag_query.py`（`/chat` SSE 串流） | 沿用 `rq.call_llm` |
| [`app.py`](app.py) | Streamlit 聊天前端，串接 `api_server.py` 的 SSE | — |

## Agentic（LangGraph 多節點版，生產級 agentic 入口）
| 檔案 | 用途 | 模型分層 |
|---|---|---|
| [`agentic_rag_v2.py`](agentic_rag_v2.py) | **現役** agentic RAG（2026-07-26 重構）。LangGraph Supervisor 管線：Plan（拆平行子問題）→ Execute（**確定性** `_retrieve_chunks`，無 Agent A LLM）→ Grade（`_check_sufficiency` 判足夠+targeted 改寫）→ Synthesize（單次生成 + citation validator + **一致性 validator** + reflect）。**一致性 validator**（`_consistency_check_and_fix`，2026-08-07 加、08-08 改架構）抓「同一主體同一指標出現互斥數值卻不調和」。**分工＝LLM 抽取、Python 判斷**：`_extract_claims`（CHECKER_MODEL）把答案每個數字抽成 `value/unit/metric/entity/scope/period_months/period_end/period/kind/basis/quote/period_evidence`，再由 `find_claim_conflicts` 跑零 LLM 的確定性規則（R1 同格互斥、**R2 分部加總 vs 合併總計**）。**期間三件套**（2026-08-08 加，詳見下方「期間接地」）：① `_extract_claims` 現在**同時餵 chunks**，讓它能回原文查證而非照抄答案 ② 期間判準從自由文字 `period` 換成客觀的 `period_key = f"{period_months}M@{period_end}"`（`_period_key`，兩欄任一缺就退回舊 `period` 字串，故舊行為完全保留）③ `_ground_period_from_source` 用**零 LLM** 的字串定位，拿數字回 chunk 找它落在哪個期間段並覆寫期間。抽取失敗就跳過這層（2026-08-08 移除 regex 降級路徑 `find_numeric_conflicts`＋`_CONSIST_*` 詞表：它用寫死的指標/實體/措辭清單做**感知**，col-11 就是因措辭不在清單而漏抓；而它的觸發條件「抽取失敗」離線量測 200 份答案的發生率是 0，等於從未跑過卻要維護的死碼）。**偵測 4/4 精準，但重寫路徑實測 2 好 1 壞**（mix-06／mix-03 修對，col-11 修掉矛盾卻把總營收誤標成雲端營收，且 citation validator／再偵測／reflect 三層全過）——重寫是否該預設關閉尚未定案。時間感知走雙軸 `freshness_mode`：`snapshot`（eval，「最新」= KB coverage 掃出的最新資料，不看 wall clock）／`live`（prod，看 `_get_as_of_date`，可用 `AGENTIC_AS_OF_DATE` 固定） | RETRIEVAL=`gpt-oss-20b`、CHECKER/GEN=`gpt-oss-120b`（皆 NVIDIA，env `AGENTIC_*_MODEL` 可覆蓋） |
| [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md) | agentic 變更史＋知識庫附錄（eval 量尺、檢索層決策、診斷結論） | — |

## Ingest／Migration（要重建語料或改 payload 時才看）
| 檔案 | 用途 |
|---|---|
| [`data_update_edgar.py`](data_update_edgar.py) | **唯一 ingest 執行入口，且 2026-08-08 起零網路**（見下方「抓取／處理分離」）：讀本機 `.nc` → chunk → 寫入獨立 EDGAR collection（`--collection`，生產為 `us_stock_rag_edgar_exp4`；chunking 世代演進見 memory `edgar-chunking-pipeline-experiments`）。四種 doc_type 都由本檔處理：10-K/10-Q 從 `data/raw/sec_local/` 讀本機申報檔，News/Fundamentals/IncomeStatement 的 `.txt` 借用 `unstructured_components` 的組件。**增量策略三軌**：① `.txt` MD5 快取（`hashes_edgar.json`，per-collection 記帳，未變則跳過 embed；`--force-txt` 繞過）② SEC filing 不快取（申報後內容不變），維持 delete-by-source 重寫 ③ Fundamentals/IncomeStatement keep-latest：只 ingest 最新快照，且**主動刪除過期快照的殘留 chunk**（保證庫裡只有一份；靜態財報 10-K/10-Q 則允許多期並存供查歷史）。<br>**期間章節硬邊界**（2026-08-08 加，`_split_by_period_section`）：散文切塊在 Item 之下多一層邊界——依 `X Months Ended <date> Compared with Y Months Ended <date>` 標題先切段再各自送 SemanticChunker，並把期間標籤前綴進被 embed 的文字（`[MSFT 10-Q period 202603] [Three Months Ended March 31, 2026 vs March 31, 2025] …`）＋存成 payload `period_context`。詳見下方「期間章節邊界」小節 |
| [`unstructured_components.py`](unstructured_components.py) | **函式庫，非執行入口**（前身 `data_update_unstructure.py`，2026-08-07 剝除 CLI/Qdrant 寫入/hash）。提供 unstructured 解析與 chunking 組件：`partition_and_clean`、`build_chunk_records`、表格處理（`table_element_to_text`／`_find_caption`／`_llm_summarize_table`）、`extract_filing_metadata`、`BGEM3DenseEmbeddings`。只被 `data_update_edgar.py` import |
| [`fetch_data.py`](fetch_data.py) | **唯一對外抓取入口**（2026-08-08 起）。三類來源：yfinance 新聞 → `data/raw/News/`、yfinance 基本面 → `data/raw/Fundamentals/`、**edgartools SEC filing** → `data/raw/Filings/*.html`（人眼用）＋ `data/raw/sec_local/`（機器用 `.nc`）＋ `data/raw/sec_manifest.json`（取件清單）。`--skip-news` / `--skip-sec` / `--skip-fundamentals` 可分別跳過 |
| [`data_update.py`](data_update.py) | 最舊的 ingest 管線（MD5 增量＋`hashes.json`），已被 EDGAR 版取代，保留對照 |

### 抓取／處理分離（2026-08-08）
**在此之前兩支程式各自去 SEC 抓同一批 filing**：`fetch_data.py::fetch_sec_filings` 抓一次存成 `.html`，`data_update_edgar.py` 又自己 `company.get_filings()` 抓一次、且完全不理會前者存下的檔。後果是**每次重新切塊都可能悄悄換到不同版本的文件**——改了切塊規則跑出來的差異，分不清是規則造成還是換版造成。

現在：**抓取層決定「抓哪幾份」並落地，處理層只照 manifest 取件、零網路。**

| 產物 | 用途 |
|---|---|
| `data/raw/Filings/*.html` | primary document，人眼查閱（處理層**不讀**） |
| `data/raw/sec_local/filings/{YYYYMMDD}/{accession}.nc` | **完整申報檔，處理層唯一讀得懂的格式**（279 MB / 21 份） |
| `data/raw/sec_manifest.json` | 取件清單（ticker/form/cik/accession/filing_date）＝**控制點** |

```bash
python fetch_data.py --tickers NVDA MSFT AAPL GOOGL AMZN META TSLA --skip-news --skip-fundamentals   # 抓
python data_update_edgar.py --tickers MSFT --skip-txt --rcts-fallback --collection <名稱>            # 處理（離線）
```

**三個踩過的坑，都會靜默出錯**：
1. **`Filing.sgml()` 找不到本機檔會無聲 fallback 去下載**（讀原始碼確認）。所以 `_filings_from_manifest` 自己檢查 `.nc` 存在與否，缺檔就**中止**；`--allow-fetch` 是明知後果時的逃生口。
2. **`Filing.html()` 對 inline-XBRL 一律重新下載**：`html.startswith("<?xml")` 分支會丟掉已從本機讀到的 HTML、改打 `homepage.primary_html_document.download()`。而 SEC 財報全都是 `<?xml` 開頭 → 本機儲存對 10-K/10-Q 幾乎沒生效。`_install_offline_html_patch()` 繞過它；**繞過前已驗證內容 MD5 完全相同**（MSFT 10-Q、NVDA 10-K 各驗一份，只差一個結尾換行）。
3. `local_filing_path()` 回**絕對路徑**，`SEC_LOCAL_DIR` 是相對路徑，`relative_to` 會拋 ValueError 並被外層 `except` 吞掉——第一次跑就是這樣：21 份 `.nc` 全寫成功，manifest 卻是空的，而畫面上顯示 `x ... failed`。

**驗證方法（別只看有沒有報錯）**：攔掉 `sec.gov` 的 DNS 解析再跑完整處理流程。實測 MSFT 10-Q 全程 **0 次連線嘗試**，產出 141 records、40 個帶 `period_context` 的 chunk。

### 重建生產 collection 的正確指令（2026-08-07 補記）
```bash
.venv/Scripts/python.exe data_update_edgar.py --rebuild --rcts-fallback --collection us_stock_rag_edgar_exp4
```
**`--rcts-fallback` 不可省，但預設是關的**——這是最容易踩、且先前沒有任何文件記載的坑。漏掉它會得到一個實質劣化的索引：實測 3272 vs 3673 chunks（少 12%），且 **7.4%** 的 text chunk 超過 RCTS 的 1200-token 補切門檻（開啟時僅 0.1%），超出部分在 rerank 階段會被 `RERANK_MAX_LENGTH=2048` 截斷、內容等於看不到。

判定 exp4 當初有開的依據是 **token 分佈**：exp4 的 text chunk 最大 1208 token、>1200 者只有 4 個，精準卡在門檻上，是 RCTS 開啟的明確特徵。
> ⚠ **別用字元數推 token 數**：exp4 有 6898 字元的 chunk，直覺會換算成 ~1700 token 而誤判成「沒開 RCTS」，實測卻只有 1208 token（BGE-M3 多語 tokenizer 對英文約 **5.7 字元/token**）。2026-08-07 就是這樣推錯一次、用錯旗標跑掉一次完整重建。要判斷就載 `BAAI/bge-reranker-v2-m3` 的 tokenizer 實際數。

### 期間章節邊界（2026-08-08 加）
> ⚠ **生產 collection `us_stock_rag_edgar_exp4` 尚未套用**——這是 ingest 期的改動，只有重建後才生效。要判斷有沒有生效：payload 有沒有 `period_context` 欄位。

**病灶**：10-Q 的 MD&A 把「單季比較」與「累計比較」寫成兩個相鄰章節，期間**只寫在章節標題**、內文每句都不重述。SemanticChunker 依語意切塊時把標題切到別的 chunk，內文 chunk 就讀不出自己屬於哪一期。生成器不是讀錯，是**資訊根本不在 context 裡**。

| 實測（exp4 全庫，剝掉自注入的 metadata 前綴後） | 數字 |
|---|---|
| 含變動陳述的 filing text chunk | 109 |
| 其中讀不出期間 | **15（14%）** |
| 集中在 MSFT 10-Q | **9／24＝38%** |

集中的原因：`X Compared with Y` 這個章節標題結構**只有 MSFT 在用**（12 份 10-Q 全掃：MSFT 24 處、其餘 6 家 0 處），別家把期間寫在句子裡，切塊切不掉。已知受害案例：**col-11**（單季 19/33/12/22% 與九個月 18/29/20% 被當成互斥數值並陳）、**mix-03**（九個月 +$20.4B 與單季 +$2.7B 混用）。

**修法**：`_split_by_period_section` 把標題當**硬邊界**（先切段、各段再各自送 SemanticChunker），再把期間標籤前綴進被 embed 的文字。
- **必須切段、不能只貼標**：實測 MSFT `#104` 開頭是九個月數字、標題卻出現在它中段——一個 chunk 橫跨兩期，只貼標會標錯。
- `item_chunk_index` 跨期間段**連續編號**（Neighbor Expansion 靠 ±1 查鄰居，各段從 0 起算會重號）。
- 對沒有這種標題的 filing 回傳單一段，行為與加這層之前完全相同。
- 驗證方式：`_split_by_period_section` 對 7 家最新 10-Q 的實際 SEC 原文跑過——MSFT `Part I, Item 2` 切出 13 段、95 條變動陳述有 85 條（89%）拿到期間標籤；其餘 6 家 0 段。

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

### 語料目錄結構（2026-08-07 重整）
`data/raw/` 已分成三類子目錄，**根目錄不再有任何檔案**：

| 目錄 | 內容 | ingest 行為 |
|---|---|---|
| `Filings/` | 10-K/10-Q `.html`（primary document） | 由 `fetch_data.py` 產生供人眼查閱；**ingest 不讀這裡**（餵不進 `filing.obj()`，見「抓取／處理分離」） |
| `sec_local/` | `filings/{YYYYMMDD}/{accession}.nc` 完整申報檔 | **ingest 的 10-K/10-Q 唯一來源**（2026-08-08 加，279 MB） |
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
| [`eval/reference_answers.json`](eval/reference_answers.json) | RAGAS 的 ground truth。**已納入版控**（2026-08-07 前被 `*.json` 誤忽略）——它跟 eval_set 同級是「標準答案」不是跑分輸出，且非腳本可免費重現（需一次 LLM 全量生成 ＋ 含 24 處人工校正） | — |
| [`eval/gen_reference_answers.py`](eval/gen_reference_answers.py) | 從 `relevant` 黃金來源檔生成完整參考答案 → `eval/reference_answers.json`（RAGAS 的 ground truth）。dense 選 chunk 用**英文譯句**（2026-08-01 修：中文 query 跨語言 dense 會漏事實 chunk→ false-negative gold），`GOLD_TOP_K=8`。**快取三條件**：`query`、`gold_files`（**順序不敏感**——舊快取存的是未排序值，用 `list==` 比會把「同組檔案不同順序」誤判成換版，2026-08-08 實測害 mh-09 被白白重寫）、`collection` 都相同才 skip。①② 是檔案層級比較，對「同一份檔案、切塊變了」無感，故 2026-08-08 加 ③；但 ③ 只在快取項**已有** `collection` 欄位時判得出來，**既有 `reference_answers.json` 沒有這欄，第一次換 collection 時 ③ 攔不到**——要用新增的 `--force`（配 `--ids`）定向強制重生成。⚠ `--force` 不帶 `--ids` 就是全量重生成，會洗掉 24 處人工校正。**語言**：`--match-lang-from` > 既有 `reference_lang` > English——忘帶參數不會再無聲變英文。手轉「億」勸不動時由 `repair_yi_against_source()` 拿來源確定性修正 | 專案 `.venv`，`rq.call_llm` |
| [`eval/run_agentic_on_evalset.py`](eval/run_agentic_on_evalset.py) | 把 agentic 模組（`--module`，預設 `agentic_rag_v2`）跑在 eval_set → generation_judge schema 結果檔（含 `contexts`，供 RAGAS）。`--freshness-mode` 預設 `snapshot` | 專案 `.venv`，NVIDIA |
| [`eval/eval_generation_llm_judge.py`](eval/eval_generation_llm_judge.py) | **單管線**（rag_query）版：真實 retrieve+generate + 3 維判定（Correctness/Refusal/Context Recall）→ 同 generation_judge schema | 專案 `.venv`，NVIDIA（`--gen-model` 指定 NVIDIA 名） |
| [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) | 讀結果檔 + `reference_answers.json` → RAGAS 六指標（context_recall/precision、nv_context_relevance、faithfulness、answer_relevancy、answer_correctness）。`--from-results` 可指任一結果檔、`--output` 自訂、`--ids` 只評指定題（改少數題 reference 時免整份重跑）| **獨立 `.venv-ragas`**（`ragas==0.2.15` 需 `langchain<0.4`，會弄壞生產 `.venv`）；NVIDIA |

> **兩個 Python 環境，別混用**：生產 `.venv`（`requirements.txt`，langchain 1.x + langgraph）／評測 `.venv-ragas`（[`requirements-ragas.txt`](requirements-ragas.txt)，langchain 0.3.x + ragas 0.2.15）。ragas 0.2.x 綁 `langchain-core<0.4`，裝進生產環境會把 langchain 降版、弄壞 agentic 管線與 SemanticChunker。
> 新機器還原：`python -m venv .venv-ragas` → `pip install -r requirements-ragas.txt`（2026-08-07 用 `pip install --dry-run` 驗過：解析出 97 個套件，與現有環境數量與關鍵版本一致）。

**標準跑法**（三步，reference 只需生成一次可重用）：
1. `gen_reference_answers.py` → `reference_answers.json`
2. 產生結果檔：agentic 版跑 `run_agentic_on_evalset.py`；單管線版跑 `eval_generation_llm_judge.py`
3. 切 `.venv-ragas` 跑 `eval_ragas_vs_rubric.py --from-results <結果檔> --reference-file eval/reference_answers.json --output <輸出>`

> 只改動少數題的 reference 時**不必重跑全部 RAGAS**：RAGAS 每題獨立評分，用 `--ids <題號…>` 只評改動題，再把新分數拼接回原結果（重跑只多出 judge 噪音）。
> **`eval/` 只放輸入與標準答案**（`eval_set.json`／`eval_set_agentic.json`／`reference_answers.json`），跑分輸出一律寫到 `experiments/`。過期產物在 `eval/_archive_stale/`（2026-08-07 歸檔 6 個；未硬刪是因為它們未納版控，刪掉即永久遺失）。⚠ `eval_generation_llm_judge.py` 與 `eval_ragas_vs_rubric.py` 的 `--output` **預設值仍指向 `eval/`**，跑的時候要自己帶 `--output experiments/...`，否則輸出又會混進來。
> `answer_correctness` 偏低常是量尺問題（長度對齊／false-negative gold），非系統答錯——先讀 memory `ragas-correctness-length-artifact`、`eval-false-negative-gold` 再下結論。
