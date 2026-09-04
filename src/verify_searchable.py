from __future__ import annotations

import argparse
import difflib
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable

try:
    import fitz
except ImportError as exc:
    raise RuntimeError("PyMuPDF is required") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = ROOT.parent / "book-to-quiz-pipeline" / "data" / "语音学教程.pdf"
DEFAULT_SEARCHABLE = ROOT / "out" / "searchable_sample.pdf"

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
    """Estimate suspicious legacy text-layer characters as a numeric ratio."""
    if not text:
        return 0.0
    compact_length = sum(not char.isspace() for char in text)
    if compact_length == 0:
        return 0.0
    bad_chars = sum(1 for char in text if _BAD_CHAR_RE.fullmatch(char))
    bad_chars += sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t")
    bad_chars += sum(
        1 for char in text if unicodedata.category(char) in {"Co", "Cs", "Cn"}
    )
    bad_tokens = sum(len(match.group(0)) for match in _SUSPICIOUS_TOKEN_RE.finditer(text))
    return min(1.0, (bad_chars + bad_tokens) / compact_length)


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


def _ascii_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--compare-a", type=Path)
    parser.add_argument("--compare-b", type=Path)
    args = parser.parse_args()

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