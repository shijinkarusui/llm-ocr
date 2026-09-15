"""W2: 共享LLM flag单源 (plan §4.5: add_llm_args(p, include_concurrency) 单源; ocr_page/batch_plan/cli import它)."""
from __future__ import annotations

import argparse


def add_llm_args(p: argparse.ArgumentParser, include_concurrency: bool) -> argparse.ArgumentParser:
    """W2冻结: --base-url/--model/--api-key/--key-stdin/--endpoint/--detail/--extra-json/--timeout/--system (+--concurrency仅include时)."""
    p.add_argument("--base-url", type=str, default=None, help="Override LLM base URL, e.g. http://YOUR_GATEWAY_HOST:2113/v1")
    p.add_argument("--model", type=str, default=None, help="Override model id")
    p.add_argument("--api-key", type=str, default=None, help="Override API key (or set LLM_OCR_KEY env; warns: prefer env/--key-stdin)")
    p.add_argument("--key-stdin", action="store_true", help="Read API key from stdin (pipe only; with --api-key the latter wins + warns)")
    p.add_argument("--endpoint", type=str, default=None, help="responses|chat|auto (+experimental messages). None=resolve default (CLI responses / lib chat)")
    p.add_argument("--detail", type=str, default=None, help="Image detail high|low|auto (default high)")
    if include_concurrency:
        p.add_argument("--concurrency", type=int, default=None, help="1..20 (default 4 CLI / 1 lib; >8 warns, >20 rejects)")
    p.add_argument("--extra-json", type=str, default=None, help='JSON object merged into request body')
    p.add_argument("--timeout", type=int, default=None, help="POST timeout seconds (default 120; GET fixed 15s)")
    p.add_argument("--system", type=str, default=None, help="System prompt (Anthropic dedicated; chat/responses via extra if needed)")
    return p


def parse_extra_json(extra_json: str | None, parser: argparse.ArgumentParser) -> dict | None:
    """W2: --extra-json解析 (dict对象, 非法即parser.error)."""
    import json as _json

    if not extra_json:
        return None
    try:
        data = _json.loads(extra_json)
    except _json.JSONDecodeError as exc:
        parser.error(f"--extra-json invalid JSON: {exc}")
    if not isinstance(data, dict):
        parser.error("--extra-json must be a JSON object")
    return data


def read_stdin_key(args: object) -> str | None:
    """W2: --key-stdin三态 (api_key优先+warn; TTY报错; 管道全读strip). 返回key或None."""
    import sys as _sys
    import warnings as _warnings

    key_stdin = bool(getattr(args, "key_stdin", False))
    api_key = getattr(args, "api_key", None)
    if not key_stdin:
        return api_key
    if api_key:
        _warnings.warn("--api-key given with --key-stdin: --api-key wins", UserWarning)
        return api_key
    try:
        from .config import read_key_stdin  # type: ignore
    except ImportError:
        try:
            from config import read_key_stdin  # type: ignore
        except ImportError:
            read_key_stdin = None  # type: ignore
    if read_key_stdin is not None:
        return read_key_stdin()  # type: ignore
    data = _sys.stdin.read()
    if _sys.stdin.isatty():
        raise RuntimeError("--key-stdin needs a pipe (e.g. echo KEY | ... --key-stdin)")
    data = data.strip()
    if not data:
        raise RuntimeError("--key-stdin got empty input")
    return data
