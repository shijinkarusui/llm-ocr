import { useEffect, useRef, useState } from "react";
import { BookOpen } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { useLog } from "@/stores/log";
import { searchableBuild, job, jobCancel, bookResolve, type BookResolveRes } from "@/lib/engine";
import { pickFile, pickDir, PDF_FILTER } from "@/lib/pick";

type Geo = "auto" | "embedded" | "external" | "fallback_only";
type Phase = "idle" | "running" | "done" | "error";

export function SearchableView() {
  const emit = useLog((s) => s.emit);
  const [pdf, setPdf] = useState("");
  const [dir, setDir] = useState("");
  const [geo, setGeo] = useState<Geo>("auto");
  const [kw, setKw] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [book, setBook] = useState<BookResolveRes | null>(null);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const timer = useRef<number | null>(null);

  function stopPoll() {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  }

  useEffect(() => stopPoll, []);

  async function poll(id: string) {
    try {
      const j = await job(id);
      if (j.status === "done") {
        stopPoll();
        setResult((j.result as Record<string, unknown>) ?? {});
        setPhase("done");
        emit("构建双层 PDF 完成", "ok");
      } else if (j.status === "error") {
        stopPoll();
        setPhase("error");
        setError(j.error ?? "未知错误");
        emit(`构建失败：${j.error ?? "未知错误"}`, "err");
      }
    } catch (e) {
      emit(`轮询失败：${e instanceof Error ? e.message : e}`, "warn");
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
    setPhase("running");
    setError("");
    setResult(null);
    emit("开始：构建双层 PDF", "muted");
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
      const r = await searchableBuild(pdfUse, dir.trim(), geo, kws);
      for (const w of r.warnings ?? []) emit(w, "warn");
      setJobId(r.job_id);
      stopPoll();
      timer.current = window.setInterval(() => poll(r.job_id), 2000);
      await poll(r.job_id);
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      emit(`构建启动失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

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
            {(["auto", "embedded", "external", "fallback_only"] as Geo[]).map((g) => (
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
          <p className="text-xs leading-5 text-muted-foreground">双层 PDF 的文字层是看不见的，肉眼没法确认它有没有写进去；这里填的词会被拿到输出 PDF 的文字层里去搜，用来证明文字层确实存在、位置合理。</p>
          <p className="text-xs leading-5 text-muted-foreground">填你确信书里会出现、且分布在不同页的词，多个词用逗号分隔（中英文逗号都行）。纯子串匹配、不分词，所以别填「的」「了」这种满页都有的字，挑有辨识度的词；留空 = 跳过验证，不影响构建。</p>
          <p className="text-xs leading-5 text-muted-foreground">输出目录填批量产出的书文件夹，也就是含 book.json 与 pages/*.md 的那一层；填上级目录时靠各书的 book.json 认出唯一一本，认不出会直接报错而不猜。</p>
        </CardContent>
        <CardFooter className="justify-end">
          {phase === "running" ? (
            <Button variant="destructive" onClick={async () => { if (jobId) await jobCancel(jobId); stopPoll(); emit("已请求取消", "warn"); }}>
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
        <CardContent>
          {phase === "idle" && (
            <EmptyState icon={BookOpen} tone="idle" title="还没有构建" description="选好原 PDF 与输出目录后点“构建并验证”。" />
          )}
          {phase === "running" && (
            <div className="flex flex-col gap-2" aria-label="构建中">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-full" />
            </div>
          )}
          {phase === "error" && (
            <EmptyState icon={BookOpen} tone="error" title="构建失败" description={error} actionLabel="重试" onAction={build} />
          )}
          {phase === "done" && result && (
            <div className="text-[13px] leading-6">
              <p className="break-all">输出：{String(result.pdf ?? "")}</p>
              <p className="tabular">
                大小：{String(result.bytes ?? "?")} 字节 页数：{String(result.pages ?? "?")}
              </p>
              <p className="tabular">
                可复制字数：{String(result.copyable_chars ?? "?")} 命中页数：
                {String(result.hit_page_count ?? "?")}
              </p>
              <p>关键词：{kw.trim() ? kw : "未填（已跳过关键词验证）"}</p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
