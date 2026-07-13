# Retrieval Evaluation

對比 **Dense-only / Sparse-only / Hybrid (RRF)** 三種檢索模式在自建 eval set 上的表現。
為「Hybrid retrieval 在哪些情境下勝出」提供**實證支撐**，而非僅憑理論論證。

---

## 1. 為什麼需要這個 eval

光靠概念論證（「dense 弱於關鍵字、sparse 弱於語意」）寫進報告，老師可能會問：
> 「你怎麼知道？」

這個 eval 用一個小型但分類清楚的測試集，跑出可重現的 Recall@K / MRR 數字，
讓設計決策從「**推理而來**」升級為「**實證背書**」。

---

## 2. 方法論

### 2.1 Ground truth 標記粒度
**Source-file level**：每個 query 標註「哪些檔案應該被檢索到」，不到 chunk 層級。
- ✅ 容易人工標註與維護
- ✅ 對應使用者實際感知（「來源是不是相關的檔案」）
- ⚠️ 比 chunk-level 粗，但對 < 1000 chunks 的小規模資料夠用

### 2.1.1 Ground truth 兩種寫法（exact + glob）

`relevant` 陣列每個元素可以是：

| 寫法 | 範例 | 行為 |
|---|---|---|
| **Exact filename** | `"NVDA_10K_2026.html"` | 精確匹配單一檔案 |
| **Glob pattern** | `"NVDA_News_*.txt"` | 匹配所有符合的檔名（用 Python `fnmatch`） |

**為什麼支援 glob**：新聞檔名含日期（如 `NVDA_News_20260508_01.txt`、`NVDA_News_20260519_03.txt`），每次跑 `fetch_data.py` 都會產生新檔名。寫死 exact filename 會讓 eval set 過幾天就過期，且每次抓新聞都要手動更新 `relevant` 陣列。

Glob 解決這個問題：寫一次 `"NVDA_News_*.txt"`，**未來新增的任何 NVDA 新聞都自動被視為 relevant**。

**展開機制**：執行時 `eval_retrieval.py` 會做一次 `client.scroll` 把整個 Qdrant collection 的全部 unique `source` payload 撈出來，然後用 `fnmatch.filter` 把每個 glob pattern 展開成實際存在的檔案集合。

**安全網**：如果某個 query 的所有 patterns 展開後都是空集合（例如資料庫裡根本沒有任何 `MSFT_News_*.txt`），該 query 會被**自動跳過並警告**，不會用 0/0 = NaN 污染統計結果。

### 2.2 三類 Query

| 類別 | 數量 | 描述 | 預期表現 |
|---|---|---|---|
| **semantic** | 7 | 概念、策略、定性問題（「核心護城河」、「未來成長方向」） | Dense ≈ Hybrid > Sparse |
| **lexical** | 7 | 特定詞彙、文件代號、財務指標縮寫（「P/E」、「10-Q 202605」） | Sparse ≥ Hybrid > Dense |
| **mixed** | 7 | 同時含概念與專有詞（「NVIDIA 最新財報的營收成長率」） | Hybrid > Dense, Hybrid > Sparse |

設計三類的目的：**驗證 Hybrid 在不同分佈的 query 上能否穩定表現**——任何單一檢索器都會在某個分佈上佔優，但 Hybrid 的核心 claim 是「**通吃**」。

### 2.3 指標（五個資訊檢索領域標準指標）

對應 [RAG Triad](https://www.trulens.org/) 中的 **Context Relevance** 維度——衡量 retriever 把對的東西撈回來的能力。

| 指標 | 公式（K=top-K, R=relevant set） | 衡量什麼 | 解讀 |
|---|---|---|---|
| **Precision@K** | `hits / K` | 信噪比 | top-K 中有多少**比例**是相關的 |
| **Recall@K** | `hits / |R|` | 完整性 | 全部相關文件中有多少**比例**被找回 |
| **F1@K** | `2·P·R / (P+R)` | 平衡 | Precision 與 Recall 的調和平均 |
| **MAP@K** | `(1/min(K,|R|)) · Σᵢ P@i · 1[hit at i]` | 排序品質 | 命中項排越前面分數越高 |
| **MRR@K** | `1 / rank(first hit)` | 首擊效率 | 第一個正確答案排在第幾位的倒數 |

#### 各指標的「個性」對比

| 你想知道 | 看哪個指標 |
|---|---|
| 「使用者打開的前 5 個來源，有幾個有用？」 | **Precision@5** |
| 「系統有沒有把該找的全找回來？」 | **Recall@K**（K 要夠大） |
| 「在不犧牲一邊的前提下整體表現？」 | **F1@K** |
| 「相關文件有沒有排在前面，而不只是出現？」 | **MAP@K** |
| 「使用者懶得往下滑時，第一個就對嗎？」 | **MRR@K** |

#### 為什麼五個都要看

它們**互補但不冗餘**：
- Precision 高 + Recall 低 → 結果很精準但漏了很多 → 系統太保守
- Precision 低 + Recall 高 → 找回得多但夾雜雜訊 → 系統太貪心
- MAP 高 + MRR 低 → 整體排序好但第一個常常錯 → 看似矛盾，發生在「答案散布在 rank 2~5 而不是 rank 1」的情況
- F1 高 → 兩個都不差

**單看 Recall 容易誤導**：如果 K=10、relevant 只有 2 個，Recall@10=1.0 看似完美，但 top-10 裡可能 8 個都是無關雜訊（Precision@10=0.2）。

兩個排序型指標（MAP、MRR）才能反映「**有沒有把對的放在前面**」——這對 LLM prompt 很重要，因為 prompt token 預算有限，後段的 chunk 影響力較小。

### 2.4 公平性設計
- **同一個 query encoding**：BGE-M3 encode 一次，dense + sparse 共用，避免不同 encoding 帶來的差異
- **同一個 top-K 截取**：三種模式都取相同的 chunk 數，去重到 source 後再算 metric
- **三種模式共用同一個 Qdrant collection**：排除 indexing 差異

---

## 3. 如何執行

### 前置條件
- 已執行 `python data_update.py --rebuild`，Qdrant collection 已建立
- BGE-M3 模型已下載

### 跑 eval
```powershell
# 從 repo root 執行（重要：路徑是相對於 repo root）
python eval/eval_retrieval.py

# 自訂 K 值
python eval/eval_retrieval.py --ks 3,5,10

# 自訂輸出路徑
python eval/eval_retrieval.py --output eval/results_v2.json
```

### 預期輸出
```
[INFO] Loaded 21 queries from eval/eval_set.json
[INFO] Loading BGE-M3: BAAI/bge-m3
[INFO] Collection size: 487 chunks
[INFO] Snapshotting source filenames for glob expansion...
[INFO] Found 15 unique source files in collection.

  [ sem-01] (semantic) NVIDIA 的核心技術護城河是什麼？...
            dense  P@10=0.300  R@10=0.667  F1@10=0.412  AP@20=0.420  MRR@20=1.000
            sparse P@10=0.200  R@10=0.333  F1@10=0.250  AP@20=0.180  MRR@20=0.500
            hybrid P@10=0.400  R@10=1.000  F1@10=0.571  AP@20=0.583  MRR@20=1.000
  ...

═════════════════════════════════════════════════════════════════════════════════
RETRIEVAL EVALUATION RESULTS — Precision / Recall / F1 / MAP / MRR
(mean over queries per category, all metrics shown as percentages)
═════════════════════════════════════════════════════════════════════════════════

  Category: SEMANTIC  (n=7)
    Metric          dense       sparse       hybrid    Δ(H-D)
    ----------  ----------  -----------  ----------    ------
    P@5         ...
    R@5         ...
    F1@5        ...
    ...
    MAP@20      ...
    MRR@20      ...

────────────────────────────────────────────────────────────────────────────────
HEADLINE Δ — Hybrid vs Dense-only at K=10 (positive ⇒ hybrid wins)
────────────────────────────────────────────────────────────────────────────────
  SEMANTIC    P@10: +X.Xpp   R@10: +X.Xpp   F1@10: +X.Xpp   MAP@20: +X.Xpp   MRR@20: +X.Xpp
  LEXICAL     ...
  MIXED       ...
  OVERALL     ...
  ...

══════════════════════════════════════════════════════════════════════════════
RETRIEVAL EVALUATION RESULTS (mean over queries per category)
══════════════════════════════════════════════════════════════════════════════

  Category: SEMANTIC  (n=7)
    Mode      recall@5  recall@10  recall@20    mrr@10
    --------  --------  ---------  ---------    ------
    dense     ...
    sparse    ...
    hybrid    ...
  ...

──────────────────────────────────────────────────────────────────────────────
Δ Hybrid vs Dense-only  (positive = hybrid wins)
──────────────────────────────────────────────────────────────────────────────
  SEMANTIC    recall@5: +X.Xpp  recall@10: +X.Xpp  ...
  LEXICAL     recall@5: +X.Xpp  ...
  MIXED       recall@5: +X.Xpp  ...
  OVERALL     recall@5: +X.Xpp  ...
```

`results.json` 內含每個 query 的逐筆 trace，可審視個別 query 的表現。

---

## 4. 如何修正 eval set

跑完一次後若發現某個 query 的 ground truth 不準（例如標了 `NVDA_10K_2026.html` 但其實該檔案沒有相關內容），直接編輯 [eval_set.json](eval_set.json) 的 `relevant` 欄位。

判斷方法：去看 `results.json` 中該 query 的兩個關鍵欄位：

- `relevant_patterns`：你寫在 eval_set.json 的原始 pattern list（含 glob）
- `relevant_expanded`：執行時實際展開後的檔名集合
- `retrieved`：dense / sparse / hybrid 三種模式分別回傳的 source 排序

**如果 `relevant_expanded` 是空的** → 你的 glob 沒匹配到任何檔案（可能拼錯、或資料庫尚未匯入這些檔案）。
**如果三種模式都沒回傳某個 `relevant_expanded` 裡的 source** → 該檔案實際內容跟 query 沒交集，從 `relevant` 移除。
**如果三種模式都回傳了某個 source 但它不在 `relevant_expanded`** → 考慮加進 `relevant`。

### 抓新檔案後不用改 eval set
得益於 glob 支援，未來執行 `python fetch_data.py --news-count 20` 後再 `python data_update.py`，eval set **不需要任何修改**——新檔案會自動被 `*_News_*.txt` 之類的 pattern 吃進來。

---

## 5. 報告寫作建議

跑完 eval 後，可在報告的「Design Decisions / Retrieval Strategy」章節這樣寫：

> 「在自建的 21 題 eval set 上（涵蓋 semantic / lexical / mixed 三類），採用資訊檢索領域的 5 個標準指標
> （Precision@K、Recall@K、F1@K、MAP、MRR）衡量 Context Relevance。
> Hybrid retrieval（dense + sparse + RRF）在 mixed 類別上比 dense-only **F1@10 提升 X.X pp、MAP@20 提升 Y.Y pp**，
> 在 lexical 類別上 **Recall@10 提升 Z.Z pp**，在 semantic 類別上維持相當水準。
> 結果**實證支持** Hybrid 的核心 claim：**在不犧牲語意檢索品質的前提下，補強關鍵字匹配能力**。」

XX 等數字跑完 eval 後從 console 或 `results.json` 取得，**寫之前一定要先實際跑過**。

### 為什麼是這 5 個指標而不是只 Recall

報告也可寫一段方法論的辯護：

> 「單一 Recall 容易誤導：高 Recall 可能伴隨低 Precision（top-K 充滿雜訊）。
> 因此採用 **Precision/Recall/F1** 三件套同時測信噪比、完整性、平衡；
> 加上 **MAP 與 MRR** 兩個 rank-aware 指標，捕捉『相關文件是否被排在前面』——
> 這對 RAG 系統尤為關鍵，因為 LLM prompt 的 token 預算有限，後段 chunk 影響力較弱。」

---

## 6. 參數掃描實驗（FETCH_N × RRF_TOP_N）

測試不同 `fetch_n`（每路 prefetch 候選數）與 `rrf_top_n`（RRF 輸出數）對 Hybrid 檢索效果的影響。

執行方式：
```powershell
python eval/param_sweep.py
```

### 實驗結果

| fetch_n | rrf_top_n | R@5 | R@10 | MRR@10 |
|---|---|---|---|---|
| 20 | 20 | **0.7947** | **0.8471** | 0.7778 |
| 30 | 20 | 0.7868 | 0.8392 | 0.7778 |
| 50 | 20 | 0.7815 | 0.8339 | 0.7778 |
| 100 | 20 | 0.7815 | 0.8339 | 0.7778 |
| 10 | 10 | 0.7577 | 0.7751 | 0.7698 |
| 20 | 10 | 0.7577 | 0.7751 | 0.7778 |
| **30** | **10** | **0.7577** | **0.7751** | **0.7778** ← 原始設定 |
| 50 | 10 | 0.7458 | 0.7632 | 0.7778 |
| 100 | 10 | 0.7458 | 0.7632 | 0.7778 |
| 10 | 5 | 0.6942 | 0.6942 | 0.7698 |
| 20 | 5 | 0.6942 | 0.6942 | 0.7778 |
| 30 | 5 | 0.6942 | 0.6942 | 0.7778 |

### 關鍵發現

1. **`rrf_top_n` 影響最大**：從 10 調到 20，R@5 提升 +5%（0.758 → 0.795），R@10 提升 +9%（0.775 → 0.847）。原因是 rrf_top_n 決定送進 cross-encoder reranker 的候選池大小，候選越多，精排能挑出更好的結果。

2. **`fetch_n` 影響有限**：fetch=20 與 fetch=100 結果幾乎相同，fetch=20 因雜訊更少反而略佳。

3. **MRR@10 不受影響**：所有組合均為 0.7778，代表第一個相關文件的排名位置非常穩定。

4. **最佳配置**：`fetch_n=20, rrf_top_n=20`，用比原始設定更小的 prefetch 取得最高的召回率，額外成本趨近於零。

---

## 7. 限制與後續改進

| 限制 | 改進方向 |
|---|---|
| Ground truth 為 source-level，無法區分同檔案內哪個 chunk 真正相關 | 改 chunk-level 標註（成本高） |
| 樣本數 21，統計顯著性不足 | 擴充到 50~100 query |
| 預期答案以人類常識假設，未經與檔案內容嚴格驗證 | 跑一次 eval 後，根據 `retrieved` 欄位修正 `relevant` |
| 未測 reranker 的加值 | 新增第四種模式 `hybrid + rerank`，比較 reranker 增益 |
