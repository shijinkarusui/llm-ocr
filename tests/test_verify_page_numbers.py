import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from verify_searchable import _resolve_page_numbers


def test_explicit_page_numbers_win():
    report = {"pages": [{"page_index": 7}]}
    assert _resolve_page_numbers([3, 4], report, 2) == [3, 4]


def test_subset_pdf_maps_to_reported_source_page():
    # A one-page output built from source page 100 must be checked against page
    # 100. Defaulting to range(page_count) looked up page 0 instead, so the body
    # page was compared against the cover Markdown and scored 0.0.
    report = {"pages": [{"page_index": 100, "aligned_coverage": 1.0}]}
    assert _resolve_page_numbers(None, report, 1) == [100]


def test_multi_page_subset_keeps_report_order():
    report = {"pages": [{"page_index": 100}, {"page_index": 101}, {"page_index": 102}]}
    assert _resolve_page_numbers(None, report, 3) == [100, 101, 102]


def test_falls_back_to_positions_when_report_is_short():
    report = {"pages": [{"page_index": 5}]}
    assert _resolve_page_numbers(None, report, 3) == [0, 1, 2]


def test_empty_report_falls_back_to_positions():
    assert _resolve_page_numbers(None, {}, 2) == [0, 1]


def test_non_int_page_index_is_ignored():
    report = {"pages": [{"page_index": "x"}, {"page_index": 4}]}
    assert _resolve_page_numbers(None, report, 1) == [4]
