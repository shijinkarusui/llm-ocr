"""llm-ocr engine bridge (S2): stdlib-only HTTP on 127.0.0.1:21139 for the Tauri UI.

Run: python serve.py [--port 21139]
Key hygiene: the key only arrives in request bodies/headers, lives in memory,
and is never written to disk; logs print only sk-**** + key_len.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# Frozen (PyInstaller onefile) resources live under sys._MEIPASS;
# dev runs use the repo tree. _res_file covers both layouts.
def _base_dir():
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return ROOT


BASE = _base_dir()


def _res_file(*parts):
    cand = BASE.joinpath(*parts)
    if cand.is_file():
        return cand
    return ROOT.joinpath(*parts)


def _ensure_prompt_module():
    import ocr_page
    ocr_page.PROMPT_PATH = _res_file("prompts", "ocr_system.md")
    return ocr_page

SERVE_VERSION = "0.2.0-s2"
DEFAULT_PORT = 21139
LOCK_NAME = "serve.lock"

JOBS: dict = {}
JOBS_LOCK = threading.Lock()


def masked_key(key):
    if not key:
        return "(empty)"
    return "sk-**** len=%d" % len(key)


def log(msg):
    line = "[serve %s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        # The Tauri shell exited and closed its end of our stdout pipe (orphaned
        # sidecar), or sys.stdout was closed under us.  Logging must never be
        # fatal: BaseHTTPRequestHandler.send_response() calls log_message()
        # *before* it writes the status line, so an exception raised here aborts
        # the response and every request comes back as 0 bytes.  Drop the line.
        pass


def _force_utf8_stdio():
    """The Tauri shell decodes this process's output as UTF-8; hold up our end.

    When stdout is a pipe or file rather than a console, Windows Python encodes
    with the ANSI code page (cp936 here), so a Chinese log line would reach the
    user's log console as replacement characters.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _body(handler):
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"_raw_error": "invalid JSON body"}
    return data if isinstance(data, dict) else {"_raw_error": "top level must be object"}


def _llm_kwargs(data):
    extra = data.get("extra") or {}
    if not isinstance(extra, dict):
        raise ValueError("extra must be an object")
    timeout = data.get("timeout")
    if timeout is not None:
        extra = dict(extra)
        extra.setdefault("timeout", timeout)
    return {
        "base_url": data.get("base_url"),
        "model": data.get("model"),
        "api_key": data.get("key") or "",
        "detail": data.get("detail") or "high",
    }, extra


def _request_prompt(data):
    """Optional caller-supplied OCR prompt; None means use prompts/ocr_system.md."""
    prompt = data.get("prompt")
    if prompt is None:
        return None
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    return prompt if prompt.strip() else None


def _code_for_status(st):
    """Preserve upstream HTTP status when it is a real 4xx/5xx, else 502."""
    try:
        s = int(st)
    except (TypeError, ValueError):
        return 502
    if 400 <= s <= 599:
        return s
    return 502


def _progress_from_usage(outdir, planned=None, started_at=None, concurrency=None):
    """P0: progress schema. Dedup by latest row per page (pno_0based else page_number-1);
    skipped excludes transient==True; p50/p95 reuse batch_plan._pctl over success
    duration_ms; pct/planned/eta/pages_per_min/elapsed/n429 added. Concurrency is a
    live hint: caller passes on_progress snapshots (current/init) when available."""
    try:
        from batch_plan import _pctl as _bp_pctl
    except ImportError:
        from src.batch_plan import _pctl as _bp_pctl  # type: ignore
    usage = Path(outdir) / "usage.jsonl" if str(outdir or "") else Path("") / "usage.jsonl"
    rows = []
    if str(outdir or "") and usage.is_file():
        for line in usage.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    latest = {}
    for idx, r in enumerate(rows):
        if r.get("pno_0based") is not None:
            key = r.get("pno_0based")
        elif r.get("page_number") is not None:
            try:
                key = int(r.get("page_number")) - 1
            except (TypeError, ValueError):
                key = r.get("page_number")
        else:
            key = idx
        latest[key] = r
    pages = list(latest.values())
    ok = [r for r in pages if r.get("status") == "success"]
    skipped = sum(1 for r in pages if r.get("status") == "skipped" and not r.get("transient"))
    failed = sum(1 for r in pages if r.get("status") == "failed")
    toks = sum(int(r.get("total_tokens") or 0) for r in ok)
    durs = sorted(int(r.get("duration_ms") or 0) for r in ok if r.get("duration_ms"))
    n429 = 0
    for r in rows:
        try:
            if int(r.get("rate_429") or 0):
                n429 += int(r.get("rate_429") or 0)
        except (TypeError, ValueError):
            pass
        try:
            if r.get("status") == "failed" and int(r.get("http_status") or 0) == 429:
                n429 += 1
        except (TypeError, ValueError):
            pass
    out = {"records": len(rows), "pages_ok": len(ok), "tokens": toks,
           "skipped": skipped, "failed": failed,
           "p50_ms": _bp_pctl(durs, 50), "p95_ms": _bp_pctl(durs, 95),
           "n429": int(n429)}
    if planned is not None:
        try:
            planned_n = int(planned)
        except (TypeError, ValueError):
            planned_n = 0
        out["planned"] = planned_n
        out["pct"] = round(len(ok) / planned_n * 100, 1) if planned_n > 0 else 0
    else:
        out["planned"] = 0
        out["pct"] = 0
    if started_at is not None:
        try:
            elapsed_ms = int((time.time() - float(started_at)) * 1000)
        except (TypeError, ValueError):
            elapsed_ms = 0
        out["elapsed_ms"] = max(0, elapsed_ms)
        elapsed_min = max(0, elapsed_ms) / 60000.0
        ppm = (len(ok) / elapsed_min) if elapsed_min > 0 and len(ok) > 0 else 0.0
        out["pages_per_min"] = round(ppm, 2)
        remaining = max(0, (out.get("planned") or 0) - len(ok))
        out["eta_ms"] = int(remaining / ppm * 60000) if ppm > 0 and remaining > 0 else 0
    else:
        out["elapsed_ms"] = 0
        out["pages_per_min"] = 0.0
        out["eta_ms"] = 0
    if isinstance(concurrency, dict):
        try:
            out["concurrency_current"] = int(concurrency.get("current", 0))
            out["concurrency_init"] = int(concurrency.get("init", 0))
        except (TypeError, ValueError):
            pass
    return out

def _usage_latest_rows(outdir):
    """Latest usage.jsonl row per page (same dedup rule as _progress_from_usage)."""
    usage = Path(outdir) / "usage.jsonl" if str(outdir or "") else Path("") / "usage.jsonl"
    rows = []
    if str(outdir or "") and usage.is_file():
        for line in usage.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    latest = {}
    for idx, r in enumerate(rows):
        if r.get("pno_0based") is not None:
            latest[r.get("pno_0based")] = r
        elif r.get("page_number") is not None:
            try:
                latest[int(r.get("page_number")) - 1] = r
            except (TypeError, ValueError):
                latest[r.get("page_number")] = r
        else:
            latest[idx] = r
    return list(latest.values())

_CONCURRENCY_HINT: dict = {}

def _on_batch_progress(jid, snap):
    """run_batch on_progress callback: remember the live limiter snapshot for polling."""
    if isinstance(snap, dict):
        try:
            _CONCURRENCY_HINT[jid] = {"current": int(snap.get("current", 0)), "init": int(snap.get("init", 0))}
        except (TypeError, ValueError):
            pass

def _job_failed_pages(job, status="failed"):
    """Pages for GET /api/jobs/<id>/pages?status=failed (default: latest failed rows)."""
    book_dir = (job or {}).get("book_dir") or (job or {}).get("outdir") or ""
    pages = _usage_latest_rows(book_dir)
    want = (status or "failed").strip().lower()
    if want in ("all", "*"):
        rows = pages
    else:
        rows = [r for r in pages if str(r.get("status") or "").lower() == want]
    out = []
    for r in rows:
        try:
            pno = int(r.get("pno_0based")) if r.get("pno_0based") is not None else int(r.get("page_number")) - 1
        except (TypeError, ValueError):
            continue
        try:
            page_number = int(r.get("page_number")) if r.get("page_number") is not None else pno + 1
        except (TypeError, ValueError):
            page_number = pno + 1
        out.append({"pno_0based": pno, "page_number": page_number,
                    "status": str(r.get("status") or ""),
                    "error": str(r.get("error") or "")[:300],
                    "http_status": r.get("http_status"),
                    "attempt": r.get("attempt"),
                    "duration_ms": r.get("duration_ms")})
    out.sort(key=lambda x: x["pno_0based"])
    return out


def _new_job(kind, label):
    jid = uuid.uuid4().hex[:12]
    now = time.time()
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "label": label, "status": "queued",
                     "created": now, "created_at": now, "started_at": now, "cancel": False}
    return jid


def _job_set(jid, **kw):
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(kw)


def _job_get(jid):
    with JOBS_LOCK:
        return dict(JOBS.get(jid) or {})


def _job_cancelled(jid):
    with JOBS_LOCK:
        return bool((JOBS.get(jid) or {}).get("cancel"))


def _find_job_by_outdir(outdir):
    """Most recent job whose book_dir/outdir equals `outdir` (for outdir-only retry)."""
    want = str(outdir or "").strip()
    if not want:
        return None
    best = None
    best_ts = -1.0
    with JOBS_LOCK:
        items = list(JOBS.values())
    for j in items:
        hit = any(str((j or {}).get(k) or "").strip() == want for k in ("book_dir", "outdir"))
        if not hit:
            continue
        try:
            ts = float((j or {}).get("created_at") or (j or {}).get("created") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= best_ts:
            best_ts = ts
            best = dict(j)
    return best


def _clamp_pno_1based(requested, page_count):
    """1-based clamp for single-page APIs: returns (pno_0based, page_1based, clamped)."""
    try:
        req = int(requested)
    except (TypeError, ValueError):
        raise ValueError("pno must be an integer")
    total = int(page_count)
    if total <= 0:
        raise ValueError("empty PDF")
    page_1 = min(max(req, 1), total)
    return page_1 - 1, page_1, (page_1 != req)


def _coerce_dpi(value, default=150):
    """DPI for preview/searchable APIs: int in 72..300, else ValueError."""
    try:
        dpi = int(value) if value is not None and str(value).strip() != "" else int(default)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("dpi must be an integer in 72..300")
    if dpi < 72 or dpi > 300:
        raise ValueError("dpi out of range 72..300")
    return dpi


def _render_page_png(pdf_path, pno_0based, dpi):
    """Render one page to PNG bytes (prefers src/pdf_render.py, falls back to src/render.py)."""
    render_fn = None
    try:
        from pdf_render import render_page as render_fn  # type: ignore
    except ImportError:
        render_fn = None
    if render_fn is None:
        try:
            from render import render_page as render_fn  # type: ignore
        except ImportError:
            from src.render import render_page as render_fn  # type: ignore
    return render_fn(str(pdf_path), int(pno_0based), int(dpi))


def _run_batch_job(jid, pdf_path, outdir, kw, cleanup=None):
    from batch_plan import book_output_dir, run_batch
    _job_set(jid, status="running", started_at=time.time())
    try:
        # P0 true cancel: cooperative stop wired into run_batch; in-flight pages
        # finish, queued futures are dropped (cancel_futures=True).
        summary = run_batch(pdf_path, outdir, should_stop=lambda: _job_cancelled(jid),
                            on_progress=lambda snap: _on_batch_progress(jid, snap), **kw)
        # usage.jsonl lives in the per-book folder, not directly under outdir.
        _job = _job_get(jid)
        prog = _progress_from_usage(book_output_dir(pdf_path, outdir), planned=_job.get("planned"),
                                    started_at=_job.get("started_at"),
                                    concurrency=_CONCURRENCY_HINT.get(jid))
        if _job_cancelled(jid):
            _job_set(jid, status="cancelled", summary=summary, progress=prog)
            log("batch job %s cancelled" % jid)
            return
        _job_set(jid, status="done", summary=summary, progress=prog)
        log("batch job %s done tokens=%s failed=%s" % (
            jid, summary.get("total_tokens", 0), summary.get("failed_pages_this_run", 0)))
    except Exception as exc:  # noqa: BLE001 - surfaced via job polling
        _job_set(jid, status="error", error="%s: %s" % (type(exc).__name__, exc))
        log("batch job %s error %s" % (jid, exc))
    finally:
        if cleanup:
            try:
                Path(cleanup).unlink()
            except OSError:
                pass


def _run_searchable_job(jid, pdf_path, pages_dir, out_dir, geo_source, keywords, book_dir=None, dpi=150):
    import fitz
    from make_searchable import make_searchable
    from verify_searchable import verify, verify_alignment
    try:
        dpi = _coerce_dpi(dpi, default=150)
    except ValueError:
        dpi = 150
    def _stage(name, pct):
        try:
            _job_set(jid, status="running", progress={"stage": name, "pct": int(pct)})
        except Exception:
            pass
    _job_set(jid, status="running")
    _stage("boxes", 5)
    try:
        root = Path(out_dir)
        pages = sorted(Path(pages_dir).glob("page_*.md"),
                       key=lambda p: int(p.stem.split("_")[1]))
        if not pages:
            raise ValueError("no pages/page_*.md under %s" % pages_dir)
        md = {int(p.stem.split("_")[1]): p.read_text(encoding="utf-8") for p in pages}
        _stage("boxes", 20)
        with fitz.open(str(pdf_path)) as doc:
            total = doc.page_count
        # P0 structured missing pages: machine-readable list + resume hint.
        missing = sorted(p for p in range(total) if p not in md and str(p) not in md)
        if missing:
            _job_set(jid, status="error", error="missing Markdown for %d page(s); resume batch first" % len(missing),
                     error_code="MISSING_PAGES", hint_cn="缺 %d 页，先回批量把缺的页补跑出来" % len(missing),
                     next_action="resume batch then rebuild",
                     missing_pages=missing[:200], missing_count=len(missing),
                     missing_example=missing[:5])
            log("searchable job %s missing %d pages e.g. %s" % (jid, len(missing), missing[:5]))
            return
        target = root / "book_searchable.pdf"
        align_out = root / "align"
        _stage("render", 40)
        make_searchable(pdf_path, list(range(total)), md, None, target,
                        geo_source=geo_source, align_out=align_out, dpi=dpi)
        _stage("embed", 65)
        res = verify(target, keywords) if keywords else {}
        _stage("verify", 85)
        # P0 alignment verification: run against the exact align artifacts this
        # build just wrote (boxes.json + align_report.json), not keyword search.
        align_report = None
        try:
            align_report = verify_alignment(
                target, md, align_out / "boxes.json", align_out / "align_report.json",
                report_path=align_out / "verify_report.json")
        except Exception as exc:  # noqa: BLE001 - verify must not sink a good build
            align_report = {"verify_error": "%s: %s" % (type(exc).__name__, exc)}
        fonts = {"ipa": bool(Path(r"C:\Windows\Fonts\segoeui.ttf").is_file()),
                 "symbol": bool(Path(r"C:\Windows\Fonts\seguisym.ttf").is_file())}
        font_warnings = []
        if not fonts["ipa"]:
            font_warnings.append("IPA 字体缺失 (segoeui.ttf)：音标字形可能回退显示")
        if not fonts["symbol"]:
            font_warnings.append("符号字体缺失 (seguisym.ttf)：特殊符号可能回退显示")
        out = {"pdf": str(target), "bytes": target.stat().st_size, "pages": total,
               "copyable_chars": (res or {}).get("copyable_chars", "?"),
               "hit_pages": (res or {}).get("hit_pages", []),
               "hit_page_count": len((res or {}).get("hit_pages", [])),
               "align_dir": str(align_out),
               "align_summary": ((align_report or {}).get("summary") if isinstance(align_report, dict) else None),
               "verify_report": str(align_out / "verify_report.json"),
               "fonts": fonts, "font_warnings": font_warnings}
        if isinstance(align_report, dict) and align_report.get("verify_error"):
            out["verify_error"] = align_report["verify_error"]
        if book_dir:
            # Keep the book folder self-describing: record the artifact we just made.
            # For a legacy dir (no book.json yet) this also writes the missing
            # identity fields, so the folder becomes verifiable from now on.
            try:
                from book_id import book_json_path, update_book
                update_book(book_dir, source_pdf=pdf_path, page_count=total,
                            searchable_pdf=target.name)
                out["book_json"] = str(book_json_path(book_dir))
            except Exception as exc:  # noqa: BLE001
                out["book_json_error"] = "%s: %s" % (type(exc).__name__, exc)
        if _job_cancelled(jid):
            _job_set(jid, status="cancelled", result=out,
                     progress={"stage": "done", "pct": 100})
            log("searchable job %s cancelled" % jid)
            return
        _job_set(jid, status="done", result=out,
                 progress={"stage": "done", "pct": 100})
        log("searchable job %s done pages=%d" % (jid, total))
    except Exception as exc:  # noqa: BLE001
        _job_set(jid, status="error", error="%s: %s" % (type(exc).__name__, exc))
        log("searchable job %s error %s" % (jid, exc))

class Handler(BaseHTTPRequestHandler):
    server_version = "llm-ocr-serve/" + SERVE_VERSION

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def log_error(self, fmt, *args):
        # http.server writes errors straight to sys.stderr from send_error() --
        # again *before* the status line.  Guard it like log() so a dead stderr
        # cannot turn "400 Bad request" into an empty reply.
        try:
            BaseHTTPRequestHandler.log_error(self, fmt, *args)
        except (OSError, ValueError):
            pass

    def _cors(self):
        origin = self.headers.get("Origin") or ""
        allow = ""
        if origin.startswith("tauri://") or origin.startswith("http://localhost:") \
                or origin.startswith("http://127.0.0.1:") or origin.startswith("http://tauri."):
            allow = origin
        self.send_header("Access-Control-Allow-Origin", allow or "tauri://localhost")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-LLM-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, obj):
        body = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, http_code, error_code, error, hint_cn="", next_action="", http_status=None, elapsed_ms=None, extra=None):
        """P0 unified error envelope: keep legacy `error`, add error_code/hint_cn/next_action."""
        obj = {"error": error, "error_code": error_code, "hint_cn": hint_cn, "next_action": next_action}
        if http_status is not None:
            obj["http_status"] = http_status
        if elapsed_ms is not None:
            obj["elapsed_ms"] = elapsed_ms
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in obj:
                    obj[k] = v
        self._send(http_code, obj)
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)
        try:
            if path == "/api/health":
                self._send(200, {"ok": True, "version": SERVE_VERSION, "engine": "src"})
            elif path == "/api/models":
                from llm_client import list_models
                base_url = (qs.get("base_url") or [""])[0]
                key = self.headers.get("X-LLM-Key") or ""
                log("models base=%s key=%s" % (base_url, masked_key(key)))
                try:
                    models = list_models(base_url=base_url or None, api_key=key or None)
                except Exception as exc:  # noqa: BLE001 - GET envelope must carry gateway code
                    _mst = getattr(exc, "status", None)
                    _mec = getattr(exc, "error_code", None) or "UNKNOWN"
                    _mhe = getattr(exc, "hint_cn", "") or "拉取模型列表失败"
                    _mne = getattr(exc, "next_action", "") or "check base_url and key"
                    self._send_error(_code_for_status(_mst) if _mst is not None else 502, _mec,
                                     "%s: %s" % (type(exc).__name__, exc), _mhe, _mne, http_status=_mst)
                    return
                ids = [m.get("id") if isinstance(m, dict) else str(m) for m in models]
                self._send(200, {"models": ids, "count": len(ids)})
            elif path == "/api/prompt":
                from pathlib import Path as _P
                prompt = _res_file("prompts", "ocr_system.md").read_text(encoding="utf-8")
                self._send(200, {"prompt": prompt})
                return
            elif path == "/api/config/check":
                base_url = (qs.get("base_url") or [""])[0]
                model = (qs.get("model") or [""])[0]
                endpoint = (qs.get("endpoint") or ["responses"])[0]
                detail = (qs.get("detail") or ["high"])[0]
                key = self.headers.get("X-LLM-Key") or ""
                self._send(200, {"base_url": base_url, "model": model,
                                 "key": masked_key(key), "endpoint": endpoint,
                                 "detail": detail})
            elif path == "/api/jobs":
                try:
                    limit = int((qs.get("limit") or ["50"])[0])
                except (TypeError, ValueError):
                    limit = 50
                limit = max(1, min(200, limit))
                with JOBS_LOCK:
                    items = [dict(j) for j in JOBS.values()]
                items.sort(key=lambda j: float(j.get("created_at") or j.get("created") or 0), reverse=True)
                slim = [{"id": j.get("id"), "kind": j.get("kind"), "label": j.get("label"),
                         "status": j.get("status"), "created_at": j.get("created_at") or j.get("created")} for j in items[:limit]]
                for _full, _s in zip(items[:limit], slim):
                    for _k in ("planned", "total_pages", "book_dir", "outdir", "progress"):
                        if _k in _full:
                            _s[_k] = _full[_k]
                self._send(200, {"jobs": slim, "total": len(items)})
            elif path == "/api/pdf/preview":
                pdf = ((qs.get("path") or qs.get("pdf_path") or [""])[0] or "").strip()
                if not pdf or not Path(pdf).is_file():
                    self._send_error(404, "NOT_FOUND", "pdf not found", "PDF路径不存在（服务端本地路径）", "check path (server-side path)")
                    return
                try:
                    dpi = _coerce_dpi((qs.get("dpi") or ["150"])[0], default=150)
                except ValueError as exc:
                    self._send_error(400, "BAD_DPI", str(exc), "DPI 只允许 72-300", "send dpi in 72..300")
                    return
                import fitz as _pv_fitz
                try:
                    with _pv_fitz.open(str(pdf)) as _doc:
                        _total = _doc.page_count
                except Exception as exc:
                    self._send_error(400, "BAD_EXTRA_JSON", "%s: %s" % (type(exc).__name__, exc), "PDF打不开", "check pdf file")
                    return
                if _total <= 0:
                    self._send_error(404, "EMPTY_PDF", "empty PDF (0 pages)", "PDF 页数为 0", "check pdf file")
                    return
                try:
                    pno_0, _page1, _cl = _clamp_pno_1based((qs.get("pno") or ["1"])[0], _total)
                except ValueError as exc:
                    self._send_error(400, "BAD_EXTRA_JSON", str(exc), "页码必须是整数", "send integer pno (1-based)")
                    return
                if _cl:
                    self._send_error(404, "PAGE_OUT_OF_RANGE", "pno out of range 1..%d" % _total, "页码越界（共 %d 页）" % _total, "use pno in 1..%d" % _total)
                    return
                try:
                    png = _render_page_png(pdf, pno_0, dpi)
                except Exception as exc:
                    self._send_error(500, "UNKNOWN", "%s: %s" % (type(exc).__name__, exc), "渲染失败", "retry later")
                    return
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
            elif path.startswith("/api/jobs/") and not path.endswith("/cancel"):
                jid = path[len("/api/jobs/"):]
                if "/" in jid:
                    # e.g. /api/jobs/<id>/pages?status=failed&limit=200&offset=0
                    head, _, tail = jid.partition("/")
                    tail_path = tail.split("?", 1)[0]
                    if tail_path == "pages":
                        job = _job_get(head)
                        if not job:
                            self._send_error(404, "UNKNOWN", "unknown job", "未找到该任务", "check job_id")
                            return
                        want = (qs.get("status") or ["failed"])[0]
                        try:
                            limit = max(1, min(2000, int((qs.get("limit") or ["200"])[0])))
                        except (TypeError, ValueError):
                            limit = 200
                        try:
                            offset = max(0, int((qs.get("offset") or ["0"])[0]))
                        except (TypeError, ValueError):
                            offset = 0
                        pages = _job_failed_pages(job, status=want)
                        self._send(200, {"pages": pages[offset:offset + limit], "total": len(pages)})
                        return
                    self._send_error(404, "UNKNOWN", "not found", "未知子路径", "check URL: /api/jobs/<id>/pages")
                    return
                job = _job_get(jid)
                if not job:
                    self._send_error(404, "UNKNOWN", "unknown job", "未找到该任务", "check job_id")
                    return
                out = {k: v for k, v in job.items() if k != "cancel"}
                if job.get("kind") == "batch" and job.get("status") == "running":
                    try:
                        out["progress"] = _progress_from_usage(
                            job.get("book_dir") or job.get("outdir") or "",
                            planned=job.get("planned"), started_at=job.get("started_at"),
                            concurrency=_CONCURRENCY_HINT.get(jid))
                    except Exception:
                        pass
                self._send(200, out)
            else:
                self._send_error(404, "UNKNOWN", "not found: %s" % path, "未知接口", "check URL")
        except Exception as exc:  # noqa: BLE001
            log("GET %s failed %s" % (path, exc))
            _gec = getattr(exc, "error_code", None) or "UNKNOWN"
            _geh = getattr(exc, "hint_cn", "") or "服务端内部错误"
            _gen = getattr(exc, "next_action", "") or "retry later"
            _gst = getattr(exc, "status", None)
            _gcode = _code_for_status(_gst) if _gst is not None else 500
            self._send_error(_gcode, _gec, "%s: %s" % (type(exc).__name__, exc), _geh, _gen, http_status=_gst)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        try:
            data = _body(self)
            if "_raw_error" in data:
                self._send_error(400, "BAD_EXTRA_JSON", data["_raw_error"], "请求体不是合法JSON对象", "send a JSON object body")
                return
            if path == "/api/probe":
                self._handle_probe(data)
            elif path == "/api/ocr/image":
                self._handle_ocr_image(data)
            elif path == "/api/ocr/url":
                self._handle_ocr_url(data)
            elif path == "/api/ocr/pdf-page":
                self._handle_ocr_pdf_page(data)
            elif path == "/api/batch/dry-run":
                self._handle_dry_run(data)
            elif path == "/api/batch/run":
                self._handle_batch_run(data)
            elif path == "/api/batch/retry":
                self._handle_batch_retry(data)
            elif path == "/api/searchable/build":
                self._handle_searchable(data)
            elif path == "/api/book/resolve":
                self._handle_book_resolve(data)
            elif path == "/api/notation/check":
                self._handle_notation(data)
            elif path == "/api/file/b64":
                self._handle_file_b64(data)
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                jid = path[len("/api/jobs/"):-len("/cancel")]
                with JOBS_LOCK:
                    if jid in JOBS:
                        JOBS[jid]["cancel"] = True
                        JOBS[jid]["cancel_requested_at"] = time.time()
                        self._send(200, {"ok": True})
                    else:
                        self._send_error(404, "UNKNOWN", "unknown job", "未找到该任务", "check job_id")
            else:
                self._send_error(404, "UNKNOWN", "not found: %s" % path, "未知接口", "check URL")
        except ValueError as exc:
            log("POST %s bad request %s" % (path, exc))
            _pec = getattr(exc, "error_code", None) or "BAD_EXTRA_JSON"
            _phe = getattr(exc, "hint_cn", "") or "请求参数错误"
            _pne = getattr(exc, "next_action", "") or "fix request params"
            _pst = getattr(exc, "status", None)
            _pcode = _code_for_status(_pst) if _pst is not None else 400
            self._send_error(_pcode, _pec, "%s: %s" % (type(exc).__name__, exc), _phe, _pne, http_status=_pst)
        except Exception as exc:  # noqa: BLE001
            log("POST %s failed %s" % (path, exc))
            try:
                traceback.print_exc()
            except (OSError, ValueError):
                pass  # stderr gone too (orphaned process): still answer the caller
            _eec = getattr(exc, "error_code", None) or "UNKNOWN"
            _ehe = getattr(exc, "hint_cn", "") or "服务端内部错误"
            _ene = getattr(exc, "next_action", "") or "retry later"
            _est = getattr(exc, "status", None)
            _ecode = _code_for_status(_est) if _est is not None else 500
            self._send_error(_ecode, _eec, "%s: %s" % (type(exc).__name__, exc), _ehe, _ene, http_status=_est)
    def _handle_probe(self, data):
        import time as _time
        try:
            llm, extra = _llm_kwargs(data)
        except ValueError as exc:
            self._send_error(400, "BAD_EXTRA_JSON", str(exc), "extra必须是对象", "send extra as JSON object")
            return
        endpoint = (data.get("endpoint") or "responses").strip().lower()
        log("probe ep=%s model=%s key=%s" % (endpoint, llm["model"], masked_key(llm["api_key"])))
        png = _res_file("tests", "cand_165.png").read_bytes()
        import llm_client as C
        prompt = "Transcribe this page exactly (OCR only):"
        t0 = _time.monotonic()
        try:
            if endpoint == "auto":
                text, usage = C.auto_vision(png, prompt, **llm, **extra)
            elif endpoint == "responses":
                text, usage = C.responses_vision(png, prompt, **llm, **extra)
            elif endpoint == "chat":
                text, usage = C.chat_vision(png, prompt, **llm, **extra)
            elif endpoint in ("messages", "anthropic"):
                import warnings as _warn
                _warn.warn("messages experimental, no guarantee", UserWarning)
                text, usage = C.anthropic_vision(png, prompt, **llm, **extra)
            else:
                self._send_error(400, "UNKNOWN", "unknown endpoint: %s" % endpoint, "不支持的endpoint", "use responses|chat|auto|messages")
                return
        except Exception as exc:
            ms = int((_time.monotonic() - t0) * 1000)
            _ec = getattr(exc, "error_code", None) or "UNKNOWN"
            _hc = getattr(exc, "hint_cn", "") or ""
            _nx = getattr(exc, "next_action", "") or ""
            _st = getattr(exc, "status", None)
            _code = _code_for_status(_st)
            self._send_error(_code, _ec, "%s: %s" % (type(exc).__name__, exc), _hc, _nx, http_status=_st, elapsed_ms=ms)
            return
        ms = int((_time.monotonic() - t0) * 1000)
        ep_used = str((usage or {}).get("_endpoint_normalized", endpoint)) if isinstance(usage, dict) else endpoint
        if not (text or "").strip():
            self._send_error(422, "EMPTY_OUTPUT", "empty OCR output", "模型返回空: 调大max_output_tokens或改reasoning low", "raise max_output_tokens / set reasoning low", elapsed_ms=ms)
            return
        self._send(200, {"endpoint_used": ep_used, "text": text,
                         "usage": usage if isinstance(usage, dict) else {},
                         "elapsed_ms": ms})

    def _handle_ocr_image(self, data):
        import time as _time
        from ocr_page import ocr_image
        _ensure_prompt_module()
        try:
            llm, extra = _llm_kwargs(data)
        except ValueError as exc:
            self._send_error(400, "BAD_EXTRA_JSON", str(exc), "extra必须是对象", "send extra as JSON object")
            return
        try:
            png = base64.b64decode(data.get("png_b64") or "", validate=True)
        except (binascii.Error, ValueError):
            self._send_error(400, "BAD_EXTRA_JSON", "png_b64 is not valid base64", "png_b64不是合法base64", "send valid base64 PNG bytes")
            return
        if not png:
            self._send_error(400, "BAD_EXTRA_JSON", "png_b64 required", "缺少png_b64", "send png_b64")
            return
        t0 = _time.monotonic()
        text = ocr_image(png, base_url=llm["base_url"], model=llm["model"],
                         api_key=llm["api_key"], endpoint=data.get("endpoint") or "responses",
                         detail=llm["detail"], extra=extra, timeout=data.get("timeout"),
                         prompt=_request_prompt(data))
        ms = int((_time.monotonic() - t0) * 1000)
        if not (text or "").strip():
            self._send_error(422, "EMPTY_OUTPUT", "empty OCR output", "模型返回空: 调大max_output_tokens或改reasoning low", "raise max_output_tokens / set reasoning low", elapsed_ms=ms)
            return
        self._send(200, {"markdown": text, "elapsed_ms": ms, "dpi_actual": 0, "page_count": 1})

    def _handle_ocr_url(self, data):
        import time as _time
        from ocr_page import ocr_image_url
        _ensure_prompt_module()
        try:
            llm, extra = _llm_kwargs(data)
        except ValueError as exc:
            self._send_error(400, "BAD_EXTRA_JSON", str(exc), "extra必须是对象", "send extra as JSON object")
            return
        url = (data.get("image_url") or "").strip()
        if not url:
            self._send_error(400, "BAD_EXTRA_JSON", "image_url required", "缺少image_url", "send image_url")
            return
        t0 = _time.monotonic()
        text = ocr_image_url(url, base_url=llm["base_url"], model=llm["model"],
                             api_key=llm["api_key"], endpoint=data.get("endpoint") or "responses",
                             detail=llm["detail"], extra=extra, timeout=data.get("timeout"),
                             prompt=_request_prompt(data))
        ms = int((_time.monotonic() - t0) * 1000)
        if not (text or "").strip():
            self._send_error(422, "EMPTY_OUTPUT", "empty OCR output", "模型返回空: 调大max_output_tokens或改reasoning low", "raise max_output_tokens / set reasoning low", elapsed_ms=ms)
            return
        self._send(200, {"markdown": text, "elapsed_ms": ms, "dpi_actual": 0, "page_count": 1})

    def _handle_ocr_pdf_page(self, data):
        import time as _time
        from ocr_page import ocr_pdf_page
        _ensure_prompt_module()
        from render import render_page
        # P1: accept both 0-based `pno` and 1-based `pno`/`page`/`page_number`;
        # a 1-based-looking key wins only when it is explicitly present.
        pno_raw = data.get("pno", 0)
        one_based = False
        for _k in ("page", "page_number", "page_1based", "pno_1based"):
            if data.get(_k) is not None and str(data.get(_k)).strip() != "":
                pno_raw = data.get(_k)
                one_based = True
                break
        try:
            llm, extra = _llm_kwargs(data)
        except ValueError as exc:
            self._send_error(400, "BAD_EXTRA_JSON", str(exc), "extra必须是对象", "send extra as JSON object")
            return
        pdf = (data.get("pdf_path") or "").strip()
        if not pdf or not Path(pdf).is_file():
            self._send_error(400, "BAD_EXTRA_JSON", "pdf_path must be an existing file", "PDF路径不存在", "check pdf_path (server-side path)")
            return
        try:
            dpi = int(data.get("dpi") or 200)
        except (TypeError, ValueError):
            dpi = 200
        import fitz as _fitz
        try:
            with _fitz.open(str(pdf)) as _doc:
                _page_count = _doc.page_count
        except Exception as exc:
            self._send_error(400, "BAD_EXTRA_JSON", "%s: %s" % (type(exc).__name__, exc), "PDF打不开", "check pdf file")
            return
        if _page_count <= 0:
            self._send_error(422, "EMPTY_PDF", "empty PDF (0 pages)", "PDF 页数为 0", "check pdf file")
            return
        # P1: 1-based clamp to [1, page_count]; legacy 0-based pno keeps working.
        try:
            if one_based:
                pno, page_1, clamped = _clamp_pno_1based(pno_raw, _page_count)
                requested_pno = int(pno_raw)
            else:
                requested_pno = int(pno_raw)
                if requested_pno < 0:
                    pno, page_1, clamped = 0, 1, True
                elif requested_pno >= _page_count:
                    pno, page_1, clamped = _page_count - 1, _page_count, True
                else:
                    pno, page_1, clamped = requested_pno, requested_pno + 1, False
        except (TypeError, ValueError):
            self._send_error(400, "BAD_EXTRA_JSON", "pno must be an integer", "页码必须是整数", "send integer pno")
            return
        t0 = _time.monotonic()
        try:
            preview = base64.b64encode(render_page(pdf, pno, 150)).decode("ascii")
            text = ocr_pdf_page(pdf, pno, base_url=llm["base_url"], model=llm["model"],
                                api_key=llm["api_key"], endpoint=data.get("endpoint") or "responses",
                                detail=llm["detail"], extra=extra, dpi=dpi, timeout=data.get("timeout"),
                                prompt=_request_prompt(data))
        except (IndexError, ValueError) as exc:
            self._send_error(400, "BAD_EXTRA_JSON", "%s: %s" % (type(exc).__name__, exc), "请求参数错误", "fix request params")
            return
        ms = int((_time.monotonic() - t0) * 1000)
        if not (text or "").strip():
            self._send_error(422, "EMPTY_OUTPUT", "empty OCR output", "模型返回空: 调大max_output_tokens或改reasoning low", "raise max_output_tokens / set reasoning low", elapsed_ms=ms)
            return
        self._send(200, {"markdown": text, "png_b64_preview": preview, "elapsed_ms": ms, "dpi_actual": dpi, "page_count": _page_count, "pno": pno, "page_number": page_1, "requested_pno": requested_pno, "clamped": bool(clamped)})

    def _handle_dry_run(self, data):
        import fitz
        from batch_plan import book_output_dir, page_plan
        pdf = (data.get("pdf_path") or "").strip()
        if not pdf or not Path(pdf).is_file():
            self._send_error(400, "BAD_EXTRA_JSON", "pdf_path must be an existing file", "PDF\u8def\u5f84\u4e0d\u5b58\u5728\uff08\u670d\u52a1\u7aef\u672c\u5730\u8def\u5f84\uff09", "check pdf_path (server-side path)")
            return
        try:
            with fitz.open(str(pdf)) as _dry_doc:
                total = _dry_doc.page_count
        except Exception as exc:
            self._send_error(400, "BAD_EXTRA_JSON", "%s: %s" % (type(exc).__name__, exc), "PDF\u6253\u4e0d\u5f00", "check pdf file")
            return
        if total <= 0:
            self._send_error(422, "EMPTY_PDF", "empty PDF (0 pages)", "PDF \u9875\u6570\u4e3a 0", "check pdf file")
            return
        # P1: 1-based clamp - out-of-range pages clamp, never 4xx;
        # only missing pdf / 0 pages go 4xx.
        requested_pno = None
        one_based_key = False
        for _k in ("page", "page_number", "page_1based", "pno_1based"):
            if data.get(_k) is not None and str(data.get(_k)).strip() != "":
                requested_pno = data.get(_k)
                one_based_key = True
                break
        try:
            if one_based_key:
                _pno0, _page1, _cl = _clamp_pno_1based(requested_pno, total)
                requested_start, requested_end = _pno0, _pno0 + 1
                start_c, end_c = _pno0, _pno0 + 1
                clamped = bool(_cl)
                requested_pno = int(requested_pno)
            else:
                _rs = data.get("start", 0)
                if _rs is None or (isinstance(_rs, str) and _rs.strip() == ""):
                    requested_start = 0
                else:
                    requested_start = int(_rs)
                _re = data.get("end")
                if _re is None or (isinstance(_re, str) and str(_re).strip() == ""):
                    requested_end = None
                else:
                    requested_end = int(_re)
                start_c = min(max(requested_start, 0), total)
                if requested_end is None:
                    end_c = None
                else:
                    end_c = min(max(requested_end, 0), total)
                    if end_c < start_c:
                        end_c = start_c
                clamped = (start_c != requested_start) or (requested_end is not None and end_c != requested_end)
        except (TypeError, ValueError):
            self._send_error(400, "BAD_EXTRA_JSON", "start/end/pno must be integers", "\u8d77\u6b62\u9875\u5fc5\u987b\u662f\u6574\u6570", "send integer start/end (0-based) or page (1-based)")
            return
        outdir = (data.get("outdir") or "out/book_gui").strip() or "out/book_gui"
        plan = page_plan(Path(pdf), Path(outdir), start_c, end_c)
        first5 = [{"pno_0based": i["pno_0based"], "page_number": i["page_number"]} for i in plan[:5]]
        # P0 resume visibility: done pages already on disk under the per-book root.
        book_dir = book_output_dir(Path(pdf), Path(outdir))
        done_rows = [r for r in _usage_latest_rows(str(book_dir)) if r.get("status") == "success"]
        done_pages = len(done_rows)
        book_json = str(book_dir / "book.json") if (book_dir / "book.json").is_file() else None
        # Per-book root the run will actually write to (pages/, usage.jsonl, book md).
        resp = {"total_pages": total, "planned_pages": len(plan), "first5": first5,
                "book_dir": str(book_dir), "done_pages": done_pages,
                "remaining": max(0, len(plan) - done_pages), "book_json": book_json,
                "start": start_c, "end": end_c,
                "requested_start": requested_start, "requested_end": requested_end,
                "clamped": bool(clamped)}
        if one_based_key:
            resp["requested_pno"] = requested_pno
            resp["page_number"] = _page1
        self._send(200, resp)

    def _handle_batch_run(self, data):
        import fitz as _batch_fitz
        from batch_plan import book_output_dir, page_plan
        pdf = (data.get("pdf_path") or "").strip()
        if not pdf or not Path(pdf).is_file():
            self._send_error(400, "BAD_EXTRA_JSON", "pdf_path must be an existing file", "PDF路径不存在（服务端本地路径）", "check pdf_path (server-side path)")
            return
        llm, extra = _llm_kwargs(data)
        outdir = (data.get("outdir") or "out/book_gui").strip() or "out/book_gui"
        start = int(data.get("start") or 0)
        end = data.get("end")
        end = int(end) if end is not None and str(end).strip() != "" else None
        # run_batch only accepts a path, so a front-end prompt is staged in a
        # temp file that the job thread removes when it finishes.
        prompt_file = str(_res_file("prompts", "ocr_system.md"))
        cleanup = None
        custom_prompt = _request_prompt(data)
        if custom_prompt is not None:
            import os
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix="llmocr_prompt_", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(custom_prompt)
            prompt_file = tmp
            cleanup = tmp
        kw = dict(dpi=int(data.get("dpi") or 200), concurrency=int(data.get("concurrency") or 4),
                  retries=int(data.get("retries") if data.get("retries") is not None else 2),
                  start=start, end=end, base_url=llm["base_url"], model=llm["model"],
                  prompt_path=prompt_file,
                  api_key=llm["api_key"], endpoint=data.get("endpoint") or "responses",
                  detail=llm["detail"], extra=extra, timeout=data.get("timeout"))
        jid = _new_job("batch", "batch %s" % Path(pdf).name)
        _job_set(jid, outdir=outdir, book_dir=str(book_output_dir(Path(pdf), Path(outdir))),
                 planned=len(page_plan(Path(pdf), Path(outdir), start, end)))
        try:
            with _batch_fitz.open(str(pdf)) as _doc:
                _job_set(jid, total_pages=_doc.page_count)
        except Exception:
            pass
        threading.Thread(target=_run_batch_job,
                         args=(jid, pdf, outdir, kw, cleanup), daemon=True).start()
        log("batch job %s started pdf=%s key=%s" % (jid, pdf, masked_key(llm["api_key"])))
        self._send(200, {"job_id": jid})

    def _handle_batch_retry(self, data):
        from batch_plan import book_output_dir, page_plan
        jid_src = (data.get("job_id") or "").strip() if isinstance(data.get("job_id"), str) else data.get("job_id")
        outdir_arg = (data.get("outdir") or "").strip() if isinstance(data.get("outdir"), str) else ""
        only_failed = data.get("only_failed", True)
        if isinstance(only_failed, str):
            only_failed = only_failed.strip().lower() not in ("0", "false", "no", "")
        else:
            only_failed = bool(only_failed)
        job = _job_get(jid_src) if jid_src else None
        if job is None and outdir_arg:
            job = _find_job_by_outdir(outdir_arg)
            if job is not None:
                jid_src = job.get("id")
        if job is None:
            self._send_error(400, "MISSING_ARG", "job_id or outdir is required", "缺少 job_id 或 outdir 参数", "send job_id (or outdir)")
            return
        pdf = str((job.get("source_pdf") or job.get("pdf_path") or "")).strip()
        outdir = str((job.get("outdir") or outdir_arg or "")).strip()
        if not pdf or not Path(pdf).is_file():
            book_dir = str((job.get("book_dir") or outdir or "")).strip()
            bj = Path(book_dir) / "book.json" if book_dir else None
            if bj is not None and bj.is_file():
                try:
                    rec = json.loads(bj.read_text(encoding="utf-8"))
                    cand = str((rec or {}).get("source_pdf") or "").strip()
                    if cand and Path(cand).is_file():
                        pdf = cand
                except (ValueError, OSError):
                    pass
        if not pdf or not Path(pdf).is_file():
            self._send_error(400, "MISSING_ARG", "source PDF unknown for this job", "该任务找不到源 PDF（book.json 无记录）", "send pdf_path with outdir, or rerun batch")
            return
        if not outdir:
            self._send_error(400, "MISSING_ARG", "outdir is required", "缺少 outdir 参数", "send outdir")
            return
        if not only_failed:
            self._send_error(400, "MISSING_ARG", "only_failed must be true", "当前仅支持 only_failed:true 的失败页重试", "send only_failed:true")
            return
        failed_rows = _job_failed_pages(job, status="failed")
        retried_1based = sorted({int(r.get("page_number") or 0) for r in failed_rows if int(r.get("page_number") or 0) > 0})
        if not retried_1based:
            self._send(200, {"job_id": jid_src, "retried": [], "job": _job_get(jid_src)})
            return
        llm, extra = _llm_kwargs(data)
        prompt_file = str(_res_file("prompts", "ocr_system.md"))
        cleanup = None
        custom_prompt = _request_prompt(data)
        if custom_prompt is not None:
            import os
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix="llmocr_prompt_", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(custom_prompt)
            prompt_file = tmp
            cleanup = tmp
        try:
            dpi = int(data.get("dpi") or job.get("dpi") or 200)
        except (TypeError, ValueError):
            dpi = 200
        try:
            concurrency = int(data.get("concurrency") or job.get("concurrency") or 4)
        except (TypeError, ValueError):
            concurrency = 4
        try:
            retries = int(data.get("retries") if data.get("retries") is not None else job.get("retries") if job.get("retries") is not None else 2)
        except (TypeError, ValueError):
            retries = 2
        kw = dict(dpi=dpi, concurrency=concurrency, retries=retries,
                  start=min(r - 1 for r in retried_1based), end=max(retried_1based),
                  base_url=llm["base_url"] or job.get("base_url"), model=llm["model"] or job.get("model"),
                  prompt_path=prompt_file,
                  api_key=llm["api_key"] or job.get("api_key") or "",
                  endpoint=data.get("endpoint") or job.get("endpoint") or "responses",
                  detail=llm["detail"] or job.get("detail") or "high",
                  extra=extra or job.get("extra"), timeout=data.get("timeout") if data.get("timeout") is not None else job.get("timeout"))
        # Delete the failed page files so run_batch re-OCRs them instead of
        # emitting `skipped` sentinels (existing non-empty outputs are skipped).
        book_dir = str(book_output_dir(Path(pdf), Path(outdir)))
        for _p1 in retried_1based:
            try:
                (Path(book_dir) / "pages" / ("page_%04d.md" % (_p1 - 1))).unlink(missing_ok=True)
            except OSError:
                pass
        jid = _new_job("batch", "retry %s (%d pages)" % (Path(pdf).name, len(retried_1based)))
        _job_set(jid, outdir=outdir, book_dir=book_dir, source_pdf=pdf,
                 planned=len(retried_1based), retried_from=jid_src, retried_pages=retried_1based,
                 dpi=dpi, concurrency=concurrency, retries=retries,
                 base_url=kw.get("base_url"), model=kw.get("model"),
                 endpoint=kw.get("endpoint"), detail=kw.get("detail"),
                 extra=kw.get("extra"), timeout=kw.get("timeout"))
        try:
            import fitz as _retry_fitz
            with _retry_fitz.open(str(pdf)) as _doc:
                _job_set(jid, total_pages=_doc.page_count)
        except Exception:
            pass
        threading.Thread(target=_run_batch_job,
                         args=(jid, pdf, outdir, kw, cleanup), daemon=True).start()
        log("batch retry job %s from %s pages=%s" % (jid, jid_src, retried_1based))
        self._send(200, {"job_id": jid, "retried": retried_1based, "job": _job_get(jid)})

    def _handle_searchable(self, data):
        from batch_plan import book_output_dir
        from book_id import resolve_book
        pdf = (data.get("pdf_path") or "").strip()
        outdir = (data.get("outdir") or "").strip()
        try:
            dpi = _coerce_dpi(data.get("dpi", 150), default=150)
        except ValueError as exc:
            self._send_error(400, "BAD_DPI", str(exc), "DPI 只允许 72-300", "send dpi in 72..300")
            return
        if not outdir or not Path(outdir).is_dir():
            self._send_error(400, "BAD_EXTRA_JSON", "outdir must contain pages/page_*.md", "输出目录不存在或没有 pages（服务端本地路径）", "check outdir (server-side path)")
            return
        if pdf and not Path(pdf).is_file():
            self._send_error(400, "BAD_EXTRA_JSON", "pdf_path must be an existing file", "PDF路径不存在（服务端本地路径）", "check pdf_path (server-side path)")
            return
        # Bind the folder to a book before building anything: guessing here would
        # silently lay one book's OCR onto another book's raster.
        resolved = resolve_book(
            outdir, pdf or None,
            book_dir_hint=book_output_dir(pdf, outdir) if pdf else None,
        )
        if resolved["status"] in ("error", "not_found", "ambiguous", "mismatch"):
            self._send_error(400, "BAD_EXTRA_JSON", resolved["message"], resolved["message"], "pick the book_dir from candidates or bind with pdf_path",
                             extra={"status": resolved["status"], "candidates": resolved.get("candidates") or []})
            return
        pdf_use = resolved.get("source_pdf") or pdf
        if not pdf_use or not Path(pdf_use).is_file():
            self._send_error(400, "BAD_EXTRA_JSON", "source PDF unknown: pick the original PDF", "无法确定源 PDF：该目录没有 book.json 记录源文件，请手动选择原 PDF", "pick pdf_path manually")
            return
        pages_dir = resolved.get("pages_dir") or str(Path(outdir) / "pages")
        if not Path(pages_dir).is_dir():
            self._send_error(400, "MISSING_PAGES", "no pages/*.md under %s" % pages_dir, "目录里没有 pages/*.md：%s" % pages_dir, "run batch first, then rebuild")
            return
        geo = (data.get("geo_source") or "auto").strip()
        if geo == "external":
            self._send_error(400, "BAD_EXTRA_JSON", "geo_source=external needs external boxes (API takes no boxes param)", "geo_source=external 需要外部 boxes（当前 API 未接 boxes 参数），请用 auto/embedded/fallback_only", "use auto/embedded/fallback_only")
            return
        kws = data.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.replace("\uff0c", ",").split(",") if k.strip()]
        job_book_dir = resolved.get("book_dir") or outdir
        jid = _new_job("searchable", "searchable %s" % Path(pdf_use).name)
        _job_set(jid, outdir=outdir, book_dir=resolved.get("book_dir"),
                 source_pdf=pdf_use, bind_status=resolved["status"],
                 warnings=resolved.get("warnings") or [], dpi=dpi, geo_source=geo)
        threading.Thread(target=_run_searchable_job,
                         args=(jid, pdf_use, pages_dir, job_book_dir, geo, kws,
                               resolved.get("book_dir"), dpi), daemon=True).start()
        log("searchable job %s started pdf=%s bind=%s" % (jid, pdf_use, resolved["status"]))
        self._send(200, {"job_id": jid, "status": resolved["status"],
                         "book_dir": resolved.get("book_dir"), "source_pdf": pdf_use,
                         "warnings": resolved.get("warnings") or []})

    def _handle_book_resolve(self, data):
        """Bind a directory (book folder / legacy OCR dir / parent dir) to one book."""
        from batch_plan import book_output_dir
        from book_id import resolve_book
        outdir = (data.get("dir") or data.get("outdir") or "").strip()
        pdf = (data.get("pdf_path") or "").strip()
        if not outdir:
            self._send_error(400, "BAD_EXTRA_JSON", "dir is required", "缺少目录参数 dir（服务端本地路径）", "send dir")
            return
        resolved = resolve_book(
            outdir, pdf or None,
            book_dir_hint=book_output_dir(pdf, outdir) if pdf else None,
        )
        self._send(200, resolved)

    def _handle_file_b64(self, data):
        import base64 as _b64mod
        p = (data.get("path") or "").strip()
        if not p or not Path(p).is_file():
            self._send_error(400, "BAD_EXTRA_JSON", "path must be an existing file", "文件路径不存在（服务端本地路径）", "check path (server-side path)")
            return
        try:
            _size = Path(p).stat().st_size
        except OSError as exc:
            self._send_error(400, "BAD_EXTRA_JSON", "%s: %s" % (type(exc).__name__, exc), "文件读取失败", "check path (server-side path)")
            return
        if _size > 50 * 1024 * 1024:
            self._send_error(400, "BAD_EXTRA_JSON", "file too large (>50MB)", "文件超过 50MB 上限", "pick a smaller file")
            return
        # P2: chunked streaming read (57*1024 is a multiple of 3, so only the
        # final chunk carries base64 padding and plain concatenation stays valid).
        try:
            _parts = []
            with open(p, "rb") as _fh:
                while True:
                    _ch = _fh.read(57 * 1024)
                    if not _ch:
                        break
                    _parts.append(_b64mod.b64encode(_ch).decode("ascii"))
        except OSError as exc:
            self._send_error(500, "UNKNOWN", "%s: %s" % (type(exc).__name__, exc), "文件读取失败", "retry later")
            return
        b64 = "".join(_parts)
        self._send(200, {"b64": b64, "bytes": len(b64)})

    def _handle_notation(self, data):
        from collections import Counter
        from check_notation import check_file
        d = (data.get("dir") or "").strip()
        if not d or not Path(d).is_dir():
            self._send_error(400, "BAD_EXTRA_JSON", "dir must be an existing directory", "目录不存在（服务端本地路径）", "check dir (server-side path)")
            return
        pages = sorted(Path(d).glob("pages/page_*.md")) or sorted(Path(d).glob("page_*.md"))
        codes: Counter = Counter()
        worst = []
        total = 0
        for p in pages:
            iss = check_file(p)
            total += len(iss)
            for i in iss:
                codes[i["code"]] += 1
            if iss:
                worst.append({"page": p.name, "issues": len(iss)})
        worst.sort(key=lambda w: w["issues"], reverse=True)
        self._send(200, {"files": len(pages), "total": total,
                         "codes": dict(codes.most_common(10)), "worst": worst[:10]})

class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Bind 127.0.0.1:<port> so a second process cannot silently share it.

    On Windows SO_REUSEADDR means "let me bind a port somebody else already
    holds", and http.server sets it by default.  A foreign process squatting on
    21139 therefore does not make serve.exe fail: both bind, the kernel hands
    each connection to an arbitrary one, and the UI just times out at random.
    SO_EXCLUSIVEADDRUSE makes that conflict a hard error instead.  POSIX keeps
    SO_REUSEADDR, where it only skips the TIME_WAIT wait.
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        HTTPServer.server_bind(self)


def _pid_alive(pid):
    """Best-effort "is this PID still running?" with the stdlib only.

    The lock can outlive its owner: the Tauri shell kills the sidecar with
    TerminateProcess, so Python's `finally` never runs and the lock file stays
    behind. Deciding whether such a lock is stale needs a liveness probe.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a probe on Windows (it can only terminate), so
        # ask the kernel directly; ctypes is stdlib, no third-party dependency.
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but owned by somebody else
    except OSError:
        return False
    return True


def _proc_start_time(pid):
    """Process creation time as a Unix timestamp, or None when unavailable.

    A bare PID is not an identity: Windows recycles PIDs, so a stale lock can
    name a PID that a completely unrelated process now owns. Comparing creation
    times tells the two apart. Windows only — elsewhere we stay conservative.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        created, exited, kernel, user = (FILETIME(), FILETIME(), FILETIME(), FILETIME())
        if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if ticks == 0:
            return None
        # FILETIME counts 100ns intervals since 1601-01-01.
        return (ticks - 116444736000000000) / 1e7
    finally:
        k32.CloseHandle(handle)


def _lock_holder_alive(pid, recorded_start):
    """Is the process that wrote this lock still running?

    `recorded_start` is the holder's creation time as stored in the lock; when it
    disagrees with the live process, the PID was recycled and the holder is gone.
    """
    if not _pid_alive(pid):
        return False
    if recorded_start is None:
        # Legacy/unknown lock format: cannot prove recycling, so assume it lives.
        return True
    actual = _proc_start_time(pid)
    if actual is None:
        return True  # unable to verify -> stay conservative
    return abs(actual - recorded_start) <= 1.0


def _port_listening(port, timeout=0.35):
    """True when something accepts a TCP connection on 127.0.0.1:port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _read_lock(lock):
    """(pid, start_time_or_None) recorded in an existing lock file."""
    try:
        raw = lock.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    pid = None
    start = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("pid="):
            try:
                pid = int(line[4:].strip())
            except ValueError:
                pid = None
        elif line.startswith("start="):
            try:
                start = float(line[6:].strip())
            except ValueError:
                start = None
    return pid, start


#: An empty (pid-less) lock may only be reclaimed after this many seconds: a
#: live creator writes its identity microseconds after creating the file, so an
#: empty lock older than this is debris from a crash inside that window.
LOCK_EMPTY_GRACE = 15.0

#: Handle of the lock file we own, held open for the process lifetime.  On
#: Windows an open handle that does not grant FILE_SHARE_DELETE makes the kernel
#: refuse another process's unlink/rename/replace of that file
#: (ERROR_SHARING_VIOLATION / ERROR_ACCESS_DENIED -> PermissionError), so a
#: racing starter physically cannot destroy or take over the winner's lock while
#: the winner lives.  Read sharing is still granted, so _read_lock() keeps
#: working for everybody else.
_LOCK_HANDLE = None


def _lock_reclaimable(lock, pid, start):
    """True only when this lock is *provably* the corpse of a dead instance.

    The dangerous direction is the opposite one: reading a lock that another
    process created a moment ago (empty or half written, no pid yet) and wiping
    it out from under its owner.  Without a pid the only available signal is
    age, so a fresh pid-less lock is treated as a live claim.
    """
    if pid:
        return not _lock_holder_alive(pid, start)
    try:
        age = time.time() - os.stat(str(lock)).st_mtime
    except OSError:
        return False
    return age > LOCK_EMPTY_GRACE


def _same_file(path, handle):
    """True when `path` names the very file `handle` is open on.

    Identity, not content: on Windows st_ino is the NTFS file index, so this
    still tells two locks apart when their bytes happen to match.
    """
    try:
        a = os.stat(str(path))
        b = os.fstat(handle.fileno())
    except (OSError, ValueError):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _claim_stale_lock(lock):
    """Take over a stale lock atomically.  Returns our open handle, or None.

    Both obvious ways to recycle a stale lock are racy.  `unlink()` (what this
    used to do) is read-check-act: between reading the lock and deleting it
    another process can create its own, and the delete then destroys the
    *winner's* lock -- measured on the old code, 2 of 15 rounds with 4
    simultaneous starts ended with no lock at all.  Delete-then-recreate has the
    mirror-image hole: for a moment the path does not exist, so every process
    that reads it inside that window also concludes "free".

    So the file is never removed: our lock is built under a unique name and
    moved into place with os.replace(), which swaps the directory entry in one
    step -- no empty window, no restore dance, no content re-read that another
    process can invalidate.  A live holder still has its handle open, and
    replacing a file that is open without FILE_SHARE_DELETE fails with
    ERROR_ACCESS_DENIED (verified on this box), so the kernel itself refuses to
    let a loser overwrite a live winner.
    """
    recheck_pid, recheck_start = _read_lock(lock)
    if recheck_pid and _lock_holder_alive(recheck_pid, recheck_start):
        return None  # the owner came back between our first read and this one
    tmp = "%s.%d-%s.new" % (lock, os.getpid(), uuid.uuid4().hex[:8])
    start = _proc_start_time(os.getpid())
    try:
        with open(tmp, "x") as fh:
            # Identity, not just a number: PID + creation time survives PID reuse.
            fh.write("pid=%d\nstart=%s\ntime=%s\n" % (os.getpid(), start, time.time()))
        # The temp file has to be closed before it can be moved: Windows refuses
        # to rename a file that is still open (verified: WinError 32).
        os.replace(tmp, str(lock))
    except OSError as exc:
        # ERROR_ACCESS_DENIED here means the current holder still has the file
        # open: a live claimant, so leave its lock alone.
        print("serve cannot take over lock %s (%s: %s); leaving it to its owner"
              % (lock, type(exc).__name__, exc), flush=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    # Re-open the file we just installed and keep *that* handle open for the rest
    # of the process lifetime: from here on the kernel rejects any other
    # process's unlink/rename/replace of it.
    try:
        handle = open(str(lock), "rb")
    except OSError as exc:
        print("serve cannot re-open claimed lock %s (%s: %s); backing off"
              % (lock, type(exc).__name__, exc), flush=True)
        return None
    if _read_lock(lock) != (os.getpid(), start) or not _same_file(lock, handle):
        # Another starter slipped in between the replace and this open and
        # installed its own lock; the loser backs off rather than serving twice.
        print("serve lock %s was taken over while claiming it; backing off" % lock, flush=True)
        try:
            handle.close()
        except OSError:
            pass
        return None
    return handle


def _release_lock(lock):
    """Drop our claim at exit.  The handle has to go first: Windows refuses to
    delete a file that is still open."""
    global _LOCK_HANDLE
    handle, _LOCK_HANDLE = _LOCK_HANDLE, None
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass
    if lock is None:
        return
    try:
        Path(lock).unlink()
    except OSError:
        pass


def _acquire_lock(port):
    # sidecar-safe: lock lives in tempdir, not next to the exe
    import tempfile as _tf
    global _LOCK_HANDLE
    lock = Path(_tf.gettempdir()) / ("llm-ocr-serve-%d.lock" % port)
    try:
        fd = open(str(lock), "x")
    except FileExistsError:
        pid, start = _read_lock(lock)
        if pid and _lock_holder_alive(pid, start):
            # A genuine instance owns the lock: refuse to start a second one.
            print("serve already running (lock=%s pid=%s holder_alive=True); exiting"
                  % (lock, pid), flush=True)
            return None
        busy = _port_listening(port)
        if not _lock_reclaimable(lock, pid, start):
            # No pid (or unreadable): the owner cannot be proven gone, so this is
            # treated as a live claim instead of touching somebody's lock.
            print("serve lock %s is not provably stale (pid=%s port_%d_listening=%s); "
                  "exiting" % (lock, pid, port, busy), flush=True)
            return None
        if busy:
            # The lock's owner is gone but the port is squatted by a foreign
            # program.  Do not answer that with the misleading "already running"
            # (exit 2): take the corpse's place and let the bind fail loudly
            # instead (exit 3 + FATAL "cannot bind").
            print("serve stale lock (pid=%s not running) but port %d is already in "
                  "use; reclaiming it and attempting the bind so the real error "
                  "surfaces" % (pid, port), flush=True)
        else:
            # A stale lock left by TerminateProcess, which skips Python's
            # `finally`: recycle it atomically (see _claim_stale_lock), so two
            # processes racing on the same corpse cannot both end up serving.
            print("serve stale lock (pid=%s not running, port %d free); reclaiming %s"
                  % (pid, port, lock), flush=True)
        handle = _claim_stale_lock(lock)
        if handle is None:
            print("serve lost the lock race for %s; exiting" % lock, flush=True)
            return None
        _LOCK_HANDLE = handle   # keep it open: see the comment on _LOCK_HANDLE
        return lock
    else:
        # Identity, not just a number: PID + creation time survives PID reuse.
        fd.write("pid=%d\nstart=%s\ntime=%s\n"
                 % (os.getpid(), _proc_start_time(os.getpid()), time.time()))
        fd.flush()   # others must be able to read it while we keep it open
        _LOCK_HANDLE = fd   # keep it open: see the comment on _LOCK_HANDLE
        return lock


def main(argv=None):
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(description="llm-ocr engine bridge")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    lock = _acquire_lock(args.port)
    if lock is None:
        print("serve already running (lock exists); exiting", flush=True)
        return 2
    try:
        try:
            srv = ExclusiveHTTPServer(("127.0.0.1", args.port), Handler)
        except OSError as exc:
            # Loud, non-zero exit: the Tauri shell captures this and shows it to
            # the user instead of letting the UI time out with no explanation.
            log("FATAL cannot bind 127.0.0.1:%d (%s: %s) — another program is "
                "already using this port" % (args.port, type(exc).__name__, exc))
            return 3
        srv.daemon_threads = True
        log("listening on 127.0.0.1:%d version=%s" % (args.port, SERVE_VERSION))
        srv.serve_forever()
    finally:
        _release_lock(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
