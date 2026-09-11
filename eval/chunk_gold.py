"""**chunk 層 gold**：定義、解析器、漂移偵測、自測（零 LLM、零網路）。

## 為什麼需要這一支

`eval_set.json` 的 `relevant` **只到檔名**，而 `BACKLOG.md`〈量尺缺口〉有三條線同時卡在
「沒有 chunk 粒度的 gold」上：

1. **lexical 的承接數只有 1.93**（全類最低），而檔案層命中 12/15 **與單發完全相同**
   → 病灶是「同一個檔裡收太少／收錯 chunk」，而檔名層的尺看不到這件事。
2. **`probe_relevant_ids` 只判得出 `all_gold_dropped`（檔層）**。Grader 平均只圈選 0.53 的
   候選，但「它濾掉的是離題 chunk（正確）還是答案所在的 chunk（危險）」在檔名層**結構上
   分不出來**——同一個檔裡兩者都有。
3. **Grader 責任剝離**的復活條件之一就是上面那一格量得到損害。

## gold 怎麼定義：`literal`，不是 `chunk_index`

**gold ＝「參考答案裡那個數字，落在這個 collection 的哪幾顆 chunk 上」**，由 `literal`
確定性定位。**不寫死 `chunk_index`**——那是 `eval_set.json` 的 `pin_chunks` 已經示範過的
**兩種漂移**：

- **索引漂移**：重建不逐字重現舊 collection（見 CLAUDE.md〈重建 collection〉）→ 指標位移。
- **檔案漂移**（2026-09-04 實測到的那一種）：`relevant` 於 `10a0caa`／`53d4baf` 更新到新一季，
  `pin_chunks`（寫於 `3417ad0`）沒跟著改 → **8 題的 pin 一顆都比對不到**
  （sem-04／mix-04／mix-05／mix-08／mix-09／col-03／col-12／col-13）。

用 literal 的話，「gold 是哪幾顆」與前提檢查「這個事實在 KB 裡在不在」**是同一個操作**，
不會各自漂移。這個作法抄自 `probe_historical_benefit.py`（`period_probe_benefit_queries.json`
的 `_meta` 有同一段論證）。

## 消費語意：**union ＋ all-dropped**

一題的 gold 是所有 literal 命中 chunk 的**聯集**，危險判定是「**這些 chunk 全部被丟掉**」。
刻意這樣定，因為 union 偏大時判定只會**更難觸發**＝ false-negative 方向，那是安全的一邊。
（例：`lex-04` 的 `2006` 除了 CUDA 那一顆，還中了股權激勵計畫表格＝巧合。留著它只會讓
「全滅」更難成立，不會製造誤報。）

## literal 從哪裡來，以及它為什麼不與被測物耦合

候選是**從 `reference_answers.json` 的參考答案文字**抽出的數字，再用兩個門檻篩特異性：

    n_in_relevant <= 3     literal 在該題 gold 檔裡命中幾顆（要指得到「那一顆」）
    n_same_ticker <= 8     literal 在該公司全語料命中幾顆（濾掉常見 token）

⚠ **這兩個門檻自己就把日曆年份濾掉了**，所以本檔**沒有**「排除年份」的硬規則——證據是
唯一存活的年份型 literal 是 `2006`（`lex-04`／`sem-01` 的 CUDA 推出年），而**那真的是答案**。
加一條形狀規則反而會殺掉它。

⚠ **不耦合**：`reference_answers.json` 是 RAGAS 的 ground truth（含 24 處人工校正），
**不是 agentic 管線的輸出**——量尺讀的東西不會因為修 Grader 而改變。
⚠ **偏誤方向已知且安全**：參考答案是「dense 選 chunk ＋ pin」生成的，dense 漏掉的事實
不會出現在參考答案裡 → 本檔的 gold 只會**比真值少**，不會多。少 ＝ 判不出來，多 ＝ 誤報。

## 逐題人工驗證

`literals[].verified` 逐條記載**怎麼驗的**（同 `number_claims.json` 的紀律：人工驗證過才登錄）。
判準一句話：**這個數字在命中 chunk 裡扮演的角色，就是題目問的那個量**。
被剔掉的形狀有三種，各有實例寫在 `_meta.rejected`：脈絡值（`lex-10` 的 `3,499` 是投資活動
現金流不是資本支出）、同表鄰居（`col-10` 的 `78.23` 總現金 vs 題目問的自由現金流）、
純巧合（`mix-13` 的 `3.5` 是市值里程碑表格不是儲能營收）。

用法：
    # 自測（零 Qdrant，毫秒級）
    .venv/Scripts/python.exe eval/chunk_gold.py --selftest
    # 對現行 collection 解析並比對凍結快照（偵測語料漂移）
    RAG_COLLECTION=us_stock_rag_edgar_multiyear .venv/Scripts/python.exe eval/chunk_gold.py --check
    # 重建凍結快照（換 collection／重建語料後才做，會改動版控檔）
    .venv/Scripts/python.exe eval/chunk_gold.py --rebuild-snapshot
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
# ⚠ Windows 主控台預設 cp950，而本檔的報表帶著 ⚠／✔／① 等字元 ⇒ **印到一半就 crash**，
#   而 crash 的退出碼與「有 FAIL」外觀相同 ＝ 把量尺自己的失敗讀成系統的失敗。
#   2026-09-11 普查：eval/ 的 51 支裡有 27 支帶著這個地雷，其中兩支當天真的踩了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

GOLD_PATH = Path(__file__).parent / "chunk_gold.json"

_NUMERIC_LITERAL_RE = re.compile(r"^[0-9][0-9,.]*$")


def literal_matcher(lit: str):
    """把 `literal` 編成一個 `(text) -> bool`。

    數字型要用邊界比對，否則 `1,353` 會吃到 `21,353`、`3.19` 會吃到 `13.19` 與 `3.190`
    ——那會讓 gold 多出幾顆 chunk，而 union 變大會讓「全滅」判定失去判別力。
    片語型則是大小寫無關的子字串。

    ⚠ **這是全 repo 的唯一定義點**：`probe_historical_benefit.py` 原本自帶一份逐字相同的
    實作，2026-09-04 改成 import 這裡（它的雙向自測因此變成測共用實作＝嚴格更好）。
    """
    if _NUMERIC_LITERAL_RE.match(lit):
        # ⚠ **邊界要排除的是「數字的延續」，不是「標點」**（2026-09-11 修）。
        #   舊版是 `(?<![0-9,.])…(?![0-9,.])`，於是 `2006,` `4.90.` 這種
        #   **後面緊跟逗號或句點**的寫法一律匹配不到——而英文散文裡數字後面
        #   最常見的正是逗號與句點。實測 `lex-04` 的 gold 因此指到兩顆**附件索引**
        #   （`3/7/2006`，後面接空白所以匹配得到），而真正的答案
        #   `With our introduction of CUDA in 2006, we opened …` 匹配不到。
        #   ⇒ 三支尺（召回、層級歸因、漏斗）一起把一個答對的題目報成檢索缺陷。
        #   現在的判準：只有「前後真的接著數字」才算延續——
        #   `.`／`,` 只有在**它自己後面（或前面）也是數字**時才算數字的一部分。
        rx = re.compile(r"(?<!\d)(?<!\d[.,])" + re.escape(lit) + r"(?!\d)(?![.,]\d)")
        return lambda t: bool(rx.search(t or ""))
    low = lit.lower()
    return lambda t: low in (t or "").lower()


def load(path: Path | str = GOLD_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def by_id(gold: dict | None = None) -> dict:
    g = gold or load()
    return {q["id"]: q for q in g["queries"]}


def scan_docs(client, collection: str) -> list[dict]:
    """掃一次 collection。`rq.retrieve()` 的 chunk dict 沒有 document 全文，而定位需要它。"""
    out, off = [], None
    while True:
        pts, off = client.scroll(
            collection_name=collection, limit=1024, offset=off, with_vectors=False,
            with_payload=["document", "source", "chunk_index", "ticker"])
        for p in pts:
            pl = p.payload or {}
            out.append({"document": pl.get("document") or "",
                        "source": pl.get("source") or "",
                        "chunk_index": str(pl.get("chunk_index")),
                        "ticker": (pl.get("ticker") or "").upper()})
        if off is None:
            break
    return out


def resolve(docs: list[dict], entry: dict) -> dict:
    """把一題的 literal 解析成 chunk 集合。

    回 `{literal: [(source, chunk_index), …]}`。**限定同 ticker**（別家公司剛好有同一個
    數字是巧合，不是 gold），並限定在該題的 `relevant` 檔內。
    """
    import fnmatch
    pats = entry["relevant"]
    tks = set(entry["ticker_scope"])
    got: dict[str, list] = {}
    for spec in entry["literals"]:
        lit = spec["literal"]
        hit = literal_matcher(lit)
        got[lit] = sorted(
            (d["source"], d["chunk_index"]) for d in docs
            if d["ticker"] in tks
            and any(fnmatch.fnmatch(d["source"], p) for p in pats)
            and hit(d["document"]))
    return got


def gold_chunks(docs: list[dict], entry: dict) -> set:
    """一題的 gold ＝所有 literal 命中 chunk 的**聯集**（見檔頭〈消費語意〉）。"""
    return {c for hits in resolve(docs, entry).values() for c in hits}


def frozen_chunks(entry: dict) -> set:
    """凍結快照裡的 gold 集合。**定義仍然是 literal**，快照只用來偵測漂移。"""
    return {(c["source"], c["chunk_index"]) for c in entry.get("resolved_snapshot") or []}


# ---------------------------------------------------------------- 自測

_SELFTEST = [
    # (literal, text, 期望, 為什麼)
    ("1,353", "revenue of 21,353 million", False, "數字型不可被較長的數字包含"),
    ("1,353", "revenue of 1,353 million", True, "數字型正命中"),
    ("3.19", "ratio 13.19", False, "小數型不可被前綴吃掉"),
    ("3.19", "ratio 3.190", False, "小數型不可被後綴吃掉"),
    ("3.19", "ratio was 3.19%", True, "後接非數字字元仍命中"),
    ("128.3", "$128.3 billion in 2024 and 2025", True, "lex-10 的真實形狀"),
    ("128.3", "$1,128.3 billion", False, "千分位前綴要擋掉"),
    ("2006", "CUDA in 2006", True, "年份型 literal（lex-04 真的以年份為答案）"),
    ("2006", "in 12006 units", False, "數字緊鄰要擋掉"),
    # ⚠ **已知範圍，不是缺陷**：邊界只擋 `[0-9,.]`，所以斜線分隔的日期照樣命中。
    #    第一版把這一條寫成 False 而自測當場抓到——實作（沿用 probe_historical_benefit
    #    的原版）沒錯，是斷言寫錯了。收窄邊界會誤傷 `ratio was 3.19%` 那種正當命中，
    #    而 union 語意下多命中一顆只會讓判定更保守 → 維持現狀並在這裡記下來。
    ("2006", "on 12/2006/01", True, "斜線分隔的日期仍命中（已知範圍，見上方註解）"),
    # ── 2026-09-11：邊界改成「只擋數字延續」之後補的八條 ─────────────────────
    # ⚠ **上面那條 `("2006", "CUDA in 2006", True)` 是恆真的**：它把生產文字裡的
    #   **逗號拿掉了**，而壞掉的正是逗號那條路 ⇒ 那一格從來沒被測到。
    #   （本 repo 同一個形狀已經踩過五次：⑰i、⑱f、⑳e、⑭j、⑮q。）
    ("2006", "With our introduction of CUDA in 2006, we opened the parallel",
     True, "**生產的逐字形狀**：數字後面緊跟逗號——舊邊界在這裡回 False"),
    ("4.90", "Diluted EPS was 4.90.", True, "句末句點：舊邊界同樣回 False"),
    ("23.49", "diluted earnings per share of 23.49, up from", True, "逗號＋空白"),
    ("128.3", "capex of 128.3, and", True, "逗號（lex-10 的另一種寫法）"),
    # ⚠ **前方邊界那條路一開始也沒被測到**（變異 M3「只修後方」全綠）——上面四條
    #   的前綴全是空白／`$`，兩版邊界都放行 ⇒ 補這兩條真實形狀（SEC 的 HTML 轉純文字
    #   很常出現點引線，逗號緊貼也是表格轉出來的常態）：
    ("1,353", "Total revenue......1,353", True,
     "點引線：前一個字元是 `.` 但它前面不是數字 ⇒ 不是小數點"),
    ("4.90", "Diluted EPS,4.90", True,
     "逗號緊貼：前一個字元是 `,` 但它前面不是數字 ⇒ 不是千分位"),
    # 以下四條是**誤報對照**：放寬之後仍然必須擋住真正的「數字延續」
    ("353", "revenue of 1,353 million", False,
     "**誤報對照**：前面是 `,` 而 `,` 前面是數字 ⇒ 那是千分位，不是獨立的數"),
    ("1,353", "revenue of 1,353.27 million", False,
     "**誤報對照**：後面是 `.` 而 `.` 後面是數字 ⇒ 小數點，不是句點"),
    ("3.19", "ratio 3.19.5 weird", False, "**誤報對照**：`.` 後接數字仍算延續"),
    ("2006", "code 2006.4 revision", False, "**誤報對照**：同上，年份也不例外"),
    ("CUDA", "our cuda platform", True, "片語型大小寫無關"),
    ("CUDA", "our OpenCL platform", False, "片語型陰性"),
]


def _selftest() -> int:
    bad = 0
    print("① literal_matcher 雙向自測")
    for lit, text, want, why in _SELFTEST:
        got = literal_matcher(lit)(text)
        ok = got == want
        bad += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {why:34} {lit!r} in {text!r} -> {got}")

    print("\n② gold 檔自洽性")
    g = load()
    qs = g["queries"]
    ids = [q["id"] for q in qs]
    checks = [
        ("id 不重複", len(ids) == len(set(ids))),
        ("每題至少一個 literal", all(q["literals"] for q in qs)),
        ("每個 literal 都有 verified", all(s.get("verified") for q in qs for s in q["literals"])),
        ("每題都有 ticker_scope", all(q.get("ticker_scope") for q in qs)),
        ("每題都有凍結快照", all(q.get("resolved_snapshot") for q in qs)),
    ]
    for why, ok in checks:
        bad += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {why}")

    print("\n③ 與 eval_set.json 對齊（gold 的 relevant 必須逐字同源）")
    ev = {q["id"]: q for q in json.loads(
        (Path(__file__).parent / "eval_set.json").read_text(encoding="utf-8"))["queries"]}
    for q in qs:
        src = ev.get(q["id"])
        ok = src is not None and src["relevant"] == q["relevant"]
        bad += 0 if ok else 1
        if not ok:
            print(f"  [FAIL] {q['id']} 的 relevant 與 eval_set 不一致："
                  f"{q['relevant']} vs {(src or {}).get('relevant')}")
    print(f"  [{'PASS' if all(ev.get(q['id']) and ev[q['id']]['relevant'] == q['relevant'] for q in qs) else 'FAIL'}]"
          f" {len(qs)} 題全部對齊")

    print("\n④ 誤報對照：union ＋ all-dropped 的語意")
    #    少了這一條，一個「gold 只留第一顆」的實作也會通過 ①~③，而那會把
    #    「Grader 保留了 gold 的另一顆」誤報成危險。
    fake = {"id": "_t", "relevant": ["X_10K_2025.html"], "ticker_scope": ["X"],
            "literals": [{"literal": "128.3", "verified": "t"}, {"literal": "77.7", "verified": "t"}]}
    docs = [{"document": "capex were $77.7 billion, and $128.3 billion", "source": "X_10K_2025.html",
             "chunk_index": "5", "ticker": "X"},
            {"document": "restated $128.3 billion", "source": "X_10K_2025.html",
             "chunk_index": "9", "ticker": "X"},
            {"document": "$128.3 billion", "source": "Y_10K_2025.html", "chunk_index": "1", "ticker": "Y"}]
    got = gold_chunks(docs, fake)
    want = {("X_10K_2025.html", "5"), ("X_10K_2025.html", "9")}
    ok = got == want
    bad += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] 聯集含兩顆、且別家 ticker 的同值 chunk 不算 gold -> {sorted(got)}")

    print(f"\n{'PASS' if bad == 0 else 'FAIL'} — {bad} 項失敗")
    return 1 if bad else 0


# ---------------------------------------------------------------- 漂移偵測

def _check(collection: str | None) -> int:
    import rag_query as rq
    col = collection or rq.COLLECTION_NAME
    docs = scan_docs(rq.make_qdrant_client(), col)
    print(f"[INFO] collection={col} chunks={len(docs)}")
    g = load()
    frozen_col = (g["_meta"].get("resolved_against") or {}).get("collection")
    if frozen_col != col:
        print(f"[WARN] 凍結快照綁的是 {frozen_col}，現在跑的是 {col}——差異可能全來自換 collection")
    bad = 0
    for q in g["queries"]:
        live, froz = gold_chunks(docs, q), frozen_chunks(q)
        empty = [lit for lit, hits in resolve(docs, q).items() if not hits]
        if empty:
            bad += 1
            print(f"  [DRIFT] {q['id']} — literal 在現行語料裡查無：{empty}")
        elif live != froz:
            bad += 1
            print(f"  [DRIFT] {q['id']} — 命中集合變了 (+{sorted(live - froz)} / -{sorted(froz - live)})")
    print(f"\n{'PASS' if bad == 0 else 'FAIL'} — {len(g['queries'])} 題，{bad} 題漂移")
    return 1 if bad else 0


def _rebuild(collection: str | None) -> int:
    import rag_query as rq
    col = collection or rq.COLLECTION_NAME
    docs = scan_docs(rq.make_qdrant_client(), col)
    g = load()
    for q in g["queries"]:
        q["resolved_snapshot"] = [{"source": s, "chunk_index": i}
                                  for s, i in sorted(gold_chunks(docs, q))]
    g["_meta"]["resolved_against"] = {"collection": col, "n_chunks": len(docs)}
    # ⚠ **不可用 `write_text`**：Windows 上它會把 `\n` 轉成 `\r\n`，而本檔在版控裡是 LF
    #   ⇒ 每次重建都製造一份全檔的**假 diff**，把真正的 gold 變動埋進噪音裡。
    #   （2026-09-11 踩到：只改了 3 題，diff 卻是 1385/1334 行。）
    with io.open(GOLD_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(g, ensure_ascii=False, indent=1) + "\n")
    tot = sum(len(q["resolved_snapshot"]) for q in g["queries"])
    print(f"[OK] 重建快照：{len(g['queries'])} 題／{tot} 顆 gold chunk（collection={col}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true", help="零 Qdrant 的確定性自測")
    ap.add_argument("--check", action="store_true", help="對現行 collection 解析並比對凍結快照")
    ap.add_argument("--rebuild-snapshot", action="store_true", help="重寫凍結快照（會改動版控檔）")
    ap.add_argument("--collection", default=None)
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.rebuild_snapshot:
        return _rebuild(a.collection)
    if a.check:
        return _check(a.collection)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
