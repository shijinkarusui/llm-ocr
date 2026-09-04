from __future__ import annotations

try:  # dotenv FIRST import statement (plan v3: import line before docstring)
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    import warnings as _dotenv_warn
    _dotenv_warn.warn(
        "python-dotenv is not installed; .env files will NOT be auto-loaded. "
        "Install it with: pip install python-dotenv "
        "or export variables manually before running "
        "(Windows: set LLM_OCR_KEY=... / POSIX: export LLM_OCR_KEY=...)."
    )

"""llm-ocr central config (Lane A, plan v5 S4.1; CONTRACT #4/#9).

Split: GlobalConfig{base_url,model,key,timeout} vs
RunConfig{endpoint,detail,concurrency,dpi,retries,system,extra}.

Priority everywhere is CLI > env > DEFAULT. Full env table:
  base_url:    ns.base_url   > LLM_OCR_BASE_URL  > LLM_OCR_BASEURL > DEFAULT_BASE_URL
  model:       ns.model      > LLM_OCR_MODEL     > OCTOPUS_MODEL   > DEFAULT_MODEL
  key:         ns.api_key    > ns.key_stdin(bool)> LLM_OCR_KEY > LLM_OCR_API_KEY
                                   > OCTOPUS_API_KEY > OCTOPUS_KEY
  endpoint:    ns.endpoint   > LLM_OCR_ENDPOINT  > CLI default "responses" / lib default "chat"
  concurrency: ns.concurrency> LLM_OCR_CONCURRENCY > CLI default 4 / lib default 1
  detail:      ns.detail     > LLM_OCR_DETAIL    > "high"
  timeout:     ns.timeout    > LLM_OCR_TIMEOUT   > 90 (POST-only; GET is fixed 15s elsewhere)
  dpi/retries: CLI-only, NO env fallback (ns.dpi / ns.retries taken directly).
  LLM_OCR_PORT: deprecated; if set non-empty we warnings.warn and ignore it
    (base_url is the only endpoint knob).

CLI-vs-lib defaults are told apart by ns.is_cli (bool); missing/None means CLI.

Key hygiene: key lives in memory only. write_env() never writes plaintext key
(placeholder comment instead). check() prints only sk-**** + key_len.
OC/ conditional hint is NOT done here (llm_client._http_hint owns it, W0 part).
"""

import json
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_DETAIL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_DPI",
    "DEFAULT_RETRIES",
    "CLI_DEFAULT_ENDPOINT",
    "LIB_DEFAULT_ENDPOINT",
    "CLI_DEFAULT_CONCURRENCY",
    "LIB_DEFAULT_CONCURRENCY",
    "MIN_CONCURRENCY",
    "MAX_CONCURRENCY",
    "CONCURRENCY_WARN_ABOVE",
    "GlobalConfig",
    "RunConfig",
    "read_key_stdin",
    "resolve_config",
    "write_env",
    "check",
]

DEFAULT_BASE_URL = "http://YOUR_GATEWAY_HOST:2113/v1"
DEFAULT_MODEL = "OC/muse-spark-1.3-contributor-free"
DEFAULT_DETAIL = "high"
DEFAULT_TIMEOUT = 90
DEFAULT_DPI = 200
DEFAULT_RETRIES = 2
CLI_DEFAULT_ENDPOINT = "responses"
LIB_DEFAULT_ENDPOINT = "chat"
CLI_DEFAULT_CONCURRENCY = 4
LIB_DEFAULT_CONCURRENCY = 1
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 20
CONCURRENCY_WARN_ABOVE = 8


@dataclass
class GlobalConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    key: str = ""
    timeout: int = DEFAULT_TIMEOUT


@dataclass
class RunConfig:
    endpoint: str = CLI_DEFAULT_ENDPOINT
    detail: str = DEFAULT_DETAIL
    concurrency: int = CLI_DEFAULT_CONCURRENCY
    dpi: int = DEFAULT_DPI
    retries: int = DEFAULT_RETRIES
    system: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _is_cli(ns: Any) -> bool:
    """ns.is_cli tells CLI apart from lib use; missing/None counts as CLI."""
    if ns is None:
        return True
    v = getattr(ns, "is_cli", None)
    if v is None:
        return True
    return bool(v)


def _pick_str(cli_val: Any, env_names: tuple[str, ...], default: str) -> str:
    """CLI > env > DEFAULT for plain strings (empty/blank counts as unset)."""
    if cli_val is not None and str(cli_val).strip() != "":
        return str(cli_val).strip()
    for name in env_names:
        v = os.environ.get(name)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def _to_int(raw: Any, label: str) -> int:
    if isinstance(raw, bool):
        raise ValueError("invalid %s value %r: must be an integer" % (label, raw))
    try:
        return int(raw) if isinstance(raw, int) else int(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid %s value %r: must be an integer" % (label, raw))


def _pick_int(cli_val: Any, env_names: tuple[str, ...], default: int, label: str) -> int:
    """CLI > env > DEFAULT for ints; bad literals raise ValueError."""
    raw: Any = None
    if cli_val is not None and not (isinstance(cli_val, str) and cli_val.strip() == ""):
        raw = cli_val
    else:
        for name in env_names:
            v = os.environ.get(name)
            if v is not None and v.strip() != "":
                raw = v.strip()
                break
    if raw is None:
        return default
    return _to_int(raw, label)


def read_key_stdin() -> str:
    """Read the API key from a stdin pipe (helper for --key-stdin reuse).

    Reads all of sys.stdin and strips it. Raises RuntimeError when stdin is a
    TTY (no pipe), with usage guidance.
    """
    stdin = sys.stdin
    try:
        is_tty = bool(stdin.isatty())
    except Exception:
        is_tty = False
    if is_tty:
        raise RuntimeError(
            "--key-stdin needs a pipe, e.g. echo KEY | python -m src.cli ... --key-stdin "
            "(stdin is a TTY; refusing to read interactively)."
        )
    data = stdin.read()
    if data is None:
        return ""
    return str(data).strip()


def _resolve_key(ns: Any) -> str:
    cli_key = getattr(ns, "api_key", None) if ns is not None else None
    if cli_key is not None and str(cli_key).strip() == "":
        cli_key = None
    key_stdin = bool(getattr(ns, "key_stdin", False)) if ns is not None else False
    if key_stdin and cli_key is not None:
        warnings.warn("--api-key wins over --key-stdin (both given); stdin ignored.")
        return str(cli_key)
    if key_stdin:
        return read_key_stdin()
    if cli_key is not None:
        if _is_cli(ns):
            warnings.warn(
                "--api-key puts the key on the command line/shell history; "
                "prefer --key-stdin (pipe) or the LLM_OCR_KEY env var."
            )
        return str(cli_key)
    for name in ("LLM_OCR_KEY", "LLM_OCR_API_KEY", "OCTOPUS_API_KEY", "OCTOPUS_KEY"):
        v = os.environ.get(name)
        if v is not None and v.strip() != "":
            return v.strip()
    return ""


def _resolve_concurrency(ns: Any) -> int:
    cli_val = getattr(ns, "concurrency", None) if ns is not None else None
    default = CLI_DEFAULT_CONCURRENCY if _is_cli(ns) else LIB_DEFAULT_CONCURRENCY
    iv = _pick_int(cli_val, ("LLM_OCR_CONCURRENCY",), default, "concurrency")
    if iv < MIN_CONCURRENCY or iv > MAX_CONCURRENCY:
        raise ValueError(
            "concurrency %d out of range %d..%d" % (iv, MIN_CONCURRENCY, MAX_CONCURRENCY)
        )
    if iv > CONCURRENCY_WARN_ABOVE:
        warnings.warn(
            "concurrency %d > %d: high parallelism risks 429/overload; "
            "watch 429_count and lower it on failures." % (iv, CONCURRENCY_WARN_ABOVE)
        )
    return iv


def _parse_extra(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if s == "":
            return {}
        try:
            v = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError("invalid extra JSON: %s" % e)
        if not isinstance(v, dict):
            raise ValueError("invalid extra JSON: top level must be an object")
        return v
    raise ValueError("invalid extra value %r: must be a dict or JSON object str" % (raw,))


def _warn_port_deprecated() -> None:
    if os.environ.get("LLM_OCR_PORT", "").strip() != "":
        warnings.warn(
            "LLM_OCR_PORT is deprecated and ignored; "
            "base_url is the only endpoint knob (use --base-url / LLM_OCR_BASE_URL)."
        )


def resolve_config(ns: Any = None) -> tuple[GlobalConfig, RunConfig]:
    """Resolve (GlobalConfig, RunConfig) with CLI > env > DEFAULT priority."""
    _warn_port_deprecated()
    cli = ns
    base_url = _pick_str(
        getattr(cli, "base_url", None) if cli is not None else None,
        ("LLM_OCR_BASE_URL", "LLM_OCR_BASEURL"),
        DEFAULT_BASE_URL,
    )
    model = _pick_str(
        getattr(cli, "model", None) if cli is not None else None,
        ("LLM_OCR_MODEL", "OCTOPUS_MODEL"),
        DEFAULT_MODEL,
    )
    key = _resolve_key(cli)
    timeout = _pick_int(
        getattr(cli, "timeout", None) if cli is not None else None,
        ("LLM_OCR_TIMEOUT",),
        DEFAULT_TIMEOUT,
        "timeout",
    )
    default_ep = CLI_DEFAULT_ENDPOINT if _is_cli(cli) else LIB_DEFAULT_ENDPOINT
    endpoint = _pick_str(
        getattr(cli, "endpoint", None) if cli is not None else None,
        ("LLM_OCR_ENDPOINT",),
        default_ep,
    ).lower()
    detail = _pick_str(
        getattr(cli, "detail", None) if cli is not None else None,
        ("LLM_OCR_DETAIL",),
        DEFAULT_DETAIL,
    )
    concurrency = _resolve_concurrency(cli)
    # DPI/RETRIES are CLI-only: taken from ns directly, no env fallback.
    dpi_raw = getattr(cli, "dpi", None) if cli is not None else None
    dpi = DEFAULT_DPI if dpi_raw is None else _to_int(dpi_raw, "dpi")
    ret_raw = getattr(cli, "retries", None) if cli is not None else None
    retries = DEFAULT_RETRIES if ret_raw is None else _to_int(ret_raw, "retries")
    system = getattr(cli, "system", None) if cli is not None else None
    if system is not None and isinstance(system, str) and system.strip() == "":
        system = None
    extra = _parse_extra(getattr(cli, "extra", None) if cli is not None else None)
    g = GlobalConfig(base_url=base_url, model=model, key=key, timeout=timeout)
    r = RunConfig(
        endpoint=endpoint,
        detail=detail,
        concurrency=concurrency,
        dpi=dpi,
        retries=retries,
        system=system,
        extra=extra,
    )
    return g, r


def write_env(path: str | Path) -> Path:
    """Write current resolve result to a .env file; never writes plaintext key.

    base_url/model/endpoint/concurrency/detail/timeout are echoed verbatim;
    the key slot is a placeholder comment only. Returns the path.
    """
    p = Path(path)
    g, r = resolve_config(None)
    nl = chr(10)
    lines = [
        "# Generated by llm-ocr config.write_env. Key is NEVER written here.",
        "# Set exactly one of LLM_OCR_KEY / LLM_OCR_API_KEY / OCTOPUS_API_KEY / OCTOPUS_KEY",
        "# in your environment, or use --key-stdin with a pipe (echo KEY | ... --key-stdin).",
        "LLM_OCR_BASE_URL=" + g.base_url,
        "LLM_OCR_MODEL=" + g.model,
        "# LLM_OCR_KEY=<placeholder: fill your own key here or export env; plaintext key never written>",
        "LLM_OCR_ENDPOINT=" + r.endpoint,
        "LLM_OCR_CONCURRENCY=" + str(r.concurrency),
        "LLM_OCR_DETAIL=" + r.detail,
        "LLM_OCR_TIMEOUT=" + str(g.timeout),
    ]
    content = nl.join(lines) + nl
    parent = p.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def check() -> str:
    """Masked config dump: key shows sk-**** + key_len, rest verbatim. Returns str."""
    g, r = resolve_config(None)
    if g.key:
        key_disp = "sk-**** (key_len=%d)" % len(g.key)
    else:
        key_disp = "(empty) (key_len=0)"
    lines = [
        "base_url=%s" % g.base_url,
        "model=%s" % g.model,
        "key=%s" % key_disp,
        "timeout=%d" % g.timeout,
        "endpoint=%s" % r.endpoint,
        "detail=%s" % r.detail,
        "concurrency=%d" % r.concurrency,
        "dpi=%d" % r.dpi,
        "retries=%d" % r.retries,
        "system=%s" % (r.system if r.system is not None else "(none)"),
        "extra=%s" % (json.dumps(r.extra, ensure_ascii=True) if r.extra else "{}"),
    ]
    return chr(10).join(lines) + chr(10)
