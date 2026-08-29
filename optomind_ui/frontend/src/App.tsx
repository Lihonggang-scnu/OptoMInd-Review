import { useCallback, useEffect, useState } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { HomePage } from "@/pages/HomePage";
import { TaskPage } from "@/pages/TaskPage";
import { RunDetailPage } from "@/pages/RunDetailPage";
import { VisualsPage } from "@/pages/VisualsPage";
import { DecisionsPage } from "@/pages/DecisionsPage";
import { EventsPage } from "@/pages/EventsPage";
import { OnboardingPage } from "@/pages/OnboardingPage";
import { ReplayPage } from "@/pages/ReplayPage";
import { CommandPalette } from "@/components/commands/CommandPalette";
import { ConfirmDialog } from "@/components/dialogs/ConfirmDialog";
import { ToastProvider, useToast } from "@/components/toast/Toast";
import { stopRun } from "@/api/optomind";

function AppRoutes() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [stopRunId, setStopRunId] = useState<string | null>(null);
  const staticReplay =
    document.documentElement.dataset.optomindMode === "static-replay";

  // Ctrl/Cmd+K opens the palette anywhere; j/k list nav lives in the log.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const requestStop = useCallback((runId: string) => {
    setStopRunId(runId);
  }, []);

  async function confirmStop() {
    if (!stopRunId) return;
    try {
      await stopRun(stopRunId);
      toast.push("已发送停止指令，进程将退出。", "success");
      if (location.pathname.startsWith("/task/")) {
        navigate("/run/" + stopRunId);
      }
    } catch (cause) {
      toast.push(cause instanceof Error ? cause.message : "停止失败", "error");
    } finally {
      setStopRunId(null);
    }
  }

  return (
    <>
      <Routes>
        <Route path="/" element={staticReplay ? <ReplayPage /> : <HomePage />} />
        <Route path="/task/:runId" element={<TaskPage />} />
        <Route path="/run/:runId" element={<RunDetailPage />} />
        <Route path="/run/:runId/visuals" element={<VisualsPage />} />
        <Route path="/run/:runId/decisions" element={<DecisionsPage />} />
        <Route path="/run/:runId/events" element={<EventsPage />} />
        <Route path="/onboarding" element={<OnboardingPage />} />
        <Route path="/replay" element={<ReplayPage />} />
        <Route path="*" element={staticReplay ? <ReplayPage /> : <NotFoundPage />} />
      </Routes>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onRequestStop={requestStop} />
      <ConfirmDialog
        open={stopRunId !== null}
        title="停止运行"
        body={"确定要停止 " + (stopRunId ?? "") + " 吗？此操作不可撤销，已产生的费用不予退还。"}
        confirmLabel="停止运行"
        danger
        onConfirm={() => void confirmStop()}
        onCancel={() => setStopRunId(null)}
      />
    </>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <AppRoutes />
    </ToastProvider>
  );
}

function NotFoundPage() {
  return (
    <div className="mx-auto max-w-xl p-spacing-6">
      <h1 className="text-display">404</h1>
      <p className="mt-spacing-2 text-body text-muted">页面不存在。</p>
    </div>
  );
}
