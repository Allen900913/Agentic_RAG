"""agentic_rag_version.ratio — ratio 題的意圖判定與財務錨源保底。

⚠ **判別力來自「LLM 說不是就必須不是」**：`_classify_ratio_fields` 判不出來時會退回詞表
  `_RATIO_INTENT_RE`，於是詞表仍然是實際做決定的人，而端到端跑分看不出任何差別。
  這條路的準確度**不能從結果檔推**——量它的是 `eval/probe_ratio_intent.py`。

⚠ 失敗方向不對稱：ratio 題判成非 ratio ＝ 保底不執行、口徑警語不觸發 →
  **拿財年數字冒充 TTM 且零揭露**；反向只是多撈一個 chunk。
"""
from __future__ import annotations

import json
import os
import re

import rag_query as rq
import llm_replay as _replay
from .tracing import _trace

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。

# ──────────────────────────────────────────────────────────────────────────────
# 常數 / 模型選型（NVIDIA 目錄 id，實測見舊版 CHANGELOG 續八）
# ──────────────────────────────────────────────────────────────────────────────
# RETRIEVAL_MODEL：只做 retrieve 內部 filter/translate 這類「機械型」輕量呼叫，
# 與 CHECKER_MODEL（推理）、GEN_MODEL（生成）分離。
# 2026-08-14 從 gpt-oss-20b 改為 120b。原本的理由是「機械型輕量呼叫，用小模型求快」，
# 而 `eval/ablate_retrieval_model.py`（100 題零噪音對照）把那個前提推翻了：
#   · 速度：**20b 在 NIM 上慢一倍**（3.57s vs 1.76s，n=200/模型）——理由本身是反的。
#   · 品質：等價。gold recall 0.8459(20b) vs 0.8479(120b)，差 0.0020，而同一個模型跑
#     兩次的噪音就有 0.0014；完全撈不到 gold 的四題（col-07/lex-03/lex-07/lex-14）三臂
#     一模一樣；候選池雖然會換（Jaccard 0.897 vs 噪音 0.947）但換掉的都是無關 chunk。
# ⚠ 這會讓本次之後的 agentic 跑分與 2026-08-14 以前的前處理不同（雖已量到無作用）。
#   要重現舊跑分請設 env `AGENTIC_RETRIEVAL_MODEL=openai/gpt-oss-20b`。
RETRIEVAL_MODEL = os.getenv("AGENTIC_RETRIEVAL_MODEL", "openai/gpt-oss-120b")

# ── ratio 題財務錨源保底（doc_type 版的 _ensure_ticker_coverage）─────────────────────
# 病灶（見 trace/probe）：毛利率/淨利率/成長率等「可從 10-K/10-Q 原始行自行計算」的指標，池中
#   同時有 ① Fundamentals（預算好的 TTM 比率，寫死值，rerank rank0）② 10-K/10-Q（原始行，可現算）
#   兩個近似平手來源。Grader 一次只圈 1 個 → 在兩來源間擲硬幣，時而圈中 10-Q（季度口徑、與 gold 的
#   Fundamentals 錨點不一致）。市值/P/E/ROE 等「單一來源」指標不受影響（想挑錯都沒得挑），故 recall 高。
# 修法：ratio 意圖時，若 commit 集合裡沒有該公司的 Fundamentals，就從池中把它補回（純確定性、零 LLM、
#   只加不減）——Grader 的語意判斷照跑，這層只接住「圈錯來源」的壞 run，消掉 run 間變異。
_RATIO_INTENT_RE = re.compile(
    r"毛利率|淨利率|净利率|利潤率|利润率|營業利益率|营业利益率|營業利潤率|獲利率|获利率|"
    r"淨利潤率|净利润率|營收成長|营收成长|成長率|成长率|營收增長|营收增长|"
    r"gross\s*margin|net\s*margin|operating\s*margin|profit\s*margin|\bmargin\b|growth\s*rate",
    re.IGNORECASE,
)

# ratio 意圖 → Fundamentals 檔裡的**欄位名**。這是**格式定義的封閉集合**（欄位名由本專案自己的
# ingest 產生，見 data/edgar_processed/Fundamentals/*.txt），不是拿字串比對做感知，
# 屬於 CLAUDE.md〈硬編碼詞表是警訊〉明列的正當例外。
#
# **為什麼需要它**（2026-08-20 實測，推翻了 BACKLOG 掛了 9 天的「未驗證」假設）：
#   MSFT_Fundamentals #0（698 字元）含 Revenue Growth (YoY) 18.30%、Gross Margin 68.31%、
#   Operating Margin 46.33%、Profit Margin 39.34% ——**每一個比率都在這裡**；
#   #1（2045 字元）只有 Total Cash／Total Debt，**一個比率都沒有**。
#   而 cross-encoder 對「Microsoft 的毛利率是多少？」把 **#1 排在 #0 前面**（0.928 vs 0.918）。
#   → `_ensure_ratio_source_coverage` 原本補「該家分數最高的 Fundamentals」，補進來的正是
#     那個一個比率都沒有的 #1；Generator 因此沒有 TTM 值可引，退回 10-K/10-Q 的財年／單季數字
#     （口徑不同、數值接近、無揭露）。這就是 mi-05／lex-17 的真因。
#   舊記載猜的是「#1 長三倍造成 rerank 偏好」——**方向對了，但真正的傷害不在 rerank 本身，
#   而在保底機制用「分數最高」而不是「有沒有那個欄位」來挑**。
_RATIO_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    (r"毛利率|gross\s*margin", "Gross Margin"),
    (r"營業利益率|营业利益率|營業利潤率|operating\s*margin", "Operating Margin"),
    (r"淨利率|净利率|淨利潤率|净利润率|net\s*margin|profit\s*margin", "Profit Margin"),
    (r"營收成長|营收成长|營收增長|营收增长|成長率|成长率|growth\s*rate", "Revenue Growth"),
)


def _wanted_ratio_fields(task: str) -> list[str]:
    """這個子問題問的是 Fundamentals 的哪幾個欄位。認不出來就回空 list ＝ 退回舊行為（挑分數最高）。"""
    return [f for pat, f in _RATIO_FIELD_HINTS if re.search(pat, task or "", re.IGNORECASE)]


def _is_ratio_intent(task: str) -> bool:
    """子問題是否問「可從 10-K/10-Q 現算、故有雙來源」的比率/成長率指標（毛利率/淨利率/成長率…）。
    這類指標 Fundamentals 已預算好寫死值，應以 Fundamentals 為錨、繞開 10-Q 現算路徑（路由非算術）。
    單一來源指標（市值/P/E/ROE/EPS/自由現金流）不觸發：無雙來源擲硬幣風險，補了也只是多餘。"""
    return bool(_RATIO_INTENT_RE.search(task or ""))


# ── ratio 意圖：LLM 判定（詞表退位成 fallback）────────────────────────────────
# **為什麼要改**（2026-08-25，BACKLOG 殘留①）：上面那條 `_RATIO_INTENT_RE` 是拿字串比對
#   做感知，正是 CLAUDE.md〈硬編碼詞表是警訊〉講的形狀。實測 `col-11` 連跑四輪，Planner
#   把子問題寫成「Microsoft 的營收成長得快不快？」時整條 ratio 機制**靜默繞過**——不是
#   補撈失敗，是根本沒進到補撈。往詞表加「快不快」只會換一個措辭再漏一次。
# **判準**（CLAUDE.md〈LLM 與 Python 的分工〉）：「這句話想知道哪個量」沒有唯一機械答案
#   → LLM；「那個量在 Fundamentals 叫什麼欄位名」是封閉集合 → 由 enum 收斂。所以 LLM 只
#   被允許從 `_RATIO_FIELD_ENUM` 裡挑，挑到集合外的一律丟掉。
# ⚠ **為什麼不擴 `_PLANNER_PROMPT` 而是另外一次 call**：planner prompt 一動，子問題拆解
#   本身就會漂（輸出格式從 `["字串"]` 變成物件陣列），**65 題每一題的檢索池都會跟著變**，
#   等於把被測項和基準一起搬走。多付一次輕量 call 換 planner prompt 逐字不變，是這裡唯一
#   划算的交易（同 `verify_web_gate_isolation` 對 Checker prompt 的 byte-identical 斷言）。
# ⚠ **fallback 會遮住 LLM 的失手**：解析失敗回 None → 退回詞表，行為與舊碼完全相同。
#   所以「LLM 判得準不準」不能從端到端結果推，要用 `eval/probe_ratio_intent.py` 直接量。
_RATIO_FIELD_ENUM: tuple[str, ...] = tuple(f for _, f in _RATIO_FIELD_HINTS)

_RATIO_INTENT_PROMPT = """你是財務問句的欄位分類器。給你一組編號的「子問題」，逐一判斷它想知道的
是不是本知識庫 Fundamentals 檔裡那四個**預先算好的比率欄位**之一。

可選欄位（只能從這四個挑，不可自創、不可改寫）：
- "Gross Margin"      毛利率
- "Operating Margin"  營業利益率／營業利潤率
- "Profit Margin"     淨利率／純益率／利潤率／獲利率
- "Revenue Growth"    營收成長率（YoY）

判準是**問題想知道的那個量**，不是它用了哪些字——口語、比喻、反問、間接問法都要照樣判：
「營收成長得快不快」問的就是 Revenue Growth；「每賺一塊錢留下多少」問的就是 Profit Margin。

問到多個就都列。問的**不是**這四個比率時回空陣列，例如：
- 市值／股價／本益比／ROE／EPS／自由現金流／現金與負債 → []
- 營收「金額」是多少、獲利「金額」多少 → []（那是金額不是比率／成長率）
- 風險、策略、競爭、產品、業務描述 → []

只輸出一個 JSON 陣列，長度與子問題數完全相同，第 i 個元素是第 i 題的欄位陣列。
不要輸出任何其他文字。例（輸入兩題）：[["Revenue Growth"],[]]"""


def _classify_ratio_fields(tasks: list[str]) -> list[list[str] | None]:
    """一次 LLM call 判定每個子問題問的是哪幾個 Fundamentals 比率欄位。

    回傳與 `tasks` 等長的清單，每格是 `list[str]`（**空 list 是有效答案＝判定不是 ratio 題**）
    或 `None`（＝LLM 沒表態，呼叫端須退回詞表）。**這兩者不可混為一談**：空 list 要能壓過
    詞表，否則詞表仍然是實際做決定的人，這次改動就只是裝飾。"""
    if not tasks:
        return []
    _rk = "\n".join(tasks)
    _hit = _replay.get("ratio", _rk)
    if _hit is not _replay.MISS:
        return [None if x is None else list(x) for x in _hit]
    user = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(tasks))
    try:
        with _pkg._quiet():
            raw = rq.call_llm(
                [{"role": "system", "content": _RATIO_INTENT_PROMPT},
                 {"role": "user", "content": user}],
                RETRIEVAL_MODEL, temperature=0.0,
            )
        data = _pkg._loads_json_lenient(raw)
        if not isinstance(data, list) or len(data) != len(tasks):
            raise ValueError(f"shape mismatch: {data!r}")
        out: list[list[str] | None] = []
        for item in data:
            if not isinstance(item, list):
                raise ValueError(f"element not a list: {item!r}")
            out.append([f for f in _RATIO_FIELD_ENUM if f in item])   # enum 外一律丟掉
    except Exception as exc:
        _trace(f"ratio-intent: LLM 判定失敗 {exc!r} -> 退回詞表")
        return [None] * len(tasks)
    _trace(f"ratio-intent: {list(zip(tasks, out))}")
    _replay.put("ratio", _rk, out)
    return out


def _resolve_ratio_fields(fields: list[str] | None, task: str) -> list[str]:
    """要撈的 Fundamentals 欄位：LLM 表態過就以它為準（**含空 list**），沒表態才退回詞表。"""
    if fields is not None:
        return [f for f in _RATIO_FIELD_ENUM if f in fields]
    return _wanted_ratio_fields(task)


def _has_ratio_intent(fields: list[str] | None, task: str) -> bool:
    """ratio 意圖：LLM 表態過就是「有沒有挑到欄位」，沒表態才退回詞表。

    ⚠ 這裡把「意圖」與「欄位」**合成同一個訊號**，是刻意的：舊碼允許 `_is_ratio_intent`
      為真而 `_wanted_ratio_fields` 為空（兩份詞表不同步，例如「獲利率」在前者不在後者），
      而那個組合會讓 `_ensure_ratio_source_coverage` 退回「挑分數最高的 Fundamentals」——
      那正是 mi-05／lex-17 的真因。走 LLM 這條路時不再有那個縫。"""
    if fields is not None:
        return bool(_resolve_ratio_fields(fields, task))
    return _is_ratio_intent(task)


def _todos_ratio_fields(todos: list[dict] | None) -> list[str] | None:
    """把各子問題的欄位判定收成一個聯集，供 Synthesize 端的揭露 validator 用。

    ⚠ 回 `None` 的條件是「**沒有任何一個** todo 帶著判定」（＝ LLM 整批失手，或這是一份
      舊格式的 state）——那時呼叫端會退回詞表、行為同舊碼。只要有一個 todo 表過態，就以
      LLM 的判定為準，即使聯集是空的：Synthesize 看的是原始問句，而原始問句正是詞表最會
      誤判的地方（「營收多少」被 `成長率` 以外的字樣掃到）。"""
    seen = False
    out: list[str] = []
    for t in todos or []:
        f = t.get("ratio_fields")
        if f is None:
            continue
        seen = True
        for x in f:
            if x in _RATIO_FIELD_ENUM and x not in out:
                out.append(x)
    if not seen:
        return None
    return [f for f in _RATIO_FIELD_ENUM if f in out]     # 順序穩定，方便斷言


def _fetch_fundamentals_with_field(ticker: str, fields: list[str], task: str) -> dict | None:
    """直接從 Qdrant 撈該公司「含指定欄位」的 Fundamentals chunk（零 LLM、只讀 payload）。

    ⚠ **為什麼保底不能只掃池**（2026-08-21 實測，這是 lex-17 的第二層真因）：
      `_ensure_ratio_source_coverage` 原本只在 `run_state.pool` 裡找，而 pool ＝ Qdrant
      server-side RRF 回傳的 `RRF_TOP_N_PRIMARY`(=20) 個候選。實測「Microsoft 的營收成長率
      表現如何」在生產組態（`full_translate_en=True`，英譯句
      "How is Microsoft's revenue growth rate performing?"）下，**`MSFT_Fundamentals #0`
      連候選名單都沒進**（進來的是零比率的 #1，rank 5）。也就是說：名字叫「保底」，
      實作卻是「希望它剛好在池裡」——池裡沒有的時候，它什麼也做不到。
      （中文原句反而撈得到 #0，rank 7。這不是 recall 調參能一勞永逸的方向，
       且 `RRF_TOP_N_PRIMARY` 碼上註明 sweep 過 20 > 40，不該為一題去動它。）

    所以補撈走**確定性查詢**而不是相似度：「這家公司的哪個 Fundamentals chunk 含
    Revenue Growth 欄位」有唯一正確答案 → 照 CLAUDE.md〈LLM 與 Python 的分工〉交給 Python。
    篩選鍵用 `period_basis == "TTM"`（全庫只有 Fundamentals 是 TTM，見
    `data_update_edgar._period_basis_for`，22 筆，且該欄位有 payload 索引）。

    ⚠ 分數用**真實的 cross-encoder 重算**，不要捏造：這個 `raw_rerank_score` 會被印在
      答案底下的引用區塊給使用者看，塞一個假數字等於出貨一個看起來可追溯的謊。
      （量尺與被測物耦合之外的另一種同型錯誤：讓「無法追溯」變成「看起來可追溯」。）
    """
    if not ticker or not fields:
        return None
    try:
        from qdrant_client import models as qmodels
        _bge, reranker, client = _pkg._get_models()
        pts, _ = client.scroll(
            collection_name=rq.COLLECTION_NAME,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="ticker", match=qmodels.MatchValue(value=ticker)),
                qmodels.FieldCondition(key="period_basis", match=qmodels.MatchValue(value="TTM")),
            ]),
            limit=64, with_payload=True, with_vectors=False,
        )
    except Exception as exc:                       # Qdrant 不可用時退回舊行為，不要讓保底變成故障點
        _trace(f"ratio-coverage: Fundamentals 補撈失敗 {exc!r}")
        return None

    cands = []
    for pt in pts:
        pl = pt.payload or {}
        if "Fundamentals" not in (pl.get("source") or ""):
            continue
        txt = pl.get("document") or ""
        if any(f.lower() in txt.lower() for f in fields):
            cands.append(pl)
    if not cands:
        return None
    scores = reranker.predict([[task, (pl.get("document") or "")] for pl in cands], batch_size=1)
    scores = scores.tolist() if hasattr(scores, "tolist") else list(scores)
    best = max(range(len(cands)), key=lambda i: scores[i])
    chunk = rq._payload_to_chunk(cands[best], 0.0, float(scores[best]))
    _trace(f"ratio-coverage: 補撈 {chunk['source']}#{chunk['chunk_index']} "
           f"（欄位 {fields}，rerank={chunk['raw_rerank_score']:.4f}）")
    return chunk


def _ensure_ratio_source_coverage(ranked: list[dict], selected: list[dict], want_tickers,
                                  task: str = "",
                                  ratio_fields: list[str] | None = None) -> list[dict]:
    """ratio 題財務錨源保底：保證 want_tickers 每一家在 selected 裡至少有一個 Fundamentals chunk；
    缺的就從 ranked（完整 rerank 降序池）補上該家分數最高的 Fundamentals，回傳仍按 rerank 降序。
    照 rq._ensure_ticker_coverage 的結構（只加不減）。零/未知 ticker 時原樣回傳。
    Fundamentals 靠 source 檔名判定（payload doc_type 實測常為空，見 probe）。"""
    want = [want_tickers] if isinstance(want_tickers, str) else list(want_tickers or [])
    if not want:
        return selected
    is_fund = lambda c: "Fundamentals" in (c.get("source") or "")
    # 要補的不是「隨便一個 Fundamentals」，是**含被問欄位的那一個**（見 _RATIO_FIELD_HINTS 上方的實測）。
    fields = _resolve_ratio_fields(ratio_fields, task)

    def _has_field(c: dict) -> bool:
        if not fields:
            return True                      # 認不出欄位 → 退回舊行為，不要更糟
        txt = c.get("content") or ""
        return any(f.lower() in txt.lower() for f in fields)

    # 已經有「含該欄位的 Fundamentals」才算 covered——原本只看有沒有 Fundamentals，
    # 於是 #1（Balance Sheet，零比率）也會被算成已覆蓋，保底機制當場失效。
    covered = {c.get("ticker", "") for c in selected if is_fund(c) and _has_field(c)}
    missing = set(want) - covered
    if not missing:
        return selected
    out = list(selected)
    seen = {(c["source"], c["chunk_index"]) for c in selected}
    for c in ranked:                       # ranked 已降序：每家第一個 Fundamentals 命中即該家最高分
        if not missing:
            break
        if not is_fund(c):
            continue
        t = c.get("ticker", "")
        key = (c["source"], c["chunk_index"])
        if t in missing and key not in seen and _has_field(c):
            out.append(c)
            seen.add(key)
            missing.discard(t)
    # 池裡沒有「含該欄位的 Fundamentals」→ 確定性補撈，讓保底真的是保底
    # （為什麼掃池不夠，見 _fetch_fundamentals_with_field 的 docstring）
    for t in sorted(missing):
        c = _fetch_fundamentals_with_field(t, fields, task)
        if not c:
            continue
        key = (c["source"], c["chunk_index"])
        if key in seen:
            continue
        out.append(c)
        seen.add(key)
    out.sort(key=lambda x: x["raw_rerank_score"], reverse=True)
    return out


# ── multi_hop 第二跳依賴解析（Type B：「新聞中做了 X 的是哪家？該公司的財報指標多少」）──────────
# planner 對這種兩跳題會把 hop-2 寫成「該公司的毛利率是多少」——帶未解回指代名詞、句中沒點名公司。
# 若讓它跟 hop-1 同一波平行檢索,「該公司」永遠沒被填 → 無 ticker 的破檢索撈回隨機 chunk。
# 解法(全確定性,不動 replanner LLM)：execute 分波時延後這種依賴型 hop,等 hop-1 辨識出公司、
# 把代名詞回填成具體公司名後,下一波才檢索。
_BACKREF_RE = re.compile(r"該公司|該企業|該家公司|這家公司|此公司|上述公司|前述公司|上述那家公司")
# ticker → 可被 _mentioned_tickers / retrieve 重新偵測的正規公司名（回填第二跳用）
_TICKER_CANON = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
                 "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta", "TSLA": "Tesla"}
