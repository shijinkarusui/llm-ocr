/** Native file dialogs in Tauri, <input type=file> fallback in browser dev. */

export function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return window.location.protocol === "tauri:" || window.location.protocol === "asset:";
}

export async function pickFile(filters?: { name: string; extensions: string[] }[]): Promise<string | null> {
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const sel = await open({ multiple: false, filters });
    return typeof sel === "string" ? sel : null;
  }
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    if (filters?.some((f) => f.extensions.includes("pdf"))) input.accept = ".pdf";
    else if (filters) input.accept = "image/*";
    input.onchange = () => {
      const f = input.files?.[0];
      // Browser cannot expose a real path; bridge reads server-side files,
      // so in browser dev the user must type/paste the path manually.
      resolve(f ? f.name : null);
    };
    input.oncancel = () => resolve(null);
    input.click();
  });
}

export async function pickDir(): Promise<string | null> {
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const sel = await open({ multiple: false, directory: true });
    return typeof sel === "string" ? sel : null;
  }
  const v = window.prompt("输出目录（服务端路径，如 out/book_gui）：", "out/book_gui");
  return v && v.trim() ? v.trim() : null;
}

export const PDF_FILTER = [{ name: "PDF", extensions: ["pdf"] }];
export const IMAGE_FILTER = [{ name: "images", extensions: ["png", "jpg", "jpeg", "webp"] }];
