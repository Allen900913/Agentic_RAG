"""檢索前 LLM 中間產物的重放快取（eval fixture）。

**為什麼需要**：本專案的 LLM 全部跑 `temperature=0`，但預設模型 `gpt-oss-120b` 是 MoE，
溫度 0 只固定取樣、不固定專家路由 → 同一個輸入重跑會得到不同輸出。2026-08-09 實測：

| 位置 | 同輸入重跑的不一致率 |
|---|---|
| `agentic_rag_version._plan_subqueries` | **12/25 題（48%）子問題不同** |
| `rag_query.translate_query_to_english` | 由「同組態兩次跑 top-8 有 47/100 題不同」推得 |

後果是**任何 A/B 都不是受控實驗**：兩臂差異裡有一大半跟你改的東西無關。同組態連跑兩次
的實測基準（top-8 重複對 149 vs 151、47 題 top-8 變動）就是這個噪音的規模。

**這個模組買到什麼、買不到什麼**：
  · 買到：eval 的兩臂共用同一份 plan / 英譯 → 差異只剩下你真正改的那一項。
  · **買不到：生產環境仍然會抖**。MoE 路由不確定不是參數能解的，這裡也不打算解。
  · 代價：固定下來的是「某一次抽樣的結果」，不是「正確答案」。所以結論嚴格來說條件於
    這份 fixture——跟 `reference_answers.json` 是同一種東西：不需要唯一正確,只需要
    **固定且對所有組態一視同仁**。定版後就別再重生成。

**Key 刻意不含 system prompt**：`_plan_subqueries` 的 system prompt 內嵌
`_build_temporal_contract()` 的 KB Coverage Snapshot，而 coverage 是掃 collection 得來的
→ 換 collection 就換 prompt。若把 prompt 納入 key，跨 collection A/B 會全部 cache miss，
正好毀掉這個模組唯一的用途（見 memory `eval-gold-version-drift-bug` 記載的同一個坑：
換 collection 不只換檢索池,是換掉規劃器的輸入）。**副作用**：真的改了 planner prompt 時
快取不會自動失效,要手動刪檔重生成。

**一個 block 一個檔＝隨機區塊設計**：`RAG_REPLAY_CACHE` 是路徑，所以「A/B 共用 plan、
但跨重複次數重抽 plan」不需要改碼——block 1 兩臂共用 `replay_b1.json`、block 2 換
`replay_b2.json`。同一 block 內**必須先後跑**（並行會兩臂都 miss 各自寫入＝等於沒 block）。
單一 block（K=1）的結論條件於那一次抽樣，沒有自由度分辨「B 真的較好」與「B 在這個 plan
下剛好較好」。⚠ 擋得住的只有 plan／英譯；Grader 跨 collection 必然 miss（見 `check` 的
key），生成層 `GEN_TEMPERATURE=0.3` 與 RAGAS judge 也擋不住 → blocking 是必要條件不是
充分條件，block 內仍有殘餘噪音。
  · **成本**：2K 次跑 × 3~4 小時。便宜版＝block 2/3 **只重跑 block 1 裡兩臂結果不同的題**
    （其餘題兩臂一致,對差異貢獻 0）。
  · **fixture 帶先跑那臂的條件**：承上「key 不含 system prompt」,共用的 plan 嚴格說是
    「先跑那臂 coverage 下的 plan」。對「同批文件、只改切塊」的 A/B（如 period vs head,
    coverage 相同）是非議題;若 A/B 是「加了新文件的 collection」就有方向不明的偏誤,
    那時要用第三次中性 pass 生 fixture。

用法：
    RAG_REPLAY_CACHE=eval/replay_cache.json python eval/run_agentic_on_evalset.py ...
    RAG_REPLAY_MODE=strict                    # 所有 kind 的 cache miss 都直接報錯
    RAG_REPLAY_MODE=strict:plan,translate_en  # 只對這些 kind 嚴格（跨 collection A/B 用這個）
    RAG_REPLAY_READONLY=1                     # 只讀不寫（A/B 兩臂共用同一份 fixture 時必開）
未設 `RAG_REPLAY_CACHE` 時整個模組是 no-op，生產路徑完全不受影響。

**唯讀模式（`RAG_REPLAY_READONLY`）：A/B 共用一份 fixture 時的單向污染**

`atexit` 無條件回寫，於是**先跑那一臂的 miss 會變成後跑那一臂的 hit**。實測 hit 23→32，
兩臂因此不可比——而外觀上兩臂都「掛了 fixture」，看不出任何異常。這條污染是**單向**的：
它只讓後跑的那臂更穩定，所以永遠偏袒後跑的一方。

唯讀模式下 `put()` 是 no-op（`_DIRTY` 也不會被設，所以連誤呼叫 `_flush()` 都寫不出去），
miss 就讓它在兩臂**對稱地**變成噪音——那是誠實的噪音，不是偏袒誰的假穩定。

⚠ **刻意是 opt-in，預設逐字不變**：有三個既有流程依賴「跑一輪順便補快取」——
`eval/record_web_fixture.py --mode record`（它的產物就是快取本身）、
`eval/probe_historical_benefit.py` 與 `eval/probe_temporal_interference.py`
（兩支都會自動把 `RAG_REPLAY_CACHE` 指到 `eval/replay_cache.json` 並靠回寫補齊）。
預設改成唯讀會讓那三支靜默地不再累積 fixture。

⚠ **唯讀時 miss 必須照樣印出來**：非唯讀模式下唯一的 hit/miss 出口是 `_flush()` 那行
`[replay] wrote ...`，而唯讀不寫檔就不會有那行 → **miss 會完全隱形**，那正是這個模組
最不該有的失效形狀（fixture 沒涵蓋 vs 系統沒走那條路，外觀相同）。所以 `_flush()` 在
唯讀模式改印一行 `[replay] read-only`。
"""
from __future__ import annotations

import atexit
import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
_CACHE: Optional[dict] = None
_DIRTY = False
_PATH: Optional[Path] = None
_STATS = {"hit": 0, "miss": 0, "skipped_write": 0}
_PENDING = 0
_FLUSH_EVERY = int(os.getenv("RAG_REPLAY_FLUSH_EVERY", "20"))

_MISS = object()   # 與「快取裡存的就是 None」區分


def enabled() -> bool:
    return bool(os.getenv("RAG_REPLAY_CACHE", ""))


def readonly() -> bool:
    """只讀不寫（見 docstring〈唯讀模式〉）。**每次都重讀 env**，不快取成模組層常數——
    測試要能在同一個 process 裡切換，而快取成常數的版本連測都測不到。"""
    return os.getenv("RAG_REPLAY_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


# 所有呼叫端註冊的 kind＝我們自己的碼定義的**封閉集合**，所以列清單正當（同 VALID_*_ITEMS
# 的理由）。新增接點時要一起加進來——沒加會在 strict 名單裡被判成拼錯而報錯，那是刻意的。
_KNOWN_KINDS = {"plan", "translate_en", "check", "ratio", "period_intent", "ticker", "replan"}
# ⚠ 2026-08-28：`period_intent` 是 2026-08-19 加的接點，**當時漏了註冊**（`ticker` 一起補上）。
# 症狀是靜默的：bare `strict` 照樣涵蓋它（走 `{"*"}`），只有 `RAG_REPLAY_MODE=strict:period_intent`
# 會被當成拼錯而報錯——也就是說「想單獨對它嚴格」是唯一會現形的用法，而那正是最少人走的路。
# ⚠ 2026-08-29：`replan` 是**最後一個沒被錄的 LLM 呼叫**，而它是「web fixture key 無界」那條鏈
# 的源頭（replan 重抽 → 新 todo 措辭 → 新 check key → 新 new_query → 新英譯 → 新 Tavily key）。
# 漏掉它的症狀不是 miss 報錯，而是**整條下游的 fixture 一路 miss**，外觀跟「系統沒打 web」一樣。
# 這一格由 `eval/verify_web_gate_isolation.py` 閘門 ⑧ 把關（含「key 不得含自由文字結果」的值測試）。


class ReplayCacheMiss(BaseException):
    """strict 模式下 cache miss。

    ⚠ **繼承 `BaseException` 是刻意的**，理由與 `web_replay.FixtureMiss` 同一條：
    這個例外的意思是「這次測量無效」，不是「這個操作失敗了、換條路走」。舊版拋裸
    `RuntimeError`，會被下游任何一個 `except Exception` 接成優雅降級，於是 strict 模式
    **靜默失效**——而 strict 存在的唯一理由就是「fixture 不完整要當場知道」。"""


def _strict_kinds() -> Optional[set[str]]:
    """回傳 None＝非 strict；`{"*"}`＝全部 kind 嚴格；其餘＝只對指名的 kind 嚴格。

    **為什麼要 per-kind**：bare `strict` 在跨 collection A/B 下一定炸，而且是誤炸。`check`
    的 key 含「這次實際看到的候選 chunk id」，換 collection（切塊變了→id 變了）必然 miss，
    而那是**正確行為**：候選變了就是被測改動造成的差異，不該用舊決策蓋掉。所以想用 strict
    確認 fixture 覆蓋完整時，只能對「該被固定住」的中間產物嚴格：`strict:plan,translate_en`。
    """
    raw = os.getenv("RAG_REPLAY_MODE", "").strip().lower()
    if not raw.startswith("strict"):
        return None
    _, _, kinds = raw.partition(":")
    named = {k.strip() for k in kinds.split(",") if k.strip()}
    if not named:
        return {"*"}
    # 拼錯的 kind 名（strict:plna）會讓整個 strict 靜默失效＝整輪實驗在無防護下跑完、事後
    # 分不出來。這正是本專案反覆踩的那類坑，所以寧可啟動就炸。
    unknown = named - _KNOWN_KINDS
    if unknown:
        raise RuntimeError(
            f"RAG_REPLAY_MODE 指名了未知的 kind: {sorted(unknown)}；"
            f"可用的是 {sorted(_KNOWN_KINDS)}。（拼錯若不報錯,strict 會靜默失效）")
    return named


def _is_strict(kind: str) -> bool:
    ks = _strict_kinds()
    if ks is None:
        return False
    return "*" in ks or kind.lower() in ks


def _load() -> dict:
    global _CACHE, _PATH
    if _CACHE is not None:
        return _CACHE
    _PATH = Path(os.environ["RAG_REPLAY_CACHE"])
    if _PATH.exists():
        try:
            _CACHE = json.loads(_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"replay cache {_PATH} 讀取失敗：{e!r}（別靜默略過——"
                               f"靜默略過等於整輪實驗在無快取下跑，事後分不出來）") from e
    else:
        _CACHE = {}
    return _CACHE


def get(kind: str, key: str) -> Any:
    """命中回傳存值；未命中回傳 _MISS 哨兵（呼叫端用 `is MISS` 判斷）。"""
    if not enabled():
        return _MISS
    with _LOCK:
        c = _load().get(kind, {})
        if key in c:
            _STATS["hit"] += 1
            return c[key]
    _STATS["miss"] += 1
    if _is_strict(kind):
        raise ReplayCacheMiss(
            f"replay cache miss（strict 模式）: kind={kind} key={key!r}。"
            f"fixture 不完整,先在非 strict 模式跑一次補齊。"
            f"（若這是跨 collection A/B,kind=check 的 miss 是合法的——改用 "
            f"RAG_REPLAY_MODE=strict:plan,translate_en）")
    return _MISS


def put(kind: str, key: str, value: Any) -> None:
    global _DIRTY, _PENDING
    if not enabled():
        return
    if readonly():
        # ⚠ 在設 `_DIRTY` **之前**早退：這樣就算有人日後直接呼叫 `_flush()`，也寫不出去。
        #   把判斷放在 `_flush()` 裡是不夠的——那是「守在出口」，這裡守的是入口。
        _STATS["skipped_write"] += 1
        return
    with _LOCK:
        _load().setdefault(kind, {})[key] = value
        _DIRTY = True
        _PENDING += 1
        due = _PENDING >= _FLUSH_EVERY
    # 定期落盤：產生 fixture 的那一輪要跑滿 100 題（實測 ~1 小時），只靠 atexit 的話
    # 中途 Ctrl-C / 例外 / 機器睡著就整份 fixture 丟掉、得從頭再跑一次。寫的是一個
    # 小 JSON，成本可忽略。
    if due:
        _flush()


def stats() -> dict:
    return dict(_STATS)


@atexit.register
def _flush() -> None:
    global _PENDING
    if readonly():
        # 唯讀不寫檔 → 沒有 `[replay] wrote` 那行 → miss 會完全隱形。見 docstring。
        if enabled():
            print(f"[replay] read-only (hit={_STATS['hit']} miss={_STATS['miss']} "
                  f"skipped_write={_STATS['skipped_write']}) — 未回寫 {_PATH}")
        return
    # 只在有新內容時寫回；讀到一半就結束的 run 不會把檔案清空。
    if not (_DIRTY and _CACHE is not None and _PATH is not None):
        return
    with _LOCK:
        payload = json.dumps(_CACHE, ensure_ascii=False, indent=1)
        _PENDING = 0
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(_PATH)   # 原子替換：中途斷電也不會留下半份檔案
    print(f"[replay] wrote {_PATH} (hit={_STATS['hit']} miss={_STATS['miss']})")


MISS = _MISS
