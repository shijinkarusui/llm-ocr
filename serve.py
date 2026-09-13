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
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    print("[serve %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


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


def _progress_from_usage(outdir):
    """Read usage.jsonl: success pages / tokens / durations summary."""
    from collections import Counter
    usage = Path(outdir) / "usage.jsonl"
    rows = []
    if usage.is_file():
        for line in usage.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    c = Counter(r.get("status") for r in rows)
    ok = [r for r in rows if r.get("status") == "success"]
    pages = {r.get("pno_0based") for r in ok}
    toks = sum(int(r.get("total_tokens") or 0) for r in ok)
    durs = sorted(int(r.get("duration_ms") or 0) for r in ok if r.get("duration_ms"))
    p50 = durs[len(durs) // 2] if durs else 0
    p95 = durs[int(len(durs) * 0.95)] if durs else 0
    return {"records": len(rows), "pages_ok": len(pages), "tokens": toks,
            "skipped": c.get("skipped", 0), "failed": c.get("failed", 0),
            "p50_ms": p50, "p95_ms": p95}

def _new_job(kind, label):
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "label": label, "status": "queued",
                     "created": time.time(), "cancel": False}
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


def _run_batch_job(jid, pdf_path, outdir, kw, cleanup=None):
    from batch_plan import run_batch
    _job_set(jid, status="running")
    try:
        summary = run_batch(pdf_path, outdir, **kw)
        prog = _progress_from_usage(outdir)
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


def _run_searchable_job(jid, pdf_path, outdir, geo_source, keywords):
    import fitz
    from make_searchable import make_searchable
    from verify_searchable import verify
    _job_set(jid, status="running")
    try:
        root = Path(outdir)
        pages = sorted(root.glob("pages/page_*.md"),
                       key=lambda p: int(p.stem.split("_")[1]))
        if not pages:
            raise ValueError("no pages/page_*.md under %s" % outdir)
        md = {int(p.stem.split("_")[1]): p.read_text(encoding="utf-8") for p in pages}
        with fitz.open(str(pdf_path)) as doc:
            total = doc.page_count
        target = root / "book_searchable.pdf"
        align_out = root / "align"
        make_searchable(pdf_path, list(range(total)), md, None, target,
                        geo_source=geo_source, align_out=align_out)
        res = verify(target, keywords) if keywords else {}
        out = {"pdf": str(target), "bytes": target.stat().st_size, "pages": total,
               "copyable_chars": (res or {}).get("copyable_chars", "?"),
               "hit_pages": (res or {}).get("hit_pages", []),
               "hit_page_count": len((res or {}).get("hit_pages", []))}
        _job_set(jid, status="done", result=out)
        log("searchable job %s done pages=%d" % (jid, total))
    except Exception as exc:  # noqa: BLE001
        _job_set(jid, status="error", error="%s: %s" % (type(exc).__name__, exc))
        log("searchable job %s error %s" % (jid, exc))

class Handler(BaseHTTPRequestHandler):
    server_version = "llm-ocr-serve/" + SERVE_VERSION

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

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
                models = list_models(base_url=base_url or None, api_key=key or None)
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
            elif path.startswith("/api/jobs/"):
                jid = path[len("/api/jobs/"):]
                job = _job_get(jid)
                if not job:
                    self._send(404, {"error": "unknown job"})
                    return
                out = {k: v for k, v in job.items() if k != "cancel"}
                if job.get("kind") == "batch" and job.get("status") == "running":
                    try:
                        out["progress"] = _progress_from_usage(job.get("outdir") or "")
                    except Exception:
                        pass
                self._send(200, out)
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log("GET %s failed %s" % (path, exc))
            self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        try:
            data = _body(self)
            if "_raw_error" in data:
                self._send(400, {"error": data["_raw_error"]})
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
            elif path == "/api/searchable/build":
                self._handle_searchable(data)
            elif path == "/api/notation/check":
                self._handle_notation(data)
            elif path == "/api/file/b64":
                self._handle_file_b64(data)
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                jid = path[len("/api/jobs/"):-len("/cancel")]
                with JOBS_LOCK:
                    if jid in JOBS:
                        JOBS[jid]["cancel"] = True
                        self._send(200, {"ok": True})
                    else:
                        self._send(404, {"error": "unknown job"})
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log("POST %s failed %s" % (path, exc))
            traceback.print_exc()
            self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    # ---- handlers ----

    def _handle_probe(self, data):
        import time as _time
        llm, extra = _llm_kwargs(data)
        endpoint = (data.get("endpoint") or "responses").strip().lower()
        log("probe ep=%s model=%s key=%s" % (endpoint, llm["model"], masked_key(llm["api_key"])))
        png = _res_file("tests", "cand_165.png").read_bytes()
        import llm_client as C
        prompt = "Transcribe this page exactly (OCR only):"
        t0 = _time.monotonic()
        if endpoint == "auto":
            text, usage = C.auto_vision(png, prompt, **llm, **extra)
        elif endpoint == "responses":
            text, usage = C.responses_vision(png, prompt, **llm, **extra)
        else:
            text, usage = C.chat_vision(png, prompt, **llm, **extra)
        ms = int((_time.monotonic() - t0) * 1000)
        ep_used = str((usage or {}).get("_endpoint_normalized", endpoint)) if isinstance(usage, dict) else endpoint
        self._send(200, {"endpoint_used": ep_used, "text": text,
                         "usage": usage if isinstance(usage, dict) else {},
                         "elapsed_ms": ms})

    def _handle_ocr_image(self, data):
        from ocr_page import ocr_image
        _ensure_prompt_module()
        llm, extra = _llm_kwargs(data)
        try:
            png = base64.b64decode(data.get("png_b64") or "", validate=True)
        except (binascii.Error, ValueError):
            self._send(400, {"error": "png_b64 is not valid base64"})
            return
        if not png:
            self._send(400, {"error": "png_b64 required"})
            return
        text = ocr_image(png, base_url=llm["base_url"], model=llm["model"],
                         api_key=llm["api_key"], endpoint=data.get("endpoint") or "responses",
                         detail=llm["detail"], extra=extra, timeout=data.get("timeout"),
                         prompt=_request_prompt(data))
        self._send(200, {"markdown": text})

    def _handle_ocr_url(self, data):
        from ocr_page import ocr_image_url
        _ensure_prompt_module()
        llm, extra = _llm_kwargs(data)
        url = (data.get("image_url") or "").strip()
        if not url:
            self._send(400, {"error": "image_url required"})
            return
        text = ocr_image_url(url, base_url=llm["base_url"], model=llm["model"],
                             api_key=llm["api_key"], endpoint=data.get("endpoint") or "responses",
                             detail=llm["detail"], extra=extra, timeout=data.get("timeout"),
                             prompt=_request_prompt(data))
        self._send(200, {"markdown": text})

    def _handle_ocr_pdf_page(self, data):
        from ocr_page import ocr_pdf_page
        _ensure_prompt_module()
        from render import render_page
        llm, extra = _llm_kwargs(data)
        pdf = (data.get("pdf_path") or "").strip()
        if not pdf or not Path(pdf).is_file():
            self._send(400, {"error": "pdf_path must be an existing file"})
            return
        try:
            pno = int(data.get("pno", 0))
        except (TypeError, ValueError):
            self._send(400, {"error": "pno must be an integer"})
            return
        try:
            dpi = int(data.get("dpi") or 200)
        except (TypeError, ValueError):
            dpi = 200
        preview = base64.b64encode(render_page(pdf, pno, 150)).decode("ascii")
        text = ocr_pdf_page(pdf, pno, base_url=llm["base_url"], model=llm["model"],
                            api_key=llm["api_key"], endpoint=data.get("endpoint") or "responses",
                            detail=llm["detail"], extra=extra, dpi=dpi, timeout=data.get("timeout"),
                            prompt=_request_prompt(data))
        self._send(200, {"markdown": text, "png_b64_preview": preview})

    def _handle_dry_run(self, data):
        import fitz
        from batch_plan import page_plan
        pdf = (data.get("pdf_path") or "").strip()
        if not pdf or not Path(pdf).is_file():
            self._send(400, {"error": "pdf_path must be an existing file"})
            return
        outdir = (data.get("outdir") or "out/book_gui").strip() or "out/book_gui"
        start = int(data.get("start") or 0)
        end = data.get("end")
        end = int(end) if end is not None and str(end).strip() != "" else None
        plan = page_plan(Path(pdf), Path(outdir), start, end)
        with fitz.open(str(pdf)) as doc:
            total = doc.page_count
        first5 = [{"pno_0based": i["pno_0based"], "page_number": i["page_number"]} for i in plan[:5]]
        self._send(200, {"total_pages": total, "planned_pages": len(plan), "first5": first5})

    def _handle_batch_run(self, data):
        pdf = (data.get("pdf_path") or "").strip()
        if not pdf or not Path(pdf).is_file():
            self._send(400, {"error": "pdf_path must be an existing file"})
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
        _job_set(jid, outdir=outdir)
        threading.Thread(target=_run_batch_job,
                         args=(jid, pdf, outdir, kw, cleanup), daemon=True).start()
        log("batch job %s started pdf=%s key=%s" % (jid, pdf, masked_key(llm["api_key"])))
        self._send(200, {"job_id": jid})

    def _handle_searchable(self, data):
        pdf = (data.get("pdf_path") or "").strip()
        outdir = (data.get("outdir") or "").strip()
        if not pdf or not Path(pdf).is_file():
            self._send(400, {"error": "pdf_path must be an existing file"})
            return
        if not outdir or not Path(outdir).is_dir():
            self._send(400, {"error": "outdir must contain pages/page_*.md"})
            return
        geo = (data.get("geo_source") or "auto").strip()
        kws = data.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.replace("\uff0c", ",").split(",") if k.strip()]
        jid = _new_job("searchable", "searchable %s" % Path(pdf).name)
        threading.Thread(target=_run_searchable_job,
                         args=(jid, pdf, outdir, geo, kws), daemon=True).start()
        self._send(200, {"job_id": jid})

    def _handle_file_b64(self, data):
        p = (data.get("path") or "").strip()
        if not p or not Path(p).is_file():
            self._send(400, {"error": "path must be an existing file"})
            return
        if Path(p).stat().st_size > 50 * 1024 * 1024:
            self._send(400, {"error": "file too large (>50MB)"})
            return
        b64 = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        self._send(200, {"b64": b64, "bytes": len(b64)})

    def _handle_notation(self, data):
        from collections import Counter
        from check_notation import check_file
        d = (data.get("dir") or "").strip()
        if not d or not Path(d).is_dir():
            self._send(400, {"error": "dir must be an existing directory"})
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

def _acquire_lock(port):
    # sidecar-safe: lock lives in tempdir, not next to the exe
    import tempfile as _tf
    lock = Path(_tf.gettempdir()) / ("llm-ocr-serve-%d.lock" % port)
    try:
        fd = open(str(lock), "x")
        fd.write(str(time.time()))
        fd.close()
        return lock
    except FileExistsError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="llm-ocr engine bridge")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    lock = _acquire_lock(args.port)
    if lock is None:
        print("serve already running (lock exists); exiting", flush=True)
        return 2
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        srv.daemon_threads = True
        log("listening on 127.0.0.1:%d version=%s" % (args.port, SERVE_VERSION))
        srv.serve_forever()
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
