import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useLog, type LogKind } from "@/stores/log";
import { Button } from "@/components/ui/button";

/* 日志区两主题都是深底，所以固定用 console-* 前景色，不跟正文色走 */
const KIND_CLASS: Record<LogKind, string> = {
  info: "text-console-foreground",
  ok: "text-console-ok",
  warn: "text-console-warn",
  err: "text-console-err",
  muted: "text-console-muted",
};

export function LogConsole() {
  const entries = useLog((s) => s.entries);
  const emit = useLog((s) => s.emit);
  const clear = useLog((s) => s.clear);
  const [open, setOpen] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = boxRef.current;
    if (el && open) el.scrollTop = el.scrollHeight;
  }, [entries.length, open]);

  useEffect(() => {
    emit("就绪。先在“连接”页同步参数并探活，再跑单页或批量。Key 只驻内存，不落盘。", "muted");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section aria-label="运行日志" className="shrink-0 border-t border-border bg-card">
      <div className="flex h-8 items-center gap-2 px-4">
        <span className="text-xs font-medium">运行日志</span>
        <span className="text-xs text-muted-foreground">Key 仅驻内存，不落盘</span>
        <span className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="icon" className="size-6" aria-label="清空日志" onClick={clear}>
            <Trash2 aria-hidden="true" className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            aria-label={open ? "折叠日志" : "展开日志"}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? <ChevronDown aria-hidden="true" className="size-3.5" /> : <ChevronUp aria-hidden="true" className="size-3.5" />}
          </Button>
        </span>
      </div>
      {open && (
        <div
          ref={boxRef}
          tabIndex={0}
          role="log"
          aria-label="运行日志内容"
          className="h-28 overflow-y-auto bg-console px-4 py-2 font-mono text-xs leading-5"
        >
          {entries.length === 0 && <p className="text-console-muted">暂无日志。</p>}
          {entries.map((e) => (
            <p key={e.id} className={cn("whitespace-pre-wrap break-words", KIND_CLASS[e.kind])}>
              <span className="text-console-muted">{e.time}  </span>
              {e.msg}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}
