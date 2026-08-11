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
fetch_data.py ──► data/raw/{Filings,sec_local,News,Fundamentals}   （唯一對外抓取，會連網）
                          │
data_update_edgar.py ─────┘  切塊六層 → BGE-M3 dense+sparse → Qdrant   （零網路）
                                 │
                    ┌────────────┴────────────┐
              rag_query.py                agentic_rag_v2.py
             （單發管線）                （LangGraph 多節點）
                    │                          │
            api_server.py ──► app.py     eval/run_agentic_on_evalset.py
```

- **生產 collection**：`us_stock_rag_edgar_exp4`。檢索＝BGE-M3 dense+sparse hybrid → RRF → cross-encoder rerank。
- **預設 LLM**：NVIDIA NIM `openai/gpt-oss-120b`；模型名 `gemini-*` 開頭走 Gemini（`rq.call_llm` 依名稱路由）。ingest 的表格摘要走 Groq `llama-3.3-70b-versatile`。
- **切塊層級**：Item → 期間章節 → 通用小標 → 幅度接地 → SemanticChunker → RCTS 上界 → min-size 下界。前四層是**規則**（決定「哪裡不准切」），SemanticChunker 決定「裡面哪裡切」——**前者取代不了後者**。各層細節與診斷見 [`docs/INGEST.md`](docs/INGEST.md)。

---

## 全域規則

**語言**
- **回覆一律繁體中文**（使用者 2026-07-11「以後也是」）。程式碼 docstring/註解用繁中，identifier 與 log 用英文。

**兩個 Python 環境，絕對不可混用**
- 生產 `.venv`（[`requirements.txt`](requirements.txt)）：langchain 1.x + langgraph。查詢、agentic、ingest 都在這裡。
- 評測 `.venv-ragas`（[`requirements-ragas.txt`](requirements-ragas.txt)）：langchain 0.3.x + ragas 0.2.15。**只給 RAGAS 用**。
- ragas 0.2.x 綁 `langchain-core<0.4`，裝進生產環境會把 langchain 降版、弄壞 agentic 管線與 SemanticChunker。

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

**改動前的自我檢查**
- 機制宣稱要先有**能證偽它的確定性測試**（零 LLM、可重跑）。2026-08-09~11 共四個「聽起來合理」的機制假設被自己的測試推翻。
- 稽核腳本回傳「0 筆問題」先當**壞消息**查（實測兩次都是欄位名寫錯或量尺沒有判別力）。

---

## 檔案地圖

> 一次性診斷腳本（`eval/_*.py`、`*_probe*`、`* copy.py`）不列於此表；那些是拋棄式實驗，別當生產碼。
> 各檔的旗標語法與參數細節看該檔 docstring，這裡只寫「它是什麼、什麼時候該看它」。

### 生產／查詢
| 檔案 | 是什麼 |
|---|---|
| [`rag_query.py`](rag_query.py) | **檢索與生成的核心**，其他所有入口都 `import rag_query as rq` 共用 `retrieve()`／`call_llm()`／`build_user_prompt()`。`COLLECTION_NAME` 在此定義，**可用 env `RAG_COLLECTION` 覆蓋**（跑 collection A/B 用 env，不要改碼） |
| [`api_server.py`](api_server.py) | FastAPI 後端，`/chat` SSE 串流 |
| [`app.py`](app.py) | Streamlit 聊天前端，串 `api_server.py` |
| [`llm_replay.py`](llm_replay.py) | **A/B 用的重放快取**：把 plan／英譯／Grader 決策固定下來，讓兩臂差異只剩被測的那一項。未設 env `RAG_REPLAY_CACHE` 時完全 no-op，生產路徑不受影響 |

### Agentic
| 檔案 | 是什麼 |
|---|---|
| [`agentic_rag_v2.py`](agentic_rag_v2.py) | **現役 agentic 入口**。LangGraph 管線 Plan → Execute（確定性檢索）→ Grade → Synthesize（生成 + citation validator + 一致性 validator + reflect）。模型分層 RETRIEVAL=`gpt-oss-20b`、CHECKER/GEN=`gpt-oss-120b`（env `AGENTIC_*_MODEL` 可覆蓋）。時間感知走 `freshness_mode`：`snapshot`（eval）／`live`（prod）。**節點設計理由與 validator 實測見 [`docs/AGENTIC.md`](docs/AGENTIC.md)** |

### Ingest
| 檔案 | 是什麼 |
|---|---|
| [`fetch_data.py`](fetch_data.py) | **唯一對外抓取入口**：yfinance 新聞／基本面、edgartools SEC filing → `data/raw/` ＋ `sec_manifest.json` |
| [`data_update_edgar.py`](data_update_edgar.py) | **唯一 ingest 執行入口，零網路**：讀本機 `.nc` → 切塊六層 → 寫入 collection。四種 doc_type（10-K/10-Q/News/Fundamentals）都在這裡。**切塊各層、增量策略、踩過的坑見 [`docs/INGEST.md`](docs/INGEST.md)** |
| [`unstructured_components.py`](unstructured_components.py) | **函式庫，非執行入口**。unstructured 解析與切塊組件、表格處理（caption／Groq 摘要）、`BGEM3DenseEmbeddings`。只被 `data_update_edgar.py` import |
| [`data_update.py`](data_update.py) | 最舊的 ingest 管線，已被 EDGAR 版取代，保留對照 |

### Eval
> 標準跑法（三步，reference 只需生成一次可重用）：① `gen_reference_answers.py` → ② 產生結果檔（agentic 走 `run_agentic_on_evalset.py`／單管線走 `eval_generation_llm_judge.py`）→ ③ 切 `.venv-ragas` 跑 `eval_ragas_vs_rubric.py`。
> **`eval/` 只放輸入與標準答案，跑分輸出一律寫到 `experiments/`**（`--output` 的預設值仍指向 `eval/`，要自己帶）。

| 檔案 | 是什麼 | 跑在哪 |
|---|---|---|
| [`eval/eval_set.json`](eval/eval_set.json) | **100 題**題庫（news／multi_intent／semantic／mixed／lexical／colloquial 各 15 ＋ multi_hop 10） | — |
| [`eval/reference_answers.json`](eval/reference_answers.json) | RAGAS 的 ground truth，**已納入版控**（跟 eval_set 同級是「標準答案」，且含 24 處人工校正、非腳本可免費重現） | — |
| [`eval/gen_reference_answers.py`](eval/gen_reference_answers.py) | 從黃金來源檔生成參考答案。⚠ `--force` 不帶 `--ids` 是全量重生成，**會洗掉人工校正** | 生產 `.venv` |
| [`eval/run_agentic_on_evalset.py`](eval/run_agentic_on_evalset.py) | 把 agentic 跑在 eval_set → 結果檔（含 `contexts` 供 RAGAS） | 生產 `.venv` |
| [`eval/eval_generation_llm_judge.py`](eval/eval_generation_llm_judge.py) | 單管線版：真實 retrieve+generate ＋ 3 維判定 | 生產 `.venv` |
| [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) | 讀結果檔 → RAGAS 六指標。跑完會印噪音門檻與 gold 上限的判讀護欄 | **`.venv-ragas`** |
| [`eval/check_number_defects.py`](eval/check_number_defects.py) ＋ [`eval/number_claims.json`](eval/number_claims.json) | **確定性數字缺陷檢查（零 LLM、零噪音）＝「數字答錯」類改動的主要驗收指標**。逐條斷言判 PASS／FAIL／N/A 三態。⚠ N/A 不能併進 PASS。量尺的四次失效史見 [`docs/EVAL.md`](docs/EVAL.md) | 生產 `.venv`（只讀結果檔） |
| [`eval/audit_gold_numbers.py`](eval/audit_gold_numbers.py) | **gold 自洽性稽核（零 LLM、零網路）＝跑任何評測前的前置閘門**。⚠ 不查 filing 類的數字（大乾草堆裡「值有沒有出現」不帶資訊） | 生產 `.venv` |
| [`eval/verify_segment_split.py`](eval/verify_segment_split.py) | 驗收 ingest 的三層硬邊界（期間／小標／幅度接地）。零網路、零 embedding、不碰 Qdrant → **可在別的實驗跑的時候執行** | 生產 `.venv` |
| [`eval/verify_table_captions.py`](eval/verify_table_captions.py) | 驗收表格 caption 品質（零 LLM、只讀 Qdrant）。**重建後必跑**：`missing_on_big` 是 Groq 429 靜默降級的唯一出口，`numeric/stub` 是 caption 選錯來源。⚠ `no_caption` 本身不是缺陷（小表不值得花 LLM call） | 生產 `.venv` |
| [`eval/verify_chunk_grounding.py`](eval/verify_chunk_grounding.py) | 驗收 **chunk 層**的幅度接地（零 LLM、只讀 Qdrant ＋ reranker tokenizer）。**重建後必跑**——`verify_segment_split.py` 判準⑤ 量的是 section 層，SemanticChunker 之後的邊界它看不到（2026-08-12 就是這個盲點讓「mix-09 已修好」的宣稱被推翻）。閘門只有 `groundable_not_grounded`；`unreachable`／`blocked_by_cap` 是規則正確地不作用 | 生產 `.venv` |
| [`eval/migrate_fundamentals_pct.py`](eval/migrate_fundamentals_pct.py) | 一次性遷移（2026-08-09 已執行）：Fundamentals 比率欄位小數 → 百分比，**並同步修 gold** | 生產 `.venv` |
