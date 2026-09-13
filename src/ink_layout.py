"""Raster ink layout analysis: real line boxes for pages with no vector geometry.

Scanned pages carry neither embedded text nor vector outlines, so the writer
would otherwise fall back to synthetic full-width strips that place the
invisible text layer nowhere near the printed glyphs. Projection profiling of
the rendered bitmap recovers true per-line boxes -- the same thing Umi-OCR
gets from its OCR detector's line quads -- so the text layer can be fitted
into the actual printed region.

Pure local: no network, no OCR model. numpy is used when present, with a
slower pure-Python fallback so the frozen build never depends on it.
"""
from __future__ import annotations

from typing import Any

try:
    import fitz
except ImportError:  # pragma: no cover - fitz is a project dependency
    fitz = None  # type: ignore

try:
    import numpy as _np
except ImportError:  # pragma: no cover - optional accelerator
    _np = None  # type: ignore

DEFAULT_DPI = 150
INK_THRESHOLD = 176          # gray <= this counts as ink
ROW_MIN_INK_PX = 2           # rows/columns with fewer ink px are noise
BAND_MERGE_GAP_PX = 2        # merge bands separated by <= this many blank rows
MIN_BAND_HEIGHT_PX = 5
MAX_BAND_HEIGHT_RATIO = 0.12  # reject bands taller than 12% of the page (figures)
MIN_BAND_WIDTH_PX = 6
COL_GAP_PX = 16              # blank column run that separates layout columns
MIN_COL_WIDTH_PX = 8


def _bands_from_counts(
    counts: Any,
    min_count: int,
    merge_gap: int,
    min_len: int,
    max_len: int,
) -> list[tuple[int, int]]:
    """Group consecutive above-threshold positions into (start, end) bands."""
    bands: list[tuple[int, int]] = []
    start = -1
    gap = 0
    n = len(counts)
    for i in range(n):
        if counts[i] >= min_count:
            if start < 0:
                start = i
            gap = 0
        elif start >= 0:
            gap += 1
            if gap > merge_gap:
                bands.append((start, i - gap))
                start = -1
                gap = 0
    if start >= 0:
        bands.append((start, n - 1))
    out: list[tuple[int, int]] = []
    for a, b in bands:
        if b < a:
            continue
        length = b - a + 1
        if length < min_len or length > max_len:
            continue
        out.append((a, b))
    return out


def _detect_numpy(page: "fitz.Page", dpi: int, threshold: int, split_columns: bool) -> list[list[float]]:
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    w, h, n = pix.width, pix.height, pix.n
    zoom = dpi / 72.0
    buf = _np.frombuffer(pix.samples, dtype=_np.uint8)
    arr = buf.reshape(h, w) if n == 1 else buf.reshape(h, w, n)[:, :, 0]
    ink = arr <= threshold

    row_counts = ink.sum(axis=1)
    row_bands = _bands_from_counts(
        row_counts, ROW_MIN_INK_PX, BAND_MERGE_GAP_PX,
        MIN_BAND_HEIGHT_PX, max(MIN_BAND_HEIGHT_PX, int(h * MAX_BAND_HEIGHT_RATIO)),
    )
    boxes: list[list[float]] = []
    for y0, y1 in row_bands:
        sub = ink[y0:y1 + 1]
        col_counts = sub.sum(axis=0)
        if split_columns:
            col_bands = _bands_from_counts(col_counts, ROW_MIN_INK_PX, COL_GAP_PX, MIN_COL_WIDTH_PX, w)
        else:
            col_bands = _bands_from_counts(col_counts, ROW_MIN_INK_PX, COL_GAP_PX, MIN_BAND_WIDTH_PX, w)
            if col_bands:
                col_bands = [(col_bands[0][0], col_bands[-1][1])]
        for x0, x1 in col_bands:
            boxes.append([
                x0 / zoom,
                y0 / zoom,
                (x1 + 1) / zoom,
                (y1 + 1) / zoom,
            ])
    return boxes


def _detect_pure(page: "fitz.Page", dpi: int, threshold: int) -> list[list[float]]:
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    w, h, n = pix.width, pix.height, pix.n
    zoom = dpi / 72.0
    s = pix.samples
    row_counts = [0] * h
    row_lo = [w] * h
    row_hi = [-1] * h
    for y in range(h):
        base = y * w * n
        cnt = 0
        lo = -1
        hi = -1
        for x in range(w):
            if s[base + x * n] <= threshold:
                cnt += 1
                if lo < 0:
                    lo = x
                hi = x
        row_counts[y] = cnt
        row_lo[y] = lo
        row_hi[y] = hi

    row_bands = _bands_from_counts(
        row_counts, ROW_MIN_INK_PX, BAND_MERGE_GAP_PX,
        MIN_BAND_HEIGHT_PX, max(MIN_BAND_HEIGHT_PX, int(h * MAX_BAND_HEIGHT_RATIO)),
    )
    boxes: list[list[float]] = []
    for y0, y1 in row_bands:
        lo = w
        hi = -1
        for y in range(y0, y1 + 1):
            if row_lo[y] >= 0 and row_lo[y] < lo:
                lo = row_lo[y]
            if row_hi[y] > hi:
                hi = row_hi[y]
        if lo < 0 or hi < lo:
            continue
        boxes.append([lo / zoom, y0 / zoom, (hi + 1) / zoom, (y1 + 1) / zoom])
    return boxes


def detect_line_boxes(
    page: "fitz.Page",
    *,
    dpi: int = DEFAULT_DPI,
    threshold: int = INK_THRESHOLD,
    split_columns: bool = False,
) -> list[list[float]]:
    """Detect printed text lines on a page bitmap.

    Returns boxes in PDF points, ordered top-to-bottom then left-to-right,
    each ``[x0, y0, x1, y1]`` tightly bounding one printed line band.
    """
    if fitz is None:  # pragma: no cover
        raise RuntimeError("PyMuPDF is required")
    if page is None:
        return []
    try:
        if _np is not None:
            return _detect_numpy(page, dpi, threshold, split_columns)
        return _detect_pure(page, dpi, threshold)
    except Exception:
        return []


def detect_normalized_boxes(
    page: "fitz.Page",
    *,
    dpi: int = DEFAULT_DPI,
    threshold: int = INK_THRESHOLD,
    split_columns: bool = False,
) -> list[list[float]]:
    """Same as :func:`detect_line_boxes` but normalized to 0-1000 units."""
    rect = fitz.Rect(page.rect)
    if rect.width <= 0 or rect.height <= 0:
        return []
    out: list[list[float]] = []
    for x0, y0, x1, y1 in detect_line_boxes(
        page, dpi=dpi, threshold=threshold, split_columns=split_columns
    ):
        nx0 = max(0.0, min(1000.0, x0 / rect.width * 1000.0))
        ny0 = max(0.0, min(1000.0, y0 / rect.height * 1000.0))
        nx1 = max(0.0, min(1000.0, x1 / rect.width * 1000.0))
        ny1 = max(0.0, min(1000.0, y1 / rect.height * 1000.0))
        if nx1 <= nx0 or ny1 <= ny0:
            continue
        out.append([round(nx0, 2), round(ny0, 2), round(nx1, 2), round(ny1, 2)])
    return out


__all__ = ["detect_line_boxes", "detect_normalized_boxes"]
