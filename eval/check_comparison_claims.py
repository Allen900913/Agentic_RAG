"""多實體比較題的逐條斷言（**零 LLM、零網路、零 Qdrant**，只讀結果檔）。

**為什麼需要這一支**：`multi_hop` 的 5 題全是「在 A、B、C 中，X 最高的是哪一家？」。
2026-08-29 查過全碼庫：**沒有任何一行 Python 在做這個比較**（`agentic_rag_version/__init__.py` 裡所有
`max()` 都在比日期或 rerank 分數）。六個子問題各自撈回 chunk → `_fair_select` 挑一批 →
**由 Generator 自己讀著數字比大小**。

而這一步是這條路上唯一沒有安全網的：比錯了，Synthesize 的五道 validator **一道都不會響**
（citation 合法、數字溯源得到、期別也對、一致性也對）——錯的只有「最高」這個結論。
這正是 CLAUDE.md〈LLM 與 Python 的分工〉說的「比對／定位／算術給 Python」被違反的地方。

---

## 量尺不與被測物耦合：真值從**答案自己看到的 contexts** 算

不是重跑一次檢索、也不是查 gold 檔。理由是：真值若來自別的地方，FAIL 會混進「檢索沒撈到」
這個完全不同的病。從 record 自己的 `contexts` 算，`FAIL` 的意思就毫不含糊：
**正確的數字就攤在它面前，它還是挑錯了。**

代價是「某家公司的 chunk 根本沒進 contexts」時算不出真值 → 那一題 `N/A`。
但那件事本身是另一個缺陷，所以獨立成第二個指標（`unfounded`），**不併進 PASS 也不併進 FAIL**。

## 三個指標，不可合併

| 指標 | 意思 | 代價 |
|---|---|---|
| `wrong_winner` | 每家的值都在 contexts 裡，Python 的 argmax ≠ 答案宣稱的那家 | **危險**：靜默答錯，validator 全綠 |
| `unfounded` | 有公司的值**不在** contexts 裡，答案卻仍宣告了一個最高者 | **危險**：無依據的比較（沒看過就說誰最大） |
| `no_claim` | 答案裡抽不出「哪家最高」的宣稱（拒答／改口） | **N/A**，不是通過 |

⚠ **N/A 不能併進 PASS**（同 `check_number_defects.py`）。

## ⚠ 這支目前的判別力上限（判讀前一定要看）

現有語料的五題**每一題的差距都很大**（NVDA 85.2% vs META 33.1%、GOOGL 160.21B vs
MSFT 125.22B）。**沒有任何一題是接近的**。所以「10/10 PASS」證明的是
**「值都在、單位一致、差距很大」這個容易的情況下它不會錯**，
不能外推到接近值、缺值、或跨單位（B vs M）的情況——那三種**這批語料裡都沒有**。

`--selftest` 的變異測試（把 argmax 換成 argmin）是這支有沒有判別力的唯一證明。

用法：
    .venv/Scripts/python.exe eval/check_comparison_claims.py --selftest
    .venv/Scripts/python.exe eval/check_comparison_claims.py \\
        --from-results experiments/agentic/gj_multiyear_prod_r1.json \\
                       experiments/agentic/gj_multiyear_prod_r2.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ⚠ Windows 主控台預設 cp950，而本檔的報表帶著 ⚠／✔ 等字元 ⇒ **摘要那一行會 crash**，
#   而 crash 的退出碼與「有 wrong_winner」外觀相同 ＝ 把一個編碼問題讀成一個系統缺陷。
#   2026-09-11 實際踩到（5/5 全 PASS，卻是 UnicodeEncodeError 收場）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_query as rq          # noqa: E402  只為了共用 _COMPANY_TICKER（唯一定義點）

# Fundamentals chunk 的版面是 ingest 產生的固定格式 → 封閉集合，列清單正當
# （同 CLAUDE.md 對 VALID_*_ITEMS 的判準）。單位一併抽出來，混單位要能發現。
_FIELD_RE_CACHE: dict[str, re.Pattern] = {}

# 「哪一家最高」的宣稱錨點。只認**比較級**用詞：這是結論句的標記，不是一般敘述。
_SUPERLATIVE_RE = re.compile(r"最高|最大|最多|居首|領先|排名第一|勝過|超越|優於|高於")

# ⚠ **及物比較動詞**：「A 領先於 B」的贏家在錨點**之前**，後面接的是輸家。
#   實測 `gj_ratio_llm_r2` 的 mh-03 就是這個句型（「NVIDIA … 領先於 Meta 與 Alphabet」），
#   純粹取「離錨點最近」會挑到 Meta ＝ 假缺陷。凍結在 selftest ⑨。
_TRANSITIVE_RE = re.compile(r"領先|勝過|超越|優於|高於")

# 兩個公司提及之間若「只隔分隔符／括號／數字／百分比」→ 它們同屬一個**列舉**（題目重述清單），
# 而列舉的成員是候選名單、**不是宣稱**。實測 `gj_mdna_65q_after2` 的 mh-01 就是這個句型
# （「是 Amazon、Microsoft、Alphabet、Meta 四家公司中規模最大的」）→ 純距離會挑到 Meta。
# 凍結在 selftest ⑧。⚠ 同一個 ticker 的相鄰提及（「Alphabet（GOOGL）」）不算列舉。
_SEP_ONLY_RE = re.compile(r"^[\s、,，/／|與和及或者的是為以\(\)（）\[\]【】0-9.,%$＄億美元十二個月TTM\-—─~～]*$")

_CJK_RE = re.compile(r"[一-鿿]")


def _field_re(field: str) -> re.Pattern:
    if field not in _FIELD_RE_CACHE:
        _FIELD_RE_CACHE[field] = re.compile(
            re.escape(field) + r"\s*:\s*\$?(-?[\d.]+)\s*([BM%]?)")
    return _FIELD_RE_CACHE[field]


def _parse_context(ctx: str) -> tuple[str | None, str]:
    """contexts 的每一則開頭是 `[TICKER] ...`（run_agentic_on_evalset 寫入的格式）。"""
    m = re.match(r"\[([A-Z]{1,6})\]", ctx or "")
    return (m.group(1) if m else None), (ctx or "")


def _extract_value(ctx: str, field: str) -> tuple[float, str] | None:
    m = _field_re(field).search(ctx)
    return (float(m.group(1)), m.group(2)) if m else None


def _normalize(val: float, unit: str) -> float | None:
    """把 $XB / $XM 正規化到百萬；% 與純數原樣。認不得的單位回 None（＝這一題 N/A，不猜）。"""
    if unit == "B":
        return val * 1000.0
    if unit in ("M", "%", ""):
        return val
    return None


def _company_positions(text: str) -> list[tuple[int, int, str]]:
    """回傳 [(起, 迄, ticker)]，用 rq._COMPANY_TICKER 這個唯一定義點。
    英文別名要詞界（避免 'meta' 命中 'metadata'）；中文別名直接子字串（CJK 無 \\b）。
    ⚠ **迄** 是必要的：列舉偵測要看「兩個提及之間」的字，用 起+1 會把公司名自己算進去
    （第一版就是這個錯，導致列舉偵測整個失效而 selftest ⑧ 現形）。"""
    hits: list[tuple[int, int, str]] = []
    low = text.lower()
    for name, ticker in rq._COMPANY_TICKER.items():
        if _CJK_RE.search(name):
            start = 0
            while (i := text.find(name, start)) >= 0:
                hits.append((i, i + len(name), ticker))
                start = i + 1
        else:
            for m in re.finditer(r"\b" + re.escape(name) + r"\b", low):
                hits.append((m.start(), m.end(), ticker))
    return sorted(hits)


def _enumerated(body: str, hits: list[tuple[int, int, str]]) -> set[int]:
    """回傳「屬於某個列舉」的提及索引集合。相鄰兩個**不同 ticker** 的提及之間若只隔分隔符，
    兩者都算列舉成員。列舉是候選名單不是宣稱（理由見 `_SEP_ONLY_RE` 上方）。"""
    marked: set[int] = set()
    for i in range(len(hits) - 1):
        (_, e1, t1), (s2, _, t2) = hits[i], hits[i + 1]
        if t1 == t2:                       # 「Alphabet（GOOGL）」是同一家的兩個別名,不是列舉
            continue
        if _SEP_ONLY_RE.match(body[e1:s2]):
            marked.add(i)
            marked.add(i + 1)
    return marked


def _claimed_winner(answer: str, candidates: set[str]) -> str | None:
    """抽出答案宣稱「最高的是哪一家」。回 None ＝ 抽不出來（判 N/A，**不猜**）。

    三條規則，每一條都是被真實答案逼出來的（各自凍結在 selftest）：
      ① 砍掉 `📚` 之後的機械式引用清單——那裡公司名密度最高。
      ② **列舉不是宣稱**：題目重述的「在 A、B、C 中…」整串排除（selftest ⑧）。
      ③ **及物比較動詞**（領先／高於／勝過…）的贏家在錨點**之前**（selftest ⑨）；
         純粹的最高級（最高／最大）才是前後都可能。
    ⚠ 這是感知不是規則，仍可能抽錯 → 輸出一律把抽到的那家印出來供人工掃一遍。"""
    body = (answer or "").split("📚")[0]
    m = _SUPERLATIVE_RE.search(body)
    if not m:
        return None
    anchor = m.start()
    hits = [h for h in _company_positions(body) if h[2] in candidates]
    if not hits:
        return None
    skip = _enumerated(body, hits)
    cands = [(s, t) for i, (s, _, t) in enumerate(hits) if i not in skip]
    if not cands:            # 全部都在列舉裡 → 分不出宣稱,誠實回 None(N/A) 而不是猜
        return None
    if _TRANSITIVE_RE.match(body, anchor):
        before = [(anchor - p, t) for p, t in cands if p < anchor]
        if before:
            return min(before)[1]
    return min((abs(p - anchor), t) for p, t in cands)[1]


def evaluate(record: dict, claim: dict, *, _mutate_direction: bool = False) -> dict:
    """回傳這一題這一輪的判定。`_mutate_direction` 只給 --selftest 用（argmax↔argmin）。"""
    companies = list(claim["companies"])
    field = claim["compare_field"]
    want_max = (claim.get("direction", "max") == "max") != _mutate_direction

    values: dict[str, float] = {}
    bad_unit = False
    for ctx in record.get("contexts") or []:
        tk, body = _parse_context(ctx)
        if tk not in companies or tk in values:
            continue
        got = _extract_value(body, field)
        if got is None:
            continue
        norm = _normalize(*got)
        if norm is None:
            bad_unit = True
            continue
        values[tk] = norm

    claimed = _claimed_winner(record.get("answer", ""), set(companies))
    missing = [c for c in companies if c not in values]

    if claimed is None:
        # 兩種都算 N/A 但成因不同,診斷時要分得開：沒有比較級宣稱（拒答／改口）vs
        # 有宣稱但抽不出是哪一家（量尺的極限,不是系統的缺陷）。
        body = (record.get("answer", "") or "").split("📚")[0]
        why = "no_claim" if not _SUPERLATIVE_RE.search(body) else "ambiguous_claim"
        return {"verdict": "N/A", "reason": why, "claimed": None,
                "truth": None, "missing": missing}
    if bad_unit:
        return {"verdict": "N/A", "reason": "unit_unparsed", "claimed": claimed,
                "truth": None, "missing": missing}
    if missing:
        # 有公司的值沒進 contexts,卻仍然宣告了最高者 → 獨立缺陷,且真值算不出來
        return {"verdict": "N/A", "reason": "unfounded", "claimed": claimed,
                "truth": None, "missing": missing}
    truth = (max if want_max else min)(values, key=lambda k: values[k])
    return {"verdict": "PASS" if claimed == truth else "FAIL", "reason": "",
            "claimed": claimed, "truth": truth, "missing": [],
            "values": {k: round(v, 2) for k, v in values.items()}}


# ──────────────────────────────────────────────────────────────────────────────
# selftest：這支有沒有判別力的唯一證明
# ──────────────────────────────────────────────────────────────────────────────

_SYNTH_CTX = [
    "[NVDA] === NVDA Company Fundamentals === --- Growth --- Revenue Growth (YoY): 85.20% ",
    "[META] === META Company Fundamentals === --- Growth --- Revenue Growth (YoY): 33.10% ",
    "[GOOGL] === GOOGL Company Fundamentals === --- Growth --- Revenue Growth (YoY): 21.80% ",
]
_SYNTH_CLAIM = {"id": "synthetic", "companies": ["NVDA", "META", "GOOGL"],
                "compare_field": "Revenue Growth (YoY)", "direction": "max"}


def selftest() -> int:
    cases: list[tuple[str, dict, dict, str, bool]] = [
        ("① 正確答案 → PASS",
         {"answer": "最近一年營收年增率最高的是 **NVIDIA**，其 YoY 增長為 85.20%。",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "PASS", False),
        ("② 變異：argmax→argmin,同一份答案必須翻成 FAIL（判別力證明）",
         {"answer": "最近一年營收年增率最高的是 **NVIDIA**，其 YoY 增長為 85.20%。",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "FAIL", True),
        ("③ 宣稱了別家 → FAIL（抽取器 + 比較器一起測）",
         {"answer": "在三家公司中，營收年增率最高的是 **Meta**，為 33.10%。",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "FAIL", False),
        ("④ 公司名在錨點**之前** → 仍要抽得到（實測兩種語序都出現過）",
         {"answer": "NVIDIA 在最近一年的營收年增率最高，為 85.20%。",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "PASS", False),
        ("⑤ 少一家的值 → N/A(unfounded),不可算 PASS 也不可算 FAIL",
         {"answer": "營收年增率最高的是 **NVIDIA**。", "contexts": _SYNTH_CTX[:2]},
         _SYNTH_CLAIM, "N/A", False),
        ("⑥ 沒有比較級宣稱（拒答形狀）→ N/A(no_claim)",
         {"answer": "知識庫中查無足夠資料可以比較這三家公司。", "contexts": _SYNTH_CTX},
         _SYNTH_CLAIM, "N/A", False),
        ("⑦ 引用清單裡的公司名不得污染抽取（📚 之後全部忽略）",
         {"answer": "NVIDIA 的營收年增率最高。\n📚 引用來源:\n - META_Fundamentals.txt #0\n - GOOGL_Fundamentals.txt #0",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "PASS", False),
        # ⑧⑨ 是**真實答案逐字凍結**（2026-08-29 抓到的兩個假缺陷）。第一版量尺在這兩句上
        #    各報一次 FAIL，而兩句的答案都是對的——量尺的錯不是系統的錯。
        ("⑧ [真實句型] 題目重述清單緊接最高級：列舉不是宣稱（來源 gj_mdna_65q_after2 mh-01）",
         {"answer": "在過去十二個月（TTM）中，NVIDIA 的營收年增率為 **85.20%**，"
                    "是 NVIDIA、Meta、Alphabet 三家公司中最高的。",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "PASS", False),
        ("⑨ [真實句型] 「A 領先於 B、C」：錨點後面接的是輸家（來源 gj_ratio_llm_r2 mh-03）",
         {"answer": "NVIDIA 以 85.20% 的營收年增率領先於 Meta（33.10%）與 Alphabet（21.80%）。",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "PASS", False),
        ("⑩ 及物動詞只有兩家、無列舉可排除 → 仍要挑錨點**之前**那家",
         {"answer": "NVIDIA 的營收年增率高於 Meta。", "contexts": _SYNTH_CTX},
         _SYNTH_CLAIM, "PASS", False),
        ("⑪ 誤報對照：⑧ 的句型但宣稱換成錯的那家 → 必須仍抓得到 FAIL",
         {"answer": "在過去十二個月（TTM）中，Meta 的營收年增率為 **33.10%**，"
                    "是 NVIDIA、Meta、Alphabet 三家公司中最高的。",
          "contexts": _SYNTH_CTX}, _SYNTH_CLAIM, "FAIL", False),
    ]
    ok = 0
    for name, rec, claim, want, mut in cases:
        got = evaluate(rec, claim, _mutate_direction=mut)
        good = got["verdict"] == want
        ok += good
        print(f"  {'✓' if good else '✗'} {name}\n      預期 {want} / 實得 {got['verdict']}"
              f"({got['reason'] or '-'}) claimed={got['claimed']} truth={got['truth']}")
    print(f"\nselftest: {ok}/{len(cases)} PASS")
    return 0 if ok == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="先跑這個：證明這支有判別力")
    ap.add_argument("--from-results", nargs="*", default=[],
                    help="agentic 結果檔（可多份＝多輪，逐題印 k/n）")
    ap.add_argument("--claims", default=str(Path(__file__).parent / "comparison_claims.json"))
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.from_results:
        ap.error("要嘛 --selftest，要嘛給 --from-results")

    claims = {c["id"]: c for c in
              json.loads(Path(args.claims).read_text(encoding="utf-8"))["claims"]}
    rounds: list[tuple[str, dict]] = []
    for f in args.from_results:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        rounds.append((Path(f).name, {r["id"]: r for r in (d.get("records") or d.get("results") or [])}))

    print(f"claims={len(claims)}  rounds={len(rounds)}")
    for i, (n, _) in enumerate(rounds):
        print(f"  [{i}] {n}")
    print("\n逐輪判定：P=PASS  F=FAIL(值都在還挑錯)  N=N/A  -=這輪沒有這題\n")
    w = max(len(rounds) + 2, 8)
    print(f"{'題':<8}{'比較欄位':<24}{'逐輪':<{w}}{'答案宣稱':<12}{'真值':<9}{'缺值':<8}")
    print("-" * (58 + w))
    _SYM = {"PASS": "P", "FAIL": "F", "N/A": "N"}

    n_fail = n_unfounded = n_na = n_pass = 0
    for qid, claim in claims.items():
        verdicts, claimed_set, truth_set, missing_all = [], set(), set(), set()
        reasons: set[str] = set()
        for _, by_id in rounds:
            rec = by_id.get(qid)
            if rec is None:
                verdicts.append("-")
                continue
            v = evaluate(rec, claim)
            verdicts.append(_SYM[v["verdict"]])
            if v["reason"]:
                reasons.add(v["reason"])
            if v["claimed"]:
                claimed_set.add(v["claimed"])
            if v["truth"]:
                truth_set.add(v["truth"])
            missing_all.update(v["missing"])
            n_fail += (v["verdict"] == "FAIL")
            n_pass += (v["verdict"] == "PASS")
            n_na += (v["verdict"] == "N/A")
            n_unfounded += (v["reason"] == "unfounded")
        print(f"{qid:<8}{claim['compare_field']:<24}{''.join(verdicts):<{w}}"
              f"{'/'.join(sorted(claimed_set)) or '-':<12}"
              f"{'/'.join(sorted(truth_set)) or '-':<9}"
              f"{','.join(sorted(missing_all)) or '-':<8}"
              f"{' '.join(sorted(reasons))}")

    total = len(claims) * len(rounds)
    print()
    print(f"⚠ wrong_winner（值都在眼前還挑錯）：{n_fail}/{total}")
    print(f"⚠ unfounded（沒看到某家的值卻仍宣告最高者）：{n_unfounded}/{total}")
    print(f"  N/A（這一輪沒量到東西，**不是通過**）：{n_na}/{total}")
    print(f"  PASS：{n_pass}/{total}")
    print("\n⚠ 判別力上限：這批語料**沒有任何一題是接近值**，也沒有缺值或跨單位的情況。")
    print("  全 PASS 只證明「值都在、單位一致、差距很大」時不會錯，不能外推。")
    print("⚠ 宣稱抽取是感知不是規則 → 上表的『答案宣稱』欄請人工掃一遍再下結論。")
    return 1 if (n_fail or n_unfounded) else 0


if __name__ == "__main__":
    raise SystemExit(main())
