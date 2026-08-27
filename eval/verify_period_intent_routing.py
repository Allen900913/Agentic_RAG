"""驗收期間意圖路由（零 LLM、零網路，只讀 Qdrant 的 payload，秒級）。

**這支測什麼**：`period_ref`（LLM 判的）→ 具體期碼 filter（Python 算的）這條路上，
**Python 那一半**的每一個確定性決策。LLM 判得準不準是另一回事，量法見
`docs/EVAL.md`〈期間意圖解析〉的 57 題交叉表——那個不可能寫成零噪音斷言。

**為什麼需要這支**：整條路的價值全押在「期別誰新誰舊」這個序上，而那個序**踩過坑**：
舊的 `_get_latest_10q_periods` 用 `report_period_code` 字串比大小取 max，10-K 是 4 位年份、
10-Q 是 6 位 yyyymm，混在一起比會**時對時錯**。閘門② 就是把這件事釘死成可重跑的斷言。

## 五道閘門

| # | 測什麼 | 為什麼是它 |
|---|---|---|
| ① | `_fiscal_sort_key` 真值表 | 序的最小單元；缺欄位必須回 None（不參與比較）而不是猜 |
| ② | ladder 排序 **vs 期碼字串比大小** | **有判別力的那一道**：多年語料下實測 385 對有 31 對（8.1%）判反。單年語料下這道會接近 0 對——那不代表沒問題，代表**測資不夠**，見下方警告 |
| ③ | `ladder_pick` 真值表 | 挑不到必須回 None，**不可退而求其次**（挑錯期別比不挑更糟：答案會帶著引用一起錯） |
| ④ | `period_ref` → filter 對應 | 只有 `latest` 該產生 filter；`range` 產生 filter 會讓趨勢題直接答不出來 |
| ⑤ | `range` → 跳過 collapse 的接線 | B 的損害唯一的解法；斷了就回到趨勢題期別數 −38% |

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


def main() -> int:
    client = rq.make_qdrant_client()
    print(f"collection = {rq.COLLECTION_NAME}")
    gate1_sort_key()
    gate2_ladder(client)
    gate3_ladder_pick(client)
    gate4_period_ref_to_filter(client)
    gate5_collapse_skip()
    gate6_tier1_label_year(client)
    print(f"\nPASS {_P}  FAIL {_F}")
    print("GATE: " + ("PASS" if _F == 0 else "FAIL"))
    return 1 if _F else 0


if __name__ == "__main__":
    raise SystemExit(main())
