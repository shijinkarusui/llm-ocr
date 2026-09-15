"""W2: 统一入口 python -m src.cli (models/probe/ocr/batch/config/--tui). 旧入口冻结=不新增flag."""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

try:
    from .cli_common import add_llm_args, parse_extra_json, read_stdin_key
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cli_common import add_llm_args, parse_extra_json, read_stdin_key

try:
    from .config import resolve_config
except ImportError:
    try:
        from config import resolve_config  # type: ignore
    except ImportError:
        resolve_config = None  # type: ignore

import types


def _ns(args: argparse.Namespace, is_cli: bool = True) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        base_url=args.base_url, model=args.model, api_key=getattr(args, "api_key", None),
        key_stdin=bool(getattr(args, "key_stdin", False)),
        endpoint=args.endpoint, detail=args.detail, timeout=getattr(args, "timeout", None),
        concurrency=getattr(args, "concurrency", None), dpi=getattr(args, "dpi", None),
        retries=getattr(args, "retries", None), system=getattr(args, "system", None),
        extra=None, is_cli=is_cli,
    )


def _resolve_or_die(args: argparse.Namespace) -> tuple:
    try:
        from .config import read_key_stdin  # noqa: F401
        _has_cfg = True
    except ImportError:
        try:
            from config import read_key_stdin  # noqa: F401
            _has_cfg = True
        except ImportError:
            _has_cfg = False
    if not _has_cfg or resolve_config is None:
        raise RuntimeError("src/config.py missing (Lane A deliverable)")
    key = read_stdin_key(args)
    ns = _ns(args)
    ns.api_key = key
    g, r = resolve_config(ns)
    if getattr(args, "api_key", None):
        warnings.warn("--api-key passes key on command line; prefer env/--key-stdin", UserWarning)
    return g, r, key


def cmd_models(args: argparse.Namespace) -> int:
    g, r, key = _resolve_or_die(args)
    try:
        from .llm_client import list_models
    except ImportError:
        from llm_client import list_models  # type: ignore
    models = list_models(base_url=g.base_url, api_key=key or g.key)
    print(f"base_url={g.base_url} count={len(models)}")
    for m in models[:30]:
        extra = "" if not isinstance(m, dict) else (" owned_by=" + str(m.get("owned_by", "")) if m.get("owned_by") else "")
        print(f"- {m.get('id') if isinstance(m, dict) else m}{extra}")
    if not models:
        print("no models listed; fill --model manually (Octopus: keep OC/ prefix)")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    import time as _time

    g, r, key = _resolve_or_die(args)
    ep_req = (r.endpoint or "responses").strip().lower()
    try:
        from . import llm_client as C
    except ImportError:
        import llm_client as C  # type: ignore
    img = Path(args.image)
    if not img.is_file():
        print(f"probe image not found: {img}", file=sys.stderr)
        return 2
    data = img.read_bytes()
    prompt = "Transcribe this page exactly (OCR only):"
    import argparse as _ap2
    extra = parse_extra_json(args.extra_json, _ap2.ArgumentParser())
    xw = dict(extra) if extra else {}
    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        xw.setdefault("timeout", timeout)
    t0 = _time.monotonic()
    http_status: int | None = 200
    ep_norm = ep_req
    try:
        if ep_req == "auto":
            text, usage = C.auto_vision(data, prompt, base_url=g.base_url, model=g.model, api_key=key or g.key, detail=r.detail, **xw)
            ep_norm = str((usage or {}).get("_endpoint_normalized", "auto")) if isinstance(usage, dict) else "auto"
        elif ep_req == "responses":
            text, usage = C.responses_vision(data, prompt, base_url=g.base_url, model=g.model, api_key=key or g.key, detail=r.detail, **xw)
        elif ep_req == "chat":
            text, usage = C.chat_vision(data, prompt, base_url=g.base_url, model=g.model, api_key=key or g.key, detail=r.detail, **xw)
        elif ep_req in ("messages", "anthropic"):
            warnings.warn("messages experimental, no guarantee", UserWarning)
            text, usage = C.anthropic_vision(data, prompt, base_url=g.base_url, model=g.model, api_key=key or g.key, **xw)
        else:
            print(f"unknown endpoint: {ep_req}", file=sys.stderr)
            return 2
    except Exception as exc:
        http_status = getattr(exc, "status", None)
        if not isinstance(http_status, int):
            http_status = None
        print(f"probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed_ms = int((_time.monotonic() - t0) * 1000)
    out = Path("out/probe.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "endpoint_requested": ep_req,
        "endpoint_normalized": ep_norm,
        "resolved_endpoint": ep_norm,
        "base_url": g.base_url,
        "model": g.model,
        "detail": r.detail,
        "usage": usage if isinstance(usage, dict) else {},
        "elapsed_ms": elapsed_ms,
        "http_status": http_status,
        "text_preview": (text or "")[:400],
    }
    out.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"probe ok endpoint={ep_req}->{ep_norm} elapsed_ms={elapsed_ms} status={http_status} out={out}")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    try:
        from .ocr_page import ocr_image, ocr_pdf_page
    except ImportError:
        from ocr_page import ocr_image, ocr_pdf_page  # type: ignore
    g, r, key = _resolve_or_die(args)
    import argparse as _ap
    _p = _ap.ArgumentParser()
    extra = parse_extra_json(args.extra_json, _p)
    _xw = dict(extra) if extra else {}
    _to = getattr(args, "timeout", None)
    if _to is not None:
        _xw.setdefault("timeout", _to)
    if args.image is not None:
        data = Path(args.image).read_bytes()
        text = ocr_image(data, base_url=args.base_url or g.base_url, model=args.model or g.model,
                         api_key=key or g.key, endpoint=(args.endpoint or r.endpoint), detail=(args.detail or r.detail),
                         system=args.system, extra=_xw)
    else:
        text = ocr_pdf_page(args.pdf, args.page, base_url=args.base_url or g.base_url, model=args.model or g.model,
                            api_key=key or g.key, endpoint=(args.endpoint or r.endpoint), detail=(args.detail or r.detail),
                            system=args.system, extra=_xw, dpi=args.dpi)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text((text or "").rstrip() + "\n", encoding="utf-8")
    print(f"written={out} endpoint={args.endpoint or r.endpoint}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    try:
        from .batch_plan import page_plan, run_batch
    except ImportError:
        from batch_plan import page_plan, run_batch  # type: ignore
    if getattr(args, "dry_run", False):
        # W2: dry-run不联网, endpoint仅归一校验; messages打印experimental warn
        import fitz as _fitz
        from pathlib import Path as _P
        ep = (getattr(args, "endpoint", None) or "responses").strip().lower()
        if ep not in ("responses", "chat", "auto", "messages"):
            print(f"unknown endpoint: {ep}", file=sys.stderr)
            return 2
        if ep == "messages":
            warnings.warn("messages experimental, no guarantee", UserWarning)
        plan = page_plan(_P(args.pdf), _P(args.output_dir), args.start, args.end)
        with _fitz.open(str(args.pdf)) as doc:
            total_pages = doc.page_count
        print(f"dry_run=true total_pages={total_pages} planned_pages={len(plan)} dpi={args.dpi} concurrency={args.concurrency} retries={args.retries}")
        for item in plan[:5]:
            print(f"pno_0based={item['pno_0based']} page_number={item['page_number']} output={item['output']}")
        print(f"endpoint={ep} detail={args.detail or 'high'} base_url={args.base_url or 'env/default'} model={args.model or 'env/default'}")
        return 0
    g, r, key = _resolve_or_die(args)
    import argparse as _ap
    _p = _ap.ArgumentParser()
    extra = parse_extra_json(args.extra_json, _p)
    summary = run_batch(
        args.pdf, args.output_dir, dpi=args.dpi,
        concurrency=args.concurrency, retries=args.retries, prompt_path=args.prompt,
        start=args.start, end=args.end,
        base_url=(args.base_url or g.base_url), model=(args.model or g.model), api_key=(key or g.key),
        endpoint=(args.endpoint or r.endpoint), detail=(args.detail or r.detail), system=args.system, extra=extra,
        timeout=(getattr(args, "timeout", None) if getattr(args, "timeout", None) is not None else g.timeout),
    )
    print(f"batch_complete=true total_tokens={summary.get('total_tokens', 0)} failed={summary.get('failed_pages_this_run', 0)}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.init:
        return _config_init(args)
    g, r, key = _resolve_or_die(args)
    try:
        from .config import check as _check  # type: ignore
    except ImportError:
        try:
            from config import check as _check  # type: ignore
        except ImportError:
            _check = None  # type: ignore
    if _check is not None:
        print(_check())
    else:
        masked = ("sk-****" if key else "(empty)") + f" key_len={len(key) if key else 0}"
        print(f"base_url={g.base_url}\nmodel={g.model}\nkey={masked}\nendpoint={r.endpoint}\nconcurrency={r.concurrency}")
    return 0


def _config_init(args: argparse.Namespace) -> int:
    """W2: config --init 向导: base_url -> Key掩码/--key-stdin -> models选择/手填 -> endpoint -> concurrency -> 写.env."""
    base_url = input("base_url [http://YOUR_GATEWAY_HOST:2113/v1]: ").strip() or "http://YOUR_GATEWAY_HOST:2113/v1"
    import getpass
    key = getpass.getpass("api key (hidden, or leave empty to use env later): ").strip()
    models: list = []
    if key:
        try:
            try:
                from .llm_client import list_models
            except ImportError:
                from llm_client import list_models  # type: ignore
            models = list_models(base_url=base_url, api_key=key)
        except Exception as exc:
            print(f"models fetch failed ({exc}); fill manually")
            models = []
    if models:
        print("models:")
        for i, m in enumerate(models[:20]):
            print(f"  [{i}] {m.get('id') if isinstance(m, dict) else m}")
        sel = input("pick number or type model id [0]: ").strip() or "0"
        try:
            model = models[int(sel)].get("id") if isinstance(models[int(sel)], dict) else str(models[int(sel)])
        except (ValueError, IndexError):
            model = sel
    else:
        model = input("model [OC/muse-spark-1.3-contributor-free]: ").strip() or "OC/muse-spark-1.3-contributor-free"
    endpoint = (input("endpoint responses|chat|auto [responses]: ").strip() or "responses").lower()
    conc_raw = input("concurrency 1..20 [4]: ").strip() or "4"
    try:
        concurrency = max(1, min(20, int(conc_raw)))
    except ValueError:
        concurrency = 4
    try:
        from .config import write_env  # type: ignore
    except ImportError:
        from config import write_env  # type: ignore
    import types as _t
    ns = _t.SimpleNamespace(base_url=base_url, model=model, api_key=None, key_stdin=False,
                            endpoint=endpoint, detail="high", timeout=120, concurrency=concurrency,
                            dpi=200, retries=2, system=None, is_cli=True)
    g2, r2 = resolve_config(ns)
    path = write_env(Path(".env"))
    if key:
        print("key kept in memory only (not written to .env); export LLM_OCR_KEY=... or re-enter")
    print(f"wrote {path} (key not stored); run: python -m src.cli config --check")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    """W2: --tui内联薄问答 (解环; interactive.py为shim转调cli.main)."""
    print("llm-ocr tui: answer or Enter for default")
    base_url = input("base_url [env/default]: ").strip() or None
    model = input("model [env/default]: ").strip() or None
    endpoint = input("endpoint responses|chat|auto [responses]: ").strip() or "responses"
    conc = input("concurrency 1..20 [4]: ").strip() or "4"
    print(f"tui: base_url={base_url or 'env/default'} model={model or 'env/default'} endpoint={endpoint} concurrency={conc}")
    print("tui hint: python -m src.cli models / probe / batch --help")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="src.cli", description="llm-ocr unified CLI (W2)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pm = sub.add_parser("models", help="唯一模型拉取入口 GET /v1/models")
    add_llm_args(pm, include_concurrency=False)
    pm.set_defaults(func=cmd_models)
    pp = sub.add_parser("probe", help="单图探活 落out/probe.json")
    pp.add_argument("--image", type=str, required=True)
    add_llm_args(pp, include_concurrency=False)
    pp.set_defaults(func=cmd_probe)
    po = sub.add_parser("ocr", help="单页OCR")
    po.add_argument("--image", type=str, default=None)
    po.add_argument("--pdf", type=str, default=None)
    po.add_argument("--page", type=int, default=0)
    po.add_argument("--dpi", type=int, default=200)
    po.add_argument("--output", type=str, required=True)
    add_llm_args(po, include_concurrency=False)
    po.set_defaults(func=cmd_ocr)
    pb = sub.add_parser("batch", help="批量OCR")
    pb.add_argument("--pdf", type=str, required=True)
    pb.add_argument("--output-dir", type=str, default="out")
    pb.add_argument("--prompt", type=str, default="prompts/ocr_system.md")
    pb.add_argument("--dpi", type=int, default=200)
    pb.add_argument("--retries", type=int, default=2)
    pb.add_argument("--start", type=int, default=0)
    pb.add_argument("--end", type=int, default=None)
    pb.add_argument("--dry-run", action="store_true")
    add_llm_args(pb, include_concurrency=True)
    pb.set_defaults(func=cmd_batch if True else cmd_batch)
    pc = sub.add_parser("config", help="config --init/--check")
    pc.add_argument("--init", action="store_true")
    pc.add_argument("--check", action="store_true")
    add_llm_args(pc, include_concurrency=False)
    pc.set_defaults(func=cmd_config)
    p.add_argument("--tui", action="store_true", help="内联薄问答")
    return p


def main(argv: list[str] | None = None) -> int:
    # --tui为顶层flag (python -m src.cli --tui), 无子命令时走薄问答
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw == ["--tui"] or (len(raw) == 1 and raw[0] == "--tui"):
        return cmd_tui(argparse.Namespace())
    p = build_parser()
    args = p.parse_args(raw)
    # messages dry-run warn: batch --dry-run --endpoint messages 打印warn+303规划 (W2-2实现dry-run分支时)
    # endpoint choices校验 (messages experimental warn在各cmd内)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
