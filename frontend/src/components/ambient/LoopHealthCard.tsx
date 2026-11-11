"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { api, AmbientScene, LoopHealth, LoopHealthResult, LoopLevel, errorText } from "@/lib/api";

const DOT: Record<LoopLevel, string> = {
  good: "bg-emerald-400",
  warn: "bg-amber-400",
  bad: "bg-red-400",
};

/** 3.2 -> "0:03" */
function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function Reading({ level, text }: { level: LoopLevel; text: string }) {
  return (
    <li className="flex items-start gap-2 text-sm">
      <span className={clsx("mt-1.5 w-2 h-2 rounded-full shrink-0", DOT[level])} aria-hidden="true" />
      <span>
        <span className="sr-only">{level === "good" ? "Good: " : level === "warn" ? "Worth a look: " : "Problem: "}</span>
        {text}
      </span>
    </li>
  );
}

/** The last seconds of the loop then its first seconds, on repeat, with a marker at the join. */
function SeamPlayer({ health, aspect }: { health: LoopHealth; aspect: string }) {
  const [duration, setDuration] = useState(health.join_at_seconds * 2);
  const [t, setT] = useState(0);
  const [failed, setFailed] = useState(false);
  const join = health.join_at_seconds;
  const pct = (x: number) => `${Math.min(100, Math.max(0, (x / (duration || 1)) * 100))}%`;
  // The URL already carries ?shot_id=…; measured_at changes when the clip does.
  const src = `${api.mediaUrl(health.seam_preview_url)}&v=${encodeURIComponent(health.measured_at)}`;
  if (failed)
    return <div className="text-xs text-red-300">Couldn&apos;t make the seam preview. Try Measure again.</div>;
  return (
    <div className="space-y-1.5">
      <div className="bg-black rounded-md overflow-hidden">
        <video
          key={src}
          src={src}
          autoPlay
          loop
          muted
          playsInline
          preload="auto"
          className={clsx("mx-auto max-h-[40vh]", aspect === "9:16" ? "h-[40vh]" : "w-full")}
          onLoadedMetadata={(e) => {
            const d = e.currentTarget.duration;
            if (Number.isFinite(d) && d > 0) setDuration(d);
          }}
          onTimeUpdate={(e) => setT(e.currentTarget.currentTime)}
          onError={() => setFailed(true)}
          aria-label={`Seam preview: the end of the loop, then its start. The join is at ${clock(join)}.`}
        />
      </div>
      <div className="relative h-1.5 rounded-full bg-ink-800" aria-hidden="true">
        <div className="absolute inset-y-0 left-0 rounded-full bg-ink-500" style={{ width: pct(t) }} />
        <div className="absolute -top-1 -bottom-1 w-0.5 bg-amber-300" style={{ left: pct(join) }} />
      </div>
      <div className="flex justify-between text-[11px] text-ink-400">
        <span>End of the loop</span>
        <span className="text-amber-300">Join at {clock(join)}</span>
        <span>Start of the loop</span>
      </div>
    </div>
  );
}

/** "Loop health": how the loop joins, flickers, freezes and drifts. Measured locally, free. */
export function LoopHealthCard({
  scene,
  disabled,
  onMeasured,
}: {
  scene: AmbientScene;
  /** The scene is being generated: measuring waits until it's done. */
  disabled: boolean;
  onMeasured: (r: LoopHealthResult) => void;
}) {
  const toast = useToast();
  const shots = scene.loop_shot ? [scene.loop_shot, ...(scene.variant_shots ?? [])] : [];
  const healths = [scene.loop_health ?? null, ...(scene.variant_loop_health ?? [])];
  const labels = scene.plan.variant_labels ?? [];
  const multi = shots.length > 1 && labels.length > 1;
  const [pick, setPick] = useState(0);
  const [showSeam, setShowSeam] = useState(false);
  const [busy, setBusy] = useState(false);
  const idx = pick < shots.length ? pick : 0;
  const health = healths[idx] ?? null;
  const clipReady = (i: number) => !!shots[i]?.clip_path && (i === 0 || shots[i]?.status === "completed");

  // A different variation or a new reading: start the seam player closed again.
  useEffect(() => {
    setShowSeam(false);
  }, [idx, health?.measured_at]);

  const measure = async () => {
    setBusy(true);
    try {
      const r = await api.measureLoopHealth(scene.id);
      onMeasured(r);
      toast.success("Loop measured again");
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't measure the loop"));
    } finally {
      setBusy(false);
    }
  };

  if (!shots.length) return null;

  return (
    <div className="px-4 py-3 border-t border-ink-800 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Loop health</h3>
          <div className="text-[11px] text-ink-400">
            How the loop joins, flickers, freezes and drifts. Measured on this server, free.
          </div>
        </div>
        <button
          type="button"
          className="btn-ghost text-xs disabled:opacity-50"
          onClick={measure}
          disabled={busy || disabled}
          title={disabled ? "Measure once the loop is ready." : "Measures every variation's clip again. Free."}
        >
          {busy ? "Measuring…" : health ? "Measure again" : "Measure now"}
          <span className="text-ink-400"> · free</span>
        </button>
      </div>

      {multi && (
        <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Variation">
          {shots.map((s, i) => (
            <button
              key={s.id}
              type="button"
              role="tab"
              aria-selected={i === idx}
              className={clsx("chip text-[11px] py-0.5 inline-flex items-center gap-1.5", i === idx && "chip-active")}
              onClick={() => setPick(i)}
            >
              {healths[i] && (
                <span
                  className={clsx(
                    "w-1.5 h-1.5 rounded-full",
                    DOT[
                      (["bad", "warn"] as LoopLevel[]).find((l) =>
                        [healths[i]!.seam, healths[i]!.flicker, healths[i]!.frozen, healths[i]!.drift].some(
                          (r) => r.level === l
                        )
                      ) ?? "good"
                    ]
                  )}
                  aria-hidden="true"
                />
              )}
              {labels[i] ?? `Variation ${i + 1}`}
            </button>
          ))}
        </div>
      )}

      {health ? (
        <>
          <ul className="space-y-1">
            <Reading level={health.seam.level} text={health.seam.text} />
            <Reading level={health.flicker.level} text={health.flicker.text} />
            <Reading level={health.frozen.level} text={health.frozen.text} />
            <Reading level={health.drift.level} text={health.drift.text} />
          </ul>
          <div className="text-[11px] text-ink-500">
            Measured {new Date(health.measured_at).toLocaleString()} over {clock(health.clip_seconds)} of clip.
          </div>
          {showSeam ? (
            <div className="space-y-1.5">
              <SeamPlayer health={health} aspect={scene.aspect_ratio} />
              <button type="button" className="btn-ghost text-xs" onClick={() => setShowSeam(false)}>
                Hide the seam
              </button>
            </div>
          ) : (
            <button type="button" className="btn-ghost text-xs" onClick={() => setShowSeam(true)}>
              Watch the seam
            </button>
          )}
        </>
      ) : (
        <div className="text-xs text-ink-400">
          {clipReady(idx)
            ? "Not measured yet, or the clip changed since. Measure now to see how it joins."
            : "This variation has no clip yet."}
        </div>
      )}
    </div>
  );
}
