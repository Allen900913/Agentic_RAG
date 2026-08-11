# BACKLOG — 未做 / 待決 / 已接受的極限

> **這裡放什麼**：還沒做的事、做了但沒定案的事、以及查清楚後決定**不修**的極限。
> **這裡不放什麼**：已完成的改動（→ `CHANGELOG*.md`）、知識與踩坑（→ `docs/`）。
>
> **維護紀律**：一件事做完就從這裡**刪掉**並寫進 `CHANGELOG*.md`；查清楚決定不做就搬到最後一節「已接受的極限」並寫下**復活條件**。
> 每一條都要能回答「**卡在哪**」——沒有卡點的條目不是 backlog，是願望。

---

## 立即可做（不需重建、不燒額度）

- **把 mix-09 登錄成 `number_claims.json` 斷言**。它已端到端確診（head 讀不到 22%、退回引用新聞的 28%），修法也已進碼（幅度接地），但**還沒有斷言守著**。⚠ `known_defect` 必須填 `forbid_pct`（28），否則量尺沒有判別力。
- **mix-07 登錄成斷言或已知限制**：Plan 把混合題整個譯成新聞查詢 → 只撈到 News chunk → 拒答。**已證實與 collection 無關**（period 自己的 replay2 同樣拒答）。卡點：這是 plan 抽樣變異，斷言可能不穩定，要先量三個 run。
- **mi-05 人工三判**：gold 的 18.3% 不在那個 run 撈到的 contexts 裡。卡點：要先確認是檢索缺口還是 gold 標錯。
- **`fetch_data.py` 尚未納入版控**。真實風險：它是唯一對外抓取入口且含 `_pct`，未來重抓會**靜默還原**小數→百分比的遷移。
- **`eval/replay_cache.json` 有未提交的新增項**（+25 translate_en、+141 check，0 筆覆寫）。要決定納不納版控。

---

## 待重建才驗得到（ingest 期改動已進碼，2026-08-11）

三個改動都已提交、**尚未重建**，建議一次到位（新 collection 名稱建議 `us_stock_rag_edgar_ground`）：

1. 幅度接地（`_merge_unquantified_sections`）— 零網路驗收已過（AAPL 孤兒段 31→0）
2. 表格 caption 五修（Groq 70B、給前後文、頁碼不再擋 LLM、壓分隔符、429 退避）
3. 通用小標層（08-09 已進碼，`exp4` 也還沒套用）

重建後的驗收順序（**先確定性、後聚合**）：`verify_segment_split.py` 判準⑤ → caption 分類探針 → `check_number_defects.py` 斷言 → 最後才 RAGAS（當「沒崩壞」的粗閘門，±0.05）。

---

## 未定案的決策

- **`us_stock_rag_edgar_head` 是否升生產**：mix-03 確定修好（三個 run FAIL → PASS），但同 fixture 對照下 correctness **−0.032**、context_recall **−0.039**（皆略超 MDE）。**非 AAPL 的 42 題退步目前沒有已證實的解釋**——「證據量變少」的機制已被 2026-08-05 的 pool 實驗推翻。卡點：等幅度接地重建後重新比。
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
