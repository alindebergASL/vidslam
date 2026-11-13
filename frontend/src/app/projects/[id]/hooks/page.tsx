"use client";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { useEvents } from "@/components/NotificationsProvider";
import { PublishPanel } from "@/components/PublishPanel";
import { useToast } from "@/components/Toaster";
import {
  api,
  errorText,
  HookBase,
  HookLab,
  HookPlan,
  HookPlanRow,
  HookPreviewAudio,
  HookSuggestion,
  HookVariant,
  Project,
  Shot1Source,
} from "@/lib/api";
import { fmtPrice, splitFirstSentence } from "@/lib/hook";
import { usePoll } from "@/lib/usePoll";
import { AutoTextarea } from "@/components/AutoTextarea";

/**
 * Hook lab: other openings of a finished storyboard video. The first spoken
 * line (and optionally shot 1) changes; the rest of the video is reused, so
 * each opening costs a fraction of the whole video.
 */
export default function HooksPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

const RUNNING = ["making", "ready", "composing"];
const EDITABLE = ["draft", "failed"];
const TEMPO_MAX = 1.15;

const STATUS_TEXT: Record<string, string> = {
  making: "Making the voice and clips…",
  ready: "Parts ready, waiting to be put together…",
  composing: "Putting the video together…",
};

/** The same guess the server makes from text length, while the user types. */
function estimateFit(text: string, original: string, window: number) {
  const n = (original || "").trim().length;
  const est = n ? (window * (text || "").trim().length) / n : 0;
  const overflow = Math.max(est - window * TEMPO_MAX, 0);
  return { est, fits: overflow <= 0.005, overflow };
}

const sec = (n: number | null | undefined) => `${(Number(n) || 0).toFixed(1)} s`;

function Inner() {
  const params = useParams();
  const search = useSearchParams();
  const router = useRouter();
  const toast = useToast();
  const projectId = Number(params?.id);
  const wantedBase = Number(search?.get("base")) || null;

  const [project, setProject] = useState<Project | null>(null);
  const [lab, setLab] = useState<HookLab | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [plan, setPlan] = useState<HookPlan | null>(null);
  // The price is being worked out again: the shown one may be out of date.
  const [planLoading, setPlanLoading] = useState(false);
  // Variants left out of "Make videos" (all drafts are in by default).
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  // Latest progress line per variant, from the live events (only a
  // started/progress line is shown, and only while it is being made).
  const [progress, setProgress] = useState<Record<number, { status: string; text: string; vstatus: string }>>({});
  const [making, setMaking] = useState(false);
  // Drafts with unsaved edits (or a save in flight): Make waits for them.
  const [dirty, setDirty] = useState<Record<number, string>>({});
  // Bumped after Listen, so the price (and "too long") is worked out again.
  const [priceTick, setPriceTick] = useState(0);
  const markDirty = useCallback((id: number, why: string) => {
    setDirty((d) => {
      if ((d[id] || "") === why) return d;
      const n = { ...d };
      if (why) n[id] = why;
      else delete n[id];
      return n;
    });
  }, []);

  const load = useCallback(async () => {
    try {
      const [p, l] = await Promise.all([api.getProject(projectId), api.hookLab(projectId)]);
      setProject(p);
      setLab(l);
      setError(null);
    } catch (e) {
      setError(errorText(e, "Couldn't load the hook lab"));
    }
  }, [projectId]);
  useEffect(() => {
    load();
  }, [load]);

  const base: HookBase | null = useMemo(() => {
    if (!lab || lab.bases.length === 0) return null;
    return lab.bases.find((b) => b.render_id === wantedBase) ?? lab.bases[0];
  }, [lab, wantedBase]);

  const variants = base?.variants ?? [];
  const drafts = variants.filter((v) => EDITABLE.includes(v.status));
  const running = variants.filter((v) => RUNNING.includes(v.status));
  const done = variants.filter((v) => v.status === "completed");
  const selected = drafts.filter((v) => !excluded.has(v.id)).map((v) => v.id);
  const selectedKey = selected.join(",");

  // Where the original opening ends: as the drafts use it, else as found.
  const currentWin = drafts[0]?.window_seconds ?? base?.window_seconds ?? 0;
  const [win, setWin] = useState<number>(0);
  useEffect(() => {
    setWin(currentWin);
  }, [base?.render_id, currentWin]);

  // Price of what "Make videos" would make (no spend).
  useEffect(() => {
    if (!selectedKey) {
      setPlan(null);
      setPlanLoading(false);
      return;
    }
    let live = true;
    setPlanLoading(true);
    const ids = selectedKey.split(",").map(Number);
    api
      .hookPlan(projectId, ids)
      .then((p) => live && setPlan(p))
      .catch(() => live && setPlan(null))
      .finally(() => live && setPlanLoading(false));
    return () => {
      live = false;
    };
    // Re-price when a draft changes (its updated_at moves) or after Listen.
  }, [projectId, selectedKey, drafts.map((d) => d.updated_at).join("|"), priceTick]);

  // Live: hook-lab events for this project refetch; a fallback poll runs
  // while openings are being made.
  const poke = usePoll(load, { enabled: running.length > 0, interval: 3000 });
  const statusOf = useMemo(() => new Map(variants.map((v) => [v.id, v.status])), [variants]);
  useEvents({ kind: "hook_lab", project_id: projectId }, (ev) => {
    if (ev && ev.entity_type === "hook_variant") {
      const vstatus = statusOf.get(ev.entity_id) || "";
      setProgress((p) => ({
        ...p,
        [ev.entity_id]: { status: ev.status, text: ev.status === "failed" ? "" : ev.detail || ev.title, vstatus },
      }));
    }
    poke();
  });
  // A progress line is stale once the opening moves on (or is made again).
  const progressText = (v: HookVariant) => {
    const p = progress[v.id];
    if (!p || !p.text || !["started", "progress"].includes(p.status)) return "";
    return p.vstatus === v.status || !p.vstatus ? p.text : "";
  };

  const selectedDirty = selected.map((id) => dirty[id]).find(Boolean) || "";
  const knownTooLong = (plan?.variants || []).find((r) => selected.includes(r.id) && r.too_long_seconds > 0);
  const blockReason =
    running.length > 0
      ? "Wait until the openings being made are done"
      : selectedDirty
      ? selectedDirty
      : knownTooLong
      ? `Opening ${knownTooLong.label} is about ${knownTooLong.too_long_seconds.toFixed(1)} s too long: shorten it or untick it`
      : "";

  const make = async () => {
    if (!plan || planLoading || selected.length === 0 || blockReason) return;
    const guessedLong = drafts.filter((v) => selected.includes(v.id) && v.fits === false && !v.audio_url);
    if (
      guessedLong.length > 0 &&
      !window.confirm(
        `Opening ${guessedLong.map((v) => v.label).join(", ")} might be too long for the original's time, and ` +
          "would then fail after its voice is paid for. Listen first to check. Make anyway?"
      )
    )
      return;
    setProgress((p) => {
      const n = { ...p };
      for (const id of selected) delete n[id];
      return n;
    });
    setMaking(true);
    try {
      await api.hookMake(projectId, selected);
      toast.success(`Making ${selected.length} opening${selected.length === 1 ? "" : "s"}`);
      await load();
    } catch (e) {
      toast.error(errorText(e, "Couldn't start making the videos"));
      load();
    } finally {
      setMaking(false);
    }
  };

  const cancel = async (v: HookVariant) => {
    if (!window.confirm(`Stop making opening ${v.label}? Clips already being made are still paid for.`)) return;
    try {
      await api.hookCancelVariant(v.id);
      toast.success(`Opening ${v.label} stopped`);
    } catch (e) {
      toast.error(errorText(e, "Couldn't stop it"));
    }
    load();
  };

  if (error && !lab) {
    return (
      <div className="p-4 sm:p-8 max-w-3xl">
        <Link href={`/projects/${projectId}`} className="text-xs text-ink-300 hover:text-ink-100">
          ← Back to the project
        </Link>
        <div className="card p-4 mt-3 text-sm text-red-300">{error}</div>
      </div>
    );
  }
  if (!lab || !project) return <div className="p-8 text-ink-300">Loading…</div>;

  return (
    <div className="p-4 sm:p-8 max-w-6xl min-w-0">
      <Link href={`/projects/${projectId}`} className="text-xs text-ink-300 hover:text-ink-100">
        ← {project.title || "Project"}
      </Link>
      <header className="mt-1 mb-5 flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">Try other openings</h1>
          <p className="text-sm text-ink-300 mt-1 max-w-2xl">
            Change the first line people hear (and, if you like, the first shot). The rest of the video is reused,
            so each opening costs a small part of the whole video.
          </p>
        </div>
        {lab.bases.length > 1 && base && (
          <label className="flex items-center gap-2 text-sm">
            <span className="text-ink-300">Video</span>
            <select
              className="input py-1.5 w-auto"
              value={base.render_id}
              onChange={(e) => router.replace(`/projects/${projectId}/hooks?base=${e.target.value}`)}
            >
              {lab.bases.map((b) => (
                <option key={b.render_id} value={b.render_id}>
                  {b.version_label || `Version ${b.version ?? ""}`}
                </option>
              ))}
            </select>
          </label>
        )}
      </header>

      {!base ? (
        <div className="card p-6 text-sm text-ink-300">
          There is no finished video with a voiceover yet. Make the video first, then come back to try other
          openings.
        </div>
      ) : (
        <>
          <Original base={base} drafts={drafts} current={currentWin} win={win} setWin={setWin} onChanged={load} />

          {!base.reusable ? (
            <>
              <div className="card p-4 mb-6 text-sm text-amber-200" role="status">
                {base.reuse_error || "This version can't be reused right now."}
              </div>
              {drafts.length > 0 && (
                <StrandedDrafts drafts={drafts} reason={base.reuse_error} onChanged={load} />
              )}
            </>
          ) : (
            <Drafts
              win={win}
              lab={lab}
              base={base}
              projectId={projectId}
              drafts={drafts}
              plan={plan}
              excluded={excluded}
              onDirty={markDirty}
              onListened={() => setPriceTick((t) => t + 1)}
              onToggle={(id, on) =>
                setExcluded((s) => {
                  const n = new Set(s);
                  if (on) n.delete(id);
                  else n.add(id);
                  return n;
                })
              }
              onChanged={load}
            />
          )}

          {base.reusable && drafts.length > 0 && (
            <section className="card p-4 mb-6 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3" aria-label="Make videos">
              <div className="min-w-0">
                <div className="text-sm font-medium">
                  {selected.length === 0
                    ? "Tick at least one opening to make it."
                    : planLoading || !plan
                    ? "Working out the price…"
                    : plan.summary}
                </div>
                <p className="text-[11px] text-ink-400 mt-0.5">
                  An estimate. Each opening becomes its own video next to the original.
                </p>
                {blockReason && selected.length > 0 && (
                  <p className="text-[11px] text-amber-200 mt-0.5" role="status">
                    {blockReason}
                  </p>
                )}
              </div>
              <button
                type="button"
                className="btn-primary disabled:opacity-50 shrink-0"
                disabled={making || selected.length === 0 || !plan || planLoading || !!blockReason}
                onClick={make}
                title={blockReason || undefined}
              >
                {making
                  ? "Starting…"
                  : `Make ${selected.length || ""} video${selected.length === 1 ? "" : "s"}${
                      plan && !planLoading ? ` · about ${fmtPrice(plan.total, "$0.00")}` : ""
                    }`}
              </button>
            </section>
          )}

          {running.length > 0 && (
            <section aria-labelledby="hl-running" className="mb-6">
              <h2 id="hl-running" className="text-base font-medium mb-2">Being made</h2>
              <ul className="space-y-2">
                {running.map((v) => (
                  <li key={v.id} className="card p-3 flex items-start gap-3" role="status">
                    <span className="chip text-xs font-semibold shrink-0">{v.label}</span>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm break-words">“{v.hook_text}”</div>
                      <div className="text-xs text-ink-300 mt-1 flex items-center gap-2">
                        <span className="w-2 h-2 rounded-full bg-amber-300 animate-pulse shrink-0" aria-hidden />
                        {progressText(v) || STATUS_TEXT[v.status] || "Working…"}
                      </div>
                    </div>
                    {v.status !== "composing" && (
                      <button
                        type="button"
                        className="btn-ghost text-xs shrink-0"
                        onClick={() => cancel(v)}
                        aria-label={`Stop making opening ${v.label}`}
                      >
                        Stop
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <Compare base={base} done={done} projectId={projectId} note={lab.youtube_note} onChanged={load} />
        </>
      )}
    </div>
  );
}

// ---------- the original opening + its window ----------

// Openings not made yet (or failed) of a version that can't be reused right
// now: they can't be edited or made, but they can still be removed.
function StrandedDrafts({
  drafts,
  reason,
  onChanged,
}: {
  drafts: HookVariant[];
  reason?: string | null;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState<number | null>(null);
  const remove = async (v: HookVariant) => {
    setBusy(v.id);
    try {
      await api.hookDeleteVariant(v.id);
      onChanged();
    } catch (e) {
      toast.error(errorText(e, "Couldn't remove it"));
    } finally {
      setBusy(null);
    }
  };
  return (
    <section aria-labelledby="hl-stranded" className="mb-6">
      <h2 id="hl-stranded" className="text-base font-medium mb-1">Openings not made</h2>
      <p className="text-[11px] text-ink-400 mb-2">
        These can&apos;t be made from this version right now
        {reason ? ` (${reason.replace(/\.$/, "")})` : ""}. You can remove them.
      </p>
      <ul className="space-y-2">
        {drafts.map((v) => (
          <li key={v.id} className="card p-3 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-sm truncate">
                <span className="text-ink-300 mr-1">{v.label}</span>
                {v.hook_text}
              </div>
              {v.status === "failed" && v.error && (
                <p className="text-[11px] text-red-300 mt-0.5">{v.error}</p>
              )}
            </div>
            <button
              type="button"
              className="btn-ghost text-xs shrink-0 disabled:opacity-50"
              disabled={busy === v.id}
              onClick={() => remove(v)}
            >
              {busy === v.id ? "Removing…" : "Remove"}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Original({
  base,
  drafts,
  current,
  win,
  setWin,
  onChanged,
}: {
  base: HookBase;
  drafts: HookVariant[];
  /** Where the opening ends for the drafts now. */
  current: number;
  /** The end being tried (new drafts use it). */
  win: number;
  setWin: (f: (w: number) => number) => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const found = base.window_seconds ?? 0;
  const [saving, setSaving] = useState(false);

  // A one-sentence script: the opening may run to the end of the voiceover.
  const oneSentence = !splitFirstSentence(base.script || "")[1];
  const max = oneSentence ? Math.max(base.audio_seconds, 0.3) : Math.max(base.audio_seconds - 0.3, 0.3);
  const nudge = (d: number) => setWin((w) => Math.min(Math.max(Math.round((w + d) * 10) / 10, 0.3), max));
  const changed = Math.abs(win - current) > 0.01;

  const apply = async () => {
    setSaving(true);
    try {
      for (const v of drafts) {
        if (Math.abs(v.window_seconds - win) > 0.01) await api.hookPatchVariant(v.id, { window_seconds: win });
      }
      toast.success("New openings will end there");
      onChanged();
    } catch (e) {
      toast.error(errorText(e, "Couldn't move the end of the opening"));
    } finally {
      setSaving(false);
    }
  };

  const previewUrl = win > 0 ? api.mediaUrl(`/api/renders/${base.render_id}/opening-audio?seconds=${win}`) : null;

  return (
    <section className="card p-4 mb-6" aria-labelledby="hl-original">
      <div className="flex flex-col sm:flex-row gap-4">
        {base.thumbnail_url && (
          <img
            src={api.mediaUrl(base.thumbnail_url)}
            alt=""
            className="w-24 sm:w-28 aspect-9/16 object-cover rounded-md bg-ink-800 shrink-0"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="chip text-xs font-semibold">A</span>
            <h2 id="hl-original" className="text-base font-medium">
              Original opening
            </h2>
            <span className="text-xs text-ink-400">{base.version_label}</span>
          </div>
          <p className="mt-2 text-base text-ink-100 break-words">“{base.hook_text}”</p>

          {base.window_seconds == null ? (
            <p className="text-xs text-amber-200 mt-2">Couldn&apos;t find where the opening ends in the voiceover.</p>
          ) : (
            <div className="mt-3 space-y-2">
              <div className="text-xs text-ink-300">
                The opening is spoken in the first <strong className="text-ink-100 tabular-nums">{sec(win)}</strong>
                {changed ? ` (was ${sec(current)})` : ""}. A new opening has to fit in that time.
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={() => nudge(-0.5)}
                  disabled={win <= 0.3}
                  aria-label="End the opening half a second earlier"
                >
                  − 0.5 s
                </button>
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={() => nudge(0.5)}
                  disabled={win >= max}
                  aria-label="End the opening half a second later"
                >
                  + 0.5 s
                </button>
                {Math.abs(win - found) > 0.01 && (
                  <button type="button" className="btn-ghost text-xs" onClick={() => setWin(() => found)}>
                    Reset
                  </button>
                )}
                {changed && drafts.length > 0 && (
                  <button type="button" className="btn-primary text-xs disabled:opacity-50" disabled={saving} onClick={apply}>
                    {saving ? "Saving…" : "Use this end"}
                  </button>
                )}
              </div>
              {previewUrl && (
                <div>
                  <div className="text-[11px] text-ink-400 mb-1">
                    Listen: the opening up to {sec(win)}, plus half a second. It should stop right after the first
                    sentence.
                  </div>
                  <audio key={previewUrl} controls preload="none" src={previewUrl} className="w-full max-w-md h-9" />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

// ---------- drafts: suggest, add, edit, shot 1, listen ----------

type Local = { key: string; style: string; text: string; saving?: boolean; error?: string };

function Drafts({
  win,
  lab,
  base,
  projectId,
  drafts,
  plan,
  excluded,
  onToggle,
  onDirty,
  onListened,
  onChanged,
}: {
  win: number;
  lab: HookLab;
  base: HookBase;
  projectId: number;
  drafts: HookVariant[];
  plan: HookPlan | null;
  excluded: Set<number>;
  onToggle: (id: number, on: boolean) => void;
  onDirty: (id: number, why: string) => void;
  onListened: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [suggesting, setSuggesting] = useState(false);
  const [local, setLocal] = useState<Local[]>([]);
  const seq = useRef(0);
  // Room on screen (a few at a time) and on the version (its opening limit).
  const baseRoom = Math.max((base.room ?? lab.max_per_request) - local.length, 0);
  const room = Math.min(Math.max(lab.max_per_request - drafts.length - local.length, 0), baseRoom);
  const full = (base.room ?? 1) <= 0;

  const create = async (items: { style: string; text: string }[]) => {
    const res = await api.hookCreateVariants(projectId, {
      base_render_id: base.render_id,
      window_seconds: win > 0 ? win : undefined,
      variants: items.map((i) => ({ hook_text: i.text, style: i.style, shot1_source: "keep" as Shot1Source })),
    });
    onChanged();
    return res;
  };

  const suggest = async () => {
    setSuggesting(true);
    try {
      const n = Math.max(Math.min(lab.max_per_request - drafts.length, lab.max_per_request, baseRoom), 1);
      const res = await api.hookSuggest(projectId, base.render_id, n);
      const ideas: HookSuggestion[] = res.suggestions.slice(0, n);
      if (ideas.length === 0) {
        toast.error("No usable openings came back. Try again, or write your own.");
        return;
      }
      try {
        await create(ideas.map((i) => ({ style: i.style, text: i.text })));
      } catch (e) {
        // Keep them on screen to fix and add by hand.
        setLocal((l) => [
          ...l,
          ...ideas.map((i) => ({ key: `s${++seq.current}`, style: i.style, text: i.text, error: errorText(e) })),
        ]);
      }
    } catch (e) {
      toast.error(errorText(e, "Couldn't get suggestions"));
    } finally {
      setSuggesting(false);
    }
  };

  const saveLocal = async (item: Local) => {
    setLocal((l) => l.map((x) => (x.key === item.key ? { ...x, saving: true, error: "" } : x)));
    try {
      await create([{ style: item.style, text: item.text.trim() }]);
      setLocal((l) => l.filter((x) => x.key !== item.key));
    } catch (e) {
      setLocal((l) => l.map((x) => (x.key === item.key ? { ...x, saving: false, error: errorText(e) } : x)));
    }
  };

  const atLimit = drafts.length >= lab.max_per_request;

  return (
    <section aria-labelledby="hl-new" className="mb-6">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div>
          <h2 id="hl-new" className="text-base font-medium">New openings</h2>
          <p className="text-[11px] text-ink-400">
            Up to {lab.max_per_request} at a time. Nothing is paid until you listen or make the videos.
          </p>
          {full && (
            <p className="text-[11px] text-amber-200" role="status">
              This version has as many other openings as it can ({base.max_per_base}). Remove one to add another.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-primary text-sm disabled:opacity-50"
            onClick={suggest}
            disabled={suggesting || atLimit || room === 0}
            title={
              full
                ? "This version has as many openings as it can"
                : atLimit
                ? "Make or remove the openings below first"
                : undefined
            }
          >
            {suggesting ? "Thinking…" : `Suggest openings · about ${fmtPrice(lab.suggest_cost_estimate)}`}
          </button>
          <button
            type="button"
            className="btn-ghost text-sm disabled:opacity-50"
            disabled={room === 0}
            onClick={() => setLocal((l) => [...l, { key: `m${++seq.current}`, style: "custom", text: "" }])}
          >
            Add my own
          </button>
        </div>
      </div>

      {drafts.length === 0 && local.length === 0 ? (
        <div className="card p-5 text-sm text-ink-300">
          Ask for suggestions in different styles (a question, a bold claim, a number…), or write your own.
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
          {drafts.map((v) => (
            <DraftCard
              key={v.id}
              onDirty={onDirty}
              onListened={onListened}
              lab={lab}
              base={base}
              v={v}
              row={plan?.variants.find((r) => r.id === v.id) ?? null}
              included={!excluded.has(v.id)}
              onToggle={(on) => onToggle(v.id, on)}
              onChanged={onChanged}
            />
          ))}
          {local.map((item) => (
            <LocalCard
              key={item.key}
              win={win}
              lab={lab}
              base={base}
              item={item}
              onChange={(next) => setLocal((l) => l.map((x) => (x.key === item.key ? { ...x, ...next } : x)))}
              onSave={() => saveLocal(item)}
              onDrop={() => setLocal((l) => l.filter((x) => x.key !== item.key))}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function StyleSelect({
  lab,
  value,
  onChange,
  disabled,
  id,
}: {
  lab: HookLab;
  value: string;
  onChange: (s: string) => void;
  disabled?: boolean;
  id: string;
}) {
  return (
    <select
      id={id}
      aria-label="Opening style"
      className="chip chip-active text-xs py-0.5 pr-6 cursor-pointer disabled:cursor-default"
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      {lab.styles.map((s) => (
        <option key={s.key} value={s.key}>
          {s.label}
        </option>
      ))}
    </select>
  );
}

function FitMeter({ text, base, window }: { text: string; base: HookBase; window: number }) {
  const { est, fits, overflow } = estimateFit(text, base.hook_text, window);
  if (!text.trim()) return null;
  const pct = Math.min((est / Math.max(window * TEMPO_MAX, 0.1)) * 100, 100);
  return (
    <div className="mt-2">
      <div className="h-1.5 rounded-full bg-ink-800 overflow-hidden" aria-hidden>
        <div className={clsx("h-full", fits ? "bg-emerald-400" : "bg-amber-400")} style={{ width: `${pct}%` }} />
      </div>
      <div className={clsx("text-[11px] mt-1", fits ? "text-emerald-300" : "text-amber-200")}>
        {fits
          ? `Fits · about ${sec(est)} of ${sec(window)}`
          : `Might be about ${overflow.toFixed(1)} s too long · listen to check, or shorten it`}
      </div>
    </div>
  );
}

function LocalCard({
  win,
  lab,
  base,
  item,
  onChange,
  onSave,
  onDrop,
}: {
  win: number;
  lab: HookLab;
  base: HookBase;
  item: Local;
  onChange: (next: Partial<Local>) => void;
  onSave: () => void;
  onDrop: () => void;
}) {
  return (
    <div className="card p-4 flex flex-col gap-2 min-w-0">
      <div className="flex items-center justify-between gap-2">
        <StyleSelect lab={lab} id={`style-${item.key}`} value={item.style} onChange={(s) => onChange({ style: s })} />
        <button type="button" className="btn-ghost text-xs" onClick={onDrop}>
          Remove
        </button>
      </div>
      <label className="sr-only" htmlFor={`text-${item.key}`}>
        Opening line
      </label>
      <AutoTextarea
        id={`text-${item.key}`}
        className="input min-h-[72px]"
        maxLength={300}
        placeholder="Your first sentence"
        value={item.text}
        onChange={(e) => onChange({ text: e.target.value })}
        autoFocus={!item.text}
      />
      <FitMeter text={item.text} base={base} window={win} />
      {item.error && <p className="text-[11px] text-red-300">{item.error}</p>}
      <button
        type="button"
        className="btn-primary text-sm disabled:opacity-50 self-start"
        disabled={!item.text.trim() || item.saving}
        onClick={onSave}
      >
        {item.saving ? "Adding…" : "Add this opening"}
      </button>
    </div>
  );
}

// A line as the server saves it: trimmed, runs of spaces made single.
const squash = (t: string) => (t || "").split(/\s+/).filter(Boolean).join(" ");

function DraftCard({
  lab,
  base,
  v,
  row,
  included,
  onToggle,
  onDirty,
  onListened,
  onChanged,
}: {
  lab: HookLab;
  base: HookBase;
  v: HookVariant;
  row: HookPlanRow | null;
  included: boolean;
  onToggle: (on: boolean) => void;
  onDirty: (id: number, why: string) => void;
  onListened: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [text, setText] = useState(v.hook_text);
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const [preview, setPreview] = useState<HookPreviewAudio | null>(null);
  const [error, setError] = useState<string>("");
  const [shotDirty, setShotDirty] = useState("");
  useEffect(() => {
    setText(v.hook_text);
  }, [v.hook_text]);
  // A Listen result is about the audio as it was: a moved end ("Use this
  // end") or a changed line clears it on the server, so it goes here too.
  useEffect(() => {
    setPreview(null);
  }, [v.window_seconds, v.audio_url]);

  const patch = async (body: Parameters<typeof api.hookPatchVariant>[1]) => {
    setBusy(true);
    setError("");
    try {
      await api.hookPatchVariant(v.id, body);
      setPreview(null);
      onChanged();
      return true;
    } catch (e) {
      setError(errorText(e, "Couldn't save"));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const saveText = () => {
    const t = squash(text);
    if (!t || t === v.hook_text) {
      // Nothing to save (only spaces changed): show the saved line again.
      setText(v.hook_text);
      return;
    }
    patch({ hook_text: t });
  };

  // Free only for the original line itself (the server's is_original rule).
  const norm = (t: string) => (t || "").split(/\s+/).filter(Boolean).join(" ").toLowerCase();
  const isOriginal = v.style === "original" && norm(v.hook_text) === norm(base.hook_text);
  const listenCost = v.audio_url || isOriginal ? 0 : (v.hook_text.length / 1000) * lab.tts_cost_per_1k_chars;
  const listen = async () => {
    setListening(true);
    setError("");
    try {
      const res = await api.hookPreviewAudio(v.id);
      setPreview(res);
      if (res.fits) onChanged();
      onListened();
    } catch (e) {
      toast.error(errorText(e, "Couldn't make the voice"));
    } finally {
      setListening(false);
    }
  };

  const remove = async () => {
    try {
      await api.hookDeleteVariant(v.id);
      onChanged();
    } catch (e) {
      toast.error(errorText(e, "Couldn't remove it"));
    }
  };

  const audioUrl = preview ? preview.url : v.audio_url;
  // The server saves the line with runs of spaces made single, so only a
  // change in the words themselves is unsaved.
  const textDirty = squash(text) !== v.hook_text;
  const dirtyWhy = busy
    ? `Saving opening ${v.label}…`
    : textDirty
    ? `Opening ${v.label}'s new text isn't saved yet: click outside it first`
    : shotDirty
    ? `Opening ${v.label}: ${shotDirty}`
    : "";
  useEffect(() => {
    onDirty(v.id, dirtyWhy);
  }, [v.id, dirtyWhy, onDirty]);
  useEffect(() => () => onDirty(v.id, ""), [v.id, onDirty]);

  return (
    <div className={clsx("card p-4 flex flex-col gap-2 min-w-0", !included && "opacity-70")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="chip text-xs font-semibold">{v.label}</span>
          <StyleSelect
            lab={lab}
            id={`style-${v.id}`}
            value={v.style}
            disabled={busy}
            onChange={(s) => patch({ style: s })}
          />
        </div>
        <label className="flex items-center gap-1.5 text-xs text-ink-300">
          <input
            type="checkbox"
            className="accent-iris"
            checked={included}
            onChange={(e) => onToggle(e.target.checked)}
            aria-label={`Make opening ${v.label}`}
          />
          <span aria-hidden>Make it</span>
        </label>
      </div>
      {v.status === "failed" && (
        <p className="text-[11px] text-red-300 rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1">
          Last try failed: {v.error || "unknown error"}. Change it or make it again.
        </p>
      )}
      <label className="sr-only" htmlFor={`text-${v.id}`}>
        Opening {v.label}
      </label>
      <AutoTextarea
        id={`text-${v.id}`}
        className="input min-h-[72px]"
        maxLength={300}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={saveText}
      />
      <FitMeter text={text} base={base} window={v.window_seconds} />

      <Shot1Choice base={base} v={v} row={row} busy={busy} onPatch={patch} onDirty={setShotDirty} />

      {row && row.notes.length > 0 && (
        <ul className="text-[11px] text-ink-300 space-y-0.5">
          {row.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      {row && row.warnings.length > 0 && (
        <ul className="text-[11px] text-amber-200 space-y-0.5">
          {row.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {row && !row.ok && <p className="text-[11px] text-red-300">{row.error}</p>}
      {error && <p className="text-[11px] text-red-300">{error}</p>}

      <div className="mt-1 space-y-2">
        {audioUrl && !textDirty && (
          <audio key={audioUrl} controls preload="none" src={api.mediaUrl(audioUrl)} className="w-full h-9" />
        )}
        {preview && !preview.fits && (
          <p className="text-[11px] text-amber-200">
            {preview.message || `About ${preview.overflow_seconds.toFixed(1)} s too long. Shorten it.`} You can still
            hear the line on its own above.
          </p>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-2">
            {!audioUrl && (
              <button
                type="button"
                className="btn-ghost text-xs disabled:opacity-50"
                disabled={listening || busy || textDirty}
                onClick={listen}
                title={textDirty ? "Click outside the text to save it first" : undefined}
              >
                {listening ? "Making the voice…" : `Listen · ${fmtPrice(listenCost)}`}
              </button>
            )}
            <button type="button" className="btn-ghost text-xs" onClick={remove} disabled={busy}>
              Remove
            </button>
          </div>
          <span className="text-xs text-ink-200 tabular-nums">
            {row ? `about ${fmtPrice(row.total, "$0.00")}` : ""}
          </span>
        </div>
      </div>
    </div>
  );
}

function Shot1Choice({
  base,
  v,
  row,
  busy,
  onPatch,
  onDirty,
}: {
  base: HookBase;
  v: HookVariant;
  row: HookPlanRow | null;
  busy: boolean;
  onPatch: (body: Parameters<typeof api.hookPatchVariant>[1]) => Promise<boolean>;
  /** Why the shot-1 choice on screen isn't saved yet ("" when it is). */
  onDirty: (why: string) => void;
}) {
  const shot1 = base.shot1;
  // "new" and "take" need more input before they can be saved.
  const [choice, setChoice] = useState<Shot1Source>(v.shot1_source);
  const [prompt, setPrompt] = useState(v.shot1_prompt || shot1?.prompt || "");
  useEffect(() => {
    setChoice(v.shot1_source);
  }, [v.shot1_source]);
  useEffect(() => {
    if (v.shot1_prompt) setPrompt(v.shot1_prompt);
  }, [v.shot1_prompt]);
  const unsaved =
    choice === "take" && v.shot1_source !== "take"
      ? "pick a take for shot 1, or keep it"
      : choice === "new" && v.shot1_source !== "new"
      ? "the new shot 1 prompt isn't saved yet (Use this prompt)"
      : choice === "new" && prompt.trim() !== (v.shot1_prompt || "")
      ? "the changed shot 1 prompt isn't saved yet (Save prompt)"
      : "";
  useEffect(() => {
    onDirty(unsaved);
  }, [unsaved, onDirty]);
  const radios = useRef<(HTMLButtonElement | null)[]>([]);

  if (!shot1) return null;
  const otherTakes = shot1.takes.filter((t) => t.file_exists !== false);
  const chosenCost = row?.shot1 && row.shot1.source === v.shot1_source ? row.shot1.cost : null;
  const priceFor = (s: Shot1Source) => {
    if (s === "keep" || s === "take") return "free";
    if (s === v.shot1_source && chosenCost != null) return `about ${fmtPrice(chosenCost, "$0.00")}`;
    return "paid";
  };

  const options: { id: Shot1Source; label: string; hint: string; show: boolean }[] = [
    { id: "keep", label: "Keep shot 1", hint: "The same first shot", show: true },
    {
      id: "take",
      label: "Another take",
      hint: "A clip of shot 1 you already have",
      show: otherTakes.length > 1 || (otherTakes.length === 1 && otherTakes[0].id !== shot1.take_id),
    },
    { id: "new", label: "New shot 1", hint: `A new ${sec(shot1.seconds)} clip from a prompt`, show: true },
    {
      id: "lipsync",
      label: "Redo talking head",
      hint: "Lip-sync shot 1 to the new words",
      show: shot1.can_redo_talking_head,
    },
  ];

  const pick = (id: Shot1Source) => {
    setChoice(id);
    if (id === "keep" || id === "lipsync") onPatch({ shot1_source: id });
  };
  const shown = options.filter((o) => o.show);
  const focusIdx = Math.max(
    shown.findIndex((o) => o.id === choice),
    0
  );
  // Radio-group keys: arrows move (and select), one tab stop for the group.
  const onKey = (e: React.KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const j = (i + d + shown.length) % shown.length;
    radios.current[j]?.focus();
    pick(shown[j].id);
  };

  return (
    <fieldset className="mt-1">
      <legend className="text-[11px] uppercase tracking-wider text-ink-400 mb-1">Shot 1</legend>
      <div className="grid grid-cols-2 gap-1.5" role="radiogroup" aria-label={`Shot 1 for opening ${v.label}`}>
        {shown.map((o, i) => (
            <button
              key={o.id}
              ref={(el) => {
                radios.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={choice === o.id}
              tabIndex={i === focusIdx ? 0 : -1}
              disabled={busy}
              onKeyDown={(e) => onKey(e, i)}
              onClick={() => pick(o.id)}
              title={o.hint}
              className={clsx(
                "rounded-md border px-2 py-1.5 text-left text-xs transition disabled:opacity-60",
                choice === o.id ? "border-iris/70 bg-iris/15 text-ink-100" : "border-ink-700 hover:border-ink-500 text-ink-200"
              )}
            >
              <span className="block font-medium">{o.label}</span>
              <span className="block text-[10px] text-ink-400">{priceFor(o.id)}</span>
            </button>
          ))}
      </div>

      {choice === "take" && (
        <div className="mt-2">
          <div className="text-[11px] text-ink-400 mb-1">Pick a take</div>
          <div className="flex gap-2 overflow-x-auto pb-1">
            {otherTakes.map((t) => {
              const on = v.shot1_source === "take" && v.shot1_take_id === t.id;
              return (
                <button
                  key={t.id}
                  type="button"
                  disabled={busy}
                  onClick={() => onPatch({ shot1_source: "take", shot1_take_id: t.id })}
                  aria-pressed={on}
                  aria-label={`Take ${t.id}${t.id === shot1.take_id ? " (in the video now)" : ""}`}
                  className={clsx(
                    "relative shrink-0 w-16 aspect-9/16 rounded-md overflow-hidden border-2 bg-ink-800",
                    on ? "border-iris" : "border-transparent hover:border-ink-500"
                  )}
                >
                  <video src={`${api.mediaUrl(t.url)}#t=0.1`} preload="metadata" muted playsInline className="w-full h-full object-cover" />
                  {t.id === shot1.take_id && (
                    <span className="absolute bottom-0 inset-x-0 text-[9px] bg-ink-950/80 text-ink-200">now</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {choice === "new" && (
        <div className="mt-2 space-y-1.5">
          <label className="text-[11px] text-ink-400 block" htmlFor={`prompt-${v.id}`}>
            What shot 1 should show
          </label>
          <AutoTextarea
            id={`prompt-${v.id}`}
            className="input min-h-[64px] text-xs"
            maxLength={2000}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
          <button
            type="button"
            className="btn-ghost text-xs disabled:opacity-50"
            disabled={busy || !prompt.trim() || (v.shot1_source === "new" && prompt.trim() === v.shot1_prompt)}
            onClick={() => onPatch({ shot1_source: "new", shot1_prompt: prompt.trim() })}
          >
            {v.shot1_source === "new" ? "Save prompt" : "Use this prompt"}
          </button>
          {v.shot1_source !== "new" && (
            <p className="text-[10px] text-ink-400">The price shows once the prompt is saved.</p>
          )}
        </div>
      )}
    </fieldset>
  );
}

// ---------- finished openings side by side ----------

function Compare({
  base,
  done,
  projectId,
  note,
  onChanged,
}: {
  base: HookBase;
  done: HookVariant[];
  projectId: number;
  note: string;
  onChanged: () => void;
}) {
  const toast = useToast();
  if (done.length === 0) return null;
  const columns: {
    key: string;
    label: string;
    text: string;
    style: string;
    renderId: number;
    videoUrl: string;
    thumb: string | null;
    versionLabel: string;
    cost: string;
    variant?: HookVariant;
  }[] = [
    {
      key: "A",
      label: "A",
      text: base.hook_text,
      style: "Original opening",
      renderId: base.render_id,
      videoUrl: base.video_url,
      thumb: base.thumbnail_url,
      versionLabel: base.version_label,
      cost: "",
    },
    ...done.map((v) => ({
      key: String(v.id),
      label: v.label,
      text: v.hook_text,
      style: v.style_label,
      renderId: v.render_id!,
      videoUrl: v.video_url || "",
      thumb: v.thumbnail_url,
      versionLabel: v.version_label,
      cost: v.actual_cost != null ? `This opening cost ${fmtPrice(v.actual_cost, "$0.00")}` : "",
      variant: v,
    })),
  ];

  const archive = async (v: HookVariant) => {
    try {
      await api.hookDeleteVariant(v.id);
      toast.success(`Opening ${v.label} removed here; its video stays in the render history`);
      onChanged();
    } catch (e) {
      toast.error(errorText(e, "Couldn't remove it"));
    }
  };

  return (
    <section aria-labelledby="hl-compare" className="mb-6">
      <h2 id="hl-compare" className="text-base font-medium">Side by side</h2>
      <p className="text-[11px] text-amber-200 mt-1 mb-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2">
        {note}
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {columns.map((c) => (
          <div key={c.key} className="min-w-0 space-y-3">
            <div className="card overflow-hidden">
              <div className="px-3 py-2 flex items-start gap-2 border-b border-ink-800">
                <span className="chip text-xs font-semibold shrink-0">{c.label}</span>
                <div className="min-w-0 min-h-[3.25rem]">
                  <div className="text-[11px] text-ink-400">
                    {c.style} · {c.versionLabel}
                  </div>
                  <div className="text-sm break-words line-clamp-2" title={c.text}>“{c.text}”</div>
                </div>
              </div>
              <div className="bg-black">
                <video
                  src={api.mediaUrl(c.videoUrl)}
                  poster={c.thumb ? api.mediaUrl(c.thumb) : undefined}
                  controls
                  playsInline
                  preload="metadata"
                  className="w-full max-h-[60vh] mx-auto"
                />
              </div>
              <div className="px-3 py-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-ink-400">
                <span>{c.cost}</span>
                <span className="flex gap-2">
                  <Link href={`/projects/${projectId}/render?id=${c.renderId}`} className="hover:underline text-ink-300">
                    Open
                  </Link>
                  {c.variant && !c.variant.published && (
                    <button type="button" className="hover:underline text-ink-300" onClick={() => archive(c.variant!)}>
                      Remove from here
                    </button>
                  )}
                </span>
              </div>
            </div>
            <PublishPanel projectId={projectId} renderId={c.renderId} />
          </div>
        ))}
      </div>
    </section>
  );
}
