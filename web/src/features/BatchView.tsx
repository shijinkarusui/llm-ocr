import { useEffect, useRef, useState } from "react";
import { Layers } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { useSession } from "@/stores/session";
import { useLog } from "@/stores/log";
import { batchDryRun, batchRun, job, jobCancel, type DryRunRes, type JobProgress } from "@/lib/engine";
import { pickFile, pickDir, PDF_FILTER } from "@/lib/pick";
import { parseExtra } from "@/lib/utils";

type Phase = "idle" | "loading" | "running" | "done" | "error";

export function BatchView() {
  const s = useSession();
  const emit = useLog((x) => x.emit);
  const [pdf, setPdf] = useState("");
  const [outdir, setOutdir] = useState("out/book_gui");
  const [start, setStart] = useState(0);
  const [endText, setEndText] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [dry, setDry] = useState<DryRunRes | null>(null);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState("");
  const [prog, setProg] = useState<JobProgress | null>(null);
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const timer = useRef<number | null>(null);

  function stopPoll() {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  }

  useEffect(() => stopPoll, []);

  async function doDry() {
    if (!pdf.trim()) {
      emit("请选择有效 PDF", "err");
      return;
    }
    setPhase("loading");
    setError("");
    emit("开始：批量 Dry-Run", "muted");
    try {
      const end = endText.trim() === "" ? undefined : Number(endText);
      const r = await batchDryRun(pdf.trim(), outdir.trim() || "out/book_gui", start, end);
      setDry(r);
      setPhase("idle");
      emit(`dry-run：全书 ${r.total_pages} 页，本次计划 ${r.planned_pages} 页`, "ok");
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      emit(`Dry-Run 失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  async function poll(id: string) {
    try {
      const j = await job(id);
      if (j.progress) setProg(j.progress);
      if (j.status === "done") {
        stopPoll();
        setSummary((j.summary as Record<string, unknown>) ?? null);
        setPhase("done");
        const toks = (j.summary as Record<string, unknown> | undefined)?.total_tokens ?? "?";
        const failed = (j.summary as Record<string, unknown> | undefined)?.failed_pages_this_run ?? "?";
        emit(`batch 完成 tokens=${String(toks)} failed=${String(failed)}`, "ok");
      } else if (j.status === "error") {
        stopPoll();
        setPhase("error");
        setError(j.error ?? "未知错误");
        emit(`批量失败：${j.error ?? "未知错误"}`, "err");
      }
    } catch (e) {
      emit(`轮询失败：${e instanceof Error ? e.message : e}`, "warn");
    }
  }

  async function doRun() {
    if (!pdf.trim()) {
      emit("请选择有效 PDF", "err");
      return;
    }
    let extra: Record<string, unknown>;
    try {
      extra = parseExtra(s.extraText);
    } catch (e) {
      emit(`extra-json 非法：${e instanceof Error ? e.message : e}`, "err");
      return;
    }
    setPhase("running");
    setError("");
    setProg(null);
    setSummary(null);
    emit("开始：批量 OCR（断点续跑）", "muted");
    try {
      const end = endText.trim() === "" ? undefined : Number(endText);
      const r = await batchRun(
        {
          baseUrl: s.baseUrl, model: s.model, key: s.apiKey,
          endpoint: s.endpoint, detail: s.detail, timeout: s.timeout,
        },
        pdf.trim(), outdir.trim() || "out/book_gui", start, end,
        s.dpi, s.concurrency, s.retries, extra, s.ocrPrompt,
      );
      setJobId(r.job_id);
      stopPoll();
      timer.current = window.setInterval(() => poll(r.job_id), 2000);
      await poll(r.job_id);
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      emit(`批量启动失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  async function doCancel() {
    if (!jobId) return;
    await jobCancel(jobId);
    stopPoll();
    emit("已请求取消（当前页跑完后停止）", "warn");
  }

  const total = dry?.planned_pages ?? 0;

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>批量任务</CardTitle>
          <CardDescription>断点续跑：已成功的页自动跳过（看 usage.jsonl）。先 Dry-Run 看页数，再开始。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex items-center gap-2.5">
            <Label htmlFor="batch-pdf">PDF 文件</Label>
            <Input id="batch-pdf" value={pdf} onChange={(e) => setPdf(e.target.value)} placeholder="服务端可读路径" />
            <Button variant="outline" size="sm" onClick={async () => { const v = await pickFile(PDF_FILTER); if (v) setPdf(v); }}>
              选 PDF…
            </Button>
          </div>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="batch-out">输出目录</Label>
            <Input id="batch-out" value={outdir} onChange={(e) => setOutdir(e.target.value)} />
            <Button variant="outline" size="sm" onClick={async () => { const v = await pickDir(); if (v) setOutdir(v); }}>
              选目录…
            </Button>
          </div>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="batch-start">页码范围</Label>
            <Input id="batch-start" className="w-24" inputMode="numeric" value={String(start)} onChange={(e) => setStart(Number(e.target.value) || 0)} />
            <span className="text-xs text-muted-foreground">到</span>
            <Input id="batch-end" className="w-32" inputMode="numeric" value={endText} onChange={(e) => setEndText(e.target.value)} placeholder="留空=到尾页" />
          </div>
          {dry && (
            <p className="text-[13px] tabular" role="status">
              全书 {dry.total_pages} 页，本次计划 {dry.planned_pages} 页
            </p>
          )}
          <p className="text-xs leading-5 text-muted-foreground" role="status">
            {s.ocrPrompt.trim()
              ? `使用自定义提示词（${s.ocrPrompt.length} 字，在「参数」页编辑）`
              : "使用默认提示词（可在「参数」页自定义）"}
          </p>
        </CardContent>
        <CardFooter>
          <Button variant="outline" onClick={doDry} disabled={phase === "loading" || phase === "running"}>
            规划页数
          </Button>
          <span className="ml-auto flex gap-2">
            {phase === "running" ? (
              <Button variant="destructive" onClick={doCancel}>取消任务</Button>
            ) : (
              <Button onClick={doRun}>开始批量</Button>
            )}
          </span>
        </CardFooter>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>进度</CardTitle>
          <CardDescription>进度条按“已成功页 / 计划页”推进；数字行读 usage.jsonl 汇总。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {phase === "loading" && (
            <div className="flex flex-col gap-2" aria-label="加载中">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-2.5 w-full" />
            </div>
          )}
          {phase === "error" && (
            <EmptyState icon={Layers} tone="error" title="出错了" description={error} actionLabel="重试 Dry-Run" onAction={doDry} />
          )}
          {(phase === "idle" || phase === "running" || phase === "done") && (
            <>
              <p className="text-[13px] tabular" role="status">
                成功 {prog?.pages_ok ?? 0} 页 · tokens {prog?.tokens ?? 0} · skipped {prog?.skipped ?? 0} ·
                failed {prog?.failed ?? 0}
                {(prog?.p50_ms ?? 0) > 0 && <> · p50 {prog?.p50_ms}ms p95 {prog?.p95_ms}ms</>}
              </p>
              <Progress value={prog?.pages_ok ?? 0} max={total > 0 ? total : 100} />
              {phase === "idle" && !prog && (
                <EmptyState icon={Layers} tone="idle" title="尚未开始" description="跑的过程中自动轮询实时数字；中断后重跑自动续上。" />
              )}
              {phase === "done" && summary && (
                <p className="text-[13px] text-muted-foreground">
                  完成：tokens {String(summary.total_tokens ?? "?")}，本轮失败{" "}
                  {String(summary.failed_pages_this_run ?? "?")} 页。
                  {summary.merged_md ? ` 整本 Markdown：${String(summary.merged_md)}` : " 未合并出整本 Markdown（pages/ 下还没有页文件）。"}
                </p>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
