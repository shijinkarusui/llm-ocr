from __future__ import annotations

"""Deterministic Markdown cleaning for OCR output.

This module adapts the rules from the external
``E:/PI-Desktop/云端知识库/clean_and_repair_markdown.py`` without making the
runtime depend on that file.  It deliberately separates page-local cleaning
from whole-book page reconstruction: normal OCR runs must never invent missing
pages.
"""

import re
from collections.abc import Iterable, Mapping


_PUNCT = {
    ",": "\uff0c",
    ".": "\u3002",
    ";": "\uff1b",
    ":": "\uff1a",
    "(": "\uff08",
    ")": "\uff09",
}
_LEFT_QUOTE = "\u201c"
_RIGHT_QUOTE = "\u201d"
_LEFT_APOS = "\u2018"
_RIGHT_APOS = "\u2019"
_FOOTER_RE = re.compile(r"^[ \t]*[\u00b7.]\s*\d+\s*[\u00b7.][ \t]*$")
_CODE_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")
_LATEX_RE = re.compile(
    r"(?<!\\)(?:\\\[[^\]]*\\\]|\\\([^)]*\\\)|\$[^\$\n]*\$|"
    r"\\(?:underset|overset|text|mathrm|mathbf|mathit|frac|sqrt)\s*"
    r"(?:\{(?:[^{}]|\{[^{}]*\})*\}|[^\s]+)(?:\s*\{(?:[^{}]|\{[^{}]*\})*\})?|"
    r"\^\{(?:[^{}]|\{[^{}]*\})*\}|_\{(?:[^{}]|\{[^{}]*\})*\})"
)
_PLACEHOLDER = "\ue000MARKDOWN_CLEANER_%d\ue001"
_LOCAL_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?!https?://|data:)[^)]+\)", re.IGNORECASE)
_HTML_PAGE_RE = re.compile(r"^<!--\s*PAGE\s+(\d+)\s*-->$", re.IGNORECASE)
_PAGE_LABEL_RE = re.compile(
    r"^(?:(?P<prefix>前言|目录|前置)\s+)?第\s*(?P<number>\d+)\s*页$"
    r"|^(?:page|p)\s*(?P<latin_number>\d+)$"
    r"|^(?P<cover>封面|内封|扉页|扉頁|版权页|版權頁)$",
    re.IGNORECASE,
)
_INDEX_ITEM_RE = re.compile(r"^-\s*Page\s+\d+\s*:\s*PAGE\s+\d+\s*$", re.IGNORECASE)


class _PageBlock:
    __slots__ = ("label", "body")

    def __init__(self, label: str, body: str) -> None:
        self.label = label
        self.body = body


def _protect(text: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def repl(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return _PLACEHOLDER % (len(saved) - 1)

    # Restore these byte-for-byte after punctuation and whitespace work.
    protected = re.sub(r"(?m)^[ \t]*[\u00b7.]\s*\d+\s*[\u00b7.][ \t]*$", repl, text)
    protected = _CODE_RE.sub(repl, protected)
    protected = _LATEX_RE.sub(repl, protected)
    return protected, saved


def _restore(text: str, saved: list[str]) -> str:
    for index, value in enumerate(saved):
        text = text.replace(_PLACEHOLDER % index, value)
    return text


def _is_cjk(char: str) -> bool:
    return bool(char) and "\u3400" <= char <= "\u9fff"


def _near_cjk(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return _is_cjk(before) or _is_cjk(after)


def _normalize_punctuation(text: str) -> str:
    out: list[str] = []
    quote_open = True
    for index, char in enumerate(text):
        if char in _PUNCT:
            out.append(_PUNCT[char] if _near_cjk(text, index) else char)
        elif char == '"':
            out.append(_LEFT_QUOTE if quote_open else _RIGHT_QUOTE)
            quote_open = not quote_open
        elif char == "'":
            before = text[index - 1] if index else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            if before.isalnum() and after.isalnum():
                out.append(_RIGHT_APOS)
            else:
                out.append(_LEFT_APOS if quote_open else _RIGHT_QUOTE)
                quote_open = not quote_open
        else:
            out.append(char)
    return "".join(out)


def _normalize_lines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    result: list[str] = []
    blank_pending = False
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            blank_pending = True
            continue
        if blank_pending and result:
            result.append("")
        blank_pending = False
        result.append(stripped if _FOOTER_RE.fullmatch(line) else line)
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


def clean_md(text: str) -> str:
    """Normalize punctuation and blank lines while preserving protected syntax."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    protected, saved = _protect(text)
    cleaned = _normalize_punctuation(protected)
    return _normalize_lines(_restore(cleaned, saved))


def _replace_local_images(text: str) -> str:
    # Image replacement is page-local, but code and formula syntax must remain byte-for-byte.
    protected, saved = _protect(text)
    return _restore(_LOCAL_IMAGE_RE.sub("[图片]", protected), saved)

def clean_single_page(text: str) -> str:
    """Clean one OCR result without interpreting or inventing page structure."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    return clean_md(_replace_local_images(text))


def _label_from_line(line: str) -> str | None:
    match = _PAGE_LABEL_RE.fullmatch(line.strip())
    if not match:
        return None
    if match.group("cover"):
        cover = match.group("cover")
        return {"扉頁": "扉页", "版權頁": "版权页"}.get(cover, cover)
    if match.group("latin_number"):
        return f"第 {int(match.group('latin_number'))} 页"
    prefix = (match.group("prefix") or "").strip()
    return f"{prefix + ' ' if prefix else ''}第 {int(match.group('number'))} 页"


def _label_from_html(line: str) -> str | None:
    match = _HTML_PAGE_RE.fullmatch(line.strip())
    return f"第 {int(match.group(1))} 页" if match else None


def _strip_outer_separators(lines: list[str]) -> list[str]:
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    while body and body[0].strip() == "---":
        body.pop(0)
        while body and not body[0].strip():
            body.pop(0)
    while body and body[-1].strip() == "---":
        body.pop()
        while body and not body[-1].strip():
            body.pop()
    return body


def _remove_page_index(lines: list[str]) -> list[str]:
    """Remove the old generated Page Index without touching normal bullets."""
    result: list[str] = []
    index_mode = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "## page index":
            index_mode = True
            continue
        if index_mode:
            if not stripped or _INDEX_ITEM_RE.fullmatch(stripped):
                continue
            index_mode = False
        result.append(line)
    return result


def _is_page_label_line(lines: list[str], index: int, *, allow_bare: bool = True) -> str | None:
    label = _label_from_line(lines[index])
    if label is None:
        return None
    previous = index - 1
    while previous >= 0 and not lines[previous].strip():
        previous -= 1
    # A bare label is safe only at the beginning. Inside a page body, the same
    # text can be ordinary prose (for example, "第 12 页"). Canonical and
    # legacy separated page blocks always have a rule before the label.
    if previous < 0:
        return label if allow_bare else None
    if lines[previous].strip() == "---":
        return label
    return None

def _parse_page_blocks(text: str, *, allow_bare_labels: bool = True) -> tuple[list[str], list[_PageBlock]]:
    lines = _remove_page_index(clean_single_page(text).split("\n"))
    markers: list[tuple[int, str, bool]] = []
    for index, line in enumerate(lines):
        html_label = _label_from_html(line)
        if html_label is not None:
            markers.append((index, html_label, True))
            continue
        label = _is_page_label_line(lines, index, allow_bare=allow_bare_labels)
        if label is not None:
            markers.append((index, label, False))

    if not markers:
        return lines, []

    # A legacy HTML marker may be followed by blank lines/separators and a
    # second explicit label.  Treat that label as metadata for the same page.
    compact: list[tuple[int, str, bool]] = []
    for marker in markers:
        if compact:
            previous_index, _previous_label, previous_is_html = compact[-1]
            between = lines[previous_index + 1 : marker[0]]
            if previous_is_html and all(not line.strip() or line.strip() == "---" for line in between):
                continue
        compact.append(marker)
    markers = compact

    title_lines = _strip_outer_separators(lines[: markers[0][0]])
    blocks: list[_PageBlock] = []
    for pos, (marker_index, label, is_html) in enumerate(markers):
        end = markers[pos + 1][0] if pos + 1 < len(markers) else len(lines)
        start = marker_index + 1
        if is_html:
            while start < end and (not lines[start].strip() or lines[start].strip() == "---"):
                start += 1
            if start < end and _label_from_line(lines[start]) is not None:
                start += 1
        body = _strip_outer_separators(lines[start:end])
        blocks.append(_PageBlock(label, "\n".join(body)))
    return title_lines, blocks


_PAGE_NUMBER_RE = re.compile(r"第\s*(\d+)\s*页")


def _sort_page_blocks(blocks: list[_PageBlock]) -> list[_PageBlock]:
    """Order numbered pages while keeping non-numbered labels stable."""
    if not blocks:
        return blocks
    numbered = [block for block in blocks if _PAGE_NUMBER_RE.search(block.label)]
    if len(numbered) != len(blocks):
        return blocks
    return sorted(
        blocks,
        key=lambda block: int(_PAGE_NUMBER_RE.search(block.label).group(1)),
    )


def clean_merged_book(text: str) -> str:
    """Normalize a whole book into canonical page blocks without filling gaps.

    The output uses one leading and one trailing separator per page::

        ---
        第 X 页

        body

        ---
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    title_lines, blocks = _parse_page_blocks(text)
    blocks = _sort_page_blocks(blocks)
    if not blocks:
        return clean_single_page("\n".join(title_lines))
    title = "\n".join(_strip_outer_separators(title_lines)).strip()
    output: list[str] = []
    if title:
        output.append(title)
    for block in blocks:
        body = clean_single_page(block.body)
        page = "---\n" + block.label + "\n\n"
        if body:
            page += body + "\n\n"
        page += "---"
        output.append(page)
    return "\n\n".join(output).rstrip() + "\n"


def extract_page_document(text: str, fallback_label: str) -> tuple[str, str]:
    """Return ``(label, body)`` from one page file for the merge pipeline."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    title_lines, blocks = _parse_page_blocks(text)
    if blocks:
        block = blocks[0]
        # Content before a page marker is a page-local title in malformed OCR;
        # retain it instead of silently dropping it.
        prefix = "\n".join(_strip_outer_separators(title_lines)).strip()
        body = "\n\n".join(part for part in (prefix, block.body) if part)
        return block.label, clean_single_page(body)
    return fallback_label, clean_single_page(text)


def merge_page_documents(pages: Mapping[int, str], title: str) -> str:
    """Build and clean a whole-book document from numbered page Markdown."""
    if not hasattr(pages, "items"):
        raise TypeError("pages must be a mapping")
    normalized: dict[int, str] = {}
    for pno, md in pages.items():
        if isinstance(pno, bool) or not isinstance(pno, int) or pno < 1:
            raise ValueError("page numbers must be positive integers")
        if not isinstance(md, str):
            raise TypeError("page markdown must be str")
        normalized[pno] = md

    title_md = clean_md(title).strip()
    chunks: list[str] = []
    if title_md:
        chunks.append(f"# {title_md.lstrip('#').strip()}")
    for pno in sorted(normalized):
        label, body = extract_page_document(normalized[pno], f"第 {pno} 页")
        page = "---\n" + label + "\n\n"
        if body:
            page += body + "\n\n"
        page += "---"
        chunks.append(page)
    return clean_merged_book("\n\n".join(chunks) + ("\n" if chunks else ""))


__all__ = [
    "clean_md",
    "clean_single_page",
    "clean_merged_book",
    "extract_page_document",
    "merge_page_documents",
]
