import { useState } from "react";
import { FileText, Save } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { useSession } from "@/stores/session";
import { useLog } from "@/stores/log";
import { ocrImage, ocrPdfPage, ocrUrl, fileB64 } from "@/lib/engine";
import { pickFile, PDF_FILTER, IMAGE_FILTER } from "@/lib/pick";

type Kind = "image" | "pdf-page" | "image-url";
type Phase = "idle" | "loading" | "done" | "error";

async function readImageB64(path: string): Promise<string> {
  const r = await fileB64(path);
  return r.b64;
}
export function SingleView() {
  const s = useSession();
  const emit = useLog((x) => x.emit);
  const [kind, setKind] = useState<Kind>("image");
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [pno, setPno] = useState(0);
  const [phase, setPhase] = useState<Phase>("idle");
  const [markdown, setMarkdown] = useState("");
  const [preview, setPreview] = useState("");
  const [error, setError] = useState("");

  async function chooseFile() {
    const sel = await pickFile(kind === "pdf-page" ? PDF_FILTER : IMAGE_FILTER);
    if (sel) setPath(sel);
  }

  async function run() {
    setPhase("loading");
    setError("");
    setPreview("");
    emit("开始：单页 OCR", "muted");
    try {
      const p = {
        baseUrl: s.baseUrl, model: s.model, key: s.apiKey,
        endpoint: s.endpoint, detail: s.detail, timeout: s.timeout,
      };
      if (kind === "image-url") {
        if (!url.trim()) throw new Error("请填写远端 URL");
        const r = await ocrUrl(p, url.trim(), s.ocrPrompt);
        setMarkdown(r.markdown);
      } else if (kind === "pdf-page") {
        if (!path.trim()) throw new Error("请选择有效 PDF（服务端可读路径）");
        const r = await ocrPdfPage(p, path.trim(), pno, s.dpi, s.ocrPrompt);
        setMarkdown(r.markdown);
        if (r.png_b64_preview) setPreview(r.png_b64_preview);
      } else {
        if (!path.trim()) throw new Error("请选择有效图片");
        const b64 = await readImageB64(path.trim());
        const r = await ocrImage(p, b64, s.ocrPrompt);
        setMarkdown(r.markdown);
      }
      setPhase("done");
      emit("单页 OCR 完成", "ok");
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      emit(`单页 OCR 失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  function save() {
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "page.md";
    a.click();
    URL.revokeObjectURL(a.href);
    emit("已保存 page.md", "ok");
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
              <Input id="single-path" value={path} onChange={(e) => setPath(e.target.value)} placeholder="服务端可读路径" />
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
            <div className="flex items-center gap-2.5">
              <Label htmlFor="single-pno">PDF 页码</Label>
              <Input
                id="single-pno"
                className="w-24"
                inputMode="numeric"
                value={String(pno)}
                onChange={(e) => setPno(Number(e.target.value) || 0)}
              />
              <span className="text-xs text-muted-foreground">从 0 开始数</span>
            </div>
          )}
          <p role="status" className="text-xs leading-5 text-muted-foreground">
            {s.ocrPrompt.trim()
              ? `使用自定义提示词（${s.ocrPrompt.length} 字，在「参数」页编辑）`
              : "使用默认提示词（可在「参数」页自定义）"}
          </p>
          {preview && (
            <img
              src={`data:image/png;base64,${preview}`}
              alt="页面渲染预览"
              className="max-h-64 self-start rounded-md border object-contain"
            />
          )}
        </CardContent>
        <CardFooter className="justify-end">
          <Button variant="outline" onClick={save} disabled={!markdown}>
            <Save aria-hidden="true" />
            保存结果…
          </Button>
          <Button className="ml-2" onClick={run} disabled={phase === "loading"}>
            {phase === "loading" ? "识别中…" : "开始识别"}
          </Button>
        </CardFooter>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>识别结果</CardTitle>
          <CardDescription>成功显示 Markdown，失败看底部运行日志。</CardDescription>
        </CardHeader>
        <CardContent>
          {phase === "idle" && (
            <EmptyState icon={FileText} title="还没有结果" description="选好来源后点“开始识别”。" />
          )}
          {phase === "loading" && (
            <div className="flex flex-col gap-2" aria-label="加载中">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          )}
          {phase === "error" && (
            <EmptyState icon={FileText} title="识别失败" description={error} actionLabel="重试" onAction={run} />
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
