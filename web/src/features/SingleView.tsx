import { useEffect, useRef, useState } from "react";
import { FileText, Save, Copy, XCircle } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { useSession } from "@/stores/session";
import { useLog } from "@/stores/log";
import { ocrImage, ocrPdfPage, ocrUrl, fileB64, pdfPreviewUrl, type OcrRes } from "@/lib/engine";
import { toZh } from "@/lib/errors";
import { pickFile, PDF_FILTER, IMAGE_FILTER } from "@/lib/pick";

type Kind = "image" | "pdf-page" | "image-url";
type Phase = "idle" | "loading" | "done" | "error";

async function readImageB64(path: string): Promise<string> {
  const r = await fileB64(path);
  return r.b64;
}

function stampName(prefix: string, ext: string): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${prefix}-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.${ext}`;
}

function safeName(s: string): string {
  return s.replace(/[\\/:*?"<>|]/g, "_").slice(0, 60) || "page";
}

export function SingleView() {
  const s = useSession();
  const emit = useLog((x) => x.emit);
  const [kind, setKind] = useState<Kind>("image");
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [pno, setPno] = useState(0);
  // P1: user-facing 1-based page input; pno (0-based) stays the wire value.
  const [page1, setPage1] = useState(1);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [clampNote, setClampNote] = useState("");
  const [thumbOk, setThumbOk] = useState(true);
  const [phase, setPhase] = useState<Phase>("idle");
  const [markdown, setMarkdown] = useState("");
  const [preview, setPreview] = useState("");
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [waited, setWaited] = useState(0);
  const [copied, setCopied] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const tickRef = useRef<number | null>(null);

  function stopTick() {
    if (tickRef.current !== null) {
      window.clearInterval(tickRef.current);
      tickRef.current = null;
    }
  }

  useEffect(() => stopTick, []);

  async function chooseFile() {
    const sel = await pickFile(kind === "pdf-page" ? PDF_FILTER : IMAGE_FILTER);
    if (sel) {
      setPath(sel);
      setThumbOk(true);
      setPageCount(null);
      setClampNote("");
    }
  }

  function cancel() {
    abortRef.current?.abort();
  }

  async function run() {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    // Hard cap: server timeout + 15s headroom so a hung socket cannot spin forever.
    const cap = window.setTimeout(() => ctrl.abort(), s.timeout * 1000 + 15000);
    const t0 = Date.now();
    setWaited(0);
    stopTick();
    tickRef.current = window.setInterval(() => setWaited(Math.floor((Date.now() - t0) / 1000)), 500);
    setPhase("loading");
    setError("");
    setPreview("");
    setElapsed(null);
    setCopied(false);
    setClampNote("");
    emit("开始：单页 OCR", "muted");
    try {
      const p = {
        baseUrl: s.baseUrl, model: s.model, key: s.apiKey,
        endpoint: s.endpoint, detail: s.detail, timeout: s.timeout,
      };
      let r: OcrRes;
      if (kind === "image-url") {
        if (!url.trim()) throw new Error("请填写远端 URL");
        r = await ocrUrl(p, url.trim(), s.ocrPrompt, ctrl.signal);
        setMarkdown(r.markdown);
      } else if (kind === "pdf-page") {
        if (!path.trim()) throw new Error("请选择有效 PDF（服务端可读路径）");
        // P1: 1-based clamp up front (backend still validates; its `clamped` wins below).
        let pno0 = Math.max(0, page1 - 1);
        if (pageCount != null) {
          const c1 = Math.min(Math.max(page1, 1), pageCount);
          if (c1 !== page1) {
            setPage1(c1);
            setClampNote(`已钳制到 1..${pageCount}`);
          }
          pno0 = c1 - 1;
          setPno(pno0);
        } else if (pno0 !== pno) {
          setPno(pno0);
        }
        r = await ocrPdfPage(p, path.trim(), pno0, s.dpi, s.ocrPrompt, ctrl.signal);
        setMarkdown(r.markdown);
        if (r.png_b64_preview) setPreview(r.png_b64_preview);
        if (typeof r.page_count === "number" && Number.isFinite(r.page_count) && r.page_count > 0) {
          setPageCount(r.page_count);
          const bPno = typeof r.pno === "number" ? r.pno : (typeof r.page_number === "number" ? r.page_number - 1 : null);
          if (r.clamped || (bPno !== null && bPno !== pno0)) {
            const b1 = (bPno ?? pno0) + 1;
            setPage1(b1);
            setPno(bPno ?? pno0);
            setClampNote(`后端已钳制到第 ${b1} 页（共 ${r.page_count} 页）`);
          }
        }
      } else {
        if (!path.trim()) throw new Error("请选择有效图片");
        const b64 = await readImageB64(path.trim());
        if (ctrl.signal.aborted) return;
        r = await ocrImage(p, b64, s.ocrPrompt, ctrl.signal);
        setMarkdown(r.markdown);
      }
      if (!r!.markdown.trim()) {
        throw new Error(JSON.stringify({ error_code: "EMPTY_OUTPUT" }) + " 模型返回空：调大 max_output_tokens 或把 reasoning 降到 low 后重试。");
      }
      setElapsed(r!.elapsed_ms ?? Date.now() - t0);
      setPhase("done");
      emit(`单页 OCR 完成${r!.elapsed_ms != null ? `（${r!.elapsed_ms}ms）` : ""}`, "ok");
    } catch (e) {
      if ((e instanceof DOMException && e.name === "AbortError") || ctrl.signal.aborted) {
        setPhase("idle");
        emit("单页 OCR 已取消", "warn");
        return;
      }
      setPhase("error");
      const zh = toZh(e);
      setError(zh);
      emit(`单页 OCR 失败：${e instanceof Error ? e.message : e}`, "err");
    } finally {
      window.clearTimeout(cap);
      stopTick();
      if (abortRef.current === ctrl) abortRef.current = null;
    }
  }

  function save() {
    const src = kind === "image-url" ? safeName(url.trim().split("/").pop() || "url") : safeName((path.trim().split(/[\\/]/).pop() || "page").replace(/\.[^.]+$/, ""));
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = stampName(`ocr-${src}${kind === "pdf-page" ? `-p${pno}` : ""}`, "md");
    a.click();
    URL.revokeObjectURL(a.href);
    emit(`已保存 ${a.download}`, "ok");
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopied(true);
      emit("识别结果已复制", "ok");
    } catch (e) {
      emit(`复制失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>输入</CardTitle>
          <CardDescription>图片直传，PDF 按页渲染，远端 URL 由网关拉取。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <fieldset className="flex items-center gap-4">
            <legend className="w-24 shrink-0 text-[13px] leading-8 text-muted-foreground">来源</legend>
            {([["image", "图片"], ["pdf-page", "PDF 单页"], ["image-url", "图片链接"]] as [Kind, string][]).map(
              ([v, label]) => (
                <label key={v} className="flex items-center gap-1.5 text-[13px]">
                  <input
                    type="radio"
                    name="single-kind"
                    value={v}
                    checked={kind === v}
                    onChange={() => setKind(v)}
                    className="size-3.5 accent-[var(--primary)]"
                  />
                  {label}
                </label>
              ),
            )}
          </fieldset>
          {kind !== "image-url" ? (
            <div className="flex items-center gap-2.5">
              <Label htmlFor="single-path">本地路径</Label>
              <Input
                id="single-path"
                value={path}
                onChange={(e) => {
                  setPath(e.target.value);
                  setThumbOk(true);
                  setPageCount(null);
                  setClampNote("");
                }}
                placeholder="服务端可读路径"
              />
              <Button variant="outline" size="sm" onClick={chooseFile}>
                {kind === "pdf-page" ? "选 PDF…" : "选图片…"}
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2.5">
              <Label htmlFor="single-url">远端 URL</Label>
              <Input id="single-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
            </div>
          )}
          {kind === "pdf-page" && (
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center gap-2.5">
                <Label htmlFor="single-pno">PDF 页码</Label>
                <Input
                  id="single-pno"
                  className="w-24"
                  inputMode="numeric"
                  value={String(page1)}
                  onChange={(e) => {
                    const raw = Number(e.target.value) || 1;
                    if (pageCount != null) {
                      const c1 = Math.min(Math.max(raw, 1), pageCount);
                      setPage1(c1);
                      setPno(c1 - 1);
                      setClampNote(c1 !== raw ? `已钳制到 1..${pageCount}` : "");
                    } else {
                      const c1 = Math.max(1, raw);
                      setPage1(c1);
                      setPno(c1 - 1);
                      setClampNote(c1 !== raw ? "已钳制到 1..N（总页数未知，先按 1 起算）" : "");
                    }
                    setThumbOk(true);
                  }}
                />
                <span className="text-xs text-muted-foreground">
                  第 1 页起{pageCount != null ? `，共 ${pageCount} 页` : "（识别后可知总页数）"}
                </span>
              </div>
              {clampNote && (
                <p role="status" className="text-xs leading-5 text-warn">{clampNote}</p>
              )}
              {path.trim() && thumbOk && (
                <img
                  src={pdfPreviewUrl(path.trim(), pno + 1, 120)}
                  alt="PDF 预览缩略图"
                  className="max-h-40 self-start rounded-md border object-contain"
                  onError={() => setThumbOk(false)}
                />
              )}
            </div>
          )}
          <p role="status" className="text-xs leading-5 text-muted-foreground">
            {s.ocrPrompt.trim()
              ? `使用自定义提示词（${s.ocrPrompt.length} 字，在「参数」页编辑）`
              : "使用默认提示词（可在「参数」页自定义）"}
            {phase === "loading" && ` · 已等待 ${waited}s / 超时 ${s.timeout}s`}
            {elapsed != null && phase === "done" && ` · 耗时 ${elapsed}ms`}
          </p>
          {s.ocrPrompt.length > 8000 && (
            <p role="note" className="text-xs leading-5 text-warn">
              提示词已超 8k 字符，可能挤占输出窗口导致空返回。
            </p>
          )}
          {preview && (
            <img
              src={`data:image/png;base64,${preview}`}
              alt="页面渲染预览"
              className="max-h-64 self-start rounded-md border object-contain"
            />
          )}
        </CardContent>
        <CardFooter className="justify-end">
          {phase === "loading" ? (
            <Button variant="destructive" onClick={cancel}>
              <XCircle aria-hidden="true" />
              取消
            </Button>
          ) : (
            <>
              <Button variant="outline" onClick={copy} disabled={!markdown}>
                <Copy aria-hidden="true" />
                {copied ? "已复制" : "复制"}
              </Button>
              <Button variant="outline" onClick={save} disabled={!markdown}>
                <Save aria-hidden="true" />
                保存结果…
              </Button>
              <Button className="ml-2" onClick={run}>
                开始识别
              </Button>
            </>
          )}
        </CardFooter>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>识别结果</CardTitle>
          <CardDescription>成功显示 Markdown，失败看底部运行日志。</CardDescription>
        </CardHeader>
        <CardContent>
          {phase === "idle" && (
            <EmptyState icon={FileText} tone="idle" title="还没有结果" description="选好来源后点“开始识别”。" />
          )}
          {phase === "loading" && (
            <div className="flex flex-col gap-2" aria-label="加载中">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          )}
          {phase === "error" && (
            <EmptyState icon={FileText} tone="error" title="识别失败" description={error} actionLabel="重试" onAction={run} />
          )}
          {phase === "done" && (
            <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-xs leading-5">
              {markdown}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
