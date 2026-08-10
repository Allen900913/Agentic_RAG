"""確定性數字缺陷檢查（零 LLM、零噪音）——本專案「數字答錯」類改動的**主要驗收指標**。

**為什麼不用 RAGAS 驗收**：2026-08-09 實測 judge 噪音與最小可偵測效應（MDE, 95%, n=100）
——recall 0.029 / precision 0.046 / faithfulness 0.028 / correctness 0.019，換算成「要幾題
從 0 修到 0.5」是 **4~7 題**。而 ingest／檢索層的單一改動通常只影響 2~5 題，**在原理上就量
不出來**。加上 Plan 節點與 query 英譯在 `temperature=0` 下仍不確定（48% / 47% 不一致），
單次 A/B 兩臂差異有近半跟被測改動無關。詳見 memory `ragas-judge-noise-floor`、
`pipeline-nondeterminism-temp0`。

所以：**能用確定性指標驗收的改動，就不要用 RAGAS 驗收。**

## 主指標＝※ 接地主張斷言（2026-08-10 加，`eval/number_claims.json`）

**為什麼需要它——「比第幾個數字」兩種寫法都被實測推翻**（2026-08-09/10）：

| 比法 | mix-03（確診答錯，答 24% 而合併是 20%） | 判斷 |
|---|---|---|
| 比**第一個**百分比（舊 ①） | 抓到 ✅ | 但 col-11／mi-04 誤報——它們把答案的頭條挑成 Azure 40%／LTM 12.8%，**內容其實對** |
| 比**任一個**百分比 | **漏抓 ❌** | 答案裡的 21%（Productivity 分部）落在 gold 20% 的 ±1pt 內 |

→ 答案裡有 4~8 個百分比、容差 ±1pt，**任何值幾乎都能湊到**，所以「值有沒有出現」不帶資訊。
唯一可靠的確定性比法是**把數字接地到主張**：anchor 命中後 N 字元內的第一個百分比。
實測對 mix-03 FAIL、col-11 PASS、mi-04 PASS，與人工獨立判讀 3/3 一致。

**三態，缺一不可**：PASS ／ FAIL ／ **anchor 未命中＝無法比對**。第三態不能併進 PASS——
否則答案改寫措辭或拒答時會**靜默變成通過**。

## 輔助指標（篩選用，不當閘門）

  ① 頭條百分比**候選**：⚠ 實測 6 個旗標裡有 3 個不是錯（col-11／mi-04／mi-08）。這一節只
     用來**發現尚未登錄的新缺陷**，讀完確認是真缺陷才登錄進 `number_claims.json`。
     ⚠ **「某個 run 沒給百分比」不是衝突**，是無法比對（獨立第三桶）。2026-08-09 就因為把
     它算成衝突，兩檔並列得到 10、單檔卻是 6／4／5，害我把閘門門檻設在一個不可比的數字上。
  ② 每個候選值的**來源類型**：把該數字拿回 contexts 定位，看它出現在 news 還是財報／
     Fundamentals。實測錯誤數字**都在原文裡找得到（零捏造）**——所以病不是幻覺、
     也不是檢索找不到，是「同一指標有多個版本時挑錯」。
  ③ 跨 run 穩定性：判斷差異是系統性錯誤還是抽樣噪音。實測 8/30 題的頭條百分比會自己變，
     **所以單一題的變化不可歸因於任何改動**。

用法：
    python eval/check_number_defects.py --results experiments/agentic/gj_v2_period_replay1.json
    python eval/check_number_defects.py --results A.json B.json     # 多份併排（看穩定錯 vs 隨機錯）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLD_PATH = Path("eval/reference_answers.json")
CLAIMS_PATH = Path("eval/number_claims.json")
FOOTER = "\n\n---\n📚 引用來源"
INLINE_CIT = re.compile(r"\s*【[^】]*?\.(?:html|txt)[^】]*?】")
PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# 「問題明確要數字」的字面訊號。不含這些字的開放式題（「策略是什麼」）不該用數字判對錯。
ASK_NUMBER = re.compile(r"成長|增加|多少|幅度|成長率|比例|是多少|水準")
AUTHORITATIVE = ("10-K", "10-Q", "income_statement", "fundamentals")


def answer_body(text: str) -> str:
    """剝掉引用 metadata（尾端 footer ＋ 句末 inline 標記），只留答案本文。"""
    if not text:
        return ""
    i = text.find(FOOTER)
    return INLINE_CIT.sub("", text[:i] if i != -1 else text)


def source_types_of(value: float, contexts: list[str], sources: list[str]) -> list[str]:
    """這個百分比出現在哪些來源類型的 chunk 裡（字串定位，零 LLM）。

    小數表示（0.166 = 16.6%）**只在 fundamentals 比對**：全庫實測只有它把比率寫成小數，
    10-K/10-Q 的 `0.xx` 是債券票面利率與每股金額、news 的是股價漲跌，在那些來源比對小數
    會配到完全無關的東西。同 `agentic_rag_v2._ground_source_type` 的理由。
    """
    from rag_query import infer_source_type
    pats = [f"{value:g}%", f"{value:g} percent", f"{value:g} percentage points"]
    dec = f"{value / 100:g}"
    hits = set()
    for i, body in enumerate(contexts):
        st = infer_source_type(sources[i] if i < len(sources) else "")
        if any(p in body for p in pats) or (st == "fundamentals" and dec in body):
            hits.add(st)
    return sorted(hits)


def anchored_pct(text: str, anchor: str, window: int = 60):
    """把百分比**接地到主張**：anchor 命中後 window 字元內的第一個百分比。

    **先往後找,找不到才往前找**（不對稱是刻意的）：中英文的財務句主流是
    「<主體><指標>成長 N%」,數值在描述之後,所以往後找優先;但中文也寫得出
    「**16.6% 的年度（YoY）成長**」把數值放在前面（實測 mi-04 兩個 run 就是這樣寫,
    純往後找會全部退化成 N/A）。往前找容易撈到**上一個主張**的數值,所以只當退路,
    而且窗口砍半。
    多處命中時回傳第一處——同一個主張在答案裡重複陳述時數值應一致,不一致本身就是缺陷
    （由 agentic 的一致性 validator 負責,不是這裡）。
    回傳 (值, 證據片段) 或 None（＝anchor 未命中,**無法比對,不算 FAIL**）。
    """
    for mo in re.finditer(anchor, text):
        seg = text[mo.end():mo.end() + window]
        p = PCT.search(seg)
        if p:
            return float(p.group(1)), re.sub(r"\s+", " ", seg[:p.end()])
    for mo in re.finditer(anchor, text):
        seg = text[max(0, mo.start() - window // 2):mo.start()]
        hits = PCT.findall(seg)
        if hits:                                   # 取最靠近 anchor 的那一個
            return float(hits[-1]), "(往前) " + re.sub(r"\s+", " ", seg[-60:])
    return None


def load_results(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {r["id"]: r for r in data.get("records", [])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", nargs="+", required=True, help="一或多份 generation_judge 結果檔")
    ap.add_argument("--gold", default=str(GOLD_PATH))
    ap.add_argument("--claims", default=str(CLAIMS_PATH))
    ap.add_argument("--tolerance", type=float, default=1.0, help="百分比容許差（百分點）")
    args = ap.parse_args()

    gold = {x["id"]: x for x in json.loads(Path(args.gold).read_text(encoding="utf-8"))}
    runs = [(Path(p).name, load_results(Path(p))) for p in args.results]

    # ══ ※ 主指標：接地主張斷言（零 LLM、三態）═══════════════════════════════════
    print("=" * 92)
    print("※ 接地主張斷言（主指標）——anchor 命中後的第一個百分比 vs 人工驗證過的正解")
    claims = json.loads(Path(args.claims).read_text(encoding="utf-8")) \
        if Path(args.claims).exists() else []
    tally = {n: {"PASS": 0, "FAIL": 0, "N/A": 0} for n, _ in runs}
    for c in claims:
        print(f"  [{c['status']:16s}] {c['id']:8s} {c['claim']}"
              f"（正解 {c['expect_pct']:g}%"
              + (f"，禁止 {c['forbid_pct']:g}%" if c.get("forbid_pct") is not None else "") + "）")
        for name, res in runs:
            rec = res.get(c["id"])
            if not rec:
                tally[name]["N/A"] += 1
                print(f"      {name[:28]:30s} N/A  該題不在結果檔裡")
                continue
            body = answer_body(rec.get("answer") or "")
            got = anchored_pct(body, c["anchor"], c.get("window", 60))
            # `expect_text`／`forbid_text`：**金額**字串的獨立判準,可在 anchor 未命中時接手。
            # 為什麼加：mix-03 修好後答案寫「營業利益較去年同期增加 64 億美元，增幅約 20%」,
            # 完全沒用「整體/合併」字樣 → anchor 未命中 → 只有 anchor 的話會退化成 N/A,
            # 給不出「修好了」的判定。金額（64 億／$6.4 billion vs 27 億／$2.7 billion）在
            # 單一題目內幾乎不會撞,而百分比會（見本檔開頭的 ±1pt 巧合問題）。
            txt_ok = txt_bad = None
            if c.get("expect_text"):
                txt_ok = bool(re.search(c["expect_text"], body))
            if c.get("forbid_text"):
                txt_bad = bool(re.search(c["forbid_text"], body))
            if got is None and txt_ok is None:
                # ⚠ 不能算 PASS：答案改寫措辭或拒答時會靜默變成通過
                tally[name]["N/A"] += 1
                print(f"      {name[:28]:30s} N/A  anchor {c['anchor']!r} 未命中（無法比對）")
                continue
            checks, ev = [], ""
            if got is not None:
                v, ev = got
                checks.append(abs(v - c["expect_pct"]) <= args.tolerance)
                if c.get("forbid_pct") is not None:
                    checks.append(abs(v - c["forbid_pct"]) > args.tolerance)
                ev = f"讀到 {v:g}%  …{ev[:60]}"
            else:
                ev = f"anchor 未命中，改用金額判準"
            if txt_ok is not None:
                checks.append(txt_ok)
                ev += f"  expect_text={'ok' if txt_ok else 'MISS'}"
            if txt_bad is not None:
                checks.append(not txt_bad)
                ev += f"  forbid_text={'HIT' if txt_bad else 'ok'}"
            verdict = "PASS" if all(checks) else "FAIL"
            tally[name][verdict] += 1
            print(f"      {name[:28]:30s} {verdict}  {ev}")
    print()
    for name, t in tally.items():
        print(f"  {name[:40]:42s} PASS {t['PASS']}  FAIL {t['FAIL']}  N/A {t['N/A']}"
              f"   （共 {len(claims)} 條主張）")
    print("  [!] N/A 不是通過：anchor 未命中代表這一輪的答案沒有陳述該主張，要人工看一眼。")

    print()
    print("=" * 92)
    print("① 頭條百分比**候選**（篩選未登錄的新缺陷用，不是閘門）")
    print("  [!] 實測 6 個旗標裡 3 個不是錯（col-11／mi-04／mi-08）——讀完確認才登錄進 number_claims.json")
    base = runs[0][1]
    rows = []
    for qid, rec in base.items():
        g = gold.get(qid)
        if not g or not ASK_NUMBER.search(rec.get("query") or ""):
            continue
        gp = PCT.findall(g["reference"])
        if not gp:
            continue
        vals = []
        for _, res in runs:
            ap_ = PCT.findall(answer_body((res.get(qid) or {}).get("answer") or ""))
            vals.append(float(ap_[0]) if ap_ else None)
        if all(v is None for v in vals):
            continue
        rows.append((qid, rec.get("category"), float(gp[0]), vals))

    # **每個 run 各自算**。舊版把「所有 run 都要對」當一致 → 傳 2 檔與傳 1 檔的定義不同,
    # 兩檔並列得到 10、單檔卻是 6／4／5,無法互相比較（2026-08-09 踩過）。
    print(f"  {'run':42s}{'可比對':>8s}{'一致':>8s}{'候選':>8s}{'無法比對':>10s}")
    for k, (name, _) in enumerate(runs):
        cmp_ = [r for r in rows if r[3][k] is not None]
        agree = [r for r in cmp_ if abs(r[3][k] - r[2]) <= args.tolerance]
        na = len(rows) - len(cmp_)
        print(f"  {name[:40]:42s}{len(cmp_):8d}{len(agree):8d}"
              f"{len(cmp_) - len(agree):8d}{na:10d}")
    # 候選清單以第一個 run 為準（②的來源定位需要它的 contexts）
    bad = [r for r in rows if r[3][0] is not None and abs(r[3][0] - r[2]) > args.tolerance]
    known = {c["id"] for c in claims}
    print(f"\n  {'id':9s}{'category':13s}{'gold':>8s}" + "".join(f"{n[:14]:>16s}" for n, _ in runs)
          + "   已登錄?")
    for qid, cat, gv, vals in bad:
        cells = "".join(f"{(f'{v:g}%' if v is not None else '-'):>16s}" for v in vals)
        print(f"  {qid:9s}{cat or '':13s}{gv:7g}%{cells}"
              f"   {'OK' if qid in known else '← 未登錄，需人工判讀'}")

    print()
    print("=" * 92)
    print("② 候選數字的來源類型（在 contexts 裡找得到嗎？是新聞還是財報？）")
    n_found = n_news_only = 0
    for qid, cat, gv, vals in bad:
        rec = base[qid]
        ctxs = rec.get("contexts") or []
        srcs = [(s.get("source") if isinstance(s, dict) else s) for s in (rec.get("sources") or [])]
        v = vals[0]
        if v is None:
            continue
        st_ans = source_types_of(v, ctxs, srcs)
        st_gold = source_types_of(gv, ctxs, srcs)
        if st_ans:
            n_found += 1
        if st_ans == ["news"]:
            n_news_only += 1
        verdict = ("← 挑了新聞、財報有正解" if st_ans == ["news"] and
                   any(t in AUTHORITATIVE for t in st_gold) else "")
        print(f"  {qid:9s} 答案 {v:g}% 出現在 {st_ans or '（contexts 裡找不到＝可能捏造）'}"
              f"　gold {gv:g}% 出現在 {st_gold or '（找不到）'} {verdict}")
    print(f"\n  衝突數字能在 contexts 裡定位到的: {n_found}/{len(bad)}"
          f"（找不到才是幻覺；找得到＝挑錯來源／期間／範圍）")
    print(f"  其中「答案用新聞值、財報有不同值」的: {n_news_only}  ← R3 財報優先的目標")

    if len(runs) > 1:
        print()
        print("=" * 92)
        print("③ 跨 run 穩定性（同一題兩次跑答案是否一致）——判斷是系統性錯誤還是抽樣噪音")
        unstable = [r for r in rows if len({None if v is None else round(v, 1) for v in r[3]}) > 1]
        print(f"  頭條百分比在不同 run 間改變的題: {len(unstable)}/{len(rows)}")
        for qid, cat, gv, vals in unstable:
            print(f"    {qid:9s} gold={gv:g}%  " +
                  " / ".join(f"{v:g}%" if v is not None else "-" for v in vals))


if __name__ == "__main__":
    main()
