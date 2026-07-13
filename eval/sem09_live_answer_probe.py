# -*- coding: utf-8 -*-
"""sem-09 單題 fresh end-to-end 探針：retrieve+generate+judge，印出完整答案與
最終 top-5 chunk 清單，直接肉眼核對「答案有沒有用到已經在 top-5 裡的 Gemini/AI
Overviews 內容（chunk #139/#179）」。

背景：sem09_register_regression_probe.py 的結果顯示，不管有無 rewrite/翻譯，
最終 top-5（10-K chunk #139「Making AI Helpful for Everyone」與 #179「AI
Overviews and AI Mode in Search」）本身就直接命中 rubric checkpoint「提到 Gemini
模型 / AI 整合進搜尋」——這推翻了「新聞被刷掉=答案漏 Gemini」的因果鏈假設。
本探針要確認：生成端有沒有用到這些已經在手的內容。

Usage:
  python eval/sem09_live_answer_probe.py --repeat 1
"""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Windows cp950 終端無法印部分 unicode 字元（如 U+202F narrow no-break space），
# 這裡連 eval_generation_llm_judge.run_single_pass 內部的 print 也會踩到——
# 用 utf-8 + errors=replace 重設 stdout，比逐一包 ascii-safe 更徹底。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _safe_print(text):
    print(text or "")

from dotenv import load_dotenv
load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq
from eval_generation_llm_judge import run_single_pass

QID = "sem-09"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-model", default=rq.DEFAULT_MODEL)
    parser.add_argument("--gen-model", default=rq.DEFAULT_GEN_MODEL)
    parser.add_argument("--judge-model", default="openai/gpt-oss-20b")
    parser.add_argument("--judge-votes", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", default="eval/sem09_live_answer_probe_result.json")
    cli_args = parser.parse_args()

    with open("eval/eval_set.json", encoding="utf-8") as f:
        eval_set = json.load(f)
    q = next(x for x in eval_set["queries"] if x["id"] == QID)

    print("[INFO] Loading BGE-M3 + reranker...")
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rerank_model = CrossEncoder(rq.RERANK_MODEL)
    client = rq.make_qdrant_client()

    all_sources = set()  # expand_relevant 只用來算 overlap，這裡不影響 correctness

    args = SimpleNamespace(
        top_k=rq.DEFAULT_TOP_K,
        retrieval_model=cli_args.retrieval_model,
        rewrite=True,
        translate_query_en=False,
        rerank_multi_query=False,
        correctness_only=True,
        gen_model=cli_args.gen_model,
        judge_model=cli_args.judge_model,
        judge_votes=cli_args.judge_votes,
    )

    all_runs = []
    for i in range(cli_args.repeat):
        out_path = Path(f"eval/sem09_live_answer_probe_run{i+1}.json")
        if out_path.exists():
            out_path.unlink()  # 強制 fresh generate，不吃到 resume 快取
        records, _summary = run_single_pass([q], all_sources, bge_m3, rerank_model, client, args, out_path)
        rec = records[0]
        all_runs.append(rec)

        _safe_print(f"\n{'='*80}\n[RUN {i+1}] sem-09 fresh answer\n{'='*80}")
        _safe_print(f"Query: {rec['query']}")
        _safe_print(f"\nTop-5 sources used:")
        for s in rec["sources"]:
            _safe_print(f"  {s['source']} #{s['chunk_index']}")
        _safe_print(f"\nAnswer:\n{rec['answer']}")
        _safe_print(f"\nMentions 'Gemini': {'Gemini' in (rec['answer'] or '')}")
        _safe_print(f"Correctness: {rec['correctness']}")
        _safe_print(f"Correctness verdict: {json.dumps(rec['correctness_verdict'], ensure_ascii=False, indent=2)}")

    with open(cli_args.output, "w", encoding="utf-8") as f:
        json.dump(all_runs, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {cli_args.output}")


if __name__ == "__main__":
    main()
