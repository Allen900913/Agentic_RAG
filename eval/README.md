# `eval/` — 題庫、標準答案、閘門與探針

> **這裡放什麼**：eval 資料檔的格式與維護規則。
> **這裡不放什麼**：腳本清單（→ [`../CLAUDE.md`](../CLAUDE.md) 檔案地圖）、量測知識與已試無效總表（→ [`../docs/EVAL.md`](../docs/EVAL.md)）、待辦（→ [`../BACKLOG.md`](../BACKLOG.md)）。
>
> ⚠ **`eval/` 只放輸入與標準答案，跑分輸出一律寫到 `experiments/`。** 部分腳本的 `--output` 預設值仍指向 `eval/`，跑的時候要自己帶。

---

## 1. 四種驗收，職責不同

| 種類 | 例子 | 噪音 | 什麼時候用 |
|---|---|---|---|
| **確定性閘門**（`verify_*.py`） | 期別排序真值表、validator 的誤報對照 | **零**（零 LLM、秒級） | **改動前後都跑**。這是主力 |
| **逐條斷言**（`check_*.py`） | 「這題該有 18.30%、不該有 18%」 | **零**（只讀結果檔） | 「數字答錯」類改動的驗收指標 |
| **聚合指標**（RAGAS 六項） | `answer_correctness` | **大**（MDE 換算 4~7 題） | 只當副判準，且**看到數字之前先寫死判準** |
| **探針**（`probe_*.py`） | 「ratio 意圖的 LLM 分類準不準」 | 有（含 LLM、MoE 不固定路由） | **不是閘門**。`--repeat 3` 以上才下結論 |

**兩條判讀規則貫穿全部**：
- **N/A 不能併進 PASS**——它是「這一輪沒量到東西」不是通過。`check_number_defects` 的六次失效裡有兩次是這樣抓到的。
- **陰性對照不可省**——少了它，「一律回 X」的實作也會滿分。

---

## 2. 題庫（`eval_set.json`）

**65 題**：semantic 15／mixed 15／lexical 17／colloquial 13／multi_hop 5。**0 題帶 `rubric`**（rubric 這條線目前沒有活的消費端）。

⚠ **分母變動過兩次，跨 2026-08-19 的分數一律不可直接比**：100 題 → 拆出 37 題含新聞 gold 的到 `eval_set_news.json`（剩 63）→ 同日把兩題去掉新聞子句補回成 `lex-16`／`lex-17`（65）。**「FAIL 變少」有一次就是分母變動不是改善**（搬走的兩條一 PASS 一 FAIL）。

### `relevant` 的兩種寫法
精確檔名（`"NVDA_10K_2026.html"`）或 glob（`"AMZN_10Q_*.html"`，用 `fnmatch` 展開成 collection 裡實際存在的檔案）。**安全網**：所有 pattern 展開後都是空集合時該題會被跳過並警告，不會用 0/0 污染統計。

⚠ **glob 在多年語料下會失去判別力**：`MSFT_10K_*.html` 在單年只展開到一份，加入舊年度後會同時吃到多個年份 → **撈到哪一年都算命中**，量尺正好在最需要判別力的地方變鈍。所以升生產時已把 `sem-03`／`sem-04`／`col-02`／`col-03` 釘死成實際含答案的那幾份。
⚠ 反方向的代價也記著：釘死之後會**過嚴**（`MSFT_10K_2024` 提到 Copilot 35 次、2026 版只有 17 次，檢索回舊年報不必然答錯而量尺記成 miss）。**這把尺在那幾題上是低估不是高估。**

### 冷凍的 `eval_set_news.json`（37 題）
2026-08-19 KB 拔除新聞時移出。⚠ **現在不要拿它跑分**：eval 預設 `freshness_mode=snapshot`、web 是關的，跑了必然全滅。
用途是日後驗收「live web 能不能取代 KB 新聞」，量尺是 `web_claims_news37.json` 的**性質斷言**，**不是對照新聞 gold**——那批 gold 是「2026 年 6 月的新聞說了什麼」，live web 回答「現在的網路說什麼」，**是兩個不同的問題**。

---

## 3. chunk 層 gold（`chunk_gold.json` ＋ `chunk_gold.py`）

**41/65 題**＝「答案落在哪幾顆 chunk」。**gold 由 `literal` 定義、不寫死 `chunk_index`**（重建會位移索引）。消費語意是 **union ＋ all-dropped**（聯集偏大 ⇒ 偏 false-negative 方向）。

⚠ **41/65 是上限不是缺陷**：定性 semantic 題沒有可定位的數值答案，未涵蓋的 24 題是 **N/A 不是 PASS**。
⚠ `literal_matcher` 的**唯一定義點**在 `chunk_gold.py`（消費端一律 import）。它的數字邊界必須排除「數字的延續」而**不是「標點」**——寫成 `(?<![0-9,.])` 會讓任何後面緊跟逗號或句點的數字一律匹配不到，2026-09-11 三支尺同時誤報就是這個。
⚠ 改 `eval_set.json` 的 `relevant` 要跟著跑 `--selftest`；重建 collection 後跑 `--check`。

---

## 4. 標準答案（`reference_answers.json`）

RAGAS 的 ground truth，**已納入版控**（含 24 處人工校正、不是腳本能免費重現的東西）。
⚠ **`--force` 不帶 `--ids` 是全量重生成，會洗掉那 24 處人工校正**（現在要再加 `--force-all` 才跑得動）。
⚠ **預設仍是「不要重生成」**：現有 65 題是舊模型生的 ＋ 人工校正，只重生成一部分會讓 gold 變成兩代模型的混血。

**維護規則（每一條都是踩過坑換來的）**：
1. **新增數字型題目前先確認該指標會不會跨快照漂移**。會漂移的（P/E、市值、YoY、TTM margin）必須枚舉所有快照值，或直接 pin 單一日期檔案。
2. **「系統 vs gold 差 10 倍」先讀原文對一次單位**。gold 自己犯過這個錯：來源寫 `$253.49B`，reference 抄成「253.49 億美元」（正解 2,534.9 億）。⚠ 對照組證明這不是隨機——來源是 `(In millions)` 表格時換算全對，**錯的只有「B 結尾」那一種字面**。
3. **反向也要驗**：`grep` 抓不到 **≠** 語料沒有（數字常在 MD&A 敘述段）。實測有五批「false-negative gold」，reference 誤寫「來源未揭露」而語料白紙黑字有，**答對的系統反被打 recall=0**。
4. **概念型 checkpoint 的括號舉例必須 grep 有源**——舉例詞彙若語料沒有，judge 可能拿舉例當唯一判準。
5. **分析任何跑分輸出前先掃污染**：看有幾題的分數是 `None`。非零就代表這組數字被額度耗盡或逾時污染過（實測發生過一次：22 題靜默留空、**且未被排除出分母**，category summary 仍顯示滿額）。

---

## 5. 逐條斷言（`number_claims.json`）

`check_number_defects.py` 的輸入。**每一條都要人工驗證過才登錄**（`verified` 欄位寫怎麼驗的）。`status` 兩種：`known_defect`（現在會 FAIL，修好要變 PASS）／`regression_guard`（現在 PASS，防日後退步）。

**`anchored_pct` 的設計**（四種比法被實測推翻才收斂成現在這個）：不接地不行（答案裡有 4~8 個百分比、容差 ±1pt，任何值幾乎都能湊到）；接地後「挑哪一個值」也猜不出來（值在 anchor 前或後都是合法中文寫法）。**現行做法是問集合成員關係**：anchor ±N 字內的百分比集合，正解在不在／禁止值在不在。

⚠ **判別力由 `forbid_pct` 承擔，不是 `expect_pct`**（只問「正解在附近嗎」時，答案把正解與干擾值並陳也會 PASS）⇒ `known_defect` 必須填 forbid。
⚠ **forbid 只在「錯值不會與正解正當並存」時可用**（`col-11` 的 Azure 40% 與 Microsoft Cloud 29% 正當並存）。
⚠ **anchor 本質上是詞表**，只能用在**已人工驗證過的單一主張**上。放寬 anchor 的判準是「同一個主張的其他合法中文寫法」，**不是「把更多數字納進窗口」**——後者會讓 `expect` 變成幾乎必中。
⚠ **一個 id 可以掛多條主張**：`lex-17` 掛 `require_chunk` ＋ `anchored_pct`，因為**進池 ≠ 有用它**。
⚠ **兩支腳本的期望不可相反**：`mix-03` 的 `expect_text` 曾只認捨位後的值，而 `check_rounding_fidelity` 正把捨位當缺陷在量 ⇒ 答案寫精確值反而被判 FAIL。**同一件事在兩支腳本裡有相反的期望，比任何一邊判錯都糟。**

---

## 6. fixture（`replay_cache.json`／`web_fixture*.json`）

跟 `reference_answers.json` 是同一種東西：**不需要唯一正確，只需要固定且對所有組態一視同仁**。定版後別再重生成。

- `replay_cache.json` — plan／replan／英譯／Grader／ratio／期間意圖／ticker 七種 LLM 中間產物。A/B 兩臂共用它，差異才只剩被測的那一項（**共用時必開 `RAG_REPLAY_READONLY=1`**）。
- `web_fixture.json`（5 題）／`web_fixture_news37.json`（37 題 ＋ 3 題陰性對照） — Tavily 的**原始回應**。
  ⚠ 錄製必須同時開 `RAG_REPLAY_CACHE`：送給 Tavily 的 query 是 LLM 決定的，只錄 web 會在重放時 miss。
  ⚠ **重錄前一定要清空 replay cache**：舊快取的 plan 若是加 `route` 之前的純字串陣列，會全部落到 `kb` → **一次 web 都不打，錄出一份空 fixture**。

---

## 7. 已退役的舊 harness（留著但沒在用）

`eval_retrieval.py`／`eval_rerank.py`／`eval_chunk_recall.py`／`eval_two_stage.py`／`ablate_translate_rerank.py`／`classify_answer_shape.py`／`diagnose_crit_miss.py`／`latency_benchmark.py`／`rejudge_with_model.py`／`chunk_size_stats.py`／`eval_agentic.py`。

它們屬於更早的世代（21 題 source-level eval、Dense/Sparse/Hybrid 三模式對照、`data_update.py` 那條 ingest 線），**沒有跟上現行的 65 題題庫與 collection**。當年的結論已經進了設計（hybrid 通吃、cap=8 的 multi-variant 合併最好），記載在 [`../README.md`](../README.md)〈Design Decisions〉與 [`../docs/EVAL.md`](../docs/EVAL.md)。
⚠ `eval_agentic.py` 是**確定的死碼**（import 的 module 已不存在），見 [`../BACKLOG.md`](../BACKLOG.md)。
