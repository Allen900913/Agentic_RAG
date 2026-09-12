# US Stock RAG — 現況、架構、全域規則、檔案地圖

> **這份檔案只放四種東西**：① 現況快照 ② 架構 ③ **全域規則**（不知道就會做錯事，且與日期無關）④ 檔案地圖。
> **不要往這裡放**：某次改動的診斷過程與數字、單一模組的旗標細節、待辦。判準：**「這條規則換一個人接手還適用嗎？」**
>
> **維護規則**：改動任何檔案前先看這份地圖有沒有因此過時。**有任何一格對不上實況，就在同一次改動裡改到一致。**

---

## 現況快照（2026-09-12）

| | 現況 |
|---|---|
| **生產 collection** | `us_stock_rag_edgar_multiyear`（77 份 filing／13,022 chunks／**無新聞**）。每家 3×10-K ＋ 8×10-Q，橫跨 ~2.5 年 |
| **題庫** | [`eval/eval_set.json`](eval/eval_set.json) **65 題**（semantic 15／mixed 15／lexical 17／colloquial 13／multi_hop 5）。**0 題帶 rubric** |
| **冷凍題庫** | [`eval/eval_set_news.json`](eval/eval_set_news.json) 37 題（2026-08-19 拔除新聞時移出）。**現在不要拿它跑分**（web 預設關，跑了必然全滅） |
| **預設 LLM** | `nvidia/nemotron-3-super-120b-a12b`（2026-09-03 換；前一代 `openai/gpt-oss-120b` 被 NVIDIA 退役／410）。**五個定義點**：[`rag_query.py`](rag_query.py) `DEFAULT_MODEL`／`DEFAULT_GEN_MODEL`、`agentic_rag_version/` 的 `CHECKER_MODEL`／`GEN_MODEL`／`RETRIEVAL_MODEL`。選型判準是**輸出穩定性與延遲，不是模型大小**（證據 `experiments/_model_bakeoff_20260903.log`）。ingest 表格摘要走 Groq `openai/gpt-oss-20b`，不受影響 |
| **RAGAS judge** | `google/gemma-4-31b-it`（2026-09-04 換）。⚠ **跨 2026-09-04 的 RAGAS 分數一律不可比** |
| **確定性閘門** | `verify_period_intent_routing` 85／`verify_answer_validators` 339／`verify_cross_period_collapse` 17／`verify_web_gate_isolation` 256／`verify_eval_harness` 19／`verify_cited_evidence` — **全 PASS** |
| **量尺狀態** | RAGAS 六指標**五個已達或超過 gold 上限**，唯一有空間的是 `answer_correctness`（0.654 vs 0.987）。⚠ **飽和是量尺的性質不是系統的**：chunk 層 gold 量到 `gold@5 0.878`／`gold@20 0.976`、**5/41＝12.2% 的可量題有檢索層缺陷**（41 題×3 輪、翻面 0，`experiments/cgr_rrf30_goldfix3_20260911.json`） |
| **檢索層現況** | 問題主體是**排序不是召回（4:1）**：召回全滅只剩 `sem-05`，排序損失 `sem-03`／`sem-04`／`mix-08`／`col-03`（三輪不動）。層級歸因（`experiments/rla_20260911.json`）四個名額格**全部 0** ⇒ 名額問題已結案。漏斗（`experiments/gold_funnel_r4_20260911.json`）：檢索層 vs 圈選層 ＝ **2 : 5** ⇒ **接下來能動的是 Grader 圈選與 `COMMIT_TOP_K`，不是召回** |
| **五個分母（四輪 09-08~09-11）** | `experiments/agentic/gj_65q_denominators_*.json`。⚠ **一輪的點估計不可靠，一律報區間**：`forced_pass` 15.5%→9.2%→7.5%→7.4%（R1 是離群值，後三輪落在 **7.4~9.2%**）。⚠ **「穩定核心」不成立**（每加一輪就縮，四輪都在的只有 `lex-04`／`mix-12`）。⚠ **逐類沒有任何一格是穩的**，不要引用單類數字。⚠ `forced_pass` **不等於答錯**（R3 的 `mh-01`／`mh-05` 是 forced_pass，而 `check_comparison_claims` 判 PASS）。⚠ **其中 ~2/8 是 Grader 判錯、事實就在 KB 裡**（R3／R4 各獨立複現一次；`mh-04` 那顆 chunk 就在該子問題自己的候選池裡、答案還引用了它）⇒ **不要把 `forced_pass` 接成拒答**。其餘：`kb_unfixable_exit` 恆 0（**snapshot 下是定義不是觀察**，閘門⑰）／`refused_budget` 2→0→0→0 ⇒ **不拆 `MAX_TODOS`**／`revision_stats` R3 起出現退回（24 次重生成 1 次）⇒ 接受守衛有負載／`unit_stats` ③ 10→23→23→28 ＝ **LLM 違反 Rule 11 的頻率高且不穩**／`crashed` 是**時間叢集不是類別**（R4 十題降級全是 provider 端 `NotFoundError: 404` 的連續視窗） |
| **已知帶著上線的取捨** | `RRF_TOP_N_PRIMARY` 20 → **30**（2026-09-11 在修好的 gold 上複驗：`gold@5` 0.829→0.878、`gold@20` 0.927→0.976，差異恰好三題且三輪一致）。**成本兩條路都付**（單次檢索 28.8s → 41.9s），**收益只落在單管線那條路**（`api_server` → web UI ＝使用者實際在用的，一次檢索、top-5 就是 Generator 看到的全部）；agentic 上那三題本來就沒有損失（保底／跨子問題聯集／卡在 Grader 圈選）。⚠ **而單管線的答案層沒有任何 eval 涵蓋** |
| **已知帶著上線的缺陷** | `MSFT_10K_2024.html#158` 幅度接地漏一筆（1/1009＝0.1%）；`*_IncomeStatement_*.txt` 的財年標頭只在 `#0`。兩者都等下次正當重建時一起修，見 [`BACKLOG.md`](BACKLOG.md) |

**一句話交接**：切塊／檢索這條路已經到頂、量尺飽和；現在能動的只有生成層、Grader 圈選與確定性 validator，而驗收一律用零噪音的逐條斷言（`check_number_defects` ＋ `verify_*` 閘門），不是 RAGAS。

---

## 文件地圖（要寫東西之前先看這裡）

| 你手上的東西 | 該寫進 | 判準 |
|---|---|---|
| 架構、全域規則、檔案職責 | **本檔** | 與日期無關、全域適用 |
| 「今天改了什麼」 | [`CHANGELOG.md`](CHANGELOG.md) | 有日期、已完成 |
| agentic 的早期演進（①~⑩） | [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md) | **已封存**，只因生產碼有 7 處 docstring 引用那個編號才留著 |
| 切塊各層、重建注意事項 | [`docs/INGEST.md`](docs/INGEST.md) | 「為什麼切塊長這樣」 |
| 噪音底線、量尺失效史、**已試無效總表** | [`docs/EVAL.md`](docs/EVAL.md) | 「這個數字能不能相信」 |
| agentic 節點設計、validator | [`docs/AGENTIC.md`](docs/AGENTIC.md) | 「為什麼管線長這樣」 |
| 未做、待決、**查清楚後決定不修的極限** | [`BACKLOG.md`](BACKLOG.md) | 還沒做完 |
| 對外說明（安裝、執行、設計理由） | [`README.md`](README.md) | 給不熟這個 repo 的人看 |
| 單一模組的旗標語法、閘門逐條斷言 | **該模組自己的 docstring** | 只有讀那個檔的人需要 |

**三條搬移紀律**：
1. **做完 → 從 `BACKLOG.md` 刪掉、寫進 `CHANGELOG.md`**；查清楚決定不做 → 留在 `BACKLOG.md` 的「已接受的極限」並寫下復活條件。
2. **null result 一定要寫**（`docs/EVAL.md` 已試無效總表）。沒寫下來的無效實驗會被重跑——發生過兩次。
3. **同一件事只寫一個地方**，其他地方放連結。重複的兩份必然漂移。

---

## 架構

```
fetch_data.py ──► data/raw/{Filings,sec_local,Fundamentals}   （唯一對外抓取，會連網）
                  （News/ 仍在磁碟上但**不進 KB**）
                          │
data_update_edgar.py ─────┘  切塊六層 → BGE-M3 dense+sparse → Qdrant   （零網路）
                                 │
                    ┌────────────┴────────────┐
              rag_query.py                agentic_rag_version/
             （單發管線）                （LangGraph 四節點）
                    │                          │
            api_server.py ──► app.py     eval/run_agentic_on_evalset.py
```

- **檢索**：BGE-M3 dense+sparse hybrid → server-side RRF（`RRF_TOP_N_PRIMARY=30`）→ cross-encoder rerank → top-5。
- **切塊六層**：Item → 期間章節 → 通用小標 → 幅度接地 → SemanticChunker → RCTS 上界 → min-size 下界。前四層是**規則**（「哪裡不准切」），SemanticChunker 決定「裡面哪裡切」——**前者取代不了後者**。
- **⚠ 不要再用 RAGAS 量切塊／檢索**：六個指標五個已達或超過 gold 上限。改用 chunk 層 gold（[`eval/probe_chunk_gold_recall.py`](eval/probe_chunk_gold_recall.py)）。
- **⚠ 判讀 chunk 層 gold 要先看 `@5` 與 `@20` 的差**：@20 進得來、@5 進不來 ＝ **排序**問題；@20 也進不來 ＝ **召回**問題（HyDE 這類機制才有意義的那一格）。合併成一個數字就分不出來。
- **⚠ `@k` 不等於「整個候選池」**（被讀錯過兩次）：RRF 回 `RRF_TOP_N_PRIMARY` 個候選，而 `rq.retrieve()` 回的是那些**經過 hard filter／跨期 collapse／去重刪剩下的**（實測 7~19）。
- **⚠ 「有缺陷」與「修了有用」是兩個問題，第二個仍然沒有量尺。**
- **⚠ 表格摘要模型換過兩次，兩次都是被迫的。換它必須先跑 bake-off**：它是推理模型，`TABLE_SUMMARY_MAX_TOKENS` 太小會讓 reasoning token 吃光正文額度、**靜默吐空字串**。判準見 [`unstructured_components.py`](unstructured_components.py) `TABLE_SUMMARY_MODEL` 上方的證據表。失敗的唯一出口是 [`eval/verify_table_captions.py`](eval/verify_table_captions.py) 的 `missing_on_big`。

### Collection 一覽

| collection | 角色 |
|---|---|
| **`us_stock_rag_edgar_multiyear`** | **生產**（`COLLECTION_NAME` 預設）。77 份／13,022 chunks／無新聞 |
| `us_stock_rag_edgar_mdna` | 前生產（單年 21 份／3,827 chunks），小標層收窄到 `_MDNA_ITEMS` 白名單 |
| `us_stock_rag_edgar_period` / `head` / `ground2` | 切塊層的中間世代，只留作對照。`period` 無小標層，**mix-03 會錯** |
| `us_stock_rag_edgar_exp4` | **歷史基準，不要當對照臂**（Fundamentals 比率還是小數 `0.183` 而非 `18.30%`） |

⚠ **升生產是與 `RQ_PERIOD_INTENT_LLM` 預設翻開同一次做的，而且必須如此**：多年語料上線而它沒開，當期題會退步。兩半證據見 [`docs/EVAL.md`](docs/EVAL.md)〈多年語料〉。

⚠ **這一格漂移過兩次。跑任何實驗前先 `grep COLLECTION_NAME rag_query.py` 確認**，並用 env `RAG_COLLECTION` 覆蓋而不是改碼。

---

## 全域規則

**語言**
- **回覆一律繁體中文**。程式碼 docstring／註解用繁中，identifier 與 log 用英文。

**兩個 Python 環境，絕對不可混用**
- 生產 `.venv`（[`requirements.txt`](requirements.txt)）：langchain 1.x + langgraph。查詢、agentic、ingest 都在這裡。
- 評測 `.venv-ragas`（[`requirements-ragas.txt`](requirements-ragas.txt)）：langchain 0.3.x + ragas 0.2.15。**只給 RAGAS 用**。
- ragas 0.2.x 綁 `langchain-core<0.4`，裝進生產環境會把 langchain 降版、弄壞 agentic 管線與 SemanticChunker。

**資料範圍：KB 只收「記錄」，不收「流」**
- **KB ＝ 10-K／10-Q ＋ Fundamentals／IncomeStatement。沒有新聞。** `data/raw/News/` 留在磁碟上，但 [`data_update_edgar.py`](data_update_edgar.py) 的 `RAW_EXCLUDE_DIRS` 於掃描階段排除它——**那一行是整條規則的單一開關**。
- 判準是「這份東西的正確性靠什麼判定」：記錄靠**期別對＋有引用＋數字可驗**，流靠**夠新＋來源可信**。本專案為期別正確性做的每一層對新聞**沒有一項成立**（新聞 chunk 連 `report_period_code` 都是 0% 覆蓋）。
- 「市場現在怎麼看」由 **agentic 的 live web 路徑**供應。⚠ 別把它接回 ingest；web fallback 也**不負責抓最新 10-Q**，那是 ingest 的事。
- ⚠ **不要為了讓 news 類題目有分數就把新聞加回來**（完整理由見 [`docs/EVAL.md`](docs/EVAL.md)）。

**抓取與處理分離**
- **只有 [`fetch_data.py`](fetch_data.py) 會連網**。[`data_update_edgar.py`](data_update_edgar.py) 一律離線，照 `data/raw/sec_manifest.json` 取件。
- 不要為了「順便更新」去重抓 SEC：換版會讓切塊實驗不可重現。

**重建 collection**
```bash
.venv/Scripts/python.exe data_update_edgar.py --rebuild --rcts-fallback --collection <名稱>
```
- **`--rcts-fallback` 不可省，但預設是關的**。漏掉會少 12% chunks，且 7.4% 的 text chunk 超過 rerank 截斷長度＝內容等於看不到。
- **重建不會逐字重現舊 collection**（兩個正當差異來源）。判斷重建正常與否要比 **text chunk**（確定性），不要比 table。
- **重建後必跑三道閘門**：`verify_table_captions.py`（`missing_on_big` 是 Groq 429 靜默降級的唯一出口）、`verify_segment_split.py`、`verify_chunk_grounding.py`。

**量測（跑任何 A/B 之前）**
- **這個專案的量測解析度比多數改動的效果粗一個數量級**：RAGAS 的 MDE（95%, n=100）換算成「要幾題從 0 修到 0.5」是 **4~7 題**，而典型 ingest 改動只動 2~5 題。
- 所以：**先問「這個差值超過噪音嗎」再問「為什麼」**；A/B 兩臂要共用 `RAG_REPLAY_CACHE` fixture；**提實驗前先 `ls experiments/` 與 [`docs/EVAL.md`](docs/EVAL.md) 的已試無效總表**。
- **`temperature=0` 不等於確定性**：MoE 的溫度只固定取樣、不固定專家路由。
- 逐條斷言（[`eval/check_number_defects.py`](eval/check_number_defects.py)）是零噪音的，聚合指標不是。⚠ **它的判讀一律要 ≥2 輪**（實測同一份 collection 兩輪跑出 PASS 7／FAIL 0 與 PASS 4／FAIL 3，差異全來自 Plan 子問題變異）。
- **跨輪比 RAGAS 聚合值 ＝ 把 judge 噪音算進系統差異。** A/B 兩臂要在同一批判分裡比。

**LLM 與 Python 的分工**
- 判準是「**這個子任務有不有唯一正確答案**」：抽取／裁決給 LLM，比對／定位／算術給 Python。
- **硬編碼詞表是警訊**——那通常代表在用字串比對做感知，換個措辭就漏。例外是**格式定義的封閉集合**（`VALID_*_ITEMS`、`_TABLE_DOMINATED_ITEMS`、`WEB_ALLOWED_DOMAINS`、OCC 選擇權代號）。
- ⚠ **改成 LLM 之後，判別力來自「LLM 說不是就必須不是」**——若空答案會掉回詞表，詞表仍然是實際做決定的人，而端到端跑分看不出差別（`_RATIO_INTENT_RE` 就是這個形狀）。

**期別誰新誰舊，只有一套序**
- **一律用 payload 的 `(fiscal_year, fiscal_period)`**（`rag_query._fiscal_sort_key`／`_get_period_ladder`，agentic 端是 `_fiscal_rank`）。
- **不要拿 `report_period_code` 比字串**：10-K 是 4 位年份、10-Q 是 6 位 yyyymm，混比會**時對時錯**（77 份 filing 實測 385 個配對有 31 對＝8.1% 判反）。
- **財年不等於曆年**：`NVDA_10K_2026` 是 FY2026，而 `NVDA_10Q_202604` 是 **FY2027 Q1**——後者才新。用日期或檔名年份排序都會判反。
- 改期別排序前先跑 [`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py) 與 [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py)。

**web_search（live 專用）的四條規則**
- **eval 隔離只靠 `freshness_mode == LIVE` 與 `ENABLE_WEB_SEARCH` 兩個獨立條件**，各自都足夠。任何「相關性詞表」**都不是隔離機制**——它們守的是相關性卻讓非新聞措辭的即時題永遠打不到 web，而**漏網的代價是把三週前的數字講成「今天股價」**。要改 web 判斷式先跑 [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py)。
- **只有「真實日曆日期」能證明候選池夠新**（`_is_freshness_evidence`）。10-K 的 4 碼財年戳、10-Q 的 6 碼期別戳是**財報期間不是發布日**（`MSFT_10K_2026` 會被算成 2026-12-31＝未來）——它們**不算過期、但也不算證據**。混淆這兩件事會讓財報替整池背書說「夠新」→ `kb_unfixable` 恆 False → 白燒 `MAX_REWRITES` 輪改寫，**且第二道防線被靜默關閉**。`_stale_for_realtime` 與 `_kb_ceiling_date` **必須共用同一套資格判準**。
- **web 內容要能被引用才用得到**。`rq.SYSTEM_PROMPT` Rule 1/2/8（「Every number you state must be traceable to a cited chunk」）會讓模型寧可捏造推導值也不碰 web 數字。放行條款**必須加在 system message**，user message 版本已實測無效。
- **白名單只是授權，不是過濾**。Tavily 的 `include_domains` 是**子網域包含式**比對，一筆 `finance.yahoo.com` 會連 `ca.`／`hk.` 一起收，而那些是**別的市場的報價**（同一天差 12%）。收到結果後要用 `_host_allowed` 自己複核（只認 exact ＋ `www.`）。這個坑踩過兩次。**清單本身必須公司無關**——加一筆 IR 主機名就是 O(n) 的開始。
- **這條路的診斷成本主要在觀測性**：實測兩次根因都是「trace 印得不夠」才查不出來。**先把要判斷的東西印出來，再猜成因**。

**改動前的自我檢查**
- 機制宣稱要先有**能證偽它的確定性測試**（零 LLM、可重跑）＋ 變異測試。實測有四個「聽起來合理」的機制假設被自己的測試推翻。
- 稽核腳本回傳「0 筆問題」先當**壞消息**查；**反過來也一樣：稽核回報 FAIL，先問「是不是量尺錯」**。兩個方向的查證成本一樣是一次 `grep`。
- **量尺凡是要重現生產行為的地方，一律 import 不抄寫**（同一形狀踩過四次）。失敗方式是**改生產的那一刻脫鉤，而量尺照跑、照印數字、外觀完全正常**。守門在 [`eval/verify_eval_harness.py`](eval/verify_eval_harness.py) H3。
- **量尺自己壞掉時，不可以與系統壞掉外觀相同**：cp950 主控台 crash 的退出碼與「有 FAIL」相同、N/A 與 PASS 外觀相同、coverage 太窄時「0 筆」與「真的沒問題」相同。守門在 H1 ＋ 各腳本的三態設計。
- **量尺為了「能攔到」而搬注入點之前，先問「是不是呼叫端該改」**：把 patch 點從 `ar.` 搬到定義模組會讓斷言變綠，但那是**遷就缺陷**，並讓那個裸名呼叫永久隱形。守門在 H2 與閘門⑬。
- **量尺不可與被測物耦合**（犯過六次）。兩種形狀：① 量尺讀了修法會改動的產物（陽性一修好就消失）② 量尺**自備輸入**，於是從來沒驗過生產供不供得出那個輸入。
  → 通則：**驗證器讀哪個欄位，就要有一條斷言拿真實資料餵生產的建構路徑**。
- **測資的形狀必須與生產相同**（踩過七次）：自測寫 `CUDA in 2006` 而生產是 `CUDA in 2006,`、寫英文句子而生產是 markdown 表格、寫 `N 億美元` 而 LLM 寫 `$N 億`——**形狀不同，那條路等於從來沒被測過**。

---

## 檔案地圖

> 一次性診斷腳本（`eval/_*.py`）與已退役的舊 harness（`eval_retrieval.py`、`eval_rerank.py`、`eval_chunk_recall.py`、`eval_two_stage.py`、`ablate_translate_rerank.py`、`classify_answer_shape.py`、`diagnose_crit_miss.py`、`latency_benchmark.py`、`rejudge_with_model.py`、`chunk_size_stats.py`、`eval_agentic.py`）不列於此表。
> **各檔的旗標語法、逐條斷言與參數細節看該檔 docstring**，這裡只寫「它是什麼、什麼時候該看它」。

### 生產／查詢

| 檔案 | 是什麼 |
|---|---|
| [`rag_query.py`](rag_query.py) | **檢索與生成的核心**，其他入口都 `import rag_query as rq` 共用 `retrieve()`／`call_llm()`／`build_user_prompt()`。`COLLECTION_NAME` 在此定義（env `RAG_COLLECTION` 可覆蓋）。**五個「唯一定義點」在這裡，新的消費端一律用它們**：<br>· `finalize_answer_units` — 金額單位後處理的唯一組裝點（① 剝掉 LLM 自算的億 → ② 程式獨佔換算 → ③ 修剩下的孤立億）。⚠ **順序不可調換、只能呼叫一次**（② 不冪等）。⚠ 不要裸呼叫 `convert_usd_units_to_yi`——它明文「不碰已經是億的值」，LLM 先斬後奏時結構性失明。金額資格的唯一判準是 `_is_monetary_yi`<br>· `_payload_to_chunk` — payload→chunk dict 的唯一建構點<br>· `strip_evidence_tail`／`looks_like_refusal` — 「證據尾巴」與「這份答案算不算拒答」<br>· `_sole_ticker(filters, query)` — 「這是哪家公司」。**不要自己重跑 `_find_all_ticker_aliases`**<br>· `_fiscal_sort_key`／`_get_period_ladder` — 期別的序<br>⚠ ticker **不是** `QUERY_FILTER_SYSTEM_PROMPT` 抽的（那支只抽 filing_type／fiscal_year／fiscal_period）——字面公司名（含中文別名）走 regex 表 `_COMPANY_TICKER`，**產品／子公司名走 `resolve_tickers_llm`，且只在 regex 沉默時才被叫** |
| [`api_server.py`](api_server.py) | FastAPI 後端，`/chat` SSE 串流。**產品線走的是這條（單管線）** |
| [`app.py`](app.py) | Streamlit 聊天前端，串 `api_server.py` |
| [`web_replay.py`](web_replay.py) | **Tavily 原始回應的錄／放**（live 路徑的 eval fixture）。未設 env `RAG_WEB_REPLAY` 時完全 no-op。⚠ 刻意錄在 `TavilyClient.search()` 的**原始回應**，不是 `_tavily_search()` 的回傳字串（後者已跑完白名單複核／去重／抽日期／過濾／截斷，錄那裡等於把要測的六道一起 mock 掉）。⚠ `get()`／`put()` **兩邊都要 deepcopy**。⚠ `note_meta()` **只在 record 模式寫**（舊版無條件寫 ＝ 證據自我抹除） |
| [`llm_replay.py`](llm_replay.py) | **A/B 用的重放快取**：plan／replan／英譯／Grader／ratio／期間意圖／ticker 七種中間產物固定下來。未設 env `RAG_REPLAY_CACHE` 時完全 no-op。⚠ **A/B 兩臂共用一份 fixture 時必開 `RAG_REPLAY_READONLY=1`**：`atexit` 無條件回寫 → 先跑那臂的 miss 會變成後跑那臂的 hit（單向污染，兩臂外觀都正常）。⚠ 新增接點要一起註冊進 `_KNOWN_KINDS` |

### Agentic

| 檔案 | 是什麼 |
|---|---|
| [`agentic_rag_version/`](agentic_rag_version/) | **現役 agentic 入口**（2026-09-03 從單一 4549 行檔套件化）。**四個模組，分層單向**：`retrieval`（chunk 操作／KB 涵蓋／ratio 意圖）→ `validators`（**來源**資格：日期、過時、白名單、期別序 ＋ **答案**偵測：citation、衝突、R4/R5/R6、數字溯源、缺口揭露）→ `tools`（Tavily ＋ `rag_search`／`web_search`）→ `graph`（Plan／Grade 契約、route 分派與兩種 executor、LangGraph 四節點）。`__init__.py` 是**門面 ＋ 生成／補救層**。CLI 是 `python -m agentic_rag_version`。<br>⚠ **兩條套件化規則，違反了 eval 會靜默失效**：① 子模組**不得裸用**被 eval monkeypatch 的 14 個名字（含 `ENABLE_WEB_SEARCH` 這類常數），一律 `import agentic_rag_version as _pkg` 再 `_pkg.<name>`——**循環 import 是刻意的**，屬性在呼叫時才解析，stub 才蓋得到；② 子模組**不得 `from .x import` 那些名字**。**定義在哪個模組無所謂。** 守門在 `verify_web_gate_isolation` 閘門⑬ 與 `verify_eval_harness` H2。<br>⚠ `__init__` 的 re-export 名單**從各模組 AST 生成、不手列**（手列實測漏過 9 個）。<br>**節點設計理由與 validator 實測見 [`docs/AGENTIC.md`](docs/AGENTIC.md)** |

**⚠ agentic 一題燒 50~60 次 LLM 呼叫**（單發管線 3~4 次）：每個子問題內部各跑一次完整 `rq.retrieve()`，所以查詢理解層被乘上子問題數。

### Ingest

| 檔案 | 是什麼 |
|---|---|
| [`fetch_data.py`](fetch_data.py) | **唯一對外抓取入口**：edgartools SEC filing ＋ yfinance 基本面 → `data/raw/` ＋ `sec_manifest.json`。份數用 `--annuals N --quarters N`。⚠ 新聞是 `--with-news` **opt-in（預設不抓）**，且抓了也不會進 KB |
| [`data_update_edgar.py`](data_update_edgar.py) | **唯一 ingest 執行入口，零網路**：讀本機 `.nc` → 切塊六層 → 寫入 collection。**News 已由 `RAW_EXCLUDE_DIRS` 於掃描階段排除**。細節見 [`docs/INGEST.md`](docs/INGEST.md) |
| [`unstructured_components.py`](unstructured_components.py) | **函式庫，非執行入口**。unstructured 解析與切塊組件、表格處理（caption／Groq 摘要）、`BGEM3DenseEmbeddings` |
| [`data_update.py`](data_update.py) | 最舊的 ingest 管線，已被 EDGAR 版取代，保留對照 |

### Eval — 輸入與標準答案

> **`eval/` 只放輸入與標準答案，跑分輸出一律寫到 `experiments/`**（部分腳本 `--output` 預設仍指向 `eval/`，要自己帶）。

| 檔案 | 是什麼 |
|---|---|
| [`eval/eval_set.json`](eval/eval_set.json) | **65 題**題庫。**跨 2026-08-19 的分數不可直接比**（分母變過兩次） |
| [`eval/eval_set_news.json`](eval/eval_set_news.json) ＋ `reference_answers_news.json` ＋ `number_claims_news.json` | **冷凍的 37 題**。⚠ 現在不要拿它跑分；用途是日後驗收「live web 能不能取代 KB 新聞」 |
| [`eval/chunk_gold.py`](eval/chunk_gold.py) ＋ `chunk_gold.json` | **chunk 層 gold**（41/65 題）＝「答案落在哪幾顆 chunk」。**gold 由 `literal` 定義、不寫死 `chunk_index`**（重建會位移索引）。消費語意是 **union ＋ all-dropped**（偏大＝偏向 false-negative）。⚠ **41/65 是上限不是缺陷**：未涵蓋的 24 題是 **N/A 不是 PASS**。⚠ `literal_matcher` 的**唯一定義點**在這裡。⚠ 改 `eval_set.json` 的 `relevant` 要跟著跑 `--selftest`；重建 collection 後跑 `--check` |
| [`eval/reference_answers.json`](eval/reference_answers.json) | RAGAS 的 ground truth，**已納入版控**（含 24 處人工校正、非腳本可免費重現） |
| [`eval/number_claims.json`](eval/number_claims.json) | `check_number_defects` 的逐條主張。每條都要**人工驗證過才登錄**。`status`：`known_defect`／`regression_guard` |
| `eval/period_probe_*.json`、`web_claims*.json`、`web_fixture*.json`、`news_routing_questions.json`、`replay_cache.json` | 各探針／閘門的 fixture、題目分類與凍結基準 |

### Eval — 跑分主線（三步；reference 生成一次即可重用）

| 檔案 | 是什麼 | 跑在哪 |
|---|---|---|
| [`eval/gen_reference_answers.py`](eval/gen_reference_answers.py) | ① 生成參考答案。⚠ `--force` 不帶 `--ids` 是全量重生成、**會洗掉 24 處人工校正** → 現在要再加 `--force-all` 才跑得動。⚠ `GEN_MODEL` 的判準與別處不同：**不能與 judge 或系統 generator 同源**（會把 correctness 灌高）→ `openai/gpt-oss-20b`。⚠ **預設仍是「不要重生成」**（只重生成一部分會讓 gold 變成兩代模型的混血） | `.venv` |
| [`eval/run_agentic_on_evalset.py`](eval/run_agentic_on_evalset.py) | ② agentic 版結果檔（含 `contexts` 供 RAGAS）。`--freshness-mode` **預設 snapshot** | `.venv` |
| [`eval/eval_generation_llm_judge.py`](eval/eval_generation_llm_judge.py) | ② 單管線版：真實 retrieve+generate ＋ 3 維判定 | `.venv` |
| [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) | ③ 讀結果檔 → RAGAS 六指標，跑完會印噪音門檻與 gold 上限的判讀護欄。judge ＝ `google/gemma-4-31b-it`。⚠ **跨 2026-09-04 的分數一律不可比**；`GOLD_BASELINE`／`NOISE` 已在新 judge ＋ 65 題上重量。⚠ **`--timeout` 預設 420 在 gemma 上實測不夠**：`answer_correctness` 會逾時 → **靜默變 NaN**，而 NaN 印成 `n/a`、外觀與「算不出來」相同 → 用 `--timeout 900 --nvidia-passes 2`。⚠ `--from-results` 吃多個檔是「拼接同一臂」**不是 A/B**（同 id 會被平均） | **`.venv-ragas`** |
| [`eval/record_web_fixture.py`](eval/record_web_fixture.py) | live web fixture 的唯一產生入口（`--mode record` 會連網燒額度；`--mode replay` 絕不連網）。⚠ **必須同時開 `RAG_REPLAY_CACHE`**（送給 Tavily 的 query 由 LLM 決定）。⚠ `--mode replay` 是 bare `strict` ＋ 唯讀；換 fixture 或加新接點時要退回 per-kind strict。⚠ **重錄前一定要清空 `--replay-cache`** | `.venv` |
| [`eval/migrate_fundamentals_pct.py`](eval/migrate_fundamentals_pct.py) | 一次性遷移（已執行）：Fundamentals 比率小數 → 百分比，並同步修 gold | `.venv` |

### Eval — 確定性閘門（零 LLM、可重跑、秒級；改動前後都該跑）

| 檔案 | 守什麼（逐條斷言看該檔 docstring） |
|---|---|
| [`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py) | **query-understanding → Qdrant filter 的 Python 半邊**，八道閘門 85 項：期別真值表（含 NVDA 財年反轉）／ladder 排序／`ladder_pick` 挑不到必須回 None／`period_ref`→filter／`range` 跳過 collapse／label-year 命中資格／實體解析接線／期間路由讀**已解出的** ticker。<br>⚠ 判別力集中在 **⑥b**（⑥a 全餵合成 payload，ingest 哪天讓 10-K 兩欄位分歧，⑥a 照樣全綠而推理前提會崩）、**⑦b**（regex 抽得到時 LLM 必須一次都不叫）、**⑧g**（呼叫端引數被重構掉要叫得出來）。⚠ 閘門② 在單年 collection 上幾乎沒有判別力，腳本會自己說明 |
| [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py) | **Synthesize 端的確定性 validator**，二十三道閘門 339 項：期別／缺口警語／數字溯源／R4 web↔財報衝突要求並陳／引用修補／ratio 保底／口徑揭露／拒答不得附引用／四道 validator 守門／期間降級揭露的接線與歸因／rnd 0 的 query 逐字／R5 並陳帶時點／R6 web 未採用揭露／金額單位三層後處理（⑲，含 `stats` 分母與 `$N 億` 形狀）／`_fair_select` 順序／重生成接受守衛（㉑）／兩個觀測通道（㉒）。<br>⚠ **這支的價值幾乎全在誤報對照，不是陽性斷言**——多數陽性一個「一律不做」的實作也會通過。<br>⚠ **⑭／⑮ 的 AST 掃描讀的是「整個套件的每個模組」不是 `ar.__file__`**（套件化後後者只是 `__init__.py`）；只收 top-level 函式（`ast.walk` 會把 method 撈進來）。<br>⚠ 凡是「機械式附加到答案」的東西都要驗**邊界前綴**（否則量尺會被受測物的輸出改變，⑱f 第一版是恆真的） |
| [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) | **web_search 的 eval 隔離與資料流**（零網路、零 Qdrant），十七道閘門 256 項：隔離真值表／`web_extra` 跟著每次重生成／**無 web 時 system message 逐字不變**／白名單有生效且公司無關／時效日期算術與 `kb_unfixable` 誤殺真值表／web 結果整理（摘要不得截掉深處數字、地區子網域要擋、單域名上限、去重、預算）／replay 的 miss 不准被 `except Exception` 吞掉／replan 進快取且 key 不含自由文字／fixture 環境綁定不准被 replay 改寫／日期抽取／`route` 分派與 `_effective_route` 隔離（⑪）／replay 唯讀（⑫）／**套件化後 monkeypatch 仍攔得住（⑬）**／Replanner 額度拒絕可見（⑭）／`depends_on` 宣告與結構檢驗（⑮）／`forced_pass` 等四種出場不可合併（⑯）／**snapshot 下時效整條惰性（⑰）**。<br>⚠ 閘門① 的 `_gate()` 是生產判斷式的**抄寫不是 import**，改那一行務必同步改這裡。<br>⚠ 閘門⑦ 守的是**結構性**保證（`FixtureMiss`／`RecordError`／`ReplayCacheMiss` 不繼承 `Exception`），不是契約——全碼庫 16 個 `except Exception`，漏一個就把「fixture 沒涵蓋」變成「系統沒打 web」。<br>⚠ 這支在 **import 時**就把 `ar._tavily_search` 換成 stub，要測真身一律用 `_REAL_TAVILY_SEARCH`。<br>⚠ **變異測試的注入點要在定義處**（executor 是裸名呼叫，只改 `ar.<name>` 到不了呼叫端）。<br>⚠ `_MARKER_WINDOW` 的**數值**（48）沒有任何斷言分得出來——那是判斷，不要當成有證據支持 |
| [`eval/verify_cross_period_collapse.py`](eval/verify_cross_period_collapse.py) | 跨期 field collapsing 的規則，17 項（毫秒級）。兩條界線各有專屬斷言：①同一份 filing 的同節多 chunk 全留 ②別份 filing 插隊後原本那份的後續 chunk 仍要留。⚠ **只測規則不測有沒有用** |
| [`eval/verify_segment_split.py`](eval/verify_segment_split.py) | ingest 的三層硬邊界（**section 層**）。零網路、零 embedding、不碰 Qdrant → 可在別的實驗跑的時候執行 |
| [`eval/verify_chunk_grounding.py`](eval/verify_chunk_grounding.py) | **chunk 層**的幅度接地（只讀 Qdrant ＋ reranker tokenizer）。**重建後必跑**——與上一支缺一不可（section 層報「全部接地」而 chunk 層抓到漏抓） |
| [`eval/verify_table_captions.py`](eval/verify_table_captions.py) | 表格 caption 品質。**重建後必跑**：`missing_on_big` 是 Groq 429 靜默降級的唯一出口。⚠ `no_caption` 本身不是缺陷 |
| [`eval/verify_eval_harness.py`](eval/verify_eval_harness.py) | **守量尺自己的閘門**（19 項自測、4/4 變異）。量尺失效的五個根因裡，這支守三個機械可判定的：**H1** 會印非 ASCII 的腳本一律要轉 stdout **與** stderr（crash 退出碼與「有 FAIL」外觀相同）；**H2** eval 攔截的名字（從腳本**反推**、不手列）在套件子模組裡不得有裸名引用；**H3** 生產常數的值被凍結（`eval_harness_baseline.json`），一變就 FAIL 並列出所有抄寫點要求重新複核。<br>⚠ **H3 刻意不猜哪個字面是抄來的**（那是語意判斷，17 個候選裡多數是誤報）——它守的是「常數變動時有人被迫重看」，**那正是 `RRF_TOP_N_PRIMARY` 20→30 時缺的一步**。<br>⚠ 改動生產常數之後：逐筆複核 → 更新 `reason` → 才可以跑 `--update-baseline` |
| [`eval/verify_cited_evidence.py`](eval/verify_cited_evidence.py) | **文件引用的 `experiments/...` 證據檔必須存在且已納入版控**。守一種**安靜的**失敗：證據檔被 gitignore 擋掉時文件照樣讀得通。⚠ **反過來刻意不查**：沒被引用的檔案不是缺陷 |
| [`eval/repair_degraded_records.py`](eval/repair_degraded_records.py) | **把崩潰降級的題從結果檔挑出來刪掉，好讓 resume 補跑**。守一種安靜的失敗：降級路徑照樣產出 answer、不寫 error，而 resume 判準是「有 answer 且無 error → 跳過」⇒ **降級題會被永遠跳過**。⚠ 判準優先用 `degraded_reason`，**退回**五個 stats 鍵全 `None` 的舊指紋（既有結果檔沒有新欄位）。⚠ 子問題層的 `exec_stats.crashed` **另計回報、不併進刪除名單**（四種出場不可合併）。⚠ `--dry-run` 是預設 |

### Eval — 驗收指標（零 LLM，只讀結果檔）

| 檔案 | 是什麼 |
|---|---|
| [`eval/check_number_defects.py`](eval/check_number_defects.py) | **「數字答錯」類改動的主要驗收指標**。逐條斷言判 PASS／FAIL／N/A 三態。**跑之前先 `--selftest`**。⚠ **N/A 不能併進 PASS**——它是「這一輪沒量到東西」不是通過（第五、第六次失效都是這樣抓到的）。⚠ 一個 id 可以掛多條主張。六次失效史見 [`docs/EVAL.md`](docs/EVAL.md) |
| [`eval/check_historical_generation.py`](eval/check_historical_generation.py) | **多年語料在生成端買到了什麼**：逐題判三態（答對／承認期間不可得／拿別年份硬答且沒交代）。⚠ **不要用 `looks_like_refusal` 判「誠實」**（那個函式問的是「整份都不作答」還帶 150 字上限，而最典型的誠實答案是**長的**）。⚠ 它的 before 臂**同時是比值比對更強的前提檢查** |
| [`eval/check_news_routing.py`](eval/check_news_routing.py) | **新聞題有沒有拿財報冒充新聞**（只讀結果檔）。KB 依設計沒有新聞，所以新聞題引用 10-K／10-Q 就是缺陷——而 Synthesize 的五道 validator **一道都不會響**。⚠ **狀態名中性、判定分類相對**（`kb_only` 在 flow 題是危險態、在 record 題是正確行為）。⚠ **`record` 那一半（陰性對照）不可省**。⚠ 判別力集中在一條規則：**承認措辭必須與流資訊範圍詞同句**。⚠ `mixed` 是**不對稱**判定（`web_grounded` 不判定、`kb_only`／`ungrounded` 判危險）。⚠ 第二軸的歸因條件是**讀判定表**不是寫死 `kd == "flow"` |
| [`eval/check_rounding_fidelity.py`](eval/check_rounding_fidelity.py) | 量「答案把來源數字捨成約值」＝Rule 8 NUMERIC FIDELITY 唯一能被證偽的方式。⚠ **歸屬必須逐引用**（整篇 contexts 比對會把「約 18%」放行）。⚠ 統計效力很低 |
| [`eval/check_comparison_claims.py`](eval/check_comparison_claims.py) ＋ `comparison_claims.json` | **「哪一家最高」這個結論對不對**（multi_hop 唯一的自動化證據）。全碼庫**沒有一行 Python 在做這個比較**，比錯了五道 validator 一道都不會響。⚠ 真值**從答案自己的 `contexts` 算**，不是重跑檢索（否則 FAIL 會混進「檢索沒撈到」）。⚠ 三態不可合併：`wrong_winner`／`unfounded`／N/A。⚠ **判別力上限**：現有語料每題差距都很大，全 PASS 只證明容易的情況不會錯 |
| [`eval/check_web_claims.py`](eval/check_web_claims.py) ＋ `web_claims.json` | **live web 路徑的逐條確定性驗收**＝這條路唯一的自動化證據（含 LLM 的端到端）：有沒有引用 web、主機是否在白名單且非地區子網域、**答案裡每個報價是否都在 fixture 原始內容裡逐字出現**、KB 舊值與時點是否與 web 新值並陳。⚠ 斷言只能測「不論 LLM 挑 fixture 裡哪個來源都成立」的性質。⚠ **`blocked` 狀態**：fixture **在輸入層**就承載不了某條斷言時判 N-A（**照樣讓退出碼非零**）。判準：**FAIL 的成因在輸入 → blocked；輸入有而系統沒用上 → 真 FAIL** |
| [`eval/web_claims_news37.json`](eval/web_claims_news37.json) ＋ `web_fixture_news37.json` | **冷凍 37 題在 live web 上的性質斷言**（40 條，含 3 題陰性對照）。⚠ 刻意不對照新聞 gold（那批 gold 問的是別的問題）；⚠ 不能用 `numbers_must_be_in_fixture`（KB 與 web 數字並存，沒有字面樣式分得開） |

### Eval — 探針

> 共同判讀規則：**看不對稱的錯誤**不是整體準確率；**陰性對照不可省**；MoE 不固定路由，**下結論前 `--repeat 3` 以上**。

| 檔案 | 量什麼 / 不對稱在哪 |
|---|---|
| [`eval/probe_chunk_gold_recall.py`](eval/probe_chunk_gold_recall.py) | **生產檢索的 chunk 層召回率**＝「檢索到頂」目前唯一能重新驗證的尺。⚠ 先看 @5 與 @20 的差不是絕對值。⚠ 偏誤方向**只會高估**（gold 是聯集）⇒ **低分是硬證據，高分不是健康證明**。⚠ N/A 24 題不可併進分母。⚠ 檢索路徑上有一次英譯 LLM → 預設自動掛 `replay_cache.json` 釘死。⚠ `--from-results` 量的是跨子問題聯集，**兩欄不可並排比較** |
| [`eval/probe_recall_layer_attribution.py`](eval/probe_recall_layer_attribution.py) | **召回失敗死在哪一層**：① collapse／filter 刪掉 gold ② RRF 名額 ③ prefetch 名額（`FETCH_N`）④ 真的撈不到。⚠ 寬臂只在生產臂已經失敗時才跑（偏誤保守）。⚠ 錄的是 `client.query_points` 的**回傳**，不重寫一份 `_query_points`。⚠ 生產臂的常數一律 import 不抄寫 |
| [`eval/probe_gold_funnel.py`](eval/probe_gold_funnel.py) | **撈到了 → 有沒有用**（三段：`retrieved_union` → `sources` → `cited`）。⚠ 第一段要 `retrieved_union`（2026-09-09 起才有），舊結果檔只跑得動兩段版且兩群不可合併。⚠ 引用是 LLM 自己宣稱的 ⇒ 每段都只會**高估**。⚠ **進入答案有兩條入口**：`_ensure_ratio_source_coverage` 直接打 Qdrant ⇒ **`sources ⊆ retrieved_union` 不是不變量**（第二條入口記在 `used_via_backstop`，不可併進任何一欄） |
| [`eval/probe_kb_content_ceiling.py`](eval/probe_kb_content_ceiling.py) | **`forced_pass` 的子問題是「KB 真的沒有」還是「沒撈到」**。四格：`prod_sufficient`／`rescued_by_width`／`ceiling_recency`／`ceiling_content`。⚠ **兩種 ceiling 不可混成一格**。⚠ **不對稱**：撈得到誤判成天花板 ＝ 白白拒答財報裡明明寫著的東西，比現況更糟。⚠ **它的陰性對照抽樣自「生產判夠」的子問題 ⇒ 結構上看不到「生產判不夠而事實在 KB 裡」**——所以那個 0/15 誤殺率不可引用（見 BACKLOG）。⚠ 跨輪只重跑 Grader、檢索池凍住 ⇒ 量的是 **Grader 的穩定性**。⚠ `--rescore` 從自己的輸出重算判定（零 LLM） |
| [`eval/probe_ticker_resolution.py`](eval/probe_ticker_resolution.py) | 產品／子公司名 → 母公司 ticker。**只有這一支在量它**（eval_set 65 題全由 regex 解出）。**解不出來** ＝ 只是沒改善；**解成別家** ＝ hard filter 鎖到別人的財報＝比修法前更糟 |
| [`eval/probe_route_classification.py`](eval/probe_route_classification.py) | **Planner 判的 `route` 準不準**（閘門⑪ 只驗接線）。三個指標不可合併：`危險誤判`（flow → 純 kb）／`浪費誤判`／**`跨輪不一致`**。⚠ **必須帶 `--as-of` 且與被比較的那次跑分一致**（as-of 會進 Planner prompt）。⚠ `RAG_REPLAY_CACHE` 開著會造成假的穩定 → 本檔開跑前 exit 2 |
| [`eval/probe_ratio_intent.py`](eval/probe_ratio_intent.py) | ratio 意圖的 LLM 分類。**fallback 會遮住 LLM 的失手** ⇒ 準確度不能從結果檔推。判成非 ratio ＝ 拿財年數字冒充 TTM 且零揭露；反向只是多撈一個 chunk |
| [`eval/probe_realtime_need.py`](eval/probe_realtime_need.py) | Grader 的 `realtime_need` 分類。實時題判成 `none` ＝ 拿 10-K 回答今天股價；財報題判成實時 ＝ 白燒一次 web |
| [`eval/probe_relevant_ids.py`](eval/probe_relevant_ids.py) | Grader 的 `relevant_ids` 圈選＝**承接池的實際守門員**。五個指標不可合併：`gold 全滅`（檔層）／`答案全滅`（chunk 層）／合成混池的`誤選他家`（陰性對照）／`圈選率`（1.00 ＝ 等於沒過濾）／`空圈選`。⚠ **公司層的陰性對照在自然候選池裡不存在**（ticker hard filter 在上游就濾掉了）→ 用 `MIX` 合成混池。⚠ 「gold 檔的**部分** chunk 沒被圈選」刻意不算缺陷 |
| [`eval/probe_replan_contribution.py`](eval/probe_replan_contribution.py) | **Replanner 加的待辦貢獻了什麼**。四個指標不可合併：`n_new`／**`n_new_cited`**／web／秒。⚠ **誤報對照＝把 Planner 的待辦用完全相同的方式量並排印**。⚠ 零網路的樁**必須回傳像樣的非空內容**（回空字串等於先驗地判定 web 待辦沒貢獻）⇒ 這支量的是 KB chunk 的貢獻，不是 web 的 |
| [`eval/probe_temporal_interference.py`](eval/probe_temporal_interference.py) ＋ `period_probe_baseline.json` | **多年語料的期別干擾**（損害那一半）。三個**互相獨立**的指標：F1 排擠／F2 錯選／`gold_rank`。⚠ **不可合併**（實測 F1 68 席 vs F2 4 題）。⚠ **順序不可顛倒**：要先在乾淨的單年 collection 上取 before。⚠ gold 對照**凍結快照**展開 |
| [`eval/probe_historical_benefit.py`](eval/probe_historical_benefit.py) | 同一個決定的**收益**那一半。⚠ gold 不寫死檔名，由 `literal` 確定性定位。⚠ **收益區從 T-3 才開始**（10-K 損益表自帶三年、MD&A 兩年）。⚠ 單年臂的 `gold@k=0` 是**定義使然不是量測**。⚠ 這支量檢索不量生成 |
| [`eval/probe_multi_company_period.py`](eval/probe_multi_company_period.py) | 多公司題的期別損害。⚠ 必須拆 **kind A（同家舊季擠掉新季）vs kind B（那家公司連一個 10-Q 都沒進 top-k）** |
| [`eval/probe_news_web_routing.py`](eval/probe_news_web_routing.py) | 冷凍 37 題**會不會**被送去 web（零 Tavily）。⚠ 零網路的作法是**把 `_tavily_search` 換成計數樁、其餘管線原封不動**，不要另抄一份判斷式 |
| [`eval/judge_regression.py`](eval/judge_regression.py) | correctness judge 自己的回歸套件＝11 個凍結案例。**先跑 `--dry-run`**。⚠ **不是零噪音，n=3 還不夠** → 一律看**逐題 k/n**。⚠ 第四態 N/A（`known_limitation`）不能併進 PASS。⚠ rubric 這條線**目前沒有活的消費端** |
| [`eval/audit_gold_numbers.py`](eval/audit_gold_numbers.py) | **gold 自洽性稽核＝跑任何評測前的前置閘門**。⚠ 不查 filing 類的數字 |
| [`eval/ablate_retrieval_model.py`](eval/ablate_retrieval_model.py) | ⚠ **2026-09-06 起跑不起來，刻意不修**：`MODEL_BIG`＝`gpt-oss-120b` 已 410。**不偷偷指向活的模型**（那兩個常數是已完成實驗的臂定義）。重跑要改哪三個地方寫在該檔 `MODEL_BIG` 上方 |
