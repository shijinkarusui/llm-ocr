from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


BANNED_BRACKET_MARK = re.compile(r"\\?\[[^\]\r\n]*[_^][^\]\r\n]*\\?\]")
DROPPED_W = re.compile(r"\\?\[(?:tc|tɕ|t|k|d)w\\?\]")
DISPLAY_DELIMITER = re.compile(r"\\\[|\\\]")
MATH_DELIMITER = re.compile(r"(?<!\\)\$")
POSITION_COMMAND = re.compile(r"\\(underset|overset)\b")
RAW_ASCII_POSITION = re.compile(r"(?<!\\)[_^]")
ADJACENT_MATH_DELIMITER = re.compile(r"\]\$\$\[")


def _issue(code: str, message: str, position: int, text: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "position": position,
        "line": text.count("\n", 0, position) + 1,
        "snippet": text[max(0, position - 24):position + 48].replace("\n", " "),
    }


def _math_spans(text: str) -> tuple[list[tuple[int, int]], list[int]]:
    positions = [m.start() for m in MATH_DELIMITER.finditer(text)]
    spans = [(positions[i], positions[i + 1]) for i in range(0, len(positions) - 1, 2)]
    return spans, positions


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < position < end for start, end in spans)


def _read_braced(text: str, start: int | None) -> int | None:
    if start is None:
        return None
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                return None
    return None


def _balanced_delimiters(text: str, spans: list[tuple[int, int]]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for start, end in spans:
        stack: list[tuple[str, int]] = []
        escaped = False
        pairs = {"{": "}", "[": "]"}
        closing = set(pairs.values())
        for index in range(start + 1, end):
            char = text[index]
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


def _command_problems(text: str, spans: list[tuple[int, int]]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for match in POSITION_COMMAND.finditer(text):
        if not _inside(match.start(), spans):
            problems.append(_issue("command_outside_math", "position command is outside inline math", match.start(), text))
            continue
        first = _read_braced(text, match.end())
        second = _read_braced(text, first)
        if first is None or second is None:
            problems.append(_issue("command_arguments", "position command needs two braced arguments", match.start(), text))
    return problems


def _position_problems(text: str, spans: list[tuple[int, int]]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for match in BANNED_BRACKET_MARK.finditer(text):
        # Inside math, the bare operator check below distinguishes valid Y_{x}/Y^{x}
        # from forbidden t_w/k^h. The broad bracket rule applies to plain text only.
        if not _inside(match.start(), spans):
            problems.append(_issue("banned_bracket_mark", "bracketed ASCII position mark is forbidden", match.start(), text))
    for match in DROPPED_W.finditer(text):
        problems.append(_issue("dropped_attached_mark", "bracketed w appears concatenated with its base", match.start(), text))
    for match in DISPLAY_DELIMITER.finditer(text):
        problems.append(_issue("display_math_delimiter", "use inline dollar delimiters", match.start(), text))
    for match in ADJACENT_MATH_DELIMITER.finditer(text):
        problems.append(_issue("adjacent_math_delimiter", "adjacent inline formulas share $$; separate with a space", match.start(), text))
    for match in RAW_ASCII_POSITION.finditer(text):
        position = match.start()
        if not _inside(position, spans):
            problems.append(_issue("position_outside_math", "caret or underscore is outside inline math", position, text))
            continue
        before = text[max(0, position - 16):position]
        if re.search(r"\\(?:under|over)set$", before):
            continue
        if position + 1 >= len(text) or text[position + 1] != "{":
            problems.append(_issue("bare_position_operator", "caret or underscore needs a braced payload", position, text))
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
    spans, delimiters = _math_spans(text)
    problems: list[dict[str, Any]] = []
    if len(delimiters) % 2:
        problems.append(_issue("unmatched_dollar", "inline math dollar delimiter is unmatched", delimiters[-1], text))
    problems.extend(_balanced_delimiters(text, spans))
    problems.extend(_command_problems(text, spans))
    problems.extend(_position_problems(text, spans))
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
    parser = argparse.ArgumentParser(description="Check inline IPA attached-mark notation")
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

