from __future__ import annotations

"""S1 geometry extraction: source-PDF embedded text -> normalized 0-1000 boxes.

Uses PyMuPDF native ``get_text("dict")`` block/line/span bboxes instead of
re-clustering words. Output schema is compatible with the project's existing
``_page_boxes`` consumer and carries all metadata required by the reviewed
plan (mode, line_id, column, cell coordinates, subline, order, order_exempt).
"""

import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import fitz

GARBLE_BAD_CHAR_RE = re.compile(r"[?\ufffd]")
GARBLE_SUSPICIOUS_TOKEN_RE = re.compile(
    r"(?<!\w)(?:[A-Z]{2,}[a-z]|[A-Za-z]*\d+[A-Za-z]+|[A-Za-z]+\?[A-Za-z]*)(?!\w)"
)

# Matching the plan's fixed constants.
GARBLE_THRESHOLD_DEFAULT = 0.3
HEADER_FOOTER_BAND = 0.12
COLUMN_GAP_MIN = 0.12  # fraction of page width

# Page-number / footer-like marker patterns. A header/footer must be positional
# (top/bottom band) AND match at least one of these, or appear in
# KNOWN_HEADER_FOOTER_ANCHORS. A merely short line near the page edge is NOT
# enough.
_PAGE_NUMBER_RE = re.compile(
    r"(?:"
    r"^[ \t]*[\u00b7.]\s*\d{1,4}\s*[\u00b7.][ \t]*$"  # · 12 · / . 12 .
    r"|^[ \t]*[-–—]\s*\d{1,4}\s*[-–—][ \t]*$"          # - 12 -
    r"|^[ \t]*\d{1,4}[ \t]*$"                           # 12
    r"|^[ \t]*PAGE\s*\d{1,4}[ \t]*$"                    # PAGE 12
    r"|^[ \t]*第\s*\d{1,4}\s*页[ \t]*$"                  # 第 12 页
    r"|^[ \t]*页\s*\d{1,4}[ \t]*$"                      # 页 12
    r")",
    re.IGNORECASE,
)

# Optional explicit anchor list for known running heads/footers. Kept as a
# tuple so it is cheap to extend; empty by default.
KNOWN_HEADER_FOOTER_ANCHORS: tuple[str, ...] = ()


def _is_page_like(text: str) -> bool:
    """Return True if ``text`` looks like a page number or visible footer marker."""
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_PAGE_NUMBER_RE.fullmatch(stripped))


def garble_ratio(text: str) -> float:
    """Estimate suspicious text-layer characters as a numeric ratio (0..1)."""
    if not text:
        return 0.0
    compact_length = sum(not char.isspace() for char in text)
    if compact_length == 0:
        return 0.0
    bad_chars = sum(1 for char in text if GARBLE_BAD_CHAR_RE.fullmatch(char))
    bad_chars += sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t")
    bad_chars += sum(
        1 for char in text if unicodedata.category(char) in {"Co", "Cs", "Cn"}
    )
    bad_tokens = sum(
        len(match.group(0)) for match in GARBLE_SUSPICIOUS_TOKEN_RE.finditer(text)
    )
    return min(1.0, (bad_chars + bad_tokens) / compact_length)


def normalize_coords(
    bbox: tuple[float, float, float, float] | list[float] | Any,
    page_rect: fitz.Rect,
) -> list[float] | None:
    """Convert PDF-point bbox to normalized 0-1000 coordinates."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if page_rect.width <= 0 or page_rect.height <= 0:
        return None
    nx0 = max(0.0, min(1000.0, x0 * 1000.0 / page_rect.width))
    nx1 = max(0.0, min(1000.0, x1 * 1000.0 / page_rect.width))
    ny0 = max(0.0, min(1000.0, y0 * 1000.0 / page_rect.height))
    ny1 = max(0.0, min(1000.0, y1 * 1000.0 / page_rect.height))
    if nx1 <= nx0 or ny1 <= ny0:
        return None
    return [nx0, ny0, nx1, ny1]


def _map_bbox_to_output(
    bbox: tuple[float, float, float, float] | list[float] | Any,
    page: fitz.Page,
) -> list[float] | None:
    """Map a raw PyMuPDF text bbox into the output page coordinate system.

    Raw ``get_text`` bboxes are in media/crop coordinates before /Rotate is
    applied. We subtract the cropbox origin, then apply ``page.rotation_matrix``
    so the result matches the raster-backed output page used by
    ``make_searchable`` (also created from ``page.rect``).
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
        crop = page.cropbox
        corners = [
            (x0 - crop.x0, y0 - crop.y0),
            (x1 - crop.x0, y0 - crop.y0),
            (x0 - crop.x0, y1 - crop.y0),
            (x1 - crop.x0, y1 - crop.y0),
        ]
        mapped = []
        for x, y in corners:
            pt = fitz.Point(x, y)
            if page.rotation:
                pt = pt * page.rotation_matrix
            mapped.append((pt.x, pt.y))
        xs = [p[0] for p in mapped]
        ys = [p[1] for p in mapped]
        return [min(xs), min(ys), max(xs), max(ys)]
    except Exception:
        return None


def _normalize_anchor_text(text: str) -> str:
    """Normalize a candidate header/footer anchor for cross-page matching."""
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split())


def _page_anchor_candidates(page: fitz.Page, max_len: int = 80) -> set[str]:
    """Collect short line texts in the top/bottom band of one page."""
    height = float(page.rect.height)
    band_top = HEADER_FOOTER_BAND * height
    band_bottom = (1.0 - HEADER_FOOTER_BAND) * height
    candidates: set[str] = set()
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = _line_text(line).strip()
            if not text or len(text) > max_len:
                continue
            bbox = line.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            y0, y1 = float(bbox[1]), float(bbox[3])
            if y0 <= band_top or y1 >= band_bottom:
                candidates.add(_normalize_anchor_text(text))
    return candidates


def repeated_anchors_from_candidate_sets(
    candidate_sets: list[set[str]],
    min_repeat: int = 2,
) -> tuple[str, ...]:
    """Pure helper: return texts appearing on at least ``min_repeat`` pages."""
    counts: Counter[str] = Counter()
    for candidates in candidate_sets:
        for text in candidates:
            counts[text] += 1
    return tuple(sorted(text for text, count in counts.items() if count >= min_repeat))


def extract_repeated_header_anchors(
    pdf_path: str | Path,
    page_nums: list[int] | None = None,
    min_repeat: int = 2,
) -> tuple[str, ...]:
    """Scan a PDF and return cross-page repeated top/bottom band anchors.

    The caller should scope this to the requested page range to keep it cheap.
    Returns an empty tuple when nothing repeats or the document cannot be read.
    """
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return ()
    try:
        indices = list(range(doc.page_count)) if page_nums is None else [int(p) for p in page_nums]
        candidate_sets: list[set[str]] = []
        for pno in indices:
            if 0 <= pno < doc.page_count:
                candidate_sets.append(_page_anchor_candidates(doc.load_page(pno)))
        return repeated_anchors_from_candidate_sets(candidate_sets, min_repeat=min_repeat)
    finally:
        doc.close()


def extract_words(page: fitz.Page) -> list[dict[str, Any]]:
    """Debug/cross-check helper: return normalized word boxes from PyMuPDF."""
    rect = page.rect
    result: list[dict[str, Any]] = []
    for word in page.get_text("words"):
        mapped = _map_bbox_to_output(word[:4], page)
        if mapped is None:
            continue
        box = normalize_coords(mapped, rect)
        if box is None:
            continue
        result.append({"text": word[4], "box": box})
    return result


def _line_text(line: dict[str, Any]) -> str:
    return "".join(span.get("text", "") for span in line.get("spans", []))


def _assign_columns(lines: list[dict[str, Any]], page_width: float) -> None:
    """Simple two-column detection based on x-center clustering."""
    if len(lines) < 2:
        for line in lines:
            line["column"] = 0
        return
    centers = [((line["bbox"][0] + line["bbox"][2]) / 2.0) for line in lines]
    lo = min(centers)
    hi = max(centers)
    gap = COLUMN_GAP_MIN * page_width
    if hi - lo < gap:
        for line in lines:
            line["column"] = 0
        return
    midpoint = (lo + hi) / 2.0
    for line in lines:
        line["column"] = 0 if ((line["bbox"][0] + line["bbox"][2]) / 2.0) < midpoint else 1


def _assign_cells(lines: list[dict[str, Any]]) -> None:
    """Basic table cell metadata: group lines into rows by y, cols by column id.

    This gives verify/align a conservative cell_row/cell_col even when real
    table boundaries are unavailable; the writer still treats unmatched table
    structures as fallback capacity if needed.
    """
    if not lines:
        return
    multi_column = max((ln.get("column", 0) for ln in lines), default=0) > 0
    rows: list[list[dict[str, Any]]] = []
    for ln in sorted(lines, key=lambda x: (x["bbox"][1] + x["bbox"][3]) / 2.0):
        center_y = (ln["bbox"][1] + ln["bbox"][3]) / 2.0
        if rows and abs(center_y - rows[-1][0].get("_row_center", center_y)) < 1.5:
            rows[-1].append(ln)
        else:
            ln["_row_center"] = center_y
            rows.append([ln])
    for r_idx, row in enumerate(rows):
        for ln in row:
            ln["cell_row"] = r_idx
            ln["cell_col"] = ln.get("column") if multi_column else None


def _is_header_footer(
    line: dict[str, Any],
    page_height: float,
    known_header_anchors: tuple[str, ...] | list[str] | None = None,
) -> bool:
    """Header/footer detection: positional AND explicit page/footer evidence.

    A short line near the top or bottom is not sufficient. We require the line
    to be in the top/bottom band AND at least one of:
    - a page-number/footer pattern (e.g. ``· 12 ·``, ``12``, ``-12-``, ``PAGE n``);
    - a known hard-coded running-head/footer anchor (optional global list);
    - a cross-page repeated anchor supplied by the caller.
    """
    _, y0, _, y1 = line["bbox"]
    band_top = HEADER_FOOTER_BAND * page_height
    band_bottom = (1.0 - HEADER_FOOTER_BAND) * page_height
    positional = y0 <= band_top or y1 >= band_bottom
    if not positional:
        return False
    # Prefer the already-extracted ``text`` field written by extract_lines.
    # Fall back to re-joining spans for callers that pass raw dict lines.
    text = str(line.get("text") or _line_text(line)).strip()
    if not text:
        return False
    if _is_page_like(text):
        return True
    # Optional known anchors (running heads/footers without page numbers).
    if any(anchor in text for anchor in KNOWN_HEADER_FOOTER_ANCHORS):
        return True
    # Cross-page repeated header/footer anchors supplied by the caller.
    anchors = tuple(known_header_anchors or ())
    norm_text = _normalize_anchor_text(text)
    if any(_normalize_anchor_text(anchor) == norm_text for anchor in anchors):
        return True
    return False


def extract_lines(
    page: fitz.Page,
    known_header_anchors: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Extract native dict lines from one PyMuPDF page.

    Returns a page-container dict with ``reliable`` and ``lines``.
    Lines are normalized to 0-1000 and carry plan metadata. Raw bboxes are
    mapped through cropbox/rotation into the output page coordinate system.
    """
    rect = fitz.Rect(page.rect)
    raw = page.get_text("dict")
    raw_lines: list[dict[str, Any]] = []
    transform_failed = False
    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = _line_text(line)
            if not text.strip():
                continue
            bbox = line.get("bbox")
            mapped = _map_bbox_to_output(bbox, page)
            if mapped is None:
                transform_failed = True
                continue
            norm = normalize_coords(mapped, rect)
            if norm is None:
                continue
            raw_lines.append({"text": text, "bbox": list(mapped), "box": norm})

    if not raw_lines:
        return {
            "reliable": False,
            "lines": [],
            "garble_ratio": garble_ratio(""),
            "rotation": page.rotation,
            "warnings": ["no_lines"] + (["transform_failed"] if transform_failed else []),
        }

    _assign_columns(raw_lines, rect.width)
    for line in raw_lines:
        line["header_footer"] = _is_header_footer(
            line, rect.height, known_header_anchors=known_header_anchors
        )
        line["source"] = "embedded"
        line["mode"] = "aligned"  # align engine may downgrade to fallback later.
        line["cell_row"] = None
        line["cell_col"] = None
        line["subline_index"] = 0
        line["order_exempt"] = False
    _assign_cells(raw_lines)
    for line in raw_lines:
        line.pop("_row_center", None)

    # Deterministic reading order: column first, then y, then x.
    raw_lines.sort(
        key=lambda line: (
            line.get("column", 0),
            (line["bbox"][1] + line["bbox"][3]) / 2.0,
            (line["bbox"][0] + line["bbox"][2]) / 2.0,
        )
    )
    for order, line in enumerate(raw_lines):
        line["line_id"] = f"L{order:04d}"
        line["order"] = order

    text = " ".join(line["text"] for line in raw_lines)
    gr = garble_ratio(text)
    warnings: list[str] = []
    if transform_failed:
        warnings.append("transform_failed: could not map one or more bboxes to output coordinates")
    if page.rotation:
        warnings.append(
            "rotation=%d: mapped via rotation_matrix + cropbox" % page.rotation
        )
    if gr > GARBLE_THRESHOLD_DEFAULT:
        warnings.append("garble_ratio=%.4f>%.2f fallback recommended" % (gr, GARBLE_THRESHOLD_DEFAULT))
    return {
        "reliable": bool(raw_lines) and not transform_failed,
        "lines": raw_lines,
        "garble_ratio": gr,
        "rotation": page.rotation,
        "warnings": warnings,
    }


def extract_page_geometry(page: fitz.Page) -> dict[str, Any]:
    """Public alias used by make_searchable / geom_align."""
    return extract_lines(page)


def extract_words_for_debug(page: fitz.Page) -> list[dict[str, Any]]:
    return extract_words(page)
