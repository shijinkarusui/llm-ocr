import * as React from "react";
import { cn } from "@/lib/utils";

export interface ProgressProps extends React.HTMLAttributes<HTMLDivElement> {
  value: number;
  max?: number;
}

function Progress({ className, value, max = 100, ...props }: ProgressProps) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={Math.round(value)}
      className={cn("h-2.5 w-full overflow-hidden rounded-full bg-secondary", className)}
      {...props}
    >
      <div
        className="h-full rounded-full bg-primary transition-[width] duration-[180ms]"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export { Progress };
