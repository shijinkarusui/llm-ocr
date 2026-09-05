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
except ImportError:
    from geom_align import normalize_md as _normalize_md
    from geom_extract import garble_ratio as _garble_ratio

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
) -> dict[str, Any]:
    """Best-effort table cell-level recall/precision gate.

    When both a Markdown table and cell_row/cell_col metadata are available,
    compute recall/precision. Otherwise return an explicit non-computed status
    (``not_implemented`` or ``no_table``) rather than silently omitting the gate.
    """
    expected = _expected_table_cells(md_text)
    actual = _actual_cell_texts(page_lines)
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


def _actual_read_order(page: fitz.Page) -> tuple[str, list[dict[str, Any]]]:
    """Derive reading order from the OUTPUT PDF's actual bboxes (not boxes.json)."""
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
    # Normal page order: y primary, x secondary. Double-column would need
    # column metadata from boxes; this is a conservative default.
    items.sort(key=lambda it: ((it["y0"] + it["y1"]) / 2.0, (it["x0"] + it["x1"]) / 2.0))
    return _canonical("".join(it["text"] for it in items)), items


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
            actual_canon, actual_items = _actual_read_order(page)

            page_boxes = boxes.get(str(orig_pno)) or boxes.get(orig_pno) or {}
            page_lines = page_boxes.get("lines") or []
            exempt_lines = [ln for ln in page_lines if bool(ln.get("order_exempt", False))]
            non_exempt_lines = [ln for ln in page_lines if not bool(ln.get("order_exempt", False))]
            all_exempt = bool(page_lines) and not non_exempt_lines

            # Zero-loss is order-independent: every expected non-whitespace
            # character must be present, and no extra characters may appear.
            zero_loss = _char_multiset(expected) == _char_multiset(actual_canon)

            # Reading-order gate compares only non-order_exempt text. Fallback
            # / header-footer text may be placed at the bottom and must not make
            # a mixed page fail.
            expected_non_exempt = _canonical("".join(
                str(ln.get("text", "")) for ln in sorted(non_exempt_lines, key=lambda ln: ln.get("order", 0))
            ))
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
