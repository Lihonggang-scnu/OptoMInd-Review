import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { PageFrame, type Crumb } from "@/components/layout/PageFrame";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { fetchNarrative, fetchProgress, type NarrativePayload } from "@/api/optomind";

function useRunCrumbs(section?: string): Crumb[] {
  const { runId = "" } = useParams();
  const crumbs: Crumb[] = [{ label: "首页", to: "/" }];
  if (section) {
    crumbs.push({ label: "详情 · " + runId, to: "/run/" + runId });
    crumbs.push({ label: section });
  } else {
    crumbs.push({ label: "详情 · " + runId });
  }
  return crumbs;
}

function RunTabs({ runId }: { runId: string }) {
  const tabs = [
    { to: "/run/" + runId, label: "概览" },
    { to: "/run/" + runId + "/visuals", label: "视觉" },
    { to: "/run/" + runId + "/decisions", label: "决策" },
    { to: "/run/" + runId + "/events", label: "事件" },
  ];
  return (
    <nav className="flex flex-wrap gap-spacing-3 border-b border-line pb-spacing-2 text-small">
      {tabs.map((tab) => (
        <Link key={tab.to} to={tab.to} className="text-muted hover:text-accent">
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}

/** Shared overview body: only human labels ever reach the screen. */
function RunOverviewBody() {
  const { runId = "" } = useParams();
  const [progress, setProgress] = useState<Record<string, unknown> | null>(null);
  const [narrative, setNarrative] = useState<NarrativePayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [p, n] = await Promise.all([
          fetchProgress(runId).catch(() => null),
          fetchNarrative(runId).catch(() => null),
        ]);
        if (!cancelled) {
          if (p) setProgress(p);
          if (n) setNarrative(n);
        }
      } catch {
        // leave placeholders; retry on next visit
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  return (
    <>
      <RunTabs runId={runId} />
      <h1 className="mt-spacing-4 text-title">运行详情：{runId}</h1>
      <Card className="mt-spacing-3">
        <CardContent>
          <div className="text-body font-medium">
            {String(narrative?.headline ?? progress?.status_label ?? "读取状态中…")}
          </div>
          {narrative?.detail && (
            <div className="mt-spacing-1 text-small text-muted">{narrative.detail}</div>
          )}
          <dl className="mt-spacing-3 grid grid-cols-2 gap-spacing-2 text-small sm:grid-cols-3">
            <div><dt className="text-muted">运行状态</dt><dd>{String(progress?.status_label ?? "—")}</dd></div>
            <div><dt className="text-muted">当前阶段</dt><dd>{String(progress?.current_label ?? "—")}</dd></div>
            <div><dt className="text-muted">事件条数</dt><dd>{String(progress?.event_count ?? "—")}</dd></div>
            <div><dt className="text-muted">已用费用</dt><dd>¥ {typeof progress?.cost_cny === "number" ? progress.cost_cny.toFixed(2) : "—"}</dd></div>
          </dl>
        </CardContent>
      </Card>
    </>
  );
}

export function RunDetailPage() {
  return (
    <PageFrame crumbs={useRunCrumbs()}>
      <RunOverviewBody />
    </PageFrame>
  );
}

export function VisualsPage() {
  const { runId = "" } = useParams();
  return (
    <PageFrame crumbs={useRunCrumbs("视觉")}>
      <RunTabs runId={runId} />
      <h1 className="mt-spacing-4 text-title">视觉产物：{runId}</h1>
      <p className="mt-spacing-2 text-body text-muted">图表画廊在 F5 接入。</p>
    </PageFrame>
  );
}

export function DecisionsPage() {
  const { runId = "" } = useParams();
  return (
    <PageFrame crumbs={useRunCrumbs("决策")}>
      <RunTabs runId={runId} />
      <h1 className="mt-spacing-4 text-title">人在回路决策：{runId}</h1>
      <p className="mt-spacing-2 text-body text-muted">决策队列在 F5 接入。</p>
    </PageFrame>
  );
}

export function EventsPage() {
  return (
    <PageFrame crumbs={useRunCrumbs("事件")}>
      <EventsInner />
    </PageFrame>
  );
}

import { EventLog, type LogRow } from "@/components/log/EventLog";

function EventsInner() {
  const { runId = "" } = useParams();
  const [rows, setRows] = useState<LogRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const nextOffsetRef = useRef(0);

  const loadOlder = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(
        "/api/runs/" + encodeURIComponent(runId) +
        "/events?offset=" + nextOffsetRef.current + "&limit=500",
      );
      if (!response.ok) throw new Error("事件读取失败（" + response.status + "）");
      const body = await response.json();
      const events: unknown[] = Array.isArray(body?.events) ? body.events : [];
      const totalLines: number | null =
        typeof body?.total_lines === "number" ? body.total_lines : null;
      setTotal(totalLines);
      const older = toRows(events, nextOffsetRef.current).reverse();
      setRows((prev) => [...older, ...prev]);
      nextOffsetRef.current += events.length;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void loadOlder();
  }, [loadOlder]);

  return (
    <>
      <div className="mb-spacing-3 flex items-center justify-between">
        <h1 className="text-title">完整事件流：{runId}</h1>
        <span className="text-small text-muted">
          已加载 {rows.length}{total != null ? " / " + total : ""} 行 · 纯追加渲染，文字可选中复制
        </span>
      </div>
      {error && (
        <p role="alert" className="mb-spacing-3 text-body text-danger">
          {error}
          <Button size="sm" className="ml-spacing-3" onClick={() => void loadOlder()}>重试</Button>
        </p>
      )}
      <EventLog rows={rows} runId={runId} heightPx={560} />
      {nextOffsetRef.current > 0 && (total == null || rows.length < total) && (
        <div className="mt-spacing-3 text-center">
          <Button size="sm" disabled={loading} onClick={() => void loadOlder()}>
            {loading ? "加载中…" : "向前加载更早事件"}
          </Button>
        </div>
      )}
    </>
  );
}

function toRows(events: unknown[], startIndex: number): LogRow[] {
  return events.map((raw, i) => {
    const row = (raw ?? {}) as Record<string, unknown>;
    const data = row.data;
    let text =
      typeof row.text === "string" ? row.text
      : typeof data === "object" && data !== null && typeof (data as { text?: string }).text === "string"
        ? (data as { text: string }).text
        : JSON.stringify(row);
    if (text.length > 400) text = text.slice(0, 400) + "…";
    return {
      key: "evt-" + (startIndex + i) + "-" + String(row.ts ?? "") + "-" + i,
      ts: typeof row.ts === "string" ? row.ts : undefined,
      event: typeof row.raw_event === "string" ? row.raw_event : undefined,
      stage_label:
        typeof row.stage_label === "string" && row.stage_label ? row.stage_label : undefined,
      text,
      truncated: row.truncated === true,
      raw_bytes: typeof row.raw_bytes === "number" ? row.raw_bytes : undefined,
      withheld_fields:
        typeof row.withheld_fields === "number" ? row.withheld_fields : undefined,
      withheld_bytes:
        typeof row.withheld_bytes === "number" ? row.withheld_bytes : undefined,
      offset: startIndex + i,
    };
  });
}
