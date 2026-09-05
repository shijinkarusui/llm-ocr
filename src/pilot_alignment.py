from __future__ import annotations

"""Pilot / census entry point for the text-layer alignment implementation.

This script is intentionally small and stdlib + PyMuPDF only. It provides:

- ``--pages`` pilot run: build a searchable PDF for a page subset, run
  ``verify_alignment``, and write ``align_report.json`` + ``verify_report.json``
  under ``--align-out``.
- ``--census-only``: do not build a PDF; iterate the selected pages, run the
  geometry extract / align stage, and write ``census_report.json`` with per-page
  garble_ratio, line count, aligned coverage, predicted path, and failure reasons.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError as exc:  # pragma: no cover - dependency is required
    raise RuntimeError("PyMuPDF is required") from exc

try:
    from .geom_extract import extract_lines, garble_ratio
    from .geom_align import align_page
    from .make_searchable import make_searchable
    from .verify_searchable import verify_alignment
except ImportError:  # pragma: no cover - direct script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from geom_extract import extract_lines, garble_ratio
    from geom_align import align_page
    from make_searchable import make_searchable
    from verify_searchable import verify_alignment


def parse_pages(spec: str) -> list[int]:
    """Parse ``"0-29"``, ``"0,3,5"``, or a mix like ``"0-2,7"``."""
    result: list[int] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            start, end = int(a.strip()), int(b.strip())
            if end < start:
                raise ValueError(f"invalid page range: {chunk}")
            result.extend(range(start, end + 1))
        else:
            result.append(int(chunk))
    return sorted(set(result))


def _lookup_md(md_dict: dict[str, Any], pno: int) -> str:
    for key in (str(pno), pno, str(pno + 1), pno + 1):
        if key in md_dict:
            return str(md_dict[key])
    return ""


def run_census(
    pdf_path: str | Path,
    md_dict: dict[str, Any],
    pages: list[int],
    align_out: str | Path,
) -> dict[str, Any]:
    """Run geometry extraction + alignment for selected pages; write census json."""
    source = Path(pdf_path)
    out = Path(align_out)
    out.mkdir(parents=True, exist_ok=True)
    pages_report: list[dict[str, Any]] = []
    with fitz.open(str(source)) as doc:
        for pno in pages:
            if pno >= doc.page_count:
                raise IndexError(f"page index {pno} outside PDF with {doc.page_count} pages")
            page = doc.load_page(pno)
            geom = extract_lines(page)
            md_text = _lookup_md(md_dict, pno)
            lines, metrics = align_page(md_text, geom, page_index=pno)
            predicted_path = "embedded" if geom.get("reliable") and geom.get("lines") else "fallback_capacity"
            pages_report.append({
                "page_index": pno,
                "garble_ratio": geom.get("garble_ratio", garble_ratio(md_text)),
                "line_count": len(geom.get("lines") or []),
                "reliable": bool(geom.get("reliable", False)),
                "warnings": geom.get("warnings", []),
                "predicted_path": predicted_path,
                "aligned_coverage": metrics.get("aligned_coverage", 0.0),
                "covered_coverage": metrics.get("covered_coverage", 0.0),
                "page_aligned": metrics.get("page_aligned", False),
                "failure_reasons": metrics.get("failure_reasons", {}),
                "table_unreliable": metrics.get("table_unreliable", False),
            })
    census = {
        "schema": "census_report_v1",
        "pages": pages_report,
        "counts": {
            "predicted_embedded": sum(1 for p in pages_report if p["predicted_path"] == "embedded"),
            "predicted_fallback": sum(1 for p in pages_report if p["predicted_path"] == "fallback_capacity"),
            "aligned_pages": sum(1 for p in pages_report if p["page_aligned"]),
        },
    }
    (out / "census_report.json").write_text(json.dumps(census, ensure_ascii=False, indent=2), encoding="utf-8")
    return census


def run_pilot(
    pdf_path: str | Path,
    md_dict: dict[str, Any],
    pages: list[int],
    align_out: str | Path,
    out_pdf: str | Path,
    *,
    census_only: bool = False,
    boxes_dict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the pilot build+verify, or census-only, and write report artifacts.

    If ``boxes_dict`` is provided, it is used as external geometric boxes and
    ``geo_source=external`` is forced; otherwise ``geo_source=auto`` uses the
    source PDF's embedded text layer.
    """
    align_dir = Path(align_out)
    align_dir.mkdir(parents=True, exist_ok=True)
    if census_only:
        census = run_census(pdf_path, md_dict, pages, align_dir)
        return {"census": census, "built": False}

    target = Path(out_pdf)
    make_searchable(
        pdf_path,
        pages,
        md_dict,
        boxes_dict,
        target,
        geo_source="external" if boxes_dict is not None else "auto",
        align_out=align_dir,
        report=align_dir / "align_report.json",
    )
    boxes_path = align_dir / "boxes.json"
    align_report_path = align_dir / "align_report.json"
    verify_report_path = align_dir / "verify_report.json"
    verify = verify_alignment(
        target,
        md_dict,
        boxes_path,
        align_report_path,
        verify_report_path,
        page_numbers=pages,
    )
    return {"built": True, "out_pdf": str(target), "verify_summary": verify.get("summary", {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pilot / census runner for text-layer alignment")
    parser.add_argument("--pdf", type=Path, required=True, help="Source PDF path")
    parser.add_argument("--md-json", type=Path, required=True, help="JSON object mapping page to Markdown")
    parser.add_argument("--pages", default="0-29", help="Page range spec, e.g. 0-29 or 0,3,5")
    parser.add_argument("--align-out", type=Path, required=True, help="Directory for report artifacts")
    parser.add_argument("--out-pdf", type=Path, help="Output searchable PDF (ignored with --census-only)")
    parser.add_argument("--boxes-json", type=Path, help="External boxes JSON (forces geo_source=external)")
    parser.add_argument("--census-only", action="store_true", help="Skip PDF build; write census_report.json only")
    args = parser.parse_args()

    if not args.pdf.is_file():
        parser.error(f"PDF not found: {args.pdf}")
    if not args.md_json.is_file():
        parser.error(f"md-json not found: {args.md_json}")

    md_dict = json.loads(args.md_json.read_text(encoding="utf-8"))
    boxes_dict = None
    if args.boxes_json is not None:
        if not args.boxes_json.is_file():
            parser.error(f"boxes-json not found: {args.boxes_json}")
        boxes_dict = json.loads(args.boxes_json.read_text(encoding="utf-8"))
    pages = parse_pages(args.pages)

    if args.census_only:
        census = run_census(args.pdf, md_dict, pages, args.align_out)
        print("census=" + json.dumps({"pages": len(census["pages"]), "counts": census["counts"]}, ensure_ascii=False))
        return 0

    if args.out_pdf is None:
        parser.error("--out-pdf is required unless --census-only is used")
    result = run_pilot(args.pdf, md_dict, pages, args.align_out, args.out_pdf, boxes_dict=boxes_dict)
    print("pilot=" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())