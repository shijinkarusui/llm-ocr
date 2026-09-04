"""Tab 1: single image / PDF-page OCR with preview."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from gui_core import State, Runner, LogBus, card, row, make_output, show, FONT_OUT


def build(notebook: ttk.Notebook, state: State, runner: Runner, log: LogBus) -> None:
    page = ttk.Frame(notebook, padding=(2, 6, 2, 8))
    notebook.add(page, text="2 · 单页")

    c = card(page, "输入",
             "图片直传，PDF 按页渲染，远端 URL 由网关拉取。")
    kind_v = tk.StringVar(value="image")
    kind_bar = ttk.Frame(c, style="Panel.TFrame")
    kind_bar.pack(fill="x", pady=(2, 4))
    ttk.Label(kind_bar, text="来源", style="Field.TLabel", width=13).pack(side="left")
    for label, value in (("图片", "image"), ("PDF 单页", "pdf-page"), ("图片链接", "image-url")):
        ttk.Radiobutton(kind_bar, text=label, value=value,
                        variable=kind_v).pack(side="left", padx=(0, 12))
    path_e = ttk.Entry(c)
    url_e = ttk.Entry(c)
    row(c, "本地路径", path_e)
    row(c, "远端 URL", url_e)
    prow = ttk.Frame(c, style="Panel.TFrame")
    prow.pack(fill="x", pady=3)
    ttk.Label(prow, text="PDF 页码", style="Field.TLabel", width=13).pack(side="left")
    pno_s = ttk.Spinbox(prow, from_=0, to=1000, width=8)
    pno_s.set("0")
    pno_s.pack(side="left", padx=(10, 0))
    ttk.Label(prow, text="从 0 开始数", style="PanelHint.TLabel").pack(side="left", padx=(10, 0))
    brow = ttk.Frame(c, style="Panel.TFrame")
    brow.pack(fill="x", pady=(10, 0))
    ttk.Button(brow, text="选图片…",
               command=lambda: _pick(path_e, [("images", "*.png *.jpg *.jpeg *.webp"), ("all", "*.*")])).pack(side="left")
    ttk.Button(brow, text="选 PDF…",
               command=lambda: _pick(path_e, [("PDF", "*.pdf")])).pack(side="left", padx=(8, 0))


    r = card(page, "识别结果", "成功显示 Markdown，失败看底部运行日志。")
    out = make_output(r, height=12, font=FONT_OUT)
    out.pack(fill="both", expand=True)
    out.configure(state="normal")
    out.insert("end", "还没有结果。选好来源后点“开始识别”。", "ph")
    out.configure(state="disabled")

    def _pick(entry: ttk.Entry, types) -> None:
        p = filedialog.askopenfilename(filetypes=list(types))
        if p:
            entry.delete(0, "end")
            entry.insert(0, p)

    def _do_ocr() -> None:
        kind = kind_v.get()
        try:
            extra = state.extra()
        except ValueError as exc:
            log.emit("extra-json 非法：" + str(exc), "err")
            return
        try:
            from gui_core import ensure_prompt
            ensure_prompt()
        except Exception:
            pass
        if state.timeout is not None:
            extra.setdefault("timeout", state.timeout)
        def _fn():
            if kind == "image-url":
                url = url_e.get().strip()
                if not url:
                    raise ValueError("请填写远端 URL")
                from ocr_page import _norm_ep as _ne
                import llm_client as C
                from ocr_page import PROMPT_PATH
                prompt = PROMPT_PATH.read_text(encoding="utf-8")
                ep = _ne(state.endpoint)
                kw = dict(base_url=state.base_url, model=state.model,
                          api_key=state.key, detail=state.detail)
                if ep == "auto":
                    t, _u = C.auto_vision(url, prompt, **kw, **extra)
                elif ep == "responses":
                    t, _u = C.responses_vision_url(url, prompt, **kw, **extra)
                else:
                    t, _u = C.chat_vision_url(url, prompt, **kw, **extra)
                return t
            if kind == "pdf-page":
                from ocr_page import ocr_pdf_page
                pdf = path_e.get().strip()
                if not pdf or not Path(pdf).is_file():
                    raise ValueError("请选择有效 PDF")
                return ocr_pdf_page(pdf, int(pno_s.get() or 0), base_url=state.base_url, model=state.model,
                                    api_key=state.key, endpoint=state.endpoint, detail=state.detail,
                                    extra=extra, timeout=state.timeout)
            from ocr_page import ocr_image
            img = path_e.get().strip()
            if not img or not Path(img).is_file():
                raise ValueError("请选择有效图片")
            data = Path(img).read_bytes()
            return ocr_image(data, base_url=state.base_url, model=state.model, api_key=state.key,
                             endpoint=state.endpoint, detail=state.detail, extra=extra, timeout=state.timeout)
        def _done(text, exc) -> None:
            if exc is None and text:
                show(out, text)
            else:
                show(out, "识别失败，详情见底部运行日志。")
        runner.run("单页 OCR", _fn, _done)

    def _do_save() -> None:
        p = filedialog.asksaveasfilename(defaultextension=".md", filetypes=[("Markdown", "*.md")])
        if not p:
            return
        Path(p).write_text(out.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
        log.emit("已保存 " + p, "ok")

    ttk.Button(brow, text="开始识别", command=_do_ocr,
               style="Primary.TButton").pack(side="right")
    ttk.Button(brow, text="保存结果…", command=_do_save).pack(side="right", padx=(0, 8))
