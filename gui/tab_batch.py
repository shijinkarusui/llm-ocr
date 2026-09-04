"""Tab 2: full-book batch OCR with progress + live counters."""
from __future__ import annotations

import json
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import filedialog, ttk

from gui_core import State, Runner, LogBus, card, row, INDIGO, PINE


def _summarize_usage(usage_path: Path) -> dict:
    rows = []
    if usage_path.is_file():
        for line in usage_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
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
    return {"records": len(rows), "counter": dict(c), "pages": len(pages), "tokens": toks, "p50": p50, "p95": p95}


def build(notebook: ttk.Notebook, state: State, runner: Runner, log: LogBus) -> None:
    page = ttk.Frame(notebook, padding=(2, 6, 2, 8))
    notebook.add(page, text="3 · 批量")

    c = card(page, "批量任务",
             "断点续跑：已成功的页自动跳过（看 usage.jsonl）。先 Dry-Run 看页数，再开始。")
    pdf_e = ttk.Entry(c)
    out_e = ttk.Entry(c)
    out_e.insert(0, "out/book_gui")
    row(c, "PDF 文件", pdf_e)
    row(c, "输出目录", out_e)
    prow = ttk.Frame(c, style="Panel.TFrame")
    prow.pack(fill="x", pady=3)
    ttk.Label(prow, text="页码范围", style="Field.TLabel", width=13).pack(side="left")
    s_s = ttk.Spinbox(prow, from_=0, to=100000, width=8)
    s_s.set("0")
    s_s.pack(side="left", padx=(10, 0))
    ttk.Label(prow, text="到", style="Panel.TLabel").pack(side="left", padx=(8, 8))
    e_s = ttk.Spinbox(prow, from_=0, to=100000, width=8)
    e_s.set("")
    e_s.pack(side="left")
    ttk.Label(prow, text="结束留空=到尾页", style="PanelHint.TLabel").pack(side="left", padx=(10, 0))
    brow = ttk.Frame(c, style="Panel.TFrame")
    brow.pack(fill="x", pady=(10, 0))
    ttk.Button(brow, text="选 PDF…",
               command=lambda: _pick(pdf_e, [("PDF", "*.pdf")])).pack(side="left")
    ttk.Button(brow, text="选输出目录…",
               command=lambda: _pickdir(out_e)).pack(side="left", padx=(8, 0))


    p = card(page, "进度",
             "进度条按“已成功页 / 计划页”推进；数字行读 usage.jsonl 汇总。")
    stat = ttk.Label(p, text="成功 0 页 · tokens 0 · skipped 0 · failed 0",
                     style="Panel.TLabel")
    stat.pack(anchor="w")
    bar = ttk.Progressbar(p, mode="determinate", style="Horizontal.TProgressbar")
    bar.pack(fill="x", pady=(8, 2))
    bar.configure(maximum=100, value=0)
    ttk.Label(p, text="跑的过程中点“刷新进度”可看实时数字；中断后重跑自动续上。",
              style="PanelHint.TLabel").pack(anchor="w", pady=(4, 0))

    def _pick(entry: ttk.Entry, types) -> None:
        p2 = filedialog.askopenfilename(filetypes=list(types))
        if p2:
            entry.delete(0, "end")
            entry.insert(0, p2)

    def _pickdir(entry: ttk.Entry) -> None:
        p2 = filedialog.askdirectory()
        if p2:
            entry.delete(0, "end")
            entry.insert(0, p2)

    def _refresh(outdir: str, total: int) -> None:
        s = _summarize_usage(Path(outdir) / "usage.jsonl")
        stat.configure(text="成功 %d 页 · tokens %d · skipped %d · failed %d · p50 %dms p95 %dms" % (
            s["pages"], s["tokens"], s["counter"].get("skipped", 0),
            s["counter"].get("failed", 0), s["p50"], s["p95"]))
        if total > 0:
            bar.configure(maximum=total, value=min(s["pages"], total))

    def _do_dry() -> None:
        from batch_plan import page_plan
        import fitz
        pdf = pdf_e.get().strip()
        if not pdf or not Path(pdf).is_file():
            log.emit("请选择有效 PDF", "err")
            return
        plan = page_plan(Path(pdf), Path(out_e.get().strip() or "out/book_gui"),
                         int(s_s.get() or 0), int(e_s.get()) if (e_s.get() or "").strip() else None)
        with fitz.open(str(pdf)) as doc:
            total = doc.page_count
        _refresh(out_e.get().strip(), len(plan))
        log.emit("dry-run: 全书 %d 页，本次计划 %d 页 endpoint=%s" % (total, len(plan), state.endpoint), "ok")

    def _do_run() -> None:
        pdf = pdf_e.get().strip()
        outdir = out_e.get().strip() or "out/book_gui"
        if not pdf or not Path(pdf).is_file():
            log.emit("请选择有效 PDF", "err")
            return
        try:
            from gui_core import ensure_prompt
            ensure_prompt()
        except Exception:
            pass
        try:
            extra = state.extra()
        except ValueError as exc:
            log.emit("extra-json 非法：" + str(exc), "err")
            return
        kw = dict(dpi=state.dpi, concurrency=state.concurrency, retries=state.retries,
                  start=int(s_s.get() or 0),
                  end=int(e_s.get()) if (e_s.get() or "").strip() else None,
                  base_url=state.base_url, model=state.model, api_key=state.key,
                  endpoint=state.endpoint, detail=state.detail, extra=extra, timeout=state.timeout)
        def _fn():
            from batch_plan import page_plan, run_batch
            plan = page_plan(Path(pdf), Path(outdir), kw["start"], kw["end"])
            total = len(plan)
            summary = run_batch(pdf, outdir, **kw)
            summary["_total"] = total
            return summary
        def _done(summary, exc) -> None:
            total = (summary or {}).get("_total", 0) if isinstance(summary, dict) else 0
            _refresh(outdir, total)
            if exc is None and isinstance(summary, dict):
                log.emit("batch 完成 tokens=%s failed=%s" % (
                    summary.get("total_tokens", 0), summary.get("failed_pages_this_run", 0)), "ok")
        runner.run("批量 OCR（断点续跑）", _fn, _done)

    def _do_poll() -> None:
        _refresh(out_e.get().strip() or "out/book_gui", int(bar.cget("maximum") or 0))

    ttk.Button(brow, text="规划页数",
               command=_do_dry).pack(side="left", padx=(8, 0))
    ttk.Button(brow, text="开始批量", command=_do_run,
               style="Primary.TButton").pack(side="right")
    ttk.Button(brow, text="刷新进度",
               command=_do_poll).pack(side="right", padx=(0, 8))
