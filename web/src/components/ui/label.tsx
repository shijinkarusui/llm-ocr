import * as React from "react";
import { cn } from "@/lib/utils";

function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn("w-24 shrink-0 text-[13px] leading-8 text-muted-foreground", className)}
      {...props}
    />
  );
}

export { Label };
