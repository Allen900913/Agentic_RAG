"""gold 數字稽核（零 LLM、零網路）——**查 gold 自身的自洽性**，不是查「數字在原文有沒有出現」。

**為什麼要有這支**：gold 錯的話後面全白跑（memory `eval-gold-version-drift-bug`：gold 鎖舊季報
→「最新一季」題假性腰斬；`gold-unit-conversion-sweep`：13 題 23 處單位錯）。gold 含人工校正、
不是腳本可免費重現的產物,所以要能**反覆稽核**而不是重新生成。

**⚠ 第一版是「把 gold 的數字拿回原始申報檔定位」,實測沒有判別力,已整支換掉。** 注入 10x
單位錯、改錯百分比、改錯金額、抓錯期間四種已知錯誤,**四種全部漏抓**。原因量清楚了：
`MSFT_10Q_202603.html` 有 17,268 個數字（1,125 個相異）,1% 相對容差下**隨機取 100 個四位數
有 94 個配得到**。這與 `check_number_defects.py` 前三版失效是同一件事——**大乾草堆裡的
「值有沒有出現」不帶資訊**（見 memory `number-defect-metric-needs-claim-anchoring`）。

現在改查**自洽性**：判準自足、不需要乾草堆,所以沒有這個問題（memory
`gold-unit-conversion-sweep` 的結論「稽核判準要自足（雙寫自洽）」就是這個意思）。

| 檢查 | 原理 | 抓得到什麼 |
|---|---|---|
| ① 雙寫自洽 | gold 慣例是「N 億美元（$M billion）」兩種單位並寫 → N/10 必須等於 M | 單位換算錯（歷史上 23 處都是這類） |
| ② 成長率自洽 | 同句有「A → B」與「X%」→ (B/A−1) 必須等於 X | 拿錯運算元、拿錯期間欄、算錯 |
| ③ 小來源定位 | 只在 gold_files 總量 < `--small-source-kb` 時啟用（Fundamentals 只有幾十行） | 該欄位值被改過／語料換版 |

③ 對 10-K/10-Q **刻意不做**——就是上面那個沒判別力的檢查。要驗 filing 類的數字得先接地到
主張,那是 `check_number_defects.py` 的工作,不是這裡。

`--self-test` 會注入已知錯誤再跑一次,確認這支腳本**現在**還抓得到（判別力會因為 regex 改動
而悄悄消失,而「0 筆問題」看起來跟「沒問題」一模一樣）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    for _s in (sys.stdout, sys.stderr):  # ⚠ stderr 也要轉：traceback 走 stderr，只轉 stdout 的話「印到一半 crash」照樣發生（2026-09-11 閘門 H1）
        _s.reconfigure(encoding="utf-8")

RAW = Path("data/raw")
EVAL_SET = Path("eval/eval_set.json")
GOLD = Path("eval/reference_answers.json")

PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
TAG = re.compile(r"<[^>]+>")
FOOTER = re.compile(r"\n\s*(參考來源|引用來源)[\s\S]*$")

# 1 億 = 1e8 → 億美元 換成 billion 要除 10、換成 million 要乘 100
# ⚠ 億 的值**必須吃千分位**（`[\d,]+`）：gold 寫 `1,602.1 億美元（$160.21 billion）`,用 `\d+`
# 只會抓到逗號後的 `602.1` → 誤判成「少了一位數」。第一版就是這樣製造了 53 筆假警報,而且
# 假警報**看起來極有規律**（正確值都在、只差開頭一位數）,規律本身讓人更相信它是真的。
YI_PAREN = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*億美元\s*[（(]\s*\$?\s*([\d,]+(?:\.\d+)?)\s*(billion|million|百萬|十億|兆)")
UNIT_TO_M = {"billion": 1000.0, "十億": 1000.0, "million": 1.0, "百萬": 1.0, "兆": 1_000_000.0}
DOUBLE_WRITE_TOL = 0.02        # 允許 847.5億→$84.75B 這類進位寫法

# 句中的金額（統一換算成「百萬美元」）
AMT_PATS = [
    (re.compile(r"([\d,]+(?:\.\d+)?)\s*億美元"), 100.0),        # 同 YI_PAREN：必須吃千分位
    (re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:billion|十億)"), 1000.0),
    (re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|百萬)"), 1.0),
    (re.compile(r"([\d,]+(?:\.\d+)?)\s*百萬美元"), 1.0),
]
GROWTH_TOL = 1.0               # 百分點
DEDUP_CHARS = 40               # 雙寫的兩個數值相隔多近就算同一筆

# ② 只在句中有「明確的兩值比較」時才驗算。實測不加這個限制的假警報率是 25/25——
# 一句話裡有金額、又有**與那些金額無關**的百分比太常見了：毛利率、稅率、可轉債票面
# 利率（news-09 的 6.25% 優先股）、環比成長（前一季金額不在句中）、YoY 成長（去年
# 金額不在句中）。所以要求句子自己宣告「A 變成 B」,否則跳過。
COMPARE_MARK = re.compile(r"提升至|增至|成長至|增加到|上升至|降至|減至|較去年同期的|"
                          r"較去年同季的|較上季的|相較於.{0,12}的|從.{0,40}?(?:至|到)")
# 這些詞緊鄰的百分比不是成長率,是「比率」——即使句中真有兩值比較也不該拿去驗算
RATIO_WORD = re.compile(r"毛利率|淨利率|營業利益率|稅率|利率|佔比|占比|比重|殖利率|margin|rate")


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def check_double_write(ref: str) -> list[dict]:
    out = []
    for mo in YI_PAREN.finditer(ref):
        yi, val, unit = _f(mo.group(1)), _f(mo.group(2)), mo.group(3)
        want_m = yi * 100.0                       # 億 → 百萬
        got_m = val * UNIT_TO_M[unit]
        if abs(got_m - want_m) > max(want_m * DOUBLE_WRITE_TOL, 0.5):
            out.append({"kind": "雙寫不符", "text": mo.group(0).strip(),
                        "detail": f"{yi:g} 億美元 = ${yi / 10:g} billion，"
                                  f"但括號寫 {val:g} {unit}（＝${got_m / 1000:g} billion）",
                        "pos": mo.start()})
    return out


def _amounts_in(sent: str) -> list[tuple[int, float]]:
    """句中金額 → [(位置, 百萬美元)]，並把雙寫的同一筆折成一筆。"""
    raw: list[tuple[int, float]] = []
    for pat, mult in AMT_PATS:
        for mo in pat.finditer(sent):
            raw.append((mo.start(), _f(mo.group(1)) * mult))
    raw.sort()
    out: list[tuple[int, float]] = []
    for pos, v in raw:
        if out and abs(pos - out[-1][0]) <= DEDUP_CHARS and \
                abs(v - out[-1][1]) <= max(out[-1][1] * DOUBLE_WRITE_TOL, 0.5):
            continue                              # 同一筆的第二種寫法
        out.append((pos, v))
    return out


def check_growth(ref: str) -> list[dict]:
    """同句有 ≥2 個金額與 ≥1 個百分比時，要求存在一組有序配對能重現該百分比。

    刻意**不指定**哪個是舊值哪個是新值——中文兩種語序都寫得出（「從 A 增至 B」／
    「B，較去年同期的 A」）。只要有一組配對算得出來就算自洽;都算不出來才報。
    """
    out = []
    for sent in re.split(r"[。\n]", ref):
        amts = _amounts_in(sent)
        pcts = [(m.start(), float(m.group(1))) for m in PCT.finditer(sent)]
        if len(amts) < 2 or not pcts or not COMPARE_MARK.search(sent):
            continue
        vals = [v for _, v in amts]
        derivable = set()
        for i, a in enumerate(vals):
            for j, b in enumerate(vals):
                if i == j or a == 0:
                    continue
                derivable.add(round((b / a - 1) * 100, 4))
                derivable.add(round(b / a * 100, 4))       # 「佔比」型陳述
        for ppos, p in pcts:
            if RATIO_WORD.search(sent[max(0, ppos - 14):ppos + 6]):
                continue                                       # 毛利率／稅率等比率,不是成長率
            if not any(abs(d - p) <= GROWTH_TOL for d in derivable):
                out.append({"kind": "成長率算不出", "text": f"{p:g}%",
                            "detail": f"句中金額（百萬）={[round(v, 1) for v in vals]}，"
                                      f"任一有序配對都算不出 {p:g}%",
                            "pos": 0, "sent": sent.strip()[:110]})
    return out


def resolve_sources(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in patterns or []:
        stem = p.replace(".html", "").replace(".txt", "")
        for cand in (f"**/{stem}.html", f"**/{stem}.txt"):
            out.extend(RAW.glob(cand))
    return sorted(set(out))


def check_small_source(ref: str, paths: list[Path], limit_kb: int) -> tuple[list[dict], str]:
    total = sum(p.stat().st_size for p in paths if p.exists())
    if total > limit_kb * 1024:
        return [], f"跳過定位（來源 {total / 1024:.0f} KB > {limit_kb} KB，大乾草堆無判別力）"
    src = " ".join(TAG.sub(" ", p.read_text(encoding="utf-8", errors="ignore"))
                   for p in paths if p.exists())
    src = re.sub(r"\s+", " ", src)
    src_pcts = [float(m.group(1)) for m in PCT.finditer(src)]
    src_decs = [float(m.group(0)) for m in re.finditer(r"0\.\d+", src)]
    out = []
    for mo in PCT.finditer(ref):
        lit = mo.group(1)
        v = float(lit)
        # **比對要在 gold 自己宣告的精度上做**：gold 寫 74.1%，來源是 74.144995% ——
        # 用嚴格相等會把「正當四捨五入」全報成找不到（實測 23 筆裡多數是這個）。
        d = len(lit.split(".")[1]) if "." in lit else 0
        if any(round(p, d) == v for p in src_pcts):
            continue
        if any(round(x * 100, d) == v for x in src_decs):       # Fundamentals 舊格式 0.183
            continue
        out.append({"kind": "小來源找不到", "text": f"{v:g}%",
                    "detail": f"來源 {total / 1024:.0f} KB 內沒有（比對精度 {d} 位小數）",
                    "pos": mo.start()})
    return out, f"已定位（來源 {total / 1024:.0f} KB）"


def audit(gold: list[dict], es: dict, qids, limit_kb: int) -> list[dict]:
    findings = []
    for g in gold:
        qid = g["id"]
        if qids and qid not in qids:
            continue
        ref = FOOTER.sub("", g.get("reference") or "")
        rows = check_double_write(ref) + check_growth(ref)
        note = ""
        e = es.get(qid)
        if e:
            paths = resolve_sources(e.get("relevant", []))
            if paths:
                loc, note = check_small_source(ref, paths, limit_kb)
                rows += loc
            else:
                rows.append({"kind": "relevant 解析不到檔案", "text": str(e.get("relevant")),
                             "detail": "整題無法稽核", "pos": 0})
        if rows:
            findings.append({"id": qid, "category": g.get("category", "?"),
                             "note": note, "rows": rows,
                             "ref": ref})
    return findings


def _self_test(gold: list[dict], es: dict, limit_kb: int) -> None:
    """注入已知錯誤,確認這支腳本現在還抓得到。判別力消失時「0 筆」看起來跟「沒問題」一樣。"""
    by = {x["id"]: x for x in gold}
    # expect="catch"：注入後筆數必須增加。expect="clean"：**原樣必須 0 筆**（防假警報）。
    # 兩種都要,因為判別力會雙向失效：抓不到真錯,或把正確寫法當成錯。
    cases = [
        ("mix-09", "金額改錯 15,369→51,369", lambda t: t.replace("15,369", "51,369"), "catch"),
        ("mix-09", "百分比改錯 22%→45%", lambda t: t.replace("**22%**", "**45%**"), "catch"),
        ("mi-01", "雙寫 10x 錯 $81.6B→$816B",
         lambda t: t.replace("$81.6 billion", "$816 billion"), "catch"),
        ("mi-05", "小來源百分比 18.30→88.30", lambda t: t.replace("18.30%", "88.30%"), "catch"),
        # 千分位：第一版 regex 用 `\d+` 抓不到 `1,602.1` 的逗號前段 → 製造 53 筆假警報。
        # 釘成 clean 回歸測試,因為那批假警報**看起來極有規律**（正確值都在、只差開頭一位數），
        # 規律本身讓人更相信它是真的。
        ("mh-05", "千分位寫法（雙寫必須 0 筆）", lambda t: t, "clean_double"),
        ("mh-05", "雙寫真的錯 $160.21B→$160.21 million",
         lambda t: t.replace("$160.21 billion", "$160.21 million"), "catch"),
    ]
    print("── 判別力自我測試（雙向：真錯要抓到、正確寫法不能誤報）")
    ok = True
    for qid, label, mut, expect in cases:
        src = by.get(qid)
        if not src:
            print(f"  [!] {qid} 不在 gold 裡，跳過")
            continue
        fake = dict(src)
        fake["reference"] = mut(src["reference"])
        got = audit([fake], es, [qid], limit_kb)
        kinds = [r["kind"] for f in got for r in f["rows"]] if got else []
        base = audit([src], es, [qid], limit_kb)
        base_n = len([r for f in base for r in f["rows"]]) if base else 0
        if expect == "catch":
            good = len(kinds) > base_n
        else:                                    # clean_double：雙寫這一類必須 0 筆
            good = not any(k == "雙寫不符" for k in kinds)
        ok &= good
        print(f"  {'OK  ' if good else '失敗'} {qid:8} {label:34} "
              f"原樣 {base_n} 筆 → 測試後 {len(kinds)} 筆 {kinds[:3]}")
    print(f"  → {'雙向判別力正常' if ok else '[!] 判別力有缺口,這支腳本的結論不可信'}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--small-source-kb", type=int, default=40,
                    help="來源總量小於此值才做定位檢查（大檔無判別力）")
    ap.add_argument("--self-test", action="store_true", help="先跑判別力自我測試")
    ap.add_argument("--show-sent", action="store_true", help="成長率不符時印出整句")
    args = ap.parse_args()

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    es = {x["id"]: x for x in json.loads(EVAL_SET.read_text(encoding="utf-8"))["queries"]}

    print("=" * 100)
    print("gold 自洽性稽核（零 LLM）——雙寫換算、成長率驗算、小來源定位")
    if args.self_test:
        _self_test(gold, es, args.small_source_kb)

    fs = audit(gold, es, args.ids, args.small_source_kb)
    by_kind: dict[str, int] = {}
    for f in fs:
        print(f"\n  ══ {f['id']:8s} [{f['category']}]  {f['note']}")
        for r in f["rows"]:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            print(f"     [{r['kind']}] {r['text']}")
            print(f"        → {r['detail']}")
            if args.show_sent and r.get("sent"):
                print(f"        原句: {r['sent']}")

    print("\n" + "=" * 100)
    print(f"{len(fs)} 題有問題；分類：" + ("、".join(f"{k} {v}" for k, v in by_kind.items()) or "無"))
    print(f"稽核題數 {len(args.ids) if args.ids else len(gold)}")
    print("[!] 『成長率算不出』常是同句混了不同主體/期間的金額，先讀原句再改 gold。")


if __name__ == "__main__":
    main()
