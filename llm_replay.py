"""檢索前 LLM 中間產物的重放快取（eval fixture）。

**為什麼需要**：本專案的 LLM 全部跑 `temperature=0`，但預設模型 `gpt-oss-120b` 是 MoE，
溫度 0 只固定取樣、不固定專家路由 → 同一個輸入重跑會得到不同輸出。2026-08-09 實測：

| 位置 | 同輸入重跑的不一致率 |
|---|---|
| `agentic_rag_v2._plan_subqueries` | **12/25 題（48%）子問題不同** |
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

用法：
    RAG_REPLAY_CACHE=eval/replay_cache.json python eval/run_agentic_on_evalset.py ...
    RAG_REPLAY_MODE=strict   # 額外要求：cache miss 直接報錯（確認 fixture 覆蓋完整）
未設 `RAG_REPLAY_CACHE` 時整個模組是 no-op，生產路徑完全不受影響。
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
_STATS = {"hit": 0, "miss": 0}
_PENDING = 0
_FLUSH_EVERY = int(os.getenv("RAG_REPLAY_FLUSH_EVERY", "20"))

_MISS = object()   # 與「快取裡存的就是 None」區分


def enabled() -> bool:
    return bool(os.getenv("RAG_REPLAY_CACHE", ""))


def _strict() -> bool:
    return os.getenv("RAG_REPLAY_MODE", "").lower() == "strict"


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
    if _strict():
        raise RuntimeError(f"replay cache miss（strict 模式）: kind={kind} key={key!r}。"
                           f"fixture 不完整,先在非 strict 模式跑一次補齊。")
    return _MISS


def put(kind: str, key: str, value: Any) -> None:
    global _DIRTY, _PENDING
    if not enabled():
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
