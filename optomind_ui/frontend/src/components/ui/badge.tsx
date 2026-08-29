import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export type BadgeTone = "neutral" | "success" | "warning" | "danger" | "running";

const toneClasses: Record<BadgeTone, string> = {
  neutral: "bg-surface-raised text-muted border-line",
  success: "bg-success/10 text-success border-success/30",
  warning: "bg-warning/10 text-warning border-warning/30",
  danger: "bg-danger/10 text-danger border-danger/30",
  running: "bg-running/10 text-running border-running/30",
};

export function Badge({
  tone = "neutral",
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm border px-spacing-2 py-0",
        "text-small leading-[1.25rem]",
        toneClasses[tone],
        className,
      )}
      {...props}
    />
  );
}
