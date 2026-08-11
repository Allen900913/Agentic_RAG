# BACKLOG — 未做 / 待決 / 已接受的極限

> **這裡放什麼**：還沒做的事、做了但沒定案的事、以及查清楚後決定**不修**的極限。
> **這裡不放什麼**：已完成的改動（→ `CHANGELOG*.md`）、知識與踩坑（→ `docs/`）。
>
> **維護紀律**：一件事做完就從這裡**刪掉**並寫進 `CHANGELOG*.md`；查清楚決定不做就搬到最後一節「已接受的極限」並寫下**復活條件**。
> 每一條都要能回答「**卡在哪**」——沒有卡點的條目不是 backlog，是願望。

---

## 立即可做（不需重建、不燒額度）

- **`eval/replay_cache.json` 有未提交的新增項**（+25 translate_en、+141 check，0 筆覆寫）。要決定納不納版控。
- **`us_stock_rag_edgar_exp4` 的 Fundamentals 比率還是小數**（`Revenue Growth (YoY): 0.183`），`head`／`period` 已是 `18.30%`——`migrate_fundamentals_pct.py` 沒套到 exp4。卡點：要先決定 exp4 的角色。**CLAUDE.md 寫它是「生產 collection」、本檔〈環境雜務〉寫它是「歷史基準」，兩者矛盾**；若是歷史基準就該留著不動並改 CLAUDE.md，若是生產就得補遷移。（2026-08-11 scroll 三個 collection 時發現）

---

## 未定案的決策

- **`us_stock_rag_edgar_ground2` 是否升生產**（2026-08-12 重建完成，四個 ingest 修法全部生效：期間章節／通用小標／幅度接地（chunk 層）／表格 caption）。確定性閘門全綠、四條斷言 9/12（`period` 5/12、`head` 1/4、`ground` 7/12）。**卡點：等全量 100 題 ＋ RAGAS 的 ±0.05「沒崩壞」對照**（`gj_ground2_full100_20260812`，掛共用 replay fixture 與 `gj_v2_period_replay1` 可比）。順帶要決定 `exp4`／`head`／`period`／`ground` 這幾個舊 collection 留哪些。
- ~~`us_stock_rag_edgar_head` 是否升生產~~ → 已被 `ground2` 取代。當初卡在「非 AAPL 的 42 題 correctness −0.032／recall −0.039 沒有已證實的解釋」，等新的全量對照出來再看這個差距還在不在。
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

