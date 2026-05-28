"use client";
import { useEffect, useState } from "react";
import { api, PreflightCheck, PreflightResult } from "@/lib/api";

export function PreflightModal({
  projectId,
  onClose,
  onConfirm,
}: {
  projectId: number;
  onClose: () => void;
  onConfirm: (force: boolean) => void;
}) {
  const [result, setResult] = useState<PreflightResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.preflight(projectId).then(setResult).catch((e) => setError(e.message));
  }, [projectId]);

  return (
    <div className="fixed inset-0 z-30 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="card w-full max-w-lg p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="text-lg font-semibold">Preflight checks</div>
            <div className="text-xs text-ink-400 mt-1">
              Catches structural problems before we spend any provider budget.
            </div>
          </div>
          <button className="text-ink-300 hover:text-white" onClick={onClose}>
            ✕
          </button>
        </div>

        {error && <div className="text-sm text-accent">{error}</div>}
        {!result && !error && <div className="text-ink-300 text-sm">Running checks…</div>}

        {result && (
          <>
            <div className="space-y-2 mb-4">
              {result.checks.map((c) => (
                <CheckRow key={c.id} c={c} />
              ))}
            </div>

            <div className="flex items-center justify-between pt-2 border-t border-ink-800">
              <div className="text-xs text-ink-300">
                {result.summary === "ok" && "All checks passed."}
                {result.summary === "warn" && "Soft warnings — generation allowed."}
                {result.summary === "fail" && "One or more checks failed."}
              </div>
              <div className="flex gap-2">
                <button className="btn-ghost" onClick={onClose}>
                  Cancel
                </button>
                {result.summary === "fail" ? (
                  <button
                    className="btn-ghost"
                    onClick={() => onConfirm(true)}
                    title="Skip preflight and generate anyway"
                  >
                    Generate anyway
                  </button>
                ) : (
                  <button className="btn-primary" onClick={() => onConfirm(false)}>
                    Generate Video →
                  </button>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function CheckRow({ c }: { c: PreflightCheck }) {
  const tone =
    c.status === "ok"
      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
      : c.status === "warn"
      ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
      : "border-red-500/40 bg-red-500/10 text-red-300";
  const icon = c.status === "ok" ? "✓" : c.status === "warn" ? "!" : "✕";
  return (
    <div className={`flex gap-3 p-3 rounded-md border ${tone}`}>
      <div className="w-5 text-center font-semibold">{icon}</div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-ink-100">{c.label}</div>
        <div className="text-xs text-ink-200 mt-0.5">{c.message}</div>
        {c.detail && <div className="text-xs text-ink-300 mt-1">{c.detail}</div>}
      </div>
    </div>
  );
}
