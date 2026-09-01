# BACKLOG — 未做 / 待決 / 已接受的極限

> **這裡放什麼**：還沒做的事、做了但沒定案的事、以及查清楚後決定**不修**的極限。
> **這裡不放什麼**：已完成的改動（→ `CHANGELOG*.md`）、知識與踩坑（→ `docs/`）。
>
> **維護紀律**：一件事做完就從這裡**刪掉**並寫進 `CHANGELOG*.md`；查清楚決定不做就搬到「已接受的極限」並寫下**復活條件**。
> 每一條都要能回答「**卡在哪**」——沒有卡點的條目不是 backlog，是願望。

---

## 量尺缺口（做別的事之前先看這裡：這件事現在量得到嗎）

- **RAGAS 的 `GOLD_BASELINE` 已在 65 題上重量，但 `NOISE` 只量過 1~2 對。**
  `NOISE["answer_correctness"]` 從 .007 改成 .012 就是因為第二對推翻了第一對（見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺重建〉）。`context_precision` 那格更不可信：新測 .005 vs 舊值 .046，而 .046 的成因（最高排名那筆判定翻面 → 整題 1.0→0.0）隨時會再發生，現在取的是兩者較大值。
  **卡點是成本**：要收斂門檻得在 `.venv-ragas` 多跑幾對同輸入重評。**引用任何 `NOISE` 常數之前，先看它是幾對量出來的。**

- **`check_number_defects` 全 PASS ＝ 沒有進度指標。** 零噪音量尺目前只剩護欄功能，任何生成層改動都「量不出變好」。
  做法：用結果檔的頭條百分比候選篩新缺陷，**人工讀完確認是真缺陷才登錄**（實測 6 個旗標有 3 個不是錯）。
  ⚠ 不要為了讓量尺有 FAIL 就把不確定的東西登錄成 `known_defect`。

- **`check_rounding_fidelity` 的統計效力太低下不了結論。** 改動前三個結果檔共 195 題只有 **1 筆**事件，所以「改完是 0」跟改動前的 0 長得一模一樣。
  **復活條件**：① 累積到事件數 ≥5；或 ② **造一批必然觸發的題**——gold 是 Fundamentals 的兩位小數比率、且同公司 10-K 有相近整數值的那種（`lex-17` 就是這個形狀）。
  ⚠ 那批題**只用來量這條規則**，不要併進 `eval_set.json`（分母再變一次，跨日分數就再也比不了）。

- **lexical 的缺口在 chunk 層，而 gold 只到檔名。** agentic 在 lexical 平均只承接 **1.93 個 chunk**（全類最低），而 lexical 正是 `context_recall` 唯一明顯輸單發的類別（−0.078）；但**檔案層命中 12/15 與單發完全相同** → 病灶是「同一個檔裡收太少／收錯 chunk」，不是撈錯檔。
  **卡點＝量尺**：要驗證得先有 chunk 層 gold，而 `eval_set.json` 的 `relevant` 只到檔名。
  附帶事實：`lex-03`／`lex-07`／`lex-14` 三題**兩條管線都撈不到 gold**，那是檢索層問題不是 agentic 問題。
  **2026-08-28 找到很可能的機制**（`probe_relevant_ids.py --repeat 3`）：Grader 的 `relevant_ids` 平均只圈選 **0.53** 的候選，而 `mix-01`／`mix-03` 都是 **0.33**（5 取 ~1.65）——**低承接數很可能就是這個欄位造成的**，不是檢索少撈。
  ⚠ **但「那有沒有害」正好被這條缺口本身擋住**：同一次量測顯示「gold 檔整個被排除」0 題、合成混池「誤選他家」0 題、圈選率 1.00 的題 0/8 → 在**檔名層**看不出任何損害。要分辨「它濾掉的是離題 chunk（正確）還是答案所在的 chunk（危險）」，**只能靠 chunk 層 gold**。
  → 這兩條原本分開的條目其實是同一件事：**先有 chunk 層 gold，才談得上要不要動 `relevant_ids`。**

- **live 路徑的量尺（`check_web_claims`，5 條）是 flaky 的，不能當閘門用。**
  實測 5 次跑分（2 個 collection × 有無 cache 污染 ＋ 1 次單題重跑）：`web-02` 的判定序列是 PASS,FAIL,FAIL,FAIL,PASS，`web-04` 是 FAIL,FAIL,FAIL,PASS。**同碼同 collection 會翻面。**
  根因不在斷言而在**輸入**：fixture 的 key 含 LLM 生成的英文 query 字串，每輪都可能不同 → `FixtureMiss` → 確定性 executor 的 `except Exception` 把它降級成「沒打 web」。
  ⚠ **重錄 fixture 不是解法**：只會換一組會再度 miss 的 key。
  **後半已於 2026-08-28 修掉**：`FixtureMiss`／`RecordError`／`ReplayCacheMiss` 移出 `Exception` 階層 → 任何 `except Exception` 都抓不到 → fixture miss 現在**當場炸、exit=1**，不會再被量尺報成「系統沒打 web」（閘門⑦，9 項，4/4 變異全抓到）。
  **剩下的那一半**：query 字面仍然綁死。實測同一題 web-02 在四輪裡生出四個不同的英文 query（`NVDA current stock price today` / `NVIDIA current stock price live quote` / `NVIDIA current stock price August 15 2026` / `NVIDIA NVDA latest share price June 2026`）。
  **方向**：讓 replay 的比對不綁 query 字面（按子問題 index，或對 query 做正規化／近似比對）。
  **在修好之前**：這 5 條只能當「跑幾次看趨勢」的 probe，判讀一律 ≥3 輪、看逐題 k/n。**好消息是失敗現在會自己現形**——不再需要人去分辨 FAIL 是系統還是量尺。

- **`llm_replay.py` 沒有唯讀模式，A/B 兩臂共用一份 cache 會單向污染。**
  `atexit` 無條件把 miss 現場算出的結果寫回 → **第一臂的 miss 變成第二臂的 hit**（實測 hit 23→32，兩臂結果因此不可比）。現有防護只有 `RAG_REPLAY_MODE=strict`（miss 就報錯），而 `record_web_fixture.py --mode replay` 沒設它——那支的「replay」對 Tavily 成立、對 LLM 不成立。
  **權宜做法**：每一臂用自己的 cache 副本（`--replay-cache <副本>`），跑完丟掉。
  **卡點**：正解是加一個唯讀旗標，但要先確認沒有哪個既有流程依賴「跑一輪順便補快取」。

---

## 觀察：multi_hop 的「比大小」目前沒有 Python 在做（量過了，沒有損害）

`agentic_rag_v2.py` 裡所有 `max()` 都在比日期或 rerank 分數，**沒有一行在比「哪家公司的指標大」**。
六個子問題各自撈回 chunk → `_fair_select` 挑一批 → 由 Generator 自己讀著數字比。
這違反 CLAUDE.md〈LLM 與 Python 的分工〉的「比對／算術給 Python」，而且**比錯了五道 validator 全綠**。

**2026-08-29 量了（`check_comparison_claims.py`，零 LLM／零網路，只讀既有結果檔）**：
· 現行架構 8 輪 × 5 題 = **40/40 PASS**，`wrong_winner` 0、`unfounded` 0。
· 擴到全部 67 個結果檔（335 筆）：**`wrong_winner` 仍然 0**；`unfounded` 6 筆，
  **全部出自舊 ReAct 世代**的 `gj_v2_multihop*`（其中兩筆答案確實是錯的：宣稱 MSFT 而真值 AMZN、
  宣稱 NVDA 而真值 META，而且四家的值一個都不在 contexts 裡）。與 ReAct 執行層退役的理由一致。
→ **「這是最大暴露面」這個假設沒有證據支持，據此撤回。** 不補 Python 比較器。

**⚠ 但這個結論的適用範圍很窄，復活條件寫在這裡**：
現有語料**每一題的差距都很大**（NVDA 85.2% vs META 33.1%、GOOGL 160.21B vs MSFT 125.22B），
而且值全部來自**同一種 chunk、同一個版面、同一個單位**。**三種真正危險的情況一次都沒測到**：
① 接近值（差 1% 以內）② 某家的值缺席 ③ 跨單位（$XB vs $XM）。
任一情況在題庫裡出現、或有人把 Fundamentals 的版面改掉 → 重新評估。
造這三種題**不能加進 `eval_set.json`**（分母不可變），要走獨立斷言集。

## ⚠ `eval/web_fixture.json` 已經不可重放，必須重錄（2026-08-29 查清）

**這份 fixture 是 live web 那條路唯一的自動化證據，而它從 2026-08-19 起就對不上系統了。**
症狀先前被當成「LLM 不穩」：同一題五輪 PASS/FAIL 亂跳（web-02 {P,F,F,F,P}／web-04 {F,F,F,P}）。
逐層查下去是**三個獨立成因疊在一起**，由重到輕：

1. **fixture 錄在「KB 還有新聞」的時代 → 結構性不可重放。** 它 as-of **2026-08-15**，而 KB 於
   **2026-08-19** 拔除新聞。快取裡的 `check` key 逐字引用 `TSLA_News_20260721_01.txt`、
   `GOOGL_News_20260721_01.txt`、`NVDA_News_20260519_01.txt`——**那些 chunk 現在任何 collection
   裡都不存在**。這一項換 collection 也救不回來。
2. **`_meta` 的綁定被每一次 replay 覆寫（已修）。** `note_meta()` 舊版無條件寫 → 那一格記的是
   「上次誰跑過」不是「錄在什麼條件下」。所以 2026-08-27 生產從 `..._mdna` 換到 `..._multiyear`
   之後，fixture 每跑一次就自動宣稱綁在新 collection 上，**不一致這件事自己把自己抹掉了**。
3. **`replan` 沒進重放快取（已修）。** 即使前兩項修好，replan 每輪重抽仍會生出措辭不同的 todo
   → 新的 `check` key → 新的 `new_query` → 新的英譯 → 新的 Tavily key。快取裡直接看得到爆炸的
   原始證據：`改用網路搜尋查…最新新聞`／`…的重要消息`／`…（2026-07-22至2026-08-15）`／
   `使用網路搜尋查找…` 五個近義待辦。

**已經做完的**：②③ 都修了，並各配一道閘門（⑨／⑧）；`--mode replay` 預設對
`plan,replan,translate_en,check` 嚴格；開跑前比對 fixture 綁的 collection 與 as-of。
現在跑會**當場 exit=2 並說清楚原因**，不再安靜跑出一個假數字。

**還沒做的（要你決定）**：重錄一份綁**現在這個 collection**、**新的 as-of** 的 fixture。
· 成本：連網 ＋ 燒 Tavily 與 LLM 額度，5 題。
· ⚠ 重錄後 [`eval/web_claims.json`](eval/web_claims.json) 的斷言**要逐條重新確認**——它們斷言的是
  「答案裡的報價在 fixture 原始內容裡逐字出現」，換一份 fixture 就換一組數字。
· ⚠ 重錄時 `--mode record` 會連網，**別在同一次動到 `web_fixture_news37.json`**（那份 as-of
  2026-08-20，是另一組斷言）。
· 重錄後這裡的 strict 可以從 `strict:plan,replan,translate_en,check` 收緊成 bare `strict`
  （`period_intent`／`ticker`／`ratio` 三個接點比舊 fixture 新，現在放寬是為了避開假陽性）。

⚠ **在重錄之前，`check_web_claims.py` 的任何結果都不能當證據。**

## `_WEB_TODO_RE` 的詞表漏洞（查清楚了，刻意沒修）

`_WEB_TODO_RE = re.compile(r"網路|上網|web\s*search|internet")`。2026-08-29 實測 replan 生出的
web 待辦有一整族**它匹配不到**：

    在 Yahoo Finance 上查詢 NVDA 當前股價
    在 Bloomberg 上查詢 NVDA 當前股價
    在 MarketWatch / Reuters / CNBC 上查詢 NVDA 當前股價
    使用NASDAQ官方網站或API查詢NVDA即時股價        ← 「網站」不是「網路」

**沒修的理由**：它現在只剩一個消費端——`_node_replan` 在 snapshot／`--no-web` 時拒絕 web 待辦。
那是**成本**問題不是隔離問題（`verify_web_gate_isolation` 閘門① 證明隔離只靠 `freshness_mode`
與 `ENABLE_WEB_SEARCH` 兩個獨立條件，詞表**不是**隔離機制）。漏掉的代價是 snapshot 多跑一個
註定撈不到東西的待辦，不是資料外洩。

⚠ **不要用「加幾個詞」修它**（`網站|Yahoo|Bloomberg|…`）——那正是 CLAUDE.md〈硬編碼詞表是警訊〉
說的 O(n) 開始，而且站名清單天生無界。真要修就改成 LLM 判定，**但要先看那條警告**：
「改成 LLM 之後，判別力來自『LLM 說不是就必須不是』——若空答案會掉回詞表，詞表仍然是實際
做決定的人，而端到端跑分看不出任何差別」。

**復活條件**：這個詞表出現第二個消費端、或 snapshot 的成本變成問題。
（`_web_retry_is_pointless` **刻意不使用它**，就是為了不繼承這個洞。）

## 待決的決策

- **rubric 這條線要正式退役，還是把 rubric 補回 `eval_set.json`？**
  實測 65 題**一題都沒有 `rubric`**（`git log -S` 查過：從 100 題定版那次就沒有），結果檔的 `correctness` 全為 null。全 repo 只有 `eval/eval_set_agentic.json` 5 題帶 rubric，而讀它的 `eval/eval_agentic.py` 是死碼（見下）。
  ⚠ **這不表示該刪**：生成品質改由 RAGAS `answer_correctness` 負責是刻意的（見 `eval_generation_llm_judge.py` 檔頭 2026-07-23）。要決定的是退役還是復活。
  **在決定之前不要再往 judge prompt 加規則**——沒有活的消費端，加了也量不到。

- **judge 判不了「捏造」，因為它看不到來源。** `_CORRECTNESS_FEEDBACK_SYSTEM` 的 FABRICATION SCOPE 寫著「a SPECIFIC number … **that is not grounded in the sources**」，而 `evaluate_correctness_with_feedback` **沒有任何參數會拿到 chunk**。於是這條規則只能靠合理性判：九次觀測下 `col01`（市佔率 92%）7/9 抓到、`sem03`（管理層預期 45% CAGR）**1/9**——離譜到光看就站不住的抓得到，讀起來合理的抓不到。
  ⚠ **不建議把 chunk 塞回 judge**：groundedness 這一軸已由 RAGAS faithfulness 負責，那正是當初移除的理由。真要修該修**題目的 mn checkpoint 措辭**（改成 judge 拿得到的資訊判得出來的形狀）。與上一條綁在一起決定。

- **`eval/eval_agentic.py` 是死碼，要刪還是標退役。** 它 `--module` 預設 `agentic_rag_mix`，而 `agentic_rag_mix`／`agentic_rag_nv` **在 repo 裡都不存在了**；呼叫 judge 的地方也帶著已被移除的 `hall_result=`／`relevance_score=`。
  ⚠ **刻意沒跟著修**：修好 kwargs 還是跑不動（import 的 module 不存在），半修反而讓它看起來像活的。

- **`eval/replay_cache.json`／`web_fixture*.json`（359 KB）要不要納版控。**
  **傾向要**：它跟 `reference_answers.json` 是同一種東西——不需要唯一正確，只需要**固定且對所有組態一視同仁**，而且沒有它 `check_web_claims.py` 的斷言一條都跑不了。卡點只有檔案大小。

- **複雜度 router**（簡單題→單發、複雜→agentic）。保守偏 agentic（誤判複雜為簡單代價高）。
  ratio 路由屬**正交檢索提示、非第三分支**，該放共用檢索層讓兩條管線都吃到。**卡點：measure-gated，目前量尺分不出。**

- **生產 `rag_query.py` 是否跟進 `full_translate_en=True`**（agentic 已固定開）。卡點：要先確認同樣的 chunk-level rerank 平坦問題在單發管線也存在。

- **一致性 validator 的重寫路徑**：偵測 4/4 精準，但重寫實測 **2 好 1 壞**（`col-11` 修掉矛盾卻把總營收誤標成雲端營收，且三層驗證全過）。選項：改成**只偵測不重寫**（把矛盾標記給使用者看）以拿掉那個 1 壞。

- **Grader 的責任剝離（評判／重寫／圈選／時效分類拆成不同呼叫）要不要做。**
  現況：`_check_sufficiency` **一次 LLM 呼叫**吐 `{sufficient, missing, new_query, relevant_ids}`，
  live 再加 `realtime_need`；下游四個消費端全部吃它（下一輪檢索 query、送 Tavily 的 query、
  哪些 chunk 進承接池、要不要打 web）。
  **2026-08-28 逐項查過，結論是現在不做**——當初主張要拆的三個理由各自被自己的量測削弱：
  ① `realtime_need` 誤判 → **是 prompt 問題不是架構問題**。只改 live 專屬區塊三句話後，
     「NVIDIA 最近有什麼新進展？」0/3 → **5/5**、「蘋果最近有什麼消息？」1/3 → **5/5**，
     四題陰性對照一格不動。成本幾小時，拆架構是幾天而且拆完仍要寫這幾句話。
  ② 「`relevant_ids` 才是真防波堤、最該先拆」→ **沒有證據，假設撤回**。
     `probe_relevant_ids.py` 8 題×3 輪：`gold 全滅` **0 題**、合成混池 `誤選他家` **0 題**、
     圈選率 1.00 的題 **0/8**（平均 0.53、空圈選 0/24）。它確實在工作，且檔名層量不到損害。
  ③ `new_query` 讓機器腦補的年份被說成「所詢問」→ **已由閘門 ⑮f／⑮g 從歸因面結構性堵掉**
     （歸因掛在 todo 的出身 ＋ 輪次），不必動改寫的歸屬就解決了。
  ⚠ **仍然站得住的理由只剩可觀測性**：一次呼叫吐五個欄位時，失手了不知道是哪一個。但 08-28 的
    兩次診斷都靠 `AGENTIC_TRACE=true` 在同一個 session 內分辨出來，所以收益目前是「省診斷時間」，
    不是「否則查不出來」。
  ⚠ **反對的證據要一起記**：把 query 改寫權交給**有 agency 的節點**實測更差——乾淨 decomposition
    的 gold-chunk 覆蓋 **35/75** vs 真實 ReAct **16/75**（見 `_run_executor_deterministic` docstring）。
    而把改寫路由給 Replanner 會直接餵養已記載的子問題爆炸級聯（`QUERY_WEB_BUDGET` 上方註解：
    「改用網路搜尋查…」→「即時網路搜尋…」→「使用即時金融網站…」，實測 7 次 web／近一小時一題）。
  **復活條件（任一成立就重新評估）**：
  · `realtime_need`／`sufficient` 在**另一個沒測過的措辭族**上再失手一次 → 打地鼠開始了，
    那才是「責任壓太多在同一次呼叫上」的證據；
  · chunk 層 gold 建起來後，`probe_relevant_ids` 量到「被濾掉的是答案所在的 chunk」；
  · 子問題爆炸級聯在生產再現。
  **真要做時的形狀**：拆成**獨立的單一職責 rewriter 呼叫**，**不是**把改寫還給 Planner／Replanner
  （理由就是上面那個 35/75 vs 16/75）。

- **`find_claim_conflicts` R2 放寬**（`len(seg_changes) >= 2` → ≥1）。卡點：要先量誤報率——「總計 < 某部門」在另一部門衰退時是**合法的**。

---

## 已知缺陷（查清楚了、還沒修）

- **多公司題沒有期別保護。** `_resolve_period_filter_llm` 遇到兩家以上直接回 None，因為 `build_qdrant_filter` 吃 flat AND list、**結構上寫不出 per-ticker 的 OR-of-ANDs**（`MatchAny` 是全域 OR，會讓 A 公司的舊季通過 B 公司的期碼）。正確結構是 `should=[ must=[ticker==AAPL, period==202606 or empty], must=[ticker==MSFT, period==202603 or empty] ]`。
  **卡點是表達力**，要先讓 `build_qdrant_filter` 多接受一種 per-ticker 群組型別。
  **損害範圍只在單發管線**（實測）：agentic 的 Planner 把 8 題多公司題拆成 17 個子問題、17 個都只剩一家公司 → 席位競爭在 agentic 上結構上不存在。而 `api_server.py` 直接呼叫 `rq.retrieve()`，**使用者實際在用的 web UI 就是有損害的那條路**。
  ⚠ 真病灶是**席位競爭**不是期別：實測 6 件損害 **kind A（同家舊季擠掉新季）＝0、kind B（那家公司連一個 10-Q 都沒進 top-5）＝6**。`_ensure_ticker_coverage` 有保底但它補**該家分數最高的 chunk**（實測補進來的是 `AAPL_News`、`TSLA_Fundamentals`），答不了「最新一季營收」。
  **收斂後的修法**：補位時若本次檢索帶 `routed_latest` 意圖，優先挑該公司**最新期碼的 10-Q**。⚠ `eval_set` 量不到（0 題）——這是產品修正不是分數修正，驗收用 probe 的 `missing_rate 0.353 → ?`。

- **`realtime_need="none"` 會短路整段時效日期算術。** `REALTIME_STALE_DAYS["none"] is None` → `_stale_for_realtime` 第一行就 return → `_kb_ceiling_date()`／`kb_unfixable`／天花板比對**整段不執行，連 `as_of` 都沒讀到**。等於「KB 補不了就去 web」被一個三分類的 LLM 欄位單點守住，而 Grader 對「某季營收」填 `none` 是照 prompt 做對的。
  **2026-08-28 量到了危險的那一半**（`probe_realtime_need.py`，生產 collection）：「NVIDIA 最近有什麼新進展？」**0/3** 判成 `none`（穩定地錯，而它刻意不含「新聞／消息」字樣＝措辭泛化的形狀）、「蘋果最近有什麼消息？」**1/3**。intraday 兩題與四題陰性對照全對 → **病灶只在「近期動態」這個措辭族，不是整個分類器**。
  **同日改 prompt（只動 live 專屬區塊）後 `--repeat 5`：兩題都 5/5，四題陰性對照一格不動、跨輪不穩定 0 題。** 加的三條規則：① 候選片段全是財報**不構成**填 `none` 的理由（「財報答不答得了」與「問題需要多新」是兩件事）② 「最新一季／上一季」指**財報期別**不是 wall clock，仍然填 `none`（這條是保護陰性對照的，少了它會判過頭）③ 判不出來填 `days` 不填 `none`（代價不對稱）。
  ⚠ **這條沒有因此關閉。** 修的是分類器在一個措辭族上的準確度，**不是那個單點依賴**——「KB 補不了就去 web」仍然由一個三分類的 LLM 欄位單獨守著，換一種沒測過的措辭仍可能失手。要移除單點依賴得動架構（把 `realtime_need` 從 Grader 那一次呼叫裡拆出來），而那要先有 §量尺缺口 裡說的前提。
  **為什麼沒修：目前測不出來。** KB 裡沒有任何一家的 filing 天花板超出申報週期，這條路徑在現有資料上永遠不會觸發，改了也沒有陽性案例——那就是又一個「聽起來合理」的機制假設。
  **復活條件**：① 有一題的 KB 天花板真的落後於申報週期（換 as-of 或補一家新公司都行）；② 先寫出能證偽它的真值表（`period_ref` × 天花板 × as_of），加進 `verify_web_gate_isolation.py` 閘門⑤。
  ⚠ 不要用詞表判「這題是不是相對期間指稱」。可行方向是 Grader 加一個與 `realtime_need` **正交**的 `period_ref` 欄位，但**只能加在 live block**：動到 snapshot 的 Checker prompt 會破壞 eval 基準逐字不變。

- **`MSFT_10K_2024.html#158` 幅度接地在 chunk 層漏一筆（1/1009＝0.1%，帶著上線）。**
  `#158`「General and administrative expenses increased **slightly**」不帶幅度，數字在 `#157`。把兩塊存檔文字餵回判斷式重放，**兩個合併條件都是 True** → 量尺沒錯，是真的「併得起來卻沒併」。
  **卡點＝根因要重跑切塊器才定得了案**：`_merge_small_chunks` 是**逐硬邊界呼叫**的，而閘門只看 `chunk_index - 1`（會跨邊界）→ 兩者對「相鄰」的定義不同。三個可能：(a) 兩塊當時分屬不同 segment（那是量尺問題）(b) 併了之後被 RCTS 上界再切開 (c) `#158` 是 segment 首塊、204 字元只比 `_MIN_CHUNK_CHARS=200` 多 4 個字元。
  **復活條件**：下次有正當理由重跑 ingest 時（不要為了 0.1% 專門跑一次三小時重建）。⚠ 升生產**不需要**重跑 ingest，所以那次沒有觸發這個條件。

- **live 路徑仍會出貨少數指向不存在來源的引用。** 確定性還原 `Reference N → allowed_chunks[N-1]` 已修掉大部分（真實樣本 5 題有 3 題完全清乾淨），但**兩個守門條件擋下的那些刻意不動**（序號越界／`chunk #M` 與第 N 筆不一致）：走原本的重寫路徑，重試用完仍會原樣出貨。
  **這是已知且刻意的極限**——猜錯比不修更糟（把「無法追溯」變成「看起來可追溯的錯引用」）。

- **重述（restatement）無法偵測。** 10-K 會因改分部結構／會計政策**重述前一年數字**，於是同一個事實在新年報裡是另一個值（實測 `MSFT_10K_2026` 的補充未審計表帶著重述後的 `219,790`／`87,464`，而原始值是 `211,915`／`105,362`）。**值不存在 ≠ 事實不存在。**
  這與「`filing_date` 沒進 payload → 沒有 transaction time」是同一個根（雙時間軸）。目前沒有任何一般性機制。

- **「證據尾巴」在 consumer 側仍有四份定義。** `eval/check_number_defects.FOOTER`、`eval/check_rounding_fidelity._TAIL_RE`、`eval/eval_ragas_vs_rubric` 的 footer strip、以及 `run_agentic_on_evalset` 那份。
  正式定義已收攏成 `rq.strip_evidence_tail()`，**新消費端一律用它**；**producer 側已經乾淨**（`_compose_answer_tail` 是唯一產生點，由閘門⑬c 鎖住）。
  **卡點**：動 RAGAS 那份會移動分數，要單獨做並重新取基準。

- **Planner 對「KB 結構上不可能有」的題仍白搜一輪。**
  ⚠ **不要讓 Planner 自己判**：它手上只有 coverage 摘要、沒有真實 chunk，只能用猜的；且猜錯的代價不對稱（猜「沒有」但其實有 → 整題答不出來），所以架構刻意把判斷延後到 Grader 看見真實候選之後。
  **剩餘收益已經很小**：`kb_unfixable` 已把浪費從 21 輪壓到 4 輪，而那 4 輪是每個子問題各 1 次首輪檢索＝必要的。除非量到具體損害否則不做。

- **KB 真的沒有資料時，replanner 仍會生一串同義待辦。** `kb_unfixable` 只涵蓋**時效**這一種「KB 補不了」；當不足的原因是 KB 根本沒有那個數字時旗標不會亮，於是 replan 一路加到 7 個待辦。
  **成本已經不同了**：web 被 `QUERY_WEB_BUDGET` 封在 3 次，多出來的只有本機檢索（免費、秒級），所以優先度低。
  ⚠ **不要用字串相似度認同義待辦**——實測分離度是負的（正向最低 jaccard 0.04、負向最高 0.50）。
  可行方向：把 `kb_unfixable` 從「時效」推廣到「同一子問題連續兩輪 `missing` 沒有實質變化」——但那是啟發式，要先有能證偽它的測試。

- **web 結果的外國掛牌「路徑」擋不到。** `_host_allowed` 擋掉了地區**子網域**，但同一主機下的外國掛牌**路徑**擋不到——實測 `stockanalysis.com/quote/bvl/AAPL/market-cap`（利馬交易所）$4.98T vs 美股頁 $4.45T，**同一天差 12%**。
  ⚠ **不要用路徑詞表擋**：那是各站自訂的 URL 慣例，站方改版就失效，且新增一個來源就要重讀它的 URL 結構＝O(n) 白名單那個病。
  **對照：同類但已解的是選擇權合約頁**——那有 OCC 標準格式可比對，屬**格式定義的封閉集合**；外國掛牌路徑**沒有**跨站標準，差別就在這裡。
  **現況風險有限**：`TAVILY_PER_DOMAIN_CAP=2` 讓同站最多兩則，美股主頁通常 rerank 較前，但沒有保證。

---

## 觀察（記著，但刻意沒有登錄成斷言）

- **兩題「打了 web 卻沒引用」尚未判定。** `mh-07` 打 3 次 web、答案誠實說「新聞稿中並未出現」；`mh-09` 改用 KB 近似值推論身分並自己標明「現有文件未直接披露」。
  **沒有登錄**：web 有沒有撈到可用內容**不在系統控制內**，斷言它等於把外部世界寫進量尺。要判定得先人工讀 fixture 裡那幾筆回應到底有沒有答案。

- **Fundamentals 在口語問法下擠不進 top-k。** 同一支探針測四種問法問 MSFT 營收成長率：「…表現如何？」`#0` 在 rank 5、「最近一年的營收年增率（YoY revenue growth）」rank 1，但口語的「微軟的營收成長率是多少？」**Fundamentals 完全沒進 top-5**。
  **沒有登錄**：10-Q 的季度成長是合法的另一種口徑，判它錯等於用斷言獎勵一個沒有根據的偏好。
  **卡點是這個判斷本身**：要先決定「使用者沒指定口徑時，系統該給 TTM 還是財期」——那是 eval 設計問題不是檢索問題。**決定之前不要動檢索。**

- **補進 Fundamentals `#0` 之後那 6 題的 correctness 平均 −0.029，需要逐題人工複審。**
  假說：`#0` 塞滿 TTM 比率，可能誘使模型在 **gold 要的是財期值**的題目上改答 TTM。
  ⚠ **不要拿這個 −0.029 當結論**：n=6，而單題波動輕易到 ±0.3（同一輪裡 `col-11` −.46、`col-08` +.45，兩題都在碼路徑之外）。聚合分數在這個樣本數下沒有判別力。
  **要做的是逐題人工複審那 6 題**（`mix-01`／`mix-06`／`mix-08`／`mix-10`／`lex-17`／`col-12`）：看「gold 的口徑是財期還是 TTM」與「答案用了哪一個」。若 gold 本身要財期值，那是 eval 設計問題；若答案確實被 `#0` 帶偏，修法是**在補撈的 chunk 上標明口徑**，而不是不補。

---

## 已知缺陷：mix-07（Plan 把混合題譯成純新聞查詢）

**真因**：Plan 節點把「Wiz 收購金額」這個**財報事實**譯成新聞查詢 → 檢索被限制在 News chunk → 財報裡的 $29.5B 撈不到。六個封存 run 裡「只撈到 News」與拒答 **6/6 完全相關**，是乾淨的預測指標。跨 collection 命中率沒有差別，**兩邊都被 plan 變異主導**。

⚠ **掛了 replay fixture 也擋不住它**：replan 產生的新 query 不在快取裡，會重新問 LLM。所以 mix-07 **不能當 collection A/B 的判準**，只能當 planner 改動的判準。

⚠ **KB 拔除新聞後它已 2/2 從 FAIL 翻成 PASS**（`I don't have enough information` → 答出 $29.5B），那是拔除新聞唯一零噪音量到的收益。但成因（plan 變異）沒有消失。

**已登錄成斷言**：`kind: require_text`，`expect_text: 29\.5\s*billion|295\s*億`。舊的 `anchored_pcts` 判不出它（拒答的答案裡沒有百分比 → N/A 而不是 FAIL）。

**卡點**：這是 plan 抽樣變異，修法可能要在 planner prompt 加「財報事實不要譯成新聞查詢」的約束，但那類 prompt 改動的效果在現有量尺下量不出來。**現在至少有零噪音的進度指標**：拒答比例降下來才算有動。

---

## 已知缺陷：`lex-17`（ratio 題的殘留）

`mi-05`／`lex-17` 的**檢索半邊已修**（確定性補撈 Fundamentals `#0`），**詞表閘門與生成端四捨五入也已修**。剩下的是：

**開頭第一句仍以 10-K 財年 18% 當結論**，而 gold 要的是 TTM 18.30%。⚠ 另外 `anchored_pct` 現在是靠 `expect_text` 析取才 PASS（anchor 抓到的仍是 18%）——**它和 `require_text` 幾乎重複，名字說的事情已經不再量了**。這是量尺放寬的常見代價：加一條析取讓它更容易通過，判別力就從那一條身上轉移走了。

**卡點與 `mix-07` 同型**：它的觸發取決於 Plan 產生的子問題，而 planner prompt 類改動的效果在現有量尺下量不出來。判讀**必須跑 ≥2 輪看 FAIL 比例**（實測同一份碼 r1 全 PASS、r2 逐字重現原症狀）。

---

## 環境雜務

- 刪除過期的 Qdrant collection（**保留 `us_stock_rag_edgar_exp4` 當歷史基準**）。
- `git prune`：目前有過多 unreachable loose objects（每次 commit 都會警告）。

---

## 已接受的極限（查清楚後決定不修；要動請先滿足復活條件）

| 項目 | 真因 | 復活條件 |
|---|---|---|
| **切塊／ingest 不再改** | **量尺飽和**：六個 RAGAS 指標五個已達或超過 gold 上限；檢索全修到完美只值 +0.024，低於 0.067 噪音底線。詳見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺飽和〉 | 換沒飽和的量尺（擴充 `check_number_defects` 覆蓋率），或改攻生成層 |
| **KB 不收新聞** | 新聞的正確性判準（夠新＋來源可信）與本系統的每一層機制（期別＋引用＋數字可驗）正交；實測它佔 2.5% 語料卻造成最大的殘留檢索失敗，且 28 條來源標記有 17 條不合本專案自己的 web 白名單標準 | **不是「把舊語料放回去」**。①路由已驗（37 題觸發 web ≥35/37、陰性對照 0/7）②內容已驗（PASS 35／FAIL 5，5 個 FAIL 全是上面那條 live 引用缺陷）。→ **沒有任何一項證據支持把新聞加回 ingest** |
| **排序層加時間衰減** | 全域 decay 假設「越新越好」，而加入舊資料的目的正是要能回答歷史題。`relative` 那類已證明**期間解析得到時根本不需要動排序層**（兄弟密度漲 4 倍、指標一格未動）。LangChain `TimeWeightedVectorStoreRetriever`／LlamaIndex recency postprocessor／ES decay function 之所以通用，是因為場景是**沒有明確指定期間的新聞流** | 提得出「只在期間無法解析時生效」的版本 ＋ **歷史題陰性對照**證明沒把它們弄壞 |
| **跨期近重複去重／MMR（修法 C）** | 殘留的排擠是**定義出來的**不是缺陷：`k2_newest` 剩下的 48 席全部是「組內恰好 2 份 filing」＝`keep=2` 刻意允許的第二席，**沒有任何一組 >2 ＝ collapse 零漏抓**。再用 `crowding_seats` 追它，量到的只會是 keep 值本身（收益指標與規則同定義的套套邏輯）。而那第二席不是浪費：`keep=1` 讓趨勢題期別覆蓋 −41% | 要有一把**不是 metadata 分組**的尺（內容層近重複）。而本專案量尺已飽和、沒有能調 λ 的尺 |
| **曆年 vs 財年的年份歧義** | `fiscal_year` filter 現在只認財年，於是「Microsoft **2025 年**的季報」（曆年語意）會降級到 Tier 2。實測**內容沒變差**（前三名仍是 `MSFT_10Q_202512`／`202603`），變的是多一句揭露語 | 要真正解掉得讓 `parse_query_filters` 講出「這個年份是財年還是曆年」——那是動 LLM prompt、會動到 replay 基準與 65 題的檢索結果，**目前判斷不值得** |
| **期別探針的 gold 不再細修** | 人工複審：**46/49 的 gold 就是該公司最新那一份**，而跨期 collapse 的規則正是「留最新」→ `gold@k` **結構上偏袒它**；3 題「gold 不是最新」的陰性對照經人工讀原文**全是假警報** | ⚠ **偏袒現在跑在生產 collection 上**。復活條件是「下一次要用 `gold@k` 的差值做決定時」——那時它不再是背景說明，是會左右結論的偏差。要修就得重寫 gold 到 **chunk 粒度並標註「哪些期別同樣可接受」**，那是重做題庫等級的工 |
| **`faithfulness` 不再追高** | 這是**負空間**：把 gold 當答案餵回去只拿 0.659，多數題輸給系統。往上推等於要求系統比標準答案更保守 | 換 metric 才談 |
| **`us_stock_rag_edgar_exp4` 的 Fundamentals 比率仍是小數** | exp4 ＝ 歷史基準，早就不是任何入口實際查的東西 | 若日後要拿 exp4 當對照臂，得先跑 `migrate_fundamentals_pct.py`，否則 Fundamentals 類題目的差異分不清是切塊還是單位 |
| **`sem-08`**（AMZN AWS 策略方向） | 三種系統側修法皆敗於同一真因：生成模型判定「獲利數字」與「策略方向」問法無關而主動略過，**非訊號埋沒** | 只剩調整 rubric 或維持現狀 |
| **`col-07`**（否定框架） | 病根是「非龍頭」vs「minority share」的**框架落差**，不是翻譯；`full_translate_en` 已開仍漏。只有已知答案字面（＝HyDE）才撈得到 rank 1 | 不為單題上 HyDE（全管線改、有反傷） |
| **`col-08`**（Tesla 口語版）／**`col-05`**（AWS 口語版） | dense rank 25/106，落差超出 rewrite 射程 | 有新召回機制才重試 |
| **`col-04`**（Azure 口語版） | 關鍵內容在 top-5，生成被口語框架帶偏、選擇誠實拒答。**非 bug** | 觀察中 |
