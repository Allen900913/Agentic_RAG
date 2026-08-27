"""check_historical_generation.py — 多年語料在**生成端**買到了什麼？（零 LLM、零網路，只讀結果檔）

`probe_historical_benefit.py` 量的是**檢索**：gold 有沒有進 top-k（0/20 → 18/20）。這支量的是
**使用者實際看到的東西**——同一批歷史題，答案到底是拒答、答對、還是**拿別的年份硬答**。
拆法同 `probe_news_web_routing.py` 的 ①／②：便宜的必要條件先量，貴的內容半另外做。

**三態，優先序由高到低**（順序是判準的一部分，不可調換）：

  1. `answered`     答案裡有那個 `literal` ＝ 該題的事實真的講出來了。**零噪音**。
     ⚠ 就算答案同時扯了別的年份也算答對——**問到的事實有給到**是這一態的定義。
  2. `admits_gap`   沒答對，但答案**明講「你問的那個期間，這裡沒有」**。誠實行為。
  3. `silent_wrong` **危險態**：兩者皆非 ＝ 端出一個有引用、看起來很有根據的數字，而它是
     別的年份的，且完全沒有交代。

⚠ **`admits_gap` 不能用 `looks_like_refusal()` 判**（第一版就是這樣寫的，40 份答案裡錯了一片）。
那個函式問的是「**整份答案都不作答**」，還帶一道 150 字上限；而這裡最典型的誠實答案是**長的**：

> 「我沒有足夠的資訊…根據提供的 10-K，僅列示了 2023、2024 與 2025 年的營收金額（…），
>   未包含 2022 年的營收資料，因此無法得知該年度的總營收數字。」

那是**模範答案**，卻會被長度閘判成「不是拒答」→ 落進危險態。**兩者是不同的概念，不要互相
代用**，也不要為了這裡去彎 `looks_like_refusal`（它的標記清單是拿 2209 份答案量過誤報的）。

⚠ **`admits_gap` 的措辭清單是硬編碼的，這裡是刻意的例外**（CLAUDE.md 說硬編碼詞表是警訊）：
本檔量的是**一次性、封閉的 40 份答案**（20 題 × 2 臂），而那 40 份我逐份讀過——**沒有「沒看過
的母體」讓詞表漏掉**。要拿它去量新的 run 之前，必須重新逐份複核，或改成 LLM 裁決。

⚠ **生產的期間缺口揭露語（`_build_fallback_note`）在 agentic 這條路上根本到不了答案**：
`agentic_rag_v2._retrieve_chunks()` 寫的是 `chunks, _note = rq.retrieve(...)`——note 被丟掉。
單管線（`rag_query.py` CLI／`api_server.py`）兩邊都有（SSE 顯示 ＋ 注入 generator prompt），
產品線走的是單管線。所以下面報的 `gap_note` 欄在 agentic 結果檔上恆為 0，**那是在覆述一行
程式碼，不是量測**。修法見 BACKLOG。

**gold 怎麼算**：`literal` 在**該臂自己的 collection** 裡定位（`locate_gold`，import
`probe_historical_benefit` 不抄寫）。這讓「答案有沒有引用到 gold 檔」這個診斷欄位對兩臂各自
成立——單年臂上 gold 集合是空的，那是定義使然。

用法：
    .venv/Scripts/python.exe eval/check_historical_generation.py
        --results experiments/agentic/gj_hist_before_mdna.json
                  experiments/agentic/gj_hist_after_multiyear.json
    .venv/Scripts/python.exe eval/check_historical_generation.py --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import rag_query as rq  # noqa: E402
# ⚠ 刻意**不** import `looks_like_refusal`：它問的是另一個概念（見上方 docstring），
# 而 import 一個看似相關的判定式最容易被後人「順手用起來」。
from eval.probe_historical_benefit import literal_matcher, locate_gold, scan_docs  # noqa: E402

QUERY_SET = _ROOT / "eval" / "period_probe_benefit_queries.json"
STATES = ("answered", "admits_gap", "silent_wrong")

# 「答案承認問到的期間不可得」的措辭。⚠ 硬編碼是刻意的例外，理由與使用限制見 docstring。
# 每一條都逐字取自實測的 40 份答案，**不是我想像的措辭**：
#   沒有足夠的資訊 / 未列示 / 未包含 / 無法得知 / 無法回答 / 未提供 / 參考資料中沒有 /
#   資料中未 / 不在提供的資料 / 未在…中列出
_ADMIT_RE = re.compile(
    r"沒有足夠(的)?(資訊|資料)"
    r"|未(列示|列出|包含|提供|揭露|披露|載明|指出)"
    r"|無法(得知|回答|判斷|提供|給出|取得|確定)"
    r"|(參考)?資料中(沒有|未|並未)"
    r"|沒有任何文件提及"
    r"|不在(所)?提供的"
    r"|未能(得知|找到|取得)"
    r"|找不到"
    # ⚠ 生成器**偶爾會用英文拒答**（bh-20 實測：「I don't have enough information in my
    #    knowledge base to answer this.」）。第一版全中文的詞表把它判成危險態。
    r"|(don't|do not) have enough information"
    r"|not (disclosed|provided|available) in the",
    re.IGNORECASE,
)


def _period_gap_markers() -> list[str]:
    """從生產的 `_build_fallback_note()` **反推**期間缺口揭露語的不變子字串。

    為什麼不直接寫死措辭：那句話是使用者可見句、改過一次（2026-08-27 把「所詢問期間」改成
    「所詢問財年」以免自相矛盾）。寫死就會漂移，而漂移的方向是**安靜地少算揭露**。
    作法是拿兩組不同的合成輸入各生一句，取兩句的公共片段＝與 ticker／年份無關的骨架。"""
    class _P:
        def __init__(self, **kw):
            self.payload = kw

    a = rq._build_fallback_note(
        [{"field": "fiscal_year", "value": "2022", "polarity": "include"},
         {"field": "ticker", "value": "AAPL", "polarity": "include"}],
        [_P(fiscal_year="2025", report_period_code="2025")])
    b = rq._build_fallback_note(
        [{"field": "report_period_code", "value": "202403", "polarity": "include"},
         {"field": "ticker", "value": "TSLA", "polarity": "include"}],
        [_P(fiscal_year="2026", report_period_code="202606")])
    assert a and b, "生產的 _build_fallback_note 回了空字串 → 這支的判準會失效"
    # 兩句共同的長片段：'的資料，以下回答改用最接近的可得期間（'
    marks = []
    for frag in ("以下回答改用最接近的可得期間", "知識庫沒有"):
        if frag in a and frag in b:
            marks.append(frag)
    assert marks, f"抓不到共同骨架，_build_fallback_note 的措辭可能大改了：\n{a}\n{b}"
    return marks


_GAP_MARKS = _period_gap_markers()


def classify(answer: str, literal, ) -> str:
    """三態判定。優先序見 docstring，不可調換。判在**答案本體**上（切掉證據尾巴）。

    `literal` 可以是單一字串或字串清單（`answer_literals`）：任何一個命中就算答對。
    ⚠ 前提檢查用的 `literal` 與這裡用的**可以不同**——前者在英文語料裡找，後者在中文答案裡
    找（bh-19 的教訓：英文片語永遠不會逐字出現在中文答案裡，不分開就把答對判成危險態）。"""
    body = rq.strip_evidence_tail(answer or "")
    lits = [literal] if isinstance(literal, str) else list(literal)
    if any(literal_matcher(x)(body) for x in lits):
        return "answered"
    # 生產的期間缺口揭露語也算承認（單管線走得到；agentic 走不到，見 docstring）
    if any(m in body for m in _GAP_MARKS) or _ADMIT_RE.search(body):
        return "admits_gap"
    return "silent_wrong"


def has_gap_note(answer: str) -> bool:
    """答案有沒有帶到生產 `_build_fallback_note()` 的揭露語（診斷欄，非判定）。"""
    body = rq.strip_evidence_tail(answer or "")
    return any(m in body for m in _GAP_MARKS)


def _load(path: Path) -> tuple[dict, dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get("meta", {}), {r["id"]: r for r in d.get("records", [])}


def run(paths: list[str], query_set: str) -> int:
    qs_all = json.loads(Path(query_set).read_text(encoding="utf-8"))["queries"]
    void = [q for q in qs_all if q.get("void")]
    qs = [q for q in qs_all if not q.get("void")]
    if void:
        print(f"⚠ 題庫標了 {len(void)} 題 `void`，**排除不計**（分母 {len(qs_all)} → {len(qs)}）：")
        for q in void:
            print(f"    {q['id']}  {q.get('void_reason', '(未寫原因)')}")
    # `answer_literals`（若有）只管「答案有沒有講出來」；`literal` 管前提檢查與 gold 定位。
    lit = {q["id"]: q.get("answer_literals") or q["literal"] for q in qs}
    cat = {q["id"]: q["category"] for q in qs}

    arms = []
    client = rq.make_qdrant_client()
    for p in paths:
        meta, recs = _load(Path(p))
        coll = meta.get("collection") or rq.COLLECTION_NAME
        docs = scan_docs(client, coll)
        gold = {q["id"]: set(locate_gold(docs, q["ticker"], q["literal"])[0]) for q in qs}
        arms.append({"name": Path(p).name, "coll": coll, "recs": recs, "gold": gold})

    ids = [q["id"] for q in qs if all(q["id"] in a["recs"] for a in arms)]
    missing = [q["id"] for q in qs if q["id"] not in ids]
    if missing:
        print(f"⚠ 這些題不是每一臂都有結果，已排除：{missing}")

    print("=" * 98)
    for a in arms:
        print(f"  {a['name']}  ←  {a['coll']}")
    print("=" * 98)
    w = 22
    print(f"{'id':<8}{'類別':<12}" + "".join(f"{a['name'][:20]:<{w}}" for a in arms))
    print("-" * 98)
    rows: dict = {a["name"]: {c: {s: 0 for s in STATES} for c in ("historical", "control")}
                  for a in arms}
    for qid in ids:
        line = f"{qid:<8}{cat[qid]:<12}"
        for a in arms:
            rec = a["recs"][qid]
            st = classify(rec.get("answer", ""), lit[qid])
            rows[a["name"]][cat[qid]][st] += 1
            cited = {s.get("source") for s in rec.get("sources") or []}
            mark = "＊" if cited & a["gold"][qid] else " "   # ＊＝有引用到 gold 檔
            line += f"{st + mark:<{w}}"
        print(line)

    print("-" * 98)
    for c in ("historical", "control"):
        n = sum(1 for q in ids if cat[q] == c)
        if not n:
            continue
        print(f"\n{c}（{n} 題）")
        for s in STATES:
            label = {"answered": "答對",
                     "admits_gap": "承認問到的期間不可得（誠實）",
                     "silent_wrong": "**拿別年份硬答且沒交代**"}[s]
            print(f"  {label:<26}" + "".join(
                f"{rows[a['name']][c][s]:>{w}}" for a in arms))
    print("\n＊ ＝ 該題答案引用到了 gold 檔（診斷欄，不進判定）")
    for a in arms:
        n = sum(has_gap_note(a["recs"][q].get("answer", "")) for q in ids)
        print(f"  {a['name']}：帶到生產期間缺口揭露語的題數 {n}/{len(ids)}")
    print("⚠ agentic 結果檔上那個數字**恆為 0 是結構使然**——"
          "`agentic_rag_v2._retrieve_chunks()` 把 `rq.retrieve` 的 note 丟掉了。")
    print("   它在覆述一行程式碼，不是量測。產品線走的單管線兩邊都有揭露。見本檔 docstring。")
    print("⚠ `admits_gap` 的措辭清單是硬編碼、且只對這 40 份讀過的答案負責；"
          "拿去量新的 run 前必須重新逐份複核。")
    return 0


def selftest() -> int:
    """四態判定的雙向自測。危險的方向是**把 silent_wrong 判成別的態**——那會讓這支
    量尺報出一個比實況好看的數字。"""
    L = "394,328"
    note = rq._build_fallback_note(
        [{"field": "fiscal_year", "value": "2022", "polarity": "include"},
         {"field": "ticker", "value": "AAPL", "polarity": "include"}],
        [type("P", (), {"payload": {"fiscal_year": "2025"}})()])
    cases = [
        (f"Apple 在 2022 財年的總營收是 $394,328 百萬【AAPL_10K_2023.html, chunk #1】。", L,
         "answered", "答對：literal 在答案裡"),
        (f"{note}\n總營收是 $394,328 百萬。", L, "answered",
         "答對優先於揭露：問到的事實有給到就是答對"),
        ("我沒有足夠的資訊來回答此問題。", L, "admits_gap", "短拒答"),
        # ⚠ 這條是第一版最大的錯：它是**模範答案**，卻因為寫得長被 looks_like_refusal 的
        #   150 字上限判成「不是拒答」→ 整片落進危險態。逐字取自實測（bh-03 的 before 臂）。
        ("我沒有足夠的資訊來提供 Alphabet 2022 年的總營收。根據提供的 10-K 報表，僅列示了 "
         "2023、2024 與 2025 年的營收金額，未包含 2022 年的營收資料，因此無法得知該年度的"
         "總營收數字。[GOOGL_10K_2025.html, chunk #0]", L, "admits_gap",
         "**長篇誠實拒答**：寫得長不代表不誠實（第一版在這裡整片判錯）"),
        (f"{note}\nApple 最新財年總營收 $416,161 百萬。", L, "admits_gap",
         "生產的期間缺口揭露語也算承認（單管線走得到，agentic 走不到）"),
        ("Apple 在 2022 財年的總營收是 $416,161 百萬【AAPL_10K_2025.html, chunk #0】。", L,
         "silent_wrong", "**危險態**：拿別年份的數字硬答，有引用、零交代"),
        ("⚠ 口徑說明：以上比率取自財報期間口徑，不是 TTM。\n"
         "Apple 在 2022 財年的總營收是 $416,161 百萬。", L, "silent_wrong",
         "誤報對照：口徑揭露**不算**承認期間缺口（它沒說「你問的那年沒有」）"),
        ("Alphabet 在 2023 年的研發費用中認列了 **8.48 億美元（$848 million）** 的裁員資遣費用。",
         ["848"], "answered",
         "`answer_literals`：前提檢查用英文片語、答案比對用數字（bh-19 的教訓）"),
        ("I don't have enough information in my knowledge base to answer this.", L,
         "admits_gap", "生成器偶爾用**英文**拒答（bh-20 實測），全中文詞表會判成危險態"),
        ("提供的文件只列出增幅與增額，未披露 2024 財年的完整營收金額，因此無法給出絕對值。",
         L, "admits_gap", "『未披露』『無法給出』——第一版詞表兩個都漏（bh-16 實測）"),
        ("Apple 2022 財年營收 $1,394,328 百萬。", L, "silent_wrong",
         "誤報對照：literal 被更長的數字包住不算命中（邊界比對）"),
        ("Apple 在 2022 財年的總營收是 $416,161 百萬。\n\n---\n"
         "📚 引用來源（Generator 實際依據的 chunk）:\n  - X.html #1\n  - 未提供\n", L,
         "silent_wrong", "誤報對照：證據尾巴要先切掉，尾巴裡的字不得構成「承認」"),
    ]
    ok = 0
    for ans, literal, want, why in cases:
        got = classify(ans, literal)
        ok += got == want
        print(f"[{'PASS' if got == want else 'FAIL'}] {why}\n         got={got} want={want}")
    print(f"\n{ok}/{len(cases)} PASS")
    return 0 if ok == len(cases) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", nargs="+", help="一或多份 generation_judge 結果檔")
    ap.add_argument("--query-set", default=str(QUERY_SET))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.results:
        ap.error("--results 是必要的（或用 --selftest）")
    return run(args.results, args.query_set)


if __name__ == "__main__":
    raise SystemExit(main())
