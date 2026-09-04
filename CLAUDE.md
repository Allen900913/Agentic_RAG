# US Stock RAG — 現況、架構、全域規則、檔案地圖

> **這份檔案只放四種東西**：① 現況快照 ② 架構（資料怎麼流）③ **全域規則**（不知道就會做錯事，且與日期無關）④ 檔案地圖（哪個檔負責什麼）。
>
> **不要往這裡放**：某次改動的診斷過程與實測數字、單一模組的旗標細節、待辦、日期記錄。那些各有歸屬，見〈文件地圖〉。判準是一句話：**「這條規則明天、下個月、換一個人接手都還適用嗎？」**
>
> **維護規則**：改動任何檔案前先看這份地圖有沒有因此過時（新增/刪除/更名檔案、改變用途或角色、換 LLM/collection、改變資料流）。**有任何一格對不上實況，就在同一次改動裡改到一致。**

---

## 現況快照（2026-08-28）

| | 現況 |
|---|---|
| **生產 collection** | `us_stock_rag_edgar_multiyear`（77 份 filing／13,022 chunks／**無新聞**）。每家 3×10-K ＋ 8×10-Q，橫跨 ~2.5 年 |
| **題庫** | [`eval/eval_set.json`](eval/eval_set.json) **65 題**（semantic 15／mixed 15／lexical 17／colloquial 13／multi_hop 5）。**0 題帶 rubric** |
| **冷凍題庫** | [`eval/eval_set_news.json`](eval/eval_set_news.json) 37 題（2026-08-19 拔除新聞時移出）。**現在不要拿它跑分** |
| **預設 LLM** | `nvidia/nemotron-3-super-120b-a12b`（2026-09-03 換）。前一代 `openai/gpt-oss-120b` 於 2026-09-03T08:00Z 被 NVIDIA 退役（HTTP 410 Gone），而它是**檢索／判定／生成三側共用**的預設 → 當天整條管線的 LLM 全死。**五個定義點**：[`rag_query.py`](rag_query.py) `DEFAULT_MODEL`／`DEFAULT_GEN_MODEL`、`agentic_rag_version/` 的 `CHECKER_MODEL`／`GEN_MODEL`／`RETRIEVAL_MODEL`（後三者可用 env 覆蓋）。選型證據 `experiments/_model_bakeoff_20260903.log`：**判準是輸出穩定性與延遲，不是模型大小**——nemotron-3-super 三個結構化角色 3/3 且最快（plan 10.4s／check 6.5s／filter 2.8s），`deepseek-v4-pro` plan 一次 **604 秒**（一題 50~60 次呼叫 → 不可用）、`llama-3.1-nemotron-ultra-253b` 在 NIM 上 **404**。⚠ **換模型讓所有既有基準與 replay fixture 失效**（中間產物全變），且**沒有辦法與舊模型 A/B**（舊的已下架）。⚠ 「LLM 自己把 `$X million` 換算成億且算錯位數」**舊模型同病**（2026-08-07 稽核 13 題 23 處），2026-09-03 已由 `rq.finalize_answer_units` 接線修掉，見 [`CHANGELOG.md`](CHANGELOG.md)；仍未量的是**頻率**，見 [`BACKLOG.md`](BACKLOG.md)。ingest 表格摘要走 Groq `openai/gpt-oss-20b`，不受影響 |
| **確定性閘門** | `verify_period_intent_routing` 85／`verify_answer_validators` **265**／`verify_cross_period_collapse` 17／`verify_web_gate_isolation` **181** — **全 PASS** |
| **量尺狀態** | RAGAS 六指標**五個已達或超過 gold 上限**；唯一有空間的是 `answer_correctness`（0.642 vs 0.972），而那 0.33 落差對 retrieval 免疫 |
| **已知帶著上線的缺陷** | `MSFT_10K_2024.html#158` 幅度接地漏一筆（1/1009＝0.1%），見 [`BACKLOG.md`](BACKLOG.md) |

**一句話交接**：切塊／檢索這條路已經到頂，量尺飽和；現在能動的只有生成層與確定性 validator，而驗收一律用零噪音的逐條斷言（`check_number_defects` ＋ `verify_*` 閘門），不是 RAGAS。

---

## 文件地圖（要寫東西之前先看這裡）

| 你手上的東西 | 該寫進 | 判準 |
|---|---|---|
| 架構、全域規則、檔案職責 | **本檔** | 與日期無關、全域適用 |
| 「今天改了什麼」 | [`CHANGELOG.md`](CHANGELOG.md)（主管線／ingest／eval）<br>[`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md)（agentic 早期演進，已封存） | 有日期、已完成 |
| 語料處理知識：切塊各層、重建注意事項 | [`docs/INGEST.md`](docs/INGEST.md) | 「為什麼切塊長這樣」 |
| 量測知識：噪音底線、MDE、量尺失效史、**已試無效總表** | [`docs/EVAL.md`](docs/EVAL.md) | 「這個數字能不能相信」 |
| agentic 知識：節點設計、validator、架構診斷 | [`docs/AGENTIC.md`](docs/AGENTIC.md) | 「為什麼管線長這樣」 |
| 未做、待決、**查清楚後決定不修的極限** | [`BACKLOG.md`](BACKLOG.md) | 還沒做完 |
| 對外說明（安裝、執行、設計理由） | [`README.md`](README.md) | 給不熟這個 repo 的人看 |
| 單一模組的旗標語法、參數細節 | **該模組自己的 docstring** | 只有讀那個檔的人需要 |

**三條搬移紀律**：
1. **一件事做完 → 從 `BACKLOG.md` 刪掉、寫進 `CHANGELOG*.md`**；查清楚決定不做 → 留在 `BACKLOG.md` 的「已接受的極限」並寫下復活條件。
2. **null result 一定要寫**（`docs/EVAL.md` 的已試無效總表）。沒寫下來的無效實驗會被重跑——實測發生過兩次。
3. **同一件事只寫一個地方**，其他地方放連結。重複的兩份必然漂移。

---

## 架構

```
fetch_data.py ──► data/raw/{Filings,sec_local,Fundamentals}   （唯一對外抓取，會連網）
                  （News/ 仍在磁碟上但**不進 KB**，見〈資料範圍〉）
                          │
data_update_edgar.py ─────┘  切塊六層 → BGE-M3 dense+sparse → Qdrant   （零網路）
                                 │
                    ┌────────────┴────────────┐
              rag_query.py                agentic_rag_version/
             （單發管線）                （LangGraph 多節點）
                    │                          │
            api_server.py ──► app.py     eval/run_agentic_on_evalset.py
```

- **檢索**：BGE-M3 dense+sparse hybrid → server-side RRF → cross-encoder rerank → top-5。
- **切塊六層**：Item → 期間章節 → 通用小標 → 幅度接地 → SemanticChunker → RCTS 上界 → min-size 下界。前四層是**規則**（決定「哪裡不准切」），SemanticChunker 決定「裡面哪裡切」——**前者取代不了後者**。
- **⚠ 切塊／ingest 這條路已經到頂，不要再改**：六個 RAGAS 指標五個已達或超過 gold 上限。檢索完美與檢索全失敗的題只差 0.141 correctness → 全修好也只有 +0.024，低於噪音底線。詳見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺飽和〉。
- **⚠ 表格摘要模型換過兩次，兩次都是被迫的**（gemini-2.5-flash → llama-3.3-70b → 2026-08-16 後者被 Groq 退役）。**換這個模型必須先跑 bake-off**：它是推理模型，`TABLE_SUMMARY_MAX_TOKENS` 太小會讓 reasoning token 吃光正文額度、**靜默吐空字串**（實測 gpt-oss-120b @200 是 10/10 全空）。判準見 [`unstructured_components.py`](unstructured_components.py) `TABLE_SUMMARY_MODEL` 上方的證據表——選型看**輸出穩定性**不是模型大小。失敗的唯一出口是 [`eval/verify_table_captions.py`](eval/verify_table_captions.py) 的 `missing_on_big`。

### Collection 一覽

| collection | 角色 |
|---|---|
| **`us_stock_rag_edgar_multiyear`** | **生產**（2026-08-27 起，`COLLECTION_NAME` 預設）。77 份／13,022 chunks／無新聞 |
| `us_stock_rag_edgar_mdna` | 前生產（2026-08-13 ~ 08-27）。單年 21 份／3,827 chunks。小標層收窄到 `_MDNA_ITEMS` 白名單 |
| `us_stock_rag_edgar_period` / `head` / `ground2` | 切塊層的中間世代，只留作對照。`period` 無小標層，**mix-03 會錯** |
| `us_stock_rag_edgar_exp4` | **歷史基準，不要當對照臂**。Fundamentals 比率還是小數（`0.183` 而非 `18.30%`） |

⚠ **升生產是與 `RQ_PERIOD_INTENT_LLM` 預設翻開同一次做的**，而且必須如此：多年語料上線而它沒開，當期題會退步（收益探針 control 臂 gold@5 4/4 → 3/4）。兩半證據見 [`docs/EVAL.md`](docs/EVAL.md)〈多年語料的期別干擾〉（損害：45 題掉 2 題、錯期率 0.000 → 0.044）與〈多年語料買到了什麼〉（收益：歷史題 gold@5 0/20 → 18/20，當期陰性對照 4/4 不動）。

⚠ **這一格漂移過兩次**（文件寫的生產值與碼上不符）。**跑任何實驗前先 `grep COLLECTION_NAME rag_query.py` 確認**，並用 env `RAG_COLLECTION` 覆蓋而不是改碼。

---

## 全域規則

**語言**
- **回覆一律繁體中文**（使用者 2026-07-11「以後也是」）。程式碼 docstring/註解用繁中，identifier 與 log 用英文。

**兩個 Python 環境，絕對不可混用**
- 生產 `.venv`（[`requirements.txt`](requirements.txt)）：langchain 1.x + langgraph。查詢、agentic、ingest 都在這裡。
- 評測 `.venv-ragas`（[`requirements-ragas.txt`](requirements-ragas.txt)）：langchain 0.3.x + ragas 0.2.15。**只給 RAGAS 用**。
- ragas 0.2.x 綁 `langchain-core<0.4`，裝進生產環境會把 langchain 降版、弄壞 agentic 管線與 SemanticChunker。

**資料範圍：KB 只收「記錄」，不收「流」**
- **KB ＝ 10-K／10-Q ＋ Fundamentals／IncomeStatement。沒有新聞。** `data/raw/News/` 的檔案留在磁碟上，但 [`data_update_edgar.py`](data_update_edgar.py) 的 `RAW_EXCLUDE_DIRS` 於掃描階段排除它——**那一行是整條規則的單一開關**。
- 判準是「這份東西的正確性靠什麼判定」：記錄靠**期別對＋有引用＋數字可驗**，流靠**夠新＋來源可信**。本專案為期別正確性做的每一層對新聞**沒有一項成立**——新聞 chunk 連 `report_period_code` 都是 0% 覆蓋。
- 「市場現在怎麼看」由 **agentic 的 live web 路徑**供應。⚠ 別把它接回 ingest：web fallback 也**不負責抓最新 10-Q**，那是 ingest 的事。
- ⚠ **不要為了讓 news 類題目有分數就把新聞加回來。** 完整理由見 [`docs/EVAL.md`](docs/EVAL.md)〈KB 拔除新聞〉。

**抓取與處理分離**
- **只有 [`fetch_data.py`](fetch_data.py) 會連網**。[`data_update_edgar.py`](data_update_edgar.py) 一律離線，照 `data/raw/sec_manifest.json` 取件。
- 不要為了「順便更新」去重抓 SEC：換版會讓切塊實驗不可重現（改了規則跑出的差異分不清是規則還是換版）。

**重建 collection**
```bash
.venv/Scripts/python.exe data_update_edgar.py --rebuild --rcts-fallback --collection <名稱>
```
- **`--rcts-fallback` 不可省，但預設是關的**。漏掉會少 12% chunks，且 7.4% 的 text chunk 超過 rerank 截斷長度＝內容等於看不到。
- **重建不會逐字重現舊 collection**，有兩個正當差異來源。判斷重建正常與否要比 **text chunk**（確定性），不要比 table。細節見 [`docs/INGEST.md`](docs/INGEST.md)。
- **重建後必跑三道閘門**：`verify_table_captions.py`（`missing_on_big` 是 Groq 429 靜默降級的唯一出口）、`verify_segment_split.py`、`verify_chunk_grounding.py`。

**量測（跑任何 A/B 之前）**
- **這個專案的量測解析度比多數改動的效果粗一個數量級**：RAGAS 的 MDE（95%, n=100）換算成「要幾題從 0 修到 0.5」是 **4~7 題**，而典型 ingest 改動只動 2~5 題。
- 所以：**先問「這個差值超過噪音嗎」再問「為什麼」**；A/B 兩臂要共用 `RAG_REPLAY_CACHE` fixture；**提實驗前先 `ls experiments/` 與 [`docs/EVAL.md`](docs/EVAL.md) 的已試無效總表**。
- **`temperature=0` 不等於確定性**：`gpt-oss-120b` 是 MoE，溫度只固定取樣、不固定專家路由。
- 逐條斷言（[`eval/check_number_defects.py`](eval/check_number_defects.py)）是零噪音的，聚合指標不是。「數字答對了沒有」用前者驗收。⚠ **它的判讀一律要 ≥2 輪**——實測同一份 collection 上兩輪跑出 PASS 7／FAIL 0 與 PASS 4／FAIL 3，差異全來自 Plan 子問題變異。這條規則已當場救過兩次。
- **跨輪比 RAGAS 聚合值 ＝ 把 judge 噪音算進系統差異。** A/B 兩臂要在同一批判分裡比，或各自附一個同期基準。

**LLM 與 Python 的分工**
- 判準是「**這個子任務有不有唯一正確答案**」：抽取／裁決給 LLM，比對／定位／算術給 Python。
- **硬編碼詞表是警訊**——那通常代表在用字串比對做感知，換個措辭就漏。例外是**格式定義的封閉集合**（`VALID_*_ITEMS`、`_TABLE_DOMINATED_ITEMS`、`WEB_ALLOWED_DOMAINS`、OCC 選擇權代號），列清單正當。
- ⚠ **改成 LLM 之後，判別力來自「LLM 說不是就必須不是」**——若空答案會掉回詞表，詞表仍然是實際做決定的人，而端到端跑分看不出任何差別（`_RATIO_INTENT_RE` 就是這個形狀）。

**期別誰新誰舊，只有一套序**
- **一律用 payload 的 `(fiscal_year, fiscal_period)`**（`rag_query._fiscal_sort_key`／`_get_period_ladder`，agentic 端是 `_fiscal_rank`）。
- **不要拿 `report_period_code` 比字串**：10-K 是 4 位年份、10-Q 是 6 位 yyyymm，混比會**時對時錯**——`'2026' > '202510'` 為真（對），但 `'2025' < '202506'` 也為真（錯）。**77 份 filing 實測：385 個配對有 31 對（8.1%）判反。**
- **財年不等於曆年**：`NVDA_10K_2026` 是 FY2026，而 `NVDA_10Q_202604` 是 **FY2027 Q1**——後者才新。用日期或檔名年份排序都會判反。
- 改期別排序前先跑 [`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py) 與 [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py)。

**web_search（live 專用）的四條規則**
- **eval 隔離只靠 `freshness_mode == LIVE` 與 `ENABLE_WEB_SEARCH` 兩個獨立條件**，各自都足夠。任何「相關性詞表」**都不是隔離機制**——它們守的是相關性卻讓非新聞措辭的即時題永遠打不到 web，而**漏網的代價是把三週前的數字講成「今天股價」**。要改 web 判斷式，先跑 [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py)。
- **只有「真實日曆日期」能證明候選池夠新**（`_is_freshness_evidence`）。10-K 的 4 碼財年戳、10-Q 的 6 碼期別戳是**財報期間不是發布日**（`MSFT_10K_2026` 會被算成 2026-12-31＝未來）——它們**不算過期、但也不算證據**。混淆這兩件事會讓財報替整個池子背書說「夠新」（實測 6 題中 4 題，含 intraday 股價題）→ `kb_unfixable` 恆為 False → 白燒 `MAX_REWRITES` 輪改寫，**且第二道防線被靜默關閉**。`_stale_for_realtime` 與 `_kb_ceiling_date` **必須共用同一套資格判準**。
- **web 內容要能被引用才用得到**。`rq.SYSTEM_PROMPT` Rule 1/2/8（尤其「Every number you state must be traceable to a cited chunk」）會讓模型寧可捏造推導值也不碰 web 數字。放行條款**必須加在 system message**，user message 版本已實測無效。
- **白名單只是授權，不是過濾**。Tavily 的 `include_domains` 是**子網域包含式**比對，一筆 `finance.yahoo.com` 會連 `ca.`／`hk.` 一起收，而那些是**別的市場的報價**（同一天差 12%）。收到結果後還要用 `_host_allowed` 自己複核（只認 exact ＋ `www.`）。這個坑踩過兩次。**清單本身必須公司無關**（統一入口是 `sec.gov`），加一筆 IR 主機名就是 O(n) 的開始。
- **這條路的診斷成本主要在觀測性**：實測兩次根因都是「trace 印得不夠」才查不出來（只印字數看不出摘要被自己截掉、只印網域看不出日期為何抽不到）。**先把要判斷的東西印出來，再猜成因**——每猜錯一次就要重跑一次 LLM＋網路。

**改動前的自我檢查**
- 機制宣稱要先有**能證偽它的確定性測試**（零 LLM、可重跑）。實測有四個「聽起來合理」的機制假設被自己的測試推翻。
- 稽核腳本回傳「0 筆問題」先當**壞消息**查（實測兩次都是欄位名寫錯或量尺沒有判別力）。
- **反過來也一樣：稽核回報 FAIL，先問「是不是量尺錯」再問「系統怎麼壞的」。** 兩個方向的查證成本一樣是一次 `grep`。
- **量尺不可與被測物耦合**（這個專案犯過六次）。兩種形狀：① 量尺讀了修法會改動的產物（陽性一修好就消失）② 量尺**自備輸入**，於是從來沒驗過生產供不供得出那個輸入。
  → 通則：**驗證器讀哪個欄位，就要有一條斷言拿真實資料餵生產的建構路徑**，不能只驗資料源有那個欄位。

---

## 檔案地圖

> 一次性診斷腳本（`eval/_*.py`、`* copy.py`）與已退役的舊 eval harness（`eval_retrieval.py`、`eval_rerank.py`、`eval_chunk_recall.py`、`eval_two_stage.py`、`ablate_translate_rerank.py`、`classify_answer_shape.py`、`diagnose_crit_miss.py`、`latency_benchmark.py`、`rejudge_with_model.py`、`chunk_size_stats.py`、`eval_agentic.py`）不列於此表。
> 各檔的旗標語法與參數細節看該檔 docstring，這裡只寫「它是什麼、什麼時候該看它」。

### 生產／查詢

| 檔案 | 是什麼 |
|---|---|
| [`rag_query.py`](rag_query.py) | **檢索與生成的核心**，其他所有入口都 `import rag_query as rq` 共用 `retrieve()`／`call_llm()`／`build_user_prompt()`。`COLLECTION_NAME` 在此定義，**可用 env `RAG_COLLECTION` 覆蓋**。五個「唯一定義點」在這裡，新的消費端一律用它們、不要再寫一份：<br>· `finalize_answer_units` — 金額單位後處理的**唯一組裝點**（① 剝掉 LLM 自算的億 → ② 程式獨佔換算 → ③ 修剩下的孤立億）。⚠ **順序不可調換、只能呼叫一次**：② 不冪等，會匹配自己的產出。⚠ ③ 有**兩種獨立證據**：A 來源有 `$N billion`；B `②` 自己產出了 `N*10 億`。**B 不可省**——A 要求單位詞緊貼數字，而 SEC 的數字幾乎都住在 markdown 表格裡（`| Revenue | $82,886 |`，`In millions` 只在表頭）→ A 對表格來源**兩個方向都失明**（2026-09-04 實測）。不要改成「教 `_src_has` 讀表頭」：哪個表頭管哪個儲存格是感知不是規則。⚠ 不要裸呼叫 `convert_usd_units_to_yi`——它明文「不碰已經是億的值」，LLM 先斬後奏時**結構性失明**。閘門⑲ 守這件事<br>· `_payload_to_chunk` — payload→chunk dict 的**唯一建構點**（`period_basis` 曾經在 payload 裡卻沒被帶出來，害口徑 validator 結構性失效）<br>· `strip_evidence_tail`／`looks_like_refusal` — 「證據尾巴」與「這份答案算不算拒答」<br>· `_sole_ticker(filters, query)` — 「這是哪家公司」。**不要自己重跑 `_find_all_ticker_aliases`**：`parse_query_filters()` 的產出已含 regex ＋ LLM 兩個來源<br>· `_fiscal_sort_key`／`_get_period_ladder` — 期別的序<br>⚠ ticker **不是** `QUERY_FILTER_SYSTEM_PROMPT` 抽的（那支只抽 filing_type／fiscal_year／fiscal_period）——字面公司名（含中文別名）走 regex 表 `_COMPANY_TICKER`，**產品／子公司名走 `resolve_tickers_llm`，且只在 regex 沉默時才被叫** |
| [`api_server.py`](api_server.py) | FastAPI 後端，`/chat` SSE 串流。**產品線走的是這條（單管線）** |
| [`app.py`](app.py) | Streamlit 聊天前端，串 `api_server.py` |
| [`web_replay.py`](web_replay.py) | **Tavily 原始回應的錄／放**（live 路徑的 eval fixture）。未設 env `RAG_WEB_REPLAY` 時完全 no-op。⚠ 刻意錄在 `TavilyClient.search()` 的**原始回應**，不是 `_tavily_search()` 的回傳字串——後者已跑完白名單複核／去重／抽日期／過時過濾／截斷，錄那裡等於把要測的六道一起 mock 掉。⚠ `get()`／`put()` **兩邊都要 deepcopy**（下游會就地寫 `_pub_date`）。⚠ **`note_meta()` 只在 record 模式寫**：舊版無條件寫，於是每次 replay 都把 `_meta` 的綁定改成當下環境＝**證據自我抹除**，害「fixture 對不上 collection」三週沒有任何跡象。設計理由見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A8 |
| [`llm_replay.py`](llm_replay.py) | **A/B 用的重放快取**：把 plan／**replan**／英譯／Grader／ratio／期間意圖／ticker **七種**中間產物固定下來，讓兩臂差異只剩被測的那一項。未設 env `RAG_REPLAY_CACHE` 時完全 no-op。⚠ **A/B 兩臂共用一份 fixture 時必開 `RAG_REPLAY_READONLY=1`**：`atexit` 無條件回寫 → **先跑那臂的 miss 會變成後跑那臂的 hit**（實測 hit 23→32，兩臂不可比）。這條污染是**單向**的（永遠偏袒後跑的一方），而且兩臂外觀都「掛了 fixture」，看不出異常。唯讀刻意是 **opt-in**：`record_web_fixture --mode record` 與 `probe_historical_benefit`／`probe_temporal_interference` 都靠回寫補快取，預設改掉會讓那三支靜默地不再累積。`--mode replay` 已自動開。守門在 `verify_web_gate_isolation` 閘門⑫（10 項，5/5 變異全抓到）。⚠ 新增接點要一起註冊進 `_KNOWN_KINDS`，漏了只有 `strict:<kind>` 會現形（最少人走的路） |

### Agentic

| 檔案 | 是什麼 |
|---|---|
| [`agentic_rag_version/`](agentic_rag_version/) | **現役 agentic 入口**（2026-09-03 從單一 4549 行檔套件化）。**四個模組，分層是單向的**：`retrieval`（chunk 操作／KB 涵蓋／ratio 意圖）→ `validators`（**來源**資格：日期、過時、白名單、期別序 ＋ **答案**偵測：citation、衝突、R4/R5/R6、數字溯源、缺口揭露）→ `tools`（Tavily ＋ `rag_search`／`web_search`）→ `graph`（Plan／Grade 契約、route 分派與兩種 executor、LangGraph 四節點）。`__init__.py` 是**門面 ＋ 生成／補救層**（`*_check_and_fix` 會重生成，刻意不與零 LLM 的偵測器混在一起）。CLI 是 `python -m agentic_rag_version`。<br>⚠ **兩條套件化規則，違反了 eval 會靜默失效**：① 子模組**不得裸用**被 eval monkeypatch 的 14 個名字（含 `ENABLE_WEB_SEARCH` 這類常數），一律 `import agentic_rag_version as _pkg` 再 `_pkg.<name>`——**循環 import 是刻意的**，屬性在呼叫時才解析，stub 才蓋得到；② 子模組**不得 `from .x import` 那些名字**（那會在呼叫端壓一份當時的物件）。**定義在哪個模組無所謂。** 守門在 `verify_web_gate_isolation` 閘門⑬。<br>⚠ `__init__` 的 re-export 名單**從各模組 AST 生成、不手列**（手列實測漏過 9 個，⑬f 守它）。 **節點設計理由與 validator 實測見 [`docs/AGENTIC.md`](docs/AGENTIC.md)** |

**⚠ agentic 一題燒 50~60 次 LLM 呼叫**（單發管線 3~4 次）：每個子問題內部各跑一次完整 `rq.retrieve()`，所以查詢理解層被乘上子問題數。全碼庫 `call_llm` 共 15 個呼叫點。

### Ingest

| 檔案 | 是什麼 |
|---|---|
| [`fetch_data.py`](fetch_data.py) | **唯一對外抓取入口**：edgartools SEC filing ＋ yfinance 基本面 → `data/raw/` ＋ `sec_manifest.json`。份數用 `--annuals N --quarters N`。⚠ 新聞是 `--with-news` **opt-in（預設不抓）**，且抓了也不會進 KB |
| [`data_update_edgar.py`](data_update_edgar.py) | **唯一 ingest 執行入口，零網路**：讀本機 `.nc` → 切塊六層 → 寫入 collection。三種 doc_type 都在這裡；**News 已由 `RAW_EXCLUDE_DIRS` 於掃描階段排除**。**切塊各層、增量策略、踩過的坑見 [`docs/INGEST.md`](docs/INGEST.md)** |
| [`unstructured_components.py`](unstructured_components.py) | **函式庫，非執行入口**。unstructured 解析與切塊組件、表格處理（caption／Groq 摘要）、`BGEM3DenseEmbeddings`。只被 `data_update_edgar.py` import |
| [`data_update.py`](data_update.py) | 最舊的 ingest 管線，已被 EDGAR 版取代，保留對照 |

### Eval — 輸入與標準答案

> **`eval/` 只放輸入與標準答案，跑分輸出一律寫到 `experiments/`**（`--output` 的預設值仍指向 `eval/`，要自己帶）。

| 檔案 | 是什麼 |
|---|---|
| [`eval/eval_set.json`](eval/eval_set.json) | **65 題**題庫。2026-08-19 從 100 題拆出 63 題（37 題 gold 含新聞者移出），同日再補回 `lex-16`／`lex-17`。**跨該日的分數不可直接比**（分母變了兩次） |
| [`eval/eval_set_news.json`](eval/eval_set_news.json) ＋ `reference_answers_news.json` ＋ `number_claims_news.json` | **冷凍的 37 題**。⚠ **現在不要拿它跑分**：eval 預設 web 是關的，跑了必然全滅、那不是證據。用途是日後驗收「live web 能不能取代 KB 新聞」 |
| [`eval/reference_answers.json`](eval/reference_answers.json) | RAGAS 的 ground truth，**已納入版控**（含 24 處人工校正、非腳本可免費重現） |
| [`eval/number_claims.json`](eval/number_claims.json) | `check_number_defects` 的逐條主張。每條都要**人工驗證過才登錄**（`verified` 欄位寫怎麼驗的）。`status` 兩種：`known_defect`／`regression_guard` |
| `eval/period_probe_*.json`、`web_claims*.json`、`web_fixture*.json`、`news_routing_questions.json`、`replay_cache.json` | 各探針／閘門的 fixture、題目分類與凍結基準 |

### Eval — 腳本

**跑分主線（三步；reference 生成一次即可重用）**

| 檔案 | 是什麼 | 跑在哪 |
|---|---|---|
| [`eval/gen_reference_answers.py`](eval/gen_reference_answers.py) | ① 從黃金來源檔生成參考答案。⚠ `--force` 不帶 `--ids` 是全量重生成，**會洗掉 24 處人工校正** | `.venv` |
| [`eval/run_agentic_on_evalset.py`](eval/run_agentic_on_evalset.py) | ② agentic 版結果檔（含 `contexts` 供 RAGAS） | `.venv` |
| [`eval/eval_generation_llm_judge.py`](eval/eval_generation_llm_judge.py) | ② 單管線版：真實 retrieve+generate ＋ 3 維判定 | `.venv` |
| [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) | ③ 讀結果檔 → RAGAS 六指標。跑完會印噪音門檻與 gold 上限的判讀護欄。⚠ `--from-results` 吃多個檔是「拼接同一臂」**不是 A/B**（同 id 會被平均）；只改少數題的 reference 時用 `--ids` 只重評那幾題 | **`.venv-ragas`** |
| [`eval/record_web_fixture.py`](eval/record_web_fixture.py) | live web fixture 的唯一產生入口（`--mode record` 會連網燒額度；`--mode replay` 絕不連網）。⚠ **必須同時開 `RAG_REPLAY_CACHE`**：送給 Tavily 的 query 由 LLM 決定，只錄 web 會在重放時 miss。<br>⚠ **`--mode replay` 是 bare `strict` ＋ 唯讀**（2026-09-02）：strict 讓 fixture 沒涵蓋當場炸，唯讀讓重複跑不會偷改 fixture。**換一份 fixture 或加新接點時要退回 per-kind strict**，否則會炸在「這份 fixture 錄的時候還沒有這個接點」＝假陽性。<br>⚠ **重錄前一定要清空 `--replay-cache`**：舊快取的 plan 若是加 `route` 之前的純字串陣列，`_parse_plan_output` 會把它們全部落到 `kb` → **一次 web 都不打，錄出一份空 fixture**。 | `.venv` |
| [`eval/migrate_fundamentals_pct.py`](eval/migrate_fundamentals_pct.py) | 一次性遷移（已執行）：Fundamentals 比率小數 → 百分比，**並同步修 gold** | `.venv` |

**確定性閘門（零 LLM、可重跑、秒級——改動前後都該跑）**

| 檔案 | 是什麼 |
|---|---|
| [`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py) | **query-understanding → Qdrant filter 的 Python 半邊**，八道閘門 85 項。①`_fiscal_sort_key` 真值表（含 NVDA 財年反轉）②ladder 排序 vs 期碼字串比大小 ③`ladder_pick`（挑不到必須回 None，**不可退而求其次**）④`period_ref` → filter（只有 `latest` 該產生 filter）⑤`range` → 跳過 collapse 的接線 ⑥Tier 1 的 label-year 命中資格 ⑦實體解析的接線 ⑧期間路由讀的是**已解出的** ticker。<br>⚠ 判別力集中在三處，改動時特別看：**⑥b**（⑥a 全餵合成 payload，ingest 哪天讓 10-K 的兩個欄位分歧，⑥a 照樣全綠而整個推理前提會崩）、**⑦b**（regex 抽得到時 LLM 必須一次都不叫）、**⑧g**（⑧a~⑧f 全是手餵 filters，呼叫端引數被重構掉它們照樣全綠）。<br>⚠ 閘門② **在單年 collection 上幾乎沒有判別力**，腳本會自己印出「判反幾對」並在 0 對時明說**那是測資不足不是系統健康** |
| [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py) | **Synthesize 端的確定性 validator**，十九道閘門 265 項（只讀 Qdrant coverage）。①`_fiscal_rank` 排序 ②期別陽性 ③期別陰性對照 ④`_news_freshness_gaps` ⑤缺口→警語接線 ⑥端到端回歸 ⑦`find_untraceable_numbers` ⑧R4 web↔財報衝突**要求並陳不裁決** ⑨`_repair_reference_citations` ⑩`_ensure_ratio_source_coverage` ⑪`_basis_disclosure_notice` ⑫ratio 意圖詞表退位 ⑬拒答不得附引用清單 ⑭Synthesize 四道 validator 守門 ⑮期間降級揭露的接線**與歸因**。<br>⚠ **⑭／⑮ 的 AST 掃描讀的是「整個套件的每個模組」不是 `ar.__file__`**（2026-09-03 套件化時改）：單一檔時 `ar.__file__` 就是全部原始碼，套件化之後它只是 `__init__.py`，被拆進子模組的函式會**查無此人**。修法不該是「把函式搬回 `__init__` 遷就量尺」——那是量尺與被測物耦合。`_pkg_funcdefs()` 另附一條「套件裡沒有跨模組同名函式」的前提斷言（保留先掃到的那個會讓後面每條斷言都可能驗到另一個同名函式）。⚠ 它**只收 top-level 函式**：用 `ast.walk` 會把類別的 method 撈進來，於是每個 `__init__` 都被記成重複——這是加上檢查當場誤報出來的。<br>⚠ **這支的價值幾乎全在誤報對照，而不是陽性斷言**——多數陽性一個「一律不做」的實作也會通過。五個最重要的：**②**（陽性答案**逐字凍結在測試檔裡，不讀 `experiments/`**，否則一修好陽性就消失）、**⑩b/⑪b**（拿真實 payload 餵**生產建構子**，⑪ 原本 8 條全餵測試自造的 dict 於是全綠、而生產根本沒把那個欄位放進 chunk dict）、**⑬b/⑭d**（拒答判過頭＝把有依據答案的 provenance 砍掉／validator 全部跳過）、**⑭a**（逐個 validator 走 AST 驗，改三道漏一道要叫得出來）、**⑮e**（⑮b 只驗「呼叫點寫了 `period_note=`」，那照樣可能傳一個永遠是空的變數——⑮e 驗**值**真的從 state 流到 Generator）、**⑮f**（⑮a~⑮e 全部只問「有沒有接上」，只有 ⑮f 問「這句話**該不該說**」——揭露句寫的是「**所詢問**財年」，而那個年份可能是 Grader 補救改寫憑空加的，見 [`BACKLOG.md`](BACKLOG.md)〈已知缺陷〉的 col-10）、**⑮g**（⑮f 只擋得住「Grader 在同一個子問題內改寫」；**replanner 另外加一個 todo** 時，那個 todo 的 rnd 0 照樣算「第一輪」→ 假前提從另一扇門回來而 ⑮f 全綠。所以歸因掛在 **todo 的出身**上：`_node_plan` 建的標 True、`_node_replan` 建的標 False）。<br>⑱**R6 抓到 web 卻一個都沒引用 → 揭露**（8 項）：`web_extra` 非空且答案零 web 引用時，機械式附一句「網路結果未採用」。⚠ **是揭露不是重生成**：web 真的回垃圾時那句話字面為真，誤報方向安全。⚠ **必須掛在 Synthesize 不能掛 todo 層**：`_format_unresolved_freshness_notice` 對 `web_used` 為真的待辦直接跳過，而這裡的病正好是「`web_used` 為真、答案卻沒引用」。⚠ **⑱f 是這道最重要的一條，而且第一版是恆真的**：原本驗「附上揭露句後 `check_news_routing.classify` 判定不變」——那必然成立，因為揭露句以 `\n\n---\n⚠` 開頭、命中 `rq.EVIDENCE_TAIL_RE`，消費端根本看不到它（變異測試證實：改成含承認詞的措辭照樣全綠）。現在驗的是**讓它安全的那個不變量**——邊界前綴，並配一條誤報對照（⑱f2：拿掉前綴，同一段文字會把 `kb_only` 翻成 `admits_gap`）。**凡是「機械式附加到答案」的東西都要驗這件事**，否則量尺會被受測物的輸出改變。<br>⑰**R5 並陳必須帶時點**（12 項）：答案把 web 金額與財報金額並列時，兩邊都要在**同一句**裡交代時點。⚠ R4 抓不到這個——它的 bucket key 含 `unit`，而 `$4.75 兆` 與 `$4342.02 billion` 落到**不同桶**。⚠ **判別力幾乎全在誤報對照，而其中三條是乾跑實際踩出來的**：**⑰c**（U+2011 不換行連字號寫的日期，第一版 281 份答案裡 4 個觸發有 **3 個**是這個字元造成的誤報——凡是拿 regex 讀 LLM 寫的日期都要先正規化 dash）、**⑰f**（新聞敘述型引用；只要求「同時引用 web 與財報」的第一版觸發率是 **34/123＝28%**，收窄成「兩邊都要有可比較的金額」後變成 **1/123 且那 1 次是真陽性**）、**⑰g**（量級差 >10 倍＝不是同一個量）。⚠ **⑰i 是變異測試逼出來的**：第一版拿 ⑰a 的測資測「檔名期別戳算不算時點」，而那份測資的財報句裡本來就寫著日曆日期 → 檔名戳那條路**從來沒被測到**（把 `_FISCAL_MARK_RE` 拿掉全綠）。<br>⑯**rnd 0 的 query 逐字 ＋ 歸因只在 rnd 0**（6 項，2026-09-01 加）：⑮f/⑮g 判斷「該不該說『所詢問財年』」的**前提**就是這裡——誰讓 rnd 0 先改寫一次，⑮ 全綠而前提已經崩了。⚠ **刻意是行為測試不是 AST**：AST 看得到「有沒有寫 `active_query = task`」，看不到「第一次真的送出去的是哪個字串」。⚠ 判別力在 ⑯c/⑯e 兩條誤報對照（每輪都用 task＝`MAX_REWRITES` 空轉；歸因只看 `rnd == 0` 而漏掉 todo 出身）。4/4 變異全抓到。<br>另 ⑮g6/⑮g7：todo 的 **`route` 值**真的流到 executor（不是呼叫點寫了 `route=` 而已，同 ⑮e 的教訓），且**沒有 route 的舊 todo 退回 `kb`** ＝ 與加 route 之前逐字相同的行為。<br>⑲**金額單位三層後處理**（26 項，2026-09-03 加、09-04 補證據 B）：`rq.finalize_answer_units`。⚠ **價值幾乎全在誤報對照**——陽性那三條，一個「一律把億剝掉」的粗暴實作也會過；會出事的是它動到不該動的（**⑲e** 非金額計數「10 億使用者／2.57 億股」、**⑲f** 敘述性括號）或**擾動既有基準**（**⑲d** LLM 照 Rule 11 寫的正常路徑必須與單獨跑 `convert_usd_units_to_yi` **逐字相同**）。⚠ **⑲g 是最重要的一條**：三層的正確性全建立在順序上，而順序只是註解裡的宣稱——⑲g 把順序調換證明它當場壞掉。**這條的症狀第一版猜錯了**：原本斷言「調換會吐巢狀」，實跑發現 `repair_paired_yi` 會把巢狀自己拆掉、於是**乾淨地留下 LLM 那個錯值**（84.75 而非 847.5），外觀完全正常、危險得多 → 現在斷言的是**值**不是格式。⚠ **⑲i 掃三個檔**（`rag_query`／`graph`／`api_server`）驗「有沒有別的路徑繞過這層」，並要求裸呼叫 `convert_usd_units_to_yi` 的次數為 0（`rq` 內部那一次除外）。<br>⚠ **⑲k 這一組（8 項）是端到端跑出來才補的，不是想出來的**：⑲a~⑲j 全綠的情況下，真實 agentic 答案仍吐出 `828.86 億美元（$82,886 百萬），相當於 $82.886 億美元`——同句自相矛盾、後半差 10 倍。成因是 ③ 的證據 A 對**表格來源**結構性失明（見上方 `finalize_answer_units` 那格）。**教訓是量尺的測資形狀**：⑲h/⑲h2 的來源是手寫的英文句子 `Total revenue was $84.75 billion`，而生產的來源是 markdown 表格——**測資與生產的來源形態不同，那條路等於從來沒被測過**。⑲k2 因此是前提斷言（先證明證據 A 對這份表格確實失明，否則 ⑲k 測到的是別條路）。⚠ 判別力仍在誤報對照：**⑲k4**（兩個**真的**差 10 倍的獨立金額——變異測試證實拿掉 `_DUAL_PAREN_RE` 會把 `82.886 億美元（$8,288.6 百萬）` 改成 `828.86`，是真的把對的改壞）、**⑲k5/⑲k6**（② 無產出時證據 B 必須整條靜默，擋掉退化成「看到億就乘 10」）、**⑲k7**（非金額計數不因 ② 剛好產出 10 倍值而被改）。3/3 變異全抓到 |
| [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) | **web_search 的 eval 隔離與資料流**（零網路、零 Qdrant），**十三道閘門 181 項**。①eval 隔離真值表 ②`web_extra` 跟著每次重生成 ③**無 web 時 system message 逐字不變**（＝eval 基準不被動到的證明）④白名單有生效、濾空不退回全網、**清單必須公司無關** ⑤Grader 時效判準的日期算術（含 `kb_unfixable` 誤殺真值表與時效證據資格）⑥web 結果整理（摘要不得截掉深處數字、地區子網域要擋、單域名上限、去重、預算）⑦replay 的 miss 不准被任何 `except Exception` 吞掉 ⑧`replan` 必須進重放快取、且 key **不含自由文字結果** ⑨fixture 的環境綁定不准被 replay 改寫、且開跑前要被比對。⑩web 結果的日期抽取（`published_date` 全空時退回**內容裡標記相鄰**的日期）。⑧ 另含「intraday 且已搜過 web 時，追加待辦一律拒絕」的判準與接線。<br>⚠ 閘門① 的 `_gate()` 是生產判斷式的**抄寫不是 import**，改那一行務必同步改這裡。<br>⚠ 閘門⑦ 守的是一條**結構性**保證（`FixtureMiss`／`RecordError`／`ReplayCacheMiss` 不繼承 `Exception`），不是契約。舊版靠「呼叫端記得 re-raise」，而全碼庫有 16 個 `except Exception`——漏一個就把「fixture 沒涵蓋」變成「系統沒打 web」，兩者外觀完全相同。<br>⚠ 這支在 **import 時**就把 `ar._tavily_search` 換成 stub（絕不連網的保證），所以要測真身一律用 `_REAL_TAVILY_SEARCH`——閘門④ 與 ⑦e 各自踩過一次。<br>⚠ 閘門⑧ 的判別力**全在誤報對照**（⑧d~⑧g）：只驗「快取會命中」的話，一個「key 是常數」的實作也會滿分，而那會讓**所有** replan 決策互相蓋掉。每個「應該要 miss」的維度（task／freshness_mode／query／status）都各有一條。<br>⚠ 閘門⑨ 同理要有 ⑨b：只驗「replay 不寫」的話，一個 `note_meta` 直接 return 的實作也會通過——那樣連 record 都不寫，fixture 從此沒有綁定可查。<br>⚠ **⑧p 是一條被踩出來的斷言**：守衛第二版用 `_is_web_todo(task)` 當前置條件，而 `_WEB_TODO_RE`（`網路|上網|web search|internet`）**匹配不到「在 Yahoo Finance 上查詢」「使用 NASDAQ 官方網站」**——六個真實措辭逐字凍結在那裡。<br>⚠ 閘門⑩ 的**危險方向是不對稱的**：抽到**太新**的日期會讓過期頁冒充新鮮並替整池背書，抽不到只是維持現狀。所以誤報對照（未來的財報日／除息日、歷史表格列、無標記、無日期標記收編隔壁表格）比陽性斷言重要。測資是 2026-09-01 一次真 Tavily 呼叫的**逐字內容**。<br>⚠ `_MARKER_WINDOW` 的**數值**（48）沒有任何斷言分得出來（變異測試實測 24 與 48 皆全綠）——⑩o 測的是「窗口存在」不是那個數字。那個值是判斷，不要當成有證據支持。<br>⑪**路由欄位與確定性分派**（30 項）：`route` → tool 真值表／升級規則／Plan 輸出雙格式解析／**`_effective_route` 的 eval 隔離**。⚠ **⑪t~⑪y 是變異測試逼出來的**：把 `_effective_route` 的隔離整段拿掉（snapshot 也照 route 走 ＝ eval 直接連網），**當時兩支閘門一條都沒響**——因為閘門① 的 `_gate()` 是**抄寫不是 import**，抄的還是加 route 之前的條件。隔離必須在**它現在真正住的地方**再被驗一次。⚠ 判別力集中在三條誤報對照：**⑪f** 非法 route 必須當場炸（靜默預設成 kb 的失敗外觀與「路由判成 kb」完全相同）、**⑪i** `kb`＋不足但非 `kb_unfixable` → **仍然 kb**（少了它，一個「不足就上網」的實作全綠＝改了個寂寞）、**⑪n** 舊格式 `list[str]` 的 route 預設必須是 `kb`（既有 fixture 錄的 plan 值全是字串陣列，改預設＝所有重放行為悄悄改變）。<br>⑬**套件化之後 monkeypatch 仍攔得住**（9 項，2026-09-03 加）：eval 全靠 `ar.<name> = stub` 攔截，而那是改**套件物件**上的綁定；呼叫端一旦寫成 `from .webtools import _tavily_search`，名字就綁死在呼叫端 globals 裡，**stub 再也蓋不到**→「eval 絕不連網」會真的連網，而閘門照樣全綠（它只數自己那個 stub 被叫幾次）。常數同病（`ENABLE_WEB_SEARCH`／`QUERY_WEB_BUDGET` 也被 eval 直接改寫），所以 **⑬c 抓的是所有裸引用不只是呼叫**——常數是 `Name` load 不是 `Call`，只看呼叫完全看不到它。⚠ **危險的是「用 import 複製綁定」不是「定義」**：`def _tavily_search` 住哪個模組都無所謂，只要沒人裸用、大家走 `_pkg.`，patch 就蓋得到；真正讓 stub 失效的是 `from .webtools import _tavily_search`（在呼叫端 globals 壓了一份當時的物件）。第一版連定義都算違規，代價是那 13 個名字被永久釘在 `__init__`、檔案瘦不下去——**⑬e4 就是擋這個誤判方向的反向誤報對照**。⚠ **⑬a 從 eval 腳本反推 patch 名單而不是只讀凍結清單**：漏掉一個站點的失敗方式，與「那個站點不存在」外觀相同。⚠ **判別力全在 ⑬e1~e3**：拆分未完成時 ⑬b~⑬d 是**真空成立**，腳本會自己印出「子模組 N 個」，N=0 時那三條回報 OK **不代表有判別力**。⑬e 拿故意造出來的違規餵同一套檢查。⚠ **⑬f 是被踩出來的**：`__init__` 的 re-export 清單手列，拆 validators 時漏了 `_WEB_UNCITED_MARK` → 閘門⑱ 當場 AttributeError；回頭重算 `freshness`，**手列的 27 個裡漏了 9 個**（只是還沒被任何斷言碰到）。**手列必漏，而漏掉的失敗方式是「有人用到才爆」**，可能拖很久才現形。<br>⑫**replay 唯讀模式**（10 項）：A/B 共用 fixture 時 `atexit` 回寫造成的**單向污染**。⚠ 判別力集中在三條：**⑫e** 先重現污染再證明它消失（少了前半，「armB miss」可能只是這個測試沒建立前提）、**⑫i** 唯讀時**既有的** key 照樣 hit（擋掉「唯讀＝把模組關掉」，那會讓兩臂**一起**失去 fixture，比污染更糟且更難察覺）、**⑫f** 唯讀時 hit/miss 照樣印（非唯讀唯一的出口是 `[replay] wrote` 那行，唯讀不寫檔就沒有 → miss 完全隱形，外觀與「系統沒走那條路」相同）。⚠ 守衛放在 `put()` **早退**而不是 `_flush()`：那是守入口不是守出口，變異 N3 專測這一條 |
| [`eval/verify_cross_period_collapse.py`](eval/verify_cross_period_collapse.py) | 跨期 field collapsing 的規則，17 項（毫秒級）。兩條最容易在重構時被破壞的界線各有專屬斷言：①同一份 filing 的同節多 chunk **全留** ②別份 filing 插隊後、原本那份的後續 chunk 仍要留。⚠ **只測規則不測有沒有用**——collapse 的規則與探針的 `crowding_seats` 是同一個定義，開了必然歸零，那是套套邏輯 |
| [`eval/verify_segment_split.py`](eval/verify_segment_split.py) | ingest 的三層硬邊界（期間／小標／幅度接地）。零網路、零 embedding、不碰 Qdrant → **可在別的實驗跑的時候執行** |
| [`eval/verify_table_captions.py`](eval/verify_table_captions.py) | 表格 caption 品質（只讀 Qdrant）。**重建後必跑**：`missing_on_big` 是 Groq 429 靜默降級的唯一出口。⚠ `no_caption` 本身不是缺陷（小表不值得花 LLM call） |
| [`eval/verify_chunk_grounding.py`](eval/verify_chunk_grounding.py) | **chunk 層**的幅度接地（只讀 Qdrant ＋ reranker tokenizer）。**重建後必跑**——`verify_segment_split.py` 量的是 section 層，SemanticChunker 之後的邊界它看不到 |
| [`eval/audit_gold_numbers.py`](eval/audit_gold_numbers.py) | **gold 自洽性稽核＝跑任何評測前的前置閘門**。⚠ 不查 filing 類的數字（大乾草堆裡「值有沒有出現」不帶資訊） |

**驗收指標（零 LLM，只讀結果檔）**

| 檔案 | 是什麼 |
|---|---|
| [`eval/check_number_defects.py`](eval/check_number_defects.py) | **「數字答錯」類改動的主要驗收指標**。逐條斷言判 PASS／FAIL／N/A 三態。**跑之前先 `--selftest`**。⚠ **N/A 不能併進 PASS**——它是「這一輪沒量到東西」不是通過，第五、第六次失效都是這樣抓到的。⚠ **一個 id 可以掛多條主張**（`lex-17` 掛 `require_chunk` ＋ `anchored_pct`：進池 ≠ 有用它）。六次失效史見 [`docs/EVAL.md`](docs/EVAL.md) |
| [`eval/check_historical_generation.py`](eval/check_historical_generation.py) | **多年語料在生成端買到了什麼**：逐題判三態（答對／承認期間不可得／**拿別年份硬答且沒交代**）。⚠ **不要用 `looks_like_refusal` 判「誠實」**：那個函式問的是「整份答案都不作答」還帶 150 字上限，而最典型的誠實答案是**長的**。⚠ 它的 before 臂**同時是比值比對更強的前提檢查**（抓到過兩題因重述而無效的題目） |
| [`eval/check_news_routing.py`](eval/check_news_routing.py) ＋ `news_routing_questions.json` | **新聞題有沒有拿財報冒充新聞**（零 LLM／零網路／零 Qdrant，只讀結果檔）。KB **依設計沒有新聞**（`RAW_EXCLUDE_DIRS`），所以新聞題引用 10-K／10-Q 就是缺陷——而 Synthesize 的五道 validator **一道都不會響**（chunk 是真的、數字溯源得到、期別也對）。先跑 `--selftest`。<br>⚠ **狀態名中性、判定分類相對**：`kb_only` 在 flow 題是危險態、在 record 題是正確行為。把分類烘進狀態名，record 那一半的誤報就再也看不見了。<br>⚠ **`record` 那一半（陰性對照）不可省**：只量 flow 的話，一個「所有題目一律送 web」的實作會滿分——而那正是它要驗收的改動最可能的失敗方式。<br>⚠ **判別力集中在一條規則：承認措辭必須與流資訊範圍詞同句**。詞表刻意寫寬，判別力放在同句共現；實測整篇比對會在 40 份答案裡誤報 3 筆，分屬三種形狀（承認的是別的細節／跨句湊對／「未提供」出現在純業務敘述裡），三型各有逐字凍結的誤報對照。<br>⚠ **`mixed` 是「不對稱」判定**（2026-09-02 改）：原本整個 kind 不判定，理由是「分不出新聞那一半是不是也用財報答的」——**那句話只對好的方向成立**。`web_grounded` **仍然不判定**（有一個 web 引用不代表新聞那一半用的是它，要判需逐宣稱歸屬＝感知）；`kb_only`／`ungrounded` **判危險，而且是純規則**（mixed 依定義帶著流的半邊，整份答案零 web 引用又沒承認 → 那半邊必然不是用 web 答的，與 flow 同一個推論）。少了這一格，22 題的危險方向完全沒有量尺；加上去當場撈出 `mh-09`。<br>⚠ **第二軸的歸因條件是讀判定表**（`"危險" in _VERDICT[kd][st]`）**不是寫死 `kd == "flow"`**：舊版寫死，於是 mixed 有了危險態之後 `mh-09` **有判定卻在第二軸完全不出現**。列 kind 清單就會漏下一個。<br>⚠ **不驗 web 答案對不對**（那是 `check_web_claims.py`，它要 fixture 這支不要），**也不驗 record 題答得對不對** |
| [`eval/check_rounding_fidelity.py`](eval/check_rounding_fidelity.py) | 量「答案把來源數字捨成約值」＝`SYSTEM_PROMPT` Rule 8 NUMERIC FIDELITY 唯一能被證偽的方式。⚠ **歸屬必須逐引用**：整篇 contexts 比對會把「約 18%」放行（同題 10-K 確實寫著 `increased 18%`）——**有來源，但不是那一句掛的那個來源**。⚠ **統計效力很低**（見 BACKLOG）。先跑 `--selftest` |
| [`eval/check_comparison_claims.py`](eval/check_comparison_claims.py) ＋ `comparison_claims.json` | **「哪一家最高」這個結論對不對**（`multi_hop` 5 題唯一的自動化證據）。全碼庫**沒有一行 Python 在做這個比較**——它整個交給 Generator 讀 chunk 自己比大小，而比錯了 Synthesize 的五道 validator **一道都不會響**（citation 合法、數字溯源得到、期別也對）。先跑 `--selftest`。<br>⚠ 真值**從答案自己的 `contexts` 算**，不是重跑檢索也不是查 gold 檔——否則 FAIL 會混進「檢索沒撈到」這個完全不同的病。這樣 FAIL 的意思才毫不含糊：**數字就攤在它面前還是挑錯了**。<br>⚠ 三態不可合併：`wrong_winner`（危險）／`unfounded`（某家的值不在 contexts 卻仍宣告贏家，**獨立缺陷**）／`N/A`。<br>⚠ **判別力上限**：現有語料**每一題差距都很大**，沒有接近值、沒有缺值、沒有跨單位（B vs M）。全 PASS 只證明容易的情況不會錯，**不能外推**。<br>⚠ 「答案宣稱哪一家」是**感知不是規則**，第一版在 40 筆裡誤報 2 筆（題目重述清單被當成宣稱、「A 領先於 B」的錨點後面是輸家）。兩個真實句型已逐字凍結成 selftest ⑧⑨，並各配一個誤報對照 |
| [`eval/check_web_claims.py`](eval/check_web_claims.py) ＋ `web_claims.json` | **live web 路徑的逐條確定性驗收**＝這條路唯一的自動化證據。只測**含 LLM 的端到端**：有沒有引用 web、主機是否在白名單且非地區子網域、**答案裡每個報價是否都在 fixture 原始內容裡逐字出現**、KB 舊值與時點是否與 web 新值並陳。⚠ 斷言只能測「不論 LLM 挑 fixture 裡哪個來源都成立」的性質。<br>⚠ **它曾經 flaky，成因是 fixture 不是斷言**（fixture 綁在拔除新聞之前的世界 → `check` key 全 miss → 新 query → `FixtureMiss`）。2026-09-02 重錄後跑 **4 輪判定逐題完全一致**（`hit=36 miss=0`），而**四輪的答案全都不一樣**——後者才是「斷言沒被凍住」的證明，只看前者會把「量尺死了」誤讀成「量尺穩」。<br>⚠ **`blocked` 狀態**：fixture **在輸入層**就承載不了某條斷言時（實測 web-01：Tavily 兩次獨立錄製逐字回傳同一批不含 Apple 市值的垃圾），標 `blocked` → 判 **N-A**、理由與證據逐字印出，而 **N-A 照樣讓退出碼非零**。判準：**FAIL 的成因在輸入 → blocked；輸入有而系統沒用上 → 真 FAIL，不准 block**。⚠ 刻意**不做成自動前置條件**（拿 regex 去 fixture 找）：`fixture_text` 是整份攤平的 blob，一條樣式會匹配到**別題**的回應 → 在錯誤的證據上放行。 |
| [`eval/web_claims_news37.json`](eval/web_claims_news37.json) ＋ `web_fixture_news37.json` | **冷凍 37 題在 live web 上的性質斷言**（40 條，含 3 題陰性對照），as-of 2026-08-20。⚠ **刻意不對照新聞 gold**：那批 gold 是「2026 年 6 月的新聞說了什麼」，live web 回答「現在的網路說什麼」——**是兩個不同的問題**。⚠ **不能用 `numbers_must_be_in_fixture`**：題目天生混合，答案裡 KB 與 web 數字並存，沒有字面樣式分得開 |

**探針（含 LLM、非零噪音、不是閘門——它們量的是「這個 LLM 判斷準不準」）**

> 共同判讀規則：**看不對稱的錯誤**，不是整體準確率；**陰性對照不可省**（少了它「一律回 X」也會滿分）；MoE 不固定路由，**下結論前 `--repeat 3` 以上**。

| 檔案 | 量什麼 / 不對稱在哪 |
|---|---|
| [`eval/probe_ticker_resolution.py`](eval/probe_ticker_resolution.py) | 產品／子公司名 → 母公司 ticker。⚠ **只有這一支在量它**：eval_set 65 題全部由 regex 解出 ticker，這個修法在既有跑分上量不到差異。**解不出來** ＝ 退回修法前（只是沒改善）；**解成別家** ＝ hard filter 鎖到別人的財報＝**比修法前更糟**。陽性臂拆兩層（知名產品／10-K 分部名），難度在後者 |
| [`eval/probe_route_classification.py`](eval/probe_route_classification.py) ＋ `news_routing_questions.json` | **Planner 判的 `route` 準不準**。閘門⑪ 驗的是 route 有沒有被正確**接線**，這支問「判得對不對」——搬進欄位讓路由**可稽核**，不代表它**準**。<br>三個指標不可合併：`危險誤判`（flow → 純 kb ＝ 拿舊財報回答「最近有什麼消息」，五道 validator 一道都不會響）／`浪費誤判`（record → web/both ＝ 不撈 KB、丟掉期別 metadata 與引用鏈）／**`跨輪不一致`（與前兩格正交——兩輪都對可能只是運氣）**。<br>⚠ **必須帶 `--as-of` 且與被比較的那次跑分一致**：as-of 會進 `_build_temporal_contract`＝Planner prompt 的一部分。第一版漏了它，量出 0/115「完全穩定」，而同一題在真實跑分裡翻面過——**量尺與被測物的環境要綁在一起**。<br>⚠ `RAG_REPLAY_CACHE` 開著會讓每輪命中同一份快取 → 假的穩定；本檔**開跑前中止**（exit 2）。<br>⚠ `probe_only` 的 8 題定性財報題**刻意不進任何 eval set**（進去就改動分母）。它們是**看到失敗族之後才寫的**，所以拿它們量到的改善帶有貼合成分——擋這件事的是 flow 那半的誤報對照 |
| [`eval/probe_ratio_intent.py`](eval/probe_ratio_intent.py) | ratio 意圖的 LLM 分類。**fallback 會遮住 LLM 的失手**（判不出來就退回詞表），所以準確度**不能從結果檔推**。ratio 題判成非 ratio ＝ 保底不執行、口徑警語不觸發 → **拿財年數字冒充 TTM 且零揭露**；反向只是多撈一個 chunk |
| [`eval/probe_realtime_need.py`](eval/probe_realtime_need.py) | Grader 的 `realtime_need` 分類。實時題判成 `none` ＝ 不叫 web、拿 10-K 回答今天股價；財報題判成實時 ＝ 白燒一次 web |
| [`eval/probe_replan_contribution.py`](eval/probe_replan_contribution.py) | **Replanner 加的待辦到底貢獻了什麼**。2026-08-28 給 todo 加上 `attributable` 之後才做得出來（在那之前「哪些待辦是機器造的」只能從 trace 字面猜）。四個指標不可合併：`n_new`（先前待辦沒撈過的 chunk）／**`n_new_cited`（那些 chunk 最終真的被引用）**／web／秒。<br>⚠ **`n_new` 高不等於有用**——撈到別人沒撈到的，跟那東西被寫進答案，是兩件事。<br>⚠ **誤報對照＝把 Planner 的待辦用完全相同的方式量並排印出來**。少了它，「Replanner 貢獻 0」可能只是這個指標沒有判別力。<br>⚠ 零網路靠把 `_tavily_search` 換成計數樁，但**樁必須回傳像樣的非空內容**——回空字串等於先驗地判定 web 待辦沒貢獻。**因此這支量的是 KB chunk 的貢獻，不是 web 的** |
| [`eval/probe_relevant_ids.py`](eval/probe_relevant_ids.py) | Grader 的 `relevant_ids` 圈選＝**承接池的實際守門員**（有圈選就只收圈選的，沒圈選才退回 rerank top-k 全收）。2026-08-28 之前完全沒有量尺（`eval/` 裡每一筆 `relevant_ids` 都是測試樁）。四個指標不可合併：`gold 全滅`（危險）／合成混池的`誤選他家`（陰性對照）／`圈選率`（**1.00 ＝ 這一題等於沒過濾**）／`空圈選`。<br>⚠ **公司層的陰性對照在自然候選池裡不存在**——ticker hard filter 在上游就濾掉了，所以第一版八題的「未點名公司」全是 0＝零判別力。現在改用 `MIX`（同一問題換一家公司，兩半都走生產 `rq.retrieve` ＋ 生產 `_merge_chunks`）**合成**混池，判讀時要記得這一格是合成的。<br>⚠ 「gold 檔的**部分** chunk 沒被圈選」**刻意不算缺陷**：gold 只到檔名，排除同檔裡離題的 chunk 正是這個欄位該做的事。第一版把它當危險指標，量出「4 題危險」——那是量尺的錯 |
| [`eval/probe_news_web_routing.py`](eval/probe_news_web_routing.py) | 冷凍 37 題**會不會**被送去 web（零 Tavily）。⚠ 零網路的作法是**把 `_tavily_search` 換成計數樁、其餘管線原封不動**，不要另外抄一份判斷式。news 題 0 次 web ＝ 拿財報硬答且零揭露（危險）／財報題打 web ＝ 白花錢 |
| [`eval/judge_regression.py`](eval/judge_regression.py) | correctness judge 自己的回歸套件＝11 個凍結案例。**先跑 `--dry-run`**（零 LLM 的接線檢查）。⚠ **這支不是零噪音，n=3 還不夠**（同一份碼三個單輪 10/11、9/11、11/11，兩輪 `--repeat 3` 之間也不一致）→ 一律看**逐題 k/n**、退出碼只認「全掛」。⚠ 第四態 **N/A（`known_limitation`）不能併進 PASS**。⚠ 判讀前先看 [`BACKLOG.md`](BACKLOG.md)：rubric 這條線**目前沒有活的消費端** |

**探針（零 LLM 判定，只讀 Qdrant ＋ reranker）**

| 檔案 | 量什麼 |
|---|---|
| [`eval/probe_temporal_interference.py`](eval/probe_temporal_interference.py) ＋ `period_probe_baseline.json` | **多年語料的期別干擾**＝「KB 要不要納入更多年度」的損害那一半。三個**互相獨立**的指標：F1 排擠／F2 錯選／`gold_rank`。⚠ **不可合併**：實測 F1 68 席 vs F2 4 題，只看聚合會把「多樣性問題」誤診成「期別選擇問題」。⚠ **順序不可顛倒**：要先在乾淨的單年 collection 上取 before，灌下去就回不去了。⚠ gold 對照**凍結快照**展開，不是當下 manifest（4 題萬用字元 gold 照當下展開會「撈到哪一年都算命中」） |
| [`eval/probe_historical_benefit.py`](eval/probe_historical_benefit.py) ＋ `period_probe_benefit_queries.json` | 同一個決定的**收益**那一半。⚠ **gold 不寫死檔名**，由 `literal` 在 collection 裡確定性定位 → 「gold 是哪幾份」與前提檢查「這個事實在單年 KB 到底在不在」**是同一個操作**。⚠ **收益區從 T-3 才開始**（10-K 損益表自帶三年、MD&A 自帶兩年，拿 T-1／T-2 造題會憑空灌水）。⚠ 單年臂的 `gold@k=0` 是**定義使然不是量測**。⚠ 這支量檢索不量生成 |
| [`eval/probe_multi_company_period.py`](eval/probe_multi_company_period.py) | 多公司題的期別損害。⚠ 指標必須拆 **kind A（同家舊季擠掉新季）vs kind B（那家公司連一個 10-Q 都沒進 top-k）**——只看聚合缺失率會把「席位競爭」誤診成「期別問題」 |
| [`eval/period_probe_trend_queries.json`](eval/period_probe_trend_queries.json) | **跨期 collapse 的陰性對照**：8 題本來就需要多個期別才答得出來的趨勢題。**eval_set 裡沒有任何一題是這個形狀** → 沒有它，「collapse 弄壞了什麼」這個方向的量尺完全是空的 |
| [`eval/ablate_retrieval_model.py`](eval/ablate_retrieval_model.py) | 檢索側 LLM 換模型的零噪音對照。兩 stage：`understand` 錄下輸出，`rank` 把那些輸出釘死送進真實檢索器（**零 LLM**）。噪音底線＝同模型跑兩次。⚠ 別用字串比對當結論——翻譯同模型跑兩次就不一樣（實測 68/100） |
