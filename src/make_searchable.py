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
import json
import re
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
        for key in (
            "source", "mode", "line_id", "column", "cell_row", "cell_col",
            "subline_index", "order", "order_exempt", "header_footer", "align",
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


def _insert_segmented(
    page: fitz.Page,
    x_anchor: float,
    y_baseline: float,
    text: str,
    fontsize: float,
    *,
    align: str = "left",
) -> None:
    """Insert one logical line as multiple font segments using the layout formula.

    ``start_x = anchor + sum(previous segment widths)``; center/right shift the
    whole line first.
    """
    if not text:
        return
    width, segments = _measured_width(text, fontsize)
    start = x_anchor
    if align == "center":
        start = x_anchor - width / 2.0
    elif align == "right":
        start = x_anchor - width
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


def _insert_boxed_lines(page: fitz.Page, lines: list[dict[str, Any]], overflow_report: dict[str, Any] | None = None) -> None:
    """Insert aligned geometry lines with height sizing, horizontal alignment,
    real vertical baseline, wrapping for over-wide lines, and subline distribution.

    PyMuPDF's ``insert_text`` truncates text that extends beyond the physical page
    edge, so long paragraph-level boxes must be wrapped into several physical
    lines instead of being inserted as one over-wide string.
    """
    width, height = page.rect.width, page.rect.height
    overflow_count = 0
    for line in lines:
        x0, y0, x1, y1 = line["box"]
        x0, y0, x1, y1 = (
            x0 * width / 1000.0,
            y0 * height / 1000.0,
            x1 * width / 1000.0,
            y1 * height / 1000.0,
        )
        box_height = max(1.0, y1 - y0)
        box_width = max(1.0, x1 - x0)
        text = str(line.get("text", ""))
        fontsize = max(1.0, min(box_height * 0.92, box_height * 0.78 + 1.0))
        measured, _segments = _measured_width(text, fontsize)
        if measured > box_width:
            scaled = fontsize * box_width / measured
            fontsize = max(MIN_FONT_PT, scaled)
        align = str(line.get("align", "left"))

        # If the line still does not fit after font scaling, wrap it into
        # multiple physical lines. This keeps zero-loss intact (all chars are
        # inserted) and prevents PyMuPDF's page-edge truncation.
        wrapped = [text]
        if _measured_width(text, fontsize)[0] > box_width:
            wrapped = []
            for raw in text.split("\n"):
                if raw.strip():
                    wrapped.extend(_wrap_line(raw, fontsize, box_width))
            if not wrapped:
                wrapped = [text]
            if len(wrapped) == 1 and _measured_width(wrapped[0], fontsize)[0] > box_width:
                overflow_count += 1
                if overflow_report is not None:
                    overflow_report["overflow_expected"] = overflow_report.get("overflow_expected", 0) + 1

        if len(wrapped) == 1:
            # Center the glyph vertically inside its box. Using the box top plus
            # font ascent would pull small (width-scaled) fonts to the very top
            # of the box and flip physical reading order relative to box order.
            baseline = (y0 + y1) / 2.0 + fontsize * 0.35
            _insert_segmented(page, x0, baseline, wrapped[0], fontsize, align=align)
        else:
            step = max(1.0, box_height / len(wrapped))
            actual_fontsize = max(1.0, min(fontsize, step * 0.75))
            for i, chunk in enumerate(wrapped):
                baseline = y0 + (i + 0.5) * step
                _insert_segmented(page, x0, baseline, chunk, actual_fontsize, align=align)


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


def make_searchable(
    pdf_path: str | Path,
    page_nums_0based: Sequence[int],
    md_dict: Mapping[Any, Any],
    boxes_dict_or_None: Mapping[Any, Any] | None = None,
    out_pdf: str | Path | None = None,
    geo_source: str = "auto",
    align_out: str | Path | None = None,
    report: str | Path | None = None,
) -> Path:
    """Create a raster-backed searchable PDF for selected zero-based pages.

    Old calling convention ``make_searchable(pdf, pages, md, boxes_or_none, target)``
    remains valid.
    """
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
                pixmap = source_page.get_pixmap(dpi=DEFAULT_DPI, alpha=False)
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
                        geom = _extract_lines(source_page, known_header_anchors)
                        page_lines, page_metrics = _align_page(text, geom, pno)
                        reason = "embedded" if geom.get("reliable") else "embedded_no_geom"

                aligned, header_footer, fallback = _partition_lines(page_lines)
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
