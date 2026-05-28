"use client";
import { Render, Shot } from "@/lib/api";

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
}: {
  render: Render;
  shots: Shot[];
}) {
  const failed = render.status === "failed";
  const current = stageIndex(render.status);

  const bodyShots = shots.filter((s) => s.shot_type !== "end_card");
  const doneShots = bodyShots.filter((s) => s.status === "completed").length;
  const shotsActive = render.status === "generating_shots" || render.status === "polling";

  return (
    <section className="card p-4 mb-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-medium">
          {failed ? "Generation failed" : render.status === "completed" ? "Generation complete" : "Generating…"}
        </h2>
        {shotsActive && bodyShots.length > 0 && (
          <span className="text-xs text-ink-300">
            {doneShots} of {bodyShots.length} shots
          </span>
        )}
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
                    ? "bg-accent"
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

      {failed && render.error && (
        <pre className="mt-3 p-3 bg-ink-800 text-xs text-accent whitespace-pre-wrap rounded">
          {render.error}
        </pre>
      )}
    </section>
  );
}
