# -*- coding: utf-8 -*-
"""**守量尺自己的閘門**（零 LLM／零網路／零 Qdrant／毫秒級）。

## 為什麼需要這一支

2026-09-11 盤點：本 repo 的量尺失效史已有 6 次「與被測物耦合」、6 次
`check_number_defects` 判法被推翻、5 批 false-negative gold，而**同一天又冒出五個新的**。
逐一補丁補了十幾次之後把它們歸類，只剩**四個根因**，其中三個是機械可判定的
——那就該有一道閘門，而不是下一次再靠運氣發現。

| 家族 | 形狀 | 這道閘門守哪一半 |
|---|---|---|
| **A 抄寫** | 量尺把生產的組態抄成字面常數；改生產的那一刻兩者脫鉤，而量尺**照跑、照印數字、外觀完全正常** | **H3**：生產常數的值被凍結；它一變，所有登錄過的抄寫點**當場 FAIL 要求重新複核** |
| **B patch 點不符** | eval 用 `ar.<name> = stub` 攔截，而呼叫端是**裸名** ⇒ stub 蓋不到，閘門卻照樣全綠 | **H2**：patch 名單從 eval 腳本**反推**（不手列），逐一驗套件裡沒有裸名引用 |
| **C 定位規則自己有 bug** | `chunk_gold.literal_matcher` 的數字邊界把「數字延續」與「標點」混為一談；`number_claims` 的 `expect_text` 只認捨位後的值 | **不在這裡**——那要靠各自的雙向自測（兩支都已有 `--selftest`），這裡管不到語意 |
| **D 量尺壞掉的外觀與系統壞掉相同** | 報表 crash 被當成 FAIL、N/A 被當成 PASS、coverage 太窄卻回報「0 筆」 | **H1**：會印非 ASCII 的腳本一律要把 stdout **與** stderr 轉成 utf-8 |

⚠ **A 家族只有一半是機械可判定的。**「這個字面 `20` 是不是抄來的」需要語意判斷
（`MAX_REWRITES = 2` 與任何字面 `2` 都撞，實測 11 筆候選裡多數是誤報）。
所以 H3 **不去猜**，改成兩條都精確的斷言：① 每個抄寫候選都必須登錄並寫下理由；
② 生產常數的值一旦變動，閘門 FAIL 並列出要重新複核的檔案。
**那正是 2026-09-10 `RRF_TOP_N_PRIMARY` 20 → 30 時缺的東西**——當天三支尺一起脫鉤
（`probe_recall_layer_attribution` 的生產臂、它的報表標籤、它寫進證據檔的 `_meta`），
而**沒有任何一道閘門叫**。

⚠ **H2 只管套件、不管 `rag_query`**：後者是單一檔案，檔內裸名引用是正常的
（patch `rq.call_llm` 攔的是**外部**呼叫端），把它算違規會是誤報。
⚠ **H2 也不管 `__init__.py`**：門面本身就持有這些名字，裸用是它的工作。

用法：
    .venv/Scripts/python.exe eval/verify_eval_harness.py
    .venv/Scripts/python.exe eval/verify_eval_harness.py --selftest
    .venv/Scripts/python.exe eval/verify_eval_harness.py --update-baseline   # 複核完才可以跑
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EVAL_DIR = ROOT / "eval"
PKG_DIR = ROOT / "agentic_rag_version"
BASELINE = EVAL_DIR / "eval_harness_baseline.json"

# 追蹤哪些生產常數。**只收「量尺會想抄的」**：檢索名額與各種上限。
TRACKED = {
    # ⚠ 常數住哪個模組是逐一確認過的，不要憑印象填：`POOL_RETURN_K`／`COMMIT_TOP_K`
    #   在 **agentic** 不在 `rag_query`，填錯的話 `getattr` 回 None → 那一格
    #   **真空成立**（H3 少追蹤一個常數，而外觀完全正常）。③g 守這件事。
    "rag_query": ("FETCH_N", "RRF_TOP_N_PRIMARY", "RERANK_INPUT_N"),
    "agentic_rag_version": ("POOL_RETURN_K", "COMMIT_TOP_K", "MAX_TODOS",
                            "MAX_REWRITES", "MAX_ITERS", "WRITER_MAX_CHUNKS",
                            "QUERY_WEB_BUDGET"),
}


def eval_files() -> list[Path]:
    """`eval/` 下的正式腳本。`_*.py` 是一次性診斷腳本，刻意排除。"""
    return [p for p in sorted(EVAL_DIR.glob("*.py")) if not p.name.startswith("_")]


# ── H1 ───────────────────────────────────────────────────────────────────────
def prints_non_ascii(src: str) -> bool:
    """會不會印出非 ASCII。**註解不算**（註解不會被印出來）。"""
    if "print(" not in src:
        return False
    return any(ord(ch) > 127 for ch in re.sub(r"#.*", "", src))


def has_stream_reconfigure(src: str) -> bool:
    """有沒有把 stdout **與** stderr 都轉成 utf-8。

    ⚠ **兩個都要**：traceback 走 stderr，只修 stdout 的話「印到一半 crash」照樣發生，
      而那正是 D 家族最危險的形狀——crash 的退出碼與「有 FAIL」外觀相同。
    """
    if "reconfigure(" not in src:
        return False
    return "sys.stdout" in src and "sys.stderr" in src


def h1_violations() -> list[str]:
    out = []
    for p in eval_files():
        src = p.read_text(encoding="utf-8")
        if prints_non_ascii(src) and not has_stream_reconfigure(src):
            out.append(p.name)
    return out


# ── H2 ───────────────────────────────────────────────────────────────────────
# `ar.x = ` / `_g.x = ` / `agentic_rag_version.graph.x = `。⚠ 刻意不吃 `==`、
# 不吃 `ar.x.y = `（那是改屬性的屬性，不是攔截 x）。
_PATCH_RE = re.compile(
    r"^[^\S\n]*(?:ar|_pkg|_g|graph|agentic_rag_version(?:\.\w+)?)\.(\w+)\s*=(?!=)", re.M)


def patched_names() -> dict[str, set[str]]:
    """**從 eval 腳本反推** patch 名單（不手列——手列實測漏過 9 個）。"""
    out: dict[str, set[str]] = {}
    for p in eval_files():
        for m in _PATCH_RE.finditer(p.read_text(encoding="utf-8")):
            out.setdefault(m.group(1), set()).add(p.name)
    return out


def bare_refs_in_pkg(names: set[str]) -> dict[str, list[str]]:
    """套件子模組裡對這些名字的**裸名**引用（`_pkg.x` 不算，模組自己定義的不算）。"""
    hits: dict[str, list[str]] = {}
    for p in sorted(PKG_DIR.glob("*.py")):
        if p.name == "__init__.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        defined |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in names and node.id not in defined):
                hits.setdefault(node.id, []).append(f"{p.name}:{node.lineno}")
    return hits


# ── H3 ───────────────────────────────────────────────────────────────────────
def current_constants() -> dict[str, int]:
    import importlib
    out: dict[str, int] = {}
    for mod, names in TRACKED.items():
        m = importlib.import_module(mod)
        for n in names:
            v = getattr(m, n, None)
            if isinstance(v, int) and not isinstance(v, bool):
                out[n] = v
    return out


def _lit_on(src: str, consts: dict, fname: str = "t.py") -> list[tuple]:
    """純函式版：（檔名, 常數, 值, 次數）。供自測餵合成原始碼，也是實掃的實作。

    ⚠ **這是候選不是結論**。它的用途有兩個：逼每一筆被登錄並寫下理由，
      以及在生產常數變動時列出「要重看哪幾個檔」。
    """
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    ints = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, int)
            and not isinstance(n.value, bool)]
    for name, val in consts.items():
        if re.search(r"\b" + name + r"\b", src) and val in ints:
            out.append((fname, name, val, ints.count(val)))
    return out


def literal_transcriptions(consts: dict[str, int]) -> list[tuple]:
    out = []
    for p in eval_files():
        out += _lit_on(p.read_text(encoding="utf-8"), consts, p.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把當下的常數值與抄寫點寫回 baseline。**複核完才可以跑**")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    fails: list[str] = []
    base = json.loads(io.open(BASELINE, encoding="utf-8").read()) if BASELINE.exists() else {}

    print("=" * 78)
    print("H1 報表不可以在 cp950 主控台印到一半 crash（stdout ＋ stderr 都要轉）")
    v1 = h1_violations()
    print(f"  掃了 {len(eval_files())} 支｜違規 {len(v1)} 支")
    for n in v1:
        print(f"    [FAIL] {n}")
    if v1:
        fails.append(f"H1：{len(v1)} 支腳本會在 cp950 下印到一半 crash")

    print()
    print("=" * 78)
    print("H2 eval 攔截的名字，套件子模組裡不得有裸名引用（否則 stub 蓋不到）")
    pn = patched_names()
    print(f"  從 {len(eval_files())} 支腳本反推出 {len(pn)} 個 patch 目標")
    bare = bare_refs_in_pkg(set(pn))
    if bare:
        for name, locs in sorted(bare.items()):
            print(f"    [FAIL] {name} 被裸名引用於 {locs[:5]}"
                  f"（eval 從 {sorted(pn[name])[:3]} 攔它）")
        fails.append(f"H2：{len(bare)} 個 patch 目標在套件裡被裸名引用")
    else:
        print("    ✔ 沒有裸名引用")

    print()
    print("=" * 78)
    print("H3 抄寫點要登錄；生產常數一變就要求重新複核")
    consts = current_constants()
    frozen = base.get("constants", {})
    drift = {k: (frozen.get(k), v) for k, v in consts.items() if frozen.get(k) != v}
    ack = {(a["file"], a["constant"], a["value"]) for a in base.get("acknowledged", [])}
    lits = literal_transcriptions(consts)
    if drift:
        print("  [FAIL] 生產常數與 baseline 不一致：")
        for k, (o, n) in sorted(drift.items()):
            print(f"    {k}: {o} → {n}")
        print("  ⇒ **每一個登錄過的抄寫點都要重新複核**，複核完再跑 `--update-baseline`：")
        for f, c, val, cnt in lits:
            print(f"    {f}  提到 {c}，且含字面 {val} × {cnt}")
        fails.append(f"H3：{len(drift)} 個生產常數變動，抄寫點需重新複核")
    else:
        print(f"  ✔ {len(consts)} 個生產常數與 baseline 一致")
    new = [t for t in lits if (t[0], t[1], t[2]) not in ack]
    if new:
        print("  [FAIL] 未登錄的抄寫候選（要寫下理由，或改成從模組讀）：")
        for f, c, val, cnt in new:
            print(f"    {f}  {c}={val}  字面出現 {cnt} 次")
        fails.append(f"H3：{len(new)} 個未登錄的抄寫候選")
    else:
        print(f"  ✔ {len(lits)} 個抄寫候選全部已登錄並附理由")

    if args.update_baseline:
        old = {(a["file"], a["constant"]): a.get("reason", "") for a in base.get("acknowledged", [])}
        BASELINE.write_text(json.dumps(
            {"_why": base.get("_why", ""), "constants": consts,
             "acknowledged": [{"file": f, "constant": c, "value": v,
                               "reason": old.get((f, c), "⚠ 未填理由——請補")}
                              for f, c, v, _ in lits]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] 已更新 {BASELINE.name}")
        return 0

    print()
    if fails:
        print("✖ GATE: FAIL")
        for f in fails:
            print(f"   {f}")
        return 1
    print("✔ GATE: PASS")
    return 0


def _selftest() -> int:
    """⚠ **判別力幾乎全在誤報對照**：陽性那幾條，一個「一律回違規」的實作也會過。"""
    ok = fail = 0

    def _a(name, cond, note=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  OK    {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {note}")

    # ── H1 ──
    _a("①  陽性：印中文又沒 reconfigure → 違規",
       prints_non_ascii('print("中文")') and not has_stream_reconfigure('print("中文")'))
    _a("①b **誤報對照**：純 ASCII 報表不算違規（它不會 crash）",
       not prints_non_ascii('print("all ascii")'))
    _a("①c **誤報對照**：非 ASCII 只在**註解**裡不算（註解不會被印出來）",
       not prints_non_ascii('# 中文註解\nprint("ascii")'))
    _a("①d **最重要的一條**：只轉 stdout 不算過關——traceback 走 stderr",
       not has_stream_reconfigure('sys.stdout.reconfigure(encoding="utf-8")'))
    _a("①e 兩個都轉才算過關",
       has_stream_reconfigure(
           'for _s in (sys.stdout, sys.stderr):\n    _s.reconfigure(encoding="utf-8")'))
    _a("①f **誤報對照**：完全不 print 的模組不算違規",
       not prints_non_ascii('X = "中文常數"'))

    # ── H2 ──
    _a("②  陽性：`ar.x = stub` 會被反推成 patch 目標",
       _PATCH_RE.findall("    ar.x = stub") == ["x"])
    _a("②b 陽性：patch 在定義模組上（`_g.y = stub`）也算",
       _PATCH_RE.findall("_g.y = stub") == ["y"])
    _a("②c **誤報對照**：`==` 比較不是指派，不可以算成 patch 目標",
       _PATCH_RE.findall("if ar.x == 1:") == [])
    _a("②d **誤報對照**：`ar.x.y = 1`（改屬性的屬性）不算攔截 `x`",
       _PATCH_RE.findall("ar.x.y = 1") == [])
    _a("②e **誤報對照**：讀取 `v = ar.x` 不算 patch",
       _PATCH_RE.findall("v = ar.x") == [])
    _a("②f 前提：反推得到的 patch 目標不是空的（空的話 H2 會真空成立）",
       len(patched_names()) >= 10)

    # ── H3 ──
    _a("③  陽性：提到常數名且含它的現值 → 列為候選，且計數正確",
       _lit_on("# RRF_TOP_N_PRIMARY\nf(30, 30)", {"RRF_TOP_N_PRIMARY": 30})
       == [("t.py", "RRF_TOP_N_PRIMARY", 30, 2)])
    _a("③b **誤報對照**：只提名字、沒有那個字面值 → 不列",
       _lit_on("x = RRF_TOP_N_PRIMARY + 1", {"RRF_TOP_N_PRIMARY": 30}) == [])
    _a("③c **誤報對照**：只有字面值、完全沒提名字 → 不列（否則任何 `30` 都中）",
       _lit_on("x = 30", {"RRF_TOP_N_PRIMARY": 30}) == [])
    _a("③d **誤報對照**：`True` 是 `int` 的子類，不可以被算成字面整數 1",
       _lit_on("# COMMIT_TOP_K\nx = True", {"COMMIT_TOP_K": 1}) == [])
    _a("③e **誤報對照**：名字只是別的識別字的一部分 → 不算提到它",
       _lit_on("MY_RRF_TOP_N_PRIMARY_X = 1\ny = 30", {"RRF_TOP_N_PRIMARY": 30}) == [])
    _a("③f 前提：baseline 檔存在且有 constants／acknowledged 兩區",
       BASELINE.exists()
       and set(json.loads(io.open(BASELINE, encoding="utf-8").read()))
       >= {"constants", "acknowledged"})
    _a("③g 前提：追蹤的常數真的都讀得到（讀不到 → H3 會真空成立）",
       len(current_constants()) == sum(len(v) for v in TRACKED.values()))

    print(f"\n  PASS {ok}  FAIL {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
