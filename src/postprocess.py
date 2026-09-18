from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Mapping

try:
    from .markdown_cleaner import _CODE_RE, _LATEX_RE, clean_md, merge_page_documents
except ImportError:
    from markdown_cleaner import _CODE_RE, _LATEX_RE, clean_md, merge_page_documents  # type: ignore



def merge_pages(pages: Mapping[int, str], title: str = "语音学教程") -> str:
    """Return a titled Markdown book with canonical page separators.

    Page files are cleaned before merging and the complete result is cleaned
    again. Missing page numbers are intentionally left missing.
    """
    return merge_page_documents(pages, title=title)


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
