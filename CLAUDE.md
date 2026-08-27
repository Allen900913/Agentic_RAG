# US Stock RAG — 架構、全域規則、檔案地圖

> **這份檔案只放三種東西**：① 系統架構（資料怎麼流）② **全域規則**（不知道就會做錯事，且與日期無關）③ 檔案地圖（哪個檔負責什麼）。
>
> **不要往這裡放**：某次改動的診斷過程與實測數字、單一模組的旗標細節、待辦、日期記錄。那些各有歸屬，見下方〈文件地圖〉。判準是一句話：**「這條規則明天、下個月、換一個人接手都還適用嗎？」** 不適用的就不屬於這裡。
>
> **維護規則**：改動任何檔案前先看這份地圖有沒有因此過時（新增/刪除/更名檔案、改變用途或角色（生產⇄棄用）、換 LLM/collection、改變資料流或路徑）。**有任何一格對不上實況，就在同一次改動裡改到一致**，別讓程式碼與文件漂移。

---

## 文件地圖（要寫東西之前先看這裡：這件事該寫哪一份）

| 你手上的東西 | 該寫進 | 判準 |
|---|---|---|
| 架構、全域規則、檔案職責 | **本檔** | 與日期無關、全域適用 |
| 「今天改了什麼」 | [`CHANGELOG.md`](CHANGELOG.md)（主管線／ingest／eval）<br>[`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md)（agentic） | 有日期、已完成 |
| 語料處理知識：切塊各層、重建注意事項、當初的診斷 | [`docs/INGEST.md`](docs/INGEST.md) | 「為什麼切塊長這樣」 |
| 量測知識：噪音底線、MDE、量尺失效史、**已試無效總表** | [`docs/EVAL.md`](docs/EVAL.md) | 「這個數字能不能相信」 |
| agentic 知識：節點設計、validator、檢索層決定、架構診斷 | [`docs/AGENTIC.md`](docs/AGENTIC.md) | 「為什麼管線長這樣」 |
| 未做、待決、**查清楚後決定不修的極限** | [`BACKLOG.md`](BACKLOG.md) | 還沒做完 |
| 對外說明（安裝、執行、設計理由） | [`README.md`](README.md) | 給不熟這個 repo 的人看 |
| 單一模組的旗標語法、參數細節 | **該模組自己的 docstring** | 只有讀那個檔的人需要 |

**三條搬移紀律**：
1. **一件事做完 → 從 `BACKLOG.md` 刪掉、寫進 `CHANGELOG*.md`**；查清楚決定不做 → 留在 `BACKLOG.md` 的「已接受的極限」並寫下復活條件。
2. **null result 一定要寫**（`docs/EVAL.md` 的已試無效總表）。沒寫下來的無效實驗會被重跑——2026-08-11 就重跑了一次 2026-08-05 做過的。
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

- **檢索**：BGE-M3 dense+sparse hybrid → RRF → cross-encoder rerank。
- **collection 現況（2026-08-27 升生產：`mdna` → `multiyear`）**：

  | collection | 角色 |
  |---|---|
  | `us_stock_rag_edgar_mdna` | **前生產（2026-08-13 ~ 2026-08-27）**。單年：21 份 filing／3,827 chunks。**2026-08-19 起無新聞**（3925 → 3827 chunks）。小標層收窄到 `_MDNA_ITEMS` 白名單；三道確定性閘門全綠、`check_number_defects` **7 條主張，同一份 collection 上兩輪跑出 PASS 7／FAIL 0 與 PASS 4／FAIL 3**（2026-08-19；差異全來自 Plan 子問題變異 → ⚠ **這個指標的判讀一律要 ≥2 輪，單輪會給出相反結論**，實測就吃過一次虧，見 CHANGELOG 同日）。⚠ 這一格當天走過 6 條 PASS 4／FAIL 2 → 拆題庫後 4 條 PASS 3／FAIL 1 → 補回 `lex-16`／`lex-17` 後 7 條；**「FAIL 變少」有一次是分母變動不是改善**。拔除新聞唯一零噪音量到的收益是 `mix-07`（2/2 拒答 → 答出 $29.5B）；`mi-05`／`lex-17` 的檢索缺陷**沒有**被修好、檢索類 RAGAS 已達 gold 上限 |
  | `us_stock_rag_edgar_period` | 前生產（2026-08-13 以前的碼上預設）。無小標層，**mix-03 會錯**（把分部的 24%／27 億當成公司整體，答案拼自三個 chunk） |
  | `us_stock_rag_edgar_ground2` | 收窄前的最佳臂。四個 ingest 修法全生效，但 Item 1／1A 被小標層切碎 → semantic recall 0.497 |
  | `us_stock_rag_edgar_multiyear` | **生產（2026-08-27 起，`COLLECTION_NAME` 預設）**。77 份 filing／13,022 chunks，每家 3×10-K ＋ 8×10-Q 橫跨 ~2.5 年、無新聞。⚠ **升生產是與 `RQ_PERIOD_INTENT_LLM` 預設翻開同一次做的**（多年語料上線而 A 沒開，當期題會退步——收益探針 control 臂 gold@5 4/4 → 3/4）。決策的兩半證據：**損害**見 [`docs/EVAL.md`](docs/EVAL.md)〈多年語料的期別干擾〉〈階段 4〉（45 題掉 2 題、錯期率 0.000 → 0.044，兩題都在「問題沒提期間」那類）；**收益**見〈多年語料買到了什麼〉（歷史題 gold@5 0/20 → 18/20，兌現率 0.900，當期陰性對照 4/4 不動）。⚠ `verify_chunk_grounding` 有 1 筆 FAIL（`MSFT_10K_2024.html#158`，1/1009＝0.1%）**帶著上線**——它的復活條件是「下次有正當理由重跑 ingest 時」，而升生產不需要重跑，見 [`BACKLOG.md`](BACKLOG.md)。其餘 ingest 閘門（table caption／segment split）全綠 |
  | `us_stock_rag_edgar_exp4` | **歷史基準，不要當對照臂**。Fundamentals 比率還是小數（`0.183` 而非 `18.30%`），且從未進入 period／head／ground 這條線的任何一次對照 |
  | `head` / `ground` | 切塊層的中間世代，只留作對照 |

  ⚠ **這一格漂移過兩次**，改碼時請一併更新：①本檔原本寫「生產 ＝ exp4」而碼上是 `period`；②本檔原本寫 `period` 的 mix-03／mix-09 都會錯，**實測只有 mix-03 錯、mix-09 是 PASS**。**跑任何實驗前先 `grep COLLECTION_NAME rag_query.py` 確認**，並用 env `RAG_COLLECTION` 覆蓋而不是改碼。
- **⚠ 切塊／ingest 這條路已經到頂，不要再改**：六個 RAGAS 指標有五個已達或超過 gold 上限（把標準答案原文當系統答案餵進去評的分數，常數見 [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) `GOLD_BASELINE`）。檢索完美與檢索全失敗的題只差 0.141 correctness → 全修好也只有 +0.024，低於噪音底線。詳見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺飽和〉。
- **預設 LLM**：NVIDIA NIM `openai/gpt-oss-120b`；模型名 `gemini-*` 開頭走 Gemini（`rq.call_llm` 依名稱路由）。ingest 的表格摘要走 Groq `openai/gpt-oss-20b`（env `TABLE_SUMMARY_MODEL` 可覆蓋）。
  - ⚠ **表格摘要模型換過兩次，而且兩次都是被迫的**（gemini-2.5-flash → llama-3.3-70b-versatile → 2026-08-16 後者被 Groq 退役、Llama 全系列下架）。**換這個模型必須先跑 bake-off**：它是推理模型，`TABLE_SUMMARY_MAX_TOKENS` 太小會讓 reasoning token 吃光正文額度、**靜默吐空字串**（實測 gpt-oss-120b @200 是 10/10 全空）。判準見 [`unstructured_components.py`](unstructured_components.py) `TABLE_SUMMARY_MODEL` 上方的證據表，選型看的是**輸出穩定性**不是模型大小。失敗的唯一出口是 [`eval/verify_table_captions.py`](eval/verify_table_captions.py) 的 `missing_on_big`。
- **切塊層級**：Item → 期間章節 → 通用小標 → 幅度接地 → SemanticChunker → RCTS 上界 → min-size 下界。前四層是**規則**（決定「哪裡不准切」），SemanticChunker 決定「裡面哪裡切」——**前者取代不了後者**。各層細節與診斷見 [`docs/INGEST.md`](docs/INGEST.md)。

---

## 全域規則

**語言**
- **回覆一律繁體中文**（使用者 2026-07-11「以後也是」）。程式碼 docstring/註解用繁中，identifier 與 log 用英文。

**兩個 Python 環境，絕對不可混用**
- 生產 `.venv`（[`requirements.txt`](requirements.txt)）：langchain 1.x + langgraph。查詢、agentic、ingest 都在這裡。
- 評測 `.venv-ragas`（[`requirements-ragas.txt`](requirements-ragas.txt)）：langchain 0.3.x + ragas 0.2.15。**只給 RAGAS 用**。
- ragas 0.2.x 綁 `langchain-core<0.4`，裝進生產環境會把 langchain 降版、弄壞 agentic 管線與 SemanticChunker。

**資料範圍：KB 只收「記錄」，不收「流」**（2026-08-19）
- **KB ＝ 10-K／10-Q ＋ Fundamentals／IncomeStatement。沒有新聞。** `data/raw/News/` 的檔案留在磁碟上，但 [`data_update_edgar.py`](data_update_edgar.py) 的 `RAW_EXCLUDE_DIRS` 於掃描階段排除它——**那一行是整條規則的單一開關**。
- 判準是「這份東西的正確性靠什麼判定」：記錄靠**期別對＋有引用＋數字可驗**，流靠**夠新＋來源可信**。本專案為期別正確性做的每一層（切塊六層、期別階梯、跨期 collapse、期間意圖、citation validator）**對新聞沒有一項成立**——新聞 chunk 連 `report_period_code` 都是 0% 覆蓋。
- 「市場現在怎麼看」由 **agentic 的 live web 路徑**供應（有發布日、來源白名單、過時過濾）。⚠ 別把它接回 ingest：web fallback 也**不負責抓最新 10-Q**，那是 ingest 的事。
- ⚠ **不要為了讓 news 類題目有分數就把新聞加回來**。那 37 題已冷凍在 [`eval/eval_set_news.json`](eval/eval_set_news.json)，要驗的是「live web 能不能取代它」，不是「把舊語料放回去」。完整理由與資料實況見 [`docs/EVAL.md`](docs/EVAL.md)〈KB 拔除新聞〉。

**抓取與處理分離**
- **只有 [`fetch_data.py`](fetch_data.py) 會連網**。[`data_update_edgar.py`](data_update_edgar.py) 一律離線，照 `data/raw/sec_manifest.json` 取件。
- 不要為了「順便更新」去重抓 SEC：換版會讓切塊實驗不可重現（改了規則跑出的差異分不清是規則還是換版）。

**重建 collection**
```bash
.venv/Scripts/python.exe data_update_edgar.py --rebuild --rcts-fallback --collection <名稱>
```
- **`--rcts-fallback` 不可省，但預設是關的**。漏掉會少 12% chunks，且 7.4% 的 text chunk 超過 rerank 截斷長度＝內容等於看不到。
- **重建不會逐字重現舊 collection**，有兩個正當差異來源。判斷重建正常與否要比 **text chunk**（確定性），不要比 table。細節見 [`docs/INGEST.md`](docs/INGEST.md)。

**量測（跑任何 A/B 之前）**
- **這個專案的量測解析度比多數改動的效果粗一個數量級**：RAGAS 的 MDE（95%, n=100）換算成「要幾題從 0 修到 0.5」是 **4~7 題**，而典型 ingest 改動只動 2~5 題。
- 所以：**先問「這個差值超過噪音嗎」再問「為什麼」**；A/B 兩臂要共用 `RAG_REPLAY_CACHE` fixture；**提實驗前先 `ls experiments/` 與 [`docs/EVAL.md`](docs/EVAL.md) 的已試無效總表**。
- 逐條斷言（[`eval/check_number_defects.py`](eval/check_number_defects.py)）是零噪音的，聚合指標不是。「數字答對了沒有」用前者驗收。

**LLM 與 Python 的分工**
- 判準是「**這個子任務有不有唯一正確答案**」：抽取／裁決給 LLM，比對／定位／算術給 Python。
- **硬編碼詞表是警訊**——那通常代表在用字串比對做感知，換個措辭就漏（見 memory `llm-vs-python-task-split`）。例外是**格式定義的封閉集合**（`VALID_*_ITEMS`、`_TABLE_DOMINATED_ITEMS`），列清單正當。
- **實例（2026-08-25）**：`_RATIO_INTENT_RE` 這條詞表讓整條 ratio 保底在口語問法下靜默繞過，已退位成 LLM 失手時的 fallback（`_classify_ratio_fields`）。⚠ 改成 LLM 之後**判別力來自「LLM 說不是就必須不是」**——若空答案會掉回詞表，詞表仍然是實際做決定的人，而端到端跑分看不出任何差別。

**期別誰新誰舊，只有一套序**
- **一律用 payload 的 `(fiscal_year, fiscal_period)`**（`rag_query._fiscal_sort_key`／`_get_period_ladder`，agentic 端是 `_fiscal_rank`）。**不要拿 `report_period_code` 比字串**：10-K 是 4 位年份、10-Q 是 6 位 yyyymm，混比會**時對時錯**——`'2026' > '202510'` 為真（對），但 `'2025' < '202506'` 也為真（錯，年報當然比自己的 Q3 新）。**77 份 filing 實測：385 個配對有 31 對（8.1%）判反。**
- **財年不等於曆年**：`NVDA_10K_2026` 是 FY2026 FY，而 `NVDA_10Q_202604` 是 **FY2027 Q1**——後者才新。用日期或檔名年份排序都會判反。
- 改期別排序前先跑 [`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py)（閘門①②③⑥）與 [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py)（閘門①）。

**改動前的自我檢查**
- 機制宣稱要先有**能證偽它的確定性測試**（零 LLM、可重跑）。2026-08-09~11 共四個「聽起來合理」的機制假設被自己的測試推翻。
- 稽核腳本回傳「0 筆問題」先當**壞消息**查（實測兩次都是欄位名寫錯或量尺沒有判別力）。
- **反過來也一樣：稽核回報 FAIL，先問「是不是量尺錯」再問「系統怎麼壞的」。** 2026-08-15 把一個 `numbers_must_be_in_fixture` 的誤報（來源寫 `196.8165`、答案正確四捨五入成 `196.82`，而斷言只做逐字比對）當成真陽性，寫了一條 BACKLOG、一段 CHANGELOG、一節 docs 才發現。兩個方向的查證成本一樣是一次 `grep`。

---

## 檔案地圖

> 一次性診斷腳本（`eval/_*.py`、`*_probe*`、`* copy.py`）不列於此表；那些是拋棄式實驗，別當生產碼。
> 各檔的旗標語法與參數細節看該檔 docstring，這裡只寫「它是什麼、什麼時候該看它」。

### 生產／查詢
| 檔案 | 是什麼 |
|---|---|
| [`rag_query.py`](rag_query.py) | **檢索與生成的核心**，其他所有入口都 `import rag_query as rq` 共用 `retrieve()`／`call_llm()`／`build_user_prompt()`。`COLLECTION_NAME` 在此定義，**可用 env `RAG_COLLECTION` 覆蓋**（跑 collection A/B 用 env，不要改碼）。⚠ payload→chunk dict 的**唯一建構點是 `_payload_to_chunk`**：下游 validator 讀得到什麼欄位由它決定，加欄位一律加在那裡（`period_basis` 曾經在 payload 裡卻沒被帶出來，害口徑 validator 結構性失效）。⚠ **「答案的證據尾巴」與「這份答案算不算拒答」的唯一定義也在這裡**（`strip_evidence_tail`／`looks_like_refusal`，後者 2026-08-27 從 eval 端搬進來——生產也要用它決定拒答不附引用清單，而生產不可以 import `eval/`）。新的消費端一律用它，不要再寫一份 |
| [`api_server.py`](api_server.py) | FastAPI 後端，`/chat` SSE 串流 |
| [`app.py`](app.py) | Streamlit 聊天前端，串 `api_server.py` |
| [`web_replay.py`](web_replay.py) | **Tavily 原始回應的錄／放**（live 路徑的 eval fixture）。未設 env `RAG_WEB_REPLAY` 時完全 no-op。⚠ 刻意錄在 `TavilyClient.search()` 的**原始回應**，不是 `_tavily_search()` 的回傳字串——後者已跑完白名單複核／去重／抽日期／過時過濾／截斷，錄那裡等於把要測的六道一起 mock 掉。⚠ `get()`／`put()` **兩邊都要 deepcopy**（下游會就地寫 `_pub_date`）。設計理由與四個坑見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A8 |
| [`llm_replay.py`](llm_replay.py) | **A/B 用的重放快取**：把 plan／英譯／Grader 決策固定下來，讓兩臂差異只剩被測的那一項。未設 env `RAG_REPLAY_CACHE` 時完全 no-op，生產路徑不受影響 |

### Agentic
| 檔案 | 是什麼 |
|---|---|
| [`agentic_rag_v2.py`](agentic_rag_v2.py) | **現役 agentic 入口**。LangGraph 管線 Plan → Execute（確定性檢索）→ Grade → Synthesize（生成 + citation validator + 一致性 validator + reflect）。模型分層 RETRIEVAL／CHECKER／GEN **三個都是 `gpt-oss-120b`**（env `AGENTIC_*_MODEL` 可覆蓋）。⚠ 這一格漂移過：RETRIEVAL 早在 2026-08-14 就從 `20b` 換成 `120b`（實測 20b 在 NIM 上**慢一倍**、品質等價，見該檔 `RETRIEVAL_MODEL` 上方），本檔卻一直寫著 20b。要重現 2026-08-14 以前的跑分請設 `AGENTIC_RETRIEVAL_MODEL=openai/gpt-oss-20b`。時間感知走 `freshness_mode`：`snapshot`（eval）／`live`（prod）。**節點設計理由與 validator 實測見 [`docs/AGENTIC.md`](docs/AGENTIC.md)** |

**web_search（live 專用）的四條規則**：
- **eval 隔離只靠 `freshness_mode == LIVE` 與 `ENABLE_WEB_SEARCH` 兩個獨立條件**，各自都足夠。任何「相關性詞表」（`looks_like_news_query`、`_RELATIVE_TIME_RE`）**都不是隔離機制**——它們守的是相關性卻讓非新聞措辭的即時題永遠打不到 web，且**漏網的代價是把三週前的數字講成「今天股價」**。要改 web 判斷式，先跑 [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py)。
  - ⚠ **這一格漂移過**：2026-08-13 只從 **web 觸發**拿掉那兩道，時效警語（`_build_todo_temporal_scope`）那道漏改，本檔卻已寫成「全數移除」，兩天後才發現。2026-08-15 補完：`_RELATIVE_TIME_RE` **已從碼上刪除**，時效缺口改由 `_news_freshness_gaps` 看「這次實際用了誰的新聞」算。`rq.looks_like_news_query` 2026-08-19 起**也退出檢索路由**（KB 已無新聞），只剩診斷探針拿它當分類標籤——碼上刻意留著但不接任何生產判斷。
- **只有「真實日曆日期」能證明候選池夠新**（`_is_freshness_evidence`，2026-08-19）。10-K 的 4 碼財年戳、10-Q 的 6 碼期別戳是**財報期間不是發布日**（`MSFT_10K_2026` 會被算成 2026-12-31＝未來）——它們**不算過期、但也不算證據**。混淆這兩件事會讓財報替整個池子背書說「夠新」，時效改判就不觸發（實測 6 題中 4 題，含 intraday 股價題）→ `kb_unfixable` 恆為 False → 每個子問題白燒 `MAX_REWRITES` 輪改寫，**且第二道防線被靜默關閉**。⚠ 這**不等於** web 不會被叫——主防線是 Grader 的 LLM 判斷，實測那 4 題舊碼下 web 一樣會觸發。`_stale_for_realtime` 與 `_kb_ceiling_date` **必須共用同一套資格判準**。
- **web 內容要能被引用才用得到**。`rq.SYSTEM_PROMPT` Rule 1/2/8（尤其「Every number you state must be traceable to a cited chunk」）會讓模型寧可捏造推導值也不碰 web 數字。放行條款**必須加在 system message**，user message 版本已實測無效。
- **白名單只是授權，不是過濾**。Tavily 的 `include_domains` 是**子網域包含式**比對，一筆 `finance.yahoo.com` 會連 `ca.`／`hk.` 一起收，而那些是**別的市場的報價**（同一天差 12%）。所以收到結果後還要用 `_host_allowed` 自己複核一次（只認 exact ＋ `www.`）。這個坑踩過兩次（`apple.com`→`apps.apple.com`、`finance.yahoo.com`→`ca.finance.yahoo.com`）。**清單本身必須公司無關**（統一入口是 `sec.gov`），加一筆 IR 主機名就是 O(n) 的開始。
- **web 這條路的診斷成本主要在觀測性**。實測兩次根因都是「trace 印得不夠」才查不出來：只印字數看不出摘要被自己截掉（`content[:300]` 切掉了數字）、只印網域看不出日期為何抽不到。**先把要判斷的東西印出來，再猜成因**——這條路每猜錯一次就要重跑一次 LLM＋網路。

### Ingest
| 檔案 | 是什麼 |
|---|---|
| [`fetch_data.py`](fetch_data.py) | **唯一對外抓取入口**：edgartools SEC filing ＋ yfinance 基本面 → `data/raw/` ＋ `sec_manifest.json`。⚠ 新聞改成 `--with-news` **opt-in（預設不抓）**，且抓了也不會進 KB |
| [`data_update_edgar.py`](data_update_edgar.py) | **唯一 ingest 執行入口，零網路**：讀本機 `.nc` → 切塊六層 → 寫入 collection。三種 doc_type（10-K/10-Q/Fundamentals＋IncomeStatement）都在這裡；**News 已由 `RAW_EXCLUDE_DIRS` 於掃描階段排除**（那一行是整條「拔除新聞」的單一開關）。**切塊各層、增量策略、踩過的坑見 [`docs/INGEST.md`](docs/INGEST.md)** |
| [`unstructured_components.py`](unstructured_components.py) | **函式庫，非執行入口**。unstructured 解析與切塊組件、表格處理（caption／Groq 摘要）、`BGEM3DenseEmbeddings`。只被 `data_update_edgar.py` import |
| [`data_update.py`](data_update.py) | 最舊的 ingest 管線，已被 EDGAR 版取代，保留對照 |

### Eval
> 標準跑法（三步，reference 只需生成一次可重用）：① `gen_reference_answers.py` → ② 產生結果檔（agentic 走 `run_agentic_on_evalset.py`／單管線走 `eval_generation_llm_judge.py`）→ ③ 切 `.venv-ragas` 跑 `eval_ragas_vs_rubric.py`。
> **`eval/` 只放輸入與標準答案，跑分輸出一律寫到 `experiments/`**（`--output` 的預設值仍指向 `eval/`，要自己帶）。

| 檔案 | 是什麼 | 跑在哪 |
|---|---|---|
| [`eval/eval_set.json`](eval/eval_set.json) | **65 題**題庫（semantic 15／mixed 15／**lexical 17**／colloquial 13／multi_hop 5）。2026-08-19 從 100 題拆出 63 題（37 題 gold 含新聞者移入下一列），同日再把 `mi-04`／`mi-05` **去掉新聞子句**補回成 `lex-16`／`lex-17`（動機是量尺覆蓋不是題數）。**跨該日的分數不可直接比**（分母變了兩次） | — |
| [`eval/eval_set_news.json`](eval/eval_set_news.json) ＋ `reference_answers_news.json` ＋ `number_claims_news.json` | **冷凍的 37 題**（news 15／multi_intent 15／colloquial 2／multi_hop 5）。⚠ **現在不要拿它跑分**：eval 預設 web 是關的，跑了必然全滅、那不是證據。它的用途是日後驗收「live web 能不能取代 KB 新聞」，屆時要先把 `web_fixture.json` 錄到覆蓋這 37 題 | — |
| [`eval/reference_answers.json`](eval/reference_answers.json) | RAGAS 的 ground truth，**已納入版控**（跟 eval_set 同級是「標準答案」，且含 24 處人工校正、非腳本可免費重現） | — |
| [`eval/gen_reference_answers.py`](eval/gen_reference_answers.py) | 從黃金來源檔生成參考答案。⚠ `--force` 不帶 `--ids` 是全量重生成，**會洗掉人工校正** | 生產 `.venv` |
| [`eval/run_agentic_on_evalset.py`](eval/run_agentic_on_evalset.py) | 把 agentic 跑在 eval_set → 結果檔（含 `contexts` 供 RAGAS） | 生產 `.venv` |
| [`eval/eval_generation_llm_judge.py`](eval/eval_generation_llm_judge.py) | 單管線版：真實 retrieve+generate ＋ 3 維判定 | 生產 `.venv` |
| [`eval/judge_regression.py`](eval/judge_regression.py) | **correctness judge 自己的回歸套件（含 LLM，非零噪音，不是閘門）**＝11 個凍結案例（真實事故 6 ＋ 人工反例 5），每次改 `_CORRECTNESS_FEEDBACK_SYSTEM` 或換 judge model 前後跑。⚠ **先跑 `--dry-run`**：那是零 LLM 的接線檢查（AST 讀出實際送給 judge 的 kwargs 比對簽章、每題 `expect` 的 checkpoint id 不得越界、`compute_correctness` 三態）。它存在的理由就是本檔自己踩的坑——judge 簽章 2026-07-23 少了兩個參數，本檔沒跟上、**一跑就 TypeError 而壞了一個月沒人發現**，因為它「要燒 LLM」平常沒人跑。⚠ **這支不是零噪音，單輪總分不可比，n=3 也還不夠**：judge 是 MoE，同一份碼三個單輪拿到 10/11、9/11、11/11，而兩輪 `--repeat 3` 之間**也不一致**（`col01_true_fabrication_negative` 一輪 1/3、一輪 3/3；九次觀測合起來 7/9＝雜訊）→ 一律用 `--repeat` 看**逐題 k/n**、下結論前把多輪的 k/n 加起來，**退出碼只認「全掛」**（k=0；把時好時壞也變紅燈，這支就會再次被當成壞掉而沒人跑）。⚠ 第四態 **N/A（`known_limitation`）不能併進 PASS**——那是「題目的前提沒了」不是 judge 判錯；它是唯一能讓失敗不算失敗的旗標，所以 `--dry-run` 每次都印出有幾題帶著它。⚠ 判讀前先看 [`BACKLOG.md`](BACKLOG.md)：rubric 這條線**目前沒有活的消費端**（`eval_set.json` 65 題全無 rubric），且 FABRICATION SCOPE 那類 mn checkpoint **judge 拿不到來源、原則上只能靠合理性判** | 生產 `.venv` |
| [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) | 讀結果檔 → RAGAS 六指標。跑完會印噪音門檻與 gold 上限的判讀護欄 | **`.venv-ragas`** |
| [`eval/check_number_defects.py`](eval/check_number_defects.py) ＋ [`eval/number_claims.json`](eval/number_claims.json) | **確定性數字缺陷檢查（零 LLM、零噪音）＝「數字答錯」類改動的主要驗收指標**。逐條斷言判 PASS／FAIL／N/A 三態。**跑之前先 `--selftest`**（`anchored_pcts` 的切窗四條雙向鎖）。⚠ N/A 不能併進 PASS——**它是「這一輪沒量到東西」不是通過**，第五、第六次失效都是這樣抓到的（`col-11` 答對了卻因為 anchor 措辭寫死而判 N/A）。⚠ **一個 id 可以掛多條主張**（`lex-17` 掛 `require_chunk` ＋ `anchored_pct`：進池 ≠ 有用它）。量尺的五次失效史見 [`docs/EVAL.md`](docs/EVAL.md) | 生產 `.venv`（只讀結果檔） |
| [`eval/audit_gold_numbers.py`](eval/audit_gold_numbers.py) | **gold 自洽性稽核（零 LLM、零網路）＝跑任何評測前的前置閘門**。⚠ 不查 filing 類的數字（大乾草堆裡「值有沒有出現」不帶資訊） | 生產 `.venv` |
| [`eval/verify_segment_split.py`](eval/verify_segment_split.py) | 驗收 ingest 的三層硬邊界（期間／小標／幅度接地）。零網路、零 embedding、不碰 Qdrant → **可在別的實驗跑的時候執行** | 生產 `.venv` |
| [`eval/verify_table_captions.py`](eval/verify_table_captions.py) | 驗收表格 caption 品質（零 LLM、只讀 Qdrant）。**重建後必跑**：`missing_on_big` 是 Groq 429 靜默降級的唯一出口，`numeric/stub` 是 caption 選錯來源。⚠ `no_caption` 本身不是缺陷（小表不值得花 LLM call） | 生產 `.venv` |
| [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) | **驗收 web_search 的 eval 隔離與資料流（零 LLM、零網路、零 Qdrant，秒級）**。六道閘門 79 項斷言：①eval 隔離真值表 ②`web_extra` 跟著每次重生成走 ③**無 web 時 system message 逐字不變**（＝eval 基準不被動到的證明）④來源白名單有生效、濾空不退回全網、**清單必須公司無關** ⑤Grader 時效判準的日期算術，含 `kb_unfixable` 的誤殺真值表（**候選池過期 ≠ KB 沒有更新的**，要拿 collection 天花板再比一次）＋ **時效證據資格**（2026-08-19：財報期間戳不得替池子背書說夠新，含未來日期的回歸鎖與「池裡有 1 天前來源 → 不判過期」的誤報對照） ⑥**web 結果整理**（摘要不得截掉深處數字、地區子網域要擋、單域名上限、同頁去重、過時過濾、query 級 web 預算、`kb_unfixable` 提早跳出）。⚠ 閘門① 的 `_gate()` 是生產判斷式的**抄寫不是 import**，改那一行務必同步改這裡 | 生產 `.venv` |
| [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py) | **驗收 Synthesize 端的確定性 validator（零 LLM、零網路，只讀 Qdrant coverage，秒級）**。十三道閘門 149 項：①`_fiscal_rank` 排序真值表——含 **NVDA 反轉陷阱**（財年非曆年，用 `_source_newest_date` 排序會把 `10K_2026` 判得比更新的 `10Q_202604`(FY2027 Q1) 還新；哪天有人換回去這條會 FAIL） ②期別陽性，**且「引了最新 10-K 但 10-Q 挑舊季」也要抓**（只比全期別會漏）——⚠ 陽性答案**逐字凍結在測試檔裡，不讀 `experiments/`**：讀結果檔的話，一修好陽性就消失，閘門會隨修法生效而自己失去判別力 ③期別陰性對照（沒有「最新」宣稱、已引最新期別、跨公司不比、`最新的 2026 年財報` 是絕對指稱…） ④`_news_freshness_gaps` 看結果不看問法（⚠ 2026-08-19 起**自帶合成 coverage**：KB 已無新聞，讀真實 coverage 會讓三條陽性斷言測不出東西＝量尺失去判別力） ⑤缺口→警語接線（含 **⑤b `realtime` 缺口**：2026-08-19 KB 拔除新聞後，`_news_freshness_gaps` 恆回空 → **時效警語整個死掉**；新判準是「需要即時資料卻沒拿到 web」。那 10 條是回歸鎖，⚠ 含「財報題 → 零缺口」的陰性對照——少了它，警語會印在財報題底下） ⑥端到端回歸：現行結果檔逐題不得留期別落差（與② 方向相反，兩者缺一不可） ⑧R4 `find_unreconciled_web_conflicts`——web↔財報衝突**要求並陳不裁決**（R3 判「一邊為錯」，套到 web 會系統性報舊數字）。⚠ 重點在**誤報對照**：R4 的動作在誤報下也正確，會出事的方向是**話太多**不是漏抓 ⑦`find_untraceable_numbers`——**這項沒有實測正例**，陽性靠變異注入，價值在那組**誤報對照**（億／兆換算、四捨五入、千分位差異；靠它們才有 100 題乾跑誤報 0 題）。⚠ 宣稱詞辨識那組是回歸鎖：第一版詞表窮舉措辭，**同一次迭代內就漏掉自己重生成寫出的「最新單季」「最新公布的季報」** ⑨`_repair_reference_citations`——把 `【Reference 7, chunk #15】`（只有序號沒有檔名）確定性還原成真檔名。⚠ 重點同樣在**兩條誤報對照**：序號越界／`chunk #` 與第 N 筆不一致時**刻意不動**——修補的危險方向不是漏修（漏修＝維持現狀），而是把「無法追溯」變成「看起來可追溯的錯引用」。⚠ `Reference 0` 那條看似邊界瑣事，其實是最危險的一條：Python 的 `[-1]` 是合法索引，少了守門會**安靜地**指到最後一筆 ⑩`_ensure_ratio_source_coverage`——ratio 題的 Fundamentals 保底必須補「**含被問欄位**」的那個 chunk，不是分數最高的（`#1` 是 Balance Sheet、零比率，卻常被 rerank 排在 `#0` 前面）。⚠ **⑩b 才是照到生產病灶的那一組**：⑩ 原本整組假設「`#0` 在池裡、只是沒被挑中」，而 2026-08-21 實測生產組態（英譯 query）下 **`#0` 連 RRF 候選名單都沒進** → 掃池的保底無論怎麼改都救不了。⑩b 測的是確定性補撈（直接查 Qdrant），含「欄位不存在 → 回 None，不可退而求其次」與「分數必須真的算出來、不可捏造」兩條誤報對照 ⑪`_basis_disclosure_notice`——沒指定口徑的比率題只引到財報期間 chunk 時必須揭露。⚠ **⑪b 是這道閘門唯一有效的部分**：⑪ 的 8 條斷言全部拿測試自己造的 `{"period_basis": ...}` 餵進去，於是全綠，而生產的 `rq.retrieve()` **根本沒把 `period_basis` 放進 chunk dict**——validator 在線上結構性永遠不觸發。「payload 有這個欄位」與「chunk dict 有這個欄位」是兩件事，中間隔著一個建構子。⑪b 改拿**真實 payload 餵生產建構子** `rq._payload_to_chunk`（變異測試實測：拿掉那個欄位，⑪ 的 8 條照樣 PASS、只有 ⑪b 叫） ⑫ ratio 意圖改由 LLM 判之後**詞表必須真的退位**。⚠ 判別力**不在口語陽性那條，在「LLM 說空」那幾條**：把覆寫寫成 `if fields:`（而非 `if fields is not None:`）會讓「判定不是 ratio 題」的空 list 掉回詞表 → 詞表仍然在做決定，而**端到端跑分完全看不出差別**（詞表判對的題本來就會過）。含「聯集為空要回 `[]` 不是 `None`」的邊界（回 None 會讓詞表在 Synthesize 端復活）與三條 LLM 輸出解析的誤報對照 ⑬`_compose_answer_tail`——**拒答不得附上「📚 引用來源」**（那段宣稱「Generator 實際依據的 chunk」，印在一份剛說自己沒有依據的答案底下就是假的）。⚠ 判別力**不在陽性那幾條**（一個「一律不附」的實作也會全過），在 ⑬b 的**誤報對照**：拒答判過頭＝把真的有依據的答案的 provenance 砍掉，那才是危險方向——三條各鎖一種近似形狀，其中「寫得長的誠實答案（只有 2023–2025、未包含 2022）」正是多年語料 before 臂 18/18 的形狀。⚠ ⑬a 末條是**回歸鎖**：舊守門 `startswith("I don't have enough")` 唯一擋得住的那句英文預設值，換判準後仍要擋得住 | 生產 `.venv` |
| [`eval/record_web_fixture.py`](eval/record_web_fixture.py) | **live web fixture 的唯一產生入口**（`--mode record` 會連網、燒 Tavily＋LLM 額度；`--mode replay` 絕不連網、miss 直接報錯）。5 題含 **1 個陰性對照**（純歷史財報題，斷言 web 打 0 次——沒有它，「web 有觸發」這個量尺就沒有判別力）。⚠ **必須同時開 `RAG_REPLAY_CACHE`**：送給 Tavily 的 query 由 Planner／Grader 的 LLM 決定，MoE 不固定路由 → 只錄 web 會在重放時 miss。⚠ 2026-08-19 加 `--from-eval-set`（改從 eval set 檔取題）——**fixture 路徑必須一起換**，寫回 `eval/web_fixture.json` 會弄壞既有 5 題的斷言與 as-of 綁定，腳本會直接 ABORT 擋下這件事 | 生產 `.venv` |
| [`eval/check_web_claims.py`](eval/check_web_claims.py) ＋ [`eval/web_claims.json`](eval/web_claims.json) | **live web 路徑的逐條確定性驗收（零 LLM、零網路）**＝這條路唯一的自動化證據。不重測六道閘門已涵蓋的確定性處理，只測**含 LLM 的端到端**：有沒有引用 web、引用主機是否在白名單且非地區子網域、**答案裡每個報價是否都在 fixture 原始內容裡逐字出現（零幻覺）**、KB 舊值與時點是否與 web 新值並陳。⚠ 斷言只能測「不論 LLM 挑 fixture 裡哪個來源都成立」的性質，寫死某一次的答案會 flaky。⚠ N/A 不能併進 PASS。2026-08-20 加 `no_fabricated_citations`（引用必須長得像檔名）——那是**普世性質**，判別力來自 35 PASS／5 FAIL 的雙向分佈，不靠陰性對照 | 生產 `.venv` |
| [`eval/web_claims_news37.json`](eval/web_claims_news37.json) ＋ [`eval/web_fixture_news37.json`](eval/web_fixture_news37.json) | **冷凍 37 題在 live web 上的性質斷言（40 條，含 3 題陰性對照）**＝「live web 能不能取代 KB 新聞」的 ② 內容半。as-of 2026-08-20、86 筆 Tavily 原始回應。⚠ **驗收刻意不對照新聞 gold**：那批 gold 是「2026 年 6 月的新聞說了什麼」，live web 回答「現在的網路說什麼」——**是兩個不同的問題**，比對會系統性低估且低分原因與 web 無關。⚠ **這批不能用 `numbers_must_be_in_fixture`**：題目天生混合（財報半＋新聞半），答案裡 KB 數字與 web 數字並存，沒有字面樣式分得開（實測 news-13 的 7 個「不在 fixture」逐一讀完全是 Fundamentals 值）。現況 PASS 35／FAIL 5，5 個 FAIL 全是同一個 live 專屬缺陷（見 BACKLOG〈live 路徑會出貨指向不存在來源的引用〉）| 生產 `.venv` |
| [`eval/verify_chunk_grounding.py`](eval/verify_chunk_grounding.py) | 驗收 **chunk 層**的幅度接地（零 LLM、只讀 Qdrant ＋ reranker tokenizer）。**重建後必跑**——`verify_segment_split.py` 判準⑤ 量的是 section 層，SemanticChunker 之後的邊界它看不到（2026-08-12 就是這個盲點讓「mix-09 已修好」的宣稱被推翻）。閘門只有 `groundable_not_grounded`；`unreachable`／`blocked_by_cap` 是規則正確地不作用 | 生產 `.venv` |
| [`eval/probe_news_web_routing.py`](eval/probe_news_web_routing.py) | **量「冷凍的 37 題會不會被送去 web」（含 LLM，零 Tavily、零網路，不是閘門）**＝「live web 能不能取代 KB 新聞」這件事的**第一步、也是便宜的那一步**。該問題拆成兩個成本差一個數量級的子問題：①**會不會**打 web（只燒 LLM，本檔）② 打回來的東西**夠不夠**（連網燒 Tavily，`record_web_fixture.py` ＋ `check_web_claims.py`）。**① 是 ② 的必要條件**——不會觸發 web 的題，錄再多 fixture 也救不了。⚠ 零網路的作法是**把 `_tavily_search` 換成計數樁、其餘管線原封不動**，不要另外抄一份判斷式（抄寫會漂移，見 `verify_web_gate_isolation.py` 閘門① 的教訓）；樁回空字串 ＝ 模擬「打了但什麼都沒撈到」，所以**本檔只量路由、不量答案品質**。⚠ 判讀看不對稱的錯誤：news 題 0 次 web ＝ 拿財報硬答且零揭露（危險）／財報題打 web ＝ 白花錢。⚠ `CONTROL_IDS` 那 8 題陰性對照**不可省**——少了它，「一律打 web」也會在 news 那一臂滿分；對照題若被改名而消失，腳本會直接 ABORT 而不是靜默縮水 | 生產 `.venv` |
| [`eval/probe_ratio_intent.py`](eval/probe_ratio_intent.py) | **量 ratio 意圖的 LLM 分類（含 LLM，非零噪音，不是閘門）**。2026-08-25 起「這個子問題問的是不是某個 Fundamentals 比率欄位」由 `_classify_ratio_fields` 判、詞表 `_RATIO_INTENT_RE` 退位成 fallback；**fallback 會遮住 LLM 的失手**（判不出來就退回詞表，端到端跑分看起來跟舊碼一樣）→ 準確度**不能從結果檔推**，只有這一支在直接量。⚠ 判讀看不對稱的錯誤：ratio 題判成非 ratio ＝ 保底不執行、口徑警語也不觸發 → **拿財年數字冒充 TTM 且零揭露**（危險）；非 ratio 判成 ratio ＝ 多撈一個 chunk（只是浪費）。⚠ 6 題陰性對照不可省——少了它們，「一律回 Revenue Growth」也會在 ratio 那一臂滿分。⚠ 口語臂那句**逐字取自實測的 Planner 子問題**，自己改寫措辭會測不到病灶 | 生產 `.venv` |
| [`eval/check_rounding_fidelity.py`](eval/check_rounding_fidelity.py) | **量「答案把來源數字捨成約值」（零 LLM、零網路、只讀結果檔）**＝`rq.SYSTEM_PROMPT` Rule 8 那段 NUMERIC FIDELITY 唯一能被證偽的方式。判準：來源有帶小數的 Y、答案寫了捨入版 X、且答案沒給 Y 而 X 也不在來源裡。⚠ **歸屬必須逐引用**：整篇 contexts 比對會把 `lex-17` 那句「約 18%」放行（同題 10-K 確實寫著 `increased 18%`）——**`18%` 有來源，但不是那一句掛的那個來源**。改成逐引用之後真陽性抓到、粗版在 `sem-05` 的誤報同時消失。⚠ **統計效力很低**：改動前三個結果檔共 195 題只有 1 筆，「改完是 0」幾乎不構成證據。⚠ 先跑 `--selftest`（8 條雙向自測）再相信那個 0 | 生產 `.venv`（只讀結果檔） |
| [`eval/probe_realtime_need.py`](eval/probe_realtime_need.py) | **量 Grader 的 `realtime_need` 分類（含 LLM，非零噪音，不是閘門）**。時效改判分兩半：LLM 判「這題要多新」、Python 算「來源多舊」。後者由 [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) 閘門⑤ 蓋住，**前者只有這一支在量**。⚠ 判讀看的是**不對稱的錯誤**：實時題被判成 `none` ＝ 不叫 web、拿 10-K 回答今天股價（危險）；財報題被判成實時 ＝ 白燒一次 web（只花錢）。⚠ 四題 `none` 是**陰性對照不可省**——少了它們，「一律回 days」也會滿分。⚠ MoE 不固定路由，單輪看不出穩定度，下結論請 `--repeat 3` 以上 | 生產 `.venv` |
| [`eval/probe_multi_company_period.py`](eval/probe_multi_company_period.py) | **多公司題的期別損害 probe（零 LLM、只讀 Qdrant ＋ reranker）**。期碼從 source 檔名解，三臂：單公司路由開（陰性對照）／關（**陽性對照**，證明量尺有判別力）／多公司構造題（被測項）。⚠ 指標必須拆 **kind A（同一家舊季擠掉新季）vs kind B（那家公司連一個 10-Q 都沒進 top-k）**——只看聚合缺失率會把「席位競爭」誤診成「期別問題」，第一版判定就這樣錯過一次 | 生產 `.venv` |
| [`eval/probe_temporal_interference.py`](eval/probe_temporal_interference.py) ＋ [`eval/period_probe_baseline.json`](eval/period_probe_baseline.json) | **多年語料的期別干擾 probe（零 LLM 判定、只讀 Qdrant ＋ reranker）**＝「KB 要不要納入更多年度」這個決定唯一的量尺。三個**互相獨立**的指標：F1 排擠（top-k 被同一節的別年份佔走的席位）／F2 錯選（對的期別沒進 top-k、錯的進來了）／`gold_rank` 連續量。分組鍵是 payload 的 `(ticker, filing_type, item_id)`——「同一節、不同年份」是確定性認出來的，不用字串相似度猜。⚠ 三個指標**不可合併**：實測 F1 68 席 vs F2 4 題，只看聚合會把「多樣性問題」誤診成「期別選擇問題」。⚠ **順序不可顛倒**：要先在乾淨的單年 collection 上取 before 基準，灌下去就回不去了。⚠ gold 對照 `period_probe_baseline.json`（**凍結快照**）展開，不是對照當下 manifest——eval_set 有 4 題 gold 是萬用字元，照當下展開會「撈到哪一年都算命中」＝量尺失去判別力 | 生產 `.venv` |
| [`eval/probe_historical_benefit.py`](eval/probe_historical_benefit.py) ＋ [`eval/period_probe_benefit_queries.json`](eval/period_probe_benefit_queries.json) | **多年語料的收益 probe（零 LLM 判定、只讀 Qdrant ＋ reranker）**＝「KB 要不要納入更多年度」這個決定的**另一半**——`probe_temporal_interference.py` 量的每一格都是損害，這支量的是收益。24 題（歷史題 20 ＋ **當期陰性對照 4**；⚠ 其中 `bh-07`／`bh-16` 2026-08-27 標 `void`——**值不存在 ≠ 事實不存在**，10-K 的重述讓同一事實在新年報變成另一個值，值比對的前提檢查看不見，實際有效 18 題）。⚠ **gold 不寫死檔名**，由 `literal` 在 collection 裡確定性定位 → 「gold 是哪幾份」與前提檢查「這個事實在單年 KB 到底在不在」**是同一個操作**，不會各自漂移。⚠ **收益區從 T-3 才開始**：10-K 損益表自帶三年、MD&A 自帶兩年，拿 T-1／T-2 造題會憑空灌水（BACKLOG 原本舉的例子就是錯的）。⚠ 單年臂的 `gold@k=0` 是**定義使然不是量測**，那一臂的資訊在 `--compare` 的「同節別期席位」——**單年 KB 不是空手，它交回同一節的別的年份**。⚠ 判別力兩列來源不同：control 有真陽性對照（A 關掉就退步），historical **沒有**，只有「不是滿分且兩個 FAIL 各有成因」這個間接證據。⚠ **這支量檢索不量生成**（生成端那一半見 BACKLOG）。先跑 `--selftest` | 生產 `.venv` |
| [`eval/check_historical_generation.py`](eval/check_historical_generation.py) | **多年語料在生成端買到了什麼（零 LLM、只讀結果檔）**＝收益的另一半：`probe_historical_benefit.py` 量檢索，這支量**使用者實際看到的東西**。逐題判三態（答對／承認期間不可得／**拿別年份硬答且沒交代**）。⚠ **不要用 `looks_like_refusal` 判「誠實」**：那個函式問的是「整份答案都不作答」還帶 150 字上限，而最典型的誠實答案是**長的**（要解釋有哪幾年）——第一版就是這樣把一整片模範答案判成危險態。⚠ `answered` 是零噪音（literal 比對），`admits_gap` 的措辭清單是**硬編碼**、只對讀過的封閉母體負責，量新的 run 前必須逐份複核。⚠ **`literal` 與 `answer_literals` 是兩個職責**：前者在英文語料裡做前提檢查與 gold 定位，後者在中文答案裡比對——`bh-19` 的 literal 是英文片語，不拆開就把答對判成危險態。⚠ 它的 before 臂**同時是比值比對更強的前提檢查**（2026-08-27 就是它抓到 `bh-07`／`bh-16` 因重述而無效） | 生產 `.venv`（只讀結果檔） |
| [`eval/verify_cross_period_collapse.py`](eval/verify_cross_period_collapse.py) | **驗收跨期 field collapsing 的規則（零 LLM、零網路、零 Qdrant，毫秒級）**。17 項。兩條最容易在重構時被破壞的界線各有專屬斷言：①同一份 filing 的同節多 chunk **全留**（`item_chunk_index` 0/1/2 是互補內容）②別份 filing 插隊後、原本那份的後續 chunk 仍要留（「看到第 N 個就停」的寫法會在這裡出錯）。⚠ **這支只測規則，不測有沒有用**——collapse 的規則與探針 `crowding_seats` 是同一個定義，開了必然歸零，那是套套邏輯。收益看 `gold@k`、損害看 `--trend` | 生產 `.venv` |
| [`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py) | **驗收期間意圖路由的 Python 半邊（零 LLM，只讀 payload，秒級）**。六道閘門 53 項：①`_fiscal_sort_key` 真值表（含 NVDA 財年反轉）②**ladder 排序 vs 期碼字串比大小**③`ladder_pick`（挑不到必須回 None，**不可退而求其次**）④`period_ref` → filter 對應（只有 `latest` 該產生 filter）⑤`range` → 跳過 collapse 的接線 ⑥**Tier 1 的 label-year 命中資格**（`tier1_hit_is_qualified`）——`fiscal_year` 的硬 filter 是與 `report_label_year` 的雙座標系 OR，而**實測 77 份 filing：10-K 兩者永遠相等（21/21），會分歧的只有 10-Q**（15/56） → 那半個 OR 唯一的效果就是放行「曆年標籤符合、財年不符」的季報。**它只能放寬命中、不能自己構成命中**，否則 Tier 1 假命中會把 Tier 2 的降級與揭露語一起關掉（2026-08-27 生產實測：問 MSFT FY2025 → 五席全是 `MSFT_10Q_202512`、note 空字串）。⚠ 判別力在 **⑥b**：⑥a 全餵合成 payload，ingest 哪天讓 10-K 的兩個欄位分歧，⑥a 照樣全綠而整個推理前提會崩。⚠ ⑥d 鎖的是降級揭露語**不可自相矛盾**（`actual` 裡會出現曆年標籤 2025，措辭必須說「財年 2025」）。⚠ 閘門② **在單年 collection 上幾乎沒有判別力**，腳本會自己印出「判反幾對」並在 0 對時明說**那是測資不足不是系統健康** | 生產 `.venv` |
| [`eval/period_probe_trend_queries.json`](eval/period_probe_trend_queries.json) | **跨期 collapse 的陰性對照**：8 題本來就需要多個期別才答得出來的趨勢題。**eval_set 裡沒有任何一題是這個形狀** → 沒有它，「collapse 弄壞了什麼」這個方向的量尺完全是空的。指標是 `periods_covered@k`，⚠ 那是損害的**上界**不是實害（單一份 10-K 的 MD&A 本來就含跨年比較）| 生產 `.venv`（`--trend`） |
| [`eval/ablate_retrieval_model.py`](eval/ablate_retrieval_model.py) | **檢索側 LLM 換模型的零噪音對照**（20b vs 120b，可換成任何兩個模型）。兩 stage：`understand` 錄下 `parse_query_filters`／`translate_query_to_english` 的輸出（免 Qdrant），`rank` 把那些輸出釘死送進真實檢索器比 top-k 與 gold 命中（**零 LLM**）。噪音底線＝同模型跑兩次。⚠ 別用字串比對當結論——翻譯同模型跑兩次就不一樣（實測 68/100） | 生產 `.venv` |
| [`eval/migrate_fundamentals_pct.py`](eval/migrate_fundamentals_pct.py) | 一次性遷移（2026-08-09 已執行）：Fundamentals 比率欄位小數 → 百分比，**並同步修 gold** | 生產 `.venv` |
