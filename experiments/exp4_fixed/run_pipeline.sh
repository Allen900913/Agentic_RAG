#!/bin/bash
set -e
cd /d/hw3-build-your-personal-rag-Allen900913

echo "=== [1/4] Full 7-ticker ingest into us_stock_rag_edgar_exp4 (fixed pipeline) ==="
.venv/Scripts/python.exe data_update_edgar.py --collection us_stock_rag_edgar_exp4 --rebuild --rcts-fallback

echo "=== [2/4] chunk_size_stats ==="
.venv/Scripts/python.exe eval/chunk_size_stats.py --collection us_stock_rag_edgar_exp4 --out experiments/exp4_fixed/chunk_size_stats.json

echo "=== [3/4] generation_judge (90 questions, NVIDIA) ==="
.venv/Scripts/python.exe eval/eval_generation_llm_judge.py --collection us_stock_rag_edgar_exp4 --output experiments/exp4_fixed/generation_judge.json

echo "=== [4/4] RAGAS (venv-ragas, NVIDIA) ==="
.venv-ragas/Scripts/python.exe eval/eval_ragas_vs_rubric.py --from-results experiments/exp4_fixed/generation_judge.json --output experiments/exp4_fixed/ragas_scores.json

echo "=== PIPELINE_ALL_DONE ==="
