# CHANGELOG

紀錄本專案每次有意義的程式修改（架構調整、參數變更、新增功能、放棄的實驗）。新條目加在最上面。
**每筆條目只留「改了什麼、關鍵數字、結論」**，診斷過程與推導細節見 `docs/` 與 git log，不重述。

## 2026-09-10

### 65 題第三輪：三個我自己前兩輪寫下的結論被推翻，一個量尺的錯前提被實跑抓到

`experiments/agentic/gj_65q_r3_20260910.json`（65 題、`--no-web`、snapshot、
**不掛 `RAG_REPLAY_CACHE`**、組態與 R1/R2 逐字相同）。**第一份帶著 `retrieved_union`
與 `degraded_reason` 的全量結果檔**，也就是三段漏斗第一次跑得動。
⚠ **一次 repair 就是 0 降級**（R2 要三輪 9→4→0）——支持「時間叢集不是系統性」那個判讀。

**被推翻的三件事**（都寫在 [`BACKLOG.md`](BACKLOG.md) T0）：

1. **「穩定核心 5 題」縮成 3 題。** 三輪都觸發 `forced_pass` 的只有 `lex-04`／`mix-12`／
   `sem-14`；`lex-10`／`sem-15` 第三輪不觸發，而 `mh-01`／`mh-05` 是新冒出來的。
2. **「multi_hop 兩輪都是 0%」不成立。** R3 是 2/28＝7.1%。三輪之後**逐類沒有一格是穩的**
   （R3 五類全擠在 5~11%）⇒ **不要引用任何單類的 `forced_pass` 數字**。
3. **「revision 退回恆為 0 ⇒ 這條鏈沒在互相破壞」不成立。** R3 是接受 9／退回 1
   （`lex-03`：`reflect: untraceable 0→2`）⇒ 鏈**確實會**互相破壞，只是頻率低
   （三輪 24 次重生成 1 次）。**決定仍然是不做 critique↔refine**，但理由換成
   「接受守衛零成本地擋住了，而 critique↔refine 要買的那件事仍然沒有量尺」。

**另外兩個第一次量到的東西**：

- **`unit_stats` ① 首次非 0**（4 筆／3 題，前兩輪皆 0 ＝ 沒量到不是不發生）。
  `col-12` 那兩筆 `60,801 百萬元（約 $60.80 億）` 是**真的差 10 倍**，① 攔下、② 重算成
  608.01 億 ⇒ 閘門⑲ 那一層是有負載的。
- **`forced_pass` 不等於答錯**：`mh-01`／`mh-05` 這輪是 forced_pass，而
  `check_comparison_claims` 對它們判 **PASS**。同輪 `check_number_defects` 18/18 一致、
  0 候選。⇒ 那個分母量的是「Grader 說不足而系統照樣答」，**不是答錯率**。

**三段漏斗第一次跑得動**（`experiments/gold_funnel_r3_20260910.json`，41 題可量）：
① 進了 Generator 卻沒引用 **0**／② 撈到了被圈選丟掉 **3（7%）**／③ 真的沒撈到 **8（20%）**／
④ 保底補撈救回 **1**／真的資料不一致 **0**。

⚠ **這推翻了我 09-09 交接時的排序。** mh-03 單題確實是「撈 78 顆、只有 6 顆進
`collected`（92% 被丟掉）」，但**全量上 gold 落進 ② 的只有 3 題**、落進 ③ 的有 8 題
⇒ **丟掉的比例很高，但被丟掉的多半不是 gold**。「丟掉 92%」與「92% 的損害」是兩個
不同的分母，我把前者講成了系統最大的問題——**那個排序不成立**。

⚠ **量尺的錯前提是實跑先報 FAIL 才現形的**（「稽核回報 FAIL，先問是不是量尺錯」）：
`probe_gold_funnel` 原本斷言 `sources ⊆ retrieved_union` 是**結構不變量**，實跑印出
「✗ 破了，資料錯，先查這個」。查下去發現 `_ensure_ratio_source_coverage` 最後一段
（`_fetch_fundamentals_with_field`）**直接打 Qdrant、繞過 `run_state.pool`**
⇒ **進入答案有兩條入口，`retrieved_union` 依設計只涵蓋第一條**，那從來就不是不變量。
而 `lex-17` 的 gold（`MSFT_Fundamentals_20260612.txt#0`）**從來沒進過候選池**，
是保底撈回來的 ⇒ **量尺把一次成功報成了一種病**；少了新的 `used_via_backstop` 那一格，
它還會落進③「檢索根本沒撈到」＝ 把「檢索漏了但系統救回來了」讀成「檢索漏了」。
修法：新增 `used_via_backstop`，**既不可併進 `used`**（那會讓「檢索漏了」看不見）
**也不可併進 `cited_not_retrieved`**（那會稀釋真正的資料錯），逐類三欄不含它、同行另標。
判別力在 ⑫c／⑫d／⑬b 三條誤報對照，**4/4 變異全抓到**，selftest 16 → **20 項**。

## 2026-09-09

### `kb_unfixable` 這條線：我推導錯兩次，現在有量尺了

**背景**：09-08 的判讀是「`kb_unfixable_exit` 兩輪 225 個子問題一次都沒觸發 ⇒ 讓它觸發是
`forced_pass` 的第一順位」。

**錯誤 ①**：`kb_unfixable` 只在 `_check_sufficiency` 的 `if _live:` ＋ `_classify_staleness`
改判時設 ＝ **時效**天花板的旗標；而 28 筆 `forced_pass` 的 `missing` **沒有一筆在講時效**，
全是**內容**（「10-K 沒寫 CUDA 推出年份」「沒有中國市場出貨量」）。**兩件事從頭就不是同一件。**
⚠ `BACKLOG.md` 第 682 行早就寫著這句話，我仍然重新推導出相反的結論 →
**下結論前先 grep 自己的 BACKLOG**（同「提實驗前先 `ls experiments/`」）。

**錯誤 ②**：改成「財報題庫上 `realtime_need` 依設計是 `none` ⇒ 結構上不可能觸發」，
證據是煙霧測試 10/10——**樣本太小**。完整一輪（25 子問題 × 3 輪 × 2 臂 ＝ 150 次判定）：
`realtime_need` **none 124／days 14／intraday 12**，`kb_unfixable` **True 17 次**，
全在真正的即時題（`col-09`「蘋果現在市值」、`col-03`「亞馬遜最近跟哪家 AI 公司搭上線」）。

**新增量尺** `eval/probe_kb_content_ceiling.py`（`experiments/kb_ceiling_20260909.json`）：
兩臂 `prod` vs `wide`（`FETCH_N` 60→180、`RRF_TOP_N_PRIMARY` 20→50、`POOL_RETURN_K` 5→15），
四格 `prod_sufficient`／`rescued_by_width`／`ceiling_recency`／`ceiling_content`。

| | 結果 |
|---|---|
| `forced_pass` 子問題 10 筆 | **`ceiling_content` 8**／`rescued_by_width` 1（`mix-04`）／`prod_sufficient` 1 |
| 陰性對照 15 筆 | `prod_sufficient` 11／`rescued_by_width` 1／`ceiling_recency` 2／`ceiling_content` 1 |
| **內容誤殺率** | **0/15**（唯一那筆 `lex-06` 是市值題，只因旗標閃爍才落到內容格） |
| 旗標穩定性 | 在**凍結的**候選池上，5 個即時子問題有 **4 個**跨輪翻面 |

⇒ **`ceiling_content` 是真正的缺口**（8/10 在 3 倍寬的檢索下仍不足，而系統對它沒有任何旗標），
且陰性對照顯示做一個內容旗標的**誤殺風險是 0/15**。

⚠ **`kb_unfixable_exit` 在生產是 0，仍未解釋。** 旗標閃爍是最可能的線索但沒有證實——
今天已在這題上編錯兩次機制，不編第三次。**下一步是觀測不是修法**：把 `realtime_need`／
`kb_unfixable` 逐輪記進 `exec_stats`。

⚠ **量尺自己也錯一次**：第一版把兩種 ceiling 混成一格，三筆即時題被算成「誤殺 3/15」。
拆開後是 0/15。`--rescore`（零 LLM 從輸出 JSON 重算判定）就是為此加的——判定規則改了
不該重燒 45 分鐘的 LLM＋檢索。

⚠ **順帶確認 eval 隔離沒破**：迴圈裡 `eff_route = _next_route` **不會**重新套
`_effective_route`，所以升級後 `eff_route` 可能是 `"web"`；但 `web_search` 工具第一行就檢查
`_pkg.ENABLE_WEB_SEARCH` → 回「已停用」。兩個獨立條件各自足夠這件事仍然成立。

### 兩個觀測通道：降級成因、檢索候選池聯集（只加觀測，行為逐字不變）

**病灶一：降級的成因無處可查。** `run_agentic` 的 `graph.invoke` 崩潰降級只用 `_trace` 印
（非 verbose 完全不輸出），而降級記錄**外觀完全正常**（有 answer、無 error、resume 還跳過它）
→ 2026-09-08 三題、09-09 九題降級，成因全是猜的。

**病灶二：「撈到了卻沒被用」結構性量不到。** 結果檔的 `sources` 是
`rq.retrieve()` → `run_state.pool` → **Grader `relevant_ids` 圈選** → `[:COMMIT_TOP_K]` →
`picked` → `collected` 這條鏈的**最末端**（65 題中位數 3 顆），於是 `probe_gold_funnel`
那一格兩輪都是 0——**那是量尺的性質不是系統健康**。⚠ 這條鏈歸因錯過兩次（先說「跨子問題的
聯集」、再說「`_fair_select` 的產出」，而後者根本不被 `run_agentic` 回傳）。

**做了什麼**（`agentic_rag_version/{__init__,graph}.py`、`eval/{run_agentic_on_evalset,
repair_degraded_records,probe_gold_funnel}.py`）：

- `degraded_reason`：例外型別＋訊息＋**最深一層的檔:行**，寫進 record。⚠ 只帶 `repr(e)` 的
  資訊量與現行 `_trace` 相同 ＝ 沒解決任何事（閘門㉒d）。
- `retrieved_union`：各子問題 `run_state.pool` 的 key（**Grader 圈選之前**），逐子問題標記，
  跨波累加。去重鍵是 `(subq, source, chunk_index)` **三元組**——同一顆被兩個子問題撈到要留
  兩筆，那正是這個通道要回答的問題。
- 兩者都遵守**缺席 ≠ 空值**：`degraded_reason` 缺席＝沒降級、`retrieved_union` 缺席＝沒經過
  graph（同 `unit_stats` 的 `None` ≠ `{}`）。
- `repair_degraded_records` 改用新指紋並印出成因，**舊指紋保留**（既有結果檔沒有新欄位，
  只認新的會讓它們整份變成「沒有降級題」）。
- `probe_gold_funnel` 升成三段漏斗，舊結果檔自動退回兩段版並**分群印**（兩群的 ③ 不是同一
  個東西，不可合併）。

**第一個數字**：mh-03 單題 6 個子問題、候選池聯集 **78 顆去重 chunk，只有 6 顆進 `collected`
——92% 被 Grader 圈選＋`COMMIT_TOP_K` 丟掉**。⇒ **修檢索對那 72 顆完全無效**，
「32% 的可量題有檢索層缺陷」這句話下面藏著一個更大的漏斗。

**驗收**：閘門㉒（23 項，`verify_answer_validators` 316 → **339**，全 PASS），
9/9 變異全抓到；`probe_gold_funnel --selftest` 16/16；`repair_degraded_records --selftest` 7/7；
四道確定性閘門全 PASS（85／339／17／246）。

⚠ **兩個量尺自己的錯，都留在紀錄裡**：① ㉒n3 第一版走 `ast.Return`，而 crash 兜底那個 dict
是**指派**不是 return → 當場 FAIL 而系統是對的。② 變異「去重鍵漏掉 subq」第一版只改迴圈裡的
`k` 沒改 `seen`，實際變成「去重從此不觸發」→ 被 ㉒l 抓到而 **㉒k 那條路一次都沒被走到**。
**變異本身也要自洽**，否則「抓到了」是抓到別的東西。

⚠ **既有結果檔沒有這兩格**，要重跑一輪才量得到三段漏斗。


### 五個分母的第二輪：兩個 R1 的結論被推翻，四個決定可以定案

`experiments/agentic/gj_65q_denominators_r2_20260909.json`（65 題、0 降級，repair 三輪：9→4→0）。

| 分母 | R1 | R2 |
|---|---|---|
| `forced_pass`（子問題／題） | 18/116 ＝ 15.5%／10 題 | 10/109 ＝ **9.2%**／7 題 |
| `kb_unfixable_exit` | 0 | **0** |
| `crashed` | 4 | 3 |
| `refused_budget` | 2 | **0** |
| `deps_disagreed`／`deps_pruned` | 2／0 | 1／0 |
| `revision` 接受／退回 | 7／0 | **7／0** |
| `unit_stats` ①／③ | 0／10（7 題） | 0／**23**（8 題） |

**定案四則**：① **不拆 `MAX_TODOS`**（兩輪 155 個 replan round 只擋掉 2 個待辦，且撈的是
已在 collected 裡的 chunk）② **不做 critique↔refine 迴圈**（兩輪接受 7／退回 0，四項指紋一次都沒變差）
③ `deps_disagreed` 的例外維持（兩輪非 0、`deps_pruned` 兩輪 0）
④ **`forced_pass` 的第一順位是讓 `kb_unfixable` 真的觸發，不是加揭露句**——
兩輪 225 個子問題那個旗標一次都沒設起來，而 `missing` 有 ~14/18 正是 KB 天花板。

**被第二輪推翻的兩個 R1 結論（都是我寫的）**：
- 「`crashed` 全在 multi_hop」**不成立**：R2 落在 `sem-05`／`sem-10`／`mh-01`。是**時間叢集**不是類別。
- 逐類 `forced_pass` 只有兩格穩：`semantic` 兩輪最高、`multi_hop` 兩輪 0%；
  `colloquial` 23.5% → **0%**，不可引用。

**`probe_gold_funnel` 在 R2 重跑**：30/41（73%）vs R1 的 31/41，**41 題裡 40 題判定相同**
（只有 `mix-12` 翻面）＝ 第一段指標穩定；「撈到卻沒引用」兩輪都是 0，與「結構性接近恆真」一致。

⚠ **新的觀測性缺口（未修）**：graph 崩潰降級的例外只用 `_trace` 印、非 verbose 不輸出 →
結果檔與 log 都查不到「為什麼降級」。R1 三題、R2 九題，成因至今是猜的。
修法：把 `repr(e)` 寫進 record 的 `degraded_reason`（行為外的觀測性改動）。

### 召回失敗的層級歸因：`RRF-20` 名額用滿、collapse 一筆 gold 都沒吃掉

新增 [`eval/probe_recall_layer_attribution.py`](eval/probe_recall_layer_attribution.py)（selftest 8/8，5 條誤報對照）。
41 題 × 2 輪，`experiments/rla_20260909.json`。把 09-08 量到的 9 題召回失敗拆成四格：

| 格 | 題數 | 題 |
|---|---|---|
| ① collapse／filter 刪掉 gold | **0** | —— |
| ② RRF 名額不夠（`RRF_TOP_N_PRIMARY=100` 才進得來） | 2 | `lex-17` `sem-03` |
| ③ prefetch 名額不夠（`FETCH_N=300` 才進得來） | 3 | `col-01` `lex-04` `sem-01` |
| ④ 真的撈不到 | 3 | `lex-03` `lex-14` `sem-05` |
| 跨輪翻面 | 1 | `lex-07` |

- **① ＝ 0 是乾淨的 null result**：跨期 collapse／三層 filter／去重沒有吃掉任何 gold。
- **5/9 是名額問題**，且 `FETCH_N`（3 題）比 `RRF_TOP_N_PRIMARY`（2 題）更值得動。**兩個常數要分開 sweep。**
- ④ 的三題有兩題同形狀（`lex-03`／`lex-14` 都是「某公司某財年的 Diluted EPS」）→ 結構性成因，不是查詢向量問題。
- ⚠ **一路上讀錯過兩次同一個數字**：`probe_chunk_gold_recall` 印的 `pool` 是 `retrieve()` 的**回傳長度**
  （RRF-20 被 filter／collapse／去重刪剩下的），**不是候選池**。09-08 據此推出「池是滿的」、
  09-09 上午又據此推翻成「名額沒用完」——**實測 `pre20` 全部都是 20**，原判讀方向才是對的。
  `@20` ≠「整個候選池」這件事已寫進 CLAUDE.md 與 BACKLOG。

### `gold@cited` 漏斗：量出來的比預期窄，而那本身是結論

新增 [`eval/probe_gold_funnel.py`](eval/probe_gold_funnel.py)（selftest 9/9）。
41 題可量：撈到且引用 31（76%）／**撈到卻沒引用 0**／沒撈到 10。`experiments/gold_funnel_20260908.json`。

- ⚠ **那個 0 照規則當壞消息查，確實是量尺的問題**：`sources` 是 `_fair_select` **之後**
  交給 Generator 的 5 顆，不是撈到過的聯集 → **5 顆裡的 gold 幾乎必然被引用**＝第二段結構性接近恆真。
- ⇒ **「撈到了但被丟掉」目前量不到，缺的是資料不是方法**：結果檔沒有子問題檢索的聯集。
  下一步是在 `run_agentic` 加 `retrieved_union` 觀測通道（同 `unit_stats`／`exec_stats` 的作法）。
- **仍有用的一半**：與單管線臂逐題比對只有三題分歧，其中 `lex-17`（單管線 gold 不在候選池、
  agentic 撈到且引用）是**「子問題拆解改善召回」目前唯一的直接證據**；`mix-08`／`sem-04`
  的分歧被兩支的 **k 不同**解釋掉（top-50 vs 最終 5 顆），不是 agentic 的缺陷。

### 讀完 09-08 那一輪的三批明細（零成本，讀既有結果檔）

- **確診缺陷（未修）**：`_fill_dependent_hop` 用 `_DEP_PLACEHOLDER_RE.sub(entity, task)`
  把**每一個** `#N` 都換成同一個實體 → `mh-03` 實際送出的子問題是
  「在 **NVIDIA、NVIDIA、NVIDIA** 中，哪一家的最近一年營收年增率最高？」。零 LLM 可重現。
  底層成因是 `depends_on` **單一 int 表達不了多父依賴**，而比較型的 hop 天生多父。
  ⚠ 該題**最後答對了**（三家 Fundamentals chunk 都在池子裡，Generator 自己比的）→ 端到端跑分看不到。
- **`MAX_TODOS` 維持 7**：被額度擋掉的 2 個待辦（META／GOOGL 的 TTM 淨利）雖然有意義，
  但那三顆 chunk **本來就已經在 collected 裡**＝重複撈。復活條件：出現一筆被擋掉、
  而該資訊確實不在 collected 裡的待辦。
- **`kb_unfixable_exit` 整輪 0，而 18 筆 `forced_pass` 有 ~14 筆正是 KB 天花板**
  （「神經網路訓練細節」「Android 開發者人數」「與其他車廠的比較」…）。
  ⇒ **第一順位不是加揭露句，是讓 `kb_unfixable` 真的會觸發**——那條路早就接好了。
  這是 `_check_sufficiency` 的判定品質問題，要先有 probe（陰性對照不可省）。

### 順帶：`_extract_citations` 對 `;` 串接的引用整段抓不到（生產缺陷，未修）

`_CITE_RE` 要求 chunk 編號後緊接右括號 → `[A, chunk #20; B, chunk #19]` 抓到 **0 筆**（不是部分）。
65 題結果檔 363 個引用區段裡 **5 個（1.4%）**，分屬 `sem-01`／`mix-10`／`mh-01`／`sem-13`。
⚠ 影響閘門㉑ 接受守衛的 `citations` 指紋（漏報方向）。未修：改它會動到既有判定，要配斷言＋跑全閘門。

## 2026-09-08

### 補上「生產檢索到底撈不撈得到」這把尺（`probe_chunk_gold_recall.py`）

**病灶不是碼，是一個沒有證據支撐的結論。** CLAUDE.md 與 BACKLOG 都寫著「切塊／檢索已經到頂」，
而那個結論的來源是 **100 題 × 舊 judge（`gpt-oss-120b`，已 410）× 舊 collection** 的 RAGAS
檢索三指標。照本 repo 自己的規則（跨 2026-09-04 不可比、跨輪比聚合值＝把 judge 噪音算進系統差異），
**那把尺已經作廢**。而 `chunk_gold.json`（41/65 題、83 顆、literal 定位、逐題人工驗證）
2026-09-04 就建好了，卻**只被 `probe_relevant_ids` 拿去量 Grader 的圈選，從來沒量過
`rq.retrieve()` 本身**。

· 兩條臂：`--arm single`（跑真實 `rq.retrieve`，逐題 gold@5／gold@20／rank）與
  `--from-results`（讀結果檔的 `sources`，量 agentic 的聯集，零檢索零 LLM）。
  **兩欄不可並排比較**——一個是 top-k 切片，一個是跨子問題的聯集，分母定義不同。
· **判讀方式先於數字**：@20 進得來、@5 進不來 ＝ 排序問題；@20 也進不來 ＝ 召回問題。
  合併成一個 recall 數字就分不出來，而那兩格對應**完全不同**的修法。

**寫的時候撞到的兩件事，都寫進 docstring 與報表**：
1. **`@20` 其實就是整個候選池**：`RRF_TOP_N_PRIMARY = 20`，實測中位池只有 **14**（去重與
   hard filter 之後）。所以「召回問題」同時指向那個常數本身（名額不夠），不只是 embedding
   撈不到——而它上一次 sweep 是在舊 collection 上做的。腳本自己印池大小並在 `中位池 < @k` 時警告。
2. **偏誤方向與 `chunk_gold` 原本的用途相反**：gold 是 union、偏大。用在 all-dropped 判定上
   偏大＝更難觸發（安全）；用在**召回**上偏大＝**只會高估**。
   ⇒ **低分是硬證據，高分不是健康證明。**

⚠ N/A 24 題逐類印出、不併進分母（同 `check_number_defects`）。
⚠ 檢索路徑有一次英譯 LLM（同輸入實測 47/100 題 top-8 不同）→ 預設掛 `replay_cache.json` 釘死；
  下結論前 `--repeat 3` 以上並看逐題 k/n。`--no-translate` 是零 LLM 臂但**不是生產組態**。
⚠ `--selftest` 6 項，判別力在③④⑤三條誤報對照（@5 全中不得歸排序問題／跨輪翻面歸「不穩定」／
  @20 全滅才是召回問題）——陽性那兩條，一個「一律回 0」的實作也會過。

**三題 smoke（n=1 輪，不可下結論）**：`lex-03` 召回問題（與 BACKLOG 既有記錄一致）、
`mh-01`／`col-08` @5 就在。⚠ **`col-08` 與 BACKLOG 舊註記「dense rank 25/106」對不上**，
先查是不是尺錯（那筆註記寫於單年 collection 時代）。

## 2026-09-08

### 四個分母第一次量到，其中兩個推翻了原本的假設

**一輪全量 65 題（`experiments/agentic/gj_65q_denominators_20260908.json`，0 降級——
中間補跑兩次，見上一則）＋ chunk 層召回探針 3 輪。**

**① `exec_stats.forced_pass` = 15.5%**（18/116 子問題、**10/65 題**）。
逐類別：semantic 37.5%／colloquial 23.5%／mixed 12.0%／lexical 11.1%／multi_hop 0%。
`kb_unfixable_exit` 與 `web_budget_exit` 都是 0（`--no-web`，預期）。

**交叉分析推翻了我自己的假設**：把那 10 題對上 `chunk_gold` →
**5 題是「撈到了、Grader 仍判不足」**、只有 **1 題（`lex-04`）是真的沒撈到**、4 題無 gold
（而那 4 題 Grader 要的是「神經網路訓練細節」「Android 開發者人數」「與其他車廠比較」
——10-K／10-Q 本來就沒有）。
⇒ **`forced_pass` 不是檢索問題，修法方向是揭露不是撈更多。**
⚠ 推論限制：`sources` 是跨子問題的**聯集**，多 todo 的題較弱；紮實的只有
`lex-04`（1/1 todo，gold 不在聯集）、`lex-10`（1/1 todo，gold 在）、`mix-12`（2/2 todo 全 fp）。

**附帶撈到的**：`crashed` 4 筆**全在 multi_hop**（`mh-01` 1/7、`mh-05` 2/6、`mh-02` 1/6）
⇒ **5 題 multi_hop 有 3 題是帶著崩掉的子問題產出答案的**，而在今天之前結果檔對此完全沉默；
`check_comparison_claims` 40/40 PASS 也照不出來（它只驗贏家對不對）。

**② `replan_stats.refused_budget` = 2（1/65 題）→ 不拆 `MAX_TODOS`。**
09-06 那個「Planner 拆 7 個時 Replanner 預算是 0」的推理是對的，但**實際咬到只有 `mh-03` 一題**。
⇒ 證據不足以動額度（子問題爆炸級聯有前科）。BACKLOG 那一格改成「已量、不動」。

**③ `plan_stats`：65/65 都宣告了 `depends_on`，`deps_pruned` = 0、`deps_disagreed` = 2**（全在 `mh-03`）。
只有 4 題宣告依賴、全是 multi_hop。⇒「說 null 但句子沒主詞仍延後」那個例外**只動了 2 次**，
拿不拿掉影響都極小 → **維持現狀**。

**④ `revision_stats`：接受 7、退回 0、空 0**（7/65 題觸發重生成）。
⇒ **不把 Synthesize 改成 critique↔refine 迴圈**（那會每題多燒 LLM，而目前量到的破壞是 0）。
⚠ 這不代表閘門㉑ 的守衛沒用——它代表這一輪沒觸發，7 次是小樣本。

**⑤ chunk 層召回（41 題 × 3 輪，跨輪翻面 0）：`gold@5 = 0.683`／`gold@20 = 0.780`。**

| 診斷 | 題數 | 哪幾題 |
|---|---|---|
| 召回問題（gold 從沒進過候選池） | **9** | `col-01` `lex-03` `lex-04` `lex-07` `lex-14` `lex-17` `sem-01` `sem-03` `sem-05` |
| 排序問題（@20 進得來、@5 進不來） | 4 | `col-03` `mix-08` `mix-14` `sem-04` |
| 沒問題 | 28 | |

**lexical 的 `@5` 與 `@20` 完全相同（30/30）⇒ 那一類的病 100% 在召回、與排序無關**，
而 9 題召回失敗有 5 題是 lexical（`lex-03`／`lex-07`／`lex-14` 與 BACKLOG 既有記錄一致）。
候選池中位數**正好是 20**（`RRF_TOP_N_PRIMARY` 上限）⇒ 池是滿的、gold 仍不在裡面。

⚠ **這修正了一句被讀錯的話**：「切塊／檢索已經到頂」原本是**關於 RAGAS 這把尺**的結論
（檢索三指標飽和），卻被讀成關於系統。chunk 層 gold 顯示 **32% 的可量題有檢索層缺陷**。
⚠ **但「有缺陷」不等於「修了有用」**：① 的交叉分析顯示 10 題 forced_pass 只有 1 題是檢索問題。
**第二個問題目前仍然沒有量尺**，別把 32% 當成可回收的收益。

### 崩潰降級的題會被 resume 永遠跳過（`repair_degraded_records.py`）

**跑 65 題分母那一輪當場撞到的。** `run_agentic_on_evalset.py` 的 resume 判準是
「**有 answer 且無 error** → 跳過」，而 `run_agentic` 的 graph 崩潰降級路徑**照樣產出 answer、
也不寫 error**（它走一次乾淨的單發檢索+生成兜底）。兩件事湊起來：**降級題會被 resume 永遠跳過**，
於是一份「跑完 65 題」的結果檔裡可能混著 N 題根本沒經過 agentic 管線的記錄，而且外觀完全正常。

**觸發它的是 NVIDIA NIM 的間歇性 404**（證據 `experiments/_nim404_probe.txt`）：
同一個 model、同一段 prompt、同一支 `rq.call_llm`，同一分鐘內 **10/20 失敗**，幾分鐘後 **0/6**。
model 是 `nvidia/nemotron-3-super-120b-a12b` ＝ `rq.DEFAULT_MODEL` 與 agentic 三個常數**同一個 id**。
plan／replan 的 `rq.call_llm` **沒有包 try/except** → 一次 404 就炸掉整個 `graph.invoke`。
⚠ **判定是暫時性事件不是下架，所以刻意不換模型**：下架的表徵是 410 且**持續**
（`gpt-oss-120b` 2026-09-03 就是那樣）；這次是 404 且**幾分鐘內從 50% 掉到 0%**。
換模型的代價是所有基準與 replay fixture 全部失效、且沒得 A/B——不為一場會過去的外部故障付。

· `eval/repair_degraded_records.py`：判準是**五個 stats 鍵全部都是 `None`**。
  ⚠ 這正是那五個通道當初保留 `None` ≠ `{}` 的用途（`{}` ＝ 跑完了只是沒觸發）——
  **這個區分上線第一天就派上用場了**：對照 2026-09-05 的基準 `unit_stats=None` 是 **0/65**，
  而事件當天前 3 題就有 2 題全 `None`。沒有它，我會拿一份混著崩潰題的資料去算 `forced_pass` 發生率。
  ⚠ 刻意用「全部皆 None」不是「任一個 None」：任一個會把舊格式結果檔整份誤判成降級。
  ⚠ 刻意**不自動重跑**（補跑仍走 `run_agentic_on_evalset.py` 同一個入口，免得變成第二個跑分入口）；
  ⚠ 刻意 `--dry-run` 為預設（結果檔是跑了好幾小時的東西）。
  selftest 5 項，判別力全在②③④三條誤報對照。

⚠ **一個我自己寫錯又當場被資料推翻的判讀**：第一版警語寫「降級會系統性偏向 multi_hop／mixed
（LLM 呼叫次數多）」，而實測 3 筆降級**全是 semantic**。真相是 `eval_set.json` **按類別排序**
（前 15 題全是 semantic），而崩潰依**時間**叢集 → **類別與跑的先後在這份題庫裡是共線的**，
那一欄根本分不出兩者。警語已改成講這件事。

### executor 的「重試用完強制放行」在此之前完全不可觀測

**病灶**（2026-09-07 端到端實跑撞到的，不是想出來的）：`_run_executor_*` 的迴圈出口是

```python
if verdict["sufficient"] or rnd >= MAX_REWRITES:   # 註解自己寫著「或次數用盡強制放行」
```

而回傳的 4-tuple `(summary, web_notes, realtime_need, period_notes)` **沒有任何一格**說明它是
哪一種。Synthesize 因此分不出「Grader 判過」與「重試三輪都不夠、硬放」，兩者在結果檔裡外觀完全相同。

**實測那一題**：「Magnificent Seven 裡最近十二個月淨利最高的是哪一家？」hop-1 連跑三輪
`sufficient=False`（Grader 每輪都在列缺哪幾家），答案照樣產出、**零保留**，而且答錯——
答 Apple $122.58B，KB 裡實際最高是 GOOGL $160.21B（AAPL 排第 4）。
**系統早就知道自己資料不夠，那個訊號被丟掉了。**

（那一題答錯的**成因**是另一件事，已登錄成已接受的極限：「Magnificent Seven」是集合詞 →
`_mentioned_tickers` 回 `[]` → `_ensure_ticker_coverage` 的每家保底整個不觸發 → top-5 裝不下 7 家。
這次**沒有**修那個，見 `BACKLOG.md`。）

**這次做的是分母不是行為**（同 `unit_stats`／`replan_stats`／`plan_stats`／`revision_stats` 的順序，
第五個）：`exec_stats` 逐子問題記下**唯一**的出場方式，經 graph state → `run_agentic` → record
寫進結果檔。行為逐字沒變，兩條護欄守著（⑯g 帶不帶參數輸出逐字相同、⑯m 強制放行仍照常產出摘要）。

· `graph._EXEC_OUTCOMES`：五種出場的封閉集合。**四種病不可合併**——只有 `forced_pass` 是缺陷，
  `kb_unfixable_exit`（KB 結構上補不了，提早跳出是**正確行為**且另有時效揭露）、
  `web_budget_exit`（route=web 且 query 級預算用完）、`crashed` 各是別的病。一個
  「出場時 sufficient 為 False 就算 forced_pass」的實作會把四種混成一種＝分母灌水到沒有意義。
· `_note_exec_outcome(stats, outcome, **detail)`：out-param、**set-once**。out-param 是因為
  `_node_execute` 是 ThreadPoolExecutor（模組層全域會被別的執行緒蓋掉，而蓋掉後的外觀與
  「這一題沒觸發」相同）；set-once 是因為 react 在 break 之前還會生成摘要，那一段拋例外會落到
  `except` → 沒有 set-once 就一次執行被算成兩種病。**刻意不改回傳型別**：4-tuple 有三個呼叫端。
· `_merge_exec_stats(prev, per_todo)`：**必須讀 `prev`**——LangGraph 對沒有 reducer 的 key 是
  取代語意，只回傳這一波會讓最終只剩最後一波，而 multi_hop 的第二跳正好都在最後一波。
  沒寫 outcome 的（wave worker 在 executor 之外炸了）一律算 `crashed` 並計入 `todos`。

**閘門⑯（22 項）**加在 `eval/verify_web_gate_isolation.py`：224 → **246 項**，四道閘門
85／316／17／246 全 PASS。判別力集中在四條誤報對照（⑯b 第一輪就夠、⑯d `kb_unfixable`、
⑯e web 預算、⑯l 崩潰），**變異 Q1／Q2 只有它們抓得到**。

⚠ **變異測試第一輪的 Q1／Q2 是假性「沒抓到」**：executor 是**裸名**呼叫 `_note_exec_outcome`，
只改 `ar.<name>` 的 patch 根本到不了呼叫端。改注 `agentic_rag_version.graph` 之後 8/8 全抓到。
**變異的注入點要在定義處，不是門面。**
⚠ 兩處測試樁跟著改：`verify_answer_validators` ⑮ 的 `_run_executor` lambda 要收 `exec_stats=None`；
本檔 `_drive` 的 `_tavily_search` 樁要收 `need=`（模組級樁是 `lambda q:`，升級到 web 那一路會炸成
`crashed`，害 ⑯d 測到別的東西）。

**還沒做的**：`forced_pass` 的**發生率**。全量 65 題跑一輪才有分母——在那之前不要動揭露或拒答。

## 2026-09-06

### agentic D：Synthesize 的六道修補鏈沒有不動點，加上零成本的回歸守衛

**病灶**：`_node_synthesize` 是一條六道的鏈（citation → consistency → period → reflect → number →
dual_source），每一道抓到就**整篇重生成**。引用是守住的（每道 `*_check_and_fix` 內部都補跑一次
`_validate_and_fix_citations`），**但其他不變量沒有**：`_dual_source_check_and_fix` 的重生成可以
引入新的不可溯源數字或新的期別錯誤，而那兩道**已經跑完了，沒有人再看**。碼上的註解顯示排序是
**兩兩推理**出來的（「number 放 reflect 之後」「dual_source 放最末」），那個論證只在
「後面那道不會破壞前面那道」時成立，六道兩兩之間並不都成立。

**先查了業界**（四篇，記進 `docs/AGENTIC.md` A13）：整篇重生成會讓先前已滿足的約束被破壞，
解法是 targeted correction ＋ **每次 refine 之後重跑全部約束**（DeCRIM 的 critique↔refine 迴圈）；
競爭型約束會造成震盪；**沒有外部回饋的自我修正常常反而變差**（本 repo 的偵測器是零 LLM 的外部
回饋，站在對的那一邊）。

**本 repo 的成本結構讓修法很便宜，所以刻意不做 DeCRIM 那種迴圈**：那種迴圈每一輪 refine 都要
一次 LLM 呼叫，而這裡一題已經燒 50~60 次。改做**單向的接受守衛**——critique 側本來就是零 LLM，
重生成之後重算一次四項缺陷指紋（不合法引用／不可溯源數字／期別倒退／並陳缺時點），
**任何一格變差就退回重生成前的答案**。零額外 LLM 呼叫。

· `validators._deterministic_defects(answer, chunks, web_extra) -> dict[str,int]`：四項指紋，
  四個 key 一律在場。**刻意不收 `find_claim_conflicts`**——它要先跑 `_extract_claims`（一次 LLM），
  收進來就毀掉「critique 免費」這個前提。
· `_accept_revision(...)`：判準是**逐格**不是加總（四格是四種病，加總會讓「修好一個期別錯、
  引入一個捏造數字」看起來持平）；**相等就接受**（以「沒變好就退回」當判準會把整條鏈關掉）；
  **空的重生成不算退回**（那是 LLM 失敗，另一種病）。
· 五道會重生成的 validator 全部掛上，`rev_stats` 走 out-param（不改回傳型別——五個呼叫端，
  改成 tuple 會讓漏改的那個安靜地把 tuple 當字串接）。計數經 graph state → `run_agentic` →
  結果檔的 `revision_stats`，與 `unit_stats`／`replan_stats`／`plan_stats` 同一條路、同一套
  `None` ≠ `{}` 語意。

**驗收：閘門㉑ 16 項（300 → 316 全 PASS）。7/7 變異全抓到。** 另跑一次真實端到端：
reflect 抓到「54.5 億美元 ← 來源實際為 $54.5 billion」→ 重生成 → 守衛判定沒有退步 → 接受，
最終答案是 `545 億美元（$54.5 billion）`（⑲m 的修法同時在線）。

⚠ **這次拿到的是「重生成破壞了什麼」的分母，那在此之前完全不存在**。要不要進一步做成
critique↔refine 迴圈，判準就是 `revision_stats.rejected` 的實際發生率——見 BACKLOG。
⚠ **㉑b 的測資第一版寫錯**：`find_untraceable_numbers` 的正則要求「恰好兩位小數且後面不接 %」，
而我寫了 `71.3%` → 一個都匹配不到、㉑b 當場 FAIL。**同一天第四次踩到「測資的形狀與被測物不同」**
（⑳e／⑭j／⑮q 是前三次）。
⚠ **㉑h 是變異測試 P3 逼出來的**：少了「一格變好一格變壞、總和持平」那條測資，
一個「比四格加總」的實作全綠。

### agentic C：multi_hop 的依賴改由 Planner 宣告，`_BACKREF_RE` 詞表退位

**病灶**：`depends_on` 這個欄位兩個 todo 建構點都有，值**恆為 `None`**；實際決定「這一跳要不要等
前一跳」的是 `_BACKREF_RE`（`該公司|該企業|該家公司|這家公司|此公司|上述公司|前述公司|上述那家公司`）。
那張詞表匹配不到「**那家公司**」「它」「該廠商」「這間公司」與任何英文措辭——而寫那句話的是
Planner LLM，措辭每輪都在變。同 `rq.looks_like_news_query`／`_RELATIVE_TIME_RE`／`_WEB_TODO_RE`。

**先查了業界怎麼做**（三篇一致，見 `docs/AGENTIC.md`）：依賴由規劃器**當結構吐出來**，子問題用
MuSiQue 的 `#1` 或 A.DOT 的 `$var_d` 指涉前一跳；執行前跑確定性結構檢驗（variable hygiene／無環）；
**懸空引用是 prune ＋ warning 不是靜默**。佔位符是**格式定義的封閉集合**，正好落在
CLAUDE.md 允許詞表的那個例外裡。照這三段做：

1. **宣告**：`_PLANNER_PROMPT` 輸出 `depends_on`，子問題把待解公司寫成 `#索引`。
2. **驗證**：新增 `validate_plan_dependencies`（純函式）。`0 <= depends_on < id` **同時**保證無懸空、
   無自環、無環（id 依清單位置指派）——不需要 Kahn，O(n)。非法值 prune ＋ trace ＋ 計數。
   副產品：id 0 恆為 `None` ⇒ **永遠有 ready todo，不會 deadlock**。
3. **判定與替換**：`_is_dependent_hop` 改讀欄位；`_fill_dependent_hop` 三層退路
   （`#N` → 回指代名詞 → 兩者皆無且句中沒點名公司時**前綴實體**）；`_resolve_hop_entity` 多一個
   `parent_id`，有宣告就**只看那一個父 todo**（舊行為是所有 done todo 的 ticker 眾數，會被別的
   子問題裡出現更多次的公司蓋掉）。

**關鍵取捨：「說沒有」與「沒說」必須分得開。** `depends_on: null` ＝ Planner 宣告不依賴，**它說了算**；
整個 key 不存在（舊格式 `list[str]`、既有 replay fixture、`_node_replan` 建的 todo）＝ 沒有資訊，
**才**退回詞表。分不開的話詞表仍然是實際做決定的人，而端到端跑分看不出任何差別
——`_RATIO_INTENT_RE` 就是那個形狀。**一個有意識的例外**：說 `null` 但句子字面沒有主詞時仍然延後
（那是**字面事實**不是感知，代價不對稱），分歧記進 `plan_stats.deps_disagreed` ＝ 日後拿不拿掉它的分母。

**驗收：閘門⑮ 28 項（`verify_web_gate_isolation.py`，196 → 224 全 PASS）。10/10 變異全抓到。**
**加上兩次真實端到端**（snapshot、無 web）：
· 「三家中營收年增率最高 → 該公司 TTM 淨利」→ Planner 自己列滿 6 個子問題、`deps_count=0`（正確：
  候選集合寫在題面上，扇出比建依賴好）。
· 「Magnificent Seven 裡淨利最高的那一家的毛利率、營業利益率」→ Planner 吐 `#0` ＋ `depends_on:0`，
  驗證 `deps_count=2 / pruned=0`，執行時從**宣告的父 todo** 解出實體、`#0` → `Apple`，兩個第二跳
  各一輪就 sufficient。**業界那三段完整跑通。**

⚠ **在 `eval_set.json` 上量到的差異是 0，而那是預期**：5 題 multi_hop 的候選集合全部寫在題面上。
  這次改的是「候選集合不在題面上」那一類，而題庫裡沒有。見 [`BACKLOG.md`](BACKLOG.md)。
⚠ **既有 replay fixture 逐字相容**：`plan` 的 replay key 不含 system prompt，改 prompt 不會 miss；
  舊 fixture 錄的 plan 沒有 `depends_on` key → `deps_declared=False` → 走詞表 ＝ 行為完全不變（⑮d/⑮l）。
⚠ **BACKLOG 那條「已接受的極限」當初的成本理由是錯的**：它寫著「要把單波 ThreadPoolExecutor 拆成多波」，
  但 `ready`／`deferred` 兩波機制**當時就已經存在**（由詞表驅動）。這次一個 wave 都沒有多。
⚠ **⑮q 第一版是恆真的**：`_find_all_ticker_aliases` 回傳集合、同一個 todo 每家只算一票，兩個 todo
  時「眾數」剛好也回對的答案（變異 N8 沒抓到）→ 改成三個 done todo。
  **同一天第三次踩到「測資讓那條路從來沒被走到」**（⑳e、⑭j 是前兩次）。

### agentic B：Replanner 的待辦額度用完時，拒絕原本一個字都不印

**先量再改。** 零 LLM 驅動 graph 控制流（`_node_execute` / `_node_replan` / `_route_after_replan`），
replan 的 LLM 換成「每輪都想加 3 個」的樁：

| planner 拆幾個 | replan 真的加得進去 | 總執行次數 | 停在哪個上限 |
|---|---|---|---|
| 1 | 6 | 7 | `MAX_TODOS` |
| 5 | 2 | 7 | `MAX_TODOS` |
| **7** | **0** | 7 | `MAX_TODOS` |

**① `MAX_ITERS=8` 在現行常數下咬不到。** todo 的狀態是單向的（pending → in_progress → done，
不會回頭），所以總執行次數**恆等於**曾經建立過的 todo 數 ≤ `MAX_TODOS=7`。它的註解寫著
「防 replanner 無限加待辦」，但做那件事的是 `MAX_TODOS`。**它不是死碼**——把 `MAX_TODOS` 調到
≥ 8 的那一刻它就活過來，而那時會有 todo 被建立卻永遠不執行、且完全靜默。註解改成實話，
不變量交給閘門⑭a。

**② `MAX_TODOS` 是 Planner 與 Replanner 共用的一個額度、先到先得，而 Planner 一定先跑。**
所以「拆得最細的題」＝「Replanner 完全沒有預算的題」，恰好是 multi_hop 那一類。

**③ 改的是可觀測性，不是額度。** 額度用完的拒絕原本是 `if len(todos) < MAX_TODOS:` 的**隱含
else**：沒有 trace、沒有計數——而另外兩個拒絕分支（時間模式不符、intraday 徒勞）都有 trace，
只有最常發生的這個沒有。於是「replanner 想加 3 個但額度滿了」與「replanner 什麼都不想加」
在結果檔裡**外觀完全相同**，`probe_replan_contribution.py` 量到的「Replanner 貢獻 0」在拆得細
的題上**讀不出來**。現在落地成 `replan_stats`（`rounds`／`added`／`refused_budget`／
`refused_tasks`），走 graph state → `run_agentic` → `run_agentic_on_evalset` 的 record，
與 `unit_stats` **同一條路、同一套 `None` ≠ `{}` 語意**。

⚠ **刻意不動任何常數。** 「該不該給 Replanner 獨立額度」需要證據，而證據就是這次落地的計數。
沒量到之前調高上限＝又一個「聽起來合理」的機制假設（子問題爆炸級聯有前科）。見 BACKLOG。
⚠ **行為逐字不變**（⑭f 是那條回歸護欄）：早退取代隱含 else，todos 的產出完全相同。

**驗收：閘門⑭ 15 項（`verify_web_gate_isolation.py`，181 → 196 全 PASS）。7/7 變異全抓到。**

⚠ **⑭j 第一版是恆真的**：它原本只數 `_node_replan` 裡 `_trace` 呼叫的**總數**（≥ 4），而改動前
就已經有四個以上 → 自測當場全綠。改成問「那個 trace 的引數裡有沒有 `MAX_TODOS`」才有判別力。
**這是同一天第二次踩到同一個形狀**（⑳e 是第一次），兩次都是變異測試抓出來的。

### agentic A：`_fair_select` 修掉一個自我矛盾——順序不再由跨子問題不可比的分數決定

**病灶是同一個函式的兩句話互相矛盾。** [`agentic_rag_version/retrieval.py`](agentic_rag_version/retrieval.py)
的 `_fair_select` docstring 寫著「cross-encoder 原始分數是**相對當次 query** 的，跨子問題不可
直接比（B 的 0.72 可能已是 B 的最佳答案，卻輸給 A 的第七名）」——那正是**選擇**改成 round-robin
的全部理由——然後最後一行仍然 `sorted(picked, key=raw_rerank_score)`，**把同一個剛被宣告不可比
的分數拿回來決定順序**。分數不可比就是不可比，不會因為換成排序就變可比。

**實際後果**：某個 facet **唯一**的證據（在自己的子問題裡是第 1 名、絕對分數低）會被壓到名單
最後，而 writer budget 上限 `WRITER_BUDGET_CAP=16`、`rq.SYSTEM_PROMPT` Rule 4 要第一句給結論、
Rule 9 要多公司題逐一具名歸屬。

**改了兩件事**：① 拿掉最後那道 `sorted`，輪詢序就是回傳序；② 一併拿掉 `len(collected) <= k`
的 early-return——那條捷徑原樣回傳輸入（＝全域分數降序），於是同一個函式對「剛好裝得下」與
「裝不下」給**兩種不同的順序規則**。迴圈本身在 `len <= k` 時就會把全部撿完。
**桶內仍然照分數降序**（同一個 query 評出來的分數是可比的）。

**驗收：閘門⑳ 9 項（`verify_answer_validators.py`，291 → 300 項全 PASS）。4/4 變異全抓到。**

⚠ **收益沒有量，也不宣稱**。RAGAS 分不出這個量級（見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺飽和〉），
所以 9 條斷言全是**結構性質**，沒有一條說「答案會更好」。這是「修掉一個自我矛盾」不是「量到改善」。
⚠ **會擾動既有基準**：送給 Generator 的 chunk 順序變了 → 之後跑出來的答案與 09-06 之前的
不可逐字比。replay fixture **不受影響**（七個 kind 都在 `_fair_select` 之前，生成本來就沒有快取）。

**兩個被踩出來的東西**（都是「測資的形狀與生產不同」，同族第四、第五次）：

1. **⑳e 第一版是恆真的。** 它要驗「桶內仍照分數降序」，測資卻是本來就已經分數降序的 pool
   → 「有排序」與「沒排序」輸出逐字相同，**變異 M3 當場沒抓到**。改成刻意打亂輸入序才有判別力。
   同 ⑰i／⑱f 的形狀。
2. **拿掉 early-return 當場炸出閘門⑮ 的測資缺陷。** ⑮ 餵給 `_node_synthesize` 的單顆合成 chunk
   **沒有 `raw_rerank_score`**，而生產的 chunk 一律由 `rq._payload_to_chunk` 建、必定有這個欄位
   （⑳h 拿真實 payload 餵生產建構子守這條）。舊的 early-return 讓那顆測資永遠走捷徑 → 缺陷
   **從來沒現形**。修法是**改測資不是改生產函式**——後者就是「讓量尺反過來拉著被測物走」。

### RAGAS 量尺在新 judge 上重量完畢：gold 上限整組換新，噪音兩格被推高

換 judge（`gpt-oss-120b` 退役 → `google/gemma-4-31b-it`）之後，`GOLD_BASELINE` 與 `NOISE`
兩組常數綁的都是死掉的舊 judge，本檔自己印著「一律不可引用」。這次把它們補回來。
**三輪依序跑**（`--max-workers 2`，NVIDIA 限速，併發不是槓桿；每輪約 2 小時）：

| 臂 | 輸出 | 用途 |
|---|---|---|
| A2 | `experiments/ragas_65q_gemma_sysA2.json` | 系統分數 ＋ 噪音對的一半 |
| B | `experiments/ragas_65q_gemma_sysB.json` | **同一份結果檔**再判一次 ＝ 噪音對的另一半 |
| C | `experiments/ragas_65q_gemma_goldceiling.json` | gold 當答案（只換 answer，contexts 不動）＝ 上限 |

**① gold 上限：判別力沒有變差。** `answer_correctness` **.972 → .987**（擔心的是「連標準答案
都認不出來」，沒有發生）。整組換新（不取新舊較大值——那是噪音門檻的規則；上限要跟**同一輪**
系統分數比，拿 gemma 判的分數去比 120b 判的上限正是本檔明令禁止的跨 judge 比較）：

| | correctness | faith | relevancy | recall | precision | nv |
|---|---|---|---|---|---|---|
| 舊（120b／65 題） | .972 | .659 | .871 | .788 | .843 | .969 |
| **新（gemma／65 題）** | **.987** | **.726** | **.888** | **.826** | **.780** | **.923** |

**② 噪音：兩格被推高。** |A2−B| ＝ recall .006／precision .006／**nv .012**／**faith .021**／
relevancy .002／correctness .008。照既有規則取新舊較大值 → **`nv` .008→.012、`faithfulness`
.010→.021**，其餘不動（correctness 這次比舊值小，維持 .012）。
⚠ faithfulness 那 .021 不只是均值抖動：逐題最大 |Δ| 是 **`mix-15` 1.000 → 0.500**（整題砍半），
`mix-07` .867→.500、`lex-06` 1.000→.667 同一形狀 → **這個 judge 在 faithfulness 上比舊的抖**。
⚠ 只有**一對**。同一支腳本寫著「一對是抽樣不是上界」，這條仍然成立，別拿它往下調門檻。
⚠ 三對噪音樣本裡**有兩對推翻了前一對的某一格**（.007→.012、.008→.012／.010→.021），
那句警告的實證從此有三筆。

**③ 判讀結論不變：量尺仍然飽和。** 五個指標已達或超過上限（faithfulness 是 gold **自己比
系統低 .149**，成因與 2026-08 相同：gold 依 `gold_files` 生成、與 agentic 撈到的 contexts
不同源）；唯一有空間的仍是 `answer_correctness`，系統 **.654** vs 上限 **.987 ＝ 落差 .333**。
`answer_relevancy` 有一條 +.029 的細縫（高於 .004 門檻），很窄。

**④ 順手修掉護欄自己製造的誤讀。** `_print_interpretation_guide` 判「已達上限」的容差寫死
`0.005`，於是 `context_precision`（噪音 **.046**）會印出「距 gold 上限 +0.008」——而那 .008
依定義就讀不出來。改成 `max(0.005, 該指標的噪音門檻)`。

**⑤ `--timeout 420` 在 gemma 上不夠，而失敗是靜默的。** 第一輪全量（`ragas_65q_gemma_sysA.json`）
**11 筆 NaN**（correctness 5／precision 3／faith 2／nv 1）→ 那輪的 correctness 均值是 **60 列**
算出來的，**不可當基準**。改用 `--timeout 900 --nvidia-passes 2` 之後：A2 剩 1 筆（`mix-13`
的 correctness）、B 與 C 都是 0 筆。⚠ 這兩個值目前**只在 CLI 傳，預設仍是 420／1**。

**⑦ 順帶驗了 correctness judge（另一支、另一個 prompt）：也維持 gemma，但理由是反直覺的。**
`judge_regression.py` 11 個凍結案例，gemma 與 `gpt-oss-20b` 各跑 9 輪。聚合幾乎一樣
（80/90 vs 82/90），**失敗方向相反**：gemma 五個陰性對照 **全部 9/9**（種進去的缺陷一個
沒漏），唯一硬傷是 `sem03_grounded_specific_number` **0/9** ＝ 把有根據的具體數字判成捏造
（**誤報**，安全方向）；20b 沒有全掛的題，但**三個陰性對照會漏**（8／6／2）＝ 認不出植入的
假數字（**漏報**，危險方向）。照本 repo 一貫判準 → 維持 gemma。
⚠ **只看 `sem03` 那一題會選 20b**（9/9 vs 0/9），結論與逐題 k/n 攤開後**相反**——
這就是那支腳本自己寫的「比逐題 k/n，不要比單輪總分」的用處。
⚠ `sem03_grounded_specific_number` 記為 gemma 的**已知行為差異不是回歸**：65 題 **0 題帶
rubric**，其餘引用 `DEFAULT_JUDGE_MODEL` 的五支全已退役 ＝ **目前沒有活的消費端**。

**⑥ 併發不是槓桿（一條 null result）。** `--max-workers 6` 探針：沒有加速，且出現 3 次
500/503（`--max-workers 2` 是 0 次）。`--max-workers 2` 本來就是預設，help 字串早就寫著
「NVIDIA 限速，別開太高」——這 40 分鐘重新推導了 repo 已經知道的事。
牆鐘時間是算術不是模型問題：390 個 metric-row × 88s ÷ 2 workers ≈ 4.8 小時。

### 清掉四筆積欠：兩個死模型預設、一個沒人守的不變量、一個決定不做的歷史改寫

**① `gen_reference_answers.py` 的 `GEN_MODEL`：`gpt-oss-120b`（410）→ `openai/gpt-oss-20b`。**
這個角色的判準與別處不同——參考答案是 `answer_correctness` 的 **ground truth**，
所以**不能與 judge 或系統 generator 同源**（同源會把 correctness 系統性灌高，本 repo 已經
量過並否決這個形狀）→ 排除 `gemma-4-31b-it`（現在的 RAGAS judge）與 `nemotron-3-super-120b-a12b`
（現在的生產 `GEN_MODEL`）。20b 的「太慢」在這個角色不成立：65 題跑一次、輸出直接進版控。
⚠ **這個角色沒有 bake-off**，上面是排除法＋可用性，不是量出來的品質排名。

**② 換完才發現：死模型原本是一道意外的保險。** `--force` 不帶 `--ids` ＝ 全量重生成
＝ 洗掉 `reference_answers.json` 的 **24 處人工校正**（腳本重現不了），而在換掉之前跑下去
第一個呼叫就 410、校正毫髮無傷。原本擋這件事的**只有 help 字串**。換成活模型等於拆掉那道
保險 → 同一次補了真的：不帶 `--ids` 的 `--force` 必須再加 `--force-all`，否則 exit 2，
**且在載入 BGE-M3／Qdrant 之前就退**（實測）。

**③ `ablate_retrieval_model.py` 的 `MODEL_BIG`：標記為「跑不起來，刻意不修」。**
**不偷偷指向活的模型**——那兩個常數是一次**已完成實驗的臂定義**，換掉之後檔裡的結論、
結果檔的 meta、以及寫死的臂標籤（`120b#0`／`120b#1`／`20b#0`）就會與實際跑過的東西對不上
＝ 把舊結論掛到沒跑過的組態上。重跑要改哪三個地方寫在該檔 `MODEL_BIG` 上方。

**④ 新閘門 `eval/verify_cited_evidence.py`：文件引用的證據檔必須存在且已納入版控。**
本 repo 的結論幾乎每條都掛著一個 `experiments/...` 檔，而那些檔被 gitignore 擋掉時的失敗
是**安靜的**：文件照樣讀得通，只有真的去點路徑的人才會發現。2026-09-04 手動撈過一次
（commit `16d4646`），這次把它變成可重跑的。實跑：15 份 `.md`／8 個引用，**全部通過**。
⚠ **反過來刻意不查**：`experiments/` 底下沒被引用的檔案**不是缺陷**——實測 24 個 `.log`／
3.3 MB 原始 stdout，沒有任何結論掛在上面；把「未被引用」也算 FAIL 會把垃圾灌進版控。
⚠ 判別力：`--selftest` 6 條（③④⑤是誤報對照——陽性那兩條，一個「一律回報 FAIL」的實作
也會過），另在**真實掃描路徑**做過變異測試（往 README 塞一筆假引用 → 抓到 → 還原後回綠）。

**⑤ 決定不改寫 commit `16d4646`**（加了 161 檔又被 `dad5544` 刪掉 262 檔那次）。
它**已經 push 到 `origin/refactor-agentic-modules`**，改寫要對共用分支 force-push；
而代價根本不痛（`.git` 目前 **55 MB**）。復活條件與正確做法記在 BACKLOG〈已接受的極限〉。

四道確定性閘門改動前後皆全綠：85／17／181／291。

## 2026-09-05

### 65 題端到端：「億」的頻率有分母了，同一輪也揪出後處理的第三個缺口

**這是「億」修法上線後的第一次全量跑**（`experiments/agentic/gj_multiyear_r2_unitstats.json`，
65/65 完成、零錯誤、**零 `unit_stats=None`**）。

**① 頻率（BACKLOG 欠的那個分母，現在有了）**

| | |
|---|---|
| 分母 | **65 題** |
| 觸發後處理 | **10 題（15.4%）／共 14 筆** |
| ① 剝掉 LLM 緊貼配對的億 | 2 筆 |
| ③ 孤立的億判定為 10x 音譯而修掉 | 12 筆 |

＝ **LLM 大約每 6~7 題就違反一次 Rule 11 自己寫億**。不是偶發，backstop 是必要的。
逐筆明細可稽核（`unit_stats.*_detail`），抽查三題對得上來源：`lex-01`（`Market Cap : $4962.16B`
→ `49,621.6 億美元`）、`sem-09`（19.19）、`col-10`（37.01）。

**② 修法在真實輸出上成立**：掃 65 題全部**相鄰**雙寫金額配對 **92 處、0 筆不一致**
——⑲k 那個「同句自相矛盾差 10 倍」的病徵不再出現。
⚠ 掃描的第一版報 95 筆 FAIL，是**量尺錯**：它把同句所有億值與所有美元值做笛卡兒積，
於是正確的 `767 億美元($76.7 billion)` 會去跟同句別的金額比。**金額比對必須相鄰配對。**

**③ 但 `check_number_defects` 的 col-11 FAIL 是真的，而且揭出第三個缺口**

```
答案：Microsoft Cloud 收入在 2026 財政年度第三季增加 29% 至 $54.5 億
來源：Microsoft Cloud revenue increased 29% to $54.5 billion      ← 差 10 倍
```

`_YI_SOLO_RE` 把幣別詞寫成**無條件必填**（`億[\s]*(美元|歐元)`），所以 `$54.5 億` 這種
「有 `$` 但沒幣別詞」的寫法**一個都匹配不到**——實測該答案匹配數 **0**，同題共 **5 處**
（`$22.1/$21.8/$7.9/$7.8/$54.5 億`），而 `_src_has(..., billion)` 對 5 處**全部為 True**
＝③ 手上證據齊全，只是從來沒看到它們。

**修法刻意很窄：幣別詞可省，但只在有 `$`／`US$` 前綴時。** 必填原本是刻意的
（回測踩過 news-10／mi-11：「10 億使用者」被改成「100 億美元（$10 billion）使用者」），
而那個回歸的前提是「**沒有**幣別標記」——`$` 本身就是幣別標記，沒有人把使用者數寫成 `$10 億`。
金額資格收斂成單一判準 `_is_monetary_yi()`，`_yi_values()`（產生證據 B）與
`repair_yi_against_source()`（執行修改）**共用它**。

**同一跑順帶修掉 `repair_paired_yi` 的 `$` 孤兒**：`_PAIRED_YI_BEFORE_RE` 從 `[0-9]` 起頭、
不吃前綴 `$` → 剝掉億之後 `$` 留下來，吐出 `$$54.5 billion`、再經 ② 成為 `$545 億美元(...)`。
值是對的但多一個 `$`。這是既有的潛在 bug，舊的 ③ 看不到 `$N 億` 所以從沒觸發過。

**閘門⑲m（14 項）**，277 → **291**。⚠ **判別力幾乎全在誤報對照**：⑲m3~⑲m6 逐字凍結四種
非金額計數、⑲m9 排除條款、⑲m10 既有正確雙寫不動。**⑲m8 是最重要的一條**——證據端與修改端
必須共用同一個資格判準，各判各的會出現「被當成證據卻不會被修」，而那是靜默的。
4/4 變異全抓到：N1 幣別詞退回必填 → ⑲m1/m2/m2b/m8；N2 `_is_monetary_yi` 恆真 → ⑲m3/m4/m8；
N3 `_yi_values` 不過判準 → ⑲m8；N4 ① 不吃 `$` → ⑲m11。
⚠ **⑲m7 的第一版斷言是寫錯的**（斷言「`545 億` 不得出現」，但那正是 ①＋② 算出來的**正確**值，
錯的是期望值不是系統）；改成驗「③ 一次都不觸發 ＋ 值正確」，而那一跑才揭出 ⑲m11。

**三次了，每一次都是端到端跑出來的，不是想出來的**（證據 B ← 表格來源失明；⑲k ← 測資是英文
句子而生產是 markdown 表格；⑲m ← 測資寫 `N 億美元` 而 LLM 寫 `$N 億`）。共同形狀是
**測資的形狀與生產不同，那條路等於從來沒被測過**。

## 2026-09-04

### chunk 層 gold：三條卡住的量尺缺口，卡在同一個前提上

`eval_set.json` 的 `relevant` 只到**檔名**，而 BACKLOG 有三條線同時卡在「沒有 chunk 粒度的
gold」：lexical 承接數 1.93、`relevant_ids` 的損害判不出來、Grader 責任剝離的復活條件。

**新增 [`eval/chunk_gold.py`](eval/chunk_gold.py) ＋ `chunk_gold.json`：41/65 題、83 顆 gold chunk。**
gold **由 `literal` 定義、不寫死 `chunk_index`**——理由是同一次查到的兩種漂移：
① 重建會位移索引 ② **`eval_set.json` 的 `pin_chunks` 已經漂了 8 題**（`relevant` 於 `10a0caa`／
`53d4baf` 更新到新一季，pin 沒跟著改 → `fetch_gold_chunks` 只撈 `relevant` 的檔，那 8 題的 pin
一顆都比對不到 → `gen_reference_answers.py:305` 印一行警告然後**退回 dense-only**）。

**候選來自 `reference_answers.json` 的數字，兩個門檻篩特異性**（`n_in_relevant<=3`、
`n_same_ticker<=8`），再**逐題人工驗證**（判準：這個數字在命中 chunk 裡扮演的角色，就是題目
問的那個量）。剔掉三種形狀：脈絡值（`lex-10` 的 `3,499` 是投資活動現金流不是資本支出）、
同表鄰居（`col-10` 的 `78.23` 總現金 vs 答案 `37.01` 自由現金流）、純巧合（`mix-13` 的 `3.5`
是市值里程碑表格）。⚠ **沒有加「排除年份」的形狀規則**——兩個數值門檻自己就濾掉了年份，
而唯一存活的 `2006` 是 `lex-04` 的 CUDA 推出年、**真的是答案**。

**消費語意是 union ＋ all-dropped**：一題的 gold 是所有 literal 命中 chunk 的聯集，留住任何
一顆就不算全滅 → 聯集偏大只會讓判定**更難**觸發＝ false-negative 方向。

`literal_matcher` 三處收攏成一個定義點（`probe_historical_benefit.py` 改成 import，它的 10/10
雙向自測因此變成測共用實作）。

### 這條缺口第一次量得到，而第一輪就撈出兩個量尺缺陷

`probe_relevant_ids.py` 多一欄 `答案全滅`。12 題 × 3 輪：**`答案全滅` 0 題**（7 題量得到），
平均圈選率 0.48 → **「Grader 濾掉的是離題 chunk 不是答案」不再是被缺口擋住的問題，有證據了**。

同一輪撈出兩個**量尺**（不是系統）的缺陷：

1. **`lex-17` 檔層 `gold全滅` 3/3、chunk 層 `答案全滅` N/A**——答案那顆
   （`MSFT_Fundamentals_20260612#0` 的 `Revenue Growth (YoY): 18.30%`）**根本沒進候選池**。
   舊指標會把「檢索沒撈到」記成「Grader 丟掉了」，兩件完全不同的病。
2. **檔層 gold 用精確比對，而 14 題的 `relevant` 是萬用字元**（`AAPL_Fundamentals_*.txt`），
   **且那 14 題全部是 lexical／colloquial**——正好是這支最該量的一類。它們的檔層指標
   一直是結構性 N/A。已改用 `fnmatch`。⚠ 是加了 chunk 層 gold（用 fnmatch）之後**兩欄
   對不上**才被逼出來的，不是想出來的。

### 「億」發生頻率的分母：從只 print 變成落地到結果檔

`rq.finalize_answer_units(..., stats=dict)` 填兩個計數（`yi_stripped`／`yi_repaired`）與逐筆
明細，經 graph state → `run_agentic` → record 的 `unit_stats` 欄位寫進結果檔。跑一批就有分母
（① 與 ③ 觸發的每一筆都代表 LLM 違反 Rule 11 自己寫了億）。

- **刻意用 out-param 不用模組層全域**：`_node_execute` 是 ThreadPoolExecutor，全域會被別的
  執行緒蓋掉，而蓋掉後的外觀與「這一題沒觸發」完全相同。
- **刻意不改回傳型別**：三個呼叫端，改成 tuple 會讓漏改的那個安靜地把 tuple 當字串接下去。
- **`None` 與 `{}` 不可合併**：`None` ＝ 沒經過後處理（崩潰降級那條路確實不經過），
  `{}`／全 0 ＝ 經過但沒觸發。合併會把降級題算進分母＝頻率被系統性低估。

**閘門⑲l（12 項）**，265 → **277**。⚠ 判別力全在誤報對照——陽性那兩條，一個「stats 一律
填 1」的實作也會過。3/3 變異全抓到：① stats 只在觸發時才填 → ⑲l4 ② record 把缺席填成 `{}`
→ ⑲l10 ③ 呼叫點拿掉 `stats=` → ⑲l6。
⚠ **⑲l3 的測資第一版寫反了**（拿 `8.475 億` 去找 `8.475 billion`），自測當場 FAIL。

### RAGAS judge 換掉，並揭出一個會靜默吃掉主指標的預設值

`DEFAULT_RAGAS_MODEL`：`openai/gpt-oss-120b`（410 Gone）→ **`openai/gpt-oss-20b`**。

**為什麼不是現在的 `GEN_MODEL` `nemotron-3-super`**：`judge_regression.py` 11 案例實測
`gpt-oss-20b` 10/11 vs `gpt-oss-120b` 9/11，而後者被判掉的理由之一就是**與 gen_model 同一支
（自評偏誤）**——judge 與 generator 同源是本 repo 量過並否決的形狀。09-03 的 bake-off 只量了
三個結構化角色（判準是輸出穩定性與延遲），**沒有量判定品質**，外推到 judge 沒有根據。
liveness 逐一實測：20b ALIVE／120b **410 Gone**／nemotron ALIVE。

**換完暴露出的東西**：`--timeout` 預設 120 秒對新 judge 不夠，`answer_correctness`
（六個指標裡最貴，要拆 claim 逐條比對）**逾時 → 靜默變 NaN**，而輸出裡 NaN 印成 `n/a`、
外觀與「這個指標算不出來」相同。**那正是 CLAUDE.md 說唯一還有空間的指標** → 一整輪跑分
會在最重要的那格上無聲歸零。預設拉到 **420**，同兩題就算得出來（0.615／0.513）。

⚠ **所有既有 RAGAS 聚合值就此失去可比性**：`GOLD_BASELINE`／`NOISE` 都是舊 judge 量的，
新 judge 上重量之前**一律不可引用**（腳本自己會印這句）。重量本身還沒做，留在 BACKLOG。

### 同日再換一次 judge：`gpt-oss-20b` → `google/gemma-4-31b-it`（兩支 judge 一起）

上一條換完幾小時就發現**太慢**——一輪 65 題 RAGAS 六指標是幾百次呼叫，judge 的中位延遲
直接決定跑不跑得完。`gpt-oss-20b` 沒跑過任何一輪全量，所以這次換**不損失任何可比性**。

**bake-off**（5 模型 × 3 個 judge 角色 × 3 輪；角色＝NLI 裁決／rubric 評分／長 context
faithfulness，判準沿用本 repo 的**輸出穩定性 ＋ 延遲**）：

| 模型 | parse | 判定對 | 中位 | max | 事故 |
|---|---|---|---|---|---|
| `openai/gpt-oss-20b`（前任） | 9/9 | 9/9 | 8.8s | 18.6s | — |
| `minimaxai/minimax-m3` | 7/9 | 7/9 | 10.5s | 16.2s | **429 ×2** |
| **`google/gemma-4-31b-it`** | 9/9 | 9/9 | **3.0s** | 24.0s | — |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | 8/9 | 8/9 | 9.0s | 16.0s | 503 ×1 |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | 9/9 | 9/9 | **33.1s** | 38.3s | — |

**⚠ 這場 bake-off 對「判定品質」零判別力**——五個全 9/9，那三個角色太容易。它量到的
**只有延遲與可用性**，不可拿來說 gemma 判得準；判別力仍要靠 gold 上限重量（BACKLOG）。

**⚠ TTFT 榜與真實 judge 負載是反的。** 候選是照一份「1+1=?」的 TTFT 榜挑的，而那個量法
量不到 judge 真正會失敗的方式：TTFT 榜首 `minimax-m3`（131ms）第 8 次呼叫就 429；TTFT 第五
的 `nemotron-3.5-lightning`（2.1s）把思考過程寫進 content → 中位變 **33 秒＝比前任慢 4 倍**。
**單 token 延遲量不到 judge 會失敗的方式。**

**選非推理模型是刻意的**：本 repo 栽過兩次「reasoning token 吃光正文額度 → 靜默吐空字串」
（`TABLE_SUMMARY_MAX_TOKENS`，`gpt-oss-120b` @200 是 10/10 全空），而 `gpt-oss-20b` 在那份
TTFT 榜上正是回「無內容」。`gemma-4-31b-it` 是 instruct 模型，沒有這條路。

**同時換的還有 `eval_generation_llm_judge.DEFAULT_JUDGE_MODEL`**（correctness judge）。
⚠ 該檔記著的 `20b 10/11 vs 120b 9/11` **是綁在 20b 上的，對 gemma 不成立**，要拿新的就跑
`judge_regression.py`——而那支不是零噪音（同碼同模型三個單輪 10/11、9/11、11/11）。

**沒換的兩個 20b，理由不同**：`unstructured_components.TABLE_SUMMARY_MODEL` 走 **Groq 不是
NIM**（gemma 不在 Groq 上），且是 ingest 專用、換了要 `--rebuild` 才生效，CLAUDE.md 明訂換它
要先 bake-off ＋ `verify_table_captions.py`；`ablate_retrieval_model.MODEL_SMALL` 是 **A/B 臂的
定義**不是預設值，而 `MODEL_BIG` 已是死的 120b——換一半會讓那支從此沒有意義。

**驗收**：RAGAS 端 2 題 smoke（`sem-01`／`lex-03`）**六指標全部算得出來、零 NaN**
（`answer_correctness` 0.643／0.609）。⚠ `--timeout` 420 是在 20b 上量的，**gemma 上沒有重量**
——420 只是上界，留著無害，要往下調得先量。

## 2026-09-04

### 端到端跑出來的洞：backstop 的證據判準對「表格來源」結構性失明

前一天的三層後處理（⑲a~⑲j，18 項全綠）上線後跑真實管線，第 1 次就吐出：

```
總營收為 828.86 億美元（$82,886 百萬），相當於 $82.886 億美元
```

**同句自相矛盾、後半差 10 倍**。三層全漏：① 不動它（沒跟來源數字緊貼配對，是獨立的
「相當於」子句）、② 不動它（明文不碰已經是億的值）、③ 該接但**條件搆不到**。

**成因（零 LLM 重現確認，不是推論）**：③ 的 `_src_has` 要求單位詞**緊貼**數字，而來源是
markdown 表格 `| Revenue | $82,886 |`，`In millions` 只寫在表頭 → 實測
`_src_has(82.886, billion)` 與 `_src_has(82886, million)` **兩個方向都是 False**。
而 SEC 的數字幾乎都住在表格裡，所以這不是邊緣情況。

**這其實是量尺的測資形狀問題**：⑲h/⑲h2 用的來源是手寫英文句 `Total revenue was
$84.75 billion`，生產的來源是表格——**形態不同，那條路等於從來沒被測過**。同一個形狀
（量尺自備輸入、沒驗過生產供不供得出那個輸入）在 CLAUDE.md 已經記過，這是第七次。

**修法：換證據源，不是去教 `_src_has` 讀表頭**（哪個表頭管哪個儲存格是感知不是規則，
判錯會把對的數字改壞）。③ 改成兩種獨立證據，任一成立即可定罪，排除條款對兩者一律先跑：

- **A**（原有）來源有 `$N billion`。
- **B**（新）`convert_usd_units_to_yi` 已從來源形態的數字算出 `N*10 億` 並寫進同一份答案
  → 那個值是程式算的、是權威的，LLM 另外寫的 `N 億` 就是對**同一個量**的音譯錯誤。
  用 ② 跑前跑後的億值**差集**取得，不動 ② 的簽章（⑲d 凍結了它的正常路徑逐字輸出）。
  ⚠ 證據 B **刻意不補 `（$N billion）`**：那字串不在來源裡，補了會被
  `find_untraceable_numbers` 判成無法溯源。

另修兩處外觀：`_USD_UNIT_RE`／`_PAIRED_YI_AFTER_RE` 的尾綴收裸「元」（LLM 寫
`$119,796 百萬元` 時舊版吐 `1,197.96 億美元（$119,796 百萬）元`）；`_YI_SOLO_RE` 吃掉
前導 `$`（`$82.886 億美元` 這種 `$`＋億的混寫）。

**驗收**：`verify_answer_validators` **257 → 265**（⑲k 一組 8 項），其餘三道不動
（85／17／PASS）。變異測試 **3/3 全抓到**——拿掉證據 B → ⑲k 掛；拿掉 `_DUAL_PAREN_RE`
→ ⑲k4 掛（且輸出證明它會把對的 `82.886 億美元（$8,288.6 百萬）` 改成 `828.86`）；
尾綴不收「元」→ ⑲k8 掛。真實 agentic 兩題 8 處換算全對且內部自洽
（150.89 ＋ 677.97 ＝ 828.86）。

**頻率**：修法前 4 次跑觀察到 1 次，間歇。

---

## 2026-09-03

### 預設 LLM 被退役當天換掉：五個定義點 → `nemotron-3-super`

`openai/gpt-oss-120b` 於 2026-09-03T08:00Z 被 NVIDIA 退役（`410 Gone`）。它是**檢索／判定／
生成三側共用**的預設 → 當天整條管線的 LLM 全死。發現方式：重構驗收跑到一半，答案從某一題
起全部變成 `_fallback_local_summary` 的「生成步驟發生錯誤」，時間邊界乾淨得可疑。

**五個定義點**全部換成 `nvidia/nemotron-3-super-120b-a12b`：`rq.DEFAULT_MODEL`／
`DEFAULT_GEN_MODEL`、`CHECKER_MODEL`／`GEN_MODEL`／`RETRIEVAL_MODEL`（後三者可 env 覆蓋）。

**選型判準是輸出穩定性與延遲，不是模型大小**（同表格摘要模型換兩次的教訓）。
證據 `experiments/_model_bakeoff_20260903.log`：nemotron-3-super 三個結構化角色 3/3 且最快
（plan 10.4s／check 6.5s／filter 2.8s）；`deepseek-v4-pro` plan 一次 **604 秒**（一題 50~60 次
呼叫 → 不可用）；`llama-3.1-nemotron-ultra-253b` 在 NIM 上 **404**。

**換模型帶出來的一個既有缺陷**：單發管線的 system message **沒有附語言指示**，agentic 有。
舊模型碰巧都吐中文所以看不出來，換成 nemotron 之後當場現形——單發路徑 12 題有 8 題整篇英文。
`_ZH_ANSWER_DIRECTIVE` 因此上移成 `rq.ZH_ANSWER_DIRECTIVE`（唯一定義點），單發與 API 都接上；
`ar._ZH_ANSWER_DIRECTIVE` 保留為別名，因為 `verify_web_gate_isolation` 閘門③ 直接讀它。

⚠ **這次換模型讓所有既有基準與 replay fixture 的生成端失效**（中間產物有快取、生成沒有），
且**沒有辦法與舊模型 A/B**——舊的已經下架。跨 2026-09-03 比任何 RAGAS 聚合值都是無效比較。

⚠ eval 側還有三支的預設仍指向死模型，各自卡點不同（含 RAGAS judge 不可順手改成
`nemotron-3-super`：那正是現在的 generator，自評偏誤是量過並否決的形狀）。見 `BACKLOG.md`。

### 金額換算「億」：三層防線本來只裝在 eval，生產管線是漏的

**起因是一個誤診。** 換上 `nemotron-3-super` 當天看到 `$82,886 百萬美元（約 $82.9 億美元）`
（差 10 倍），先記成「新模型的缺陷」。查證推翻了兩點：

1. **舊模型同病** — `gen_reference_answers.py` 的註解記著 2026-08-07 稽核抓到 **13 題 23 處**
   同形狀。這是長期缺陷，不是換模型造成的回歸。
2. **A/B 做得到**（先前寫「無法」是錯的）— 既有 147 個結果檔 4543 份答案掃得到。

**真正的病是接線，不是模型。** 專案早就設計好三層（Rule 11 勸 → 偵測重寫 →
`repair_yi_against_source` 拿來源算對，實測 44 處零誤報），但**三層都只活在
`eval/gen_reference_answers.py`**。生產側：agentic 只有 `convert_usd_units_to_yi`，
單發管線（`api_server`／`app` 產品線）**一層都沒有**。

而 `convert_usd_units_to_yi` 對這個病**結構性失明**——它明文「不碰已經是億的值」。
實測它還有第二個洞：**不冪等**，LLM 先斬後奏寫雙寫時它會吐巢狀
（`84.75 億美元（$84.75 billion）` → `84.75 億美元（847.5 億美元（$84.75 billion））`）。

**改動**：
- `rq.repair_paired_yi`（新）— 剝掉與來源單位數字**緊貼配對**的億。剝而不改對：Rule 11 的
  契約是 Writer 完全不寫億，改對它等於承認兩個會打架的權威。**順帶解掉巢狀**。
- `rq.finalize_answer_units`（新）— 三層的**唯一組裝點**，順序 ① 剝 → ② 換算 → ③ 依來源補。
- `repair_yi_against_source` 等 5 個名字**從 eval 提升到 `rq`**（唯一定義點），eval 改成 import。
  eval 的**呼叫順序刻意不動**：參考答案含 24 處人工校正，換順序等於動到 gold 的產生方式。
- 接線：`run_single_query`／`run_interactive`／agentic synthesize／`api_server`。
  串流那條走**送完再送一次定稿**（新 `revision` SSE 事件，只在真的動過才發，舊前端會忽略）。

**驗收**：`verify_answer_validators` 新增閘門⑲（18 項），**239 → 257 全 PASS**，其餘三道不動。
⚠ **⑲g 的症狀是第一版猜錯的**：原本斷言「順序調換會吐巢狀」，實跑發現 `repair_paired_yi`
會把巢狀自己拆掉、於是**乾淨地留下 LLM 那個錯值**（84.75 而非 847.5）——外觀完全正常，
危險得多。現在斷言的是**值**不是格式。

⚠ **仍未量**：新模型犯這個錯的**頻率**是否高於舊模型。緊配對形態量不到它
（那 1578 組全是 `convert_usd_units_to_yi` 產的，不是 LLM 寫的）。

### 套件化的端到端驗收：兩輪 fixture 重放，與重構前無法區分

閘門是零 LLM 的**結構**驗證，覆蓋不到「答案品質有沒有變」。所以補跑
`record_web_fixture --mode replay`（零網路、bare strict、唯讀）兩輪 ＋ `check_web_claims`。

**先定基準再跑，避免事後解釋。** 重構前 10 輪逐輪判定拉出來：
· `web-02`／`03`／`04`／`05` — **10/10 全 PASS**，一次都沒浮動。
· `web-01` — r1~r3 跑在重錄前的壞 fixture（必 FAIL）；重錄後 r5~r11 是 **4 PASS / 3 FAIL**。
→ 判準：**web-02~05 任一輪 FAIL ＝ 回歸**；**web-01 單輪 FAIL 不帶訊號**（它本來就 ~57% 浮動，
  成因是取樣變異，見上面 `web_ignored` 那條）；**任何 `FixtureMiss`／`ReplayCacheMiss` ＝ 回歸**。

**結果（r12／r13，重構後）**：

| 指標 | 重構前 | 重構後 |
|---|---|---|
| replay cache | `hit=36 miss=0` | `hit=36 miss=0`（兩輪都是） |
| 每題 web 呼叫（01~05） | `1／3／0／3／0`（r7~r11 五輪全同） | `1／3／0／3／0`（兩輪都是） |
| `check_web_claims` | web-02~05 恆 PASS | **5/5 PASS**（兩輪，含浮動的 web-01） |

**`hit=36 miss=0` 在 bare strict 下是這次最強的一條**：它代表重構後管線送出的**每一個中間
產物**（plan／replan／英譯／Grader／ratio／期間意圖／ticker）與重構前**逐字相同**——
任何一個 key 變了都會當場 `ReplayCacheMiss` 而不是靜默給出不同答案。

⚠ **反向證據不可省**：r12／r13 兩輪答案**逐字 0/5 相同**，與重構前 r7／r9／r11 也是
0/5、0/5、1/5。**判定穩定的同時文字一直在變**——只看「全 PASS」會把「量尺死了」
誤讀成「系統穩」，這個坑 `check_web_claims` 自己踩過一次。

⚠ **這兩輪不證明什麼**：5 題、跑的是 live web 路徑。切塊、期別排序、多公司題、
multi_hop 都不在裡面——那些靠的是四道閘門的 522 項。

---

### agentic_rag_v2.py 套件化：4549 行單檔 → agentic_rag_version/ 四個模組

**分層是單向的**：`retrieval` 664 → `validators` 1560 → `tools` 294 → `graph` 1287，
`__init__.py` 1206（門面 ＋ 生成／補救層）、`tracing` 18、`__main__` 13。
CLI 變成 `python -m agentic_rag_version`。

⚠ **中途拆成 11 個模組，被打回來重合併成 4 個，而那個回饋是對的。**
我把「一個職責一個檔」當成目標，但那不是目標——**能不能一眼知道去哪找**才是。
11 個檔的代價是跨模組 import 爆炸（`nodes.py` 一個檔就從 9 個模組拉東西），
而那些邊界多數只是我切的，不是碼上本來就有的。
合併只花了一輪，因為 **522 項閘門就是合併的驗收**——合錯了會當場叫（實測叫了兩次：
`_COVERAGE_SOURCE_RE` 留了一行合併前的 import 造成真循環、⑬f 抓到 export 沒重生成）。

**九步，每步都是獨立 commit ＋ 跑完四道閘門**。閘門從 85／238／17／172 走到
**85／239／17／181**，全程 PASS——這 512 → 522 項就是這次重構唯一的驗收標準。

**這次重構真正的難點不是怎麼切，是兩個「切壞了外觀零差異」的耦合。**

**① 14 處 `ar.<name> = stub` 的 monkeypatch。** eval 全靠它攔截，而那是改**套件物件**上的
綁定。呼叫端一旦寫成 `from .webtools import _tavily_search`，名字就綁死在呼叫端 globals，
stub 再也蓋不到 → `verify_web_gate_isolation` 保證的「eval 絕不連網」會**真的連網**，
而閘門本身照樣全綠（它只數自己那個 stub 被叫幾次）。常數同病
（`ENABLE_WEB_SEARCH`／`QUERY_WEB_BUDGET` 也被直接改寫）。
→ 規則：**定義在哪個模組無所謂**；子模組**不得裸用**那些名字（一律 `import agentic_rag_version as _pkg` 再
`_pkg.<name>`，**循環 import 是刻意的**——屬性在呼叫時才解析），且**不得 `from .x import`**
它們。**定義在哪個模組無所謂**。守門是新的閘門⑬（9 項）。

**② 兩道閘門用 `ast.parse(Path(ar.__file__).read_text())` 找函式**（⑭a／⑮）。
單檔時 `ar.__file__` 就是全部原始碼，套件化之後它只是 `__init__.py`。
→ 改成掃**套件裡每一個模組**。**修法不該是「把函式搬回 `__init__` 遷就量尺」**，那是耦合。

**踩出來的坑，一個都沒有是「切錯地方」，全部是量尺或工具的邊界**：

· **⑬a 的反推漏了元組賦值。** 它用 regex `\b_?ar\.(\w+)\s*=(?!=)` 從 eval 腳本反推 patch
  名單，而 ⑦e 那行是 `ar._tavily_raw, ar.ENABLE_WEB_SEARCH = _boom, True`——匹配不到，
  於是 `_tavily_raw` 從來沒進過凍結清單，拆 webtools 時被留成裸用，**⑦e 當場 FAIL**。
  改用 AST 展開 Tuple 目標（13 → 14 個名字）。
  → **「從腳本反推」的價值全繫於反推得完整**，而這正是它會漏的方式。

· **手列 export 必漏。** `__init__` 的 re-export 清單本來手列，漏了 `_WEB_UNCITED_MARK`
  → 閘門⑱ AttributeError。回頭重算 `freshness`，發現**手列的 27 個裡漏了 9 個**——
  它們只是還沒被任何斷言碰到。改成從各模組 AST 生成，加閘門⑬f 守它
  （變異測試：拿掉一個 export 當場 FAIL）。

· **`global` 宣告的模組級快取看不見。** 缺項偵測把函式內的 Store 也算成已綁定，
  而 `global _kb_coverage` 正是這個形狀 → 兩道閘門 NameError 炸掉才現形。

· **⑬b 第一版把「定義」也當違規**，代價是那 14 個名字被永久釘在 `__init__`、檔案瘦不下去。
  收窄成只抓「用 import 複製綁定」，並補 **⑬e4 反向誤報對照**（定義處不得被誤報）。
  四條誤報對照現在兩個方向都有守。

· **⑬c 第一版只看 `Call`。** 常數是 `Name` load 不是 call，`if ENABLE_WEB_SEARCH:` 完全看不到。

· **加「跨模組同名函式」斷言時第一版立刻誤報**：`ast.walk` 會撈進類別的 method，
  於是每個 `__init__` 都算重複。收窄成只掃 `tree.body`。
  ——「稽核回報 FAIL，先問是不是量尺錯」當場又生效一次。

⚠ **CHANGELOG 的舊條目刻意不改名**：當時的檔名就是 `agentic_rag_v2.py`，改掉是竄改歷史。

---

## 2026-09-02

### mixed 的答案側第一次量到；R6「web 未採用」揭露上線

**① 補上 mixed 的 prod fixture（21 題，唯一花額度的一步）。** 綁 `us_stock_rag_edgar_multiyear`
＋ as-of 2026-09-02，自己一份 fixture 與 replay cache。
⚠ **刻意不併進既有的 `web_fixture_newsroute_prod.json`**（那份綁 as-of 09-01）：`_url_published_date`
對未來日期一律丟棄（`d <= as_of`），今天抓的頁面錄在 09-01 之下會**失去日期** ＝ 把一個人工痕跡
烘進基線。日後 A/B 各自對同期基準比（CLAUDE.md 那條）。

**基線（三輪重放，`hit=351 miss=0`）**：21 題 **0 危險**，每輪 20 `web_grounded` ＋ 1 `admits_gap`。
⚠ 反向證據：**21 題三輪的答案沒有任何一題逐字相同**——LLM 一直在變、判定不動，這一輪是真的量測
不是被凍住。⚠ `mh-09` 從 08-20 那份舊資料的 `kb_only`（危險）變成 `web_grounded`，方向對，
但那是跨架構／跨 collection／跨 as-of 的單輪對照，**不構成證據**。

**② `web_ignored` 的完整圖像：8 / 111 題×輪＝7.2%**（`newsroute_after2` 18 ＋ mixed 三輪 63
＋ `web-01` 重放 30）。
⚠ **這一格 2026-09-03 修正過：原本寫 1／116，那是錯的**——當時的稽核腳本讀 `web_extra` 欄位，
而結果檔存的是 `n_web_calls`，於是 288 份答案裡**一份都沒進母體**卻印出一個像樣的數字。
「稽核回傳 0 筆先當壞消息查」這條規則第三次生效，而這次是我自己漏掉的。
→ **母體要先印出來**：任何比率型稽核都要同時印分子與分母，只印比率看不出分母是 0。

**但 7.2% 不等於 7.2% 的「安靜無視新數據」**，八筆逐一看過：
· `mh-03`（1）：純財報題，web 是別的子問題打的，答案 KB-only **本來就對**。
· `mh-07`／`mi-08`（3）：答案明寫「知識庫中查無相關資料」＝**誠實承認**，正是想要的行為。
· `web-01`（4）：其中 **3 筆（r1~r4）跑在重錄前的 fixture 上**，那份 fixture 裡根本沒有 Apple 
  市值（fixture 重錄於 12:03，r3 是 11:34、r5 是 12:08）——**模型手上沒有東西可用**。
→ 真正的殘留只有 **`web-01` r10 一筆**：同一題、同一份好 fixture、同一個模型，
  r5~r9／r11 都引用了 web，**只有 r10 沒有**。
→ **所以這不是能力上限，是取樣變異**（`GEN_TEMPERATURE=0.3` ＋ MoE 不固定路由）。
  能力上限長的是 0/7，不是 6/7。**換更大的模型不能從這份證據推出來**。
→ **R6 的修法端到端量不出來**（它只揭露不重生成），這點先寫在前面。

**③ R6 上線**（`web_fetched_but_uncited_notice`）：`web_extra` 非空且答案零 web 引用 →
機械式附一句「本次已取得網路搜尋結果，但最終答案未引用其中任何來源」。
· **是揭露不是重生成**：web 真的回垃圾時那句話字面為真，誤報方向安全，且零 LLM 成本。
· **必須掛在 Synthesize 不能掛 todo 層**：`_format_unresolved_freshness_notice` 對 `web_used`
  為真的待辦直接 `continue`，而這裡的病正好是「`web_used` 為真、答案卻沒引用」。
· 乾跑 359 份答案觸發 22 次（6%）。比 R5 高一個數量級，但**揭露的門檻本來就比重生成低**：
  一句話、零 LLM，而且每一次觸發時它字面為真。

閘門⑱ 8 項、**5/5 變異全抓到**（228 → **238**）。

⚠ **⑱f 的第一版是恆真的，變異測試當場抓到。** 原本驗「附上揭露句後
`check_news_routing.classify` 判定不變」——那必然成立：揭露句以 `\n\n---\n⚠` 開頭，命中
`rq.EVIDENCE_TAIL_RE`，每個呼叫 `strip_evidence_tail` 的消費端**根本看不到它**（把措辭改成含
承認詞的「本次無法取得可用的網路新聞來源」照樣全綠）。改成驗**讓它安全的那個不變量**——
邊界前綴——並配一條誤報對照（⑱f2：拿掉前綴，同一段文字會把 `kb_only` 翻成 `admits_gap`）。
→ **凡是「機械式附加到答案」的東西都要驗這一條**，否則量尺會被受測物的輸出改變。

**④ 試著收緊 R5 的 KB 側，量完決定不做。** r11 那輪 `web-01` FAIL 的是「財報那側沒有日曆
日期、web 那側有」，而 R5 接受檔名期別戳所以沉默。試著改成「對稱要求」：179 份並陳答案的
觸發從 2 變成 9，而**新增的 7 筆全是假配對**——
`news-14`（KB 九個月服務收入 917.28 億 vs web FY2025 全年 1,092 億，不同期間）、
`news-03`（KB 九個月營收 vs web 盈餘，不同指標）。
→ **金額量級配對是「同一個量」的代理不是它本身，而「財報側接受檔名戳」正是在吸收這個不精確
——⑰i 是承重的，不是讓步。** 因此 `web_claims` 的 `web-01` 斷言**比 R5 嚴格是正確的**：
那一題經人工確認兩個值是同一個量，validator 沒有這個知識，兩者不該對齊。
**真正的解法**是讓配對精確：把 R4 的 bucket key 做單位正規化（它已有 LLM 抽的 `metric`／
`entity`，只差 `unit`）。那要動 R4 的行為，需要它自己的乾跑與閘門，已記在碼上。

---

### R5「並陳必須帶時點」validator 上線；web query 形式那條是 null result

**① web query 形式 → 撤回，不動生產。** 第一輪的 2×2 看起來像「帶 ticker 大幅改善命中」
（nvda 股價 0/2 vs 10/10）。**三小時後的第二輪沒有重現**：nvda A 從 0/2 變 **7/12**、
msft D 從 0/4 變 **6/11**、apple A 從 5/12 變 **0/2**。同一句 query 的 top 分數兩輪間
**0.377 ↔ 0.899** → Tavily 的時間變異蓋過形式效應，單輪 n=3 分不出任何東西。
`eval/probe_web_query_form.py` 保留當方法（加了 `--fixture`，**第二輪必須換路徑**，
否則 record 模式會先命中舊 fixture ＝ 量到重放不是重複）。

**② R5 上線**（`find_undated_dual_sourcing`）：答案把 web 金額與財報金額並列時，兩邊都要在
**同一句**裡交代時點。掛在 Synthesize **最末**——時點是措辭，前面每一道 validator 的重生成都
可能改掉它，放中間等於只驗了一個會被後面推翻的版本。

**為什麼 R4 抓不到**：`find_unreconciled_web_conflicts` 的 bucket key 含 `unit`，而失敗那輪
寫的是 `$4.75 兆` 與 `$4342.02 billion` → **兩個單位落到不同桶**，規則從頭到尾沒觸發。
R5 把金額正規化成百萬美元再比，也不需要 LLM 抽的 `claims`。

**收窄了兩次，兩次都是乾跑（281 份既有答案）逼的**：

| 版本 | 觸發 | 問題 |
|---|---|---|
| v1「同時引用 web＋財報就要有時點」 | **34/123（28%）** | 多數是新聞敘述引用 |
| v2 加「兩邊都要有可比較的金額」 | 4/123 | 其中 **3 筆**是 U+2011 不換行連字號造成的誤報 |
| v3 加 dash 正規化 | **1/123，就是真陽性** | ✔ 達到進主線的標準 |

⚠ **那個連字號值得記著**：`2026‑09‑01` 看起來與 ASCII 版一模一樣，但 `[-/年]` 匹配不到。
`eval/check_web_claims._norm` 早就在做 dash 正規化，這裡沒抄到那一課。
→ **凡是拿 regex 讀 LLM 寫出來的日期，都要先正規化 dash。**

閘門⑰ **12 項、6/6 變異全抓到**（總數 216 → **228**）。⚠ **⑰i 是變異逼出來的**：第一版拿
⑰a 的測資去測「檔名期別戳算不算時點」，而那份測資的財報句本來就寫著日曆日期 →
**檔名戳那條路從來沒被測到**（把 `_FISCAL_MARK_RE` 拿掉全綠）。同一天第二次踩到
「斷言的名字與它真正量的東西不一致」。

**⚠ 驗收僅止於確定性那一半——不要當成端到端改善。** 掛上前後各三輪，`web-01` 都是 **2/3**：

| | before（r5/r6/r7） | after（r8/r9/r10） |
|---|---|---|
| 「並陳缺時點」型 | 1 次（r6） | 0 次 |
| **`web_ignored` 型** | 0 次 | **1 次（r10）** |

**n=3 遠低於任何解析度**，不能宣稱它修好了什麼。真正成立的是：R5 在 r6 那份答案上會發話
（逐字凍結在 ⑰a），而在其餘 280 份上沉默。

**③ r10 反而暴露了另一種病，而且它有第二個獨立實例。** r10 的答案是
「Apple 目前的市值約為 43,420.2 億美元【AAPL_Fundamentals_20260612.txt, chunk #0】」——
`n_web_calls=1`、fixture 裡就有 $4.75 兆，而答案**一個 web 都沒引用**，把 **82 天前**的快照
當「目前」。R5 對這一型**正確地沉默**（沒有並陳就沒有時點問題）。
另一個實例是 `mh-09`（同日由 mixed 不對稱判定撈出來的，`c=3`）。兩者在 `check_news_routing`
的第二軸都歸在 `web_ignored`。
⚠ **不是「web 回垃圾」那一型**：r8/r9/r10 重放的是**同一份 fixture**，而 r8/r9 兩輪都引用了它
→ 成因在 Generator 的抽樣，不在輸入。已登錄 BACKLOG。

---

### `web-01` 解除 block，順帶量到「並陳」其實不穩（1/3 掉出去）

Tavily 恢復正常後重錄 `web-01`，答案做的正是這條斷言要測的行為：
「$4.75 兆（截至 2026-09-01）【web: stockanalysis.com】＋ KB 的 $4,342.02B（2026-06-12）」。

**但三輪重放是 PASS／FAIL／PASS**，而拆開來看兩種 FAIL 只有一種是真的：

| 輪 | 現象 | 歸屬 |
|---|---|---|
| r5 | 兩個時點都在 | PASS |
| **r6** | 引用了 web 的 $4.75 兆卻**完全沒給時點**，還寫「兩者皆屬於同一時間段的不同來源」（六月 vs 九月） | **真缺陷** |
| r7 | 兩個時點都在，但 KB 日期寫成「2026 **年 6 月 12 日**」 | **量尺誤報** |

**同一個錯我在這條斷言上犯了兩次**：第一版把值寫死（`4[.,]4\d`，綁 2026-08-15 那次錄到的
~$4.4 兆），重錄後就死了；我改成綁 ISO 日期字面，**下一輪就誤報**——那只是換一種綁法。
三輪寫出三種形式（`（截至 2026-09-01）`／`此數據來自 2026 年 9 月 1 日的最新報告`／
`依據 2026 年 6 月 12 日的…`）。現在兩條 `must_match` 都**不綁數值也不綁日期寫法**，
四條誤報對照（`FY2026`／`2026 年`／`2026-08-26`／`2026 年 6 月`）一條都不中。

→ r6 那個 FAIL 寫進 BACKLOG：**「並陳」是 Generator 靠 prompt 做的，validator 那條防線是空的**
——該條 `why` 早就這樣寫了，現在有數字。`web-01` 的兩條日期斷言就是修它的驗收指標。

**最終穩定性**：`web-02`／`03`／`04`／`05` 橫跨 **r1~r7 七輪全 PASS**；不穩的只有 `web-01`，
而那是受測物不是量尺。

---

### `check_news_routing` 的 `mixed`：從「不判定」改成**不對稱判定**

原本整個 kind 都不判定，理由是「分不出新聞那一半是不是也用財報答的」。重讀那句話發現
**它只對好的方向成立**：

- `web_grounded` → **仍然不判定**（整份答案有一個 web 引用，不代表**新聞那一半**用的是它；
  要判它需要逐宣稱歸屬＝感知不是規則）
- `kb_only`／`ungrounded` → **判危險，而且是純規則**（mixed 題**依定義**帶著流的半邊，
  整份答案零 web 引用又沒承認 → 那半邊必然不是用 web 答的，與 flow 是同一個推論）

少了這一格，22 題（21 題在冷凍 37、1 題 `col-08` 在生產 65）的**危險方向完全沒有量尺**。
加上去當場撈出 **`mh-09`**（21 題裡 1 題）——而它正是 docstring 裡**逐字凍結的誤報對照**：
同句規則早就正確判成 `kb_only`，只是先前不准講。

**順手修掉一個連帶的不一致**：第二軸（成因歸因）寫死 `kd == "flow"`，於是 mixed 有了危險態之後
`mh-09` **有判定卻在第二軸完全不出現**。改成讀判定表（`"危險" in _VERDICT[kd][st]`）
——列 kind 清單就會漏掉下一個。現在它現形為 `web_ignored`（打了 web 卻沒引用 → Generator 側），
與 r6 的 web-01 是**同一個病灶**。

selftest **19/19**（新增一條測判定表的不對稱性本身：危險邊要與 flow 同判語、好的那邊要維持
不判定——少了它，「mixed 全部判危險」與「退回全部不判定」兩種改動都不會被發現）。

⚠ **這不會讓「一律送 web」的實作變好看**：擋它的仍然是 `record` 那半的陰性對照。
⚠ **mixed 目前只有一份結果檔量得到**（`web_fixture_answers_news37_all.json`，08-20、mdna
collection、route 改動之前）。要知道現況得重跑那 21 題的 live-web。

---

### 重錄 `eval/web_fixture.json`：live 量尺不再 flaky，成因是 fixture 不是斷言

BACKLOG 掛了三週的「必須重錄」做掉了。綁 `us_stock_rag_edgar_multiyear` ＋ as-of `2026-09-02`，5 筆 web 回應。

⚠ **重錄前必須清空 `--replay-cache`**，差一點整批報廢：舊快取的 plan 全是**加 `route` 之前**的
純字串陣列（`["Apple 目前的市值是多少？"]`），`_parse_plan_output` 會把它們落到 `kb`
→ **一次 web 都不打，錄出一份空 fixture**。這一條已寫進 CLAUDE.md 的檔案地圖。

**斷言逐條重新確認**：`web-05`／`web-03`（兩個陰性對照，web 各 0 次）、`web-02`
（`numbers_must_be_in_fixture` 自適應，NVDA `$215.38` 溯得回 CNBC）、`web-04` → PASS；
`web-01` 先判 **N-A（blocked）**、**同日解除**（見下），解除後三輪 **PASS／FAIL／PASS**——那 1 個 FAIL 是真缺陷。KB 側三個寫死的值都還在（`416.16`／`2026-06-12`／`4342.02`）。

**四輪重放（bare `strict` ＋ 唯讀）判定逐題完全一致**，`hit=36 miss=0`：

| id | r1 | r2 | r3 | r4 |
|---|---|---|---|---|
| web-01 | N-A | N-A | N-A | N-A |
| web-02／03／04／05 | PASS | PASS | PASS | PASS |

⚠ **關鍵證據不是「四輪一樣」，是「四輪一樣的同時答案全都不一樣」**：web-05 的答案是
521／448／327／451 字，五題**沒有任何一題**在任兩輪逐字相同。只看前者會把「量尺死了」
誤讀成「量尺穩」——那正是這個專案犯過六次的形狀。
→ 舊的 PASS,FAIL,FAIL,FAIL,PASS **成因是 fixture 綁在拔除新聞之前的世界**（`check` key 逐字
引用已不存在的 news chunk → 全 miss → 新 query → `FixtureMiss`），**不是斷言在抖**。
`--mode replay` 因此收緊成 bare `strict`（BACKLOG 寫的收緊條件滿足了，證據是 `miss=0`）。

**新增 `blocked` 狀態**（web-01 用了幾小時就解除，但機制留著）：fixture **在輸入層**就承載不了
某條斷言時標它 → 判 **N-A**、
理由與證據逐字印出，而 **N-A 照樣讓退出碼非零**（block 一條不會讓這支變綠，忘了解除也不會安靜過去）。
判準寫死在碼裡：**FAIL 的成因在輸入 → blocked；輸入有而系統沒用上 → 真 FAIL，不准 block**。
⚠ 刻意**不做成「拿 regex 去 fixture 自動找前置條件」**：`fixture_text` 是整份攤平的 blob，
一條「市值出現了嗎」的樣式會匹配到**別題**的回應（NVDA 那題裡就有 `Market Cap (intraday)5.262T`）
→ 自動判準會在錯誤的證據上放行。人工設、寫下證據、可被 diff 看見，比一個會誤判的自動判準誠實。
用既有的 news37 結果檔回歸驗過：**40 PASS／0 FAIL／0 N-A**，與改動前相同。

**這次重錄真正挖出來的是兩個系統面發現**（都寫進 BACKLOG〈已知缺陷〉）：
1. ~~**「現在市值」的 web query 不帶 ticker → Tavily 確定性回垃圾。**~~ → **同日被我自己推翻。**
   原始觀察為真：`What is Apple's current market capitalization?` 拿回 12 筆垃圾（最高分 **0.377**
   是一支 Tim Cook 影片，其餘是首頁、**`en.wikipedia.org` 的「Capitalization」詞條**、世界銀行 GDP
   指標、`capitalone.com`），**兩次獨立錄製逐字相同**。錯的是從那裡推出的「系統性不是偶發」——
   ⚠ **相隔十分鐘的相同回應，同樣可以只是 Tavily 在快取那個 query。** 缺的對照不是「再錄一次」，
   是**時間上分得夠開的一次**。約一小時後同一句拿回 `companiesmarketcap.com`／`stockanalysis.com`／
   `macrotrends`（**5/12 有值、top 0.899**），重錄 `web-01` 就答出
   「$4.75 兆（截至 2026-09-01）【web: stockanalysis.com】＋ KB 的 $4,342.02B（2026-06-12）」
   ——**正是這條斷言要測的並陳行為**。`blocked` 已解除。
   ⚠ 白名單那一項**不受影響、仍然成立**：`en.wikipedia.org`／`data.worldbank.org`／`capitalone.com`
   **根本不是**任何 allowed domain 的子網域 → `include_domains` 連「包含式」都算不上，
   **本地 `_host_allowed` 複核是唯一真正生效的那一層**。

   **順帶量到一個真的**（`eval/probe_web_query_form.py`，2×2、零 LLM、經生產 `_host_allowed` 複核）：
   **query 帶 ticker 會大幅改善命中**——nvda 股價 A 問句無 ticker **0/2** vs B 問句有 ticker **10/10**，
   msft 市值 0/3 vs 1/12。而**關鍵詞形式六格全是 0**：`_web_query_en` docstring 記的 2026-08-13
   「`Apple market cap` → score 0.91」**今天不再成立**。
   ⚠ 只有一輪，而 Tavily 已證實會隨時間變 → **動生產前要跑時間分開的第二輪**。
   ⚠ 且**不可以拿「web-01 解除 block」當這個修法的證據**——那是為了讓 fixture 錄得漂亮而改受測物。

2. **web 回應「非空」不等於「答得出來」。** `_unfulfilled_web_route_gaps` 判的是 `web_notes` 空不空，
   拿回一堆無關但非空的頁面時**不留缺口**，外觀與「web 成功回答了」相同。這是那個函式的設計範圍
   不是 bug，記下來是為了讓這個洞**有名字**。可見後果：答案標題句寫「Apple **目前**的市值約為…」，
   值來自 **82 天前**的快照。⚠ **時點有揭露**（第二句寫了 2026-06-12），是「標題句措辭」不是「零揭露」。

⚠ **`web-01` 被 block 期間，「KB 快照 vs web 當日值並陳」這個行為全碼庫沒有載體**：
`web_claims_news37.json` 那 40 條沒有 `col-08`，而 `sem-06` 的註記自己寫著「web-01 對同型問題的
斷言正是 `web_calls_gte 1`」。這一句寫進斷言檔的 `_role_at_risk`，不是只放在腦子裡。

順帶：`mh-09` 那條「**打了 web 卻沒引用**：尚未判定是缺陷還是 web 真的沒撈到可用內容」的觀察，
web-01 這次把**同一個現象**的成因查出來了（輸入真的沒有那個事實），支持後者那一支——
但那只是一個實例，**不能替 mh-09 自己下結論**。

---

### `depends_on` 的實際解析：量下去沒有實例，撤回

route 站穩之後回頭處理 BACKLOG 留的 `depends_on`（欄位存在但零讀取端，名字取自 `mh-07`
的「該公司」）。**先量再改**：拿生產 5 題 multi_hop 跑 Planner **30 輪／78 個子任務**。

| | 次數 |
|---|---|
| 單一公司的**扇出**子任務（每家、兩跳指標各問一次） | **72/78** |
| 延後指涉（「總營收最高的公司的毛利率是多少」） | 6/78 |
| **通靈成某一家** | **0/78** |

原因是候選集合就寫在題面上（「在 A、B、C 三家中」），Planner 扇出、讓 Generator 讀著數字比大小
——對**封閉候選集**這是正確策略，結構上不需要依賴解析。這也解釋了 `check_comparison_claims.py`
08-29 量出的 40/40 PASS。

那 6 個延後指涉再往下追一層（真正的危險是它在 ticker 解析被**猜**成某一家 → hard filter
鎖到別人的財報，比不解析更糟）：`parse_query_filters` 9 次**全回 `ticker=None`**，
失敗形狀是「沒有 hard filter → 退回廣泛語意搜尋」＝浪費一個子任務，**不是鎖錯公司**。

**另外兩件事讓它更不值得做**：① 欄位名取自的 `mh-07` 在**冷凍 37 題**裡（不計分），
且它第一跳要的是**新聞**——KB 依設計沒有，做出完美的拓撲排序第一跳照樣空手，
那裡的綁定約束是語料不是依賴。② 代價是把 `_node_execute` 的單波 `ThreadPoolExecutor`
拆成多波，序列化一條**一題已燒 50~60 次 LLM** 的關鍵路徑。

→ 搬進 [`BACKLOG.md`](BACKLOG.md)〈已接受的極限〉並附三條復活條件。

⚠ **這是我自己留下的欄位**：留欄位不解析在當時是對的，但我沒量頻率就把它寫成待辦，
於是它看起來像一筆工作。**量了之後它不是。**

---

### 三個 kind 落在哪個題庫——一張對照表推翻了兩個「下一步」提案

| kind | 冷凍 37 | 生產 65 | probe_only |
|---|---|---|---|
| `flow` | **16** | 0 | 0 |
| `mixed` | **21** | 1（`col-08`） | 0 |
| `record` | 0 | **7** | 8 |

**這同時是對 09-01 那則回報的更正**：「flow `kb_only` 2→0、財報污染 36→20→8」量的是
**冷凍 37 題**（走 news37 fixture 那條 live-web 跑分）。那是它們該被量的地方，但不能讀成
「生產 65 題變安全了」。route 改動在生產 65 上真正被覆蓋到的是 **7 題 record 陰性對照
（浪費誤判 0/75）＋ `col-08`**。

連帶地「那 21 題盲飛、隨時會悄悄壞掉」也不成立——它們不在計分的 65 題裡。

---

### mixed 的**計畫側**量了：0/66，且完全穩定

`probe_route_classification.py --only mixed --repeat 3 --as-of 2026-09-01`（22 題）：

```
弱判準未達（缺一邊）   0/66 次   涉及 0/22 題
跨輪不一致            0/22 題
route 出現次數        {'kb': 63, 'web': 63, 'both': 3}
```

每一題都同時產生 kb 與 web 待辦（`col-08` 用 `both`）。

⚠ **這個「全綠」要跟 record 那半一起讀才有意義**：mixed 的判準很弱（只要求兩邊都有代表），
一個「一律 both」的 Planner 在這裡也會滿分——擋住它的是 record 的**浪費誤判 0/75**。
兩格分開看都沒有判別力，合起來才有。

⚠ **這只量到計畫側。** 答案側（新聞那一半是不是也用財報答的）仍然沒有量尺，
`check_news_routing.py` 對 mixed **只計數不判定**那條註記照舊成立。

---

### `llm_replay` 唯讀模式：堵住 A/B 的**單向**污染（閘門⑫，10 項）

`atexit` 無條件回寫 → **先跑那臂的 miss 變成後跑那臂的 hit**（實測 hit 23→32，兩臂不可比）。
這條污染是**單向**的（永遠偏袒後跑的一方），而且兩臂外觀都「掛了 fixture」，看不出異常。

`RAG_REPLAY_READONLY=1`：`put()` 早退（**守入口不是守出口**——連誤呼叫 `_flush()` 都寫不出去），
`get()` 照常讀。**刻意 opt-in**：BACKLOG 記的卡點「有沒有既有流程依賴回寫」查清楚了，有三個
（`record_web_fixture --mode record`＋`probe_historical_benefit`／`probe_temporal_interference`
會自動把快取指到 `eval/replay_cache.json`）。`--mode replay` 自動開唯讀——**strict 只涵蓋四個
kind，其餘三個 miss 之後仍會回寫**＝「replay」那一輪會偷偷改掉 fixture，
與 `web_replay.note_meta` 踩過的「證據自我抹除」同一個形狀。

閘門⑫ 判別力在三條，**變異測試 5/5 全抓到**：
· **⑫e** 先重現污染（非唯讀下 armB 真的 hit）再證明它消失——少了前半，「armB miss」可能只是
  這個測試沒建立前提。
· **⑫i** 唯讀時**既有的** key 照樣 hit → 擋掉「唯讀＝把模組關掉」的實作（N2），那會讓兩臂
  **一起**失去 fixture，比污染更糟且更難察覺。
· **⑫f** 唯讀時 hit/miss 照樣印 → 非唯讀唯一的出口是 `[replay] wrote` 那行，唯讀不寫檔就沒有
  → **miss 完全隱形**，外觀與「系統沒走那條路」相同（N4）。
· N3 專測「守衛放在 `put()` 而不是 `_flush()`」，N5 專測「`readonly()` 被快取成 import 時常數」。

**順手更正兩處過期記載**：BACKLOG 寫的「`record_web_fixture --mode replay` 沒設 strict」
早在 08-29 就設了；而 `check_web_claims` flaky 的那組 PASS/FAIL 翻面數字量於 08-28 之前，
**根因鏈的源頭此後被堵過兩次**（`replan` 註冊、唯讀）→ 「還 flaky 嗎」現在是個**沒有答案的
問題**，不是已知缺陷。⚠ 但重測卡在 `eval/web_fixture.json` **結構性不可重放**（錄於 KB 還有
新聞的時代），要先重錄。**不要先去改 fixture 的 key**：比對放寬會讓一次搜尋配到**錯的**那筆
錄影，比 miss 更糟（miss 會炸，配錯不會）。

四道閘門：**85／216／17／172 全 PASS**。

---

## 2026-09-01

### 新聞題「拿財報冒充新聞」第一次有量尺（`check_news_routing.py`）

**被一份實測資料逼出來的**。拿「Tesla 最近有什麼重要消息」跑 `probe_replan_contribution.py`，
commit 進答案的 chunk 逐個看是 `TSLA_10K_2023#59`／`TSLA_10K_2024#63`／`TSLA_10Q_202506#83`
——**一則新聞都沒有**（KB 依設計不收新聞），而那支探針把它們記成「引用貢獻＝3」。
**那個指標量的是「chunk 有沒有被引用」，不是「引用它對不對」**，在流資訊題上正好量到反面。

在這支之前**全碼庫沒有任何量尺**分得出這兩件事：Synthesize 的五道 validator 一道都不會響
（chunk 是真的、數字溯源得到、答案沒宣稱期別）。

**設計**：五個不可合併的狀態（`web_grounded`／`admits_gap`／`kb_only`／`ungrounded`／`no_answer`），
只讀答案本體的引用標記與措辭，零 LLM／零網路／零 Qdrant。三個關鍵決定：

1. **狀態名中性、判定分類相對**。`kb_only` 在 flow 題是危險態、在 record 題是正確行為。
   把分類烘進狀態名（例如叫它 `filing_as_news`），record 那一半的誤報就再也看不見了。
2. **`record` 陰性對照不可省**。只量 flow 的話，一個「所有題目一律送 web」的實作會滿分
   ——而那正是它要驗收的改動最可能的失敗方式。
3. **判別力集中在「承認措辭必須與流資訊範圍詞同句」**。詞表刻意寫寬，判別力放在同句共現。
   實測整篇比對在 40 份答案裡誤報 3 筆，分屬三種形狀：
   ① `mh-09` 承認的是**別的細節**（金額）而主張照樣斷言；② `news-08` 承認在後句、「新聞」在前句
   ＝**跨句湊對**；③ `news-06` 的「對 NVIDIA **未提供**獨立伺服器 CPU」是**純業務敘述**，
   跟資訊可得性無關——這一型光靠收窄詞表擋不掉。三型各有一條逐字凍結的誤報對照。
   ⚠ `最近` 刻意不收進範圍詞：它同時是流資訊與財報用語（「最近一季」），
   實測加進去對 40 份答案**零差異**＝只增加誤報面、買不到判別力。

**自測 18/18；變異測試 7/7 全抓到**（含「改成整篇比對」「不切證據尾巴」「只認半形 web 標記」
「優先序顛倒」「record 與 flow 共用判語」）。

**跑 `web_fixture_answers_news37_all.json`（40 題，web 開、as-of 2026-08-20、mdna collection）**：

| kind | 結果 |
|---|---|
| flow（16） | `web_grounded` 14／**`kb_only` 2**（news-05、news-09） |
| record（3） | `kb_only` 3、web 呼叫 0 → **陰性對照乾淨，web 沒有被無差別觸發** |
| mixed（21） | 只計數：`web_grounded` 19／`admits_gap` 1（mh-07）／`kb_only` 1（mh-09） |

**第二軸把成因分開了**（兩種病的修法不同，不可合併成一個失敗率）：
兩題危險全部是 `no_route`（`web_calls == 0`，路由沒觸發 → Grader／路由層），
`web_ignored`（打了 web 卻沒用 → Generator）0 題。

### 路由分類探針：定性財報題的誤判 **13/75 → 0/75**（flow 那半 0/80 不動）

新增 [`eval/probe_route_classification.py`](eval/probe_route_classification.py)。閘門⑪ 驗的是
route 有沒有被正確**接線**，它**完全不問** Planner 判得對不對——這支補那一格。

#### ⚠ 第一版量出 0/115「完全穩定」，那是假的

探針**沒有設 `AGENTIC_AS_OF_DATE`**，而真實跑分設了（2026-09-01）。as-of 會進
`_build_temporal_contract` ＝ **Planner prompt 的一部分** → 探針量的不是同一個 prompt。
補上 `--as-of` 之後 `sem-06` 從 0/5 變成 **3/5 誤判**，與 after2 那次翻面對得上。
**「量尺與被測物的環境要綁在一起」又一次，這回是 as-of。**

⚠ 另外兩道守衛：① `RAG_REPLAY_CACHE` 開著會讓每輪命中同一份快取 → 報出假的穩定，
本檔**開跑前中止**（exit 2）而不是印警告；② 判別力已用變異驗過——把 prompt 裡 kb 與 web 的
判準對調，探針報 **flow 15/16 危險誤判 ＋ record 7/7 浪費誤判**。

#### 讓尾巴變得可量，再決定要不要改

原本的 record 只有 7 題、其中只有 2 題是定性敘述題 → 那條尾巴在既有題庫上量不到
（0/115）。補 8 題 `probe_only` 的定性財報題（10-K Item 1／1A／7 答得出來的：競爭、護城河、
商業模式、風險因子、供應鏈、地緣政治），**題目文字就地寫在 `news_routing_questions.json`、
刻意不進任何 eval set**（進去就改動分母，跨日期分數比不了）。

baseline：**13/75 次誤判、涉及 6/15 題**，其中 `qual-05`（Alphabet 反壟斷訴訟與監管審查）
是 **5/5 穩定地錯**——「反壟斷／監管／訴訟」被讀成新聞。

#### 修法：把第 2 步壓掉的那半條規則接回 route 的 `"kb"` 條目

原文是【不要注入來源類型】裡的「『如何/為何/怎麼做/靠什麼』這類定性題答案寫在 10-K 的
business / competition / strategy 段落，不是新聞」。第 2 步壓縮時我保留了「來源中性」那一半、
**把這一半刪了**，而誤判的正好就是這一族。加回去時額外寫明：**「監管」「反壟斷」「訴訟」
「地緣政治」「供應鏈風險」這些字本身不代表要上網**（10-K Item 1A 就逐條列著），
只有問句帶時間動態（「最近有什麼進展」「最新裁決」）才算 web——後半句是**防矯枉過正**的。

| | before | after |
|---|---|---|
| record 浪費誤判 | 13/75 次（6/15 題） | **0/75（0/15）** |
| record 跨輪不一致 | 6/15 題 | **0/15** |
| **flow 危險誤判（誤報對照）** | 0/80 | **0/80 不動** |

⚠ **方法學上要打折的地方**：`qual-01`~`qual-08` 是**看到失敗族之後才寫的**，所以
「13/75 → 0/75」帶有貼著已知失敗調整的成分。擋住這件事的是 flow 那半——它是
**這次改動最可能的副作用方向**（把定性題往 kb 推，順手把新聞題也推過去），而它 0/80 沒動。
真正的外推證據要等下一批沒看過的題目。

### after2 臂：兩個修正有效，但**陰性對照抓到 router 不穩定**

23 題／0 錯誤，全新 replay cache（刻意不沿用 after 那份——沿用會讓 plan 直接命中、路由決定
被凍結，就量不到 router 穩不穩）。

**兩個修正確實有效**，最清楚的指標是「flow 題的答案裡引用了幾個財報 chunk」：

    before 36 → after 20 → **after2 8**

殘留的 8 個幾乎全來自 replan **正當地**建立的 kb 待辦（news-07 的「查詢微軟 2026 Q3 10-Q
中關於 AI 策略的敘述」、news-15 的「Apple 的最新基本面數據」）——那是補充脈絡，不是拿財報冒充新聞。
flow 16/16 `web_grounded`，第二軸兩個成因仍是空的。

#### ⚠ 但 record 那半換了一題響：`sem-06`

「Amazon 在 AWS、Prime 與電商業務上面臨哪些反壟斷或監管風險？」——純 10-K Item 1A 的題目，
after 判全 `kb`，**after2 判全 `web`**。同一份碼、同一題、相反的答案（MoE 不固定專家路由）。

事前寫死的判準是「record 的 `web_grounded` 必須 ≤ 1」，after2 剛好是 1 ——**但那是換了一題，
不是同一題還在。判準達標是運氣不是穩定性。**

**3 輪路由探測（零網路、只跑 plan）**：23 題裡 1 題誤判（`sem-01`「NVIDIA 的核心技術護城河」
3 輪中有 1 輪判 web），而 `sem-06` 那 3 輪反而全對。**不穩的是同一族：定性的財報敘述題**
（護城河、競爭定位、監管與反壟斷風險），不是隨機散佈。

#### 很可能的成因：**第 2 步是我自己把那條規則壓掉的**

被壓掉的【不要注入來源類型】原文裡有這麼一段：

> 特別是「如何 / 為何 / 怎麼做 / 靠什麼」這類問策略、競爭定位、商業模式、產品佈局的**定性題**
> ——答案通常寫在 10-K / 10-Q 的業務描述（business / competition / strategy）段落，**不是新聞**

我當時保留了「來源中性」那一半，**把「定性題也在 10-K 裡」這一半刪了**——而現在誤判的正好
就是那一族。壓縮 prompt 時「這段話在防什麼」比「這段話讀起來冗不冗」重要。

**下一步**：把那段守備範圍接回 route 的 `"kb"` 條目底下，並先做一支正式的路由探針
（`--repeat` ≥3、附陰性對照）——搬到 Plan 讓路由**可稽核**，不代表它**準**，這一輪就是證據。

### 路由改動第 5 步：兩個殘留的洞都補掉，`_WEB_TODO_RE` 連函式一起刪除

#### ① web 預算用完時，`route=web` 不再退回 KB

after 臂量到 `news-10`／`news-13` 的財報引用不是來自 replan，而是**我自己寫的降級**：
`QUERY_WEB_BUDGET` 取不到額度時把 route 降成 `kb` → 又去撈財報 → 拿財報回答新聞題。
**那正是這整個改動要治的病。**

修法：`route=web` 拿不到額度 → **直接收工、不撈 KB**，並由新的 `_unfulfilled_web_route_gaps`
留下可揭露的時效缺口。判準是一貫的代價不對稱：空答案看得見，拿舊財報冒充新聞看不見。

⚠ 新缺口來源**刻意不看 `realtime_need`**。`_unmet_realtime_gaps` 的 docstring 早就點名這個
情境，但那條路要 Grader 跑過才有值——而 `route=web` ＋ 預算用完時根本沒跑到 Grader，
它必然沉默。新判準是結構性的：**Planner 說這題要上網、而網路一次都沒查到**，零判斷成分。

斷言 ⑪z1~⑪z5，其中三條是誤報對照：`both` 的 KB 半照撈、有預算拿到 web 時**不得**留缺口
（否則警語永遠印＝沒有判別力）、snapshot 下不得憑空多出缺口（eval 基準不動）。

⚠ **⑪z4 當場抓出我的樁寫錯**：樁回傳以「（」開頭，而生產約定那是「查無結果」標記會被濾掉
→ web_notes 空 → 缺口恆為真。又一次「樁要長得像生產的產物」。

#### ② replan 建的 todo 也帶 route

`_REPLANNER_PROMPT` 改輸出 `[{"task":…, "route":…}]`，並明令**不要把來源寫進 task 文字**
（不要再寫「改用網路搜尋查…」）。解析複用**同一個** `_parse_plan_output`（唯一定義點，
舊 replay 快取的純字串照樣吃、落到 `kb`）。replan 的重放 key 納入 route。

**`_WEB_TODO_RE` / `_is_web_todo` 因此沒有呼叫端 → 刪除**，碼上留墓碑註解（照
`_RELATIVE_TIME_RE` 的先例）。它是「用硬編碼詞表做感知」的**第三個死者**。
snapshot 的 web-todo 拒絕判準從「待辦文字裡有沒有『網路』二字」換成「route 欄位是什麼」。
⚠ 閘門⑧p 的六個措辭**留著**：它守的是「守衛不可以用**待辦文字**當前置條件」，與詞表存不存在無關。

新增斷言 ⑧s~⑧w（replan 帶 route／舊格式相容／snapshot 依 route 拒絕＋誤報對照／route 是 key 維度）。

#### 閘門與變異

四支全 PASS（`verify_answer_validators` 216、`verify_web_gate_isolation` 162）。
**6/6 變異全抓到**（退回 KB／不記缺口／缺口恆真／replan 不帶 route／snapshot 一律放行／key 不含 route）。

### 路由改動第 4 步：after 臂跑完，兩軸都改善、**陰性對照也變好**

23 題／0 錯誤，與 before 臂**唯一的差別就是碼**（同 collection／as-of／題目／web fixture）。

| kind | before | after |
|---|---|---|
| flow（16） | `web_grounded` 14／**`kb_only` 2** | `web_grounded` **16**／`kb_only` **0** |
| record（7，陰性對照） | `kb_only` 6／**`web_grounded` 1** | `kb_only` **7**／`web_grounded` **0** |

第二軸兩個成因（`no_route` news-09、`web_ignored` news-02）**都清空**。
**子問題層的路由零誤判**：flow 24/24 → `web`，record 15/15 → `kb`。
`mix-01`（before 臂唯一那筆過度路由、題庫裡標著「把『最新』一律當 flow 就會現形」的 canary）
判成 `kb`，過度路由消失。

⚠ **`news-02` 被修好了，而我事前明說 route 修不到它。** 它的病是 `web_ignored`（打了 3 次 web
卻零 web 引用），我判定那是 Generator 側。實際機制是**排他路由把財報 chunk 整個拿掉**
（`k=2 → 0`），Generator 沒有別的東西可引用。**預測錯了，方向是好的**——記下來是因為
下次不該用同一個理由排除某個修法。

#### 兩個殘留的洞（都是「route 還沒走到那裡」，已登錄 BACKLOG）

after 臂仍有 **7/16 題 flow 的答案引用了財報 chunk**，成因剛好二分：

· **5 題來自 replan 造的 todo**（`news-03`／`05`／`06`／`09`／`14`）——那些 todo 還沒有 `route`，
  落到刻意的保守預設 `kb`（閘門 ⑮g7 釘著）。這是計畫第 5 步。
· **2 題來自 web 預算降級**（`news-10`／`13`，兩題都沒有 replan todo）——`QUERY_WEB_BUDGET=3`
  用完時我的碼把 route 降成 `kb`，於是又去撈財報。**那正好是這次改動要治的病本身。**
  這一輪沒有翻掉任何判定（兩題仍是 `web_grounded`），所以是潛在缺陷不是已實現的損害。

#### 判讀護欄

· **兩臂各只跑一輪**，而 `gpt-oss-120b` 是 MoE、temp=0 不固定專家路由 → 方向清楚，
  但「穩不穩」要第二輪才說得上（尤其 `mix-01` 這種邊界題的分類）。
· **時間不可比**：after 1h10m vs before 1h32m，但 after 共用了 before 錄好的 web fixture，
  命中就不用再連網。這個差值是混雜的，不要當成效能改善。
· **`mixed` 那 21 題完全沒量**（`mi-*`／`mh-06~10`）——量尺對它們判別力弱，刻意排除。

### 路由改動第 3 步：確定性分派上線（四支閘門全 PASS、7/7 變異全抓到）

三個純函式，policy 與 mechanical 刻意分開：

| 函式 | 職責 |
|---|---|
| `_effective_route(route, freshness_mode)` | **eval 隔離**。snapshot／`ENABLE_WEB_SEARCH` 關閉 → 一律降級 `kb` |
| `_escalate_route(route, verdict)` | KB 走不下去要不要升級。**只認 `kb_unfixable`，不認「不足」** |
| `_dispatch_todo(route, query, …)` | 純執行：route 說什麼就叫什麼。非法 route **當場炸** |

**拿掉了迴圈後那段 web fallback**。它的觸發條件是 `not verdict["sufficient"]`——「答不出來就
上網」，正是 before 臂量到的過度路由來源（`mix-01`「Azure 最新一季營收成長率」是純財報題卻
打了 web 並引用）。現在改由 `route`（該不該上網）＋ `_escalate_route`（走不下去要不要升級）接手。

#### 兩個「先寫的斷言不等於對的斷言」

**① `_escalate_route` 升級目標：`both` → `web`，被既有的閘門⑤當場推翻。**
我在⑪j 寫的規格是 `kb + kb_unfixable → both`。實作後閘門⑤（「KB 補不了 → 只檢索 1 次」）
報 FAIL 實得 2——因為 `both` 會再撈一次 KB，而 `kb_unfixable` 的定義就是「KB 補不了」，
kb 那一半又已經在池子裡（`_merge_chunks` 只加不減）。**那正是 2026-08-14「21 次檢索原地打轉」
換個寫法請回來。** 改成 `web` 並把理由寫進 ⑪j 的註解。

**② eval 隔離搬家之後，沒有任何斷言守得住它——變異測試才發現。**
把 `_effective_route` 的隔離整段拿掉（snapshot 也照 route 走 ＝ **eval 直接連網**），
**兩支閘門一條都沒響**。根因是閘門① 的 `_gate()` 是生產判斷式的**抄寫不是 import**
（該處自己的 ⚠ 就寫著這件事），而它抄的是加 route 之前的條件。
補上 **⑪t~⑪y**：`_effective_route` 真值表（含「live 時不可一律 kb」的誤報對照）＋
**端到端接線**（route=web 的 todo 在 snapshot 下 web 呼叫 0 次、在 live 下必須真的打到）。
同一輪變異測試也抓出 ⑪g 只驗了 `_tavily_search` 而漏掉 `_web_query_en`（送中文 query 給 Tavily）。

#### 已知成本（刻意接受）

升級成 web 之後還會再 grade 一次，而那一次的池子與前一輪相同（web_notes 不是 chunk）→
**多燒一次 Grader 呼叫**。省掉它要在迴圈裡插 inline dispatch＋break，而這個迴圈已經為了
「省一輪」踩過兩次坑。一次 LLM 呼叫換迴圈可讀性。

#### 閘門

`verify_answer_validators` 214 → **216**（新增 ⑮g6/⑮g7：todo 的 **route 值**真的流到 executor、
沒有 route 的舊 todo 退回 `kb`）；`verify_web_gate_isolation` 146 → **152**，**四支全 PASS**。
⑮f2/⑮f3 的 AST 錨點跟著 `_dispatch_todo` 移位——那是錨點過期不是系統壞掉。

### 路由改動第 2 步：Plan 輸出 `route` 欄位 ＋ 雙格式相容解析

**`VALID_ROUTES = ("kb", "web", "both")`**、新增純函式 `_parse_plan_output`、Planner prompt
改輸出 `[{"task":…, "route":…}]`、`_node_plan` 的 todo 多 `route` 與 `depends_on` 兩格。
閘門⑪ 由 19 紅 → **8 綠 11 紅**（剩下的是 `_dispatch_todo`／`_escalate_route`，第 3 步）。

**Planner prompt 是壓縮不是新增**。原本三段規則（【不要注入來源類型】、【量化財務題同樣不得
注入新聞】、「⚠ 不再有例外」）存在的唯一理由，就是「沒有 route 欄位，只好叫 Plan 別在**文字裡**
暗示來源」。壓成一句「子問題文字保持來源中性，來源由 route 決定」＋ 一段 route 判準。
⚠ **計畫寫的是「刪三段」，實作時改成壓縮**：來源中性這條規則仍然有用（`route=both` 的
子問題，KB 那一半仍然會被「…的新聞內容是什麼」污染），刪掉的是那三段的長篇論證。

**兩個預設值刻意不同，這是這一步最容易寫錯的一格**：

| 情況 | 預設 | 它回答的是哪個問題 |
|---|---|---|
| 舊格式 `list[str]` | **`kb`** | **可重現性**。既有 fixture 是在「KB 先撈、web 當 fallback」的世界錄的；落到 web/both 會讓每一份既有重放**憑空多打網路** ＝ 基準不再可比 |
| 新格式缺 route／非法值 | **`both`** | **代價不對稱**，判準沿用 `realtime_need` 的「判不出來填 days 不填 none」：誤判成 kb 會讓新聞題**拿財報冒充新聞且零揭露**（而那個失敗看不見），誤判成 both 只是多打一次網路 |

原則是**解析寬鬆、分派嚴格**：非法值在解析時就正規化掉，不原樣傳下去——`_dispatch_todo`
對非法 route 是當場炸的（⑪f），一次 LLM 亂填不該毀掉整個 query。

**驗收（兩半都過）**：
· 閘門⑪a、⑪m~⑪s 轉綠；其餘三支閘門 PASS（`verify_answer_validators` 214/214）。
· **既有 fixture 仍能重放**：拿 `eval/web_replay_llm_news37.json` 的 **40 筆 plan 快取**逐筆跑
  生產的 `_plan_subqueries`，**40/40 任務文字逐字相同、route 全部落到 `kb`**，零新 Tavily key。

⚠ **測這件事時自己踩了一次坑**：第一版測試腳本讀錯快取結構，把 `"plan"` 當成 query 送進去
打了真 LLM，**並把一筆垃圾寫進版控中的 fixture**（`llm_replay` 的 `atexit` 無條件回寫，見
BACKLOG〈沒有唯讀模式〉）。已 `git checkout` 還原。之後改用 BACKLOG 建議的做法：
**cache 複製到 scratchpad 再測 ＋ 開 `RAG_REPLAY_MODE=strict:plan`**，讓 miss 當場炸而不是靜默打 LLM。

### 路由改動的斷言先寫好了（閘門⑪ 19 項刻意全紅、閘門⑯ 6 項今天就綠）

計畫的第 1 步：**先寫能證偽它的確定性測試，再改碼**。

**閘門⑪（`verify_web_gate_isolation.py`，19 項，全紅是對的）**——`_dispatch_todo`／
`_escalate_route`／`_parse_plan_output` 都還不存在。斷言先寫是為了讓「改完算不算對」
在動工前就定義好。判別力集中在三條誤報對照：

· **⑪f** 非法 route 必須**當場炸**——靜默預設成 kb 的話，Plan 打錯一個字就會讓新聞題全退回
  「拿財報硬答」，而那個失敗的外觀與「路由判成 kb」**完全相同**。
· **⑪i** `kb` ＋ 不足但**非** `kb_unfixable` → **仍然 kb**。少了這條，一個「不足就升級上網」的
  實作會全綠——而那正是現在這條路（`not sufficient` 就打 web）＝改了個寂寞，還會加重
  record 那半的過度路由（`mix-01` 已經是陽性）。
· **⑪n** 舊格式 `list[str]` 的 route 預設必須是 **kb**。既有 fixture 錄的 plan 值全是字串陣列，
  預設成 web/both 會讓所有既有重放的行為悄悄改變＝跨日基準不再可比。

**閘門⑯（`verify_answer_validators.py`，6 項，今天就是綠的）**——`rnd 0 的 query 與子問題逐字
相同 ＋ 歸因只在 rnd 0`。它是**重構期間的回歸護欄**：⑮f/⑮g 判斷「該不該說『所詢問財年』」的
**前提**就是這一格，而下一步要動的正是 `_run_executor_deterministic` 的迴圈頭。

⚠ **刻意是行為測試不是 AST**：AST 看得到「有沒有寫 `active_query = task`」，看不到「第一次
真的送出去的是哪個字串」（同 ⑮b→⑮e 的教訓）。**4/4 變異全抓到**，且各由預期的那條斷言抓到。

⚠ 寫這支時踩到 CLAUDE.md 說的**「量尺自備輸入」**：樁 chunk 少了 `raw_rerank_score`，
`_merge_chunks` 拋的 KeyError 被 `_run_executor_deterministic` 的 `except Exception` **吞掉** →
迴圈只跑一輪走降級路徑，量到的會是降級行為不是受測行為。已在樁上註明。

順手：`verify_answer_validators.py` 加 `sys.stdout.reconfigure` —— `gate8` 印的 `↔` 在 Windows
預設 cp950 下會讓整支閘門當場炸（既有問題，與本次改動無關）。

### news 路由的 before 臂基準（23 題，生產 collection ＋ live）

`experiments/newsroute_before.json`（collection=`us_stock_rag_edgar_multiyear`、as-of 2026-09-01、
live、23 題／0 錯誤）。**現行生產行為＝KB 無條件先撈、web 是 Grader 判不足後的 fallback。**

| kind | 結果 |
|---|---|
| flow（16） | `web_grounded` 14／**`kb_only` 2**（news-02、news-09） |
| record（7） | `kb_only` 6／**`web_grounded` 1**（mix-01，過度路由） |

**第二軸把兩題危險分成兩種病**（修法不同，不可合併）：
`no_route`＝news-09（web 一次沒打，Grader 看著財報判 sufficient）；
`web_ignored`＝news-02（打了 3 次 web 卻一個 web 都沒引用 → Generator 側）。

⚠ **陰性對照當場響了一格，而且是預測到的那一題**：`mix-01`「Microsoft Azure **最新一季**的
營收成長率」在題庫裡就標著「刻意選它——如果路由把『最新』一律當 flow，這一題會現形」。
它現在就已經打了 web 並引用。新的 `route` 由 Plan 讀同一段文字判，**陷阱原封不動搬過去** →
驗收判準：record 那半的 `web_grounded` **必須 ≤ 1**。

順手修掉 `record_web_fixture.py` 的兩個缺陷（**一次真實付費長跑被它們整批丟掉才發現**）：
① 輸出只在跑完才落盤 → 改成**逐題落盤**，中斷最多丟一題；
② web 內容含 ` `，Windows cp950 在**進度列印**上 UnicodeEncodeError——炸點在答案算完之後，
   等於 LLM 與 Tavily 額度全燒完才把結果丟掉。加 `sys.stdout.reconfigure`，且必須早於
   `import agentic_rag_v2`（它會包住 stdout 並保存 `_real` 參考，reconfigure 是就地改）。

### 計畫（未動工）：路由改成 todo 上的 `route` 欄位

逐檔逐函式、五條確定性斷言、驗收順序與「明確不做的」見 [`BACKLOG.md`](BACKLOG.md)。
最重要的取捨記在那裡：**責任拆開但迴圈留在同一個 LangGraph node**——拆成兩個 node 會讓
`_node_execute` 的 ThreadPoolExecutor wave 失效、多子問題序列化。

### 訂正：`probe_replan_contribution.py` 的 `n_new_cited` 在流資訊題上會把傷害讀成貢獻

該檔 docstring 原本寫「這一格才是『有沒有用』」。**在 flow 題上不成立**（理由同上），
已加註：判讀 `web-04` 這種題目時一律要配 `check_news_routing.py` 一起看。
`web-01`／`web-02`／`web-03` 與對照組是財報題，這一格在它們身上的原意仍然成立。

### `eval/.gitignore` 白名單漏掉新輸入檔——**第五次**

`news_routing_questions.json` 的 `kind` 是人工判的，而判錯的代價不對稱：
把 flow 誤標成 record 會讓「拿財報冒充新聞」被記成正確行為（`col-08` 就差點放錯——
它的 KB 側有 2026-06-12 快照市值，正確行為是**並陳**而不是擋 web）。

## 2026-08-29

### web 結果的日期：`published_date` 全是空的，而日期就寫在內容裡

**一次真 Tavily 呼叫問出來的**（2026-09-01，`What is NVIDIA's current share price?`，
12 則結果）。原本的假設是「web 撈不到即時報價」，**查下去不成立**——第一則就是：

    NVIDIA Corp.NVDA (U.S.: Nasdaq) REAL TIME 11:49 AM EDT 08/31/26 $219.8169USD 2.2669 1.04%

即時報價、帶時間戳、排第一、完全在截斷範圍內。**真正的病灶是日期**：

· **12/12 的 `published_date` 都是 `None`**（`_web_result_date` 的 docstring 早就記著
  只有 `topic="news"` 會回，而 news 模式拿不到數據頁），網址是 `/quote/NVDA` 也推不出日期。
· 於是每一則都印「（未標示日期）」，而內容裡寫著 `At close: August 31 at 4:00:01 PM EDT`。
· 後果是連鎖的：`_dedupe_web_results` 第③道過時過濾**整個失效**（抽不出日期一律保留），
  同一個池子裡 08/18、08/28、08/31 三個日期的價格**沒有任何東西替它們排序**。

**修法**：`_web_result_date` 加第三段 `_content_published_date`——`published_date` → 網址 →
**內容裡標記相鄰的日期**。四條紀律，每一條都對應一個誤報對照：
1. **只認標記相鄰**（`at close`／`after hours`／`real time`／`published`／`updated`／`as of`…）。
   同一頁裡有分析師評等日、歷史表格列，那些都不是發布日。
2. **未來日期一律丟棄**。yahoo 那則含 `Nov 17, 2026`（財報日）與 `Sep 10, 2026`（除息日）。
3. **省略年份**（`At close: August 31`）補「不晚於 as-of 的最近一次」，**絕不外推到未來**。
4. **在第一個表格／區段邊界（`|` `[` `]` `#`）截斷**：`Pre-Market:` 後面沒有日期時，
   不可以收編隔壁表格的日期——若那個誤收的比較**新**就是危險方向。
不合格 → `None`（與加這層之前逐字相同的行為）。

**閘門 ⑩（15 項，112 → 127 全 PASS）**，7/7 變異全被抓到。測資是那次真呼叫的**逐字內容**。
⚠ **危險方向不對稱**：抽到太新＝過期頁冒充新鮮並替整池背書；抽不到只是維持現狀。
   所以八條誤報對照比七條陽性斷言重要。
⚠ **`_MARKER_WINDOW` 的數值沒有證據支持**：變異測試裡 24 與 48 都全綠，⑩o 測的是
   「窗口存在」不是那個數字。有了邊界截斷之後窗口不是承重的。
⚠ 過程中換過一次設計：窗口先設 24，但那會把 `| Aug 25, 2026` 截成 `Aug 2` 而**合成出一個
   不存在的日期**——截半個 token 比截掉整個 token 危險。改成 48 ＋ 邊界截斷。

**這一步是「先 B 後 A」的 B**：修的是**所有** web 結果的日期盲點，新聞題一起受惠。
A（報價／財報數字改用專用 tool 直接回 JSON）是下一步——它解的是另外兩個問題：
訊噪比（marketwatch 那 1200 字是 Dow／S&P／VIX／黃金／原油／競爭對手市值，價格夾在中間）
與同池多價格無法排序。⚠ **A 不能取代 B**：新聞題的 `published_date` 一樣是 None。

### Replanner 的浪費：量出來、修錯兩次、第三次才對

**量**（新增 [`eval/probe_replan_contribution.py`](eval/probe_replan_contribution.py)，6 題×2 輪，
web 走計數樁）。`attributable` 讓「哪些待辦是機器造的」第一次可查詢，讀數分得非常開：

| 題型 | Replanner 待辦 | 獨有且被引用 | 秒 |
|---|---|---|---|
| intraday（web-01/02） | **12** | **0** | 948 |
| 新聞（web-04） | 4 | **6**（比 planner 自己的 4 多） | 761 |
| 財報 | **0**（沒出手） | — | — |

→ 浪費**完全集中在 intraday**，而那正是 executor 已經算出 `kb_unfixable` 也已經打過 web 的情況。
實際生出的待辦是在**列舉網站**。所以不是關掉 replan，是把它該知道的告訴它。

**修法演進（兩次失敗都由這支探針當場抓到，記在這裡因為兩個都是通用教訓）**

· **v1（prompt）**：`_REPLANNER_LIVE_BLOCK` 寫「搜過就 `sufficient: true` 收斂」。
  → **12 輪 replan 全不出手，連新聞題那 4 個有貢獻的一起殺掉**（貢獻 6 → 0）。
  **通用教訓：通用收斂指令不是窄修法。** 判準必須窄到造成浪費的那個條件本身。

· **v2（確定性判準 ＋ `_is_web_todo(task)` 前置）**：改用
  `_web_retry_is_pointless()`＝「有待辦 `realtime_need == intraday` 且 `web_used`」。
  → web-02 照樣生出 6 個待辦、503 秒、答案引用 0——因為 `_WEB_TODO_RE`
  （`網路|上網|web search|internet`）**匹配不到**「在 Yahoo Finance 上查詢」「使用 NASDAQ 官方網站」。
  **通用教訓：在呼叫端引用一個詞表，就繼承了那個詞表的洞**（CLAUDE.md〈硬編碼詞表是警訊〉）。
  詞表本身**刻意沒修**，理由與復活條件見 [`BACKLOG.md`](BACKLOG.md)。

· **v3（判準不看待辦文字）**：`intraday` ＋ 已搜過 web → 追加**任何**待辦一律拒絕。
  依據是 before ＋ v2 兩臂合計 **18 個 intraday replan 待辦、獨有且被引用 ＝ 0**。

**四臂對照**（判準：intraday 要掉到 0，**而新聞的貢獻不能跟著掉**）

| 臂 | intraday replan／貢獻／秒 | 新聞 replan／貢獻／秒 | 總秒 | 總引用 |
|---|---|---|---|---|
| before | 12 / 0 / 948 | 4 / **6** / 761 | 2158 | 26 |
| v1 prompt | 0 / 0 / 252 | **0 / 0** / 198 ✗ | 1068 | 20 |
| v2 詞表 | **6** / 0 / 705 ✗ | 0 / 0 / 210 | 1413 | 20 |
| **v3** | **0 / 0 / 296** ✓ | 5 / **5** / 676 ✓ | 1447 | **27** |

**閘門 ⑧h~⑧r（11 項，101 → 112 全 PASS）**，其中六項是誤報對照：
`days`（新聞）不可被擋／第一次搜不可被擋／`none`（財報）不可被擋／新聞的 web 待辦仍加得進去／
intraday 但沒搜過仍加得進去／**⑧p 把 v2 漏掉的六個站名措辭逐字凍結**。
另有 ⑧i/⑧j：**snapshot 的 replanner prompt 逐字不變**（65 題基準沒被動到的證明）＋ 其誤報對照。

⚠ **判讀限制**：n＝每題 2 輪。兩臂 replan 都沒出手的題（web-03／web-05／mh-03）引用數照樣
   ±2 在動 → **總引用 26 vs 27 在噪音裡**，能說的只有「沒掉」不是「變好」。
⚠ 探針的 web 是樁且**每個 query 回同一段內容**，所以它量的是 **KB chunk 的貢獻，不是 web 的**。
   「web 重試沒有用」這件事**這支證明不了**——只證明了「KB 側沒有新東西」。

### live 量尺三週的靜默失效：查清成因、修掉兩個、第三個要重錄 fixture

先前把「同一題五輪 PASS/FAIL 亂跳」歸因為「fixture key 綁死 LLM 生成的 query 字面」。
**那只是最後一環。** 逐層查下去是三個獨立成因：

1. **fixture 錄在「KB 還有新聞」的時代（2026-08-15），而新聞於 2026-08-19 拔除。**
   快取裡的 `check` key 逐字引用 `TSLA_News_20260721_01.txt` 等 chunk——**現在任何 collection
   裡都不存在**。這一項**修不了，只能重錄**（見 [`BACKLOG.md`](BACKLOG.md)）。
2. **`note_meta()` 每次 replay 都覆寫 `_meta` 的環境綁定** → 那一格記的是「上次誰跑過」。
   於是 2026-08-27 生產換 collection 之後，fixture 每跑一次就自動宣稱綁在新的上，
   **不一致自己抹掉自己**，三週沒有任何跡象。→ 改成只在 record 模式寫。
3. **`replan` 是全碼庫唯一沒進重放快取的 LLM 呼叫。** 它每輪重抽 → 新 todo 措辭 → 新 `check`
   key → 新 `new_query` → 新英譯 → 新 Tavily key → `FixtureMiss`。→ 加上快取。

**改了什麼**
· `_node_replan` 走 `_replay.get/put("replan", ...)`；`_KNOWN_KINDS` 加 `replan`。
  key ＝ `freshness_mode | query | [id:status:task]`，**刻意不含** ① system prompt（內嵌隨
  collection 變動的 coverage，同 `plan` 的理由）② 各待辦的 `result` 自由文字（每輪都不同，
  納入等於快取永不命中）。⚠ 代價：同一份待辦清單、不同局部結果會共用決策——這是 **fixture
  用的重放不是通用函式快取**。`status` 有入 key，所以「做完了沒」仍分得開。
· `web_replay.note_meta()` 只在 record 模式寫。
· `record_web_fixture.py --mode replay` **預設 `RAG_REPLAY_MODE=strict:plan,replan,translate_en,check`**
  （這四個恰好構成決定 Tavily query 的那條鏈；另外三個接點比舊 fixture 新，全開會假陽性），
  並在開跑前比對 fixture 綁的 `collection` 與 `as_of`，不合就 exit=2。`--lenient-replay` 放寬。

**閘門 ⑧（7 項）＋ ⑨（4 項），90 → 101 全 PASS**；⑧ 的 **5/5 變異全被抓到**
（M1 未註冊／M2 key 改常數／M3 result 納入 key／M4 拿掉 put／M5 key 拿掉 status）。
兩支的判別力都在誤報對照：⑧d~⑧g 是四個「應該要 miss」的維度，⑨b 擋的是「連 record 都不寫」。

⚠ **CLAUDE.md 的閘門數先前漂移**：寫 88，實測基準是 90。已改成 101。

⚠ **變異測試腳本自己踩了兩個坑，記在這裡因為下次還會踩**：
① `Path.write_text()` 在 Windows 把 LF 換成 CRLF → 整個 `agentic_rag_v2.py` 變成 7904 行 diff。
   一律 `open(..., newline='')`。
② 逾時被砍時 `finally` 不會跑 → 一個變異殘留在檔案裡（`_replay.put` 被換成 `pass`）。
   改用 `atexit` 全域還原表，並在跑完後用 `git diff --stat` 確認。

### 補上 multi_hop「哪一家最高」的斷言（新檔），再一次推翻自己的假設

先前主張「比大小沒有 Python 在做 ＝ multi_hop 最大的暴露面」。查證屬實（全碼庫零比較器，
而比錯了 Synthesize 五道 validator 一道都不會響），**但量下去沒有損害**。

新增 [`eval/check_comparison_claims.py`](eval/check_comparison_claims.py) ＋ `comparison_claims.json`
（零 LLM、零網路、零 Qdrant，只讀既有結果檔 → **成本幾乎為零**）。三態不可合併：
`wrong_winner`（值都在眼前還挑錯）／`unfounded`（某家的值不在 contexts 卻仍宣告贏家）／`N/A`。
真值刻意**從 record 自己的 `contexts` 算**，不重跑檢索也不查 gold——否則 FAIL 會混進「檢索沒撈到」。

**讀數**：現行架構 8 輪 × 5 題 **40/40 PASS**；擴到全部 67 個結果檔（335 筆）**`wrong_winner` 仍是 0**。
`unfounded` 6 筆全部出自舊 ReAct 世代的 `gj_v2_multihop*`，其中兩筆是真的答錯
（宣稱 MSFT／真值 AMZN、宣稱 NVDA／真值 META，且四家的值一個都不在 contexts 裡）
——與 2026-07-29 ReAct 執行層退役的理由一致。

**⚠ 量尺自己先出了兩次錯，記在這裡因為它們是判讀前提**：
1. **第一版在 40 筆裡誤報 2 筆 FAIL**，兩筆答案其實都是對的。成因是「離錨點最近的公司提及」
   這條規則遇到兩個真實句型會失手：① 題目重述清單緊接最高級（「是 A、B、C、D 四家公司中規模最大的」
   → 挑到清單最後一個）② 及物比較動詞（「NVIDIA 領先於 Meta 與 Alphabet」→ 錨點後面接的是**輸家**）。
   → 依 CLAUDE.md〈稽核回報 FAIL，先問是不是量尺錯〉逐字看了原文才發現。
   兩個句型已**逐字凍結成 selftest ⑧⑨**，並各配一個誤報對照（⑪ 同句型但宣稱換錯家 → 仍須 FAIL）。
2. **列舉偵測寫成 `body[p1+1:p2]`**（只跳過一個字元而不是整個公司名）→ 那道規則**從頭到尾沒生效**，
   是 selftest ⑧ 把它逼出來的。`_company_positions` 因此改回傳 `(起, 迄, ticker)`。
   → 這正是「機制宣稱要先有能證偽它的確定性測試」：沒有 ⑧，這條規則會以「已實作」的姿態躺著不作用。

**結論**：不補 Python 比較器。**但適用範圍很窄**——現有語料每一題差距都很大、單位一致、值全都在，
**接近值／缺值／跨單位一次都沒測到**。復活條件記在 [`BACKLOG.md`](BACKLOG.md)。

⚠ 附帶事實（另一個方向的證據）：`_PLANNER_PROMPT` **完全沒有提到依賴型第二跳**，
而 10 次實跑的拆解裡帶「該公司」的子問題出現 **0 次**——Planner 一律把 multi_hop **攤平成笛卡兒積**。
於是 `_is_dependent_hop`／`_resolve_hop_entity`／`_fill_dependent_hop`／replan 的 `has_pending_dependent`
這一整套在這批題目上是**死碼**。⚠ `_resolve_hop_entity` 用**眾數**挑公司，而問題問的是「最大的那家」
——四家各出現一次時眾數是任意的。目前不會被觸發，但改 Planner prompt 就會上線。

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

### 補上 `relevant_ids` 的量尺（新檔），順便推翻自己的假設

`relevant_ids` 決定「這個子問題把哪些 chunk 交給 Generator」（`_run_one_todo`：有圈選就只收
圈選的，沒圈選才退回 rerank top-k 全收）。它比 `new_query` 更靠近答案，而在此之前
**完全沒有量尺**——`grep relevant_ids eval/*.py` 的每一筆命中都是測試樁裡的 `"relevant_ids": []`。

新增 [`eval/probe_relevant_ids.py`](eval/probe_relevant_ids.py)。四個指標不可合併：
`gold 全滅`（危險）／合成混池的 `誤選他家`（陰性對照）／`圈選率`（**1.00 ＝ 等於沒過濾**）／`空圈選`。

**第一版有兩個洞，都在自己身上，記在這裡因為它們是判讀前提**：
1. **公司層的陰性對照在自然候選池裡不存在**：ticker hard filter 在上游就把沒點名的公司濾掉了，
   八題的「未點名公司 chunk 數」全是 **0** ＝ 那條斷言零判別力。
   → 這本身是關於 `relevant_ids` 的發現：**它在「排除別家公司」上的邊際價值接近零**，
     真正的工作是同一家公司內的主題相關性。陰性對照改用**合成**混池（兩半都走生產路徑）。
2. **「gold 檔的部分 chunk 沒被圈選」不是缺陷**：gold 只到檔名，排除同檔裡離題的 chunk 正是
   這個欄位該做的事。第一版把它當危險指標，量出「4 題危險」——**那是量尺的錯不是系統的錯**。
   改成只留 `gold 全滅`（進了候選卻一個都沒被圈選）這個不含糊的形狀。

**修正後的讀數（8 題 × 3 輪）**：`gold 全滅` **0 題**（6 題可量、2 題 N/A）、`誤選他家` **0 題**
（sem-01 混入 1 筆、mix-01 混入 2 筆，陰性對照確實有東西可選）、圈選率 1.00 的題 **0/8**、
平均圈選率 **0.53**、空圈選 **0/24**。
→ **`relevant_ids` 確實在工作，且在檔名層量不到損害。** 先前「它是最該先拆的耦合」這個假設
**沒有證據支持**，據此撤回。

**但接起了兩條原本分開的 BACKLOG**：`mix-01`／`mix-03` 的圈選率都是 0.33（5 取 ~1.65），
而 agentic 在 lexical 只承接 1.93~2.06 chunk——**低承接數很可能就是這個欄位造成的**。
而「那有沒有害」需要 chunk 層 gold 才分得出來，那正是〈量尺缺口〉裡已登錄的那條。
**先有 chunk 層 gold，才談得上要不要動 `relevant_ids`。**

### `realtime_need` 對「近期動態」措辭的誤判（prompt 修法）

`probe_realtime_need.py` 量到的危險錯誤：「NVIDIA 最近有什麼新進展？」**0/3** 判成 `none`
（穩定地錯；這題刻意不含「新聞／消息」字樣＝措辭泛化的形狀）、「蘋果最近有什麼消息？」**1/3**。
判成 `none` 的代價是 `_stale_for_realtime` 第一行就 return → 不叫 web → **拿 10-K 回答
「最近有什麼消息」且零揭露**。

**病灶不在架構在 prompt**：`_CHECKER_LIVE_RECENCY_BLOCK` 從頭到尾沒有一句告訴模型
「候選片段全是 10-K／10-Q **不構成**填 `none` 的理由」，而它前面剛讀完一整段「KB 天花板到了
就判 sufficient=true」。加三條規則（**只動 live 專屬區塊**，snapshot 的 Checker prompt 逐字不變，
由 `verify_web_gate_isolation.py` 的 byte-identical 斷言把關）：

1. 這個欄位只看子問題在問什麼，**與候選片段裡有什麼無關**。
2. ⚠ 例外，防判過頭：「最新一季」「上一季」指**財報期別**不是 wall clock → 仍填 `none`。
3. 判不出來填 `days` 不填 `none`（填錯 `days` 多搜一次；填錯 `none` 是拿舊資料冒充現況）。

**結果（`--repeat 5`）**：兩題實時題 **0/3 → 5/5**、**1/3 → 5/5**；intraday 兩題 5/5；
**四題陰性對照一格不動（全 `none`、0/5 叫 web）**；跨輪不穩定從 1 題 → 0 題。
陰性對照不動這一格是關鍵——沒有它，「一律回 days」也會拿滿分。

⚠ **這不等於那條 BACKLOG 關閉了**。改的是分類器在**一個措辭族**上的準確度，不是「KB 補不了
就去 web」由單一 LLM 欄位守著這件事。換一種沒測過的措辭仍可能失手。
⚠ 這支量的是 Grader **單獨**的分類（固定池上呼叫 `_check_sufficiency`），**不是端到端**。

### 歸因改掛在 todo 的出身上（堵住 col-10 的第二扇門）

⑮f 用 `attributable=(rnd == 0)` 判歸因，那**只擋得住「Grader 在同一個子問題內改寫」**。
Replanner 另外加一個 todo 時，那個 todo 的 rnd 0 照樣是「第一輪」→ 條件為真 → 機器腦補的年份
又會被說成「所詢問財年」，而 ⑮f **全綠**（它驗的條件確實還在）。

實測 replanner 就是會生出這種待辦串：`'改用網路搜尋查…'` → `'即時網路搜尋…'` →
`'使用即時金融網站…'`（見 `agentic_rag_v2.py` 的 `QUERY_WEB_BUDGET` 上方註解），本次 web-02／
web-04 的 trace 也各出現兩個。

**修法**：歸因掛在 todo 的**出身**，不從輪次推。
`_node_plan` 建的 todo → `attributable: True`（Planner 的分解是使用者意圖的重述）；
`_node_replan` 建的 → `attributable: False`（機器對 Grader `missing` 的反應）。
`_run_one_todo` 用 **`todo["attributable"]` 下標讀**（不是 `.get(..., True)`——給預設＝新的建立點
會靜默沿用可歸因）。三個 executor 入口的 `attributable` 一律必填 keyword-only。
補救迴圈的條件變成 `attributable and rnd == 0`：**兩個都要成立**。

**閘門 ⑮g（10 項，198 → 208 全 PASS）**，5/5 變異全被抓到：replan 標 True→g2；
`.get(..., True)`→g4；executor 給預設值→g3；迴圈退回只看輪次→g6；plan 標 False（把功能關掉
冒充修好）→g1。⑮g6 的兩個方向缺一不可。

⚠ 必填參數第二次逼出自造替身：`verify_web_gate_isolation.py` 三處、`verify_answer_validators.py`
一處的 `_run_executor_deterministic(...)` 呼叫都要補參數。**這是要的效果。**
⚠ 補這些呼叫點時踩到一個 anchor 陷阱：8 空格縮排的那一行是 12 空格那行的**子字串**，
`str.count`/`replace` 會一次吃掉兩處。多行替換一律連前一行一起當錨點。

### replay 的 miss 改成「大聲炸」：把契約換成型別

承上一條的體檢。`web-02` 的 FAIL 之所以無法歸因，是因為三件事疊在一起：`_tavily_search` 對
`FixtureMiss` **故意 re-raise**（正確）→ 上一層 `_run_executor_deterministic` 的 `except Exception`
把它接成「降級」（連該子問題已建好的池一起丟）→ 結果檔不記 error。於是「fixture 沒涵蓋」與
「系統沒打 web」在外觀上**完全相同**，而量尺只看得到後者。

**修法**：`web_replay.FixtureMiss`／`RecordError`、`llm_replay.ReplayCacheMiss`（原本是裸
`RuntimeError`）一律改成繼承 **`BaseException`**。判準與 `SystemExit`／`KeyboardInterrupt` 同一條
——「這不是可以就地處理的錯誤，這是『這次測量無效，停下來』」。

**為什麼不是逐個加 re-raise**：全碼庫有 16 個 `except Exception`。契約是 O(n) 的，漏一個就破功、
而且破得靜默（這次就是漏了）。移出 `Exception` 階層是語言層面的保證，新增 catch-all 也不會破。
⚠ 通則寫進 [`docs/EVAL.md`](docs/EVAL.md) §4.5：**「絕不可被吞掉」要寫成型別，不要寫成契約。**

**閘門⑦（9 項，79 → 88 全 PASS）**，4/4 變異全抓到：`FixtureMiss` 改回 `RuntimeError`→⑦a＋⑦c；
`ReplayCacheMiss` 改回→⑦a；executor 的 catch 擴大成 `BaseException`→⑦c；`_tavily_search` 拿掉
re-raise→⑦e。**⑦d 的誤報對照不可省**：只驗「FixtureMiss 會傳播」的話，一個把整個 try/except
拿掉的實作也會通過——那等於把 executor 的容錯關掉。

**端到端驗證**：空 fixture 跑 `web-02`，從「安靜產出 `n_web_calls: 0` 的結果檔、exit=0」變成
「traceback ＋ 印出闖禍的 query ＋ exit=1」。

⚠ 寫 ⑦e 時自己踩了本檔 L48 註解記載的同一個坑：`verify_web_gate_isolation.py` 在 **import 時**
就把 `ar._tavily_search` 換成 stub（絕不連網的保證），所以要測真身一律用 `_REAL_TAVILY_SEARCH`。
那行註解寫著「2026-08-13 實際踩到」——這是第二次。

### 期間降級揭露只在「可歸因」時才逸出（修 col-10 的假前提）

上一條接線上線後，65 題裡只有 `col-10` 觸發揭露，而那一句是**假的**：使用者問「微軟每年能自由
運用的現金大概有多少？」，Planner 分解成「Microsoft 每年的自由現金流大概是多少」（**兩者都沒有
年份、沒有 10-K**），揭露句卻寫「知識庫沒有「MSFT 10-K」在**所詢問財年（2022）**的資料」。
那個 2022 只可能來自 Grader 的 targeted rewrite。

`「所詢問」`這個措辭在單管線永遠成立（query 就是使用者原句），在 agentic 不成立——送進
`rq.retrieve()` 的 query 至少被機器改寫過一次。**分界線不是輪次而是意圖歸屬**：Planner 的分解是
使用者意圖的重述（可歸因）；Grader 的 targeted rewrite 被 `_CHECKER_PROMPT` **明令**去加使用者
沒說過的具體詞（「用更具體的關鍵字 / 實體 / 財報標準術語」），ReAct 的 `rag_search` 同理。

**修法**：`_retrieve_chunks(query, *, attributable)`，**必填、無預設**（有預設＝日後新增的呼叫點會
靜默沿用可歸因）。四個呼叫點逐一表態，確定性補救迴圈傳 `attributable=(rnd == 0)`。不可歸因的 note
**丟棄而不是改措辭**——那個期間約束本來就不是使用者問的，講出來只是噪音。

**閘門 ⑮f（11 項，187 → 198 全 PASS）**，5/5 變異全被抓到，且分別由不同斷言抓到：
`attributable` 給預設值→f1；迴圈寫死 `True`→f3＋f5；拿掉閘門／反向一律丟棄→f4 的兩個方向；
漏傳一個呼叫點→f2。**f4 的誤報對照不可省**：只驗接線的話，一個「一律丟棄」的實作也會全綠。

⚠ 連帶：`verify_web_gate_isolation.py` 自造的 `_fake_retrieve(q)` 樁吃不到新參數而 TypeError，
已改成 `(q, *, attributable=True)`。**必填參數的代價就是這個**——它會逼出所有自造替身，那正是
要的效果（樁悄悄與生產簽名分家，是量尺失效的常見形狀）。

### live web 量尺的體檢：它是 flaky 的，而且原因有三層

起因是 `eval/web_claims.json` 的 `_meta.collection` 還寫著前生產 `us_stock_rag_edgar_mdna`。
本來只打算重錄 fixture，先做了一個零網路的對照（`mdna` vs `multiyear` 各重放一次）——**結論
推翻了前提**：

| | web-02 | web-04 |
|---|---|---|
| mdna / multiyear / mdna / multiyear / 單題重跑 | PASS,FAIL,FAIL,FAIL,**PASS** | FAIL,FAIL,FAIL,**PASS** |

同一份碼、同一個 collection，PASS/FAIL 會翻面。**這把量尺自稱「零噪音」，但那個性質是從斷言
形式推來的，而斷言的輸入（web 到底有沒有觸發）是 LLM 決定的。** 三層成因：

1. **fixture 的 key 含 LLM 生成的英文 query 字串。** 實測 Grader 這次吐 `NVIDIA current stock
   price August 15 2026`，fixture 裡只有 `NVDA current stock price today` 與
   `NVIDIA current stock price live quote` → `FixtureMiss`。**重錄只會換一組會再度 miss 的 key。**
2. **刻意的大聲失敗被上一層吞掉。** `_tavily_search` 對 `FixtureMiss` 是**故意 re-raise** 的
   （不讓「fixture 沒涵蓋」被靜默當成「web 不可用」），但確定性 executor 的 `except Exception`
   接走它變成「降級」，**連同該子問題已建好的池一起丟**，結果檔又不記 error → 量尺把
   「fixture 沒涵蓋」報成「系統沒打 web」。
3. **`realtime_need` 對新聞措辭真的會判錯**（這一層是真缺陷，不是量尺問題）。
   `probe_realtime_need.py --repeat 3`：「NVIDIA 最近有什麼新進展？」**0/3**（穩定地錯）、
   「蘋果最近有什麼消息？」**1/3**；intraday 兩題 3/3 全對，四題陰性對照 3/3 全對。

**順手撿到兩個工具缺陷**：① `record_web_fixture.py --mode replay` **不是唯讀**——`llm_replay` 的
`atexit` 無條件寫回、`web_replay.note_meta()` 又在 mode 檢查之前呼叫，第一次跑就改掉了兩個版控
fixture（已復原）。防護是 `RAG_REPLAY_MODE=strict`，但這支沒設。**任何共用同一份 cache 的 A/B，
第一臂的 miss 都會變成第二臂的 hit**（實測 hit 23→32）。② fixture 裡錄著一筆 Tavily query 是
`"I'm unable to browse the web, so I can't retrieve the latest Tesla news for that period."`
——模型的拒答句被當成搜尋詞送出去了。

### agentic 接回期間降級揭露（BACKLOG「agentic 沒有揭露通道」）

`_retrieve_chunks` 寫的是 `chunks, _note = rq.retrieve(...)`——Tier 2 降級時那句「知識庫沒有 X 在
所詢問財年（Y）的資料，以下回答改用最接近的可得期間（Z）」**被直接丟掉**。單管線兩個消費端都有
（CLI／SSE 顯示 ＋ 注入 generator prompt），agentic 兩半都沒有 → 「系統會不會揭露」在所有 agentic
評測上**結構性恆為 0**。這條在產品線走單管線時不影響出貨，改用 agentic 為主之後就是實害。

**接線**：`_retrieve_chunks` 收下 note → `_RunState.period_notes`（**五個呼叫點共用一個收集點**：
ReAct 工具、確定性迴圈、兩處例外降級、graph 崩潰降級，逐個回傳必有人漏接）→ executor 第四個回傳值
→ `_node_execute` 跨子問題去重保序併進 state → Synthesize 走 `rq.build_user_prompt` 的**第三參數**
注入 Generator。**顯示**那一半由 `run_agentic` 回傳 `period_notes`、CLI 印 `⚠️`——與
`run_single_query` 同一個通道。設計理由（兩個獨立通道、為何不機械式附加）見
[`docs/AGENTIC.md`](docs/AGENTIC.md) A11。

⚠ **`period_note` 與 `web_extra` 同一個理由必須跟著每一次重生成走**：validator 重生成會換掉
`extra_user`，寫在那裡的話揭露只活到第一次生成（web_extra 踩過這個坑）。所以五道 validator ＋
它們內部回頭呼叫的 citation 稽核全部要帶，共 16 個接點。

**實測（生產 collection，零 LLM 生成）**：「Microsoft 在 2021 財年的營收是多少？」→ 揭露句產生並
出現在 user prompt；陰性對照「Microsoft 最新一季的營收」→ Tier 1 命中、**無揭露**（不會變成每題
都掛一句的背景噪音）。

**量尺**：新增閘門⑮，`verify_answer_validators` 170 → **187**。**變異測試 5/5 全抓到**，其中最重要的
是 **M3「關鍵字有寫、但傳的值永遠是空的」——⑮b 全綠、只有 ⑮e 叫得出來**（⑮b 驗呼叫點寫了
`period_note=`，⑮e 驗**值**真的從 state 流到 Generator）。這是 ⑧g／⑭a 那個教訓的第三次。

⚠ **既有基準的移動範圍是可枚舉的**：⑮c 鎖住「system message 逐字不變」與「空揭露時 user prompt
與不傳這個參數逐字相同」→ **只有真的觸發 Tier 2 降級的題**答案文字會變；揭露刻意**不併進 answer
字串**，所以結果檔的答案本體格式不動。四道確定性閘門（85／187／17／79）全 PASS。

**觀察，沒有動**：`rq._build_fallback_note` 的「最接近的可得期間」會把該公司**全部**期碼列出來
（實測 11 個），讀起來很吵。那是單管線共用的既有措辭，動它會同時改變單管線的使用者可見句，
另案再議。

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
