import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
}

/**
 * Three mandatory states for every async region (F3 change 3):
 * loading -> skeleton (never a spinner), empty -> reason + next action,
 * error -> cause + retry. Never a silent blank area.
 */
export function AsyncSection<T>({
  state,
  isEmpty,
  emptyHint,
  onRetry,
  skeleton,
  children,
}: {
  state: AsyncState<T>;
  isEmpty?: (data: T) => boolean;
  emptyHint?: ReactNode;
  onRetry?: () => void;
  skeleton?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (state.loading) {
    return (
      <div aria-busy="true" aria-label="加载中">
        {skeleton ?? <Skeleton className="h-24 w-full" />}
      </div>
    );
  }
  if (state.error) {
    return (
      <Card>
        <CardContent>
          <p className="text-body text-danger">加载失败：{state.error.message}</p>
          {onRetry && (
            <Button size="sm" className="mt-spacing-3" onClick={onRetry}>
              重试
            </Button>
          )}
        </CardContent>
      </Card>
    );
  }
  if (state.data === null || (isEmpty && isEmpty(state.data))) {
    return (
      <Card>
        <CardContent>
          <p className="text-body text-muted">
            {emptyHint ?? "暂无数据。"}
          </p>
        </CardContent>
      </Card>
    );
  }
  return <>{children(state.data)}</>;
}
