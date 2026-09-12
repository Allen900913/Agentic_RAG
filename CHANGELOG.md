# CHANGELOG

> **這裡只寫「哪一天改了什麼、關鍵數字、結論」。** 為什麼那樣設計 → `docs/`；還沒做的 → [`BACKLOG.md`](BACKLOG.md)；現況 → [`CLAUDE.md`](CLAUDE.md)。
> 每次改動在最上面新增日期區塊。**null result 一定要寫**（同時登錄到 `docs/EVAL.md` 已試無效總表）。
> ⚠ **兩個不可比的分界線**：跨 **2026-08-19**（題庫 100 → 65）、跨 **2026-09-04**（RAGAS judge 換 gemma）的分數一律不可直接比。

---

## 2026-09-12

- **`--trace` / `--verbose` 的 trace 半邊從套件化那天起就一個字都不印（靜默失效）。** `__init__.main()` 寫的是 `global _TRACE; _TRACE = True`，重綁的是 `agentic_rag_version._TRACE`（`from .tracing import _TRACE` **複製來**的快照），而四個子模組用的 `_trace()` 讀的是 `tracing._TRACE` ⇒ **沒有人讀被重綁的那個名字**。修法：`tracing.set_trace_enabled()` 成為**唯一寫入點**（刻意**不**讓 `_trace()` 去讀 `_pkg._TRACE`——那會讓 `tracing` 反向依賴套件，正好拆掉它「零相依」的存在理由）。⚠ **`AGENTIC_TRACE=true` 一直是好的**（env 在 import 時就讀完），而 BACKLOG 記載的兩次實際診斷都走 env ⇒ **能用的替代路徑會讓壞掉的那條活得更久**。
- **為什麼 H2／閘門⑬ 結構上攔不到它，以及補上的那一半。** 那兩道的 patch 名單**從 eval 腳本反推**，而沒有任何 eval monkeypatch `_TRACE` ⇒ 它永遠進不了那份名單：**規則涵蓋得到，強制器涵蓋不到**。新增閘門㉓（`verify_answer_validators` 339 → **349 項**，**4/4 變異**）：㉓d **exec `main()` 裡 `--trace` 分支的真實原始碼**再探針（**原 bug 就死在這條**）／㉓f 把修法前的寫法注入、要求探針保持靜音（否則 ㉓d 可能只是恆真）／㉓e 關掉 `tracing._TRACE` 必須立刻靜音（＝沒有第二個開關）／㉓g `ar._TRACE` 那份快照不得被任何模組讀寫。⚠ **快照刻意留著不刪**：閘門⑬f 要求子模組每個 top-level 名字都 `hasattr(ar, …)` 拿得到，刪掉會當場打掉 ⑬f ⇒ 改成「留著但有守門」。⚠ 刻意**不用** subprocess 跑真 CLI（要先過 `_get_models()` 再燒 50~60 次 LLM 呼叫，與本檔零 LLM 相違）。變異：M1 改回原 bug → ㉓d1/㉓d2/㉓g 抓到；M2 漏 re-export → ㉓a2；M3 子模組偷讀快照 → ㉓g；M4 `_trace` 反向依賴 → ㉓a/㉓b/㉓d。
- **三條從來沒驗過的路在新機器上跑通（單發管線與確定性閘門之外的那三條）。** ① **兩個 venv 無污染**：prod `langchain 1.4.0`／`langchain-core 1.6.3`／`langgraph 1.2.11`、**無 ragas**；`.venv-ragas` `langchain-core 0.3.86` ＋ `ragas 0.2.15`、**無 langgraph**，且 prod 的 `SemanticChunker`／`langgraph`／`agentic_rag_version` 全 import 得起來 ② **agentic 端到端**（multi_hop）：Plan 正確拆 2 個子問題、跨子問題聯集 11 顆 chunk、NVDA ~74% > MSFT ~68% 結論正確，**口徑揭露主動並陳** Fundamentals TTM 74.14% 與 FY2026 10-K 自算的 71.06% ③ **產品線 `api_server` + `app.py`** SSE 全程通，期別判到 `NVDA_10Q_202604`（FY2027 Q1 ＝ 財年反轉那一格），`revision` 事件的金額單位後處理前端有接並整段換掉。
- **產品線撿到一個缺陷，登錄 [`BACKLOG.md`](BACKLOG.md) 未修**：`app.py` 的事件鏈**靜默丟掉 `fallback_note`**（期間降級揭露）。確定性那條通道在 UI 上沒了，只剩 prompt 注入後由 LLM 轉述的那條（實測這次有講，但不保證）。
- **文件**：[`README.md`](README.md) 的 agentic CLI 那節補上「**`--freshness-mode` 預設是 `live` ⇒ 會真的呼叫 Tavily 燒額度**」與 `--trace` 用法。先前**沒有任何一處**寫過 CLI 預設與 `run_agentic_on_evalset.py`（預設 snapshot）**相反**——這次是實測撞到才發現。

## 2026-09-11

- **量尺為什麼一直錯：歸類成五個根因，三個機械可判定的做成一道閘門。** 新增 [`eval/verify_eval_harness.py`](eval/verify_eval_harness.py)（19 項、4/4 變異）：**H1** 非 ASCII 輸出要轉 stdout **與** stderr（掃出 27＋7 支違規）／**H2** eval 攔截的名字在套件子模組裡不得裸名引用（當場抓到 `_classify_staleness` @ `graph.py:417`）／**H3** 生產常數凍結＋抄寫點登錄制（`eval/eval_harness_baseline.json`）。五個根因與判別力分析見 [`docs/EVAL.md`](docs/EVAL.md) §4.0。
- **`literal_matcher` 的數字邊界 bug（一個 bug 解釋了三支尺的誤報）。** `(?<![0-9,.])…(?![0-9,.])` 把**數字的延續**與**一般標點**混為一談 ⇒ 後面緊跟逗號或句點的數字一律匹配不到。於是 `lex-04` 的 gold 指到兩顆**附件索引**，真正的答案 `With our introduction of CUDA in 2006, we opened …` 一顆都沒收，`probe_chunk_gold_recall`／`probe_recall_layer_attribution`／`probe_gold_funnel` 三支一起把答對的題報成檢索缺陷。修成 `(?<!\d)(?<!\d[.,])…(?!\d)(?![.,]\d)`，gold 83 → 102 顆，`--check` 0 漂移。⚠ **自測表裡本來就有那個 literal，只是把逗號拿掉了**＝第七次踩到「測資形狀與生產不同」。
- **`_classify_staleness` 的裸名呼叫鏈（B 家族）。** H2 → ⑰f 注入點搬回 `ar.` → ⑬a 凍結清單少列 → ⑬b 抓到 `graph.py:47` 的 `from .validators import` **壓了一份綁定**＝真正的根。**一個 import 行，四層閘門才挖到底。**
- **E6：gold 修完之後重量。** `experiments/cgr_rrf30_goldfix3_20260911.json`（41 題×3 輪、翻面 0）：`gold@5` 0.683 → **0.878**、`gold@20` 0.780 → **0.976**、檢索層缺陷 32% → **12.2%**、召回失敗 9 → **1**（`sem-05`）。⚠ **9 → 1 裡只有 2 題是真的修好**（`lex-17`／`sem-03` 靠 RRF 20→30 進池），**6 題是量尺本來就錯**（EPS 族 gold 太窄 3 題；裸年份 literal 的邊界 bug 3 題）——**引用這個數字一定要一起講拆帳**。
- **E6b：層級歸因重跑**（`experiments/rla_20260911.json`，41 題×2 輪、翻面 0）：① collapse／filter 刪掉 gold **0**／② RRF 名額 **0**／③ prefetch **3**／④ 真的撈不到 **1**。⇒ **「五題是名額問題」整個來自 gold 缺陷，RRF 名額那條懷疑結案**；而 ① 兩次都是 0 ＝ collapse／hard filter 沒有刪掉過 gold。⚠ ③ 那 3 題目前**不可歸因**（舊設計的寬臂把 `RRF` 與 `FETCH_N` 一起放寬，而 `FETCH_N` 單獨調已知零效果）→ 補了切開兩者的第三條臂（selftest 13 → 18）。
- **E7：RRF=20 在修好的 gold 上重測**（`experiments/cgr_rrf20_goldfix3_20260911.json` vs `..._rrf30_...`）：`gold@5` 0.829 → 0.878、`gold@20` 0.927 → 0.976，差異**恰好三題且三輪一致（0/3 → 3/3）**——`lex-17`（進池）、`mix-14`（進 @5）、`sem-03`（進池）。`+0.049` 與在有缺陷 gold 上量到的相同 ⇒ **收益可複現，維持 30**，先前「當初是在壞尺上量的」這個疑慮排除。
- **E1：那三題在 agentic 上本來就沒有損失**（逐題查 R3 結果檔）：`lex-17` 靠 ratio 保底（`used_via_backstop`）、`mix-14` 已答對、`sem-03` 的 gold **在 `retrieved_union` 裡卻被圈選丟掉** ⇒ 卡的是別的層。⇒ sweep 的收益只落在單管線那條路，而**那條路的答案層沒有任何 eval 涵蓋**＝已記進 CLAUDE.md 的取捨。
- **E2：內容天花板旗標的證據翻面，決定不做。** 直接讀 Qdrant 驗證後，R3 的 8 個 `forced_pass` 子問題有 **2 個是 Grader 判錯、事實就在 KB 裡**（`lex-04` CUDA 2006、`mh-01` GOOGL TTM 營收），R4 又獨立複現同樣的 2/8（`mh-04` 那顆 chunk 就在該子問題自己的候選池裡、答案還引用了它）。⇒ 誤殺率至少 **25%**，而探針原本的 0/15 是**取樣框架排除了危險形狀**。詳見 [`BACKLOG.md`](BACKLOG.md)。
- **漏斗重算**（`experiments/gold_funnel_r4_20260911.json`）：③ 檢索沒撈到 5 → 4 → **2**；② 撈到了被丟掉不動（**5**）；撈到且引用 30 → 31 → **33（80%）** ⇒ 檢索層與圈選層的比例從 **5:5 變成 2:5**。
- **R4：第四輪分母 ＋ 三個量尺缺陷。** `experiments/agentic/gj_65q_r4_20260911.json`。① **`repair_degraded_records` 原本只抓 graph 層** ⇒ `mix-03` 的子問題崩潰漏網、補跑後回報「降級 0 題」而它並不乾淨 → 加了子問題層另計回報（selftest 7 → 12）。② `number_claims` 的 `mix-03` 與 `check_rounding_fidelity` **期望相反**（前者只認捨位後的 `6.4 billion`，後者把捨位當缺陷）→ 放寬 `expect_text`。③ **R4 的 `crashed` 成因第一次查得出來**：10 題降級全是 `NotFoundError: 404`、連續落在 `sem-09`~`mix-04`，當場另打 `DEFAULT_MODEL` 證實模型仍活（3.7s）⇒ **「崩潰依時間叢集不是類別」從推論變成直接證據**。
- **`probe_recall_layer_attribution` 的生產臂把常數寫死**（量尺與被測物耦合，第三次）→ 改成 `import` `rq.RRF_TOP_N_PRIMARY`／`rq.FETCH_N`，`_meta.arms` 與標籤一律從常數算。

## 2026-09-10

- **`RRF_TOP_N_PRIMARY` 20 → 30（生產組態變更）。** 全量 sweep（41 題×3 輪×三臂，`experiments/cgr_rrf_baseline20_20260910.json` 等）：基準臂逐字重現 09-08 的數字 ⇒ 差異可乾淨歸因。20／30／50 的 `gold@5` 是 0.683／**0.732**／0.732，單次檢索 28.8／41.9／67.8s ⇒ **30 買得到全部收益，50 是純浪費**（`RERANK_INPUT_N=50` 是重排輸入上限，第 31~50 名沒有一個含 gold）。同時把 `rag_query.py` 那句過期的「sweep 顯示 20 > 40」註解改成本次證據。
- **`kb_unfixable_exit = 0` 不是謎，是 snapshot 的定義——我在同一個旗標上讀錯的第三次。** `_check_sufficiency` 的 `need, kb_unfixable = "none", False` 寫在 `if _live:` **之前**，而跑分預設 `--freshness-mode snapshot` ⇒ **那兩個欄位恆為常數、`kb_unfixable_exit` 不可能非 0**；而探針那 17 次觸發是用它自己的預設 `live` 跑的 ⇒ **兩個數字從來不是同一個組態**。原本規劃的「加逐輪觀測」**取消**（snapshot 下只會錄到三個常數）。結構事實凍結成閘門⑰（10 項、5/5 變異，含「跑分預設 snapshot／探針預設 live／兩者不得相同」三條斷言）。
- **65 題第三輪：三個我自己前兩輪寫下的結論被推翻。** ① 「穩定核心 5 題」→ 三輪都在的只有 3 題 ② 「multi_hop 兩輪都是 0%」→ R3 是 2/28＝7.1% ③ `unit_stats` ① 首次非 0（4 筆／3 題，`col-12` 的 `60,801 百萬元（約 $60.80 億）` 是真的差 10 倍）。另 `revision_stats` 首次出現退回（9／1）。

## 2026-09-09

- **兩個觀測通道（只加觀測、行為逐字不變）**：`degraded_reason`（崩潰降級的例外型別＋訊息＋最深一層檔:行）與 `retrieved_union`（各子問題 `run_state.pool` 的 key，**Grader 圈選之前**）。閘門㉒ 23 項、9/9 變異。⚠ **缺席 ≠ 空值**（填 `""`／`[]` 會讓「沒降級」與「降級但查不出成因」外觀相同）。
- **`gold@cited` 三段漏斗**（新檔 [`eval/probe_gold_funnel.py`](eval/probe_gold_funnel.py)，selftest 9/9）。第一版量到的比預期窄：`sources` 不是「撈到過的全部」而是 `_fair_select` 之後交給 Generator 的那一把（實測 5 顆）⇒ 第二段**結構性接近恆真**，那個 0 不是健康證明。→ 這就是加 `retrieved_union` 的理由。
  ⚠ **同一次量到「第二條入口」，而那是量尺自己的錯先現形的**：探針原本斷言 `sources ⊆ retrieved_union` 是不變量，實跑報「✗ 破了」；查下去發現 `_ensure_ratio_source_coverage` **直接打 Qdrant、繞過 `run_state.pool`** ⇒ 那不是不變量，而 `lex-17` 的 gold 是**保底救回來的成功**被量尺報成了一種病。
- **召回失敗的層級歸因**（新檔 [`eval/probe_recall_layer_attribution.py`](eval/probe_recall_layer_attribution.py)，`experiments/rla_20260909.json`）：① collapse／filter 吃掉 gold **0 題**（乾淨的 null result，那條懷疑排除）／② RRF 名額 2／③ prefetch 3／④ 真的撈不到 3／跨輪翻面 1。
- **`probe_kb_content_ceiling.py`：`forced_pass` 是「KB 真的沒有」還是「沒撈到」**（25 子問題×3 輪，`experiments/kb_ceiling_20260909.json`）。⚠ **兩種 ceiling 第一版被混成一格**，於是三筆真正的即時題被算進誤殺率——拆成 `ceiling_recency`／`ceiling_content` 之後才對。（這份的 0/15 誤殺率已於 09-11 被推翻，見上。）
- **五個分母的第二輪**（`experiments/agentic/gj_65q_denominators_r2_20260909.json`）：`forced_pass` 15.5% → 9.2%；`crashed` 的類別歸因被推翻（R2 的 3 筆落在 `sem-05`／`sem-10`／`mh-01`）⇒ **「multi_hop 比較會崩」不成立，那是時間叢集**。⚠ **三輪 repair 才到 0 降級**（9 → 4 → 0）。
- **讀完 09-08 那一輪的三批明細**（零成本）：**(a)** 確診 `_fill_dependent_hop` 把所有 `#N` 換成同一個實體（`在 NVIDIA、NVIDIA、NVIDIA 中`），根因是 `depends_on` 是單一 int 而比較型 hop 天生多父——**未修**，見 BACKLOG。**(b)** 被額度擋掉的 2 個待辦撈的是**已經在池子裡**的 chunk ⇒ `MAX_TODOS` 維持 7。**(c)** 28 筆 `forced_pass` 的 `missing` **沒有一筆在講時效**，全在講內容 ⇒ 「讓 `kb_unfixable` 觸發」修不到 `forced_pass`（**本檔 BACKLOG 早就寫過這件事——下結論之前先 grep 自己的 BACKLOG**）。
- **順帶：`_extract_citations` 對 `;` 串接的引用整段抓不到**（生產缺陷，未修）。363 個引用區段裡 5 個（1.4%）是這形狀，影響閘門㉑ 的指紋（方向是漏報）。

## 2026-09-08

- **補上「生產檢索到底撈不撈得到」這把尺**（新檔 [`eval/probe_chunk_gold_recall.py`](eval/probe_chunk_gold_recall.py)）＝「檢索已經到頂」目前唯一能重新驗證的尺（原本那個結論來自 100 題 × 舊 judge × 舊 collection 的 RAGAS，照本 repo 自己的規則已作廢）。首輪：`gold@5 0.683`／`gold@20 0.780`／13 題有缺陷（後於 09-11 拆帳，其中 6 題是量尺錯）。
- **四個分母第一次量到**（`experiments/agentic/gj_65q_denominators_20260908.json`），其中兩個推翻了原本的假設（見 BACKLOG 的「已被證據關掉的決策」）。
- **崩潰降級的題會被 resume 永遠跳過**（新檔 [`eval/repair_degraded_records.py`](eval/repair_degraded_records.py)）：降級路徑照樣產出 answer、不寫 error，而 resume 的判準是「有 answer 且無 error → 跳過」⇒ 一份「跑完 65 題」的結果檔混著 N 題根本沒經過 agentic 管線的記錄，**外觀完全正常**。
- **executor 的「重試用完強制放行」在此之前完全不可觀測**（閘門⑯ 22 項、8/8 變異）。實測「Magnificent Seven 裡最近十二個月淨利最高的是哪一家？」hop-1 連跑三輪 `sufficient=False`，答案照樣產出、零保留，**而且答錯**（答 Apple $122.58B，實際最高是 GOOGL $160.21B）。⚠ **四種出場不可合併**（`forced_pass`／`kb_unfixable_exit`／`web_budget_exit`／`crashed`）。⚠ 同時登錄「集合詞題不支援」為已接受的極限。

## 2026-09-06

- **agentic D：Synthesize 的六道修補鏈沒有不動點，加上零成本的回歸守衛**（閘門㉑ 16 項、7/7 變異）。重生成後重算四項零 LLM 缺陷指紋，**任何一格變差就退回原答案**。⚠ 指紋刻意不含 `find_claim_conflicts`（要一次 LLM）。設計理由與業界對照見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A13。
- **agentic C：multi_hop 的依賴改由 Planner 宣告，`_BACKREF_RE` 詞表退位**（閘門⑮ 28 項、10/10 變異）。三段式：Planner 吐 `depends_on` ＋ `#N` 佔位符 → 確定性結構檢驗（`0 <= depends_on < id` 同時保證無懸空／無自環／無環）→ 懸空是 prune ＋ trace。⚠ **「說沒有」與「沒說」必須分得開**，否則詞表仍然是實際做決定的人。⚠ **在 eval_set 上量到的差異是 0，而那是預期**（5 題 multi_hop 的候選集合都寫在題面上）。
- **agentic B：Replanner 的待辦額度用完時，拒絕原本一個字都不印**（閘門⑭ 15 項、7/7 變異）。`MAX_TODOS` 是 Planner 與 Replanner **共用**的額度、先到先得（實測 planner 拆 7 個時 Replanner 預算是 0）。**這道修的是可觀測性不是額度**——先做出分母再談改常數。
- **agentic A：`_fair_select` 修掉一個自我矛盾**（閘門⑳ 9 項、4/4 變異）。docstring 自己寫著「cross-encoder 原始分數跨子問題不可比」（那正是**選擇**改成 round-robin 的理由），然後最後一行仍然拿同一個分數決定**順序**。⚠ **只動順序不動選擇**（集合必須逐字相同）。⚠ **收益沒有量，一條斷言都不宣稱答案會變好。**
- **RAGAS 量尺在新 judge 上重量完畢**（`experiments/ragas_65q_gemma_goldceiling.json`／`ragas_65q_gemma_sysA2.json`／`sysB.json`）：gold 上限整組換新、0 筆 NaN；`answer_correctness` 上限 .972 → **.987** ⇒ **判別力沒有變差**，落差仍是 .333。噪音兩格被推高（nv .008→.012、faithfulness .010→.021），correctness 取較大值後維持 .012。⚠ **`--timeout` 420 在 gemma 上不夠**：全量第一輪 11 筆 NaN（那輪均值只有 60 列、不可當基準），`--timeout 900 --nvidia-passes 2` 之後降到 1 筆／0 筆。
- **清掉四筆積欠**：① `gen_reference_answers.GEN_MODEL` → `openai/gpt-oss-20b`（**不能與 judge 或系統 generator 同源**，否則會把 correctness 灌高）並補上 `--force-all` 守衛（死模型原本是一道意外的保險，換成活模型等於拆掉它）② correctness judge 一起換 gemma，逐題 k/n 對照（11 案例×9 輪）：**gemma 五個陰性對照全 9/9**，唯一硬傷是把一個有根據的具體數字判成捏造（**誤報方向，安全的那一邊**）；20b 反過來有三個陰性對照會漏（**漏報方向，危險的那一邊**）⇒ **維持 gemma**。⚠ 這個結論與只看那一題得到的印象相反——**那就是「逐題 k/n」這條規則的用處**。③ `ablate_retrieval_model.py` 標記為「跑不起來，刻意不修」④ 決定不改寫 commit `16d4646`。
- **null result**：RAGAS `--max-workers` 開到 6 **沒有加速**且出現 3 次 500/503。**這 40 分鐘重新推導了 repo 已經知道的事**（help 字串早就寫著「NVIDIA 限速，別開太高」）。

## 2026-09-05

- **65 題端到端：「億」的頻率有分母了**（`experiments/agentic/gj_multiyear_r2_unitstats.json`）：65 題觸發 10 題（15.4%）／共 14 筆／`unit_stats=None` 0 題 ＝ 約每 6~7 題違反一次 Rule 11。⚠ 與舊模型的比較做不到（`gpt-oss-120b` 已 410）。
- **同一輪揪出後處理的第三個缺口 → 閘門⑲m**（14 項、4/4 變異）：⑲a~⑲l 全綠時，`col-11` 仍吐出「增加 29% 至 **$54.5 億**」而來源寫著 `$54.5 billion` ＝差 10 倍。成因是 `_YI_SOLO_RE` 把幣別詞寫成**無條件必填**，於是那份答案匹配到 0 個。放行**只限有 `$` 前綴**（`$` 本身就是幣別標記，而沒有人把使用者數寫成 `$10 億`）。⚠ **教訓與 ⑲k 同一個**：測資全寫成 `N 億美元`，而 LLM 實際會寫 `$N 億`。

## 2026-09-04

- **端到端跑出來的洞：backstop 的證據判準對「表格來源」結構性失明 → 閘門⑲k**（8 項、3/3 變異）。⑲a~⑲j 全綠時真實答案吐出 `828.86 億美元（$82,886 百萬），相當於 $82.886 億美元`——同句自相矛盾、後半差 10 倍。**教訓是量尺的測資形狀**：⑲h 的來源是手寫英文句子 `Total revenue was $84.75 billion`，而生產的來源是 markdown 表格（`In millions` 只在表頭）⇒ **那條路等於從來沒被測過**。因此補了證據 B（② 自己產出了 `N*10 億`）。
- **chunk 層 gold 建起來**（[`eval/chunk_gold.py`](eval/chunk_gold.py) ＋ `chunk_gold.json`，41/65 題）。第一輪就撈出兩個量尺缺陷：`probe_relevant_ids` 的檔層 gold 對 14 題萬用字元永遠是 N/A（改用 `fnmatch`）、`gold 全滅` 分不出「濾掉離題（正確）vs 濾掉答案（危險）」（加 chunk 層 `答案全滅`）。
- **「億」發生頻率的分母：從只 print 變成落地到結果檔**（閘門⑲l 12 項、3/3 變異）。⚠ 刻意用 out-param 不用模組層全域（`_node_execute` 是 ThreadPoolExecutor，而被蓋掉後的外觀與「這題沒觸發」完全相同）；⚠ 刻意不改回傳型別（三個呼叫端，改成 tuple 會讓漏改的那個安靜地把 tuple 當字串接）。
- **RAGAS judge 換掉，並揭出一個會靜默吃掉主指標的預設值。** `gpt-oss-120b` 410 → 先換 `gpt-oss-20b`（中位 8.8s 太慢）→ 同日再換 `google/gemma-4-31b-it`。⚠ `--timeout` 預設 120 對新 judge 不夠，`answer_correctness` 會逾時 → **靜默變 NaN**，而 NaN 印成 `n/a`、外觀與「這指標算不出來」相同——那正是唯一還有空間的指標。⚠ **所有既有 RAGAS 聚合值就此失去可比性。** ⚠ 選型 bake-off 對「判定品質」**零判別力**（5 模型×3 角色全 9/9），它量到的只有延遲與可用性。

## 2026-09-03

- **預設 LLM 被退役當天換掉：五個定義點 → `nvidia/nemotron-3-super-120b-a12b`。** `gpt-oss-120b` 於 08:00Z 被 NVIDIA 退役（410），而它是**檢索／判定／生成三側共用**的預設 → 當天整條管線的 LLM 全死。選型證據 `experiments/_model_bakeoff_20260903.log`：**判準是輸出穩定性與延遲，不是模型大小**——nemotron-3-super 三個結構化角色 3/3 且最快（plan 10.4s／check 6.5s／filter 2.8s），`deepseek-v4-pro` plan 一次 **604 秒**（不可用）、`llama-3.1-nemotron-ultra-253b` 在 NIM 上 404。⚠ **換模型讓所有既有基準與 replay fixture 的生成端失效**，且**沒有辦法與舊模型 A/B**。
- **金額換算「億」：三層防線本來只裝在 eval，生產管線是漏的。** `rq.finalize_answer_units` 接線（① 剝掉 LLM 自算的億 → ② 程式獨佔換算 → ③ 修剩下的孤立億），閘門⑲ 26 項。⚠ **順序不可調換**：調換後 `repair_paired_yi` 會把巢狀自己拆掉、**乾淨地留下 LLM 那個錯值**（84.75 而非 847.5），外觀完全正常、危險得多 → ⑲g 斷言的是**值**不是格式。
- **`agentic_rag_v2.py` 套件化：4549 行單檔 → `agentic_rag_version/` 四個模組**（`retrieval` → `validators` → `tools` → `graph`，`__init__` 是門面＋生成層）。**兩條規則違反了 eval 會靜默失效**：子模組不得裸用被 monkeypatch 的 14 個名字、不得 `from .x import` 那些名字（**循環 import 是刻意的**）。守門在閘門⑬（9 項）。⚠ `__init__` 的 re-export 名單改成**從 AST 生成**（手列實測漏過 9 個）。
- **套件化的端到端驗收**：兩輪 fixture 重放，與重構前無法區分。

## 2026-09-02

- **`mixed` 的答案側第一次量到；R6「web 未採用」揭露上線**（閘門⑱ 8 項）。`check_news_routing` 的 `mixed` 從「不判定」改成**不對稱判定**——`web_grounded` 仍不判定（要判需逐宣稱歸屬＝感知），但 `kb_only`／`ungrounded` 判危險且是純規則。少了這一格，22 題的危險方向完全沒有量尺；加上去當場撈出 `mh-09`。⚠ 第二軸的歸因條件改成**讀判定表**不是寫死 `kd == "flow"`。
- **R5「並陳必須帶時點」validator 上線**（閘門⑰ 12 項、6/6 變異）。⚠ R4 抓不到這個（它的 bucket key 含 `unit`，`$4.75 兆` 與 `$4342.02 billion` 落到不同桶）。⚠ **判別力幾乎全在誤報對照，其中三條是乾跑實際踩出來的**：U+2011 不換行連字號（281 份答案的 4 個觸發有 3 個是它造成的誤報——**凡是拿 regex 讀 LLM 寫的日期都要先正規化 dash**）、新聞敘述型引用（第一版觸發率 28%，收窄成「兩邊都要有可比較的金額」後變成 1/123 且是真陽性）、量級差 >10 倍。
- **重錄 `eval/web_fixture.json`：live 量尺不再 flaky，成因是 fixture 不是斷言。** 重錄後 4 輪判定逐題完全一致，而**四輪的答案全都不一樣**——後者才是「斷言沒被凍住」的證明，只看前者會把「量尺死了」誤讀成「量尺穩」。
- **`llm_replay` 唯讀模式**（閘門⑫ 10 項）：A/B 共用 fixture 時 `atexit` 回寫造成的**單向污染**（先跑那臂的 miss 變成後跑那臂的 hit，實測 hit 23→32）。守衛放在 `put()` **早退**而不是 `_flush()`（守入口不是守出口）。
- **null results**：① web query 形式（帶不帶 ticker）第二輪沒重現，**不動 `_web_query_en`** ② `depends_on` 的實際解析在題庫上量下去沒有實例，當時撤回（09-06 改用宣告式重做）③ `mixed` 的計畫側 0/66 且完全穩定。

## 2026-09-01

- **新聞題「拿財報冒充新聞」第一次有量尺**（新檔 [`eval/check_news_routing.py`](eval/check_news_routing.py)）。KB 依設計沒有新聞，所以新聞題引用 10-K／10-Q 就是缺陷——而 Synthesize 的五道 validator **一道都不會響**（chunk 是真的、數字溯源得到、期別也對）。⚠ **判別力集中在一條規則：承認措辭必須與流資訊範圍詞同句**（整篇比對會在 40 份答案裡誤報 3 筆，三型各有逐字凍結的誤報對照）。
- **路由改成 todo 上的 `route` 欄位**（五步完成，閘門⑪ 30 項）：Plan 輸出 `route` → 雙格式相容解析 → 確定性分派 → `_WEB_TODO_RE` 連函式一起刪除。⚠ **⑪t~⑪y 是變異測試逼出來的**：把 `_effective_route` 的隔離整段拿掉（snapshot 也照 route 走 ＝ eval 直接連網），**當時兩支閘門一條都沒響**——因為閘門① 的 `_gate()` 是**抄寫不是 import**，抄的還是加 route 之前的條件。**隔離必須在它現在真正住的地方再被驗一次。**
- **路由分類探針**（新檔 [`eval/probe_route_classification.py`](eval/probe_route_classification.py)）：定性財報題的誤判 **13/75 → 0/75**，flow 那半 0/80 不動。⚠ **必須帶 `--as-of` 且與被比較的那次跑分一致**（第一版漏了它，量出 0/115「完全穩定」而同一題在真實跑分裡翻面過）。

## 2026-08-29

- **web 結果的日期：`published_date` 全是空的，而日期就寫在內容裡**（閘門⑩）。**危險方向是不對稱的**：抽到太新的日期會讓過期頁冒充新鮮並替整池背書，抽不到只是維持現狀 ⇒ 誤報對照（未來的財報日／除息日、歷史表格列、無標記）比陽性斷言重要。⚠ `_MARKER_WINDOW` 的**數值**（48）沒有任何斷言分得出來——那是判斷，不要當成有證據支持。
- **Replanner 的浪費：量出來、修錯兩次、第三次才對。**
- **live 量尺三週的靜默失效：查清三個成因，修掉兩個，第三個要重錄 fixture**（見 [`docs/EVAL.md`](docs/EVAL.md) §4.5）。其中最重要的修法是把 `FixtureMiss`／`RecordError`／`ReplayCacheMiss` 移出 `Exception` 階層改繼承 `BaseException`——**一個「絕不可被吞掉」的保證要寫成型別不是契約**（全碼庫 16 個 `except Exception`，漏一個就破功且破得靜默）。閘門⑦ 9 項、4/4 變異。
- **補上 multi_hop「哪一家最高」的斷言**（新檔 [`eval/check_comparison_claims.py`](eval/check_comparison_claims.py)）。全碼庫**沒有一行 Python 在做這個比較**。⚠ 真值**從答案自己的 `contexts` 算**，這樣 FAIL 的意思才毫不含糊：**數字就攤在它面前還是挑錯了**。⚠ 「答案宣稱哪一家」是**感知不是規則**，第一版在 40 筆裡誤報 2 筆（題目重述被當宣稱、「A 領先於 B」的錨點後面是輸家）。

## 2026-08-28

- **補上 `relevant_ids` 的量尺**（新檔 [`eval/probe_relevant_ids.py`](eval/probe_relevant_ids.py)）＝承接池的實際守門員，在此之前 `eval/` 裡每一筆 `relevant_ids` 都是測試樁。⚠ **公司層的陰性對照在自然候選池裡不存在**（ticker hard filter 在上游就濾掉了）→ 改用 `MIX` 合成混池。⚠ 「gold 檔的**部分** chunk 沒被圈選」**刻意不算缺陷**（第一版把它當危險指標，量出「4 題危險」——那是量尺的錯）。
- **`realtime_need` 對「近期動態」措辭的誤判**（prompt 修法）：0/3 → 5/5、1/3 → 5/5，四題陰性對照不動。加的三條規則裡有一條是**保護陰性對照**的（「最新一季」指財報期別不是 wall clock，仍填 `none`）。
- **歸因改掛在 todo 的出身上**（閘門⑮g）：`_node_replan` 另外加一個 todo 時，那個 todo 的 rnd 0 照樣算「第一輪」→ 假前提從另一扇門回來而 ⑮f 全綠。所以 `_node_plan` 建的標 True、`_node_replan` 建的標 False。
- **agentic 接回期間降級揭露**：`_retrieve_chunks` 原本寫成 `chunks, _note = ...` **整句丟掉** ⇒ 「系統會不會揭露」在所有 agentic 評測上結構性恆為 0，**量到的 0 是在覆述一行程式碼**。收集點只能有一個（`_RunState.period_notes`），且**必須跟著每一次重生成走**（16 個接點）。
- **產品名解析成母公司**（AWS → AMZN，`resolve_tickers_llm`，只在 regex 沉默時才叫）。新檔 `probe_ticker_resolution.py`——**只有這一支在量它**（eval_set 65 題全由 regex 解出）。

## 2026-08-27

- **多年語料升生產**：`mdna` → `multiyear`（21 → 77 份），並**同一次**翻開 `RQ_PERIOD_INTENT_LLM`（分兩次做會讓當期題退步）。兩半證據見 [`docs/EVAL.md`](docs/EVAL.md) §5。
- **修掉「Tier 1 命中 ≠ 答得了」**：label-year 那半個 OR 會冒充財年命中（`tier1_hit_is_qualified`）。**65 題題庫一題都不受影響——這正是它從沒被抓到的原因，不是它不重要。**
- **拒答不再附假的引用清單**（閘門⑬）：實測既有結果檔 4175 份答案／59 份拒答，**21 份帶著引用尾巴出貨**。⚠ **危險方向不是漏判而是判過頭**（真有依據的答案被當拒答 → 四道 validator 一次全部跳過）。
- **`eval/judge_regression.py` 修好**（壞了一個月）。
- **`anchored_pct` 量尺第六次失效**：切片把百分比 token 剖半。

## 2026-08-26 ~ 08-19（多年語料壓測與 KB 拔除新聞）

- **08-26 多年語料修法定案**：`RAG_CROSS_PERIOD_COLLAPSE`（`keep=2`／`prefer=newest`）＋ 期間意圖 LLM。**四次自我否決**（`prefer=rank` 嚴重退步／`prefer=hybrid` 等於不修／`keep=1` 拿趨勢題換／A 的第一版方向弄反）見 `docs/EVAL.md` §5.2。**null results**：跨期近重複去重／MMR、排序層新近度 tie-break。
- **08-25 ratio 意圖交給 LLM**（詞表退位成 fallback）＋ 生成端禁止四捨五入。⚠ 後者是 null result：規則被明文違反，發生率在噪音內、**量不出效果**（規則本身沒有害處，死的是「能證明它有效」）。
- **08-21 `lex-17` 的兩個真因都在「我修的那一層下面」**；after 臂是 null result。
- **08-20 live web 取代 KB 新聞：②內容這一半量完**——可信（PASS 35／FAIL 5），但抓到一個 **live 專屬**的引用缺陷（`Reference N, chunk #M` 指向不存在的東西，snapshot 0/100 vs live 5/37）。⚠ 修法的守門條件比修補本身重要（**失敗方向不對稱**：猜錯＝把「無法追溯」變成「看起來可追溯的錯引用」）。
- **08-20 量尺重建（65 題）**：兩組常數是分開失效的，也要分開修好。
- **08-19 KB 拔除新聞**：只留「記錄」，把「流」交給 live web。同日連帶修掉「財報被誤用來證明候選池夠新」（`_is_freshness_evidence`：只有 8 碼真實日曆日期算證據），而**時效警語一度整個死掉**（判準失去指涉對象 → 恆回空）→ 同日補回新判準「需要即時資料卻沒拿到 web」。
- **08-19 檢索層兩條期別修法**（跨期 field collapsing ＋ 期間意圖解析）；**多年語料壓力測試**（21 → 77 份）；量尺補回 `lex-16`／`lex-17`（而 `mi-05` 的缺陷**沒有**被拔除新聞修好）；**新增 R4**：web ↔ 財報數值衝突要求**並陳**而不是裁決；新增 `probe_news_web_routing.py`（先做便宜的那一半）。

## 2026-08-18 ~ 08-11（ingest 切塊層定案）

- **08-18 Groq 讓 `llama-3.3-70b-versatile` 退役 → 表格摘要換 `openai/gpt-oss-20b`。** 換這個模型必須先跑 bake-off：它是推理模型，`TABLE_SUMMARY_MAX_TOKENS` 太小會讓 reasoning token 吃光正文額度、**靜默吐空字串**（實測 gpt-oss-120b @200 是 10/10 全空）。
- **08-15 數字溯源稽核**（`find_untraceable_numbers`）：一道**沒有實測正例**的防線。**期別稽核**（答案自稱「最新一季」卻引用較舊期別）見 `docs/AGENTIC.md` A8——⚠ 其中一次誤診每一步都對、結論卻錯，把 coverage 印出來才發現 KB 一點都不缺。**live web 的可重現評測**：把不確定性切開，而不是硬定 gold。**兩個被自己的探針證偽的假設。**
- **08-14 檢索側 LLM 換模型（20b vs 120b）：候選會動，gold 不動** ⇒ 兩個模型等價，而 20b 在 NIM 上**慢一倍**（原本「用小模型求快」的前提是假的）。**多公司期別 probe**：損害是真的，但期別 filter 修不到——病灶是**席位競爭**。**web 這條路的九個缺陷：主因是自己把摘要截掉**（`content[:300]`，而數據頁的數字排在站台樣板文字之後）。**單發 vs agentic 全量對照**：整體「沒有顯著改善」，但那是**抵銷出來的**（multi_hop +0.267／lexical −0.078）。
- **08-13 live／web 這條路修通**（三個阻塞點、來源白名單、Grader 時效判準）。**`us_stock_rag_edgar_mdna` 升生產；切塊這條路確認到頂**（量尺飽和）。
- **08-12 幅度接地判準下移到 `_merge_small_chunks`**（section 層修法被實測推翻）。全量 100 題 ＋ RAGAS 解開了 head-vs-period 的懸案。`mix-03` 改用 `require_text`（`forbid` 對它結構上不安全）。
- **08-11 數字缺陷量尺加兩個斷言型別**（`require_text`／`require_chunk`）。

## 2026-08-08 ~ 08-01

- **08-08 抓取／處理分離：ingest 不再連網**（三個會靜默出錯的坑見 `docs/INGEST.md`）。**期間章節硬邊界**（chunking ②）。
- **08-02 `SYSTEM_PROMPT` 新增 Rule 11-13**（單位／方向／per-intent）＋ **確定性單位換算 `convert_usd_units_to_yi()`**：billion→億 是 LLM 翻譯層的 token 習慣，prompt 只能隨機壓（實測「叫它 ×10」兩跑一對一全錯）。正解＝Rule 11 要 Writer 原樣保留 `$X billion`，再由**純程式**乘算，**零誤報**。
- **08-01 agentic `GEN_MODEL` 定案 `gpt-oss-120b`**：候選 benchmark 只量了速度/品質、**漏了「per-model 限速」這一維** → `glm-5.2` 單發 127~217s、`deepseek-v4-pro` 品質最佳但有 429 硬牆（100 題必團滅）。同時給 `_nvidia_call_llm` 加 429/5xx 指數退避（先前無重試，長 run 撞日配額 → 56/100 題掉進機械 fallback、**汙染生成分數**）。

---

## 早期演進（2026-07，架構已被取代，保留摘要供追溯）

⚠ **這一段的數字全部不可與現況比較**：那時是 100/90 題題庫、舊 collection（`us_stock_rag_unstructured`／`exp1`~`exp4`）、rubric + judge 評測、Groq backend、且 agentic 還在 deepagents 世代。

**agentic 的三次架構世代**：① deepagents 雙層 agent（自主性太高，檢索品質退步）→ ② 混合版（檢索用 agency、生成由 Python 強制接管）→ ③ **LangGraph 全面重寫**（現行架構的起點）。**煙槍在「ReAct 執行層亂改 query」**——乾淨 decomposition 的 gold-chunk 覆蓋 35/75 vs 真實 ReAct 16/75。
→ **逐項記錄（①~⑩，生產碼有 7 處 docstring 用那個編號引用）在 [`CHANGELOG_AGENTIC.md`](CHANGELOG_AGENTIC.md)**，這裡不重複。

**主管線的關鍵定案**（理由已寫進 `README.md`〈Design Decisions〉與 `docs/EVAL.md`）：
- **07-22 LLM backend 統一改 NVIDIA NIM**（取代 Groq 的 TPD 硬牆）。同期修掉 `gen_reference_answers.py` 的續跑快取只認 id 不比對 query（eval_set 改版沿用舊 id 換了題目 → **47/90 題撈到文不對題的舊 reference**，修好後 context_recall 0.345 → 0.679）。
- **07-21 新聞 hard filter**（後於 08-19 隨新聞拔除而永久閒置）。
- **07-20 rewrite × translate 是負交互**：各自單開有小增益，疊加全數抵銷。方案 A「evidence-first 生成」機制成功但分數不動 ⇒ 病灶在 judge/框架層。**逐題 run-to-run std 普遍 0.2~0.5，遠高於 overall std** ⇒ 過去多次「單跑一次定案」不可靠。
- **07-19 修復「靜默降級污染結果檔」**（第三次踩同一坑）：三個 query-understanding 函式對 LLM 例外一律靜默降級，TPD 耗盡時分數照樣算——**量到的其實是「沒開 rewrite」**。修法：確定失敗就整輪中止（exit 2，已完成題目可 resume）。
- **07-16 100 題 baseline 首跑**；修 2 題 rubric 缺陷；發現 judge 的「億」單位換算誤判、生成層 billion→億 誤譯（新類型）。
- **07-15 RAGAS ground_truth 改用完整參考答案**（rubric 清單合成會讓篇幅錯配、F1 精確率崩到 ~0.31）；`.venv-ragas` 獨立環境自此成立。
- **07-13 Rerank 延遲攻堅**：根因是 cross-encoder 在純 CPU 上評分 20 候選要 ~46s。`batch_size=1` 免費 2.33x 加速零損失、`max_length=2048`（全庫僅 1.6% 超標，與未截斷逐位一致）→ 130s → 37s。**ONNX/小模型/級聯三條路都失敗**（見 `docs/EVAL.md` 已試無效總表）。
- **07-07 重大發現：sem-02/08/11 的排序問題根因是 cross-lingual rerank**（中文問句 vs 英文文檔評分嚴重失真）。翻成英文後命中分數暴漲——**這是先前所有「調數量/multi-query/chunking」路線全部無效的統一解釋**。同時建立「單次跑 mean 差 <0.1 是噪音」的量測紀律（`--repeat k`）。
- **07-06 溫度拆分**（檢索側/judge temp=0、生成 temp=0.3，greedy 會 mode collapse）；**Rule 9 多公司比較題強制逐家點名**；**Qdrant local mode → Docker server mode**（local 是排他檔案鎖）。
- **07-11 教訓（仍然適用）**：colloquial 首次驗證推翻「口語 degrade」假說——7 題非滿分裡 3 題是純測量 bug。**分數低不要急著找新機制，先排除判定工具本身的既有毛病。**
