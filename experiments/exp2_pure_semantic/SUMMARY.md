# Exp 2 — 純 Semantic Chunking（Item boundary → SemanticChunker，無 size cap）

執行日期：2026-07-23
Collection：`us_stock_rag_edgar_exp1`（2974 chunks，Exp 1 產出的乾淨資料，散文切法本身即 Exp 2 定義的方法）
量測方式：`eval/chunk_size_stats.py`，reranker tokenizer（BAAI/bge-reranker-v2-m3）

## 對照 Exp 0 Baseline

| 指標 | Exp 0（unstructured heuristic header）| Exp 2（edgartools Item boundary）|
|---|---|---|
| ALL n | 2367 | 2974 |
| ALL p50 | 253 | 223 |
| ALL p90 | 1094 | 980 |
| ALL p95 | 1487 | 1306 |
| ALL max | 4738 | 6516 |
| ALL >1200 | 8.62% | 6.56% |
| ALL >2048（截斷）| 1.61% | 1.55% |
| table max | 3448 | 1290 |
| table >2048 | 0.81% | 0.0% |
| text max | 4738 | 6516 |
| text >2048 | 1.97% | 1.58% |

## 結論

1. **財報三表截斷問題已解決**：改用 edgartools `Statement.to_markdown()` 後，table chunk 的 >2048 截斷率從 0.81% 降到 0%，max 從 3448→1290。範圍限定在 income/balance sheet/cash flow 三張核心報表；文件裡其他表格（如 segment 明細）現在併入散文，走 Item+Semantic 路徑，不是嚴格同範圍比較。

2. **Item 邊界對散文有實質但有限的幫助**：整體分布（p50/p90/p95/>1200 比例）都比舊 heuristic header 好，證實「用 SEC 官方 Item 邊界取代啟發式偵測」的方向正確。但**最壞情況（max、>2048 截斷率）沒有被根本解決**——`META_10K_2025.html` 的 Item 8（財務報表附註）產出一個 6516 token 的單一 chunk，比 Exp 0 baseline 的最大值還高。原因：Item 邊界只保證「不同章節不混在一起」，章節內部若語意同質性高（如冗長的附註逐條列示），SemanticChunker 沒有大小上限時仍可能判斷整段語意連貫、不切開。

3. **驗證了 Exp 3 的必要性**：純 Semantic Chunking（即使邊界乾淨）不能保證消除 oversized chunk 尾端風險，必須加 RCTS fallback 兜底。

## Exp 1 → Exp 2 過程中發現並修正的 3 個 edgartools 資料品質問題

1. TSLA 誤抓 10-K/A 修正案（缺 XBRL、檔名判斷錯誤）→ 加 `amendments=False`
2. 6/7 家 10-K 的 `Item 8` 在 `obj.items` 清單裡被重複列兩次 → `dict.fromkeys()` 依名稱去重
3. 6/7 家 10-Q 都多吐出不存在於官方表格結構的偽 `Part I, Item 8`（內容與 Item 1/2 部分重疊）→ 改用 SEC 官方 Item 白名單過濾

另確認 META 的 Item 3（法律訴訟）與 Item 8（財報附註）之間 38 組小段內容重疊，經人工檢查是真實文件的合法交叉揭露（會計準則要求財報附註複述訴訟揭露內容），非 bug，未處理。

## 產出檔案

- `../exp1_edgartools/`：NVDA/TSLA 財報三表樣本、edgartools API 驗證紀錄
- `chunk_size_stats.json`：完整 token 分布數據
- `data/edgar_processed/*.txt`：21 份 filing 的 Item 結構與財報三表人工驗證 dump

## 下一步：Exp 3

Semantic Chunking → 超過 1200 token 時用 RCTS fallback（800/80，length_function 用 reranker tokenizer）。重點驗證：oversized chunk 是否消失、截斷率是否趨近於零、chunk 數量是否過度增加。
