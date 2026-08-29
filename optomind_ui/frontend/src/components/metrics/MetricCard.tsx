import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { useAnimatedNumber } from "@/hooks/useAnimatedNumber";
import { cn } from "@/lib/cn";

function formatNumber(value: number, digits: number): string {
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function MetricCard({
  label,
  value,
  digits = 0,
  prefix,
  suffix,
  tone,
  hint,
}: {
  label: string;
  value?: number;
  digits?: number;
  prefix?: string;
  suffix?: string;
  tone?: "default" | "warning" | "danger" | "success";
  hint?: ReactNode;
}) {
  const animated = useAnimatedNumber(value);
  const toneClass =
    tone === "danger" ? "text-danger"
    : tone === "warning" ? "text-warning"
    : tone === "success" ? "text-success"
    : "text-foreground";
  return (
    <Card className="p-spacing-4">
      <div className="text-small text-muted">{label}</div>
      <div className={cn("mt-spacing-1 font-semibold tabular-nums text-title", toneClass)}>
        {animated === undefined ? (
          <span className="text-muted">—</span>
        ) : (
          <>
            {prefix}
            {formatNumber(animated, digits)}
            {suffix && <span className="ml-1 text-small text-muted">{suffix}</span>}
          </>
        )}
      </div>
      {hint && <div className="mt-spacing-1 text-small text-muted">{hint}</div>}
    </Card>
  );
}

/** Budget consumption bar; crosses into warning near the ceiling. */
export function BudgetBar({ used, budget }: { used?: number; budget?: number }) {
  if (used === undefined || budget === undefined || budget <= 0) return null;
  const ratio = Math.min(1, used / budget);
  const pct = Math.round(ratio * 1000) / 10;
  const tone = ratio >= 0.95 ? "bg-danger" : ratio >= 0.8 ? "bg-warning" : "bg-accent";
  return (
    <div className="col-span-full">
      <div className="flex items-center justify-between text-small text-muted">
        <span>预算消耗</span>
        <span className="tabular-nums">{pct.toFixed(1)}%</span>
      </div>
      <div
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="费用预算消耗比例"
        className="mt-spacing-1 h-2 w-full overflow-hidden rounded-sm bg-line"
      >
        <div className={cn("h-full rounded-sm transition-all duration-normal ease-out", tone)} style={{ width: pct + "%" }} />
      </div>
    </div>
  );
}

export function MetricGrid({ children }: { children: ReactNode }) {
  return (
    <div className="grid grid-cols-2 gap-spacing-3 sm:grid-cols-3 lg:grid-cols-6">
      {children}
    </div>
  );
}
