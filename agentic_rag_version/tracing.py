"""agentic_rag_version.tracing — [TRACE] 輸出。

拆成獨立模組只有一個理由：**每一個子模組都要用它**，而它自己零相依。
留在 `__init__` 的話，子模組要嘛循環 import、要嘛各自複製一份。
"""
from __future__ import annotations

import os


# [TRACE]：把每個節點的當下決策印到 stderr（retrieve 的 DEBUG 已被 _quiet() 靜音）。
_TRACE = os.getenv("AGENTIC_TRACE", "false").lower() in ("true", "1", "yes")


def _trace(msg: str) -> None:
    if _TRACE:
        import sys
        print(f"[TRACE] {msg}", file=sys.stderr, flush=True)
