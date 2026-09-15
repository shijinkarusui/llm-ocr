"""Regression tests for _geometric_gate unit handling (0-1000 normalized coords).

actual_items from _actual_read_order are already 0-1000 normalized, as are
expected boxes. A /10.0 scale on the actual y values shrank them to 0-100,
so the positional pre-filter excluded everything and the gate vacuously
passed (checked=0, pass_ratio=1.0).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from src.verify_searchable import _geometric_gate
except ImportError:
    from verify_searchable import _geometric_gate  # type: ignore

try:
    from src.postprocess import clean_md
except ImportError:
    from postprocess import clean_md  # type: ignore


def test_geometric_gate_aligned_line_passes():
    expected = [{"text": "塞音测试", "box": [0, 400, 1000, 500]}]  # ey = 450
    actual = [{"text": "塞音测试", "x0": 0, "y0": 440, "x1": 1000, "y1": 460}]
    res = _geometric_gate(actual, expected)
    assert res["checked"] == 1
    assert res["passed"] == 1


def test_geometric_gate_drifted_line_fails():
    expected = [{"text": "塞音测试", "box": [0, 400, 1000, 500]}]  # ey = 450
    # ay center = 710, dev = 260. tol=200 keeps the item inside the
    # 2*tol pre-filter window (450 +- 400) while dev=260 > tol fails it,
    # so the drift is actually checked instead of silently skipped.
    actual = [{"text": "塞音测试", "x0": 0, "y0": 700, "x1": 1000, "y1": 720}]
    res = _geometric_gate(actual, expected, tol=200)
    assert res["checked"] == 1
    assert res["passed"] == 0


def test_clean_md_keeps_english_decimal():
    assert "3.14" in clean_md("value is 3.14 ok")


def test_clean_md_converts_comma_next_to_cjk():
    assert clean_md("中文，test,中文") == "中文，test，中文"
