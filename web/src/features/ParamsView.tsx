import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useSession, type Detail, type Endpoint } from "@/stores/session";
import { useLog } from "@/stores/log";
import { parseExtra } from "@/lib/utils";
import { NotationCard } from "@/features/NotationCard";
import { PromptCard } from "@/features/PromptCard";

function RadioRow<T extends string>({
  name,
  legend,
  options,
  value,
  onChange,
}: {
  name: string;
  legend: string;
  options: T[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <fieldset className="flex items-center gap-2.5">
      <legend className="w-24 shrink-0 text-[13px] leading-8 text-muted-foreground">{legend}</legend>
      {options.map((o) => (
        <label key={o} className="flex items-center gap-1.5 text-[13px]">
          <input
            type="radio"
            name={name}
            value={o}
            checked={value === o}
            onChange={() => onChange(o)}
            className="size-3.5 accent-[var(--primary)]"
          />
          {o}
        </label>
      ))}
    </fieldset>
  );
}

function SliderRow({
  id,
  label,
  hint,
  value,
  min,
  max,
  onChange,
}: {
  id: string;
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="flex items-center gap-2.5">
      <Label htmlFor={id}>{label}</Label>
      <output htmlFor={id} className="w-10 shrink-0 text-[13px] tabular">
        {value}
      </output>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-secondary accent-[var(--primary)]"
      />
      <span className="w-20 shrink-0 text-xs text-muted-foreground">{hint}</span>
    </div>
  );
}

/** Keys the llm_client actually honours (llm_client._coalesce_*, _post_json, ocr_page). */
const KNOWN_EXTRA_KEYS = new Set([
  "temperature", "top_p", "top_k", "seed", "max_tokens", "max_completion_tokens",
  "max_output_tokens", "reasoning_effort", "reasoningEffort", "reasoning", "reasoning_budget",
  "reasoning_budget_tokens", "thinking", "thinking_budget", "text", "tools", "tool_choice",
  "frequency_penalty", "presence_penalty", "stop", "stop_sequences", "stream", "system",
  "timeout", "attempts", "max_retries", "seed_optional",
]);

function warnExtra(obj: Record<string, unknown>): string[] {
  const warns: string[] = [];
  for (const k of Object.keys(obj)) {
    if (!KNOWN_EXTRA_KEYS.has(k)) warns.push(`未知键 ${k}：网关可能忽略它`);
  }
  const len = obj.max_output_tokens ?? obj.max_tokens ?? obj.max_completion_tokens;
  if (len !== undefined && typeof len === "number" && len < 30000) {
    warns.push(`输出上限 ${len} 小于默认 30000：推理模型可能只剩思考、输出为空`);
  }
  return warns;
}

export function ParamsView() {
  const s = useSession();
  const set = useSession((x) => x.set);
  const emit = useLog((x) => x.emit);
  const [note, setNote] = useState("");
  const [extraErr, setExtraErr] = useState("");
  const [extraWarns, setExtraWarns] = useState<string[]>([]);

  // P3 extra 即时校验：边打字边拦，错了标红并禁用“应用参数”。
  useEffect(() => {
    const raw = s.extraText.trim();
    if (!raw) {
      setExtraErr("");
      setExtraWarns([]);
      return;
    }
    try {
      const obj = parseExtra(s.extraText);
      setExtraErr("");
      setExtraWarns(warnExtra(obj));
    } catch (e) {
      setExtraErr(e instanceof Error ? e.message : String(e));
      setExtraWarns([]);
    }
  }, [s.extraText]);

  function apply() {
    try {
      const obj = parseExtra(s.extraText);
      const warns = warnExtra(obj);
      setNote(warns.length > 0 ? `参数已应用（${warns.length} 条警告）` : "参数已应用");
    } catch (e) {
      setNote(`透传 JSON 非法：${e instanceof Error ? e.message : e}`);
    }
    emit(
      `参数应用 endpoint=${s.endpoint} concurrency=${s.concurrency} timeout=${s.timeout} dpi=${s.dpi} retries=${s.retries}`,
      "muted",
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>运行参数</CardTitle>
          <CardDescription>这里改的是同一份会话参数，各页共用。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <RadioRow<Endpoint>
            name="ep"
            legend="请求链路"
            options={["responses", "chat", "auto"]}
            value={s.endpoint}
            onChange={(v) => set({ endpoint: v })}
          />
          <RadioRow<Detail>
            name="detail"
            legend="图片清晰度"
            options={["high", "low", "auto"]}
            value={s.detail}
            onChange={(v) => set({ detail: v })}
          />
          <SliderRow id="p-conc" label="并发" hint="默认 10" value={s.concurrency} min={1} max={20} onChange={(v) => set({ concurrency: v })} />
          <SliderRow id="p-timeout" label="超时秒" hint="默认 120" value={s.timeout} min={30} max={300} onChange={(v) => set({ timeout: v })} />
          <SliderRow id="p-dpi" label="DPI" hint="默认 200" value={s.dpi} min={100} max={300} onChange={(v) => set({ dpi: v })} />
          <SliderRow id="p-retries" label="重试" hint="" value={s.retries} min={0} max={5} onChange={(v) => set({ retries: v })} />
          {s.concurrency > 8 && (
            <p role="note" className="text-xs leading-5 text-warn">
              注意：并发大于 8 后吞吐未必再涨，注意 p95 与 429。批量 500 页以上启动时还会再问一次。
            </p>
          )}
          <div className="flex gap-2.5">
            <Label htmlFor="p-extra">透传 JSON</Label>
            <Textarea
              id="p-extra"
              rows={4}
              value={s.extraText}
              onChange={(e) => set({ extraText: e.target.value })}
              aria-describedby="p-extra-note"
              aria-invalid={extraErr ? true : undefined}
              className={extraErr ? "border-destructive" : undefined}
            />
          </div>
          {extraErr && (
            <p id="p-extra-note" role="alert" className="text-xs leading-5 text-destructive">
              透传 JSON 非法：{extraErr}
            </p>
          )}
          {!extraErr && extraWarns.map((w) => (
            <p key={w} role="note" className="text-xs leading-5 text-warn">
              {w}
            </p>
          ))}
          {note && !extraErr && (
            <p role="status" className="text-xs text-muted-foreground">
              {note}
            </p>
          )}
        </CardContent>
        <CardFooter>
          <Button onClick={apply} disabled={!!extraErr}>应用参数</Button>
        </CardFooter>
      </Card>
      <PromptCard />
      <NotationCard />
    </div>
  );
}
