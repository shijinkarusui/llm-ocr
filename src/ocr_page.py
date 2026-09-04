from __future__ import annotations

"""Vision LLM OCR for a PDF page or PNG image — supports 3 ingresses (chat/responses/messages).

All Octopus gateway fields are passthrough: use --extra-json to inject any
official OpenAI / Responses / Anthropic param without changing this file.

Examples:
  # chat (default)
  python -m src.ocr_page --image tests/cand_165.png --output out/cand_165.md
  python -m src.ocr_page --image tests/cand_165.png --output out/cand_165.md --endpoint chat --detail high --extra-json '{"temperature":0.2,"max_completion_tokens":8192}'

  # responses
  python -m src.ocr_page --image tests/cand_165.png --output out/resp.md --endpoint responses --extra-json '{"reasoning":{"effort":"low"}}'

  # anthropic
  python -m src.ocr_page --image tests/cand_165.png --output out/claude.md --endpoint messages --system "OCR..."
"""

import argparse
import json
import pathlib
import sys
from typing import Final

try:
    from .llm_client import (
        anthropic_vision,
        anthropic_vision_url,
        auto_vision,
        chat_vision,
        chat_vision_url,
        responses_vision,
        responses_vision_url,
    )
except ImportError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    try:
        from llm_client import (
            anthropic_vision,
            anthropic_vision_url,
            auto_vision,
            chat_vision,
            chat_vision_url,
            responses_vision,
            responses_vision_url,
        )
    except ImportError:
        from llm_client import (
            anthropic_vision,
            anthropic_vision_url,
            chat_vision,
            chat_vision_url,
            responses_vision,
            responses_vision_url,
        )
        auto_vision = None  # type: ignore  # test injection: tests may set ocr_page.auto_vision

try:
    import fitz
except ImportError as exc:
    raise RuntimeError("render_page requires PyMuPDF (import name fitz)") from exc

ROOT: Final = pathlib.Path(__file__).resolve().parents[1]
PROMPT_PATH: Final = ROOT / "prompts" / "ocr_system.md"


def _resolve_ocr(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    detail: str | None = None,
    system: str | None = None,
) -> tuple[str | None, str | None, str, str, str | None]:
    """C1 None-默认-then-resolve: 显参优先, None落resolve_config(ns)(库默认endpoint chat)."""
    import types as _types

    # C1: 单测可monkeypatch ocr_page.resolve_config (模块顶无import, 此处globals优先)
    _rc = globals().get("resolve_config")
    if _rc is None:
        try:
            from .config import resolve_config as _rc  # type: ignore
        except ImportError:
            try:
                from config import resolve_config as _rc  # type: ignore
            except ImportError:
                _rc = None
    if _rc is None:
        return base_url, model, api_key or "", (endpoint if endpoint is not None else "chat"), (detail if detail is not None else "high")
    ns = _types.SimpleNamespace(
        base_url=base_url, model=model, api_key=api_key, key_stdin=False,
        endpoint=endpoint, detail=detail, system=system, is_cli=False,
    )
    g, r = _rc(ns)
    return g.base_url, g.model, g.key, (endpoint if endpoint is not None else r.endpoint), r.detail


def _ladder_or_direct(pdf_path: str | pathlib.Path, pno: int, dpi: int) -> tuple[bytes, int, int, int]:
    """C1: ocr_page侧ladder (try复用batch_plan._ladder_render, except本地最小copy, W2可合一)."""
    try:
        from .batch_plan import _ladder_render  # type: ignore
        return _ladder_render(pathlib.Path(pdf_path), pno, dpi)
    except ImportError:
        pass
    try:
        from batch_plan import _ladder_render as _lr  # type: ignore
        return _lr(pathlib.Path(pdf_path), pno, dpi)
    except ImportError:
        pass
    png = render_page(pdf_path, pno, dpi)
    import base64 as _b64
    blen = len(_b64.b64encode(png))
    if blen > 1_500_000:
        raise ValueError(f"payload_too_large: b64 {blen} > 1500000 at dpi {dpi} (ladder unavailable)")
    return png, dpi, blen, blen


def render_page(pdf_path: str | pathlib.Path, pno: int, dpi: int = 200) -> bytes:
    """Render zero-based PDF page pno to PNG bytes."""
    if pno < 0:
        raise ValueError("pno must be zero-based page index")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    pdf = pathlib.Path(pdf_path)
    with fitz.open(pdf) as document:
        if pno >= document.page_count:
            raise IndexError(f"page index {pno} outside PDF with {document.page_count} pages")
        page = document.load_page(pno)
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        return pixmap.tobytes("png")


def _parse_extra(extra_json: str | None) -> dict:
    if not extra_json:
        return {}
    try:
        data = json.loads(extra_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--extra-json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("--extra-json must be a JSON object")
    return data


def _norm_ep(ep: str | None) -> str:
    e = (ep or "chat").strip().lower()
    if e in ("chat/completions", "/v1/chat/completions"):
        return "chat"
    if e in ("/v1/responses",):
        return "responses"
    if e in ("anthropic", "/v1/messages"):
        return "messages"
    return e


def ocr_image(
    png_bytes: bytes,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    detail: str | None = None,
    system: str | None = None,
    extra: dict | None = None,
    timeout: int | None = None,
) -> str:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    extra = extra or {}
    base_url, model, api_key, endpoint, detail = _resolve_ocr(base_url=base_url, model=model, api_key=api_key, endpoint=endpoint, detail=detail, system=system)
    if timeout is not None:
        extra = dict(extra)
        extra.setdefault("timeout", timeout)
    ep = _norm_ep(endpoint)
    if ep == "auto":
        _av = globals().get("auto_vision")
        if _av is None:
            raise RuntimeError("auto_vision unavailable (llm_client import failed)")
        content, _usage = _av(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **extra)  # type: ignore
        return content
    if ep == "chat":
        content, _usage = chat_vision(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **extra)
        return content
    if ep == "responses":
        # responses uses instructions for system-like prompt; keep ocr_system as input prompt
        content, _usage = responses_vision(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **extra)
        return content
    if ep == "messages":
        content, _usage = anthropic_vision(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, system=system, **extra)
        return content
    raise ValueError(f"unknown endpoint: {endpoint} (expected chat|responses|messages|auto)")


def ocr_image_url(
    image_url: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    detail: str | None = None,
    system: str | None = None,
    extra: dict | None = None,
    timeout: int | None = None,
) -> str:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    extra = extra or {}
    base_url, model, api_key, endpoint, detail = _resolve_ocr(base_url=base_url, model=model, api_key=api_key, endpoint=endpoint, detail=detail, system=system)
    if timeout is not None:
        extra = dict(extra)
        extra.setdefault("timeout", timeout)
    ep = _norm_ep(endpoint)
    if ep == "auto":
        _av = globals().get("auto_vision")
        if _av is None:
            raise RuntimeError("auto_vision unavailable (llm_client import failed)")
        content, _usage = _av(image_url, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **extra)  # type: ignore
        return content
    if ep == "chat":
        content, _usage = chat_vision_url(image_url, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **extra)
        return content
    if ep == "responses":
        content, _usage = responses_vision_url(image_url, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **extra)
        return content
    if ep == "messages":
        content, _usage = anthropic_vision_url(image_url, prompt, base_url=base_url, model=model, api_key=api_key, system=system, **extra)
        return content
    raise ValueError(f"unknown endpoint: {endpoint}")


def ocr_pdf_page(
    pdf_path: str | pathlib.Path,
    pno: int,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    detail: str | None = None,
    system: str | None = None,
    extra: dict | None = None,
    dpi: int = 200,
    timeout: int | None = None,
) -> str:
    png, _dpi_actual, _o, _f = _ladder_or_direct(pdf_path, pno, dpi)
    return ocr_image(png, base_url=base_url, model=model, api_key=api_key, endpoint=endpoint, detail=detail, system=system, extra=extra, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vision LLM OCR for a PDF page or PNG image (3 ingresses, full passthrough)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=pathlib.Path, help="Local PNG/JPG path (sent as base64 data URI)")
    source.add_argument("--image-url", type=str, help="Remote http(s) image URL (sent directly, no base64)")
    source.add_argument("--pdf", type=pathlib.Path, help="PDF path (rendered locally then sent)")
    parser.add_argument("--page", type=int, default=0, help="PDF page index, zero-based")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--base-url", type=str, default=None, help="Override LLM base URL, e.g. http://YOUR_GATEWAY_HOST:2113/v1")
    parser.add_argument("--model", type=str, default=None, help="Override model id")
    parser.add_argument("--api-key", type=str, default=None, help="Override API key (or set LLM_OCR_KEY env)")
    parser.add_argument("--endpoint", type=str, default="chat", help="chat | responses | messages (default chat). Octopus exposes all three on /v1/*")
    parser.add_argument("--detail", type=str, default="high", help="Image detail: high|low|auto (default high, recommended for IPA)")
    parser.add_argument("--system", type=str, default=None, help="System prompt override (Anthropic 'system'); for chat/responses it merges as extra.system if needed")
    parser.add_argument("--extra-json", type=str, default=None, help='JSON object merged into request body, e.g. \'{"temperature":0.2,"reasoning_effort":"low","max_completion_tokens":8192}\'')
    args = parser.parse_args()

    extra = _parse_extra(args.extra_json)
    # allow --system via positional too
    if args.system and "system" not in extra:
        extra_system = args.system
    else:
        extra_system = None

    # for anthropic, system should be passed as the dedicated param not just extra
    def _ocr_kwargs(ep: str) -> dict:
        kw: dict = {"base_url": args.base_url, "model": args.model, "api_key": args.api_key, "endpoint": ep, "detail": args.detail, "extra": extra}
        if ep.lower() in ("messages", "anthropic") and args.system:
            kw["system"] = args.system
        return kw

    if args.image is not None:
        data = args.image.read_bytes()
        # dispatch by endpoint to keep detail handling precise
        ep = args.endpoint.strip().lower()
        if ep in ("messages", "anthropic"):
            result = ocr_image(data, **_ocr_kwargs(ep))
        elif ep in ("responses",):
            result = ocr_image(data, **_ocr_kwargs(ep))
        else:
            result = ocr_image(data, **_ocr_kwargs("chat"))
    elif args.image_url is not None:
        result = ocr_image_url(args.image_url, **_ocr_kwargs(args.endpoint))
    else:
        result = ocr_pdf_page(args.pdf, args.page, **_ocr_kwargs(args.endpoint))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result + "\n", encoding="utf-8")
    print(f"written={args.output} endpoint={args.endpoint} detail={args.detail}")
    if extra:
        print(f"extra={json.dumps(extra, ensure_ascii=True)[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
