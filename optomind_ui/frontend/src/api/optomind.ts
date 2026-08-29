import { apiGet, apiPost, type NormalizedError } from "./client";

export interface RunSummary {
  run_id: string;
  question?: string;
  status?: string;
  status_label?: string;
  created_at?: string;
  cost_cny?: number;
}

export async function fetchRuns(): Promise<RunSummary[]> {
  const data = await apiGet<unknown>("/api/runs");
  // Server returns a list of run dicts; tolerate wrapper shapes.
  if (Array.isArray(data)) return data as RunSummary[];
  if (data && typeof data === "object" && Array.isArray((data as { runs?: unknown }).runs)) {
    return (data as { runs: RunSummary[] }).runs;
  }
  return [];
}

export interface IntentResult {
  verdict: "research" | "self" | "irrelevant";
  display_title?: string;
  reply?: string;
  confidence?: number;
  degraded?: boolean;
  token?: string;
}

export async function confirmIntent(question: string): Promise<IntentResult> {
  return apiGet<IntentResult>("/api/intent?question=" + encodeURIComponent(question));
}

export interface NarrativePayload {
  headline?: string;
  detail?: string;
  metrics?: Record<string, number>;
  run_id?: string;
}

export async function fetchNarrative(runId: string): Promise<NarrativePayload> {
  return apiGet<NarrativePayload>("/api/runs/" + encodeURIComponent(runId) + "/narrative");
}

export async function fetchProgress(runId: string): Promise<Record<string, unknown>> {
  return apiGet<Record<string, unknown>>(
    "/api/tasks/" + encodeURIComponent(runId) + "/progress",
  );
}

export async function stopRun(runId: string): Promise<void> {
  await apiPost("/api/tasks/" + encodeURIComponent(runId) + "/stop", {});
}

export async function createTask(
  question: string,
  intentToken: string,
): Promise<{ runId: string }> {
  try {
    const body = await apiPost<{ run_id: string }>("/api/tasks", {
      question,
      intent_token: intentToken,
    });
    return { runId: body.run_id };
  } catch (cause) {
    const normalized = (cause as { normalized?: NormalizedError }).normalized;
    if (normalized?.status === 409 && normalized.existingRunId) {
      return { runId: normalized.existingRunId }; // single-flight: rejoin
    }
    throw cause;
  }
}
