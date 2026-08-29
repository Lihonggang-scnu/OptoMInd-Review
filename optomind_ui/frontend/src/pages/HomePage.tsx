import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { PageFrame } from "@/components/layout/PageFrame";
import { AsyncSection } from "@/components/states/AsyncSection";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { confirmIntent, createTask, fetchRuns, type RunSummary } from "@/api/optomind";

/** Home: start a study; success MUST land on /task/:runId (URL = truth). */
export function HomePage() {
  const navigate = useNavigate();
  const [question, setQuestion] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runsState, setRunsState] = useState<{
    data: RunSummary[] | null;
    loading: boolean;
    error: Error | null;
  }>({ data: null, loading: true, error: null });

  const loadRuns = useCallback(async () => {
    setRunsState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const runs = await fetchRuns();
      setRunsState({ data: runs, loading: false, error: null });
    } catch (cause) {
      setRunsState({
        data: null,
        loading: false,
        error: cause instanceof Error ? cause : new Error(String(cause)),
      });
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const intent = await confirmIntent(question.trim());
      if (intent.degraded &&
          !window.confirm("意图判断服务暂不可用，已按研究类请求处理。是否继续？")) {
        return;
      }
      if (intent.verdict !== "research") {
        setError("这不是一个研究请求：" + (intent.reply ?? ""));
        return;
      }
      const { runId } = await createTask(question.trim(), intent.token ?? "");
      navigate("/task/" + runId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <PageFrame crumbs={[{ label: "首页" }]}>
      <h1 className="text-display">开始一项研究</h1>
      <form onSubmit={onSubmit} className="mt-spacing-5 flex max-w-2xl gap-spacing-3">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="输入光学领域的研究问题，例如：日间辐射制冷材料的最新进展"
          aria-label="研究问题"
          className="h-9 flex-1 rounded-md border border-line bg-surface px-spacing-3 text-body focus:border-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        />
        <Button variant="primary" type="submit" disabled={submitting || !question.trim()}>
          {submitting ? "启动中…" : "开始研究"}
        </Button>
      </form>
      {error && <p role="alert" className="mt-spacing-4 text-body text-danger">{error}</p>}

      {!question && (
        <div className="mt-spacing-4 flex flex-wrap items-center gap-spacing-2 text-small">
          <span className="text-muted">试试示例：</span>
          {[
            "介绍光学中的日间辐射致冷及其应用",
            "超表面平面透镜的研究进展综述",
            "拓扑光子学在光通信中的应用综述",
          ].map((sample) => (
            <button
              key={sample}
              type="button"
              onClick={() => setQuestion(sample)}
              className="rounded-sm border border-line bg-surface px-spacing-2 py-1 text-muted transition-colors duration-fast ease-out hover:border-accent hover:text-accent"
            >
              {sample}
            </button>
          ))}
        </div>
      )}

      <section className="mt-spacing-6">
        <h2 className="text-title">历史运行</h2>
        <div className="mt-spacing-3">
          <AsyncSection
            state={runsState}
            isEmpty={(runs) => runs.length === 0}
            emptyHint="还没有任何运行。在上方输入研究问题即可开始第一次综述。"
            onRetry={() => void loadRuns()}
            skeleton={
              <div className="space-y-spacing-3">
                <div className="h-12 w-full rounded-sm bg-line animate-pulse" />
                <div className="h-12 w-full rounded-sm bg-line animate-pulse" />
              </div>
            }
          >
            {(runs) => (
              <ul className="divide-y divide-line rounded-lg border border-line bg-surface">
                {runs.map((run) => (
                  <li key={run.run_id} className="flex items-center gap-spacing-3 p-spacing-3">
                    <Badge tone={run.status === "running" ? "running" : "neutral"}>
                      {run.status_label || "未知"}
                    </Badge>
                    {/* Selectable text: the row is NOT a button; navigation is
                        an explicit link so titles stay copyable. */}
                    <span className="min-w-0 flex-1 select-text text-small">
                      {run.question || run.run_id}
                    </span>
                    <button
                      type="button"
                      onClick={() => navigate("/run/" + run.run_id)}
                      className="shrink-0 text-small text-accent hover:underline"
                    >
                      打开详情
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </AsyncSection>
        </div>
      </section>
    </PageFrame>
  );
}
