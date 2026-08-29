/** Unified API client: timeout, idempotent-GET retry, error normalization. */

export interface NormalizedError {
  code: string;
  message: string;
  detail?: unknown;
  status?: number;
  existingRunId?: string;
}

export class ApiError extends Error {
  readonly normalized: NormalizedError;

  constructor(normalized: NormalizedError) {
    super(normalized.message);
    this.name = "ApiError";
    this.normalized = normalized;
  }
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  timeoutMs?: number;
  retries?: number;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function once<T>(path: string, opts: RequestOptions, signal: AbortSignal): Promise<T> {
  const method = opts.method ?? "GET";
  const response = await fetch(path, {
    method,
    headers: opts.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal,
  });
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = await response.json();
    } catch {
      detail = null;
    }
    const body = (detail ?? {}) as Record<string, unknown>;
    const error: NormalizedError = {
      code: response.status === 409 ? "conflict" : "http_" + response.status,
      message:
        typeof body.detail === "string"
          ? body.detail
          : "请求失败（HTTP " + response.status + "）",
      detail,
      status: response.status,
      existingRunId:
        typeof body.existing_run_id === "string" ? body.existing_run_id : undefined,
    };
    throw new ApiError(error);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { timeoutMs = 15000, retries = opts.method === "GET" ? 2 : 0 } = opts;
  for (let attempt = 0; ; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await once<T>(path, opts, controller.signal);
    } catch (cause) {
      const retryable =
        attempt < retries &&
        !(cause instanceof ApiError); // HTTP-level errors are final
      if (!retryable) {
        if (cause instanceof DOMException && cause.name === "AbortError") {
          throw new ApiError({ code: "timeout", message: "请求超时，请重试" });
        }
        if (cause instanceof TypeError) {
          throw new ApiError({ code: "network", message: "无法连接本地服务，请确认服务已启动" });
        }
        throw cause;
      }
      await sleep(300 * (attempt + 1)); // linear backoff, GET-only
    } finally {
      clearTimeout(timer);
    }
  }
}

export const apiGet = <T>(path: string) => apiRequest<T>(path);
export const apiPost = <T>(path: string, body: unknown) =>
  apiRequest<T>(path, { method: "POST", body });
