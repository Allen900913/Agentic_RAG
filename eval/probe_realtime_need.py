"""量 Grader 的 `realtime_need` 分類（**含 LLM，非零噪音**）。

**這支測什麼**：時效改判由兩半組成，判準見 CLAUDE.md〈LLM 與 Python 的分工〉——

| 半 | 誰做 | 已有的量尺 |
|---|---|---|
| 「這題需要多新的資料」→ `realtime_need` | **LLM**（Grader） | **本檔**（唯一一支） |
| 「候選來源多舊、夠不夠新」→ `stale_days`／`kb_unfixable` | Python（確定性算術） | `eval/verify_web_gate_isolation.py` 閘門⑤ |

2026-08-19 修好 Python 那半（財報期間戳不得替候選池背書說夠新）之後，整條路的正確性
就只剩 LLM 這半沒有被量過。**分類錯的代價不對稱**：
  · `none` 誤判成 `days`／`intraday` → 白叫一次 web（浪費額度，答案不會錯）
  · `days`／`intraday` 誤判成 `none` → **不叫 web，拿 10-K 回答今天股價，零揭露**
所以判讀時 **`實時題被判成 none` 這一格才是要盯的**，不是整體準確率。

⚠ **這支不是閘門**（LLM 有噪音，MoE 不固定路由）。它是 probe：量到、寫下來、必要時
重跑幾次看穩不穩。要當回歸保護請把結論釘成 `llm_replay` fixture，別把這支接進 CI。

⚠ **`none` 那四題是陰性對照，不可省**。少了它們，一個「一律回 days」的 Grader 也會滿分。

用法：
    AGENTIC_AS_OF_DATE=2026-08-19 .venv/Scripts/python.exe eval/probe_realtime_need.py
    # --repeat N 跑 N 輪看穩定度（MoE 不固定路由，同一題可能不同輪不同答）
"""
from __future__ import annotations

import argparse
import collections
import os
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq          # noqa: E402
import agentic_rag_version as ar     # noqa: E402

# (問題, 期望的 realtime_need, 為什麼)
CASES = [
    ("微軟現在的股價是多少？", "intraday", "當下報價，任何 filing 都答不了"),
    ("NVIDIA 目前的市值多少？", "intraday", "當下市值，隨盤變動"),
    ("蘋果最近有什麼消息？", "days", "近期動態，可容忍數天"),
    ("NVIDIA 最近有什麼新進展？", "days", "近期動態，且**不含**「新聞／消息」字樣——"
                                          "刻意測措辭泛化，那正是詞表閘門會漏的形狀"),
    # ── 以下四題是陰性對照：答案在財報裡，判成 days/intraday 就是白燒 web ──────
    ("Microsoft Azure 最新一季的營收成長率是多少？", "none", "『最新一季』指財報期別不是 wall clock"),
    ("NVIDIA 的核心技術護城河是什麼？", "none", "質性業務描述，不隨時間變動"),
    ("Apple 2025 財年的總營收是多少？", "none", "明確指定財年 → prompt 要求一律 none"),
    ("Tesla 在 10-K 裡提到哪些競爭風險？", "none", "直接點名 10-K"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1, help="跑幾輪（看 MoE 的穩定度）")
    args = ap.parse_args()

    if not os.getenv("AGENTIC_AS_OF_DATE"):
        os.environ["AGENTIC_AS_OF_DATE"] = "2026-08-19"   # 釘死 wall clock，否則跨日不可重現
    as_of = ar._get_as_of_date()

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder
    bge = BGEM3FlagModel(rq.EMBEDDING_MODEL, use_fp16=True)
    rr = CrossEncoder(rq.RERANK_MODEL, max_length=rq.RERANK_MAX_LENGTH)
    cl = rq.make_qdrant_client()

    print(f"collection = {rq.COLLECTION_NAME}   as_of = {as_of}   repeat = {args.repeat}")
    print(f"CHECKER_MODEL = {ar.CHECKER_MODEL}\n")

    # 檢索只做一次（同一個池餵所有輪次）：要量的是 LLM 的分類，不是檢索的抖動
    pools = {q: rq.retrieve(q, bge, rr, cl, top_k=ar.POOL_RETURN_K)[0] for q, _, _ in CASES}

    votes: dict[str, list[str]] = collections.defaultdict(list)
    web_fire: dict[str, list[bool]] = collections.defaultdict(list)
    for rnd in range(args.repeat):
        for q, _want, _why in CASES:
            scope = ar._build_todo_temporal_scope(q, ar.FRESHNESS_LIVE)
            v = ar._check_sufficiency(q, pools[q], scope, ar.FRESHNESS_LIVE)
            votes[q].append(v.get("realtime_need", "?"))
            # 生產的 web 觸發條件就是這個（見 _node_execute）
            web_fire[q].append(not v["sufficient"])

    print(f"{'問題':<38}{'期望':<10}{'LLM 判':<22}{'會叫web':<9}判定")
    print("-" * 96)
    wrong_dangerous, wrong_wasteful = [], []
    for q, want, _why in CASES:
        got = votes[q]
        shown = ",".join(f"{k}×{c}" for k, c in collections.Counter(got).most_common())
        fire = f"{sum(web_fire[q])}/{len(web_fire[q])}"
        maj = collections.Counter(got).most_common(1)[0][0]
        if maj == want:
            verdict = "OK"
        elif want in ("intraday", "days") and maj == "none":
            verdict = "❌ 危險（該上網卻判不用）"
            wrong_dangerous.append(q)
        else:
            verdict = "⚠ 浪費（白叫 web）"
            wrong_wasteful.append(q)
        print(f"{q:<38}{want:<10}{shown:<22}{fire:<9}{verdict}")

    unstable = [q for q, v in votes.items() if len(set(v)) > 1]
    print()
    print(f"危險錯誤（實時題判成 none）：{len(wrong_dangerous)} 題 {wrong_dangerous}")
    print(f"浪費錯誤（財報題判成實時）：{len(wrong_wasteful)} 題 {wrong_wasteful}")
    if args.repeat > 1:
        print(f"跨輪不穩定：{len(unstable)} 題 {unstable}")
    else:
        print("⚠ 只跑一輪＝看不出穩定度。MoE 不固定路由，要下結論請 --repeat 3 以上。")
    # 危險錯誤才回非零：浪費錯誤只花額度，不影響答案正確性
    return 1 if wrong_dangerous else 0


if __name__ == "__main__":
    raise SystemExit(main())
