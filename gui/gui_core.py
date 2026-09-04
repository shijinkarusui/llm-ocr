"""llm-ocr GUI shared core: theme, state, background runner, log bus (Tkinter, stdlib only)."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


PAPER = "#EDF0F4"
PANEL = "#FFFFFF"
EDGE = "#D5DCE6"
INK = "#1C2733"
MUTED = "#5D6E83"
FAINT = "#8A99AD"
INDIGO = "#2F4B9B"
INDIGO_DARK = "#243C7E"
PINE = "#1E6B5A"
AMBER = "#96690F"
BRICK = "#B3362B"
FIELD_BG = "#FFFFFF"
FIELD_DIS = "#F1F4F8"
LOG_BG = "#141D2B"
LOG_FG = "#E4EAF3"
LOG_OK = "#6FD3A0"
LOG_WARN = "#E5B567"
LOG_ERR = "#F08A80"
LOG_TIME = "#5E7386"

BG = PAPER
BG2 = PANEL
BG3 = "#DCE3EC"
FG = INK
ACCENT = PINE
ACCENT2 = "#C99A2B"
DANGER = BRICK
BLUE = INDIGO
ENTRY_BG = FIELD_BG

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_SUB = ("Microsoft YaHei UI", 9)
FONT_SECTION = ("Microsoft YaHei UI", 11, "bold")
FONT_HINT = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 9)
FONT_OUT = ("Microsoft YaHei UI", 10)


def _ensure_process_dpi_awareness() -> str:
    """Set Windows DPI awareness before any Tk window exists.

    Tries Per-Monitor V2 (SetProcessDpiAwareness(2)), falls back to
    legacy SetProcessDPIAware. Silent no-op on non-Windows or when
    shcore/user32 is unavailable. Returns a short label."""
    try:
        import ctypes
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return "skip-non-windows"
        try:
            shcore = windll.shcore
            rc = shcore.SetProcessDpiAwareness(2)
            return "per-monitor-v2" if rc in (0, None) else "per-monitor-v2-rc-%s" % (rc,)
        except Exception:
            pass
        try:
            windll.user32.SetProcessDPIAware()
            return "system-fallback"
        except Exception:
            return "skip-unavailable"
    except Exception:
        return "skip-error"


def _query_window_dpi(root) -> tuple:
    """Return (dpi, source) for a live Tk root, best effort."""
    # 1) GetDpiForWindow(hwnd) -- Win10 1607+, most accurate per-monitor.
    try:
        import ctypes
        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            try:
                hwnd = int(root.winfo_id())
                get_dpi = getattr(windll.user32, "GetDpiForWindow", None)
                if get_dpi is not None:
                    get_dpi.restype = ctypes.c_uint
                    dpi = int(get_dpi(hwnd))
                    if 48 <= dpi <= 768:
                        return (float(dpi), "GetDpiForWindow")
            except Exception:
                pass
            # 2) GetDeviceCaps(LOGPIXELSX=88) via screen DC.
            try:
                LOGPIXELSX = 88
                hdc = windll.user32.GetDC(0)
                if hdc:
                    try:
                        dpi = int(windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX))
                        if 48 <= dpi <= 768:
                            return (float(dpi), "GetDeviceCaps-LOGPIXELSX")
                    finally:
                        try:
                            windll.user32.ReleaseDC(0, hdc)
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    # 3) Tk fallback: pixels per inch (reflects true DPI once aware).
    try:
        dpi = float(root.winfo_fpixels("1i"))
        if 48 <= dpi <= 768:
            return (dpi, "winfo_fpixels")
    except Exception:
        pass
    return (96.0, "default-96")


def init_dpi(root_or_none=None):
    """Shared DPI init for main() and _smoke().

    Phase 1 (always): process DPI awareness, must run before the first
    Tk() is created; re-calling later is a harmless no-op.
    Phase 2 (root given): Tk 8.6+ already auto-sets tk scaling from the
    real DPI once aware; we still enforce scaling = dpi/72 (rounded to
    2 decimals, clamped to [1.0, 4.0]) as belt-and-braces, then record
    the effective value Tk keeps (Tk may quantize to exact dpi/72).
    Never raises: all platform calls are guarded so --smoke cannot crash.
    Returns dict(awareness, dpi, source, scaling_auto, scaling_target,
    scaling_effective)."""
    awareness = _ensure_process_dpi_awareness()
    info = {"awareness": awareness, "dpi": None, "source": None,
            "scaling_auto": None, "scaling_target": None,
            "scaling_effective": None}
    if root_or_none is None:
        return info
    try:
        root = root_or_none
        try:
            info["scaling_auto"] = float(root.tk.call("tk", "scaling"))
        except Exception:
            pass
        dpi, source = _query_window_dpi(root)
        scaling = round(float(dpi) / 72.0, 2)
        # Clamp to a sane Tk range.
        if scaling < 1.0:
            scaling = 1.0
        elif scaling > 4.0:
            scaling = 4.0
        try:
            root.tk.call("tk", "scaling", scaling)
        except Exception:
            pass
        try:
            info["scaling_effective"] = float(root.tk.call("tk", "scaling"))
        except Exception:
            pass
        info.update({"dpi": dpi, "scaling_target": scaling, "source": source})
    except Exception:
        pass
    return info


class State:
    """Shared session state (Key lives in memory only, never written)."""

    def __init__(self) -> None:
        self.base_url = "http://YOUR_GATEWAY_HOST:2113/v1"
        self.key = os.environ.get("LLM_OCR_KEY", "")
        self.model = "OC/muse-spark-1.3-contributor-free"
        self.endpoint = "responses"
        self.detail = "high"
        self.concurrency = 10
        self.timeout = 120
        self.dpi = 200
        self.retries = 2
        self.extra_text = (
            '{"max_output_tokens":100000,"reasoning":{"effort":"medium"},'
            '"temperature":0.5,"frequency_penalty":0.5,"presence_penalty":0.5}'
        )

    def extra(self) -> dict:
        import json

        raw = (self.extra_text or "").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("--extra-json must be a JSON object")
        return data


def masked_key(key: str) -> str:
    if not key:
        return "(empty)"
    return "sk-**** len=%d" % len(key)


def resource_root() -> Path:
    """Project/resource root: frozen (MEIPASS) or dev (project dir)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def prompt_path() -> Path:
    return resource_root() / "prompts" / "ocr_system.md"


def ensure_prompt() -> Path:
    """Point ocr_page.PROMPT_PATH (and batch_plan.DEFAULT_PROMPT) at the bundled prompt."""
    fallback = prompt_path()
    try:
        import ocr_page as _op
        try:
            cur = _op.PROMPT_PATH
            exists = cur.is_file() if isinstance(cur, Path) else False
        except OSError:
            exists = False
        if not exists and fallback.is_file():
            _op.PROMPT_PATH = fallback
            cur = fallback
    except ImportError:
        cur = fallback
    try:
        import batch_plan as _bp
        try:
            bcur = _bp.DEFAULT_PROMPT
            bexists = bcur.is_file() if isinstance(bcur, Path) else False
        except OSError:
            bexists = False
        if not bexists and fallback.is_file():
            _bp.DEFAULT_PROMPT = fallback
    except ImportError:
        pass
    return cur


def apply_theme(root: tk.Tk) -> None:
    root.configure(bg=PAPER)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=PAPER, foreground=INK, font=FONT_UI)
    style.configure("TFrame", background=PAPER)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=PAPER, foreground=INK)
    style.configure("Title.TLabel", background=PAPER, foreground=INK, font=FONT_TITLE)
    style.configure("Sub.TLabel", background=PAPER, foreground=MUTED, font=FONT_SUB)
    style.configure("Muted.TLabel", background=PAPER, foreground=MUTED)
    style.configure("Panel.TLabel", background=PANEL, foreground=INK)
    style.configure("Section.TLabel", background=PANEL, foreground=INK, font=FONT_SECTION)
    style.configure("Field.TLabel", background=PANEL, foreground=MUTED)
    style.configure("Hint.TLabel", background=PAPER, foreground=MUTED, font=FONT_HINT)
    style.configure("PanelHint.TLabel", background=PANEL, foreground=MUTED, font=FONT_HINT)
    style.configure("Stat.TLabel", background=PAPER, foreground=INK, font=FONT_UI)
    style.configure("TButton", background="#FFFFFF", foreground=INK,
                    borderwidth=1, padding=(12, 7))
    style.map("TButton",
              background=[("active", "#E4EAF2"), ("disabled", FIELD_DIS)],
              foreground=[("disabled", FAINT)])
    style.configure("Primary.TButton", background=INDIGO, foreground="#FFFFFF",
                    borderwidth=0, padding=(14, 7), font=("Microsoft YaHei UI", 10, "bold"))
    style.map("Primary.TButton",
              background=[("active", INDIGO_DARK), ("disabled", "#9AA6C4")],
              foreground=[("disabled", "#FFFFFF")])
    style.configure("Danger.TButton", background="#F9EDEB", foreground=BRICK, borderwidth=0)
    style.map("Danger.TButton", background=[("active", "#F2DCD7")])
    style.configure("Ghost.TButton", background=PAPER, foreground=MUTED, borderwidth=0)
    style.map("Ghost.TButton",
              background=[("active", "#DCE3EC")],
              foreground=[("active", INK)])
    style.configure("TEntry", fieldbackground=FIELD_BG, foreground=INK,
                    insertcolor=INK, padding=4)
    style.map("TEntry", fieldbackground=[("disabled", FIELD_DIS)],
              foreground=[("disabled", FAINT)])
    style.configure("TCombobox", fieldbackground=FIELD_BG, foreground=INK,
                    arrowcolor=MUTED, padding=4)
    style.configure("TSpinbox", fieldbackground=FIELD_BG, foreground=INK,
                    insertcolor=INK, arrowcolor=MUTED, padding=3)
    style.configure("TNotebook", background=PAPER, borderwidth=0)
    style.configure("TNotebook.Tab", background="#DCE3EC", foreground=MUTED,
                    padding=(14, 8), font=FONT_UI)
    style.map("TNotebook.Tab",
              background=[("selected", PANEL)],
              foreground=[("selected", INK)])
    style.configure("Horizontal.TProgressbar", background=PINE,
                    troughcolor="#DCE3EC", borderwidth=0, thickness=10)
    style.configure("TScale", background=PANEL, troughcolor="#D8E0EB", borderwidth=0)
    style.configure("TRadiobutton", background=PANEL, foreground=INK)
    style.configure("TSeparator", background=EDGE)


class LogBus:
    """Thread-safe log sink feeding a Text widget."""

    def __init__(self, text: tk.Text) -> None:
        self.text = text
        self.q: queue.Queue[tuple] = queue.Queue()
        text.tag_configure("ok", foreground=LOG_OK)
        text.tag_configure("warn", foreground=LOG_WARN)
        text.tag_configure("err", foreground=LOG_ERR)
        text.tag_configure("muted", foreground="#8EA0B5")
        text.tag_configure("time", foreground=LOG_TIME)

    def emit(self, msg: str, tag: str = "") -> None:
        self.q.put((time.strftime("%H:%M:%S"), msg, tag))

    def pump(self) -> None:
        try:
            while True:
                stamp, msg, tag = self.q.get_nowait()
                self.text.configure(state="normal")
                self.text.insert("end", stamp + "  ", "time")
                self.text.insert("end", msg + "\n", tag)
                self.text.see("end")
                self.text.configure(state="disabled")
        except queue.Empty:
            pass


class Runner:
    """Run blocking backend calls in a daemon thread; on_done back on UI thread."""

    def __init__(self, root: tk.Tk, log: LogBus) -> None:
        self.root = root
        self.log = log
        self.busy = False

    def run(self, label: str, fn, on_done=None) -> bool:
        if self.busy:
            self.log.emit("忙碌中，请稍候…", "warn")
            return False
        self.busy = True
        self.log.emit("开始：" + label, "muted")

        def _work() -> None:
            try:
                out = fn()
                self.root.after(0, lambda: self._done(label, out, None, on_done))
            except Exception as exc:  # noqa: BLE001 - surface to UI
                self.root.after(0, lambda: self._done(label, None, exc, on_done))

        threading.Thread(target=_work, daemon=True).start()
        return True

    def _done(self, label: str, out, exc, on_done) -> None:
        self.busy = False
        if exc is not None:
            self.log.emit("%s 失败：%s: %s" % (label, type(exc).__name__, exc), "err")
        else:
            self.log.emit("%s 完成" % label, "ok")
        if on_done is not None:
            try:
                on_done(out, exc)
            except Exception as exc2:  # noqa: BLE001
                self.log.emit("回调异常：%s" % exc2, "err")


def card(parent, title: str, hint: str = ""):
    shell = tk.Frame(parent, bg=EDGE, bd=0)
    shell.pack(fill="x", padx=12, pady=7)
    body = ttk.Frame(shell, style="Panel.TFrame", padding=(14, 11, 14, 12))
    body.pack(fill="both", expand=True, padx=1, pady=1)
    top = ttk.Frame(body, style="Panel.TFrame")
    top.pack(fill="x", pady=(0, 2))
    tk.Frame(top, bg=INDIGO, width=3, height=18, bd=0).pack(side="left", padx=(0, 9))
    ttk.Label(top, text=title, style="Section.TLabel").pack(side="left")
    if hint:
        ttk.Label(body, text=hint, style="PanelHint.TLabel",
                  wraplength=660, justify="left").pack(anchor="w", pady=(2, 0))
    ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(9, 8))
    return body


def row(parent, label: str, widget, width: int = 13):
    line = ttk.Frame(parent, style="Panel.TFrame")
    line.pack(fill="x", pady=3)
    line.columnconfigure(0, weight=0)
    line.columnconfigure(1, weight=1)
    ttk.Label(line, text=label, style="Field.TLabel", width=width).grid(row=0, column=0, sticky="w")
    widget.grid(row=0, column=1, sticky="ew", padx=(10, 0), in_=line)
    try:
        widget.lift()
    except tk.TclError:
        pass
    return line


def make_output(parent, height: int = 12, font=None, wrap: str = "word"):
    box = tk.Text(parent, height=height, bg="#FFFFFF", fg=INK,
                  insertbackground=INK, relief="flat", wrap=wrap,
                  highlightthickness=1, highlightbackground=EDGE,
                  highlightcolor=INDIGO, font=font or FONT_MONO)
    box.tag_configure("ph", foreground=FAINT)
    return box


def _set_text(widget: tk.Text, text: str, tag: str = "") -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("end", text, tag)
    widget.configure(state="disabled")


def placeholder(widget: tk.Text, text: str) -> None:
    _set_text(widget, text, "ph")


def show(widget: tk.Text, text: str) -> None:
    _set_text(widget, text)


def status_dot(parent, size: int = 10):
    return tk.Label(parent, text="\u25cf", font=("Microsoft YaHei UI", size),
                    bg=PAPER, fg=FAINT, bd=0)
