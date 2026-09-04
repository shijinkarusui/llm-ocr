from __future__ import annotations

"""Build a raster-backed PDF with an invisible searchable text layer."""

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import fitz

DEFAULT_DPI = 200
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_NON_CJK_RE = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
IPA_FONT_PATH = Path(r"C:\Windows\Fonts\arial.ttf")


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
        valid.append({"text": text, "box": coords})
    return valid or None


def _usable_boxes(lines: list[dict[str, Any]] | None, page_rect: fitz.Rect) -> bool:
    if not lines or len(lines) < 2 or page_rect.width <= 0 or page_rect.height <= 0:
        return False
    ys = [coord for line in lines for coord in (line["box"][1], line["box"][3])]
    return min(ys) >= 0 and max(ys) <= 1000 and max(ys) - min(ys) >= 50


def _font_for_line(text: str) -> str:
    if _CJK_RE.search(text):
        return "china-s"
    if any(ord(char) > 127 for char in text) and IPA_FONT_PATH.is_file():
        return "ocripa"
    return "helv"


def _font_width(font_name: str, text: str, fontsize: float) -> float:
    if font_name == "ocripa" and IPA_FONT_PATH.is_file():
        return fitz.Font(fontfile=str(IPA_FONT_PATH)).text_length(text, fontsize=fontsize)
    return fitz.Font(fontname=font_name).text_length(text, fontsize=fontsize)


def _insert_line(page: fitz.Page, point: fitz.Point, text: str, fontsize: float) -> None:
    """Insert one complete line with one font to keep extraction clean."""
    if not text:
        return
    font = _font_for_line(text)
    if font == "ocripa":
        page.insert_font(fontname=font, fontfile=str(IPA_FONT_PATH))
    page.insert_text(
        point,
        text,
        fontname=font,
        fontsize=max(0.5, float(fontsize)),
        render_mode=3,
        overlay=True,
    )


def _auxiliary_ipa_text(texts: Sequence[str]) -> str:
    """Collect non-CJK runs for a font with better IPA Unicode coverage."""
    parts: list[str] = []
    for text in texts:
        for part in _NON_CJK_RE.findall(text):
            part = part.strip()
            # The primary china-s/Helvetica layer already carries ordinary
            # ASCII. Keep only non-ASCII runs here to preserve IPA glyphs
            # without duplicating normal words in extracted text.
            if part and any(ord(char) > 127 for char in part):
                parts.append(part)
    return " ".join(parts)


def _wrap_line(text: str, fontsize: float, max_width: float) -> list[str]:
    """Wrap a line by measured glyph width, including unspaced CJK text."""
    if not text:
        return []
    font = _font_for_line(text)
    chunks: list[str] = []
    current: list[str] = []
    current_width = 0.0
    for char in text:
        char_width = _font_width(font, char, fontsize)
        if current and current_width + char_width > max_width:
            chunks.append("".join(current))
            current = []
            current_width = 0.0
        current.append(char)
        current_width += char_width
    if current:
        chunks.append("".join(current))
    return chunks or [text]


def _insert_auxiliary(page: fitz.Page, texts: Sequence[str]) -> None:
    if not IPA_FONT_PATH.is_file():
        return
    value = _auxiliary_ipa_text(texts)
    if not value:
        return
    margin_x = min(18.0, page.rect.width * 0.04)
    chunks = _wrap_line(value, 0.5, max(2.0, page.rect.width - 2 * margin_x))
    for index, chunk in enumerate(chunks):
        _insert_line(page, fitz.Point(margin_x, max(2.0, page.rect.height - 2.0 - index * 0.6)), chunk, 0.5)


def _insert_boxed_lines(page: fitz.Page, lines: list[dict[str, Any]]) -> None:
    width, height = page.rect.width, page.rect.height
    texts = [line["text"] for line in lines]
    for line in lines:
        x0, y0, x1, y1 = line["box"]
        x0, y0, x1, y1 = (x0 * width / 1000.0, y0 * height / 1000.0, x1 * width / 1000.0, y1 * height / 1000.0)
        box_height = max(1.0, y1 - y0)
        fontsize = max(1.0, min(box_height * 0.92, box_height * 0.78 + 1.0))
        _insert_line(page, fitz.Point(x0, y0 + fontsize), line["text"], fontsize)



def _insert_fallback(page: fitz.Page, text: str) -> None:
    logical_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not logical_lines:
        return
    width, height = page.rect.width, page.rect.height
    margin_x = min(18.0, width * 0.04)
    max_width = max(2.0, width - 2 * margin_x)
    # Complete wrapped text block in the invisible bottom band for copying/searching.
    bottom_chunks: list[str] = []
    for line in logical_lines:
        bottom_chunks.extend(_wrap_line(line, 0.5, max_width))
    for index, chunk in enumerate(bottom_chunks):
        _insert_line(page, fitz.Point(margin_x, max(2.0, height - 2.0 - index * 0.6)), chunk, 0.5)
    distributed: list[str] = []
    for line in logical_lines:
        distributed.extend(_wrap_line(line, 8.0, max_width))
    top = min(height * 0.08, height - 2.0)
    bottom = max(top + 2.0, height * 0.94)
    step = (bottom - top) / max(1, len(distributed))
    fontsize = max(1.0, min(8.0, step * 0.55))
    for index, line in enumerate(distributed):
        y = top + (index + 0.72) * step
        _insert_line(page, fitz.Point(margin_x, y), line, fontsize)


def make_searchable(
    pdf_path: str | Path,
    page_nums_0based: Sequence[int],
    md_dict: Mapping[Any, Any],
    boxes_dict_or_None: Mapping[Any, Any] | None,
    out_pdf: str | Path,
) -> Path:
    """Create a raster-backed searchable PDF for selected zero-based pages.

    Markdown and boxes can key pages by zero-based index or one-based page
    number. Boxes are [x0, y0, x1, y1] normalized to 0-1000. A page with
    missing, malformed, explicitly unreliable, or too-small boxes uses the
    evenly distributed full-text fallback layer.
    """
    source = Path(pdf_path)
    target = Path(out_pdf)
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
    try:
        with fitz.open(str(source)) as document:
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
                boxes = _page_boxes(boxes_dict_or_None, pno)
                if _usable_boxes(boxes, rect):
                    _insert_boxed_lines(page, boxes or [])
                else:
                    _insert_fallback(page, text)
        target.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(target), garbage=4, deflate=True)
    finally:
        output.close()
    return target


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create raster-backed searchable PDF")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("out_pdf", type=Path)
    parser.add_argument("--pages", required=True, help="comma-separated zero-based page indexes")
    parser.add_argument("--md-json", required=True, type=Path, help="JSON object mapping page to Markdown")
    parser.add_argument("--boxes-json", type=Path, help="JSON object mapping page to box lines")
    args = parser.parse_args()
    pages = [int(item.strip()) for item in args.pages.split(",") if item.strip()]
    make_searchable(args.pdf, pages, _load_json(args.md_json), _load_json(args.boxes_json) if args.boxes_json else None, args.out_pdf)
    print("created=" + str(args.out_pdf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
