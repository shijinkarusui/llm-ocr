from __future__ import annotations

import json
import sys
from pathlib import Path
import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from src.make_searchable import (
        _extract_page_toc,
        _detect_page_physical_number,
        _build_page_labels_rules,
        plan_boxed_lines,
        make_searchable,
    )
except ImportError:
    from make_searchable import (
        _extract_page_toc,
        _detect_page_physical_number,
        _build_page_labels_rules,
        plan_boxed_lines,
        make_searchable,
    )


def test_extract_page_toc() -> None:
    md = "# 第一章 绪论\n\n正文描述...\n\n## 1.1 研究背景\n背景介绍\n### 1.1.1 详细分类"
    plan = [
        {"text": "第一章 绪论", "x": 100.0, "baseline": 150.0},
        {"text": "1.1 研究背景", "x": 100.0, "baseline": 250.0},
    ]
    toc = _extract_page_toc(md, pno_1based=1, page_plan=plan)
    assert len(toc) == 3
    # [lvl, title, pno]
    assert toc[0] == [1, "第一章 绪论", 1]
    assert toc[1] == [2, "1.1 研究背景", 1]
    assert toc[2] == [3, "1.1.1 详细分类", 1]


def test_detect_page_physical_number() -> None:
    # Pattern 1: · 12 ·
    md1 = "正文内容\n\n· 12 ·"
    assert _detect_page_physical_number(md1) == ("D", 12)

    # Pattern 2: - 45 -
    md2 = "正文内容\n\n- 45 -"
    assert _detect_page_physical_number(md2) == ("D", 45)

    # Pattern 3: 第 88 页
    md3 = "正文内容\n第 88 页"
    assert _detect_page_physical_number(md3) == ("D", 88)

    # Pattern 4: Roman numerals
    md4 = "序言内容\n· iv ·"
    assert _detect_page_physical_number(md4) == ("r", 4)


def test_build_page_labels_rules() -> None:
    detected = [
        ("r", 1), # page 0: i
        ("r", 2), # page 1: ii
        ("D", 1), # page 2: 1
        ("D", 2), # page 3: 2
        None,     # page 4: 3 (continuous)
    ]
    rules = _build_page_labels_rules(detected, total_pages=5)
    assert len(rules) == 2
    assert rules[0] == {"startpage": 0, "style": "r", "prefix": "", "firstpagenum": 1}
    assert rules[1] == {"startpage": 2, "style": "D", "prefix": "", "firstpagenum": 1}


def test_formula_atom_protection() -> None:
    # A long formula in a narrow box should NOT be chopped into pieces by _wrap_line
    line = {
        "text": "$$E = mc^2 + \\int_{0}^{\\infty} f(x)dx + \\sum_{i=1}^{n} x_i$$",
        "box": [100.0, 100.0, 300.0, 140.0],
        "is_equation": True,
    }
    plan, overflow = plan_boxed_lines([line], width=600.0, height=800.0)
    assert len(plan) == 1
    assert plan[0]["text"] == line["text"]
    assert plan[0]["equation_region"] is not None


def test_make_searchable_end_to_end_toc_and_labels(tmp_path: Path) -> None:
    # Create a tiny 2-page source PDF
    src_pdf = tmp_path / "src.pdf"
    doc = fitz.open()
    p1 = doc.new_page(width=500, height=700)
    p1.draw_rect(fitz.Rect(10, 10, 490, 690), color=(0.8, 0.8, 0.8), fill=(0.95, 0.95, 0.95))
    p2 = doc.new_page(width=500, height=700)
    p2.draw_rect(fitz.Rect(10, 10, 490, 690), color=(0.8, 0.8, 0.8), fill=(0.95, 0.95, 0.95))
    doc.save(str(src_pdf))
    doc.close()

    md_dict = {
        0: "# 第一章 概论\n\n语言学调查基础...\n\n· 1 ·",
        1: "## 1.1 调查方法\n\n田野调查原则...\n\n· 2 ·",
    }
    out_pdf = tmp_path / "searchable_out.pdf"
    report_file = tmp_path / "align_report.json"

    make_searchable(
        src_pdf,
        [0, 1],
        md_dict,
        out_pdf=out_pdf,
        geo_source="fallback_only",
        report=report_file,
    )

    assert out_pdf.is_file()
    assert report_file.is_file()

    # Verify TOC and Page Labels in generated PDF
    with fitz.open(str(out_pdf)) as out_doc:
        assert out_doc.page_count == 2

        # 1. TOC verification
        toc = out_doc.get_toc()
        assert len(toc) == 2
        assert toc[0][0] == 1
        assert toc[0][1] == "第一章 概论"
        assert toc[0][2] == 1

        assert toc[1][0] == 2
        assert toc[1][1] == "1.1 调查方法"
        assert toc[1][2] == 2

        # 2. Page Labels verification
        labels = out_doc.get_page_labels()
        assert len(labels) >= 1
        assert labels[0]["style"] == "D"
        assert labels[0]["firstpagenum"] == 1
        assert out_doc[0].get_label() == "1"
        assert out_doc[1].get_label() == "2"

    # 3. Align report verification
    rep = json.loads(report_file.read_text(encoding="utf-8"))
    assert rep.get("toc_count") == 2
    assert rep.get("has_page_labels") is True
