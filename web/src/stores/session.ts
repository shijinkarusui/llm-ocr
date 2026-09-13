import { create } from "zustand";

export type Endpoint = "responses" | "chat" | "auto";
export type Detail = "high" | "low" | "auto";
export type ViewId = "connect" | "single" | "batch" | "searchable" | "params";

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
  view: ViewId;
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
  view: "connect",
  dark: false,
  set: (p) => set(p),
}));
