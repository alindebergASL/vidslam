"use client";
import clsx from "clsx";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Dialog } from "@/components/Dialog";
import { useToast } from "@/components/Toaster";
import { usd } from "@/components/sound/shared";
import {
  api,
  errorText,
  type ClipEstimate,
  type SoundClipBlock,
  type SoundStory,
  type StoryClips,
  type StoryPreviewScope,
} from "@/lib/api";

/** "About $0.42 (12 clips to make, 30 already made)". */
export function ClipPriceNote({ price }: { price: ClipEstimate | null | undefined }) {
  if (!price) return <span className="text-ink-400">Working out the price…</span>;
  const made = (price.clips ?? 0) - (price.missing ?? 0);
  return (
    <span>
      About <strong className="text-ink-100">{usd(price.total)}</strong>
      {price.clips !== undefined && (
        <>
          {" "}
          ({price.missing ?? 0} clip{price.missing === 1 ? "" : "s"} to make
          {made > 0 ? `, ${made} already made and free` : ""})
        </>
      )}
    </span>
  );
}

const STATE_LABEL: Record<string, string> = {
  missing: "Not made yet",
  done: "Ready",
  flagged: "Check this one",
  outdated: "Voice changed",
};

const STATE_TONE: Record<string, string> = {
  missing: "border-ink-600 text-ink-300",
  done: "border-emerald-500/40 text-emerald-300",
  flagged: "border-amber-500/50 text-amber-300",
  outdated: "border-sky-500/40 text-sky-300",
};

type Confirm = { title: string; body: string; price: ClipEstimate | null; go: () => void };

/**
 * The voices and sound effects of a story: Generate / Cancel / Resume with
 * the remaining cost, progress, a Preview, and one cue block per clip with
 * play, regenerate (priced) and its takes. Flagged clips (failed their check
 * twice) are marked to listen to.
 */
export function ClipsPanel({
  story,
  dirty,
  onChanged,
}: {
  story: SoundStory;
  /** Unsaved edits in the editor: nothing is generated from them. */
  dirty: boolean;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [data, setData] = useState<StoryClips | null>(null);
  const [remaining, setRemaining] = useState<ClipEstimate | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [chapter, setChapter] = useState<number | "all">("all");
  const [onlyFlagged, setOnlyFlagged] = useState(false);
  const [previewScope, setPreviewScope] = useState<string>("first");
  const [previewAt, setPreviewAt] = useState<string | null>(null);

  const generating = story.status === "generating";
  const scripting = story.status === "scripting";
  const runningClips = generating && story.task === "clips";
  const p = story.clips;

  const load = useCallback(async () => {
    try {
      const [c, e] = await Promise.all([api.storyClips(story.id), api.storyEstimate({ story_id: story.id })]);
      setData(c);
      setRemaining(e.generate);
    } catch {
      /* the editor shows its own errors */
    }
  }, [story.id]);

  // Reload with the story: a new script, a finished clip, a status change.
  useEffect(() => {
    load();
  }, [load, story.script_version, story.status, p.done, p.flagged, story.updated_at]);

  const run = async (key: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(key);
    try {
      await fn();
      if (ok) toast.success(ok);
    } catch (e) {
      toast.error(errorText(e, "Something went wrong"));
    } finally {
      setBusy(null);
      onChanged();
      load();
    }
  };

  const guard = (): boolean => {
    if (dirty) {
      toast.error("Save or discard your changes first.");
      return false;
    }
    return true;
  };

  const askGenerate = (resume: boolean) => {
    if (!guard()) return;
    setConfirm({
      title: resume ? "Resume the voices?" : "Generate the voices and sound effects?",
      body: resume
        ? "The clips already made are kept; only the missing ones are made."
        : "Every line is voiced and every sound effect made, clip by clip, in the background. You can cancel at any time and keep the clips already made; lines you haven't changed are never paid for twice.",
      price: remaining,
      go: () =>
        run(resume ? "resume" : "generate", () =>
          resume ? api.resumeStory(story.id) : api.generateStoryClips(story.id)
        ),
    });
  };

  const cancel = () => run("cancel", () => api.cancelStory(story.id), "Cancelled");

  const scopeOf = (v: string): StoryPreviewScope => {
    if (v === "first") return { first_minutes: 3 };
    if (v === "first1") return { first_minutes: 1 };
    return { chapter: Number(v.replace("ch", "")) };
  };

  const askPreview = async () => {
    if (!guard()) return;
    const scope = scopeOf(previewScope);
    try {
      const e = await api.storyEstimate({ story_id: story.id, preview: scope });
      setConfirm({
        title: "Make a preview?",
        body: "Only the clips of this part are made (those already made are reused), then mixed quickly so you can listen before paying for the rest.",
        price: e.preview ?? null,
        go: () =>
          run("preview", async () => {
            const r = await api.previewStory(story.id, scope);
            setPreviewAt(String(Date.now()));
            if (r.flagged) toast.info(`${r.flagged} clip${r.flagged === 1 ? "" : "s"} to check in the preview.`);
          }),
      });
    } catch (e) {
      toast.error(errorText(e, "Couldn't price the preview"));
    }
  };

  const askRegenerate = async (b: SoundClipBlock) => {
    if (!guard()) return;
    try {
      const e = await api.storyEstimate({ story_id: story.id, cue_key: b.key });
      setConfirm({
        title: b.kind === "voice" ? "Make a new take of this line?" : "Make this sound again?",
        body: "A new take is made now and used; the earlier takes stay, so you can go back to one.",
        price: e.regenerate ?? null,
        go: () => run(`regen:${b.key}`, () => api.regenerateClip(story.id, b.key), "New take ready"),
      });
    } catch (e) {
      toast.error(errorText(e, "Couldn't price this"));
    }
  };

  const select = (takeId: number) => run(`take:${takeId}`, () => api.selectClipTake(story.id, takeId));

  const blocks = useMemo(() => {
    const all = data?.clips ?? [];
    return all.filter(
      (b) => (chapter === "all" || b.chapter === chapter) && (!onlyFlagged || b.state === "flagged")
    );
  }, [data, chapter, onlyFlagged]);

  const hasScript = story.parsed.voiced_chars > 0 || story.parsed.sfx > 0;
  const missing = remaining?.missing ?? 0;
  const locked = generating || scripting || !!busy;
  const chapters = data?.chapters ?? [];
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;

  return (
    <section className="card p-3 mb-4" aria-labelledby="clips-h">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 id="clips-h" className="text-sm font-semibold">
            Voices and sound effects
          </h2>
          <p className="text-xs text-ink-400">
            {data
              ? `${data.clips.filter((b) => b.state === "done" || b.state === "flagged").length} of ${data.clips.length} clips made`
              : "Loading…"}
            {p.flagged ? ` · ${p.flagged} to check` : ""}
            {remaining ? ` · left to make: ${usd(remaining.total)}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {generating && (
            <button type="button" className="btn-ghost" disabled={busy === "cancel"} onClick={cancel}>
              Cancel
            </button>
          )}
          {!generating && p.can_resume && (
            <button type="button" className="btn-primary" disabled={locked || !hasScript} onClick={() => askGenerate(true)}>
              Resume{remaining ? ` (${usd(remaining.total)})` : ""}
            </button>
          )}
          {!generating && !p.can_resume && (
            <button
              type="button"
              className={missing ? "btn-primary" : "btn-ghost"}
              disabled={locked || !hasScript || !missing}
              onClick={() => askGenerate(false)}
            >
              {missing ? `Generate${remaining ? ` (${usd(remaining.total)})` : ""}` : "All clips made"}
            </button>
          )}
        </div>
      </div>

      {runningClips && (
        <div className="mt-3" aria-live="polite">
          <div className="flex justify-between text-xs text-ink-300">
            <span>
              Making clip {Math.min(p.done + 1, p.total || 1)} of {p.total || "…"}
            </span>
            <span className="text-ink-400">Clips already made are kept if you cancel.</span>
          </div>
          <div className="mt-1 h-2 rounded bg-ink-800 overflow-hidden">
            <div className="h-full bg-violet-500 transition-all" style={{ width: `${Math.max(pct, 3)}%` }} />
          </div>
        </div>
      )}
      {generating && story.task === "preview" && (
        <p className="mt-3 text-xs text-ink-300" aria-live="polite">
          Making the preview…
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-end gap-2 border-t border-ink-800 pt-3">
        <label className="text-xs flex flex-col gap-1">
          <span className="text-ink-400">Preview</span>
          <select className="input text-xs" value={previewScope} onChange={(e) => setPreviewScope(e.target.value)}>
            <option value="first1">The first minute</option>
            <option value="first">The first 3 minutes</option>
            {chapters.map((c) => (
              <option key={c.index} value={`ch${c.index}`}>
                {`${c.title || "Opening"} (first 3 minutes)`}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn-ghost" disabled={locked || !hasScript} onClick={askPreview}>
          {busy === "preview" ? "Making the preview…" : "Preview"}
        </button>
        {p.preview && (
          <audio
            controls
            preload="none"
            className="h-9 max-w-full"
            src={api.storyPreviewUrl(story.id, previewAt ?? p.preview.created_at)}
          />
        )}
      </div>

      {data && data.clips.length > 0 && (
        <div className="mt-3 border-t border-ink-800 pt-3">
          <div className="flex flex-wrap items-center gap-2 mb-2 text-xs">
            <select
              className="input text-xs"
              aria-label="Chapter"
              value={String(chapter)}
              onChange={(e) => setChapter(e.target.value === "all" ? "all" : Number(e.target.value))}
            >
              <option value="all">Every chapter</option>
              {chapters.map((c) => (
                <option key={c.index} value={c.index}>
                  {c.title || "Opening"}
                </option>
              ))}
            </select>
            <label className="inline-flex items-center gap-1">
              <input type="checkbox" checked={onlyFlagged} onChange={(e) => setOnlyFlagged(e.target.checked)} />
              Only clips to check
            </label>
          </div>
          <ol className="flex flex-col gap-2 max-h-[32rem] overflow-y-auto pr-1">
            {blocks.map((b) => (
              <CueBlock
                key={b.key}
                block={b}
                busy={busy}
                locked={locked}
                onRegenerate={() => askRegenerate(b)}
                onSelect={select}
              />
            ))}
            {blocks.length === 0 && <li className="text-xs text-ink-400">No clips here.</li>}
          </ol>
        </div>
      )}

      {confirm && (
        <Dialog onClose={() => setConfirm(null)} title={confirm.title}>
          <p className="text-sm text-ink-300">{confirm.body}</p>
          <p className="text-sm mt-3">
            <ClipPriceNote price={confirm.price} />.
          </p>
          {confirm.price?.note && <p className="text-xs text-ink-500 mt-1">{confirm.price.note}</p>}
          <div className="mt-4 flex justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={() => setConfirm(null)}>
              Not now
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                const go = confirm.go;
                setConfirm(null);
                go();
              }}
            >
              Go{confirm.price ? ` (${usd(confirm.price.total)})` : ""}
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

function CueBlock({
  block: b,
  busy,
  locked,
  onRegenerate,
  onSelect,
}: {
  block: SoundClipBlock;
  busy: string | null;
  locked: boolean;
  onRegenerate: () => void;
  onSelect: (takeId: number) => void;
}) {
  const [showTakes, setShowTakes] = useState(false);
  const sel = b.takes.find((t) => t.id === b.selected) ?? null;
  const flagged = b.state === "flagged";
  return (
    <li
      className={clsx(
        "rounded-lg border p-2 text-sm",
        flagged ? "border-amber-500/50 bg-amber-500/5" : "border-ink-700 bg-ink-900/40"
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {b.kind === "voice" ? (
              <span className="font-semibold text-amber-200">{b.speaker}</span>
            ) : (
              <span className="font-semibold text-sky-300">Sound effect</span>
            )}
            {b.tags && b.tags.length > 0 && <span className="text-violet-300">{b.tags.map((t) => `[${t}]`).join("")}</span>}
            <span className="text-ink-500">
              line{b.lines.length > 1 ? "s" : ""} {b.lines.length > 1 ? `${b.lines[0]}–${b.lines[b.lines.length - 1]}` : b.lines[0]}
            </span>
            <span className={clsx("rounded-full border px-1.5 py-px", STATE_TONE[b.state] ?? STATE_TONE.missing)}>
              {STATE_LABEL[b.state] ?? b.state}
            </span>
          </div>
          <p className="mt-1 text-ink-200 break-words line-clamp-3">
            {b.kind === "voice" ? b.text : `${b.prompt} · ${b.seconds ?? 3} s${b.placement && b.placement !== "after" ? ` · ${b.placement}` : ""}`}
          </p>
          {flagged && sel?.error && <p className="mt-1 text-xs text-amber-200">{sel.error} Listen before you use it, or make a new take.</p>}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {sel && (
              <audio controls preload="none" className="h-8 w-48" src={api.soundUrl(sel.url, sel.id)} aria-label="Play this clip" />
          )}
          <button
            type="button"
            className="btn-ghost text-xs"
            disabled={locked || busy === `regen:${b.key}`}
            onClick={onRegenerate}
            title={`A new take costs about ${usd(b.cost)}`}
          >
            {busy === `regen:${b.key}` ? "Making…" : `${sel ? "New take" : "Make"} (${usd(b.cost)})`}
          </button>
          {b.takes.length > 1 && (
            <button type="button" className="text-xs underline text-ink-300" onClick={() => setShowTakes((v) => !v)}>
              {b.takes.length} takes
            </button>
          )}
        </div>
      </div>
      {showTakes && (
        <ul className="mt-2 flex flex-col gap-1 border-t border-ink-800 pt-2">
          {b.takes.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="w-14 text-ink-400">Take {t.take_no}</span>
              <audio controls preload="none" className="h-7 w-44" src={api.soundUrl(t.url, t.id)} />
              {t.status === "flagged" && <span className="text-amber-300">to check</span>}
              {t.fingerprint !== (b.takes.find((x) => x.id === b.selected)?.fingerprint ?? t.fingerprint) && (
                <span className="text-sky-300">other voice</span>
              )}
              {t.selected ? (
                <span className="text-emerald-300">In use</span>
              ) : (
                <button
                  type="button"
                  className="underline disabled:opacity-40"
                  disabled={locked || busy === `take:${t.id}`}
                  onClick={() => onSelect(t.id)}
                >
                  Use this take
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
