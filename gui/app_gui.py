"""Shim: delegate to the single root entry (keeps one code path for dev + EXE)."""
from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _meipass = getattr(sys, "_MEIPASS", None)
    if _meipass and str(_meipass) not in sys.path:
        sys.path.insert(0, str(_meipass))
    _root = Path(sys.executable).resolve().parent
else:
    _root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from app_gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
