import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

export interface LogRow {
  key: string;
  ts?: string;
  event?: string;
  stage_label?: string;
  text: string;
  truncated?: boolean;
  raw_bytes?: number;
  withheld_fields?: number;
  withheld_bytes?: number;
  offset?: number; // archive mode: position in HARNESS_EVENTS.jsonl
}

function formatBytes(size: number): string {
  if (size >= 1024 * 1024) return (size / (1024 * 1024)).toFixed(1) + " MB";
  if (size >= 1024) return (size / 1024).toFixed(1) + " KB";
  return size + " 字节";
}

type Level = "info" | "warn" | "error" | "stage";

function levelOf(row: LogRow): Level {
  const name = (row.event ?? "").toLowerCase();
  if (name.includes("error") || name.includes("fail")) return "error";
  if (name.includes("warn") || name.includes("degrad") || name.includes("recover")) return "warn";
  if (row.stage_label || name.startsWith("stage")) return "stage";
  return "info";
}

const LEVEL_CLASS: Record<Level, string> = {
  info: "text-muted",
  warn: "text-warning",
  error: "text-danger",
  stage: "text-accent",
};
const LEVEL_TEXT: Record<Level, string> = {
  info: "信息", warn: "警告", error: "错误", stage: "阶段",
};

const timeFormatter = new Intl.DateTimeFormat("zh-CN", { hour12: false });

function formatTs(ts: string | undefined, relative: boolean): string {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  if (!relative) return timeFormatter.format(date);
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return seconds + " 秒前";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return minutes + " 分钟前";
  return Math.round(minutes / 60) + " 小时前";
}

/**
 * Virtualized, APPEND-ONLY event log.
 * Rows are passed in by the parent and only ever grow -- this component never
 * rebuilds the list DOM (the F2-era full-list rewrite on every poll tick is
 * the forbidden pattern; the exit gate greps for its API name). Text is plain
 * selectable content.
 */
export function EventLog({
  rows,
  runId,
  heightPx = 420,
}: {
  rows: LogRow[];
  runId: string;
  heightPx?: number;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [newCount, setNewCount] = useState(0);
  const lastLenRef = useRef(0);
  const [relativeTs, setRelativeTs] = useState(false);
  const [levelFilter, setLevelFilter] = useState<"all" | Level>("all");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Record<string, string>>({});
  const [loadingExpand, setLoadingExpand] = useState<Record<string, boolean>>({});

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (levelFilter !== "all" && levelOf(row) !== levelFilter) return false;
      if (keyword && !(
        row.text.toLowerCase().includes(keyword) ||
        (row.event ?? "").toLowerCase().includes(keyword) ||
        (row.stage_label ?? "").includes(keyword)
      )) return false;
      return true;
    });
  }, [rows, levelFilter, query]);

  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 28,
    overscan: 12,
  });

  // Sticky-bottom bookkeeping: pure append => remember how many new rows
  // arrived while the user scrolled away; never touch scrollTop otherwise.
  useEffect(() => {
    if (rows.length > lastLenRef.current) {
      const delta = rows.length - lastLenRef.current;
      lastLenRef.current = rows.length;
      if (follow && parentRef.current) {
        parentRef.current.scrollTop = parentRef.current.scrollHeight;
      } else {
        setNewCount((n) => n + delta);
      }
    }
  }, [rows.length, follow]);

  function onScroll() {
    const el = parentRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
    setFollow(atBottom);
    if (atBottom) setNewCount(0);
  }

  async function expandRow(row: LogRow) {
    if (row.offset === undefined || expanded[row.key]) return;
    setLoadingExpand((prev) => ({ ...prev, [row.key]: true }));
    try {
      const response = await fetch(
        "/api/runs/" + encodeURIComponent(runId) +
        "/events?offset=" + row.offset + "&limit=1",
      );
      const body = await response.json();
      const events = Array.isArray(body?.events) ? body.events : [];
      setExpanded((prev) => ({
        ...prev,
        [row.key]: JSON.stringify(events[0] ?? {}, null, 2),
      }));
    } finally {
      setLoadingExpand((prev) => ({ ...prev, [row.key]: false }));
    }
  }

  async function copyText(text: string, description: string) {
    try {
      await navigator.clipboard.writeText(text);
      window.dispatchEvent(new CustomEvent("optomind:toast", {
        detail: { message: description + "已复制", tone: "success" },
      }));
    } catch {
      // clipboard unavailable (permissions); silent no-op is acceptable here
    }
  }

  return (
    <div className="flex min-h-0 flex-col">
      <div className="mb-spacing-2 flex flex-wrap items-center gap-spacing-2">
        {(["all", "info", "stage", "warn", "error"] as const).map((level) => (
          <button
            key={level}
            type="button"
            onClick={() => setLevelFilter(level)}
            className={cn(
              "rounded-sm border px-spacing-2 py-0.5 text-small transition-colors duration-fast ease-out",
              levelFilter === level
                ? "border-accent text-accent"
                : "border-line bg-surface text-muted hover:text-foreground",
            )}
          >
            {level === "all" ? "全部" : LEVEL_TEXT[level]}
          </button>
        ))}
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="关键词过滤…"
          aria-label="日志关键词过滤"
          className="h-7 w-40 rounded-sm border border-line bg-surface px-spacing-2 text-small focus:border-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        />
        <Button variant="ghost" size="sm" onClick={() => setRelativeTs(!relativeTs)}>
          {relativeTs ? "绝对时间" : "相对时间"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => copyText(filtered.map((r) => r.text).join("\n"), "可见日志")}
        >
          复制全部可见
        </Button>
      </div>

      <div className="relative min-h-0 flex-1">
        <div
          ref={parentRef}
          onScroll={onScroll}
          style={{ height: heightPx }}
          role="list"
          aria-label="事件日志"
          className="overflow-auto rounded-md border border-line bg-surface p-spacing-2 font-mono-token text-mono"
        >
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((item) => {
              const row = filtered[item.index];
              const level = levelOf(row);
              return (
                <div
                  key={row.key}
                  role="listitem"
                  data-index={item.index}
                  ref={virtualizer.measureElement}
                  className="group absolute left-0 top-0 w-full px-spacing-1"
                  style={{ transform: "translateY(" + item.start + "px)" }}
                >
                  <div className="flex items-start gap-spacing-2 py-0.5 select-text">
                    {row.ts && (
                      <span className="shrink-0 text-[11px] text-muted/80 tabular-nums">
                        {formatTs(row.ts, relativeTs)}
                      </span>
                    )}
                    {row.stage_label && (
                      <span className="shrink-0">[{row.stage_label}]</span>
                    )}
                    <span className={cn("min-w-0 flex-1 break-all whitespace-pre-wrap", LEVEL_CLASS[level])}>
                      {row.text}
                    </span>
                    {row.truncated && row.offset !== undefined && !expanded[row.key] && (
                      <button
                        type="button"
                        className="shrink-0 text-small text-accent underline-offset-2 hover:underline"
                        onClick={() => void expandRow(row)}
                      >
                        {loadingExpand[row.key]
                          ? "加载中…"
                          : "展开原始 JSON（" + (row.raw_bytes ?? "?") + " 字节）"}
                      </button>
                    )}
                    {row.withheld_bytes !== undefined && row.withheld_bytes > 0 && (
                      <span
                        className="shrink-0 text-small text-muted/70"
                        title={
                          "本行有 " +
                          (row.withheld_fields ?? 0) +
                          " 个大字段（如 selection/evidence）未随事件流下发，" +
                          "以免多兆字节载荷进入前端；原始数据仍在运行目录中。"
                        }
                      >
                        已省略大字段 {formatBytes(row.withheld_bytes)}
                      </span>
                    )}
                    <button
                      type="button"
                      tabIndex={-1}
                      aria-hidden
                      className="invisible shrink-0 text-small text-muted group-hover:visible hover:text-accent"
                      onClick={() => void copyText(row.text, "本行")}
                    >
                      复制
                    </button>
                  </div>
                  {expanded[row.key] && (
                    <pre className="my-spacing-1 max-h-48 overflow-auto rounded-sm bg-surface-raised p-spacing-2 text-[11px] select-text">
                      {expanded[row.key]}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {!follow && newCount > 0 && (
          <Button
            variant="primary"
            size="sm"
            className="absolute bottom-spacing-3 right-spacing-4 shadow-md"
            onClick={() => {
              setFollow(true);
              setNewCount(0);
              const el = parentRef.current;
              if (el) el.scrollTop = el.scrollHeight;
            }}
          >
            跳到最新（{newCount} 条新消息）
          </Button>
        )}
      </div>
    </div>
  );
}
