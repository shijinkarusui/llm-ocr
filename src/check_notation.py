from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


# Transcription contract: prompts/ocr_system.md + prompts/notation_spec.md.
# Output is plain Markdown with Unicode combining IPA diacritics. LaTeX
# position syntax (\underset / \overset / $...$ / \[...\]) is forbidden:
# attached marks must be single Unicode glyph units with their base.

# ASCII fallback inside square-bracket transcription, e.g. [t_w] or [k^h].
BANNED_BRACKET_MARK = re.compile(r"\[[^\]\r\n]*[_^][^\]\r\n]*\]")
# Attached labialization concatenated with its base, e.g. [tw] or [tɕ'w].
DROPPED_W = re.compile(r"\[(?:tɕ'?|tc'?|[tkdp])[wW]\]")
# Plain ASCII aspiration / palatalization in brackets, e.g. [kh] or [ɕj].
ASCII_MODIFIER_HJ = re.compile(r"\[(?:[kpt][hH]|[ɕnl][jJ])\]")
# LaTeX leftovers: position commands, unescaped dollars, display delimiters.
LATEX_RESIDUE = re.compile(r"\\(?:underset|overset)\b|(?<!\\)\$|\\\[|\\\]")
# Bare ASCII position operators outside bracket transcription.
RAW_ASCII_POSITION = re.compile(r"(?<!\\)[_^]")
# Combining diacritic separated from its base by whitespace, e.g. "t ʷ".
SPLIT_COMBINING = re.compile(
    r"[A-Za-zɕŋəɛɔɑβʔʂʐɖɳɭɰɣʃʒɹɿʅɦɥɤɲ'’]\s+"
    r"[ʷʰʲː̃̚ˈˌ\u0300-\u036F\u1AB0-\u1AFF\u1DC0-\u1DFF\u20D0-\u20FF\uFE20-\uFE2F]"
)
# Combining diacritic with no base character before it.
STRAY_COMBINING = re.compile(
    r"(?:^|[\s\u3000-\u303F\uFF00-\uFFEF.,;:!?，。；：“”‘’（）|(){}\[\]])"
    r"[\u0300-\u036F\u1AB0-\u1AFF\u1DC0-\u1DFF\u20D0-\u20FF\uFE20-\uFE2F]",
    re.MULTILINE,
)
BRACKET_SPAN = re.compile(r"\[[^\[\]\r\n]*\]")


def _issue(code: str, message: str, position: int, text: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "position": position,
        "line": text.count("\n", 0, position) + 1,
        "snippet": text[max(0, position - 24):position + 48].replace("\n", " "),
    }


def _bracket_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in BRACKET_SPAN.finditer(text)]


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < position < end for start, end in spans)


def _balanced_delimiters(text: str) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    stack: list[tuple[str, int]] = []
    pairs = {"{": "}", "[": "]", "(": ")"}
    closing = set(pairs.values())
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in pairs:
            stack.append((char, index))
        elif char in closing:
            if not stack or pairs[stack[-1][0]] != char:
                problems.append(_issue("unbalanced_brackets", "closing bracket without opener", index, text))
            else:
                stack.pop()
    for opener, position in reversed(stack):
        problems.append(_issue("unbalanced_brackets", "opening bracket without closer", position, text))
    return problems


def _extract_json_text(data: Any) -> str:
    if isinstance(data, dict):
        transcript = data.get("transcript_response")
        if isinstance(transcript, dict):
            choices = transcript.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
        for key in ("text", "content"):
            if isinstance(data.get(key), str):
                return data[key]
    values: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if key in {"_raw_stream", "raw_stream"}:
            return
        if key == "text" and isinstance(value, str):
            values.append(value)
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)

    visit(data)
    return "\n".join(dict.fromkeys(values))


def _load_text(path: Path) -> str:
    if path.suffix.lower() != ".json":
        return path.read_text(encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return _extract_json_text(data)


def check_text(text: str) -> list[dict[str, Any]]:
    """Check an in-memory Markdown string; useful for focused tests."""
    if not text:
        return []
    spans = _bracket_spans(text)
    problems: list[dict[str, Any]] = []
    for match in LATEX_RESIDUE.finditer(text):
        problems.append(_issue("latex_residue", "LaTeX position syntax is forbidden; use Unicode combining", match.start(), text))
    for match in BANNED_BRACKET_MARK.finditer(text):
        problems.append(_issue("banned_bracket_mark", "bracketed ASCII position mark is forbidden", match.start(), text))
    for match in DROPPED_W.finditer(text):
        problems.append(_issue("dropped_attached_mark", "bracketed w concatenated with its base; use ʷ", match.start(), text))
    for match in ASCII_MODIFIER_HJ.finditer(text):
        problems.append(_issue("ascii_modifier_letter", "plain h/j in brackets; use ʰ/ʲ", match.start(), text))
    for match in SPLIT_COMBINING.finditer(text):
        problems.append(_issue("split_combining_mark", "combining mark separated from its base by space", match.start(), text))
    for match in STRAY_COMBINING.finditer(text):
        problems.append(_issue("stray_combining_mark", "combining mark without a base character", match.start(), text))
    for match in RAW_ASCII_POSITION.finditer(text):
        if not _inside(match.start(), spans):
            problems.append(_issue("ascii_position_operator", "ASCII caret/underscore is not valid IPA notation", match.start(), text))
    problems.extend(_balanced_delimiters(text))
    return sorted(problems, key=lambda item: (item["position"], item["code"]))


def check_file(md_path: str | Path) -> list[dict[str, Any]]:
    """Return notation issues for Markdown or JSON containing OCR text."""
    path = Path(md_path)
    try:
        text = _load_text(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [{"code": "input_error", "message": type(exc).__name__, "position": 0, "line": 1, "snippet": ""}]
    return check_text(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Unicode IPA combining-mark notation (no LaTeX)")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = args.paths or [root / "tests/page21_llm.md", root / "out/page21.md"]
    if not args.paths:
        paths.extend(sorted((root / "out").glob("box_raw_*.json")))
    total = 0
    for path in paths:
        issues = check_file(path)
        total += len(issues)
        print("file=%s issues=%d" % (path.as_posix(), len(issues)))
        for item in issues:
            print("line=%d code=%s" % (item["line"], item["code"]))
    print("total_files=%d total_issues=%d" % (len(paths), total))
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
