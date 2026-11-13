"use client";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { Dialog } from "@/components/Dialog";
import { clockText } from "@/components/FrameFromVideo";
import { api, errorText, JOIN_MAX_CLIPS, JOIN_MIN_CLIPS, JoinTransition, StudioJob } from "@/lib/api";

/** The dissolve Join clips uses by default, and its limits (as the server). */
const DISSOLVE_SECONDS = 0.5;
const MIN_DISSOLVE = 0.1;

/** A clip's length: its measured length, else the length asked for. */
export function clipSeconds(job: StudioJob): number {
  return job.result_duration_seconds ?? job.duration_seconds ?? 0;
}

/** Total length of the joined clip: each dissolve overlaps two clips (kept
 *  to a third of the shortest clip, and a cut when that is too short). */
export function joinedSeconds(clips: StudioJob[], transition: JoinTransition): number {
  const lengths = clips.map(clipSeconds);
  const sum = lengths.reduce((a, b) => a + b, 0);
  if (transition !== "dissolve" || lengths.length < 2) return sum;
  const cf = Math.min(DISSOLVE_SECONDS, Math.min(...lengths) / 3);
  return cf >= MIN_DISSOLVE ? sum - (lengths.length - 1) * cf : sum;
}

/**
 * Join clips: finished Studio clips played back to back as one new clip,
 * in the order shown (move up / down, remove), with a cut or a short
 * dissolve between them. Free: made on this server, no model.
 */
export function JoinClipsDialog({
  clips: initial,
  skipped = 0,
  onClose,
  onJoined,
}: {
  /** The clips in the order they were picked. */
  clips: StudioJob[];
  /** Selected items that aren't finished clips (pictures, running jobs): left out. */
  skipped?: number;
  onClose: () => void;
  onJoined: (job: StudioJob) => void;
}) {
  // Each row keeps its own key so a clip picked twice can be moved on its own.
  const [rows, setRows] = useState(() => initial.slice(0, JOIN_MAX_CLIPS).map((job, i) => ({ key: i, job })));
  const [transition, setTransition] = useState<JoinTransition>("cut");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dropped = Math.max(0, initial.length - JOIN_MAX_CLIPS);
  const clips = rows.map((r) => r.job);
  const total = joinedSeconds(clips, transition);
  const enough = rows.length >= JOIN_MIN_CLIPS;

  // Keyboard focus after a move or remove: the button pressed may now be
  // disabled (moved to an end) or gone (removed), so focus goes to the row's
  // other move button, or the next (else previous) row's remove button, or
  // Cancel. `focusNext` is a data-focus value, applied once rows re-render.
  const listRef = useRef<HTMLOListElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const focusNext = useRef<string | null>(null);
  const [announce, setAnnounce] = useState("");
  useEffect(() => {
    const target = focusNext.current;
    if (target === null) return;
    focusNext.current = null;
    const el = target ? listRef.current?.querySelector<HTMLElement>(`[data-focus="${target}"]`) : null;
    (el ?? cancelRef.current)?.focus();
  }, [rows]);

  const move = (i: number, by: -1 | 1) => {
    const j = i + by;
    if (j < 0 || j >= rows.length) return;
    const key = rows[i].key;
    const atEnd = j === 0 || j === rows.length - 1;
    focusNext.current = `${key}-${atEnd ? (by < 0 ? "down" : "up") : by < 0 ? "up" : "down"}`;
    setAnnounce(`Clip moved to position ${j + 1} of ${rows.length}`);
    setRows((cur) => {
      const next = [...cur];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  };
  const remove = (i: number) => {
    const neighbour = rows[i + 1] ?? rows[i - 1];
    focusNext.current = neighbour ? `${neighbour.key}-remove` : "";
    setAnnounce(`Clip removed; ${rows.length - 1} left`);
    setRows((cur) => cur.filter((_, k) => k !== i));
  };

  const join = async () => {
    setBusy(true);
    setError(null);
    try {
      onJoined(await api.joinClips(clips.map((c) => c.id), transition));
    } catch (e) {
      setError(errorText(e, "Couldn't join the clips"));
      setBusy(false);
    }
  };

  return (
    <Dialog title="Join clips" onClose={onClose}>
      <p className="text-xs text-ink-300">
        Plays the clips one after another as a new clip. The first clip sets the size; each clip keeps its sound.
        {skipped > 0 && ` ${skipped === 1 ? "1 selected item isn't" : `${skipped} selected items aren't`} a finished clip, so it's left out.`}
        {dropped > 0 && ` Only the first ${JOIN_MAX_CLIPS} clips are used.`}
      </p>

      <ol ref={listRef} className="space-y-2" aria-label="Clips, in the order they play">
        {rows.map((row, i) => (
          <li key={row.key} className="flex items-center gap-2 rounded-md border border-ink-800 p-1.5">
            <span className="w-5 text-center text-xs text-ink-400" aria-hidden>
              {i + 1}
            </span>
            <video
              src={`${api.studioPreview(row.job.id)}#t=0.1`}
              preload="metadata"
              muted
              playsInline
              className="h-12 w-16 shrink-0 rounded bg-ink-900 object-cover"
              aria-hidden
            />
            <div className="min-w-0 flex-1 text-xs">
              <div className="truncate" title={row.job.label || row.job.prompt}>
                {row.job.label || row.job.prompt || `Clip #${row.job.id}`}
              </div>
              <div className="text-ink-400">
                #{row.job.id} · {clockText(clipSeconds(row.job))}
              </div>
            </div>
            <div className="flex shrink-0 gap-1">
              <button
                type="button"
                className="chip text-xs px-2 disabled:opacity-40"
                onClick={() => move(i, -1)}
                data-focus={`${row.key}-up`}
                disabled={busy || i === 0}
                aria-label={`Move clip ${i + 1} up`}
                title="Play earlier"
              >
                ↑
              </button>
              <button
                type="button"
                className="chip text-xs px-2 disabled:opacity-40"
                onClick={() => move(i, 1)}
                data-focus={`${row.key}-down`}
                disabled={busy || i === rows.length - 1}
                aria-label={`Move clip ${i + 1} down`}
                title="Play later"
              >
                ↓
              </button>
              <button
                type="button"
                className="chip text-xs px-2 hover:text-accent disabled:opacity-40"
                onClick={() => remove(i)}
                data-focus={`${row.key}-remove`}
                disabled={busy}
                aria-label={`Remove clip ${i + 1}`}
                title="Leave this clip out"
              >
                ✕
              </button>
            </div>
          </li>
        ))}
      </ol>
      <p className="sr-only" aria-live="polite">
        {announce}
      </p>

      <fieldset>
        <legend className="label">Between clips</legend>
        <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Between clips">
          {(
            [
              ["cut", "Cut", "Go straight to the next clip"],
              ["dissolve", "Dissolve", "Fade into the next clip over half a second"],
            ] as const
          ).map(([value, label, hint]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={transition === value}
              onClick={() => setTransition(value)}
              disabled={busy}
              title={hint}
              className={clsx("chip text-xs", transition === value && "chip-active")}
            >
              {label}
            </button>
          ))}
        </div>
      </fieldset>

      <p className="text-sm" aria-live="polite">
        {enough ? (
          <>
            {rows.length} clips · about {clockText(total)} in all
          </>
        ) : (
          <span className="text-ink-300">Pick at least {JOIN_MIN_CLIPS} clips to join.</span>
        )}
      </p>
      {error && (
        <p className="text-sm text-red-300" role="alert">
          {error}
        </p>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        <button ref={cancelRef} type="button" className="btn-ghost text-sm" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn-primary text-sm disabled:opacity-50" onClick={join} disabled={busy || !enough}>
          {busy ? "Joining…" : "Join · free"}
        </button>
      </div>
    </Dialog>
  );
}
