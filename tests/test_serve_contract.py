"""S2 contract: serve.py offline endpoints on an ephemeral port (no LLM calls)."""
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


def _get(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


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
        with tempfile.TemporaryDirectory() as tmp:
            code, chk = _post(port, "/api/notation/check", {"dir": tmp})
            assert code == 200 and chk.get("files") == 0
    finally:
        proc.terminate()
