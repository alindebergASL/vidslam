"use client";
import { useState } from "react";
import { api, Render, Shot } from "@/lib/api";
import { useToast } from "@/components/Toaster";

// Friendly, ordered stages mapped from Render.status. The renderer moves through
// these in order; we show a stepper so a long mock/real render reads as progress
// rather than an opaque status string.
const STAGES: { keys: string[]; label: string; hint: string }[] = [
  { keys: ["pending", "planning"], label: "Planning", hint: "Building the storyboard" },
  { keys: ["generating_audio"], label: "Voiceover", hint: "Synthesizing or loading audio" },
  { keys: ["generating_shots", "polling"], label: "Shots", hint: "Generating each clip" },
  { keys: ["rendering"], label: "Compositing", hint: "Stitching, captions, end card" },
  { keys: ["completed"], label: "Done", hint: "Final MP4 ready" },
];

function stageIndex(status: string): number {
  for (let i = 0; i < STAGES.length; i++) {
    if (STAGES[i].keys.includes(status)) return i;
  }
  return 0;
}

export function GenerationProgress({
  render,
  shots,
  projectId,
  onRetry,
}: {
  render: Render;
  shots: Shot[];
  projectId: number;
  onRetry?: () => void;
}) {
  const failed = render.status === "failed";
  const current = stageIndex(render.status);
  const toast = useToast();
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const bodyShots = shots.filter((s) => s.shot_type !== "end_card");
  const doneShots = bodyShots.filter((s) => s.status === "completed").length;
  const shotsActive = render.status === "generating_shots" || render.status === "polling";
  const cancelled = render.status === "cancelled";
  const active = !["completed", "failed", "cancelled"].includes(render.status);

  // Clips already at the provider can't be stopped: they finish and are billed.
  const inFlight = bodyShots.filter((s) => ["submitted", "polling", "running"].includes(s.status)).length;
  const clips = (n: number) => `${n} clip${n === 1 ? "" : "s"}`;

  const cancel = async () => {
    const message =
      inFlight > 0
        ? `Cancel this render? No new clips are started, but ${clips(inFlight)} already sent to the provider will still finish and be billed. They are kept, and Resume uses them without paying again.`
        : "Cancel this render? No new clips are started. Finished clips are kept, and Resume carries on from here.";
    if (!window.confirm(message)) return;
    setCancelling(true);
    try {
      const r = await api.cancelRender(projectId);
      if (r.action === "signalled") {
        const n = r.clips_in_flight ?? 0;
        toast.info(
          n > 0
            ? `Cancelled. ${clips(n)} already at the provider will still finish and be billed; Resume uses them.`
            : "Cancelled. Finished clips are kept."
        );
      } else {
        toast.info("Nothing to cancel");
      }
    } catch (e: any) {
      toast.error(e.message || "Cancel failed");
    } finally {
      setCancelling(false);
    }
  };

  const retry = async () => {
    setRetrying(true);
    try {
      const r = await api.retryRender(projectId);
      const n = r.clips_in_flight ?? 0;
      const voice =
        r.voiceover === "new"
          ? ` A new voiceover is made too${
              r.voiceover_cost ? ` (about $${r.voiceover_cost.toFixed(2)})` : ""
            }, because the spoken script or voice changed.`
          : r.voiceover === "reuse"
          ? " The voiceover is reused, not paid for again."
          : "";
      toast.info(
        r.action === "recompose"
          ? "All clips and the voiceover are ready: putting the video together, no new charges"
          : (n > 0
              ? `Resuming: waiting for ${clips(n)} already being made (not paid for again); only missing, failed or edited shots are generated.`
              : "Resuming: only missing, failed or edited shots are generated.") + voice
      );
      onRetry?.();
    } catch (e: any) {
      toast.error(e.message || "Retry failed");
    } finally {
      setRetrying(false);
    }
  };

  return (
    <section className="card p-4 mb-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-medium">
          {failed
            ? "Generation failed"
            : cancelled
            ? "Generation cancelled"
            : render.status === "completed"
            ? "Generation complete"
            : "Generating…"}
        </h2>
        <div className="flex items-center gap-3">
          {shotsActive && bodyShots.length > 0 && (
            <span className="text-xs text-ink-300">
              {doneShots} of {bodyShots.length} shots
            </span>
          )}
          {active && (
            <button
              className="btn-ghost text-xs disabled:opacity-50"
              onClick={cancel}
              disabled={cancelling}
              title="Stops new clips from starting. Clips already sent to the provider still finish and are billed; they are kept."
            >
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1">
        {STAGES.map((stage, i) => {
          const isDone = !failed && i < current;
          const isCurrent = !failed && i === current && render.status !== "completed";
          const isComplete = render.status === "completed" && i === STAGES.length - 1;
          const reached = isDone || isCurrent || isComplete || (render.status === "completed" && i < current);
          const isFailedHere = failed && i === current;
          return (
            <div key={stage.label} className="flex-1">
              <div
                className={`h-1.5 rounded-full transition-colors ${
                  isFailedHere
                    ? "bg-red-500"
                    : reached || render.status === "completed"
                    ? "grad"
                    : "bg-ink-700"
                } ${isCurrent ? "animate-pulse" : ""}`}
              />
              <div className="mt-2">
                <div
                  className={`text-xs font-medium ${
                    isCurrent ? "text-accent" : reached || render.status === "completed" ? "text-ink-100" : "text-ink-400"
                  }`}
                >
                  {stage.label}
                </div>
                {isCurrent && <div className="text-[10px] text-ink-400 mt-0.5">{stage.hint}</div>}
              </div>
            </div>
          );
        })}
      </div>

      {/* Per-shot progress bar while shots are generating */}
      {shotsActive && bodyShots.length > 0 && (
        <div className="mt-4 flex gap-1.5">
          {bodyShots.map((s) => (
            <div
              key={s.id}
              title={`Shot ${s.shot_order}: ${s.status}`}
              className={`flex-1 h-2 rounded ${
                s.status === "completed"
                  ? "bg-emerald-500/70"
                  : s.status === "failed"
                  ? "bg-red-500/70"
                  : ["submitted", "polling"].includes(s.status)
                  ? "bg-amber-500/70 animate-pulse"
                  : "bg-ink-700"
              }`}
            />
          ))}
        </div>
      )}

      {(failed || cancelled) && (
        <div className="mt-3 space-y-2">
          {render.error && (
            <pre className="p-3 bg-ink-800 text-xs text-accent whitespace-pre-wrap rounded-sm">
              {render.error}
            </pre>
          )}
          <div className="flex items-center gap-2">
            <button className="btn-primary text-xs disabled:opacity-50" onClick={retry} disabled={retrying}>
              {retrying ? "Retrying…" : cancelled ? "Resume" : "Retry"}
            </button>
            <span className="text-[11px] text-ink-400">
              {cancelled
                ? "Carries on from here: finished clips and the voiceover are reused, clips still being made are waited for (not paid for twice), and only missing or edited shots are generated."
                : "Finished clips and an unchanged voiceover are reused. Only failed, missing or edited shots are generated."}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
