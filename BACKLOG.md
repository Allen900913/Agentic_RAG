# BACKLOG — 未做 / 待決 / 已接受的極限

> **這裡放什麼**：還沒做的事、做了但沒定案的事、以及查清楚後決定**不修**的極限。
> **這裡不放什麼**：已完成的改動（→ `CHANGELOG*.md`）、知識與踩坑（→ `docs/`）。
>
> **維護紀律**：一件事做完就從這裡**刪掉**並寫進 `CHANGELOG*.md`；查清楚決定不做就搬到最後一節「已接受的極限」並寫下**復活條件**。
> 每一條都要能回答「**卡在哪**」——沒有卡點的條目不是 backlog，是願望。

---

## 立即可做（不需重建、不燒額度）

- **`--trace` 不印 web 回傳內容，只印字數**（`web fallback → 2163 chars`）。導致「web 打了但資料沒進答案」無法診斷——分不出是 Tavily 沒撈到，還是 Generator 拿到了不用。**卡點**：無。就是加幾行 trace（印標題＋網域，不印全文避免 log 爆掉）。**這是下一步該做的第一件事**，在它做完之前不要再猜下面那條的成因。
- **`eval/replay_cache.json` 有未提交的新增項**（+25 translate_en、+141 check，0 筆覆寫）。要決定納不納版控。
- ~~**`us_stock_rag_edgar_exp4` 的 Fundamentals 比率還是小數**~~ → **2026-08-12 定案：exp4 ＝ 歷史基準，不補遷移、不當對照臂**。比率留在 `0.183`（`head`／`period`／`ground2` 都已是 `18.30%`）。理由：查 [`rag_query.py`](rag_query.py) `COLLECTION_NAME` 後發現**碼上的預設一直是 `us_stock_rag_edgar_period`**，exp4 早就不是任何入口實際查的東西——CLAUDE.md 的「生產 collection ＝ exp4」是純文件漂移，已改成如實的 collection 現況表。
  **復活條件**：若日後要拿 exp4 當對照臂（例如驗「切塊六層相對 Exp0~4 世代的累積效果」），得先跑 `migrate_fundamentals_pct.py`，否則 Fundamentals 類題目的差異分不清是切塊還是單位。

---

## 未定案的決策

- ~~**`use_heading` 收窄到 `_MDNA_ITEMS`**~~ → **2026-08-13 完成並升生產**（`us_stock_rag_edgar_mdna`），見 [`CHANGELOG.md`](CHANGELOG.md)。
- ~~**`us_stock_rag_edgar_ground2` 是否升生產**~~ → **2026-08-13 由 `mdna` 取代**（收窄後每項都不輸、`check_number_defects` 同為 PASS 4／FAIL 2、semantic recall 0.497 → 0.593）。當初的兩個卡點都已解消：①收窄做完了；②**exp4 對照臂不必補**——量尺已飽和（見下），補了也量不出東西，且 exp4 連 Fundamentals 百分比遷移都沒套。
- ~~`us_stock_rag_edgar_head` 是否升生產~~ → 被 `ground2` 取代（每一項整體指標都更好）。當初的懸案「非 AAPL 的 42 題退步沒有已證實的解釋」**已解**：semantic 15 題貢獻整體 recall 缺口 ~68%、correctness 缺口 ~59%，機制是小標層把 chunk 切小→每個 chunk 帶的證據變少。
- **web 打了但資料沒進最終答案（2026-08-13 殘留）**。兩個實例：①「蘋果的即時市值」7 次 web、**18 次時效改判**，答案仍是 `AAPL_Fundamentals_20260612` 的 $4342.02B；② FSD 進展某一跑 2 次 web、答案零個 `[web:]` 引用。
  **已排除**：不是管線斷掉——同一題的答案從「截至**目前**的即時市值」變成「截至 **2026-06-12** 的即時市值」，證明 `web_extra` 確實進到 Generator（修訂條款＋時間契約生效）。
  **相關觀察（未證因果）**：web 呼叫次數多的跑次答案就好（8月新聞 5 次→優／2 次→拒答；FSD 3 次→優／2 次→零引用／0 次→只有財報）。暗示成因可能在 Tavily 回傳品質而非管線。
  **卡點**：`--trace` 不印 web 內容（見上一節第一條），無法分辨成因。**先補觀測性再查**。
- **live 模式成本上升**：拿掉詞表閘門＋時效改判後，前漏網三題從 1~7 輪暴增到 21、21、4 輪。機制：時效改判把 `sufficient` 一路壓成 False → replanner 一直生新子問題（每題 3 輪 × 最多 7 個 todo）。**卡點**：要先確認品質收益站得住（目前 n 太小），才知道這個成本值不值得。上界仍由 `MAX_ITERS`／`MAX_TODOS`／`WEB_SEARCH_MAX_CALLS` 擋住，不會失控。
- **Planner 對「KB 結構上不可能有」的題仍白搜一輪**。實測 `What is NVDA's latest stock price?` 跑 21 輪檢索才輪到 web，而股價根本不是財報內容。
  **不要讓 Planner 自己判**：它手上只有 coverage 摘要、沒有真實 chunk，只能用猜的；且 `_plan_subqueries` 回傳 `list[str]`，格式上就沒有「不檢索」這個選項。猜錯的代價不對稱（猜「沒有」但其實有 → 整題答不出來），所以架構刻意把判斷延後到 Grader 看見真實候選之後——同 [[multi-intent-agentA-is-the-leak]] 的思路。
  **可行方向**：在 todo 層加確定性判斷——`realtime_need == "intraday"` 的子問題（股價、盤中報價）標成 web-first。訊號跟 2026-08-13 加的那個一樣，只是用在更前面一層。**卡點**：要先有觀測性確認 web 這條路本身是好的，否則是把資源導向一條還沒驗證的路。
- **`find_authority_conflicts`（R3 財報優先）對 web 來源全盲**。[`agentic_rag_v2.py`](agentic_rag_v2.py) `_ground_source_type` 只拿 **chunks** 回頭定位數字來源，web 內容從不在 `chunks` 裡 → 從 web 來的宣稱拿不到 `src_types` → R3 的 `if not c.get("src_types"): continue` 直接跳過。2026-08-13 放行「web 數字可引用」之後，這條防線對 web-vs-財報的衝突一格都不設防。
  **但不要直接把 web 塞進 R3**：R3 的規則是「權威（財報）> 新聞」，而 web 觸發的情境恰恰是**時效性衝突**（KB 有舊值、web 有新值），正確答案是新的那個——硬接會讓 R3 系統性地判反，把剛修好的東西再弄壞一次。R3 處理的是「同期間、不同來源」，時效衝突不在它的設計範圍。
  **卡點**：需要的是一條新規則（同指標、值不同、時點不同 → 兩個都保留並標時點），不是改 R3。而且目前沒有量得出它的題目——eval 全程 snapshot、web 恆關，這條路徑在跑分裡永遠不執行。
  **復活條件**：要嘛做出帶 web 的評測情境（需固定 wall clock ＋ 錄下 Tavily 回應當 fixture，否則不可重現），要嘛實跑觀察到具體誤答案例。
- **一致性 validator 的重寫路徑**：偵測 4/4 精準，但重寫實測 **2 好 1 壞**（col-11 修掉矛盾卻把總營收誤標成雲端營收，且三層驗證全過）。選項：改成**只偵測不重寫**（把矛盾標記給使用者看）以拿掉那個 1 壞。
- **router（複雜度分派）**：簡單題→單發、複雜→agentic；保守偏 agentic（誤判複雜為簡單代價高）。ratio 路由屬**正交檢索提示、非第三分支**，應放共用檢索層讓兩條管線都吃到。卡點：measure-gated，目前量尺分不出。
- 生產 `rag_query.py` 是否跟進 `full_translate_en=True`。卡點：要先確認同樣的 chunk-level rerank 平坦問題在單發管線也存在。
- FY 雙向硬 filter、「最新年度」as-of 錨定。卡點：要先跑全量 eval 防回歸。

---

## 想做但量不出來（先別動）

- **`_check_sufficiency` 保留率**：只留 49.3% 候選（18/58 題最後只剩 1 個 chunk），而 `context_recall ↔ correctness` 相關 0.53 是六指標最強。**但「單純放寬候選數」已實測為零效果**（詳見 `docs/EVAL.md` 已試無效總表）。真正該試的是「同樣 5 個席位裝 5 份不同證據」＝**Grader 前確定性去重**。卡點：它動到全部 100 題，效果仍在 MDE 之下，驗不了。
- **`find_claim_conflicts` R2 放寬**（`len(seg_changes) >= 2` → ≥1）。卡點：要先量誤報率——「總計 < 某部門」在另一部門衰退時是**合法的**。
- `GEN_TEMPERATURE=0` 對答案層數字穩定性的影響（目前 0.3）。
- 「近一年／最近一年／過去一年」→ TTM 硬路由（擴充 `_TTM_RE`）；IncomeStatement 財年硬邊界。

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

## 已知缺陷：mi-05（同一個檔案裡撈到錯的 chunk，4/4 穩定）

2026-08-11 三判完成，**gold 沒錯、語料有、檔案也撈到了——撈到的是同一個檔的錯 chunk**：

| | |
|---|---|
| gold | `18.30%`（Revenue Growth YoY） |
| 語料 | `MSFT_Fundamentals_20260612.txt` **#0** 有 `Revenue Growth (YoY): 18.30%`（698 字元） |
| 四個 run 實際撈到 | 同一個檔的 **#1**（Balance Sheet：Total Cash／Total Debt／Debt/Equity／Current Ratio，2045 字元）——**4/4 完全一致** |
| 各 run 答案 | 16%／16-17%／17%／18%（全部改用 10-K/10-Q 的財期數字） |

**不是抽樣變異**（四個 run 同一個結果），是檢索層對「營收成長率」這個 query 穩定挑錯 chunk——而 `Revenue Growth (YoY)` 這個字面就在 #0 裡，sparse/BM25 理應命中。可能與 #1 長三倍有關（rerank 對長 chunk 的偏好），**未驗證，不要當結論**。

⚠ **2026-08-12 更正「4/4 穩定」這個說法**：`ground`／`ground2` 各 3 run 裡都各有 1 次 PASS（撈到 #0）。所以它**不是純檢索層的固定行為，部分取決於 Plan 產生的子問題**。原本寫「不是抽樣變異」是在 4 個 run 的樣本下的推論，樣本擴大到 10 個 run 就被推翻了——結論是「大多數情況撈錯（8/10）」而不是「永遠撈錯」。

**為什麼不能用答案層斷言**（量尺陷阱，記錄下來避免下次踩）：
- `forbid_pct` 不能填 16/17——那些是**合法的財期數字**（10-Q 的季度/累計成長），只是口徑與 gold 的 TTM 不同（同 memory `period-basis-ttm-disambiguation`）。
- `expect_pct: 18.3` 也不行——head 答的「約 **18%**（增加 501 億美元）」是 FY2026 10-K 的財年數字，**數值恰好接近但是不同的量**，容差 ±1pt 下會 PASS，等於**用對的分數獎勵錯的理由**。
- 真正的判別訊號是「**撈到的是 #0 還是 #1**」——那是檢索層，與答案措辭無關。

**已登錄成斷言**（2026-08-11）：`kind: require_chunk`，`require_chunks: [MSFT_Fundamentals_20260612.txt#0]`。四個封存檔實測 **4/4 FAIL**，訊息皆為「同檔撈到 #[1] ← 撈錯 chunk」；正對照（改指實際撈到的 #1）吐 PASS，證明 FAIL 來自被斷言的內容而非機制。索引穩定性已用 exp4／head／period 三個 collection 交叉確認（Fundamentals 都是 3 chunk、#0 是 Company Overview）。

**卡點**：修法未定。要嘛查清為何 sparse/BM25 沒讓 `Revenue Growth (YoY)` 字面命中 #0（懷疑 #1 長三倍造成 rerank 偏好，**未驗證**），要嘛先釐清這題的 gold 口徑該是 TTM 還是財期（那是 eval 設計問題不是系統問題）。

