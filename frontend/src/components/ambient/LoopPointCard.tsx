"use client";
import { useEffect, useState } from "react";
import { useToast } from "@/components/Toaster";
import { api, AmbientScene, errorText } from "@/lib/api";

const sec = (n: number) => `${n.toFixed(1)} s`;
const blendSec = (n: number) => `${n % 1 ? n.toFixed(1) : n.toFixed(0)} s`;
const VERDICT = {
  seamless: "seamless",
  slight_jump: "slight jump",
  visible_jump: "visible jump",
  snap: "jumps at the very end",
} as const;

/**
 * "Find a better loop point": a free, local search for the stretch of the
 * loop clip whose end matches its start best. The render then loops that
 * stretch (opening on the clip's own first frames); "Use the whole clip"
 * goes back.
 */
export function LoopPointCard({
  scene,
  disabled,
  onChanged,
}: {
  scene: AmbientScene;
  /** The scene is being made: the search waits until it's done. */
  disabled: boolean;
  /** Reload the scene (the blend, the plan and the seam card follow). */
  onChanged: () => void | Promise<void>;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const lp = scene.loop_point ?? null;
  const state = scene.loop_point_state ?? null;
  const running = state?.status === "running";
  const t = scene.seam_treatment;

  // A search on the job queue: reload until it has finished. `scene` is a
  // new object after every reload, so this re-arms each time while the
  // search is still running (the page's own poll is off: nothing is in flight).
  useEffect(() => {
    if (!running) return;
    const h = window.setTimeout(() => void onChanged(), 2000);
    return () => window.clearTimeout(h);
  }, [running, scene, onChanged]);

  if (scene.config.source !== "video" || !scene.loop_shot) return null;

  const find = async () => {
    setBusy(true);
    try {
      await api.findLoopPoint(scene.id);
      await onChanged();
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't look for a loop point"));
    } finally {
      setBusy(false);
    }
  };

  const wholeClip = async (whole: boolean) => {
    if (!whole) return find();
    setBusy(true);
    try {
      await api.useWholeClip(scene.id);
      await onChanged();
      toast.success("Looping the whole clip");
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't switch back to the whole clip"));
    } finally {
      setBusy(false);
    }
  };

  const applies = lp ? lp.applies : scene.loop_point_applies ?? true;
  const inUse = !!lp && !lp.off && lp.applies;
  const perHour = lp && lp.loop_seconds > 0 ? Math.round(3600 / lp.loop_seconds) : null;
  // In / out are the clip's own seconds; a slowed scene plays the stretch
  // longer in the finished video, so the two are labelled apart.
  const clipSpan = lp ? lp.out_seconds - lp.in_seconds : 0;
  const slowed = !!lp && Math.abs(lp.loop_seconds - clipSpan) > 0.05;

  return (
    <div className="px-4 py-3 border-t border-ink-800 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Loop point</h3>
          <div className="text-[11px] text-ink-400">
            Loops the stretch of the clip whose end matches its start best. Looked for on this server, free.
          </div>
        </div>
        {applies && (
          <button
            type="button"
            className="btn-ghost text-xs disabled:opacity-50"
            onClick={find}
            disabled={busy || running || disabled}
            title={disabled ? "Look once the loop is ready." : "Searches the loop clip for a better join. Free."}
          >
            {running ? "Looking…" : "Find a better loop point"}
            <span className="text-ink-400"> · free</span>
          </button>
        )}
      </div>

      {!applies && (
        <p className="text-xs text-ink-400">Loop points apply to single-clip scenes for now; variations loop their whole clips.</p>
      )}

      {lp && applies && (
        <div className="space-y-1.5">
          <p className="text-sm" aria-live="polite">
            {inUse ? "Loops" : "Found"} {sec(lp.in_seconds)} → {sec(lp.out_seconds)} of the clip (
            {slowed ? `${sec(lp.loop_seconds)} in the video, slowed` : sec(lp.loop_seconds)}
            {perHour ? `, repeats ~${perHour}×/h` : ""}) · seam: {VERDICT[lp.verdict]}
            {inUse && t?.loop_point ? `, blended ${blendSec(t.blend_seconds)}` : ""}
            {inUse && lp.intro ? " · opens on the clip's first frames" : ""}
          </p>
          {!lp.mismatch_ok && (
            <p className="text-xs text-amber-300">The two stretches it blends differ, so the blend may show a faint double image.</p>
          )}
          {lp.snap && (
            <p className="text-xs text-ink-300">The clip jumps to its last frame at the very end; that tail is left out.</p>
          )}
          <label className="inline-flex items-center gap-2 text-xs text-ink-300">
            <input
              type="checkbox"
              checked={lp.off}
              disabled={busy || running || disabled}
              onChange={(e) => void wholeClip(e.target.checked)}
            />
            Use the whole clip
          </label>
        </div>
      )}

      {/* A search that found a stretch is described above (in use or not):
          its "Loops a–b s" message would contradict "Use the whole clip". */}
      {state && state.status !== "running" && state.message && !state.found && (
        <p className={state.status === "failed" ? "text-xs text-red-300" : "text-xs text-ink-400"}>{state.message}</p>
      )}
    </div>
  );
}
