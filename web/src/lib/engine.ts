/** Strongly-typed client for serve.py (S2 contract). No hand-written URL typos: all paths here. */

const DIRECT = "http://127.0.0.1:21139";
// Browser dev (http) goes through the Vite /api proxy; Tauri talks direct.
// Tauri v2 on Windows serves the app from http://tauri.localhost, whose protocol is
// also "http:" — so the protocol alone cannot separate dev from production. Detect the
// Tauri runtime explicitly instead.
const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
export const ENGINE_BASE = isTauri ? DIRECT : "";

export interface LlmParams {
  baseUrl: string;
  model: string;
  key: string;
  endpoint: string;
  detail: string;
  timeout: number;
}

async function req<T>(path: string, init?: RequestInit, apiKey?: string): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (init?.headers) Object.assign(headers, init.headers);
  if (apiKey) headers["X-LLM-Key"] = apiKey;
  const url = `${ENGINE_BASE}${path}`;
  const r = await fetch(url, { ...init, headers });
  // Read text first: when ENGINE_BASE points somewhere that is not serve.py (wrong
  // port, a proxy, an HTML error page) a bare r.json() throws "SyntaxError:
  // Unexpected token '<'", which hides the real problem. Decode defensively and
  // report what actually came back.
  const text = await r.text();
  let data: T & { error?: string; error_code?: string; hint_cn?: string; next_action?: string };
  try {
    data = JSON.parse(text) as T & { error?: string; error_code?: string; hint_cn?: string; next_action?: string };
  } catch {
    throw new Error(
      `响应不是 JSON: HTTP ${r.status}${r.statusText ? " " + r.statusText : ""} ${url} ` +
        `content-type=${r.headers.get("content-type") ?? "(none)"} ` +
        `body=${JSON.stringify(text.slice(0, 200))}`,
    );
  }
  if (!r.ok) {
    const env = data as { error?: string; error_code?: string; hint_cn?: string; next_action?: string };
    const msg = env.hint_cn || env.error || `HTTP ${r.status}`;
    const err = new Error(
      env.error_code ? JSON.stringify({ error_code: env.error_code, hint_cn: env.hint_cn, error: env.error }) + ` ${msg}` : msg,
    );
    (err as unknown as Record<string, unknown>).code = env.error_code;
    (err as unknown as Record<string, unknown>).envelope = env;
    throw err;
  }
  return data;
}

export interface Health { ok: boolean; version: string; engine: string }
export function health(): Promise<Health> {
  return req<Health>("/api/health");
}

export interface ModelsRes { models: string[]; count: number }
export function listModels(baseUrl: string, apiKey: string): Promise<ModelsRes> {
  return req<ModelsRes>(`/api/models?base_url=${encodeURIComponent(baseUrl)}`, undefined, apiKey);
}

export interface ProbeRes {
  endpoint_used: string;
  text: string;
  usage: Record<string, unknown>;
  elapsed_ms: number;
}
export function probe(p: LlmParams, signal?: AbortSignal): Promise<ProbeRes> {
  return req<ProbeRes>("/api/probe", {
    method: "POST",
    signal,
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout,
    }),
  });
}

export interface OcrRes {
  markdown: string;
  png_b64_preview?: string;
  elapsed_ms?: number;
  dpi_actual?: number;
  page_count?: number;
}

/** Only send a prompt when the user actually customised one: an empty editor
 *  must fall back to the server's prompts/ocr_system.md, not blank it out. */
function promptField(prompt?: string): Record<string, string> {
  const v = (prompt ?? "").trim();
  return v ? { prompt: v } : {};
}

export function ocrImage(p: LlmParams, pngB64: string, prompt?: string, signal?: AbortSignal): Promise<OcrRes> {
  return req<OcrRes>("/api/ocr/image", {
    method: "POST",
    signal,
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout, png_b64: pngB64,
      ...promptField(prompt),
    }),
  });
}

export function ocrUrl(p: LlmParams, imageUrl: string, prompt?: string, signal?: AbortSignal): Promise<OcrRes> {
  return req<OcrRes>("/api/ocr/url", {
    method: "POST",
    signal,
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout, image_url: imageUrl,
      ...promptField(prompt),
    }),
  });
}

export function ocrPdfPage(
  p: LlmParams, pdfPath: string, pno: number, dpi: number, prompt?: string, signal?: AbortSignal,
): Promise<OcrRes> {
  return req<OcrRes>("/api/ocr/pdf-page", {
    method: "POST",
    signal,
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout,
      pdf_path: pdfPath, pno, dpi,
      ...promptField(prompt),
    }),
  });
}

export interface DryRunRes {
  total_pages: number;
  planned_pages: number;
  first5: { pno_0based: number; page_number: number }[];
  book_dir?: string;
  done_pages?: number;
  remaining?: number;
  book_json?: string | null;
}

export function batchDryRun(pdfPath: string, outdir: string, start: number, end?: number): Promise<DryRunRes> {
  return req<DryRunRes>("/api/batch/dry-run", {
    method: "POST",
    body: JSON.stringify({ pdf_path: pdfPath, outdir, start, end: end ?? null }),
  });
}

export interface BatchRunRes { job_id: string }
export function batchRun(
  p: LlmParams, pdfPath: string, outdir: string, start: number, end: number | undefined,
  dpi: number, concurrency: number, retries: number, extra: Record<string, unknown>,
  prompt?: string,
): Promise<BatchRunRes> {
  return req<BatchRunRes>("/api/batch/run", {
    method: "POST",
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout,
      pdf_path: pdfPath, outdir, start, end: end ?? null,
      dpi, concurrency, retries, extra,
      ...promptField(prompt),
    }),
  });
}

export interface JobProgress {
  records: number; pages_ok: number; tokens: number;
  skipped: number; failed: number; p50_ms: number; p95_ms: number;
  // P0 unified contract (all optional: old backend simply omits them).
  planned?: number; pct?: number; eta_ms?: number; pages_per_min?: number;
  elapsed_ms?: number; concurrency_current?: number; concurrency_init?: number;
  n429?: number;
}
export interface JobRes {
  id: string; kind: string; label: string; status: string;
  progress?: JobProgress; summary?: Record<string, unknown>;
  result?: Record<string, unknown>; error?: string;
  error_code?: string; hint_cn?: string; next_action?: string;
  planned?: number; total_pages?: number; started_at?: number;
  cancel_requested_at?: number;
}
export interface JobPageItem { pno_0based: number; page_number?: number; status?: string; error?: string; http_status?: number | null; attempt?: number; duration_ms?: number }
export function jobPages(id: string, status = "failed", limit = 200, offset = 0): Promise<{ pages: JobPageItem[]; total: number }> {
  return req<{ pages: JobPageItem[]; total: number }>(
    `/api/jobs/${encodeURIComponent(id)}/pages?status=${encodeURIComponent(status)}&limit=${limit}&offset=${offset}`,
  );
}
export function job(id: string): Promise<JobRes> {
  return req<JobRes>(`/api/jobs/${id}`);
}

export function jobCancel(id: string): Promise<{ ok: boolean }> {
  return req<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: "POST", body: "{}" });
}

export interface SearchableRunRes {
  job_id: string;
  status?: BookBindStatus;
  book_dir?: string | null;
  source_pdf?: string | null;
  warnings?: string[];
}
export function searchableBuild(
  pdfPath: string, outdir: string, geoSource: string, keywords: string[],
): Promise<SearchableRunRes> {
  return req<SearchableRunRes>("/api/searchable/build", {
    method: "POST",
    body: JSON.stringify({ pdf_path: pdfPath, outdir, geo_source: geoSource, keywords }),
  });
}

/** Which book a folder belongs to, per its book.json (see src/book_id.py). */
export type BookBindStatus = "bound" | "unbound" | "ambiguous" | "not_found" | "mismatch" | "error";
export interface BookCandidate {
  book_dir: string;
  source_name: string;
  source_pdf: string | null;
  page_count: number | null;
  pages_done: number | null;
  updated?: string | null;
}
export interface BookResolveRes {
  status: BookBindStatus;
  book_dir: string | null;
  pages_dir: string | null;
  source_pdf: string | null;
  recorded_pdf: string | null;
  record: Record<string, unknown> | null;
  candidates: BookCandidate[];
  warnings: string[];
  message: string;
}
/** Bind an output dir (book folder / legacy dir / parent dir) to exactly one book. */
export function bookResolve(dir: string, pdfPath?: string): Promise<BookResolveRes> {
  return req<BookResolveRes>("/api/book/resolve", {
    method: "POST",
    body: JSON.stringify({ dir, pdf_path: pdfPath ?? "" }),
  });
}

export interface NotationRes {
  files: number; total: number;
  codes: Record<string, number>; worst: { page: string; issues: number }[];
}
export function notationCheck(dir: string): Promise<NotationRes> {
  return req<NotationRes>("/api/notation/check", {
    method: "POST",
    body: JSON.stringify({ dir }),
  });
}

export function promptText(): Promise<{ prompt: string }> {
  return req<{ prompt: string }>("/api/prompt");
}

export interface FileB64Res { b64: string; bytes: number }
export function fileB64(path: string): Promise<FileB64Res> {
  return req<FileB64Res>("/api/file/b64", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}
