import { cn } from "@/lib/cn";

/** Loading placeholder -- skeleton, never a spinner (F3 three-state rule). */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        "animate-pulse rounded-sm bg-line",
        className,
      )}
    />
  );
}
