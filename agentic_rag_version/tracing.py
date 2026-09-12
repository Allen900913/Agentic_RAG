"""agentic_rag_version.tracing — [TRACE] 輸出。

拆成獨立模組只有一個理由：**每一個子模組都要用它**，而它自己零相依。
留在 `__init__` 的話，子模組要嘛循環 import、要嘛各自複製一份。
"""
from __future__ import annotations

import os


# [TRACE]：把每個節點的當下決策印到 stderr（retrieve 的 DEBUG 已被 _quiet() 靜音）。
_TRACE = os.getenv("AGENTIC_TRACE", "false").lower() in ("true", "1", "yes")


def set_trace_enabled(on: bool) -> None:
    """開關 `[TRACE]` 通道——**唯一的寫入點**（同 `_payload_to_chunk`／`_fiscal_sort_key` 的模式）。

    ⚠ **別的模組不可以寫 `global _TRACE; _TRACE = True`**：那重綁的是那個模組自己命名空間裡
      `from .tracing import _TRACE` **複製來**的值，而 `_trace()` 讀的是**本模組**的 `_TRACE`
      ⇒ 旗標靜默失效、一個字都不印，而「沒印」的外觀與「這個節點沒有 trace 可印」**完全相同**。
      `__init__.main()` 的 `--trace`／`--verbose` 就是這樣壞掉的（2026-09-12 查出）。
      諷刺的是 `AGENTIC_TRACE=true` 那條路一直是好的（env 在 import 時就讀完），於是兩次實際
      診斷都走 env、沒人發現旗標壞了——**能用的替代路徑會讓壞掉的那條活得更久**。

    ⚠ 反過來也不要讓 `_trace()` 去讀 `_pkg._TRACE`：那會讓本模組**反向依賴套件**，正好拆掉
      「它自己零相依」這個存在理由（見本檔開頭）。要開關就呼叫這支。

    守門在 `eval/verify_answer_validators.py` 閘門㉓（行為測試：撥了開關之後 `_trace()` 印不印）。
    ⚠ 閘門⑬／H2 **結構上攔不到這個 bug**：它們的名單從 eval 腳本反推，而沒有任何 eval
      monkeypatch `_TRACE` ⇒ 它永遠進不了那份名單。
    """
    global _TRACE
    _TRACE = bool(on)


def _trace(msg: str) -> None:
    if _TRACE:
        import sys
        print(f"[TRACE] {msg}", file=sys.stderr, flush=True)
