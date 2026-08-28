"""Tavily 原始回應的錄製／重放（live 路徑的 eval fixture）。

**要解的問題**：live web 沒辦法定 gold——資料每天都在變。但那不代表整條 web 管線就該
處於無法自動化測試的狀態。解法是把不確定性切開：

  · **確定性的部分**（白名單、去重、單域名上限、日期算術、預算、`kb_unfixable`）
    已經由 [`eval/verify_web_gate_isolation.py`](eval/verify_web_gate_isolation.py)
    六道閘門 79 項斷言涵蓋——**零 LLM、零網路、零 Qdrant、秒級**。這個模組不重測那些。
  · **含 LLM 的部分**（Generator 會不會真的引用 web 數字、時效警語會不會跟答案矛盾、
    web 值與 KB 舊值衝突時有沒有並陳）現在**完全沒有被測過**，因為 eval 全程 snapshot、
    web 恆關。要測就必須真的跑 LLM，而那需要輸入是固定的 → 這個模組。

把 Tavily 回應錄下來 ＋ 用既有的 `AGENTIC_AS_OF_DATE` 把「今天」鎖死，那一題的外部
世界就變成靜態的，於是可以寫斷言。

**⚠ 錄在哪一層是這件事唯一會做壞的地方**：必須錄 `TavilyClient.search()` 的**原始回應**，
不能錄 `_tavily_search()` 的回傳字串。後者是已經跑完 `_host_allowed` 複核 → `_normalize_url`
去重 → 單域名上限 → `_url_published_date` 抽日期 → 過時過濾 → 摘要截斷之後的成品；錄在
那裡等於把要測的六道處理一起 mock 掉。同 `llm_replay` 刻意 key 在函式輸入輸出的理由。

**key 含請求參數**（`max_results` / `search_depth` / `include_domains`）：改了白名單或撈取
筆數，真實 Tavily 的回應本來就會不同 → 那時 miss 是**正確行為**，代表這份 fixture 沒涵蓋
你的新組態，而不是可以將就用。

**驗收不用聚合指標**：這條路的 n 只有個位數題，而本專案 RAGAS 的 MDE 在 n=100 是 0.067、
n=10 換算約 0.17~0.21——任何分數都落在噪音裡、解讀不了。所以配套的是**逐條確定性斷言**
（同 [`eval/check_number_defects.py`](eval/check_number_defects.py) 的模式）。

用法：
    # 錄（會連網、會燒 Tavily 額度）
    RAG_WEB_REPLAY=eval/web_fixture.json RAG_WEB_REPLAY_MODE=record \\
    AGENTIC_AS_OF_DATE=2026-08-15 python ...

    # 放（絕不連網；miss 直接報錯）
    RAG_WEB_REPLAY=eval/web_fixture.json AGENTIC_AS_OF_DATE=2026-08-15 python ...

未設 `RAG_WEB_REPLAY` 時整個模組是 no-op，生產路徑完全不受影響。
"""
from __future__ import annotations

import atexit
import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
_CACHE: Optional[dict] = None
_DIRTY = False
_PATH: Optional[Path] = None
_STATS = {"hit": 0, "miss": 0, "recorded": 0}

_MISS = object()
MISS = _MISS


# ⚠ **繼承 `BaseException` 而不是 `Exception`，這是刻意的**（2026-08-28）。
#
# 舊版繼承 `RuntimeError`，靠的是一條契約：「呼叫端必須先 `except FixtureMiss: raise`
# 再接通用處理」。`_tavily_search` 確實照做了，**但那不夠**——它 re-raise 之後，上一層
# `_run_executor_deterministic` 的 `except Exception` 又把它接走，印一行「例外 → 降級」
# 就繼續跑，結果檔還不記 error。於是 `web-02` 的 fixture miss 被量尺報成「系統沒打 web」，
# 而那兩件事在外觀上完全無法區分（2026-08-28 實測，見 docs/EVAL.md §4.5）。
#
# 全碼庫有 16 個 `except Exception`，逐個加 re-raise 是 O(n) 而且下次新增一個就破功。
# 移出 `Exception` 階層是**結構性**的：`except Exception` 在語言層面就抓不到。
# 判準與 `SystemExit`／`KeyboardInterrupt` 同一條——「這不是可以就地處理的錯誤，
# 這是『這次測量無效，停下來』」。
# 由 `eval/verify_web_gate_isolation.py` 閘門⑦ 把關（含誤報對照：一般例外仍要被降級接住）。


class FixtureMiss(BaseException):
    """replay 模式下 fixture 沒涵蓋這個請求。

    整輪 replay 實驗在無 fixture 下跑完、事後分不出來，是這條路最貴的失敗模式：
    它不會報錯、只會讓每一格斷言都變成「系統沒打 web」。"""


class RecordError(BaseException):
    """錄製時寫不進 fixture。同 `FixtureMiss` 的理由——被吞掉的話，會得到一份
    **安靜地少了幾筆**的 fixture，而少的那幾筆正是下次 replay 會 miss 的那幾筆。"""


def enabled() -> bool:
    return bool(os.getenv("RAG_WEB_REPLAY", ""))


def recording() -> bool:
    """record 模式才允許連網。**預設是 replay**——反過來會讓「fixture 沒涵蓋到」
    靜默退化成真的連網，那正是 eval 最不能發生的事（結果不可重現且沒人知道）。"""
    return os.getenv("RAG_WEB_REPLAY_MODE", "replay").strip().lower() == "record"


def make_key(query: str, max_results: int, search_depth: str, include_domains) -> str:
    """請求簽章。domains 排序後入 key：清單順序不該造成 miss，內容變了才該。"""
    return json.dumps({
        "q": (query or "").strip(),
        "n": int(max_results),
        "depth": str(search_depth),
        "domains": sorted(str(d) for d in (include_domains or [])),
    }, ensure_ascii=False, sort_keys=True)


def _load() -> dict:
    global _CACHE, _PATH
    if _CACHE is not None:
        return _CACHE
    _PATH = Path(os.environ["RAG_WEB_REPLAY"])
    if _PATH.exists():
        try:
            _CACHE = json.loads(_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            # 靜默略過＝整輪實驗在無 fixture 下跑完、事後分不出來（同 llm_replay 的理由）
            raise RuntimeError(f"web fixture {_PATH} 讀取失敗：{e!r}") from e
    else:
        _CACHE = {"_meta": {}, "responses": {}}
    _CACHE.setdefault("responses", {})
    return _CACHE


def get(key: str) -> Any:
    """命中回原始 Tavily 回應 dict；未命中回 MISS。
    replay 模式下未命中會**報錯**而不是回 MISS——呼叫端若在 replay 模式還走到連網分支，
    那就是靜默破壞可重現性。"""
    if not enabled():
        return _MISS
    with _LOCK:
        c = _load()["responses"]
        if key in c:
            _STATS["hit"] += 1
            # ⚠ **必須深拷貝**（同 put 的理由，但後果更嚴重）：下游 `_dedupe_web_results`
            #   就地寫 `r["_pub_date"]`。回傳參照的話，同一個 key 重放第二次時日期已經
            #   被上一次塞好了 → `_url_published_date` 那段**根本不會執行**，等於兩次
            #   重放走的是不同程式路徑，fixture 就不再是「固定輸入」了。
            return copy.deepcopy(c[key])
    _STATS["miss"] += 1
    if not recording():
        raise FixtureMiss(
            f"web fixture miss（replay 模式，絕不連網）：{key}\n"
            f"  這份 fixture 沒涵蓋這個請求。要嘛用 RAG_WEB_REPLAY_MODE=record 補錄，"
            f"要嘛檢查是不是改了 WEB_ALLOWED_DOMAINS／TAVILY_FETCH_RESULTS（那會合法地換 key）。")
    return _MISS


def put(key: str, value: Any) -> None:
    """存一筆原始回應。

    ⚠ **必須深拷貝**：我們刻意錄在最外層，而下游 `_dedupe_web_results` 會**就地改寫**
      同一批 dict（`r["_pub_date"] = d`，`agentic_rag_v2.py:584`）。存參照的話快取會被塞進
      `date` 物件 → 落盤時 `TypeError: Object of type date is not JSON serializable`。
      2026-08-15 首次真實錄製就這樣掉了第 8 筆。「錄在最外層」與「下游會改寫」是一組的，
      不能只做前者。
    ⚠ **序列化失敗當場炸**：原本這個錯誤發生在 `atexit`，那時整輪已經跑完、錢也燒完了，
      而且 atexit 的例外只會印一行 traceback 不影響 exit code ＝ 看起來成功、實則缺資料。"""
    global _DIRTY
    if not enabled():
        return
    safe = copy.deepcopy(value)
    try:
        json.dumps(safe, ensure_ascii=False)
    except TypeError as e:
        raise RecordError(f"這筆回應無法序列化成 JSON（{e}）：{key}") from e
    with _LOCK:
        _load()["responses"][key] = safe
        _STATS["recorded"] += 1
        _DIRTY = True
    _flush()      # 錄製是要花錢連網的，每筆立刻落盤，別讓中途中斷白燒


def note_meta(**kv) -> None:
    """把「這份 fixture 錄在什麼條件下」寫進檔案。
    as-of 日期與 collection 都會影響 `kb_unfixable` 的判斷（它要拿 KB 天花板跟今天比），
    所以 fixture 不是只有 web 回應——它綁定一組環境。"""
    if not enabled():
        return
    global _DIRTY
    with _LOCK:
        _load().setdefault("_meta", {}).update({k: v for k, v in kv.items() if v is not None})
        _DIRTY = True


def stats() -> dict:
    return dict(_STATS)


@atexit.register
def _flush() -> None:
    if not (_DIRTY and _CACHE is not None and _PATH is not None):
        return
    with _LOCK:
        try:
            payload = json.dumps(_CACHE, ensure_ascii=False, indent=1)
        except TypeError as e:
            # put() 存的時候已經驗過可序列化，所以走到這裡只有一個可能：快取在存進去
            # 之後被就地改寫了（get/put 少了 deepcopy）。atexit 的 traceback 不影響
            # exit code＝看起來成功實則缺資料，所以這裡要印得像個失敗。
            print(f"[web_replay] ✗ 落盤失敗：{e}\n"
                  f"  快取在存入後被就地改寫——檢查 get()/put() 的 deepcopy。"
                  f"  這一輪的新錄製沒有寫進 {_PATH}。")
            return
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(_PATH)        # 原子替換
