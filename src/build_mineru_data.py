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
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out"
PART1 = Path(
    r"F:\DSH工作区\mineru-results\part1\e8a095c9-594c-4c9a-b06b-f766b4d30bb1_content_list.json"
)
PART2 = Path(
    r"F:\DSH工作区\mineru-results\part2\87a61508-e876-46a8-a121-939cc43ec109_content_list.json"
)
PART2_OFFSET = 200


def html_table_to_text(h: str) -> str:
    if not h:
        return ""
    rows = re.findall(r"<tr>(.*?)</tr>", h, re.S)
    out = []
    for row in rows:
        cells = re.findall(r"<td>(.*?)</td>", row, re.S)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        # Join with spaces, not "|": the writer inserts this text verbatim and
        # verify only collapses real Markdown table rows (single leading AND
        # trailing pipe), so literal notation like "||" in prose is preserved.
        out.append(" ".join(cells))
    return "\n".join(out)


def html_table_cells(h: str) -> list[str]:
    """Cell texts of one HTML table (expected side of the external table gate)."""
    if not h:
        return []
    rows = re.findall(r"<tr>(.*?)</tr>", h, re.S)
    cells = []
    for row in rows:
        for c in re.findall(r"<td>(.*?)</td>", row, re.S):
            text = re.sub(r"<[^>]+>", "", c).strip()
            if text:
                cells.append(text)
    return cells


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


def build() -> tuple[dict, dict]:
    parts = [
        (json.loads(PART1.read_text(encoding="utf-8")), 0),
        (json.loads(PART2.read_text(encoding="utf-8")), PART2_OFFSET),
    ]
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
                ln["is_table"] = True
                ln["table_cells"] = html_table_cells(str(blk.get("table_body", "") or ""))
            if blk.get("type") == "equation":
                # Equation blocks are one visual formula region even though
                # PyMuPDF splits them into many small glyph bboxes.
                ln["is_equation"] = True
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
