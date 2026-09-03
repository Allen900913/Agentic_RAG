"""驗收 query-understanding → Qdrant filter 這條路的 Python 半邊（零 LLM、零網路，秒級）。

**這支測什麼**：`period_ref`（LLM 判的）→ 具體期碼 filter（Python 算的）這條路上，
**Python 那一半**的每一個確定性決策；2026-08-28 起也含**實體解析**（產品名 → ticker）的接線
——那也是「LLM 判、Python 收斂成 filter」的同一個形狀，同一份 `parse_query_filters` 出口。

**LLM 判得準不準一律不在這裡量**（那含 LLM、非零噪音，寫不成斷言）：期間意圖看
`docs/EVAL.md`〈期間意圖解析〉的 57 題交叉表，實體解析看
[`eval/probe_ticker_resolution.py`](probe_ticker_resolution.py)。

**為什麼需要這支**：整條路的價值全押在「期別誰新誰舊」這個序上，而那個序**踩過坑**：
舊的 `_get_latest_10q_periods` 用 `report_period_code` 字串比大小取 max，10-K 是 4 位年份、
10-Q 是 6 位 yyyymm，混在一起比會**時對時錯**。閘門② 就是把這件事釘死成可重跑的斷言。

## 八道閘門

⚠ 這張表 2026-08-27 漂移過一次：閘門⑥ 那天加了，標題還寫著「五道」。改這支請一併改表。

| # | 測什麼 | 為什麼是它 |
|---|---|---|
| ① | `_fiscal_sort_key` 真值表 | 序的最小單元；缺欄位必須回 None（不參與比較）而不是猜 |
| ② | ladder 排序 **vs 期碼字串比大小** | **有判別力的那一道**：多年語料下實測 385 對有 31 對（8.1%）判反。單年語料下這道會接近 0 對——那不代表沒問題，代表**測資不夠**，見下方警告 |
| ③ | `ladder_pick` 真值表 | 挑不到必須回 None，**不可退而求其次**（挑錯期別比不挑更糟：答案會帶著引用一起錯） |
| ④ | `period_ref` → filter 對應 | 只有 `latest` 該產生 filter；`range` 產生 filter 會讓趨勢題直接答不出來 |
| ⑤ | `range` → 跳過 collapse 的接線 | B 的損害唯一的解法；斷了就回到趨勢題期別數 −38% |
| ⑥ | Tier 1 的 label-year 命中資格 | 那半個 OR **只能放寬命中、不能自己構成命中**；否則 Tier 1 假命中會把 Tier 2 的降級與揭露語一起關掉 |
| ⑦ | 實體解析的**接線** | LLM 只在 regex 沉默時被叫、只補不覆寫、輸出過封閉集合、失敗回空。⚠ 這裡不量**準不準**（那含 LLM、非零噪音），準確度在 [`probe_ticker_resolution.py`](probe_ticker_resolution.py) |
| ⑧ | 期間路由讀的是**已解出的 ticker** | 兩條管線在同一次 `retrieve()` 裡都要「這是哪家公司」，而答案 ⑦ 已經算好擺在呼叫端手上。⚠ 判別力在 **⑧g 的接線鎖**：⑧a~⑧f 全是手餵的 filters，呼叫端的引數哪天被重構掉，它們照樣全綠 |

⚠ **閘門② 在單年 collection 上幾乎沒有判別力**（每組最多 2~3 份 filing，撞不到編碼長度
不同的配對）。跑在 `us_stock_rag_edgar_mdna` 上看到「0 對判反」是**測資不足**不是系統健康
——同 CLAUDE.md「稽核回傳 0 筆問題先當壞消息查」。要有判別力請跑多年 collection。

用法：
    .venv/Scripts/python.exe eval/verify_period_intent_routing.py
    RAG_COLLECTION=us_stock_rag_edgar_multiyear .venv/Scripts/python.exe \\
        eval/verify_period_intent_routing.py
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq  # noqa: E402

_P, _F = 0, 0


def ck(name, got, want):
    global _P, _F
    if got == want:
        _P += 1
        print(f"  PASS  {name}")
    else:
        _F += 1
        print(f"  FAIL  {name}\n         got ={got}\n         want={want}")


def gate1_sort_key():
    print("\n── 閘門① `_fiscal_sort_key` 真值表 ──")
    k = rq._fiscal_sort_key
    ck("FY 排在 Q4 之後（年報涵蓋整年，比同年任何一季新）", k("2026", "FY") > k("2026", "Q4"), True)
    ck("Q1<Q2<Q3<Q4", [k("2026", q) for q in ("Q1", "Q2", "Q3", "Q4")],
       [(2026, 1), (2026, 2), (2026, 3), (2026, 4)])
    ck("年份優先於期別（FY2025 FY 比 FY2026 Q1 舊）", k("2025", "FY") < k("2026", "Q1"), True)
    ck("NVDA 反轉：FY2026 FY < FY2027 Q1", k("2026", "FY") < k("2027", "Q1"), True)
    ck("小寫/空白容忍", k(" 2026 ", " q3 "), (2026, 3))
    ck("期別缺漏 → None（不參與比較）", k("2026", None), None)
    ck("期別不合法 → None", k("2026", "H1"), None)
    ck("年份缺漏 → None", k(None, "FY"), None)
    ck("年份非四位 → None", k("26", "FY"), None)


def gate2_ladder(client):
    print("\n── 閘門② ladder 排序 vs 期碼字串比大小（判別力來源）──")
    lad = rq._get_period_ladder(client)
    ck("ladder 掃得到東西", bool(lad), True)
    if not lad:
        return
    # 每個 ticker 內必須嚴格由新到舊
    bad_order = [tk for tk, rows in lad.items()
                 if any(a["rank"] <= b["rank"] for a, b in zip(rows, rows[1:]))]
    ck("每個 ticker 內嚴格由新到舊、無並列", bad_order, [])

    inverted = []
    for tk, rows in lad.items():
        for a, b in itertools.combinations(rows, 2):        # rows 已降序 → a 必定較新
            if (a["rank"] > b["rank"]) != (a["period_code"] > b["period_code"]):
                inverted.append((tk, a["source"], a["period_code"], b["source"], b["period_code"]))
    total = sum(len(r) * (len(r) - 1) // 2 for r in lad.values())
    print(f"    期碼字串比大小會判反的配對：{len(inverted)}/{total}")
    for row in inverted[:3]:
        print(f"      {row[0]}: {row[1]}({row[2]}) 實際較新，字串比大小卻說 {row[3]}({row[4]}) 較新")
    if inverted:
        ck("→ 這份語料對閘門② 有判別力（存在字串比大小判反的配對）", True, True)
    else:
        print("    ⚠ 一對都沒有 → **測資不足，不是系統健康**。每組 filing 太少或編碼長度沒有"
              "混到，這道閘門在這個 collection 上等於沒有作用。要判別力請跑多年 collection。")


def gate3_ladder_pick(client):
    print("\n── 閘門③ `ladder_pick` 真值表 ──")
    lad = rq._get_period_ladder(client)
    tk = next((t for t, rows in lad.items() if len(rows) >= 3), None)
    if tk is None:
        print("    ⚠ 找不到有 3 份以上 filing 的 ticker，跳過（測資不足）")
        return
    rows = lad[tk]
    ck(f"{tk}: nth=0 就是 ladder 第一筆", rq.ladder_pick(lad, tk)["source"], rows[0]["source"])
    ck(f"{tk}: nth=1 就是第二筆", rq.ladder_pick(lad, tk, nth=1)["source"], rows[1]["source"])
    q = [r for r in rows if r["kind"] == "10-Q"]
    if q:
        ck(f"{tk}: kind=10-Q 只在 10-Q 裡挑",
           rq.ladder_pick(lad, tk, kind="10-Q")["source"], q[0]["source"])
    k10 = [r for r in rows if r["kind"] == "10-K"]
    if k10:
        ck(f"{tk}: kind=10-K 只在 10-K 裡挑",
           rq.ladder_pick(lad, tk, kind="10-K")["source"], k10[0]["source"])
        y = k10[0]["fiscal_year"]
        ck(f"{tk}: year={y} 限定財年",
           rq.ladder_pick(lad, tk, kind="10-K", year=y)["source"], k10[0]["source"])
    # ⚠ 挑不到一律 None：退而求其次會讓答案帶著引用一起錯
    ck("不存在的年份 → None（不可退而求其次）",
       rq.ladder_pick(lad, tk, kind="10-Q", year=1999), None)
    ck("不存在的 ticker → None", rq.ladder_pick(lad, "ZZZZ"), None)
    ck("nth 超出範圍 → None", rq.ladder_pick(lad, tk, nth=999), None)
    ck("nth 負數 → None", rq.ladder_pick(lad, tk, nth=-1), None)


def gate4_period_ref_to_filter(client):
    print("\n── 閘門④ `period_ref` → filter 對應 ──")
    f = rq._resolve_period_filter_llm
    Q1 = "Microsoft 最新一季的營收成長率是多少？"
    latest = {"period_ref": "latest", "fiscal_year": None, "granularity": "quarter"}
    got = f(Q1, client, latest)
    ck("latest+quarter → 產生 report_period_code filter",
       (got or {}).get("field"), "report_period_code")
    ck("latest 產生的 filter 帶 routed_latest（Tier1 才會放寬給 News/Fundamentals）",
       (got or {}).get("routed_latest"), True)
    lad = rq._get_period_ladder(client)
    exp = rq.ladder_pick(lad, "MSFT", kind="10-Q")
    if exp:
        ck("latest+quarter 挑到的就是 ladder 的最新 10-Q",
           (got or {}).get("value"), exp["period_code"])
    expk = rq.ladder_pick(lad, "MSFT", kind="10-K")
    if expk:
        ck("latest+annual 改挑最新 10-K",
           (f(Q1, client, {"period_ref": "latest", "fiscal_year": None,
                           "granularity": "annual"}) or {}).get("value"), expk["period_code"])
    # 只有 latest 該產生 filter
    for ref in ("absolute", "range", "none"):
        ck(f"period_ref={ref} → 不注入 filter",
           f(Q1, client, {"period_ref": ref, "fiscal_year": 2026, "granularity": "quarter"}), None)
    # 多／零公司：build_qdrant_filter 是 flat AND，寫不出 per-ticker 的 OR-of-ANDs
    ck("兩家公司 → None（MatchAny 是全域 OR，會放行 A 公司的舊季）",
       f("比較 Microsoft 和 Apple 最新一季的營收", client, latest), None)
    ck("零公司 → None", f("最新一季的營收成長率是多少？", client, latest), None)
    # 指定年份挑不到 → 放掉年份重試
    got_y = f(Q1, client, {"period_ref": "latest", "fiscal_year": 1999, "granularity": "quarter"})
    ck("指定不存在的年份 → 退回該 kind 的最新，而不是整個放棄",
       (got_y or {}).get("value"), (exp or {}).get("period_code"))
    # ⚠ granularity 缺漏必須落到 10-Q，不是「所有類型裡最新的」。實測 col-11
    #   「微軟的雲端服務最近成長得快不快？」不設限會挑到年報 `MSFT_10K_2026`，
    #   把 gold 的 `MSFT_10Q_202603` 擠掉（gold_rank 1 → None）。這條是那次的回歸鎖。
    ck("granularity 缺漏 → 落到最新 10-Q（不是年報、也不是兩者混挑）",
       (f(Q1, client, {"period_ref": "latest", "fiscal_year": None,
                       "granularity": None}) or {}).get("value"), (exp or {}).get("period_code"))
    ck("granularity 缺漏挑到的**不是**最新 10-K",
       (f(Q1, client, {"period_ref": "latest", "fiscal_year": None,
                       "granularity": None}) or {}).get("value") != (expk or {}).get("period_code"),
       True)


def gate5_collapse_skip():
    print("\n── 閘門⑤ `range` → 跳過 collapse 的接線 ──")
    # ⚠ 這是生產判斷式的**抄寫不是 import**（它埋在 retrieve() 中段，中途攔不下來）。
    #    改 rag_query 那一行務必同步改這裡。
    def _skip(intent):
        return bool(intent and intent.get("period_ref") == "range")
    ck("range → 跳過", _skip({"period_ref": "range"}), True)
    for ref in ("latest", "absolute", "none"):
        ck(f"{ref} → 不跳過", _skip({"period_ref": ref}), False)
    ck("intent 為 None（LLM 路徑沒開）→ 不跳過，維持原行為", _skip(None), False)
    src = Path(rq.__file__).read_text(encoding="utf-8")
    ck("生產碼裡確實有這條接線（抄寫沒有漂移）",
       '_period_intent.get("period_ref") == "range"' in src, True)


class _P0:
    """最小的假 point：只需要 payload。"""

    def __init__(self, **payload):
        self.payload = payload


def gate6_tier1_label_year(client):
    print("\n── 閘門⑥ Tier 1 的 label-year 命中資格（`tier1_hit_is_qualified`）──")
    Y = [{"field": "fiscal_year", "value": "2025", "polarity": "include"},
         {"field": "ticker", "value": "MSFT", "polarity": "include"}]
    q = rq.tier1_hit_is_qualified

    # ⑥a 真值表（合成點）。危險的方向是**放行**：讓一份別的財年的季報冒充年度答案，
    #     而且連 Tier 2 的揭露語都不會出現。所以第一條是那條。
    ck("⑥a 只靠 label-year 命中（財年不符）→ 不合格",
       q(Y, [_P0(fiscal_year="2026", report_label_year="2025"),
             _P0(fiscal_year="2026", report_label_year="2025")]), False)
    ck("⑥a 有任一點的 fiscal_year 相符 → 合格（label-year 只是放寬，不影響這裡）",
       q(Y, [_P0(fiscal_year="2026", report_label_year="2025"),
             _P0(fiscal_year="2025", report_label_year="2025")]), True)
    ck("⑥a **誤殺對照**：fiscal_year 為空的點（News/Fundamentals，relax_empty_fields "
       "刻意放行的 col-15 那條路）→ 合格",
       q(Y, [_P0(fiscal_year=None, report_label_year=None)]), True)
    ck("⑥a 誤殺對照：payload 根本沒有 fiscal_year 這個 key → 合格",
       q(Y, [_P0(source="AAPL_Fundamentals.txt")]), True)
    ck("⑥a 沒有 fiscal_year 約束 → 一律合格（不干預既有行為）",
       q([{"field": "ticker", "value": "MSFT", "polarity": "include"}],
         [_P0(fiscal_year="2026")]), True)
    ck("⑥a exclude 極性的 fiscal_year 不算約束",
       q([{"field": "fiscal_year", "value": "2025", "polarity": "exclude"}],
         [_P0(fiscal_year="2026")]), True)
    ck("⑥a 多值（MatchAny）：命中其中一個就合格",
       q([{"field": "fiscal_year", "value": ["2025", "2026"], "polarity": "include"}],
         [_P0(fiscal_year="2026")]), True)
    ck("⑥a 空候選 → 不合格（呼叫端本來就會走 Tier 2，這裡不可回 True）",
       q(Y, []), False)

    # ⑥b **這一組才是照到真實語料的**：⑥a 全部餵合成 payload，若哪天 ingest 改成
    #     「10-K 的 report_label_year 也可能與 fiscal_year 不同」，⑥a 照樣全綠，而這個
    #     修法的整個推理前提會崩掉（那時 label-year 那半個 OR 就不只放行 10-Q 了）。
    #     ⚠ 同時要有「至少一份 10-Q 兩者不同」——否則這個修法沒有作用對象，
    #     ⑥a 綠燈只是套套邏輯（同 CLAUDE.md：稽核 0 筆問題先當壞消息查）。
    print("  ── ⑥b 語料的結構前提（實際 payload，不是合成的）──")
    tenk_diff, tenq_diff, tenq_total = [], [], 0
    seen: dict = {}
    off = None
    while True:
        pts, off = client.scroll(
            collection_name=rq.COLLECTION_NAME, limit=1024, offset=off, with_vectors=False,
            with_payload=["source", "filing_type", "fiscal_year", "report_label_year"])
        for p in pts:
            pl = p.payload or {}
            if pl.get("filing_type") in ("10-K", "10-Q"):
                seen[pl.get("source")] = (pl["filing_type"], pl.get("fiscal_year"),
                                          pl.get("report_label_year"))
        if off is None:
            break
    for src, (ft, fy, rl) in seen.items():
        if ft == "10-Q":
            tenq_total += 1
        if fy != rl:
            (tenk_diff if ft == "10-K" else tenq_diff).append(src)
    ck(f"⑥b **10-K 的 fiscal_year 一律等於 report_label_year**（{len(seen)} 份 filing 全掃）"
       f"——這是「label-year 那半個 OR 只會放行 10-Q」的唯一依據",
       tenk_diff, [])
    ck(f"⑥b 至少一份 10-Q 兩者不同（{len(tenq_diff)}/{tenq_total}）＝這個修法有作用對象",
       bool(tenq_diff), True)
    print(f"       兩者不同的 10-Q：{sorted(tenq_diff)}")

    # ⑥c 接線沒有被拆掉（同閘門⑤ 的作法：抄寫會漂移，這裡驗生產碼真的有呼叫）
    src_txt = Path(rq.__file__).read_text(encoding="utf-8")
    ck("⑥c 生產碼的 Tier 1 真的有呼叫這個資格判斷",
       "not tier1_hit_is_qualified(tier1_key_filters, fused)" in src_txt, True)

    # ⑥d 降級揭露語不可自相矛盾。⑥ 把更多曆年查詢趕進 Tier 2，於是這句話開始被看見：
    #     `MSFT_10Q_202512` 的 fiscal_year=2026、report_label_year=2025，問 FY2025 降級後
    #     `actual` 裡會**同時出現 2025** → 舊措辭是「沒有 2025 的資料，改用最接近的（…2025）」。
    #     ⚠ 這句同時是使用者可見句與注入 generator 的事實，說錯比不說更糟。
    note = rq._build_fallback_note(Y, [_P0(fiscal_year="2026", report_label_year="2025",
                                           report_period_code="202512")])
    ck("⑥d 年份降級的揭露語要說明是**財年**（否則與 actual 裡的曆年標籤自相矛盾）",
       "所詢問財年（2025）" in note, True)
    note_code = rq._build_fallback_note(
        [{"field": "report_period_code", "value": "202509", "polarity": "include"}],
        [_P0(fiscal_year="2026", report_period_code="202512")])
    ck("⑥d 對照：期碼降級仍然說「期間」，措辭沒有被改壞",
       "所詢問期間（202509）" in note_code, True)
    ck("⑥d 對照：沒有年份／期碼約束 → 不算 fallback，回空字串",
       rq._build_fallback_note([{"field": "ticker", "value": "MSFT",
                                 "polarity": "include"}], [_P0(fiscal_year="2026")]), "")


def gate7_ticker_resolution():
    print("\n── 閘門⑦ 實體解析：LLM 只在 regex 沉默時被叫，且只補不覆寫 ──")
    # 病灶：`AWS 在 2022 年的淨銷售額` 抽不到 ticker → 無 hard filter → Tier 3 跨公司污染。
    # ⚠ 這道**全部零 LLM**：把 `resolve_tickers_llm` 換成計數樁，量的是**接線**不是準確度。
    #   準確度是含 LLM、非零噪音的，那支在 `eval/probe_ticker_resolution.py`。
    calls: list[str] = []

    def stub(q, model_name=None):
        calls.append(q)
        return {"AWS 在 2022 年的淨銷售額": ["AMZN"],
                "AWS 跟 Azure 誰成長比較快": ["AMZN", "MSFT"],
                "這一季景氣好嗎": []}.get(q, [])

    _orig = rq.resolve_tickers_llm
    rq.resolve_tickers_llm = stub
    try:
        def tick(q):
            calls.clear()
            fs = rq.parse_query_filters(q)
            return ([f["value"] for f in fs if f["field"] == "ticker"] or [None])[0], len(calls)

        # ⑦a 陽性：regex 沉默 → LLM 被叫、結果變成 ticker filter
        ck("⑦a 產品名 → 解出母公司 ticker，且 LLM 被叫 1 次",
           tick("AWS 在 2022 年的淨銷售額"), ("AMZN", 1))
        ck("⑦a 多家 → list（`build_qdrant_filter` 靠形狀分 MatchValue/MatchAny）",
           tick("AWS 跟 Azure 誰成長比較快"), (["AMZN", "MSFT"], 1))

        # ⑦b **誤報對照（判別力在這裡）**：regex 抽得到就**完全不呼叫 LLM**。
        #    寫成「都叫、再合併」的話這兩條照樣會過，而每個 query 都會多燒一次 LLM，
        #    且 LLM 有機會推翻字面公司名——那是比省錢更重要的理由。
        ck("⑦b 字面公司名 → LLM 一次都不叫（regex 已高精度命中）",
           tick("Amazon 2022 年的淨銷售額"), ("AMZN", 0))
        ck("⑦b 中文別名同理", tick("亞馬遜 2022 年的淨銷售額"), ("AMZN", 0))

        # ⑦c LLM 說「不是任何一家」→ 必須真的沒有 ticker filter（不可退而求其次猜一家）
        ck("⑦c LLM 回空 → 沒有 ticker filter", tick("這一季景氣好嗎"), (None, 1))

        # ⑦d kill switch：關掉之後行為必須逐字退回本修法之前
        import os
        os.environ["RQ_TICKER_LLM"] = "0"
        try:
            ck("⑦d RQ_TICKER_LLM=0 → 不叫 LLM、也不產生 ticker filter",
               tick("AWS 在 2022 年的淨銷售額"), (None, 0))
        finally:
            os.environ.pop("RQ_TICKER_LLM", None)
    finally:
        rq.resolve_tickers_llm = _orig

    # ⑦e 封閉集合：LLM 吐 KB 以外的東西一律丟掉，**不做模糊比對**。
    #    危險方向是「猜」——鎖到錯的公司比不鎖更糟（答案會帶著引用一起錯）。
    _orig_call = rq.call_llm
    try:
        for label, raw, want in [
            ("正常輸出", '{"tickers": ["AMZN"]}', ["AMZN"]),
            ("KB 以外的 ticker（AMD）整個丟掉", '{"tickers": ["AMD", "NVDA"]}', ["NVDA"]),
            ("公司名不是 ticker → 丟掉，不要猜它想講誰", '{"tickers": ["Amazon"]}', []),
            ("小寫照收（大小寫正規化）", '{"tickers": ["amzn"]}', ["AMZN"]),
            ("重複去重保序", '{"tickers": ["MSFT", "AMZN", "MSFT"]}', ["MSFT", "AMZN"]),
            ("解不出 JSON → 回空（不是回 None、不是丟例外）", 'sorry I cannot', []),
            ("tickers 不是 list → 回空", '{"tickers": "AMZN"}', []),
        ]:
            rq.call_llm = lambda *a, _r=raw, **k: _r
            ck(f"⑦e {label}", rq.resolve_tickers_llm("x"), want)
        # LLM 整個掛掉 → 回空，不可讓實體解析變成單點故障
        def _boom(*a, **k):
            raise RuntimeError("nim 504")
        rq.call_llm = _boom
        ck("⑦e LLM 拋例外 → 回空（不是單點故障）", rq.resolve_tickers_llm("x"), [])
    finally:
        rq.call_llm = _orig_call

    # ⑦f 封閉集合的**單一來源**：`_KB_TICKERS` 必須由 `_COMPANY_TICKER` 導出。
    #     另抄一份的話，哪天加減公司兩邊會漂移（本 repo 已經因為抄寫吃過三次虧）。
    ck("⑦f _KB_TICKERS 就是 _COMPANY_TICKER 的值域",
       set(rq._KB_TICKERS), set(rq._COMPANY_TICKER.values()))
    # 而且它要真的對得上 KB：prompt 裡列的七家是照這個集合寫的
    ck("⑦f prompt 列的七家與 _KB_TICKERS 一致",
       sorted(t for t in rq._KB_TICKERS if t in rq.TICKER_RESOLUTION_SYSTEM_PROMPT),
       sorted(rq._KB_TICKERS))


def gate8_period_reads_resolved_ticker(client):
    print("\n── 閘門⑧ 期間路由讀的是**已解出的 ticker**，不是自己重跑 regex ──")
    # 病灶（2026-08-28）：同一次 `retrieve()` 裡有兩條管線都需要「這是哪家公司」。
    #   A `parse_query_filters()` → regex ＋（沉默時）LLM 實體解析 → 解得出 AWS = AMZN
    #   B `_resolve_period_filter_llm()` → **自己重跑一次 regex** → 看不到 AWS → 不路由
    # 也就是說答案早就算好擺在呼叫端手上,B 卻只用了比較弱的那半個來源。這道鎖的是接線。
    f = rq._resolve_period_filter_llm
    latest = {"period_ref": "latest", "fiscal_year": None, "granularity": "quarter"}
    lad = rq._get_period_ladder(client)
    amzn = rq.ladder_pick(lad, "AMZN", kind="10-Q")
    msft = rq.ladder_pick(lad, "MSFT", kind="10-Q")

    # ⑧a 真陽性：query 裡沒有任何**字面**公司名,ticker 只存在於已解出的 filters 裡
    Q_AWS = "AWS 最新一季的營收成長率是多少？"
    ck("⑧a 前提：這個 query 的 regex 確實抽不到 ticker（不然這道在測別的東西）",
       rq._find_all_ticker_aliases(Q_AWS.lower(), Q_AWS), [])
    fx = [{"field": "ticker", "value": "AMZN", "polarity": "include"}]
    if amzn:
        ck("⑧a 帶著已解出的 ticker → 路由到該公司最新 10-Q",
           (f(Q_AWS, client, latest, fx) or {}).get("value"), amzn["period_code"])
    # ⚠ 這條是**修法有作用**的證明（＝真陽性對照）。少了它,一個把 filters 參數整個忽略的
    #   實作也會讓 ⑧b~⑧f 全綠。
    ck("⑧a 對照：不傳 filters（＝修法之前）→ 仍然路由不到,回 None",
       f(Q_AWS, client, latest), None)

    # ⑧b **誤報對照**：regex 抽得到時,行為必須逐字不變。危險方向是「順手改壞既有路由」。
    Q_MS = "Microsoft 最新一季的營收成長率是多少？"
    # ⚠ 這裡刻意不叫 `parse_query_filters()`（那含 LLM,本檔是零 LLM 閘門）。regex 抽得到時
    #   它的 ticker 就是 `_extract_structural_filters` 抽的那一個,`_append_llm_ticker` 只補不覆寫。
    fx_ms = rq._extract_structural_filters(Q_MS)
    ck("⑧b 誤報對照：regex 抽得到 → 傳 filters 與不傳的結果**逐字相同**",
       f(Q_MS, client, latest, fx_ms), f(Q_MS, client, latest))
    if msft:
        ck("⑧b 而且那個結果仍然是對的（MSFT 最新 10-Q）",
           (f(Q_MS, client, latest, fx_ms) or {}).get("value"), msft["period_code"])

    # ⑧c~⑧e 三條「不猜」的界線。危險方向全部相同：**鎖到別人的財報**——答案會帶著正當的
    #   引用一起錯,那比「不鎖、退回 Tier 3」更糟。
    ck("⑧c 多公司（MatchAny）→ None（flat AND 寫不出 per-ticker 的 OR-of-ANDs）",
       f(Q_AWS, client, latest,
         [{"field": "ticker", "value": ["AMZN", "MSFT"], "polarity": "include"}]), None)
    ck("⑧d exclude 的 ticker 不算鎖定（『不要 AAPL』讀成『鎖定 AAPL』是反向錯誤）",
       f(Q_AWS, client, latest,
         [{"field": "ticker", "value": "AAPL", "polarity": "exclude"}]), None)
    ck("⑧e 兩條互相矛盾的 include ticker → None,不挑第一個",
       f(Q_AWS, client, latest,
         [{"field": "ticker", "value": "AMZN", "polarity": "include"},
          {"field": "ticker", "value": "MSFT", "polarity": "include"}]), None)
    ck("⑧e 對照：同一個 ticker 出現兩次 → 仍然算恰好一家",
       rq._sole_ticker([{"field": "ticker", "value": "AMZN", "polarity": "include"},
                        {"field": "ticker", "value": "AMZN", "polarity": "include"}], ""), "AMZN")
    ck("⑧e 對照：filters 裡沒有 ticker → 退回 regex（語意等同修法之前）",
       rq._sole_ticker([{"field": "fiscal_year", "value": "2026", "polarity": "include"}], Q_MS),
       "MSFT")
    ck("⑧e 對照：filters=None → 退回 regex", rq._sole_ticker(None, Q_MS), "MSFT")

    # ⑧f 多了 filters 不代表可以開始注入——只有 latest 該產生 filter（同閘門④ 的界線）
    for ref in ("absolute", "range", "none"):
        ck(f"⑧f period_ref={ref} → 即使 filters 給了 ticker 也不注入",
           f(Q_AWS, client, {"period_ref": ref, "fiscal_year": 2026, "granularity": "quarter"},
             fx), None)

    # ⑧g **接線鎖**：⑧a~⑧f 全部是拿手餵的 filters 在測。哪天有人重構掉呼叫端那個引數,
    #   上面每一條照樣全綠而生產又回到「兩條管線各自認公司」。所以要驗生產碼真的傳了。
    #   （同閘門⑤⑥c 的作法：抄寫會漂移,驗原始碼。）
    src = Path(rq.__file__).read_text(encoding="utf-8")
    ck("⑧g 生產碼的 retrieve() 真的把 detected_filters 傳給期間路由",
       "_resolve_period_filter_llm(query, client, _period_intent, detected_filters)" in src, True)
    # ⑧h 舊路徑刻意**不接**：`RQ_PERIOD_INTENT_LLM=0` 的用途是「逐字退回修法之前」做 A/B,
    #    接上實體解析就不再是乾淨的對照臂。這條鎖的是「別好心順手把它也改了」。
    ck("⑧h 對照：舊路徑 `_resolve_latest_quarter_filter` 維持只吃 (query, client)",
       "_resolve_latest_quarter_filter(query, client)" in src, True)


def main() -> int:
    client = rq.make_qdrant_client()
    print(f"collection = {rq.COLLECTION_NAME}")
    gate1_sort_key()
    gate2_ladder(client)
    gate3_ladder_pick(client)
    gate4_period_ref_to_filter(client)
    gate5_collapse_skip()
    gate6_tier1_label_year(client)
    gate7_ticker_resolution()
    gate8_period_reads_resolved_ticker(client)
    print(f"\nPASS {_P}  FAIL {_F}")
    print("GATE: " + ("PASS" if _F == 0 else "FAIL"))
    return 1 if _F else 0


if __name__ == "__main__":
    raise SystemExit(main())
