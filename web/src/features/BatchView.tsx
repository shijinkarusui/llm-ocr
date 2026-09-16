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
import { batchDryRun, batchRetry, batchRun, job, jobCancel, jobPages, listJobs, pdfPreviewUrl, type DryRunRes, type JobPageItem, type JobProgress } from "@/lib/engine";
import { toZh } from "@/lib/errors";
import { pickFile, pickDir, PDF_FILTER } from "@/lib/pick";
import { parseExtra } from "@/lib/utils";

type Phase = "idle" | "loading" | "running" | "stopping" | "done" | "cancelled" | "error";

function fmtEta(ms?: number): string {
  if (!ms || ms <= 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `约${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `约${m}分${s % 60}s`;
  return `约${Math.floor(m / 60)}时${m % 60}分`;
}

function fmtElapsed(ms?: number): string {
  if (ms == null || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}分${s % 60}s`;
  return `${Math.floor(m / 60)}时${m % 60}分${s % 60}s`;
}

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
  const [failPages, setFailPages] = useState<JobPageItem[] | null>(null);
  const [failTotal, setFailTotal] = useState(0);
  const [copiedFail, setCopiedFail] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [thumbOk, setThumbOk] = useState(true);
  const timer = useRef<number | null>(null);
  const failCount = useRef(0);

  function stopPoll() {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }

  function schedule(id: string, delayMs: number) {
    stopPoll();
    timer.current = window.setTimeout(() => poll(id), delayMs);
  }

  useEffect(() => {
    let unmounted = false;
    void (async () => {
      try {
        const jobs = await listJobs(10);
        if (unmounted || !jobs) return;
        const activeBatch = jobs.find(
          (item) => item.kind === "batch" && (item.status === "running" || item.status === "queued"),
        );
        if (activeBatch) {
          setJobId(activeBatch.id);
          setPhase("running");
          emit(`已连接后台运行中的批量任务（job=${activeBatch.id}）`, "ok");
          schedule(activeBatch.id, 0);
        }
      } catch {
        /* 静默跳过 */
      }
    })();
    return () => {
      unmounted = true;
      stopPoll();
    };
  }, []);

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
      const resume = (r.done_pages ?? 0) > 0 ? `（已完成 ${r.done_pages}，还剩 ${r.remaining ?? "?"}，点开始批量继续跑）` : "";
      emit(`dry-run：全书 ${r.total_pages} 页，本次计划 ${r.planned_pages} 页${resume}`, "ok");
    } catch (e) {
      setPhase("error");
      setError(toZh(e));
      emit(`Dry-Run 失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  async function loadFailPages(id: string) {
    try {
      const r = await jobPages(id, "failed", 200, 0);
      setFailPages(r.pages);
      setFailTotal(r.total);
    } catch {
      setFailPages(null);
    }
  }

  async function poll(id: string) {
    try {
      const j = await job(id);
      failCount.current = 0;
      if (j.progress) setProg(j.progress);
      if (j.status === "done") {
        stopPoll();
        setSummary((j.summary as Record<string, unknown>) ?? null);
        setPhase("done");
        setRetrying(false);
        const toks = (j.summary as Record<string, unknown> | undefined)?.total_tokens ?? "?";
        const failed = (j.summary as Record<string, unknown> | undefined)?.failed_pages_this_run ?? "?";
        emit(`batch 完成 tokens=${String(toks)} failed=${String(failed)}`, "ok");
        await loadFailPages(id);
      } else if (j.status === "cancelled") {
        stopPoll();
        setSummary((j.summary as Record<string, unknown>) ?? null);
        setPhase("cancelled");
        setRetrying(false);
        emit(`批量已取消：已落盘的不再涨，重跑自动续上`, "warn");
        await loadFailPages(id);
      } else if (j.status === "error") {
        stopPoll();
        setPhase("error");
        setRetrying(false);
        setError(toZh(j.error ?? j.hint_cn ?? "未知错误"));
        emit(`批量失败：${j.error ?? j.hint_cn ?? "未知错误"}`, "err");
        await loadFailPages(id);
      } else {
        // running / queued: keep polling every 2s
        setPhase("running");
        schedule(id, 2000);
      }
    } catch (e) {
      failCount.current += 1;
      emit(`轮询失败（${failCount.current}）：${e instanceof Error ? e.message : e}`, "warn");
      if (failCount.current >= 20) {
        stopPoll();
        setPhase("error");
        setError(`轮询多次失败已暂停（${failCount.current}次）：${toZh(e)}。任务可能仍在跑，点“继续轮询”可接着看。`);
      } else {
        schedule(id, failCount.current >= 5 ? 10000 : 2000);
      }
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
    // 764 页级大书二次确认：计划页数来自 dry-run，未规划时按全书估。
    const plannedGuess = dry?.planned_pages ?? 0;
    if (plannedGuess >= 500 && !window.confirm(`本次计划 ${plannedGuess} 页，耗时较长（中途可取消、可断点续跑）。确定开始吗？`)) {
      return;
    }
    setPhase("running");
    setError("");
    setProg(null);
    setSummary(null);
    setFailPages(null);
    setFailTotal(0);
    setCopiedFail(false);
    failCount.current = 0;
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
      schedule(r.job_id, 0);
    } catch (e) {
      setPhase("error");
      setError(toZh(e));
      emit(`批量启动失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  async function doRetryFailed() {
    if (!jobId) return;
    setRetrying(true);
    setError("");
    emit("开始：仅重试失败页", "muted");
    try {
      const r = await batchRetry(jobId);
      // Backend may return the same (0 failed) or a new retry job id; poll either way.
      const nextId = r.job_id || jobId;
      if (r.retried && r.retried.length > 0) {
        emit(`后端已建重试任务：${r.retried.length} 页（${r.retried.slice(0, 10).join("、")}${r.retried.length > 10 ? "…" : ""}），job=${nextId}`, "ok");
      } else {
        emit(`后端接手重试：job=${nextId}（无失败页则原地复查）`, "ok");
      }
      setJobId(nextId);
      setPhase("running");
      failCount.current = 0;
      stopPoll();
      schedule(nextId, 0);
    } catch (e) {
      setRetrying(false);
      const zh = toZh(e);
      emit(`仅重试失败页不可用：${e instanceof Error ? e.message : e}`, "err");
      setError(`${zh}（该后端暂无 /api/batch/retry，请点“开始批量”手动续跑：已成功页会自动跳过）`);
    }
  }

  async function doCancel() {
    if (!jobId) return;
    setPhase("stopping");
    try {
      await jobCancel(jobId);
    } catch (e) {
      emit(`取消请求失败：${e instanceof Error ? e.message : e}`, "err");
    }
    // 真取消：在途页跑完即停；继续轮询直到后端回 cancelled，不擅自停转。
    emit("正在停止…（在途页跑完即停，已落盘的不再涨）", "warn");
    schedule(jobId, 1000);
  }

  async function copyFailPages() {
    if (!failPages || failPages.length === 0) return;
    const text = failPages.map((p) => String(p.page_number ?? p.pno_0based + 1)).join(",");
    try {
      await navigator.clipboard.writeText(text);
      setCopiedFail(true);
      emit(`已复制 ${failPages.length} 个失败页号`, "ok");
    } catch (e) {
      emit(`复制失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  const total = dry?.planned_pages ?? prog?.planned ?? 0;
  const pct = prog?.pct ?? (total > 0 ? Math.round(((prog?.pages_ok ?? 0) / total) * 1000) / 10 : 0);
  const running = phase === "running" || phase === "stopping";
  // Boolean flags (not inline phase checks) so TS narrowing cannot hide the error branch.
  const progressVisible =
    phase === "idle" || running || phase === "done" || phase === "cancelled" || (phase === "error" && prog !== null);
  const failListVisible =
    (phase === "done" || phase === "cancelled" || phase === "error") && failPages !== null;
  const doneVisible = phase === "done" || phase === "cancelled";
  // P1: done + has failed pages -> offer failed-only retry (graceful fallback inside doRetryFailed).
  const retryFailedVisible = phase === "done" && failTotal > 0;

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
            <Input id="batch-pdf" value={pdf} onChange={(e) => { setPdf(e.target.value); setThumbOk(true); }} placeholder="服务端可读路径" />
            <Button variant="outline" size="sm" onClick={async () => { const v = await pickFile(PDF_FILTER); if (v) { setPdf(v); setThumbOk(true); } }}>
              选 PDF…
            </Button>
          </div>
          {pdf.trim() && thumbOk && (
            // P1: thumbnail only; a missing /api/pdf/preview hides silently and never blocks OCR.
            <img
              src={pdfPreviewUrl(pdf.trim(), start + 1, 120)}
              alt="PDF 起始页预览"
              className="max-h-40 self-start rounded-md border object-contain"
              onError={() => setThumbOk(false)}
            />
          )}
          <div className="flex items-center gap-2.5">
            <Label htmlFor="batch-out">输出目录</Label>
            <Input id="batch-out" value={outdir} onChange={(e) => setOutdir(e.target.value)} />
            <Button variant="outline" size="sm" onClick={async () => { const v = await pickDir(); if (v) setOutdir(v); }}>
              选目录…
            </Button>
          </div>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="batch-start">页码范围</Label>
            <Input id="batch-start" className="w-24" inputMode="numeric" value={String(start)} onChange={(e) => { setStart(Math.max(0, Number(e.target.value) || 0)); setThumbOk(true); }} />
            <span className="text-xs text-muted-foreground">到</span>
            <Input id="batch-end" className="w-32" inputMode="numeric" value={endText} onChange={(e) => setEndText(e.target.value)} placeholder="留空=到尾页" />
          </div>
          {dry && (
            <p className="text-[13px] tabular" role="status">
              全书 {dry.total_pages} 页，本次计划 {dry.planned_pages} 页
              {(dry.done_pages ?? 0) > 0 && (
                <> · 已完成 {dry.done_pages} 页，还剩 {dry.remaining ?? "?"} 页（开始即续跑）</>
              )}
              {dry.book_dir && <><br />书目录：{dry.book_dir}</>}
            </p>
          )}
          <p className="text-xs leading-5 text-muted-foreground" role="status">
            {s.ocrPrompt.trim()
              ? `使用自定义提示词（${s.ocrPrompt.length} 字，在「参数」页编辑）`
              : "使用默认提示词（可在「参数」页自定义）"}
          </p>
        </CardContent>
        <CardFooter>
          <Button variant="outline" onClick={doDry} disabled={phase === "loading" || running}>
            规划页数
          </Button>
          <span className="ml-auto flex gap-2">
            {running ? (
              <Button variant="destructive" onClick={doCancel} disabled={phase === "stopping"}>
                {phase === "stopping" ? "正在停止…" : "取消任务"}
              </Button>
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
            <EmptyState
              icon={Layers} tone="error" title="出错了" description={error}
              actionLabel={jobId ? "继续轮询" : "重试 Dry-Run"}
              onAction={() => { if (jobId) { failCount.current = 0; schedule(jobId, 0); } else void doDry(); }}
            />
          )}
          {phase === "cancelled" && (
            <EmptyState
              icon={Layers} tone="idle" title="已取消" description="在途页已跑完，排队页已丢弃。已落盘的不再涨，重跑自动续上。"
              actionLabel="继续跑（续上）" onAction={doRun}
            />
          )}
          {progressVisible && (
            <>
              <p className="text-[13px] tabular" role="status">
                成功 {prog?.pages_ok ?? 0} 页 / 计划 {total || "?"} 页（{pct ?? 0}%）
                {" · "}tokens {prog?.tokens ?? 0} · skipped {prog?.skipped ?? 0} ·
                failed {prog?.failed ?? 0}
                {(prog?.p50_ms ?? 0) > 0 && <> · p50 {prog?.p50_ms}ms p95 {prog?.p95_ms}ms</>}
              </p>
              <Progress value={prog?.pages_ok ?? 0} max={total > 0 ? total : 100} />
              <p className="text-xs tabular text-muted-foreground" role="status">
                速度 {prog?.pages_per_min ?? 0} 页/分 · 剩余 {fmtEta(prog?.eta_ms)} · 已用 {fmtElapsed(prog?.elapsed_ms)}
                {(prog?.concurrency_current ?? 0) > 0 && <> · 并发 {prog?.concurrency_current}/{prog?.concurrency_init}</>}
                {(prog?.n429 ?? 0) > 0 && <> · 429 共 {prog?.n429} 次（已自动降并发）</>}
              </p>
              {phase === "idle" && !prog && (
                <EmptyState icon={Layers} tone="idle" title="尚未开始" description="跑的过程中自动轮询实时数字；中断后重跑自动续上。" />
              )}
              {doneVisible && summary && (
                <p className="text-[13px] text-muted-foreground">
                  {phase === "cancelled" ? "已取消：" : "完成："}tokens {String(summary.total_tokens ?? "?")}，本轮失败{" "}
                  {String(summary.failed_pages_this_run ?? "?")} 页。
                  {summary.merged_md ? ` 整本 Markdown：${String(summary.merged_md)}` : " 未合并出整本 Markdown（pages/ 下还没有页文件）。"}
                </p>
              )}
              {failListVisible && (
                <div className="flex flex-col gap-1.5">
                  <p className="text-[13px]" role="status">
                    失败页 {failTotal} 页{failPages!.length > 0 ? `（前 ${failPages!.length} 页如下）` : ""}
                    {failPages!.length > 0 && (
                      <Button variant="outline" size="sm" className="ml-2" onClick={copyFailPages}>
                        {copiedFail ? "已复制" : "复制页号"}
                      </Button>
                    )}
                    {retryFailedVisible && (
                      <Button size="sm" className="ml-2" onClick={doRetryFailed} disabled={retrying || running}>
                        {retrying ? "重试中…" : `仅重试失败页（${failTotal}）`}
                      </Button>
                    )}
                  </p>
                  {failPages!.length > 0 && (
                    <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs leading-5">
                      {failPages!.slice(0, 50).map((p) => (
                        <li key={p.pno_0based} className="break-all font-mono">
                          p{p.pno_0based}（第{p.page_number ?? p.pno_0based + 1}页）{p.http_status ? ` HTTP ${p.http_status}` : ""}{p.error ? ` ${p.error}` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
