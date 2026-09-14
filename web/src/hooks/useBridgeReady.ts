import { useEffect, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { health } from "@/lib/engine";
import { useLog } from "@/stores/log";

/** Failure reports already printed. The shell replays the event because the
 *  sidecar can fail before this listener is attached. */
const seenShellErrors = new Set<number>();

/** Wait for the serve.py sidecar on boot; retry ~15s before warning. */
export function useBridgeReady(): boolean {
  const emit = useLog((s) => s.emit);
  const [ready, setReady] = useState(false);
  const warned = useRef(false);

  /* The shell pushes *why* the sidecar is down (exit code, output tail, log file
   * path) on "shell-error". Show it in 运行日志 — the timeout message below is
   * all the user used to get, and it names no cause. */
  useEffect(() => {
    if (typeof window === "undefined" || !("__TAURI_INTERNALS__" in window)) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;
    void listen<{ id: number; message: string; detail: string }>("shell-error", (e) => {
      const p = e.payload;
      if (!p || seenShellErrors.has(p.id)) return;
      seenShellErrors.add(p.id);
      emit(`引擎异常：${p.message}`, "err");
      for (const line of String(p.detail ?? "").split("\n")) {
        if (line.trim()) emit(line, "err");
      }
    })
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch(() => {
        /* browser dev: no Tauri event bus to listen on */
      });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [emit]);

  useEffect(() => {
    let cancelled = false;
    let tries = 0;
    async function tick() {
      tries += 1;
      try {
        const h = await health();
        if (cancelled) return;
        if (h.ok) {
          setReady(true);
          emit("bridge ready (" + h.version + ")", "ok");
          return;
        }
      } catch {
        // not up yet
      }
      if (cancelled) return;
      if (tries >= 30) {
        if (!warned.current) {
          warned.current = true;
          emit("bridge not ready after 15s: check sidecar on 127.0.0.1:21139", "err");
        }
        window.setTimeout(() => {
          if (!cancelled) {
            tries = 0;
            warned.current = false;
            void tick();
          }
        }, 10000);
        return;
      }
      window.setTimeout(() => {
        if (!cancelled) void tick();
      }, 500);
    }
    void tick();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return ready;
}
