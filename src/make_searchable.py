from __future__ import annotations

"""Build a raster-backed PDF with an invisible searchable text layer.

Implements the reviewed text-layer alignment plan:
- old public/CLI call shape preserved;
- geo_source mode matrix auto/external/embedded/fallback_only;
- embedded geometry via geom_extract + geom_align;
- mixed-font segmented insertion with real baseline and subline handling;
- fallback capacity flow as a single layer (no 0.5pt duplicate band);
- align_out boxes.json + align_report.json artifacts.
"""

import argparse
import difflib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import fitz

DEFAULT_DPI = 200
# CJK font routing: include CJK unified ideographs plus CJK punctuation/fullwidth
# forms and the middle dot. The built-in china-s font renders these exactly,
# while Arial maps many of them to NUL (breaking zero-loss extraction).
CJK_RE = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef\u00b7]"
)
NON_CJK_RE = re.compile(
    r"[^\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef\u00b7]+"
)
# IPA and related modifier/combining marks must use IPA_FONT_PATH (Segoe UI);
# china-s drops many of these glyphs, and Arial maps U+0259 to Cyrillic U+04D9.
# The ranges cover Latin-1/Latin Extended letters (æ ç ð ø œ ŋ þ ß …), IPA
# extensions, combining marks, Greek letters used in phonetics (β θ), and
# U+2026 (china-s maps it to U+22EF while Segoe UI preserves it). Everything
# else non-ASCII (CJK, CJK punctuation, symbols such as ∶①②③ⅡⅢ) stays on
# china-s.
IPA_RE = re.compile(
    r"[\u00c0-\u024f\u0250-\u02af\u02b0-\u02ff\u0300-\u036f\u0370-\u03ff\u1d00-\u1d7f\u1d80-\u1dbf\u2026]"
)
# Symbols that china-s drops but Segoe UI Symbol preserves: Latin-1 punctuation
# (² ¬ ± ° …), general punctuation (quotes, daggers, ellipsis), primes, super/
# subscripts, letterlike symbols, arrows, mathematical operators, dingbats.
SYMBOL_RE = re.compile(
    r"[\u00a0-\u00bf\u2010-\u2027\u2030-\u205e\u2070-\u209f\u2100-\u214f\u2190-\u21ff\u2200-\u22ff\u2700-\u27bf\u2b00-\u2bff]"
)


def _is_ipa(char: str) -> bool:
    return bool(IPA_RE.match(char))
# Segoe UI preserves IPA glyphs exactly (Arial maps U+0259 schwa to U+04D9
# Cyrillic schwa, and china-s does not render most IPA marks).
IPA_FONT_PATH = Path(r"C:\Windows\Fonts\segoeui.ttf")
# Segoe UI Symbol preserves mathematical/dingbat/superscript glyphs that both
# china-s and Segoe UI drop (∅ ✘ ‡ ² ¬ ₀ …).
SEGOE_SYM_FONT_PATH = Path(r"C:\Windows\Fonts\seguisym.ttf")

try:
    from .geom_extract import (
        extract_lines as _extract_lines,
        extract_repeated_header_anchors as _extract_repeated_header_anchors,
        garble_ratio as _garble_ratio,
    )
    from .geom_align import (
        ALIGNED_PAGE_THRESHOLD,
        MAX_CENTER_DEVIATION,
        MIN_FONT_PT,
        SIMILARITY_THRESHOLD,
        align_page as _align_page,
        normalize_md as _as_text_normalized,
    )
except ImportError:  # pragma: no cover - direct script execution path
    from geom_extract import (
        extract_lines as _extract_lines,
        extract_repeated_header_anchors as _extract_repeated_header_anchors,
        garble_ratio as _garble_ratio,
    )
    from geom_align import (
        ALIGNED_PAGE_THRESHOLD,
        MAX_CENTER_DEVIATION,
        MIN_FONT_PT,
        SIMILARITY_THRESHOLD,
        align_page as _align_page,
        normalize_md as _as_text_normalized,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    value = str(value) if not isinstance(value, str) else value
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"^\s{0,3}#{1,6}\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*[-*+]\s+", "", value, flags=re.MULTILINE)
    return value.strip()


def _page_text(md_dict: Mapping[Any, Any], pno: int) -> str:
    for key in (pno, pno + 1, str(pno), str(pno + 1)):
        if key in md_dict:
            return _as_text(md_dict[key])
    raise KeyError(f"missing Markdown for page index {pno}")


def _page_boxes(boxes: Mapping[Any, Any] | None, pno: int) -> list[dict[str, Any]] | None:
    if boxes is None:
        return None
    if "lines" in boxes:
        value: Any = boxes
    else:
        value = None
        for key in (pno, pno + 1, str(pno), str(pno + 1)):
            if key in boxes:
                value = boxes[key]
                break
    if value is None:
        return None
    if isinstance(value, Mapping):
        if value.get("reliable") is False:
            return None
        value = value.get("lines")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    valid: list[dict[str, Any]] = []
    for line in value:
        if not isinstance(line, Mapping):
            continue
        text = _as_text(line.get("text", ""))
        box = line.get("box")
        if not text or not isinstance(box, Sequence) or len(box) != 4:
            continue
        try:
            coords = [float(item) for item in box]
        except (TypeError, ValueError):
            continue
        if not all(0 <= item <= 1000 for item in coords):
            continue
        x0, y0, x1, y1 = coords
        if x1 <= x0 or y1 <= y0:
            continue
        item: dict[str, Any] = {"text": text, "box": coords}
        # Preserve plan metadata (mode is the writer/verify routing authority).
        # is_table/table_cells carry MinerU table markup for the external
        # table cell gate; is_equation marks formula regions that the verifier
        # merges into one reading-order unit; the verify report reads them from
        # these align boxes.
        for key in (
            "source", "mode", "line_id", "column", "cell_row", "cell_col",
            "subline_index", "order", "order_exempt", "header_footer", "align",
            "is_table", "table_cells", "table_grid", "table_caption", "table_span_markup", "table_raw_html", "ocr_rows", "is_equation", "row_refined",
        ):
            if key in line:
                item[key] = line[key]
        valid.append(item)
    return valid or None


def _usable_boxes(lines: list[dict[str, Any]] | None, page_rect: fitz.Rect) -> bool:
    """A page is geometrically usable if it has real (non-fallback) geometry.

    ``header_footer_excluded`` lines carry real source boxes too, so they count
    as usable; only ``fallback_capacity`` lines do not.
    """
    if not lines or page_rect.width <= 0 or page_rect.height <= 0:
        return False
    return any(line.get("mode") in (None, "aligned", "header_footer_excluded") for line in lines)


def _partition_lines(lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split boxes-v2 lines into writer routes: aligned / header_footer / fallback."""
    aligned = [ln for ln in lines if ln.get("mode") in (None, "aligned")]
    header_footer = [ln for ln in lines if ln.get("mode") == "header_footer_excluded"]
    fallback = [ln for ln in lines if ln.get("mode") == "fallback_capacity"]
    return aligned, header_footer, fallback


def _is_symbol(ch: str) -> bool:
    return bool(SYMBOL_RE.fullmatch(ch))


def _font_for_line(text: str) -> str:
    """Font for a whole line/segment; per-character helper is _font_for_char."""
    if any(_is_ipa(ch) for ch in text) and IPA_FONT_PATH.is_file():
        return "ocripa"
    if any(_is_symbol(ch) for ch in text) and SEGOE_SYM_FONT_PATH.is_file():
        return "ocrisym"
    if any(ord(ch) > 127 for ch in text):
        return "china-s"
    return "helv"


def _font_for_char(ch: str) -> str:
    if _is_ipa(ch) and IPA_FONT_PATH.is_file():
        return "ocripa"
    if _is_symbol(ch) and SEGOE_SYM_FONT_PATH.is_file():
        return "ocrisym"
    if ord(ch) > 127:
        return "china-s"
    return "helv"


def _font_width(font_name: str, text: str, fontsize: float) -> float:
    if font_name == "ocripa" and IPA_FONT_PATH.is_file():
        return fitz.Font(fontfile=str(IPA_FONT_PATH)).text_length(text, fontsize=fontsize)
    if font_name == "ocrisym" and SEGOE_SYM_FONT_PATH.is_file():
        return fitz.Font(fontfile=str(SEGOE_SYM_FONT_PATH)).text_length(text, fontsize=fontsize)
    return fitz.Font(fontname=font_name).text_length(text, fontsize=fontsize)


def _center_baseline_offset(font_name: str, fontsize: float) -> float:
    """Baseline offset that puts a glyph's bbox center at the given y.

    PyMuPDF's glyph bbox center sits at ``baseline - (ascender + descender)/2 *
    fontsize`` (descender is negative). Using real font metrics instead of the
    old 0.35 heuristic keeps small annotation glyphs (A/B/C/D labels) centered
    in their boxes, so physical reading order matches box order even when label
    and caption fonts differ in size.
    """
    try:
        if font_name == "ocripa" and IPA_FONT_PATH.is_file():
            font = fitz.Font(fontfile=str(IPA_FONT_PATH))
        elif font_name == "ocrisym" and SEGOE_SYM_FONT_PATH.is_file():
            font = fitz.Font(fontfile=str(SEGOE_SYM_FONT_PATH))
        else:
            font = fitz.Font(fontname=font_name)
        asc = float(font.ascender or 0.8)
        desc = float(font.descender or -0.2)
    except Exception:
        asc, desc = 0.8, -0.2
    return (asc + desc) / 2.0 * float(fontsize)


def _font_segments(text: str) -> list[tuple[str, str]]:
    """Split a mixed line into font-runnable segments.

    Per-character routing: IPA/modifier marks -> Arial (``ocripa``), all other
    non-ASCII (CJK, CJK punctuation, symbols, accented Latin) -> ``china-s``,
    ASCII -> ``helv``. This keeps ASCII hyphens away from Arial, which extracts
    ``-`` as U+00AD, and keeps CJK punctuation/symbols away from Arial, which
    maps many of them to NUL.
    """
    if not text:
        return []
    segments: list[tuple[str, str]] = []
    current_font: str | None = None
    current_chars: list[str] = []
    for ch in text:
        font = _font_for_char(ch)
        if font == current_font:
            current_chars.append(ch)
        else:
            if current_chars:
                segments.append((current_font, "".join(current_chars)))
            current_font = font
            current_chars = [ch]
    if current_chars:
        segments.append((current_font, "".join(current_chars)))
    return segments


def _measured_width(text: str, fontsize: float) -> tuple[float, list[tuple[str, str]]]:
    segments = _font_segments(text)
    total = 0.0
    for font, seg in segments:
        total += _font_width(font, seg, fontsize)
    return total, segments


def _fitz_font(font_name: str) -> "fitz.Font":
    """fitz.Font object for a layout font name (mirrors _font_width routing)."""
    if font_name == "ocripa" and IPA_FONT_PATH.is_file():
        return fitz.Font(fontfile=str(IPA_FONT_PATH))
    if font_name == "ocrisym" and SEGOE_SYM_FONT_PATH.is_file():
        return fitz.Font(fontfile=str(SEGOE_SYM_FONT_PATH))
    return fitz.Font(fontname=font_name)


def _insert_scaled(
    page: fitz.Page,
    start_x: float,
    y_baseline: float,
    segments: list[tuple[str, str]],
    fontsize: float,
    scale: float,
) -> None:
    """Insert pre-split font segments with a horizontal-only stretch.

    The run is laid out from x=0 and then mapped by ``Matrix(scale, 0, 0, 1,
    start_x, 0)``, so it begins exactly at ``start_x`` and spans
    ``scale * natural_width`` while keeping its height. This is the PyMuPDF
    equivalent of Umi-OCR's ReportLab ``setHorizScale`` box fitting: the
    invisible layer covers the same horizontal extent as the printed glyphs it
    transcribes, which is what makes selection/copy highlighting line up.
    """
    writer = fitz.TextWriter(page.rect)
    cursor = 0.0
    for font_name, seg in segments:
        if not seg.strip():
            continue
        writer.append(
            fitz.Point(cursor, y_baseline),
            seg,
            font=_fitz_font(font_name),
            fontsize=max(0.5, float(fontsize)),
        )
        cursor += _font_width(font_name, seg, fontsize)
    morph = (
        fitz.Point(0.0, 0.0),
        fitz.Matrix(float(scale), 0.0, 0.0, 1.0, float(start_x), 0.0),
    )
    writer.write_text(page, render_mode=3, morph=morph, overlay=True)


def _insert_segmented(
    page: fitz.Page,
    x_anchor: float,
    y_baseline: float,
    text: str,
    fontsize: float,
    *,
    align: str = "left",
    rotate: float = 0.0,
    scale: float = 1.0,
) -> None:
    """Insert one logical line as multiple font segments using the layout formula.

    ``start_x = anchor + sum(previous segment widths)``; center/right shift the
    whole line first. With ``rotate=90`` the line runs bottom-to-top (the
    typical orientation of a rotated y-axis label): segments stack upward from
    ``y_baseline`` at a fixed ``x_anchor``.

    ``scale`` applies a horizontal-only stretch (Umi-OCR style box fitting):
    the line is laid out from its left edge and then mapped through
    ``Matrix(scale, 0, 0, 1, start, 0)``, so the glyph run spans exactly
    ``scale * natural_width`` without changing its height. Search, selection
    and copy still work because the text operators keep their order.
    """
    if not text:
        return
    width, segments = _measured_width(text, fontsize)
    if rotate:
        cursor_y = y_baseline
        for font, seg in segments:
            # Skip whitespace-only segments (e.g. "\n" inside a MinerU block).
            # PyMuPDF raises on insert_text of an effectively empty string.
            if not seg.strip():
                continue
            if font == "ocripa":
                page.insert_font(fontname=font, fontfile=str(IPA_FONT_PATH))
            elif font == "ocrisym":
                page.insert_font(fontname=font, fontfile=str(SEGOE_SYM_FONT_PATH))
            page.insert_text(
                fitz.Point(x_anchor, cursor_y),
                seg,
                fontname=font,
                fontsize=max(0.5, float(fontsize)),
                rotate=rotate,
                render_mode=3,
                overlay=True,
            )
            cursor_y -= _font_width(font, seg, fontsize)
        return
    start = x_anchor
    if align == "center":
        start = x_anchor - width / 2.0
    elif align == "right":
        start = x_anchor - width
    if abs(scale - 1.0) > 0.01 and width > 0.0:
        _insert_scaled(page, start, y_baseline, segments, fontsize, scale)
        return
    cursor = start
    for font, seg in segments:
        # Skip whitespace-only segments (e.g. "\n" inside a MinerU block).
        # PyMuPDF raises on insert_text of an effectively empty string.
        if not seg.strip():
            continue
        if font == "ocripa":
            page.insert_font(fontname=font, fontfile=str(IPA_FONT_PATH))
        elif font == "ocrisym":
            page.insert_font(fontname=font, fontfile=str(SEGOE_SYM_FONT_PATH))
        page.insert_text(
            fitz.Point(cursor, y_baseline),
            seg,
            fontname=font,
            fontsize=max(0.5, float(fontsize)),
            render_mode=3,
            overlay=True,
        )
        cursor += _font_width(font, seg, fontsize)


def _insert_line(page: fitz.Page, point: fitz.Point, text: str, fontsize: float) -> None:
    """Compatibility wrapper: single-font insertion for existing callers/tests."""
    _insert_segmented(page, point.x, point.y, text, fontsize, align="left")


def _wrap_line(text: str, fontsize: float, max_width: float) -> list[str]:
    """Wrap by measured glyph width for a single fallback line."""
    if not text:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_width = 0.0
    for char in text:
        char_width = _font_width(_font_for_line(char), char, fontsize)
        if current and current_width + char_width > max_width:
            chunks.append("".join(current))
            current = []
            current_width = 0.0
        current.append(char)
        current_width += char_width
    if current:
        chunks.append("".join(current))
    return chunks or [text]


def _insert_fallback_capacity(page: fitz.Page, text: str, fontsize: float = 8.0) -> None:
    """Single-layer fallback capacity flow (no duplicate 0.5pt hidden band)."""
    logical_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not logical_lines:
        return
    width, height = page.rect.width, page.rect.height
    margin_x = min(18.0, width * 0.04)
    max_width = max(2.0, width - 2 * margin_x)
    distributed: list[str] = []
    for line in logical_lines:
        distributed.extend(_wrap_line(line, fontsize, max_width))
    top = min(height * 0.08, height - 2.0)
    bottom = max(top + 2.0, height * 0.94)
    step = (bottom - top) / max(1, len(distributed))
    actual_fontsize = max(1.0, min(fontsize, step * 0.55))
    for index, line in enumerate(distributed):
        y = top + (index + 0.72) * step
        _insert_segmented(page, margin_x, y, line, actual_fontsize, align="left")


def _compact(text: str) -> str:
    """Canonical form for block/line text comparison: NFC, no whitespace."""
    return "".join(ch for ch in unicodedata.normalize("NFC", str(text)) if not ch.isspace())


def _embedded_rows(page: fitz.Page) -> list[dict[str, Any]]:
    """Native embedded text rows of the SOURCE page, normalized 0-1000.

    MinerU external boxes are block-level (one bbox may cover several visual
    rows), while the source PDF's own text layer is row-level. Returning the
    row boxes here lets the writer map block text onto true visual rows, so
    the invisible layer sits exactly on the scanned glyphs instead of being
    slot-distributed inside a coarse block box (the page-80 misalignment).
    Header/footer rows are excluded: MinerU blocks carry ``header_footer``
    flags and are written separately, so including page-number/caption rows
    here would steal their text.
    """
    try:
        from geom_extract import _is_header_footer as _embedded_is_hf
        from geom_extract import _line_text as _embedded_line_text
        from geom_extract import _map_bbox_to_output as _map_embedded_bbox
        from geom_extract import normalize_coords as _normalize_embedded
    except ImportError:
        from .geom_extract import _is_header_footer as _embedded_is_hf
        from .geom_extract import _line_text as _embedded_line_text
        from .geom_extract import _map_bbox_to_output as _map_embedded_bbox
        from .geom_extract import normalize_coords as _normalize_embedded
    rect = fitz.Rect(page.rect)
    rows: list[dict[str, Any]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = _embedded_line_text(line)
            if not text.strip():
                continue
            mapped = _map_embedded_bbox(line.get("bbox"), page)
            if mapped is None:
                continue
            norm = _normalize_embedded(mapped, rect)
            if norm is None:
                continue
            probe = {"text": text, "bbox": list(mapped)}
            try:
                if _embedded_is_hf(probe, rect.height):
                    continue
            except Exception:
                pass
            rows.append({"text": text, "box": norm})
    rows.sort(key=lambda r: (((r["box"][1] + r["box"][3]) / 2.0), (r["box"][0] + r["box"][2]) / 2.0))
    return rows


def _transfer_spacing(block_slice: str, embedded_text: str) -> str:
    """Re-insert the embedded row's spacing into a block slice.

    The block text is authoritative for content, but MinerU drops inter-word
    spaces (``只有10毫秒`` vs the visual ``只有 10毫秒``). When the compact
    forms match exactly, the embedded row's space positions are trustworthy,
    so they are transferred (whitespace runs collapse to one space, ends
    stripped). On any content disagreement the block slice is kept verbatim
    to protect zero-loss. Whitespace never affects the gates (multiset /
    canonical both ignore it), only search fidelity.
    """
    if _compact(embedded_text) != block_slice:
        return block_slice
    out: list[str] = []
    j = 0
    for ch in embedded_text:
        if ch.isspace():
            if out and not out[-1].isspace():
                out.append(" ")
        else:
            out.append(block_slice[j])
            j += 1
    return "".join(out).strip()


def map_block_to_rows(
    text: str,
    rows: list[dict[str, Any]],
    *,
    tol: float = 15.0,
    min_ratio: float = 0.6,
) -> list[dict[str, Any]] | None:
    """Map one MinerU block text onto the embedded rows inside its y-band.

    ``rows`` must be y-sorted (``_embedded_rows`` output). The block's rows are
    those whose y-center falls inside ``[block_y0 - tol, block_y1 + tol]``.
    Block characters are dealt out row by row: each row takes as many compact
    characters as its own embedded text has (the row widths already encode the
    true visual line breaks, including short tail rows), and any leftover
    characters merge into the last row. Each fragment additionally inherits
    the embedded row's spacing via ``_transfer_spacing``. Returns ``None``
    when the band has no rows or the block/rows texts do not resemble each
    other (``min_ratio``), so the caller can fall back to the old
    slot-distribution path.
    """
    band = [r for r in rows if r["box"][1] >= 0]
    if not band:
        return None
    compact = _compact(text)
    if not compact:
        return None
    joined = _compact("".join(r["text"] for r in band))
    if not joined:
        return None
    if difflib.SequenceMatcher(None, compact, joined).ratio() < min_ratio:
        return None
    out: list[dict[str, Any]] = []
    cursor = 0
    # The block text is authoritative (MinerU content_list), the rows only
    # donate geometry: MinerU's own ocr row boxes (``ocr_rows``) already ARE
    # the visual line breaks including short tails, so slice the block text
    # proportionally to row widths — each row takes round(its width share of
    # the remaining chars), leftovers merge into the last row. Embedded rows
    # (fallback path) additionally donate spacing via ``_transfer_spacing``.
    widths = [max(1.0, r["box"][2] - r["box"][0]) for r in band]
    total_w = sum(widths)
    counts = None
    if not any("text" in r and str(r.get("text") or "") for r in band):
        counts = [max(1, round(w / total_w * len(compact))) for w in widths]
    else:
        counts = [sum(1 for ch in str(r.get("text", "")) if not ch.isspace()) for r in band]
    for idx, row in enumerate(band):
        if idx < len(band) - 1:
            take = min(counts[idx], len(compact) - cursor)
            chunk = compact[cursor:cursor + take]
            cursor += take
        else:
            chunk = compact[cursor:]
            cursor = len(compact)
        if not chunk:
            continue
        frag_text = _transfer_spacing(chunk, str(row.get("text", ""))) if row.get("text") else chunk
        out.append({"text": frag_text, "box": list(row["box"])})
    return out or None


def _cluster_columns(band: list[dict[str, Any]], gap: float = 40.0) -> list[list[float]]:
    """Cluster band fragments into column x-ranges (left to right).

    Assignment is by x0 gaps: sorted fragment x0s are cut wherever the gap
    exceeds ``gap`` (default 40 normalized units: on the measured page-99
    table, intra-column x0 jitter tops out at ~30 — a narrow ASCII cell
    fragment starting 30 right of a wide CJK fragment in the same column —
    while true inter-column gaps start at ~42). Each column's ``[x0, x1]``
    range is the union of its member fragments' x extents.
    """
    if not band:
        return []
    # Column edges from per-ROW structure, not global x0 gaps. For each
    # y-cluster (one visual table row), sort its fragments by x0 and record
    # every inter-fragment midpoint as a candidate edge (no gap threshold:
    # garbled shards like ``雪``/``￥`` split cells with small gaps, so a
    # threshold would drop the true edge with them). A REAL column edge is a
    # candidate shared by MULTIPLE DATA rows (≥2): the header row's wide
    # fragments (``唇音舌尖音`` spanning two columns, ``舌尖后音舌尖前音``
    # spanning two more) vote phantom mid-edges that must not fuse with real
    # ones, so cluster 0 (the topmost = header row) never votes. Likewise a
    # lone single-fragment cluster (rowspan group header like ``发音方法``)
    # casts no vote. Word-internal splits (``塞擦音送``/``气``) happen once
    # and die in the vote. Candidate edges group within 40 units; the final
    # edges are group means.
    clusters: list[list[dict[str, Any]]] = []
    for r in sorted(band, key=lambda f: (f["box"][1] + f["box"][3]) / 2.0):
        yc = (r["box"][1] + r["box"][3]) / 2.0
        if clusters:
            prev_yc = sum((c["box"][1] + c["box"][3]) / 2.0 for c in clusters[-1]) / len(clusters[-1])
            if abs(yc - prev_yc) <= 12.0:
                clusters[-1].append(r)
                continue
        clusters.append([r])
    votes: list[float] = []
    for ci, cl in enumerate(clusters):
        if ci == 0 or len(cl) < 2:
            continue
        frags = sorted(cl, key=lambda f: f["box"][0])
        for a, b in zip(frags, frags[1:]):
            # A fragment SPANNING a candidate edge vetoes it: e.g. the header
            # row's ``唇音舌尖音`` [342.7,516.4] covers the 唇音|舌尖音 edge
            # (~x=429), and ``舌尖后音舌尖前音`` [715.9,915.4] covers the
            # 舌尖后音|舌尖前音 edge (~x=815) — but the header row never votes
            # anyway, so only DATA-row spans veto (a two-column CJK shard
            # inside one visual row means no column edge runs there).
            mid = (a["box"][2] + b["box"][0]) / 2.0
            if any(
                f is not a and f is not b
                and f["box"][0] < mid < f["box"][2]
                and (f["box"][2] - f["box"][0]) >= 60.0
                for f in cl
            ):
                continue
            votes.append(mid)
    if not votes:
        return []
    votes.sort()
    groups: list[list[float]] = []
    for v in votes:
        if groups and v - groups[-1][-1] <= 40.0:
            groups[-1].append(v)
        else:
            groups.append([v])
    # A singleton group survives only if it is FAR (>40) from every kept
    # edge: a real column edge seen in just one row (e.g. the last narrow
    # data column whose shards merged elsewhere) must not be lost, while a
    # singleton adjacent to a kept edge is its own echo and dies.
    multi = [sum(g) / len(g) for g in groups if len(g) >= 2]
    singles = [sum(g) / len(g) for g in groups if len(g) == 1]
    edges = sorted(multi + [s for s in singles if all(abs(s - m) > 40.0 for m in multi)])
    if not edges:
        return []
    bounds = [0.0] + edges + [1000.0]
    cols: list[list[float]] = []
    for lo, hi in zip(bounds, bounds[1:]):
        # A fragment belongs to a bucket only if it does NOT span either
        # edge: a two-column shard (``唇音舌尖音`` [342.7,516.4] over edge
        # ~429) otherwise pollutes both neighbours' x-ranges.
        members = [
            r for r in band
            if lo <= (r["box"][0] + r["box"][2]) / 2.0 < hi
            and not (r["box"][0] < lo < r["box"][2] and (r["box"][2] - r["box"][0]) >= 60.0)
            and not (r["box"][0] < hi < r["box"][2] and (r["box"][2] - r["box"][0]) >= 60.0)
        ]
        if members:
            cols.append([min(r["box"][0] for r in members), max(r["box"][2] for r in members)])
    # Merge a column fully CONTAINED in ANY other column (a shard bucket like
    # [356.8,401.4] inside [342.7,516.4], split only because two nearby edge
    # estimates fused into neighbours). Containment means one logical column.
    # Partial overlaps are kept: genuinely different columns whose ranges
    # interleave across rows.
    absorbed = [False] * len(cols)
    for i, c in enumerate(cols):
        for j, d in enumerate(cols):
            if i == j or absorbed[i]:
                continue
            if d[0] <= c[0] and c[1] <= d[1] and (d[0] < c[0] or c[1] < d[1]):
                absorbed[i] = True
                break
    return [c for c, drop in zip(cols, absorbed) if not drop]


def _is_group_header_cluster(cluster: list[dict[str, Any]], col0_x1: float) -> bool:
    """True for a rowspan group header (e.g. ``发音方法``): a single short
    all-CJK fragment confined to the row-header column, with no ASCII cells.
    Data rows always span several columns, so this never misfires on them; a
    misfire would only skip one grid row and fail the count check below,
    falling back to the whole-block path (never silent corruption)."""
    if len(cluster) != 1:
        return False
    frag = cluster[0]
    if frag["box"][2] > col0_x1:
        return False
    compact = _compact(str(frag.get("text", "")))
    return bool(compact) and len(compact) <= 8 and all(ord(ch) > 127 for ch in compact)


def _table_cell_lines(
    line: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    tol: float = 15.0,
) -> list[dict[str, Any]] | None:
    """Split one MinerU table block into one line per non-empty CELL.

    Geometry comes from the source page's native layer (which fragments a
    visual table row column by column but garbles cell texts like ``p`` into
    ``雪``); text comes from the MinerU grid (authoritative, LaTeX-cleaned).
    Each grid row maps in order onto a y-cluster of fragments (rowspan group
    headers skipped); each cell maps by column index onto a column x-range
    (7-wide rows -> cols 0-6, 6-wide rows with a missing row header ->
    cols 1-6). A cell's x-extent is the union of that row-cluster's fragments
    in its column when that union is sane (<= 1.2x the column width),
    otherwise the full column width; y-extent is the row cluster's y union.
    Garbled fragments never contribute TEXT, only geometry. Any structural
    surprise (no band, column/grid count mismatch, empty result) returns
    ``None`` so the caller keeps the fallback whole-block path.
    """
    grid = line.get("table_grid")
    if not grid:
        return None
    # Structural self-check BEFORE any geometry: when the HTML had hidden
    # span markup, recount the <td>/<th> cells per <tr> from the stored raw
    # HTML and require the expanded grid width to cover every row's spanned
    # width (sum of colspans). A short grid means a rowspan/colspan parse
    # error (e.g. an unhandled <th> or attribute order) — return None
    # (fallback whole-block) rather than mapping cells onto wrong columns.
    if line.get("table_span_markup") and line.get("table_raw_html"):
        try:
            import re as _re

            expected_widths = []
            for row_html in _re.findall(r"<tr>(.*?)</tr>", str(line["table_raw_html"]), _re.S):
                w = 0
                for m in _re.finditer(r"<t[dh](.*?)>", row_html, _re.S):
                    mc = _re.search(r"colspan\s*=\s*\"?(\d+)", m.group(1))
                    w += int(mc.group(1)) if mc else 1
                expected_widths.append(w)
            need = max(expected_widths) if expected_widths else 0
            if not expected_widths or any(len(r) < w for r, w in zip(grid, expected_widths)):
                return None
            if any(len(r) != need for r in grid):
                return None
        except Exception:
            return None
    if not any(any(c.strip() for c in row) for row in grid):
        return None
    try:
        y0n, y1n = float(line["box"][1]), float(line["box"][3])
    except (TypeError, ValueError, KeyError):
        return None
    band = sorted(
        (r for r in rows if y0n - tol <= (r["box"][1] + r["box"][3]) / 2.0 <= y1n + tol),
        key=lambda r: (r["box"][1] + r["box"][3]) / 2.0,
    )
    if not band:
        return None
    clusters: list[list[dict[str, Any]]] = []
    for r in band:
        yc = (r["box"][1] + r["box"][3]) / 2.0
        if clusters:
            prev_yc = sum((c["box"][1] + c["box"][3]) / 2.0 for c in clusters[-1]) / len(clusters[-1])
            if abs(yc - prev_yc) <= 12.0:
                clusters[-1].append(r)
                continue
        clusters.append([r])
    cols = _cluster_columns(band)
    if not cols:
        return None
    col0_x1 = cols[0][1]
    data_clusters = [c for c in clusters if not _is_group_header_cluster(c, col0_x1)]
    # The header cluster (e.g. 表头 row) is kept: only group headers are out.
    # The caption cluster (``表 3-3 …`` above the table, y far above the
    # header row) is NOT a table row either: drop a leading cluster whose
    # fragments all end above the next cluster's top. Both drops must hold
    # for the count check below to be meaningful.
    if (
        len(data_clusters) > len(grid)
        and clusters
        and data_clusters
        and clusters[0] is data_clusters[0]
        and len(data_clusters) > 1
        and max(c["box"][3] for c in data_clusters[0]) <= min(c["box"][1] for c in data_clusters[1])
    ):
        data_clusters = data_clusters[1:]
    if len(data_clusters) != len(grid):
        return None
    out: list[dict[str, Any]] = []
    base_id = str(line.get("line_id", "M"))
    ncols = max(len(g) for g in grid)
    # Pad missing TRAILING columns by splitting the last detected column:
    # the vote can miss the final narrow edge (its shards merge into the
    # neighbour bucket), leaving cols[-1] spanning two logical columns
    # (e.g. [715.9,915.4] holding 舌尖后音+舌尖前音). Split at the midpoint
    # of the widest detected column — the merged one is always the widest
    # (it holds two columns' fragments). If columns are already complete,
    # this is a no-op.
    while len(cols) < ncols:
        widths = [c[1] - c[0] for c in cols]
        j = max(range(len(cols)), key=lambda i: widths[i])
        mid = (cols[j][0] + cols[j][1]) / 2.0
        cols[j:j + 1] = [[cols[j][0], mid], [mid, cols[j][1]]]
    if len(cols) != ncols:
        return None
    # Column roles: strict 1:1 grid-row -> detected-columns. The old code
    # RIGHT-aligned a short header row and LEFT-aligned short data rows, but
    # any width mismatch means the column detector missed an edge — shifting
    # cells to "fit" then corrupts the whole table (p80: 7 cols vs 8 grid
    # cols put every cell one column left). Mismatch -> fallback below.
    # The table caption (e.g. ``表 3-3 …``) lives in the block text but NOT
    # in the grid: emit it as one line so zero-loss is kept. Prefer the
    # caption's own embedded row (it sits above the table band and is NOT in
    # ``clusters``); fall back to the first cluster's bbox.
    caption = ""
    for key in ("table_caption", "caption"):
        val = line.get(key)
        if isinstance(val, (list, tuple)):
            val = "\n".join(str(v) for v in val)
        if str(val or "").strip():
            caption = str(val).strip()
            break
    if caption:
        cap_box = None
        try:
            cy1 = min(c["box"][1] for c in data_clusters[0])
            above = [r for r in rows if (r["box"][1] + r["box"][3]) / 2.0 < cy1]
            if above:
                cap_row = max(above, key=lambda r: (r["box"][1] + r["box"][3]) / 2.0)
                cap_box = list(cap_row["box"])
        except Exception:
            cap_box = None
        if cap_box is None and clusters:
            cap_box = [
                min(c["box"][0] for c in clusters[0]),
                min(c["box"][1] for c in clusters[0]),
                max(c["box"][2] for c in clusters[0]),
                max(c["box"][3] for c in clusters[0]),
            ]
        if cap_box is not None:
            cap_child = dict(line)
            cap_child["text"] = caption
            cap_child["box"] = cap_box
            cap_child["line_id"] = f"{base_id}#caption"
            cap_child["row_refined"] = True
            out.append(cap_child)
    for gi, (gcells, cluster) in enumerate(zip(grid, data_clusters)):
        # Every grid row must cover every detected column 1:1. The old code
        # RIGHT-aligned a short header row (skipping col 0) and LEFT-aligned
        # short data rows — both silently shift cells when the column
        # detector misses an edge (7 cols detected vs 8 grid cols on p80:
        # every cell from c2 on sat one column left). A width mismatch is a
        # structural surprise -> fallback, never a shifted mapping.
        if len(gcells) != len(cols):
            return None
        col_map = list(range(len(cols)))
        # Row y-extent is the SHORT-fragment core: tall fragments (a merged
        # two-row header like ``塞音送气`` h=38.6, a tall garbled shard like
        # ``，`` h=38.4 — height > 1.5x the cluster's median height) are
        # EXCLUDED from the y math (their union/core otherwise stretches the
        # row over two visual rows: c4 union y=[227,266] vs true glyphs
        # ~[240,255]). Median center ± median SHORT height stays on glyphs;
        # all-short clusters are unaffected (nothing excluded).
        hs_all = sorted(c["box"][3] - c["box"][1] for c in cluster)
        med_h_all = hs_all[len(cluster) // 2]
        short = [c for c in cluster if (c["box"][3] - c["box"][1]) <= 1.5 * med_h_all] or cluster
        scys = sorted((c["box"][1] + c["box"][3]) / 2.0 for c in short)
        shs = sorted(c["box"][3] - c["box"][1] for c in short)
        med_cy = scys[len(short) // 2]
        med_h = shs[len(short) // 2]
        row_y0 = med_cy - med_h / 2.0
        row_y1 = med_cy + med_h / 2.0
        if row_y1 <= row_y0:
            row_y0 = min(c["box"][1] for c in short)
            row_y1 = max(c["box"][3] for c in short)
        # A span-expanded grid repeats one logical cell across ADJACENT
        # columns (colspan ``鼻音,鼻音``): emit it once, spanning the merged
        # column range, else zero-loss breaks (the block text carries one
        # copy). NON-adjacent repeats (e.g. ``s`` in two data columns) are
        # distinct visual cells and are KEPT. Rowspan repeats in the
        # row-header column (``塞音`` on both sub-rows) are one merged cell
        # visually: keep the FIRST copy only... but ONLY when the grid is
        # span-marked — an unspanned grid repeating col-0 text on two rows
        # would be two genuinely identical rows, and dropping one would
        # break zero-loss.
        span_marked = bool(line.get("table_span_markup"))
        dedup: list[tuple[str, list[int]]] = []
        for cell_text, ci in zip(gcells, col_map):
            cell_text = str(cell_text or "").strip()
            if not cell_text:
                continue
            if dedup and dedup[-1][0] == cell_text:
                dedup[-1][1].append(ci)
            elif span_marked and ci == 0 and any(t == cell_text for t, _ in dedup):
                continue
            else:
                dedup.append((cell_text, [ci]))
        for cell_text, cis in dedup:
            col_x0 = min(cols[ci][0] for ci in cis)
            col_x1 = max(cols[ci][1] for ci in cis)
            col_w = max(1.0, col_x1 - col_x0)
            ci0 = cis[0]
            # Per-cell x from SHORT fragments overlapping THIS column
            # (tall shards excluded same as the row math: ``，`` h=38.4
            # spanning two rows must not donate x to either). No short
            # member -> full column box (``l``/``r`` have no usable
            # fragments; the glyph sits at column center). Union snapped
            # to ≤1.2x col width.
            in_col = [
                c for c in short
                if c["box"][0] < col_x1 and c["box"][2] > col_x0
            ]
            if in_col:
                ux0 = min(c["box"][0] for c in in_col)
                ux1 = max(c["box"][2] for c in in_col)
                if ux1 > ux0 and (ux1 - ux0) <= 1.2 * col_w:
                    cell_x0, cell_x1 = max(ux0, col_x0 - col_w), min(ux1, col_x1 + col_w)
                else:
                    cell_x0, cell_x1 = col_x0, col_x1
            else:
                cell_x0, cell_x1 = col_x0, col_x1
            if cell_x1 <= cell_x0:
                cell_x0, cell_x1 = col_x0, col_x1
            child = dict(line)
            child["text"] = cell_text
            child["box"] = [cell_x0, row_y0, cell_x1, row_y1]
            child["align"] = "center"
            child["line_id"] = f"{base_id}#t{gi}c{ci0}"
            child["row_refined"] = True
            out.append(child)
    return out or None


def refine_external_lines(
    lines: list[dict[str, Any]],
    page: fitz.Page,
    *,
    tol: float = 15.0,
    min_ratio: float = 0.6,
) -> list[dict[str, Any]]:
    """Rewrite external MinerU block lines as row-level lines where possible.

    Text blocks prefer MinerU's own ``ocr_rows`` geometry (attached by the
    data builder; same frame as the block boxes, with true per-row x-ranges
    including short tails) via ``map_block_to_rows``; blocks without ocr
    rows fall back to the source page's embedded rows. Table blocks are
    split per cell via ``_table_cell_lines``. Equations, rotated labels and
    blocks that do not map are returned unchanged; mapped fragments carry
    ``row_refined=True``. Metadata (mode/line_id/order/flags) is inherited
    by every fragment so verify's exempt/metadata logic keeps working.
    """
    try:
        rows = _embedded_rows(page)
    except Exception:
        rows = []
    refined: list[dict[str, Any]] = []
    for line in lines:
        text = str(line.get("text", ""))
        box = line.get("box")
        if (
            not text.strip()
            or not isinstance(box, Sequence)
            or len(box) != 4
            or line.get("is_equation")
            or line.get("order_exempt")
            or _is_rotated_axis_label(text, float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))
        ):
            refined.append(line)
            continue
        if line.get("is_table"):
            try:
                table_frags = _table_cell_lines(line, rows, tol=tol)
            except Exception:
                table_frags = None
            if table_frags:
                refined.extend(table_frags)
            else:
                refined.append(line)
            continue
        ocr_band = line.get("ocr_rows")
        if ocr_band:
            band = [{"box": list(b)} for b in ocr_band]
        else:
            try:
                y0n, y1n = float(box[1]), float(box[3])
            except (TypeError, ValueError):
                refined.append(line)
                continue
            band = [
                r for r in rows
                if y0n - tol <= (r["box"][1] + r["box"][3]) / 2.0 <= y1n + tol
            ]
            if not band:
                refined.append(line)
                continue
        try:
            frags = map_block_to_rows(text, band, tol=tol, min_ratio=min_ratio)
        except Exception:
            frags = None
        if not frags:
            refined.append(line)
            continue
        base_id = str(line.get("line_id", "M"))
        for i, frag in enumerate(frags):
            child = dict(line)
            child["text"] = frag["text"]
            child["box"] = frag["box"]
            child["line_id"] = f"{base_id}#r{i}"
            child["row_refined"] = True
            refined.append(child)
    return refined


def _is_rotated_axis_label(text: str, box_width_norm: float, box_height_norm: float) -> bool:
    """True for a genuine rotated y-axis label (e.g. ``Frequency (Hz)``).

    Criteria are deliberately strict so horizontal figure captions like
    ``[t] [a]`` (wide box) and vertical CJK side text are never rotated:
    extremely narrow box (<40 normalized), tall (height > 2x width), short
    ASCII text without newlines.
    """
    t = text.strip()
    return bool(
        t
        and len(t) >= 6
        and len(t) <= 60
        and "\n" not in t
        and t.isascii()
        and box_width_norm < 40.0
        and box_height_norm > 2.0 * box_width_norm
    )


MIN_HSCALE = 0.5
MAX_HSCALE = 3.0
BOX_HEIGHT_TO_FONT = 1.0


def _fit_box_text(
    text: str,
    box_height: float,
    box_width: float,
    *,
    min_fontsize: float = MIN_FONT_PT,
) -> tuple[float, float]:
    """Umi-OCR's ``_calculateFontSize``: fit the glyph run to the box WIDTH.

    Ported from ``UmiOCR-data/py_src/ocr/output/output_pdf_layered.py``: start
    from the box height, walk the size down until the run fits the box width,
    walk it back up until it just exceeds it, then refine in 0.1pt steps. The
    glyph aspect ratio is never distorted, and a printed line whose ink is
    shorter than its em box still gets its true point size — fitting to the box
    HEIGHT alone (the old behaviour) undersized exactly those lines, which is
    why the cover title rendered far smaller than the print.

    Returns ``(fontsize, 1.0)``; the horizontal-stretch path stays available
    for other callers, but Umi-OCR never stretches.
    """
    if box_height > box_width:  # vertical text: measure along the long axis
        box_width, box_height = box_height, box_width
    limit = max(1.0, float(min_fontsize))
    # Umi-OCR leaves the upward walk unbounded, which is safe only because its
    # boxes come from an OCR detector and contain exactly the text in the box.
    # Our Markdown occasionally disagrees with the printed band, so cap the
    # size at 1.6x the ink height: real printed glyphs never exceed that
    # (Latin caps top out near 1.55x, CJK near 1.35x).
    ceiling = max(limit, float(box_height) * 1.6)
    fontsize = float(min(max(limit, round(float(box_height))), ceiling))
    measured, _segments = _measured_width(text, fontsize)
    if measured <= 0.0:
        return fontsize, 1.0
    while measured > box_width and fontsize > limit:
        fontsize = max(limit, fontsize - 1.0)
        measured, _segments = _measured_width(text, fontsize)
    guard = 0
    while measured < box_width and fontsize < ceiling:
        fontsize = min(ceiling, fontsize + 1.0)
        measured, _segments = _measured_width(text, fontsize)
        guard += 1
        if guard > 400:
            break
    while measured > box_width and fontsize > limit:
        fontsize = max(limit, fontsize - 0.1)
        measured, _segments = _measured_width(text, fontsize)
        if measured <= 0.0:
            break
    return fontsize, 1.0


def plan_boxed_lines(
    lines: list[dict[str, Any]],
    width: float,
    height: float,
) -> tuple[list[dict[str, Any]], int]:
    """Compute the physical-line placement plan for geometry lines.

    This is the single source of truth for how a boxes-v2 line is laid out
    (font scaling, wrapping into physical lines, per-line baselines). The writer
    (``_insert_boxed_lines``) and the verifier's expected reading order both use
    it, so the gate compares like-for-like: block-level boxes are expanded into
    the same line-level order the output PDF actually has.

    Returns ``(plan, overflow_count)`` where each plan entry carries the text,
    insertion coordinates (points), and the normalized center used for reading
    order.
    """
    plan: list[dict[str, Any]] = []
    overflow_count = 0
    for line in lines:
        x0n, y0n, x1n, y1n = (float(v) for v in line["box"])
        x0, y0, x1, y1 = (
            x0n * width / 1000.0,
            y0n * height / 1000.0,
            x1n * width / 1000.0,
            y1n * height / 1000.0,
        )
        box_height = max(1.0, y1 - y0)
        box_width = max(1.0, x1 - x0)
        text = str(line.get("text", ""))
        # Umi-OCR-style fitting: the font size comes from the box HEIGHT and
        # the horizontal extent is matched by a horizontal-only scale. Shrinking
        # the font until the text happens to fit (the old behaviour) is what
        # made the invisible layer's glyphs far smaller than the printed ones.
        fontsize, hscale = _fit_box_text(text, box_height, box_width)
        measured, _segments = _measured_width(text, fontsize)
        align = str(line.get("align", "left"))
        box_width_norm = x1n - x0n
        box_height_norm = y1n - y0n
        region = (x0n, y0n, x1n, y1n) if line.get("is_equation") else None

        # Genuine rotated y-axis labels: insert the text rotated 90 degrees
        # bottom-to-top, centered in the tall-narrow box. This prevents the
        # label from colliding with the horizontal axis label (page 109) and
        # keeps its physical bbox aligned with the box.
        if _is_rotated_axis_label(text, box_width_norm, box_height_norm):
            fontsize = max(1.0, min(box_height * 0.92, box_height * 0.78 + 1.0))
            fontsize = min(fontsize, box_width * 0.92)
            rotated_measured, _segments = _measured_width(text, fontsize)
            if rotated_measured > box_height:
                fontsize = max(MIN_FONT_PT, fontsize * box_height / rotated_measured)
            font_name = _font_for_line(text)
            rotated_width, _segments = _measured_width(text, fontsize)
            x_center = (x0n + x1n) / 2.0
            plan.append({
                "text": text,
                "x": (x0 + x1) / 2.0 + _center_baseline_offset(font_name, fontsize),
                "baseline": (y0 + y1) / 2.0 + rotated_width / 2.0,
                "fontsize": fontsize, "align": align, "rotate": 90,
                "y_center_norm": (y0n + y1n) / 2.0, "x_center_norm": x_center,
                "x0_norm": x0n,
                "equation_region": region,
            })
            continue

        # Split on embedded newlines FIRST (MinerU blocks often carry several
        # logical lines inside one block, e.g. "[i]\n[e]"), then wrap any line
        # that still does not fit after font scaling. This keeps zero-loss
        # intact (all chars are inserted) and prevents PyMuPDF's page-edge
        # truncation; it also makes the planner's line-level order match what
        # PyMuPDF actually renders for multi-line blocks.
        logical = [raw for raw in text.split("\n") if raw.strip()]
        if not logical:
            logical = [text]
        wrapped: list[str] = []
        for raw in logical:
            if _measured_width(raw, fontsize)[0] > box_width:
                wrapped.extend(_wrap_line(raw, fontsize, box_width))
            else:
                wrapped.append(raw)
        if len(wrapped) == 1 and _measured_width(wrapped[0], fontsize)[0] > box_width:
            overflow_count += 1

        x_center_norm = (x0n + x1n) / 2.0
        if len(wrapped) == 1:
            # Center the glyph vertically inside its box using real font metrics
            # (see _center_baseline_offset). This keeps small annotation glyphs
            # (A/B/C/D labels) at their box centers so the physical reading order
            # matches the box order even when label and caption font sizes differ.
            font_name = _font_for_line(wrapped[0])
            # Umi-OCR anchors the baseline at the box's bottom-left corner
            # (`point = fitz.Point(x0, y2)`), and for a tight detector/ink box
            # the box bottom IS the printed baseline. The old centre-anchored
            # baseline drifted by half the font height on every line.
            baseline = y1
            plan.append({
                "text": wrapped[0], "x": x0, "baseline": baseline,
                "fontsize": fontsize, "align": align, "scale": hscale,
                "y_center_norm": (y0n + y1n) / 2.0, "x_center_norm": x_center_norm,
                "x0_norm": x0n,
                "equation_region": region,
            })
        else:
            step = max(1.0, box_height / len(wrapped))
            actual_fontsize = max(1.0, min(fontsize, step * 0.75))
            for i, chunk in enumerate(wrapped):
                slot_center = y0 + (i + 0.5) * step
                chunk_font = _font_for_line(chunk)
                baseline = slot_center + _center_baseline_offset(chunk_font, actual_fontsize)
                plan.append({
                    "text": chunk, "x": x0, "baseline": baseline,
                    "fontsize": actual_fontsize, "align": align,
                    "y_center_norm": y0n + (i + 0.5) * (max(1.0, (y1n - y0n)) / len(wrapped)),
                    "x_center_norm": x_center_norm,
                    "x0_norm": x0n,
                    "equation_region": region,
                })
    return plan, overflow_count


def _insert_boxed_lines(page: fitz.Page, lines: list[dict[str, Any]], overflow_report: dict[str, Any] | None = None) -> None:
    """Insert aligned geometry lines by delegating to the shared layout planner.

    PyMuPDF's ``insert_text`` truncates text that extends beyond the physical page
    edge, so long paragraph-level boxes must be wrapped into several physical
    lines instead of being inserted as one over-wide string.
    """
    width, height = page.rect.width, page.rect.height
    plan, overflow_count = plan_boxed_lines(lines, width, height)
    if overflow_count and overflow_report is not None:
        overflow_report["overflow_expected"] = overflow_report.get("overflow_expected", 0) + overflow_count
    for entry in plan:
        _insert_segmented(
            page, entry["x"], entry["baseline"], entry["text"], entry["fontsize"],
            align=entry["align"], rotate=float(entry.get("rotate", 0.0)),
            scale=float(entry.get("scale", 1.0)),
        )


def _metrics_from_lines(
    text: str,
    lines: list[dict[str, Any]],
    page_index: int,
    reason: str,
    overflow_expected: int = 0,
    table_unreliable: bool = False,
) -> dict[str, Any]:
    """Compute align metrics from the actual page lines for ANY geo_source mode.

    External/fallback-only pages previously fell into a default that reported
    aligned_coverage=0 even when external aligned lines were present. This helper
    uses the same character-level accounting as the embedded align path.
    """
    normalized = _as_text_normalized(text)
    total_chars = sum(1 for ch in normalized if not ch.isspace())
    aligned_chars = sum(
        1 for ln in lines if ln.get("mode") in (None, "aligned") for ch in str(ln.get("text", "")) if not ch.isspace()
    )
    covered_chars = sum(
        1 for ln in lines for ch in str(ln.get("text", "")) if not ch.isspace()
    )
    aligned_coverage = (aligned_chars / total_chars) if total_chars else 1.0
    covered_coverage = (covered_chars / total_chars) if total_chars else 1.0
    mode_counts = {
        m: sum(1 for ln in lines if ln.get("mode") == m)
        for m in ("aligned", "fallback_capacity", "header_footer_excluded")
    }
    # 'reason' is a routing label, not necessarily a failure; keep failures explicit.
    if reason in ("fallback_only", "external_bad", "embedded_no_geom"):
        failure_reasons: dict[str, int] = {reason: 1}
    else:
        failure_reasons = {}
    return {
        "page_index": page_index,
        "total_non_ws_chars": total_chars,
        "aligned_chars": aligned_chars,
        "covered_chars": covered_chars,
        "aligned_coverage": round(aligned_coverage, 6),
        "covered_coverage": round(covered_coverage, 6),
        "page_aligned": aligned_coverage >= ALIGNED_PAGE_THRESHOLD,
        "table_unreliable": bool(table_unreliable),
        "similarities": [],
        "failure_reasons": failure_reasons,
        "mode_counts": mode_counts,
        "overflow_expected": int(overflow_expected),
    }


def _fallback_only_lines(text: str, page_index: int) -> list[dict[str, Any]]:
    """Create fallback_capacity metadata lines when no geometry is used at all."""
    normalized = _as_text_normalized(text)
    md_lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    lines: list[dict[str, Any]] = []
    total = max(1, len(md_lines))
    for i, ln in enumerate(md_lines):
        top = 50.0 + (i * (900.0 / total))
        lines.append({
            "text": ln,
            "box": [20.0, top, 980.0, min(1000.0, top + 20.0)],
            "source": "md",
            "mode": "fallback_capacity",
            "line_id": f"P{page_index:04d}F{i:04d}",
            "column": 0,
            "cell_row": None,
            "cell_col": None,
            "subline_index": 0,
            "order": i,
            "order_exempt": True,
            "header_footer": False,
            "align": "left",
        })
    return lines


def _split_text_by_widths(text: str, weights: list[float]) -> list[str]:
    """Split ``text`` into ``len(weights)`` chunks sized by relative weight.

    Weights are ``band_width / band_height`` — a rough count of how many glyphs
    a printed band holds, since a band of large type fits fewer characters than
    a band of small type of the same width. Used when Markdown collapsed
    several printed lines into one string: the invisible layer then still
    follows the printed line layout instead of parking one oversized line in
    the middle of the block.
    """
    if len(weights) <= 1:
        return [text]
    total = sum(w for w in weights if w > 0.0)
    if total <= 0.0:
        return [text]
    targets: list[float] = []
    acc = 0.0
    for w in weights[:-1]:
        acc += max(0.0, w)
        targets.append(acc / total)
    units_total = 0.0
    for ch in text:
        if not ch.isspace():
            units_total += 1.0 if ord(ch) > 0x2E80 else 0.5
    if units_total <= 0.0:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    acc = 0.0
    ti = 0
    armed = False
    for ch in text:
        cur.append(ch)
        if not ch.isspace():
            acc += 1.0 if ord(ch) > 0x2E80 else 0.5
        if ti < len(targets) and acc / units_total >= targets[ti]:
            # Only arm the cut here; the actual split waits for the next word
            # boundary so a run like "1999" or "XIANDAI" is never cut in half
            # (splitting mid-token makes that token unsearchable).
            armed = True
        if armed and ch.isspace():
            piece = "".join(cur).strip()
            if piece:
                chunks.append(piece)
                cur = []
                ti += 1
                armed = False
    tail = "".join(cur).strip()
    if tail:
        chunks.append(tail)
    return chunks


def _ink_aligned_lines(
    text: str,
    page: fitz.Page,
    pno: int,
) -> list[dict[str, Any]]:
    """Geometry lines derived from the raster ink profile of a scanned page.

    Used when neither external (MinerU) boxes nor embedded vector text exist.
    Printed line bands are detected on the bitmap and the page's Markdown lines
    are assigned to them in reading order; several Markdown lines landing in
    one band are stacked inside it so nothing is lost. This replaces the
    synthetic full-width strips that parked the invisible layer in the left
    margin while the print sat centred.
    """
    try:
        from .ink_layout import detect_normalized_boxes
    except ImportError:
        from ink_layout import detect_normalized_boxes
    try:
        boxes = detect_normalized_boxes(page)
    except Exception:
        return []
    md_lines = [ln.strip() for ln in _as_text_normalized(text).splitlines() if ln.strip()]
    if not boxes or not md_lines:
        return []
    total_boxes = len(boxes)
    total_lines = len(md_lines)
    # Adaptive height clamp: a band far taller than this page's typical band is
    # a merged block (figure, table rule, scan bleed), not one printed line.
    # Left alone it hands the fitted font that whole height — which is where the
    # 68pt "body text" on page 100 came from. Shrink it around its own centre so
    # the text stays on the page at a plausible size.
    heights: list[float] = []
    for raw in boxes:
        try:
            hh = float(raw[3]) - float(raw[1])
        except (TypeError, ValueError, IndexError):
            continue
        if hh > 0.0:
            heights.append(hh)
    heights.sort()
    median_h = heights[len(heights) // 2] if heights else 0.0
    max_h = median_h * 2.5
    lines: list[dict[str, Any]] = []
    order = 0
    for i, txt in enumerate(md_lines):
        lo = int(i * total_boxes / total_lines)
        hi = int((i + 1) * total_boxes / total_lines)
        if hi <= lo:
            hi = lo + 1
        group = boxes[lo:min(hi, total_boxes)] or [boxes[min(lo, total_boxes - 1)]]
        parsed: list[tuple[float, float, float, float]] = []
        for raw in group:
            try:
                bx0, by0, bx1, by1 = (float(v) for v in raw)
            except (TypeError, ValueError):
                continue
            if bx1 > bx0 and by1 > by0:
                if max_h > 0.0 and (by1 - by0) > max_h:
                    cy = (by0 + by1) / 2.0
                    by0 = cy - max_h / 2.0
                    by1 = cy + max_h / 2.0
                parsed.append((bx0, by0, bx1, by1))
        if not parsed:
            continue
        # One Markdown line per printed band wherever possible: bands are tight
        # (their height IS the printed line height), so the fitted font stays
        # honest. Merging several bands into one union box produced 60pt boxes
        # and 68pt "body text", which is what the old numbers showed.
        pieces = [txt]
        slots = parsed
        if len(parsed) > 1:
            weights = [(b[2] - b[0]) / max(1.0, b[3] - b[1]) for b in parsed]
            split = _split_text_by_widths(txt, weights)
            if len(split) == len(parsed):
                pieces = split
            else:
                slots = [(
                    min(b[0] for b in parsed),
                    min(b[1] for b in parsed),
                    max(b[2] for b in parsed),
                    max(b[3] for b in parsed),
                )]
        for si, (piece, slot) in enumerate(zip(pieces, slots)):
            lines.append({
                "text": piece,
                "box": [slot[0], slot[1], slot[2], slot[3]],
                "source": "ink",
                "mode": "aligned",
                "line_id": f"I{pno:04d}L{order:04d}",
                "column": 0,
                "cell_row": None,
                "cell_col": None,
                "subline_index": si,
                "order": order,
                "order_exempt": False,
                "header_footer": False,
                "align": "left",
            })
            order += 1
    return lines


def make_searchable(
    pdf_path: str | Path,
    page_nums_0based: Sequence[int],
    md_dict: Mapping[Any, Any],
    boxes_dict_or_None: Mapping[Any, Any] | None = None,
    out_pdf: str | Path | None = None,
    geo_source: str = "auto",
    align_out: str | Path | None = None,
    report: str | Path | None = None,
    dpi: int = DEFAULT_DPI,
) -> Path:
    """Create a raster-backed searchable PDF for selected zero-based pages.

    Old calling convention ``make_searchable(pdf, pages, md, boxes_or_none, target)``
    remains valid. ``dpi`` (72..300, default 200) controls the raster resolution.
    """
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        raise ValueError("dpi must be an integer in 72..300")
    if dpi < 72 or dpi > 300:
        raise ValueError("dpi must be an integer in 72..300")
    source = Path(pdf_path)
    if out_pdf is None:
        raise ValueError("out_pdf is required")
    target = Path(out_pdf)
    if geo_source not in {"auto", "external", "embedded", "fallback_only"}:
        raise ValueError(f"unknown geo_source: {geo_source}")
    if geo_source == "external" and boxes_dict_or_None is None:
        raise ValueError("geo_source=external requires boxes_dict_or_None")
    if not source.is_file():
        raise FileNotFoundError(str(source))
    if not isinstance(md_dict, Mapping):
        raise TypeError("md_dict must be a mapping")
    pages = list(page_nums_0based)
    if not pages:
        raise ValueError("page_nums_0based cannot be empty")
    if any(isinstance(p, bool) or not isinstance(p, int) or p < 0 for p in pages):
        raise ValueError("page numbers must be non-negative integers")

    output = fitz.open()
    align_boxes: dict[str, Any] = {}
    align_pages: list[dict[str, Any]] = []
    overflow_global = 0
    try:
        with fitz.open(str(source)) as document:
            # Cross-page repeated header/footer anchors: computed once for the
            # requested page range, not re-scanned per page and not the full book.
            known_header_anchors: tuple[str, ...] = ()
            if geo_source in ("auto", "embedded"):
                known_header_anchors = _extract_repeated_header_anchors(source, list(pages), min_repeat=2)
            for pno in pages:
                if pno >= document.page_count:
                    raise IndexError(f"page index {pno} outside PDF with {document.page_count} pages")
                source_page = document.load_page(pno)
                rect = fitz.Rect(source_page.rect)
                page = output.new_page(width=rect.width, height=rect.height)
                pixmap = source_page.get_pixmap(dpi=dpi, alpha=False)
                png_bytes = pixmap.tobytes("png")
                page.insert_image(page.rect, stream=png_bytes, overlay=False)
                text = _page_text(md_dict, pno)
                reason = "fallback_only"
                page_lines: list[dict[str, Any]] = []
                page_metrics: dict[str, Any] | None = None

                if geo_source == "fallback_only":
                    page_lines = _fallback_only_lines(text, pno)
                elif geo_source == "external":
                    ext = _page_boxes(boxes_dict_or_None, pno)
                    if ext and _usable_boxes(ext, rect):
                        page_lines = ext
                        reason = "external"
                    else:
                        ink_lines = _ink_aligned_lines(text, source_page, pno)
                        if ink_lines:
                            page_lines = ink_lines
                            reason = "ink"
                        else:
                            page_lines = _fallback_only_lines(text, pno)
                            reason = "external_bad"
                elif geo_source == "embedded":
                    geom = _extract_lines(source_page, known_header_anchors)
                    page_lines, page_metrics = _align_page(text, geom, pno)
                    reason = "embedded"
                else:  # auto
                    ext = _page_boxes(boxes_dict_or_None, pno)
                    if ext and _usable_boxes(ext, rect):
                        page_lines = ext
                        reason = "external"
                    else:
                        # Tight per-line boxes from the raster ink profile come
                        # first: the source PDF's own geometry is often
                        # block-level (one tall box per paragraph), and a tall
                        # box hands the fitted font a size far larger than the
                        # print. Ink bands are tight by construction.
                        ink_lines = _ink_aligned_lines(text, source_page, pno)
                        if ink_lines:
                            page_lines = ink_lines
                            page_metrics = None
                            reason = "ink"
                        else:
                            geom = _extract_lines(source_page, known_header_anchors)
                            page_lines, page_metrics = _align_page(text, geom, pno)
                            reason = "embedded" if geom.get("reliable") else "embedded_no_geom"

                aligned, header_footer, fallback = _partition_lines(page_lines)
                if reason == "external" and aligned:
                    # Row-level refinement: map block text onto the source
                    # page's native row boxes so the invisible layer sits on
                    # the scanned glyphs (page-80 fix). Unmapped blocks keep
                    # their original box.
                    try:
                        aligned = refine_external_lines(aligned, source_page)
                    except Exception:
                        pass
                overflow_local: dict[str, Any] = {}
                if aligned:
                    _insert_boxed_lines(page, aligned, overflow_local)
                if header_footer:
                    # header/footer lines keep their real geometry box; they are
                    # still order_exempt for verification but are NOT bottom-lumped.
                    _insert_boxed_lines(page, header_footer, overflow_local)
                overflow_global += overflow_local.get("overflow_expected", 0)
                if fallback:
                    _insert_fallback_capacity(page, "\n".join(ln["text"] for ln in fallback))
                if not aligned and not header_footer and not fallback:
                    _insert_fallback_capacity(page, text)

                # Maintain accurate metrics for external/fallback pages too.
                # For external pages the metrics (and the boxes.json the
                # verifier reads) must use the refined row-level lines.
                if reason == "external":
                    page_lines = aligned + header_footer + fallback
                if page_metrics is None:
                    page_metrics = _metrics_from_lines(
                        text,
                        page_lines,
                        pno,
                        reason,
                        overflow_expected=overflow_local.get("overflow_expected", 0),
                    )
                else:
                    page_metrics["overflow_expected"] = overflow_local.get("overflow_expected", 0)
                page_metrics["reason"] = reason
                align_boxes[str(pno)] = {"reliable": bool(page_lines), "lines": page_lines}
                align_pages.append(page_metrics)
        target.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(target), garbage=4, deflate=True)
    finally:
        output.close()

    if align_out:
        ap = Path(align_out)
        ap.mkdir(parents=True, exist_ok=True)
        (ap / "boxes.json").write_text(json.dumps(align_boxes, ensure_ascii=False, indent=2), encoding="utf-8")
        (ap / "align_report.json").write_text(json.dumps(_build_align_report(align_pages), ensure_ascii=False, indent=2), encoding="utf-8")
    if report:
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text(json.dumps(_build_align_report(align_pages), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _build_align_report(pages_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "align_report_v2",
        "pages": pages_metrics,
        "constants": {
            "SIMILARITY_THRESHOLD": SIMILARITY_THRESHOLD,
            "MIN_FONT_PT": MIN_FONT_PT,
            "MAX_CENTER_DEVIATION": MAX_CENTER_DEVIATION,
            "ALIGNED_PAGE_THRESHOLD": ALIGNED_PAGE_THRESHOLD,
        },
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create raster-backed searchable PDF")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("out_pdf", type=Path)
    parser.add_argument("--pages", required=True, help="comma-separated zero-based page indexes")
    parser.add_argument("--md-json", required=True, type=Path, help="JSON object mapping page to Markdown")
    parser.add_argument("--boxes-json", type=Path, help="JSON object mapping page to box lines")
    parser.add_argument("--geo-source", choices=("auto", "external", "embedded", "fallback_only"), default="auto")
    parser.add_argument("--align-out", type=Path, help="Directory for align_out/boxes.json + align_report.json")
    parser.add_argument("--report", type=Path, help="Path to write align_report.json")
    args = parser.parse_args()
    pages = [int(item.strip()) for item in args.pages.split(",") if item.strip()]
    make_searchable(
        args.pdf,
        pages,
        _load_json(args.md_json),
        _load_json(args.boxes_json) if args.boxes_json else None,
        args.out_pdf,
        geo_source=args.geo_source,
        align_out=args.align_out,
        report=args.report,
    )
    print("created=" + str(args.out_pdf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
