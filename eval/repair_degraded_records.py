"""repair_degraded_records.py — 把**崩潰降級**的題從結果檔裡挑出來、刪掉，好讓 resume 補跑。

## 為什麼需要這一支

`run_agentic_on_evalset.py` 的 resume 判準是「**有 answer 且無 error** → 跳過」，而
`run_agentic` 的 graph 崩潰降級路徑**照樣產出 answer、而且不寫 error**（它走一次乾淨的
單發檢索+生成兜底）。兩件事湊起來的結果是：**降級題會被 resume 永遠跳過**，於是一份
「跑完 65 題」的結果檔裡可能混著 N 題根本沒經過 agentic 管線的記錄，而且**外觀完全正常**。

2026-09-08 撞到：NVIDIA NIM 間歇性 404（實測同一分鐘內 10/20 失敗），
plan／replan 的 `rq.call_llm` **沒有包 try/except** → 一次 404 就炸掉整個 `graph.invoke`。
證據見 `experiments/_nim404_probe.txt`。

## 怎麼認出降級題：`degraded_reason`，退回 `None` ≠ `{}`

2026-09-09 起降級路徑自己會寫 `degraded_reason`（例外型別＋訊息＋最深一層的檔:行），
那是直接證據。**舊指紋不可拿掉**：既有結果檔沒有這個欄位，只認新指紋會讓它們
整份變成「沒有降級題」——而那正好是這支要防的失敗方式。

## 舊指紋（結果檔早於 2026-09-09 時唯一的判準）：`None` ≠ `{}`

`run_agentic` 的降級 return **整個不帶** stats 鍵 → record 建構子寫成 `None`；
正常跑完但沒觸發則是 `{}` 或帶 0 的 dict。**這個區分就是本檔唯一的判準**，
也是那五個 stats 通道當初刻意保留 `None` 的理由（見 CLAUDE.md ⑲l10／⑯j5）。

⚠ **刻意用五個鍵的「全部都是 None」而不是任一個**：單看一個鍵會把「那一版還沒有這個欄位」
  的舊結果檔整份誤判成降級。全部皆 None 才是降級的指紋。
⚠ **刻意不自動重跑**：本檔只負責「刪掉那幾筆 ＋ 印出 id」，補跑仍由 `run_agentic_on_evalset.py`
  自己做（同一個入口、同一組參數）——把重跑也塞進來就變成第二個跑分入口，兩邊遲早漂移。
⚠ **`--dry-run` 是預設**：要真的改檔案得明寫 `--apply`（結果檔是跑了好幾小時的東西）。

用法：
    # 先看有哪些（不改檔）
    .venv/Scripts/python.exe eval/repair_degraded_records.py experiments/agentic/xxx.json

    # 真的刪掉（會先寫一份 .bak）
    .venv/Scripts/python.exe eval/repair_degraded_records.py experiments/agentic/xxx.json --apply

    # 然後用原本那道指令補跑（resume 只會補這幾題）
    .venv/Scripts/python.exe eval/run_agentic_on_evalset.py --no-web --output experiments/agentic/xxx.json

    # 零檔案的自測
    .venv/Scripts/python.exe eval/repair_degraded_records.py --selftest
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

# 這五個鍵都由 graph 那條路填；降級 return 一個都不帶。
_STATS_KEYS = ("exec_stats", "replan_stats", "plan_stats", "revision_stats", "unit_stats")


def is_degraded(rec: dict) -> bool:
    """這筆 record 是不是 graph 崩潰降級的產物。

    ⚠ 判準是**五個鍵全部都是 `None`**，不是任一個：
      · 任一個 → 舊版結果檔（那時還沒有這個欄位）會被整份誤判成降級。
      · 全部 → 只有「降級 return 不帶任何 stats 鍵」才成立。
    ⚠ `{}` **不算降級**：那是「跑完了、只是沒觸發」。這正是 `None` ≠ `{}` 的用途。
    """
    # 2026-09-09 起降級路徑自己會寫 `degraded_reason`（型別＋訊息＋檔:行）——那是**直接**
    # 證據，比「五個 stats 鍵全 None」這個間接指紋強。⚠ 舊指紋不能拿掉：既有的結果檔
    # （含兩輪五個分母那兩份）沒有這個欄位，只認新指紋會讓它們全部變成「沒有降級題」。
    if rec.get("degraded_reason"):
        return True
    present = [k for k in _STATS_KEYS if k in rec]
    if not present:
        return False          # 整份檔都沒有這些欄位 ＝ 舊格式，不是降級
    return all(rec.get(k) is None for k in present)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="結果檔（generation_judge schema）")
    ap.add_argument("--apply", action="store_true", help="真的改檔（預設只印不改）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.path:
        ap.error("要給結果檔路徑，或用 --selftest")

    p = Path(args.path)
    data = json.loads(p.read_text(encoding="utf-8"))
    recs = data.get("records")
    if recs is None:
        print("⚠ 這份檔沒有 `records` 鍵，不處理")
        return 2

    bad = [r for r in recs if is_degraded(r)]
    good = [r for r in recs if not is_degraded(r)]
    print(f"{p}")
    print(f"  總計 {len(recs)} 題｜降級 {len(bad)} 題 ({len(bad) / max(len(recs), 1):.0%})")
    if bad:
        print(f"  降級 id：{[r.get('id') for r in bad]}")
        # 成因（2026-09-09 起才有）。舊結果檔這一格全是 None——那不是「查不出成因」，
        # 是「那時候還沒有這個欄位」，兩者不可混為一談。
        _reasons = [(r.get("id"), r.get("degraded_reason")) for r in bad]
        if any(x[1] for x in _reasons):
            print("  成因：")
            for rid, why in _reasons:
                print(f"    {rid}: {why or '（這份結果檔早於 degraded_reason，成因不可考）'}")
        else:
            print("  ⚠ 這份結果檔沒有 `degraded_reason` 欄位（早於 2026-09-09）→ 成因不可考。")
        # ⚠ **這個分布不可以直接讀成「哪一類比較容易崩」**：`eval_set.json` 是**按類別排序**的
        #   （semantic 在最前面），而崩潰來自 API 事件、會**依時間叢集**——所以「類別」與
        #   「跑的先後」在這份題庫裡是**共線的**，這一欄分不出兩者。
        #   2026-09-08 實測就是這個形狀：3 筆降級全是 semantic，而它們也正好全是最早跑的。
        #   （另一個機制同樣說得通但同樣沒被證實：一題打的 LLM 次數越多、撞到的機率越高。）
        from collections import Counter
        print(f"  降級題的類別分布：{dict(Counter(r.get('category') for r in bad))}")
        print("  ⚠ 這個分布**不等於**「哪一類容易崩」：eval_set 按類別排序，而崩潰依時間叢集，"
              "\n    兩者共線、這一欄分不出來。")
        print("  ⚠ 補跑之前不要拿剩下的題算任何比率——遺失的不是隨機樣本。")
    if not bad:
        print("  ✔ 沒有降級題，不需要補救")
        return 0
    if not args.apply:
        print("\n  （dry-run，沒有改檔。要真的刪請加 --apply）")
        return 0

    shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    data["records"] = good
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  已刪掉 {len(bad)} 筆，備份在 {p.name}.bak")
    print("  下一步：用原本那道指令補跑（resume 只會補這幾題）")
    return 0


def _selftest() -> int:
    """⚠ 判別力全在誤報對照②③④：陽性那一條，一個「一律回 True」的實作也會過。"""
    ok = fail = 0

    def _a(name, cond, note=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  OK    {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {note}")

    _a("① 陽性：五個 stats 全 None ＝ 降級",
       is_degraded({"id": "x", "exec_stats": None, "replan_stats": None,
                    "plan_stats": None, "revision_stats": None, "unit_stats": None}))
    _a("② **誤報對照**：`{}` 不算降級（那是「跑完了、只是沒觸發」＝ None ≠ {} 的全部意義）",
       not is_degraded({"id": "x", "exec_stats": {}, "replan_stats": {},
                        "plan_stats": {}, "revision_stats": {}, "unit_stats": {}}))
    _a("③ **誤報對照**：有一個帶值就不是降級（graph 跑過了）",
       not is_degraded({"id": "x", "exec_stats": {"todos": 1}, "replan_stats": None,
                        "plan_stats": None, "revision_stats": None, "unit_stats": None}))
    _a("④ **誤報對照**：舊格式（完全沒有這些欄位）不得被整份判成降級",
       not is_degraded({"id": "x", "answer": "a"}))
    _a("⑤ 部分欄位存在時只看存在的那些（欄位是逐步加上去的）",
       is_degraded({"id": "x", "unit_stats": None}))
    _a("⑥ 新指紋：有 `degraded_reason` 就是降級（直接證據，不必靠五鍵推論）",
       is_degraded({"id": "x", "degraded_reason": "RuntimeError: boom @ graph.py:12 in _node_plan",
                    "exec_stats": {"todos": 1}}))
    _a("⑦ **誤報對照**：`degraded_reason` 是 None／缺席不算降級（缺席＝沒降級）",
       not is_degraded({"id": "x", "degraded_reason": None, "exec_stats": {"todos": 1}}))

    print(f"\n  PASS {ok}  FAIL {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
