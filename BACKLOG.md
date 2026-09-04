# BACKLOG — 未做 / 待決 / 已接受的極限

> **這裡放什麼**：還沒做的事、做了但沒定案的事、以及查清楚後決定**不修**的極限。
> **這裡不放什麼**：已完成的改動（→ `CHANGELOG*.md`）、知識與踩坑（→ `docs/`）。
>
> **維護紀律**：一件事做完就從這裡**刪掉**並寫進 `CHANGELOG*.md`；查清楚決定不做就搬到「已接受的極限」並寫下**復活條件**。
> 每一條都要能回答「**卡在哪**」——沒有卡點的條目不是 backlog，是願望。

---

## 未換完：三支 eval 腳本的預設仍指向已退役的模型（2026-09-03 起）

`openai/gpt-oss-120b` 於 2026-09-03T08:00Z 被 NVIDIA 退役（`410 Gone`）。
**生產側五個定義點當天已全數換成 `nvidia/nemotron-3-super-120b-a12b`**
（`rq.DEFAULT_MODEL`／`DEFAULT_GEN_MODEL`、`CHECKER_MODEL`／`GEN_MODEL`／`RETRIEVAL_MODEL`），
選型證據 `experiments/_model_bakeoff_20260903.log`——**生產管線是活的**（2026-09-04 端到端確認）。
**沒換完的是 eval 側，而且每一支的卡點都不同**：

- `eval/eval_ragas_vs_rubric.py:65` `DEFAULT_RAGAS_MODEL` — **卡在「換成誰」，不是「換不換」**。
  ⚠ **不要寫成「留著是為了保住可比性」**（2026-09-04 一度這樣寫，是錯的）：舊 judge 已經
  410，**產不出任何新數字**，可比性在退役那一刻就沒了。留著死常數保不住任何東西。
  ⚠ **也不要順手改成 `nemotron-3-super`**——那正是現在的 `GEN_MODEL`，而
  `eval_generation_llm_judge.py:54-57` 記著 `judge_regression.py` 的 11 案例實測：
  `gpt-oss-20b` 10/11 vs `gpt-oss-120b` 9/11，**後者被判掉的理由之一就是「與 gen_model
  同一支（自評偏誤）」**。judge 與 generator 同源是這個 repo 量過並否決的形狀。
  ⚠ 2026-09-03 的 bake-off **只量了三個結構化角色**（plan／check／filter），判準是輸出
  穩定性與延遲——**它沒有量判定品質**，外推到 judge 沒有根據。
  **可行起點**：`gpt-oss-20b` 還活著、且已是 correctness judge 的預設；換過去之後跑
  `judge_regression.py`（`--repeat 3` 以上、看逐題 k/n）確認判別力，再重跑一個錨點基準。
  📌 **correctness judge 沒有壞**：它 2026-07-19 就換成 `gpt-oss-20b` 了。死的只有 RAGAS judge
  ——原條目寫「判定角色一起斷」是把兩個 judge 混為一談。
- `eval/gen_reference_answers.py:38` `GEN_MODEL` — 參考答案的生成模型。**這支該換**
  （不換的話第一次跑就 410），換本身安全：現有 `reference_answers.json` 已納版控，
  不重生成就不會被動到。
  ⚠ **真正的代價是 gold 混血**：現有 65 題是舊模型生的 ＋ 24 處人工校正；換模型後補新題，
  gold 就變成兩代模型的混合物。要嘛接受並記錄哪幾題是新的，要嘛全量重生成＋重做人工校正。
  ⚠ **任何重生成都要帶 `--ids`**：`--force` 不帶它會洗掉那 24 處。
- `eval/ablate_retrieval_model.py:61` `MODEL_BIG` — 檢索側換模型的零噪音對照。
  ⚠ **這支現在整個跑不起來**（大臂指向 410 的模型）。它正好是下面那條「拆開換」需要的
  harness，要用之前得先把兩臂重新定義。

**範圍已確認完整**（2026-09-04 全庫 AST 掃描，只認真正的字串常值、排除註解與 docstring）：
live 的就是上面三支。另有 `eval/diagnose_crit_miss.py:32` 與 `eval/rejudge_with_model.py:73`
也指向死模型，但那兩支是**已退役的舊 harness**（CLAUDE.md 檔案地圖明列不維護）→ 不修。
⚠ 用 `grep gpt-oss-120b` 會撈到 19 個檔，其中絕大多數是**記錄當時選型理由的註解**，
那些是正確的歷史、不該改（`eval_ragas_vs_rubric.py:229` 就是一例：它記的是舊常數綁在
舊 judge ＋ 100 題上，改掉反而抹掉可比性的警告）。

**仍然成立的三條判準**（換任何一支之前先讀）：

- ⚠ **跨 2026-09-03 比任何跑分都是無效的**：`experiments/` 下所有結果檔與
  `eval/web_replay_llm*.json` 的**生成端**產物都失效（中間產物有快取、生成沒有）。
- ⚠ **五個角色不必然要用同一個模型**。現在是同一個純屬歷史。
  判定側換模型會動到 judge 基準，檢索側不會 → **拆開換 ＝ 兩個獨立的 A/B，不要綁在一起做。**
- ⚠ 「**LLM 太小導致生成錯誤**」那個假設仍然沒被量到。**要先有生成端的消融 harness**
  （見上一項的 `ablate_retrieval_model.py`），否則量不出來。

---

## 量不到：先斬後奏的「億」發生**頻率**（2026-09-03；接線已修，見 CHANGELOG）

修法已完成（`rq.finalize_answer_units` 三層 ＋ 閘門⑲ 26 項，2026-09-04 補證據 B）。
**剩下的是一個量尺缺口**：

- **新模型犯這個錯的頻率是否高於舊模型，目前沒有答案。**
  目前唯一的數據點是 2026-09-04 端到端的 **4 次跑觀察到 1 次**（間歇，非零）——樣本太小，
  只夠說明「不是一次性的」，不夠比較兩代模型。
  `X 億美元（$Y million）` 這種**生產契約自帶配對**的形態可以零歧義驗，但**它量不到這個病**——
  既有 147 個結果檔 4543 份答案掃出 **1578 組緊配對、1578 組全對**，因為那些是
  `convert_usd_units_to_yi` **產生的**，不是 LLM 寫的。它量的是「後處理有沒有壞」。
- 要量「LLM 自己寫了幾次億」，判準是 `repair_yi_against_source` 的三條件（需要來源在手）。
  **可行做法**：讓 `finalize_answer_units` 在 `verbose` 時把剝掉／修掉的筆數寫進結果檔，
  跑一批就有分母。目前它只 print，沒有落地。
- ⚠ **不要用「取同句最近的數字」當配對法**：實測它把期別碼 `202512`、年份 `2026` 當成配對值，
  44＋84 筆**全是誤報**。這是 2026-09-03 那一輪的第六次量尺失效。

**復活條件**：有人要主張「該換回別的模型」或「該加強 prompt」時——那兩個主張都需要這個分母。

### 已接受的極限：證據 B 的殘餘誤報方向（2026-09-04）

`repair_yi_against_source` 的證據 B 是「② 產出了 `N*10 億` → 答案裡孤立的 `N 億` 是音譯錯誤」。
**會誤判的唯一形狀**：LLM 違反 Rule 11 寫了一個孤立的億，而那個值**剛好是正確的**、
且**剛好等於某個 ② 產出值的 1/10**。這時它會被改成 10 倍而變錯。

- 已有的防線：排除條款（來源另有 `$N/10 billion` 或 `$N*100 million` 就不動）＋
  `_DUAL_PAREN_RE`（帶括號雙寫的一律不碰，⑲k4 守它）。但**來源是表格時排除條款真空成立**
  ——那正是證據 B 存在的理由，兩者無法兼得。
- **不修的理由**：要同時滿足「LLM 違反 Rule 11」「值正確」「恰為另一值的 1/10」三個條件。
  相對的，不加證據 B 的代價是實測會發生的自相矛盾 10 倍錯（見 CHANGELOG 2026-09-04）。
- ⚠ **不要用「同句／N 字元窗口」收窄**：那會引入一個沒有任何斷言分得出來的魔術數字，
  與 `_MARKER_WINDOW`（48）同一個病——那個值是判斷，不是有證據支持的。

**復活條件**：真的觀察到一次這個形狀的誤報（⑲k4 之外的真實答案）。

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

- ~~**live 路徑的量尺（`check_web_claims`，5 條）是 flaky 的**~~ → **2026-09-02 解掉了，成因是 fixture 不是斷言。**
  重錄 fixture（綁 multiyear ＋ as-of 09-02）後跑 **4 輪：判定逐題完全一致**（PASS 4／FAIL 0／N-A 1），`hit=36 miss=0`。
  ⚠ **關鍵證據是「穩定的同時仍然有噪音」**：四輪每一題的答案都不一樣（web-05 是 521／448／327／451 字），
  而五個判定一格都沒動 → 斷言真的只測「不論 LLM 挑哪個來源都成立」的性質，不是被凍住了。
  ⚠ 下面這段留著當**病歷**：它記的是「量尺綁在錯的世界上」長什麼樣子，而那個外觀與「系統壞了」完全相同。
  **已收緊**：`--mode replay` 現在是 bare `strict` ＋ 唯讀。

  〔以下為 2026-08-28 的原始診斷，保留〕
  實測 5 次跑分（2 個 collection × 有無 cache 污染 ＋ 1 次單題重跑）：`web-02` 的判定序列是 PASS,FAIL,FAIL,FAIL,PASS，`web-04` 是 FAIL,FAIL,FAIL,PASS。**同碼同 collection 會翻面。**
  根因不在斷言而在**輸入**：fixture 的 key 含 LLM 生成的英文 query 字串，每輪都可能不同 → `FixtureMiss` → 確定性 executor 的 `except Exception` 把它降級成「沒打 web」。
  ⚠ **重錄 fixture 不是解法**：只會換一組會再度 miss 的 key。
  **後半已於 2026-08-28 修掉**：`FixtureMiss`／`RecordError`／`ReplayCacheMiss` 移出 `Exception` 階層 → 任何 `except Exception` 都抓不到 → fixture miss 現在**當場炸、exit=1**，不會再被量尺報成「系統沒打 web」（閘門⑦，9 項，4/4 變異全抓到）。
  **剩下的那一半**：query 字面仍然綁死。實測同一題 web-02 在四輪裡生出四個不同的英文 query（`NVDA current stock price today` / `NVIDIA current stock price live quote` / `NVIDIA current stock price August 15 2026` / `NVIDIA NVDA latest share price June 2026`）。
  **方向**：讓 replay 的比對不綁 query 字面（按子問題 index，或對 query 做正規化／近似比對）。
  **在修好之前**：這 5 條只能當「跑幾次看趨勢」的 probe，判讀一律 ≥3 輪、看逐題 k/n。**好消息是失敗現在會自己現形**——不再需要人去分辨 FAIL 是系統還是量尺。

  ⚠ **2026-09-02：上面那組 PASS/FAIL 翻面的實測數字是 08-28 之前量的，而根因鏈的源頭已經被堵過兩次**——`replan` 於 08-29 註冊進 `_KNOWN_KINDS`（它是「新 query」那條鏈的源頭），唯讀於 09-02 加上。**所以「還 flaky 嗎」現在是一個沒有答案的問題**，而不是一個已知的缺陷。
  **下一步是重測不是重設計**：改 fixture 的 key（正規化／近似比對）風險比現況高——比對放寬會讓一次搜尋**配到錯的那筆錄影**，那比 miss 更糟（miss 會炸，配錯不會）。先用唯讀跑 ≥3 輪看逐題 k/n，再決定要不要動 key。

- ~~**`llm_replay.py` 沒有唯讀模式**~~ → **2026-09-02 修掉**（`RAG_REPLAY_READONLY=1`，opt-in；`--mode replay` 自動開；閘門⑫ 10 項、5/5 變異）。
  卡點「有沒有既有流程依賴回寫」也查清楚了：有三個（`record_web_fixture --mode record`＋兩支 period probe 會自動把快取指到 `eval/replay_cache.json`），所以**唯讀必須是 opt-in**。
  ⚠ 這一條原本還寫著「`record_web_fixture.py --mode replay` 沒設 strict」——**那句已過期**，2026-08-29 就設了 `strict:plan,replan,translate_en,check`。剩下的漏洞是**非 strict 的三個 kind 仍會回寫**，那才是唯讀真正堵住的東西。

---

## 觀察：multi_hop 的「比大小」目前沒有 Python 在做（量過了，沒有損害）

`agentic_rag_version/__init__.py` 裡所有 `max()` 都在比日期或 rerank 分數，**沒有一行在比「哪家公司的指標大」**。
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

## ~~`eval/web_fixture.json` 不可重放~~（2026-09-02 **已重錄**，見 CHANGELOG）

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

✅ **2026-09-02 重錄完成**：綁 `us_stock_rag_edgar_multiyear` ＋ as-of `2026-09-02`，5 筆 web 回應。
斷言逐條重新確認：`web-05`／`web-03`（兩個陰性對照）、`web-02`（`numbers_must_be_in_fixture` 自適應）、
`web-04` 全 PASS；`web-01` 因輸入層缺事實而 `blocked`（判 N-A，見〈已知缺陷〉）。
KB 側寫死的三個值都還在（`416.16`／`2026-06-12`／`4342.02`）。
⚠ **strict 還沒收緊成 bare `strict`**：新快取雖然七個 kind 都有了，但收緊要另外跑一次確認不會誤炸。

## ~~`_WEB_TODO_RE` 的詞表漏洞~~（2026-09-02 **連函式一起刪掉了**）

路由改成 todo 的 `route` 欄位之後，它最後一個呼叫端（`_node_replan` 的 snapshot web-todo 拒絕）換成讀 route，整個函式與詞表因此沒有呼叫端 → 刪除，碼上留墓碑註解。
它是「用硬編碼詞表做感知」的第三個死者（前兩個：`rq.looks_like_news_query`、`_RELATIVE_TIME_RE`）。

⚠ 六個它匹配不到的真實措辭**仍逐字凍結在**閘門⑧p——那條斷言守的是「守衛不可以用**待辦文字**當前置條件」，與詞表存不存在無關，所以留著。
新判準的斷言是 ⑧u/⑧v（snapshot 下 route=web 被拒、route=kb 仍加得進去）。

## 計畫：把「路由」改成 todo 上的 `route`（2026-09-01 定案；**2026-09-02 第 1~5 步已完成**）

**做完的**：斷言（閘門⑪ 30 項／⑯ 6 項）→ Plan 輸出 `route` ＋ 雙格式解析 → `_dispatch_todo`／`_escalate_route`／`_effective_route` → after 臂 A/B → 「預算用完不退回 KB」→ replan 帶 route（`_WEB_TODO_RE` 一併刪除）。逐項見 CHANGELOG。

**還沒做的**：`depends_on` 的實際解析（mh-07 的「該公司」，只留了欄位）；`mixed` 那 21 題從來沒被量過（量尺對它們判別力弱）。下面保留原始計畫供對照。

### 目標形狀

```
_node_plan  → todos[{task, route, depends_on, …}]     route ∈ {kb, web, both}
_dispatch_todo(route, …)  純 Python，確定性叫 tool     ← 新增（**不是** LangGraph node）
_check_sufficiency        只裁決：sufficient/missing/new_query/relevant_ids
```

### 為什麼**不**做成兩個 LangGraph node（這是這份計畫最重要的取捨）

retrieve↔grade 迴圈現在跑在 `_run_one_todo` **裡面**，而 `_node_execute` 用 ThreadPoolExecutor
一個 wave **平行**跑多個子問題（`agentic_rag_version/__init__.py:3681`）。拆成兩個 graph node，迴圈就變成
graph 的邊 ＝ 單一控制流 → **5 個子問題序列化**，一題的秒數直接翻幾倍（除非另外用 Send
重建 fan-out，那是大得多的改動）。

**責任拆開、迴圈留在同一個 node 裡**：單一職責在函式與 prompt 層完全做得到，wave 平行度不動。
「每個 node 有自己的工作」買到的是圖上好看，賣掉的是平行度。

### 它修得到什麼、修不到什麼（先寫下來，免得驗收時自我加分）

`check_news_routing.py` 的第二軸把兩種病分開了。before 臂實測：

**before 臂基準**（`experiments/newsroute_before.json`，23 題／0 錯誤，
collection=`us_stock_rag_edgar_multiyear`、as-of 2026-09-01、live）：
flow 16 題 → `web_grounded` 14／**`kb_only` 2**；record 7 題 → `kb_only` 6／**`web_grounded` 1**。

| 病 | 題 | `route` 修得到嗎 |
|---|---|---|
| `no_route`（web 一次沒打，Grader 看著財報說「夠了」） | news-09 | **修得到**——`route=web` 在 rnd 0 就打，不經過 Grader |
| `web_ignored`（打了 web 卻零 web 引用） | news-02 | **修不到**——Generator 側，另一條線 |
| **過度路由**（純財報題打了 web 並引用） | mix-01 | **要盯住不能變差**——見下 |

**誠實的預期是兩種失敗模式只解決一種。**

⚠ **`mix-01` 是這次改動最重要的一格**，而且它現在就已經是陽性。題目是「Microsoft Azure
**最新一季**的營收成長率」——答案在 10-Q 裡，但「最新」讓現行路徑打了一次 web 並引用它。
新的 `route` 由 Plan 用**同一段文字**判，**同一個陷阱原封不動地搬過去**。
所以驗收時 record 那半的 `web_grounded` **必須 ≤ 1**；變成 2 以上就是路由把「最新」
一律當 flow ＝ 這次改動製造了新的病。
（附帶：`mh-03` 純財報題也打了 2 次 web 但沒引用——那是白花錢，不是正確性缺陷。）

### 逐檔逐函式

**A. `agentic_rag_version/__init__.py`**

- **A1 `_PLANNER_PROMPT`（:1712）** 輸出格式 `["子問題"]` → `[{"task": …, "route": …}]`。
  **刪掉三段**——它們存在的唯一理由就是「沒有 route 欄位，只好叫 Plan 別在文字裡暗示來源」：
  ①【不要注入來源類型】整段 ②【量化財務題同樣不得注入新聞】整段
  ③「⚠ 不再有例外（2026-08-19 KB 拔除新聞）…寫成『…的新聞內容是什麼』只會撈到空的」。
  換成一句 route 判準（KB 只有 10-K/10-Q/Fundamentals；問「外面現在怎麼樣」→ web）。
  ⚠ **這是刪不是加**：prompt 已經 55 行且早就寫著「不要杜撰原問題沒有的意圖」，
  mh-07 照樣生出 `"NVIDIA最新年度的總營收是多少？"`——再加第九條 prose 是最不可能有效的做法。

- **A2 `_plan_subqueries`（:1776）** 解析要**同時吃 `list[str]` 與 `list[dict]`**。
  ⚠ 這條相容不是好心：既有 fixture 錄的 plan 值全是字串陣列，不吃就是所有 replay 當場失效。
  `str → {"task": s, "route": "kb"}`。replay key 是 `f"{freshness_mode}|{query}"`，不含
  system_prompt，所以改 prompt **不會**讓既有 key miss。

- **A3 `_node_plan`（:3562）** todo 多兩格 `route` / `depends_on`（先只支援 `None`）。
  `attributable` 不動。

- **A4 新增 `_dispatch_todo(route, query, need)`** 純 Python：`kb`→`_retrieve_chunks`／
  `web`→`_tavily_search`（**不撈 KB**）／`both`→兩個都做。

- **A5 `_run_executor_deterministic`（:3393）**
  · rnd 0：`query = task` **逐字**、依 route 分派。
    ⚠ **逐字這件事不是省錢，是 `attributable` 的前提**：`_retrieve_chunks(…, attributable=(attributable and rnd == 0))`
    的整個保證建立在「rnd 0 問的就是使用者問的」，閘門 ⑮f/⑮g 守的就是它。
  · 升級規則（Python，非 LLM）：`route == "kb" and verdict["kb_unfixable"]` → 本輪起升級成 `both`。
  · 末端那段 `if freshness_mode == LIVE and ENABLE_WEB_SEARCH and not sufficient` 的 web
    fallback 併進 dispatch ＋ 升級規則，不再是「不足就上網」。

- **A6 `_check_sufficiency`（:1848）** **`realtime_need` 留著不刪**。它已經是 live-only
  區塊（`_CHECKER_LIVE_RECENCY_BLOCK`，snapshot prompt 逐字不變），而且還有三個消費端
  （`_tavily_search(need=)`／`_unmet_realtime_gaps`／`_web_retry_is_pointless`）。
  改的只是**它不再決定 web 打不打**——那件事交給 `route`。

- **A7 `_node_replan`（:3768）＋ `_REPLANNER_PROMPT`（:3500）** 它現在的主要產出是
  「改用網路搜尋查…」這類 todo（實測 news-11 一題 6 個近義串），有了 route 之後**這份工作消失**。
  prompt 收窄成：只在「依賴解出了」或「缺一個明確的新離散事實」時加 todo。
  新 todo 也要帶 `route` → `_WEB_TODO_RE`（:415）與 `_is_web_todo`（:1404）**可以刪**
  （刪前先確認沒有別的消費端；閘門 ⑧p 逐字凍結了六個它匹配不到的措辭，一併處理）。
  replan 的 replay key 要把 `route` 納入。

**B. `llm_replay.py`** 不需要新 kind——Executor 不寫 query（改寫仍由 Grader 的 `new_query` 出）。
日後若把改寫拆成獨立 rewriter，那時才加 `rewrite` 並配「該 miss 的維度都會 miss」誤報對照（照 ⑧d~⑧g）。

### 五條確定性斷言（先寫斷言、後改碼；現在跑會全紅，那是對的）

| # | 斷言 | 加在哪 |
|---|---|---|
| 1 | **route → tool 真值表**：`kb` 不打 web／`web` 不撈 KB／`both` 兩個都打 | `verify_web_gate_isolation.py` 新閘門 |
| 2 | **rnd 0 的 query 與 `task` 逐字相同**（守 `attributable`／⑮g） | `verify_answer_validators.py` |
| 3 | **升級規則真值表**：`kb + kb_unfixable → both`；**`kb + not sufficient + not kb_unfixable → 仍然 kb`**（誤報對照，少了它等於退回「不足就上網」） | `verify_web_gate_isolation.py` |
| 4 | **Plan 輸出雙格式相容**：`list[str]` 與 `list[dict]` 都吃得下，且 str 的 route 預設值有斷言 | `verify_web_gate_isolation.py` |
| 5 | **陰性對照：`route=kb` 的 todo web 呼叫數必須 0** | 端到端由 `check_news_routing.py` 的 record 那半守 |

⚠ 斷言 3 的判別力**全在那條誤報對照**：只驗「該升級的有升級」的話，一個「一律升級」的實作也會滿分。

### 驗收順序（每一步都能單獨驗收）

1. 五條斷言先寫（全紅）
2. A1+A2 改輸出格式與相容解析 → 驗收：**既有 fixture 仍能重放**、斷言 4 轉綠
3. A3+A4+A5 dispatch 與升級規則 → 驗收：斷言 1/2/3 轉綠
4. 端到端跑 after 臂 → `check_news_routing.py` 對 before（`experiments/newsroute_before.json`）
5. A7 收窄 replan → 驗收：`probe_replan_contribution.py` 的待辦數下降且 record 那半不動

### 明確不做的

- **不**把 Executor/Grader 拆成 LangGraph node（平行度，見上）
- **不**從 Checker 移除 `realtime_need`（三個消費端）
- **不**碰 Generator 側的 `web_ignored`（news-02）——另一條線
- **不**加 `depends_on` 的實際解析（mh-07 的「該公司」）。原本寫的是「先留欄位，等 route 站穩」；
  route 站穩後於 2026-09-02 量了，**前提不成立** → 搬到〈已接受的極限〉並附復活條件

---

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

- ~~**「現在市值」的 web query 不帶 ticker → Tavily 確定性回垃圾**~~ → **同日推翻，那是暫時性的。**
  原始觀察：`What is Apple's current market capitalization?` 拿回 12 筆垃圾（最高分 **0.377** 是一支
  Tim Cook 影片，其餘是首頁／`en.wikipedia.org` 的「Capitalization」詞條／世界銀行 GDP 指標／
  `capitalone.com`），**兩次獨立錄製逐字相同**。我據此判定「系統性不是偶發」——**那個推論是錯的**。
  ⚠ **相隔十分鐘的相同回應，同樣可以只是 Tavily 在快取那個 query。** 我少的對照不是「再錄一次」，
    是**時間上分得夠開的一次**。約一小時後同一句拿回 `companiesmarketcap.com`／`stockanalysis.com`／
    `macrotrends`，**5/12 有值、top 0.899**；再重錄 `web-01` 就答出「$4.75 兆（截至 2026-09-01）
    【web: stockanalysis.com】＋ KB 的 $4,342.02B（2026-06-12）」——**正是這條斷言要測的並陳行為**。
  → **`web-01` 的 `blocked` 已解除**，斷言改成不綁值的版本（見下）。
  → 真正留下來的教訓寫在 `docs/EVAL.md`：**Tavily 的回應會隨時間變**，任何「這個 query 撈不到東西」
    的結論都要有**時間上分開**的重複，不是連續重跑。

- ~~**web query 帶 ticker 會大幅改善檢索命中**~~ → **第二輪沒重現，撤回（2026-09-02 同日）。**
  三小時後重跑（`--fixture` 換新路徑，否則 record 模式會先命中舊 fixture ＝ 量到重放不是重複）：
  nvda 股價 A 從 **0/2 變成 7/12**、msft D 從 **0/4 變成 6/11**、apple A 從 **5/12 變成 0/2**。
  **同一句 query 的 top 分數兩輪間 0.377 ↔ 0.899** → Tavily 的時間變異蓋過形式效應，
  單輪 n=3 分不出任何東西。**不動 `_web_query_en`。** 詳見 docs/EVAL.md §4.7。
  ⚠ 留下的是**方法**：`eval/probe_web_query_form.py` 還在，要再問這個問題時直接多跑幾輪。
  〔以下為第一輪的原始數字，保留當病歷〕
  `eval/probe_web_query_form.py` 的 2×2（問句／關鍵詞 × 有無 ticker，經生產 `_host_allowed` 複核後
  數「內容裡真的有被問的那個量」）：

  | | A 問句・無ticker（生產現況） | B 問句・**有ticker** | C 關鍵詞・無ticker | D 關鍵詞・有ticker |
  |---|---|---|---|---|
  | apple 市值 | 5/12（top .899） | 3/12（top .994） | **0/4** | **0/4** |
  | msft 市值 | 0/3 | **1/12**（top .997） | 0/5 | 0/4 |
  | nvda 股價（控制） | 0/2 | **10/10**（top .992） | 0/3 | 0/4 |

  · **關鍵詞形式（C/D）六格全是 0**——`_web_query_en` docstring 記的 2026-08-13「`Apple market cap`
    → score 0.91」**今天不再成立**。那條註解要改。
  · **B 在三題都不輸且兩題大勝**，控制題最明顯（0/2 → 10/10）。A 與 B 是同一次 probe 內相隔數秒跑的，
    時間混淆很小——這正是上面那個錯誤推論缺的控制。
  ⚠ **只有一輪，而 Tavily 已證實會隨時間變** → 動生產之前要跑第二輪（時間分開）。
  ⚠ **不可以拿「web-01 有沒有解除 block」當這個修法的證據**：那是為了讓 fixture 錄得漂亮而改受測物
    ＝量尺與被測物耦合。判準只能是這支 probe（它量 Tavily，不量我們的答案）。

- ~~**「KB 舊值 vs web 新值並陳」缺 validator**~~ → **2026-09-02 補上 R5**
  （`find_undated_dual_sourcing`，掛在 Synthesize 最末；閘門⑰ 12 項、6/6 變異；
  281 份既有答案乾跑觸發 1 次且是真陽性）。
  ⚠ **驗收僅止於確定性那一半，不要當成端到端改善**：掛上前後各三輪，`web-01` 都是 **2/3**。
    「並陳缺時點」這一型在 before 是 1/3、after 是 0/3——**n=3 遠低於任何解析度**，
    不能宣稱它被修好了。真正成立的是：R5 在 r6 那份答案上會發話（逐字凍結在 ⑰a），
    而在其餘 280 份上沉默。以下為原始診斷：
  同一份 fixture 三輪重放（`web-01`，Apple 市值）：r5 PASS、**r6 FAIL**、r7 PASS。
  r6 引用了 web 的 $4.75 兆卻**完全沒給時點**，還寫「兩者皆屬於同一時間段的不同來源」
  ——六月的 KB 快照與九月的 web 值**不是**同一時間段，那是一句錯的話。
  ⚠ 這條的 `why` 早就寫著「目前這個行為是 Generator 靠 prompt 做到的，validator 那條防線仍然是空的」，
    **現在有數字了**。`find_unreconciled_web_conflicts` 那個分支實務上幾乎不觸發（見上一條 R4）。
  **修法方向**：Synthesize 端加一道「web 與 KB 值並陳時，兩邊都必須帶時點」的確定性 validator。
  **量尺已就位**：`web-01` 的兩條 `must_match`（KB 日期、web 日期）就是它的驗收指標，
  且**刻意不綁數值也不綁日期寫法**（三輪寫出三種形式，見該條 `_assert_notes`）。

- **`web_ignored`：web 打了、回應可用，但答案一個 web 都沒引用（2026-09-02，兩個獨立實例）。**
  · `web-01` 第 10 輪：`n_web_calls=1`、fixture 裡有 `stockanalysis.com` 的 $4.75 兆，
    而答案只有一句——「Apple 目前的市值約為 43,420.2 億美元【AAPL_Fundamentals_20260612.txt, chunk #0】」
    ——**82 天前的快照當「目前」，零時點揭露、零 web 引用**。
  · `mh-09`（`check_news_routing` 的 mixed 不對稱判定當天撈出來的）：`c=3` 打了三次 web，
    答案卻只有財報引用。第二軸把兩者都歸在 `web_ignored`。
  ⚠ **R5 對這一型正確地沉默**：沒有並陳就沒有時點問題。兩者是不同的病，不要合併成一個失敗率。
  ⚠ **也不是「web 回垃圾」那一型**：這兩次 web 回應裡都有可用內容（第 10 輪與第 8/9 輪重放的是
    **同一份 fixture**，而 8/9 兩輪都引用了它）。所以成因在 Generator 的抽樣，不在輸入。
  **卡點**：確定性守衛不好寫——「web 有內容卻沒被引用」在 web 真的回垃圾時是**正確行為**
  （見上面 Tavily 那條），兩者在結構層看起來一樣。要分辨得看內容層有沒有被問的那個量＝感知。
  **現有量尺**：`check_web_claims` 的 `web-01`（逐輪 k/n）＋ `check_news_routing` 的第二軸。

- **web 回應「非空」不等於「答得出來」，而結構性缺口只看得到前者（2026-09-02 命名）。**
  `_unfulfilled_web_route_gaps` 的判準是 `route ∈ (web, both)` 且 `web_notes` 為空。上面那題
  web 打了 1 次、拿回一堆**與問題無關但非空**的頁面 → `web_notes` 非空 → **不留缺口**，
  外觀與「web 成功回答了」完全相同。
  ⚠ **這是那個函式的設計範圍，不是 bug**（它刻意只做結構性判斷、不看 `realtime_need`）。
  記在這裡是為了讓這個洞**有名字**：要補得靠內容層判準（web 回應裡到底有沒有被問的那個量），
  而那是感知不是規則。
  **可見後果**：答案標題句寫「Apple **目前**的市值約為 43,420.2 億美元」，值來自 **82 天前**
  的 `AAPL_Fundamentals_20260612` 快照。⚠ **時點有揭露**（第二句寫了 2026-06-12），
  所以這是「標題句措辭」不是「零揭露」——不要把它報成後者。

- **R4「兩邊都標了出處就閉嘴」那個分支實務上幾乎不會觸發（2026-09-01 查到，方向安全）。**
  `find_unreconciled_web_conflicts` 用 `w_cited = _WEB_MARK in w["quote"]` ／ `a_cited = _KB_MARK_RE.search(a["quote"])` 判斷「模型是否已經並陳」，兩個條件同時成立才保持沉默。**兩個獨立的理由讓它幾乎恆為 False**：
  ① `quote` 是 Checker prompt 要求的「答案裡對應的原句片段（**30 字內**）」再截到 60 字元，而一則 web 引用光網址就遠超過 30 字元——片段裡通常根本沒有引用標記。
  ② `_WEB_MARK = "[web:"` 是**半形**，而實測 40 份答案的 web 引用**全部是全形**【web: …】（生成端跟著中文標點走）。
  **方向是安全的**：誤報時它要求的動作是「把兩個值連同時點都講清楚」，而那本來就是正確行為（該函式自己的 docstring 就是這樣論證選並陳而非裁決的）。代價只是**多燒一輪 reflect**。
  **為什麼沒當場修**：改判斷式要先有能證偽它的確定性測試，而 `quote` 是 LLM 產物、目前沒有任何 fixture 錄著真實的 claims 陣列——現在改就是又一個「聽起來合理」的機制假設。
  **復活條件**：① 錄一份真實 claims（含 quote）當 fixture；② 在 `verify_answer_validators.py` 加一條真值表（全形／半形 × quote 含不含標記 × 值差 >2%），確認「已並陳 → 沉默」與「未並陳 → 發話」兩個方向都測得到。
  ⚠ 修法**不是**把 `_WEB_MARK` 加上全形就好——①才是主因，而「引用標記在不在 30 字片段裡」是機率問題不是格式問題。真正的修法可能是改判斷來源（看整份答案而不是 quote）。

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
| **`depends_on` 不做實際解析** | **量下去沒有實例。** 生產 5 題 multi_hop 的 Planner **30 輪／78 個子任務**：**72 個是扇出**（每家公司、兩跳指標各問一次）、**0 個通靈**。原因是候選集合就寫在題面上（「在 A、B、C 三家中」），扇出讓 Generator 自己比大小——對**封閉候選集**這是正確策略，且結構上不需要依賴。剩下 6 個是「延後指涉」（「總營收最高的公司的毛利率是多少」），再往下追一層：`parse_query_filters` 9 次**全回 ticker=None**＝退回廣泛語意搜尋（浪費一個子任務），**不是鎖到錯的公司**——而同題其他輪次的扇出已涵蓋同一片地。欄位名取自的 `mh-07` 在**冷凍 37 題**裡（現在不計分），且它第一跳要的是**新聞**——KB 依設計沒有，就算做出完美的拓撲排序，第一跳照樣空手：那裡的綁定約束是語料不是依賴。**代價那一面**：要把 `_node_execute` 的單波 `ThreadPoolExecutor` 拆成多波，序列化一條**一題已燒 50~60 次 LLM** 的關鍵路徑，而〈明確不做的〉第一條正是為了平行度拒絕拆 node | 任一成立即重評：① 題庫出現**候選集合不在題面上**的比較題（「哪一家的 X 最高」而不列公司）② 冷凍 37 題靠 live web 復活——`mh-06`~`mh-10` 正是這個形狀，那時第一跳才有來源 ③ 延後指涉的子任務被量到**解成錯的單一 ticker**（現在 0/9；那才是危險，比 None 更糟，因為 hard filter 會鎖到別人的財報） |
| **`faithfulness` 不再追高** | 這是**負空間**：把 gold 當答案餵回去只拿 0.659，多數題輸給系統。往上推等於要求系統比標準答案更保守 | 換 metric 才談 |
| **`us_stock_rag_edgar_exp4` 的 Fundamentals 比率仍是小數** | exp4 ＝ 歷史基準，早就不是任何入口實際查的東西 | 若日後要拿 exp4 當對照臂，得先跑 `migrate_fundamentals_pct.py`，否則 Fundamentals 類題目的差異分不清是切塊還是單位 |
| **`sem-08`**（AMZN AWS 策略方向） | 三種系統側修法皆敗於同一真因：生成模型判定「獲利數字」與「策略方向」問法無關而主動略過，**非訊號埋沒** | 只剩調整 rubric 或維持現狀 |
| **`col-07`**（否定框架） | 病根是「非龍頭」vs「minority share」的**框架落差**，不是翻譯；`full_translate_en` 已開仍漏。只有已知答案字面（＝HyDE）才撈得到 rank 1 | 不為單題上 HyDE（全管線改、有反傷） |
| **`col-08`**（Tesla 口語版）／**`col-05`**（AWS 口語版） | dense rank 25/106，落差超出 rewrite 射程 | 有新召回機制才重試 |
| **`col-04`**（Azure 口語版） | 關鍵內容在 top-5，生成被口語框架帶偏、選擇誠實拒答。**非 bug** | 觀察中 |
