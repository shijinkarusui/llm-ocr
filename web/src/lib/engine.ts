/** Strongly-typed client for serve.py (S2 contract). No hand-written URL typos: all paths here. */

const DIRECT = "http://127.0.0.1:21139";
// Browser dev (http) goes through the Vite /api proxy; Tauri talks direct.
export const ENGINE_BASE =
  typeof window !== "undefined" && window.location.protocol.startsWith("http") ? "" : DIRECT;

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
  const r = await fetch(`${ENGINE_BASE}${path}`, { ...init, headers });
  const data = (await r.json()) as T & { error?: string };
  if (!r.ok) throw new Error(data.error ?? `HTTP ${r.status}`);
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
export function probe(p: LlmParams): Promise<ProbeRes> {
  return req<ProbeRes>("/api/probe", {
    method: "POST",
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout,
    }),
  });
}

export interface OcrRes { markdown: string; png_b64_preview?: string }

/** Only send a prompt when the user actually customised one: an empty editor
 *  must fall back to the server's prompts/ocr_system.md, not blank it out. */
function promptField(prompt?: string): Record<string, string> {
  const v = (prompt ?? "").trim();
  return v ? { prompt: v } : {};
}

export function ocrImage(p: LlmParams, pngB64: string, prompt?: string): Promise<OcrRes> {
  return req<OcrRes>("/api/ocr/image", {
    method: "POST",
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout, png_b64: pngB64,
      ...promptField(prompt),
    }),
  });
}

export function ocrUrl(p: LlmParams, imageUrl: string, prompt?: string): Promise<OcrRes> {
  return req<OcrRes>("/api/ocr/url", {
    method: "POST",
    body: JSON.stringify({
      base_url: p.baseUrl, model: p.model, key: p.key,
      endpoint: p.endpoint, detail: p.detail, timeout: p.timeout, image_url: imageUrl,
      ...promptField(prompt),
    }),
  });
}

export function ocrPdfPage(
  p: LlmParams, pdfPath: string, pno: number, dpi: number, prompt?: string,
): Promise<OcrRes> {
  return req<OcrRes>("/api/ocr/pdf-page", {
    method: "POST",
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
}
export interface JobRes {
  id: string; kind: string; label: string; status: string;
  progress?: JobProgress; summary?: Record<string, unknown>;
  result?: Record<string, unknown>; error?: string;
}
export function job(id: string): Promise<JobRes> {
  return req<JobRes>(`/api/jobs/${id}`);
}

export function jobCancel(id: string): Promise<{ ok: boolean }> {
  return req<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: "POST", body: "{}" });
}

export interface SearchableRunRes { job_id: string }
export function searchableBuild(
  pdfPath: string, outdir: string, geoSource: string, keywords: string[],
): Promise<SearchableRunRes> {
  return req<SearchableRunRes>("/api/searchable/build", {
    method: "POST",
    body: JSON.stringify({ pdf_path: pdfPath, outdir, geo_source: geoSource, keywords }),
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
