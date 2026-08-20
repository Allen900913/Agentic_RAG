"""check_web_claims.py — live web 路徑的逐條確定性驗收（零 LLM、零網路）。

**這支存在的理由**：live web 沒辦法定 gold（資料每天在變），所以整條 web 管線一直處於
「做了一整套機制但零實驗支撐」的狀態。解法是把不確定性切開：

  · 確定性的部分（白名單／去重／單域名上限／日期算術／預算／`kb_unfixable`）已由
    [`verify_web_gate_isolation.py`](verify_web_gate_isolation.py) 六道閘門 79 項斷言涵蓋，
    **零 LLM、零網路**。這支不重測那些。
  · **含 LLM 的部分**（Generator 到底有沒有引用 web 數字、KB 舊值與 web 新值衝突時有沒有
    並陳並標時點、web 有沒有被無差別觸發）以前沒有任何測試，因為 eval 全程 snapshot。
    把 Tavily 回應錄成 fixture ＋ 用 `AGENTIC_AS_OF_DATE` 鎖死「今天」之後，那幾題的外部
    世界變成靜態的 → 才寫得出斷言。這支就是那些斷言。

判 PASS／FAIL／N-A 三態（同 [`check_number_defects.py`](check_number_defects.py)）。
⚠ **N-A 不能併進 PASS**——那是「這題沒跑到」，不是「這題過了」。

流程：
    # ① 產生結果檔（replay 模式，絕不連網）
    .venv/Scripts/python.exe -u eval/record_web_fixture.py --mode replay \\
        --as-of 2026-08-15 --output experiments/web_replay_answers.json
    # ② 驗收
    .venv/Scripts/python.exe -u eval/check_web_claims.py \\
        --from-results experiments/web_replay_answers.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 生成端會吐各種 unicode dash（U+2011 non-breaking hyphen 等），字面比對前要正規化，
# 否則 "2026‑06‑12" 永遠對不上 "2026-06-12"。同 rq.translate_query_to_english 的處理。
_DASHES = "‐‑‒–—―"
# ⚠ 必須同時認全形【】：`gpt-oss-120b` 會吐全形引用標記（docs/AGENTIC.md A5 已記載，
#   下游 `_validate_and_fix_citations` 容忍它）。只認 ASCII 的話會把「有引用」誤判成
#   「完全沒引用 web」——2026-08-15 這支的第一版就這樣自己誤報了三題 FAIL。
_WEB_CITE_RE = re.compile(r"[\[【]\s*web:\s*([^\]】]+)[\]】]", re.IGNORECASE)
# 地區子網域＝**別的市場的報價**（實測同一天差 12%）。這是踩過兩次的坑，
# 而 fixture 的原始回應裡確實含 ca./sg./nz.finance.yahoo.com → 這條斷言測得到真防線。
_REGIONAL_RE = re.compile(r"^[a-z]{2}\.", re.IGNORECASE)


def _norm(s: str) -> str:
    for ch in _DASHES:
        s = s.replace(ch, "-")
    return s


def _number_seen(num: str, haystack: str) -> bool:
    """答案裡的數字是否溯得回來源。逐字比對 **＋ 四捨五入容忍**。

    ⚠ 只做逐字比對會誤報，而且我實際被咬過一次（2026-08-15）：`web-02` 判 FAIL 說
      `196.82` 是憑空生成，但 fixture 的 macrotrends 表格裡寫的是
      `| 2026 | 196.8165 | 188.6200 | ... |`（NVIDIA 年度平均股價）——**答案是正確地四捨五入**。
      我把那個誤報當成真陽性，寫了一整條「reflect 引入幻覺」的 BACKLOG 才發現。
      來源的小數位數常多於答案（`P/E Ratio Trailing: 31.325687` → 答案寫 `31.33` 也是同一種），
      所以捨入容忍是必要的，不是放水。

    ⚠ 與 `agentic_rag_v2.find_untraceable_numbers` 是**同一個判準的兩份實作**（這支刻意不 import
      生產模組，維持零依賴、秒級）。改一邊要同步改另一邊。
    """
    flat = haystack.replace(",", "")
    if num in haystack or num.replace(",", "") in flat:
        return True
    try:
        target = round(float(num.replace(",", "")), 2)
    except ValueError:
        return True                      # 解不出來就不主張是幻覺
    for tok in re.findall(r"\d[\d,]*\.\d+", haystack):
        try:
            if round(float(tok.replace(",", "")), 2) == target:
                return True
        except ValueError:
            continue
    return False


def _cited_hosts(answer: str) -> set[str]:
    hosts = set()
    for m in _WEB_CITE_RE.finditer(answer or ""):
        h = (urlparse(m.group(1).strip()).hostname or "").lower()
        hosts.add(h[4:] if h.startswith("www.") else h)
    hosts.discard("")
    return hosts


def _eval_one(claim: dict, rec: dict | None, allowlist: set[str],
               fixture_text: str = "") -> tuple[str, list[str]]:
    """回傳 (PASS/FAIL/N-A, 失敗原因清單)。

    **斷言只測「不論 LLM 挑哪個來源／哪個事實都必須成立」的性質。**
    第一版把值寫死成某一次錄製的答案（`4.46 兆`／`stockanalysis.com`），重放時 LLM 合法地
    挑了 fixture 裡另一個來源（`4.45 兆`／`finance.yahoo.com`）就 FAIL——那是量尺 flaky，
    不是系統退步。fixture 是一個**值空間**，不是單一答案。"""
    if rec is None:
        return "N-A", ["結果檔裡沒有這一題"]
    if rec.get("error"):
        return "N-A", [f"該題執行失敗：{rec['error'][:80]}"]

    ans = _norm(rec.get("answer") or "")
    calls = rec.get("n_web_calls", rec.get("n_web_calls_recorded"))
    a, fails = claim["assert"], []

    if "web_calls_eq" in a:
        if calls != a["web_calls_eq"]:
            fails.append(f"web 呼叫次數 {calls} ≠ 預期 {a['web_calls_eq']}")
    if "web_calls_gte" in a:
        if (calls or 0) < a["web_calls_gte"]:
            fails.append(f"web 呼叫次數 {calls} < 預期至少 {a['web_calls_gte']}")

    for s in a.get("must_contain", []):
        if _norm(s) not in ans:
            fails.append(f"答案缺少必要字串 {s!r}")
    for s in a.get("must_not_contain", []):
        if _norm(s) in ans:
            fails.append(f"答案不該出現 {s!r}")
    for group in a.get("must_contain_any", []):
        if not any(_norm(x) in ans for x in group):
            fails.append(f"答案沒有出現這組任一值 {group}")
    for pat in a.get("must_match", []):
        if not re.search(pat, ans):
            fails.append(f"答案沒有符合樣式 {pat!r} 的值")
    # 最強的一條：**答案裡符合這個樣式的每一個數字，都必須在 fixture 的原始內容裡逐字出現**。
    # 這直接測幻覺——只要 Generator 自己編或算出一個沒抓到的即時數字就會 FAIL。
    # ⚠ 樣式要收窄到「不可能被換算」的形態（如兩位小數的美元報價）；換算過的值（億／兆）
    #   本來就不會逐字出現在來源，拿來斷言只會製造假 FAIL。
    for spec in a.get("numbers_must_be_in_fixture", []):
        found = re.findall(spec, ans)
        if not found:
            fails.append(f"答案裡找不到樣式 {spec!r} 的數字（無從驗證來源）")
        for num in set(found):
            if not _number_seen(num, fixture_text):
                fails.append(f"數字 {num} 不在 fixture 抓到的內容裡（＝憑空生成）")

    # 「引用長得像出處」——2026-08-20 加。實測 live 路徑 5/37 題吐出【Reference 7, chunk #15】
    # 這種標記：它通過不了 allowlist 比對（validator 判成捏造 ID、丟回重寫），但重試上限是 1,
    # 用完就**原樣出貨**——讀者看到一個指向不存在來源的引用,比沒有引用更糟。
    # ⚠ 這是**普世性質**（任何答案都不該有）,所以 37 題全掛,判別力來自「32 題 PASS／5 題 FAIL」
    #   這個雙向分佈,不是靠陰性對照。snapshot 路徑實測 0/100,live 才有 → 不是量尺誤報。
    if a.get("no_fabricated_citations"):
        try:
            import agentic_rag_v2 as _ar
            bogus = sorted({s for s, _i in _ar._extract_citations(rec.get("answer") or "")
                            if not re.search(r"\.(html|txt)$", s, re.IGNORECASE)})
        except Exception as e:      # 匯入失敗要吵,不要靜默跳過一條斷言
            bogus, e = [], e
            fails.append(f"no_fabricated_citations 無法評估（import 失敗：{e!r}）")
        if bogus:
            fails.append(f"引用指向不存在的來源（不是檔名）：{bogus}")

    got = _cited_hosts(ans)
    if a.get("must_cite_any_web") and not got:
        fails.append("答案完全沒有引用任何 web 來源（web 打了卻沒進答案）")
    if a.get("no_web_citation") and got:
        fails.append(f"答案不該有 web 引用，卻引了 {sorted(got)}")
    if got and a.get("cited_hosts_allowlisted", True):
        # ④ 白名單只是授權不是過濾：Tavily 的 include_domains 是子網域包含式比對，
        #    真身回應裡確實混著 ca./sg./nz.finance.yahoo.com。這兩條驗的是本地複核。
        if bad := {h for h in got if _REGIONAL_RE.match(h) and h not in allowlist}:
            fails.append(f"引用了地區子網域（別的市場的報價）：{sorted(bad)}")
        if allowlist and (off := {h for h in got if h not in allowlist}):
            fails.append(f"引用了白名單外的主機：{sorted(off)}")

    return ("FAIL" if fails else "PASS"), fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-results", required=True,
                    help="record_web_fixture.py --mode replay 的輸出")
    ap.add_argument("--claims", default="eval/web_claims.json")
    ap.add_argument("--fixture", default="eval/web_fixture.json",
                    help="只讀 _meta.allowed_domains，用來驗『引用的主機都在白名單內』")
    args = ap.parse_args(argv)

    allowlist, fixture_text = set(), ""
    if Path(args.fixture).exists():
        _fx = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        allowlist = {h.lower() for h in _fx.get("_meta", {}).get("allowed_domains", [])}
        # 整份 fixture 的原始文字（title + content + url）攤平成一個 blob，供
        # numbers_must_be_in_fixture 判「這個數字我們到底有沒有抓到過」。
        fixture_text = "\n".join(
            f"{r.get('title','')} {r.get('content','')} {r.get('url','')}"
            for resp in _fx.get("responses", {}).values()
            for r in (resp.get("results") or []))

    claims = json.loads(Path(args.claims).read_text(encoding="utf-8"))
    data = json.loads(Path(args.from_results).read_text(encoding="utf-8"))
    recs = {r["id"]: r for r in data["records"]}

    meta_c, meta_r = claims.get("_meta", {}), data.get("meta", {})
    if meta_c.get("as_of") and meta_r.get("as_of") and meta_c["as_of"] != meta_r["as_of"]:
        # as-of 決定 kb_unfixable 與過時過濾的判斷 → 對不上就不是同一個實驗，別硬跑
        print(f"[ABORT] as-of 不一致：claims={meta_c['as_of']} results={meta_r['as_of']}")
        return 2

    tallies = {"PASS": 0, "FAIL": 0, "N-A": 0}
    print(f"as-of={meta_r.get('as_of')}  collection={meta_r.get('collection')}\n")
    for c in claims["claims"]:
        verdict, fails = _eval_one(c, recs.get(c["id"]), allowlist, fixture_text)
        tallies[verdict] += 1
        mark = {"PASS": "PASS", "FAIL": "FAIL", "N-A": "N-A "}[verdict]
        print(f"[{mark}] {c['id']:<8} {c['role']}")
        for f in fails:
            print(f"          └ {f}")
        if verdict == "FAIL" and c.get("expected_to_change"):
            print(f"          └ ⚠ 這條是**現況鎖**不是正確性斷言："
                  f"如果你剛改了觸發條件，FAIL 是預期的——請更新 web_claims.json 的預期值。")

    print(f"\nPASS {tallies['PASS']}  FAIL {tallies['FAIL']}  N-A {tallies['N-A']}"
          f"  （⚠ N-A 不能併進 PASS——那是沒跑到，不是過了）")
    return 1 if tallies["FAIL"] or tallies["N-A"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
