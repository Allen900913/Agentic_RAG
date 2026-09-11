"""classify_answer_shape.py — 替 eval_set.json 的每題自動貼「答案形狀」標籤。

背景（2026-07-24 決策，見對話診斷）：RAGAS answer_correctness 是「答案 vs 參考長度對齊」
的假象；FactualCorrectness 的 mode 該**按題目要求的答案形狀**選，而不是按現有的檢索難度
category（lexical/semantic/…）。實測 60 題人工分類發現：
  - lexical 幾乎全是 factoid（單一事實）→ mode="precision"
  - semantic 全是 enumeration（分析/闡述）→ mode="recall"
  - mixed / colloquial 形狀不一，同一類別混 factoid + enumeration + hybrid → 必須逐題判
  - hybrid（「成長多少？主要驅動因素是什麼？」）兩者都不對 → mode="f1"

本腳本用問句訊號做**第一版自動分類**，輸出供人工校對（尤其 mixed/colloquial/news/
multi_intent）。分類只看問句本身（不看答案、不呼叫 LLM），純規則、可重現。

  factoid    = 問具體值（多少/哪年/哪裡…）且不問解釋
  enumeration= 問解釋/列點（如何/哪些/策略/風險…）且不問具體值
  hybrid     = 同時問具體值和解釋（金融最常見：「X 成長多少？主要驅動因素是什麼？」）
  review     = 兩種訊號都沒抓到 → 標記待人工判

輸出：
  eval/answer_shape_labels.json  機器可讀（{id: {shape, mode, signals, query}}）
  stdout                          分類別的校對表（人工快速掃）

用法：
  .venv/Scripts/python.exe eval/classify_answer_shape.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

EVAL_SET = Path("eval/eval_set.json")
OUT_JSON = Path("eval/answer_shape_labels.json")

# ── 問句訊號 ────────────────────────────────────────────────────────────────
# factoid：要求一個具體的值（數字/年份/日期/地點）。
# 註：不放「佔」「比例」——會誤觸「市佔地位/市佔龍頭」（那是市場地位、非佔比數字）；
# 真正問佔比的題（mix-04「佔整體營收多少比例」）已有「多少」會被抓到，不漏。
FACTOID_SIGNALS = [
    "多少", "幾多", "幾輛", "幾個", "哪一年", "哪年", "哪一天", "哪天", "何時",
    "位於", "是多少", "確切日期", "金額",
]
# enumeration：要求解釋、策略、列點、風險、態勢、摘要等「需完整涵蓋多個重點」的答案。
# 含新聞摘要語（消息/新聞/報導/做了什麼…）——news 類多為「recap 最新新聞」的摘要題，
# 完整度重要，歸 enumeration（走 recall）。這也讓 multi_intent 複合題（財務+新聞）
# 同時吃到 factoid 與 explanation 訊號而正確落到 hybrid。
EXPLANATION_SIGNALS = [
    "為什麼", "為何", "如何", "怎麼", "怎樣", "哪些", "哪家", "驅動因素", "因素", "策略",
    "態勢", "願景", "角色", "風險", "表現如何", "表現好不好", "有何變化", "有什麼變化",
    "影響", "護城河", "差異化", "演變", "描述", "建立", "佈局", "整合", "方向",
    "扮演", "面臨", "帶動", "驅動產品", "驅動", "狀況如何", "怎麼把", "厲害在哪",
    "消息", "新聞", "報導", "做了什麼", "搭上線", "好不好", "快不快", "看法", "評價",
    "值得", "地位", "透露",
]

MODE_BY_SHAPE = {
    "factoid": "precision",
    "enumeration": "recall",
    "hybrid": "f1",
    "review": "f1",      # 待審者暫用 f1（最保守），人工校對後再定
}


def _matched(text: str, signals: list[str]) -> list[str]:
    return [s for s in signals if s in text]


def classify(query: str) -> tuple[str, dict]:
    fac = _matched(query, FACTOID_SIGNALS)
    exp = _matched(query, EXPLANATION_SIGNALS)
    if fac and exp:
        shape = "hybrid"
    elif fac:
        shape = "factoid"
    elif exp:
        shape = "enumeration"
    else:
        shape = "review"
    return shape, {"factoid_hits": fac, "explanation_hits": exp}


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # ⚠ stderr 也要轉：traceback 走 stderr，只轉 stdout 的話「印到一半 crash」照樣發生（2026-09-11 閘門 H1）
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    es = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    labels: dict[str, dict] = {}
    by_cat: dict[str, list] = defaultdict(list)

    for q in es["queries"]:
        shape, sig = classify(q["query"])
        labels[q["id"]] = {
            "shape": shape,
            "mode": MODE_BY_SHAPE[shape],
            "category": q["category"],
            "query": q["query"],
            "signals": sig,
        }
        by_cat[q["category"]].append((q["id"], shape, sig, q["query"]))

    OUT_JSON.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 校對表 ──
    shape_tag = {"factoid": "factoid   ", "enumeration": "enumerat. ",
                 "hybrid": "HYBRID    ", "review": "!!REVIEW  "}
    from collections import Counter
    overall = Counter()
    for cat in ["lexical", "mixed", "semantic", "colloquial", "news", "multi_intent", "multi_hop"]:
        rows = by_cat.get(cat, [])
        cnt = Counter(r[1] for r in rows)
        overall.update(cnt)
        print(f"\n===== {cat}  ({dict(cnt)}) =====")
        for qid, shape, sig, query in rows:
            hits = (sig["factoid_hits"] or []) + (sig["explanation_hits"] or [])
            hint = f"  ⟨{'/'.join(hits)}⟩" if hits else "  ⟨無訊號⟩"
            print(f"  {qid:8s} {shape_tag[shape]} {query}{hint}")

    print(f"\n{'='*60}")
    print(f"總計: {dict(overall)}")
    print(f"→ mode 對應: factoid=precision / enumeration=recall / hybrid=f1")
    print(f"[寫入] {OUT_JSON}（供人工校對後給 eval_ragas 逐題選 mode 用）")
    review = [qid for qid, l in labels.items() if l["shape"] == "review"]
    if review:
        print(f"[待人工判] shape=review 的題（無訊號，需你判斷）: {review}")


if __name__ == "__main__":
    main()
