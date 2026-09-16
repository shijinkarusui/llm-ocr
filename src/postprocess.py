from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Mapping

# Source stays ASCII-only; Unicode punctuation is built from escaped code points.
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
_PLACEHOLDER = "\ue000POSTPROCESS%d\ue001"


def _protect(text: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def repl(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return _PLACEHOLDER % (len(saved) - 1)

    # Footers, code, and math are restored byte-for-byte after punctuation work.
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
    """Normalize Chinese punctuation and blank lines without changing IPA or LaTeX."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    protected, saved = _protect(text)
    cleaned = _normalize_punctuation(protected)
    return _normalize_lines(_restore(cleaned, saved))


def _page_body(md: str) -> str:
    body = clean_md(md)
    return body if body else "[EMPTY PAGE]"


def merge_pages(pages: Mapping[int, str], title: str = "\u8bed\u97f3\u5b66\u6559\u7a0b") -> str:
    """Return a titled, indexed Markdown book with explicit page markers."""
    if not hasattr(pages, "items"):
        raise TypeError("pages must be a mapping")
    normalized: dict[int, str] = {}
    for pno, md in pages.items():
        if isinstance(pno, bool) or not isinstance(pno, int) or pno < 1:
            raise ValueError("page numbers must be positive integers")
        if not isinstance(md, str):
            raise TypeError("page markdown must be str")
        normalized[pno] = md
    numbers = sorted(normalized)
    chunks = [f"# {title}", ""]
    for pno in numbers:
        chunks.extend([f"<!-- PAGE {pno} -->", _page_body(normalized[pno]), ""])
    return "\n".join(chunks).rstrip() + "\n"


def _ipa_signature(text: str) -> Counter[str]:
    ranges = ((0x250, 0x2FF), (0x300, 0x36F), (0x1D00, 0x1D7F))
    return Counter(
        char
        for char in text
        if any(start <= ord(char) <= end for start, end in ranges)
    )


def _protected_tokens(text: str) -> tuple[list[str], list[str]]:
    code_math = [m.group(0) for m in _CODE_RE.finditer(text)]
    code_math += [m.group(0) for m in _LATEX_RE.finditer(text)]
    footers = [
        m.group(0).strip()
        for m in re.finditer(
            r"(?m)^[ \t]*[\u00b7.]\s*\d+\s*[\u00b7.][ \t]*$", text
        )
    ]
    return code_math, footers


def polish_with_llm(
    text: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str = "chat",
    extra: dict | None = None,
) -> tuple[str, dict]:
    """Make one polish call via llm_client passthrough and verify protected content.

    endpoint: chat|responses|messages — all go through llm_client (full passthrough)
    extra: any additional body fields (temperature, reasoning_effort, max_tokens, etc.)
    """
    try:
        from .llm_client import chat_completions, responses_create, anthropic_messages
    except ImportError:
        import sys as _sys
        from pathlib import Path as _Path

        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from llm_client import chat_completions, responses_create, anthropic_messages

    extra = extra or {}
    # dedup: strip model/base_url/api_key from extra if leaked in
    extra = {k: v for k, v in extra.items() if k not in ("model", "base_url", "api_key")}
    prompt = (
        "Polish punctuation and paragraph breaks in this Chinese OCR Markdown. "
        "Return only Markdown. Do not change IPA characters, page footer numbers, "
        "Markdown markers, or LaTeX syntax including $\\underset{}$, $\\overset{}$, "
        "^{}, and _{}.\n\n" + text
    )
    ep = (endpoint or "chat").strip().lower()
    if ep in ("responses",):
        polished, usage, _ = responses_create(prompt, instructions="You are a Chinese OCR post-processor.", base_url=base_url, model=model, api_key=api_key, **extra)
    elif ep in ("messages", "anthropic"):
        sys_prompt = extra.pop("system", "You are a Chinese OCR post-processor.") if "system" in extra else "You are a Chinese OCR post-processor."
        # Defer to anthropic_messages' own ceiling unless the caller asked for
        # an explicit one: the old hardcoded 8192 was a third, unrelated
        # default sitting between this call site and llm_client.
        polished, usage, _ = anthropic_messages([{"role": "user", "content": prompt}], base_url=base_url, model=model, api_key=api_key, **{"system": sys_prompt, **extra})
    else:
        polished, usage, _ = chat_completions([{"role": "user", "content": prompt}], base_url=base_url, model=model, api_key=api_key, **extra)

    polished = str(polished).strip()
    if _protected_tokens(text) != _protected_tokens(polished):
        raise ValueError("protected tokens changed")
    if _ipa_signature(text) != _ipa_signature(polished):
        raise ValueError("IPA changed")
    return polished, usage or {}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown postprocess (base_url+endpoint flexible, full passthrough)")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--polish", action="store_true")
    parser.add_argument("--base-url", type=str, default=None, help="Override LLM base URL")
    parser.add_argument("--model", type=str, default=None, help="Override model id")
    parser.add_argument("--api-key", type=str, default=None, help="Override API key")
    parser.add_argument("--endpoint", type=str, default="chat", help="chat|responses|messages")
    parser.add_argument("--extra-json", type=str, default=None, help='JSON object merged into polish request body')
    args = parser.parse_args()
    if args.input is None:
        parser.error("--input is required")
    raw = args.input.read_text(encoding="utf-8")
    cleaned = clean_md(raw)
    if args.output is not None:
        _write(args.output, cleaned + ("\n" if cleaned else ""))
    print("clean_input_chars=%d" % len(raw))
    print("clean_output_chars=%d" % len(cleaned))
    if args.polish:
        try:
            extra = None
            if args.extra_json:
                try:
                    extra = json.loads(args.extra_json)
                    if not isinstance(extra, dict):
                        parser.error("--extra-json must be a JSON object")
                except json.JSONDecodeError as exc:
                    parser.error(f"--extra-json invalid JSON: {exc}")
            polished, usage = polish_with_llm(cleaned, base_url=args.base_url, model=args.model, api_key=args.api_key, endpoint=args.endpoint, extra=extra)
            target = (
                args.output.with_name(args.output.stem + ".polished.md")
                if args.output
                else Path("page21.polished.md")
            )
            _write(target, polished.rstrip() + "\n")
            print("polished_output_chars=%d" % len(polished))
            print("llm_usage=" + json.dumps(usage, ensure_ascii=True, separators=(",", ":")))
        except Exception as exc:
            print("llm_status=skipped error=" + type(exc).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
