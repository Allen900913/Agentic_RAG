"""check_news_routing.py — 「新聞題有沒有拿財報冒充新聞」（零 LLM、零網路、零 Qdrant，只讀結果檔）

`check_historical_generation.py` 問的是**別的年份**：多年語料上線後，歷史題是答對、承認缺口、
還是拿別年份硬答。這支問的是**別的東西**：KB **依設計沒有新聞**
（`data_update_edgar.RAW_EXCLUDE_DIRS` 排除 `News/`），那麼新聞題的答案是從哪來的？

**為什麼需要它**（這支是被一份實測資料逼出來的）：拿「Tesla 最近有什麼重要消息」跑
`probe_replan_contribution.py`，commit 進答案的 chunk 逐個看是
`TSLA_10K_2023#59`／`TSLA_10K_2024#63`／`TSLA_10Q_202506#83`——**一則新聞都沒有**，
而那支探針把它們算成「引用貢獻」。那個指標量的是「chunk 有沒有被引用」，
**不是「引用它對不對」**。全碼庫在這支之前**沒有任何量尺**分得出這兩件事：
Synthesize 的五道 validator 一道都不會響（chunk 是真的、數字溯源得到、答案沒宣稱期別）。

---

## 五個狀態（只看答案本體，切掉證據尾巴後判；不可合併）

| 狀態 | 定義（純規則） |
|---|---|
| `web_grounded` | 本體裡有 ≥1 個 `[web: 網址]`／`【web: 網址】` 標記 |
| `admits_gap`   | 沒有 web 引用，但**同一個句子裡**同時有「承認措辭」與「流資訊範圍詞」 |
| `kb_only`      | 沒有 web 引用、沒有承認，但有 ≥1 個 `[檔名, chunk #N]` |
| `ungrounded`   | 三者皆無，卻有答案正文＝**什麼都沒引用** |
| `no_answer`    | 沒有答案／該題出錯 |

優先序 `web_grounded` > `admits_gap` > `kb_only` > `ungrounded`，順序是判準的一部分。

## 判定是**分類相對**的——狀態名刻意中性

| kind | 好 | 危險 |
|---|---|---|
| `flow`（KB 依設計沒有這種資料） | `web_grounded`／`admits_gap` | **`kb_only`／`ungrounded`** |
| `record`（**陰性對照**） | 沒有 web 引用 | **`web_grounded`＝過度路由** |
| `mixed` | **（不判定，見下）** | **`kb_only`／`ungrounded`** |

⚠ **狀態名不能寫成 `filing_as_news`**：同一個原始狀態在 record 題上是**正確行為**。
把分類烘進狀態名，等於讓量尺自己假設答案，而 record 那一半的誤報就再也看不見了。

⚠ **沒有 record 那一半，這支毫無意義**：只量 flow 的話，一個「所有題目一律送 web」的實作
會滿分——而那正是這支要驗收的改動最可能的失敗方式。

⚠ **mixed 是「不對稱」判定（2026-09-02 改）**。原本整個 kind 都不判定，理由是「分不出新聞
那一半是不是也用財報答的」——重讀那句話發現**它只對好的方向成立**：

- `web_grounded` → **仍然不判定**。整份答案有一個 web 引用，不代表**新聞那一半**用的是它
  （九成靠財報、只有一句標 web 也算）。要判它需要逐宣稱歸屬＝感知不是規則。
- `kb_only`／`ungrounded` → **判危險，而且是純規則**。mixed 題**依定義**帶著一個流資訊的
  半邊；整份答案裡**一個 web 引用都沒有**且**沒有承認** → 那半邊必然不是用 web 答的
  （KB 依設計沒有新聞）。這與 flow 的危險態是**同一個推論**，不需要任何新的感知。

少了這一格，22 題（21 題在冷凍 37、1 題 `col-08` 在生產 65）的**危險方向完全沒有量尺**。
⚠ 這不會讓「一律送 web」的實作變好看——擋它的仍然是 `record` 那半的陰性對照。

## 判別力集中在一條規則：**承認必須與流資訊同句**

承認詞表故意寫得寬（`未直接披露` 也收），**判別力不放在詞表而放在同句共現**——
因為長答案裡「某個細節沒披露」的保留幾乎一定會出現，寬詞表單獨使用會把危險態整片吃掉，
量尺於是報出一個比實況好看的數字。逐字凍結的誤報對照是 `mh-09`：

> 「META 是被穆迪評為信用正面的公司，且其營業利益率為 40.62%。**若需更精確的 847.5 億美元
>   金額，現有文件未直接披露。**」

它**斷言了一個新聞事實**（哪家公司）卻只有 10-Q 債務餘額當依據，保留的只是金額細節。
判成 `admits_gap` 就是把危險態記成誠實。對照的陽性是 `mh-07`：

> 「**新聞稿中並未出現**任何公司宣佈『在歐洲投入逾 100 億歐元』的投資計畫，因此無法確定是哪家公司。」

**同句規則實測動 3/40 題**（news-01／06／08），三題都是誤報，而且分屬三種不同的形狀：

1. `mh-09` 型：承認的是**別的細節**（金額），主張本身照樣斷言。
2. `news-08` 型：承認在後句、「新聞」在前句——**跨句湊對**。
3. `news-06` 型：承認詞出現在**純業務敘述**裡，與資訊可得性無關
   （「對 NVIDIA **未提供**獨立伺服器 CPU 的布局構成挑戰」）。這一型光靠收窄詞表擋不掉：
   「未提供」是正當中文，寬詞表必然命中。

三型各有一條逐字凍結的誤報對照（`--selftest`），且變異測試確認「改成整篇比對」會被抓到。

⚠ **`最近` 刻意不收進範圍詞**：它在本語料裡同時是流資訊（「最近有什麼消息」）與財報用語
（「最近完整財務年度」「最近一季」）。實測把它加進去對 40 份答案的判定**零差異**，
所以收它只會增加誤報面而買不到任何判別力。

## 第二軸：路由（打了沒有）vs 落地（用了沒有）

`n_web_calls` 在結果檔裡有的話另外列一軸，因為兩種病的**修法完全不同**：

- `no_route`   ：`web_calls == 0` 且狀態是 `kb_only` → **路由沒觸發**（實測 news-05／news-09）
- `web_ignored`：`web_calls >= 1` 且沒有 web 引用 → **打了網路卻沒用**（實測 mh-07／mh-09）

⚠ 歸因的觸發條件是**讀判定表**（`"危險" in _VERDICT[kd][st]`），不是寫死 `kd == "flow"`。
舊版寫死，於是 mixed 在 2026-09-02 有了危險態之後，`mh-09`（judged 危險、`c=3`＝打了 web
卻沒引用）**有判定卻在第二軸完全不出現**。列 kind 清單就會漏掉下一個。

合併成一個「新聞題失敗率」會把兩個成因混成一個數字，而它們一個在 Grader、一個在 Generator。

## 能力上界（寫清楚免得被當成保證）

- **承認詞表是硬編碼的，只對校準過的那批答案負責**（同
  [`check_historical_generation.py`](check_historical_generation.py) 的例外理由）：本檔的詞表與
  範圍詞是拿 `experiments/web_fixture_answers_news37_all.json` 的 **40 份答案逐份讀過**才定的。
  換一個生成模型或換一種答案風格之後，**要重新逐份複核或改成 LLM 裁決**，不要直接沿用。

- **不驗 web 答案對不對**。「答案裡的報價是否逐字出現在 fixture」是
  [`check_web_claims.py`](check_web_claims.py) 的事，那支要 fixture、這支不要。
  在這裡再做一份會是第二個較弱的副本。
- **不驗 record 題答得對不對**。那是 `check_number_defects.py` 與 RAGAS 那條線的事；
  這支對 record 只問一件事：**web 有沒有被無差別觸發**。
- `web_grounded` 只證明「有 web 出處」，不證明比例。一份九成靠財報、只有一句標 web 的答案
  照樣算 `web_grounded`——所以逐題明細一定要印 `web=/kb=` 兩個計數供人工看。

用法：
    .venv/Scripts/python.exe eval/check_news_routing.py --selftest
    .venv/Scripts/python.exe eval/check_news_routing.py \\
        --results experiments/web_fixture_answers_news37_all.json
    .venv/Scripts/python.exe eval/check_news_routing.py --results <before.json> <after.json>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ⚠ `hasattr` 不是防禦性寫作的裝飾：本檔會被 `verify_answer_validators` 閘門⑱ 當函式庫
#   import，而那時 `agentic_rag_version` 已經把 `sys.stdout` 換成 `_ThreadLocalMuteStream`
#   （沒有 `reconfigure`）→ import 當場炸。當腳本跑時它照樣生效。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 本檔輸出含 ⚠／全形，cp950 會炸

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import rag_query as rq                                        # noqa: E402
# 引用抽取用生產的**唯一定義點**：引用格式改了這支要跟著改，不要自己抄一份 regex。
from agentic_rag_version import _extract_citations                 # noqa: E402

QUESTION_SET = _ROOT / "eval" / "news_routing_questions.json"
STATES = ("web_grounded", "admits_gap", "kb_only", "ungrounded", "no_answer")
KINDS = ("flow", "mixed", "record")

# `[web: url]` 與 `【web: url】`。⚠ **兩種括號都要收**：生產的 `_WEB_MARK` 常數只認半形，
# 而實測 40 份答案的 web 引用**全部是全形**（生成端跟著中文標點走）。只認半形會讓這支
# 量出「web 引用 0 筆」——第一版就是這樣，而 0 筆看起來完全像個合理的壞消息。
_WEB_CITE_RE = re.compile(r"[\[【]\s*web\s*[:：]\s*([^\]】]+?)\s*[\]】]", re.IGNORECASE)

# 承認措辭。**刻意寫寬**：判別力不在這個詞表，在下面的同句共現（見 docstring）。
_ADMIT_RE = re.compile(
    r"沒有足夠(的)?(資訊|資料)"
    r"|未(直接|明確|另行)?(列示|列出|包含|提供|揭露|披露|載明|指出|提及|出現|涵蓋)"
    r"|並未(提及|出現|說明|報導)"
    r"|無法(得知|回答|判斷|提供|給出|取得|確定|查證)"
    r"|(參考)?資料中(沒有|未|並未)"
    r"|沒有(任何)?(文件|資料|來源|報導|新聞)"
    r"|查無"
    r"|找不到"
    r"|(don't|do not) have enough information"
    r"|not (disclosed|provided|available|mentioned) in the",
    re.IGNORECASE,
)

# 「流資訊」範圍詞＝這句話在講的是**外部即時世界**而不是某個財報細節。
# ⚠ 刻意**不收**「目前／現在／資料／文件」：前兩個在正常答案裡到處都是，後兩個會讓
#   `mh-09` 那種「某個金額細節文件未披露」的保留被算成承認缺新聞（見 docstring 的誤報對照）。
_FLOW_SCOPE_RE = re.compile(
    r"新聞|消息|報導|報道|媒體|新聞稿|公告|分析師|評等|目標價|股價|盤中|即時|最新|近期|網路|網站"
    r"|news|analyst|headline",
    re.IGNORECASE,
)

# 斷句：中文句號/問號/驚嘆號/換行/分號。承認與範圍詞必須落在**同一句**。
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


def _sentences(body: str) -> list[str]:
    return [s for s in _SENT_SPLIT_RE.split(body) if s.strip()]


def admits_flow_gap(body: str) -> bool:
    """答案有沒有**針對流資訊**承認拿不到。同句共現才算——理由見 docstring。"""
    return any(_ADMIT_RE.search(s) and _FLOW_SCOPE_RE.search(s) for s in _sentences(body))


def classify(answer: str | None) -> tuple[str, int, int]:
    """回 (狀態, web 引用數, chunk 引用數)。判在**答案本體**上（先切掉證據尾巴）。

    ⚠ 證據尾巴（`📚 引用來源…`）是 harness 印的「Generator 拿到什麼」，不是它引用了什麼。
      不切掉的話，一份完全沒有 inline 引用的答案會被算成有引用＝`ungrounded` 永遠不觸發。"""
    if not (answer or "").strip():
        return "no_answer", 0, 0
    body = rq.strip_evidence_tail(answer)
    n_web = len(set(_WEB_CITE_RE.findall(body)))
    n_kb = len(_extract_citations(body))
    if n_web:
        return "web_grounded", n_web, n_kb
    if admits_flow_gap(body):
        return "admits_gap", 0, n_kb
    if n_kb:
        return "kb_only", 0, n_kb
    return "ungrounded", 0, 0


# 判定表：kind → {狀態: 判語}。⚠ 同一個狀態在不同 kind 下是不同的判語，這是刻意的。
_VERDICT = {
    "flow": {"web_grounded": "好", "admits_gap": "誠實", "kb_only": "**危險**",
             "ungrounded": "**危險**", "no_answer": "N/A"},
    "record": {"web_grounded": "**過度路由**", "admits_gap": "—", "kb_only": "好",
               "ungrounded": "—", "no_answer": "N/A"},
    # ⚠ **mixed 刻意是「不對稱」的：只判危險那一邊，好的那一邊維持不判定。**
    #   原本整個 kind 都寫「（不判定）」，理由是「分不出新聞那一半是不是也用財報答的」。
    #   2026-09-02 重讀那句話發現：**它只對好的方向成立**。
    #     · `web_grounded` → 確實不能判「好」：整份答案有一個 web 引用，不代表**新聞那一半**
    #       用的是它（九成靠財報、只有一句標 web 也算）。要判它需要逐宣稱歸屬＝感知不是規則。
    #     · `kb_only`／`ungrounded` → **可以判「危險」，而且是純規則**：mixed 題**依定義**帶著
    #       一個流資訊的半邊，而整份答案裡**一個 web 引用都沒有**且**沒有承認** → 那半邊
    #       必然不是用 web 答的（KB 依設計沒有新聞）。這與 flow 的危險態是同一個推論。
    #   → 少了這一格，22 題（21 題在冷凍 37、1 題 col-08 在生產 65）的**危險方向完全沒有量尺**。
    #   ⚠ `admits_gap` 在 mixed 是「誠實」不是「好」：它交代了流那半拿不到，但 KB 那半答得
    #     對不對本量尺不驗（同 record 的註記）。
    "mixed": {"web_grounded": "（不判定）", "admits_gap": "誠實", "kb_only": "**危險**",
              "ungrounded": "**危險**", "no_answer": "N/A"},
}

_WEB_CALL_KEYS = ("n_web_calls", "web_calls", "agentic_web_calls")


def _web_calls(rec: dict):
    for k in _WEB_CALL_KEYS:
        if rec.get(k) is not None:
            return rec[k]
    return None


def _load(path: Path) -> tuple[dict, dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get("meta", {}), {r["id"]: r for r in d.get("records", [])}


def run(paths: list[str], question_set: str) -> int:
    qs = json.loads(Path(question_set).read_text(encoding="utf-8"))["questions"]
    kind = {q["id"]: q["kind"] for q in qs}

    arms = []
    for p in paths:
        meta, recs = _load(Path(p))
        arms.append({"name": Path(p).name, "meta": meta, "recs": recs})

    print("=" * 100)
    for a in arms:
        m = a["meta"]
        print(f"  {a['name']}  ←  collection={m.get('collection', '?')}  "
              f"as_of={m.get('as_of', '?')}  n={len(a['recs'])}")
    if len({(a['meta'].get('collection'), a['meta'].get('as_of')) for a in arms}) > 1:
        print("  ⚠ 各臂的 collection／as_of 不同 → **這不是乾淨的 A/B**，差異含混雜因素。")
    print("=" * 100)

    ids = [q["id"] for q in qs if any(q["id"] in a["recs"] for a in arms)]
    absent = [q["id"] for q in qs if q["id"] not in ids]
    if absent:
        print(f"⚠ 題庫裡有 {len(absent)} 題不在任何結果檔裡（不計入分母）：{absent}")

    w = 26
    tally = {a["name"]: {k: {s: 0 for s in STATES} for k in KINDS} for a in arms}
    routing = {a["name"]: {"no_route": [], "web_ignored": []} for a in arms}

    for kd in KINDS:
        rows = [i for i in ids if kind[i] == kd]
        if not rows:
            continue
        print(f"\n── kind = {kd}（{len(rows)} 題）" + "─" * 60)
        print(f"{'id':<9}" + "".join(f"{a['name'][:24]:<{w}}" for a in arms))
        for qid in rows:
            line = f"{qid:<9}"
            for a in arms:
                rec = a["recs"].get(qid)
                if rec is None:
                    line += f"{'（缺）':<{w}}"
                    continue
                st, nw, nk = classify(rec.get("answer"))
                tally[a["name"]][kd][st] += 1
                calls = _web_calls(rec)
                # ⚠ **危險態在哪個 kind 出現就要歸因**，不能只掛 flow：mixed 於 2026-09-02 起
                #   也有危險態（見 `_VERDICT` 的不對稱註記），而它的兩種成因與 flow 完全相同
                #   （路由沒觸發 vs 打了沒用）。寫死 `kd == "flow"` 會讓 mixed 的危險題
                #   **有判定卻沒有成因**——實測 `mh-09` 就是這樣：judged 危險、c=3（打了 web
                #   卻沒引用），而它在第二軸整個不出現。判準改成「這個 kind 的這個狀態判危險嗎」，
                #   直接讀判定表，不再自己列 kind 清單（列清單就會漏下一個）。
                if "危險" in _VERDICT[kd][st]:
                    (routing[a["name"]]["no_route"] if calls == 0
                     else routing[a["name"]]["web_ignored"]).append(f"{qid}({kd})")
                cs = "-" if calls is None else str(calls)
                line += f"{f'{st} w={nw} k={nk} c={cs}':<{w}}"
            print(line)

    print("\n" + "=" * 100)
    for kd in KINDS:
        n = sum(1 for i in ids if kind[i] == kd)
        if not n:
            continue
        print(f"\n{kd}（{n} 題）")
        for s in STATES:
            if all(tally[a["name"]][kd][s] == 0 for a in arms):
                continue
            v = _VERDICT[kd][s]
            print(f"  {s:<14}{v:<12}" + "".join(
                f"{tally[a['name']][kd][s]:>{w}}" for a in arms))

    print("\n" + "─" * 100)
    print("第二軸：**危險題的成因**（flow ＋ mixed；兩種病的修法不同，不可合併成一個失敗率）")
    for a in arms:
        r = routing[a["name"]]
        print(f"  {a['name']}")
        print(f"     no_route   （web_calls=0，路由沒觸發 → Grader/路由層）：{r['no_route']}")
        print(f"     web_ignored（打了 web 卻沒有 web 引用 → Generator）  ：{r['web_ignored']}")
    if all(_web_calls(rec) is None for a in arms for rec in a["recs"].values()):
        print("  ⚠ 這些結果檔沒有 web 呼叫數欄位（`run_agentic_on_evalset.py` 不寫），"
              "**第二軸整個不可得**——上面兩列一律落在 web_ignored，那是欄位缺失不是量測。")

    print("\n⚠ `mixed` 是**不對稱**判定：`kb_only`／`ungrounded` 判危險"
          "（純規則——mixed 依定義帶著流的半邊，而整份答案零 web 引用又沒承認 → "
          "那半邊必然不是用 web 答的）；`web_grounded` 仍然**不判定**"
          "（有一個 web 引用不代表**新聞那一半**用的是它）。")
    print("⚠ `record` 是陰性對照，只問「web 有沒有被無差別觸發」，**不驗答得對不對**。")
    print("⚠ `web_grounded` 不證明比例：九成靠財報、只有一句標 web 也算。看逐題的 w=/k=。")
    return 0


def selftest() -> int:
    """雙向自測。危險方向是**把 kb_only 判成 admits_gap 或 web_grounded**——那會讓這支
    報出比實況好看的數字。真實答案逐字凍結在這裡（不讀 experiments/，否則一修好就消失）。"""
    TAIL = "\n\n---\n📚 引用來源（Generator 實際依據的 chunk）:\n  - X.html #1\n  - Y.html #2\n"
    cases = [
        # ── flow 的危險態：拿財報回答「最近有什麼消息」，零交代 ─────────────────────
        ("NVIDIA 最近取得美國政府授權，允許自 2026 年 2 月起向特定中國客戶出貨少量 H200 "
         "AI 晶片，但截至目前尚未因該授權產生任何營收【NVDA_10Q_202604.html, chunk #44】。",
         "kb_only", "news-05 逐字：10-Q 冒充「最近的消息」，有引用、零交代"),
        ("Alphabet 最近透過發行債務與可轉換優先股籌得數百億美元，同時在 2025 財年大幅投入逾 "
         "900 億美元於設備購置【GOOGL_10K_2025.html, chunk #271】。",
         "kb_only", "news-09 逐字：拿 FY2025 10-K 回答「最近的籌資消息」"),
        # ── 誤報對照：長答案裡的細節保留**不算**承認缺新聞（判別力全在這一條）────────
        ("Meta Platforms, Inc.（META）是那筆 847.5 億美元融資案在 6 月 5 日獲穆迪評為"
         "「信用正面」的公司；其營業利益率在最近的 TTM 為 40.62 %"
         "【META_Fundamentals_20260612.txt, chunk #0】。"
         "若需更精確的 847.5 億美元金額，現有文件未直接披露。",
         "kb_only", "**誤報對照**：mh-09 逐字。斷言了新聞事實、只保留金額細節 → 不是承認"),
        # ── 陽性：承認與流資訊同句 ────────────────────────────────────────────────
        ("新聞稿中並未出現任何公司宣佈「在歐洲投入逾 100 億歐元」的投資計畫，因此無法確定是"
         "哪家公司。以下列出主要科技公司最近完整財務年度的總營收，供參考："
         "NVIDIA 2026 財年總營收為 2,159.38 億美元【NVDA_10K_2026.html, chunk #0】。",
         "admits_gap", "mh-07 逐字：承認沒有新聞後才附財報「供參考」＝誠實態"),
        ("我沒有足夠的資訊回答最新新聞。", "admits_gap", "短承認：承認詞＋範圍詞同句"),
        # ── 誤報對照：承認詞與範圍詞**不同句**不算（M1：整篇比對會在這裡失手）────────
        # 兩句逐字取自 news-08，只把該句的【web: 網址】換成 chunk 引用，好讓承認路徑可達
        # （原句有 web 引用就會停在 web_grounded，測不到這條規則）。
        ("有，Microsoft 最近有多則新聞報導其參與 AI 與資安的合作。"
         "Microsoft 10‑K（FY2026）亦說明公司與 OpenAI 的長期戰略合作，持續取得 AI 模型與"
         "基礎設施的使用權，為產品整合提供 AI 能力，雖未明確提及資安，但顯示公司在 AI 佈局上"
         "具優勢【MSFT_10K_2026.html, chunk #12】。",
         "kb_only", "**誤報對照（同句規則的主要證據）**：news-08 逐字。承認在後句、"
                    "「新聞」在前句 → 整篇比對會判成 admits_gap"),
        # 第三種誤報：承認詞出現在**純業務敘述**裡，跟資訊可得性完全無關。
        ("AMD 與 Intel 仍是伺服器 CPU 市場的領頭羊，兩家公司正積極向 AI 資料中心供應 CPU，"
         "對 NVIDIA 未提供獨立伺服器 CPU 的布局構成挑戰【NVDA_10K_2026.html, chunk #4】。"
         "新聞在財報前的關注點可歸納為外部競爭與內部市場布局。",
         "kb_only", "**誤報對照**：news-06 逐字。「NVIDIA 未提供獨立伺服器 CPU」是業務敘述，"
                    "不是「查不到資料」——寬詞表在這裡必然命中，靠同句規則擋"),
        ("這一季的毛利率無法從提供的文件得知【AAPL_10Q_202606.html, chunk #7】。"
         "以下補充最近的產品線概況。",
         "kb_only", "**誤報對照**：承認在前句、範圍詞在後句 → 不同句不算承認"),
        # ── web 落地 ─────────────────────────────────────────────────────────────
        ("亞馬遜近期宣布在歐洲展開多項大型投資【web: https://finance.yahoo.com/x.html】。",
         "web_grounded", "news-11 逐字：**全形**【web:】——生產的 _WEB_MARK 只認半形"),
        ("Amazon announced a €1bn plan [web: https://www.reuters.com/x].",
         "web_grounded", "半形 [web:] 也要收（兩種括號都在實測裡出現過）"),
        ("和解金為 0.95 億美元【web: https://apnews.com/a】；10-Q 另揭露 DMA 風險"
         "【AAPL_10Q_202606.html, chunk #63】。",
         "web_grounded", "web＋kb 並存時 web_grounded 優先（news-01 的形狀）"),
        ("雖然新聞中沒有任何報導提及此事【web: https://reuters.com/a】。",
         "web_grounded", "**誤報對照**：有 web 引用時不得掉進 admits_gap（優先序）"),
        # ── 證據尾巴 ─────────────────────────────────────────────────────────────
        ("Tesla 第二季交車量創高。" + TAIL, "ungrounded",
         "**誤報對照**：證據尾巴不是引用——不切掉的話 ungrounded 永遠不會觸發"),
        ("Tesla 第二季交車量創高。\n\n---\n📚 引用來源:\n  - 新聞中未提及【web: https://a.com/b】\n",
         "ungrounded", "**誤報對照**：尾巴裡的 web 標記與承認措辭都不算數"),
        # ── 題目重述不構成任何狀態 ────────────────────────────────────────────────
        ("關於「最近有什麼新聞」這個問題，Tesla 的 10-K 指出其產能擴張計畫"
         "【TSLA_10K_2023.html, chunk #59】。",
         "kb_only", "**誤報對照**：答案裡出現「新聞」二字不代表承認，也不代表有 web 出處"),
        (None, "no_answer", "沒有答案"),
        ("   ", "no_answer", "空白答案不算 ungrounded"),
    ]
    ok = 0
    for ans, want, why in cases:
        got, nw, nk = classify(ans)
        good = got == want
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] {why}\n         got={got} (web={nw} kb={nk}) want={want}")

    # 判定表本身的自測：同一個原始狀態在 flow 與 record 下**必須**是不同的判語。
    same = [s for s in STATES if _VERDICT["flow"][s] == _VERDICT["record"][s] != "N/A"]
    tbl_ok = _VERDICT["flow"]["kb_only"] != _VERDICT["record"]["kb_only"] and \
        _VERDICT["flow"]["web_grounded"] != _VERDICT["record"]["web_grounded"]
    ok += tbl_ok
    print(f"[{'PASS' if tbl_ok else 'FAIL'}] 判定表：kb_only 與 web_grounded 在 flow／record "
          f"下判語相反（狀態名中性、判定分類相對）\n         共用判語的狀態={same}")
    # mixed 的不對稱性：危險那一邊要與 flow 同判語，好那一邊要維持不判定。
    # ⚠ 沒有這一條，「mixed 全部判危險」與「退回全部不判定」兩種改動都不會被發現。
    mx = {k: _VERDICT["mixed"][k] for k in ("kb_only", "web_grounded")}
    mx_ok = (_VERDICT["mixed"]["kb_only"] == _VERDICT["flow"]["kb_only"]
             and _VERDICT["mixed"]["ungrounded"] == _VERDICT["flow"]["ungrounded"]
             and _VERDICT["mixed"]["web_grounded"] == "（不判定）"
             and _VERDICT["record"]["kb_only"] != _VERDICT["mixed"]["kb_only"])
    ok += mx_ok
    print(f"[{'PASS' if mx_ok else 'FAIL'}] 判定表：mixed **不對稱**——危險邊與 flow 同判語，"
          f"好的那邊維持不判定" + f"{chr(10)}         mixed={mx}")
    total = len(cases) + 2
    print(f"\n{ok}/{total} PASS")
    return 0 if ok == total else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", nargs="+", help="一或多份含 records[].answer 的結果檔")
    ap.add_argument("--question-set", default=str(QUESTION_SET))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.results:
        ap.error("--results 是必要的（或用 --selftest）")
    return run(args.results, args.question_set)


if __name__ == "__main__":
    raise SystemExit(main())
