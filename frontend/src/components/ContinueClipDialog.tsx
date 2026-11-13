"use client";
import { useEffect, useId, useState } from "react";
import { Dialog } from "@/components/Dialog";
import { AutoTextarea } from "@/components/AutoTextarea";
import { modelPrice, usd } from "@/components/ImageOps";
import { api, ContinueInfo, errorText, StudioJob, VideoModelOption } from "@/lib/api";

/** A clip's length: its measured length, else the length asked for. */
function lengthOf(job: StudioJob): number {
  return job.result_duration_seconds ?? job.duration_seconds ?? 0;
}

/** "About $0.30" for a clip of `seconds` with a model priced per 5 s; "" = unknown. */
export function clipPriceText(model: VideoModelOption | undefined, seconds: number): string {
  const unit = modelPrice(model?.price_text);
  if (unit === null) return "";
  const total = unit * (seconds / 5);
  return total === 0 ? "Free" : `About ${usd(total)}`;
}

/**
 * Continue this clip: a new clip that starts from this clip's last frame
 * (saved as a picture too), from what happens next. Paid like any clip;
 * the price shows before it runs. With "Join with the original when done"
 * (on by default) the two are joined into one more clip once the new one
 * is finished.
 */
export function ContinueClipDialog({
  job,
  videoModels,
  durations,
  onClose,
  onStarted,
}: {
  job: StudioJob;
  /** The prompt bar's video models (with prices); only those that start from a picture are offered. */
  videoModels: VideoModelOption[];
  /** The clip lengths offered (seconds). */
  durations: readonly number[];
  onClose: () => void;
  onStarted: (created: StudioJob) => void;
}) {
  const ids = useId();
  const models = videoModels.filter((m) => m.first_frame);
  const [info, setInfo] = useState<ContinueInfo | null>(null);
  const [prompt, setPrompt] = useState("");
  const [avoid, setAvoid] = useState("");
  const [model, setModel] = useState("");
  const [seconds, setSeconds] = useState<number>(durations[0] ?? 5);
  const [autoJoin, setAutoJoin] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .continueInfo(job.id)
      .then((i) => {
        if (!live) return;
        setInfo(i);
        setPrompt(i.prompt);
        setAvoid(i.negative_prompt);
        setModel(i.model);
        const want = i.duration_seconds || 5;
        setSeconds(durations.reduce((a, b) => (Math.abs(b - want) < Math.abs(a - want) ? b : a), durations[0] ?? 5));
      })
      .catch((e) => live && setError(errorText(e, "Couldn't load this clip")));
    return () => {
      live = false;
    };
  }, [job.id]);

  // The suggested model may not be in the list (not offered here): pick the first that can.
  useEffect(() => {
    if (info && models.length > 0 && !models.some((m) => m.id === model)) setModel(models[0].id);
  }, [info, models.length]);

  const picked = models.find((m) => m.id === model);
  const price = clipPriceText(picked, seconds);
  const noModel = info !== null && models.length === 0;
  const total = lengthOf(job) + seconds;

  const start = async () => {
    if (!prompt.trim()) {
      setError("Say what happens next.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await api.continueClip(job.id, {
        prompt: prompt.trim(),
        negative_prompt: avoid,
        model: model || undefined,
        duration_seconds: seconds,
        auto_join: autoJoin,
      });
      onStarted(created);
    } catch (e) {
      setError(errorText(e, "Couldn't continue the clip"));
      setBusy(false);
    }
  };

  return (
    <Dialog title="Continue this clip" onClose={onClose}>
      <p className="text-xs text-ink-300">
        A new clip starts from the last frame of clip #{job.id} (saved as a picture too). Say what happens next. It
        is made by the model you pick and paid like any clip.
      </p>
      <figure className="flex items-center gap-3">
        <video
          // Shows the clip's last frame (seeked there once its length is known).
          src={api.studioPreview(job.id)}
          preload="metadata"
          muted
          playsInline
          onLoadedMetadata={(e) => {
            const v = e.currentTarget;
            if (Number.isFinite(v.duration) && v.duration > 0) v.currentTime = Math.max(0, v.duration - 0.05);
          }}
          className="h-28 w-auto max-w-[45%] rounded-md bg-black object-contain"
          aria-label={`Last frame of clip #${job.id}`}
        />
        <figcaption className="text-[11px] text-ink-400">
          The new clip starts from this frame, in the same shape
          {info?.aspect_ratio ? ` (${info.aspect_ratio})` : ""}.
        </figcaption>
      </figure>
      <div>
        <label className="label" htmlFor={`${ids}-prompt`}>
          What happens next
        </label>
        <AutoTextarea
          id={`${ids}-prompt`}
          className="input"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder={info ? "She turns and walks out of the frame…" : "Loading…"}
          maxHeight="25dvh"
          disabled={!info}
        />
      </div>
      <details>
        <summary className="text-xs text-ink-300 cursor-pointer">Avoid (optional)</summary>
        <label className="sr-only" htmlFor={`${ids}-avoid`}>
          Things to avoid
        </label>
        <AutoTextarea
          id={`${ids}-avoid`}
          className="input mt-1"
          value={avoid}
          onChange={(e) => setAvoid(e.target.value)}
          placeholder="blurry, extra fingers…"
          maxHeight="15dvh"
        />
      </details>
      <div className="grid grid-cols-1 sm:grid-cols-[auto_1fr] gap-2">
        <div>
          <label className="label" htmlFor={`${ids}-len`}>
            Length
          </label>
          <select
            id={`${ids}-len`}
            className="input"
            value={seconds}
            onChange={(e) => setSeconds(Number(e.target.value))}
          >
            {durations.map((n) => (
              <option key={n} value={n}>
                {n} s
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-0">
          <label className="label" htmlFor={`${ids}-model`}>
            Model (starts from a picture)
          </label>
          <select
            id={`${ids}-model`}
            className="input"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={models.length === 0}
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
                {m.price_text ? ` · ${m.price_text}` : ""}
              </option>
            ))}
          </select>
          {noModel && (
            <p className="text-[11px] text-red-300 mt-1">No video model here can start from a picture.</p>
          )}
        </div>
      </div>
      <label className="flex items-start gap-2 text-sm cursor-pointer">
        <input
          type="checkbox"
          role="switch"
          aria-checked={autoJoin}
          className="mt-1 h-4 w-4 accent-current"
          checked={autoJoin}
          onChange={(e) => setAutoJoin(e.target.checked)}
        />
        <span>
          Join with the original when done
          <span className="block text-[11px] text-ink-400">
            Plays clip #{job.id} then the new clip as one more clip (free, about {Math.round(total)} s).
          </span>
        </span>
      </label>
      {error && (
        <p className="text-sm text-red-300" role="alert">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2 justify-end">
        <span className="text-xs text-ink-300 mr-auto" aria-live="polite">
          {price ? `${price} for the new clip` : picked ? "Price unknown" : ""}
        </span>
        <button type="button" className="btn-ghost text-sm" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="btn-primary text-sm disabled:opacity-50"
          disabled={busy || !info || noModel || !prompt.trim()}
          onClick={start}
        >
          {busy ? "Starting…" : `Continue${price ? ` · ${price.replace(/^About /, "about ")}` : ""}`}
        </button>
      </div>
    </Dialog>
  );
}
