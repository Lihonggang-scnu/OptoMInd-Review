import { useMemo } from "react";
import { STAGE_GROUPS } from "@/lib/stageGroups.generated";
import { cn } from "@/lib/cn";

export interface StepState {
  index?: number;
  stage: string;
  label?: string;
  explain?: string;
  status?: string;
  elapsed_seconds?: number;
}

type Visual = "pending" | "running" | "completed" | "degraded" | "failed";

function normalize(status?: string): Visual {
  const value = (status ?? "pending").toLowerCase();
  if (value.includes("degrad")) return "degraded";
  if (value === "completed" || value === "done" || value === "ok") return "completed";
  if (value === "failed" || value === "error") return "failed";
  if (value === "running" || value === "in_progress") return "running";
  return "pending";
}

/** Shape+glyph differ per state so colour-blind users and video compression
 *  still tell them apart; the breathing pulse honours reduced-motion. */
const DOT_CLASS: Record<Visual, string> = {
  pending: "bg-transparent border-2 border-line",
  running: "bg-running border-2 border-running animate-pulse-dot",
  completed: "bg-success border-2 border-success",
  degraded: "bg-warning border-2 border-warning",
  failed: "bg-danger border-2 border-danger",
};
const GLYPH: Record<Visual, string> = {
  pending: "",
  running: "◉",
  completed: "✓",
  degraded: "◐",
  failed: "✕",
};

function StageDot({ visual, label }: { visual: Visual; label: string }) {
  return (
    <span className="relative inline-flex h-4 w-4 shrink-0 items-center justify-center">
      <span
        aria-hidden
        className={cn("inline-flex h-full w-full items-center justify-center rounded-full text-[9px] font-bold text-white", DOT_CLASS[visual])}
      >
        {GLYPH[visual]}
      </span>
      <span className="sr-only">{label}</span>
    </span>
  );
}

/**
 * 23 stages in 7 groups (data generated from F1 stage_registry).
 * Layout is flex-wrap per group -- no fixed column count anywhere.
 */
export function StageTrack({ steps }: { steps: StepState[] }) {
  const byName = useMemo(() => {
    const map = new Map<string, StepState>();
    for (const step of steps) map.set(step.stage, step);
    return map;
  }, [steps]);

  const runningCount = steps.filter((s) => normalize(s.status) === "running").length;

  return (
    <div>
      <div aria-live="polite" className="sr-only">
        {runningCount === 1 ? "1 个阶段进行中" : runningCount + " 个阶段进行中"}
      </div>
      <div className="flex flex-wrap gap-spacing-3">
        {STAGE_GROUPS.map((group) => (
          <section key={group.name} className="min-w-[10rem] flex-1 sm:min-w-[12rem]">
            <h3 className="mb-spacing-1 text-small text-muted">{group.name}</h3>
            <ul role="list" className="flex flex-wrap gap-spacing-1">
              {group.stages.map((def) => {
                const live = byName.get(def.name);
                const visual = normalize(live?.status);
                const label = def.label || def.name;
                const explain = live?.explain ?? def.explain;
                return (
                  <li key={def.name} className="min-w-0">
                    <div
                      tabIndex={0}
                      title={explain}
                      aria-label={label + "：" + visualStateText(visual)}
                      className={cn(
                        "flex max-w-full items-center gap-spacing-1 rounded-sm border px-spacing-2 py-1 text-small transition-colors duration-fast ease-out focus-visible:border-accent",
                        visual === "running"
                          ? "border-running/40 bg-running/5 text-foreground"
                          : visual === "pending"
                            ? "border-line bg-surface text-muted"
                            : "border-line bg-surface text-foreground",
                      )}
                    >
                      <StageDot visual={visual} label={visualStateText(visual)} />
                      <span className="truncate" title={label}>{label}</span>
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}

function visualStateText(visual: Visual): string {
  return {
    pending: "未开始",
    running: "进行中",
    completed: "已完成",
    degraded: "降级完成",
    failed: "失败",
  }[visual];
}
