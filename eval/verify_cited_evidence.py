"""verify_cited_evidence.py — 文件引用的證據檔必須存在、且必須在版控裡。

**這道閘門守的是什麼**：本 repo 的結論幾乎每一條都掛著一個證據檔（`experiments/...json`
或 `.log`）。證據檔如果被 `.gitignore` 擋掉、或根本沒 commit，那條結論在別人的 clone 上
就**查無此人**——而失敗的方式是**安靜的**：文件照樣讀得通，只有真的去點那個路徑的人才會
發現。2026-09-04 就發生過一次（commit `16d4646`「讓被引用的證據 log 不再被 gitignore
擋掉」），當時是手動撈出來的。這支把那次的手動檢查變成可重跑的。

**零 LLM、零網路、零 Qdrant，毫秒級。**

判準（兩條，都必須成立）：
  ① 文件裡寫出來的每一個 `experiments/...` 路徑，檔案要**存在**。
  ② 而且要**已納入版控**（`git ls-files` 查得到）——存在於本機但沒 commit
     等於只有我這台機器看得到，對別人來說與不存在相同。

⚠ **反過來不成立，而且刻意不查**：`experiments/` 底下沒被任何文件引用的檔案**不是缺陷**。
  那些是原始 stdout 捕捉（2026-09-06 實測 24 個 `.log`／3.3 MB），沒有結論掛在上面，
  留在 gitignore 裡是對的。把「未被引用」也算成 FAIL 會把 3.3 MB 垃圾灌進版控。

⚠ **只掃已納入版控的 `.md`**：scratchpad、實驗目錄裡的暫存筆記不算 repo 的結論。

用法：
    .venv/Scripts/python.exe eval/verify_cited_evidence.py
    退出碼 0 ＝ 全部通過；非 0 ＝ 有引用指向不存在或未版控的檔案。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只認 experiments/ 底下的證據檔。`eval/` 的 fixture 由各自的 selftest 守，
# 而 `docs/` 之間的互相連結是 markdown link 不是證據路徑。
_CITE_RE = re.compile(r"experiments/[A-Za-z0-9_./\-]+\.(?:json|log|txt|csv)")


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        sys.exit(f"[error] git {' '.join(args)} 失敗：{out.stderr.strip()}")
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def classify(citations: dict[str, list[str]], tracked: set[str], exists) -> tuple[list, list]:
    """把引用分成「檔案不存在」與「存在但未納入版控」兩袋。

    `exists` 是一個 `path -> bool` 的可呼叫物，注入進來才能讓 `--selftest` 不碰磁碟。
    ⚠ 判斷順序不可調換：不存在的檔案本來就不會在 `tracked` 裡，先問存在性才不會
      把同一筆同時報成兩種病。"""
    missing: list[tuple[str, list[str]]] = []
    untracked: list[tuple[str, list[str]]] = []
    for path, where in sorted(citations.items()):
        if not exists(path):
            missing.append((path, sorted(set(where))))
        elif path not in tracked:
            untracked.append((path, sorted(set(where))))
    return missing, untracked


def selftest() -> int:
    """⚠ 這支的價值幾乎全在**誤報對照**：陽性那兩條，一個「一律回報 FAIL」的實作也會過。

    真正有判別力的是 ③④——它們證明這道閘門**不會**把正常的東西判成缺陷，
    以及 ⑤：`experiments/` 底下沒被引用的檔案**不算缺陷**（否則 3.3 MB 原始 log
    會被逼進版控）。"""
    ok = True

    def chk(no: str, cond: bool, desc: str) -> None:
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {no} {desc}")
        ok = ok and cond

    print("── selftest ──")
    # ① 陽性：引用了不存在的檔案
    m, u = classify({"experiments/nope.json": ["A.md"]}, set(), lambda p: False)
    chk("①", len(m) == 1 and not u, "引用不存在的檔案 → 記入 missing")
    # ② 陽性：檔案在本機但沒 commit
    m, u = classify({"experiments/local.json": ["A.md"]}, set(), lambda p: True)
    chk("②", not m and len(u) == 1, "存在但未 tracked → 記入 untracked")
    # ③ 誤報對照：存在且已 tracked → 兩袋都要空
    m, u = classify({"experiments/good.json": ["A.md"]},
                    {"experiments/good.json"}, lambda p: True)
    chk("③", not m and not u, "存在且已版控 → 不得回報任何缺陷")
    # ④ 誤報對照：同一筆不得同時落進兩袋（順序寫反就會）
    m, u = classify({"experiments/nope.json": ["A.md"]}, set(), lambda p: False)
    chk("④", len(m) + len(u) == 1, "不存在的檔案只算一種病，不得重複計數")
    # ⑤ 誤報對照：沒有被引用的檔案不進 citations，因此永遠不會被判 FAIL
    m, u = classify({}, set(), lambda p: False)
    chk("⑤", not m and not u, "沒有任何引用時 → 零缺陷（未被引用的 log 不是缺陷）")
    # ⑥ 正則本身：三種真實寫法都要抓得到，且不吃到句尾標點
    text = ("見 experiments/_model_bakeoff_20260903.log 與 "
            "`experiments/agentic/gj_multiyear_r2_unitstats.json`，"
            "還有 experiments/ragas_65q_gemma_sysB.json。")
        # ⚠ 中文句號／全形逗號緊貼路徑是本 repo 的常態寫法，吃進去會變成假的「檔案不存在」
    found = _CITE_RE.findall(text)
    hits = [m.group(0) for m in _CITE_RE.finditer(text)]
    chk("⑥", len(found) == 3 and all(not h.endswith((".", "，", "。")) for h in hits),
        f"三種寫法都抓到且不吃標點（抓到 {len(found)} 個）")

    print("✔ selftest 全過" if ok else "✖ selftest 有 FAIL")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    tracked = set(_git("ls-files"))
    md_files = [f for f in tracked if f.endswith(".md")]

    citations: dict[str, list[str]] = {}
    for md in md_files:
        p = ROOT / md
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _CITE_RE.finditer(text):
            citations.setdefault(m.group(0), []).append(md)

    missing, untracked = classify(citations, tracked, lambda x: (ROOT / x).exists())

    print(f"掃了 {len(md_files)} 份版控中的 .md，找到 {len(citations)} 個 experiments/ 引用")
    for label, rows in (("檔案不存在", missing), ("存在但未納入版控", untracked)):
        for path, where in rows:
            print(f"  [FAIL] {label}: {path}")
            print(f"         被引用於: {', '.join(where)}")

    bad = len(missing) + len(untracked)
    if bad:
        print(f"\n✖ {bad} 個引用指向拿不到的證據。")
        print("  修法：把該檔 commit 進去（`.gitignore` 擋住的話，比照 eval/.gitignore 的"
              " `!<檔名>` 寫法開一個例外並寫下理由），或改掉文件裡那個路徑。")
        return 1

    print("\n✔ 全部通過：每一個被引用的證據檔都存在且在版控裡。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
