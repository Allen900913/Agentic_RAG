"""
rejudge_with_model.py — 只重跑 judge，不重跑生成/檢索

背景：k=3 semantic baseline（generation_correctness_semantic_k3.json）的高變異
（sem-01/03 std=0.377）已證實至少部分是 judge 誤判（sem-01 run1 答案明確含 CUDA
卻被判 0 分），懷疑 judge 模型（openai/gpt-oss-20b）本身雜訊偏高。

比起重跑整條 pipeline（生成+檢索+judge，會再燒一次 Groq TPD），這支腳本直接讀
既有 k3 run 檔（3 輪 × 11 題 = 33 個已生成好的答案），只重打 judge call，換成
--judge-model（預設 openai/gpt-oss-120b，Groq 上有獨立 TPD 池，不跟生成模型搶額度）。

每個既有答案會被重評 --judge-repeats 次（預設 3 次），藉此把變異拆成兩層：
  - 同一答案重評 3 次的 std → 純 judge 變異（模型本身的不確定性）
  - 3 個 run（3 個不同生成答案）之間的差 → 生成變異（跨 run 的抽樣噪音）

Usage:
  python eval/rejudge_with_model.py \
    --runs eval/generation_correctness_semantic_k3_run1.json eval/generation_correctness_semantic_k3_run2.json eval/generation_correctness_semantic_k3_run3.json \
    --eval-set eval/eval_set.json \
    --judge-model openai/gpt-oss-120b \
    --judge-repeats 3 \
    --output eval/generation_correctness_semantic_k3_judge120b.json
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import eval_generation_llm_judge as ejg  # noqa: E402


def load_rubrics(eval_set_path: Path) -> dict:
    eval_set = json.loads(eval_set_path.read_text(encoding="utf-8"))
    return {q["id"]: q["rubric"] for q in eval_set["queries"] if q.get("rubric")}


def rejudge_one(query: str, answer: str, rubric: dict, ctx_recall_overlap, model: str) -> dict:
    """重現 eval_generation_llm_judge.py 在 --correctness-only 模式下的 judge 輸入。"""
    hall_result_stub = {
        "claims": [], "total_claims": 0, "unsupported_claims": 0,
        "hallucination_rate": None, "is_refusal": None, "error": None,
    }
    is_refusal_hint = ejg.looks_like_refusal(answer)
    verdict = ejg.evaluate_correctness_with_feedback(
        query=query,
        answer=answer,
        rubric=rubric,
        hall_result=hall_result_stub,
        relevance_score=None,
        ctx_recall_llm=None,
        ctx_recall_overlap=ctx_recall_overlap,
        is_refusal_hint=is_refusal_hint,
        wrongful_refusal_hint=None,
        model=model,
    )
    correctness = ejg.compute_correctness(rubric, verdict)
    return {"verdict": verdict, "correctness": correctness}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", required=True,
                         help="既有 k-run 結果檔路徑（每個檔案是一個獨立生成的 run）")
    parser.add_argument("--eval-set", default="eval/eval_set.json")
    parser.add_argument("--judge-model", default="openai/gpt-oss-120b")
    parser.add_argument("--judge-repeats", type=int, default=3,
                         help="同一個已存在的答案重新 judge 幾次（用來量測純 judge 變異）")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    rubrics = load_rubrics(Path(args.eval_set))

    # per_question[qid][run_idx] = {"answer":..., "judge_scores": [...]}
    per_question: dict[str, list[dict]] = {}

    for run_idx, run_path in enumerate(args.runs, start=1):
        data = json.loads(Path(run_path).read_text(encoding="utf-8"))
        records = data["records"]
        for rec in records:
            qid = rec["id"]
            rubric = rubrics.get(qid)
            if not rubric:
                continue
            answer = rec["answer"]
            if not answer:
                continue
            print(f"[run{run_idx}] {qid}: judging with {args.judge_model} x{args.judge_repeats}...")
            judge_scores = []
            verdicts = []
            for rep in range(args.judge_repeats):
                result = rejudge_one(
                    query=rec["query"], answer=answer, rubric=rubric,
                    ctx_recall_overlap=rec.get("context_recall_overlap"),
                    model=args.judge_model,
                )
                score = result["correctness"]["score"] if result["correctness"] else None
                judge_scores.append(score)
                verdicts.append(result["verdict"])
                print(f"    rep{rep+1}: score={score} hits={result['verdict']['must_include_hits']}")

            per_question.setdefault(qid, []).append({
                "run_idx": run_idx,
                "answer": answer,
                "judge_scores": judge_scores,
                "judge_score_mean": round(statistics.mean(s for s in judge_scores if s is not None), 4)
                    if any(s is not None for s in judge_scores) else None,
                "judge_score_std": round(statistics.pstdev(s for s in judge_scores if s is not None), 4)
                    if sum(1 for s in judge_scores if s is not None) > 1 else 0.0,
                "verdicts": verdicts,
                "original_score": rec["correctness"]["score"] if rec.get("correctness") else None,
            })

    # ── 彙整：拆「同答案 judge 變異」vs「跨 run 生成變異」──────────────
    summary = {}
    for qid, runs in per_question.items():
        judge_stds = [r["judge_score_std"] for r in runs if r["judge_score_std"] is not None]
        run_means = [r["judge_score_mean"] for r in runs if r["judge_score_mean"] is not None]
        summary[qid] = {
            "n_runs": len(runs),
            "avg_within_answer_judge_std": round(statistics.mean(judge_stds), 4) if judge_stds else None,
            "run_means": run_means,
            "cross_run_std": round(statistics.pstdev(run_means), 4) if len(run_means) > 1 else 0.0,
            "original_scores": [r["original_score"] for r in runs],
        }

    all_judge_stds = [s["avg_within_answer_judge_std"] for s in summary.values() if s["avg_within_answer_judge_std"] is not None]
    overall = {
        "judge_model": args.judge_model,
        "judge_repeats": args.judge_repeats,
        "mean_within_answer_judge_std": round(statistics.mean(all_judge_stds), 4) if all_judge_stds else None,
        "note": "avg_within_answer_judge_std = 同一個既有答案重評 N 次的 std（純 judge 變異）；"
                "cross_run_std = 3 個不同 run 生成答案的 judge_score_mean 之間的 std（生成變異，含原 k3 的 run-to-run 抽樣噪音）。",
    }

    Path(args.output).write_text(
        json.dumps({"overall": overall, "per_question_summary": summary, "raw_per_question_runs": per_question},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[DONE] wrote {args.output}")
    print(f"[DONE] mean_within_answer_judge_std = {overall['mean_within_answer_judge_std']}")


if __name__ == "__main__":
    main()
