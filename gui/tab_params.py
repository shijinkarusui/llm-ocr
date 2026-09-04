"""Tab 4: run parameters + notation check + prompt preview."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from gui_core import State, Runner, LogBus, card, row, make_output, show, FONT_MONO


def build(notebook: ttk.Notebook, state: State, runner: Runner, log: LogBus) -> None:
    page = ttk.Frame(notebook, padding=(2, 6, 2, 8))
    notebook.add(page, text="5 · 参数")

    c = card(page, "运行参数", "这里改的是同一份会话参数，各页共用。")
    ep_v = tk.StringVar(value=state.endpoint)
    dt_v = tk.StringVar(value=state.detail)
    ep_bar = ttk.Frame(c, style="Panel.TFrame")
    ep_bar.pack(fill="x", pady=3)
    ttk.Label(ep_bar, text="请求链路", style="Field.TLabel", width=13).pack(side="left")
    for label, value in (("responses", "responses"), ("chat", "chat"), ("auto", "auto")):
        ttk.Radiobutton(ep_bar, text=label, value=value, variable=ep_v).pack(side="left", padx=(0, 12))
    dt_bar = ttk.Frame(c, style="Panel.TFrame")
    dt_bar.pack(fill="x", pady=3)
    ttk.Label(dt_bar, text="图片清晰度", style="Field.TLabel", width=13).pack(side="left")
    for label, value in (("high", "high"), ("low", "low"), ("auto", "auto")):
        ttk.Radiobutton(dt_bar, text=label, value=value, variable=dt_v).pack(side="left", padx=(0, 12))

    sliders = ttk.Frame(c, style="Panel.TFrame")
    sliders.pack(fill="x", pady=(6, 2))
    conc_v = tk.IntVar(value=state.concurrency)
    to_v = tk.IntVar(value=state.timeout)
    dpi_v = tk.IntVar(value=state.dpi)
    ret_v = tk.IntVar(value=state.retries)
    for label, var, lo, hi, hint in (("并发", conc_v, 1, 20, "默认 10"),
                                     ("超时秒", to_v, 30, 300, "默认 120"),
                                     ("DPI", dpi_v, 100, 300, "默认 200"),
                                     ("重试", ret_v, 0, 5, "")):
        line = ttk.Frame(sliders, style="Panel.TFrame")
        line.pack(fill="x", pady=3)
        ttk.Label(line, text=label, style="Field.TLabel", width=13).pack(side="left")
        ttk.Label(line, textvariable=var, style="Panel.TLabel", width=4).pack(side="left")
        ttk.Scale(line, from_=lo, to=hi, variable=var, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=(10, 10))
        ttk.Label(line, text=hint, style="PanelHint.TLabel", width=9).pack(side="left")
    xrow = ttk.Frame(c, style="Panel.TFrame")
    xrow.pack(fill="x", pady=(8, 0))
    ttk.Label(xrow, text="透传 JSON", style="Field.TLabel", width=13).pack(side="left", anchor="n")
    xt = tk.Text(xrow, height=4, bg="#FFFFFF", fg="#1C2733",
                 insertbackground="#1C2733", font=FONT_MONO, relief="flat",
                 highlightthickness=1, highlightbackground="#D5DCE6",
                 highlightcolor="#2F4B9B", wrap="word")
    xt.insert("end", state.extra_text)
    xt.pack(side="left", fill="x", expand=True, padx=(10, 0))

    warn_l = ttk.Label(c, text="", style="PanelHint.TLabel")
    warn_l.pack(anchor="w", pady=(8, 0))

    brow = ttk.Frame(c, style="Panel.TFrame")
    brow.pack(fill="x", pady=(10, 0))


    k = card(page, "标号校验 / 提示词", "校验读 pages/page_*.md；提示词是 prompts/ocr_system.md 原文。")
    out = make_output(k, height=8, font=FONT_MONO)
    out.pack(fill="both", expand=True)
    out.configure(state="normal")
    out.insert("end", "还没有输出。“看提示词”预览系统提示词，“标号校验”检查公式标号。", "ph")
    out.configure(state="disabled")

    def _apply() -> None:
        state.endpoint = ep_v.get()
        state.detail = dt_v.get()
        state.concurrency = int(conc_v.get())
        state.timeout = int(to_v.get())
        state.dpi = int(dpi_v.get())
        state.retries = int(ret_v.get())
        state.extra_text = xt.get("1.0", "end").strip()
        try:
            state.extra()
            msg = "参数已应用"
        except ValueError as exc:
            msg = "透传 JSON 非法：" + str(exc)
        w = "并发大于 8 后吞吐未必再涨，注意 p95 与 429。" if state.concurrency > 8 else ""
        warn_l.configure(text=("注意：" + w + " " + msg).strip() if w else msg)
        log.emit("参数应用 endpoint=%s concurrency=%d timeout=%d dpi=%d retries=%d extra=%s" % (
            state.endpoint, state.concurrency, state.timeout, state.dpi, state.retries,
            state.extra_text[:120]), "muted")

    def _do_checkdir() -> None:
        d = filedialog.askdirectory()
        if not d:
            return
        def _fn():
            from collections import Counter
            from check_notation import check_file
            pages = sorted(Path(d).glob("pages/page_*.md"))
            pages = pages or sorted(Path(d).glob("page_*.md"))
            codes: Counter = Counter()
            worst = []
            total = 0
            for p in pages:
                iss = check_file(p)
                total += len(iss)
                for i in iss:
                    codes[i["code"]] += 1
                if iss:
                    worst.append((len(iss), p.name))
            worst.sort(reverse=True)
            return {"files": len(pages), "total": total, "codes": dict(codes.most_common(10)), "worst": worst[:10]}
        def _done(r2, exc) -> None:
            if exc is not None or not isinstance(r2, dict):
                show(out, "校验失败，详情见底部运行日志。")
                return
            lines = ["文件：%d  问题：%d" % (r2["files"], r2["total"]),
                     "高频：%s" % r2["codes"], "问题最多的页："]
            lines += ["  %s 有 %d 处" % (n, k2) for k2, n in r2["worst"]]
            show(out, "\n".join(lines))
        runner.run("全书标号校验", _fn, _done)

    def _do_prompt() -> None:
        from gui_core import resource_root
        p = resource_root() / "prompts" / "ocr_system.md"
        show(out, p.read_text(encoding="utf-8")[:3000] if p.is_file() else "prompts/ocr_system.md 缺失")

    ttk.Button(brow, text="应用参数", command=_apply,
               style="Primary.TButton").pack(side="left")
    ttk.Button(brow, text="看提示词", command=_do_prompt).pack(side="left", padx=(8, 0))
    ttk.Button(brow, text="标号校验…", command=_do_checkdir).pack(side="left", padx=(8, 0))
