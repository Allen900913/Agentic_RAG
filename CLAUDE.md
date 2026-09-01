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
| **預設 LLM** | NVIDIA NIM `openai/gpt-oss-120b`（檢索側／判定側／生成側**三個都是**）。ingest 表格摘要走 Groq `openai/gpt-oss-20b` |
| **確定性閘門** | `verify_period_intent_routing` 85／`verify_answer_validators` 208／`verify_cross_period_collapse` 17／`verify_web_gate_isolation` 112 — **全 PASS** |
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
              rag_query.py                agentic_rag_v2.py
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
| [`rag_query.py`](rag_query.py) | **檢索與生成的核心**，其他所有入口都 `import rag_query as rq` 共用 `retrieve()`／`call_llm()`／`build_user_prompt()`。`COLLECTION_NAME` 在此定義，**可用 env `RAG_COLLECTION` 覆蓋**。四個「唯一定義點」在這裡，新的消費端一律用它們、不要再寫一份：<br>· `_payload_to_chunk` — payload→chunk dict 的**唯一建構點**（`period_basis` 曾經在 payload 裡卻沒被帶出來，害口徑 validator 結構性失效）<br>· `strip_evidence_tail`／`looks_like_refusal` — 「證據尾巴」與「這份答案算不算拒答」<br>· `_sole_ticker(filters, query)` — 「這是哪家公司」。**不要自己重跑 `_find_all_ticker_aliases`**：`parse_query_filters()` 的產出已含 regex ＋ LLM 兩個來源<br>· `_fiscal_sort_key`／`_get_period_ladder` — 期別的序<br>⚠ ticker **不是** `QUERY_FILTER_SYSTEM_PROMPT` 抽的（那支只抽 filing_type／fiscal_year／fiscal_period）——字面公司名（含中文別名）走 regex 表 `_COMPANY_TICKER`，**產品／子公司名走 `resolve_tickers_llm`，且只在 regex 沉默時才被叫** |
| [`api_server.py`](api_server.py) | FastAPI 後端，`/chat` SSE 串流。**產品線走的是這條（單管線）** |
| [`app.py`](app.py) | Streamlit 聊天前端，串 `api_server.py` |
| [`web_replay.py`](web_replay.py) | **Tavily 原始回應的錄／放**（live 路徑的 eval fixture）。未設 env `RAG_WEB_REPLAY` 時完全 no-op。⚠ 刻意錄在 `TavilyClient.search()` 的**原始回應**，不是 `_tavily_search()` 的回傳字串——後者已跑完白名單複核／去重／抽日期／過時過濾／截斷，錄那裡等於把要測的六道一起 mock 掉。⚠ `get()`／`put()` **兩邊都要 deepcopy**（下游會就地寫 `_pub_date`）。⚠ **`note_meta()` 只在 record 模式寫**：舊版無條件寫，於是每次 replay 都把 `_meta` 的綁定改成當下環境＝**證據自我抹除**，害「fixture 對不上 collection」三週沒有任何跡象。設計理由見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A8 |
| [`llm_replay.py`](llm_replay.py) | **A/B 用的重放快取**：把 plan／**replan**／英譯／Grader／ratio／期間意圖／ticker **七種**中間產物固定下來，讓兩臂差異只剩被測的那一項。未設 env `RAG_REPLAY_CACHE` 時完全 no-op。⚠ 新增接點要一起註冊進 `_KNOWN_KINDS`，漏了只有 `strict:<kind>` 會現形（最少人走的路） |

### Agentic

| 檔案 | 是什麼 |
|---|---|
| [`agentic_rag_v2.py`](agentic_rag_v2.py) | **現役 agentic 入口**。LangGraph：Plan → Execute（確定性檢索）→ Grade → Synthesize（生成 ＋ citation validator ＋ 一致性 validator ＋ 期別 validator ＋ 數字溯源 ＋ reflect）。模型分層 RETRIEVAL／CHECKER／GEN **三個都是 `gpt-oss-120b`**（env `AGENTIC_*_MODEL` 可覆蓋）。時間感知走 `freshness_mode`：`snapshot`（eval）／`live`（prod）。⚠ **Synthesize 的四道 validator 守門共用 `rq.looks_like_refusal`**，不要退回英文字面比對（中文拒答會穿過去、白燒兩次 LLM 稽核）。**節點設計理由與 validator 實測見 [`docs/AGENTIC.md`](docs/AGENTIC.md)** |

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
| `eval/period_probe_*.json`、`web_claims*.json`、`web_fixture*.json`、`replay_cache.json` | 各探針／閘門的 fixture 與凍結基準 |

### Eval — 腳本

**跑分主線（三步；reference 生成一次即可重用）**

| 檔案 | 是什麼 | 跑在哪 |
|---|---|---|
| [`eval/gen_reference_answers.py`](eval/gen_reference_answers.py) | ① 從黃金來源檔生成參考答案。⚠ `--force` 不帶 `--ids` 是全量重生成，**會洗掉 24 處人工校正** | `.venv` |
| [`eval/run_agentic_on_evalset.py`](eval/run_agentic_on_evalset.py) | ② agentic 版結果檔（含 `contexts` 供 RAGAS） | `.venv` |
| [`eval/eval_generation_llm_judge.py`](eval/eval_generation_llm_judge.py) | ② 單管線版：真實 retrieve+generate ＋ 3 維判定 | `.venv` |
| [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) | ③ 讀結果檔 → RAGAS 六指標。跑完會印噪音門檻與 gold 上限的判讀護欄。⚠ `--from-results` 吃多個檔是「拼接同一臂」**不是 A/B**（同 id 會被平均）；只改少數題的 reference 時用 `--ids` 只重評那幾題 | **`.venv-ragas`** |
| [`eval/record_web_fixture.py`](eval/record_web_fixture.py) | live web fixture 的唯一產生入口（`--mode record` 會連網燒額度；`--mode replay` 絕不連網）。⚠ **必須同時開 `RAG_REPLAY_CACHE`**：送給 Tavily 的 query 由 LLM 決定，只錄 web 會在重放時 miss | `.venv` |
| [`eval/migrate_fundamentals_pct.py`](eval/migrate_fundamentals_pct.py) | 一次性遷移（已執行）：Fundamentals 比率小數 → 百分比，**並同步修 gold** | `.venv` |

**確定性閘門（零 LLM、可重跑、秒級——改動前後都該跑）**

| 檔案 | 是什麼 |
|---|---|
| [`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py) | **query-understanding → Qdrant filter 的 Python 半邊**，八道閘門 85 項。①`_fiscal_sort_key` 真值表（含 NVDA 財年反轉）②ladder 排序 vs 期碼字串比大小 ③`ladder_pick`（挑不到必須回 None，**不可退而求其次**）④`period_ref` → filter（只有 `latest` 該產生 filter）⑤`range` → 跳過 collapse 的接線 ⑥Tier 1 的 label-year 命中資格 ⑦實體解析的接線 ⑧期間路由讀的是**已解出的** ticker。<br>⚠ 判別力集中在三處，改動時特別看：**⑥b**（⑥a 全餵合成 payload，ingest 哪天讓 10-K 的兩個欄位分歧，⑥a 照樣全綠而整個推理前提會崩）、**⑦b**（regex 抽得到時 LLM 必須一次都不叫）、**⑧g**（⑧a~⑧f 全是手餵 filters，呼叫端引數被重構掉它們照樣全綠）。<br>⚠ 閘門② **在單年 collection 上幾乎沒有判別力**，腳本會自己印出「判反幾對」並在 0 對時明說**那是測資不足不是系統健康** |
| [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py) | **Synthesize 端的確定性 validator**，十五道閘門 208 項（只讀 Qdrant coverage）。①`_fiscal_rank` 排序 ②期別陽性 ③期別陰性對照 ④`_news_freshness_gaps` ⑤缺口→警語接線 ⑥端到端回歸 ⑦`find_untraceable_numbers` ⑧R4 web↔財報衝突**要求並陳不裁決** ⑨`_repair_reference_citations` ⑩`_ensure_ratio_source_coverage` ⑪`_basis_disclosure_notice` ⑫ratio 意圖詞表退位 ⑬拒答不得附引用清單 ⑭Synthesize 四道 validator 守門 ⑮期間降級揭露的接線**與歸因**。<br>⚠ **這支的價值幾乎全在誤報對照，而不是陽性斷言**——多數陽性一個「一律不做」的實作也會通過。五個最重要的：**②**（陽性答案**逐字凍結在測試檔裡，不讀 `experiments/`**，否則一修好陽性就消失）、**⑩b/⑪b**（拿真實 payload 餵**生產建構子**，⑪ 原本 8 條全餵測試自造的 dict 於是全綠、而生產根本沒把那個欄位放進 chunk dict）、**⑬b/⑭d**（拒答判過頭＝把有依據答案的 provenance 砍掉／validator 全部跳過）、**⑭a**（逐個 validator 走 AST 驗，改三道漏一道要叫得出來）、**⑮e**（⑮b 只驗「呼叫點寫了 `period_note=`」，那照樣可能傳一個永遠是空的變數——⑮e 驗**值**真的從 state 流到 Generator）、**⑮f**（⑮a~⑮e 全部只問「有沒有接上」，只有 ⑮f 問「這句話**該不該說**」——揭露句寫的是「**所詢問**財年」，而那個年份可能是 Grader 補救改寫憑空加的，見 [`BACKLOG.md`](BACKLOG.md)〈已知缺陷〉的 col-10）、**⑮g**（⑮f 只擋得住「Grader 在同一個子問題內改寫」；**replanner 另外加一個 todo** 時，那個 todo 的 rnd 0 照樣算「第一輪」→ 假前提從另一扇門回來而 ⑮f 全綠。所以歸因掛在 **todo 的出身**上：`_node_plan` 建的標 True、`_node_replan` 建的標 False） |
| [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) | **web_search 的 eval 隔離與資料流**（零網路、零 Qdrant），**九道閘門 112 項**。①eval 隔離真值表 ②`web_extra` 跟著每次重生成 ③**無 web 時 system message 逐字不變**（＝eval 基準不被動到的證明）④白名單有生效、濾空不退回全網、**清單必須公司無關** ⑤Grader 時效判準的日期算術（含 `kb_unfixable` 誤殺真值表與時效證據資格）⑥web 結果整理（摘要不得截掉深處數字、地區子網域要擋、單域名上限、去重、預算）⑦replay 的 miss 不准被任何 `except Exception` 吞掉 ⑧`replan` 必須進重放快取、且 key **不含自由文字結果** ⑨fixture 的環境綁定不准被 replay 改寫、且開跑前要被比對。⑧ 另含「intraday 且已搜過 web 時，追加待辦一律拒絕」的判準與接線。<br>⚠ 閘門① 的 `_gate()` 是生產判斷式的**抄寫不是 import**，改那一行務必同步改這裡。<br>⚠ 閘門⑦ 守的是一條**結構性**保證（`FixtureMiss`／`RecordError`／`ReplayCacheMiss` 不繼承 `Exception`），不是契約。舊版靠「呼叫端記得 re-raise」，而全碼庫有 16 個 `except Exception`——漏一個就把「fixture 沒涵蓋」變成「系統沒打 web」，兩者外觀完全相同。<br>⚠ 這支在 **import 時**就把 `ar._tavily_search` 換成 stub（絕不連網的保證），所以要測真身一律用 `_REAL_TAVILY_SEARCH`——閘門④ 與 ⑦e 各自踩過一次。<br>⚠ 閘門⑧ 的判別力**全在誤報對照**（⑧d~⑧g）：只驗「快取會命中」的話，一個「key 是常數」的實作也會滿分，而那會讓**所有** replan 決策互相蓋掉。每個「應該要 miss」的維度（task／freshness_mode／query／status）都各有一條。<br>⚠ 閘門⑨ 同理要有 ⑨b：只驗「replay 不寫」的話，一個 `note_meta` 直接 return 的實作也會通過——那樣連 record 都不寫，fixture 從此沒有綁定可查。<br>⚠ **⑧p 是一條被踩出來的斷言**：守衛第二版用 `_is_web_todo(task)` 當前置條件，而 `_WEB_TODO_RE`（`網路|上網|web search|internet`）**匹配不到「在 Yahoo Finance 上查詢」「使用 NASDAQ 官方網站」**——六個真實措辭逐字凍結在那裡 |
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
| [`eval/check_rounding_fidelity.py`](eval/check_rounding_fidelity.py) | 量「答案把來源數字捨成約值」＝`SYSTEM_PROMPT` Rule 8 NUMERIC FIDELITY 唯一能被證偽的方式。⚠ **歸屬必須逐引用**：整篇 contexts 比對會把「約 18%」放行（同題 10-K 確實寫著 `increased 18%`）——**有來源，但不是那一句掛的那個來源**。⚠ **統計效力很低**（見 BACKLOG）。先跑 `--selftest` |
| [`eval/check_comparison_claims.py`](eval/check_comparison_claims.py) ＋ `comparison_claims.json` | **「哪一家最高」這個結論對不對**（`multi_hop` 5 題唯一的自動化證據）。全碼庫**沒有一行 Python 在做這個比較**——它整個交給 Generator 讀 chunk 自己比大小，而比錯了 Synthesize 的五道 validator **一道都不會響**（citation 合法、數字溯源得到、期別也對）。先跑 `--selftest`。<br>⚠ 真值**從答案自己的 `contexts` 算**，不是重跑檢索也不是查 gold 檔——否則 FAIL 會混進「檢索沒撈到」這個完全不同的病。這樣 FAIL 的意思才毫不含糊：**數字就攤在它面前還是挑錯了**。<br>⚠ 三態不可合併：`wrong_winner`（危險）／`unfounded`（某家的值不在 contexts 卻仍宣告贏家，**獨立缺陷**）／`N/A`。<br>⚠ **判別力上限**：現有語料**每一題差距都很大**，沒有接近值、沒有缺值、沒有跨單位（B vs M）。全 PASS 只證明容易的情況不會錯，**不能外推**。<br>⚠ 「答案宣稱哪一家」是**感知不是規則**，第一版在 40 筆裡誤報 2 筆（題目重述清單被當成宣稱、「A 領先於 B」的錨點後面是輸家）。兩個真實句型已逐字凍結成 selftest ⑧⑨，並各配一個誤報對照 |
| [`eval/check_web_claims.py`](eval/check_web_claims.py) ＋ `web_claims.json` | **live web 路徑的逐條確定性驗收**＝這條路唯一的自動化證據。只測**含 LLM 的端到端**：有沒有引用 web、主機是否在白名單且非地區子網域、**答案裡每個報價是否都在 fixture 原始內容裡逐字出現**、KB 舊值與時點是否與 web 新值並陳。⚠ 斷言只能測「不論 LLM 挑 fixture 裡哪個來源都成立」的性質 |
| [`eval/web_claims_news37.json`](eval/web_claims_news37.json) ＋ `web_fixture_news37.json` | **冷凍 37 題在 live web 上的性質斷言**（40 條，含 3 題陰性對照），as-of 2026-08-20。⚠ **刻意不對照新聞 gold**：那批 gold 是「2026 年 6 月的新聞說了什麼」，live web 回答「現在的網路說什麼」——**是兩個不同的問題**。⚠ **不能用 `numbers_must_be_in_fixture`**：題目天生混合，答案裡 KB 與 web 數字並存，沒有字面樣式分得開 |

**探針（含 LLM、非零噪音、不是閘門——它們量的是「這個 LLM 判斷準不準」）**

> 共同判讀規則：**看不對稱的錯誤**，不是整體準確率；**陰性對照不可省**（少了它「一律回 X」也會滿分）；MoE 不固定路由，**下結論前 `--repeat 3` 以上**。

| 檔案 | 量什麼 / 不對稱在哪 |
|---|---|
| [`eval/probe_ticker_resolution.py`](eval/probe_ticker_resolution.py) | 產品／子公司名 → 母公司 ticker。⚠ **只有這一支在量它**：eval_set 65 題全部由 regex 解出 ticker，這個修法在既有跑分上量不到差異。**解不出來** ＝ 退回修法前（只是沒改善）；**解成別家** ＝ hard filter 鎖到別人的財報＝**比修法前更糟**。陽性臂拆兩層（知名產品／10-K 分部名），難度在後者 |
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
