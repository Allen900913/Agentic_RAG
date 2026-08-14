# CHANGELOG

紀錄本專案每次有意義的程式修改（架構調整、參數變更、新增功能、放棄的實驗）。
新條目加在最上面。每筆條目只留「改了什麼、關鍵數字、結論」，診斷過程與推導細節見 git log，不重述。

> `agentic_rag.py`（deepagents 多智能體實驗性入口）的修改紀錄獨立在 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md)。

---

> **已試無效總表**搬到 [`docs/EVAL.md`](docs/EVAL.md)（改動前先查那裡，避免重踩）。
> **已知問題／已接受的極限**搬到 [`BACKLOG.md`](BACKLOG.md)。
> 本檔只放日期式變更記錄。

## 2026-08-14

### web 這條路的九個缺陷：主因是自己把摘要截掉，不是日期把關

**起點**：08-13 修通 web 之後，「蘋果的即時市值是多少？」仍答錯——說「已達 $5 兆」，而更新的 7/31 值 $4.54T 反被當舊。原以為是缺日期把關；加了逐則印摘要的 trace 之後看到的是**六個獨立缺陷**（驗收時又冒出三個），而且真正的主因是最不起眼的那個。設計理由與量測見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A7。

| # | 缺陷 | 修法 |
|---|---|---|
| D1 | **`content[:300]` 把數字截掉**（主因） | `WEB_CONTENT_CHARS=1200` ＋ `search_depth="advanced"` |
| D2 | 單一域名壟斷全部名額 | `TAVILY_PER_DOMAIN_CAP=2`，先撈 12 則再篩 |
| D3 | 無日期 → 六年前的文章與今天並列 | `_url_published_date` 抽日期、`WEB_STALE_DAYS` 濾明顯過時、日期標進 prompt |
| D4 | **web 呼叫次數無上界**（實跑 7 次） | `QUERY_WEB_BUDGET`（query 級）＋ `kb_unfixable` 提早跳出必敗重試 |
| D5 | 同頁多變體各佔一個名額（`/amp/`、`new.`、`http://`） | `_normalize_url` 去重 |
| D6 | 地區子網域＝**別的市場的報價** | `_host_allowed`：本地端只認 exact ＋ `www.` |

**D1 的決定性證據**：trace 印出的 macrotrends 摘要是 `Apple market cap as of Augus` ——完整原文是 `as of August 07, 2026 is $4572.79B`。Tavily 的 content 實測 596~1982 字，數字排在站台樣板文字後面，**300 字正好切在數字前一個字**。資料一直都在，是自己丟掉的。「撈到了卻截掉」與「根本沒撈到」在舊 trace 裡長得一模一樣。

**D4 的根因不是 replanner 失控，是計數器放錯層**：`WEB_SEARCH_MAX_CALLS` 記在 `_RunState`，而 `_RunState` **每個子問題歸零**，所以「上限 3 次」真實語意是「每個子問題 3 次」；確定性執行層更是直接呼叫 `_tavily_search`，連那個計數器都沒經過。

**Tavily 能力實測**（決定架構的那組數字）：只有 `topic="news"` 回 `published_date` 且 `days=N` 真的生效，但 news 模式**拿不到數據頁**（macrotrends／stockanalysis／companiesmarketcap 全消失）——而即時報價題要的正是數據頁。`start_date`／`time_range` 在預設 topic 被靜默忽略。故走預設 topic ＋ 網址推日期。

**一個被自己推翻的修法**：D4 原本打算用「字元 bigram 相似度」認出 replanner 生的同義待辦。實測分離度是負的——正向（同一需求）最低 jaccard **0.04**，負向（不同需求）最高 **0.50**，最糟的負向正是「即時**市值**」vs「即時**本益比**」。那等於用字串比對做語意感知，與被拿掉的 `_RELATIVE_TIME_RE` 是同一個病，**整段撤掉**改成不判語意、只封成本。

**驗收（「蘋果的即時市值是多少？」同一題）**：

| | 修法前 | 修法後 |
|---|---|---|
| 答案 | 「已達 **$5 兆**」（7/28 當最新） | **「約 $4.57 兆」**（8/7 macrotrends），$5T 正確標為 7/28 **盤中**高點、收在 $4.98T |
| 子問題數 | 7 | 4 |
| KB 檢索 | 21 次 | 4 次 |
| web call | 7 次 | **3 次**（預算封頂）|

**同日追加（驗收時發現的第七個缺陷）**：日期標進 prompt 之後，「特斯拉今天股價」把一則 **22 天前**的 WSJ 報導當成「最新可得」，反把未標日期的即時行情頁降為次要。日期機制沒壞，錯的是修訂條款寫的「以標示日期最新的來源為準」——**對「當下數值」類問題這個偏好是反的**。修法：修訂條款按問題類型分岔（當下數值 → 未標日期的行情頁優先；近期發展 → 日期最新者為準），並把 `WEB_STALE_DAYS["intraday"]` 從 90 天收到 **7 天**。

**第八個（同批驗收找到）**：時效警語與答案自相矛盾——Azure 那題主體引用了 CNBC 與 `sec.gov` EX-99.1，底下卻印「Web 未提供可用補充」。成因是 scope 錯配：缺口逐**待辦**算，警語整**篇**只印一次。改成依「本次跑分有沒有用到 web」分岔措辭。順帶記錄該題其實是**改善**：08-13 答 40%（KB 的 FY26 Q3），現在答 **43%**（FY26 Q4），兩個獨立來源互證且其一是 SEC 原始揭露。

**第九個**：web 結果混進 **OCC 選擇權合約頁**（`finance.yahoo.com/quote/TSLA260814C00257500` ＝ 8/14 到期、履約價 $257.50 的買權），一次跑分佔走 3 個名額，而頁上的價格是**權利金不是股價**。與「地區子網域」同一類——**拿到的是別的標的**。OCC 代號是標準化格式（`{代號}{YYMMDD}{C|P}{8 位履約價}`），屬格式定義的封閉集合，用樣式排除正當。負向控制含 `quote/TSLA`、`quote/AAPL/key-statistics` 等一般報價頁不得誤殺。

閘門擴充到**六道 79 項斷言**，新增的 28 項全部零 LLM／零網路，其中兩項是行為斷言（stub 掉 Grader／檢索，量「KB 補不了時只檢索 1 次」與負向控制「一般不足仍跑滿改寫」）。

**第十個缺陷（同日追加）：`kb_unfixable` 會誤殺。** D4 那個「提早跳出必敗重試」的旗標，第一版只看 `_stale_for_realtime()`，而它量的是**候選池**最新那筆——候選池是語意檢索的結果，**池子裡最新是 62 天前不代表 collection 沒有 3 天前的**，很可能只是這輪措辭沒命中。那種不足改寫真的有救，卻被當成沒救跳過。修法：新增 `_kb_ceiling_date()`，從 `_scan_kb_coverage()` 已算好的 coverage map 取「這些 ticker 在整個 collection 最新到哪一天」——那是**與 query 無關**的量，拿它再比一次就能把「檢索沒撈到」和「KB 根本沒有」分開。天花板也過期才標 `unfixable`；掃不到 coverage 則保守維持原行為。判斷邏輯抽成 `_classify_staleness()`，**唯一理由是可測**（內嵌在 LLM 回傳處理裡的分支，閘門碰不到＝從沒被證偽過）。新增 6 項真值表斷言，其中「天花板夠新 → 不得標 unfixable」在舊碼下必 FAIL。

### 單發 vs agentic 全量對照：整體「沒有顯著改善」，但那是抵銷出來的

第一次把兩條管線放在同一條件下量。兩臂同 collection（`us_stock_rag_edgar_mdna`）、同 100 題、`freshness_mode=snapshot`、web 全程關閉，各跑一次 RAGAS，**n=100 且 0 NaN**。結果檔 `experiments/ragas_ARM_{single,agentic}_mdna.json`。

| metric | 單發 | agentic | 差值 | 判定（噪音 0.067） |
|---|---|---|---|---|
| context_recall | 0.711 | 0.767 | +0.055 | 噪音內 |
| context_precision | 0.772 | 0.835 | +0.063 | 噪音內 |
| nv_context_relevance | 0.860 | **0.963** | **+0.103** | **超過噪音** |
| faithfulness | 0.838 | 0.794 | −0.044 | 噪音內 |
| answer_relevancy | 0.678 | **0.822** | **+0.144** | **超過噪音** |
| answer_correctness | 0.616 | 0.658 | +0.042 | 噪音內 |

**agentic 贏在檢索相關性，但 `answer_correctness` +0.042 過不了門檻——不能宣稱它讓答案更正確。**

**真正的發現是分類別抵銷**：`context_recall` 整體只有 +0.055，是因為 multi_hop **+0.267**、multi_intent **+0.184**、colloquial +0.142 的領先，被 lexical **−0.078**、semantic **−0.073** 抵銷。機制與架構預期一致——agentic 拆子問題各自檢索，**需要多份不同證據的題受益**（multi_hop 要串接、multi_intent 要同時撈財報與新聞），**單一精確詞查找的題被拆解引入雜訊**（lexical 單發已 0.911，本來就沒有發揮空間）。⚠ 分類別 n=10~15，個別差值的門檻約 0.17~0.21，**除 multi_hop 外個別都不顯著**；有證據力的是跨兩個指標一致的排序模式。完整判讀限制見 [`docs/EVAL.md`](docs/EVAL.md)〈分類別抵銷效應〉。

同時燒掉三筆成本，都已寫進 `docs/EVAL.md`：①`--from-results` 吃多個檔是**拼接同一臂**（同 id 取平均）不是 A/B，兩臂丟一起得到的平均值正好落在歷史噪音帶、看起來毫無異常——**燒掉 2h49m**，旗標 help 已補警語；②NaN 補完前 `context_precision` 差值是 +0.067 剛好卡在門檻上，補完才確定在噪音內；③背景跑分要 `python -u`，否則 stdout 被緩衝、完全看不到進度。

---

## 2026-08-13

### live／web 這條路修通：三個阻塞點、來源白名單、Grader 時效判準

**起點**：生產模式四題時效題，web_search **0/4 觸發**。拆下去是三個獨立阻塞點，不是一個 bug。設計理由與完整證據見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A6。

| # | 阻塞點 | 修法 |
|---|---|---|
| ① | 兩道硬編碼詞表閘門（`rq.looks_like_news_query`、`_RELATIVE_TIME_RE`）擋在 web 補救判斷式上 | 都拿掉。18 個真實時效措辭實測**漏 10 個** |
| ② | `rq.SYSTEM_PROMPT` Rule 1/2/8 讓 web 內容不可引用＝不可用 | 有 web 時才在 **system message** 附加 `_WEB_SOURCE_AMENDMENT` ＋ 補上 Generator 一直漏接的 `_build_temporal_contract` |
| ③ | Grader 只問「有沒有這個欄位」不問「夠不多新」 | 新增 `realtime_need` 三態（LLM 判）＋ `_source_newest_date`／`_stale_for_realtime`（Python 算），**只降不升** |

**② 的決定性證據**：接好管線（web 資料確實進 prompt）後三次跑分**仍全數退回 6 月快照 $4,962.16B**，其中一次寧可拿舊市值除股數捏造「每股 $204」——違反 Rule 8「Never invent」只為守住「traceable to a cited chunk」。**缺的不是格式，是許可**；且修訂必須在 system message（user message 版本已實測無效）。

**③ 的證據**：「Apple 現在的本益比」**正規式是有過的**，`sufficient=True` 擋下 → 拿掉詞表只修一半。coverage 知識反而把 Grader 推向判「夠」（`_CHECKER_PROMPT` 的防空轉條款明文如此），故**刻意不改那段 prompt**，改在 Python 層改判。

**新增來源白名單** `WEB_ALLOWED_DOMAINS`（原始揭露方 ＋ 有編輯流程的財經媒體，不收論壇／意見文）。**濾空明確回報查無、不退回全網**。實測未餓死結果（每次仍 ~2KB）。

**驗收（8 題 live，今天＝08-13）**：

| | 修法前 | 修法後 |
|---|---|---|
| 「特斯拉今天股價漲跌」 | 「今天下跌 2.96%」← 三週前新聞、零揭露 | **「上漲 2.02%，收於 $334.11，截至 2026-08-13」**【web】 |
| 「Apple 現在的本益比」 | 35.83（6/12 快照，無時點） | 財報 35.83（截至 6-12）＋ **即時 34.67**【web】並列 |
| 「Azure 最新一季成長」（過度觸發控制） | 0 web、答 40% 正確 | **0 web、1 輪、答 40% 正確** |

**eval 不受影響**：`_CHECKER_LIVE_RECENCY_BLOCK` 只在 live 附加、replay cache key 在 live 加 `|| live` 分流、時效改判整段包在 `if _live`。新增 [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py)：**五道閘門 22 項斷言**（零 LLM／零網路／零 Qdrant），含兩項 byte-identical 斷言。

**未解決**（見 [`BACKLOG.md`](BACKLOG.md)）：web 打了但資料沒進答案（蘋果即時市值 7 次 web／18 次時效改判仍用 6/12 值）；成本上升（前漏網三題 1~7 輪 → 21 輪）。

**量測教訓**：八題各跑一次時有兩題看似明顯退步，**各補跑 2 次後兩個都被推翻**。答案品質的 run-to-run 變異大於單次測試的解析度，web 又多疊一層 Tavily 隨機性。

### `us_stock_rag_edgar_mdna` 升生產；切塊這條路確認到頂

**改動**：[`rag_query.py`](rag_query.py) `COLLECTION_NAME` 預設 `us_stock_rag_edgar_period` → `us_stock_rag_edgar_mdna`。

**mdna 驗收鏈（`b6db628`，小標層收窄到 `_MDNA_ITEMS` 白名單）**：

| 關卡 | 結果 |
|---|---|
| 重建（`--rebuild --rcts-fallback`） | 21 filings、3925 chunks、零 429 |
| `verify_segment_split` 判準③／⑤ | 0 ／ 46→0 |
| `verify_table_captions` 硬缺陷 | 0（`footer_caption` 4，非閘門） |
| `verify_chunk_grounding` | `groundable_not_grounded` 0、`unreachable` 17 |
| agentic 全量 100 題 | 100/100，零 `[WARN]` |
| `check_number_defects` | **PASS 4／FAIL 2**（`mix-03`／`mix-09`／`col-11`／`mi-04` PASS；`mi-05`／`mix-07` FAIL，兩者皆為已知缺陷） |

RAGAS：semantic `context_recall` 0.497（ground2）→ **0.593**；`_overall` 0.743 → **0.770**。⚠ 單類別 n=15 的誤差棒約 0.17，**+0.096 不足以宣稱效果成立**，只能說方向與預測機制一致。

### 「改用最簡單的 collection」假設被同源對照證偽

**問題**：既然各臂 RAGAS 都在噪音內，是否該直接用層數最少的 `period`（3699 chunks）？

**做法**：用**今天的 code ＋ 同一份 `replay_cache.json`** 重跑 `period` 全量 100 題（`gj_period_full100_20260813.json`），兩臂唯一差異只剩 collection。**不跑 RAGAS**——檢索指標已飽和，跑了拿不到資訊。

**結果 `period` P3 F3 vs `mdna` P4 F2，差在 `mix-03`**：

```
mdna  ：營業利益增加 64 億美元、成長 20%    ← 單一 chunk #63（正確）
period：營業利益成長 24%、增加約 27 億美元  ← 拼自 #111 + #109 + #108
```

`period` 把**分部層級**的數字當成公司整體。這正是小標層要防的失效模式：沒有小標邊界，「公司整體合計」與「分部門明細」落進同一片沒有範圍標記的文字，LLM 分不清數字的作用域。**小標層（收窄後）的價值由 `mix-03` 證實。**

⚠ **`gj_v2_period_full100_20260808.json` 不是合法對照臂**——它早於 `agentic_rag_v2.py` 的 `3396d55`（期間接地）與 `2f93309`（重放快取＋R3 財報優先來源接地），且當時沒有 replay cache。用它比較會得到**三個假結論**，對齊 code 後全部翻掉：`col-11`／`mi-04` 的 N/A（其實是 anchor 措辭沒對上，數字本來就對）、`mix-07` 的 PASS（今天是 FAIL，與 ground2／mdna 一致）。**以後拿舊結果檔當對照臂，一律先查 code drift。**

### 量尺飽和（詳見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺飽和〉）

六個 RAGAS 指標**五個已達或超過 gold 上限**（`context_recall` 上限 0.766／實測 0.770）。按檢索成敗分桶：`recall`=1.0 的 44 題 correctness 0.684、`recall`<0.5 的 12 題 0.543——**全修好只值 +0.024，低於 0.067 噪音底線**。08-06 之後十二次全量跑分全部落在 recall 0.73~0.77、correctness 0.62~0.66。**切塊／ingest 這條路不要再改了。**

---

## 2026-08-12

### 幅度接地判準下移到 `_merge_small_chunks`（section 層修法被實測推翻）
**背景**：2026-08-11 的幅度接地（`_merge_unquantified_sections`）下在 **section 層**。重建 `us_stock_rag_edgar_ground` 後 `verify_segment_split.py` 判準⑤ 全綠（46→0），但 **mix-09 只有 1/3 PASS，還輸給完全沒有小標層的 `period`（2/3）**。

**真因**：section 層併好之後，**SemanticChunker 會再切開**。AAPL `Segment Operating Performance` 的 2251 字元 section 被切成 1030 + 593——幅度表格留在前半、`Greater China net sales increased …` 落在後半。實測 `ground` 裡**沒有任何 chunk 同時含那句話與 `18,816`**（`period` 的 #65 有，因為它沒有小標層、整段是一個 3112 字元 chunk）。

**修法**：判準下移到 [`_merge_small_chunks`](data_update_edgar.py)，它跑在 SemanticChunker 之後，是**最後一個會改變邊界的步驟**。並傳入 `token_len_fn`/`max_tokens`——呼叫端的 RCTS 補切在它之後，合併若推過門檻就會被切回去。
⚠ **section 層那一層保留，兩者互補不重複**：`head` 的 Greater China 孤兒自成一個 section，同硬邊界內沒有前一塊可併；section 層先把它併進母節，chunk 層才有東西可併。

**確定性單元測試 6/6**（用 `ground` 實際的 #60/#61）：正案例合併且解釋＋數字同在／只用長度判準時不合併／前一塊無數字時不併（＋加上數字後會併的對照）／超過 RCTS 門檻時不併（＋門檻放寬後會併的對照）。⚠ 第一版測試③ 是**我自己寫壞的**——前一塊只給 65 字元，觸發了既有的「首塊過短往後併」，與新判準無關；改用語料裡真實的 482 字元無數字 chunk 才是有效測試。

**新增 [`eval/verify_chunk_grounding.py`](eval/verify_chunk_grounding.py)**：chunk 層閘門，三態 `groundable_not_grounded`（唯一閘門）／`unreachable`／`blocked_by_cap`。含與 `data_update_edgar.py` 的**常數一致性斷言**（讀原始碼文字、不 import，避免 >120s 相依）——常數兩份會漂移，漂移的話閘門會安靜地量錯並回報全綠。

**`us_stock_rag_edgar_ground2` 驗收（21/21、4174 chunks、零 429／零 error）**：

| 閘門 | `ground` | `ground2` |
|---|---|---|
| `verify_chunk_grounding` 漏接數 | 3（FAIL） | **0（PASS）** |
| chunk 層孤兒率 | 8.0%（22/274） | **6.3%（17/270）**，殘餘全是 `unreachable` |
| mix-09 機制（解釋＋數字同 chunk） | 沒有 | **#60（2315 字元）有** |
| `verify_table_captions` 硬缺陷 | 0 | **0** |
| `verify_segment_split` ②③⑤ | 0/0/46→0 | **0/0/46→0** |
| rerank >2048 token 的 chunk | 2 | **2（未惡化）** |

**四條斷言（每條 3 run，與封存基準對照）**：

| 主張 | `period` (3) | `head` (1) | `ground` (3) | **`ground2` (3)** |
|---|---|---|---|---|
| mix-09 | 2/3 | 0/1 | 1/3 | **3/3** |
| mix-07 | 2/3 | 0/1 | 3/3 | **3/3** |
| mix-03 | 1/3 | 1/1 | 2/3 | **2/3** |
| mi-05 | 0/3 | 0/1 | 1/3 | **1/3** |

**mix-09 全勝，且贏過原本最好的 `period`**。殘餘兩個 FAIL 都不在本次改動的層：mix-03 剩下那個是生成層挑錯運算元（拿 Productivity 的 36 億當合併總計），mi-05 是檢索層撈錯 chunk。

**兩個必須記住的教訓**：
1. **修法要下在「最後一個會改變邊界」的層**，否則下游會把它切回去。
2. **驗收閘門要與缺陷同層**。判準⑤ 量 section 層（刻意不跑 SemanticChunker 才能便宜地隨手跑），所以它**結構上看不到** chunk 層——它全綠的同時缺陷還在。「規則生效」與「缺陷消失」是兩個判準。

### 全量 100 題 ＋ RAGAS：「沒崩壞」閘門過關，並解開了 head-vs-period 的懸案
`gj_ground2_full100_20260812`（掛共用 replay fixture，與 `gj_v2_period_replay1` **plan 相同 92/100**——未 blocked 時實測只有 34/100）。六個整體指標**全部落在噪音內**（recall −0.027 vs 門檻 0.029、correctness −0.018 vs 0.019、precision +0.023 vs 0.046），±0.05 粗閘門 PASS。

**但分類別有一個真訊號**：semantic 的 recall −0.190、correctness −0.110（n=15 的類別門檻約 ±0.075）。三臂對照證明**它不是幅度接地造成的，是通用小標層**：

| | `period`（無小標層） | `head`（有小標層） | `ground2`（小標層＋chunk 層接地） |
|---|---|---|---|
| semantic recall | 0.687 | **0.509** | 0.497 |
| semantic correctness | 0.680 | **0.554** | 0.570 |
| 整體 recall | 0.770 | 0.731 | **0.743** |
| 整體 correctness | 0.662 | 0.630 | **0.644** |

`head` 早就是 −0.178／−0.126；`ground2` 與 `head` 的差距（recall −0.012、correctness **+0.016**）遠在類別噪音內。**而 `ground2` 在每一項整體指標上都優於 `head`**——幅度接地把小標層的代價回收了約三分之一。

**這解開了 BACKLOG 的懸案**「head 的 correctness −0.032／recall −0.039，非 AAPL 的 42 題退步沒有已證實的解釋」：**semantic 15 題就貢獻了整體 recall 缺口的 ~68%、correctness 缺口的 ~59%**。機制是**小標層把 chunk 切小 → 每個撈到的 chunk 帶的證據變少**：semantic 的證據量 −31.6%（全類最大），chunk 數卻幾乎沒變（58→56）。

**下一個槓桿（未做）**：`use_heading = item_name not in _TABLE_DOMINATED_ITEMS` ＝ **小標層套在所有散文 item 上**，但它的用途（mix-03 分部/合併混淆）只存在於 MD&A。實測 semantic 撈到的 52 個 filing chunk 有 **43 個（83%）來自非 MD&A**（Item 1 Business 25、Item 1A 7、Part II Item 1A 6），其中 `Item_1` 中位長度僅 1320 字元。把 `use_heading` 收窄到 `_MDNA_ITEMS` 應可同時保住 mix-03 與 semantic——見 `BACKLOG.md`。

### mix-03 改用 `require_text`：`forbid` 對它結構上不安全
ground2 r3 的答案**每個數字都對**（先給合併總計 64 億／20%，再逐部門分解 Intelligent Cloud 27 億／24%），卻因 `forbid_text` 命中 27 億被判 FAIL。**正確的分部分解必然包含分部的值**，所以 `forbid_pct: 24` 與 `forbid_text: 27 億` 兩個都不安全，不是換值就好（同 col-11 教訓）。改由「合併總計的**金額**在不在」承擔判別力。跨 10 個 run 逐格比對：**只有那一格從 FAIL 翻成 PASS，其餘 9 格不變**。

---

## 2026-08-11

### 數字缺陷量尺加兩個斷言型別：`require_text`／`require_chunk`
**動機**：mi-05 與 mix-07 兩個已確診缺陷**在既有工具下都判不出來**，只會落進 N/A（＝無法比對），等於它們不在零噪音回歸網裡。病根是**斷言型別只有一種**（`anchored_pct`），而這兩個缺陷的判別訊號不在「anchor 附近的百分比」那一層。

**新增**（`eval/check_number_defects.py`，claims 用 `kind` 欄位分派，預設 `anchored_pct` 故既有 4 條零改動）：
- `require_text`：答案本文必須匹配 `expect_text`。給 mix-07（Plan 把財報事實譯成新聞查詢→拒答；拒答文本零百分比，`anchored_pcts` 判不出來）。
- `require_chunk`：檢索池 `sources` 必須含指定 `source`+`chunk_index`。給 mi-05（同檔撈到錯 chunk，答案層無論填 expect 或 forbid 都是陷阱）。FAIL 訊息區分「同檔撈到別的 index」（真缺陷）與「該檔完全沒撈到」（可能是重編號）。

**為什麼搶在重建之前做**：封存結果檔是**已知答案的測試資料**，重建後那批 ground truth 就沒了。若那時新斷言回報 0 問題，分不清是修好了還是斷言壞了（CLAUDE.md：「稽核腳本回傳 0 筆問題先當壞消息查」）。

**判別力雙向驗證**（四個封存檔：period full100／replay1／replay2、head full100）：

| 主張 | 結果 | 說明 |
|---|---|---|
| mi-05 `require_chunk` | **4/4 FAIL** | 全部「同檔撈到 #[1] ← 撈錯 chunk」 |
| mix-07 `require_text` | **2 PASS / 2 FAIL** | 兩個答對、兩個拒答 |
| 正對照（斷言改指實際撈到的 #1／拒答文本必含的 `Wiz`） | **PASS** | 證明 FAIL 來自被斷言的內容，不是機制壞掉 |
| 既有 mix-03／mix-09／col-11／mi-04 | **逐格未變** | col-11 3 PASS+1 N/A、mi-04 2 PASS+2 N/A，護欄零誤報 |
| `_validate_claims` 負向測試 | **6/6 擋下** | kind 打錯字、require_chunks 空、缺 chunk_index、缺 expect_text、anchored_pct 缺 anchor、known_defect 缺 forbid |

⚠ `require_text`／`require_chunk` 的 `known_defect` **不強制 forbid**（`anchored_pct` 仍強制）：那條規則的前提是「正解與干擾值並陳也會 PASS」，存在性斷言沒有並陳問題，缺席就是 FAIL。

**附帶修一個潛在錯位**（`eval/run_agentic_on_evalset.py`）：`contexts` 濾掉空內容、`sources` 沒濾 → 兩個 list 可能錯位一格，下游用 `sources[i]` 配 `contexts[i]` 會歸錯來源。改成同一次過濾。**實測 14 個結果檔、1400 筆記錄零錯位＝從未實際發生**，純護欄，歷史結論不受影響。

---

## 2026-08-08

### 抓取／處理分離：ingest 不再連網
**改前**：`fetch_data.py::fetch_sec_filings` 抓 SEC 存 `.html`，`data_update_edgar.py` 又自己 `company.get_filings()` 抓一次、且不理會前者的檔。**同一批 filing 抓兩次，而且每次重新切塊都可能悄悄換到新版本文件**——切塊實驗的差異因此無法歸因。

**改後**：`fetch_data.py` 是唯一對外抓取入口，產出 `sec_local/`（`.nc` 完整申報檔，279 MB / 21 份）＋ `sec_manifest.json`（取件清單）；`data_update_edgar.py` 只照 manifest 取件，零網路。新增 `--allow-fetch` 逃生口（預設拒絕）。

**三個會靜默出錯的坑**（細節見 CLAUDE.md「抓取／處理分離」）：
1. `Filing.sgml()` 本機缺檔會**無聲 fallback 下載** → 自寫 guard，缺檔即中止。
2. `Filing.html()` 對 `<?xml` 開頭的 inline-XBRL **一律重新下載**，SEC 財報全中 → 本機儲存對 10-K/10-Q 幾乎沒生效。`_install_offline_html_patch()` 繞過，繞過前驗證兩份文件內容 **MD5 完全相同**。
3. `local_filing_path()` 回絕對路徑、`SEC_LOCAL_DIR` 是相對路徑 → `relative_to` 拋錯被外層 `except` 吞掉：21 份 `.nc` 全寫成功但 manifest 是空的，畫面卻顯示 `x ... failed`。

**驗證**：攔掉 `sec.gov` 的 DNS 解析後跑完整處理流程，**0 次連線嘗試**，MSFT 10-Q 產出 141 records。

### 期間章節硬邊界（chunking）
10-Q 的 MD&A 把單季與累計寫成相鄰章節，期間**只在章節標題**、內文不重述 → SemanticChunker 把標題切走，內文 chunk 讀不出自己屬於哪一期。**生成器不是讀錯，是資訊不在 context 裡。**

- 曝險：109 個含變動陳述的 filing text chunk 有 **15 個（14%）** 讀不出期間，其中 9 個集中在 MSFT 10-Q（24 個裡 38%）。原因是 `X Compared with Y` 標題結構**只有 MSFT 在用**（12 份 10-Q：MSFT 24 處、其餘 6 家 0 處）。
- 受害題：**col-11**（單季 19/33/12/22% 與九個月 18/29/20% 被當互斥數值並陳）、**mix-03**。
- 修法：`_split_by_period_section` 把標題當硬邊界，切段後各自 chunk，期間標籤前綴進被 embed 的文字 ＋ payload `period_context`。**必須切段不能只貼標**（MSFT `#104` 開頭是九個月數字、標題在它中段）。
- 實測落地：MSFT 10-Q 產出 40 個帶 `period_context` 的 chunk，兩種標籤。

### 其他
- `rag_query.COLLECTION_NAME` 改為可用 env **`RAG_COLLECTION`** 覆蓋——跑 collection A/B 不必改碼（改完忘了改回來是實際風險）。
- `gen_reference_answers.py` 快取加**第三條件 `collection`**：原本只比 `query` + `gold_files`，對「同一份檔案、切塊變了」完全無感。另加 `--force`（配 `--ids` 定向）——條件 ③ 對既有沒有 `collection` 欄位的 reference 判不出來，第一次換 collection 要靠它。同時修掉 `gold_files` 比對的**順序敏感**問題（舊快取存未排序值 → mh-09 被誤判成換版而白白重寫）。

### gold 重生成到 `us_stock_rag_edgar_period`（8 題）
- 條件②自動觸發 4 題（`MSFT_10K_2025→2026`、`AMZN_10Q_202509→202606`）：sem-03 / sem-04 / col-02 / col-03
- `--force` 定向 4 題（切塊改變、檔名沒變）：mix-01 / mix-02 / mix-03 / col-11
- **稽核發現舊 gold 本身有錯**：mix-02 問「最新一季」，舊 gold 卻用了**九個月累計**數字——Search 廣告 `$1.0B/10%`（來自 `#116` Nine Months）、Xbox 硬體 `-31%`、內容 `-3%`。新 gold 取 `#112` **Three Months** 的 `$304M/9%`、`-33%`、`-5%`，與原文一致。**期間標籤脫落連 gold 都毒到了。**
- 已知副作用：mix-01（199→127 字）、mix-03（453→231 字）變短，mix-03 的手寫註記「九個月累計是 $20.4B/22%，不是單季」遺失。長度落差會壓低 `answer_correctness`（見 memory `ragas-correctness-length-artifact`），讀分數時要分開看。
- 移除 agentic 一致性 validator 的 **regex 降級路徑**（詳見 CHANGELOG_AGENTIC A5.3）。

---

## 2026-08-02

> 本日改動集中在**生成契約**（`rag_query.py`，單管線與 agentic 共用此契約）。agentic 端的 synthesize
> 接入、忠實度稽核升級與逐題 smoke 驗證見 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md) ⑧。

- **`SYSTEM_PROMPT` 新增 Rule 11-13**（`rag_query.py`）。逐題讀錯誤分出三族「生成讀壞」，各補一條通用讀法紀律：**Rule 11 單位**（來源 `$X billion` 原樣保留、別轉億——billion→億 是 ×10）、**Rule 12 方向**（引用來源方向詞原文、保留符號、兩期數值靠日期定先後，禁臆測「在改善」）、**Rule 13 per-intent**（多意圖某腿有相關 chunk 卻假性拒答「無資訊」→ 禁止）。evidence-first 變體舊 Rule 11 順移 14、內文 `rules 1-10`→`1-13`。
- **確定性單位換算 `convert_usd_units_to_yi()`**（`rag_query.py` 純文字後處理）：billion→億 是 LLM 翻譯層 token 習慣，prompt 只能隨機壓（news-09「叫它×10」兩跑一對一全錯）。正解＝Rule 11 要 Writer 原樣保留 `$X billion`，再由**純程式**乘算成正確億（billion×10 / million×0.01 / trillion×10000），**零誤報**（`X billion` render 成 `X 億` 100% 是錯）。任何呼叫端可套用；邊界安全（%、EPS `$1 to $3`、GPU 台數、已是億的值都不動）。
- **eval 側 gold**：mi-11 單位錯修正（源文 `$380 billion` 誤寫「380 億」）；確認 col-03（源文真有 $15B OpenAI Series C）/mi-02（74.9%Q1 vs 71.1%TTM 口徑差）/mi-10（FY vs TTM）為 false-negative gold 或口徑差，非系統錯 → 先前 bincorr「~26 真 error」被高估、真實正確率高於 0.645。⚠ `reference_answers.json` 為 gitignore 生成物，gold 修正不進版控。**未動**檢索/filter/模型/agentic 結構。

---

## 2026-08-01

- **Agentic v2 GEN_MODEL 定案 `openai/gpt-oss-120b`**：候選 benchmark 只量了速度/品質、漏了「per-model 限速」這維，踩了兩個坑——`glm-5.2` 單發 127~217s（不可行）、`deepseek-v4-pro` 品質最佳但有低 per-model 429 硬牆（100 題必團滅）。gpt-oss-120b 快（~8s）、不限速、col-01 端到端驗證乾淨，定案。
- **`_nvidia_call_llm` 加 429/5xx 指數退避重試**：先前無重試，長 run 撞 NVIDIA 日配額 → glm 那輪 56/100 題掉進機械 fallback、汙染生成分數。修法：`RateLimitError`/連線類重試（含 `Retry-After`），5xx 才重試、4xx 立即拋；`AGENTIC_PIPELINE_WORKERS` 併發 3→2 降壓。
- **100 題乾淨 RAGAS（v2 pipeline · gpt-oss-120b · 0 fallback/error/refuse）**，_overall：context_recall 0.639、context_precision 0.790、nv_context_relevance 0.890、faithfulness 0.794、answer_relevancy 0.812、answer_correctness 0.552。
  - **關鍵**：同管線同題庫，對照被 429 汙染作廢的 glm 輪，生成兩項暴漲（answer_relevancy 0.490→0.812、answer_correctness 0.376→0.552），檢索三項守住（precision +0.064、relevance +0.030、faithfulness +0.018）。證實 glm 輪低分是 429 fallback 假象，非真實生成品質；這份才是 v2 生成真值。
  - 軟肋：`lexical` recall 0.528（各類最低，精確關鍵字題該撈未撈滿）；`answer_correctness` 偏低多為長度對齊假象（見 07-15 條），別過度解讀。

---

## 2026-07-22

- **LLM backend 統一改 NVIDIA NIM（取代 Groq）**：`rag_query.py` `call_llm()` 非 `gemini-` 分支改走 NVIDIA NIM（單一 key，無 Groq 的 TPD 硬牆）；`DEFAULT_MODEL`/`DEFAULT_GEN_MODEL` 統一為 `openai/gpt-oss-120b`（`meta/llama-3.3-70b-instruct` 在 NVIDIA 上會 timeout，不可用）。`api_server.py` 移除重複的雙份呼叫邏輯，直接沿用 `rq.call_llm`。CLI 實測通過。`eval/`、`agentic_rag*.py` 不動，仍各自獨立走 Groq。

- **RAGAS eval 重建**：`eval_set.json` 改版為 90 題（6 類 × 15），不再需要 rubric，改用 `gen_reference_answers.py` 生成的完整參考答案驅動 RAGAS 六指標。
  - **Bug（重大）**：`gen_reference_answers.py` 續跑快取只認 id 存在，不查 query 是否還是同一題——eval_set 改版沿用舊 id 但換了題目，導致 47/90 題撈到文不對題的舊 reference（如 NVIDIA 的題配到 Microsoft 的答案）。已修：快取判斷加 query 逐字比對，不符即視為 stale 強制重生。修好後 context_recall 0.345→0.679、context_precision 0.402→0.777、answer_correctness 0.352→0.613（不依賴 reference 的 faithfulness/answer_relevancy 幾乎不動，印證問題出在 reference）。
  - 順手修 `eval_ragas_vs_rubric.py`：Groq TPD 燒穿（4 key 同 org 共用額度）→ 加 `EVAL_LLM_PROVIDER=nvidia`；`main()` 缺 UTF-8 reconfigure 導致印 `↔` 崩潰、分數存檔前遺失→已補上，並加 `--timeout`/`--max-retries`/`--nvidia-passes`。
  - **最終結果**（90 題 OVERALL）：context_recall 0.679、context_precision 0.777、nv_context_relevance 0.739、faithfulness 0.784、answer_relevancy 0.738、answer_correctness 0.613。`multi_intent` 類明顯最弱（財報+新聞雙意圖單一 query 難同時撈齊），是後續 Agentic RAG 驗證目標。

---

## 2026-07-21

**新聞 hard filter**：偵測到新聞意圖（`looks_like_news_query`，先前只印 WARN 未路由）→ 硬篩 `doc_type=news`。三處改動：ingest 幫每個 chunk 補 `doc_type` payload（`infer_source_type()` 從檔名推導）；`retrieve()` 疊加 ticker/期別 filter；新檔 `migrate_add_doc_type.py` 用 `set_payload` 補現有 2367 points（不重算 embedding）。驗證：新聞題 Tier1 strict hit、財報題零影響。已知取捨：財報+新聞混合題會被硬篩成只回新聞（待後續量化評估）。

---

## 2026-07-20

- **rewrite × translate 2×2 拆解**：兩者是**負交互**——各自單開都有小增益（rewrite +0.014、translate +0.018），疊加反而全數抵銷、低於 baseline（0.795 < 0.806）。translate-alone 是本 regime（`gpt-oss-20b` retrieval）最佳單一設定。⚠ 此結論確立於 Groq TPD 燒乾後的 `gpt-oss-20b` regime，若生產仍用 `llama-3.3-70b-versatile` 需重跑才能定案。
- **方案 A「evidence-first 生成」實驗**：機制成功（Evidence Log 確實逼模型引用已檢索證據）但分數不動——證明 sem-08 殘餘失敗在 judge/框架層（措辭認定不穩），不在檢索/引用層。col-08（RECALL 受限題）反而因「窮盡覆蓋」被鎖死在錯誤召回上，變差。不轉正、不跑全量回歸，保留為 opt-in 診斷工具（`SYSTEM_PROMPT_EVIDENCE_FIRST` + `--evidence-first`）。
- **Rewrite A/B 定案跑分**（`--repeat 3 --judge-votes 3`）：overall +0.014，弱但一致的訊號（3-5 倍 std）。意外發現：逐題 run-to-run std 普遍 0.2~0.5，遠高於 overall std——过去多次「單跑一次定案」不可靠，col-08/col-04 舊定案因此存疑（待重跑，未做）。

---

## 2026-07-19

- **Judge 模型換代**：`qwen/qwen3-32b` 被 Groq 下架（404），改預設 `openai/gpt-oss-20b`（`judge_regression.py` 11 案例 10/11，優於 `gpt-oss-120b` 的 9/11 且無自評偏誤）。新增 `EVAL_LLM_PROVIDER=nvidia` 開關。
- **修復「靜默降級污染結果檔」的結構性漏洞**（第三次踩同一坑）：`rewrite_query`/`parse_query_filters`/`translate_query_to_english` 對 LLM 例外一律靜默降級，TPD 耗盡時題目照樣跑完、分數照樣算——量到的其實是「沒開 rewrite」。舊的兩份 100 題 rewrite 結果作廢（`n_with_rubric` 實際只剩 69/36，摘要卻照印 100）。修法：新增降級事件記錄，確定失敗就整輪中止（`exit 2`，已完成題目落盤可 resume）。
- **Rewrite A/B 乾淨重跑**（單次，NVIDIA）：overall +0.009，在雜訊範圍內（已知 std_across_runs=0.030）。col-08 反被弄壞（已知極限題）。

---

## 2026-07-16

- **100 題 baseline 首跑**：overall correctness 0.776（與舊 61 題基準不可比較，題集已變）。
- **修復 2 題 rubric 缺陷**（sem-18/col-17 Tesla 能源分部錯設 critical）；**新發現 3 類 bug**：judge 中文「億」單位換算誤判（sem-23）、生成層「億/billion」誤譯（sem-17，新類型）、2 個真實 RECALL 缺口（年度加總句未進池）。
- **eval_set 全量 gold 驗證 + 擴充至 100 題**（各類別 25 題）：發現 2 題整題超綱（sem-06/col-09「Apple Silicon」語料 0 命中，已替換）、3 題快照漂移缺陷（`*_Fundamentals_*` glob 匹配多份快照、數值不同，已放寬 rubric）。逐題證據見 [`eval/eval_set_evidence.md`](eval/eval_set_evidence.md)。

---

## 2026-07-15

- **RAGAS answer_correctness vs 自製 rubric 對照**：ground_truth 若用 rubric 清單合成，篇幅錯配導致 F1 精確率崩塌（~0.31）。修法：新增 `gen_reference_answers.py` 生成完整參考答案，回升到 ~0.51。剩餘差距是結構性的（rubric=加權 recall，RAGAS=F1），不能拿絕對值互相參照。standalone 跑在獨立 `.venv-ragas`（`ragas==0.2.15` 需要 `langchain<0.4`，會弄壞生產 `.venv` 的 `langchain-experimental`）。
- **`parse_query_filters` few-shot 探針**：k=3 確認無效（已寫入死路表）。意外發現 8b zero-shot 在 filter 任務上不輸 70b，但驗證 rewrite/translate 品質的對照組被 Groq TPD 耗盡阻斷，數據作廢。
- 順手修：`eval_chunk_recall.py` 用 `qwen3-32b` 當 judge 時 `<think>` 區塊沒剝除，JSON 解析 100% 失敗、靜默 fallback 成 `ckpt_R=0.00`。

---

## 2026-07-14

- **sem-08 定案為已接受極限**；col-08/col-04/col-05 現狀維持（見上方清單）。
- **工程債清理**：`api_server.py` 拆 `retrieval_model`/`gen_model` 為獨立參數（避免前端換模型悄悄改變檢索行為）；新增 `eval/.gitignore`，96 個誤入版控的結果檔 `git rm --cached`。
- **`col10_or_logic`**：judge 的 OR 邏輯從 prompt 移進程式碼、用 `any()` 聚合，11/11 通過（過去靠 LLM 自己判斷「A/B」擇一命中很不穩）。

---

## 2026-07-13

- **Rerank 延遲攻堅**：根因是 `bge-reranker-v2-m3` 在純 CPU 上評分 20 候選要 ~46s，是全流程壓倒性瓶頸。`batch_size=1` 免費 2.33x 加速零損失（消除 padding 浪費，**已採用**）；`max_length=2048`（全庫僅 1.6% chunk 超標，與未截斷結果逐位一致，**已採用生產值**）。疊加後 130s→37s（3.5x）。ONNX/小模型/級聯三條路都失敗（見死路表）。
- **轉生產預設**：`enable_rewrite` + `translate_query_en`（雙 query 取最高分）正式成為 CLI/api_server 預設。lexical/mixed 零回歸；colloquial 表面分數低但逐題拆解無新回歸（皆可歸因已知問題）。
- **正式修法**：`translate_query_en` 從「整組替換」改成「rerank 對每個候選同時用原句與翻譯句評分、取逐候選最高分」——sem-09/sem-11 同時修好（舊版全有全無會顧此失彼）。

---

## 2026-07-12

- E5 全量驗證：舊版 `translate_query_en`（全有全無替換）暫不轉正——sem-08 未真修好、sem-09 新退步（後由 07-13「雙 query 取最高分」解決）。
- sem-08 根因確認到生成層：目標 chunk 穩定進 top-5，但因混雜大量不相關內容、AWS 獲利只是從屬子句，生成模型 3/3 次選擇性略過。
- Judge 轉正 `qwen/qwen3-32b`（11 案例 10/11，優於 20b 的 9/11）。col-08 文件化為已知極限。

---

## 2026-07-11

- **rewrite 新增「語域轉換」規則** + glossary scoping 修正 + 中文 ticker 別名：根因是「策略提問語域」與「會計敘述語域」不匹配。sem-08/sem-11 k=3 穩定修好，lexical/mixed 零回歸。
- `eval/judge_regression.py` 建立：11 案例回歸套件，發現財年措辭修法未穩定生效。
- colloquial 首次驗證推翻「口語 degrade」假說：7 題非滿分裡 3 題是純測量 bug（judge/rubric），非系統退步。**教訓：分數低不要急著找新機制，先排除判定工具本身的既有毛病。**

---

## 2026-07-10

**財年措辭誤判**：07-09 只重判舊答案的驗證方法有漏洞（未驗證新生成內容本身）。真正修法改在生成端——`build_user_prompt()` 偵測 YYYYMM 期間碼，注入指示要求答案並列使用者代碼與 fiscal quarter 用語，從根源消除歧義。

---

## 2026-07-09

- **乾淨隔離 A/B**：retrieval 固定 70b、只換生成模型——`gpt-oss-120b` 全面勝出（mean 0.627→0.870，11 題零回歸）。**決策：`DEFAULT_GEN_MODEL` 轉正為 `openai/gpt-oss-120b`**，`retrieval_model` 維持 70b。
- 拆分 `retrieval_model`/`gen_model` 為獨立參數（解掉換模型 A/B 時檢索側被悄悄一起換掉的混淆變因）。
- 新增 `--judge-votes` 多數決；judge prompt 加 FISCAL vs CALENDAR 規則修財年措辭誤判。
- 溫度拆分（見 07-06）延伸驗證：檢索側 temp=0 純賺，生成側需保留隨機性。

---

## 2026-07-08

- gen=120b 全量驗證：sem-02 真實修好；先前「sem-03/07 幻覺」結論是自己查錯 chunk，已撤回。
- **sem-02 三層失敗（RECALL→RANK→生成）逐層修好**：新增 `_COMPANY_PRODUCT_GLOSSARY`（先 grep 語料確認才收錄）+ `rerank_multi_query` 英文變體版 + gen=120b，四層修法缺一不可。
- **教訓寫入 CLAUDE.md**：judge model 與 retrieval/gen model 是獨立參數，不可圖方便共用同一變數（`diagnose_crit_miss.py` 曾誤用導致診斷結論過於樂觀）。
- sem-11 資料修復：`fetch_data.py` 預設含修正案 10-K/A（無風險因子章節），改 `amendments=False` 重抓後直接修好，對全部 7 檔有效。
- Bug：7 支 eval 腳本硬編碼舊 local Qdrant path，Docker 遷移時漏改，統一改用 `make_qdrant_client()`。

---

## 2026-07-07

**🔑 重大發現：sem-02/08/11 排序問題根因是 cross-lingual rerank**（中文問句 vs 英文文檔評分嚴重失真）。翻成英文後命中分數暴漲直接進 top-k，是先前所有「調數量/multi-query/chunking」路線全部無效的統一解釋。三題不是同一種病：sem-11=純排序（翻譯已解）、sem-02=排序+邊緣（後續 glossary 解決）、sem-08=召回層（翻譯救不到，稀釋型 chunk）。

新增 `--repeat k`，建立「單次跑 mean 差 <0.1 是噪音」的量測紀律。

---

## 2026-07-06

- 溫度拆分：檢索側/judge 用 temp=0，生成答案用 temp=0.3（`GEN_TEMPERATURE`，**已採用**）——生成側 greedy 反而更差（mode collapse，擠掉內容多樣性）。
- 生成 prompt 加 Rule 9：多公司比較題強制逐家點名（sem-04 從 1.0 掉到 0.3 後修好）。
- Vector DB：Qdrant local mode（排他檔案鎖）→ Docker server mode，依 `.env` 的 `QDRANT_URL` 自動選模式。

---

## 使用慣例

- 修改 retrieval / ingest / prompt 邏輯前，先查上方「已試無效總表」。
- 每次修改後在最上面新增日期區塊，只寫「改了什麼、關鍵數字、結論」——診斷過程見 git log。
- 「已試無效」的項目附復活條件，寫進總表而非分散在各日期區塊。
