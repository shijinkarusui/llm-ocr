"""Phase0 contract: error envelope + progress schema + searchable missing pages.

All offline: no gateway calls. Spins serve.py on a free port and asserts the
P0 unified envelope (error_code/hint_cn/next_action) on every 4xx/404 path.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BABACI = Path("E:/down/QQ2/QQ/1999 XXSX XDZWHW BYC ZB BSYYG.pdf")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(port, timeout=20.0):
    t0 = time.monotonic()
    url = "http://127.0.0.1:%d/api/health" % port
    while time.monotonic() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.3)
    return False


def _post(port, path, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(port, path, headers=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_serve_offline_contract():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "serve.py"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait(port), "serve did not come up"
        code, health = _get(port, "/api/health")
        assert code == 200 and health.get("ok") is True
        code, prompt = _get(port, "/api/prompt")
        assert code == 200 and len(prompt.get("prompt", "")) > 100
        if BABACI.is_file():
            code, dry = _post(port, "/api/batch/dry-run",
                              {"pdf_path": str(BABACI), "outdir": "out/book_gui", "start": 0})
            assert code == 200 and dry.get("total_pages") == 764
        code, chk = _post(port, "/api/notation/check", {"dir": "out/nonexistent-dir-xyz"})
        assert code == 400
        # P0 envelope: every 4xx carries error_code/hint_cn/next_action.
        assert chk.get("error_code") and chk.get("hint_cn") is not None and chk.get("next_action") is not None
        with tempfile.TemporaryDirectory() as tmp:
            code, chk = _post(port, "/api/notation/check", {"dir": tmp})
            assert code == 200 and chk.get("files") == 0
    finally:
        proc.terminate()


def test_error_envelope_paths():
    """Every remaining 4xx/404 path returns the unified envelope (offline, no key)."""
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "serve.py"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait(port), "serve did not come up"

        def _assert_envelope(code, body, expect_code):
            assert code == expect_code, (code, body)
            assert body.get("error"), body
            assert body.get("error_code"), body
            assert body.get("hint_cn") is not None, body
            assert body.get("next_action") is not None, body
            return body

        # Unknown API routes (GET + POST).
        code, body = _get(port, "/api/nope-missing")
        _assert_envelope(code, body, 404)
        code, body = _post(port, "/api/nope-missing", {})
        _assert_envelope(code, body, 404)
        # Unknown job (GET + cancel + pages).
        code, body = _get(port, "/api/jobs/deadbeefcafe")
        _assert_envelope(code, body, 404)
        code, body = _post(port, "/api/jobs/deadbeefcafe/cancel", {})
        _assert_envelope(code, body, 404)
        code, body = _get(port, "/api/jobs/deadbeefcafe/pages?status=failed")
        _assert_envelope(code, body, 404)
        # Param guards.
        code, body = _post(port, "/api/batch/dry-run", {"pdf_path": "no/such.pdf"})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/batch/run", {"pdf_path": "no/such.pdf"})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/ocr/pdf-page", {"pdf_path": "no/such.pdf", "pno": 0})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/ocr/image", {"png_b64": "!!!"})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/ocr/url", {"image_url": ""})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/probe", {"endpoint": "bogus-ep", "timeout": 5})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/searchable/build", {"pdf_path": "", "outdir": "no/such-dir"})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/book/resolve", {"dir": ""})
        _assert_envelope(code, body, 400)
        code, body = _post(port, "/api/file/b64", {"path": "no/such.png"})
        _assert_envelope(code, body, 400)
        # Searchable bind failure envelope keeps machine-readable status+candidates.
        code, body = _post(port, "/api/searchable/build",
                           {"pdf_path": "", "outdir": str(ROOT / "tests"), "geo_source": "auto"})
        _assert_envelope(code, body, 400)
        assert "status" in body and "candidates" in body, body
        # Geo external stays rejected with an actionable hint.
        with tempfile.TemporaryDirectory() as tmp:
            code, body = _post(port, "/api/searchable/build",
                               {"pdf_path": "", "outdir": tmp, "geo_source": "external"})
            _assert_envelope(code, body, 400)
            assert body.get("error_code") == "BAD_EXTRA_JSON", body
    finally:
        proc.terminate()


def test_progress_schema_shape():
    """_progress_from_usage returns the full P0 progress schema keys (pure unit)."""
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    import importlib
    serve = importlib.import_module("serve")
    importlib.reload(serve)
    with tempfile.TemporaryDirectory() as tmp:
        # No usage.jsonl yet: zeros, not KeyErrors.
        prog = serve._progress_from_usage(tmp, planned=10, started_at=time.time() - 60,
                                          concurrency={"current": 3, "init": 4})
        for key in ("records", "pages_ok", "tokens", "skipped", "failed",
                    "p50_ms", "p95_ms", "n429", "planned", "pct",
                    "elapsed_ms", "pages_per_min", "eta_ms",
                    "concurrency_current", "concurrency_init"):
            assert key in prog, (key, prog)
            assert isinstance(prog[key], (int, float)), (key, prog[key])


def test_searchable_missing_pages_shape():
    """_run_searchable_job with gaps records structured MISSING_PAGES (tiny PDF, offline)."""
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    import importlib
    serve = importlib.import_module("serve")
    importlib.reload(serve)
    pytest = sys.modules.get("pytest")
    fitz = pytest and None
    try:
        import fitz as _fitz
    except ImportError:
        import unittest
        raise unittest.SkipTest("PyMuPDF missing")
    with tempfile.TemporaryDirectory() as tmp:
        pdf = str(Path(tmp) / "tiny.pdf")
        doc = _fitz.open()
        for _ in range(3):
            doc.new_page(width=200, height=200)
        doc.save(pdf)
        doc.close()
        pages = Path(tmp) / "pages"
        pages.mkdir()
        (pages / "page_0000.md").write_text("only page zero\n", encoding="utf-8")
        jid = serve._new_job("searchable", "missing-shape")
        serve._run_searchable_job(jid, pdf, str(pages), tmp, "auto", [])
        job = serve._job_get(jid)
        assert job.get("status") == "error", job
        assert job.get("error_code") == "MISSING_PAGES", job
        assert job.get("missing_count") == 2, job
        assert job.get("missing_pages") == [1, 2], job
        assert job.get("missing_example") == [1, 2], job
