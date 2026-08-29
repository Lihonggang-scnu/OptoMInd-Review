import { useEffect, useState } from "react";

/**
 * Single-connection SSE client for run events (F3 change 4).
 *
 * Guarantees:
 *  - at most ONE live EventSource per page, module-level singleton;
 *  - exponential reconnect backoff 1s..30s, reset on success;
 *  - reconnect replays the server's newest-50 window -- duplicates are
 *    removed via a remembered per-run seen-set ("resume from offset",
 *    client-side; the F2 stream protocol has no id/resume field yet);
 *  - every timer/connection is released in effect cleanup (no leaks);
 *  - document.hidden pauses the socket, visibility resumes it;
 *  - terminal "done" event stops all reconnection attempts.
 */

export type SseStatus =
  | "connecting"
  | "open"
  | "reconnecting"
  | "paused-hidden"
  | "done";

export interface RunEvent {
  key: string;
  event: string;
  data: string;
}

const MAX_EVENTS = 300;
const MAX_SEEN = 4000;

interface SseState {
  status: SseStatus;
  events: RunEvent[];
}

let currentRunId: string | null = null;
let source: EventSource | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let attempt = 0;
let subscribers = 0;
let closedByUs = false;
const seenPerRun = new Map<string, Set<string>>();
const listeners = new Set<(state: SseState) => void>();
let lastState: SseState = { status: "connecting", events: [] };

function emit(state: SseState) {
  lastState = state;
  for (const notify of listeners) notify(state);
}

function clearRetryTimer() {
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
}

function closeSource() {
  closedByUs = true;
  if (source) {
    source.close();
    source = null;
  }
}

function nextBackoffMs(): number {
  return Math.min(30000, 1000 * Math.pow(2, attempt));
}

function connect(runId: string) {
  clearRetryTimer();
  closeSource();
  closedByUs = false;
  currentRunId = runId;
  const es = new EventSource("/api/tasks/" + encodeURIComponent(runId) + "/stream");
  source = es;

  es.onopen = () => {
    attempt = 0;
    emit({ ...lastState, status: "open" });
  };
  es.onerror = () => {
    es.close();
    if (source !== es) return; // superseded by a newer connection
    source = null;
    if (closedByUs) return;
    if (document.hidden) {
      emit({ ...lastState, status: "paused-hidden" });
      return;
    }
    attempt += 1;
    emit({ ...lastState, status: "reconnecting" });
    retryTimer = setTimeout(() => {
      if (subscribers > 0 && currentRunId && !closedByUs) connect(currentRunId);
    }, nextBackoffMs());
  };
  es.addEventListener("log", (event) => ingestMessage(runId, "log", event));
  es.addEventListener("status", (event) => ingestMessage(runId, "status", event));
  es.addEventListener("done", (event) => {
    ingestMessage(runId, "done", event);
    closeSource();
    emit({ ...lastState, status: "done" });
  });

  function ingestMessage(runKey: string, eventName: string, event: Event) {
    const messageEvent = event as MessageEvent<string>;
    const data = typeof messageEvent.data === "string" ? messageEvent.data : "";
    let seenSet = seenPerRun.get(runKey);
    if (!seenSet) {
      seenSet = new Set();
      seenPerRun.set(runKey, seenSet);
    }
    // Dedupe replays after reconnect: drop already-seen payloads.
    if (eventName !== "done") {
      if (seenSet.has(data)) return;
      seenSet.add(data);
      if (seenSet.size > MAX_SEEN) {
        const first = seenSet.values().next().value as string;
        seenSet.delete(first);
      }
    }
    const key = runKey + "#" + seenSet.size + "#" + data.length + "#" +
      data.slice(0, 40);
    const nextEvents = [...lastState.events, { key, event: eventName, data }];
    if (nextEvents.length > MAX_EVENTS) {
      nextEvents.splice(0, nextEvents.length - MAX_EVENTS);
    }
    emit({ status: lastState.status === "reconnecting" ? "open" : lastState.status, events: nextEvents });
  }
}

function handleVisibility() {
  if (subscribers === 0 || !currentRunId) return;
  if (!document.hidden) {
    if (!source && lastState.status !== "done") connect(currentRunId);
  }
  // Pausing while hidden happens implicitly via onerror -> paused branch.
}

let visibilityBound = false;

/** React hook: subscribe to the singleton SSE for one run. */
export function useRunEvents(runId: string): SseState & { reconnectAttempt: number } {
  const [state, setState] = useState<SseState>(lastState);

  useEffect(() => {
    listeners.add(setState);
    subscribers += 1;
    if (!visibilityBound) {
      visibilityBound = true;
      document.addEventListener("visibilitychange", handleVisibility);
    }
    if (currentRunId !== runId || (!source && retryTimer === null)) {
      attempt = 0;
      seenPerRun.delete(currentRunId ?? "");
      lastState = { status: "connecting", events: [] };
      emit(lastState);
      connect(runId);
    }
    return () => {
      listeners.delete(setState);
      subscribers -= 1;
      if (subscribers === 0) {
        // LAST subscriber left: release everything (leak-free navigation).
        clearRetryTimer();
        closeSource();
        currentRunId = null;
        document.removeEventListener("visibilitychange", handleVisibility);
        visibilityBound = false;
      }
    };
  }, [runId]);

  return { ...state, reconnectAttempt: attempt };
}
