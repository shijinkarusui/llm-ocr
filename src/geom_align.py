from __future__ import annotations

"""S3 alignment engine: high-precision Markdown -> geometry boxes (pure local).

This module implements the reviewed plan's line-level SequenceMatcher aligner,
per-line similarity gate, unmatched-character fallback capacity placement,
aligned/covered metrics, and boxes v2 / align_report schema.
"""

import difflib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

try:
    import fitz
except ImportError:  # pragma: no cover - fitz is a project dependency
    fitz = None  # type: ignore

SIMILARITY_THRESHOLD = 0.85
MIN_FONT_PT = 4.0
MAX_CENTER_DEVIATION = 25
ALIGNED_PAGE_THRESHOLD = 0.95

_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", flags=re.MULTILINE)
_MD_LIST_RE = re.compile(r"^\s*[-*+]\s+", flags=re.MULTILINE)
_MD_BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
_MD_CODE_RE = re.compile(r"(`+)(.*?)\1")
# A Markdown link/image target normally contains no square brackets. Requiring
# that keeps literal bracket notation such as "[ɛ]([E])" (common in this book's
# TOC) from being stripped as if it were a link.
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\((?:[^()\[\]]*)\)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:[^()\[\]]*)\)")


def _strip_markdown(text: str) -> str:
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_LIST_RE.sub("", text)
    text = _MD_BOLD_RE.sub(r"\2", text)
    text = _MD_CODE_RE.sub(r"\2", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_IMAGE_RE.sub("", text)
    return text


def normalize_md(md_text: str) -> str:
    """Normalize Markdown to the plain text used for alignment/zero-loss checks.

    Keeps line breaks, removes heading/list/bold/code/link/image markup, keeps
    table cells by joining with spaces, normalizes NFC, and does not split
    combining IPA marks.
    """
    if not isinstance(md_text, str):
        md_text = str(md_text)
    md_text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in md_text.split("\n"):
        line = _strip_markdown(raw)
        if "|" in line:
            stripped = line.strip()
            # Only a real Markdown table row (single leading AND trailing pipe,
            # ≥2 separators) is collapsed; literal "|" in prose (metrical/
            # phonology notation such as （“|”表示音步的界线） or "|| 龈边音…")
            # must be preserved.
            if (
                stripped.startswith("|")
                and stripped.endswith("|")
                and not stripped.startswith("||")
                and stripped.count("|") >= 2
            ):
                parts = [p.strip() for p in stripped.strip("|").split("|")]
                line = " ".join(p for p in parts if p)
        if line.strip():
            lines.append(line.rstrip())
    out = "\n".join(lines)
    return unicodedata.normalize("NFC", out)


def _non_ws(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _fallback_box(index: int, total: int) -> list[float]:
    """Synthetic normalized fallback-capacity box (full width, distributed y)."""
    top = 50.0
    bottom = 950.0
    if total <= 1:
        y0 = (top + bottom) / 2.0 - 20.0
        y1 = (top + bottom) / 2.0 + 20.0
    else:
        step = (bottom - top) / total
        y0 = top + index * step
        y1 = y0 + max(20.0, step * 0.8)
    return [20.0, y0, 980.0, min(1000.0, y1)]


def _make_line(
    text: str,
    box: list[float],
    *,
    mode: str,
    source: str,
    order: int,
    line_id: str,
    column: int = 0,
    cell_row: int | None = None,
    cell_col: int | None = None,
    subline_index: int = 0,
    order_exempt: bool = False,
    header_footer: bool = False,
    similarity: float | None = None,
    line_rect: list[float] | None = None,
) -> dict[str, Any]:
    return {
        "text": text,
        "box": box,
        "source": source,
        "mode": mode,
        "line_id": line_id,
        "column": column,
        "cell_row": cell_row,
        "cell_col": cell_col,
        "subline_index": subline_index,
        "order": order,
        "order_exempt": order_exempt,
        "header_footer": header_footer,
        "similarity": similarity,
        "line_rect": line_rect,
    }


def align_page(
    md_text: str,
    geometry: Mapping[str, Any],
    page_index: int = 0,
    page_width: float = 1000.0,
    page_height: float = 1000.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Align one page's Markdown to extracted native geometry lines."""
    del page_width, page_height  # boxes already normalized; kept for API clarity.
    normalized = normalize_md(md_text)
    md_lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    geom_lines = list(geometry.get("lines") or [])
    geom_reliable = bool(geometry.get("reliable") and geom_lines)

    lines: list[dict[str, Any]] = []
    used_md: set[int] = set()
    sims: list[float] = []
    failed_reasons: dict[str, int] = {}

    if geom_reliable:
        for rg_line in geom_lines:
            geom_text = str(rg_line.get("text", "")).strip()
            if not geom_text:
                continue
            best_idx = -1
            best_ratio = 0.0
            for mi, md_line in enumerate(md_lines):
                if mi in used_md:
                    continue
                r = _ratio(geom_text, md_line)
                if r > best_ratio:
                    best_ratio = r
                    best_idx = mi
            sims.append(best_ratio)
            if best_idx >= 0 and best_ratio >= SIMILARITY_THRESHOLD:
                used_md.add(best_idx)
                order = len(lines)
                box = list(rg_line.get("box") or [])
                is_header_footer = bool(rg_line.get("header_footer", False))
                mode = "header_footer_excluded" if is_header_footer else "aligned"
                lines.append(
                    _make_line(
                        md_lines[best_idx],
                        box,
                        mode=mode,
                        source="md",
                        order=order,
                        line_id=f"A{order:04d}",
                        column=int(rg_line.get("column", 0)),
                        cell_row=rg_line.get("cell_row"),
                        cell_col=rg_line.get("cell_col"),
                        subline_index=0,
                        order_exempt=is_header_footer,
                        header_footer=is_header_footer,
                        similarity=best_ratio,
                        line_rect=rg_line.get("line_rect"),
                    )
                )
            else:
                failed_reasons["low_similarity"] = failed_reasons.get("low_similarity", 0) + 1

    # Unmatched Markdown must not be dropped -> fallback capacity.
    unmatched = [md_lines[i] for i in range(len(md_lines)) if i not in used_md]
    if unmatched:
        # Reuse aligned order counter for stable fallback order.
        base_order = len(lines)
        for i, md_line in enumerate(unmatched):
            box = _fallback_box(i + base_order, len(unmatched) + max(1, len(lines)))
            lines.append(
                _make_line(
                    md_line,
                    box,
                    mode="fallback_capacity",
                    source="md",
                    order=base_order + i,
                    line_id=f"F{base_order + i:04d}",
                    column=0,
                    subline_index=0,
                    order_exempt=True,
                    similarity=None,
                    line_rect=None,
                )
            )
        failed_reasons.setdefault("unmatched_md_lines", len(unmatched))

    # Reassign `order` from physical reading position, matching the plan's
    # reading-order sorting key. Use line center-y (same as the actual output
    # reading-order derivation) so title/cover pages with uneven box heights do
    # not invert small lines.
    lines.sort(key=lambda ln: ((float(ln["box"][1]) + float(ln["box"][3])) / 2.0, float(ln["box"][0])))
    for _idx, ln in enumerate(lines):
        ln["order"] = _idx

    aligned_chars = _non_ws("".join(ln["text"] for ln in lines if ln["mode"] == "aligned"))
    covered_chars = _non_ws("".join(ln["text"] for ln in lines))
    total_chars = _non_ws(normalized)
    aligned_coverage = aligned_chars / total_chars if total_chars else 1.0
    covered_coverage = covered_chars / total_chars if total_chars else 1.0
    table_unreliable = bool(geometry.get("table_unreliable", False))
    metrics = {
        "page_index": page_index,
        "total_non_ws_chars": total_chars,
        "aligned_chars": aligned_chars,
        "covered_chars": covered_chars,
        "aligned_coverage": round(aligned_coverage, 6),
        "covered_coverage": round(covered_coverage, 6),
        "page_aligned": aligned_coverage >= ALIGNED_PAGE_THRESHOLD,
        "table_unreliable": table_unreliable,
        "similarities": sims,
        "failure_reasons": failed_reasons,
        "mode_counts": {m: sum(1 for ln in lines if ln["mode"] == m) for m in ("aligned", "fallback_capacity", "header_footer_excluded")},
        "overflow_expected": 0,
    }
    return lines, metrics


def align_all(
    pdf_path: str | Path,
    md_dict: Mapping[Any, Any],
    page_nums: list[int],
    *,
    align_out: str | Path | None = None,
    report: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Standalone full-book aligner: extract geometry + align + write artifacts.

    Used by callers that want the align stage separate from PDF writing.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required")
    source = Path(pdf_path)
    boxes: dict[str, Any] = {}
    report: dict[str, Any] = report or {}
    pages_list: list[dict[str, Any]] = []
    with fitz.open(str(source)) as doc:
        for pno in page_nums:
            page = doc.load_page(pno)
            try:
                from .geom_extract import extract_lines
            except ImportError:
                from geom_extract import extract_lines
            geom = extract_lines(page)
            md_text = str(md_dict.get(pno) or md_dict.get(pno + 1) or "")
            lines, metrics = align_page(md_text, geom, page_index=pno)
            boxes[str(pno)] = {"reliable": bool(lines), "lines": lines}
            pages_list.append(metrics)
    final_report = {
        "schema": "align_report_v2",
        "pages": pages_list,
        "constants": {
            "SIMILARITY_THRESHOLD": SIMILARITY_THRESHOLD,
            "MIN_FONT_PT": MIN_FONT_PT,
            "MAX_CENTER_DEVIATION": MAX_CENTER_DEVIATION,
            "ALIGNED_PAGE_THRESHOLD": ALIGNED_PAGE_THRESHOLD,
        },
    }
    if align_out:
        out = Path(align_out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "boxes.json").write_text(json.dumps(boxes, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "align_report.json").write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")
    elif report:
        # If only report given, write report at that path; boxes still in memory.
        Path(report).write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return boxes, final_report
