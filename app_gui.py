"""llm-ocr desktop GUI entry (Tkinter, stdlib only). Run: python app_gui.py (or packaged EXE)."""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

if getattr(sys, "frozen", False):
    _meipass = getattr(sys, "_MEIPASS", None)
    if _meipass and str(_meipass) not in sys.path:
        sys.path.insert(0, str(_meipass))
else:
    SRC = Path(__file__).resolve().parent / "src"
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    GUI_DIR = Path(__file__).resolve().parent / "gui"
    if str(GUI_DIR) not in sys.path:
        sys.path.insert(0, str(GUI_DIR))

from gui_core import (  # noqa: E402
    PAPER, LOG_BG, LOG_FG, FONT_MONO, FONT_TITLE, FONT_SUB,
    State, Runner, LogBus, apply_theme, init_dpi,
)

import tab_connect  # noqa: E402
import tab_single  # noqa: E402
import tab_batch  # noqa: E402
import tab_searchable  # noqa: E402
import tab_params  # noqa: E402


def main() -> int:
    init_dpi(None)  # process DPI awareness (Per-Monitor V2) before first Tk()
    root = tk.Tk()
    init_dpi(root)  # tk scaling = dpi/72, must run before themed widgets
    root.title("llm-ocr · 语音学教程 OCR")
    root.geometry("1080x780")
    root.minsize(940, 640)
    try:
        root.state("zoomed")
    except tk.TclError:
        pass
    apply_theme(root)

    state = State()

    head = ttk.Frame(root, padding=(16, 12, 16, 4))
    head.pack(fill="x")
    title = ttk.Frame(head)
    title.pack(fill="x")
    ttk.Label(title, text="llm-ocr · 语音学教程 OCR", style="Title.TLabel").pack(side="left")
    step = ttk.Label(title, text="先连网关，再跑单页或批量",
                     style="Sub.TLabel")
    step.pack(side="left", padx=(14, 0))
    bar = tk.Frame(head, bg="#2F4B9B", height=3, bd=0)
    bar.pack(fill="x", pady=(9, 0))

    body = ttk.Frame(root, padding=(8, 4, 8, 0))
    body.pack(fill="both", expand=True)
    nb = ttk.Notebook(body)
    nb.pack(fill="both", expand=True)

    log_frame = ttk.Frame(root, padding=(12, 6, 12, 10))
    log_frame.pack(fill="x")
    log_head = ttk.Frame(log_frame)
    log_head.pack(fill="x", pady=(0, 4))
    ttk.Label(log_head, text="运行日志", style="Muted.TLabel").pack(side="left")
    ttk.Label(log_head, text="Key 仅驻内存，不落盘",
              style="Muted.TLabel").pack(side="right")
    log_shell = tk.Frame(log_frame, bg="#D5DCE6", bd=0)
    log_shell.pack(fill="x")
    log_view = tk.Text(log_shell, height=6, bg=LOG_BG, fg=LOG_FG,
                       insertbackground=LOG_FG, font=FONT_MONO,
                       relief="flat", wrap="word",
                       highlightthickness=0, bd=0)
    log_view.pack(fill="x", padx=1, pady=1)
    log_view.configure(state="disabled")
    log = LogBus(log_view)
    runner = Runner(root, log)

    tab_connect.build(nb, state, runner, log)
    tab_single.build(nb, state, runner, log)
    tab_batch.build(nb, state, runner, log)
    tab_searchable.build(nb, state, runner, log)
    tab_params.build(nb, state, runner, log)

    def _pump() -> None:
        log.pump()
        root.after(120, _pump)

    _pump()
    log.emit("就绪。先在第 1 页同步参数并探活，再跑单页或批量。", "muted")
    root.mainloop()
    return 0


def _smoke(argv: list[str]) -> int:
    """Headless build check: construct all tabs without entering mainloop."""
    import json
    import tempfile
    from gui_core import ensure_prompt  # noqa: E402
    target = None
    for arg in argv:
        if arg.startswith("--smoke-file="):
            target = Path(arg.split("=", 1)[1])
    if target is None:
        target = Path(tempfile.gettempdir()) / "llmocr_smoke.json"
    init_dpi(None)  # same DPI init as main(), keeps --smoke from crashing
    root = tk.Tk()
    root.withdraw()
    init_dpi(root)
    apply_theme(root)
    state = State()
    nb = ttk.Notebook(root)
    log = LogBus(tk.Text(root))
    runner = Runner(root, log)
    built: list[str] = []
    for name, mod in (("connect", tab_connect), ("single", tab_single),
                      ("batch", tab_batch), ("searchable", tab_searchable),
                      ("params", tab_params)):
        mod.build(nb, state, runner, log)
        built.append(name)
    prompt = ensure_prompt()
    result = {
        "ok": len(nb.tabs()) == 5,
        "tabs": built,
        "tab_count": len(nb.tabs()),
        "prompt": str(prompt),
        "prompt_exists": Path(prompt).is_file() if prompt else False,
        "extra_keys": sorted(state.extra().keys()),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    target.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    print("SMOKE-OK " + json.dumps(result, ensure_ascii=True))
    root.destroy()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        raise SystemExit(_smoke(sys.argv))
    raise SystemExit(main())
