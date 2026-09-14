import { useEffect, useRef, useState } from "react";
import { BookOpen } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { useLog } from "@/stores/log";
import { searchableBuild, job, jobCancel } from "@/lib/engine";
import { pickFile, pickDir, PDF_FILTER } from "@/lib/pick";

type Geo = "auto" | "embedded" | "external" | "fallback_only";
type Phase = "idle" | "running" | "done" | "error";

export function SearchableView() {
  const emit = useLog((s) => s.emit);
  const [pdf, setPdf] = useState("");
  const [dir, setDir] = useState("");
  const [geo, setGeo] = useState<Geo>("auto");
  const [kw, setKw] = useState("北京话,同化,韵母");
  const [phase, setPhase] = useState<Phase>("idle");
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

  async function build() {
    if (!pdf.trim()) {
      emit("请选择有效原 PDF", "err");
      return;
    }
    if (!dir.trim()) {
      emit("请选择有效 OCR 输出目录（含 pages/*.md）", "err");
      return;
    }
    setPhase("running");
    setError("");
    setResult(null);
    emit("开始：构建双层 PDF", "muted");
    try {
      const kws = kw.replace(/，/g, ",").split(",").map((x) => x.trim()).filter(Boolean);
      const r = await searchableBuild(pdf.trim(), dir.trim(), geo, kws);
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
            <Input id="search-dir" value={dir} onChange={(e) => setDir(e.target.value)} placeholder="含 pages/*.md 的目录" />
            <Button variant="outline" size="sm" onClick={async () => { const v = await pickDir(); if (v) setDir(v); }}>
              选目录…
            </Button>
          </div>
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
          <p className="text-xs leading-5 text-muted-foreground">
            输出目录指批量页含 pages/*.md 的目录；关键词用中文逗号分隔。
          </p>
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
              <p>关键词：{kw}</p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
