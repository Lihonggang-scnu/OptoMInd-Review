import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { fetchRuns } from "@/api/optomind";
import { cn } from "@/lib/cn";

interface Command {
  id: string;
  label: string;
  hint?: string;
  run: () => boolean | void | Promise<boolean | void>;
}

function currentRunId(pathname: string): string | null {
  const match = pathname.match(/^\/(?:task|run)\/([A-Za-z0-9_]+)/);
  return match ? match[1] : null;
}

/**
 * Linear-style command palette (Ctrl/Cmd+K). Full keyboard operation:
 * type to filter, arrows / j-k to move, Enter to run, Esc to close.
 */
export function CommandPalette({ open, onClose, onRequestStop }: {
  open: boolean;
  onClose: () => void;
  onRequestStop?: (runId: string) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [runs, setRuns] = useState<{ run_id: string; question?: string }[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const runId = currentRunId(location.pathname);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    inputRef.current?.focus();
    void fetchRuns()
      .then((list) => setRuns(list.slice(0, 12)))
      .catch(() => setRuns([]));
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    void runs;
    const list: Command[] = [
      {
        id: "new-study",
        label: "新建研究",
        hint: "回首页输入研究问题",
        run: () => navigate("/"),
      },
      {
        id: "toggle-theme",
        label: "切换明暗主题",
        run: () => window.dispatchEvent(new CustomEvent("optomind:toggle-theme")),
      },
    ];
    for (const run of runs) {
      list.push({
        id: "open-" + run.run_id,
        label: "跳转运行 · " + (run.question || run.run_id),
        hint: "/run/" + run.run_id,
        run: () => navigate("/run/" + run.run_id),
      });
    }
    if (runId) {
      list.push({
        id: "decisions",
        label: "打开决策页",
        hint: "/run/" + runId + "/decisions",
        run: () => navigate("/run/" + runId + "/decisions"),
      });
      list.push({
        id: "copy-run-id",
        label: "复制 run_id",
        hint: runId,
        run: async () => {
          await navigator.clipboard.writeText(runId);
          window.dispatchEvent(new CustomEvent("optomind:toast", {
            detail: { message: "run_id 已复制", tone: "success" },
          }));
        },
      });
      list.push({
        id: "stop-run",
        label: "停止运行（危险）",
        hint: "需二次确认",
        run: () => onRequestStop?.(runId),
      });
    }
    const keyword = query.trim().toLowerCase();
    if (!keyword) return list;
    return list.filter((command) =>
      (command.label + " " + (command.hint ?? "")).toLowerCase().includes(keyword),
    );
  }, [navigate, onRequestStop, query, runId, runs]);

  useEffect(() => setActive(0), [query]);

  if (!open) return null;

  async function execute(command: Command | undefined) {
    if (!command) return;
    onClose();
    await command.run();
  }

  function onInputKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    } else if (event.key === "ArrowDown" || event.key === "j") {
      event.preventDefault();
      setActive((i) => Math.min(commands.length - 1, i + 1));
    } else if (event.key === "ArrowUp" || event.key === "k") {
      event.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      void execute(commands[active]);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 p-spacing-5 pt-[12vh]"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        className="mx-auto max-w-lg overflow-hidden rounded-lg border border-line bg-surface shadow-lg"
        onClick={(event) => event.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onInputKeyDown}
          placeholder="输入命令或运行名…（↑↓/j k 移动，Enter 执行，Esc 关闭）"
          aria-label="命令搜索"
          className="w-full border-b border-line bg-surface px-spacing-4 py-spacing-3 text-body focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
        />
        <ul role="listbox" aria-label="命令列表" className="max-h-80 overflow-auto p-spacing-2">
          {commands.map((command, index) => (
            <li key={command.id}>
              <button
                type="button"
                role="option"
                aria-selected={index === active}
                className={cn(
                  "flex w-full items-center justify-between rounded-sm px-spacing-3 py-spacing-2 text-left text-small",
                  index === active ? "bg-accent/10 text-accent" : "text-foreground hover:bg-surface-raised",
                )}
                onMouseEnter={() => setActive(index)}
                onClick={() => void execute(command)}
              >
                <span>{command.label}</span>
                {command.hint && <span className="text-muted">{command.hint}</span>}
              </button>
            </li>
          ))}
          {commands.length === 0 && (
            <li className="p-spacing-3 text-small text-muted">没有匹配的命令。</li>
          )}
        </ul>
      </div>
    </div>
  );
}
