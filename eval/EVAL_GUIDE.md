# Eval Guide

所有 eval 腳本從 **repo root** 執行（腳本內用相對路徑 `eval/eval_set.json`）。

---

## 主要三支（目前實際使用的 eval pipeline）

| 腳本 | 量測什麼 | 讀 | 寫 |
|---|---|---|---|
| `eval/eval_generation_llm_judge.py` | 回答正確率（LLM-as-judge，5 指標） | `eval_set.json` + Qdrant | `generation_correctness_new_params.json` |
| `eval/eval_two_stage.py` | Chunk recall：精排前（pool）vs 精排後（top-k）兩階段 | `eval_set.json` + Qdrant | `results_new_params.json` |
| `eval/diagnose_crit_miss.py` | Critical-miss 根因分析：逐 checkpoint 拆召回/排序 | `eval_set.json` + Qdrant | `crit_miss_diagnosis.json` |
| `eval/judge_regression.py` | **Judge 本身的回歸測試**（不是評測系統，是評測「評分工具」）：固定案例（含正負對照組）直接呼叫 `evaluate_correctness_with_feedback_voted`，不燒 retrieve/generate 成本。每次改 `_CORRECTNESS_FEEDBACK_SYSTEM` 或換 judge model 前後都應該跑一次。`python eval/judge_regression.py --judge-model <model> --votes <n>` | 無（案例內建於腳本） | 終端機 PASS/FAIL 列印，非 0 exit code |

共同依賴：`rag_query.py`（被測系統本體）、`eval/eval_chunk_recall.py`（checkpoint judge 函式）、`eval/eval_retrieval.py`（file-level recall 工具函式）。

### `--repeat k`（k-run 平均，做 A/B 決策時必用）

`eval_generation_llm_judge.py` 支援 `--repeat k`：跑 k 輪獨立 eval（每輪寫 `{stem}_run{i}.json`、各自可 resume 續跑），彙整結果寫入 `--output`，內含 `category_summary`（各輪 mean、mean_of_means、std_across_runs）與 `per_question`（逐題 scores/mean/std/critical_miss_rate）。

```powershell
python eval/eval_generation_llm_judge.py --category semantic --rewrite --correctness-only \
    --gen-model llama-3.3-70b-versatile --judge-model openai/gpt-oss-20b \
    --repeat 3 --output eval/generation_correctness_semantic_k3.json
```

判讀原則（2026-07 建立，見 `CHANGELOG.md`）：
- semantic 單次跑 mean 的 std_across_runs ≈ 0.07 → **單次差異 <0.1 一律視為噪音**。
- 看逐題 `critical_miss_rate`：**=1.0（k 次全 miss）才是穩定失敗、值得修**；std 大的題（如 0.8/0.8/0.0 型）是 judge/生成噪音，不要對它們優化。
- 實務限制：k=3 會吃掉整天 Groq TPD（100k/day × 4 keys），挑決策節點用；中斷可直接重下同指令 resume。

---

## 其他可用的 eval 腳本

| 腳本 | 用途 |
|---|---|
| `eval/eval_retrieval.py` | Dense vs sparse vs hybrid：P/R/F1/MAP/MRR（targets `us_stock_rag` baseline collection） |
| `eval/eval_rerank.py` | Reranker 效果分析（targets `us_stock_rag_unstructured`） |
| `eval/eval_retrieval_filtered.py` | 帶 hard filter 的 retrieval eval（targets `us_stock_rag`） |
| `eval/eval_retrieval_filtered_unstructured.py` | 同上，targets `us_stock_rag_unstructured` |
| `eval/param_sweep.py` | 掃 FETCH_N × RRF_TOP_N（targets `us_stock_rag`） |
| `eval/param_sweep_chunk.py` | 掃 chunk 相關參數 |
| `eval/hnsw_sweep.py` | 掃 HNSW 索引參數 |
| `eval/compare_generation.py` | 不同 gen 模型的生成品質比較 |
| `eval/compare_generation_tables.py` | 同上，但針對表格查詢 |
| `eval/build_and_compare_table_summary.py` | 表格摘要策略比較 |
| `eval/build_and_compare_table_summary_meta.py` | soft metadata vs hard filter 對比（此分析催生了 hard filter 方案） |
| `eval/poc_unstructured_chunking.py` | unstructured 切塊 PoC |

> **注意**：多數 sweep/比較腳本預設 target `us_stock_rag`（舊 baseline collection），執行前確認各腳本的 `COLLECTION_NAME`。

---

## 標準答案（絕不刪）

| 檔案 | 內容 |
|---|---|
| `eval/eval_set.json` | 61 題（semantic 11 / lexical 15 / mixed 25 / colloquial 10），52 題有 rubric checkpoints |
| `eval/eval_set_business.json` | 商業面向的補充題集 |

---

## 現有結果檔（基準比較用）

| 檔案 | 內容 |
|---|---|
| `eval/generation_correctness.json` | **舊 baseline**：n=51，RRF=40/CAP=5，judge=llama-3.1-8b；overall=**0.809** |
| `eval/generation_correctness_new_params.json` | **新參數**：n=20，RRF=20/CAP=8，judge=gpt-oss-20b；overall=**0.800** |
| `eval/generation_correctness_semantic_k3.json`（+ `_run1/2/3.json`） | **semantic k=3 穩定 baseline**（split-temp：檢索 0 / 生成 0.3，Rule 9）：mean_of_means=**0.545 ± 0.068**；穩定失敗 sem-02/07/08/11，穩定滿分 sem-04 |
| `eval/colloquial_correctness_k3.json`（+ `_run1/2/3.json`） | **colloquial k=3 穩定 baseline**（gen=120b、judge=20b、含 tolerance schema）：mean_of_means=**0.727 ± 0.045**；穩定失敗 col-05/col-08（皆為 sem-08/sem-11 RECALL 層問題的鏡像，非口語特有），灰色地帶 col-04（生成被口語框架帶偏） |
| `eval/generation_correctness_semantic_newprompt.json` | 2026-07-06 溫度實驗系列：新 prompt、temp≈1（0.627） |
| `eval/generation_correctness_semantic_prompt_v2.json` | 同上 + Rule 9、temp≈1（0.582） |
| `eval/generation_correctness_semantic_temp0.json` | 同上、全 temp=0（0.518，greedy 反而更差的證據） |
| `eval/generation_correctness_semantic_split_temp.json` | 同上、檢索 0 / 生成 0.3（0.609，採用為正式設定） |
| `eval/results_new_params.json` | 新參數 chunk recall：rewrite+no-cap，ckpt_R=**0.7307** |
| `eval/results_two_stage.json` | 兩階段 eval 完整版（n=全集） |
| `eval/results_two_stage_20.json` | 兩階段 eval（n=20） |
| `eval/results_two_stage_col.json` / `_70b.json` | 不同 gen 模型的兩階段結果 |
| `eval/crit_miss_diagnosis.json` | 4 題 critical_miss 逐 checkpoint 診斷（召回 vs 排序分析） |
| `eval/sweep_chunk_results.json` | chunk 參數掃描結果 |
| `eval/param_sweep_results.json` / `_unstructured.json` | FETCH_N × RRF_TOP_N 掃描結果 |
| `eval/hnsw_sweep_results.json` | HNSW 參數掃描結果 |
