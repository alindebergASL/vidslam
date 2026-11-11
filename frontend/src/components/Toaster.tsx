"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

type ToastKind = "success" | "error" | "info";

export type Toast = {
  id: number;
  kind: ToastKind;
  message: string;
  // ms until auto-dismiss; 0 = sticky.
  duration: number;
  action?: ToastAction;
  key?: string;
};

export type ToastAction = { label: string; href: string };

export type ToastOpts = {
  duration?: number;
  /** A link button on the toast (e.g. "Open" → /projects/12). */
  action?: ToastAction;
  /** De-dup key: a toast with the same key still on screen is replaced, not stacked. */
  key?: string;
};

type ToastApi = {
  toast: (msg: string, kind?: ToastKind, opts?: ToastOpts) => void;
  success: (msg: string, opts?: ToastOpts) => void;
  error: (msg: string, opts?: ToastOpts) => void;
  info: (msg: string, opts?: ToastOpts) => void;
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
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    const t = timers.current.get(id);
    if (t) clearTimeout(t);
    timers.current.delete(id);
    setToasts((cur) => cur.filter((x) => x.id !== id));
  }, []);

  const toast = useCallback<ToastApi["toast"]>(
    (message, kind = "info", opts) => {
      const id = nextId.current++;
      const duration = opts?.duration ?? (opts?.action ? 8000 : kind === "error" ? 6000 : 3000);
      const item: Toast = { id, kind, message, duration, action: opts?.action, key: opts?.key };
      setToasts((cur) => {
        if (!item.key) return [...cur, item];
        // Same key on screen: replace it in place (a burst of the same news
        // is one toast, not a stack).
        const old = cur.find((x) => x.key === item.key);
        if (!old) return [...cur, item];
        const t = timers.current.get(old.id);
        if (t) clearTimeout(t);
        timers.current.delete(old.id);
        return cur.map((x) => (x.id === old.id ? item : x));
      });
      if (duration > 0) {
        // Auto-dismiss. We don't pause on hover to keep this small; errors get longer.
        timers.current.set(id, setTimeout(() => dismiss(id), duration));
      }
    },
    [dismiss]
  );

  // Stable across renders, so consumers can put it in effect deps.
  const api = useMemo<ToastApi>(
    () => ({
      toast,
      success: (m, o) => toast(m, "success", o),
      error: (m, o) => toast(m, "error", o),
      info: (m, o) => toast(m, "info", o),
    }),
    [toast]
  );

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
          className={`pointer-events-auto flex items-start gap-2 px-3 py-2 rounded-md border text-sm shadow-lg backdrop-blur-sm ${
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
          <span className="flex-1 min-w-0 wrap-break-word">{t.message}</span>
          {t.action && (
            <Link
              href={t.action.href}
              aria-label={`${t.action.label}: ${t.message}`}
              className="shrink-0 text-xs font-medium underline underline-offset-2 hover:text-white"
              onClick={() => onDismiss(t.id)}
            >
              {t.action.label}
            </Link>
          )}
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
