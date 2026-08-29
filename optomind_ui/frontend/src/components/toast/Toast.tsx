import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface ToastItem {
  id: number;
  message: string;
  tone: "success" | "error" | "info";
}

interface ToastApi {
  push: (message: string, tone?: ToastItem["tone"]) => void;
}

const ToastContext = createContext<ToastApi>({ push: () => undefined });

let nextId = 1;

/** Right-bottom stack; errors persist until dismissed, others auto-expire. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((item) => item.id !== id));
    const timer = timers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback((message: string, tone: ToastItem["tone"] = "info") => {
    const id = nextId++;
    setItems((prev) => [...prev.slice(-4), { id, message, tone }]);
    if (tone !== "error") {
      timers.current.set(id, setTimeout(() => dismiss(id), 3500));
    }
  }, [dismiss]);

  useEffect(() => {
    function onExternal(event: Event) {
      const detail = (event as CustomEvent<{ message?: string; tone?: string }>).detail;
      if (detail?.message) {
        push(detail.message, detail.tone === "success" ? "success" : "info");
      }
    }
    window.addEventListener("optomind:toast", onExternal);
    return () => {
      window.removeEventListener("optomind:toast", onExternal);
      for (const timer of timers.current.values()) clearTimeout(timer);
      timers.current.clear();
    };
  }, [push]);

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div
        aria-live="polite"
        className="fixed bottom-spacing-4 right-spacing-4 z-50 flex w-72 flex-col gap-spacing-2"
      >
        {items.map((item) => (
          <div
            key={item.id}
            role={item.tone === "error" ? "alert" : "status"}
            className={cn(
              "flex items-start gap-spacing-2 rounded-md border p-spacing-3 text-small shadow-md transition-all duration-fast ease-out",
              item.tone === "error" && "border-danger/40 bg-surface-raised text-danger",
              item.tone === "success" && "border-success/40 bg-surface-raised text-success",
              item.tone === "info" && "border-line bg-surface-raised text-foreground",
            )}
          >
            <span className="min-w-0 flex-1 break-words">{item.message}</span>
            <button
              type="button"
              aria-label="关闭提示"
              className="shrink-0 text-muted hover:text-foreground"
              onClick={() => dismiss(item.id)}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  return useContext(ToastContext);
}
