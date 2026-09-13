import { useState } from "react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { FileWarning } from "lucide-react";
import { useLog } from "@/stores/log";
import { notationCheck, promptText, type NotationRes } from "@/lib/engine";
import { pickDir } from "@/lib/pick";

type Phase = "idle" | "loading" | "done" | "error";

export function NotationCard() {
  const emit = useLog((s) => s.emit);
  const [phase, setPhase] = useState<Phase>("idle");
  const [res, setRes] = useState<NotationRes | null>(null);
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");

  async function check() {
    const dir = await pickDir();
    if (!dir) return;
    setPhase("loading");
    setError("");
    emit("开始：全书标号校验", "muted");
    try {
      const r = await notationCheck(dir);
      setRes(r);
      setPhase("done");
      emit(`标号校验完成：${r.files} 文件，${r.total} 问题`, "ok");
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      emit(`校验失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  async function showPrompt() {
    setPhase("loading");
    try {
      const r = await promptText();
      setPrompt(r.prompt.slice(0, 3000));
      setPhase("done");
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>标号校验 / 提示词</CardTitle>
        <CardDescription>校验读 pages/page_*.md；提示词是 prompts/ocr_system.md 原文。</CardDescription>
      </CardHeader>
      <CardContent>
        {phase === "idle" && (
          <EmptyState
            icon={FileWarning}
            title="还没有输出"
            description="“看提示词”预览系统提示词，“标号校验”检查公式标号。"
          />
        )}
        {phase === "loading" && (
          <div className="flex flex-col gap-2" aria-label="加载中">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}
        {phase === "error" && (
          <EmptyState icon={FileWarning} title="出错了" description={error} actionLabel="重试" onAction={check} />
        )}
        {phase === "done" && res && (
          <div className="text-[13px] leading-6">
            <p className="tabular">文件：{res.files} 问题：{res.total}</p>
            <p className="break-all">高频：{JSON.stringify(res.codes)}</p>
            <p>问题最多的页：</p>
            <ul className="list-disc pl-5">
              {res.worst.map((w) => (
                <li key={w.page} className="tabular">{w.page} 有 {w.issues} 处</li>
              ))}
            </ul>
          </div>
        )}
        {phase === "done" && !res && prompt && (
          <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-xs leading-5">
            {prompt}
          </pre>
        )}
      </CardContent>
      <CardFooter>
        <Button variant="outline" onClick={showPrompt}>看提示词</Button>
        <Button variant="outline" className="ml-2" onClick={check}>标号校验…</Button>
      </CardFooter>
    </Card>
  );
}
