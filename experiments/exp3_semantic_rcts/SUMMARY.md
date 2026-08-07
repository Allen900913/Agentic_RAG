# Exp 3 — Semantic Chunking + RCTS Fallback

執行日期：2026-07-23
Collection：`us_stock_rag_edgar_exp3`（3383 chunks）
方法：Item boundary → SemanticChunker → 若 chunk 超過 1200 reranker token → RCTS（800/80，
length_function=reranker tokenizer）補切
實作：`data_update_edgar.py --rcts-fallback`

## Chunk 大小統計對照（reranker tokenizer）

| 指標 | Exp 0 baseline | Exp 2（純 Semantic）| Exp 3（Semantic+RCTS）|
|---|---|---|---|
| n | 2367 | 2974 | 3383 |
| p50 | 253 | 223 | 254 |
| p90 | 1094 | 980 | 803 |
| p95 | 1487 | 1306 | 875 |
| **max** | 4738 | 6516 | **1290** |
| **>1200 token** | 8.62% | 6.56% | **0.15%** |
| **>2048（實際截斷）** | 1.61% | 1.55% | **0.0%** |
| text max | 4738 | 6516 | **1208** |
| table max | 3448 | 1290 | 1290（不受影響）|

## 結論

1. **RCTS fallback 完全達成設計目標**：oversized chunk 尾端風險（Exp 2 驗證仍存在，max 到 6516）被完全壓制，text chunk max 降到 1208，>2048 截斷率降到 **0%**。
2. **chunk 數量增加可控**：3383 vs Exp 2 的 2974（+13.7%），全部增量來自被補切的少數 Item（NVDA 單一 filing 觸發 23 次補切，屬於合理範圍，未過度碎片化）。
3. **財報三表不受影響**（max 維持 1290，設計上就不進 RCTS），仍在 2048 安全範圍內。

## 下一步

對此 collection 跑全量 90 題 generation_judge + RAGAS，與 Exp 2（純 Semantic）的 Faithfulness / Answer Correctness 正式對比，驗證「消除 oversized chunk」是否真的轉化成生成品質提升，而不只是 token 分布數字好看。
