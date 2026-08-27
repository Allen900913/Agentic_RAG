# BACKLOG — 未做 / 待決 / 已接受的極限

> **這裡放什麼**：還沒做的事、做了但沒定案的事、以及查清楚後決定**不修**的極限。
> **這裡不放什麼**：已完成的改動（→ `CHANGELOG*.md`）、知識與踩坑（→ `docs/`）。
>
> **維護紀律**：一件事做完就從這裡**刪掉**並寫進 `CHANGELOG*.md`；查清楚決定不做就搬到最後一節「已接受的極限」並寫下**復活條件**。
> 每一條都要能回答「**卡在哪**」——沒有卡點的條目不是 backlog，是願望。

---

## 立即可做（不需重建、不燒額度）

- ~~**live 路徑會出貨指向不存在來源的引用**（2026-08-20）~~ → **已修**，見 CHANGELOG 同日與 `verify_answer_validators.py` 閘門⑨（12 項、三向變異測試過）。修法是確定性還原 `Reference N → allowed_chunks[N-1]`，加兩個守門條件。⚠ **不是全部修得掉**：兩半不一致或序號越界時刻意不動（維持現狀，不猜），真實樣本上 5 題有 3 題完全清乾淨。剩下那些走原本的重寫路徑，重試用完仍會原樣出貨——**那是已知且刻意的極限**，因為猜錯比不修更糟。

- **【新】兩題「打了 web 卻沒引用」尚未判定**（2026-08-20，**觀察不是缺陷**）。`mh-07` 打 3 次 web，答案誠實說「新聞稿中並未出現」；`mh-09` 改用 KB 近似值推論身分（「META 已發行約 840 億…與題目的 847.5 億最接近」）並自己標明「現有文件未直接披露」。**沒有登錄成斷言**：web 有沒有撈到可用內容不在系統控制內，斷言它等於把外部世界寫進量尺。要判定得先讀 fixture 裡那幾筆回應到底有沒有答案——那是人工工作，還沒做。

- **【新】RAGAS 的 `GOLD_BASELINE` 與噪音底線在 65 題上失效**（2026-08-19）。那組常數綁定「120b judge ＋ **100 題** eval_set ＋ 當時的 reference」，題庫兩度換分母後**上限與底線都不能再引用**（`eval_ragas_vs_rubric.py` 已印 ⛔ 警語，但那只是擋人不是修好）。做法：在 65 題上重跑一次 gold baseline（把 reference 當系統答案餵進去）＋ 同題庫跑兩次取噪音。卡點是**成本與時機**：要在 `.venv-ragas` 跑兩輪 65 題，而且題庫若還會再動就白跑——建議等 `lex-*` 補題與 P2（live web 驗收）都定案後一次做完。

- **【新】`check_number_defects` 7 條全 PASS ＝ 現在沒有進度指標**（2026-08-19）。零噪音量尺只剩護欄功能，任何生成層改動都「量不出變好」。做法：用輔助指標① 的頭條百分比候選篩新缺陷，**人工讀完確認是真缺陷才登錄**（實測 6 個旗標有 3 個不是錯）。⚠ 不要為了讓量尺有 FAIL 就把不確定的東西登錄成 `known_defect`——`lex-17` 的口語問法就是刻意沒登錄的例子（見上一條）。

- **`mi-05` 的缺陷回到量尺了，但**仍**沒有被修**（2026-08-19 更新）。原文說要「把主張改寫成不依賴新聞的形式搬回主 claims 檔」——**已做**：`lex-17` 逐字沿用原題前半、只拿掉新聞子句，以 `known_defect` 進 `eval/number_claims.json`。**兩次端到端 1 PASS／1 FAIL，FAIL 那次逐字就是原症狀（同檔撈到 `#1`）**→ 拿掉新聞子句並沒有修好它。⚠ 我一度只憑 r1（全 PASS）就宣告「根因是新聞污染、已被推翻」並改了文件，r2 立刻打臉；完整經過見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺補回兩條〉。卡點與 2026-08-12 記的一樣：**它的觸發取決於 Plan 產生的子問題**，而 planner prompt 類改動的效果在現有量尺下量不出來。現在至少有零噪音的進度指標（`lex-17` 的 `require_chunk`），判讀**必須跑 ≥2 輪看 FAIL 比例**。

- **【新】Fundamentals 在口語問法下擠不進 top-k**（2026-08-19，**觀察不是缺陷**）。同一天的單發檢索探針測四種問法問 MSFT 營收成長率：「…表現如何？」`#0` 在 rank 5、「最近一年的營收年增率（YoY revenue growth）」rank 1，但口語的「微軟的營收成長率是多少？」**Fundamentals 完全沒進 top-5**（五席全被 10-K／10-Q 佔走）。**沒有登錄成主張**，因為 10-Q 的季度成長是合法的另一種口徑，判它錯等於用斷言獎勵一個沒有根據的偏好。卡點是這個判斷本身：要先決定「使用者沒指定口徑時，系統該給 TTM 還是財期」——那是 eval 設計問題不是檢索問題（同 memory `period-basis-ttm-disambiguation`）。決定之前不要動檢索。

- ~~**web 數值與財報衝突無人看守**（2026-08-19）~~ → **已做**，見 CHANGELOG 同日〈新增 R4〉與 [`docs/AGENTIC.md`](docs/AGENTIC.md) A10。原文：`find_authority_conflicts`（R3 財報優先）只看 chunk 的 `src_types`，而 live web 的內容走 `web_extra` **字串**、不是 chunk → `_ground_source_type` 看不到它。KB 新聞那條風險已隨拔除消失，但「web 說 X、10-Q 說 Y」是**新的同類風險且完全沒有規則**。做法：讓 `_ground_source_type` 也吃 `web_extra`（來源類型記為 `web`，非權威），R3 的判斷式不必改。⚠ 要先有確定性測試——現成的模板是 `eval/check_web_claims.py`，它已有 fixture 與零幻覺斷言。


- ~~**`--trace` 不印 web 回傳內容，只印字數**~~ → **2026-08-13/14 兩階段做完**。現在逐則印**網址＋發布日＋標題＋摘要全文**。兩次都立刻換來一個原本看不見的根因：①白名單把 `apps.apple.com` 收進來（35 筆有 33 筆是消費端頁面）；②`content[:300]` 把數字截掉（`as of Augus` ← `as of August 07, 2026 is $4572.79B`）。**「只印網域不印網址」也踩過一次**——看不出 Reuters 的日期為何抽不到（日期在網址結尾），只好另外打 API 去問。
- ~~**檢索側 LLM 用 20b 還是 120b，從來沒量過**~~ → **2026-08-14 量完：兩者等價**（gold recall 差 0.0020 vs 噪音 0.0014），見 [`docs/EVAL.md`](docs/EVAL.md)〈檢索側 LLM 換模型〉。判讀限制② 已可關閉。
  **剩下的是一個決策**：20b 在 NIM 上**慢一倍**（3.57s vs 1.76s），所以原本預期的「單發降 20b 省錢」方向是反的——該把 **agentic 的 `RETRIEVAL_MODEL` 升到 120b**（同品質、快一倍）。卡點：這會動到 agentic 生產預設，且 `AGENTIC_RETRIEVAL_MODEL` 這個 env 沒有被任何 eval 腳本鎖定，改了之後過去所有 agentic 跑分的前處理就與現況不同——要嘛接受、要嘛在改的同時重跑一次基準。
- **`eval/replay_cache.json` 有未提交的新增項**（+25 translate_en、+141 check，0 筆覆寫）。要決定納不納版控。
- **`eval/web_fixture.json`（359 KB）與 `eval/web_replay_llm.json` 要不要納版控**（2026-08-15 新增）。**傾向要**：它跟 `reference_answers.json` 是同一種東西——不需要唯一正確，只需要**固定且對所有組態一視同仁**，而且沒有它 [`check_web_claims.py`](eval/check_web_claims.py) 的 5 條斷言一條都跑不了（重錄要連網、值會變、斷言得跟著改）。卡點只有檔案大小。
- ~~**「最新一季」被答成 5 個月前的資料**~~ → **2026-08-15 查清並修掉，但成因不是這裡原本寫的那個。**
  ⚠ 原記載（「KB 裡 MSFT 最新 10-Q 是 `202603`、5 個月前的資料、成因是 `realtime_need` 判 `none` 讓時效改判不觸發、該去查 web」）**診斷錯誤**：MSFT 的 KB 天花板是 `MSFT_10K_2026`（FY2026，涵蓋到 2026-06-30），距 as-of 只有 46 天，**KB 不缺資料、web 本來就不該觸發**。真正的缺陷是「最新一季」被解析成「候選裡最新的那份 **10-Q**」而不是「證據集合裡最新的**期別**」，而更新的 10-K 就在 commit 集合裡沒被引用。修法：Synthesize 加確定性期別 validator（`find_stale_period_claims`），見 [`CHANGELOG.md`](CHANGELOG.md) 與 [`docs/AGENTIC.md`](docs/AGENTIC.md) A9。驗收 [`eval/verify_answer_validators.py`](eval/verify_answer_validators.py) 36/36。
- ~~**`reflect` 重生成會引入憑空的數字**~~ → **2026-08-15 撤回：那是量尺誤報，不是系統缺陷。**
  原記載說 `web-02` 的「2026 年的平均股價為 **$196.82**」是憑空生成。實際上 fixture 的 macrotrends 表格裡就寫著 `| 2026 | 196.8165 | 188.6200 | 235.4660 | 164.9780 | 224.0900 |`（NVIDIA 年度平均股價）——**答案是正確地四捨五入**。成因是 [`check_web_claims.py`](eval/check_web_claims.py) 的 `numbers_must_be_in_fixture` 只做**逐字比對**，沒有捨入容忍。已加上（`_number_seen`），5/5 恢復 PASS，且驗過判別力：`196.92`／`31.43` 這種鄰近錯值仍判 False。
  **教訓要記住，而且它補的是 CLAUDE.md 現有規則的鏡像面**：既有規則是「稽核回傳**0 筆問題**先當壞消息查」，這次踩的是**反過來的那一半**——**稽核回報 FAIL，也要先問「是不是量尺錯」再問「系統怎麼壞的」**。我拿一個 FAIL 當真陽性，直接寫了一整條 BACKLOG、一段 CHANGELOG 和一節 docs，才在實作生產版檢查時發現來源裡有 `196.8165`。查證成本是一次 `grep`。
- ~~**【B】跨期 field collapsing**~~ → **2026-08-19 做完並升為預設開啟**（`keep=2`／`prefer=newest`）。
  見 [`CHANGELOG.md`](CHANGELOG.md) 與 [`docs/EVAL.md`](docs/EVAL.md)〈跨期 field collapsing 與期間意圖解析〉。
  ⚠ **當初這條寫的兩個判斷，一個對一個錯**：①「規模比 A 大 17 倍」對，但**收益不是**——B 單獨只值 gold@5 +0.020，A 單獨值 +0.041；②「留 rerank 最高那份」**錯得很嚴重**（gold@5 0.837 → 0.653），因為 cross-encoder 對年份無感。正解是留**財年最新**的。
- ~~**【A】期間解析從三條窄路收攏成一個 LLM 欄位 ＋ Python 解析**~~ → **2026-08-19 實作完成，但預設仍關**（env `RQ_PERIOD_INTENT_LLM=1` 啟用）。
  **為什麼不翻預設**：在目前的單年生產 collection 上**逐題 0 變動**（買不到東西），卻要付每次 retrieve 一次 LLM（中位 **1.54s**，agentic 是每子問題每輪都打）。
  **翻預設的條件**：**多年語料升生產的同一次改動裡一起翻**。屆時它值 gold@5 +0.041、錯期率減半、`explicit` 類錯期率 0.333 → 0。⚠ **別分兩次做**——B 的趨勢題保護（`period_ref=range` → 跳過 collapse）要靠 A，多年語料上線而 A 沒開，趨勢題會吃到 collapse 的 −6%。
  ⚠ 當初這條寫的「加進 `parse_query_filters`」**行不通**：那個函式前面的 `_FILING_HINT_RE` 詞表閘門擋掉 21 題 `relative` 裡的 20 題。已改成獨立函式＋獨立 `llm_replay` kind。
- **多公司題仍然沒有期別保護**（2026-08-19 從 A 分離出來的剩餘缺口）。`_resolve_period_filter_llm` 遇到兩家以上直接回 None，因為 [`build_qdrant_filter`](rag_query.py) 吃 flat AND list、**結構上寫不出 per-ticker 的 OR-of-ANDs**（`MatchAny` 是全域 OR，會讓 A 公司的舊季通過 B 公司的期碼）。**卡點是表達力**，要先讓它多接受一種 per-ticker 群組型別。與〈「最新期間」路由的覆蓋面〉那條的 ⚠ 是同一件事。

- **【明確不做】排序層加時間衰減**。`relative` 那 21 題已證明**期間解析得到時根本不需要動排序層**（兄弟密度漲 4 倍、指標一格未動）。全域衰減會系統性答錯歷史題——而那正是加舊資料的目的。LangChain `TimeWeightedVectorStoreRetriever`／LlamaIndex recency postprocessor／ES decay function 之所以通用，是因為場景是**沒有明確指定期間的新聞流**，這裡不是。**復活條件**：B＋A 都做完後仍有殘餘 F1，且能提出「只在期間無法解析時生效」的版本＋歷史題陰性對照。

- **`MSFT_10K_2024.html#158` 幅度接地在 chunk 層漏一筆**（2026-08-19，多年語料壓測時 [`verify_chunk_grounding.py`](eval/verify_chunk_grounding.py) 抓到）。
  `#158`「General and administrative expenses increased **slightly**」不帶幅度，而數字（G&A `7,609`／`7,575`／`0%`）在 `#157`。把兩塊的存檔文字餵回判斷式重放，**兩個合併條件都是 True**（`_is_orphan_explainer(#158)`＝True、`_MAGNITUDE_RE.search(#157)`＝True，合併後 190 token 遠低於上界）——所以量尺沒錯，是真的「併得起來卻沒併」。
  **不是新缺陷、也不是模型換代造成的**：同一道閘門在生產 `us_stock_rag_edgar_mdna` 上是 **0 筆**，是新增的 FY2024 filing 才帶出來的。規模 **1/1009（0.1%）**。
  **卡點＝根因要重跑切塊器才定得了案**：[`_merge_small_chunks`](data_update_edgar.py) 是**逐硬邊界呼叫**的，而閘門只看 `chunk_index - 1`（會跨邊界）→ 兩者對「相鄰」的定義不同。`item_chunk_index` 27→28→29 連續，從 payload 分辨不出段界。可能是 (a) 兩塊當時分屬不同 segment（那麼閘門對合併器的模型偏寬，是量尺問題）、(b) 併了之後被 RCTS 上界再切開（docstring 自己警告過這條路）、(c) `#158` 是 segment 首塊、204 字元又剛好比 `_MIN_CHUNK_CHARS=200` 多 4 個字元，前後都併不到。
  **復活條件**：下次有正當理由重跑 ingest 時（不要為了這 0.1% 專門跑一次三小時的重建），在切塊器裡對這一份 filing 印出 segment 邊界，三選一。
  ⚠ 順帶：這件事再次證明 [`verify_segment_split.py`](eval/verify_segment_split.py) 與 [`verify_chunk_grounding.py`](eval/verify_chunk_grounding.py) **缺一不可**——同一次跑，section 層報 MSFT「19 → 0 全部接地」，chunk 層卻抓到這一筆。

- **`realtime_need="none"` 會短路整段時效日期算術**（2026-08-15 從 `web-03` 的誤診裡分離出來的**真機制問題**）。`REALTIME_STALE_DAYS["none"] is None` → [`_stale_for_realtime`](agentic_rag_v2.py) 第一行就 return → `_kb_ceiling_date()`／`kb_unfixable`／天花板比對**整段不執行，連 `as_of` 都沒讀到**。換句話說「KB 補不了就去 web」這條路由**被一個三分類的 LLM 欄位單點守住**，而 Grader 對「某季營收」填 `none` 是照 prompt 做對的。
  **為什麼沒有一起修**：目前**測不出來**。KB 裡沒有任何一家的 filing 天花板超出申報週期（最舊的 NVDA `202604` 距 as-of 107 天，10-Q 週期約 130 天），所以這條路徑在現有資料上永遠不會觸發，改了也沒有陽性案例可以證明它有效——那就是又一個「聽起來合理」的機制假設。
  **復活條件**：① 有一題的 KB 天花板真的落後於申報週期（換 as-of 或補一家新公司都行）；② 先寫出能證偽它的真值表（`period_ref` × 天花板 × as_of），加進 [`verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py) 閘門⑤。
  ⚠ 不要用詞表判「這題是不是相對期間指稱」——那正是 CLAUDE.md 警告的用字串比對做感知。可行方向是 Grader 加一個與 `realtime_need` **正交**的 `period_ref` 欄位（relative／absolute／none），但**只能加在 live block**：動到 snapshot 的 Checker prompt 會破壞 eval 基準逐字不變。
- ~~**`us_stock_rag_edgar_exp4` 的 Fundamentals 比率還是小數**~~ → **2026-08-12 定案：exp4 ＝ 歷史基準，不補遷移、不當對照臂**。比率留在 `0.183`（`head`／`period`／`ground2` 都已是 `18.30%`）。理由：查 [`rag_query.py`](rag_query.py) `COLLECTION_NAME` 後發現**碼上的預設一直是 `us_stock_rag_edgar_period`**，exp4 早就不是任何入口實際查的東西——CLAUDE.md 的「生產 collection ＝ exp4」是純文件漂移，已改成如實的 collection 現況表。
  **復活條件**：若日後要拿 exp4 當對照臂（例如驗「切塊六層相對 Exp0~4 世代的累積效果」），得先跑 `migrate_fundamentals_pct.py`，否則 Fundamentals 類題目的差異分不清是切塊還是單位。

---

## 未定案的決策

- ~~**`use_heading` 收窄到 `_MDNA_ITEMS`**~~ → **2026-08-13 完成並升生產**（`us_stock_rag_edgar_mdna`），見 [`CHANGELOG.md`](CHANGELOG.md)。
- ~~**`us_stock_rag_edgar_ground2` 是否升生產**~~ → **2026-08-13 由 `mdna` 取代**（收窄後每項都不輸、`check_number_defects` 同為 PASS 4／FAIL 2、semantic recall 0.497 → 0.593）。當初的兩個卡點都已解消：①收窄做完了；②**exp4 對照臂不必補**——量尺已飽和（見下），補了也量不出東西，且 exp4 連 Fundamentals 百分比遷移都沒套。
- ~~`us_stock_rag_edgar_head` 是否升生產~~ → 被 `ground2` 取代（每一項整體指標都更好）。當初的懸案「非 AAPL 的 42 題退步沒有已證實的解釋」**已解**：semantic 15 題貢獻整體 recall 缺口 ~68%、correctness 缺口 ~59%，機制是小標層把 chunk 切小→每個 chunk 帶的證據變少。
- ~~**web 打了但資料沒進最終答案**~~ → **2026-08-14 查清並修掉，成因不是管線也不是 Generator**：是 `content[:300]` 把數字截掉（詳見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A7）。當時「web 呼叫次數多的跑次答案就好」那個相關性也有解釋了——多撈幾次就多一次機會讓數字落在 300 字以內。
- ~~**live 模式成本上升（21 輪）**~~ → **2026-08-14 修掉**。成因不是 replanner 失控，是 `WEB_SEARCH_MAX_CALLS` 記在 `_RunState` 而 `_RunState` 每個子問題歸零 → 總量無上界。改用 query 級 `QUERY_WEB_BUDGET` ＋ `kb_unfixable` 提早跳出必敗重試，同一題 21 輪 → 4 輪、7 次 web → 3 次。
- **KB 真的沒有資料時，replanner 仍會生一串同義待辦**（2026-08-14 觀察）。`kb_unfixable` 只涵蓋**時效**這一種「KB 補不了」；當不足的原因是 KB 根本沒有那個數字（實測「Azure 最新一季成長率」，KB 只到 FY26 Q3）時旗標不會亮，於是 replan 一路加到 7 個待辦。
  **成本已經不同了**：web 被 `QUERY_WEB_BUDGET` 封在 3 次，多出來的只有本機 KB 檢索（免費、秒級），所以這條的優先度低。
  **不要用字串相似度認同義待辦**——2026-08-14 實測分離度是負的（正向最低 jaccard 0.04、負向最高 0.50），見 [`docs/AGENTIC.md`](docs/AGENTIC.md) A7。
  **可行方向**：把 `kb_unfixable` 從「時效」推廣到「同一子問題連續兩輪 `missing` 沒有實質變化」——但那是啟發式，要先有能證偽它的測試才做。
- **web 結果的外國掛牌路徑擋不到**（2026-08-14 新增）。`_host_allowed` 擋掉了地區**子網域**（`ca.finance.yahoo.com`、`cn.wsj.com`），但同一主機下的外國掛牌**路徑**擋不到——實測 `stockanalysis.com/quote/bvl/AAPL/market-cap`（利馬交易所）給 $4.98T，而美股頁 `stockanalysis.com/stocks/aapl/market-cap` 給 $4.45T，同一天差 12%。
  **不要用路徑詞表擋**（`/quote/xx/` 之類）：那是各站自訂的 URL 慣例，站方改版就失效，且新增一個來源就要重讀一次它的 URL 結構——就是 O(n) 白名單那個病。
  **可行方向**：讓生成端看得到「這則是哪個市場」——多數頁面的 content 裡有幣別或交易所字樣。但這要求 LLM 做判斷，先確認它真的分得出來再說。
  **現況風險**：`TAVILY_PER_DOMAIN_CAP=2` 讓同站最多兩則，美股主頁通常 rerank 較前，所以外國頁多半排不進；但沒有保證。
  **對照：同類但已解的是選擇權合約頁**（`quote/TSLA260814C00257500`）——那有 OCC 標準格式可比對，屬格式定義的封閉集合。外國掛牌路徑**沒有**跨站標準，差別就在這裡。
- **Planner 對「KB 結構上不可能有」的題仍白搜一輪**。實測 `What is NVDA's latest stock price?` 跑 21 輪檢索才輪到 web，而股價根本不是財報內容。
  **不要讓 Planner 自己判**：它手上只有 coverage 摘要、沒有真實 chunk，只能用猜的；且 `_plan_subqueries` 回傳 `list[str]`，格式上就沒有「不檢索」這個選項。猜錯的代價不對稱（猜「沒有」但其實有 → 整題答不出來），所以架構刻意把判斷延後到 Grader 看見真實候選之後——同 [[multi-intent-agentA-is-the-leak]] 的思路。
  **可行方向**：在 todo 層加確定性判斷——`realtime_need == "intraday"` 的子問題（股價、盤中報價）標成 web-first。訊號跟 2026-08-13 加的那個一樣，只是用在更前面一層。
  **2026-08-14 更新**：原本的卡點（「要先確認 web 這條路本身是好的」）已解消，而且 `kb_unfixable` 已經把浪費從 21 輪壓到 4 輪——**剩下的 4 輪是每個子問題各 1 次首輪檢索，那是必要的**（要有真實候選 Grader 才判得出時效）。所以這條的剩餘收益已經很小，除非量到具體損害否則不做。
- **`find_authority_conflicts`（R3 財報優先）對 web 來源全盲**。[`agentic_rag_v2.py`](agentic_rag_v2.py) `_ground_source_type` 只拿 **chunks** 回頭定位數字來源，web 內容從不在 `chunks` 裡 → 從 web 來的宣稱拿不到 `src_types` → R3 的 `if not c.get("src_types"): continue` 直接跳過。2026-08-13 放行「web 數字可引用」之後，這條防線對 web-vs-財報的衝突一格都不設防。
  **但不要直接把 web 塞進 R3**：R3 的規則是「權威（財報）> 新聞」，而 web 觸發的情境恰恰是**時效性衝突**（KB 有舊值、web 有新值），正確答案是新的那個——硬接會讓 R3 系統性地判反，把剛修好的東西再弄壞一次。R3 處理的是「同期間、不同來源」，時效衝突不在它的設計範圍。
  **卡點**：需要的是一條新規則（同指標、值不同、時點不同 → 兩個都保留並標時點），不是改 R3。
  ~~**復活條件**：要嘛做出帶 web 的評測情境…~~ → **2026-08-15 復活條件已達成**：[`eval/record_web_fixture.py`](eval/record_web_fixture.py) ＋ [`eval/web_claims.json`](eval/web_claims.json) 的 `web-01` 就是這個情境的載體（KB 有 2026-06-12 的 `$4,342.02B`、web 有更新的值）。
  ⚠ **而且量到那條防線目前是空的**：`web-01` 的正確行為（兩值並陳、各自標時點）是 **Generator 靠 prompt 做到的，R3 根本沒被觸發**（web 內容不在 `chunks` 裡 → 拿不到 `src_types` → `continue`）。斷言已把這個行為鎖住，所以現在改 R3 有回歸保護了。
- **一致性 validator 的重寫路徑**：偵測 4/4 精準，但重寫實測 **2 好 1 壞**（col-11 修掉矛盾卻把總營收誤標成雲端營收，且三層驗證全過）。選項：改成**只偵測不重寫**（把矛盾標記給使用者看）以拿掉那個 1 壞。
- **router（複雜度分派）**：簡單題→單發、複雜→agentic；保守偏 agentic（誤判複雜為簡單代價高）。ratio 路由屬**正交檢索提示、非第三分支**，應放共用檢索層讓兩條管線都吃到。卡點：measure-gated，目前量尺分不出。
- 生產 `rag_query.py` 是否跟進 `full_translate_en=True`。卡點：要先確認同樣的 chunk-level rerank 平坦問題在單發管線也存在。
- FY 雙向硬 filter、「最新年度」as-of 錨定。卡點：要先跑全量 eval 防回歸。

---

## 想做但量不出來（先別動）

- ~~**`_check_sufficiency` 保留率是資訊瓶頸 → 該做 Grader 前確定性去重**~~ → **2026-08-15 兩條假設都被自己的探針證偽**（零 LLM，只讀既有結果檔，見 `docs/EVAL.md` 已試無效總表）：①**沒有東西可以去重**（近重複 13/341＝3.8%，完全相同 chunk id 0）；②**Grader 砍的是冗餘不是 gold**（檔案層命中 95/100 vs 單發 97/100，淨損失 2 題）。
- **真正的缺口是 lexical 的 chunk 層**（2026-08-15 新增，取代上一條）：agentic 在 lexical 平均只承接 **1.93 個 chunk**（全類最低，semantic 4.60、colloquial 3.87），而 lexical 正是 RAGAS `context_recall` 唯一明顯輸單發的類別（−0.078）。**但檔案層命中 12/15 與單發完全相同** → 病灶不是「撈錯檔」，是**同一個檔裡收得太少或收錯 chunk**。
  **卡點＝量尺**：檔案層 gold 已飽和，分不出這個差異；要驗證得先有 chunk 層的 gold（`eval_set.json` 的 `relevant` 只到檔名）。
  **附帶事實**：lex-03／lex-07／lex-14 三題**兩條管線都撈不到 gold**，那是檢索層問題不是 agentic 問題（與 `ablate_retrieval_model.py` 三臂全空的四題一致：col-07/lex-03/lex-07/lex-14）。
- **`find_claim_conflicts` R2 放寬**（`len(seg_changes) >= 2` → ≥1）。卡點：要先量誤報率——「總計 < 某部門」在另一部門衰退時是**合法的**。
- `GEN_TEMPERATURE=0` 對答案層數字穩定性的影響（目前 0.3）。
- 「近一年／最近一年／過去一年」→ TTM 硬路由（擴充 `_TTM_RE`）；IncomeStatement 財年硬邊界。
- **「最新期間」路由的覆蓋面，會隨 KB 累積年份惡化**（2026-08-14 提出）。病灶本身**已實證且已部分修好**：cross-encoder 分不出同主題 chunk 是哪一季（mix-06 probe：舊季 0.70~0.73 與最新季幾乎並列，最新季 gold 被壓到 rank 7/13/17、掉出 top-5 → context_recall=0），[`rag_query.py`](rag_query.py) `_LATEST_QUARTER_TIME_RE` 那條 deterministic 路由就是治本修法——掃 collection 解出最新 10-Q 期碼、注入 `report_period_code` 硬 filter。**但它有三個破口，且排序層完全沒有時間先驗**（[`rag_query.py:1429`](rag_query.py#L1429) 純 `raw_rerank_score` 排序）：
  ① **觸發靠硬編碼詞表**（`最近|最新|近期|…`）——「蘋果賣得怎麼樣」語意上問現況卻不含任何詞，漏掉就退回純語意排序。這正是 CLAUDE.md 警告的「用字串比對做感知」。
  ② **只覆蓋 10-Q**——2026-08-14 掃 collection 查清楚，這是**資料層決定的、不是偷懶**：`report_period_code` 在四種 doc_type 上長得不一樣（10-K `'2025'/'2026'` 4 位年份、10-Q `'202510'…` 6 位 yyyymm、**News 98 ＋ Fundamentals 81 chunks 全是 NONE**）。所以 News／Fundamentals 是**沒有欄位可 filter**（也正是 `routed_latest` 要放寬成「== 期碼 OR 期碼為空」的原因）；10-K 則是**病灶結構上不存在**——每家只有 1 份 10-K，沒有同主題多期競爭。
  ⚠ **擴充時的地雷**：[`_get_latest_10q_periods`](rag_query.py#L516) 的 `len(code) < 6: continue` **不能只是拿掉**。期碼是字串比大小取 max，而 `'2026' > '202510'` 為真（比到第 4 位 `6>5`）→ 讓 10-K 進來的話年報會永遠贏過季報。要擴充必須先把兩種編碼空間分開。
  ③ **限「恰好一家公司」**：多公司比較題不路由。**2026-08-14 兩步查清，結論跟直覺相反**——
  　 (a) eval_set 有 **0 題**受影響（16 題通過前六道閘門的全是單一公司）；
  　 (b) 補測資實測（[`eval/probe_multi_company_period.py`](eval/probe_multi_company_period.py)，8 題人工構造多公司題）：**損害是真的**（點名公司有 **35.3%** 拿不到最新季 chunk，單公司關路由只有 6.2%），**但期別 filter 修不到**——6 件損害 **kind A（同一家的舊季擠掉新季）＝ 0、kind B（那家公司連一個 10-Q 都沒進 top-5）＝ 6**。
  　 **真病灶是席位競爭**：top-5 分給 2~3 家，一家壟斷就把另一家整個擠掉。[`_ensure_ticker_coverage`](rag_query.py#L1051) 有保底，但它補**該家分數最高的 chunk、不管 doc_type 也不管期別**（實測補進來的是 `AAPL_News`、`TSLA_Fundamentals`），回答不了「最新一季營收」。
  　 **對照組解釋了為什麼單公司題看不出來**：`single_off` 錯期率 0.577 → 缺失率只有 0.062（5 格全給一家，一半錯期還是留得住最新季）；`multi` 錯期率 0.429 → 缺失率 0.353。**席位稀釋把同樣的錯期率放大 5.7 倍**。
  　 **還沒排除的可能**：舊季 chunk 佔走的席位（mc-01 有 2 格、mc-07 有 2 格）若被期別 filter 釋放，遞補上來的會不會正好是缺席公司的最新季 10-Q。**這不能推論，要做出 filter 臂實測**。
  　 **(c) 損害範圍只在單發管線**（2026-08-14 追加實測）：agentic 的 Planner 把 8 題多公司題拆成 **17 個子問題，17 個都只剩一家公司、17 個路由都會觸發** → 席位競爭與期別破口在 agentic 上**結構上不存在**（是 planner 在上游消掉的，不是 Grader 補救的——Grader 連上場機會都沒有）。而 [`api_server.py`](api_server.py) 直接呼叫 `rq.retrieve()`、不 import agentic，所以**使用者實際在用的 web UI 就是有損害的那條路**，且它一次檢索就結束、沒有第二輪。
  　 **為什麼不把這件事交給 Grader**：①單發根本沒有 Grader；②「哪家沒撈到」是 `want_tickers - present` 的集合差、確定性的，照 CLAUDE.md 分工該給 Python，用 LLM 判要多付一輪 LLM＋一輪檢索；③Grader 有已知失效（replan 生同義待辦、只留 49.3% 候選）。
  　 **收斂後的修法**：[`_ensure_ticker_coverage`](rag_query.py#L1051) 補位時，若本次檢索帶 `routed_latest` 意圖，優先挑該公司**最新期碼的 10-Q**而非分數最高的任意 chunk（實測補進來的是 `AAPL_News`、`TSLA_Fundamentals`，答不了「最新一季營收」）。單公司題 `want <= 1` 直接 return，不受影響。**eval_set 量不到（0 題）——這是產品修正不是分數修正，驗收用 probe 的 `missing_rate 0.353 → ?`**。
  **修法方向**（照 CLAUDE.md 的 LLM/Python 分工）：「這題問的是不是最新期間」沒有唯一機械答案 → 交 LLM 判；「最新期間是哪個期碼」是確定性的 → Python 從 `agentic_rag_v2._scan_kb_coverage()` 的 coverage map 算（它已經每個 ticker、每種 doc_type 都存了最新記錄）。
  ⚠ **多公司不能用 `MatchAny`**（2026-08-14 更正，原本這裡就是這樣寫的、是錯的）：`MatchAny` 是**全域 OR**，AAPL 最新 `202606`／MSFT 最新 `202603` → `MatchAny(['202606','202603'])` 會讓 **AAPL 自己的舊季 `202603` 通過**，正是要排除的東西。正確結構是 **OR-of-ANDs**（每家自己配自己的期碼，且各自保留 `or is empty` 那層放行 News/Fundamentals）：`should=[ must=[ticker==AAPL, period==202606 or empty], must=[ticker==MSFT, period==202603 or empty] ]`。
  **真正的卡點是表達力**：[`build_qdrant_filter`](rag_query.py#L891) 吃的是 flat `[{field,value,polarity}]`、全部 AND 在一起，**結構上寫不出「每家配每家」**——要做得先讓它多接受一種 per-ticker 群組型別。
  ~~**卡點＝現在不可證偽**…**復活條件：KB 納入第二個年度的 filing 時**~~ → **2026-08-19 復活條件達成並執行完畢，量到的東西跟這條原本的預期不一樣。**
  壓測（21 → 77 份 filing，`us_stock_rag_edgar_multiyear`，[`eval/probe_temporal_interference.py`](eval/probe_temporal_interference.py)，完整分析見 [`docs/EVAL.md`](docs/EVAL.md)）：
  · **破口① 的詞表沒有變成損害來源，反而證明了那條路由不能拔**：`relative` 類兄弟密度漲 4 倍（1.90→7.52），gold@5 與錯期率**一格都沒動**——硬 filter 全吸收。但陽性對照顯示關掉它的代價從單年的 0.048 錯期率變成 **0.476**，**價值放大約 10 倍**。所以「改詞表」的優先度不是因為它會出錯，是因為**漏一題的代價變成差兩年**。
  · **排序層時間先驗仍然不是答案**：真正壞的是「問題沒提期間」那 25 題（排擠率 0.080→**0.480**），而**F1 68 席 vs F2 4 題**——病灶是**多樣性不是期別選擇**。加全域時間先驗會系統性答錯歷史題，方向是跨期近重複去重／MMR。
  · **⚠ 現成的去重工具撞到自己的假設**：[`_suppress_near_duplicates`](rag_query.py) 規則一寫著「跨檔絕不比——不同期的相同數字是巧合」。多年語料下 `AAPL_10K_2024` 與 `AAPL_10K_2025` 的同一張表**就是**真的跨檔近重複，要用它得先鬆綁這條。
  **2026-08-26 階段 4 執行完畢**：四條修法定案（A 已實作預設關、B 預設開、**C 不做**、D 不做），完整證據與「C 為什麼不做」見 [`docs/EVAL.md`](docs/EVAL.md)〈階段 4〉。同時更正了兩件事：壓測的 collection 已換版（含新聞 → 零新聞），題庫分母從 49 變 45。
  ~~**剩下的卡點只剩一個，而且是缺量尺不是缺修法**……**要補的量尺（最小版）**：一組 gold 只存在於舊年度 filing 的歷史題（例如「Apple FY2024 的總營收是多少」）~~ → **2026-08-27 量尺補完並跑完三臂**（[`eval/probe_historical_benefit.py`](eval/probe_historical_benefit.py) ＋ [`eval/period_probe_benefit_queries.json`](eval/period_probe_benefit_queries.json)，20 歷史題 ＋ 4 當期陰性對照，完整分析見 [`docs/EVAL.md`](docs/EVAL.md)〈多年語料買到了什麼〉）。
  ⚠ **上面那個括號裡的例子當時就是錯的**：10-K 損益表自帶三年、MD&A 自帶兩年 → 「Apple FY2024」單年 KB 本來就答得出來。**收益區從 T-3 才開始**，拿 T-1／T-2 造題會憑空灌水。
  · **檢索層的收益量到了**：historical `gold@5` **0/20 → 18/20（兌現率 0.900）**，gold_rank 中位 1；當期陰性對照 4/4 不動 → **不是拿當期題換來的**。
  · **收益是語料買到的、不是修法買到的**（A 開關兩臂 historical 完全一樣）；**當期題的損害才是 A 擋掉的**（A 關 → control 4/4→3/4、同節別期席位 0→7）→ 獨立佐證了上面那條「別分兩次做」。
  · **單年 KB 對歷史題不是空手**：20 題有 15 題的 top-5 至少一席是「同一節、別的年份」，10 題的 top-1 就是 → **有引用、看起來很有根據的錯答**，比拒答難發現。
  **升生產時要一起做的**：翻 `RQ_PERIOD_INTENT_LLM`（**別分兩次做**，見上）、釘死 `sem-03`／`sem-04`／`col-02`／`col-03` 的萬用字元 gold、修 `MSFT_10K_2024.html#158` 的幅度接地漏抓；**下面 ③④ 兩個缺陷都不是多年語料造成的，③ 更是現生產就在發生**（實測見該條）——所以它們是**獨立的既有缺陷，不是升生產的前置條件**。
- **【多年語料 ②】生成層的收益完全沒量**（2026-08-27，承上）。上面全是**檢索層**。「單年 KB 遇到歷史題會拒答，還是拿那 35 席的別年份硬答」要花 LLM ＋ 標準答案才量得到，而**那正是使用者實際會看到的東西**。拆法同 live web 的 ①／②（便宜的必要條件已完成 → 貴的內容半另外做）。
  **最小版**：拿那 20 題在兩個 collection 上各跑一次 agentic，判三態（拒答／答對／**拿別年份硬答且沒揭露**）。第三態才是要抓的。⚠ 不要對照 RAGAS 分數——20 題的 MDE 遠大於任何差值，這裡要的是逐題三態不是聚合指標。
- **【現生產缺陷】絕對年份 filter 會命中「標著那一年、卻不含那一年數字」的 filing**（2026-08-27，收益探針 `bh-07` 抓到，**回頭在生產 collection 上實測複現**）。
  **生產實況**：在 `us_stock_rag_edgar_mdna` 上問「Microsoft 在 2025 財年的營收是多少？」→ Tier 1 命中，top-5 **五席全是 `MSFT_10Q_202512`**（FY2026 Q2，`report_label_year=2025`），而含 FY2025 年度數字的 `MSFT_10K_2026`（三年表）被`fiscal_year=2025` 擋在外面。**這題現在就是錯的，而且 65 題題庫裡沒有任何一題照得到。**
  多年語料上的同型病灶（`bh-07`，問 FY2023）：`fiscal_year` 的硬 filter 是與 `report_label_year` 的**雙座標系 OR**，而 `MSFT_10Q_202312` 的 `report_label_year` 是 `'2023'`（財年其實是 FY2024 Q2）→ 問「Microsoft FY2023 營收」時 Tier 1 命中那份季報，含答案的 `MSFT_10K_2024`（三年表）反而被 `fiscal_year=2023` 擋掉，top-5 五席全錯。
  ⚠ **命中比落空更糟**：落空會降級到 Tier 2「丟年份、保 ticker」，`bh-01`~`bh-05` 問 FY2022 全靠它救回來（rerank 一看就把 `*_10K_2023` 的三年表排第 1）。命中則救援永不啟動。
  **根因是一個沒被寫下來的假設**：「那一年的數字住在標著那一年的 filing 裡」。單年 KB 上沒機會被證偽，多年語料一來，**語料年份下界的前一年**必踩。修法方向：年度題（`granularity=annual`）不該吃 `report_label_year` 那半個 OR；或 Tier 1 命中後再驗一次「這份 filing 的期別真的等於問的期別」。⚠ 動之前先確認不會破壞 `report_label_year` 當初要解的問題（10-Q 的曆年指稱）。
- **【多年語料 ④】`AWS` 沒有被解析成 AMZN**（2026-08-27，收益探針 `bh-14` 抓到）。「AWS 在 2022 年的淨銷售額」抽不到 ticker → Tier 3 退回無 filter → top-5 是 META／MSFT／TSLA 的 IncomeStatement 跨公司污染。**這是實體解析不是期別問題**，與 ③ 修在不同地方，別併成一個「多年語料的損害」數字。⚠ 修法**不要走硬編碼別名表**（`AWS→AMZN`、`Azure→MSFT`…那是 O(n) 的開始，見 CLAUDE.md〈LLM 與 Python 的分工〉）；ticker 抽取本來就在 LLM 那一側，該補的是 prompt 裡「產品／子公司名也要映回母公司 ticker」這條規則。

---

## 環境雜務

- 刪除過期的 Qdrant collection（**保留 `us_stock_rag_edgar_exp4` 當歷史基準**）。
- `git prune`：目前有過多 unreachable loose objects（每次 commit 都會警告）。

---

## 已接受的極限（查清楚後決定不修；要動請先滿足復活條件）

| 項目 | 真因 | 復活條件 |
|---|---|---|
| **sem-08**（AMZN AWS 策略方向） | 三種系統側修法皆敗於同一真因：生成模型判定「獲利數字」與「策略方向」問法無關而主動略過，**非訊號埋沒** | 只剩調整 rubric 或維持現狀 |
| **col-08**（Tesla 口語版） | dense rank 25/106，落差超出 rewrite 射程 | 有新召回機制才重試 |
| **col-04**（Azure 口語版） | 關鍵內容在 top-5，生成被口語框架帶偏、選擇誠實拒答。**非 bug** | 觀察中 |
| **col-05**（AWS 口語版，sem-08 鏡像） | RECALL 層問題 ＋ rewrite 變體品質不穩定 | 同 col-08 |
| **col-07**（否定框架） | 病根是「非龍頭」vs「minority share」的**框架落差**，不是翻譯；`full_translate_en` 已開仍漏 | 不上 HyDE |
| **ckpt_R = 0.7307**（chunk recall, n=20） | 約 27% checkpoint 在候選池階段沒被撈到，多非 critical | 暫不優先 |
| **faithfulness 不再追高** | 這是**負空間**：把 gold 當答案餵回去只拿 0.656，61/100 題輸給系統。往上推等於要求系統比標準答案更保守 | 換 metric 才談 |
| **期別探針的 gold 不再細修**（2026-08-19） | 人工複審 49 題：gold 指稱沒被新資料破壞，但 **46/49 的 gold 就是該公司最新那一份**，而 B 的規則正是「留最新」→ `gold@k` 結構上偏袒 B；3 題「gold 不是最新」的陰性對照經人工讀原文**全是假警報**（`mix-07`／`lex-12` 的更新一季逐字有同一句、`mix-10` 被路由到 news）。要修就得**重寫 gold 到 chunk 粒度並標註「哪些期別同樣可接受」**，那是重做題庫等級的工，而這條路的決定（B 開、A 關）不會因此改變。詳見 [`docs/EVAL.md`](docs/EVAL.md)〈期別探針的 gold 人工複審〉 | 多年語料要**升生產**時——屆時 `gold@k` 會變成驗收指標而不只是壓測指標，偏袒就從「講清楚就好」變成「必須修掉」。同一次要把 `sem-03`／`sem-04`／`col-02`／`col-03` 的萬用字元釘死 |
| **KB 不收新聞**（2026-08-19） | 新聞的正確性判準（夠新＋來源可信）與本系統的每一層機制（期別＋引用＋數字可驗）正交；實測它佔 2.5% 語料卻造成最大的殘留檢索失敗，且 28 條來源標記有 17 條不合本專案自己的 web 白名單標準。完整理由見 [`docs/EVAL.md`](docs/EVAL.md)〈KB 拔除新聞〉 | **不是「把舊語料放回去」**。2026-08-19 起分兩半做：**①路由（只燒 LLM）＝已完成**——[`eval/probe_news_web_routing.py`](eval/probe_news_web_routing.py) 實測 37 題觸發 web ≥35/37（multi_intent 15/15），strict 陰性對照 0/7 → **路由不是瓶頸**。**②內容（連網燒 Tavily）＝進行中**：錄 `eval/web_fixture_news37.json`（as-of 2026-08-20），再寫性質斷言。⚠ **驗收標準已改**：原本寫「對照 KB 有新聞的舊 collection」，但那 37 題的 gold 是「2026 年 6 月的新聞說了什麼」而 live web 回答「現在的網路說什麼」——**是兩個不同的問題**，對照打分會系統性低估且低分原因與 web 無關。改用 [`eval/check_web_claims.py`](eval/check_web_claims.py) 的性質斷言：有引用 web／主機在白名單且非地區子網域／數字逐字可回溯 fixture／時效有揭露 |
| **切塊／ingest 不再改**（2026-08-13） | **量尺飽和**：六個 RAGAS 指標五個已達或超過 gold 上限；檢索全修到完美只值 +0.024，低於 0.067 噪音底線。詳見 [`docs/EVAL.md`](docs/EVAL.md)〈量尺飽和〉 | 換沒飽和的量尺（擴充 `check_number_defects` 覆蓋率），或改攻生成層 |

---

## 已知缺陷：mix-07（Plan 把混合題譯成純新聞查詢）

**證據（四個 run，`is_refusal` 修好後重判）**：

| run | 子問題 | 只撈到 News | 結果 |
|---|---|---|---|
| `gj_v2_period_full100_20260808` | `Wiz 的金額及其對 Google Cloud 部門的影響` | 否 | 答對 |
| `gj_v2_period_replay1` | 新聞 ＋ `查找…金額與影響` | 否 | 答對 |
| `gj_v2_period_replay2` | **只有**「Wiz 的**相關新聞**內容是什麼」 | **是** | **拒答** |
| `gj_head_full100_20260810` | 「相關新聞內容」＋「搜尋 GOOGL 的**新聞資料庫**」 | **是** | **拒答** |

**與 collection 無關**（period 自己也會拒答），真因是 **Plan 節點把「收購金額」這個財報事實譯成新聞查詢** → 檢索被限制在 News chunk → 財報裡的 $29.5B 撈不到。`只撈到 News` 與拒答 **4/4 完全相關**，是乾淨的預測指標。

⚠ **2026-08-12 擴充樣本後仍成立，相關性升到 6/6**（新增 `gj_ground2_claims4_r1~r3` 三個 PASS ＋ `gj_ground2_full100_20260812` 一個拒答）。跨 collection 命中率：ground2 **3/4**、period **2/3** ＝ 統計上沒有差別，**兩邊都被 plan 變異主導**。最病態的那次 plan 有三個子問題而且**全部是新聞導向**（「相關新聞內容」／「新聞細節與金額」／「在知識庫新聞中搜尋」）→ 候選池 10 個 chunk 全是 News → 拒答。 ⚠ 這也說明**掛了 replay fixture 也擋不住它**：replan 產生的新 query 不在快取裡（該次 run `hit=449 miss=63`），會重新問 LLM。所以 mix-07 不能當 collection A/B 的判準，只能當 planner 改動的判準。

**已登錄成斷言**（2026-08-11）：`kind: require_text`，`expect_text: 29\.5\s*billion|295\s*億`。舊的 `anchored_pcts` 判不出這個缺陷（拒答的答案裡沒有百分比 → N/A 而不是 FAIL）。四個封存檔實測 **2 PASS / 2 FAIL**＝雙向都有判別力。

**卡點**：這是 plan 抽樣變異（4 個 run 有 2 種行為），修法可能要在 planner prompt 加「財報事實不要譯成新聞查詢」的約束，但那類 prompt 改動的效果在現有量尺下量不出來（同 `docs/EVAL.md` MDE）。**現在至少有零噪音的進度指標**：拒答比例從 2/4 降下來才算有動。

---
## 已知缺陷：ratio 路徑的兩個殘留（2026-08-21 起）

> mi-05／lex-17 的**檢索半邊已修**（2026-08-21）；**殘留①（詞表閘門）與殘留②（生成端四捨五入）
> 已於 2026-08-25 修掉**——真因、修法、量尺都見 [`CHANGELOG.md`](CHANGELOG.md) 該兩日，
> 依搬移紀律從本檔移出。這一節只剩「修完之後還沒收斂的兩件事」。

**殘留②的殘留：prompt 規則管得到「怎麼寫」，管不到「有沒有照做」（2026-08-25）。**

生成端的四捨五入已加規則（`rq.SYSTEM_PROMPT` Rule 8 的 NUMERIC FIDELITY，見
[`CHANGELOG.md`](CHANGELOG.md) 2026-08-25）並補上零 LLM 量尺
[`eval/check_rounding_fidelity.py`](eval/check_rounding_fidelity.py)。剩下的是**量尺效力**：

改動前三個封存結果檔的發生率是 **0／0／1 題（共 195 題）**。事件率約每輪 1 題，
所以「改完之後是 0」幾乎不構成證據——那個 0 跟改動前兩個結果檔的 0 長得一模一樣。

**復活條件**：要對這條規則下「有效／無效」的結論，得先讓量尺有東西可量。兩條路：
① 累積多輪（`n` 要到「事件數 ≥ 5」的量級才有話講）；
② **造一批必然觸發的題**——gold 是 Fundamentals 的比率欄位（帶兩位小數）、
   且同公司 10-K 有一個相近整數值的那種。`lex-17` 就是這個形狀，找齊一批就能把
   事件率從 1/65 拉到接近 1/1。⚠ 這批題**只用來量這條規則**，不要併進 `eval_set.json`
   （分母又變一次，跨日分數再也比不了；理由同 2026-08-19 拆題庫那次）。

**殘留③：補進 `#0` 之後，那 6 題的 correctness 平均 −0.029，需要逐題人工複審（2026-08-21）。**

確定性補撈讓 6 題（`mix-01`／`mix-06`／`mix-08`／`mix-10`／`lex-17`／`col-12`）的 Fundamentals
從零比率 chunk 換成含比率的 `#0`。逐題 RAGAS 配對差分：這 6 題平均 **−0.029**，
其餘 59 題（碼路徑不可達）平均 **+0.006**，全體 **+0.003**。

**假說**：`#0` 塞滿 TTM 比率，可能誘使模型在 **gold 要的是財期值**的題目上改答 TTM
（`col-12` −.167／`mix-10` −.126／`mix-06` −.080，而 `lex-17` +.122／`mix-08` +.080）。

⚠ **不要拿這個 −0.029 當結論**：n=6，而單題波動輕易到 ±0.3（同一輪裡 `col-11` −.46、
`col-08` +.45，兩題都在碼路徑之外）。聚合分數在這個樣本數下沒有判別力。

**要做的是逐題人工複審那 6 題**：看的是「gold 的口徑是財期還是 TTM」與「答案用了哪一個」。
若 gold 本身要的是財期值，那是 **eval 設計問題不是系統問題**（同 mi-05 當初的卡點）；
若答案確實被 `#0` 帶偏，修法方向是**在補撈的 chunk 上標明口徑**，而不是不補。
