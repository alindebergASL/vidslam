"use client";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

type ToastKind = "success" | "error" | "info";

export type Toast = {
  id: number;
  kind: ToastKind;
  message: string;
  // ms until auto-dismiss; 0 = sticky.
  duration: number;
};

type ToastApi = {
  toast: (msg: string, kind?: ToastKind, opts?: { duration?: number }) => void;
  success: (msg: string, opts?: { duration?: number }) => void;
  error: (msg: string, opts?: { duration?: number }) => void;
  info: (msg: string, opts?: { duration?: number }) => void;
};

const ToastCtx = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((cur) => cur.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback<ToastApi["toast"]>(
    (message, kind = "info", opts) => {
      const id = nextId.current++;
      const duration = opts?.duration ?? (kind === "error" ? 6000 : 3000);
      setToasts((cur) => [...cur, { id, kind, message, duration }]);
      if (duration > 0) {
        // Auto-dismiss. We don't pause on hover to keep this small; errors get longer.
        setTimeout(() => dismiss(id), duration);
      }
    },
    [dismiss]
  );

  const api: ToastApi = {
    toast,
    success: (m, o) => toast(m, "success", o),
    error: (m, o) => toast(m, "error", o),
    info: (m, o) => toast(m, "info", o),
  };

  return (
    <ToastCtx.Provider value={api}>
      {children}
      <Toaster toasts={toasts} onDismiss={dismiss} />
    </ToastCtx.Provider>
  );
}

function Toaster({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  // Avoid SSR hydration mismatch — render only after mount.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return null;
  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-50 flex flex-col gap-2 max-w-[90vw] pointer-events-none">
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className={`pointer-events-auto flex items-start gap-2 px-3 py-2 rounded-md border text-sm shadow-lg backdrop-blur ${
            t.kind === "success"
              ? "bg-emerald-500/15 border-emerald-500/40 text-emerald-100"
              : t.kind === "error"
              ? "bg-red-500/15 border-red-500/40 text-red-100"
              : "bg-ink-800/90 border-ink-700 text-ink-100"
          }`}
        >
          <span className="w-4 text-center">
            {t.kind === "success" ? "✓" : t.kind === "error" ? "✕" : "•"}
          </span>
          <span className="flex-1 min-w-0 break-words">{t.message}</span>
          <button
            className="text-ink-300 hover:text-white text-xs leading-none mt-0.5"
            onClick={() => onDismiss(t.id)}
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
