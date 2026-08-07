# Exp 0 — Baseline

執行日期：2026-07-22
Collection：`us_stock_rag_unstructured`（2367 chunks，現行 pipeline：unstructured partition + section-header 硬邊界 + SemanticChunker，表格獨立 chunk）
生產設定對齊：`gen_model=retrieval_model=openai/gpt-oss-120b`（NVIDIA NIM）、`translate_query_en=True`、`enable_rewrite=False`、`enable_compress=False`（與 rag_query.py 生產預設一致）
Judge model：`openai/gpt-oss-20b`（NVIDIA）
RAGAS judge model：`openai/gpt-oss-120b`（NVIDIA）

## 只保留的 4 項指標

### 1. Chunk 大小統計（reranker tokenizer：BAAI/bge-reranker-v2-m3）

量測對象：`payload["document"]`（實際送進 cross-encoder 的完整文字，含 metadata header）

| 分組 | n | p50 | p90 | p95 | max | >1200 token | >2048（實際截斷）|
|---|---|---|---|---|---|---|---|
| ALL | 2367 | 253 | 1094 | 1487 | 4738 | 8.62% | **1.61%** |
| table | 744 | 259 | 1101 | 1367 | 3448 | 8.74% | 0.81% |
| text | 1623 | 253 | 1083 | 1515 | 4738 | 8.56% | 1.97% |

截斷最嚴重的 10 個 chunk 全是大散文（META_10Q #152 = 4738 tok、META_10K #100 = 4482、AMZN_10K #79 = 3461...），印證「Item 1A/1A/7 散文過大」是主要問題。表格 chunk 也有 0.81% 超標（max 3448）——之後改成 edgartools 三表原子 chunk 不切時要盯住這個數字別惡化。

完整資料：`chunk_size_stats.json`

### 2. Retrieval Recall@k（檔案層級，post-rerank top-5，`context_recall_overlap`）

| 類別 | Recall |
|---|---|
| mixed | 1.000 |
| semantic | 1.000 |
| colloquial | 0.933 |
| lexical | 0.800 |
| news | 0.675 |
| multi_intent | 0.332 |
| **OVERALL** | **0.790** |

multi_intent 偏低是已知的結構性上限（eval_set 設計為 Agentic RAG 對照組，非本次改動目標）。

### 3. Answer Correctness（RAGAS 正式指標）

| 類別 | Correctness |
|---|---|
| news | 0.737 |
| semantic | 0.660 |
| mixed | 0.630 |
| colloquial | 0.566 |
| lexical | 0.476 |
| multi_intent | 0.438 |
| **OVERALL** | **0.582** |

### 4. Faithfulness（RAGAS 正式指標）

| 類別 | Faithfulness |
|---|---|
| news | 0.800 |
| lexical | 0.794 |
| colloquial | 0.768 |
| mixed | 0.756 |
| semantic | 0.741 |
| multi_intent | 0.652 |
| **OVERALL** | **0.752** |

## 附帶量到但非本次追蹤指標

- Context Recall (RAGAS) 0.651 / Context Precision 0.778 / Context Relevance 0.775 — `ragas_scores.json` 的 summary 裡有，供診斷用，不列入跨實驗比較主指標。
- `hallucination_rate`（eval_generation_llm_judge.py 自帶 proxy，非 RAGAS）：OVERALL 0.124，mixed 最高 0.204。**與 RAGAS Faithfulness 相關性很低**（`_corr_vs_selfmade.faithfulness`：lexical 0.06、mixed 0.26、multi_intent -0.09、news -0.02，只有 colloquial/semantic 達 0.6+）——後續判讀「幻覺是否改善」一律以 RAGAS Faithfulness 為準，不與此 proxy 混用。

## 產出檔案

- `chunk_size_stats.json` — chunk token 分布（reranker tokenizer）
- `generation_judge.json` — 90 題完整 retrieve+generate+judge 記錄（含 answer/contexts/sources，供後續 RAGAS 重跑或診斷）
- `ragas_scores.json` — RAGAS 六指標（summary + per_id）

## 踩過的坑（供後續實驗參考）

1. **eval_generation_llm_judge.py / eval_ragas_vs_rubric.py 的 provider 預設是 groq**（遷移 NVIDIA 前的舊值殘留）。已改成預設 `nvidia`（見 diff），但若之後看到 log 有 Groq base_url 或 413 TPM 錯誤，代表又跑到舊行為，先查 `EVAL_LLM_PROVIDER`/`EVAL_JUDGE_PROVIDER` 環境變數有沒有被覆寫。
2. **Windows Git Bash 的 `ps -p <pid>` 對原生 Windows PID 不可靠**，會誤判存活 process 為已結束。判斷背景 process 是否結束，用「輸出檔案是否出現」或 `powershell Get-CimInstance Win32_Process`，不要用 `ps -p`。
3. 90 題全量 pipeline（含 CPU rerank + 多次 LLM 判定）約需 40–60 分鐘；RAGAS 六指標另需本地 embedding 運算（CPU-bound），耗時相近，且輸出在完成前不會有任何 print（無法用 log 判斷進度，只能等檔案落盤）。
