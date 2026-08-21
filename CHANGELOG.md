# CHANGELOG

紀錄本專案每次有意義的程式修改（架構調整、參數變更、新增功能、放棄的實驗）。
新條目加在最上面。每筆條目只留「改了什麼、關鍵數字、結論」，診斷過程與推導細節見 git log，不重述。

> `agentic_rag.py`（deepagents 多智能體實驗性入口）的修改紀錄獨立在 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md)。

---

> **已試無效總表**搬到 [`docs/EVAL.md`](docs/EVAL.md)（改動前先查那裡，避免重踩）。
> **已知問題／已接受的極限**搬到 [`BACKLOG.md`](BACKLOG.md)。
> 本檔只放日期式變更記錄。

---

## 2026-08-21 — lex-17 的兩個真因都在「我修的那一層下面」；after 臂是 null result

**預先寫死的判準沒過**：`lex-17` 三條主張要**同時**轉 PASS 才算修法有效。after 臂
（`experiments/agentic/gj_mdna_65q_after.json`）跑出 **PASS 5／FAIL 3，與 before 臂逐條相同**，
且 `require_chunk` 的訊息從「同檔撈到 #1」變成「該檔完全沒被撈到」＝**更壞**。
→ 照判準記為 null result，不算部分成功。

逐層印出來之後，找到兩個各自獨立、都在前一天修法**下面一層**的真因：

**真因 A：保底掃的池，本來就沒有那個 chunk。**
`_ensure_ratio_source_coverage` 只在 `run_state.pool` 裡找，而 pool ＝ Qdrant server-side RRF
回傳的 `RRF_TOP_N_PRIMARY`(=20) 個候選。生產組態是 `full_translate_en=True`，實測英譯句
`"How is Microsoft's revenue growth rate performing?"` 之下，**`MSFT_Fundamentals #0` 連候選名單
都沒進**（進來的是零比率的 `#1`，RRF rank 5）；中文原句反而撈得到 `#0`（rank 7）。
名字叫「保底」，實作卻是「希望它剛好在池裡」。前一天的修法（改成挑「含該欄位」的）方向對，
但在池裡沒有 `#0` 的情況下，效果只是**正確地拒絕補一個沒用的 `#1`** → Fundamentals 整個消失。
→ 新增 `_fetch_fundamentals_with_field()`：池裡沒有時**直接查 Qdrant**（`ticker` ＋
`period_basis="TTM"` 兩個都有索引，全庫 22 筆）。「哪個 chunk 含 Revenue Growth 欄位」有唯一
正確答案 → 照〈LLM 與 Python 的分工〉交給 Python，不靠相似度。
⚠ 分數用 cross-encoder **真的重算**，不塞常數——那個數字會印在引用區塊給使用者看。
⚠ 沒有動 `RRF_TOP_N_PRIMARY`：碼上註明 sweep 過 20 > 40，不為一題改全域召回。

**真因 B：validator 讀的欄位，生產從來沒供給過。**
`_basis_disclosure_notice` 判 `chunk["period_basis"]`，但 `rq.retrieve()` 建 chunk dict 時
**沒有帶這個欄位**（payload 有、還建了索引，就是沒帶出來）→ 該 validator 在線上
**結構性永遠不觸發**。而閘門⑪ 全綠，是因為那 8 條斷言都拿測試自己造的
`{"period_basis": ...}` 餵進去。**量尺與被測物耦合，同型第五次。**
→ payload→chunk 的建構抽成 `rq._payload_to_chunk()`（唯一建構點），補上 `period_basis`。

**量尺跟著修**（`eval/verify_answer_validators.py` 89 → **117** 項）：
- ⑩b 確定性補撈：含「欄位不存在 → 回 `None`，**不可退而求其次**」與「分數必須真的算出來」兩條誤報對照；reranker 用樁替代，仍是零模型載入。
- ⑪b 生產建構子對照：拿**真實 payload 餵 `rq._payload_to_chunk`**，不自己造 dict。
- **變異測試三發全中且判別力落在對的斷言上**：M1（建構子不帶 `period_basis`）→ ⑪ 那 8 條**照樣全 PASS**、只有 ⑪b 的 3 條叫；M2（補撈不看欄位）→ ⑩b 5 條叫；M3（補撈整個移除）→ ⑩b 4 條叫。

**端到端實測**（`experiments/agentic/gj_lex17_smoke.json`）：`lex-17` 三條全 PASS，答案引到
`MSFT_Fundamentals_20260612.txt #0` 並明列 **TTM 18.30%**，且每個數字都標了所屬期間；
口徑警語**沒有**印出來——沉默條件③ 正確生效（引用裡已經有 TTM 就不需要警語）。
⚠ 殘留兩點，都不算修好：① 開頭第一句仍以 10-K 財年 **18%** 當結論，gold 是 TTM 18.3%；
② `anchored_pct` 這條是靠昨天加的 `expect_text` 析取才 PASS 的（anchor 抓到的仍是 18%），
**它現在和 `require_text` 幾乎重複，名字說的事情已經不再量了**。

## 2026-08-20

### live web 取代 KB 新聞：②內容這一半量完——可信，但抓到一個 live 專屬的引用缺陷

**完整判讀見 [`docs/EVAL.md`](docs/EVAL.md)〈live web 取代 KB 新聞：②內容這一半也量完了〉。**

- 錄 `eval/web_fixture_news37.json`（37 題 ＋ 3 題陰性對照、**86 筆 Tavily 原始回應**、as-of 2026-08-20）。**新檔**，不碰綁 as-of 2026-08-15 的舊 fixture；`record_web_fixture.py` 加 ABORT 擋住寫錯檔。
- 新增 `eval/web_claims_news37.json`（40 條性質斷言）。⚠ **驗收不對照新聞 gold**：那批 gold 是「2026 年 6 月的新聞說了什麼」，live web 回答「現在的網路說什麼」——是兩個不同的問題。
- `check_web_claims.py` 新增 `no_fabricated_citations` 斷言。
- **結果 PASS 35／FAIL 5／N-A 0**：觸發 web 35/37、引用 web 33/37、引用主機 100% 在白名單、**地區子網域 0 次**、陰性對照 0/3 打 web。

### 修：live 路徑會出貨指向不存在來源的引用（`【Reference 7, chunk #15】`）

5/37 題，**snapshot 路徑 0/100**。⚠ 不是「多一個壞引用」——**那 5 題的有效 KB 引用是 0**，整份答案沒有任何可追溯的財報出處，答案裡卻有大量財報數字。
**舊行為**：validator 判它捏造 → 丟回重寫 → `CITATION_VALIDATOR_MAX_RETRIES = 1` 用完 → `break` → **原樣 `return answer`**。而該處 trace 寫「走機械式收尾」，**程式裡從來沒有**——註解描述了一個不存在的行為，已一併改成照實說。

- 新增 `_repair_reference_citations`：`Reference N` 的編號**是我們自己編的**（`rq.build_user_prompt` 排成 `[Reference i+1: source, chunk #idx]`），所以 `N → allowed_chunks[N-1]` 是**確定性映射**，屬於 Python 那一半，不必再問 LLM。在 `_validate_and_fix_citations` 的迴圈開頭先跑，能還原的就不浪費一次 LLM 重寫。
- **兩個守門條件**（＝這次設計的重點）：①`N` 超出候選範圍 → 不動；②引用裡的 `chunk #M` 與第 N 筆的 `chunk_index` 不一致 → 不動。⚠ 修補的危險方向**不是漏修**（漏修＝維持現狀），而是**把「無法追溯」變成「看起來可追溯的錯引用」**。條件② 另有一個副作用：它讓「排序假設」不必被證明——兩半一致時，那筆修補是**被資料自己確認過的**。
- **閘門⑨ 12 項**，`verify_answer_validators` 77 → **89/89**。三向變異測試：機制恆不作用 → 7 條 FAIL；拿掉一致性守門 → **誤報對照精準 FAIL 1 條**；拿掉範圍守門 → 當場 IndexError。⚠ 變異③ 順帶證實 `Reference 0` 是三條裡最危險的：Python 的 `[-1]` 是合法索引，少了守門它會**安靜地**指到最後一筆（其他越界值反而會當場炸）。
- 真實答案上的修補率：**還原 16 筆、5 題有 3 題完全清乾淨**。⚠ 這是**下界**——量的時候用結果檔的 `sources` 當代理排序，那是跨子問題聯集，與 Generator 實際看到的清單不同；生產路徑用的是手上真正的 `allowed_chunks`。
- **端到端（帶 trace 的 replay，零網路）**：5 題裡 2 題模型仍吐序號引用，`還原 12 筆`／`還原 3 筆`、**守門擋下 0 筆**，事後掃描 5 題全部零假引用。⚠ 另外 3 題這一輪模型沒吐，**它們的 PASS 不算在修法頭上**；先前 `check_web_claims` 的 PASS 40 是「修法＋重新生成」兩個變因同時動，單看證明不了事。
- ⚠ 真實 `allowed_chunks` 排序下守門條件 **0/15 擋下** → 先前「14 筆有 4 筆對不上」確實是代理排序的假象。
- ⚠ **閘門⑨ 的端到端斷言第一版讀 `experiments/`，把修好的 replay 併回去就 `dirty=0` 而 FAIL**——量尺與被測物耦合。已改成把修法前的 20 筆序號引用**逐字凍結進測試檔**（同閘門② 早就寫下的規則）。⚠ 這是同一天第三次同型事故，見 docs/EVAL.md。


## 2026-08-19

### 新增 `eval/probe_news_web_routing.py`：把「live web 能不能取代 KB 新聞」拆成便宜的一半先做

**完整判讀見 [`docs/EVAL.md`](docs/EVAL.md)〈live web 能不能取代 KB 新聞〉。**

該問題拆成 ①**會不會**打 web（只燒 LLM）與 ②打回來**夠不夠**（連網燒 Tavily）。**① 是 ② 的必要條件**，所以先做 ①。零網路的作法是把 `_tavily_search` 換成計數樁、其餘管線原封不動。

- **結果：路由不是瓶頸。** 37 題冷凍題庫觸發 web ≥35/37，其中 **multi_intent 15/15**（最危險的形狀「財報半讓 Grader 判夠而整題不打 web」沒有發生）；strict 陰性對照 0/6。
- 兩題沒觸發都不是路由壞掉（KB 真的有相關內容且答案有揭露），且**其中一題重跑就會觸發**→「2 題不叫 web」是單輪觀察不是穩定行為。
- **順帶完成 `_unmet_realtime_gaps` 的第一次端到端驗證**：樁回空字串恰好就是它針對的形狀（打了 web 但搜不到 → 回頭用財報生成），實測兩題都印出時效警語。其中「NVIDIA 現在值多少錢」的答案本體是 6/12 的市值快照講成「目前的市值」——警語把它接住了。
- ⚠ **陰性對照修正**：「現在市值／現在股價」**不可以**當陰性對照。第一版把 `col-08` 放進去，它打 web 被記成「浪費」，但 `eval/web_claims.json` 的 `web-01` 對同型問題的斷言正是 `web_calls_gte: 1`——**同一件事在兩支腳本裡有相反的期望**。已拆成 `CONTROL_STRICT`（斷言 0 次）／`CONTROL_LOOSE`（只觀察）。
- ⚠ **57% 打滿 web 預算這個數字是樁造成的，不可外推**；它只能支撐一個結論：**② 的成本上界是 111 次 Tavily 呼叫**。

### 量尺補回兩條（`lex-16`／`lex-17`）；`mi-05` 的缺陷**沒有**被拔除新聞修好

**完整經過見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺補回兩條〉。**

拆題庫讓 `check_number_defects` 從 6 條掉到 4 條，而搬走的兩條**一 PASS 一 FAIL**。把它們**去掉新聞子句、只留財報半**補回母檔：

- `eval_set.json` 63 → **65 題**（lexical 15 → 17）。`lex-16` 承自 `mi-04`、`lex-17` **逐字沿用 `mi-05` 原題前半、其餘一字不改**（讓新聞子句成為唯一變因）。
- `reference_answers.json` 同步生成兩題。⚠ 首次生成**靜默落成英文**（其餘 63 題是繁中）——`gen_reference_answers.py` 沒有 `--match-lang-from` 就會這樣，已重生成。`lex-17` 的 reference 另有一處人工校正：自動版把 TTM 寫成「最近一個會計年度」，而這題的判別力整個在口徑上。
- `number_claims.json` 4 → **7 條**：`lex-16`（anchored_pct 16.6）、`lex-17` 拆成 `require_chunk`（#0 進池，`known_defect`）＋ `anchored_pct`（18.3 且口徑為 TTM）——**進池 ≠ 有用它**。

**結果：拿掉新聞子句沒有修好 `mi-05`。** 兩次端到端 **r1 PASS／r2 FAIL**，r2 逐字就是原症狀（同檔撈到 `#1`、答案改用 10-K 財年 18%）。⚠ **我一度只憑 r1 全 PASS 就宣告「根因是新聞污染、已被推翻」並改了三份文件**，r2 立刻打臉；而 `mi-05` 自己在 2026-08-12 就記過「部分取決於 Plan 產生的子問題」。→ **這 7 條主張的判讀一律要跑 ≥2 輪**（同一份 collection：r1 PASS 7／FAIL 0，r2 PASS 4／FAIL 3）。

**真的被拔除新聞修好的是 `mix-07`**：2/2 從 `I don't have enough information` 翻成答出 $29.5B，而它記載的根因本來就是「Plan 把財報事實譯成新聞查詢 → 候選池全是 News chunk → 拒答」。**這是拔除新聞唯一一個零噪音量到的直接收益。**

**順帶修掉兩個量尺缺口（都是 r2 逼出來的）**：
- `col-11` 的 anchor 寫死 `Microsoft Cloud|整體雲端`，而某輪答案用「雲端營收」→ 答對了卻判 **N/A**。這是 `check_number_defects` 的**第五次失效**，且與前四次不同——前四次是判反，這次是**護欄安靜地停止量測**。
- 兩條主張補 `expect_text` 當第二判準，因為 `expect_pct` 的 ±1pt 容差擋不住「數值接近但口徑不同」的錯值：`col-11` 補金額 `545 億／$54.5B`、`lex-17` 補字面 `18.3`（10-K 的 `18%` 差 0.3，原本會 PASS）。⚠ `lex-17` **刻意不加 forbid**：兩個口徑並陳並標注清楚是好答案。

### 新增 R4：web ↔ 財報數值衝突要求「並陳」而不是「裁決」

**完整設計與判斷理由見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A10。**

- `_ground_source_type` 現在也吃 `web_extra`（來源型別 `"web"`，**刻意不列入 `AUTHORITATIVE_TYPES`**）。順帶修掉 `if not chunks: return` 這個洞——「候選池空、只有 web」**正是 live 路徑的常見形狀**，原本整個跳過 grounding。
- 新增 `find_unreconciled_web_conflicts`（R4）：同格互斥值、一邊只在 web、另一邊在財報 → 要求兩個都講、各自標出處與時點。**兩邊都已標出處就沉默。**
- ⚠ **刻意不擴充 R3**：R3 判「一邊為錯」，套到 web 會讓系統系統性報舊數字（web 可以合法地比 filing 新）。`_consistency_check_and_fix` 的舊 docstring 已寫下「把 web 併進去會改變 R3 的語意」——那句是對的，只是結論該是「新增一條動作不同的規則」。
- **閘門⑧ 15 項**，`verify_answer_validators` 62 → **77/77**。雙向變異測試：拿掉機制 → 2 條 FAIL；拿掉「已標出處就沉默」→ 誤報對照 FAIL。

### 修：時效警語在 KB 拔除新聞後整個死掉

**完整診斷見 [`docs/EVAL.md`](docs/EVAL.md)〈後續：時效警語一度整個死掉〉。**

拔除新聞時把 `_news_freshness_gaps` 標成「閒置但保留」，**低估了後果**——它是缺口的唯一來源，
於是 `_format_unresolved_freshness_notice` 永遠回空字串。實測：即時題 → Grader 判不足 →
打 web → **預算用完或搜不到** → 答案用財報 chunk 生成 → **零時效揭露**。

- 新增 `_unmet_realtime_gaps`：判準改成「子問題需要即時資料（`realtime_need != none`）」。`realtime_need` 從 executor 傳出（新增第三個回傳值），`web_used` 的過濾沿用既有邏輯**不重複判斷**。
- 警語措辭分流：realtime 缺口講「知識庫本來就沒有這種資料」，news 缺口維持原文；`doc_type` 進 dedup key，兩種缺口不互相蓋掉。
- **閘門⑤b 10 條 ＋ 變異測試**：打斷機制 → 7 條乾淨 FAIL；還原 → **62/62 PASS**（原 52）。⚠ 含「財報題 → 零缺口」的陰性對照——少了它，警語會印在每道財報題底下。

### 修：財報被誤用來證明「候選池夠新」，讓 `kb_unfixable` 早退失效

**完整診斷見 [`docs/EVAL.md`](docs/EVAL.md)〈「永不過期」被誤用成「能證明夠新」〉。**

確認「證據後置」設計後的第一步是**量 Grader 有沒有照設計運作**（零 LLM、真實檢索池）。
結果 6 個即時類問題**有 4 個時效改判不觸發**，含「微軟現在的股價是多少？」這種 intraday 題。

⚠ **實際代價比第一版宣稱的小，已更正**：後續對照（舊行為 vs 新行為，同池同 Grader）顯示這
4 題**在舊碼下 web 一樣會被叫**——Grader 自己就判 `sufficient=False`。我第一版把「時效改判
沒觸發」報成了「web 不會被叫」，那是兩件事。真正的代價是 **`kb_unfixable` 恆為 False → 每個
子問題白燒 `MAX_REWRITES=2` 輪改寫才走 web**（那正是 `kb_unfixable` 被造出來要防的死迴圈），
以及**第二道防線被靜默關閉**（LLM 若判錯 `sufficient=True`，就沒有東西接得住）。

- **根因**：`_source_newest_date` 把 10-K 的 4 碼財年戳算成該年 12/31（`MSFT_10K_2026` → **2026-12-31，未來**），而 `_stale_for_realtime` 取全池 `max(dates)` → 財報不只是棄權，是**替整個池子背書說夠新**。
- **⚠ 4 題裡只有 1 題是拔除新聞造成的**，另 3 題（含最危險的 intraday 股價題）從來沒被 `looks_like_news_query` 攔過 ＝ **既有的洞，只是一直沒人量**。
- **修法**：新增 `_is_freshness_evidence()`——只有 8 碼真實日曆日期算時效證據，4/6 碼財報期間**不算證據也不算過期**。`_kb_ceiling_date` 套用同一套資格判準。無合格證據 → 哨符 `NO_REALTIME_SOURCE(-1)`，Grader 訊息分流。
- ⚠ **這推翻了一個先前刻意的成本判斷**（「誤判新鮮只是維持現狀」）——那句話在 KB 有新聞時成立，KB 只剩財報後「維持現狀」＝拿 10-K 回答今天股價。`none`（財報期間數字）不受影響，維持永不過期。
- **閘門⑤ 改寫**：三條舊斷言換成新語意，並新增**回歸鎖**（未來日期的 10-K 不得蓋過真實日期來源——這條在新舊實作之間有判別力）與**誤報對照**（池裡有 1 天前的來源 → 不判過期，沒有它「一律判過期」也會全綠）。

**修後實測**：6/6 即時題 `kb_unfixable=True`（直接跳過改寫走 web）、2/2 財報題不觸發改判。

**同時新增 [`eval/probe_realtime_need.py`](eval/probe_realtime_need.py)**（LLM 側唯一量尺）：
8 題 × 3 輪，`realtime_need` **8/8 正確、零跨輪抖動、危險錯誤 0**。其中「NVIDIA 最近有什麼
新進展？」**不含任何新聞字樣**——那正是被拆掉的 `looks_like_news_query` 詞表會漏的形狀
（實測 18 個措辭漏 10 個），LLM 判準接住了。這是「詞表換 LLM 判斷」這個決定的第一組正面證據。
⚠ 也因此得知：**這條路的主防線是 Grader 的 LLM 判斷，時效改判是第二道**。第二道的價值無法在
「LLM 判對」的題上量到——要量它得構造 LLM 會判錯的題，目前沒有。

### KB 拔除新聞：只留「記錄」，把「流」交給 live web

**完整理由、資料實況與三個量尺變動見 [`docs/EVAL.md`](docs/EVAL.md)〈KB 拔除新聞〉。**

**資料層**
- [`data_update_edgar.py`](data_update_edgar.py)：`News` 加進 `RAW_EXCLUDE_DIRS`（**整條規則的單一開關**）。掃描從 38 個 .txt 降到 14 個（只剩 Fundamentals/IncomeStatement）。
- [`fetch_data.py`](fetch_data.py)：`--skip-news` → **`--with-news`（預設不抓）**。
- Qdrant：`us_stock_rag_edgar_mdna` 3925 → **3827**、`us_stock_rag_edgar_multiyear` 13120 → **13022**（各刪 98 個 news chunk）。**歷史對照臂刻意不動**。

**程式層**
- [`rag_query.py`](rag_query.py)：移除 `doc_type=news` 硬 filter 注入、news 題的 `ticker→mentioned_tickers` 改寫、`_resolve_latest_quarter_filter` 的「新聞題不搶」守衛、撈不到 news 的 WARN。`looks_like_news_query` 保留但**退出所有生產判斷**（只剩診斷探針當分類標籤用）。
- [`agentic_rag_v2.py`](agentic_rag_v2.py)：Planner prompt 拿掉「市場事件可拆 news 子問題」的例外（向量庫已無新聞，那樣寫只會撈到空的）；`find_authority_conflicts`（R3 財報優先）與 `_news_freshness_gaps` 標記為**永久閒置但刻意保留**——它們治的病是「KB 新聞數字壓過財報」，病源消失，閒置是對的。

**成效（實測）**：`mi-01`「NVIDIA 最新財報…加上新聞中…」的 top-5 從「5 個 news chunk、filing gold 不在候選池」變成 `NVDA_10Q_202604#42／#14` 排名 1、2。這類複合題是拔除前**最大的殘留檢索失敗**（3/49）。

**量尺（⚠ 跨今日的分數一律不可直接比）**
- `eval_set.json` **100 → 63 題**；37 題（gold 含 `*_News_*.txt`）連同 reference/claims 搬到 `eval/*_news.json` **冷凍**。冷凍不是刪除——那是日後驗收「live web 能否取代 KB 新聞」唯一的現成題庫。
- `check_number_defects` 6 → 4 條，**PASS 4／FAIL 2 → PASS 3／FAIL 1**。⚠ **不是改善是組成變動**；且 `mi-05` 的缺陷（`MSFT_Fundamentals#0` 沒進候選池）**根本不是新聞造成的，它只是離開了量尺**（記進 BACKLOG）。
- [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py) 閘門④ 一度 FAIL 3 項——`_news_freshness_gaps` 的 cutoff 讀真實 KB coverage，KB 沒新聞就算不出缺口。**那是量尺失去判別力不是機制壞掉**：改成**自帶合成 coverage**（不刪斷言），52/52 綠，且從此不與 KB 內容耦合。

**閘門複驗**：`verify_cross_period_collapse` 17/17、`verify_period_intent_routing` 39/39、`verify_answer_validators` 52/52、`verify_web_gate_isolation` PASS。


### 期別探針 gold 的人工複審（零程式判定，逐題讀 filing 原文）

不採信任何聚合腳本的輸出，把 49 題 filing gold 逐題調出原文人工看。**修法的收益是真的，
但量尺有兩處要講清楚，且撤回一條先前的宣稱。**

- **撤回**：`docs/EVAL.md` 原寫「`keep=1` 新增 3 個沉默失效」——那 3 題（`mix-07`／`lex-12`／
  `mix-10`）人工讀原文後**全是量尺假陽性**（更新的一季逐字有同一句 Wiz 段落／被路由到 news）。
  `keep=2` 的決定不變，但只剩「趨勢題 −38%」這一條獨立證據支撐。
- **新記**：**46/49 的 gold 就是該公司最新那一份**，而 B 的規則正是「留最新」→ `gold@k`
  結構上偏袒 B。引用 B 的增益數字時必須同時講這句。
- **確認為真缺陷**：`mix-15`（`TSLA_10Q_202406` 說「2024 年至第二季 844,000 輛」vs gold
  「2026 年 860 千輛」，差 2%、差兩年）、`sem-05`（Anthropic 投資 2023 是 12.5 億、2025 是 148 億）。
- **確認為量尺假陽性**：`mix-10`（「higher net sales of Pro models」四季逐字都有）、
  `col-07`（「minority market share」三年逐字都在）。
- **兩把想取代人工的聚合尺都失敗**，且都是本 repo 記過的老坑：句子重現率被章節樣板稀釋、
  數字比對被單位換算誤殺（答案 `569.94` 億 vs filing `56,994` 百萬，49 題誤報 23 題）。

同時確認 **append-only 在 gold 層面成立**：14 組 `(ticker, form)` 逐一比對，沒有任何一份比
`period_probe_baseline.json` 快照更新的 filing 進來 → 「最新一季」類 gold 的指稱未被新資料改變。

詳見 [`docs/EVAL.md`](docs/EVAL.md)〈期別探針的 gold 人工複審〉；剩餘極限記在
[`BACKLOG.md`](BACKLOG.md)〈已接受的極限〉。


### 檢索層兩條期別修法：跨期 field collapsing（B）＋ 期間意圖解析（A）

承同日〈多年語料壓力測試〉量到的兩個病灶。**完整分析與四次自我否決見
[`docs/EVAL.md`](docs/EVAL.md)〈跨期 field collapsing 與期間意圖解析〉。**

**B — 跨期 field collapsing**（[`rag_query.py`](rag_query.py) `_collapse_cross_period_sections`，
**已翻預設為開啟**，`RAG_CROSS_PERIOD_COLLAPSE=0` 可關）
同一 `(ticker, filing_type, item_id)` 最多讓 `keep=2` 份 filing 佔位，組內留**財年最新**的。
是搜尋引擎的標準原語（Solr `CollapsingQParser`／ES `collapse`），**不是 MMR**——MMR 靠 embedding
相似度且帶 λ 超參數，而本專案量尺已飽和、沒有能調 λ 的尺。
- 敢翻預設的理由：**單年生產 collection 上逐題完全 no-op（0/49 題 top-5 有任何變化）**，
  多年上改 23/49 題。資料還不需要時不作用，需要時自己生效。
- 順手補了 `retrieve()` 回傳 chunk 的 `item_id`／`filing_type`／`period_code`／`fiscal_rank`
  四欄——**這個缺口先前逼三個下游各自再掃一次 Qdrant**（agentic 期別 validator、
  `_scan_kb_coverage`、`probe_temporal_interference`）。

**A — 期間意圖解析**（`resolve_period_intent` ＋ `_get_period_ladder`／`ladder_pick`，
env `RQ_PERIOD_INTENT_LLM=1` 啟用，**預設仍關**）
「這題問的是哪個期間」交 LLM（`period_ref: latest|absolute|range|none` ＋ `fiscal_year`
＋ `granularity`），「那是哪個期碼」交 Python 從 ladder 算——照 CLAUDE.md 的 LLM/Python 分工。
- **ladder 用 payload 的 `(fiscal_year, fiscal_period)` 排序，不用期碼字串比大小**：
  10-K 是 4 位年份、10-Q 是 6 位 yyyymm，混比會時對時錯，**實測 385 個配對有 31 對（8.1%）判反**
  （例：`AAPL_10K_2025`(2025) 實際比 `AAPL_10Q_202506`(202506) 新，字串比大小卻說反）。
- **`period_ref=range` 反過來保護 B**：趨勢題整個跳過 collapse。8/8 趨勢題判對，
  而 collapse 對它們的損害是 `keep=1` −38%／`keep=2` −6% —— 這是那個損害唯一的正解。
- 解決了三條確定性路徑都表達不出來的**複合指稱**：`mix-15`「Tesla **2026 年至今**」
  → `{latest, fiscal_year:2026, granularity:quarter}`，gold rank 6 → 1。

**單年生產 collection 上兩者都不動任何東西**（A+B 逐題變好 0、變差 0）→ 這是 B 敢翻預設
的理由，也是 **A 不翻**的理由：A 在目前資料上買不到東西卻要付每次 retrieve 1.54s。
A 的翻預設條件寫在 [`BACKLOG.md`](BACKLOG.md)。

**成效（`us_stock_rag_edgar_multiyear`，49 題）**：gold@5 0.837 → **0.898**、錯期率
0.082 → **0.041**、排擠率 0.278 → **0.188**；`explicit` 類錯期率 0.333 → **0**。
逐題比對**變好 3 題、變差 0 題**。

**新增閘門**：[`eval/verify_cross_period_collapse.py`](eval/verify_cross_period_collapse.py)
17/17（零 LLM、零 Qdrant）、[`eval/verify_period_intent_routing.py`](eval/verify_period_intent_routing.py)
39/39（零 LLM，只讀 payload）。後者的閘門② 帶自我檢查：印出「期碼字串比大小判反幾對」，
一對都沒有就明說**那是測資不足不是系統健康**。
**新增陰性對照**：[`eval/period_probe_trend_queries.json`](eval/period_probe_trend_queries.json)
8 題本來就需要多期的趨勢題——eval_set 的 100 題裡**沒有任何一題**是這個形狀。

### 多年語料壓力測試：KB 21 → 77 份 filing，量出期別干擾的真實形狀

`BACKLOG.md` 記了很久的「**復活條件：KB 納入第二個年度的 filing 時**」達成並執行。
往回補兩年（每家 3×10-K ＋ 8×10-Q，橫跨 ~2.5 年，13,120 chunks）灌進獨立的
`us_stock_rag_edgar_multiyear`，**生產 collection 一個位元組都沒動**。

**做了什麼**
- [`eval/probe_temporal_interference.py`](eval/probe_temporal_interference.py)（新增）：三個
  **互相獨立**的指標（F1 排擠／F2 錯選／gold rank 連續量）＋ 兩臂。分組鍵用 payload 的
  `item_id`，所以「同一節、不同年份」是確定性認出來的，不必用字串相似度猜。
- [`eval/period_probe_baseline.json`](eval/period_probe_baseline.json)（新增）：灌舊資料前的
  21 份 filing 凍結快照。eval_set 有 4 題 gold 是萬用字元（`MSFT_10K_*.html` 等），照當下
  manifest 展開會讓它們「撈到哪一年都算命中」＝**量尺在最需要判別力的地方失去判別力**。
  對照快照展開，`eval_set.json` **一個字都不用改**。
- [`fetch_data.py`](fetch_data.py)：加 `--annuals N`；加 **append-only 跳過**（`_already_local`）
  ——`latest(N)` 一定會把既有那幾份一起撈回來，照原行為會覆寫。實測既有 21 份 md5 **全數未變**。

**結果（完整分析見 [`docs/EVAL.md`](docs/EVAL.md)〈多年語料的期別干擾〉）**
- **競爭密度上升最多的那一類完全沒退步**：`relative`（最新一季）兄弟密度 1.90→7.52，
  gold@5 與錯期率**一格都沒動**——`_resolve_latest_quarter_filter` 的硬 filter 全吸收了。
- **硬 filter 的價值放大約 10 倍**：關掉路由，單年只值 0.048 錯期率，多年是 **0.476**。
  那條路由從「可有可無的優化」變成「不能拔的命脈」，而它**靠硬編碼詞表觸發**。
- **真正壞掉的是「問題沒提期間」那 25 題**：排擠率 0.080 → **0.480**，沒有任何 filter 保護。
- **F1 遠大於 F2**（68 個浪費席位 vs 4 題錯期）→ 病灶是**多樣性不是期別選擇**。

**驗收**：`verify_table_captions` PASS（`missing_on_big` 0）、`verify_segment_split` PASS
（判準②③⑤ 全 0）、`verify_chunk_grounding` **FAIL 1 筆**（`MSFT_10K_2024.html#158`，
1/1009；生產 collection 同一道閘門 0 筆 → 新資料帶出來的、與本次改動無關，已記進 BACKLOG）。
兩輪探針 `RAG_REPLAY_CACHE` **hit 65 / miss 0**，Δ 裡沒有 LLM 抽樣噪音。

## 2026-08-18

### Groq 讓 `llama-3.3-70b-versatile` 退役 → 表格摘要換 `openai/gpt-oss-20b`

**怎麼發現的**：不是稽核抓到的，是**重建當下 log 裡每張表都在 404**。2026-08-16 Groq 讓
`llama-3.3-70b-versatile` 退役並把 Llama 全系列下架（現存 chat 模型只剩 gpt-oss 系列、
`qwen/qwen3.6-27b`、`groq/compound`）。`_llm_summarize_table` 對非 429 錯誤是 `print WARN`
後 `return ""` → **整條表格摘要路靜默歸零**，正是 [`eval/verify_table_captions.py`](eval/verify_table_captions.py)
`missing_on_big` 閘門設計要抓的那種降級。多年語料重建剛跑到第 10 份 filing 就攔下來重跑。

**功能依賴只有一處**：[`unstructured_components.py`](unstructured_components.py) `TABLE_SUMMARY_MODEL`。
`eval/eval_chunk_recall.py`、`eval/eval_two_stage.py` 的實際預設是 `rq.DEFAULT_MODEL`（NVIDIA NIM），
只有 docstring 示例寫著死掉的模型名 → 一起改掉，免得把人導向 404。

**⚠ Groq 官方建議的替代品 `gpt-oss-120b` 正是本專案 2026-08-11 測過並否決的模型。**
但當初的否決理由是「reasoning token 吃光 completion 額度 → content 空字串」——那是**預算
問題不是能力問題**，可以驗。bake-off 刻意把「重現既有失敗」放進矩陣：

| 組態 | 空/錯 | 重複全等 | 平均字元 |
|---|---|---|---|
| `gpt-oss-120b` @200 | **10/10 全空** | — | 0 |
| `gpt-oss-120b` @700（14 表×3） | 0/42 | 6/14 | 150 |
| **`gpt-oss-20b` @700 `effort=low`** | 0/42 | **12/14** | 164 |
| `qwen/qwen3.6-27b` @700 | 0/10 | 5/5 | 2709（`<think>` 直接吐進 content，不可用） |

@200 全空**完美重現既有記載** → 根因確認是 completion 預算，於是 `TABLE_SUMMARY_MAX_TOKENS`
200 → 700，並新增 `TABLE_SUMMARY_REASONING_EFFORT`（預設 `low`，空字串則不帶該參數）。

**選 20b 的判準是輸出穩定性，不是模型大小**——與當初選 llama-70b 的判準一致（「對同一張表
三次輸出全等」），因為 caption 變異是 CLAUDE.md〈重建不會逐字重現舊 collection〉第 ② 條的
成因之一。代價是 20b 有 6/42 超過 system prompt 要求的 30 詞（120b 是 0/42）；長度不是正確性
問題，用它換穩定性划算。生產路徑實測 4/4 有輸出、句子完整。

⚠ **第一輪 bake-off 的結論不可信、被自己推翻**：取樣排序後 5 張全落在 `AAPL_10K` 的 exhibit
index（BACKLOG 列為已接受極限的垃圾表區），不代表真正會走 LLM 這條路的表。round 2 改成跨
7 家 × 兩種 form 的 14 張**財務**表、3 次重複，上表是 round 2 的數字。

## 2026-08-15

### 數字溯源稽核（`find_untraceable_numbers`）：一道**沒有實測正例**的防線

答案裡「不可能被換算」形態的數字（兩位小數的報價／市值），必須在 chunk 全文或 `web_notes` 裡溯得回來源。零 LLM 偵測，抓到才花一次重生成。**放在 reflect 之後**——那是驗證鏈唯一沒人看守的位置：`_validate_and_fix_citations` 的 `_CITE_RE` 要求 `, chunk #N`（`[web: url]` 不算引用），而任何 validator 的重生成之後就只剩它了。

⚠ **要說清楚它的證據狀態**：它原本是為了封 `web-02` 的「$196.82 憑空生成」而寫的，而那個 FAIL 後來查明是量尺誤報（見下）。所以**目前零實測正例**，留下的理由是結構性的，陽性測試全是變異注入。

**真正的工作量在排除誤報**，兩條規則是量出來的，不是想出來的——100 題既有答案乾跑：

| 版本 | 誤報 | 誤報形狀 |
|---|---|---|
| 逐字比對 | **11/100** | `153.69 億美元（$15,369 million）`——**億換算值**，來源寫的是 `15,369` |
| ＋排除後接 `億／兆／萬` | 1/100 | `31.33` ← 來源 `P/E Ratio Trailing: 31.325687`，**正確四捨五入** |
| ＋捨入到兩位比對 | **0/100** | — |

捨入容忍不是放水：`196.92`／`31.43` 這種鄰近錯值仍判 FAIL（閘門⑦ 有斷言）。

---

### 期別稽核：抓「答案自稱最新一季，卻引用了較舊的期別」

**病灶（web-03，一次誤診之後才找到）**：「Microsoft 最新一季的 Azure 營收成長率」答 40%【`MSFT_10Q_202603`】，還標了「截至 2026 年 3 月 31 日的三個月」。數字沒錯、期間也標了——但 MSFT 最新一季是 **Q4 FY2026（4–6 月），沒有獨立 10-Q，包在 10-K 裡**，而 `MSFT_10K_2026 #129`（`Azure and other cloud services revenue grew 41%`）**就在同一份證據集合裡、rerank rank 4**，完全沒被用到。

**先記一次誤診**：第一版結論是「`realtime_need` 判 `none` → `REALTIME_STALE_DAYS["none"] is None` → `_stale_for_realtime()` 第一行就 return，整段日期算術連 `as_of` 都沒讀到 → 該讓相對期間指稱獨立觸發 web」。短路那件事**機制上是真的**（見 BACKLOG），但 **web-03 不是它的證據**——按那個修法會把一題 KB 明明答得出來的問題送上網。差別是把 coverage 印出來才發現的（`MSFT_10K_2026` 的 stamp 是**財年**不是日曆期末）。

**修法**：Synthesize 加第三道確定性 validator [`find_stale_period_claims`](agentic_rag_v2.py)（零 LLM，只做 `(fiscal_year, fiscal_period)` 比大小），抓到就帶著落差重生成一次，沿用一致性稽核那條路徑。

| 設計決定 | 為什麼 |
|---|---|
| **判準看答案不看問題** | 判「使用者是不是在問最新一期」要嘛加詞表（換措辭就漏）、要嘛動 Grader prompt（破壞 snapshot 逐字不變的 eval 隔離）。但**答案寫出「最新一季」時它就是在做一個宣稱**，「你引的是不是證據裡最新的一期」是純比對 → 驗證答案自己的宣稱，不是猜意圖。誤報風險低（問 FY2025 時答案不會寫「最新一季」），漏判只是維持現狀 |
| **排序用 `(fiscal_year, fiscal_period)`，不用 `_source_newest_date`** | 後者把 `TICKER_10K_YYYY` 一律算成該年 12/31（刻意高估）。NVDA 財年 1 月底結束 → `NVDA_10K_2026`(→12/31) 會被判得比真正更新的 `NVDA_10Q_202604`(FY2027 Q1) 還新，**排序直接反轉**。期別欄位靠擴充 `_scan_kb_coverage` 的同一次 scroll 建表——`rq.retrieve()` 回傳的 chunk dict 沒有這些欄位，而改共用的 retrieve 投影影響面太大 |
| **每家比兩輪（全期別 ＋ 只比 10-Q）** | 只比全期別會漏。實跑印證：修好第一輪之後**重生成的答案自己暴露殘留缺口**——它照指示引了 10-K 並說明「全年 41%、未拆單季」，卻拿 `MSFT_10Q_202512`（FY2026 Q2, 39%）當「最新單季」，而 FY2026 Q3 就在證據裡。「最新一季」問的是最新的**季**，10-K 過關不代表季別選對 |

**宣稱詞辨識的第一版是窮舉清單，同一次迭代內就漏了兩個**：修正後重生成的答案寫的是「最新**單季**」與「最新公布的**季報**」，兩個都不在清單裡 → validator 對自己造成的新問題視而不見。改成「錨詞（最新／最近）＋ 5 字內期別詞，中間不許有數字」（數字那條讓「最新的 2026 年財報」這種**絕對期間**不誤觸）。

**時效警語同步改成看結果**：`_build_todo_temporal_scope` 原本用 `bool(_RELATIVE_TIME_RE.search(task)) and rq.looks_like_news_query(task)` 兩個詞表串聯決定要不要產生時效缺口——「Microsoft 最新一季的 Azure 營收成長率」過得了前者（有「最新」）卻過不了後者（不是新聞措辭）→ **一句時效警語都不會印**。改成 [`_news_freshness_gaps`](agentic_rag_v2.py)：看**這個子問題實際 commit 了誰的新聞 chunk**，零詞表且更準（問法像新聞但答案全靠財報時，舊版會印一句無關的警語）。`_RELATIVE_TIME_RE` 已從碼上刪除。⚠ 2026-08-13 宣稱「兩道詞表閘門全數移除」時只改了 web 觸發那道，警語這道漏改而文件已寫成全移除，**漂移了兩天**。

**驗收**：新增 [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py)（零 LLM、零網路，只讀 Qdrant coverage，秒級）六道閘門 **42 項全 PASS**，其中 **10 條是陰性對照**。變異注入 3/3 抓到——把 `_fiscal_rank` 換回 `_source_newest_date` 排序 → NVDA 那條立刻誤報。既有 [`verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) 79 項回歸 PASS。

⚠ **陽性案例逐字凍結在測試檔裡，不讀 `experiments/`**。第一版是去讀結果檔的——那份會被修好，一修好陽性就消失，**閘門會隨著修法生效而自己失去判別力**。「現況是乾淨的」是相反方向的斷言，由閘門⑥ 逐題另外檢查（5/5）。

**同一輪跑分的 `web-02` FAIL 是量尺誤報，不是系統缺陷**（撤回一次錯誤判定）：斷言說「2026 年的平均股價為 $196.82」是憑空生成，但 fixture 的 macrotrends 表格裡就有 `| 2026 | 196.8165 | ... |`——**答案是正確地四捨五入**。成因是 `numbers_must_be_in_fixture` 只做逐字比對。已補上捨入容忍（`_number_seen`），5/5 恢復 PASS，且 `196.92`／`31.43` 這種鄰近錯值仍判 False。
> **這補的是既有規則的鏡像面**：CLAUDE.md 寫的是「稽核回傳 **0 筆問題**先當壞消息查」，這次踩到的是反過來那一半——**稽核回報 FAIL 也要先問「是不是量尺錯」**。我拿它當真陽性寫了一條 BACKLOG、一段 CHANGELOG、一節 docs 才發現，查證成本只有一次 `grep`。

---

### live web 路徑從「零實驗支撐」變成可重現：`web_replay` ＋ 逐條斷言

**起點**：Tavily 整合、白名單＋本地複核、URL 抽發布日、時效分層、去重、單域名上限、query 級預算、`kb_unfixable`、system message 放行條款——整套機制在 eval 裡**執行次數是 0**（snapshot 恆關），唯一的「量測」是 n=1 實跑軼事。

**解法是把不確定性切開**，不是硬定 gold：
- **確定性的部分**（白名單／去重／日期算術／預算）已由 [`verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) 六道閘門 79 項斷言涵蓋，零 LLM 零網路秒級 → **不重測**。
- **含 LLM 的部分**（Generator 到底有沒有引用 web 數字、KB 舊值與 web 新值衝突時有沒有並陳並標時點、web 有沒有被無差別觸發）以前沒有任何測試 → 新增 [`web_replay.py`](web_replay.py) 錄 Tavily **原始回應** ＋ 既有的 `AGENTIC_AS_OF_DATE` 鎖死「今天」，外部世界變成靜態的之後寫斷言。

| 新檔 | 作用 |
|---|---|
| [`web_replay.py`](web_replay.py) | Tavily 原始回應錄／放；未設 `RAG_WEB_REPLAY` 完全 no-op |
| [`eval/record_web_fixture.py`](eval/record_web_fixture.py) | 5 題（含 1 個陰性對照），`--mode record\|replay` |
| [`eval/web_claims.json`](eval/web_claims.json) ＋ [`eval/check_web_claims.py`](eval/check_web_claims.py) | 逐條斷言，PASS／FAIL／**N-A** 三態 |

**結果**：**兩次獨立取樣（錄製那輪、重放那輪，LLM 挑了不同來源與不同事實）同一組斷言都 5/5 PASS**；注入 6 種變異（拔掉 web 引用／換成地區子網域／陰性對照被觸發／拿掉 KB 舊值日期／股價竄改／只竄改其中一個）**6/6 全被抓到**——全綠先當壞消息查過了。

**四個踩到的坑**：
1. **錄的層級**：必須錄 `TavilyClient.search()` 的原始回應，不能錄 `_tavily_search()` 的回傳字串——後者是跑完 `_host_allowed` 複核／去重／抽日期／過時過濾／截斷之後的成品，錄在那裡等於把要測的六道一起 mock 掉。
2. **兩邊都要 deepcopy**：`_dedupe_web_results` 就地寫 `r["_pub_date"]`（`agentic_rag_v2.py:584`）。`put()` 少 deepcopy → 落盤 `TypeError`，首次錄製**安靜掉了第 8 筆**（atexit 的例外不影響 exit code）；**`get()` 少 deepcopy 更嚴重**——重放第二次時日期已被上一次塞好 → `_url_published_date` 那段根本不執行，兩次重放走不同程式路徑。序列化失敗已從 atexit 提前到 `put()` 當場炸（`RecordError`，且不被優雅降級吞掉，同 `FixtureMiss`）。
3. **驗收腳本第一版自己誤報三題 FAIL**：正則只認 ASCII `[web:]`，而 `gpt-oss-120b` 吐的是**全形**`【web:】`（docs/AGENTIC.md A5 早有記載）。
4. **斷言不能寫死成某一次錄製的答案**：錄製時 LLM 引 stockanalysis 的 `$4.46 兆`、重放時引 finance.yahoo 的 `$4.45 兆`，**兩個都在 fixture 裡都對**。fixture 是一個**值空間**。改成只測「不論它挑哪個都必須成立」的性質後才穩。

**只錄 web 不夠**：pipeline 送給 Tavily 的 query 由 Planner／Grader 的 LLM 輸出決定，而 MoE 溫度 0 不固定路由（plan 實測 48% 重跑不同）→ 重放會生出不同 web query 而 miss。錄製腳本同時開 `RAG_REPLAY_CACHE` 把 plan／translate_en／check 一起釘死才閉環。

**順帶量到一個真缺口（`web-03`）**：「Microsoft 最新一季 Azure 成長率」答成 40%【`MSFT_10Q_202603`】。⚠ **這一段的第一版診斷是錯的**（原寫「KB 最新 10-Q 是 202603，5 個月前的資料被當成最新一季，該去查 web」）：MSFT 在 KB 的天花板是 `MSFT_10K_2026`（FY2026，涵蓋到 2026-06-30），距 as-of 只有 46 天，**KB 一點都不缺資料，web 對這題本來就不該觸發**。真正的缺陷與修法見下一節。

---

## 2026-08-14

### 檢索側 LLM 20b vs 120b：等價，而且「用小模型求快」是假的

**起點**：兩條管線的檢索側模型不同（單發 120b／agentic 20b），兩邊都沒有對照支撐，而它同時是單發 vs agentic 對照的未控制變因。

**新增 [`eval/ablate_retrieval_model.py`](eval/ablate_retrieval_model.py)**（兩 stage，n=100）：stage 1 錄下兩模型的 `parse_query_filters` JSON 與 `translate_query_to_english` 字串（各跑 2 次）；stage 2 把那些輸出 monkeypatch 進真實檢索器比最終 top-k——**stage 2 零 LLM**，同一份輸入重跑結果一樣。噪音底線取「同模型的第二次跑」。

| | 120b#0 | 120b#1 | 20b#0 |
|---|---|---|---|
| gold recall@5 | 0.8479 | 0.8465 | 0.8459 |
| 至少命中一個 gold | 0.96 | 0.96 | 0.96 |
| 完全撈不到 gold 的題 | col-07/lex-03/lex-07/lex-14 | 同左 | 同左 |
| 平均單次耗時（n=200） | **1.76s** | — | **3.57s** |
| 與 120b#0 的 chunk 集合相同 | — | 85/100 | 72/100 |

**結論**：候選池確實被換掉（Jaccard 0.897 < 噪音 0.947），但**換掉的全是無關 chunk**——gold recall 差 0.0020、噪音 0.0014，逐題兩邊各只掉 1 題。**兩個模型在檢索側等價**。

**但方向是反的**：20b 在 NIM 上慢一倍，`agentic_rag_v2.py` 那句「用小模型求快」前提為假（註解已改）。該做的是把 agentic 升 120b，不是把單發降 20b。待定案，見 [`BACKLOG.md`](BACKLOG.md)。

**順帶關掉一條判讀限制**：[`docs/EVAL.md`](docs/EVAL.md)〈分類別抵銷效應〉的判讀限制② 有兩個未控制變因，這次排除掉其中一個（不是靠對齊，是量到它沒有作用）。

**方法論**（三條寫進 [`docs/EVAL.md`](docs/EVAL.md)）：①**字串比對對翻譯沒有判別力**——86/100 跨模型不同，但 68 題是同模型自己跑兩次就不同；②`parse_query_filters` 有 **91/100 題被效率 gate 擋在 LLM 之前**，不先算這個會把「gate 擋掉」誤讀成「模型一樣好」；③A/B 兩臂的 LLM 輸出要先錄下再釘死。

### 「最新一季」硬路由的兩個覆蓋面問題：都是資料層決定的，不是偷懶

掃 collection（3925 chunks）查清 [`BACKLOG.md`](BACKLOG.md) 那條的破口② ③：

- **只覆蓋 10-Q**：`report_period_code` 在 10-K 是 4 位年份（`2025`/`2026`）、10-Q 是 6 位 yyyymm，**News 98 ＋ Fundamentals 81 chunks 全是 NONE**。所以 News/Fundamentals 是沒有欄位可 filter（也正是 `routed_latest` 要放寬成「值相符 OR 欄位為空」的原因）；10-K 則是**每家只有 1 份、病灶結構上不存在**。⚠ 地雷：`len(code) < 6` 那行不能只是拿掉——期碼是字串比大小，`'2026' > '202510'` 為真。
- **限單一公司**：**eval_set 有 0 題受影響**（16 題通過前六道閘門的全是單一公司）→ 這個破口是推理出來的、不是量出來的。同時**更正 BACKLOG 記的修法**：`MatchAny` 是全域 OR，AAPL 最新 `202606`／MSFT 最新 `202603` 會讓 AAPL 自己的舊季 `202603` 通過；正確結構是 OR-of-ANDs，而卡點是 `build_qdrant_filter` 吃 flat list、結構上寫不出「每家配每家」。

### 多公司期別 probe：損害是真的，但期別 filter 修不到——病灶是席位競爭

**新增 [`eval/probe_multi_company_period.py`](eval/probe_multi_company_period.py)**（零 LLM、三臂、8 題人工構造多公司題，期碼從 source 檔名解）。刻意挑最新期碼**不同**的公司配對（AAPL `202606`／MSFT `202603`／NVDA `202604`），因為那正是 `MatchAny` 會漏的情境。

| arm | 錯期率 | 缺最新季的公司 | 缺失率 |
|---|---|---|---|
| `single_on`（陰性對照，生產） | 0.000 | 0/16 | 0.000 |
| `single_off`（**陽性對照**，`RQ_LATEST_QUARTER_ROUTING=0`） | 0.577 | 1/16 | 0.062 |
| `multi`（被測項） | 0.429 | **6/17** | **0.353** |

**損害成立**（0.353 vs 0.062），**但機制不是期別**：6 件損害逐件拆開，**kind A（同一家的舊季擠掉新季）＝ 0、kind B（那家公司連一個 10-Q 都沒進 top-5）＝ 6**。每個出現的舊季 chunk，那家公司的最新季**也**在 top-5 裡。→ OR-of-ANDs 直接修不到任何一件。

**真病灶**：top-5 分給 2~3 家，一家壟斷。`_ensure_ticker_coverage` 有保底但補的是**該家最高分 chunk、不管 doc_type 也不管期別**（實測補進 `AAPL_News`、`TSLA_Fundamentals`）。

**對照組解釋了為什麼單公司題看不出來**：`single_off` 錯期率 0.577 卻只有 6.2% 缺失（5 格全給一家，留得住）；`multi` 錯期率較低（0.429）反而 35.3% 缺失。**席位稀釋放大 5.7 倍**。

**損害範圍只在單發管線**（追加實測，8 題 → 17 個子問題）：agentic 的 Planner 把每一題都拆成單公司子問題，**17/17 只剩一家公司、17/17 路由都會觸發** → 這個破口在 agentic 上結構上不存在。是 **Planner 在上游消掉的，不是 Grader 補救的**——Grader 連上場機會都沒有。而 [`api_server.py`](api_server.py) 直接呼叫 `rq.retrieve()`、不 import agentic → **使用者實際在用的 web UI 就是有損害的那條路，且沒有第二輪**。

**方法論（兩條）**：
- 腳本第一版的判定邏輯只看聚合 `missing_rate` → 吐出「值得做 OR-of-ANDs」，**是錯的**。加上 kind A/B 分解後才看得出該修的是別的地方。**聚合指標說「有損害」不等於知道該修哪裡**——成因分類要寫進量尺本身，不能靠事後人工看。
- **量一個破口之前先確認它在哪條管線上發作**。probe 跑的是 `rq.retrieve()`＝單發路徑；沒有先驗 Planner 的拆解行為，就會把「單發管線的缺陷」誤報成「檢索層的通用缺陷」，並且對 agentic 做無用的修改。

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
