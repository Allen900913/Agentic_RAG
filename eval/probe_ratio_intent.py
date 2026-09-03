"""量 ratio 意圖的 LLM 分類（**含 LLM，非零噪音，不是閘門**）。

**這支測什麼**：2026-08-25 起，「這個子問題問的是不是 Fundamentals 的某個比率欄位」由
`agentic_rag_version._classify_ratio_fields` 判（詞表 `_RATIO_INTENT_RE` 退位成 LLM 失手時的
fallback）。分工判準見 CLAUDE.md〈LLM 與 Python 的分工〉：

| 半 | 誰做 | 量尺 |
|---|---|---|
| 「這句話想知道哪個量」 | **LLM** | **本檔**（唯一一支） |
| 「那個量在 Fundamentals 叫什麼欄位、哪個 chunk 有」 | Python（確定性 scroll） | `verify_answer_validators.py` 閘門⑩b |
| 「覆寫有沒有真的壓過詞表」 | Python | 同上，閘門⑫ |

⚠ **為什麼非量不可**：fallback 會**遮住** LLM 的失手——判不出來就退回詞表，端到端跑分
  看起來跟舊碼一樣。所以分類準不準**不能從結果檔推**，只能在這裡直接量。

⚠ **判讀看不對稱的錯誤**，不是整體準確率：
  · ratio 題被判成 `[]` → 保底整條不執行、Fundamentals 不進 commit 集合、口徑警語也不觸發
    → **答案拿財年／單季數字冒充 TTM，且零揭露**（這就是 lex-17／mi-05 的形狀）。**危險**。
  · 非 ratio 題被判成 ratio → 多撈一個 Fundamentals chunk 進 commit 集合。**只是浪費**。
  · 欄位挑錯（毛利率判成淨利率）→ 補撈到的 chunk 仍含全部四個比率，實害有限；
    但揭露警語會講錯欄位名 → 列為 `field_mismatch` 單獨報，不併進上面兩類。

⚠ **`none` 那幾題是陰性對照，不可省**。少了它們，一個「一律回 Revenue Growth」的分類器
  也會在 ratio 那一臂滿分。

⚠ **MoE 不固定路由**，單輪看不出穩定度；下結論請 `--repeat 3` 以上。

用法：
    .venv/Scripts/python.exe eval/probe_ratio_intent.py --repeat 3
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agentic_rag_version as ar     # noqa: E402

# (子問題, 期望欄位集合, 為什麼)
CASES: list[tuple[str, set[str], str]] = [
    # ── 口語臂：詞表漏掉的那些形狀（這一臂就是這次改動要修的東西）──────────────
    ("微軟的雲端服務最近成長得快不快？", {"Revenue Growth"},
     "**逐字取自 col-11 三輪實測的 Planner 子問題**，詞表判 False"),
    ("蘋果每賣一塊錢能留下多少利潤？", {"Profit Margin"}, "口語問淨利率，詞表完全沒有這個形狀"),
    ("特斯拉的營收有沒有起色？", {"Revenue Growth"}, "反問句，詞表判 False"),
    ("NVIDIA 賣一顆晶片的成本佔比高不高？", {"Gross Margin"}, "從成本面問毛利率，繞遠路的問法"),
    # ── 正式臂：詞表本來就判得對的，改動不可以把它們弄壞（回歸方向）────────────
    ("Microsoft 的營收成長率是多少？", {"Revenue Growth"}, "詞表也判得對，不得退步"),
    ("Apple 的毛利率是多少？", {"Gross Margin"}, "同上"),
    ("Meta 的營業利益率表現如何？", {"Operating Margin"}, "同上"),
    ("Amazon 的淨利率與毛利率分別是多少？", {"Profit Margin", "Gross Margin"}, "多欄位要都列出來"),
    # ── 陰性對照：不可省。少了它們，「一律回 Revenue Growth」也會滿分 ──────────
    ("NVIDIA 目前的市值是多少？", set(), "單一來源指標，無雙來源擲硬幣風險"),
    ("Apple 的 Diluted EPS 是多少？", set(), "同上"),
    ("Microsoft FY2026 的總營收是多少？", set(), "**問的是金額不是成長率**——最容易被誤判的陰性"),
    ("Tesla 在 10-K 裡提到哪些競爭風險？", set(), "質性題"),
    ("Google 的自由現金流有多少？", set(), "不在那四個欄位裡"),
    ("Amazon 的雲端業務策略是什麼？", set(), "質性題"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1, help="跑幾輪（看 MoE 的穩定度）")
    args = ap.parse_args()

    tasks = [q for q, _, _ in CASES]
    print(f"RETRIEVAL_MODEL = {ar.RETRIEVAL_MODEL}   repeat = {args.repeat}")
    print(f"enum = {ar._RATIO_FIELD_ENUM}\n")

    votes: dict[str, list[frozenset]] = collections.defaultdict(list)
    for _ in range(args.repeat):
        got = ar._classify_ratio_fields(tasks)          # 生產路徑本身，不另抄一份判斷式
        for (q, _w, _why), g in zip(CASES, got):
            votes[q].append(frozenset(g) if g is not None else None)

    print(f"{'子問題':<34}{'期望':<26}{'LLM 判':<34}{'詞表':<8}判定")
    print("-" * 120)
    dangerous, wasteful, mismatch, unparsed = [], [], [], []
    for q, want, _why in CASES:
        got = votes[q]
        cnt = collections.Counter("|".join(sorted(g)) if g is not None else "<解析失敗>"
                                  for g in got)
        shown = ",".join(f"{k or '[]'}×{c}" for k, c in cnt.most_common())
        maj_key = cnt.most_common(1)[0][0]
        maj = None if maj_key == "<解析失敗>" else set(x for x in maj_key.split("|") if x)
        lex = "True" if ar._is_ratio_intent(q) else "False"
        if maj is None:
            verdict = "⚠ 解析失敗（退回詞表）"
            unparsed.append(q)
        elif maj == want:
            verdict = "OK"
        elif want and not maj:
            verdict = "❌ 危險（ratio 題判成非 ratio）"
            dangerous.append(q)
        elif not want and maj:
            verdict = "⚠ 浪費（非 ratio 判成 ratio）"
            wasteful.append(q)
        else:
            verdict = "⚠ 欄位挑錯"
            mismatch.append(q)
        print(f"{q:<34}{'|'.join(sorted(want)) or '[]':<26}{shown:<34}{lex:<8}{verdict}")

    unstable = [q for q, v in votes.items() if len(set(v)) > 1]
    print()
    print(f"危險錯誤（ratio 題判成非 ratio）：{len(dangerous)} 題 {dangerous}")
    print(f"浪費錯誤（非 ratio 判成 ratio）：{len(wasteful)} 題 {wasteful}")
    print(f"欄位挑錯：{len(mismatch)} 題 {mismatch}")
    print(f"解析失敗（退回詞表）：{len(unparsed)} 題 {unparsed}")
    if args.repeat > 1:
        print(f"跨輪不穩定：{len(unstable)} 題 {unstable}")
    else:
        print("⚠ 只跑一輪＝看不出穩定度。MoE 不固定路由，要下結論請 --repeat 3 以上。")
    # 只有危險錯誤回非零：浪費錯誤只花一次補撈，不影響答案正確性
    return 1 if dangerous else 0


if __name__ == "__main__":
    raise SystemExit(main())
