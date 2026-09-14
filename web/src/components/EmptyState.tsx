import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

export type EmptyTone = "idle" | "error";

const TONE_IMG: Record<EmptyTone, string> = {
  idle: "/illustrations/ill-idle.jpg",
  error: "/illustrations/ill-error.jpg",
};

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  tone?: EmptyTone;
}

export function EmptyState({ icon: Icon, title, description, actionLabel, onAction, tone }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-10 text-center">
      {tone ? (
        <img
          src={TONE_IMG[tone]}
          alt=""
          aria-hidden="true"
          loading="lazy"
          className="h-28 w-auto max-w-full rounded-lg border object-cover"
        />
      ) : (
        <span aria-hidden="true" className="flex size-10 items-center justify-center rounded-full bg-muted">
          <Icon className="size-5 text-muted-foreground" />
        </span>
      )}
      <p className="text-sm font-bold">{title}</p>
      <p className="max-w-md text-[13px] leading-6 text-muted-foreground">{description}</p>
      {actionLabel && onAction && (
        <Button variant="outline" size="sm" className="mt-1" onClick={onAction}>
          {actionLabel}
        </Button>
      )}
    </div>
  );
}
