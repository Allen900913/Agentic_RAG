# -*- coding: utf-8 -*-
"""診斷 critical_miss 題：逐 checkpoint 拆「召回 vs 排序」。

對每題計算：
  - pool_ckpt: rubric checkpoint 在「精排前完整候選池」的覆蓋（召回層）
  - topk_ckpt: rubric checkpoint 在「精排後 top-k」的覆蓋（排序層 / 送進 LLM）

判讀：
  pool 有、top-k 無  → 排序問題（reranker 把證據擠出去）
  pool 無            → 召回問題（證據根本沒進池）

Usage:
  python eval/diagnose_crit_miss.py                       # 預設題組 + 120b judge
  python eval/diagnose_crit_miss.py --ids sem-02 sem-08 sem-11
  python eval/diagnose_crit_miss.py --ids sem-02 --judge openai/gpt-oss-20b
"""
import argparse
import json
import sys
# ⚠ Windows 主控台預設 cp950，而本檔的報表帶著 ⚠／✔／① 等字元 ⇒ **印到一半就 crash**，
#   而 crash 的退出碼與「有 FAIL」外觀相同 ＝ 把量尺自己的失敗讀成系統的失敗。
#   2026-09-11 普查：eval/ 的 51 支裡有 27 支帶著這個地雷，其中兩支當天真的踩了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_query as rq
from eval_chunk_recall import judge_checkpoint_coverage

# 預設題組：目前已知的召回/排序疑慮題（sem-07 已由 rubric 修正脫離，改列 sem-02）
DEFAULT_TARGET_IDS = ["sem-02", "sem-08", "sem-11"]
# 2026-07-07 起 judge 正式改用 gpt-oss-120b（見 CHANGELOG）
DEFAULT_JUDGE = "openai/gpt-oss-120b"
TOP_K = rq.DEFAULT_TOP_K


def pool_to_chunks(pool):
    """ScoredPoint list → judge 用的 dict list。"""
    out = []
    for p in pool:
        pl = p.payload or {}
        # payload 文字欄位是 'document'（retrieve() 才把它映射成 dict 的 'content'）
        out.append({"content": pl.get("document", ""), "source": pl.get("source", "")})
    return out


def coverage_per_chunk(query, checkpoints, chunks, model):
    """逐 chunk 單獨判（一次只給 1 段），再 OR 合併。單段判定最不受
    分組/稀釋影響——這是 LLM judge 在多段同批時會出現 pool=N/topk=Y
    自相矛盾的根因。回傳 (agg, per_chunk_hits)。
    per_chunk_hits[j] = 第 j 個 chunk 命中的 checkpoint index 集合。"""
    agg = [False] * len(checkpoints)
    per_chunk_hits = []
    for c in chunks:
        cov = judge_checkpoint_coverage(query, checkpoints, [c], model)
        hits = {i for i, v in enumerate(cov) if v}
        per_chunk_hits.append(hits)
        agg = [a or v for a, v in zip(agg, cov)]
    return agg, per_chunk_hits


def main():
    parser = argparse.ArgumentParser(description="逐 checkpoint 拆召回 vs 排序")
    parser.add_argument("--ids", nargs="+", default=DEFAULT_TARGET_IDS,
                        help="要診斷的題目 id（預設 sem-02 sem-08 sem-11）")
    parser.add_argument("--judge", default=DEFAULT_JUDGE,
                        help="checkpoint coverage 判定用的 judge model")
    parser.add_argument("--retrieval-model", default=rq.DEFAULT_MODEL,
                        help="retrieve() 內部 filter/rewrite/translate 呼叫用的 model——"
                             "須跟生產/ eval_generation_llm_judge.py 的 gen-model 一致，"
                             "才能診斷出使用者實際會遇到的 pool 組成（見 CHANGELOG "
                             "2026-07-08 bug：舊版誤用 judge model 導致 sem-02 的 RANK "
                             "診斷比生產實況樂觀——llama-3.3-70b 生成的 rewrite 變體撈不到"
                             "chunk#46，120b 才撈得到，兩者結論不同）")
    parser.add_argument("--output", default="eval/crit_miss_diagnosis.json")
    parser.add_argument("--rerank-multi-query", action="store_true",
                        help="實驗性：cross-encoder 精排改用原 query+rewrite 變體取跨 query 最高分")
    parser.add_argument("--translate-query-en", action="store_true",
                        help="入口把 query 翻成英文一次，dense/sparse/rewrite/rerank 全部改用（解 cross-lingual 失真）")
    parser.add_argument("--sparse-translate-en", action="store_true",
                        help="實驗性：dense 召回維持原始 query，sparse 召回改用翻譯後英文")
    args = parser.parse_args()

    target_ids = args.ids
    judge = args.judge

    with open("eval/eval_set.json", encoding="utf-8") as f:
        eval_set = json.load(f)
    by_id = {q["id"]: q for q in eval_set["queries"]}

    print("[INFO] Loading BGE-M3 + reranker...")
    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    bge_m3 = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rerank_model = CrossEncoder(rq.RERANK_MODEL)
    client = rq.make_qdrant_client()

    results = {}
    for qid in target_ids:
        q = by_id[qid]
        query_str = q["query"]
        checkpoints = (q.get("rubric") or {}).get("must_include", [])

        chunks, _note, pool = rq.retrieve(
            query_str, bge_m3, rerank_model, client,
            top_k=TOP_K, model_name=args.retrieval_model,
            enable_rewrite=True, return_pool=True,
            rerank_multi_query=args.rerank_multi_query,
            translate_query_en=args.translate_query_en,
            sparse_translate_en=args.sparse_translate_en,
        )
        pool_chunks = pool_to_chunks(pool)

        # 逐 chunk 單獨判整個 pool（含 top-k），並標記哪些 pool chunk 屬於 top-k。
        # in_pool = OR(所有 pool chunk)；in_topk = OR(top-k chunk)。
        # 建構式保證單調：top-k ⊆ pool ⟹ topk=Y 必 pool=Y，不再自相矛盾。
        topk_keys = {(c["source"], c["chunk_index"]) for c in chunks}
        cov_pool = [False] * len(checkpoints)
        cov_topk = [False] * len(checkpoints)
        cov_rest = [False] * len(checkpoints)  # pool 扣掉 top-k 的部分
        for p, pc in zip(pool, pool_chunks):
            pl = p.payload or {}
            key = (pl.get("source", ""), pl.get("chunk_index", 0))
            cov = judge_checkpoint_coverage(query_str, checkpoints, [pc], judge)
            is_topk = key in topk_keys
            for i, v in enumerate(cov):
                if v:
                    cov_pool[i] = True
                    if is_topk:
                        cov_topk[i] = True
                    else:
                        cov_rest[i] = True

        rows = []
        for i, cp in enumerate(checkpoints):
            cp_pool, cp_topk, cp_rest = cov_pool[i], cov_topk[i], cov_rest[i]
            if cp_topk:
                verdict = "OK (in top-k)"
            elif cp_rest:
                verdict = "RANK  (pool有→top-k被擠出)"
            else:
                verdict = "RECALL(pool就沒有)"
            rows.append({
                "checkpoint": cp["checkpoint"],
                "critical": cp.get("is_critical", False),
                "weight": cp.get("weight", 1),
                "in_pool": cp_pool,
                "in_topk": cp_topk,
                "in_rest": cp_rest,
                "verdict": verdict,
            })

        results[qid] = {
            "query": query_str,
            "pool_size": len(pool),
            "topk_size": len(chunks),
            "rows": rows,
        }

        print(f"\n{'='*80}\n[{qid}] {query_str}")
        print(f"  pool_size={len(pool)}  top_k={len(chunks)}")
        for r in rows:
            flag = "★CRIT" if r["critical"] else "     "
            print(f"  {flag} pool={'Y' if r['in_pool'] else 'N'} "
                  f"topk={'Y' if r['in_topk'] else 'N'} | {r['verdict']:28s} | "
                  f"{r['checkpoint'][:60]}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[DONE] Written to {args.output}")


if __name__ == "__main__":
    main()
