import { useEffect, useRef, type ReactNode } from "react";
import { Button } from "@/components/ui/button";

/**
 * Modal confirm: focus lands on CANCEL by default (danger-op rule),
 * Esc cancels, Enter confirms; focus returns to the opener afterwards.
 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "确定",
  danger = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    cancelRef.current?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onCancel();
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      restoreRef.current?.focus();
    };
  }, [open, onCancel]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-spacing-5">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-full max-w-md rounded-lg border border-line bg-surface p-spacing-5 shadow-lg"
      >
        <h2 className="text-title">{title}</h2>
        <div className="mt-spacing-2 text-body text-muted">{body}</div>
        <div className="mt-spacing-5 flex justify-end gap-spacing-2">
          <Button ref={cancelRef} onClick={onCancel}>取消</Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
