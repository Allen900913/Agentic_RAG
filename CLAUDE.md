# US Stock RAG — 檔案地圖（reference：用到哪塊才去看那個檔）

> **維護規則（改任何檔案前必讀）**：這份檔案地圖是本專案的 reference。**只要你要改動任何檔案，就要先檢查這份地圖是否因此過時**——例如新增/刪除/更名檔案、改變某檔用途或角色（生產⇄棄用）、換 LLM/collection、改變資料流或路徑（如把 `data/raw` 分成子目錄）。**只要地圖有任何一格對不上實況，就要在同一次改動裡把它重新生成/更新到一致**，別讓程式碼與地圖漂移。

**生產 collection**：`us_stock_rag_edgar_exp4`（EDGAR 抽取 → chunk → BGE-M3 dense+sparse hybrid → RRF → cross-encoder rerank）。
**預設 LLM**：NVIDIA NIM `openai/gpt-oss-120b`；模型名 `gemini-*` 開頭走 Gemini（`rq.call_llm` 依名稱路由）。
> 一次性診斷腳本（`eval/_*.py`、`*_probe*`、`* copy.py`）不列於此表；那些是拋棄式實驗，別當生產碼。

## 生產／查詢
| 檔案 | 用途 | LLM |
|---|---|---|
| [`rag_query.py`](rag_query.py) | CLI 查詢入口（`python rag_query.py -q "..."`）。核心 `retrieve()`（hybrid+rerank，支援 `full_translate_en` 檢索中間層全英文）、`call_llm()`、`build_user_prompt()` 也被其他腳本 `import rag_query as rq` 共用。**近重複抑制** `_suppress_near_duplicates`（2026-08-09 加，env `RAG_SUPPRESS_NEAR_DUP=1` 才開，**預設關閉**）在截斷 top_k 前丟掉「同來源且數字集合是子集」的低分候選。`COLLECTION_NAME` 在此定義，**可用 env `RAG_COLLECTION` 覆蓋**（2026-08-08 加）——gold 生成／eval／agentic 全都 import 這個常數，跑 collection A/B 時設 env 即可，不必改碼（改碼跑完忘了改回來是實際風險） | NVIDIA NIM `gpt-oss-120b`；`gemini-*` 走 Gemini |
| [`api_server.py`](api_server.py) | FastAPI 後端，包住 `rag_query.py`（`/chat` SSE 串流） | 沿用 `rq.call_llm` |
| [`app.py`](app.py) | Streamlit 聊天前端，串接 `api_server.py` 的 SSE | — |
| [`llm_replay.py`](llm_replay.py) | **檢索前 LLM 中間產物的重放快取**（2026-08-09 加）。接三個點：`_plan_subqueries`／`translate_query_to_english`／`_check_sufficiency`。**未設 env `RAG_REPLAY_CACHE` 時完全 no-op**，生產路徑不受影響；設了就讓 eval 的 A/B 兩臂共用同一份 plan／英譯／圈選決策。**一個 block 一個 cache 檔＝隨機區塊設計**（A/B 共用 plan、跨重複次數重抽），零改碼。`RAG_REPLAY_MODE` 支援 per-kind（`strict:plan,translate_en`，2026-08-10 加）——跨 collection A/B 用 bare `strict` 會被 `check` 的合法 miss 誤炸。存在理由與 block 紀律見下方「量測噪音與重放快取」 | — |

## Agentic（LangGraph 多節點版，生產級 agentic 入口）
| 檔案 | 用途 | 模型分層 |
|---|---|---|
| [`agentic_rag_v2.py`](agentic_rag_v2.py) | **現役** agentic RAG（2026-07-26 重構）。LangGraph Supervisor 管線：Plan（拆平行子問題）→ Execute（**確定性** `_retrieve_chunks`，無 Agent A LLM）→ Grade（`_check_sufficiency` 判足夠+targeted 改寫）→ Synthesize（單次生成 + citation validator + **一致性 validator** + reflect）。**一致性 validator**（`_consistency_check_and_fix`，2026-08-07 加、08-08 改架構）抓「同一主體同一指標出現互斥數值卻不調和」。**分工＝LLM 抽取、Python 判斷**：`_extract_claims`（CHECKER_MODEL）把答案每個數字抽成 `value/unit/metric/entity/scope/period_months/period_end/period/kind/basis/quote/period_evidence`，再由 `find_claim_conflicts` 跑零 LLM 的確定性規則（R1 同格互斥、**R2 分部加總 vs 合併總計**）。**期間三件套**（2026-08-08 加，詳見下方「期間接地」）：① `_extract_claims` 現在**同時餵 chunks**，讓它能回原文查證而非照抄答案 ② 期間判準從自由文字 `period` 換成客觀的 `period_key = f"{period_months}M@{period_end}"`（`_period_key`，兩欄任一缺就退回舊 `period` 字串，故舊行為完全保留）③ `_ground_period_from_source` 用**零 LLM** 的字串定位，拿數字回 chunk 找它落在哪個期間段並覆寫期間。抽取失敗就跳過這層（2026-08-08 移除 regex 降級路徑 `find_numeric_conflicts`＋`_CONSIST_*` 詞表：它用寫死的指標/實體/措辭清單做**感知**，col-11 就是因措辭不在清單而漏抓；而它的觸發條件「抽取失敗」離線量測 200 份答案的發生率是 0，等於從未跑過卻要維護的死碼）。**偵測 4/4 精準，但重寫路徑實測 2 好 1 壞**（mix-06／mix-03 修對，col-11 修掉矛盾卻把總營收誤標成雲端營收，且 citation validator／再偵測／reflect 三層全過）——重寫是否該預設關閉尚未定案。時間感知走雙軸 `freshness_mode`：`snapshot`（eval，「最新」= KB coverage 掃出的最新資料，不看 wall clock）／`live`（prod，看 `_get_as_of_date`，可用 `AGENTIC_AS_OF_DATE` 固定） | RETRIEVAL=`gpt-oss-20b`、CHECKER/GEN=`gpt-oss-120b`（皆 NVIDIA，env `AGENTIC_*_MODEL` 可覆蓋） |
| [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md) | agentic 變更史＋知識庫附錄（eval 量尺、檢索層決策、診斷結論） | — |

## Ingest／Migration（要重建語料或改 payload 時才看）
| 檔案 | 用途 |
|---|---|
| [`data_update_edgar.py`](data_update_edgar.py) | **唯一 ingest 執行入口，且 2026-08-08 起零網路**（見下方「抓取／處理分離」）：讀本機 `.nc` → chunk → 寫入獨立 EDGAR collection（`--collection`，生產為 `us_stock_rag_edgar_exp4`；chunking 世代演進見 memory `edgar-chunking-pipeline-experiments`）。四種 doc_type 都由本檔處理：10-K/10-Q 從 `data/raw/sec_local/` 讀本機申報檔，News/Fundamentals/IncomeStatement 的 `.txt` 借用 `unstructured_components` 的組件。**增量策略三軌**：① `.txt` MD5 快取（`hashes_edgar.json`，per-collection 記帳，未變則跳過 embed；`--force-txt` 繞過）② SEC filing 不快取（申報後內容不變），維持 delete-by-source 重寫 ③ Fundamentals/IncomeStatement keep-latest：只 ingest 最新快照，且**主動刪除過期快照的殘留 chunk**（保證庫裡只有一份；靜態財報 10-K/10-Q 則允許多期並存供查歷史）。<br>**期間章節硬邊界**（2026-08-08 加，`_split_by_period_section`）：散文切塊在 Item 之下多一層邊界——依 `X Months Ended <date> Compared with Y Months Ended <date>` 標題先切段再各自送 SemanticChunker，並把期間標籤前綴進被 embed 的文字（`[MSFT 10-Q period 202603] [Three Months Ended March 31, 2026 vs March 31, 2025] …`）＋存成 payload `period_context`。詳見下方「期間章節邊界」小節<br>**通用小標硬邊界**（2026-08-09 加，`_split_by_subheading`）：期間章節**之下**再多一層——散文 Item 依獨立成行的小標切段，標籤前綴進被 embed 的文字（`… [section: Intelligent Cloud] …`）＋存成 payload `heading_context`。前身是只認分部的 `_split_by_segment_section`（mix-03 確診），同日通用化。詳見下方「通用小標邊界」小節<br>**min-size 下界護欄**（2026-08-09 加，`_merge_small_chunks`）：此前只有上界（`RCTS_THRESHOLD`）沒有下界。過短 chunk 併進**同一硬邊界內**的鄰居 |
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

集中的原因：`X Compared with Y` 這個章節標題結構**只有 MSFT 在用**（12 份 10-Q 全掃：MSFT 24 處、其餘 6 家 0 處），別家把期間寫在句子裡，切塊切不掉。已知受害案例：**col-11**（單季 19/33/12/22% 與九個月 18/29/20% 被當成互斥數值並陳）。
> ⚠ **mix-03 曾被歸在這裡，2026-08-09 查清後移除**：它的病不是期間而是**分部**（把 Intelligent Cloud 的 +$2.7B/24% 當成全公司總計），修在下一小節。當初的歸因只看了「兩個 operating income 數字並存」就下結論，沒有回原文驗算是哪一種混用。

**2026-08-09 補**：`_PERIOD_SECTION_RE` 加入 10-K 的財年寫法 `Fiscal Year 2026 Compared with Fiscal Year 2025`。動機不是期間標籤本身（10-K 整份就是一個財年，`report_period_code` 已足夠），而是當時**它是分部小標那層的開關**——分部切段只在有期間標籤的章節內生效。⚠ **這個開關作用在同日下午的通用化後已不存在**（通用小標層改由 `_TABLE_DOMINATED_ITEMS` 決定適用範圍，不再看有無期間標籤）；這條 pattern 現在只影響 10-K 的期間標籤本身。⚠ 這個 pattern **必須大小寫敏感**：MSFT 10-K 裡還有一句句中小寫的 `… fiscal year 2026 compared with fiscal year 2025 included:`，加 `re.I` 會從句子中間切開。

**修法**：`_split_by_period_section` 把標題當**硬邊界**（先切段、各段再各自送 SemanticChunker），再把期間標籤前綴進被 embed 的文字。
- **必須切段、不能只貼標**：實測 MSFT `#104` 開頭是九個月數字、標題卻出現在它中段——一個 chunk 橫跨兩期，只貼標會標錯。
- `item_chunk_index` 跨期間段**連續編號**（Neighbor Expansion 靠 ±1 查鄰居，各段從 0 起算會重號）。
- 對沒有這種標題的 filing 回傳單一段，行為與加這層之前完全相同。
- 驗證方式：`_split_by_period_section` 對 7 家最新 10-Q 的實際 SEC 原文跑過——MSFT `Part I, Item 2` 切出 13 段、95 條變動陳述有 85 條（89%）拿到期間標籤；其餘 6 家 0 段。

### 通用小標邊界（2026-08-09 加，當日由「分部小標」通用化）
> ⚠ 與期間那層同樣是 ingest 期改動，**需重建後才生效**；判斷有沒有生效看 payload 有沒有 `heading_context`。
> **2026-08-10 已重建成 `us_stock_rag_edgar_head`**（4221 points，`--rcts-fallback` ON）。實測結果：

| | `us_stock_rag_edgar_period`（基準） | `us_stock_rag_edgar_head` |
|---|---|---|
| filing 散文 chunk | 2955 | 3477（+18%） |
| 帶 `heading_context` | 0 | **2154（61.9%）**，七家全覆蓋 |
| 前綴進 embed 文字 | — | 2154/2154 |
| **退化 chunk（剝前綴後 <200 字元）** | **328（11.1%）** | **92（2.6%）** |
| mix-08 孤兒句所在 chunk | 65 字元 `The increases were almost entirely driven by…`（指涉全被切掉） | **255 字元**，開頭 `Family of Apps FoA revenue in the three and six months ended June 30, 2026 incre…` |
| **mix-03 斷言**（`number_claims.json`） | 三個 run 全 **FAIL**（讀到 24%） | **PASS**（答「增加 64 億美元，增幅約 20%」） |

剩下的 92 個短 chunk 都是**本來就該短**的 Item（`Item 1B. Unresolved Staff Comments None.`／`ITEM 6.[Reserved]`），沒有鄰居可併也不該丟。
⚠ **是否升為生產 collection 未定案**——`us_stock_rag_edgar_exp4` 仍是表頭寫的生產值。

**病灶與期間那層同形、低一層，但後果更嚴重**：讀不出期間是「資訊缺失」，冒充總計是「產生一個看起來有憑有據的錯數字」。

**確診案例 mix-03**（端到端查清）：問「MSFT 最新一季**整體**營業利益成長多少」，系統答 +$2.7B / 24%。那句原文在 `MSFT_10Q_202603 #111`，**開頭就是** `Operating income increased $2.7 billion or 24%.`，帶了期間標籤卻沒有分部標籤。用合併損益表驗算：

| | 單季 | 九個月 |
|---|---|---|
| 合併 operating income 2026 / 2025 | 38,398 / 32,000 → **+6,398（+20%）** | 114,634 / 94,205 → +20,429（+22%） |
| Intelligent Cloud 分部 | 13,753 / 11,095 → **+2,658（+24%）** | — |

→ #111 是 Intelligent Cloud。**gold（$6.4B / 20%）是對的**，正解那句也**在語料裡**（`#104`），只是沒被檢索到——#111 開頭就是那個句型，字面與語意都比「數字埋在段中」的 #104 更像答案。所以貼標同時修兩邊：生成端不再讀成總計，檢索端 #111 對「全公司」問題的相似度下降。

**曝險**（`us_stock_rag_edgar_period` 全庫）：32 個含 `X increased $Y or Z%` 的 filing text chunk 裡 **5 個（16%）不含任何分部名稱**＝讀起來像合併總計，**全在 MSFT**（10-Q 4、10-K 1）。

**通用化（同日下午）**：原版只認分部、只在有期間標籤的章節內生效，覆蓋率**全庫 2.8%（84/2955 個散文 chunk，全部是 MSFT）**——因為 `X Compared with Y` 期間標題只有 MSFT 在寫。通用化後**帶標籤的散文字元 53.0%**，7 家全覆蓋。

**「有規則就不需要語意切割」不成立**，這是通用化時量清楚的：276 個散文 Item 平均被切成 10.7 塊，最大的 `META_10Q Part_II_Item_1A` 是 105 塊。三件事是**不同的工作**：

| 工作 | 有無唯一正確答案 | 該用什麼 |
|---|---|---|
| 這一行是不是章節邊界（**不可跨越**） | 有 | 規則／Python |
| 這 40k 字的敘述**該在哪裡斷** | **沒有** | SemanticChunker |
| 標籤要不要傳進 chunk 文字 | 有 | 規則（前綴注入） |

規則決定「哪裡**不准**切」，切塊器決定「裡面**哪裡**切」，**前者取代不了後者**。切塊層級：Item → 期間章節 → 通用小標 → SemanticChunker → RCTS 上界 → min-size 下界。

**偵測器刻意不用硬編碼指標／分部名單**（見 memory `llm-vs-python-task-split`）。條件：① 獨立成行的短標題（4~60 字元、無數字、不以標點結尾、字數 ≤6、字首大寫、不在導航殘渣清單）② **下一個非空行像散文**（≥8 詞或以句號結尾）。
- **② 是關鍵**：少了它會把壓平表格的列標籤（`Total` 31 次／`Revenue` 11／`Amount`／`Numerator`／`Denominator`／`Assets`）全當標題。這些是**開放集合、列不完**，只能用結構判。
- **必須按行號切、不能按字串切**：標題名同時是 SEGMENT RESULTS 表的列標籤，字串比對會把表格切成兩半。
- **通用化時刪掉舊條件 ③「同檔以獨立行出現 ≥2 次」**：它是為「專門找分部」而設，對通用標題過度收斂。
- **表格為主體的 Item 整層不套用**（`_TABLE_DOMINATED_ITEMS = {Item 6, Item 8, Item 15, Part I Item 1}`）：實測 TSLA 10-K Item 15（展覽表）只用條件① 會抓 27 個候選、**區間中位數 10 字元**；Item 8 抓 150 個混雜 `Page`／`/s/ PricewaterhouseCoopers LLP`。這是格式定義的封閉集合，列清單正當（同 `VALID_*_ITEMS`）。

> ⚠ **「重複次數 ≥3 判為頁面殘渣」是錯的，實測推翻**：`Table of Contents` 在 GOOGL 20 次、META 5 次、TSLA 只有 1 次（連自己都不穩定），而真標題 `Intelligent Cloud` 7 次、`More Personal Computing` 8 次、`Energy Generation and Storage` 8 次——**完全重疊**。照那個門檻做會把 mix-03 的修復砸掉。所以拆成兩個機制：導航殘渣（`Table of Contents`／`Page`／`SIGNATURES`）＝格式定義的**封閉集合**，列清單正當；表格列標籤＝**開放集合**，用條件② 擋。
> ⚠ **別被「其餘 6 家零誤報」誤導**：原版探針**跳過了沒有期間章節的檔案**，等於根本沒測到那些情境。當日差點據此把範圍開太大。原先記錄的四類「誤報」中有三類（TSLA `Cash Flows from Operating Activities`、GOOGL `Other Bets`）**其實是真標題**，只是對「分部」偵測器而言算誤報——通用化後它們是正確標籤。
> ⚠ **這層自己會製造碎片，需要 min-size 配套**：實測切出 1210 段中 161 段（13.3%）不到 200 字元。拆開看是 80 個「開頭無標籤」（就是 Item 標題行 `ITEM 1. BUSINESS`，已改為併進下一段）＋ 81 個「有標籤」（**刻意保留**：短但自帶 scope，往前併會被貼上前一段的標籤＝貼錯標，比短更糟）＋ 0 個中段無標籤。
> ⚠ **殘留已知限制**：簽名頁的人名（`TIMOTHY D. COOK`）仍可能被當標題。判定為外觀噪音不追——那些 chunk 不帶任何財務主張，加人名偵測器的成本高於收益。

**驗收用 [`eval/verify_segment_split.py`](eval/verify_segment_split.py)**（零網路、零 embedding、可在別的實驗跑的時候執行）。2026-08-09 通用化後基準：21 份 filing 全部有切段（2~13 段/份）、抓到 **405 種**標籤（`Income Taxes` 19／`Google Cloud` 10／`More Personal Computing` 9／`Other Bets` 7…）、帶標籤散文字元 **53.0%**、表格型 Item 被切段 **0** 次、表格對帳 **0** 筆不符、目標句落在 `Intelligent Cloud`。

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

### 量測噪音與重放快取（2026-08-09，**跑任何 A/B 之前必讀**）

**核心事實：這個專案的量測解析度比多數改動的效果粗一個數量級。**

| 噪音層 | 實測 | 量法 |
|---|---|---|
| RAGAS judge | `context_precision` **0.046**（兩次獨立樣本 0.0459／0.0452 複現）、`context_recall` 0.002～0.013 | 同一份結果檔重評兩次（`--from-results` 指同一檔、`--output` 換名） |
| Plan 節點 | **12/25 題（48%）子問題不同** | 同輸入連呼叫 `_plan_subqueries` 兩次 |
| query 英譯 → 檢索 | **47/100 題 top-8 不同** | 同組態完整重跑兩次 |

`temperature=0` **不等於**確定性：`gpt-oss-120b` 是 MoE，溫度只固定取樣、不固定專家路由。
**MDE（95%, n=100）**：recall 0.029／precision 0.036／faithfulness 0.028／correctness 0.019 —— 換算成「要幾題從 0 修到 0.5」是 **4~7 題**。ingest 層那種「動 2~5 題」的改動在原理上就量不出來。

**跑 A/B 的正確做法**：設 `RAG_REPLAY_CACHE=eval/replay_cache.json`（見 [`llm_replay.py`](llm_replay.py)），兩臂共用同一份 plan／英譯／Grader 決策，差異才只剩你改的那一項。**同一份 fixture 內別再重生成**——它跟 `reference_answers.json` 一樣，不需要唯一正確，只需要固定且對所有組態一視同仁。

**多 block（隨機區塊設計，2026-08-10 釐清）**：單一 fixture＝**K=1 個 block**，結論條件於那一次 plan 抽樣，**沒有自由度**分辨「B 真的較好」與「B 在這個 plan 下剛好較好」。要多 block **不需要改碼**——`RAG_REPLAY_CACHE` 是路徑，一個 block 一個檔（`replay_b1.json`／`replay_b2.json`…），block 內兩臂共用、跨 block 重抽 plan。紀律：**同 block 內必須先後跑**（並行會兩臂都 miss 各自寫入＝等於沒 block）。
- **成本**：2K 次 × 3~4 小時。便宜版＝block 2/3 **只重跑 block 1 裡兩臂 verdict 不同的題**（沒動的題對差異貢獻 0）。
- **blocking 是必要條件不是充分條件**：擋得住 plan／英譯；**擋不住** Grader（`check` 的 key 含實際候選 id → 跨 collection 必然 miss，而那是**正確**的：候選變了就是被測改動造成的差異，不該用舊決策蓋掉）、生成層 `GEN_TEMPERATURE=0.3`、RAGAS judge。殘餘就是「同組態兩次跑 6 vs 4 個衝突」那個底線。
- **plan fixture 帶先跑那臂的條件**：plan 的 system prompt 內嵌隨 collection 變動的 KB Coverage Snapshot，而 key 刻意不含 prompt。對 `period` vs `head`（同批 filing、只有切塊不同 → coverage 相同）是非議題；若 A/B 是「加了新文件的 collection」就有方向不明的偏誤，要用第三次中性 pass 生 fixture。
- `RAG_REPLAY_MODE=strict` 讓 cache miss 直接報錯，但**跨 collection A/B 不能用 bare strict**（`check` 的合法 miss 會誤炸）→ 用 per-kind：`RAG_REPLAY_MODE=strict:plan,translate_en`。拼錯 kind 名會**啟動就報錯**而非靜默失效。

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
> ⚠ **不要拿落在噪音裡的數字反推機制**——機制聽起來越合理越危險。2026-08-09 一天內提出三個機制假設、三個都被自己的確定性測試推翻（詳見 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md) A5.5）。

**目前最大的未動槓桿**：`_check_sufficiency` 的 `relevant_ids` 只留下 **49.3%** 的候選（18/58 題最後只剩 1 個 chunk，全 100 題中位數 3 個），而 `context_recall ↔ correctness` 相關 0.53 是六指標最強、`precision ↔ correctness` 只有 0.14。等於每題用一次 LLM 判斷丟掉一半證據，換一個跟答對與否幾乎無關的指標。

### 數字缺陷指標的兩次失效（2026-08-10，**拿它當閘門前必讀**）

`check_number_defects.py` 原本用「答案的第一個百分比 vs gold 的第一個百分比」。兩種修法都被實測推翻：

| 比法 | mix-03（確診答錯：答 24%，合併實為 20%） | 副作用 |
|---|---|---|
| 比**第一個** | 抓到 ✅ | col-11／mi-04／mi-08 **誤報**——它們頭條挑了 Azure 40%／LTM 12.8%，內容其實對 |
| 比**任一個** | **漏抓 ❌** | 答案的 21%（Productivity 分部）落在 gold 20% 的 ±1pt 內 |

→ 答案裡有 4~8 個百分比、容差 ±1pt，**任何值幾乎都能湊到**，「值有沒有出現」不帶資訊。現行做法是**把數字接地到主張**（`anchored_pct`），實測 mix-03 FAIL／col-11 PASS／mi-04 PASS，與人工獨立判讀 **3/3 一致**，且 mix-03 在三個 run 全 FAIL（**穩定＝真缺陷**，對比頭條指標有 8/30 題會自己變）。

> ⚠ **兩個記帳坑，都害過事**：
> ① **「某個 run 沒給百分比」被算成衝突** → 兩檔並列得到 10、單檔卻是 6／4／5。當時據此把閘門門檻設成「衝突 8 → ≤5」，**門檻與被比較的數字不可比，等於這道閘門沒有判定力**。現在分成獨立的「無法比對」桶，且**每個 run 各自算**。
> ② 稽核腳本第一版回傳「0 筆假象」不是好消息，是 gold 欄位名寫錯（`reference_answer` vs 正確的 `reference`）。同 memory `eval-measurement-pitfalls`。
> ⚠ `number_claims.json` 的每一條都要**人工驗證過才登錄**（`verified` 欄位寫怎麼驗的）。`status` 兩種：`known_defect`（現在會 FAIL，修好要變 PASS）／`regression_guard`（現在 PASS，防日後退步）。

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
| [`eval/check_number_defects.py`](eval/check_number_defects.py) ＋ [`eval/number_claims.json`](eval/number_claims.json) | **確定性數字缺陷檢查（零 LLM、零噪音）＝「數字答錯」類改動的主要驗收指標**。**主指標＝接地主張斷言**（2026-08-10 改）：`number_claims.json` 逐條寫「anchor＋正解＋禁止值」，用 `anchored_pct`（anchor 命中後 N 字元內的第一個百分比，先往後找、找不到才往前找）判 **PASS／FAIL／N/A 三態**。⚠ **N/A 不能併進 PASS**，否則改寫措辭或拒答會靜默通過。輔助：① 頭條百分比**候選**（篩新缺陷用，**不是閘門**）② 候選值的來源類型 ③ 跨 run 穩定性。2026-08-10 基準（3 條主張）：mix-03 三個 run 全 **FAIL**（穩定＝真缺陷）、col-11／mi-04 全 PASS。詳見下方「數字缺陷指標的兩次失效」 | 生產 `.venv`（只讀結果檔） |
| [`eval/verify_segment_split.py`](eval/verify_segment_split.py) | 驗收 ingest 的期間／通用小標兩層硬邊界（見「通用小標邊界」）。零網路、零 embedding、不碰 Qdrant → **可在別的實驗正在跑的時候執行**。四判準：該切的切到／不該動的 0 段／表格未被切開／目標句歸屬正確 | 生產 `.venv` |
| [`eval/migrate_fundamentals_pct.py`](eval/migrate_fundamentals_pct.py) | 一次性遷移（2026-08-09 已執行）：把既有 `data/raw/Fundamentals/*.txt` 的比率欄位從小數改寫成百分比（`Revenue Growth (YoY): 0.166` → `16.60%`），**並同步修 gold**——實測 19 題的 `reference_answers.json` 內文直接引用了小數字面值，只改語料會讓 `context_recall` 假性下降（同 memory `eval-gold-version-drift-bug` 的坑）。只改寫法不改數值，所以 gold 語意不受影響。未來抓取由 [`fetch_data.py`](fetch_data.py)`::_pct` 直接寫成百分比。⚠ `Dividend Yield` 只加 `%` 不乘 100（yfinance 已回百分點）；`Debt/Equity`(79.548)、`Current Ratio`、P/E、Price/Sales、Price/Book、EV/EBITDA、EPS 刻意不動——它們不是百分比 | 生產 `.venv`（純字串處理） |
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
