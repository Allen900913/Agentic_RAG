"""agentic_rag_version.chunks — 候選池的合併、去重、選席與摘錄。

⚠ `_merge_chunks` 與 `_fair_select` 是**承接池的形狀**由誰決定的地方：
  Grader 有圈選就只收圈選的，沒圈選才退回 rerank top-k 全收
  （量尺是 `eval/probe_relevant_ids.py`，2026-08-28 之前完全沒有）。

⚠ `_snippet` 的視窗要**對齊查詢詞**而不是從頭截：數據頁的數字常排在站台樣板文字後面，
  從頭截會正好切在數字前面——「撈到了卻被自己截掉」與「根本沒撈到」外觀相同。
"""
from __future__ import annotations

import os
import re

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。

# ── 自適應生成預算（fix 2026-07-29，見 [[multi-intent-agentA-is-the-leak]]）────────
# 固定 8-cap 對多 facet 題會 8÷N 稀釋（chunk 層級 probe：軸A 佔 v2 漏失 23/40）。改成隨 facet 數放大，
# 單意圖題維持 ~base（不傷 lexical/colloquial 的 faithfulness），多 facet 才給更多名額（而非砍 facet）。
#   budget(n) = min(BASE + PER_FACET*(n-1), CAP)   例：1→5, 2→7, 3→9, 4→11, 6→15（CAP=16）
WRITER_BUDGET_BASE      = int(os.getenv("AGENTIC_WRITER_BUDGET_BASE", "5"))

WRITER_BUDGET_PER_FACET = int(os.getenv("AGENTIC_WRITER_BUDGET_PER_FACET", "2"))

WRITER_BUDGET_CAP       = int(os.getenv("AGENTIC_WRITER_BUDGET_CAP", "16"))

_CJK_RUN_RE = re.compile(r"[㐀-鿿]+")


def _query_terms(query: str) -> set[str]:
    r"""把 query 拆成算 window 重疊分數用的 term 集合。
    英數：抓 token（len>1）。中文：\w+ 會把「無空格中文整句」視為單一 term——除非 chunk 逐字連續
    出現整句,否則 window score 恆 0、_snippet 退回前綴截斷（P0-3,子問題本就是純繁中,對每個 >400
    字的 chunk 都咬到）。改對每段連續 CJK 抽 2-gram 當比對單位（粗但可用,不引第三方分詞;單字段落
    才退成單字元）。"""
    q = (query or "").lower()
    terms = {w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 1}
    for run in _CJK_RUN_RE.findall(q):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def _best_window_start(text_lower: str, terms: set[str], n: int) -> tuple[int, int]:
    """掃 text_lower 找 n 字視窗裡 query 詞彙命中次數最高的起點。回傳 (start, score)。"""
    if len(text_lower) <= n:
        return 0, sum(text_lower.count(t) for t in terms)
    step = max(1, n // 4)
    best_start, best_score = 0, -1
    starts = list(range(0, len(text_lower) - n + 1, step))
    if starts[-1] != len(text_lower) - n:
        starts.append(len(text_lower) - n)
    for start in starts:
        window = text_lower[start:start + n]
        score = sum(window.count(t) for t in terms)
        if score > best_score:
            best_start, best_score = start, score
    return best_start, best_score


def _snippet(text: str, query: str = "", n: int = 1200) -> str:
    """query-aware 截片段：掃全文找跟 query 詞彙重疊最高的 ~n 字為中心取，避免只看前 n 字漏掉後段重點。
    n=1200（2026-07-30 從 400 調升）：Grader 看的片段太短會漏掉關鍵數字——中文 query 對英文
    Fundamentals 內容零詞彙命中時會退回前綴截斷 t[:n]，400 字剛好切在 header/overview，把
    後段的 'Revenue (TTM): $742.78B / Gross Margin' 切掉，導致 Grader 明明有 chunk 卻誤判
    insufficient、逼執行層漂移到錯口徑（見 mh-01 診斷）。Fundamentals chunk 最長 ~1421 字，
    1200 足以讓短的 key-value chunk 全顯示。_mechanical_summary 仍顯式傳 n=200 不受影響。"""
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    terms = _query_terms(query)
    start, score = (0, -1) if not terms else _best_window_start(t.lower(), terms, n)
    if score <= 0:
        return t[:n] + "…"
    window = t[start:start + n]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + n < len(t) else ""
    return prefix + window + suffix


def _chunk_id(c: dict) -> str:
    """chunk 的可複製 id：'source#chunk_index'。"""
    return f"{c['source']}#{c['chunk_index']}"


def _merge_chunks(pool: list[dict], new: list[dict]) -> list[dict]:
    """聯集去重（(source, chunk_index) 為 key，保留 raw_rerank_score 較高的），依 rerank 降序回傳。
    讓「補救改寫重搜」只加候選、不洗掉先前找到的好 chunk（沿用舊版累積池語意，但改為顯式 state）。"""
    by_key = {(c["source"], c["chunk_index"]): c for c in (pool or [])}
    for c in new or []:
        k = (c["source"], c["chunk_index"])
        old = by_key.get(k)
        if old is None or c["raw_rerank_score"] > old["raw_rerank_score"]:
            by_key[k] = c
    return sorted(by_key.values(), key=lambda x: x["raw_rerank_score"], reverse=True)


def _fair_select(collected: list[dict], k: int) -> list[dict]:
    """跨子問題公平取 top-k 餵 Generator（P0-1）。
    collected 全域依 raw_rerank_score 排序後直接截斷有兩個病灶:① 高分子問題把其他子問題整段擠出
    WRITER_MAX_CHUNKS;② cross-encoder 原始分數是「相對當次 query」的,跨子問題不可直接比（B 的
    0.72 可能已是 B 的最佳答案,卻輸給 A 的第七名）。改成 round-robin:各子問題依自身 rerank 排序輪流
    各取一個（保底代表性）,名額用不完再由高分遞補;最終仍依 rerank 排序,給 Generator 由強到弱的穩定
    順序。單一子問題時退化成單純 top-k（collected 通常 ≤ k,直接原樣回傳）。"""
    if len(collected) <= k:
        return collected
    buckets: dict[int, list[dict]] = {}
    for c in sorted(collected, key=lambda x: x["raw_rerank_score"], reverse=True):
        buckets.setdefault(c.get("_subq", 0), []).append(c)
    order = sorted(buckets)
    pos = {i: 0 for i in order}
    picked: list[dict] = []
    while len(picked) < k:
        progressed = False
        for i in order:
            if pos[i] < len(buckets[i]):
                picked.append(buckets[i][pos[i]])
                pos[i] += 1
                progressed = True
                if len(picked) >= k:
                    break
        if not progressed:
            break
    return sorted(picked, key=lambda x: x["raw_rerank_score"], reverse=True)


def _writer_budget(n_facets: int) -> int:
    """自適應生成預算：facet 越多給越多名額（避免 8÷N 稀釋），單意圖維持 ~BASE。見上方常數說明。"""
    n = max(1, int(n_facets or 1))
    return min(WRITER_BUDGET_BASE + WRITER_BUDGET_PER_FACET * (n - 1), WRITER_BUDGET_CAP)
