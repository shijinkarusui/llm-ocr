"""W2: interactive.py为shim (解环): 转调cli.main. --tui内联薄问答住cli侧."""
from __future__ import annotations

try:
    from .cli import main
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from cli import main  # type: ignore


if __name__ == "__main__":
    raise SystemExit(main())
