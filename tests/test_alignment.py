"""Unit tests for the text-layer alignment implementation.

These tests avoid the full 303-page PDF and only use PyMuPDF for tiny
in-memory pages plus the new pure-Python helpers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from make_searchable import _font_for_char, _font_segments, _page_boxes, _usable_boxes, _fallback_only_lines, _metrics_from_lines, _partition_lines, make_searchable
from verify_searchable import _actual_read_order, _filter_exempt_actual, _table_cell_gate, garble_ratio, verify_alignment
from geom_align import align_page, normalize_md
from geom_extract import _is_header_footer, _is_page_like, extract_lines, extract_repeated_header_anchors, repeated_anchors_from_candidate_sets
from pilot_alignment import parse_pages


def test_boxes_schema_preserves_mode_for_page_boxes():
    boxes = {
        "0": {
            "reliable": True,
            "lines": [
                {
                    "text": "对齐文字",
                    "box": [10, 20, 500, 60],
                    "source": "md",
                    "mode": "aligned",
                    "line_id": "A0000",
                    "column": 0,
                    "cell_row": None,
                    "cell_col": None,
                    "subline_index": 0,
                    "order": 0,
                    "order_exempt": False,
                },
                {
                    "text": "兜底文字",
                    "box": [20, 900, 980, 940],
                    "source": "md",
                    "mode": "fallback_capacity",
                    "line_id": "F0000",
                    "column": 0,
                    "cell_row": None,
                    "cell_col": None,
                    "subline_index": 0,
                    "order": 1,
                    "order_exempt": True,
                },
            ],
        }
    }
    lines = _page_boxes(boxes, 0)
    assert lines is not None
    assert [ln["mode"] for ln in lines] == ["aligned", "fallback_capacity"]
    assert lines[0]["order_exempt"] is False
    assert lines[1]["order_exempt"] is True


def test_usable_boxes_accepts_single_aligned_line():
    line = {"text": "单行", "box": [10, 20, 500, 60], "mode": "aligned"}
    assert _usable_boxes([line], fitz.Rect(0, 0, 100, 100)) is True


def test_usable_boxes_accepts_header_footer_geometry():
    line = {"text": "· 12 ·", "box": [10, 20, 500, 60], "mode": "header_footer_excluded"}
    assert _usable_boxes([line], fitz.Rect(0, 0, 100, 100)) is True
    fallback = {"text": "兜底", "box": [10, 20, 500, 60], "mode": "fallback_capacity"}
    assert _usable_boxes([fallback], fitz.Rect(0, 0, 100, 100)) is False


def test_header_footer_requires_explicit_page_evidence():
    page_height = 1000.0
    # A short real chapter title at the top must NOT be treated as header/footer.
    title = {"bbox": [10, 5, 300, 40], "text": "第一章 语音学"}
    assert _is_header_footer(title, page_height) is False
    # Page-number/footer-like markers at the top or bottom SHOULD be detected.
    top_page = {"bbox": [10, 5, 200, 30], "text": "· 12 ·"}
    assert _is_header_footer(top_page, page_height) is True
    bottom_page = {"bbox": [10, 960, 200, 995], "text": "-12-"}
    assert _is_header_footer(bottom_page, page_height) is True
    plain_page = {"bbox": [10, 960, 200, 995], "text": "12"}
    assert _is_header_footer(plain_page, page_height) is True
    # A short body line in the middle is not header/footer even if it has a number.
    middle = {"bbox": [10, 500, 200, 530], "text": "12"}
    assert _is_header_footer(middle, page_height) is False


def test_is_page_like_only_accepts_marker_patterns():
    assert _is_page_like("· 12 ·") is True
    assert _is_page_like("-12-") is True
    assert _is_page_like("PAGE 12") is True
    assert _is_page_like("第 12 页") is True
    assert _is_page_like("12") is True
    assert _is_page_like("第一章 语音学") is False
    assert _is_page_like("标题") is False
    assert _is_page_like("") is False


def test_partition_lines_separates_header_footer_from_fallback():
    lines = [
        {"text": "正文", "mode": "aligned", "box": [10, 100, 500, 140]},
        {"text": "页脚", "mode": "header_footer_excluded", "box": [10, 10, 500, 40]},
        {"text": "兜底", "mode": "fallback_capacity", "box": [20, 900, 980, 940]},
    ]
    aligned, header_footer, fallback = _partition_lines(lines)
    assert [ln["text"] for ln in aligned] == ["正文"]
    assert [ln["text"] for ln in header_footer] == ["页脚"]
    assert [ln["text"] for ln in fallback] == ["兜底"]


def test_header_footer_excluded_written_at_geometry_box(tmp_path):
    """Header/footer-excluded lines use their real geometry box, not bottom fallback."""
    src = tmp_path / "src.pdf"
    doc = fitz.open()
    page = doc.new_page(width=200, height=300)
    doc.save(src)
    doc.close()

    md = {"0": "PAGE 12"}
    boxes = {
        "0": {
            "reliable": True,
            "lines": [
                {
                    "text": "PAGE 12",
                    "box": [10, 10, 500, 40],
                    "source": "md",
                    "mode": "header_footer_excluded",
                    "line_id": "H0000",
                    "column": 0,
                    "cell_row": None,
                    "cell_col": None,
                    "subline_index": 0,
                    "order": 0,
                    "order_exempt": True,
                }
            ],
        }
    }
    out = tmp_path / "out.pdf"
    make_searchable(src, [0], md, boxes, out, geo_source="external")

    with fitz.open(str(out)) as result:
        page = result.load_page(0)
        lines = []
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", []))
                if text.strip():
                    lines.append({"text": text, "bbox": line.get("bbox")})
    assert any("PAGE 12" in ln["text"] for ln in lines)
    hf_line = next(ln for ln in lines if "PAGE 12" in ln["text"])
    bbox = hf_line["bbox"]
    center_y = (bbox[1] + bbox[3]) / 2.0
    # 300pt page: the geometry box y=10/40 normalized to ~3pt/12pt, so the line
    # must be near the top, NOT in the bottom fallback band (~225+pt).
    assert center_y < 50.0


def test_normalize_md_removes_markup_and_keeps_table_cells():
    md = "# 标题\n\n**粗体** 和 `代码`\n\n| A | B |\n| --- | --- |\n| 甲 | 乙 |\n"
    norm = normalize_md(md)
    assert "标题" in norm
    assert "粗体" in norm and "**" not in norm
    assert "甲 乙" in norm or "甲" in norm


def test_align_page_zero_loss_with_fallback():
    md = "第一行文字\n第二行完全不同的文字"
    geom = {
        "reliable": True,
        "lines": [
            {"text": "第一行文字", "box": [10, 20, 500, 60], "column": 0,
             "header_footer": False, "cell_row": None, "cell_col": None}
        ],
    }
    lines, metrics = align_page(md, geom, page_index=0)
    all_text = "".join(ln["text"] for ln in lines)
    assert "第一行文字" in all_text
    assert "第二行完全不同的文字" in all_text
    assert metrics["covered_coverage"] == 1.0
    assert metrics["aligned_coverage"] >= 0.0


def test_fallback_only_lines_are_order_exempt_and_zero_loss():
    lines = _fallback_only_lines("甲\n乙丙", page_index=7)
    assert all(ln["mode"] == "fallback_capacity" for ln in lines)
    assert all(ln["order_exempt"] is True for ln in lines)
    assert "".join(ln["text"] for ln in lines) == "甲乙丙"


def test_mixed_font_segments_preserve_text():
    text = "中文abcIPA\u0259"
    segments = _font_segments(text)
    joined = "".join(seg for _font, seg in segments)
    assert joined == text
    assert all(seg for _font, seg in segments)


def test_actual_read_order_from_output_pdf():
    page = fitz.open().new_page(width=200, height=300)
    page.insert_text(fitz.Point(20, 40), "line one", fontsize=12)
    page.insert_text(fitz.Point(20, 160), "line two", fontsize=12)
    canonical, items = _actual_read_order(page)
    assert "lineone" in canonical
    assert "linetwo" in canonical
    assert items and items[0]["y0"] < items[-1]["y0"]


def test_garble_ratio_detects_bad_tokens():
    assert garble_ratio("正常中文") < 0.5
    assert garble_ratio(":W?32e ???") > 0.0


def test_mixed_page_reading_order_ignores_fallback(tmp_path):
    """Mixed aligned+fallback page must gate order only on non-exempt text."""
    pdf = tmp_path / "mixed.pdf"
    doc = fitz.open()
    page = doc.new_page(width=200, height=300)
    page.insert_text(fitz.Point(20, 40), "aligned text", fontsize=12)
    page.insert_text(fitz.Point(20, 240), "fallback text", fontsize=12)
    doc.save(pdf)
    doc.close()

    boxes_path = tmp_path / "boxes.json"
    report_path = tmp_path / "align_report.json"
    boxes = {
        "0": {
            "reliable": True,
            "lines": [
                {
                    "text": "aligned text",
                    "box": [10, 10, 180, 40],
                    "mode": "aligned",
                    "source": "md",
                    "line_id": "A0",
                    "column": 0,
                    "cell_row": None,
                    "cell_col": None,
                    "subline_index": 0,
                    "order": 0,
                    "order_exempt": False,
                },
                {
                    "text": "fallback text",
                    "box": [20, 800, 980, 900],
                    "mode": "fallback_capacity",
                    "source": "md",
                    "line_id": "F0",
                    "column": 0,
                    "cell_row": None,
                    "cell_col": None,
                    "subline_index": 0,
                    "order": 1,
                    "order_exempt": True,
                },
            ],
        }
    }
    boxes_path.write_text(json.dumps(boxes, ensure_ascii=False), encoding="utf-8")
    align_report = {
        "schema": "align_report_v2",
        "pages": [{
            "page_index": 0,
            "aligned_coverage": 0.5,
            "covered_coverage": 1.0,
            "page_aligned": False,
            "table_unreliable": False,
            "failure_reasons": {},
        }],
        "constants": {},
    }
    report_path.write_text(json.dumps(align_report, ensure_ascii=False), encoding="utf-8")

    result = verify_alignment(pdf, {"0": "aligned text\nfallback text"}, boxes_path, report_path)
    page0 = result["pages"][0]
    assert page0["zero_loss"] is True
    assert page0["reading_order_ok"] is True
    assert page0["order_cross_check_ok"] is True
    assert page0["table_cell_gate"]["status"] == "not_implemented"


def test_align_page_header_footer_produces_header_footer_excluded():
    geom = {
        "reliable": True,
        "lines": [
            {
                "text": "第 1 页",
                "box": [20, 20, 500, 60],
                "column": 0,
                "header_footer": True,
                "cell_row": None,
                "cell_col": None,
            }
        ],
    }
    lines, metrics = align_page("第 1 页", geom, page_index=0)
    assert len(lines) == 1
    assert lines[0]["mode"] == "header_footer_excluded"
    assert lines[0]["order_exempt"] is True
    assert metrics["mode_counts"]["header_footer_excluded"] == 1
    assert metrics["aligned_coverage"] == 0.0
    assert metrics["covered_coverage"] == 1.0


def test_metrics_from_lines_counts_external_aligned():
    lines = [
        {"text": "对齐文字", "mode": "aligned", "box": [10, 20, 500, 60]},
        {"text": "兜底", "mode": "fallback_capacity", "box": [20, 800, 980, 900]},
    ]
    metrics = _metrics_from_lines("对齐文字\n兜底", lines, 0, "external")
    assert metrics["aligned_coverage"] > 0.0
    assert metrics["covered_coverage"] == 1.0
    assert metrics["mode_counts"]["aligned"] == 1
    assert metrics["mode_counts"]["fallback_capacity"] == 1


def test_repeated_anchors_from_candidate_sets_pure_helper():
    p1 = {"《语音学教程》", "正文一句"}
    p2 = {"《语音学教程》", "另一句"}
    p3 = {"《语音学教程》"}
    anchors = repeated_anchors_from_candidate_sets([p1, p2, p3], min_repeat=2)
    assert "《语音学教程》" in anchors
    assert "正文一句" not in anchors


def test_is_header_footer_accepts_cross_page_anchor():
    line = {"bbox": [10, 5, 300, 40], "text": "《语音学教程》"}
    assert _is_header_footer(line, 1000.0) is False
    assert _is_header_footer(line, 1000.0, known_header_anchors=("《语音学教程》",)) is True


def test_extract_repeated_header_anchors_actual_pdf(tmp_path):
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(width=200, height=300)
        page.insert_text(fitz.Point(20, 20), "RUNNING HEAD", fontsize=10)
        page.insert_text(fitz.Point(20, 200), "body line", fontsize=10)
    src = tmp_path / "repeated.pdf"
    doc.save(src)
    doc.close()
    anchors = extract_repeated_header_anchors(src, [0, 1], min_repeat=2)
    assert "RUNNING HEAD" in anchors


def test_rotated_page_extract_lines_maps_coordinates():
    doc = fitz.open()
    page = doc.new_page(width=200, height=300)
    page.insert_text(fitz.Point(20, 40), "ROTATED", fontsize=12)
    page.set_rotation(90)
    geom = extract_lines(page)
    assert geom["lines"]
    assert any("rotation=90" in w for w in geom["warnings"])
    for ln in geom["lines"]:
        assert all(0.0 <= v <= 1000.0 for v in ln["box"])
    doc.close()


def test_table_cell_gate_computes_recall_precision():
    md = "| A | B |\n| --- | --- |\n| 甲 | 乙 |\n| 丙 | 丁 |\n"
    lines = [
        {"text": "A", "mode": "aligned", "cell_row": 0, "cell_col": 0},
        {"text": "B", "mode": "aligned", "cell_row": 0, "cell_col": 1},
        {"text": "甲", "mode": "aligned", "cell_row": 1, "cell_col": 0},
        {"text": "乙", "mode": "aligned", "cell_row": 1, "cell_col": 1},
        {"text": "丙", "mode": "aligned", "cell_row": 2, "cell_col": 0},
        {"text": "丁", "mode": "aligned", "cell_row": 2, "cell_col": 1},
    ]
    gate = _table_cell_gate(lines, False, md)
    assert gate["status"] == "computed"
    assert gate["expected_cells"] == 6
    assert gate["actual_cells"] == 6
    assert gate["matched_cells"] == 6
    assert gate["cell_recall"] == 1.0
    assert gate["cell_precision"] == 1.0


def test_table_cell_gate_no_cell_metadata_is_not_implemented():
    md = "| A | B |\n| --- | --- |\n| 甲 | 乙 |\n"
    gate = _table_cell_gate([], False, md)
    assert gate["status"] == "no_table"
    assert gate["note"]


def test_pilot_parse_pages():
    assert parse_pages("0-2,7") == [0, 1, 2, 7]
    assert parse_pages("0,1,2") == [0, 1, 2]
    assert parse_pages("0-0") == [0]


def test_font_segments_keeps_special_symbols_off_arial():
    # CJK punctuation, circled/roman numeral symbols, middle dot, and accented
    # Latin must route to china-s; only IPA/modifier marks should use ocripa.
    segs = _font_segments("∶①②③ⅡⅢⅠ-·éəʃ")
    fonts = [font for font, _seg in segs]
    assert "china-s" in fonts
    assert "ocripa" in fonts
    assert "helv" in fonts
    # The ASCII hyphen must not be swallowed into an Arial segment.
    for font, seg in segs:
        if font == "ocripa":
            assert "-" not in seg
        if font == "helv":
            assert "-" in seg


def test_filter_exempt_actual_does_not_delete_non_exempt_line_with_page_number():
    actual = [{"text": "=绪 论/1"}, {"text": "目录"}]
    exempt = [{"text": "1"}]
    text, kept = _filter_exempt_actual(actual, exempt)
    assert "=绪论/1" in text
    assert "目录" in text
    assert any("绪" in str(it["text"]) for it in kept)


def test_verify_alignment_supports_page_numbers_for_subset_pdf(tmp_path):
    pdf = tmp_path / "subset.pdf"
    doc = fitz.open()
    page = doc.new_page(width=200, height=300)
    page.insert_text(fitz.Point(20, 40), "subset page text", fontsize=12)
    doc.save(pdf)
    doc.close()

    boxes_path = tmp_path / "boxes.json"
    report_path = tmp_path / "align_report.json"
    boxes = {
        "1": {
            "reliable": True,
            "lines": [
                {
                    "text": "subset page text",
                    "box": [10, 10, 180, 40],
                    "mode": "aligned",
                    "source": "md",
                    "line_id": "A0",
                    "column": 0,
                    "cell_row": None,
                    "cell_col": None,
                    "subline_index": 0,
                    "order": 0,
                    "order_exempt": False,
                }
            ],
        }
    }
    boxes_path.write_text(json.dumps(boxes, ensure_ascii=False), encoding="utf-8")
    align_report = {
        "schema": "align_report_v2",
        "pages": [{
            "page_index": 1,
            "aligned_coverage": 1.0,
            "covered_coverage": 1.0,
            "page_aligned": True,
            "table_unreliable": False,
            "failure_reasons": {},
        }],
        "constants": {},
    }
    report_path.write_text(json.dumps(align_report, ensure_ascii=False), encoding="utf-8")

    result = verify_alignment(
        pdf,
        {"1": "subset page text"},
        boxes_path,
        report_path,
        page_numbers=[1],
    )
    assert result["pages"][0]["page_index"] == 1
    assert result["pages"][0]["zero_loss"] is True
    assert result["pages"][0]["reading_order_ok"] is True


def test_font_for_char_routes_ipa_symbols_and_cjk():
    """Latin IPA letters go to ocripa, math/dingbat symbols to ocrisym,
    CJK stays on china-s, ASCII stays on helv."""
    assert _font_for_char("æ") == "ocripa"
    assert _font_for_char("ŋ") == "ocripa"
    assert _font_for_char("ə") == "ocripa"
    assert _font_for_char("∅") == "ocrisym"
    assert _font_for_char("✘") == "ocrisym"
    assert _font_for_char("→") == "ocrisym"
    assert _font_for_char("²") == "ocrisym"
    assert _font_for_char("中") == "china-s"
    assert _font_for_char("-") == "helv"


def test_normalize_md_preserves_literal_pipes_but_collapses_tables():
    """Literal metrical/phonology pipes must survive; real markdown table rows
    (single leading+trailing pipe) are still collapsed to space-joined cells."""
    md = "（“|”表示音步的界线）\n|| 龈边音 G 小舌音 s’ 龈擦音\n| A | B |\n"
    norm = normalize_md(md)
    assert "（“|”表示音步的界线）" in norm
    assert "|| 龈边音 G 小舌音 s’ 龈擦音" in norm
    assert "|" in norm
    assert "A B" in norm
    assert "| A | B |" not in norm
