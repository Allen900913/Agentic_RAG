# Agentic RAG（LangGraph 版）vs Exp0-4 chunking 管線

**日期**：2026-07-24
**跑法**：`eval/run_agentic_on_evalset.py`（`agentic_rag_nv.py`，LangGraph，僅 Planner→Retriever→Sufficiency Checker→Generator）
**底座**：collection = `us_stock_rag_edgar_exp4`（與 Exp4 完全相同 chunking，唯一差別是編排/decomposition）
**題數**：eval_set.json 90 題（news/multi_intent/semantic/mixed/lexical/colloquial 各 15），0 error
**模型**：retrieval/checker = `openai/gpt-oss-120b`，generation = `z-ai/glm-5.2`
**RAGAS 評分模型**：`openai/gpt-oss-120b`（非 glm）

## OVERALL 六指標並排

| 指標 | Exp0 | Exp2 | Exp3 | Exp4 | **Agentic** |
|---|---|---|---|---|---|
| Context Recall | 0.651 | 0.529 | 0.555 | 0.599 | **0.623** |
| Context Precision | 0.778 | 0.716 | 0.709 | 0.756 | **0.636** |
| NV Context Relevance | 0.775 | 0.819 | 0.814 | 0.844 | **0.856** |
| Faithfulness | 0.752 | 0.753 | 0.777 | 0.794 | **0.902** |
| Answer Relevancy | 0.678 | 0.718 | 0.724 | 0.721 | **0.793** |
| Answer Correctness | 0.582 | 0.532 | 0.526 | 0.522 | **0.468** |

（來源：`experiments/exp{0,2,3,4}*/ragas_scores.json` + `experiments/agentic/ragas_scores.json` 的 `summary._overall`）

## 解讀

**贏在答案品質三指標，全表最高：**
- Faithfulness 0.902（+0.108 vs Exp4）— 逐子問題檢索讓答案更緊貼 context，幻覺最少。
- Answer Relevancy 0.793（+0.072）、NV Context Relevance 0.856 — 全表新高。
- Context Recall 0.623 > Exp4 0.599 — 子問題分解撈更廣的 chunk 池，recall 回升（仍略低於 Exp0 baseline 0.651）。

**代價（可解釋，非壞掉）：**
- Context Precision 0.636（-0.120 vs Exp4）— agentic 把各子問題撈到的 chunk 取聯集當 contexts，池子更大更雜，是 recall↑ 換 precision↓ 的典型 trade-off。
- Answer Correctness 0.468（全表最低）— RAGAS answer_correctness 主要量「長度對齊」而非事實正確（見專案記憶 `ragas-correctness-length-artifact`），agentic 答案明顯更長更完整（常見 1500–2200 字），越長越被扣分。分類別看 `multi_intent` 僅 0.396（複合題答案最長）印證此點，不代表事實變差。

**結論**：agentic 編排用「答案更忠實、更相關、recall 更廣」換「contexts 更雜、correctness 假象更低」。在此封閉語料產品上，faithfulness/relevancy 全面領先是實打實的收穫。

## 分類別明細

| category | n | context_recall | context_precision | nv_context_relevance | faithfulness | answer_relevancy | answer_correctness |
|---|---|---|---|---|---|---|---|
| colloquial | 15 | 0.614 | 0.699 | 0.867 | 0.866 | 0.751 | 0.477 |
| lexical | 15 | 0.617 | 0.750 | 1.000 | 0.894 | 0.897 | 0.411 |
| mixed | 15 | 0.547 | 0.535 | 0.933 | 0.873 | 0.810 | 0.444 |
| multi_intent | 15 | 0.478 | 0.332 | 0.517 | 0.919 | 0.744 | 0.396 |
| news | 15 | 0.768 | 0.733 | 0.917 | 0.903 | 0.776 | 0.568 |
| semantic | 15 | 0.714 | 0.768 | 0.900 | 0.960 | 0.780 | 0.509 |

`multi_intent` 的 context_precision（0.332）與 nv_context_relevance（0.517）明顯偏低——複合題會拆多個子查詢各自檢索，聯集池子最雜，precision 代價在此類別最集中。

## 執行紀錄（供複現參考）

- 過程中兩度出現重複 process 同時寫 `generation_judge.json`（不同背景 task 各自 resume），互相覆寫進度（61→59），已排除、改為單一 process resume。
- `news-06` 因 NVIDIA 端點 504 逾時，重試一次後成功。
- 複現：
  ```
  .venv/Scripts/python.exe -u eval/run_agentic_on_evalset.py
  .venv-ragas/Scripts/python.exe eval/eval_ragas_vs_rubric.py \
      --from-results experiments/agentic/generation_judge.json \
      --output experiments/agentic/ragas_scores.json
  ```
