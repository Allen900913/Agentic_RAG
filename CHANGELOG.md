# CHANGELOG

紀錄本專案每次有意義的程式修改（架構調整、參數變更、新增功能、放棄的實驗）。新條目加在最上面。
**每筆條目只留「改了什麼、關鍵數字、結論」**，診斷過程與推導細節見 `docs/` 與 git log，不重述。

> **這裡不是現況。** 要知道「現在是什麼狀態」看 [`CLAUDE.md`](CLAUDE.md) 開頭的〈現況快照〉。
> **已試無效總表**在 [`docs/EVAL.md`](docs/EVAL.md)（改動前先查那裡）。
> **已知問題／已接受的極限**在 [`BACKLOG.md`](BACKLOG.md)。
> agentic 的早期演進（deepagents → LangGraph，模組已刪）在 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md)。

⚠ **三個「跨此日不可直接比分數」的斷點**：
> **2026-08-09** `strip_citation_footer` 開始剝 inline 引用標記（佔答案本文 25% 字元）。
> **2026-08-19** 題庫 100 → 63 → 65（`GOLD_BASELINE` 與 `NOISE` 兩組常數同時失效，已於 08-20 重量）。
> **2026-08-25** `SYSTEM_PROMPT` Rule 8 加 NUMERIC FIDELITY（所有入口共用）。

---

## 2026-08-28

### 兩條接線：期間路由讀得到已解出的 ticker；四道 validator 守門換掉英文字面

起點是盤點「LLM 到底用在哪些地方」——全碼庫 `call_llm` 共 **15 個呼叫點**，單發管線一題 **3~4 次**、
agentic 一題 **50~60 次**（每個子問題內部各跑一次完整 `rq.retrieve`）。盤點照出兩個缺陷，
**兩個都是接線問題，零額外 LLM 呼叫、零 prompt 改動**。

**① 同一次 `retrieve()` 裡，兩條管線對「這是哪家公司」給出不同答案。**
`parse_query_filters()` 的產出已含 ticker（regex ＋ 沉默時 LLM 補的產品名），三十行後
`_resolve_period_filter_llm()` 卻**自己重跑一次 regex** → 看不到 AWS → 不路由。
修法：新增 `rq._sole_ticker(filters, query)`，呼叫端把 `detected_filters` 傳進去。
端到端：「AWS 最新一季」從沒觸發 → 路由到 `202606`、top-5 **5/5 `AMZN_10Q_202606`**；
Microsoft 那題（陰性對照）逐字不變。
⚠ 舊路徑 `_resolve_latest_quarter_filter` **刻意不接**——它是 `RQ_PERIOD_INTENT_LLM=0` 的乾淨對照臂。

**② `_node_synthesize` 還有四道守門寫著 `answer.startswith("I don't have enough")`。**
08-27 修引用尾巴時只改了尾巴，一致性／期別／reflect／數字溯源四道漏改。舊守門是英文字面、
只認開頭，而 Writer 講中文 → 中文拒答穿得過去 → 白付 `_extract_claims` ＋ `_reflect_and_fix`
**至少兩次 LLM 呼叫**去稽核一個沒有東西可稽核的對象。四道全換成 `rq.looks_like_refusal`。
⚠ **危險方向不是漏判而是判過頭**：有依據的答案被當成拒答 → 四道 validator 一次全部跳過。

**量尺**：`verify_period_intent_routing` 69 → **85**（新增⑧）、`verify_answer_validators` 152 → **170**（新增⑭）。
兩道的判別力都在**接線鎖**，各自做過零 LLM 變異測試：拿掉呼叫端引數 → 只有 ⑧g 叫；
四道守門只改三道 → ⑭a **FAIL 2 條**（逐個 validator 走 AST 驗，不是字串 grep）。
⚠ **⑭e 第一版是量尺自己錯**：fixture 本體約 130 字、還在 `REFUSAL_MAX_CHARS` 之下，**本來就該**判成拒答。

### 產品名解析成母公司（AWS → AMZN）

「AWS 在 2022 年的淨銷售額」抽不到 ticker → 無 hard filter → Tier 3 跨公司污染
（top-5 是 4 家公司的 IncomeStatement）。修法：新增 LLM 實體解析節點 `rq.resolve_tickers_llm`，
**只在 `_COMPANY_TICKER` 這張 regex 表沉默時才被叫**。修後 top-5 **5/5 AMZN**，且撈到 FY2022
數字**真正所在**的 `AMZN_10K_2023`。

不加一筆 `"aws": "AMZN"` 的理由是實測的：20 個一般人會用的產品／子公司問法**18 個抽不到**
（Azure／iPhone／YouTube／Reality Labs／CUDA／Model Y／Xbox／LinkedIn／Prime／Waymo…），
而這種名字每季都在長。
⚠ **只在 regex 沉默時叫，理由不是省錢是精度**：regex 命中的是字面公司名、高精度，LLM 沒有理由
推翻它。這跟 ratio 意圖那次（詞表必須真的退位）**不是同一個形狀**——那裡詞表會在 LLM 表過態
之後蓋回去，這裡詞表沉默時才問 LLM。
⚠ **BACKLOG 上那條記錄本身寫錯過**：原本寫「ticker 抽取本來就在 LLM 那一側，該補 prompt」。
**不是**——`QUERY_FILTER_SYSTEM_PROMPT` 只抽 filing_type／fiscal_year／fiscal_period。

**量法兩支，職責分開**：接線 → 閘門⑦（53 → 69 項，零 LLM），判別力在誤報對照
「regex 抽得到時 LLM 必須一次都不叫」；準確度 → 新增 `eval/probe_ticker_resolution.py`
（**只有這一支在量它**：eval_set 65 題全部由 regex 解出 ticker，這修法在既有跑分上量不到差異）。
`--repeat 3`：知名產品 60/60、**10-K 分部名 15/18**、陰性對照 24/24 全回空、**指錯 0**。
⚠ 陽性臂 100% 是「回 0 筆先當壞消息查」的情形，所以做了兩件事才敢信：①零 LLM 變異測試
（「一律回空」只有陽性臂抓得到、「一律猜 MSFT」兩臂同時叫）②加一層 10-K 分部名的難題臂，
它立刻找出一個真的解不出來的，而且失敗方向是**安全的那一邊**（回空 ≠ 鎖到別家）。

順手補 `llm_replay._KNOWN_KINDS` 漏註冊的 `period_intent`（08-19 加的接點）。症狀很安靜：
bare `strict` 照樣涵蓋它，只有 `strict:period_intent` 會被當成拼錯而報錯。

### 閘門⑬e：拒答**仍然**會帶尾巴，`looks_like_refusal` 的切尾巴不可以拿掉

`_compose_answer_tail` 只管 📚 那一塊；`_node_synthesize` 在 collected/web 皆空時走的是另一條路
（拒答 ＋ 時效警語，live 才有），**不經過它**。實測那條路的答案不切尾巴就**判不出是拒答**。
那個 ⚠ 尾巴是**該留的**，所以該留的也是 strip。（149 → 152 項）

---

## 2026-08-27

### 拒答不再附假的引用清單

`agentic_rag_v2` 在答案尾端機械式附「📚 引用來源（Generator 實際依據的 chunk）」，而守門原本是
`answer.startswith("I don't have enough")`——**只擋得住 graph 崩潰時那句英文預設值**。模型自己
用中文寫的拒答一路通過，於是那句 provenance 宣稱印在一份**剛宣告自己沒有依據**的答案底下。
**實測既有結果檔 4175 份答案／59 份拒答，21 份是這樣出貨的。**

- 判準改用 `rq.looks_like_refusal`。該函式**從 `eval/` 搬進 `rag_query.py`**——生產也要用它，而
  生產不可以 import `eval/`；eval 端改成轉出，兩條 import 路徑都不變。
- 尾巴組裝抽成純函式 `_compose_answer_tail`。**抽它不是為了好看**，是為了讓閘門能零 LLM 直接測
  生產那條判斷，而不是去 inspect 原始碼字串（抄寫必然漂移）。
- 閘門⑬（138 → 149 項）。⚠ 判別力**不在陽性那幾條**（「一律不附」也會全過），在 ⑬b 的**誤報對照**
  ——拒答判過頭＝把有依據答案的 provenance 砍掉。雙向變異：換回舊守門 → ⑬a 掛 3 條；
  換成「一律不附」→ ⑬b 掛 4 條。三種近似形狀裡最重要的是**寫得長的誠實答案**。
- ⚠ **不會移動既有分數**：所有消費端本來就會切尾巴，這次只是讓那塊 metadata 一開始就不要產生。

### `eval/judge_regression.py` 修好（壞了一個月）

病灶：2026-07-23 移除自製 Hallucination Rate／Answer Relevance 時 judge 的簽章少了兩個參數，
本檔沒跟上 → `TypeError` 一跑就炸。連同 11 題身上的死資料與已下架的預設 judge model 一起清掉。

⚠ **真正的教訓不是那個 TypeError，是「要燒 LLM 才跑得動的東西平常沒人跑」。** 所以加了
**零 LLM 的 `--dry-run` 接線檢查**：AST 從 `run_case` 自己的原始碼讀出實際送出的 kwargs 比對
judge 簽章（不另抄常數，**抄寫正是本次病灶**）、每題 `expect` 的 checkpoint id 不得越界
（寫個不存在的 id 會**永遠成立**＝量尺無聲死掉）、整套要同時有正例與反例。

⚠ **這支不是零噪音，單輪總分完全不可比**：同一份碼三個單輪 **10/11、9/11、11/11**，而兩輪
`--repeat 3` 之間**也不一致**。九次觀測攤開才看得出只有 `sem03_true_fabrication_negative` 是
**1/9**（其餘掉分都是 7/9 的雜訊）。判定改成報**逐題 k/n**，**退出碼只認「全掛」**——把時好時壞
也弄成紅燈，這支就會再次被當成壞掉而沒人跑。
⚠ **「≥2 輪」那條規則在這裡不夠用，我被同一批資料修正了兩次**（先把單輪 FAIL 當真陽性；接著
據一輪 `--repeat 3` 寫下「兩個捏造反例方向一致」，第二輪就推翻）。**n=3 對這支還是不夠。**

修的過程照出兩件更重要的事（已進 BACKLOG）：rubric 這條線**沒有活的消費端**；FABRICATION SCOPE
要 judge 判「未在來源出現」而 **judge 拿不到來源**。`sem03` 因此標成 `known_limitation`
（失敗記 **N/A 不記 FAIL**）——那是**唯一能讓失敗不算失敗**的旗標，所以 `--dry-run` 每次都印出
有幾題帶著它。

### 多年語料**升生產**：`mdna` → `multiyear`，並翻開期間意圖 LLM

決策的兩半證據（收益／損害）都補齊之後才做。**一起做的四件事**（換 collection 與翻 A
**不可分兩次**：多年語料上線而 A 沒開，當期題會退步——收益探針 control 臂 4/4 → 3/4）：
`COLLECTION_NAME` → `us_stock_rag_edgar_multiyear`（21 → 77 份、13,022 chunks）／
`RQ_PERIOD_INTENT_LLM` 預設翻開／四題萬用字元 gold **釘死**／文件同步。

**驗收（零噪音，兩輪）**：單年 8/0/0、8/0/0；多年 **7/1/0**、8/0/0。
**r1 的那個 FAIL 不是升生產造成的**，三個獨立證據：① 那題的頭條數字在**兩個 collection 上都在跳**
② 它引的五個 chunk **全部在單年 KB 裡** ③ 這條主張的註記早就記過同一個失效。
⚠ **只跑 r1 就收工的話**，這裡會寫成「升生產讓指標從 8/0 掉到 7/1」，然後花半天追一個不存在的干擾。

**新增的舊 filing 真的有在用**：多年 r1 引用的 217 個 chunk 有 **45 個（20.7%）**來自新增的舊年度
filing，涉及 **23/65 題**。
**閘門②從此才有判別力**：期碼字串比大小判反的配對 **0 → 31/385**（單年語料上它一直是測資不足）。
⚠ `verify_chunk_grounding` 的 `MSFT_10K_2024.html#158`（1/1009＝0.1%）**帶著上線**——修它要重跑
三小時 ingest，而升生產不需要重跑。BACKLOG 原本把它列成前置條件，那是寫錯的。

**兩個順手根除的靜默漂移**：`run_agentic_on_evalset.py` 的 `DEFAULT_COLLECTION` **寫死成已退役的
`..._period`** → 不帶 `--collection` 的每一次跑都跑在退役底座上，零警告；CLAUDE.md 寫著 agentic 的
RETRIEVAL 是 `20b`，碼上 08-14 就換成 `120b` 了。

### 多年語料的**收益**量尺與**生成端**驗證

新增 `eval/probe_historical_benefit.py`（檢索層）＋ `eval/check_historical_generation.py`（生成層），
都是零 LLM 判定。完整分析見 [`docs/EVAL.md`](docs/EVAL.md)〈多年語料：損害與收益兩半〉。

| | 單年 | 多年 |
|---|---|---|
| historical gold@5（檢索） | 0/18（**定義使然**） | **17/18**（兌現率 0.944） |
| historical 答對（生成） | **0/18** | **16/18** |
| historical 誠實承認（生成） | **18/18** | 2/18 |
| **拿別年份硬答且沒交代** | **0** | **0** |
| control（當期陰性對照） | 4/4 | 4/4 |

- **收益是語料買到的、不是修法買到的**（A 開關 historical 完全一樣）；**當期題的損害才是 A 擋掉的**。
- **擔心的那件事沒有發生**：檢索層那 33 席「同節別期」**沒有兌現成生成層的實害**。
  ⚠ 收益探針輸出裡原本那句「那是有引用、看起來很有根據的錯答」是**推論不是量測**，已就地更正。
- ⚠ **量尺被自己的資料推翻三次**，每次都是「答對了卻被判成危險態」：① 拿 `looks_like_refusal()`
  判誠實（那函式問的是「整份都不作答」還帶 150 字上限，而最典型的誠實答案是**長的**）
  ② 詞表漏英文與兩個中文措辭 ③ `literal` 是英文片語 → 拆成 `literal`（前提檢查）＋ `answer_literals`（答案比對）。
- ⚠ **最重要的副產品：值不存在 ≠ 事實不存在。** 兩題**在單年 KB 其實答得出來**——10-K 的補充表
  帶著**重述後**的值，而前提檢查比對的是原始值。兩題標 `void`。**抓到它們的不是前提檢查，是生成端
  那一輪** → 新增收益題之後兩支都要跑。

**造題階段就被自己的前提檢查擋下兩次**：① BACKLOG 原本舉的例子錯了（10-K 損益表自帶三年、MD&A
自帶兩年 → **收益區從 T-3 才開始**，拿 T-1／T-2 造題會憑空灌水）② 兩個候選題**兩個 collection
都找不到** → 語料裡根本沒有的東西，不算收益。

順帶修：`looks_like_refusal` 的長度閘把證據尾巴數進去 → 拒答**少算 8 筆（19%）**。「證據尾巴」的
正式定義收攏成 `rq.strip_evidence_tail()`（repo 裡原本有四份）。

### 修掉「Tier 1 命中 ≠ 答得了」：label-year 那半個 OR 會冒充財年命中

收益探針在 `bh-07` 抓到、**回頭在生產 collection 複現**的缺陷。掃 77 份 filing：**10-K 的
`fiscal_year` 與 `report_label_year` 永遠相等（21/21），會分歧的只有 10-Q（15/56）** → 那半個 OR
的**全部效果**就是放行「曆年標籤是 V、財年不是 V」的季報，而當它是 Tier 1 唯一的命中理由時，
**Tier 2 的降級與揭露語會一起被關掉**。

生產實測：問「Microsoft 在 2025 財年的營收」→ top-5 **五席全是 `MSFT_10Q_202512`**、note 空字串，
含 FY2025 三年欄的年報被擋在外面。**這題當時就是錯的**，只是 65 題裡沒有一題是這個形狀。

**修法**：`rq.tier1_hit_is_qualified()` —— **label-year 只能放寬命中，不能自己構成命中**。
⚠ `fiscal_year` 為空的點（News/Fundamentals）**算合格**，否則是誤殺。
**blast radius 可枚舉**：生產語料上只有兩組 `(ticker, year)`；**65 題一題都不受影響**——
**那正是它從沒被抓到的原因**。
**留下的代價**：曆年語意的年份查詢也降級到 Tier 2，內容不變、多一句揭露語。順手修掉那句話的
**自相矛盾**（「沒有期間 2025…改用最接近的（…2025）」→ 改講「所詢問**財年**」）。
⚠ 這句同時是使用者可見句與注入 generator 的事實。

**閘門⑥（39 → 53 項）**，判別力在 **⑥b**（掃真實 payload 鎖住「10-K 恆等」與「至少一份 10-Q 不等」
——⑥a 全是合成 payload，ingest 一改就會集體失去意義）。

### `anchored_pct` 量尺第六次失效：切片把百分比 token 剖半

`mix-09` 判 N/A 而答案是**對的**。根因：舊寫法**先切 seg 再找百分比**，切口會把 token 剖半——
往右切掉 `%` 就漏抓，往左切進數字中間更糟（`122%` 被讀成 `22%`，**憑空生出一個不存在的值**）。
改成**先在全文找完所有百分比，再用「數字起點是否落在 ±window 內」過濾**，窗口語意一字不變。
⚠ **第一版的回歸鎖是假的**：直接抄原句當測資，**舊碼也會過**（真實答案用的是窄空格 U+202F，
手打成一般空格字元數就變了）。改成精確構造之後舊碼才在兩條上 FAIL。

---

## 2026-08-26 — 多年語料壓測階段 4：修法定案

**四條修法定案：A 已實作（翻轉條件＝升生產同一次）、B 預設開、C 不做、D 不做。**

**開跑前先發現量測基礎不能用，兩個獨立原因**：① collection 換過版（壓測跑在含新聞的 13,120 點上，
現在是零新聞的 13,022 點；用存下的候選池重算，**當時 top-5 有 29 席是 News**）② 題庫分母從
**49 變 45**。舊資料已限縮到 45 題共同集重新聚合當作合法 before——**分母變動不是改善**。

**C 不做的理由是可證的，不是判斷**：`k2_newest` 殘留的 48 席排擠，離線重算與記錄**完全吻合**，
而組內相異 filing 數的分佈是 `{2: 48}`——每一席都是 `keep=2` **刻意允許**的第二席，
**沒有任何一組 >2 ＝ collapse 零漏抓**。再用 `crowding_seats` 追 C 就是「收益指標與規則同定義」
那個套套邏輯。

**bug：`probe_temporal_interference.py --trend` 從來沒寫出過 JSON**（`cmd_trend` 印完表就
`NameError`，是早期兩臂版本的殘留）。也就是說 08-19 的趨勢結論是從 stdout 讀的、沒有落盤證據。
修完重跑，數字逐格相同。

**README 的 Qdrant 啟動段與實況四點全不符**（容器名、image 沒釘版、bind mount、缺 restart policy），
已更正並補上「容器起來 ≠ 可以連」——恢復 shard 期間 client 會拿到 `RemoteProtocolError`，
**那不是壞掉是還沒好**。

---

## 2026-08-25 — ratio 意圖交給 LLM（詞表退位成 fallback）＋ 生成端禁止四捨五入

**兩個改動獨立，但都動到 prompt，所以跑分基準一起搬走；驗收判準寫在跑 65 題之前。**

**① ratio 意圖：`_RATIO_INTENT_RE` 詞表 → LLM 判定。**
`_ensure_ratio_source_coverage` **整個機制**掛在一條正則後面。實測 Planner 把子問題寫成
「微軟的雲端服務最近成長得快不快？」時，詞表為 False → 補撈**根本沒被執行**，看起來像隨機退步，
其實是觸發面有洞。
改法：`_classify_ratio_fields()` 在 `_node_plan` 一次 call 判完整批子問題。**LLM 只被允許從封閉
欄位集合裡挑**——「想知道哪個量」交 LLM、「那個量叫什麼欄位」是封閉集合。
⚠ **為什麼不擴 `_PLANNER_PROMPT`**：planner 的輸出格式一改，**子問題拆解本身就會漂 → 65 題每一題
的檢索池跟著變**，等於把被測項和基準一起搬走。多付一次輕量 call 換 planner prompt 逐字不變。
⚠ **fallback（解析失敗 → 退回詞表）會遮住 LLM 的失手**，端到端跑分看不出差別 → 準確度只能直接量
（`probe_ratio_intent.py`，3 輪全穩 13/14：口語臂 3/4 而**詞表 0/4**、陰性 6/6 零浪費）。
**閘門⑫（14 項，124 → 138）**。⚠ 判別力**不在口語陽性那條，在「LLM 說空」那幾條**：把覆寫寫成
`if fields:`（而非 `if fields is not None:`）會讓空 list 掉回詞表 → **詞表仍然是實際做決定的人，
而端到端跑分完全看不出差別**。
⚠ 陽性那句**逐字取自實測的子問題**：第一版我自己改寫措辭，含「營收成長」→ 詞表認得 → 當場 FAIL。
**那是量尺錯不是系統壞。**

**② 生成端禁止四捨五入**（`rq.SYSTEM_PROMPT` Rule 8 加 NUMERIC FIDELITY）。
動機：答案**引了** `MSFT_Fundamentals #0`、眼前就是 `18.30%`，卻寫成「約 18% 左右」——引用是真的、
數字看起來也對，**兩個口徑就這樣消失了**。
**加在 Rule 8 而不是新開 Rule 14**：Rule 8 本來就是「每個數字都要能追溯」，而捨入正是那條追溯性
的失效方式。⚠ **這條動的是每一題的基準**，跑分不可跨這一天直接比。
新量尺 `check_rounding_fidelity.py`。⚠ **第一版判準太粗，錯的方向是漏抓**：整篇 contexts 比對時
「約 18%」被同一題 10-K 的「increased 18%」放行——**有來源，但不是那一句掛的那個來源**。改成
**逐引用**歸屬之後，真陽性抓到、粗版的兩筆誤報同時消失（**靈敏度與誤報率同方向改善**）。
⚠ **統計效力很低**：改動前三個封存檔的發生率是 0／0／1 題（共 195 題）。

**跑完之後逐條對回預先寫死的判準**：主判準（零噪音）**兩輪都 PASS 8／FAIL 0**，`lex-17` 那兩條
**在三個基準輪裡 0/3** 的主張轉成 **2/2 PASS**；次判準 r1=0、r2=1 筆，**分不開**；
RAGAS 五正一平、四項超過門檻。
**歸因：改善來自檢索／意圖那半，不是生成端規則**——`require_chunk` 從 1/3 → 2/2 ＝ 值先進得了池，
後面兩條主張才有東西可引。而生成端**照樣捨**。
⚠ **同一份答案同時做對和做錯**：條列裡有正確的 `18.30%（TTM）`，開場句卻是約值。`require_text`
掃全篇看不出這件事——**兩個量尺各自都對，合起來會讓人以為捨入被修好了**。

---

## 2026-08-21 — lex-17 的兩個真因都在「我修的那一層下面」；after 臂是 null result

**預先寫死的判準沒過**：三條主張要**同時**轉 PASS 才算有效，實際 **PASS 5／FAIL 3，與 before 臂逐條
相同**，且 `require_chunk` 的訊息從「同檔撈到 #1」變成「該檔完全沒被撈到」＝**更壞**。
→ 照判準記為 **null result**，不算部分成功。
⚠ 一個很好聽但錯的解讀隨手可得：「Fundamentals 從 #1 變成不出現，是修法正確地拒絕了零比率的
chunk，所以其實是進步。」前半句是真的，後半句不是——使用者拿到的答案沒有變好。
**能自圓其說的敘事和有效的修法，在數字上長得一樣。**

**真因 A：保底掃的池，本來就沒有那個 chunk。** 生產組態是英譯 query，實測
`MSFT_Fundamentals #0` **連 RRF 的 20 個候選都沒進**（中文原句反而撈得到，rank 7）。
名字叫「保底」，實作卻是「希望它剛好在池裡」。
→ 新增 `_fetch_fundamentals_with_field()`：池裡沒有時**直接查 Qdrant**。「哪個 chunk 含這個欄位」
有唯一正確答案 → 交給 Python，不靠相似度。⚠ 分數用 cross-encoder **真的重算**，不塞常數
（那個數字會印給使用者看）。⚠ 沒有動 `RRF_TOP_N_PRIMARY`——不為一題改全域召回。

**真因 B：validator 讀的欄位，生產從來沒供給過。** `_basis_disclosure_notice` 判
`chunk["period_basis"]`，而 `rq.retrieve()` **沒把這個欄位放進 chunk dict** → validator 在線上
**結構性永遠不觸發**。而閘門⑪ 全綠，是因為那 8 條斷言都拿測試自己造的 dict 餵進去。
**量尺與被測物耦合，同型第六次，而且是最難自己發現的一種形狀。**
→ payload→chunk 的建構抽成 `rq._payload_to_chunk()`（唯一建構點），補上該欄位。

**量尺跟著修**（89 → **117** 項）：⑩b 確定性補撈（含「欄位不存在 → 回 None，**不可退而求其次**」
與「分數必須真的算出來」兩條誤報對照）、⑪b **拿真實 payload 餵生產建構子**。
**變異測試三發全中且判別力落在對的斷言上**：建構子不帶 `period_basis` → ⑪ 那 8 條**照樣全 PASS**、
只有 ⑪b 叫。

---

## 2026-08-20

### live web 取代 KB 新聞：②內容這一半量完——可信，但抓到一個 live 專屬的引用缺陷

錄 `eval/web_fixture_news37.json`（37 題 ＋ 3 題陰性對照、**86 筆 Tavily 原始回應**、as-of 08-20）
＋ `web_claims_news37.json`（40 條性質斷言）。⚠ **驗收不對照新聞 gold**：那批 gold 是「6 月的新聞
說了什麼」，live web 回答「現在的網路說什麼」——**是兩個不同的問題**。
**結果 PASS 35／FAIL 5**：觸發 web 35/37、引用 web 33/37、引用主機 100% 在白名單、
**地區子網域 0 次**、陰性對照 0/3。

**5 個 FAIL 全是同一個 live 專屬缺陷**：答案含 `【Reference 7, chunk #15】`——不是檔名、不是網址。
⚠ 不是「多一個壞引用」——**那 5 題的有效 KB 引用是 0**，整份答案沒有任何可追溯的出處，
答案裡卻有大量財報數字。snapshot 路徑 **0/100**。
舊行為：validator 判它捏造 → 丟回重寫 → 重試上限用完 → **原樣 return**。而該處 trace 寫「走機械式
收尾」，**程式裡從來沒有**——**註解描述了一個不存在的行為，比沒有註解更糟**。

**修法**：`Reference N` 的編號**是我們自己編的**，所以 `N → allowed_chunks[N-1]` 是**確定性映射**，
屬於 Python 那一半。**兩個守門條件才是重點**：①序號越界 → 不動 ②`chunk #M` 與第 N 筆不一致 → 不動。
⚠ 危險方向**不是漏修**（漏修＝維持現狀），而是**把「無法追溯」變成「看起來可追溯的錯引用」**。
條件② 另有一個副作用：**它讓「排序假設」不必被證明**——兩半一致時那筆修補是被資料自己確認過的。

**閘門⑨ 12 項**（77 → 89）。三向變異：機制恆不作用 → 7 條 FAIL；拿掉一致性守門 → **誤報對照精準
FAIL 1 條**；拿掉範圍守門 → 當場 IndexError。⚠ 變異③ 順帶證實 `Reference 0` 是三條裡最危險的：
Python 的 `[-1]` 是合法索引，少了守門它會**安靜地**指到最後一筆。**會吵的那種比較安全。**
⚠ 5 題裡只有 2 題模型仍吐序號引用（兩題都完整還原、守門擋下 0 筆），**另外 3 題這一輪沒吐，
它們的 PASS 不算在修法頭上**。
⚠ **閘門⑨ 的端到端斷言第一版讀 `experiments/`**，把修好的 replay 併回去就 `dirty=0` 而 FAIL——
量尺與被測物耦合，**同一天第三次同型事故**。已改成把修法前的 20 筆逐字凍結進測試檔。

### 量尺重建（65 題）：兩組常數是分開失效的，也要分開修好

題庫 100 → 65 讓 `GOLD_BASELINE` 與 `NOISE` 同時失效。⚠ **gold 重量完就宣告「量尺修好了」是最
自然、也最危險的講法**——那會讓下一個人拿一個沒有效的噪音門檻去判斷顯著性。
新 gold 上限（n=65）：correctness **.972**（舊 .989）／recall .788／precision .843／nv .969／
faith .659／relevancy .871。**最大的變化是 correctness——gold 在 65 題上沒那麼容易拿滿分**，
所以「距上限還有多少」在新舊之間不可直接比。
噪音**新測到的值沒有直接採用**，改**取新舊較大值**：`context_precision` 新測 .005 看起來小了十倍，
但舊值 .046 的成因寫在碼上（最高排名那個判定翻面整題就 1.0→0.0），這次剛好沒翻不代表它不會翻。
**門檻取大只會要求更多證據，錯的方向是安全的那一邊。**

---

## 2026-08-19

### KB 拔除新聞：只留「記錄」，把「流」交給 live web

**資料層**：`News` 加進 `RAW_EXCLUDE_DIRS`（**整條規則的單一開關**）；`fetch_data.py` 的
`--skip-news` → **`--with-news`（預設不抓）**；兩個 collection 各刪 98 個 news chunk
（**歷史對照臂刻意不動**）。
**程式層**：移除 `doc_type=news` 硬 filter 注入、news 題的 ticker 改寫、相關守衛。
`looks_like_news_query` 保留但**退出所有生產判斷**。Planner prompt 拿掉「市場事件可拆 news 子問題」
的例外。`find_authority_conflicts`（R3）與 `_news_freshness_gaps` 標記為**永久閒置但刻意保留**
——它們治的病是「KB 新聞數字壓過財報」，病源消失，**閒置是對的不是退化**。

**成效**：複合題的 top-5 從「5 個 news chunk、filing gold 不在候選池」變成 filing gold 排名 1、2。
這類題是拔除前**最大的殘留檢索失敗**。

**量尺三個變動（⚠ 跨今日的分數一律不可直接比）**：題庫 100 → 63（37 題冷凍，**冷凍不是刪除**）；
`check_number_defects` 6 → 4 條（⚠ **不是改善是組成變動**，搬走的兩條一 PASS 一 FAIL）；
閘門④ 一度 FAIL 3 項——**那是量尺失去判別力不是機制壞掉**，改成**自帶合成 coverage**（不刪斷言），
從此不與 KB 內容耦合，**比原本更好**。

### 修：財報被誤用來證明「候選池夠新」

確認「證據後置」設計後的第一步是**量 Grader 有沒有照設計運作**（零 LLM、真實檢索池）。
結果 6 個即時類問題**有 4 個時效改判不觸發**，含 intraday 股價題。
**根因**：`_source_newest_date` 把 10-K 的 4 碼財年戳算成該年 12/31（`MSFT_10K_2026` → **未來日期**），
而 `_stale_for_realtime` 取全池 `max(dates)` → **財報不只是棄權，是替整個池子背書說夠新**。
⚠ **4 題裡只有 1 題是拔除新聞造成的**，另 3 題**從來沒被詞表攔過 ＝ 既有的洞，只是一直沒人量**。
⚠ **實際代價比第一版宣稱的小，已更正**：那 4 題**在舊碼下 web 一樣會被叫**（Grader 自己就判不足）。
我第一版把「時效改判沒觸發」報成「web 不會被叫」，那是兩件事。真正的代價是 `kb_unfixable` 恆為
False → 白燒改寫輪，以及**第二道防線被靜默關閉**。
**修法**：`_is_freshness_evidence()`——只有 8 碼真實日曆日期算證據，4/6 碼財報期間**不算證據也不算
過期**。`_kb_ceiling_date` 套用同一套資格判準。
⚠ **這推翻了一個先前刻意的成本判斷**（「誤判新鮮只是維持現狀」）——那句話在 KB 有新聞時成立，
KB 只剩財報後「維持現狀」＝拿 10-K 回答今天股價。
**閘門⑤ 新增回歸鎖**（未來日期的 10-K 不得蓋過真實日期來源——**這條在新舊實作之間有判別力**）
**與誤報對照**（池裡有 1 天前的來源 → 不判過期，沒有它「一律判過期」也會全綠）。
同時新增 `eval/probe_realtime_need.py`（LLM 側唯一量尺）：8 題 × 3 輪，**8/8 正確、零抖動**。
其中一題**不含任何新聞字樣**——那正是被拆掉的詞表會漏的形狀，LLM 判準接住了。
⚠ 也因此得知：**主防線是 Grader 的 LLM 判斷，時效改判是第二道**，而第二道的價值無法在「LLM 判對」
的題上量到。

### 時效警語一度整個死掉（同日補回）

拔除新聞時把 `_news_freshness_gaps` 標成「閒置但保留」，**低估了後果**——它是缺口的唯一來源，
於是警語永遠回空字串。實測：即時題 → 打 web → **預算用完或搜不到** → 用財報生成 → **零揭露**。
新增 `_unmet_realtime_gaps`，判準改成「子問題需要即時資料」。**閘門⑤b 10 條 ＋ 變異測試**：
打斷機制 → 7 條乾淨 FAIL。⚠ 含「財報題 → 零缺口」的**陰性對照**——少了它，警語會印在每道財報題底下。

### 檢索層兩條期別修法：跨期 field collapsing（B）＋ 期間意圖解析（A）

**B**（`_collapse_cross_period_sections`，**翻預設為開**）：同一 `(ticker, filing_type, item_id)`
最多讓 `keep=2` 份 filing 佔位，組內留**財年最新**的。是搜尋引擎的標準原語（Solr `CollapsingQParser`／
ES `collapse`），**不是 MMR**——MMR 靠 embedding 相似度且帶 λ 超參數，而本專案量尺已飽和、
**沒有能調 λ 的尺**。
敢翻預設的理由：**單年上逐題完全 no-op（0/49 題有變化）**，多年上改 23/49 題。
**資料還不需要時不作用、需要時自己生效。**
順手補了 `retrieve()` 回傳 chunk 的四個欄位——**這個缺口先前逼三個下游各自再掃一次 Qdrant**。

**A**（`resolve_period_intent` ＋ `ladder_pick`，當時預設仍關）：「這題問的是哪個期間」交 LLM，
「那是哪個期碼」交 Python 從 ladder 算。
**ladder 用 payload 的 `(fiscal_year, fiscal_period)` 排序，不用期碼字串比大小**：實測 385 個配對
有 **31 對（8.1%）判反**。
**`period_ref=range` 反過來保護 B**：趨勢題整個跳過 collapse——這是 collapse 那個損害唯一的正解。

**成效（多年，49 題）**：gold@5 0.837 → **0.898**、錯期率 0.082 → **0.041**、排擠率 0.278 → **0.188**；
`explicit` 類錯期率 0.333 → **0**。逐題**變好 3 題、變差 0 題**。
**新增閘門** `verify_cross_period_collapse` 17/17、`verify_period_intent_routing` 39/39。
後者的閘門② 帶自我檢查：印出「期碼字串比大小判反幾對」，一對都沒有就明說**那是測資不足不是系統健康**。
**新增陰性對照** `period_probe_trend_queries.json` 8 題——eval_set 裡**沒有任何一題**是這個形狀。

### 多年語料壓力測試：KB 21 → 77 份 filing

往回補兩年灌進獨立的 `us_stock_rag_edgar_multiyear`，**生產 collection 一個位元組都沒動**。
新增 `probe_temporal_interference.py`（三個**互相獨立**的指標＋兩臂，分組鍵用 payload 的 `item_id`
所以「同一節、不同年份」是確定性認出來的）＋ `period_probe_baseline.json`（灌資料前的凍結快照
——eval_set 有 4 題 gold 是萬用字元，照當下 manifest 展開會讓它們**撈到哪一年都算命中**）。
`fetch_data.py` 加 `--annuals N` 與 **append-only 跳過**（實測既有 21 份 md5 全數未變）。

**結果**：① **競爭密度上升最多的那一類完全沒退步**（`relative` 兄弟密度 1.90→7.52，指標一格沒動
——硬 filter 全吸收）② **硬 filter 的價值放大約 10 倍**（關掉路由，單年只值 0.048 錯期率，多年是
**0.476**）→ 那條路由從「可有可無的優化」變成「不能拔的命脈」，而它**靠硬編碼詞表觸發**
③ **真正壞掉的是「問題沒提期間」那 25 題**（排擠率 0.080 → **0.480**）④ **F1 遠大於 F2**
（68 個浪費席位 vs 4 題錯期）→ 病灶是**多樣性不是期別選擇**。

### 量尺補回兩條（`lex-16`／`lex-17`）；`mi-05` 的缺陷**沒有**被拔除新聞修好

把搬走的兩題**去掉新聞子句、只留財報半**補回母檔（63 → **65 題**），讓新聞子句成為唯一變因。
**結果：沒有修好。** 兩次端到端 r1 PASS／r2 FAIL，r2 逐字就是原症狀。
⚠ **我一度只憑 r1 全 PASS 就宣告「根因是新聞污染」並改了三份文件**，r2 立刻打臉；而該缺陷自己的
記載早就寫過「部分取決於 Plan 產生的子問題」。→ **這些主張的判讀一律要跑 ≥2 輪**（同一份
collection：r1 PASS 7／FAIL 0，r2 PASS 4／FAIL 3）。
**真的被拔除新聞修好的是 `mix-07`**（2/2 從拒答翻成答出 $29.5B）——**那是拔除新聞唯一一個零噪音
量到的直接收益**。
順帶修掉兩個量尺缺口（都是 r2 逼出來的）：`col-11` 的 anchor 措辭寫死（**第五次失效**，且與前四次
不同——**護欄安靜地停止量測**）；兩條主張補 `expect_text` 當第二判準。
⚠ `lex-17` **刻意不加 forbid**：兩個口徑並陳並標注清楚是好答案。

### 新增 R4：web ↔ 財報數值衝突要求「並陳」而不是「裁決」

`_ground_source_type` 現在也吃 `web_extra`（型別 `"web"`，**刻意不列入權威來源**）。順帶修掉
`if not chunks: return` 這個洞——「候選池空、只有 web」**正是 live 路徑的常見形狀**。
⚠ **刻意不擴充 R3**：R3 判「一邊為錯」，套到 web 會讓系統**系統性報舊數字**（web 可以合法地比
filing 新）。R3 的舊 docstring 已寫下「把 web 併進去會改變 R3 的語意」——那句是對的，只是結論該是
「新增一條動作不同的規則」。
⚠ R4 的動作在誤報下**也正確**（要求把兩個值連同時點講清楚，對不同期間的兩個值本來就對），
所以會出事的方向是**話太多**不是漏抓。

### 新增 `eval/probe_news_web_routing.py`：先做便宜的那一半

「live web 能不能取代 KB 新聞」拆成 ①**會不會**打 web（只燒 LLM）與 ②打回來**夠不夠**（連網燒
Tavily）。**① 是 ② 的必要條件**。零網路的作法是把 `_tavily_search` 換成計數樁、其餘管線原封不動。
**結果：路由不是瓶頸**（37 題觸發 web ≥35/37，其中 multi_intent **15/15**——最危險的形狀
「財報半讓 Grader 判夠而整題不打 web」沒有發生；strict 陰性對照 0/6）。
⚠ **陰性對照修正**：「現在市值／現在股價」**不可以**當陰性對照——同一件事在兩支腳本裡有相反的
期望。已拆成 `CONTROL_STRICT`／`CONTROL_LOOSE`。
⚠ **57% 打滿 web 預算這個數字是樁造成的，不可外推**；它只能支撐「② 的成本上界」。

---

## 2026-08-18 — Groq 讓 `llama-3.3-70b-versatile` 退役 → 表格摘要換 `openai/gpt-oss-20b`

**怎麼發現的**：不是稽核抓到的，是**重建當下 log 裡每張表都在 404**。`_llm_summarize_table` 對非
429 錯誤是 `print WARN` 後 `return ""` → **整條表格摘要路靜默歸零**，正是
`verify_table_captions.py` 的 `missing_on_big` 閘門設計要抓的那種降級。

⚠ **Groq 官方建議的替代品 `gpt-oss-120b` 正是本專案 2026-08-11 測過並否決的模型**，但當初的否決
理由是「reasoning token 吃光 completion 額度 → content 空字串」——那是**預算問題不是能力問題**，
可以驗。bake-off 刻意把「重現既有失敗」放進矩陣：

| 組態 | 空/錯 | 重複全等 | 平均字元 |
|---|---|---|---|
| `gpt-oss-120b` @200 | **10/10 全空** | — | 0 |
| `gpt-oss-120b` @700 | 0/42 | 6/14 | 150 |
| **`gpt-oss-20b` @700 `effort=low`** | 0/42 | **12/14** | 164 |
| `qwen/qwen3.6-27b` @700 | 0/10 | 5/5 | 2709（`<think>` 直接吐進 content，不可用） |

@200 全空**完美重現既有記載** → 根因確認是 completion 預算，`TABLE_SUMMARY_MAX_TOKENS` 200 → 700。
**選 20b 的判準是輸出穩定性，不是模型大小**——因為 caption 變異是「重建不會逐字重現」的成因之一。
⚠ **第一輪 bake-off 的結論不可信、被自己推翻**：取樣排序後 5 張全落在 exhibit index（垃圾表區），
不代表真正會走 LLM 這條路的表。round 2 改成跨 7 家 × 兩種 form 的 14 張**財務**表。

---

## 2026-08-15

### 數字溯源稽核（`find_untraceable_numbers`）：一道**沒有實測正例**的防線

答案裡「不可能被換算」形態的數字（兩位小數的報價／市值），必須在 chunk 全文或 `web_notes` 裡溯得
回來源。零 LLM 偵測，抓到才花一次重生成。**放在 reflect 之後**——那是驗證鏈唯一沒人看守的位置。
⚠ **這項沒有實測正例**，陽性靠變異注入；價值在那組**誤報對照**（億／兆換算、四捨五入、千分位差異），
靠它們才有 100 題乾跑誤報 0 題。
⚠ 宣稱詞辨識那組是回歸鎖：第一版詞表窮舉措辭，**同一次迭代內就漏掉自己重生成寫出的措辭**。

### 期別稽核：答案自稱「最新一季」卻引用了較舊的期別

**一次誤診值得先寫下來**：第一份根因分析每一步都對，結論卻錯（把一個真的機制問題當成這題的成因）。
把 coverage 印出來才發現 KB 一點都不缺，按那個修法會把一題 KB 明明答得出來的問題送上網。
**先把要判斷的東西印出來，再猜成因。**

**真正的缺陷**：某公司最新一季**沒有獨立的 10-Q**（被包進 10-K），而系統把「最新一季」解析成
「候選裡最新的那份 **10-Q**」而不是「證據集合裡最新的**期別**」。

**三個設計決定**：① **判準看答案不看問題**（答案寫出「最新一季」時它就是在做一個宣稱，而「你引的
是不是證據裡最新的一期」是純比對）② **排序不能用 `_source_newest_date()`**（NVDA 財年 1 月底結束，
`10K_2026` 其實比 `10Q_202604`(FY2027 Q1) 舊）③ **每家要比兩輪**（只比「全期別最新」會漏，而且是
**修好第一輪之後重生成的答案自己暴露的**）。
⚠ **宣稱詞辨識：窮舉清單在同一次迭代內就漏了兩個**——修正後重生成的答案寫的是「最新**單季**」與
「最新公布的**季報**」，**validator 對自己造成的新問題視而不見**。改成「錨詞＋5 字內期別詞，中間
不許有數字」。**詞表能用的前提是「錨詞 + 結構」而不是「列舉實例」。**

### live web 的可重現評測：把不確定性切開，而不是硬定 gold

新增 `web_replay.py`（Tavily 原始回應的錄／放）＋ `eval/web_claims.json` 逐條斷言。
**切法**：確定性的部分（白名單／去重／日期算術／預算）已由零 LLM 閘門蓋住，**不重測**；
fixture 的價值在**解鎖含 LLM 的端到端路徑**。
**四個設計上的坑**（每一個都會讓 fixture 失效而不自知）：① 必須錄**原始回應**不是處理後的字串
（否則等於把要測的六道一起 mock 掉）② `get()`／`put()` **兩邊都要 deepcopy**（少了 `get()` 那邊
更嚴重：兩次重放走不同程式路徑）③ 只錄 web 不夠，要連 `llm_replay` 一起錄 ④ **斷言不能寫死成
某一次錄製的答案**——fixture 是一個**值空間**。
**驗收**：兩次獨立取樣都 5/5 PASS，注入六種變異 **6/6 全被抓到**。
⚠ **「斷言零噪音」不等於「結果零噪音」**：`5/5 PASS` 的正確讀法是「**這一次取樣**沒問題」。
⚠ **而且斷言的 FAIL 也可能是量尺錯**：某次 FAIL 說「$196.82 是憑空生成」——**是誤報**，
fixture 裡有 `196.8165`，答案是**正確地四捨五入**。我拿那個 FAIL 當真陽性，寫了一整條 BACKLOG、
一段 CHANGELOG、一節 docs 才發現。→ **「稽核回 0 筆先當壞消息查」需要一個鏡像條款：稽核回報
FAIL，也要先問「是不是量尺錯」。**

### 兩個被自己的探針證偽的假設

① 「Grader 前確定性去重」——**沒有東西可以去重**（近重複 13/341＝3.8%、完全相同 chunk id **0**）。
② 「Grader 保留率 49.3% ＝ 資訊瓶頸」——**它砍的是冗餘不是 gold**（檔案層命中 95/100 vs 單發
97/100）。⚠ 但檔案層量尺已飽和，**不能推論 chunk 層也無損失**——真缺口是 lexical 的 chunk 層
（agentic 平均只承接 1.93 個 chunk，全類最低），見 BACKLOG。

---

## 2026-08-14

### 檢索側 LLM 換模型（20b vs 120b）：候選會動，gold 不動

新增 `eval/ablate_retrieval_model.py`（兩 stage：錄下 query understanding 輸出 → 釘死送進真實
檢索器，stage 2 **零 LLM**）。
**結論**：候選池確實被換掉（Jaccard 0.897 < 噪音 0.947），但**換掉的全是無關 chunk**
——gold recall 差 0.0020、噪音 0.0014。**兩個模型在檢索側等價。**
**但方向是反的**：20b 在 NIM 上**慢一倍**（3.57s vs 1.76s），「用小模型求快」前提為假。
**三條方法論**：① **字串比對對翻譯沒有判別力**（86/100 跨模型不同，但 68 題是同模型跑兩次就不同）
② `parse_query_filters` 有 **91/100 題被效率 gate 擋在 LLM 之前**，不先算這個會把「gate 擋掉」
誤讀成「模型一樣好」③ A/B 兩臂的 LLM 輸出要先錄下再釘死。

### 多公司期別 probe：損害是真的，但期別 filter 修不到——病灶是席位競爭

新增 `eval/probe_multi_company_period.py`（零 LLM、三臂、8 題人工構造題）。

| arm | 錯期率 | 缺最新季的公司 | 缺失率 |
|---|---|---|---|
| `single_on`（陰性對照） | 0.000 | 0/16 | 0.000 |
| `single_off`（**陽性對照**） | 0.577 | 1/16 | 0.062 |
| `multi`（被測項） | 0.429 | **6/17** | **0.353** |

**損害成立，但機制不是期別**：6 件損害逐件拆開，**kind A（同家舊季擠掉新季）＝0、kind B（那家公司
連一個 10-Q 都沒進 top-5）＝6** → OR-of-ANDs 直接修不到任何一件。
**對照組解釋了為什麼單公司題看不出來**：`single_off` 錯期率 0.577 卻只有 6.2% 缺失（5 格全給一家，
留得住）；`multi` 錯期率較低反而 35.3% 缺失。**席位稀釋放大 5.7 倍。**
**損害範圍只在單發管線**：agentic 的 Planner 把 8 題拆成 17 個子問題、**17/17 只剩一家公司**
→ 這個破口在 agentic 上結構上不存在，是 **Planner 在上游消掉的，不是 Grader 補救的**。
而 `api_server.py` 直接呼叫 `rq.retrieve()` → **使用者實際在用的 web UI 就是有損害的那條路。**
**兩條方法論**：① 腳本第一版只看聚合 `missing_rate` → 吐出「值得做 OR-of-ANDs」，**是錯的**。
**成因分類要寫進量尺本身，不能靠事後人工看。** ② **量一個破口之前先確認它在哪條管線上發作。**

### web 這條路的九個缺陷：主因是自己把摘要截掉

| # | 缺陷 | 修法 |
|---|---|---|
| D1 | **`content[:300]` 把數字截掉**（主因） | `WEB_CONTENT_CHARS=1200` ＋ `search_depth="advanced"` |
| D2 | 單一域名壟斷全部名額 | `TAVILY_PER_DOMAIN_CAP=2`，先撈 12 則再篩 |
| D3 | 無日期 → 六年前的文章與今天並列 | 抽網址日期、濾過時、**日期標進 prompt** |
| D4 | **web 呼叫次數無上界**（實跑 7 次） | `QUERY_WEB_BUDGET`（query 級）＋ `kb_unfixable` 提早跳出 |
| D5 | 同頁多變體各佔一個名額 | `_normalize_url` 去重 |
| D6 | 地區子網域＝**別的市場的報價** | `_host_allowed`：本地端只認 exact ＋ `www.` |

**D1 的決定性證據**：trace 印出的摘要是 `Apple market cap as of Augus`，完整原文是
`as of August 07, 2026 is $4572.79B`。數字排在站台樣板文字後面，**300 字正好切在數字前一個字**。
**「撈到了卻截掉」與「根本沒撈到」在舊 trace 裡長得一模一樣。**
**D4 的根因不是 replanner 失控，是計數器放錯層**（記在每個子問題會歸零的 state 上）。
**Tavily 能力實測**：只有 `topic="news"` 回發布日且日期過濾真的生效，但 news 模式**拿不到數據頁**
——而即時報價題要的正是數據頁。故走預設 topic ＋ 網址推日期。
**一個被自己推翻的修法**：用字串相似度認同義待辦，**實測分離度是負的**（正向最低 jaccard 0.04、
負向最高 0.50）→ **整段撤掉，改成不判語意、只封成本**。

**驗收（同一題）**：答案從「已達 $5 兆」變成「約 $4.57 兆」（並正確標出 $5T 是盤中高點）；
子問題 7 → 4、KB 檢索 21 → 4 次、web call 7 → 3 次。

**同日追加三個**：⑦ 修訂條款的排序規則**原本是反的**（對「當下數值」類問題，未標日期的行情頁才是
今天的值）→ 分岔並把 `WEB_STALE_DAYS["intraday"]` 90 → **7** ⑧ 時效警語與答案自相矛盾（缺口逐
**待辦**算、警語整**篇**只印一次）——**警語與答案互相矛盾比沒有警語更糟** ⑨ web 結果混進 OCC
選擇權合約頁（頁上的價格是**權利金不是股價**）→ 用標準格式排除，屬格式定義的封閉集合。
**第十個**：`kb_unfixable` 會誤殺——它只看**候選池**最新那筆，而**池子裡最新是 62 天前不代表
collection 沒有 3 天前的**。修法是引入與 query 無關的 `_kb_ceiling_date()`。判斷抽成
`_classify_staleness()`，**唯一理由是可測**。

### 單發 vs agentic 全量對照：整體「沒有顯著改善」，但那是抵銷出來的

兩臂同 collection、同 100 題、web 全關，n=100 且 0 NaN。
`nv_context_relevance` **+0.103**、`answer_relevancy` **+0.144** 超過噪音；其餘四項在噪音內
——**agentic 贏在檢索相關性，但不能宣稱它讓答案更正確**。
**真正的發現是分類別抵銷**：`context_recall` 整體只有 +0.055，是因為 multi_hop **+0.267**、
multi_intent **+0.184** 的領先被 lexical **−0.078**、semantic **−0.073** 抵銷。
⚠ 分類別 n=10~15、門檻約 0.17~0.21，**除 multi_hop 外個別都不顯著**；有證據力的是**跨兩個指標
一致的排序模式**。
同時燒掉三筆成本：① `--from-results` 吃多個檔是**拼接同一臂**（同 id 取平均）不是 A/B，兩臂丟一起
得到的平均正好落在歷史噪音帶、**看起來毫無異常——燒掉 2h49m** ② NaN 補完前差值剛好卡在門檻上
③ 背景跑分要 `python -u`。

---

## 2026-08-13

### live／web 這條路修通：三個阻塞點、來源白名單、Grader 時效判準

**起點**：生產模式四題時效題，web_search **0/4 觸發**。拆下去是三個獨立阻塞點，不是一個 bug。

| # | 阻塞點 | 修法 |
|---|---|---|
| ① | 兩道硬編碼詞表閘門擋在 web 補救判斷式上 | 都拿掉。18 個真實時效措辭實測**漏 10 個** |
| ② | `SYSTEM_PROMPT` Rule 1/2/8 讓 web 內容不可引用＝不可用 | 有 web 時才在 **system message** 附加放行條款 ＋ 補上 Generator 一直漏接的時間契約 |
| ③ | Grader 只問「有沒有這個欄位」不問「夠不夠新」 | `realtime_need` 三態（LLM 判）＋ 來源日期（Python 算），**只降不升** |

**② 的決定性證據**：接好管線後三次跑分**仍全數退回舊快照**，其中一次寧可拿舊市值除股數捏造
「每股 $204」——**違反「Never invent」只為守住「traceable to a cited chunk」。缺的不是格式，是許可**；
且修訂必須在 system message（user message 版本已實測無效）。
**③ 的證據**：「Apple 現在的本益比」**正規式是有過的**，`sufficient=True` 擋下 → 拿掉詞表只修一半。
coverage 知識反而把 Grader 推向判「夠」，故**刻意不改那段 prompt**，改在 Python 層改判。
**新增來源白名單**（原始揭露方 ＋ 有編輯流程的財經媒體）。**濾空明確回報查無、不退回全網。**
新增 `eval/verify_web_gate_isolation.py`（五道閘門 22 項，含兩項 **byte-identical** 斷言）。
**量測教訓**：八題各跑一次時有兩題看似明顯退步，**各補跑 2 次後兩個都被推翻**。

### `us_stock_rag_edgar_mdna` 升生產；切塊這條路確認到頂

小標層收窄到 `_MDNA_ITEMS` 白名單後重建（3925 chunks、零 429），三道 ingest 閘門全綠，
`check_number_defects` PASS 4／FAIL 2。semantic `context_recall` 0.497 → **0.593**。
⚠ 單類別 n=15 的誤差棒約 0.17，**+0.096 不足以宣稱效果成立**，只能說方向與預測機制一致。

**「改用最簡單的 collection」假設被同源對照證偽**：用**今天的 code ＋ 同一份 replay fixture**
重跑 `period` 全量，兩臂唯一差異只剩 collection → `period` 把**分部層級**的數字當成公司整體
（拼自三個 chunk），`mdna` 給出單一 chunk 的正確值。**小標層的價值由 `mix-03` 證實。**
⚠ **舊結果檔不是合法對照臂**：某個 08-08 的檔早於三個相關 commit 且當時沒有 replay cache，
用它比較會得到**三個假結論**，對齊 code 後全部翻掉。**以後拿舊結果檔當對照臂，一律先查 code drift。**

**量尺飽和**：六個指標**五個已達或超過 gold 上限**；按檢索成敗分桶，**全修好只值 +0.024，低於
0.067 噪音底線**。**切塊／ingest 這條路不要再改了。**

---

## 2026-08-12

### 幅度接地判準下移到 `_merge_small_chunks`（section 層修法被實測推翻）

**背景**：08-11 的幅度接地下在 **section 層**，重建後閘門判準⑤ 全綠（46→0），但 `mix-09` 只有
1/3 PASS，**還輸給完全沒有小標層的 `period`（2/3）**。
**真因**：section 層併好之後，**SemanticChunker 會再切開**——幅度表格留在前半、解釋句落在後半。
**修法**：判準下移到 `_merge_small_chunks`（跑在 SemanticChunker 之後，是**最後一個會改變邊界的
步驟**），並傳入 token 長度函式（呼叫端的 RCTS 補切在它之後，合併若推過門檻會被切回去）。
⚠ **section 層那一層保留，兩者互補不重複。**
⚠ **第一版單元測試是我自己寫壞的**——前一塊只給 65 字元，觸發了既有的「首塊過短往後併」，
與新判準無關；改用語料裡真實的 482 字元無數字 chunk 才是有效測試。

**新增 `eval/verify_chunk_grounding.py`**（chunk 層閘門）。含與 ingest 的**常數一致性斷言**
（讀原始碼文字、不 import）——常數兩份會漂移，漂移的話閘門會**安靜地量錯並回報全綠**。

**兩個必須記住的教訓**：
1. **修法要下在「最後一個會改變邊界」的層**，否則下游會把它切回去。
2. **驗收閘門要與缺陷同層。** 判準⑤ 量 section 層，所以它**結構上看不到** chunk 層——它全綠的同時
   缺陷還在。**「規則生效」與「缺陷消失」是兩個判準。**

### 全量 100 題 ＋ RAGAS：解開了 head-vs-period 的懸案

六個整體指標**全部落在噪音內**，但分類別有一個真訊號：semantic recall −0.190、correctness −0.110。
三臂對照證明**它不是幅度接地造成的，是通用小標層**（`head` 早就是 −0.178／−0.126）。
**這解開了 BACKLOG 的懸案**：**semantic 15 題就貢獻了整體 recall 缺口的 ~68%、correctness 缺口的
~59%**，機制是**小標層把 chunk 切小 → 每個撈到的 chunk 帶的證據變少**（semantic 的證據量 −31.6%，
chunk 數卻幾乎沒變）。
→ **下一個槓桿**：小標層的用途（分部/合併混淆）只存在於 MD&A，而 semantic 撈到的 chunk 有 **83%
來自非 MD&A**。把 `use_heading` 收窄到 `_MDNA_ITEMS` 應可同時保住兩者（08-13 已做）。

### mix-03 改用 `require_text`：`forbid` 對它結構上不安全

某輪答案**每個數字都對**（先給合併總計、再逐部門分解），卻因 `forbid_text` 命中分部值被判 FAIL。
**正確的分部分解必然包含分部的值**，所以 forbid 不是換值就好。改由「合併總計的**金額**在不在」
承擔判別力。跨 10 個 run 逐格比對：**只有那一格從 FAIL 翻成 PASS，其餘 9 格不變。**

---

## 2026-08-11

### 數字缺陷量尺加兩個斷言型別：`require_text`／`require_chunk`

**動機**：兩個已確診缺陷**在既有工具下都判不出來**，只會落進 N/A，等於它們不在零噪音回歸網裡。
病根是**斷言型別只有一種**（`anchored_pct`），而這兩個缺陷的判別訊號不在那一層。
- `require_text`：答案本文必須匹配（給「拒答文本零百分比」那種缺陷）。
- `require_chunk`：檢索池必須含指定 `source`+`chunk_index`（給「同檔撈到錯 chunk」那種）。
  FAIL 訊息區分「同檔撈到別的 index」（真缺陷）與「該檔完全沒撈到」（可能是重編號）。

**為什麼搶在重建之前做**：封存結果檔是**已知答案的測試資料**，重建後那批 ground truth 就沒了。
若那時新斷言回報 0 問題，**分不清是修好了還是斷言壞了**。
**判別力雙向驗證**（四個封存檔）：`require_chunk` 4/4 FAIL、`require_text` 2 PASS / 2 FAIL、
正對照 PASS、既有四條逐格未變。

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
- **eval_set 全量 gold 驗證 + 擴充至 100 題**（各類別 25 題）：發現 2 題整題超綱（sem-06/col-09「Apple Silicon」語料 0 命中，已替換）、3 題快照漂移缺陷（`*_Fundamentals_*` glob 匹配多份快照、數值不同，已放寬 rubric）。逐題證據見 [`docs/_archive/eval_set_evidence-2026-07-16.md`](docs/_archive/eval_set_evidence-2026-07-16.md)（**已封存**：那份記錄的題號屬於 100 題時代，多數已不在現行 65 題裡；仍然適用的維護規則已收進 [`eval/README.md`](eval/README.md)）。

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
