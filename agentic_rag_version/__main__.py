"""`python -m agentic_rag_version -q "..."` 的入口。

⚠ **為什麼需要這個檔**：套件化之前，`agentic_rag_v2.py` 檔尾的
`if __name__ == "__main__": main()` 就是 CLI 入口；搬成 `__init__.py` 之後那個判斷式
**永遠不會成立**（套件被 import 時 `__name__` 是套件名，不是 `"__main__"`），
CLI 會靜默地什麼都不做。這個檔把入口補回來。
"""
from __future__ import annotations

from . import main

if __name__ == "__main__":
    main()
