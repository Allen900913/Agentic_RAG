"""量「答案把來源數字捨成約值」的發生率（**零 LLM、零網路、只讀結果檔**）。

**這支測什麼**：`rq.SYSTEM_PROMPT` Rule 8 的 NUMERIC FIDELITY 那一段（2026-08-25 加入）要求
逐位照抄來源數字。這支是那條規則**唯一的量尺**，也是它唯一能被證偽的方式。

**病灶**（lex-17 四輪實測 2 次發生）：答案**引了** `MSFT_Fundamentals #0`，眼前就是
`Revenue Growth (YoY): 18.30%`，卻寫成「全年與最近的 TTM 都在約 **18%** 左右【…#0】」。
引用是真的、數字看起來也對，**兩個口徑就這樣消失了**（TTM 18.30% 與財年 18% 是不同的數）。

**判準**（三個條件同時成立才算一筆）：
  ① 來源（該題的 contexts）裡有一個帶小數的數字 Y，例如 `18.30%`；
  ② 答案裡出現 Y 的**捨入版** X（`18%`／`18.3%`），且單位相同；
  ③ 答案**沒有**同時給出 Y 本身，且 X **沒有**在來源裡逐字出現。

③ 的兩個排除項就是這支的誤報對照，缺一不可：
  · 答案寫「18.30%（約 18%）」是規則**允許**的（先給精確值再給約值）→ 不算；
  · 10-K 自己就寫著「revenue increased 18%」時，答案寫 18% 是**照抄別的來源**→ 不算。
    少了這一項，每一題只要來源同時有 18.30% 與 18% 就會被誤報，量尺會噪音到不能用。

⚠ **這支只認同單位的捨入**。跨單位換算（billion↔億、百萬↔十億）由 `SYSTEM_PROMPT` Rule 11
  管，量尺是 `verify_answer_validators.py` 閘門⑦ 的 `find_untraceable_numbers`；混進來只會
  讓兩個缺陷的數字互相污染。

⚠ **零不代表沒問題**（同 CLAUDE.md〈稽核回報 0 筆先當壞消息〉）：先跑 `--selftest`，
  它用 7 個合成案例雙向證明偵測器還有判別力，再看真實結果檔的數字。

用法：
    .venv/Scripts/python.exe eval/check_rounding_fidelity.py --selftest
    .venv/Scripts/python.exe eval/check_rounding_fidelity.py \
        --results experiments/agentic/gj_mdna_65q_after2.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

# 來源裡「帶小數的數字 + 單位」。單位是**封閉集合**（%／billion／million／B／M／空），
# 屬於格式定義而非拿字串比對做感知。
_UNIT_ALT = r"%|billion|million|bn|\bB\b|\bM\b"
_SRC_NUM_RE = re.compile(rf"(?<![\d.])(\d+\.\d+)\s*({_UNIT_ALT})", re.IGNORECASE)


def _norm_unit(u: str) -> str:
    u = (u or "").strip().lower()
    return {"bn": "billion", "b": "billion", "m": "million"}.get(u, u)


def _unit_pat(u: str) -> str:
    """同一個單位的所有寫法（答案可能寫 B 也可能寫 billion）。"""
    u = _norm_unit(u)
    if u == "%":
        return r"\s*%"
    if u == "billion":
        return r"\s*(?:billion|bn|B\b)"
    if u == "million":
        return r"\s*(?:million|M\b)"
    return r"\b"


def _appears(text: str, num: str, unit: str) -> bool:
    """`num` 帶著同單位在 text 裡出現過嗎。

    ⚠ 前後都要有數字邊界：少了它，`118.3` 會被算成講出了 `18.3`，
      而那正好會在最需要示警的時候把警示關掉（同 `_value_stated` 的教訓）。
    """
    trimmed = num.rstrip("0").rstrip(".") if "." in num else num
    pat = rf"(?<![\d.]){re.escape(trimmed)}0*(?![\d]){_unit_pat(unit)}"
    return re.search(pat, text or "", re.IGNORECASE) is not None


def _rounded_variants(num: str) -> list[str]:
    """`18.30` → `['18.3', '18']`（小數位由多到少，不含自己）。"""
    d = Decimal(num)
    out, seen = [], {str(d.normalize())}
    for places in range(len(num.split(".")[1]) - 1, -1, -1):
        q = d.quantize(Decimal(1).scaleb(-places))
        s = format(q, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# 答案裡的行內引用：【檔名, chunk #N】（也吃半形中括號版）
_CITE_RE = re.compile(
    r"[【\[]\s*([A-Za-z0-9_.\-]+?\.(?:txt|html))\s*,\s*chunk\s*#(\d+)\s*[】\]]")
# 生成後才附加的引用清單／警語區塊，不是模型的主張，掃了只會誤報
_TAIL_RE = re.compile(r"\n-{3,}\n(?=\s*(?:📚|⚠))")


def _claims(answer: str) -> list[tuple[str, list[tuple[str, int]]]]:
    """把答案切成「一段主張 + 它自己掛的引用」。沒有引用的段落丟掉——無法歸屬就無法判定。

    ⚠ **為什麼一定要逐引用而不是整篇比對**（2026-08-25 實測，第一版就是整篇）：
      lex-17 那一句「都在約 18% 左右【MSFT_Fundamentals #0】」是真陽性，但同一題的
      contexts 裡有 10-K 寫著「increased 18%」——整篇比對會把它當成「18% 有來源」而放行。
      **`18%` 有來源，但不是那一句掛的那個來源**，而錯就錯在這裡：#0 裡只有 18.30%。
    """
    body = _TAIL_RE.split(answer or "", maxsplit=1)[0]
    out, pos, pending = [], 0, []
    for m in _CITE_RE.finditer(body):
        seg = body[pos:m.start()]
        cite = (m.group(1), int(m.group(2)))
        if seg.strip():
            if pending:                    # 前一段已經配過引用，這是新的一段主張
                out.append((seg, [cite]))
            else:
                out.append((seg, [cite]))
            pending = []
        elif out:                          # 連續多個引用 → 都掛在同一段主張上
            out[-1][1].append(cite)
        pos = m.end()
    return out


def scan(answer: str, contexts: list[str], sources: list[dict] | None = None) -> list[dict]:
    """回傳這一題的違規清單（每筆 = 來源值、單位、答案寫成什麼、掛在哪個引用底下）。

    `sources` 與 `contexts` 同序（`run_agentic_on_evalset.py` 的結果檔就是這樣落盤的）。
    沒有 `sources` 時退回整篇比對——**判別力比較低**，會漏掉上面 `_claims` 說的那個形狀。
    """
    by_cite: dict[tuple[str, int], str] = {}
    for src, ctx in zip(sources or [], contexts or []):
        by_cite[(src.get("source", ""), int(src.get("chunk_index", -1)))] = ctx

    hits: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for seg, cites in _claims(answer):
        texts = [by_cite[c] for c in cites if c in by_cite]
        if not texts:
            continue
        cited = "\n".join(texts)
        for m in _SRC_NUM_RE.finditer(cited):
            src_val, unit = m.group(1), _norm_unit(m.group(2))
            if _appears(seg, src_val, unit):
                continue                   # 誤報對照：這一段給了精確值（後面再補約值是允許的）
            for var in _rounded_variants(src_val):
                if _appears(cited, var, unit):
                    continue               # 誤報對照：那個約值本身就寫在**這一段掛的**來源裡
                if _appears(seg, var, unit):
                    key = (src_val, unit, var)
                    if key not in seen:
                        seen.add(key)
                        hits.append({"source_value": src_val, "unit": unit,
                                     "answer_wrote": var,
                                     "cited": f"{cites[0][0]}#{cites[0][1]}"})
                    break
    return hits


def _selftest() -> int:
    """雙向證明偵測器有判別力：3 個陽性 + 5 個誤報對照。

    ⚠ 陽性① 是**回歸鎖**：第一版 `scan` 拿整篇 contexts 比對，這一條會被 10-K 的
      「increased 18%」放行而漏抓——而它正是這支腳本存在的理由（lex-17 實測那一句）。
    """
    FUND = ("MSFT_Fundamentals_20260612.txt", 0,
            "Market Cap : $2899.62B Revenue Growth (YoY): 18.30% Gross Margin : 68.31%")
    TENK = ("MSFT_10K_2026.html", 124,
            "Fiscal Year 2026 revenue increased 18% or $50.1 billion")
    AZURE = ("MSFT_10Q_202603.html", 62, "Azure revenue was $84.75 billion in the quarter")

    def _pack(*chunks):
        return ([c[2] for c in chunks],
                [{"source": c[0], "chunk_index": c[1]} for c in chunks])

    cases = [
        ("陽性①（回歸鎖）：引的是 Fundamentals #0（只有 18.30%），卻寫成「約 18%」——"
         "**同一題別的 chunk 有 18% 不能算數**",
         "全年與最近的 TTM 都在約 18% 左右【MSFT_Fundamentals_20260612.txt, chunk #0】。",
         _pack(FUND, TENK), 1),
        ("陽性②：捨到一位小數也算（68.31% → 68.3%）",
         "毛利率約 68.3%【MSFT_Fundamentals_20260612.txt, chunk #0】",
         _pack(FUND), 1),
        ("陽性③：金額同樣成立（$84.75 billion → $85 billion）",
         "Azure 營收約 $85 billion【MSFT_10Q_202603.html, chunk #62】",
         _pack(AZURE), 1),

        ("誤報對照①：這一段給了精確值 → 不算",
         "TTM 營收成長率為 18.30%【MSFT_Fundamentals_20260612.txt, chunk #0】",
         _pack(FUND), 0),
        ("誤報對照②：精確值在前、約值在後（規則明文允許）→ 不算",
         "TTM 營收成長率為 18.30%，約 18%【MSFT_Fundamentals_20260612.txt, chunk #0】",
         _pack(FUND), 0),
        ("誤報對照③：那個約值就寫在**這一段掛的**來源裡 → 不算",
         "FY2026 營收成長 18%【MSFT_10K_2026.html, chunk #124】",
         _pack(TENK), 0),
        ("誤報對照④：同一段掛了兩個引用，18% 來自其中之一 → 不算"
         "（歸屬是逐段的，不是逐 chunk 的）",
         "營收成長 18%【MSFT_Fundamentals_20260612.txt, chunk #0】"
         "【MSFT_10K_2026.html, chunk #124】",
         _pack(FUND, TENK), 0),
        ("誤報對照⑤：沒有引用的段落無從歸屬 → 跳過，不猜",
         "營收大約成長 18%。", _pack(FUND), 0),
    ]
    ok = True
    for label, ans, (ctx, srcs), want in cases:
        got = len(scan(ans, ctx, srcs))
        mark = "PASS" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{mark}] {label}（預期 {want} 筆，實得 {got}）")
    print(f"\nSELFTEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", help="run_agentic_on_evalset.py 的結果檔")
    ap.add_argument("--selftest", action="store_true", help="只跑偵測器的合成案例")
    ap.add_argument("--json", help="把逐題結果另存一份")
    args = ap.parse_args()

    if args.selftest or not args.results:
        return _selftest()

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    records = data.get("records", data if isinstance(data, list) else [])
    rows, total = [], 0
    for r in records:
        hits = scan(r.get("answer") or "", r.get("contexts") or [],
                    r.get("sources") or [])
        if hits:
            total += len(hits)
            rows.append({"id": r.get("id"), "category": r.get("category"), "hits": hits})

    print(f"結果檔：{args.results}   題數 {len(records)}")
    print(f"{'id':<10}{'類別':<14}來源值 → 答案寫成")
    print("-" * 72)
    for row in rows:
        for h in row["hits"]:
            u = "" if h["unit"] == "" else h["unit"]
            print(f"{row['id']:<10}{row['category']:<14}{h['source_value']}{u} → "
                  f"{h['answer_wrote']}{u}   （引用 {h.get('cited', '?')}）")
    print()
    print(f"有捨入的題數：{len(rows)} / {len(records)}   總筆數：{total}")
    if not rows:
        print("⚠ 0 筆先當壞消息查：先跑 --selftest 確認偵測器還有判別力，再相信這個 0。")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"results_file": args.results, "n_records": len(records),
             "n_records_with_rounding": len(rows), "n_hits": total, "rows": rows},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"逐題結果 → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
