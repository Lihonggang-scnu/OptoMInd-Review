import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageFrame } from "@/components/layout/PageFrame";
import { AsyncSection } from "@/components/states/AsyncSection";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { apiGet } from "@/api/client";
import { useToast } from "@/components/toast/Toast";

export interface PreflightCheck {
  key: string;
  label: string;
  status: "ok" | "missing" | "degraded";
  detail: string;
  fix_hint: string;
  blocking: boolean;
}

interface PreflightPayload {
  checks: PreflightCheck[];
  ready: boolean;
  blocking_missing: string[];
}

const STATUS_BADGE: Record<PreflightCheck["status"], "success" | "danger" | "warning"> = {
  ok: "success",
  missing: "danger",
  degraded: "warning",
};

/**
 * First-run doctor (F6 change 3). Consumes GET /api/preflight (F5).
 * Blocking failures keep the user OUT of the main UI; non-blocking ones
 * can be skipped with their consequence spelled out. Never shows key
 * contents or full absolute paths -- only the relative file hint.
 */
export function OnboardingPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const [state, setState] = useState<{
    data: PreflightPayload | null;
    loading: boolean;
    error: Error | null;
  }>({ data: null, loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const data = await apiGet<PreflightPayload>("/api/preflight");
      setState({ data, loading: false, error: null });
    } catch (cause) {
      setState({
        data: null,
        loading: false,
        error: cause instanceof Error ? cause : new Error(String(cause)),
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function copyFixHint(check: PreflightCheck) {
    try {
      await navigator.clipboard.writeText(check.fix_hint);
      toast.push("修复命令已复制", "success");
    } catch {
      toast.push("复制失败，请手动选择文本", "error");
    }
  }

  return (
    <PageFrame crumbs={[{ label: "首启检查" }]}>
      <AsyncSection state={state} onRetry={() => void load()}>
        {(data) => (
          <div className="flex flex-col gap-4">
            <Card>
              <CardContent className="p-4">
                <h2 className="text-base font-semibold mb-2">开始之前：成本与耗时预期</h2>
                <ul className="text-sm leading-7 list-disc pl-5 text-[color:var(--color-text-muted,var(--foreground-muted))]">
                  <li>首次下载约 700 MB（pip 依赖 + 可选 TeX）</li>
                  <li>单次完整研究运行预算上限 ¥120，实际视题目而定</li>
                  <li>墙钟量级为数小时，可随时暂停刷新或停止</li>
                  <li>需自备 DashScope API key（放入 api_keys/qwen-api-key.txt；界面绝不显示其内容）</li>
                </ul>
              </CardContent>
            </Card>

            <ul className="flex flex-col gap-2" aria-label="前置检查结果">
              {data.checks.map((check) => (
                <li
                  key={check.key}
                  className="rounded-md border border-[--line] bg-[--surface] p-3"
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge tone={STATUS_BADGE[check.status]}>
                      {{ ok: "通过", missing: "缺失", degraded: "降级" }[check.status]}
                    </Badge>
                    <span className="font-medium text-sm">{check.label}</span>
                    {check.blocking && check.status !== "ok" && (
                      <Badge tone="danger">必需</Badge>
                    )}
                    {!check.blocking && check.status !== "ok" && (
                      <Badge tone="warning">可跳过</Badge>
                    )}
                  </div>
                  <p className="mt-1 text-sm opacity-80">{check.detail}</p>
                  {check.status !== "ok" && (
                    <div className="mt-1 flex items-center gap-2 flex-wrap">
                      <code className="text-xs px-2 py-1 rounded bg-black/10 dark:bg-white/10 break-all max-w-full">
                        {check.fix_hint || "—"}
                      </code>
                      {check.fix_hint && (
                        <Button variant="ghost" onClick={() => void copyFixHint(check)}>
                          复制命令
                        </Button>
                      )}
                      {!check.blocking && (
                        <span className="text-xs opacity-70">
                          跳过后果：将跳过 PDF 编译，仍生成 .tex/.md
                        </span>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>

            <div className="flex items-center gap-3 flex-wrap">
              <Button variant="secondary" onClick={() => void load()}>
                我已处理，重新检查
              </Button>
              <Button
                disabled={!data.ready}
                onClick={() => navigate("/")}
              >
                {data.ready ? "进入 OptoMind" : "存在必需项未就绪，暂不能进入"}
              </Button>
              {!data.ready && (
                <span className="text-sm text-[color:var(--color-danger,#dc2626)]" role="alert">
                  缺失必需项：{data.blocking_missing.join("、")}
                </span>
              )}
            </div>
          </div>
        )}
      </AsyncSection>
    </PageFrame>
  );
}
