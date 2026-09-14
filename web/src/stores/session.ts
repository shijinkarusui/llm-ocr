import { create } from "zustand";

export type Endpoint = "responses" | "chat" | "auto";
export type Detail = "high" | "low" | "auto";
export type ViewId = "connect" | "single" | "batch" | "searchable" | "params";
export type ThemeMode = "system" | "light" | "dark";

const THEME_KEY = "llm-ocr:theme";

function prefersDark(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

function readThemeMode(): ThemeMode {
  try {
    const raw = localStorage.getItem(THEME_KEY);
    if (raw === "light" || raw === "dark" || raw === "system") return raw;
  } catch {
    /* 隐私模式 / 无 localStorage 时退回跟随系统 */
  }
  return "system";
}

const initialThemeMode: ThemeMode = readThemeMode();
const initialDark: boolean =
  initialThemeMode === "dark" ||
  (initialThemeMode === "system" && prefersDark());

export const DEFAULT_EXTRA =
  `{"max_output_tokens":100000,"reasoning":{"effort":"medium"},` +
  `"temperature":0.5,"frequency_penalty":0.5,"presence_penalty":0.5}`;

interface SessionState {
  baseUrl: string;
  apiKey: string;
  model: string;
  endpoint: Endpoint;
  detail: Detail;
  concurrency: number;
  timeout: number;
  dpi: number;
  retries: number;
  extraText: string;
  ocrPrompt: string;
  view: ViewId;
  themeMode: ThemeMode;
  dark: boolean;
  set: (p: Partial<SessionState>) => void;
}

export const useSession = create<SessionState>((set) => ({
  baseUrl: "http://127.0.0.1:2113/v1",
  apiKey: "",
  model: "OC/muse-spark-1.3-contributor-free",
  endpoint: "responses",
  detail: "high",
  concurrency: 10,
  timeout: 120,
  dpi: 200,
  retries: 2,
  extraText: DEFAULT_EXTRA,
  ocrPrompt: "",
  view: "connect",
  themeMode: initialThemeMode,
  dark: initialDark,
  set: (p) =>
    set(() => {
      if (p.themeMode === undefined) return p;
      try {
        localStorage.setItem(THEME_KEY, p.themeMode);
      } catch {
        /* 隐私模式等场景忽略 */
      }
      const dark =
        p.themeMode === "dark" ||
        (p.themeMode === "system" && prefersDark());
      return { ...p, dark };
    }),
}));
