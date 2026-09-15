import { useEffect, useRef, useState } from "react";
import { BookOpen } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { Progress } from "@/components/ui/progress";
import { useLog } from "@/stores/log";
import { searchableBuild, listJobs, job, jobCancel, bookResolve, type BookResolveRes, type JobListItem } from "@/lib/engine";
import { toZh } from "@/lib/errors";
import { pickFile, pickDir, PDF_FILTER } from "@/lib/pick";

// P4: hide `external` (backend has no boxes param and always 400s it).
type Geo = "auto" | "embedded" | "fallback_only";
type Phase = "idle" | "running" | "done" | "cancelled" | "error";

function fmtBytes(n: unknown): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "?";
  if (n < 1024) return `${n} 字节`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function SearchableView() {
  const emit = useLog((s) => s.emit);
  const [pdf, setPdf] = useState("");
  const [dir, setDir] = useState("");
  const [geo, setGeo] = useState<Geo>("auto");
  const [kw, setKw] = useState("");
  // P2: searchable render DPI, 72-300, default 150, passed through to build.
  const [dpiText, setDpiText] = useState("150");
  const [phase, setPhase] = useState<Phase>("idle");
  const [book, setBook] = useState<BookResolveRes | null>(null);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [copied, setCopied] = useState(false);
  // P2: running progress stage+pct; null-safe (old backend omits them).
  const [stage, setStage] = useState<string | null>(null);
  const [pct, setPct] = useState<number | null>(null);
  // P2: job history; null = unsupported -> hidden.
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const timer = useRef<number | null>(null);
  const failCount = useRef(0);

  function stopPoll() {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  }

  useEffect(() => stopPoll, []);

  // P2: job history, hidden when the backend has no /api/jobs list endpoint.
  useEffect(() => {
    let alive = true;
    void listJobs(20).then((r) => { if (alive) setJobs(r); });
    return () => { alive = false; };
  }, []);

  async function poll(id: string) {
    try {
      const j = await job(id);
      failCount.current = 0;
      // P2: running progress stage+pct (both optional; stale values kept when omitted).
      if (j.progress) {
        if (typeof j.progress.stage === "string" && j.progress.stage) setStage(j.progress.stage);
        if (typeof j.progress.pct === "number" && Number.isFinite(j.progress.pct)) setPct(j.progress.pct);
      }
      if (j.status === "done") {
        stopPoll();
        setResult((j.result as Record<string, unknown>) ?? {});
        setPhase("done");
        const warns = ((j.result as Record<string, unknown> | undefined)?.font_warnings as string[] | undefined) ?? [];
        for (const w of warns) emit(w, "warn");
        emit("构建双层 PDF 完成", "ok");
        void listJobs(20).then((r) => { if (r) setJobs(r); });
      } else if (j.status === "cancelled") {
        stopPoll();
        setResult((j.result as Record<string, unknown> | undefined) ?? null);
        setPhase("cancelled");
        emit("构建已取消", "warn");
      } else if (j.status === "error") {
        stopPoll();
        setPhase("error");
        const zh = toZh(j.error ?? "未知错误");
        setError(j.error_code ? `${zh}（${j.error_code}）` : zh);
        emit(`构建失败：${j.error ?? "未知错误"}`, "err");
      }
    } catch (e) {
      failCount.current += 1;
      emit(`轮询失败(${failCount.current})：${e instanceof Error ? e.message : e}`, "warn");
      if (failCount.current >= 20) {
        stopPoll();
        setPhase("error");
        setError("轮询 20 次失败，已暂停；检查服务端后点“构建并验证”重试。");
      }
    }
  }

  /** Bind the chosen directory to one book via book.json before building anything. */
  async function identify(dirValue: string, pdfValue: string): Promise<BookResolveRes | null> {
    if (!dirValue) {
      emit("请先选择有效 OCR 输出目录", "err");
      return null;
    }
    try {
      const r = await bookResolve(dirValue, pdfValue || undefined);
      setBook(r);
      if (r.source_pdf && !pdfValue) {
        setPdf(r.source_pdf);
        emit(`已从 book.json 读出源 PDF：${r.source_pdf}`, "ok");
      }
      if (r.status === "bound") emit(`身份绑定：${r.message}`, r.warnings.length > 0 ? "warn" : "ok");
      else if (r.status === "unbound") emit(`无身份文件：${r.message}`, "warn");
      else emit(`身份绑定失败：${r.message}`, "err");
      for (const w of r.warnings) emit(w, "warn");
      return r;
    } catch (e) {
      setBook(null);
      emit(`身份绑定请求失败：${e instanceof Error ? e.message : e}`, "err");
      return null;
    }
  }

  const bindFailed = (r: BookResolveRes | null) =>
    r !== null && (r.status === "error" || r.status === "not_found" || r.status === "ambiguous" || r.status === "mismatch");

  async function build() {
    if (!dir.trim()) {
      emit("请选择有效 OCR 输出目录（含 pages/*.md）", "err");
      return;
    }
    // P2: DPI 72-300 clamp up front; old backend ignores the extra field harmlessly.
    const dpiRaw = Number(dpiText) || 150;
    const dpi = Math.min(300, Math.max(72, Math.round(dpiRaw)));
    if (String(dpi) !== dpiText.trim()) setDpiText(String(dpi));
    failCount.current = 0;
    setPhase("running");
    setError("");
    setResult(null);
    setCopied(false);
    setStage(null);
    setPct(null);
    emit(`开始：构建双层 PDF（DPI ${dpi}）`, "muted");
    try {
      // Never build one book's pages onto another book's raster: bind first.
      const bound = await identify(dir.trim(), pdf.trim());
      if (bindFailed(bound)) {
        setPhase("error");
        setError(bound?.message ?? "身份绑定失败");
        return;
      }
      const pdfUse = bound?.source_pdf || pdf.trim();
      if (!pdfUse) {
        setPhase("error");
        setError("无法确定源 PDF：该目录没有 book.json 记录源文件，请手动选择原 PDF。");
        return;
      }
      const kws = kw.replace(/，/g, ",").split(",").map((x) => x.trim()).filter(Boolean);
      const r = await searchableBuild(pdfUse, dir.trim(), geo, kws, dpi);
      for (const w of r.warnings ?? []) emit(w, "warn");
      setJobId(r.job_id);
      stopPoll();
      timer.current = window.setInterval(() => poll(r.job_id), 2000);
      await poll(r.job_id);
    } catch (e) {
      setPhase("error");
      setError(toZh(e));
      emit(`构建启动失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  async function copyPath() {
    const p = String(result?.pdf ?? "");
    if (!p) return;
    try {
      await navigator.clipboard.writeText(p);
      setCopied(true);
      emit("输出路径已复制", "ok");
    } catch (e) {
      emit(`复制失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  const summary = (result?.align_summary as Record<string, unknown> | undefined) ?? null;
  const fontWarnings = (result?.font_warnings as string[] | undefined) ?? [];
  const hitPages = (result?.hit_pages as number[] | undefined) ?? [];
  const missingPages = (result?.missing_pages as number[] | undefined) ?? null;
  const missingExample = (result?.missing_example as number[] | undefined) ?? [];
  const missingCount = (result?.missing_count as number | undefined) ?? null;

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>双层可搜索 PDF</CardTitle>
          <CardDescription>把原 PDF 的每一页都垫上 OCR 文字层，输出 book_searchable.pdf。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex items-center gap-2.5">
            <Label htmlFor="search-pdf">原 PDF</Label>
            <Input id="search-pdf" value={pdf} onChange={(e) => setPdf(e.target.value)} placeholder="服务端可读路径" />
            <Button variant="outline" size="sm" onClick={async () => { const v = await pickFile(PDF_FILTER); if (v) setPdf(v); }}>
              选原 PDF…
            </Button>
          </div>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="search-dir">OCR 输出目录</Label>
            <Input id="search-dir" value={dir} onChange={(e) => setDir(e.target.value)} placeholder="书文件夹（含 book.json 与 pages/）" />
            <Button variant="outline" size="sm" onClick={async () => { const v = await pickDir(); if (v) { setDir(v); await identify(v, pdf.trim()); } }}>
              选目录…
            </Button>
          </div>
          <div className="flex items-center gap-2.5">
            <span className="w-24 shrink-0 text-[13px] leading-8 text-muted-foreground">身份绑定</span>
            <Button variant="outline" size="sm" disabled={phase === "running"} onClick={() => identify(dir.trim(), pdf.trim())}>
              识别书籍
            </Button>
            <span className="text-xs text-muted-foreground">按 book.json 认书，认不出来的书不会开始构建</span>
          </div>
          {book && (
            <div className="text-xs leading-5" role="status">
              <p className={book.status === "bound" ? "text-muted-foreground" : "text-destructive"}>{book.message}</p>
              {book.source_pdf && <p className="break-all text-muted-foreground">源 PDF：{book.source_pdf}</p>}
              {book.warnings.map((w) => (
                <p key={w} className="text-destructive">注意：{w}</p>
              ))}
              {book.candidates.length > 0 && (
                <ul className="list-disc pl-4 text-muted-foreground">
                  {book.candidates.slice(0, 6).map((c) => (
                    <li key={c.book_dir} className="break-all">
                      {c.source_name}
                      {c.page_count ? `（${c.page_count} 页）` : ""} — {c.book_dir}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <fieldset className="flex items-center gap-4">
            <legend className="w-24 shrink-0 text-[13px] leading-8 text-muted-foreground">几何源</legend>
            {(["auto", "embedded", "fallback_only"] as Geo[]).map((g) => (
              <label key={g} className="flex items-center gap-1.5 font-mono text-xs">
                <input
                  type="radio"
                  name="search-geo"
                  value={g}
                  checked={geo === g}
                  onChange={() => setGeo(g)}
                  className="size-3.5 accent-[var(--primary)]"
                />
                {g}
              </label>
            ))}
          </fieldset>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="search-kw">验证关键词</Label>
            <Input id="search-kw" value={kw} onChange={(e) => setKw(e.target.value)} />
          </div>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="search-dpi">渲染 DPI</Label>
            <Input
              id="search-dpi"
              className="w-24"
              inputMode="numeric"
              value={dpiText}
              onChange={(e) => {
                const raw = e.target.value;
                setDpiText(raw);
                const n = Number(raw);
                if (raw.trim() !== "" && Number.isFinite(n) && (n < 72 || n > 300)) {
                  emit(`DPI 越界，已限制 72-300（当前 ${raw}）`, "warn");
                }
              }}
              placeholder="150"
            />
            <span className="text-xs text-muted-foreground">72-300，默认 150，直传给构建</span>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">双层 PDF 的文字层是看不见的，肉眼没法确认它有没有写进去；这里填的词会被拿到输出 PDF 的文字层里去搜，用来证明文字层确实存在、位置合理。</p>
          <p className="text-xs leading-5 text-muted-foreground">填你确信书里会出现、且分布在不同页的词，多个词用逗号分隔（中英文逗号都行）。纯子串匹配、不分词，所以别填「的」「了」这种满页都有的字，挑有辨识度的词；留空 = 跳过验证，不影响构建。</p>
          <p className="text-xs leading-5 text-muted-foreground">输出目录填批量产出的书文件夹，也就是含 book.json 与 pages/*.md 的那一层；填上级目录时靠各书的 book.json 认出唯一一本，认不出会直接报错而不猜。</p>
        </CardContent>
        <CardFooter className="justify-end">
          {phase === "running" ? (
            <Button variant="destructive" onClick={async () => { if (jobId) await jobCancel(jobId); stopPoll(); setPhase("cancelled"); emit("已请求取消", "warn"); }}>
              取消任务
            </Button>
          ) : (
            <Button onClick={build}>构建并验证</Button>
          )}
        </CardFooter>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>构建结果</CardTitle>
          <CardDescription>输出路径、页数、可复制字数与关键词命中页。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {phase === "idle" && (
            <EmptyState icon={BookOpen} tone="idle" title="还没有构建" description="选好原 PDF 与输出目录后点“构建并验证”。" />
          )}
          {phase === "running" && (
            <div className="flex flex-col gap-2" aria-label="构建中">
              {/* P2: stage+pct when the backend reports them; skeleton otherwise. */}
              {stage || pct !== null ? (
                <>
                  <p className="text-[13px] tabular" role="status">
                    {stage ? `阶段：${stage}` : "构建中"}{pct !== null ? `（${pct}%）` : ""}
                  </p>
                  {pct !== null && <Progress value={pct} max={100} />}
                </>
              ) : (
                <>
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-4 w-full" />
                </>
              )}
            </div>
          )}
          {phase === "cancelled" && (
            <EmptyState icon={BookOpen} tone="idle" title="已取消" description="构建已取消；已落盘的产物保留，可重新构建。" actionLabel="重新构建" onAction={build} />
          )}
          {phase === "error" && (
            <EmptyState icon={BookOpen} tone="error" title="构建失败" description={error} actionLabel="重试" onAction={build} />
          )}
          {phase === "error" && missingPages !== null && missingCount !== null && (
            <div className="mt-2 text-[13px] leading-6" role="status">
              <p className="tabular">缺 {missingCount} 页，例如：{missingExample.join("、")}</p>
              <p className="text-muted-foreground">先回“批量”把缺的页补跑出来（续跑自动跳过已完成页），再回来重建。</p>
            </div>
          )}
          {phase === "done" && result && (
            <div className="text-[13px] leading-6">
              <p className="break-all">输出：{String(result.pdf ?? "")}</p>
              <p className="tabular">
                大小：{fmtBytes(result.bytes)} 页数：{String(result.pages ?? "?")}
              </p>
              <p className="tabular">
                可复制字数：{String(result.copyable_chars ?? "?")} 命中页数：
                {String(result.hit_page_count ?? "?")}
              </p>
              <p>关键词：{kw.trim() ? kw : "未填（已跳过关键词验证）"}</p>
              {hitPages.length > 0 && (
                <p className="tabular text-muted-foreground">命中页：{hitPages.slice(0, 20).join("、")}{hitPages.length > 20 ? `…（共 ${hitPages.length} 页）` : ""}</p>
              )}
              {summary && (
                <div className="mt-2 rounded-md bg-muted p-2.5 text-xs leading-5" role="status">
                  <p className="tabular">
                    对齐：零损 {String(summary.zero_loss_pages ?? "?")}/{String(summary.pages_total ?? "?")} 页；
                    几何门 {String(summary.geometric_ok_pages ?? "?")}/{String(summary.pages_total ?? "?")} 页
                  </p>
                  {Array.isArray(summary.geometric_fail_pages) && summary.geometric_fail_pages.length > 0 && (
                    <p className="tabular text-destructive">
                      几何失败页：{(summary.geometric_fail_pages as number[]).slice(0, 20).join("、")}
                      {(summary.geometric_fail_pages as number[]).length > 20 ? "…" : ""}
                    </p>
                  )}
                  {Array.isArray(summary.table_unreliable_pages) && summary.table_unreliable_pages.length > 0 && (
                    <p className="tabular text-warn">
                      表格不可靠页：{(summary.table_unreliable_pages as number[]).slice(0, 20).join("、")}
                      {(summary.table_unreliable_pages as number[]).length > 20 ? "…" : ""}
                    </p>
                  )}
                  <p className="break-all text-muted-foreground">
                    对齐目录：{String(result.align_dir ?? "")} · 验证报告：{String(result.verify_report ?? "")}
                  </p>
                </div>
              )}
              {(result.verify_error as string | undefined) && (
                <p role="note" className="text-xs leading-5 text-warn">对齐验证未完成：{String(result.verify_error)}（构建产物可用）</p>
              )}
              {fontWarnings.map((w) => (
                <p key={w} role="note" className="text-xs leading-5 text-warn">{w}</p>
              ))}
              <div className="mt-2 flex gap-2">
                <Button variant="outline" size="sm" onClick={copyPath}>{copied ? "已复制" : "复制路径"}</Button>
              </div>
            </div>
          )}
          {/* P2: job history; hidden entirely when the backend has no list endpoint (jobs === null). */}
          {jobs !== null && jobs.length > 0 && (
            <div className="flex flex-col gap-1.5" aria-label="历史任务">
              <p className="text-[13px] text-muted-foreground" role="status">历史任务（近 {jobs.length} 个）</p>
              <ul className="max-h-40 overflow-y-auto pl-1 text-xs leading-5">
                {jobs.slice(0, 20).map((h) => (
                  <li key={h.id} className="break-all font-mono text-muted-foreground">
                    {h.id}{h.kind ? ` · ${h.kind}` : ""}{h.status ? ` · ${h.status}` : ""}{h.label ? ` · ${h.label}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
