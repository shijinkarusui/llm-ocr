"""Tab 3: searchable dual-layer PDF build + verification."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from gui_core import State, Runner, LogBus, card, row, make_output, show, FONT_OUT


def build(notebook: ttk.Notebook, state: State, runner: Runner, log: LogBus) -> None:
    page = ttk.Frame(notebook, padding=(2, 6, 2, 8))
    notebook.add(page, text="4 · 双层")

    c = card(page, "双层可搜索 PDF",
             "把原 PDF 的每一页都垫上 OCR 文字层，输出 book_searchable.pdf。")
    pdf_e = ttk.Entry(c)
    dir_e = ttk.Entry(c)
    kw_e = ttk.Entry(c)
    kw_e.insert(0, "北京话,同化,韵母")
    row(c, "原 PDF", pdf_e)
    row(c, "OCR 输出目录", dir_e)
    row(c, "验证关键词", kw_e)
    ttk.Label(c, text="输出目录指批量页含 pages/*.md 的目录；关键词用中文逗号分隔。",
              style="PanelHint.TLabel").pack(anchor="w", pady=(6, 0))
    brow = ttk.Frame(c, style="Panel.TFrame")
    brow.pack(fill="x", pady=(10, 0))
    ttk.Button(brow, text="选原 PDF…",
               command=lambda: _pick(pdf_e, [("PDF", "*.pdf")])).pack(side="left")
    ttk.Button(brow, text="选输出目录…",
               command=lambda: _pickdir(dir_e)).pack(side="left", padx=(8, 0))


    r = card(page, "构建结果", "输出路径、页数、可复制字数与关键词命中页。")
    out = make_output(r, height=12, font=FONT_OUT)
    out.pack(fill="both", expand=True)
    out.configure(state="normal")
    out.insert("end", "还没有构建。选好原 PDF 与输出目录后点“构建并验证”。", "ph")
    out.configure(state="disabled")

    def _pick(entry: ttk.Entry, types) -> None:
        p = filedialog.askopenfilename(filetypes=list(types))
        if p:
            entry.delete(0, "end")
            entry.insert(0, p)

    def _pickdir(entry: ttk.Entry) -> None:
        p = filedialog.askdirectory()
        if p:
            entry.delete(0, "end")
            entry.insert(0, p)

    def _do_build() -> None:
        pdf = pdf_e.get().strip()
        d = dir_e.get().strip()
        if not pdf or not Path(pdf).is_file():
            log.emit("请选择有效原 PDF", "err")
            return
        if not d or not Path(d).is_dir():
            log.emit("请选择有效 OCR 输出目录（含 pages/*.md）", "err")
            return
        kws = [k.strip() for k in kw_e.get().replace("，", ",").split(",") if k.strip()]
        def _fn():
            from make_searchable import make_searchable
            from verify_searchable import verify
            import fitz
            root = Path(d)
            pages = sorted(root.glob("pages/page_*.md"), key=lambda p: int(p.stem.split("_")[1]))
            md = {int(p.stem.split("_")[1]): p.read_text(encoding="utf-8") for p in pages}
            with fitz.open(str(pdf)) as doc:
                total = doc.page_count
            target = root / "book_searchable.pdf"
            make_searchable(pdf, list(range(total)), md, None, target)
            res = verify(target, kws) if kws else {}
            return {"pdf": str(target), "bytes": target.stat().st_size, "pages": total,
                    "copyable": (res or {}).get("copyable_chars", "?"),
                    "hit_pages": len((res or {}).get("hit_pages", []))}
        def _done(r2, exc) -> None:
            if exc is not None or not isinstance(r2, dict):
                show(out, "构建失败，详情见底部运行日志。")
                return
            show(out, "输出：%s\n大小：%s 字节  页数：%s\n可复制字数：%s  命中页数：%s\n关键词：%s" % (
                r2["pdf"], r2["bytes"], r2["pages"], r2["copyable"], r2["hit_pages"], kw_e.get().strip()))
        runner.run("构建双层 PDF", _fn, _done)

    ttk.Button(brow, text="构建并验证", command=_do_build,
               style="Primary.TButton").pack(side="right")
