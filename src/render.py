from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError as exc:
    raise RuntimeError("PyMuPDF is required; import name is fitz") from exc

DEFAULT_PDF_PATH = Path(os.environ.get("OCR_PDF_PATH", ""))
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_BRACKET_RE = re.compile(r"【([^】]*)】|\[([^\]]*)\]")


def _checked_page(document: Any, pno_0based: int) -> Any:
    if not isinstance(pno_0based, int) or isinstance(pno_0based, bool):
        raise TypeError("pno_0based must be an integer")
    if pno_0based < 0:
        raise ValueError("pno_0based must be zero or greater")
    if pno_0based >= document.page_count:
        raise IndexError(f"page index {pno_0based} outside PDF with {document.page_count} pages")
    return document.load_page(pno_0based)


def render_page(pdf_path: str | Path, pno_0based: int, dpi: int = 200) -> bytes:
    """Render one zero-based PDF page as opaque PNG bytes."""
    if not isinstance(dpi, int) or isinstance(dpi, bool):
        raise TypeError("dpi must be an integer")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(str(pdf))
    with fitz.open(str(pdf)) as document:
        page = _checked_page(document, pno_0based)
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        return pixmap.tobytes("png")


def render_to_file(
    pdf_path: str | Path,
    pno_0based: int,
    dpi: int,
    output_path: str | Path,
) -> Path:
    """Render a page and write its PNG to output_path."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(render_page(pdf_path, pno_0based, dpi))
    return output


def _text_corruption_stats(text: str) -> dict[str, int]:
    fragments = [left or right for left, right in _BRACKET_RE.findall(text)]
    return {
        "replacement_count": text.count("\ufffd"),
        "question_mark_count": text.count("?"),
        "suspicious_cjk_in_transcription": sum(
            len(_CJK_RE.findall(fragment)) for fragment in fragments
        ),
        "suspicious_control_count": sum(
            1
            for char in text
            if ord(char) < 32 and char not in {"\n", "\r", "\t"}
        ),
    }


def strip_check(
    pno: int,
    pdf_path: str | Path = DEFAULT_PDF_PATH,
    sample_chars: int = 100,
) -> dict[str, Any]:
    """Check the old text layer and document why the raster must replace it.

    The first-100-character U+FFFD ratio is reported exactly as requested.
    Broken embedded fonts can map to wrong ordinary code points instead of
    U+FFFD, so the whole document is also scanned for IPA-bracket corruption.
    """
    if sample_chars <= 0:
        raise ValueError("sample_chars must be positive")
    pdf = Path(pdf_path)
    with fitz.open(str(pdf)) as document:
        page = _checked_page(document, pno)
        text = page.get_text()
        sample = text[:sample_chars]
        sample_stats = _text_corruption_stats(sample)
        document_bad_pages: list[int] = []
        document_stats = {
            "replacement_count": 0,
            "question_mark_count": 0,
            "suspicious_cjk_in_transcription": 0,
            "suspicious_control_count": 0,
        }
        for index in range(document.page_count):
            page_stats = _text_corruption_stats(document.load_page(index).get_text())
            for key in document_stats:
                document_stats[key] += page_stats[key]
            if any(page_stats.values()):
                document_bad_pages.append(index)
    sample_length = len(sample)
    replacement_ratio = (
        sample_stats["replacement_count"] / sample_length if sample_length else 0.0
    )
    page_corrupt = any(sample_stats.values()) or any(
        value for key, value in _text_corruption_stats(text).items()
    )
    document_corrupt = any(document_stats.values())
    evidence: list[str] = []
    if sample_stats["replacement_count"]:
        evidence.append("U+FFFD replacement characters in first 100 characters")
    if document_stats["replacement_count"]:
        evidence.append("U+FFFD replacement characters in document")
    if document_stats["suspicious_cjk_in_transcription"]:
        evidence.append("CJK glyphs inside transcription brackets")
    if document_stats["question_mark_count"]:
        evidence.append("literal question marks in extracted text")
    if document_stats["suspicious_control_count"]:
        evidence.append("unexpected control characters")
    if not evidence:
        evidence.append("no explicit corruption marker in inspected text")
    must_discard = page_corrupt or document_corrupt
    return {
        "pno_0based": pno,
        "sample_chars": sample_length,
        "text_sample": sample,
        "replacement_count": sample_stats["replacement_count"],
        "replacement_ratio": replacement_ratio,
        "first100_replacement_count": sample_stats["replacement_count"],
        "first100_replacement_ratio": replacement_ratio,
        "question_mark_count_first100": sample_stats["question_mark_count"],
        "page_text_corrupt": page_corrupt,
        "document_bad_page_count": len(document_bad_pages),
        "document_bad_page_examples": document_bad_pages[:10],
        "document_replacement_count": document_stats["replacement_count"],
        "document_question_mark_count": document_stats["question_mark_count"],
        "document_suspicious_cjk_in_transcription": document_stats[
            "suspicious_cjk_in_transcription"
        ],
        "document_suspicious_control_count": document_stats[
            "suspicious_control_count"
        ],
        "evidence": evidence,
        "must_discard": must_discard,
        "reason": (
            "legacy text layer is unreliable; rebuild from raster and discard old text"
            if must_discard
            else "sample is inconclusive; inspect additional pages before preserving text layer"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline PyMuPDF page renderer and text-layer check")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=int, default=0)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strip-check", action="store_true")
    args = parser.parse_args()
    if args.strip_check:
        print(json.dumps(strip_check(args.page, args.pdf), ensure_ascii=True))
    if args.output is not None:
        render_to_file(args.pdf, args.page, args.dpi, args.output)
        print("rendered=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())