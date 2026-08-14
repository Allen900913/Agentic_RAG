# BACKLOG — 未做 / 待決 / 已接受的極限

> **這裡放什麼**：還沒做的事、做了但沒定案的事、以及查清楚後決定**不修**的極限。
> **這裡不放什麼**：已完成的改動（→ `CHANGELOG*.md`）、知識與踩坑（→ `docs/`）。
>
> **維護紀律**：一件事做完就從這裡**刪掉**並寫進 `CHANGELOG*.md`；查清楚決定不做就搬到最後一節「已接受的極限」並寫下**復活條件**。
> 每一條都要能回答「**卡在哪**」——沒有卡點的條目不是 backlog，是願望。

---

## 立即可做（不需重建、不燒額度）

- ~~**`--trace` 不印 web 回傳內容，只印字數**~~ → **2026-08-13/14 兩階段做完**。現在逐則印**網址＋發布日＋標題＋摘要全文**。兩次都立刻換來一個原本看不見的根因：①白名單把 `apps.apple.com` 收進來（35 筆有 33 筆是消費端頁面）；②`content[:300]` 把數字截掉（`as of Augus` ← `as of August 07, 2026 is $4572.79B`）。**「只印網域不印網址」也踩過一次**——看不出 Reuters 的日期為何抽不到（日期在網址結尾），只好另外打 API 去問。
- **檢索側 LLM 用 20b 還是 120b，從來沒量過**（2026-08-14 提出）。單發管線的 `--retrieval-model` 預設繼承 `rq.DEFAULT_MODEL`＝`gpt-oss-120b`，那**不是選型結論，是沒人動過的預設**（該旗標當初是為了把檢索側與生成側拆開做 A/B 才加的）；agentic 則刻意分層用 `gpt-oss-20b`（[`agentic_rag_v2.py`](agentic_rag_v2.py) `RETRIEVAL_MODEL` 註解：機械型輕量呼叫求快）。翻遍 `CHANGELOG_AGENTIC.md` **找不到任何檢索側的 20b vs 120b 對照**，而且那段註解自己說理由是「求快」、下一句又說「與 120b 同樣快」。20b 已知的兩個弱點（大 schema tool-call parse 失敗、當 validator 太弱）都是**推理**任務，不能直接套到 filter 抽取與翻譯上。
  **這件事不必動 RAGAS，可以零噪音量**：檢索側 LLM 的輸出是結構化的 → ①`parse_query_filters()` 回 JSON，100 題各跑兩個模型**逐題比對是否相同**，不同的再看誰對；②`translate_query_en()` 回英文字串，不同的那幾題把兩個版本各送進檢索器**比 gold chunk 的 rank**（sem-11 當初就是這樣量的：raw 0.39 → 翻英文後 0.94）。溫度 0、很便宜。
  **順帶解掉另一件事**：2026-08-14 的單發 vs agentic 對照有一個未控制變因就是這個（見 [`docs/EVAL.md`](docs/EVAL.md)〈分類別抵銷效應〉判讀限制②）。若 20b 與 120b 在這兩項上無差異，單發就該降到 20b——省錢、兩臂前處理一致、順便把那個判讀限制消掉。
- **`eval/replay_cache.json` 有未提交的新增項**（+25 translate_en、+141 check，0 筆覆寫）。要決定納不納版控。
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
- **「最新期間」路由的覆蓋面，會隨 KB 累積年份惡化**（2026-08-14 提出）。病灶本身**已實證且已部分修好**：cross-encoder 分不出同主題 chunk 是哪一季（mix-06 probe：舊季 0.70~0.73 與最新季幾乎並列，最新季 gold 被壓到 rank 7/13/17、掉出 top-5 → context_recall=0），[`rag_query.py`](rag_query.py) `_LATEST_QUARTER_TIME_RE` 那條 deterministic 路由就是治本修法——掃 collection 解出最新 10-Q 期碼、注入 `report_period_code` 硬 filter。**但它有三個破口，且排序層完全沒有時間先驗**（[`rag_query.py:1429`](rag_query.py#L1429) 純 `raw_rerank_score` 排序）：
  ① **觸發靠硬編碼詞表**（`最近|最新|近期|…`）——「蘋果賣得怎麼樣」語意上問現況卻不含任何詞，漏掉就退回純語意排序。這正是 CLAUDE.md 警告的「用字串比對做感知」。
  ② **只覆蓋 10-Q**：10-K 由 `_ANNUAL_INTENT_RE` 明確讓路，News／Fundamentals 無等價機制。
  ③ **限「恰好一家公司」**：多公司比較題不路由。
  **修法方向**（照 CLAUDE.md 的 LLM/Python 分工）：「這題問的是不是最新期間」沒有唯一機械答案 → 交 LLM 判；「最新期間是哪個期碼」是確定性的 → Python 從 `agentic_rag_v2._scan_kb_coverage()` 的 coverage map 算（它已經每個 ticker、每種 doc_type 都存了最新記錄）。多公司改成逐 ticker 各自解期碼（`MatchAny`）而不是放棄路由。
  **卡點＝現在不可證偽**：KB 每家只有 1 份 10-K ＋ 2~3 份 10-Q ≈ **一年**，「會不會抓到舊資料」缺乏能發作的測資，現在做就是投機建設。**復活條件：KB 納入第二個年度的 filing 時**（屆時同公司同主題 chunk 會有 6~8 份跨期競爭），先用 chunk-level probe 量最新期 gold 的 rank 分佈，再決定改詞表還是加排序層時間先驗。

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

