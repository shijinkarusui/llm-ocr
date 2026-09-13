import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function maskedKey(key: string): string {
  if (!key) return "(empty)";
  return `sk-**** len=${key.length}`;
}

export function parseExtra(text: string): Record<string, unknown> {
  const raw = (text ?? "").trim();
  if (!raw) return {};
  const data: unknown = JSON.parse(raw);
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new Error("透传 JSON 顶层必须是对象");
  }
  return data as Record<string, unknown>;
}
