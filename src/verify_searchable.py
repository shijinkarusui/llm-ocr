from __future__ import annotations

import argparse
import difflib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import fitz
except ImportError as exc:
    raise RuntimeError("PyMuPDF is required") from exc

try:
    from .geom_align import normalize_md as _normalize_md
    from .geom_extract import garble_ratio as _garble_ratio
    from .make_searchable import plan_boxed_lines as _plan_boxed_lines
except ImportError:
    from geom_align import normalize_md as _normalize_md
    from geom_extract import garble_ratio as _garble_ratio
    from make_searchable import plan_boxed_lines as _plan_boxed_lines

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = ROOT.parent / "book-to-quiz-pipeline" / "data" / "语音学教程.pdf"
DEFAULT_SEARCHABLE = ROOT / "out" / "searchable_sample.pdf"

# Visual-row tolerance for reading order (normalized 0-1000 units ≈ 8 pt on a
# 646 pt page). Items whose y-centers fall within this band are one visual row
# and are ordered left-to-right; sub-pixel font-metric differences must not
# reorder multi-column examples (page 86).
ROW_TOL = 12.0
# Column tolerance (≈ 8.5 pt): items in the same row whose x-centers fall in the
# same column band are stacked fragments of one logical line, so they keep
# top-to-bottom order instead of being x-sorted (page 86's wrapped "擦音：").
COL_TOL = 20.0

_BAD_CHAR_RE = re.compile(r"[?\ufffd]")
_SUSPICIOUS_TOKEN_RE = re.compile(
    r"(?<!\w)(?:[A-Z]{2,}[a-z]|[A-Za-z]*\d+[A-Za-z]+|[A-Za-z]+\?[A-Za-z]*)(?!\w)"
)


def _read_text(value: str | Path) -> str:
    if isinstance(value, Path):
        return value.read_text(encoding="utf-8")
    if not isinstance(value, str):
        raise TypeError("Markdown input must be text or a path")
    try:
        candidate = Path(value)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except (OSError, ValueError):
        pass
    return value


def compare_md(a: str | Path, b: str | Path) -> float:
    """Return difflib SequenceMatcher similarity for two Markdown texts or files."""
    return difflib.SequenceMatcher(None, _read_text(a), _read_text(b)).ratio()


def verify(pdf_path: str | Path, keywords: Iterable[str]) -> dict:
    """Check per-page keyword hits and total extracted character count."""
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(str(pdf))
    terms = [str(keyword) for keyword in keywords]
    hits: dict[str, list[int]] = {keyword: [] for keyword in terms}
    total_chars = 0
    with fitz.open(str(pdf)) as document:
        page_count = document.page_count
        for page_index in range(page_count):
            text = document.load_page(page_index).get_text()
            total_chars += len(text)
            folded = text.casefold()
            for keyword in terms:
                if keyword.casefold() in folded:
                    hits[keyword].append(page_index + 1)
    hit_pages = sorted({page for pages in hits.values() for page in pages})
    return {
        "pdf_path": pdf.as_posix(),
        "page_count": page_count,
        "keywords": hits,
        "hit_pages": hit_pages,
        "copyable_chars": total_chars,
        "total_copyable_chars": total_chars,
    }


def garble_ratio(text: str) -> float:
    """Compatibility wrapper; core implementation is in geom_extract."""
    return _garble_ratio(text)


def self_check(pdf_path: str | Path = DEFAULT_PDF) -> list[float]:
    """Return first-three-page old-layer ratios without exposing extracted text."""
    ratios: list[float] = []
    with fitz.open(str(pdf_path)) as document:
        for page_index in range(min(3, document.page_count)):
            ratios.append(garble_ratio(document.load_page(page_index).get_text()))
    return ratios


def verify_assimilation_sample(pdf_path: str | Path = DEFAULT_SEARCHABLE) -> dict:
    """Verify progressive and regressive English keyword hits in a sample PDF."""
    return verify(pdf_path, ("progressive assimilation", "regressive assimilation"))


def _canonical(text: str) -> str:
    """Canonicalize extracted text: strip all whitespace/artifacts for sequence compare."""
    return "".join(ch for ch in unicodedata.normalize("NFC", text) if not ch.isspace())


def _char_multiset(text: str) -> Counter:
    """Non-whitespace character multiset; order-independent zero-loss check."""
    return Counter(ch for ch in unicodedata.normalize("NFC", text) if not ch.isspace())


def _same_line_box(actual: dict[str, Any], expected_line: dict[str, Any]) -> bool:
    """Return True if an actual output line geometrically+textually matches a boxes line."""
    box = expected_line.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    try:
        ay = (float(actual["y0"]) + float(actual["y1"])) / 2.0
        by = (float(box[1]) + float(box[3])) / 2.0
    except (KeyError, TypeError, ValueError):
        return False
    # Allow modest y tolerance; line heights are around 20-60 in 0-1000 space.
    if abs(ay - by) > 30.0:
        return False
    actual_c = _canonical(str(actual.get("text", "")))
    expected_c = _canonical(str(expected_line.get("text", "")))
    if not actual_c or not expected_c:
        return False
    return actual_c == expected_c or difflib.SequenceMatcher(None, actual_c, expected_c).ratio() >= 0.85


def _filter_exempt_actual(
    actual_items: list[dict[str, Any]],
    exempt_lines: list[dict[str, Any]],
    non_exempt_lines: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return (canonical non-exempt actual text, kept items).

    When ``non_exempt_lines`` is supplied, keep only actual output lines that
    match a non-exempt boxes line by geometry AND text. This is much safer than
    text-substring removal: a short non-exempt line such as ``图`` is not deleted
    merely because it is a substring of a fallback paragraph. Without
    ``non_exempt_lines`` we fall back to the old text-chunk removal path.
    """
    chunks = [_canonical(str(ln.get("text", ""))) for ln in exempt_lines]
    non_exempt_chunks = (
        [_canonical(str(ln.get("text", ""))) for ln in non_exempt_lines]
        if non_exempt_lines is not None
        else []
    )
    kept = []
    for item in actual_items:
        c = _canonical(str(item.get("text", "")))
        if not c:
            continue
        # Keep actual lines that clearly match a non-exempt boxes line. This
        # protects short real content (for example a single "图" in a TOC)
        # from being removed just because it is also a substring of fallback.
        if any(
            c == n or difflib.SequenceMatcher(None, c, n).ratio() >= 0.85
            for n in non_exempt_chunks
        ):
            kept.append(item)
            continue
        matched = False
        for idx, chunk in enumerate(chunks):
            if not chunk:
                continue
            # Only remove an actual line when its text is wholly contained in an
            # exempt fallback/header-footer chunk. If the exempt chunk is only a
            # substring of a larger actual line (e.g. a page number inside a TOC
            # line), removing the whole line would destroy legitimate non-exempt
            # text, so keep the actual line.
            if c and c in chunk:
                chunks[idx] = chunk.replace(c, "", 1)
                matched = True
                break
        if not matched:
            kept.append(item)
    return _canonical("".join(str(it.get("text", "")) for it in kept)), kept


def _expected_table_cells(md_text: str) -> list[str]:
    """Parse Markdown table rows into non-empty expected cell texts."""
    cells: list[str] = []
    for raw in (md_text or "").splitlines():
        line = raw.strip()
        if "|" not in line:
            continue
        core = line.strip("|")
        if not core:
            continue
        # Skip GFM separator rows like | --- | :---: |.
        if re.fullmatch(r"[\s:\-|]+", core):
            continue
        for part in core.split("|"):
            text = _canonical(part)
            if text:
                cells.append(text)
    return cells


def _actual_cell_texts(page_lines: list[dict[str, Any]]) -> list[str]:
    """Collect actual cell texts from boxes-v2 metadata (aligned or header/footer only)."""
    cells: list[str] = []
    for ln in page_lines:
        if ln.get("cell_row") is None or ln.get("cell_col") is None:
            continue
        if ln.get("mode") not in (None, "aligned", "header_footer_excluded"):
            continue
        text = _canonical(str(ln.get("text", "")))
        if text:
            cells.append(text)
    return cells


def _cell_texts_match(a: str, b: str) -> bool:
    """Best-effort cell match: exact canonical equality or SequenceMatcher >=0.85."""
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _table_cell_gate(
    page_lines: list[dict[str, Any]],
    table_unreliable: bool,
    md_text: str = "",
    actual_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Best-effort table cell-level recall/precision gate.

    Two sources of expected cells are supported:

    * Markdown pipe rows (embedded geometry path, with cell_row/cell_col
      metadata on the boxes lines).
    * ``is_table`` + ``table_cells`` metadata attached by the external
      ``out/build_mineru_boxes.py`` builder (MinerU table markup). The actual
      side is then the output PDF's text inside the table bbox, so the check
      is a real render-vs-source comparison, not a metadata echo.

    Returns an explicit non-computed status (``not_implemented`` or
    ``no_table``) only when there is genuinely nothing to check.
    """
    expected = _expected_table_cells(md_text)
    actual = _actual_cell_texts(page_lines)
    table_lines = [ln for ln in page_lines if ln.get("is_table")]

    if table_unreliable:
        return {
            "status": "not_implemented",
            "note": "table marked unreliable; cell-level gate not computed",
            "expected_cells": len(expected),
            "actual_cells": len(actual),
            "matched_cells": 0,
            "cell_recall": 0.0,
            "cell_precision": 0.0,
        }

    if table_lines:
        # External MinerU path: expected cells come from the table markup that
        # the builder attached, because llm_pages.json joins table cells with
        # spaces (no pipes) and cannot be re-parsed into cells.
        ext_expected: list[str] = []
        for ln in table_lines:
            for c in (ln.get("table_cells") or []):
                t = _canonical(str(c))
                if t:
                    ext_expected.append(t)
        if not ext_expected:
            return {
                "status": "no_table",
                "note": "table line present but no cell markup attached",
                "expected_cells": 0,
                "actual_cells": 0,
                "matched_cells": 0,
                "cell_recall": 0.0,
                "cell_precision": 0.0,
            }
        # Actual side: concatenate the output PDF lines whose center lies
        # inside any table bbox (normalized coordinates, small margin).
        boxes = [tuple(float(v) for v in ln["box"]) for ln in table_lines]
        actual_region = ""
        for it in (actual_items or []):
            cx = (it["x0"] + it["x1"]) / 2.0
            cy = (it["y0"] + it["y1"]) / 2.0
            if any(x0 - 2.0 <= cx <= x1 + 2.0 and y0 - 2.0 <= cy <= y1 + 2.0 for x0, y0, x1, y1 in boxes):
                actual_region += _canonical(str(it.get("text", "")))
        matched = 0
        matched_chars = 0
        for exp in ext_expected:
            if exp in actual_region:
                matched += 1
                matched_chars += len(exp)
        # Precision: share of the rendered table-region text covered by the
        # expected cells that were found (honest coverage, not a metadata echo).
        precision = (matched_chars / len(actual_region)) if actual_region else 0.0
        return {
            "status": "computed",
            "note": "external table gate: MinerU table cells vs output PDF text in table region",
            "expected_cells": len(ext_expected),
            "actual_cells": 1 if actual_region else 0,
            "matched_cells": matched,
            "cell_recall": (matched / len(ext_expected)) if ext_expected else 0.0,
            "cell_precision": precision,
        }

    if not expected and not actual:
        return {
            "status": "not_implemented",
            "note": "no table Markdown and no cell metadata; cell-level gate not applicable",
        }
    if not expected:
        return {
            "status": "no_table",
            "note": "cell metadata present but no table Markdown rows found",
            "expected_cells": 0,
            "actual_cells": len(actual),
            "matched_cells": 0,
            "cell_recall": 0.0,
            "cell_precision": 0.0,
        }
    if not actual:
        return {
            "status": "no_table",
            "note": "table Markdown present but no cell_row/cell_col metadata; cannot compute",
            "expected_cells": len(expected),
            "actual_cells": 0,
            "matched_cells": 0,
            "cell_recall": 0.0,
            "cell_precision": 0.0,
        }
    used = [False] * len(actual)
    matched = 0
    for exp in expected:
        for idx, act in enumerate(actual):
            if not used[idx] and _cell_texts_match(exp, act):
                matched += 1
                used[idx] = True
                break
    return {
        "status": "computed",
        "note": "best-effort cell-level gate from Markdown table + cell metadata",
        "expected_cells": len(expected),
        "actual_cells": len(actual),
        "matched_cells": matched,
        "cell_recall": (matched / len(expected)) if expected else 0.0,
        "cell_precision": (matched / len(actual)) if actual else 0.0,
    }


def _lookup_md(md_dict: dict[str, Any], pno: int) -> str:
    for key in (str(pno), pno, str(pno + 1), pno + 1):
        if key in md_dict:
            return str(md_dict[key])
    return ""


def _merge_equation_actual_items(
    items: list[dict[str, Any]],
    equation_regions: Sequence[Sequence[float]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group output fragments whose centers fall inside an equation block bbox.

    PyMuPDF splits one inserted formula line into many small glyph bboxes (one
    per font segment, at slightly different y), which would otherwise interleave
    fragments from different formulas. Each equation block is a single visual
    unit, so its fragments are merged left-to-right/top-to-bottom (which for the
    raw-LaTeX text equals insertion order) and treated as one reading-order item.
    """
    regions = [tuple(float(v) for v in r) for r in equation_regions]
    others: list[dict[str, Any]] = []
    buckets: list[list[dict[str, Any]]] = [[] for _ in regions]
    for it in items:
        cx = (it["x0"] + it["x1"]) / 2.0
        cy = (it["y0"] + it["y1"]) / 2.0
        for idx, (x0, y0, x1, y1) in enumerate(regions):
            if x0 - 5.0 <= cx <= x1 + 5.0 and y0 - 5.0 <= cy <= y1 + 5.0:
                buckets[idx].append(it)
                break
        else:
            others.append(it)
    units: list[dict[str, Any]] = []
    for idx, frags in enumerate(buckets):
        if not frags:
            continue
        ordered = sorted(frags, key=lambda it: (it["x0"], it["y0"]))
        # The merged unit occupies the equation block bbox, not the tiny
        # fragment-union bbox. Fragment unions differ by sub-pixel font metrics
        # (which would make a formula's y-center flip relative to its neighbors);
        # using the block bbox keeps equation ordering identical to the planner.
        x0, y0, x1, y1 = regions[idx]
        units.append({
            "text": "".join(it["text"] for it in ordered),
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "equation_unit": True,
        })
    return others, units


def _merge_expected_equation_units(plan_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse plan entries of one equation block into a single expected unit.

    ``plan_boxed_lines`` expands an equation block into several physical lines
    (``$$`` / formula / ``$$``) so the writer keeps zero-loss; the verifier's
    expected order must mirror the actual-side equation-region merge.
    """
    groups: dict[tuple[float, float, float, float], list[dict[str, Any]]] = {}
    singles: list[dict[str, Any]] = []
    for e in plan_entries:
        r = e.get("equation_region")
        if r is not None:
            groups.setdefault(tuple(float(v) for v in r), []).append(e)
        else:
            singles.append(e)
    units = list(singles)
    for region, entries in groups.items():
        units.append({
            "text": "".join(e["text"] for e in entries),
            "y_center_norm": (region[1] + region[3]) / 2.0,
            "x_center_norm": (region[0] + region[2]) / 2.0,
        })
    return units


def _actual_read_order(
    page: fitz.Page,
    equation_regions: Sequence[Sequence[float]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Derive reading order from the OUTPUT PDF's actual bboxes (not boxes.json).

    ``equation_regions`` are normalized equation-block bboxes; fragments inside
    them are merged into one unit first (see ``_merge_equation_actual_items``).
    """
    rect = page.rect
    items: list[dict[str, Any]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            bbox = line.get("bbox")
            if not bbox:
                continue
            nx0 = bbox[0] * 1000.0 / rect.width if rect.width else 0.0
            ny0 = bbox[1] * 1000.0 / rect.height if rect.height else 0.0
            nx1 = bbox[2] * 1000.0 / rect.width if rect.width else 0.0
            ny1 = bbox[3] * 1000.0 / rect.height if rect.height else 0.0
            items.append({
                "text": text,
                "x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1,
            })
    equation_units: list[dict[str, Any]] = []
    if equation_regions:
        items, equation_units = _merge_equation_actual_items(items, equation_regions)
    # Merge font segments that belong to the same physical line: PyMuPDF splits
    # one inserted line into separate line objects when it mixes fonts (e.g.
    # CJK + IPA), and treating those as separate reading-order units would
    # interleave wrapped fragments incorrectly. Segments on the same baseline
    # with adjacent x ranges are one line; far-apart items (e.g. table columns)
    # stay separate. Sort by y-bucket + x0 so that segments of one visual row
    # are concatenated in x order even when their y0 differs by a fraction.
    grouped: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda it: (round(it["y0"] / 3.0), it["x0"])):
        if (
            grouped
            and abs(grouped[-1]["y0"] - it["y0"]) <= 3.0
            and abs(grouped[-1]["y1"] - it["y1"]) <= 3.0
            and it["x0"] <= grouped[-1]["x1"] + 30.0
            and it["x0"] >= grouped[-1]["x0"] - 1.0
        ):
            g = grouped[-1]
            g["text"] += it["text"]
            g["x1"] = max(g["x1"], it["x1"])
            g["y1"] = max(g["y1"], it["y1"])
        else:
            grouped.append(dict(it))
    grouped.extend(equation_units)
    # Normal page order: visual row first, then top-to-bottom within the row.
    # The secondary key is the LEFT edge (1px grid), not the x-center. The
    # writer inserts every full-width physical line at the same x0 inside one
    # box, so wrapped fragments of one logical line must stay together; but
    # fragments RAND at different font sizes have different widths (the tail
    # fragment of a wrapped Chinese paragraph is narrower, hence its x-center
    # drifts right, e.g. page 86's "持它的…" jumped ahead of "等来表示…").
    # Within one visual row the old x-center key compared full rows against
    # short fragments; x0 is stable across fragments of one column, while the
    # x-center tie-break only applies when two text columns genuinely interleave
    # (those differ in x0 by far more than MERGE_GAP). The old comment about
    # "：" after "擦音" is respected: that fragment keeps its wrapp-order place
    # because fragments of the SAME box share the same x0 and their y order is
    # unambiguous.
    grouped.sort(key=lambda it: (
        round(((it["y0"] + it["y1"]) / 2.0) / ROW_TOL),
        round(it["x0"]),
        (it["y0"] + it["y1"]) / 2.0,
        (it["x0"] + it["x1"]) / 2.0,
    ))
    return _canonical("".join(it["text"] for it in grouped)), grouped


def verify_alignment(
    out_pdf: str | Path,
    md_dict: dict[str, Any],
    boxes_path: str | Path,
    align_report_path: str | Path,
    report_path: str | Path | None = None,
    keywords: Iterable[str] | None = None,
    page_numbers: Sequence[int] | None = None,
) -> dict:
    """Verify the searchable output against the exact align artifacts.

    Reading order is derived from the output PDF's actual dict bboxes; boxes v2
    metadata is used only as a cross-check.
    """
    pdf = Path(out_pdf)
    boxes_file = Path(boxes_path)
    report_file = Path(align_report_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"output PDF missing: {pdf}")
    if not boxes_file.is_file():
        raise FileNotFoundError(f"boxes.json missing: {boxes_file}")
    if not report_file.is_file():
        raise FileNotFoundError(f"align_report.json missing: {report_file}")

    boxes = json.loads(boxes_file.read_text(encoding="utf-8"))
    align_report = json.loads(report_file.read_text(encoding="utf-8"))
    page_results: list[dict[str, Any]] = []
    total_zero_pass = 0
    total_order_pass = 0
    total_aligned_pages = 0
    table_unreliable_pages: list[int] = []
    sum_aligned = 0.0
    sum_covered = 0.0

    with fitz.open(str(pdf)) as doc:
        orig_numbers = list(page_numbers) if page_numbers is not None else list(range(doc.page_count))
        if len(orig_numbers) < doc.page_count:
            # Subset PDFs may have fewer pages; map by output position, not by value.
            pass
        for pno in range(doc.page_count):
            orig_pno = orig_numbers[pno] if pno < len(orig_numbers) else pno
            page = doc.load_page(pno)
            expected_raw = _lookup_md(md_dict, orig_pno)
            expected = _normalize_md(expected_raw)
            page_boxes = boxes.get(str(orig_pno)) or boxes.get(orig_pno) or {}
            page_lines = page_boxes.get("lines") or []
            exempt_lines = [ln for ln in page_lines if bool(ln.get("order_exempt", False))]
            non_exempt_lines = [ln for ln in page_lines if not bool(ln.get("order_exempt", False))]
            all_exempt = bool(page_lines) and not non_exempt_lines
            # Equation blocks are rendered as several physical lines but are one
            # visual formula region; merge their output fragments (and the
            # expected plan entries) into a single reading-order unit.
            equation_regions = [
                tuple(float(v) for v in ln["box"])
                for ln in non_exempt_lines
                if bool(ln.get("is_equation", False))
            ]
            actual_canon, actual_items = _actual_read_order(page, equation_regions=equation_regions)

            # Zero-loss is order-independent: every expected non-whitespace
            # character must be present, and no extra characters may appear.
            zero_loss = _char_multiset(expected) == _char_multiset(actual_canon)

            # Reading-order gate compares only non-order_exempt text. Fallback
            # / header-footer text may be placed at the bottom and must not make
            # a mixed page fail.
            # Expected order is the writer's physical-line layout (plan_boxed_lines),
            # not the block-level boxes order: long MinerU blocks are wrapped into
            # several physical lines, and figure labels can sit between those
            # wrapped fragments, so the gate must compare line-level vs line-level.
            if non_exempt_lines:
                expected_plan, _ = _plan_boxed_lines(non_exempt_lines, page.rect.width, page.rect.height)
                expected_units = _merge_expected_equation_units(expected_plan)
                # Same visual-row quantisation as the actual side: row primary,
                # then x0 (1px grid) so wrapped fragments of one box stay in
                # layout order, then exact centers as tie-breaks.
                expected_units.sort(key=lambda e: (
                    round(e["y_center_norm"] / ROW_TOL),
                    round(e.get("x0_norm", e["x_center_norm"])),
                    e["y_center_norm"], e["x_center_norm"],
                ))
                expected_non_exempt = _canonical("".join(e["text"] for e in expected_units))
            else:
                expected_non_exempt = ""
            actual_non_exempt, _kept = _filter_exempt_actual(actual_items, exempt_lines, non_exempt_lines)
            reading_order_ok = None
            if all_exempt:
                reading_order_ok = None  # fallback pages: order not gated, zero loss still checked
            elif expected_non_exempt:
                reading_order_ok = expected_non_exempt == actual_non_exempt
            else:
                reading_order_ok = True

            # Cross-check actual bbox order against boxes metadata non-exempt order.
            order_cross_check_ok = None
            if not all_exempt and expected_non_exempt:
                order_cross_check_ok = expected_non_exempt == actual_non_exempt

            align_page = next((p for p in align_report.get("pages", []) if p.get("page_index") == orig_pno), {})
            aligned_cov = float(align_page.get("aligned_coverage", 0.0))
            covered_cov = float(align_page.get("covered_coverage", 0.0))
            if align_page.get("page_aligned"):
                total_aligned_pages += 1
            if align_page.get("table_unreliable"):
                table_unreliable_pages.append(orig_pno)
            sum_aligned += aligned_cov
            sum_covered += covered_cov
            if zero_loss:
                total_zero_pass += 1
            if reading_order_ok:
                total_order_pass += 1

            table_cell_gate = _table_cell_gate(
                page_lines,
                bool(align_page.get("table_unreliable", False)),
                expected_raw,
                actual_items,
            )
            page_results.append({
                "page_index": orig_pno,
                "output_page_index": pno,
                "expected_non_ws": sum(1 for ch in expected if not ch.isspace()),
                "actual_non_ws": sum(1 for ch in actual_canon if not ch.isspace()),
                "zero_loss": zero_loss,
                "reading_order_ok": reading_order_ok,
                "order_cross_check_ok": order_cross_check_ok,
                "all_order_exempt": all_exempt,
                "aligned_coverage": aligned_cov,
                "covered_coverage": covered_cov,
                "page_aligned": bool(align_page.get("page_aligned", False)),
                "table_unreliable": bool(align_page.get("table_unreliable", False)),
                "table_cell_gate": table_cell_gate,
                "failure_reasons": align_page.get("failure_reasons", {}),
            })

    page_count = len(page_results)
    summary = {
        "pages_total": page_count,
        "zero_loss_pages": total_zero_pass,
        "reading_order_checked_pages": sum(1 for r in page_results if r["reading_order_ok"] is not None),
        "reading_order_ok_pages": total_order_pass,
        "aligned_pages": total_aligned_pages,
        "aligned_pages_ratio": (total_aligned_pages / page_count) if page_count else 0.0,
        "mean_aligned_coverage": (sum_aligned / page_count) if page_count else 0.0,
        "mean_covered_coverage": (sum_covered / page_count) if page_count else 0.0,
        "table_unreliable_pages": table_unreliable_pages,
        "table_cell_gate": {
            "status": "computed" if any(r["table_cell_gate"]["status"] == "computed" for r in page_results) else "not_implemented",
            "implemented": any(r["table_cell_gate"]["status"] == "computed" for r in page_results),
            "computed_pages": sum(1 for r in page_results if r["table_cell_gate"]["status"] == "computed"),
            "mean_cell_recall": (
                sum(r["table_cell_gate"]["cell_recall"] for r in page_results if r["table_cell_gate"]["status"] == "computed")
                / max(1, sum(1 for r in page_results if r["table_cell_gate"]["status"] == "computed"))
            ),
            "mean_cell_precision": (
                sum(r["table_cell_gate"]["cell_precision"] for r in page_results if r["table_cell_gate"]["status"] == "computed")
                / max(1, sum(1 for r in page_results if r["table_cell_gate"]["status"] == "computed"))
            ),
            "note": "computed from best-effort table cell matching; no_table/not_implemented pages are listed per page",
        },
    }
    report = {
        "schema": "verify_report_v2",
        "inputs": {
            "out_pdf": str(pdf),
            "boxes_json": str(boxes_file),
            "align_report_json": str(report_file),
        },
        "summary": summary,
        "pages": page_results,
    }
    if report_path:
        out = Path(report_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _ascii_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--compare-a", type=Path)
    parser.add_argument("--compare-b", type=Path)
    parser.add_argument("--verify-alignment", action="store_true", help="run verify_alignment")
    parser.add_argument("--out-pdf", type=Path, help="searchable output PDF for verify_alignment")
    parser.add_argument("--md-json", type=Path, help="JSON mapping page to Markdown for verify_alignment")
    parser.add_argument("--boxes-json", type=Path, help="align_out/boxes.json")
    parser.add_argument("--align-report", type=Path, help="align_out/align_report.json")
    parser.add_argument("--report", type=Path, help="verify_report.json output path")
    args = parser.parse_args()

    if args.verify_alignment:
        required = [args.out_pdf, args.md_json, args.boxes_json, args.align_report]
        if any(item is None for item in required):
            parser.error("--verify-alignment requires --out-pdf --md-json --boxes-json --align-report")
        md_dict = json.loads(args.md_json.read_text(encoding="utf-8"))
        result = verify_alignment(
            args.out_pdf, md_dict, args.boxes_json, args.align_report, args.report,
            keywords=args.keyword or None,
        )
        print("verify_alignment=" + _ascii_json(result["summary"]))
        return 0

    if not args.pdf.is_file():
        print("self_check=skipped missing_pdf")
    else:
        for index, ratio in enumerate(self_check(args.pdf), start=1):
            print("old_layer_garble_ratio_page%d=%.6f" % (index, ratio))
        if args.keyword:
            print("verify=" + _ascii_json(verify(args.pdf, args.keyword)))
    if args.compare_a and args.compare_b:
        print("compare_ratio=%.6f" % compare_md(args.compare_a, args.compare_b))
    if DEFAULT_SEARCHABLE.is_file():
        print("searchable_sample=" + _ascii_json(verify_assimilation_sample()))
    else:
        print("searchable_sample=skipped missing_file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
