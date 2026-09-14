from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

try:
    import msvcrt as _msvcrt  # Windows file lock
    _HAS_MSVCRT = True
except ImportError:
    _msvcrt = None  # type: ignore
    _HAS_MSVCRT = False
try:
    import fcntl as _fcntl  # POSIX file lock
    _HAS_FCNTL = True
except ImportError:
    _fcntl = None  # type: ignore
    _HAS_FCNTL = False

try:
    from .llm_client import anthropic_vision, auto_vision, chat_vision, responses_vision
    from .render import render_page
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import anthropic_vision, auto_vision, chat_vision, responses_vision
    except ImportError:
        from llm_client import anthropic_vision, chat_vision, responses_vision
        auto_vision = None  # type: ignore  # test injection: tests may set batch_plan.auto_vision
    from render import render_page

try:
    from .postprocess import merge_pages
except ImportError:
    from postprocess import merge_pages  # type: ignore

try:
    from .book_id import book_json_path, page_numbers, pdf_page_count, update_book
except ImportError:
    from book_id import book_json_path, page_numbers, pdf_page_count, update_book  # type: ignore

try:
    from .config import resolve_config
except ImportError:
    try:
        from config import resolve_config  # type: ignore
    except ImportError:
        from dataclasses import dataclass as _dc, field as _df
        @_dc
        class _FBGlobal:
            base_url: str = "http://YOUR_GATEWAY_HOST:2113/v1"
            model: str = "OC/muse-spark-1.3-contributor-free"
            key: str = ""
            timeout: int = 90
        @_dc
        class _FBRun:
            endpoint: str = "chat"
            detail: str = "high"
            concurrency: int = 1
            dpi: int = 200
            retries: int = 2
            system: str | None = None
            extra: dict = _df(default_factory=dict)
        def resolve_config(ns=None):  # type: ignore
            return _FBGlobal(), _FBRun()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = Path("")
DEFAULT_PROMPT = ROOT / "prompts" / "ocr_system.md"
DEFAULT_OUT = ROOT / "out"
DEFAULT_DPI = 200
DEFAULT_CONCURRENCY = 1
DEFAULT_RETRIES = 2
_APPEND_LOCK = threading.Lock()
SCHEMA_VERSION = 1
_PAGE_FILE_RE = re.compile(r"^page_(\d+)\.md$")
# Windows-illegal filename chars: a PDF name must never break the book folder.
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows device names cannot be used as directories, even with an extension gone.
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _book_stem(pdf_path: str | Path) -> str:
    """Book title = PDF file name without its extension (last dotted segment only).

    `Path.stem` handles spaces / CJK / multi-dot names: "a.b.pdf" -> "a.b";
    illegal Windows path characters are replaced, a trailing dot is dropped and
    reserved device names are suffixed.
    """
    stem = _BAD_NAME_CHARS.sub("_", Path(pdf_path).stem).strip().rstrip(".")
    if stem.upper() in _RESERVED_NAMES:
        stem = f"{stem}_book"
    return stem or "book"


def book_output_dir(pdf_path: str | Path, output_dir: str | Path) -> Path:
    """Per-book root: `<output_dir>/<pdf stem>` (created lazily, reused if present).

    Keeps several books in one OCR output dir from mixing into a single `pages/`.
    """
    return Path(output_dir) / _book_stem(pdf_path)


def _collect_book_pages(pages_dir: str | Path) -> dict[int, str]:
    """Read every `page_<n>.md` that actually exists under `pages_dir`.

    Keys are 1-based page numbers for `merge_pages` (file `page_0000.md` -> page 1),
    so a segmented resume naturally accumulates into one whole-book mapping.
    Unreadable or non-numeric `page_*.md` files are skipped.
    """
    source = Path(pages_dir)
    pages: dict[int, str] = {}
    if not source.is_dir():
        return pages
    for path in sorted(source.glob("page_*.md")):
        match = _PAGE_FILE_RE.match(path.name)
        if match is None:
            continue
        try:
            pages[int(match.group(1)) + 1] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a broken page file must not sink the whole book
    return pages


def merge_book_markdown(
    pdf_path: str | Path,
    book_root: str | Path,
    *,
    pages_dir: str | Path | None = None,
) -> Path | None:
    """Merge existing page files into `<book_root>/<pdf stem>-ocr.md`.

    Reuses `postprocess.merge_pages` (which runs `clean_md` per page), so the book
    file gets the `# <title>` / `## Page Index` / `<!-- PAGE n -->` structure and
    CJK punctuation normalization for free. Returns None when no page file exists
    (no empty book file is written).
    """
    root = Path(book_root)
    source = Path(pages_dir) if pages_dir is not None else root / "pages"
    pages = _collect_book_pages(source)
    if not pages:
        return None
    title = _book_stem(pdf_path)
    target = root / f"{title}-ocr.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(merge_pages(pages, title=title), encoding="utf-8")
    return target


def page_plan(
    pdf_path: str | Path,
    output_dir: str | Path,
    start: int = 0,
    end: int | None = None,
) -> list[dict[str, Any]]:
    """Build a deterministic plan without rendering or making network calls.

    `output_dir` is the dir the user typed; page files live in
    `<output_dir>/<pdf stem>/pages/` (see `book_output_dir`).
    """
    import fitz

    with fitz.open(str(pdf_path)) as document:
        page_count = document.page_count
    if start < 0 or start > page_count:
        raise ValueError("start page is outside the PDF")
    stop = page_count if end is None else min(end, page_count)
    if stop < start:
        raise ValueError("end page must be greater than or equal to start")
    pages_dir = book_output_dir(pdf_path, output_dir) / "pages"
    return [
        {
            "pno_0based": pno,
            "page_number": pno + 1,
            "output": pages_dir / f"page_{pno:04d}.md",
        }
        for pno in range(start, stop)
    ]


def _usage_counts(usage: dict[str, Any]) -> tuple[int, int, int]:
    """W2: 新网关键(input_tokens/output_tokens)优先, 旧键(prompt_tokens/completion_tokens)回退."""
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return prompt, completion, total


def _lock_file(lock_path: Path, timeout_s: float = 30.0):
    """W0: 跨进程文件锁上下文. threading.Lock由调用方持有,此处只做进程间互斥."""
    from contextlib import contextmanager

    @contextmanager
    def _mgr():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+b")
        try:
            if _HAS_MSVCRT:
                deadline = time.time() + timeout_s
                while True:
                    try:
                        fh.seek(0)
                        _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.time() > deadline:
                            raise TimeoutError(f"file lock timeout: {lock_path}")
                        time.sleep(0.05)
                try:
                    yield fh
                finally:
                    try:
                        fh.seek(0)
                        _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            elif _HAS_FCNTL:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
                try:
                    yield fh
                finally:
                    _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            else:
                yield fh  # 无锁平台:仅线程锁保护(同进程),跨进程不保证
        finally:
            fh.close()

    return _mgr()


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """W0: 双锁顺序 threading.Lock -> file-lock(usage.jsonl.lock) -> open(a)+write+flush+os.fsync."""
    line = json.dumps(record, ensure_ascii=True, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _APPEND_LOCK:
        with _lock_file(lock_path):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass


DPI_LADDER = (200, 150, 120)
PAYLOAD_B64_LIMIT = 1_500_000


def _normalize_ep(ep: str | None) -> str:
    e = (ep or "chat").strip().lower()
    if e in ("chat/completions", "/v1/chat/completions"):
        return "chat"
    if e in ("/v1/responses",):
        return "responses"
    if e in ("anthropic", "/v1/messages"):
        return "messages"
    return e


def _ladder_tiers(dpi: int) -> list[int]:
    """C1(契约#8): 档[200,150,120]取<=请求dpi的后缀; 请求>200从200起; 请求<120只试请求值."""
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        dpi = 200
    if dpi < 120:
        return [dpi]
    return [d for d in DPI_LADDER if d <= dpi] or [DPI_LADDER[-1]]


def _ladder_render(
    pdf_path: Path,
    pno: int,
    dpi: int,
    *,
    limit: int = PAYLOAD_B64_LIMIT,
) -> tuple[bytes, int, int, int]:
    """C1(契约#8, ladder住caller): 逐档render+真编base64测长.
    返回 (png_bytes, dpi_actual, bytes_orig, bytes_final). 首档即记orig; 耗尽仍超抛ValueError(payload_too_large)."""
    import base64 as _b64

    tiers = _ladder_tiers(dpi)
    first_len: int | None = None
    for tier in tiers:
        png = render_page(pdf_path, pno, tier)
        blen = len(_b64.b64encode(png))
        if first_len is None:
            first_len = blen
        if blen <= limit:
            if tier != tiers[0]:
                warnings.warn(f"b64 {blen} > ... downgraded dpi {tiers[0]} -> {tier}", UserWarning)
            return png, tier, int(first_len), int(blen)
        warnings.warn(f"b64 {blen} > {limit} at dpi {tier}, trying lower", UserWarning)
    raise ValueError(f"payload_too_large: b64 still > {limit} after tiers {tiers}")


def _resolve_run(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    detail: str | None = None,
    system: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """C1 None-默认-then-resolve: 显参优先, None落到resolve_config(ns)(CLI>env>DEFAULT, 库默认)."""
    import types as _types

    _rc = None
    try:
        from .config import resolve_config as _rc  # type: ignore
    except ImportError:
        try:
            from config import resolve_config as _rc  # type: ignore
        except ImportError:
            _rc = None
    if _rc is None:
        return {
            "base_url": base_url, "model": model, "key": api_key or "",
            "requested": endpoint if endpoint is not None else "chat",
            "detail": detail if detail is not None else "high",
            "timeout": timeout if timeout is not None else 90,
        }
    ns = _types.SimpleNamespace(
        base_url=base_url, model=model, api_key=api_key, key_stdin=False,
        endpoint=endpoint, detail=detail, system=system, timeout=timeout, is_cli=False,
    )
    g, r = _rc(ns)
    return {
        "base_url": g.base_url, "model": g.model, "key": g.key,
        "requested": endpoint if endpoint is not None else r.endpoint,
        "detail": r.detail,
        "timeout": timeout if timeout is not None else g.timeout,
    }


class _AdaptiveLimiter:
    """C2(契约#7/§4.4): 动态上限初值=concurrency; 429每次计1+该worker退避+半减(下限1); 连续20成功+1至初值."""

    def __init__(self, init: int):
        self.init = max(1, int(init))
        self.limit = self.init
        self.in_flight = 0
        self.ok_streak = 0
        self.n429 = 0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            while self.in_flight >= self.limit:
                self._cond.wait()
            self.in_flight += 1

    def release(self) -> None:
        with self._cond:
            self.in_flight = max(0, self.in_flight - 1)
            self._cond.notify_all()

    def on_429(self) -> None:
        with self._cond:
            self.n429 += 1
            self.ok_streak = 0
            self.limit = max(1, self.limit // 2)
        time.sleep(1.0)

    def on_success(self) -> None:
        with self._cond:
            self.ok_streak += 1
            if self.ok_streak >= 20 and self.limit < self.init:
                self.limit += 1
                self.ok_streak = 0

    def on_fail(self) -> None:
        with self._cond:
            self.ok_streak = 0


def _process_one(
    item: dict[str, Any],
    pdf_path: Path,
    prompt: str,
    dpi: int,
    usage_path: Path,
    retries: int,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    detail: str | None = None,
    system: str | None = None,
    extra: dict[str, Any] | None = None,
    timeout: int | None = None,
    _limiter: _AdaptiveLimiter | None = None,
) -> dict[str, Any]:
    _res = _resolve_run(base_url=base_url, model=model, api_key=api_key, endpoint=endpoint, detail=detail, system=system, timeout=timeout)
    base_url = _res["base_url"]
    timeout = _res["timeout"]
    model = _res["model"]
    api_key = _res["key"]
    endpoint_requested = _res["requested"]
    detail = _res["detail"]
    output = Path(item["output"])
    if output.exists() and output.stat().st_size > 0:
        # W0契约第5条: skipped sentinels必落盘(duration 0/null/attempt 0/dpi_actual=dpi/bytes 0/tokens 0)
        _rec = {
            "schema_version": SCHEMA_VERSION,
            "pno_0based": item["pno_0based"],
            "page_number": item["page_number"],
            "status": "skipped",
            "attempt": 0,
            "endpoint_requested": endpoint_requested,
            "endpoint_normalized": endpoint_requested,
            "base_url_resolved": base_url,
            "model_resolved": model,
            "detail": detail,
            "dpi": dpi,
            "dpi_actual": dpi,
            "image_bytes_orig": 0,
            "image_bytes_final": 0,
            "image_bytes": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "duration_ms": 0,
            "http_status": None,
            "endpoint": endpoint_requested,
            "base_url": base_url,
            "model": model,
            "output": str(output),
        }
        try:
            _append_jsonl(usage_path, _rec)
        except Exception:
            pass
        return {
            "pno_0based": item["pno_0based"],
            "page_number": item["page_number"],
            "status": "skipped",
            "output": str(output),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    last_error = "unknown error"
    last_status: int | None = None
    extra = dict(extra) if extra else {}
    # don't mutate caller's extra across retries — work on a copy per attempt
    ep = _normalize_ep(endpoint_requested)
    # C1: auto_vision经globals()取(模块顶已import; 单测monkeypatch batch_plan.auto_vision生效; 禁止此处重import绕过替身)
    _auto_fn = globals().get("auto_vision") if ep == "auto" else None
    # C1: ladder预检一次(渲染最贵, 重试不重渲)
    dpi_actual, bytes_orig, bytes_final = dpi, 0, 0
    try:
        png_bytes, dpi_actual, bytes_orig, bytes_final = _ladder_render(pdf_path, item["pno_0based"], dpi)
    except ValueError as exc:
        record = {
            "schema_version": SCHEMA_VERSION,
            "pno_0based": item["pno_0based"],
            "page_number": item["page_number"],
            "status": "failed",
            "attempt": 1,
            "endpoint_requested": endpoint_requested,
            "endpoint_normalized": "auto" if ep == "auto" else ep,
            "base_url_resolved": base_url,
            "model_resolved": model,
            "detail": detail,
            "dpi": dpi,
            "dpi_actual": dpi,
            "image_bytes_orig": 0,
            "image_bytes_final": 0,
            "image_bytes": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "duration_ms": 0,
            "http_status": None,
            "error": f"ValueError: {exc}",
        }
        if extra:
            record["extra"] = extra
        _append_jsonl(usage_path, record)
        return record
    for attempt in range(retries + 1):
        _t0 = time.monotonic()
        if _limiter is not None:
            _limiter.acquire()
        try:
            # per-attempt copy to isolate pop of 'system' for anthropic
            _extra = dict(extra)
            if timeout is not None:
                _extra.setdefault("timeout", timeout)
            ep_norm = ep
            attempt_no = attempt + 1
            if ep == "auto":
                if _auto_fn is None:
                    raise RuntimeError("auto_vision unavailable (llm_client import failed)")
                content, usage = _auto_fn(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **_extra)
                if isinstance(usage, dict):
                    ep_norm = str(usage.get("_endpoint_normalized", "auto") or "auto")
                    try:
                        attempt_no = int(usage.get("_leg", 1) or 1)
                    except (TypeError, ValueError):
                        attempt_no = 1
            elif ep == "responses":
                content, usage = responses_vision(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **_extra)
            elif ep == "messages":
                sys_prompt = system if system is not None else _extra.pop("system", None)
                if sys_prompt is not None:
                    content, usage = anthropic_vision(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, system=sys_prompt, **_extra)
                else:
                    content, usage = anthropic_vision(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, **_extra)
            else:
                content, usage = chat_vision(png_bytes, prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, **_extra)
                ep_norm = "chat"
            if not (content or "").strip():
                raise ValueError("empty OCR output (blank page or model refusal); retry or rotate")
            prompt_tokens, completion_tokens, total_tokens = _usage_counts(usage if isinstance(usage, dict) else {})
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content.rstrip() + "\n", encoding="utf-8")
            _dur = int((time.monotonic() - _t0) * 1000)
            record = {
                "schema_version": SCHEMA_VERSION,
                "pno_0based": item["pno_0based"],
                "page_number": item["page_number"],
                "status": "success",
                "attempt": attempt_no,
                "endpoint_requested": endpoint_requested,
                "endpoint_normalized": ep_norm,
                "base_url_resolved": base_url,
                "model_resolved": model,
                "detail": detail,
                "dpi": dpi,
                "dpi_actual": dpi_actual,
                "image_bytes_orig": bytes_orig,
                "image_bytes_final": bytes_final,
                "image_bytes": bytes_final,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "endpoint": endpoint_requested,
                "base_url": base_url,
                "model": model,
                "duration_ms": _dur,
                "http_status": 200,
            }
            if extra:
                record["extra"] = extra
            _append_jsonl(usage_path, record)
            if _limiter is not None:
                _limiter.release()
                _limiter.on_success()
            return record
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _st = None
            try:
                _st = getattr(exc, "status", None)
                if isinstance(_st, int):
                    last_status = _st
            except Exception:
                pass
            if _limiter is not None:
                _limiter.release()
                if _st == 429:
                    _limiter.on_429()
                else:
                    _limiter.on_fail()
    record = {
        "schema_version": SCHEMA_VERSION,
        "pno_0based": item["pno_0based"],
        "page_number": item["page_number"],
        "status": "failed",
        "attempt": retries + 1,
        "endpoint_requested": endpoint_requested,
        "endpoint_normalized": _normalize_ep(endpoint_requested),
        "base_url_resolved": base_url,
        "model_resolved": model,
        "detail": detail,
        "dpi": dpi,
        "dpi_actual": dpi_actual,
        "image_bytes_orig": bytes_orig,
        "image_bytes_final": bytes_final,
        "image_bytes": bytes_final,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "error": last_error,
        "endpoint": endpoint_requested,
        "base_url": base_url,
        "model": model,
        "duration_ms": 0,
        "http_status": last_status,
    }
    if extra:
        record["extra"] = extra
    _append_jsonl(usage_path, record)
    return record


def estimate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
) -> float | None:
    if input_price_per_million is None or output_price_per_million is None:
        return None
    return (
        prompt_tokens * input_price_per_million
        + completion_tokens * output_price_per_million
    ) / 1_000_000


def _read_new(record: dict[str, Any], new_key: str, *old_keys: str, default: Any = None) -> Any:
    """C2 reader回退: 新键优先旧键(缺键缺行不炸)."""
    try:
        v = record.get(new_key)
    except AttributeError:
        return default
    if v is not None:
        return v
    for k in old_keys:
        try:
            ov = record.get(k)
        except AttributeError:
            continue
        if ov is not None:
            return ov
    return default


def _pctl(sorted_vals: list[int], pct: float) -> int:
    """C2 就近秩百分位(p50/p95口径=duration_ms分布; 空表返回0)."""
    if not sorted_vals:
        return 0
    import math
    k = max(1, min(len(sorted_vals), int(math.ceil(pct / 100.0 * len(sorted_vals)))))
    return int(sorted_vals[k - 1])


def summarize_usage(
    usage_path: str | Path,
    output_path: str | Path | None = None,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    rate_429_count: int = 0,
) -> dict[str, Any]:
    """C2(契约#7): 按pno_0based取最新去重; records总行/pages去重页; 成功/失败/跳过按页计; 429_count+p50/p95."""
    path = Path(usage_path)
    records: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                records.append(row)
    latest: dict[Any, dict[str, Any]] = {}
    for record in records:
        key = record.get("pno_0based", record.get("page_number"))
        latest[key] = record  # 后行覆盖先行
    pages = list(latest.values())
    success = [r for r in pages if r.get("status") == "success"]
    prompt_tokens = sum(int(r.get("prompt_tokens") or 0) for r in success)
    completion_tokens = sum(int(r.get("completion_tokens") or 0) for r in success)
    durs = sorted(int(_read_new(r, "duration_ms", default=0) or 0) for r in success)
    summary = {
        "records": len(records),
        "pages": len(pages),
        "successful_pages": len(success),
        "failed_pages": sum(r.get("status") == "failed" for r in pages),
        "skipped_pages": sum(r.get("status") == "skipped" for r in pages),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in success),
        "429_count": int(rate_429_count),
        "elapsed_p50_ms": _pctl(durs, 50),
        "elapsed_p95_ms": _pctl(durs, 95),
        "input_price_per_million": input_price_per_million,
        "output_price_per_million": output_price_per_million,
        "estimated_cost": estimate_cost(
            prompt_tokens,
            completion_tokens,
            input_price_per_million,
            output_price_per_million,
        ),
        "cost_note": "Pricing is unset until provider input/output rates are supplied.",
    }
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return summary


def run_batch(
    pdf_path: str | Path,
    output_dir: str | Path,
    dpi: int = DEFAULT_DPI,
    concurrency: int | None = None,
    retries: int = DEFAULT_RETRIES,
    prompt_path: str | Path = DEFAULT_PROMPT,
    start: int = 0,
    end: int | None = None,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    detail: str | None = None,
    system: str | None = None,
    extra: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    if concurrency is None:
        try:
            from .config import resolve_config as _rrc  # type: ignore
        except ImportError:
            try:
                from config import resolve_config as _rrc  # type: ignore
            except ImportError:
                _rrc = None  # type: ignore
        concurrency = _rrc(None)[1].concurrency if _rrc is not None else DEFAULT_CONCURRENCY
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1 or concurrency > 20:
        raise ValueError("concurrency must be an int in 1..20")
    if concurrency > 8:
        warnings.warn(f"concurrency {concurrency} > 8: throughput may not scale, watch p95/429", UserWarning)
    if retries < 0:
        raise ValueError("retries cannot be negative")
    # Per-book root: <output_dir>/<pdf stem>/ holds pages/, usage.jsonl and the
    # merged book Markdown; page_plan resolves pages/ with the same rule.
    root = book_output_dir(pdf_path, output_dir)
    usage_path = root / "usage.jsonl"
    plan = page_plan(pdf_path, output_dir, start, end)
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    limiter = _AdaptiveLimiter(concurrency)
    results: list[dict[str, Any]] = []
    if concurrency == 1:
        results = [
            _process_one(item, Path(pdf_path), prompt, dpi, usage_path, retries, base_url=base_url, model=model, api_key=api_key, endpoint=endpoint, timeout=timeout, detail=detail, system=system, extra=extra, _limiter=limiter)
            for item in plan
        ]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(
                    _process_one,
                    item,
                    Path(pdf_path),
                    prompt,
                    dpi,
                    usage_path,
                    retries,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    endpoint=endpoint,
                    detail=detail,
                    system=system,
                    extra=extra,
                    timeout=timeout,
                    _limiter=limiter,
                )
                for item in plan
            ]
            results = [future.result() for future in as_completed(futures)]
    summary = summarize_usage(
        usage_path,
        root / "usage_summary.json",
        input_price_per_million,
        output_price_per_million,
        rate_429_count=limiter.n429,
    )
    summary["planned_pages"] = len(plan)
    summary["skipped_pages_this_run"] = sum(
        result.get("status") == "skipped" for result in results
    )
    summary["failed_pages_this_run"] = sum(
        result.get("status") == "failed" for result in results
    )
    summary["book_dir"] = str(root)
    # Whole-book Markdown from every page file on disk (not just this run's slice),
    # so segmented resumes keep growing the same <stem>-ocr.md.
    try:
        merged_md = merge_book_markdown(pdf_path, root)
    except (OSError, UnicodeDecodeError) as exc:
        merged_md = None
        summary["merged_md_error"] = f"{type(exc).__name__}: {exc}"
        warnings.warn(f"merge_pages failed for {root}: {exc}", UserWarning)
    if merged_md is not None:
        summary["merged_md"] = str(merged_md)
        summary["merged_pages"] = len(_collect_book_pages(root / "pages"))
    # Identity file: binds this folder to its source PDF, so the dual-layer step
    # can prove it is using the right book instead of guessing. Credential-free.
    try:
        effective = _resolve_run(base_url=base_url, model=model, api_key=api_key,
                                 endpoint=endpoint, detail=detail, system=system,
                                 timeout=timeout)
        record = update_book(
            root,
            source_pdf=pdf_path,
            page_count=pdf_page_count(pdf_path),
            pages_done=page_numbers(root / "pages"),
            merged_md=merged_md.name if merged_md is not None else None,
            model=effective.get("model"),
            endpoint=effective.get("requested"),
        )
        summary["book_json"] = str(book_json_path(root))
        summary["book_source_pdf"] = record.get("source_pdf")
        summary["book_pages_done"] = len(record.get("pages_done") or [])
    except Exception as exc:  # noqa: BLE001 - identity write must not sink a finished batch
        summary["book_json_error"] = f"{type(exc).__name__}: {exc}"
        warnings.warn(f"book.json write failed for {root}: {exc}", UserWarning)
    if base_url:
        summary["base_url"] = base_url
    if model:
        summary["model"] = model
    summary["endpoint"] = endpoint
    if extra:
        summary["extra"] = extra
    if summary["failed_pages_this_run"] > 0 and limiter.n429 > 0:
        summary["note"] = "429 observed with failures: consider lowering --concurrency and resuming"
    return summary


def _print_dry_run(
    plan: Iterable[dict[str, Any]],
    total_pages: int,
    dpi: int,
    concurrency: int,
    retries: int,
) -> None:
    items = list(plan)
    print(
        f"dry_run=true total_pages={total_pages} planned_pages={len(items)} "
        f"dpi={dpi} concurrency={concurrency} retries={retries}"
    )
    for item in items[:5]:
        # Real destination (already includes the per-book folder), not a guess.
        print(
            f"pno_0based={item['pno_0based']} page_number={item['page_number']} "
            f"output={item['output']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumable vision OCR batch framework (base_url+endpoint flexible, full passthrough)")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int)
    parser.add_argument("--base-url", type=str, default=None, help="Override LLM base URL, e.g. http://YOUR_GATEWAY_HOST:2113/v1")
    parser.add_argument("--model", type=str, default=None, help="Override model id")
    parser.add_argument("--api-key", type=str, default=None, help="Override API key (or set LLM_OCR_KEY env)")
    parser.add_argument("--endpoint", type=str, default=None, help="responses|chat|auto (+experimental messages; None=resolve default)")
    parser.add_argument("--detail", type=str, default=None, help="Image detail high|low|auto (default high)")
    parser.add_argument("--system", type=str, default=None, help="System prompt (Anthropic)")
    parser.add_argument("--extra-json", type=str, default=None, help='JSON object merged into request body, e.g. \'{"temperature":0.2,"reasoning_effort":"low"}\'')
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    extra = None
    if args.extra_json:
        try:
            extra = json.loads(args.extra_json)
            if not isinstance(extra, dict):
                parser.error("--extra-json must be a JSON object")
        except json.JSONDecodeError as exc:
            parser.error(f"--extra-json invalid JSON: {exc}")
    plan = page_plan(args.pdf, args.output_dir, args.start, args.end)
    if args.dry_run:
        import fitz

        with fitz.open(str(args.pdf)) as document:
            total_pages = document.page_count
        _print_dry_run(plan, total_pages, args.dpi, args.concurrency, args.retries)
        _book = book_output_dir(args.pdf, args.output_dir)
        print(f"book_dir={_book} merged_md={_book / (_book_stem(args.pdf) + '-ocr.md')}")
        print(f"endpoint={args.endpoint} detail={args.detail} base_url={args.base_url or 'env/default'} model={args.model or 'env/default'}")
        if extra:
            print(f"extra={json.dumps(extra, ensure_ascii=True)[:400]}")
        return 0
    summary = run_batch(
        args.pdf,
        args.output_dir,
        args.dpi,
        args.concurrency,
        args.retries,
        args.prompt,
        args.start,
        args.end,
        args.input_price_per_million,
        args.output_price_per_million,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        endpoint=args.endpoint,
        detail=args.detail,
        system=args.system,
        extra=extra,
    )
    print(
        "batch_complete=true total_tokens=%d failed=%d"
        % (summary["total_tokens"], summary["failed_pages_this_run"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
