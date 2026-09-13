import { create } from "zustand";

export type LogKind = "info" | "ok" | "warn" | "err" | "muted";

export interface LogEntry {
  id: number;
  time: string;
  msg: string;
  kind: LogKind;
}

let nextId = 1;

interface LogState {
  entries: LogEntry[];
  emit: (msg: string, kind?: LogKind) => void;
  clear: () => void;
}

function stamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export const useLog = create<LogState>((set) => ({
  entries: [],
  emit: (msg, kind = "info") =>
    set((s) => ({
      entries: [...s.entries.slice(-499), { id: nextId++, time: stamp(), msg, kind }],
    })),
  clear: () => set({ entries: [] }),
}));
