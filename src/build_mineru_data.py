"""Build the MinerU-derived data inputs for the dual-layer text alignment pilot.

Reads the two MinerU ``content_list`` JSON files and writes, under ``out/``:

* ``llm_pages.json`` — per-page high-fidelity Markdown (blocks on one page
  joined with blank lines; table cells joined with spaces, not ``|``);
* ``mineru_boxes.json`` — external geometric boxes for every page.

MinerU ``content_list`` bboxes are already normalized to 0-1000; do NOT divide
them by the PDF page size. ``out/`` stays gitignored (large artifacts), but
this generator is tracked so any checkout can reproduce the inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out"
# MinerU results live outside the repo (large artifacts, not tracked). Point
# ``LLM_OCR_MINERU_DIR`` at your own ``mineru-results`` directory; the default
# keeps the original layout for a checkout that sits next to it.
MINERU_DIR = Path(os.environ.get("LLM_OCR_MINERU_DIR", ROOT.parent / "mineru-results"))
PART1 = MINERU_DIR / "part1" / "e8a095c9-594c-4c9a-b06b-f766b4d30bb1_content_list.json"
PART2 = MINERU_DIR / "part2" / "87a61508-e876-46a8-a121-939cc43ec109_content_list.json"
PART2_OFFSET = 200
MODEL1 = MINERU_DIR / "part1" / "e8a095c9-594c-4c9a-b06b-f766b4d30bb1_model.json"
MODEL2 = MINERU_DIR / "part2" / "87a61508-e876-46a8-a121-939cc43ec109_model.json"


def _iter_table_cells(row_html: str) -> list[tuple[str, int, int]]:
    """Parse one ``<tr>`` into ``(plain_text, colspan, rowspan)`` cells.

    Matches ``<td ...>`` AND ``<th ...>`` with attributes in any order;
    missing spans default to 1. Plain text is LaTeX-cleaned downstream.
    """
    cells = []
    for m in re.finditer(r"<t[dh](.*?)>(.*?)</t[dh]>", row_html, re.S):
        attrs, inner = m.group(1), m.group(2)
        mc = re.search(r"colspan\s*=\s*\"?(\d+)", attrs)
        mr = re.search(r"rowspan\s*=\s*\"?(\d+)", attrs)
        colspan = int(mc.group(1)) if mc else 1
        rowspan = int(mr.group(1)) if mr else 1
        cells.append((re.sub(r"<[^>]+>", "", inner), colspan, rowspan))
    return cells


def expand_table_grid(h: str) -> list[list[str]]:
    """Expand an HTML table into a full row×col grid honoring spans.

    ``colspan`` repeats the cell text across its columns (header labels like
    ``发音部位 发音方法`` belong to both); ``rowspan`` carries the text into
    the rows below (``塞音`` covers its two sub-rows). Empty cells stay ``""``
    so column indexes line up with the visual table — the writer's column
    mapping depends on this.
    """
    if not h:
        return []
    grid: list[list[str]] = []
    pending: dict[int, list] = {}  # col -> [text, rows_left]
    for row_html in re.findall(r"<tr>(.*?)</tr>", h, re.S):
        row: list[str] = []
        col = 0
        cells = _iter_table_cells(row_html)
        ci = 0
        while ci < len(cells) or col in pending:
            if col in pending:
                text, left = pending[col]
                row.append(text)
                if left > 1:
                    pending[col] = [text, left - 1]
                else:
                    del pending[col]
                col += 1
                continue
            raw, colspan, rowspan = cells[ci]
            ci += 1
            text = latex_cell_to_text(raw)
            for _ in range(colspan):
                row.append(text)
                if rowspan > 1:
                    pending[col] = [text, rowspan - 1]
                col += 1
        grid.append(row)
    width = max((len(r) for r in grid), default=0)
    return [r + [""] * (width - len(r)) for r in grid]


def html_table_to_text(h: str) -> str:
    if not h:
        return ""
    out = []
    for row in expand_table_grid(h):
        # De-dup adjacent colspan repeats (``鼻音,鼻音`` -> ``鼻音``), same as
        # the writer: block text and inserted text must agree for zero-loss.
        dedup = [t for i, t in enumerate(row) if not (i and t and t == row[i - 1])]
        # Join with spaces, not "|": the writer inserts this text verbatim and
        # verify only collapses real Markdown table rows (single leading AND
        # trailing pipe), so literal notation like "||" in prose is preserved.
        out.append(" ".join(dedup))
    return "\n".join(out)


def html_table_cells(h: str) -> list[str]:
    """Cell texts of one HTML table (expected side of the external table gate)."""
    cells = []
    for row in expand_table_grid(h):
        for text in row:
            if text:
                cells.append(text)
    return cells


def html_table_grid(h: str) -> list[list[str]]:
    """Row×col grid of plain-text cells (for the row-level table writer)."""
    return expand_table_grid(h)


def latex_cell_to_text(cell: str) -> str:
    """Best-effort plain text of one MinerU table cell's LaTeX-ish markup.

    Handles the phonetics-table vocabulary actually present in the corpus:
    ``$p^{\\\\prime}$`` -> ``p'``, ``ŋ``/``ɕ`` pass through, unknown commands
    degrade to their braced content (``\\\\text{x}`` -> ``x``) so the writer and
    the table gate compare real glyphs, never raw markup.
    """
    t = str(cell or "").strip()
    if not t:
        return ""
    # $...$ wrappers and rowspans carry no visible content.
    t = t.replace("$", "")
    t = re.sub(r"\\multirow\{[^}]*\}\{[^}]*\}\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\multicolumn\{[^}]*\}\{[^}]*\}\{([^}]*)\}", r"\1", t)
    # p^{\prime} -> p' (also \prime without braces).
    t = re.sub(r"\^\{\\prime\}", "'", t)
    t = t.replace("\\prime", "'")
    # Generic ^{x}/_{x} -> x; ^x/_x (single char) -> x.
    t = re.sub(r"\^\\mathrm\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\^\{([^}]*)\}", r"\1", t)
    t = re.sub(r"_\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\^(\S)", r"\1", t)
    t = re.sub(r"_(\S)", r"\1", t)
    t = re.sub(r"\\mathrm\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\text\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\([a-zA-Z]+)", "", t)
    t = t.replace("{", "").replace("}", "").replace("\\", "")
    return t.strip()


def block_text(b) -> str:
    t = b.get("type")
    if t in (
        "text",
        "header",
        "footer",
        "page_number",
        "page_footnote",
        "ref_text",
        "equation",
    ):
        return str(b.get("text", "") or "")
    if t == "image":
        return "\n".join(str(c) for c in (b.get("image_caption") or []))
    if t == "chart":
        return "\n".join(str(c) for c in (b.get("chart_caption") or []))
    if t == "table":
        caps = b.get("table_caption") or []
        body = html_table_to_text(str(b.get("table_body", "") or ""))
        return "\n".join([*[str(c) for c in caps], body])
    return ""


def clamp(v) -> float:
    return max(0.0, min(1000.0, float(v)))


def has_hidden_span_markup(h: str) -> bool:
    """True when the table HTML uses rowspan/colspan/<th> (grid expansion matters)."""
    if not h:
        return False
    low = h.lower()
    return ("rowspan" in low) or ("colspan" in low) or ("<th" in low)


def load_ocr_rows() -> dict[int, list[list[float]]]:
    """MinerU ``model.json`` ``ocr_text`` row boxes per page (0-1000).

    These are MinerU's own row-level boxes — the row expansion of the
    ``content_list`` block boxes (same coordinate frame). Text blocks are
    split across them by the writer; the table band has no ocr rows (tables
    are layout-detected, not OCR'd) and keeps the embedded-layer path.
    """
    rows: dict[int, list[list[float]]] = {}
    for model_path, offset in ((MODEL1, 0), (MODEL2, PART2_OFFSET)):
        try:
            pages = json.loads(model_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        for page_idx, spans in enumerate(pages):
            if not isinstance(spans, list):
                continue
            idx = page_idx + offset
            for span in spans:
                if not isinstance(span, dict) or span.get("type") != "ocr_text":
                    continue
                bbox = span.get("bbox") or []
                if len(bbox) != 4:
                    continue
                norm = [clamp(v * 1000.0) for v in bbox]
                if norm[2] <= norm[0] or norm[3] <= norm[1]:
                    continue
                rows.setdefault(idx, []).append(norm)
    for idx in rows:
        rows[idx].sort(key=lambda b: ((b[1] + b[3]) / 2.0, (b[0] + b[2]) / 2.0))
    return rows


def build() -> tuple[dict, dict]:
    parts = [
        (json.loads(PART1.read_text(encoding="utf-8")), 0),
        (json.loads(PART2.read_text(encoding="utf-8")), PART2_OFFSET),
    ]
    ocr_rows = load_ocr_rows()
    md_pages: dict[str, list[tuple[str, list]]] = {}
    box_pages: dict[str, list[dict]] = {}
    for blocks, offset in parts:
        for blk in blocks:
            idx = int(blk["page_idx"]) + offset
            text = block_text(blk).strip()
            if not text:
                continue
            bbox = blk.get("bbox") or [0, 0, 0, 0]
            md_pages.setdefault(idx, []).append((text, bbox))
            if len(bbox) != 4:
                continue
            norm = [clamp(v) for v in bbox]
            if norm[2] <= norm[0] or norm[3] <= norm[1]:
                continue
            lines = box_pages.setdefault(idx, [])
            ln: dict = {
                "text": text,
                "box": norm,
                "mode": "aligned",
                "source": "mineru",
                "line_id": f"M{idx:04d}{len(lines):04d}",
                "column": 0,
                "cell_row": None,
                "cell_col": None,
                "subline_index": 0,
                "order_exempt": False,
                "header_footer": False,
                "align": "left",
            }
            if blk.get("type") == "table":
                body_html = str(blk.get("table_body", "") or "")
                ln["is_table"] = True
                ln["table_cells"] = html_table_cells(body_html)
                ln["table_grid"] = html_table_grid(body_html)
                ln["table_caption"] = [str(c) for c in (blk.get("table_caption") or [])]
                # Span-expansion audit flag: the reviewer can recount spans
                # from the stored raw HTML instead of trusting the expansion.
                ln["table_span_markup"] = has_hidden_span_markup(body_html)
                ln["table_raw_html"] = body_html
            if blk.get("type") == "equation":
                # Equation blocks are one visual formula region even though
                # PyMuPDF splits them into many small glyph bboxes.
                ln["is_equation"] = True
            # Attach the ocr row boxes inside this block's y-band: the writer
            # splits block text across them (MinerU's own row geometry).
            # Tables/equations keep their block box (no ocr rows there).
            if blk.get("type") in ("text", "header", "footer", "page_footnote", "ref_text"):
                band = [
                    b for b in ocr_rows.get(idx, [])
                    if norm[1] - 15.0 <= (b[1] + b[3]) / 2.0 <= norm[3] + 15.0
                ]
                if band:
                    ln["ocr_rows"] = band
            lines.append(ln)

    md_out: dict[str, str] = {}
    for idx, items in sorted(md_pages.items()):
        items.sort(key=lambda x: ((x[1][1] + x[1][3]) / 2.0, x[1][0]))
        md_out[str(idx)] = "\n\n".join(text for text, _ in items)

    box_out: dict[str, dict] = {}
    for idx, lines in sorted(box_pages.items()):
        lines.sort(key=lambda ln: ((ln["box"][1] + ln["box"][3]) / 2.0, ln["box"][0]))
        for i, ln in enumerate(lines):
            ln["order"] = i
        box_out[str(idx)] = {"reliable": True, "lines": lines}
    return md_out, box_out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    md_out, box_out = build()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "llm_pages.json").write_text(
        json.dumps(md_out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (args.out_dir / "mineru_boxes.json").write_text(
        json.dumps(box_out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"pages {len(md_out)} box-pages {len(box_out)} "
        f"box-lines {sum(len(v['lines']) for v in box_out.values())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
