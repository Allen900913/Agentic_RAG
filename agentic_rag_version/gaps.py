"""agentic_rag_version.gaps — 「這一題還缺什麼」與對應的揭露句。

四種缺口各有各的判準，**刻意不合併**：
  · `_news_freshness_gaps`      KB 這批 chunk 對「要多新」而言太舊
  · `_unmet_realtime_gaps`      Grader 判要即時資料而沒拿到（只看 `realtime_need`，不看問法）
  · `_unfulfilled_web_route_gaps`  route 說要上網卻沒拿到可用結果
  · `_basis_disclosure_notice`  答案用了 TTM 欄位卻沒交代口徑

⚠ **揭露句都以 `

---
⚠` 開頭**，那個邊界前綴命中 `rq.EVIDENCE_TAIL_RE`，
  於是每個呼叫 `strip_evidence_tail` 的消費端都看不到它——這是它們**安全**的原因
  （機械式附加不會改變任何量尺的判定），也是閘門⑱f 唯一該驗的不變量。

⚠ `_build_temporal_contract` 是 eval 會 monkeypatch 的名字，本模組內不得裸用。
"""
from __future__ import annotations

import re
from datetime import date

import rag_query as rq
from .tracing import _trace

import agentic_rag_version as _pkg   # ⚠ 循環 import 刻意：`_pkg.<name>` 在呼叫時
                                     #   才解析，eval 打在套件上的 stub 才蓋得到。
from .coverage import _format_kb_coverage, _mentioned_tickers, _source_coverage_parts
from .freshness import _get_as_of_date, _kb_ceiling_date, _source_newest_date
from .ratio import _BACKREF_RE, _TICKER_CANON, _has_ratio_intent, _resolve_ratio_fields

FRESHNESS_SNAPSHOT = "snapshot"


def _is_dependent_hop(task: str) -> bool:
    """依賴型第二跳：帶未解回指代名詞(該公司…)且句中沒點名任何具體公司。這種子問題要等
    第一跳辨識出公司、回填實體後才能正確檢索。已含具體公司名 → 不算未解(planner 已自行填好)。"""
    if not _BACKREF_RE.search(task or ""):
        return False
    return not _mentioned_tickers(task)


def _resolve_hop_entity(todos: list[dict], collected: list[dict]) -> str | None:
    """從已完成待辦推出「第一跳辨識出的公司」正規名,供第二跳回填『該公司』。純確定性:
    先看非依賴型 done 待辦的局部結果文字命中的 ticker(眾數),再退回已 commit chunk 的 owner ticker。"""
    from collections import Counter
    cnt: Counter = Counter()
    for t in todos:
        if t.get("status") == "done" and not _is_dependent_hop(t.get("task", "")):
            res = t.get("result", "") or ""
            for tk in rq._find_all_ticker_aliases(res.lower(), res):
                cnt[tk] += 1
    if not cnt:   # summary 沒明確命中 → 退回 commit chunk 的 owner ticker
        for c in collected:
            tk = c.get("ticker") or (c.get("payload") or {}).get("ticker")
            if tk:
                cnt[tk] += 1
    if not cnt:
        return None
    top_ticker = cnt.most_common(1)[0][0]
    return _TICKER_CANON.get(top_ticker, top_ticker)


def _fill_dependent_hop(task: str, entity: str) -> str:
    """把第二跳子問題裡的『該公司…』回指代名詞換成解出的具體公司名。"""
    return _BACKREF_RE.sub(entity, task)


def _build_temporal_contract(freshness_mode: str) -> str:
    coverage_text = _format_kb_coverage(_pkg._get_kb_coverage())
    if freshness_mode == FRESHNESS_SNAPSHOT:
        policy = """時間模式：snapshot（封閉知識庫評測）。
- 「最近／最新」只表示下方 KB snapshot 中最新可用文件，不表示真實世界今天。
- 只能使用 snapshot 或工具結果明確出現的期間；絕不靠模型記憶推算季度、年份或月份。
- 不要因 KB 早於 wall clock 而新增 web 待辦或在答案加入 cutoff 警語。
- 使用者明確指定期間時，必須尊重原期間，不得換成 KB 最新期間。"""
    else:
        web_state = "可用" if _pkg.ENABLE_WEB_SEARCH else "停用"
        policy = f"""時間模式：live。今天是 {_get_as_of_date().isoformat()}，web search 目前{web_state}。
- 「最近／最新」表示截至今天；今天與 KB cutoff 是兩條獨立時間軸，不得混為一談。
- 只能使用 snapshot 或工具結果明確出現的期間；絕不靠模型記憶推算季度、年份或月份。
- 若新聞問題要求截至今天且 KB cutoff 較舊，先查 KB，再只針對 cutoff 後的缺口決定是否用 web。
- 使用者明確指定期間時，必須尊重原期間，不得換成 KB 最新期間。"""
    return policy + "\n\n" + coverage_text


def _build_todo_temporal_scope(task: str, freshness_mode: str) -> str:
    """為單一 todo 產生精簡的 coverage 說明（餵給 Grader 的 `temporal_scope`）。

    ⚠ 2026-08-15 起**不再回傳 freshness gap**。缺口改由執行完的實際結果算（`_news_freshness_gaps`），
      理由見那支的 docstring。這裡回傳單一字串而不是留一個永遠是空 list 的第二欄——
      留著等於是給下一個人一個會漂移的死欄位。
    """
    coverage = _pkg._get_kb_coverage()
    scope_coverage = _format_kb_coverage(coverage, _mentioned_tickers(task) or None)
    if freshness_mode == FRESHNESS_SNAPSHOT:
        return ("snapshot 模式：『最近／最新』= KB 中最新可用資料；不要參照 wall clock，"
                "不要加入 cutoff 警語。\n" + scope_coverage)
    return (f"live 模式：今天是 {_get_as_of_date().isoformat()}。不得把今天誤認成 KB cutoff；"
            "不得創造未出現在下方 snapshot 或工具結果中的期間。\n" + scope_coverage)


def _news_freshness_gaps(chunks: list[dict], as_of: date) -> list[dict]:
    """從**實際採用的 chunk** 算新聞時效缺口：用到了某家的 news、而該家 news cutoff 早於今天 → 一筆。

    ⚠ **2026-08-19 起對 KB 恆回空集合**（KB 已不收新聞,沒有 `doc_type=news` 的 chunk）。
    保留函式與 `eval/verify_answer_validators.py` 閘門④⑤ 的斷言,是為了「新聞復活時警語
    立刻回來」——那些斷言餵的是合成 chunk,不讀 KB,所以現在仍有判別力。
    ⚠ **但時效警語本身沒有跟著死**：它現在由 `_unmet_realtime_gaps` 供應（判準改成「這個
    子問題需要即時資料、卻沒拿到 web 補充」）。兩種缺口併存,見 `_pkg._run_one_todo` 的接線。

    ⚠ 2026-08-15 從「看問法」改成「看結果」。舊判準是兩個硬編碼詞表串聯：
          `bool(_RELATIVE_TIME_RE.search(task)) and rq.looks_like_news_query(task)`
      這正是 CLAUDE.md 明列的反模式（用字串比對做感知），而**漏網的代價是零揭露**：
      「Microsoft 最新一季的 Azure 營收成長率」過得了 `_RELATIVE_TIME_RE`（有「最新」）卻過不了
      `looks_like_news_query`（不是新聞措辭）→ 一句時效警語都不會印。
      （2026-08-13 拿掉的是 **web 觸發**那兩道同名閘門；警語這道當時漏改，文件卻已寫成「全數移除」。）

    改看結果之後判準是純比對、零詞表，而且更準：問法像新聞、答案其實全靠財報時，舊版會印一句
    無關的新聞 cutoff 警語，新版不會。cutoff 取 **KB coverage 裡該 ticker 最新的一則新聞**
    （警語講的是「KB 的新聞收到哪天」，不是「這次剛好引到哪一則」）。
    """
    if not chunks:
        return []
    cov = _pkg._get_kb_coverage()
    if not cov.get("available"):
        return []
    used = {parts[0] for c in chunks
            for parts in (_source_coverage_parts(str(c.get("source") or "")),)
            if parts and parts[1] == "news"}
    gaps: list[dict] = []
    for ticker in sorted(used):
        cutoff = _source_newest_date(
            str(((cov.get("tickers", {}).get(ticker) or {}).get("news") or {}).get("source") or ""))
        if cutoff and cutoff < as_of:
            gaps.append({"ticker": ticker, "cutoff": cutoff.isoformat(),
                         "as_of": as_of.isoformat(), "doc_type": "news"})
    return gaps


def _unmet_realtime_gaps(task: str, need: str, chunks: list[dict], as_of: date) -> list[dict]:
    """這個子問題**需要即時資料**（Grader 判 `realtime_need != none`）→ 記一筆時效缺口。

    ⚠ **2026-08-19 新增，補上 KB 拔除新聞後死掉的那道揭露。**
      舊的唯一缺口來源 `_news_freshness_gaps` 的判準是「用了 KB 新聞、而那則新聞過期」。
      KB 不收新聞之後那個判準**沒有指涉對象 → 恆回空集合 → 時效警語永遠不印**（實測）。
      但風險沒有消失，只是換了位置：即時題 → Grader 正確判不足 → 去打 web →
      **web 預算用完（`_pkg.QUERY_WEB_BUDGET`）或搜不到** → 答案回頭用財報 chunk 生成 → 零揭露。
      那正是 CLAUDE.md 記著的「把三週前的數字講成『今天股價』」，只是來源從新聞換成了 10-K。

    判準刻意**只看 Grader 的 `realtime_need`**，不看問法、不看候選內容：
      · 「需不需要即時資料」有判斷成分 → LLM（已在 Grader 內，不多花一次呼叫）
      · 「這次有沒有拿到 web」是確定性的 → `_format_unresolved_freshness_notice` 用 `web_used` 篩
    兩者分屬 CLAUDE.md〈LLM 與 Python 的分工〉的兩邊，這裡不重複判斷。

    ⚠ 本函式**不檢查 `web_used`**：那個過濾統一在 `_format_unresolved_freshness_notice`
      裡做（它已經有「這個待辦用了 web 就跳過」的邏輯）。兩邊都做會在未來漂移。
    """
    if need not in ("intraday", "days"):
        return []
    ticker = next(iter(sorted(_mentioned_tickers(task))), "")
    ceiling = _kb_ceiling_date(chunks) if chunks else None
    return [{"ticker": ticker, "cutoff": ceiling.isoformat() if ceiling else "",
             "as_of": as_of.isoformat(), "doc_type": "realtime", "need": need}]


def _unfulfilled_web_route_gaps(route: str, web_notes: list[str], chunks: list[dict],
                                as_of: date) -> list[dict]:
    """**被路由到 web 的待辦卻一次 web 都沒拿到** → 記一筆時效缺口。確定性，零 LLM。

    ⚠ **刻意不看 `realtime_need`**（`_unmet_realtime_gaps` 走的是那條路）。那個欄位是
      Grader 的三分類 LLM 輸出，BACKLOG 記著它在一個措辭族上實測 0/3；而且它要 Grader
      跑過才有值——`route=web` ＋ 預算用完時我們根本沒跑到 Grader，那條路必然沉默。
      這裡的判準是**結構性**的：Planner 說這題要上網，而網路一次都沒查到。零判斷成分。

    ⚠ 只在 live 才會被呼叫（見 `_pkg._run_one_todo`）。snapshot 下 `_effective_route` 已把
      route 降級成 kb，這裡也不會有陽性——**eval 基準因此一格不動**（閘門⑪z5）。
    """
    if route not in ("web", "both") or web_notes:
        return []
    ceiling = _kb_ceiling_date(chunks) if chunks else None
    return [{"ticker": "", "cutoff": ceiling.isoformat() if ceiling else "",
             "as_of": as_of.isoformat(), "doc_type": "realtime", "need": "days"}]


# ── 口徑揭露 validator（確定性，零 LLM）─────────────────────────────────────────
# 治的病（2026-08-20 實測 lex-17）：使用者問「營收成長率」沒指定口徑 → 答案給 10-K 的財年 18%，
# 而 gold 是 Fundamentals 的 TTM 18.30%。**數字不是假的，錯的是口徑，而且沒有任何揭露**——
# 兩個值差 0.3pt，讀者無從分辨自己拿到的是哪一種。
#
# ⚠ **刻意做成 validator 而不是 prompt 指令**。理由是本檔已經寫過一次的教訓：
#   「Prompt 是機率性約束；snapshot / --no-web 再用 Python 硬擋」。而這裡要判的兩件事
#   **都是確定性的**——「答案引了哪些 chunk」是 regex，「那些 chunk 是什麼口徑」是 payload
#   的 `period_basis` 欄位（實測全庫只有兩個值：fundamentals=TTM 22 筆／其餘 fiscal_year 3805 筆）。
#   照 CLAUDE.md〈LLM 與 Python 的分工〉，這一半不該交給 LLM 去記得。
#
# ⚠ 措辭刻意**只陳述事實、不宣稱原因**。Gemini 版的建議是「由於缺乏最新 TTM 數據」，
#   但那個因果**可能是假的**：KB 裡可能有 TTM chunk，只是這次沒被引用。斷言一個查不到的原因
#   就是在製造新的不可信內容——同 R4 選「並陳」不選「裁決」的理由。
_BASIS_NOTICE_MARK = "⚠ 口徑說明："
# 「問題自己講明了絕對期別」＝ 使用者要的就是財報期間，這時講 TTM 是雜訊（話太多方向）。
# 只認**格式化的字面訊號**（yyyymm 期碼、年份＋財年字樣），不做語意判斷。
_EXPLICIT_FY_RE = re.compile(r"(?:19|20)\d{2}\s*(?:財年|财年|會計年度|会计年度|年度)"
                             r"|(?:fiscal\s*year|FY)\s*(?:19|20)?\d{2}", re.IGNORECASE)


# Fundamentals chunk 裡「欄位名 : 12.34%」的取值。⚠ 這不是感知，是**解析本專案自己 ingest
# 產生的固定格式**（見 data/edgar_processed/Fundamentals/*.txt），屬於格式定義的封閉集合。
_FUND_PCT_TMPL = r"{field}\s*(?:\([^)]*\))?\s*[:：]\s*(-?\d+(?:\.\d+)?)\s*%"


def _ttm_field_values(cited_chunks: list[dict], fields: list[str]) -> dict[str, str]:
    """引用到的 TTM chunk 裡，被問欄位各自的值（`{"Revenue Growth": "18.30"}`）。認不出就不放。"""
    out: dict[str, str] = {}
    for c in cited_chunks or []:
        if (c.get("period_basis") or "") != "TTM":
            continue
        txt = c.get("content") or ""
        for f in fields:
            m = re.search(_FUND_PCT_TMPL.format(field=re.escape(f)), txt, re.IGNORECASE)
            if m:
                out.setdefault(f, m.group(1))
    return out


def _value_stated(answer: str, val: str) -> bool:
    """答案裡有沒有真的講出這個值。`18.30` 與 `18.3` 視為同一個；`118.3` 不算（前後要有邊界）。"""
    trimmed = val.rstrip("0").rstrip(".") if "." in val else val
    return re.search(rf"(?<![\d.]){re.escape(trimmed)}0*(?![\d])", answer or "") is not None


def _basis_disclosure_notice(task: str, cited_chunks: list[dict], answer: str = "",
                             ratio_fields: list[str] | None = None) -> str:
    """答案只引到財報期間口徑的數字、卻是在回答一個沒指定口徑的比率題 → 回傳揭露警語。

    沉默條件（全部是「話太多」方向的誤報對照——這道護欄的失敗方向不是漏印，是變成背景噪音）：
      ① 不是比率／成長率題（市值、EPS 這類單一來源指標沒有口徑歧義）
      ② 問題自己指定了絕對期別（`2025 財年`、`FY2026`、yyyymm 期碼）——那時財報口徑正是要的
      ③ **答案裡真的講出了那個 TTM 值**

    ⚠ ③ 原本寫的是「引用裡有 TTM chunk」，**那是錯的，而且是被自己要抓的行為解除武裝**
      （2026-08-21 實測，lex-17）：補撈修好之後，答案確實引到 `MSFT_Fundamentals #0`，
      眼前就是 `Revenue Growth (YoY): 18.30%`，它卻寫成
      「全年與最近的 **TTM** 都在約 **18%** 左右【…chunk #0】」——**把 TTM 四捨五入成 18%，
      再與 10-K 的財年 18% 併成同一個說法**。引用是真的、數字看起來也對，兩個口徑就這樣消失了。
      而舊條件③ 看到「有 TTM chunk 被引用」就沉默 → **護欄正好在該叫的那一刻關掉**。
      → 判準改成看**值有沒有出現在答案裡**（確定性字串比對，見 `_value_stated`）。

    ⚠ 有值的時候警語就**把值講出來**，不是只講「這不是 TTM」：值逐字取自**答案自己引用的
      那個 chunk**，所以仍然可追溯；這是 R4 那條「要求並陳不裁決」的同一個做法。
    """
    if not _has_ratio_intent(ratio_fields, task):
        return ""
    if _EXPLICIT_FY_RE.search(task or "") or rq._PERIOD_CODE_RE.search(task or ""):
        return ""

    fields = _resolve_ratio_fields(ratio_fields, task)
    ttm_vals = _ttm_field_values(cited_chunks, fields)
    if ttm_vals:
        missing = {f: v for f, v in ttm_vals.items() if not _value_stated(answer, v)}
        if not missing:
            return ""                  # 答案真的給了 TTM 值 → 不需要這段
        detail = "、".join(f"{f} {v}%" for f, v in sorted(missing.items()))
        return (chr(10) + chr(10) + "---" + chr(10) + _BASIS_NOTICE_MARK
                + f"上文引用的 TTM（最近十二個月）口徑數值為 **{detail}**，"
                  "與文中的財報期間（財年／單季）數字不是同一個口徑——"
                  "兩者數值可能接近但不可互換。")

    basis = {(c.get("period_basis") or "") for c in (cited_chunks or [])}
    if "TTM" in basis:
        return ""                      # 引到 TTM chunk 但認不出欄位值 → 維持沉默，不在看不懂時多話
    if "fiscal_year" not in basis:
        return ""                      # 沒引到任何財報期間 chunk（例如純 web 答案）→ 不是這條的守備範圍
    return (chr(10) + chr(10) + "---" + chr(10) + _BASIS_NOTICE_MARK
            + "以上比率／成長率取自財報期間口徑（財年或單季），"
              "**不是最近十二個月（TTM）**。同一指標的兩種口徑數值可能接近但不可互換。")


def _format_unresolved_freshness_notice(todos: list[dict]) -> str:
    """只對 live 且 web 沒成功補到的新聞缺口產生機械式時效聲明；snapshot 永遠沒有 gap。

    ⚠ 缺口是**逐待辦**算的，但這段警語**整篇答案只印一次**——所以措辭不能講成整篇的結論。
      2026-08-14 實測：「Azure 最新一季成長率」的答案主體引用了 CNBC 與 sec.gov 兩個 web 來源，
      底下卻印出「Web 未提供可用補充」，因為另外幾個沒查網的待辦各自帶著 gap。警語與答案互相矛盾
      比沒有警語更糟（它會讓讀者不信任明明有出處的數字），故依「這一次跑分到底有沒有用到 web」分岔。"""
    unique: dict[tuple[str, str, str], dict] = {}
    any_web = False
    for todo in todos or []:
        if todo.get("status") != "done":
            continue
        if todo.get("web_used"):
            any_web = True
            continue
        for gap in todo.get("freshness_gaps", []) or []:
            # doc_type 入 key：news 缺口與 realtime 缺口措辭不同，混在一起會互相蓋掉
            key = (gap.get("ticker", ""), gap.get("cutoff", ""),
                   gap.get("as_of", ""), gap.get("doc_type", "news"))
            unique[key] = gap
    if not unique:
        return ""
    def _phrase(ticker: str, cutoff: str, as_of: str, doc_type: str) -> str:
        who = ticker or "本次查詢"
        if doc_type == "realtime":
            # KB 只有財報 → 要講「知識庫本來就沒有這種資料」，不是「資料有點舊」
            span = f"，知識庫最新期別截至 {cutoff}" if cutoff else "，知識庫僅含 SEC 財報與基本面"
            return f"{who} 需要即時／近期資料{span}（查詢日 {as_of}）"
        return f"{who} 新聞資料截至 {cutoff}（查詢日 {as_of}）"

    details = "; ".join(_phrase(*k) for k in sorted(unique))
    tail = ("；本次有部分子問題未經網路補充，**未標註 [web:] 出處的內容**不代表涵蓋至查詢日。"
            if any_web else
            "；Web 未提供可用補充，因此以上內容不代表涵蓋至查詢日。")
    return "\n\n---\n⚠ 資料時效：" + details + tail


# ──────────────────────────────────────────────────────────────────────────────
# 純函式工具（片段截取 / chunk id / 池合併 / JSON 容錯）
# ──────────────────────────────────────────────────────────────────────────────
