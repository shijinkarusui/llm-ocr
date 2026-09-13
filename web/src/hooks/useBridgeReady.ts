import { useEffect, useRef, useState } from "react";
import { health } from "@/lib/engine";
import { useLog } from "@/stores/log";

/** Wait for the serve.py sidecar on boot; retry ~15s before warning. */
export function useBridgeReady(): boolean {
  const emit = useLog((s) => s.emit);
  const [ready, setReady] = useState(false);
  const warned = useRef(false);
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
