import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { PageFrame } from "@/components/layout/PageFrame";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { StageTrack, type StepState } from "@/components/stages/StageTrack";
import { BudgetBar, MetricCard, MetricGrid } from "@/components/metrics/MetricCard";
import { ConfirmDialog } from "@/components/dialogs/ConfirmDialog";
import { fetchNarrative, stopRun, type NarrativePayload } from "@/api/optomind";
import { useRunEvents } from "@/api/useRunEvents";
import { useToast } from "@/components/toast/Toast";

interface ProgressPayload {
  status_label?: string;
  current_label?: string;
  steps?: StepState[];
  stale?: boolean;
}

/** Live view: stage track + metrics + narrative; poll & SSE are leak-free. */
export function TaskPage() {
  const { runId = "" } = useParams();
  const { status } = useRunEvents(runId);
  const toast = useToast();
  const [progress, setProgress] = useState<ProgressPayload | null>(null);
  const [narrative, setNarrative] = useState<NarrativePayload | null>(null);
  const [paused, setPaused] = useState(false);   // 暂停刷新：不影响运行本身
  const [confirmingStop, setConfirmingStop] = useState(false);

  useEffect(() => {
    if (paused) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    async function poll() {
      try {
        const [p, n] = await Promise.all([
          fetch("/api/tasks/" + encodeURIComponent(runId) + "/progress").then((r) => (r.ok ? r.json() : null)),
          fetchNarrative(runId).catch(() => null),
        ]);
        if (!cancelled) {
          if (p) setProgress(p);
          if (n) setNarrative(n);
        }
      } catch {
        // transient; next tick retries
      }
      if (!cancelled) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [runId, paused]);

  async function doStop() {
    try {
      await stopRun(runId);
      toast.push("已发送停止指令，进程将退出。", "success");
    } catch (cause) {
      toast.push(cause instanceof Error ? cause.message : "停止失败", "error");
    } finally {
      setConfirmingStop(false);
    }
  }

  const tone =
    status === "open" ? "running" :
    status === "done" ? "success" :
    status === "reconnecting" ? "warning" : "neutral";
  const badgeText =
    status === "open" ? "已连接" :
    status === "reconnecting" ? "连接中断，正在重连…" :
    status === "paused-hidden" ? "页面隐藏，已暂停" :
    status === "done" ? "已结束" : "连接中…";

  const m = narrative?.metrics ?? {};
  const disconnected = status !== "open";
  const stale = progress?.stale === true;

  return (
    <PageFrame
      crumbs={[{ label: "首页", to: "/" }, { label: "运行中 · " + runId }]}
      actions={
        <>
          {/* 两者视觉截然不同：暂停=ghost，停止=danger 红 */}
          <Button variant="ghost" size="sm" onClick={() => setPaused(!paused)}>
            {paused ? "恢复刷新" : "暂停刷新（不影响运行）"}
          </Button>
          <Button variant="danger" size="sm" onClick={() => setConfirmingStop(true)}>
            停止运行
          </Button>
          <Badge tone={tone as never}>{badgeText}</Badge>
        </>
      }
    >
      <h1 className="text-title">正在运行：{runId}</h1>
      <Card className="mt-spacing-4">
        <CardContent>
          <StageTrack steps={progress?.steps ?? []} />
        </CardContent>
      </Card>

      {disconnected && (
        <p role="alert" className="mt-spacing-3 text-body text-warning">
          连接中断，正在重连…（指标卡暂停刷新，恢复后自动续上）
        </p>
      )}
      {stale && (
        <p className="mt-spacing-2 text-small text-muted" role="status">
          数据刷新中，以下为最近一次成功读取的值。
        </p>
      )}

      <div aria-live="polite" className="mt-spacing-4 rounded-md border border-line bg-surface-raised px-spacing-4 py-spacing-3">
        <span className="text-body font-medium">{narrative?.headline ?? progress?.status_label ?? "读取状态中…"}</span>
        {narrative?.detail && (
          <span className="ml-spacing-3 text-small text-muted">{narrative.detail}</span>
        )}
      </div>

      <div className={
        "mt-spacing-3 transition-all duration-normal ease-out" +
        ((disconnected || paused) ? " opacity-60 saturate-50" : "")
      }>
        <MetricGrid>
          <MetricCard label={paused ? "已用费用（已暂停刷新）" : "已用费用"} value={m.cost_cny} digits={2} prefix="¥" />
          <MetricCard label="剩余预算" value={m.remaining_budget_cny} digits={2} prefix="¥"
            tone={(m.remaining_budget_cny ?? 1) / (m.global_cost_budget_cny ?? 1) < 0.15 ? "danger" : "default"} />
          <BudgetBar used={m.cost_cny} budget={m.global_cost_budget_cny} />
          <MetricCard label="已入库文献" value={m.papers_ingested} suffix="篇" />
          <MetricCard label="已发起查询" value={m.total_query_count} suffix="次" />
          <MetricCard label="模型调用" value={m.model_call_count} suffix="次" />
          <MetricCard label="总墙钟" value={m.total_wall_time_seconds} digits={0} suffix="s" />
        </MetricGrid>
      </div>

      <ConfirmDialog
        open={confirmingStop}
        title="停止运行"
        body={"确定要停止 " + runId + " 吗？此操作不可撤销，已产生的费用不予退还。"}
        confirmLabel="停止运行"
        danger
        onConfirm={() => void doStop()}
        onCancel={() => setConfirmingStop(false)}
      />
    </PageFrame>
  );
}
