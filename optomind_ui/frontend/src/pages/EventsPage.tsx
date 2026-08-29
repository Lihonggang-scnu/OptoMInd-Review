import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { PageFrame } from "@/components/layout/PageFrame";
import { EventLog, type LogRow } from "@/components/log/EventLog";
import { Button } from "@/components/ui/button";

const PAGE = 500;

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

/** Archive view: paged reads over HARNESS_EVENTS.jsonl (append-only state). */
export function EventsPage() {
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
        "/events?offset=" + nextOffsetRef.current + "&limit=" + PAGE,
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
    <PageFrame crumbs={[
      { label: "首页", to: "/" },
      { label: "详情 · " + runId, to: "/run/" + runId },
      { label: "完整事件流" },
    ]}>
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
    </PageFrame>
  );
}
