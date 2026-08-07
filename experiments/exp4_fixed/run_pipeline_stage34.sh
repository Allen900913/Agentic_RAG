#!/bin/bash
set -e
cd /d/hw3-build-your-personal-rag-Allen900913

echo "=== [3/4] generation_judge (90 questions, NVIDIA, gen-model=openai/gpt-oss-120b) ==="
.venv/Scripts/python.exe eval/eval_generation_llm_judge.py \
  --collection us_stock_rag_edgar_exp4 \
  --output experiments/exp4_fixed/generation_judge.json \
  --gen-model openai/gpt-oss-120b

echo "=== [4/4] RAGAS (venv-ragas, NVIDIA) ==="
.venv-ragas/Scripts/python.exe eval/eval_ragas_vs_rubric.py \
  --from-results experiments/exp4_fixed/generation_judge.json \
  --output experiments/exp4_fixed/ragas_scores.json

echo "=== PIPELINE_ALL_DONE ==="
