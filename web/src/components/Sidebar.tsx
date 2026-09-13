import {
  BookOpen,
  FileText,
  Layers,
  Moon,
  Plug,
  SlidersHorizontal,
  Sun,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useSession, type ViewId } from "@/stores/session";
import { Button } from "@/components/ui/button";

const NAV: { id: ViewId; label: string; icon: typeof Plug }[] = [
  { id: "connect", label: "连接", icon: Plug },
  { id: "single", label: "单页", icon: FileText },
  { id: "batch", label: "批量", icon: Layers },
  { id: "searchable", label: "双层", icon: BookOpen },
  { id: "params", label: "参数", icon: SlidersHorizontal },
];

export function Sidebar() {
  const view = useSession((s) => s.view);
  const set = useSession((s) => s.set);
  const dark = useSession((s) => s.dark);

  return (
    <aside
      aria-label="主导航"
      className="flex w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar"
    >
      <div className="flex h-11 items-center gap-2 px-4">
        <span aria-hidden="true" className="flex size-6 items-center justify-center rounded-md bg-primary">
          <BookOpen className="size-3.5 text-primary-foreground" />
        </span>
        <span className="text-sm font-bold text-sidebar-foreground">llm-ocr</span>
      </div>
      <nav aria-label="功能视图" className="flex flex-col gap-0.5 px-2 py-1">
        {NAV.map((item) => {
          const active = view === item.id;
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() => set({ view: item.id })}
              className={cn(
                "flex h-8 items-center gap-2.5 rounded-md px-2.5 text-left text-[13px]",
                active
                  ? "bg-accent font-medium text-accent-foreground"
                  : "text-sidebar-foreground hover:bg-accent/60",
              )}
            >
              <Icon aria-hidden="true" className="size-4 shrink-0" />
              {item.label}
            </button>
          );
        })}
      </nav>
      <div className="mt-auto flex items-center justify-between border-t border-sidebar-border px-4 py-2">
        <span className="text-xs text-muted-foreground">语音学教程 OCR</span>
        <Button
          variant="ghost"
          size="icon"
          aria-label={dark ? "切换到浅色" : "切换到深色"}
          onClick={() => set({ dark: !dark })}
        >
          {dark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
        </Button>
      </div>
    </aside>
  );
}
