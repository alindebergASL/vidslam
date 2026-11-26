"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { Dialog } from "@/components/Dialog";
import { useEvents } from "@/components/NotificationsProvider";
import { useToast } from "@/components/Toaster";
import {
  DiffView,
  ENHANCE_ACTIONS,
  PriceNote,
  ScriptView,
  StoryStatus,
  fmtMinutes,
  usd,
} from "@/components/sound/shared";
import { usePoll } from "@/lib/usePoll";
import {
  api,
  errorText,
  type CallPrice,
  type SoundConfig,
  type SoundStory,
  type StoryEnhanceAction,
  type StoryEnhanceScope,
  type StoryEstimate,
} from "@/lib/api";

export default function StoryEditorPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

/** A rewrite on screen: where it goes, what was there, and what it says. */
type Proposal = { start: number; end: number; before: string; after: string; version: number; fromJob: boolean };

function Inner() {
  const params = useParams<{ id: string }>();
  const id = Number(params?.id);
  const toast = useToast();

  const [story, setStory] = useState<SoundStory | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [cfg, setCfg] = useState<SoundConfig | null>(null);
  const [text, setText] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [est, setEst] = useState<StoryEstimate | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [undo, setUndo] = useState<{ text: string; after: string } | null>(null);
  const [confirm, setConfirm] = useState<null | { title: string; body: string; price?: CallPrice; go: () => void }>(
    null
  );
  const area = useRef<HTMLTextAreaElement | null>(null);
  const sel = useRef<{ start: number; end: number }>({ start: 0, end: 0 });

  const dirty = !!story && text !== story.script_text;
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;

  const reload = useCallback(async () => {
    try {
      const s = await api.getStory(id);
      setStory(s);
      // Never overwrite edits in progress.
      if (!dirtyRef.current) setText(s.script_text);
    } catch (e) {
      if (String(errorText(e, "")).startsWith("404") || (e as { status?: number })?.status === 404) setNotFound(true);
    }
  }, [id]);

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    reload();
    api.soundConfig().then(setCfg).catch(() => {});
  }, [id, reload]);

  const scripting = story?.status === "scripting";
  const poke = usePoll(reload, { enabled: scripting, interval: 2500 });
  useEvents({ kind: "sound_story", entity_id: id }, () => poke());

  // The price bar: what writing (or resuming) and generating would cost.
  const version = story?.script_version;
  const status = story?.status;
  useEffect(() => {
    if (!story) return;
    api
      .storyEstimate({ story_id: story.id })
      .then(setEst)
      .catch(() => setEst(null));
  }, [story?.id, version, status]);

  // A finished Enhance job: show its before/after once.
  const job = story?.enhance;
  useEffect(() => {
    if (!job || job.status !== "ready" || proposal) return;
    setProposal({
      start: job.start,
      end: job.end,
      before: job.before,
      after: job.after,
      version: job.script_version,
      fromJob: true,
    });
  }, [job?.status, job?.after]);

  const run = async (key: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(key);
    try {
      await fn();
      if (ok) toast.success(ok);
    } catch (e) {
      toast.error(errorText(e, "Something went wrong"));
    } finally {
      setBusy(null);
      reload();
    }
  };

  if (notFound)
    return (
      <div className="p-8">
        Story not found.{" "}
        <Link href="/sound" className="underline">
          Back to Sound Studio
        </Link>
      </div>
    );
  if (!story) return <div className="p-8 text-ink-400">Loading…</div>;

  const parsed = story.parsed;
  const hasLines = parsed.voiced_chars > 0;
  const w = story.writer;
  const writing = scripting && story.task === "writer";
  const enhancing = scripting && story.task === "enhance";

  const save = (next = text, msg = "Saved") =>
    run(
      "save",
      async () => {
        const s = await api.updateStory(story.id, { script_text: next, script_version: story.script_version });
        setStory(s);
        setText(s.script_text);
      },
      msg
    );

  const askWrite = () => {
    const price = est?.write;
    setConfirm({
      title: hasLines ? "Write the story again?" : "Write this story?",
      body: hasLines
        ? "The writer replaces the current script with a new one, chapter by chapter."
        : "The writer plans the story, then writes it chapter by chapter. You can cancel at any time and keep the chapters already written.",
      price,
      go: () =>
        run("write", async () => {
          await api.writeStory(story.id, hasLines);
        }),
    });
  };

  const askResume = () =>
    setConfirm({
      title: "Resume writing?",
      body: `The ${w.chapters_done} finished chapter${w.chapters_done === 1 ? "" : "s"} stay; the writer continues with the next one.`,
      price: est?.write,
      go: () =>
        run("resume", async () => {
          await api.resumeStory(story.id);
        }),
    });

  const cancel = () =>
    run(
      "cancel",
      async () => {
        await api.cancelStory(story.id);
      },
      "Cancelled"
    );

  // ---------- Enhance ----------

  const enhance = async (action: StoryEnhanceAction, scope: StoryEnhanceScope, opts: { chapterLine?: number; minutes?: number }) => {
    if (dirty) {
      toast.error("Save your changes first, then enhance.");
      return;
    }
    const body: Parameters<typeof api.enhanceStory>[1] = {
      action,
      scope,
      script_version: story.script_version,
      minutes: opts.minutes,
    };
    if (scope === "selection") {
      const { start, end } = sel.current;
      if (end <= start) {
        toast.error("Select some of the script first (in Edit), or pick a chapter or the whole story.");
        return;
      }
      body.start = start;
      body.end = end;
      body.text = story.script_text.slice(start, end);
    } else if (scope === "chapter") {
      body.chapter_line = opts.chapterLine;
    }
    const go = () =>
      run("enhance", async () => {
        const r = await api.enhanceStory(story.id, body);
        if (r.mode === "sync")
          setProposal({ start: r.start, end: r.end, before: r.before, after: r.after, version: r.script_version, fromJob: false });
        else toast.info("Enhancing in the background; the before/after shows here when it's ready.");
      });
    if (scope === "story") {
      // The price before Enhance-all.
      try {
        const e = await api.storyEstimate({ story_id: story.id, enhance_scope: "story" });
        setConfirm({
          title: "Enhance the whole story?",
          body: "Each chapter is rewritten in turn and checked. You see the before/after and choose whether to use it.",
          price: e.enhance,
          go,
        });
      } catch (e) {
        toast.error(errorText(e, "Couldn't price this"));
      }
      return;
    }
    go();
  };

  const useIt = async () => {
    if (!proposal) return;
    if (proposal.version !== story.script_version) {
      toast.error("The script changed since this rewrite was made; enhance it again.");
      return;
    }
    const before = story.script_text;
    const next = before.slice(0, proposal.start) + proposal.after + before.slice(proposal.end);
    await save(next, "Rewrite used");
    setUndo({ text: before, after: next });
    if (proposal.fromJob) await api.dismissStoryEnhance(story.id).catch(() => {});
    setProposal(null);
  };

  const keepMine = async () => {
    if (proposal?.fromJob) await api.dismissStoryEnhance(story.id).catch(() => {});
    setProposal(null);
    reload();
  };

  const doUndo = async () => {
    if (!undo) return;
    await save(undo.text, "Undone");
    setUndo(null);
  };

  const jumpTo = (line: number) => {
    const el = document.getElementById(`story-line-${line}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    if (editing && area.current) {
      const lines = text.split("\n");
      const pos = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
      area.current.focus();
      area.current.setSelectionRange(pos, pos);
    }
  };

  return (
    <div className="p-4 sm:p-8 max-w-7xl">
      <nav className="text-sm text-ink-400 mb-3">
        <Link href="/sound" className="hover:underline">
          Sound Studio
        </Link>{" "}
        / Story
      </nav>
      <header className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div className="min-w-0">
          <TitleEdit story={story} onSaved={setStory} busy={scripting} />
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-ink-400">
            <StoryStatus status={story.status} task={story.task} />
            <span>
              About {fmtMinutes(parsed.est_seconds)} of {story.target_minutes} min · {parsed.words.toLocaleString()} words ·{" "}
              {parsed.sfx} sound cue{parsed.sfx === 1 ? "" : "s"}
            </span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {scripting && (
            <button type="button" className="btn-ghost" disabled={busy === "cancel"} onClick={cancel}>
              Cancel
            </button>
          )}
          {!scripting && story.can_resume && (
            <button type="button" className="btn-primary" disabled={!!busy} onClick={askResume}>
              Resume writing
            </button>
          )}
          {!scripting && !story.can_resume && story.premise.trim() && (
            <button type="button" className={hasLines ? "btn-ghost" : "btn-primary"} disabled={!!busy} onClick={askWrite}>
              {hasLines ? "Write again" : "Write it for me"}
              {est ? ` (${usd(est.write.cost)})` : ""}
            </button>
          )}
        </div>
      </header>

      {story.error && !scripting && (
        <p role="alert" className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          {story.error}
        </p>
      )}

      {writing && (
        <div className="mb-4 card p-3" aria-live="polite">
          <div className="flex justify-between text-sm">
            <span>{w.chapters ? `Writing chapter ${Math.min(w.chapters_done + 1, w.chapters)} of ${w.chapters}` : "Planning the story…"}</span>
            <span className="text-ink-400">Finished chapters are kept if you cancel.</span>
          </div>
          <div className="mt-2 h-2 rounded bg-ink-800 overflow-hidden">
            <div
              className="h-full bg-violet-500 transition-all"
              style={{ width: `${w.chapters ? Math.round((w.chapters_done / w.chapters) * 100) : 4}%` }}
            />
          </div>
        </div>
      )}
      {enhancing && (
        <div className="mb-4 card p-3 text-sm" aria-live="polite">
          Enhancing{story.enhance?.total ? ` (part ${Math.min((story.enhance.done ?? 0) + 1, story.enhance.total)} of ${story.enhance.total})` : ""}…
        </div>
      )}
      {story.enhance && story.enhance.status === "failed" && !proposal && (
        <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200 flex justify-between gap-3">
          <span>{story.enhance.error || "Enhance stopped."}</span>
          <button type="button" className="underline" onClick={keepMine}>
            Dismiss
          </button>
        </div>
      )}

      <CostBar est={est} story={story} />

      <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
        <aside className="flex flex-col gap-4">
          <section className="card p-3">
            <h2 className="text-sm font-semibold mb-2">Chapters</h2>
            {parsed.chapters.length === 0 ? (
              <p className="text-xs text-ink-400">No chapters yet.</p>
            ) : (
              <ol className="flex flex-col gap-1 text-sm">
                {parsed.chapters.map((c) => (
                  <li key={c.index}>
                    <button type="button" className="text-left hover:text-violet-300 w-full" onClick={() => jumpTo(c.line)}>
                      <span className="block truncate">{c.title || "Opening"}</span>
                      <span className="text-xs text-ink-500">{fmtMinutes(c.est_seconds)}</span>
                    </button>
                  </li>
                ))}
              </ol>
            )}
          </section>
          <section className="card p-3">
            <h2 className="text-sm font-semibold mb-2">Cast</h2>
            {parsed.cast.length === 0 ? (
              <p className="text-xs text-ink-400">Only the narrator.</p>
            ) : (
              <ul className="flex flex-col gap-2 text-xs">
                {parsed.cast.map((c) => (
                  <li key={c.name}>
                    <span className="font-semibold text-amber-200">{c.name}</span>
                    {c.pan ? <span className="text-ink-500"> [{c.pan}]</span> : null}
                    {c.description ? <span className="block text-ink-400">{c.description}</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
          {parsed.warnings.length > 0 && (
            <section className="card p-3">
              <h2 className="text-sm font-semibold mb-2 text-amber-300">
                {parsed.warnings.length} warning{parsed.warnings.length === 1 ? "" : "s"}
              </h2>
              <ul className="flex flex-col gap-1 text-xs text-amber-200">
                {parsed.warnings.slice(0, 30).map((wn, i) => (
                  <li key={i}>
                    <button type="button" className="text-left hover:underline" onClick={() => jumpTo(wn.line)}>
                      Line {wn.line}: {wn.message}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <EnhanceMenu
            disabled={scripting || !!busy || !hasLines}
            chapters={parsed.chapters}
            target={story.target_minutes}
            maxMinutes={cfg?.max_minutes ?? 90}
            syncMax={cfg?.enhance_sync_max ?? 6000}
            onRun={enhance}
          />
          {undo && text === undo.after && (
            <button type="button" className="btn-ghost" onClick={doUndo}>
              Undo the rewrite
            </button>
          )}
        </aside>

        <main className="card p-3 min-w-0">
          {proposal && (
            <section className="mb-4 rounded-lg border border-violet-500/40 p-3" aria-label="Enhance suggestion">
              <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                <h2 className="text-sm font-semibold">Suggested rewrite</h2>
                <div className="flex gap-2">
                  <button type="button" className="btn-primary" disabled={busy === "save"} onClick={useIt}>
                    Use it
                  </button>
                  <button type="button" className="btn-ghost" onClick={keepMine}>
                    Keep mine
                  </button>
                </div>
              </div>
              <DiffView before={proposal.before} after={proposal.after} />
            </section>
          )}
          <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
            <div role="tablist" className="flex gap-1">
              <button
                role="tab"
                type="button"
                aria-selected={!editing}
                className={clsx("chip", !editing && "chip-active")}
                onClick={() => setEditing(false)}
              >
                Read
              </button>
              <button
                role="tab"
                type="button"
                aria-selected={editing}
                className={clsx("chip", editing && "chip-active")}
                disabled={scripting}
                onClick={() => setEditing(true)}
              >
                Edit
              </button>
            </div>
            {dirty && (
              <div className="flex items-center gap-2">
                <span className="text-xs text-amber-300">Unsaved changes</span>
                <button type="button" className="btn-ghost" onClick={() => setText(story.script_text)}>
                  Discard
                </button>
                <button type="button" className="btn-primary" disabled={busy === "save"} onClick={() => save()}>
                  Save
                </button>
              </div>
            )}
          </div>
          {editing && !scripting ? (
            <textarea
              ref={area}
              className="input w-full font-mono text-[13px] leading-6 min-h-[60vh]"
              value={text}
              spellCheck={false}
              aria-label="Script"
              onChange={(e) => setText(e.target.value)}
              onSelect={(e) => {
                const t = e.currentTarget;
                sel.current = { start: t.selectionStart, end: t.selectionEnd };
              }}
            />
          ) : text.trim() ? (
            <ScriptViewAnchored text={text} parsed={parsed} />
          ) : (
            <p className="text-sm text-ink-400 p-4">
              {writing ? "The first chapter appears here as soon as it's written." : "No script yet. Write it for me, or switch to Edit and paste one."}
            </p>
          )}
        </main>
      </div>

      {confirm && (
        <Dialog onClose={() => setConfirm(null)} title={confirm.title}>
          <p className="text-sm text-ink-300">{confirm.body}</p>
          <p className="text-sm mt-3">
            <PriceNote price={confirm.price} />.
          </p>
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
              Go{confirm.price ? ` (${usd(confirm.price.cost)})` : ""}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

/** The highlighted script, with an anchor per line for the outline's jumps. */
function ScriptViewAnchored({ text, parsed }: { text: string; parsed: SoundStory["parsed"] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const rows = ref.current?.querySelectorAll(":scope > div > div");
    rows?.forEach((row, i) => row.setAttribute("id", `story-line-${i + 1}`));
  }, [text]);
  return (
    <div ref={ref}>
      <ScriptView text={text} parsed={parsed} />
    </div>
  );
}

function TitleEdit({ story, onSaved, busy }: { story: SoundStory; onSaved: (s: SoundStory) => void; busy: boolean }) {
  const toast = useToast();
  const [title, setTitle] = useState(story.title);
  useEffect(() => setTitle(story.title), [story.title]);
  const commit = async () => {
    if (title.trim() === story.title) return;
    try {
      onSaved(await api.updateStory(story.id, { title: title.trim() }));
    } catch (e) {
      toast.error(errorText(e, "Couldn't rename"));
      setTitle(story.title);
    }
  };
  return (
    <input
      className="bg-transparent text-2xl font-semibold tracking-tight w-full focus:outline-none focus:ring-1 focus:ring-violet-500 rounded"
      value={title}
      placeholder="Untitled story"
      aria-label="Story title"
      disabled={busy}
      maxLength={200}
      onChange={(e) => setTitle(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

function CostBar({ est, story }: { est: StoryEstimate | null; story: SoundStory }) {
  if (!est) return null;
  const voices = est.generate.total;
  return (
    <div className="mb-4 flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-ink-700 bg-ink-900/60 px-3 py-2 text-xs text-ink-300">
      <span>
        {story.can_resume ? "Rest of the writing" : "Writing"}: <strong className="text-ink-100">{usd(est.write.cost)}</strong>
      </span>
      <span>
        Voices and sounds (next step): <strong className="text-ink-100">{usd(voices)}</strong>
      </span>
      <span>Mixing and export: free</span>
      {story.actual_cost > 0 && <span>Spent so far: {usd(story.actual_cost)}</span>}
    </div>
  );
}

function EnhanceMenu({
  disabled,
  chapters,
  target,
  maxMinutes,
  syncMax,
  onRun,
}: {
  disabled: boolean;
  chapters: SoundStory["parsed"]["chapters"];
  target: number;
  maxMinutes: number;
  syncMax: number;
  onRun: (action: StoryEnhanceAction, scope: StoryEnhanceScope, opts: { chapterLine?: number; minutes?: number }) => void;
}) {
  const [action, setAction] = useState<StoryEnhanceAction>("tighten");
  const [scope, setScope] = useState<StoryEnhanceScope>("selection");
  const [chapterLine, setChapterLine] = useState<number | undefined>(chapters[0]?.line);
  const [minutes, setMinutes] = useState(target);
  useEffect(() => {
    if (chapterLine === undefined && chapters[0]) setChapterLine(chapters[0].line);
  }, [chapters, chapterLine]);
  return (
    <section className="card p-3 flex flex-col gap-2" aria-labelledby="enhance-h">
      <h2 id="enhance-h" className="text-sm font-semibold">
        Enhance
      </h2>
      <label className="text-xs flex flex-col gap-1">
        <span className="text-ink-400">What</span>
        <select className="input" value={action} onChange={(e) => setAction(e.target.value as StoryEnhanceAction)}>
          {ENHANCE_ACTIONS.map((a) => (
            <option key={a.id} value={a.id}>
              {a.label}: {a.hint}
            </option>
          ))}
        </select>
      </label>
      {action === "lengthen" && (
        <label className="text-xs flex flex-col gap-1">
          <span className="text-ink-400">To about (minutes)</span>
          <input
            className="input"
            type="number"
            min={1}
            max={maxMinutes}
            value={minutes}
            onChange={(e) => setMinutes(Math.max(1, Math.min(maxMinutes, Number(e.target.value) || 1)))}
          />
        </label>
      )}
      <label className="text-xs flex flex-col gap-1">
        <span className="text-ink-400">Where</span>
        <select className="input" value={scope} onChange={(e) => setScope(e.target.value as StoryEnhanceScope)}>
          <option value="selection">The selection (in Edit)</option>
          <option value="chapter">One chapter</option>
          <option value="story">The whole story</option>
        </select>
      </label>
      {scope === "chapter" && (
        <select className="input text-xs" value={chapterLine} onChange={(e) => setChapterLine(Number(e.target.value))}>
          {chapters.map((c) => (
            <option key={c.index} value={c.line}>
              {c.title || "Opening"}
            </option>
          ))}
        </select>
      )}
      <p className="text-xs text-ink-500">
        {scope === "selection"
          ? `Up to ${syncMax.toLocaleString()} characters are rewritten at once; more runs in the background.`
          : "Runs in the background; you see the before/after when it's ready."}
      </p>
      <button
        type="button"
        className="btn-primary"
        disabled={disabled || (scope === "chapter" && !chapterLine)}
        onClick={() => onRun(action, scope, { chapterLine, minutes: action === "lengthen" ? minutes : undefined })}
      >
        Enhance
      </button>
    </section>
  );
}
