"""Tab 0: gateway connection (models / probe / config check)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui_core import (
    State, Runner, LogBus, card, row, masked_key,
    make_output, show, FONT_OUT, INDIGO, FAINT,
)


def build(notebook: ttk.Notebook, state: State, runner: Runner, log: LogBus) -> None:
    page = ttk.Frame(notebook, padding=(2, 6, 2, 8))
    notebook.add(page, text="1 · 连接")

    c = card(page, "网关连接",
             "Key 只放内存，不写盘、不打明文日志。先同步参数，再拉模型或探活。")
    base_e = ttk.Entry(c)
    base_e.insert(0, state.base_url)
    key_e = ttk.Entry(c, show="\u2022")
    key_e.insert(0, state.key)
    model_c = ttk.Combobox(c, values=[state.model])
    model_c.set(state.model)
    row(c, "网关地址", base_e)
    row(c, "密钥（内存）", key_e)
    row(c, "模型", model_c)
    ttk.Label(c, text="网关固定为 Octopus；模型保持 OC/ 前缀。拉取失败可手填。",
              style="PanelHint.TLabel").pack(anchor="w", pady=(6, 0))

    r = card(page, "返回结果", "拉模型、探活与核验的输出都在这里。")
    out = make_output(r, height=12, font=FONT_OUT)
    out.pack(fill="both", expand=True)
    out.configure(state="normal")
    out.insert("end", "还没有输出。点右上“拉取模型”或“单图探活”。", "ph")
    out.configure(state="disabled")

    def _sync_from_ui() -> None:
        state.base_url = base_e.get().strip() or state.base_url
        state.key = key_e.get().strip()
        state.model = model_c.get().strip() or state.model
        log.emit("连接参数已同步 key=%s" % masked_key(state.key), "muted")

    def _do_models() -> None:
        _sync_from_ui()
        def _fn():
            from llm_client import list_models
            return list_models(base_url=state.base_url, api_key=state.key)
        def _done(models, exc) -> None:
            if exc is not None or not models:
                show(out, "模型列表拉取失败或为空。请手填模型（保持 OC/ 前缀）。")
                return
            ids = [m.get("id") if isinstance(m, dict) else str(m) for m in models]
            model_c.configure(values=ids)
            show(out, "网关：%s\n模型数：%d\n" % (state.base_url, len(ids))
                 + "\n".join("- " + i for i in ids[:40]))
        runner.run("拉取模型列表", _fn, _done)

    def _do_probe() -> None:
        _sync_from_ui()
        def _fn():
            import llm_client as C
            from gui_core import resource_root
            png = (resource_root() / "tests" / "cand_165.png").read_bytes()
            ep = state.endpoint
            kw = dict(base_url=state.base_url, model=state.model, api_key=state.key,
                      detail=state.detail, timeout=state.timeout)
            if ep == "auto":
                return ("auto",) + C.auto_vision(png, "Transcribe this page exactly (OCR only):", **kw)
            if ep == "responses":
                return ("responses",) + C.responses_vision(png, "Transcribe this page exactly (OCR only):", **kw)
            return ("chat",) + C.chat_vision(png, "Transcribe this page exactly (OCR only):", **kw)
        def _done(res, exc) -> None:
            if exc is not None or res is None:
                show(out, "探活失败，详情见底部运行日志。")
                return
            ep, text, usage = res
            leg = (usage or {}).get("_leg", 1)
            norm = (usage or {}).get("_endpoint_normalized", ep)
            show(out, "链路：%s（实际 %s，第 %s 腿）  tokens：%s\n\n%s" % (
                ep, norm, leg, (usage or {}).get("total_tokens", "?"), (text or "")[:1500]))
        runner.run("单图探活 tests/cand_165.png", _fn, _done)

    def _do_check() -> None:
        _sync_from_ui()
        show(out, "网关：%s\n模型：%s\n密钥：%s\n链路：%s  清晰度：%s\n超时：%d 秒  并发：%d" % (
            state.base_url, state.model, masked_key(state.key), state.endpoint,
            state.detail, state.timeout, state.concurrency))

    btns = ttk.Frame(c, style="Panel.TFrame")
    btns.pack(fill="x", pady=(12, 0))
    ttk.Button(btns, text="同步参数", command=_sync_from_ui).pack(side="left")
    ttk.Button(btns, text="拉取模型", command=_do_models,
               style="Primary.TButton").pack(side="left", padx=(8, 0))
    ttk.Button(btns, text="单图探活", command=_do_probe).pack(side="left", padx=(8, 0))
    ttk.Button(btns, text="脱敏核验", command=_do_check).pack(side="left", padx=(8, 0))
