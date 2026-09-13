import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ink_layout
from ink_layout import (
    _bands_from_counts,
    _detect_numpy,
    _detect_pure,
    detect_line_boxes,
    detect_normalized_boxes,
)

PAGE_W = 595.0
PAGE_H = 842.0


def _page_with_bars(bars):
    """A page whose printed lines are solid black bars."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    for x0, y0, x1, y1 in bars:
        page.draw_rect(fitz.Rect(x0, y0, x1, y1), fill=(0, 0, 0))
    return doc, page


def _rows(n, *, x0=72.0, width=300.0, height=10.0, top=80.0, pitch=40.0):
    return [
        (x0, top + i * pitch, x0 + width, top + i * pitch + height)
        for i in range(n)
    ]


def test_bands_group_consecutive_positions():
    assert _bands_from_counts([0, 0, 5, 5, 5, 0, 0], 2, 0, 1, 100) == [(2, 4)]


def test_bands_merge_gap_within_limit():
    counts = [5, 5, 0, 5, 5]
    assert _bands_from_counts(counts, 2, 1, 1, 100) == [(0, 4)]
    assert _bands_from_counts(counts, 2, 0, 1, 100) == [(0, 1), (3, 4)]


def test_bands_drop_runs_shorter_than_min_len():
    assert _bands_from_counts([5, 0, 0, 0, 5, 5, 5], 2, 0, 3, 100) == [(4, 6)]


def test_bands_drop_runs_longer_than_max_len():
    assert _bands_from_counts([5, 5, 5, 5, 5], 2, 0, 1, 3) == []


def test_bands_close_trailing_run_at_last_index():
    assert _bands_from_counts([0, 5, 5, 5], 2, 0, 1, 100) == [(1, 3)]


def test_bands_ignore_counts_below_min_count():
    assert _bands_from_counts([1, 1, 1], 2, 0, 1, 100) == []


def test_detect_finds_every_printed_bar():
    doc, page = _page_with_bars(_rows(6))
    assert len(detect_line_boxes(page)) == 6
    doc.close()


def test_detect_orders_boxes_top_to_bottom():
    doc, page = _page_with_bars(_rows(4))
    ys = [box[1] for box in detect_line_boxes(page)]
    assert ys == sorted(ys)
    doc.close()


def test_detect_box_hugs_its_bar():
    bar = (72.0, 100.0, 372.0, 112.0)
    doc, page = _page_with_bars([bar])
    (x0, y0, x1, y1), = detect_line_boxes(page)
    assert abs(x0 - bar[0]) <= 2.0
    assert abs(x1 - bar[2]) <= 2.0
    assert abs(y0 - bar[1]) <= 2.0
    assert abs(y1 - bar[3]) <= 2.0
    doc.close()


def test_detect_ignores_bands_taller_than_ratio_cap():
    # Taller than MAX_BAND_HEIGHT_RATIO of the page: treated as a figure, not a line.
    doc, page = _page_with_bars([(72.0, 60.0, 372.0, 300.0)])
    assert detect_line_boxes(page) == []
    doc.close()


def test_detect_returns_empty_for_none_page():
    assert detect_line_boxes(None) == []


def test_detect_normalized_scales_into_0_1000():
    doc, page = _page_with_bars(_rows(3))
    norm = detect_normalized_boxes(page)
    assert len(norm) == 3
    for x0, y0, x1, y1 in norm:
        assert 0.0 <= x0 < x1 <= 1000.0
        assert 0.0 <= y0 < y1 <= 1000.0
    doc.close()


def test_pure_python_matches_numpy_path():
    # The frozen build may run without numpy, so both paths must agree.
    doc, page = _page_with_bars(_rows(5))
    want = _detect_numpy(page, ink_layout.DEFAULT_DPI, ink_layout.INK_THRESHOLD, False)
    got = _detect_pure(page, ink_layout.DEFAULT_DPI, ink_layout.INK_THRESHOLD)
    assert len(got) == len(want)
    for got_box, want_box in zip(got, want):
        for a, b in zip(got_box, want_box):
            assert abs(a - b) <= 1.0
    doc.close()


def test_detect_line_boxes_falls_back_without_numpy(monkeypatch):
    doc, page = _page_with_bars(_rows(3))
    expected = detect_line_boxes(page)
    assert expected
    monkeypatch.setattr(ink_layout, "_np", None)
    assert detect_line_boxes(page) == expected
    doc.close()
