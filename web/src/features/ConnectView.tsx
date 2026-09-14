import { useState } from "react";
import { Plug, RefreshCw, Activity, ShieldCheck } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { useSession } from "@/stores/session";
import { useLog } from "@/stores/log";
import { maskedKey } from "@/lib/utils";
import { listModels, probe, type ProbeRes } from "@/lib/engine";

type Phase = "idle" | "loading" | "done" | "error";

export function ConnectView() {
  const baseUrl = useSession((s) => s.baseUrl);
  const apiKey = useSession((s) => s.apiKey);
  const model = useSession((s) => s.model);
  const endpoint = useSession((s) => s.endpoint);
  const detail = useSession((s) => s.detail);
  const timeout = useSession((s) => s.timeout);
  const set = useSession((s) => s.set);
  const emit = useLog((s) => s.emit);
  const [phase, setPhase] = useState<Phase>("idle");
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [probeRes, setProbeRes] = useState<ProbeRes | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);

  function sync() {
    emit(`连接参数已同步 key=${maskedKey(apiKey)}`, "muted");
  }

  async function fetchModels() {
    sync();
    setPhase("loading");
    setError("");
    emit("开始：拉取模型列表", "muted");
    try {
      const data = await listModels(baseUrl, apiKey);
      setModels(data.models);
      setPhase("done");
      emit(`拉取模型列表 完成（${data.count} 个）`, "ok");
      if (data.models.length > 0 && !data.models.includes(model)) set({ model: data.models[0] });
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
      emit(`拉取模型列表 失败：${e instanceof Error ? e.message : e}`, "err");
    }
  }

  async function doProbe() {
    sync();
    setProbeBusy(true);
    emit("开始：单图探活 tests/cand_165.png", "muted");
    try {
      const r = await probe({ baseUrl, model, key: apiKey, endpoint, detail, timeout });
      setProbeRes(r);
      const toks = (r.usage?.total_tokens as number | undefined) ?? "?";
      emit(`单图探活 完成（${r.endpoint_used}，tokens ${toks}）`, "ok");
    } catch (e) {
      setProbeRes(null);
      emit(`单图探活 失败：${e instanceof Error ? e.message : e}`, "err");
    } finally {
      setProbeBusy(false);
    }
  }

  function doCheck() {
    sync();
    emit(
      `核验 base=${baseUrl} model=${model} key=${maskedKey(apiKey)} ep=${endpoint} detail=${detail}`,
      "muted",
    );
    setProbeRes(null);
    setPhase("done");
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>网关连接</CardTitle>
          <CardDescription>Key 只放内存，不写盘、不打明文日志。先同步参数，再拉模型或探活。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex items-center gap-2.5">
            <Label htmlFor="conn-base">网关地址</Label>
            <Input
              id="conn-base"
              value={baseUrl}
              placeholder="http://主机:端口/v1"
              onChange={(e) => set({ baseUrl: e.target.value })}
            />
          </div>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="conn-key">密钥（内存）</Label>
            <Input
              id="conn-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => set({ apiKey: e.target.value })}
            />
          </div>
          <div className="flex items-center gap-2.5">
            <Label htmlFor="conn-model">模型</Label>
            <Input
              id="conn-model"
              list="conn-models"
              value={model}
              onChange={(e) => set({ model: e.target.value })}
            />
            <datalist id="conn-models">
              {models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">
            模型保持 OC/ 前缀。拉取失败可手填。
          </p>
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          <Button variant="outline" onClick={sync}>同步参数</Button>
          <Button onClick={fetchModels} disabled={phase === "loading"}>
            <RefreshCw aria-hidden="true" className={phase === "loading" ? "animate-spin" : undefined} />
            拉取模型
          </Button>
          <Button variant="outline" onClick={doProbe} disabled={probeBusy}>
            <Activity aria-hidden="true" />
            {probeBusy ? "探活中…" : "单图探活"}
          </Button>
          <Button variant="ghost" onClick={doCheck}>
            <ShieldCheck aria-hidden="true" />
            脱敏核验
          </Button>
        </CardFooter>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>返回结果</CardTitle>
          <CardDescription>拉模型、探活与核验的输出都在这里。</CardDescription>
        </CardHeader>
        <CardContent>
          {phase === "idle" && !probeRes && (
            <EmptyState
              icon={Plug}
              tone="idle" title="还没有输出"
              description="点“拉取模型”从网关获取可用模型，或点“单图探活”验证链路。"
              actionLabel="拉取模型"
              onAction={fetchModels}
            />
          )}
          {phase === "loading" && (
            <div className="flex flex-col gap-2" aria-label="加载中">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          )}
          {phase === "error" && (
            <EmptyState
              icon={Plug}
              tone="error" title="拉取失败"
              description={`原因：${error}。检查网关地址与密钥后重试。`}
              actionLabel="重试"
              onAction={fetchModels}
            />
          )}
          {phase === "done" && !probeRes && models.length > 0 && (
            <div className="text-[13px] leading-6">
              <p>网关：{baseUrl}</p>
              <p className="tabular">模型数：{models.length}</p>
              <ul className="mt-1 max-h-48 list-disc overflow-y-auto pl-5">
                {models.slice(0, 40).map((m) => (
                  <li key={m} className="font-mono text-xs">{m}</li>
                ))}
              </ul>
            </div>
          )}
          {probeRes && (
            <div className="text-[13px] leading-6">
              <p>
                链路：{endpoint}（实际 {probeRes.endpoint_used}） tokens：
                {String(probeRes.usage?.total_tokens ?? "?")} 耗时：
                <span className="tabular">{probeRes.elapsed_ms}ms</span>
              </p>
              <pre className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md bg-muted p-2.5 font-mono text-xs leading-5">
                {probeRes.text.slice(0, 1500)}
              </pre>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
