"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { Dialog } from "@/components/Dialog";
import { useToast } from "@/components/Toaster";
import { checkBlockedWhy, checkCostText, InspectionChip, useClipCheckSettings } from "@/components/InspectionChip";
import { shotTypeLabel } from "@/lib/labels";
import { AddShotType, api, Project, Shot, ShotEdit, ShotEditImpact, Take, errorText } from "@/lib/api";
import { AutoTextarea } from "@/components/AutoTextarea";

const usd = (n: number) => `$${n.toFixed(2)}`;
const ADD_TYPES: AddShotType[] = ["b_roll", "hero", "talking_head", "transition"];

type ImpactQuery = Parameters<typeof api.shotEditImpact>[1];

/** What the edit would put out of date, fetched again when the query changes. */
export function useEditImpact(projectId: number, q: ImpactQuery): ShotEditImpact | null | undefined {
  // undefined = loading; null = could not be worked out.
  const [impact, setImpact] = useState<ShotEditImpact | null | undefined>(undefined);
  const key = JSON.stringify(q);
  useEffect(() => {
    let live = true;
    setImpact(undefined);
    const t = setTimeout(() => {
      api
        .shotEditImpact(projectId, q)
        .then((r) => live && setImpact(r))
        .catch(() => live && setImpact(null));
    }, 150);
    return () => {
      live = false;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, key]);
  return impact;
}

const shotList = (orders: number[]) =>
  orders.length === 1
    ? `shot ${orders[0]}`
    : `shots ${orders.slice(0, -1).join(", ")} and ${orders[orders.length - 1]}`;

/** Says which lip-synced clips the edit puts out of date, and what remaking them costs. */
export function EditImpactNote({ impact, verb }: { impact: ShotEditImpact | null | undefined; verb: string }) {
  if (impact === undefined)
    return <li className="text-ink-400">Checking which clips this affects…</li>;
  if (impact === null)
    return (
      <li className="text-amber-300">
        Couldn&apos;t check which clips this affects. Lip-synced clips after this point may go out of date and be
        made again (paid) at the next Generate video.
      </li>
    );
  if (impact.stale_count === 0)
    return <li>No clip already made goes out of date.</li>;
  const orders = impact.stale_shots.map((x) => x.shot_order).sort((a, b) => a - b);
  const one = impact.stale_count === 1;
  return (
    <li className="text-amber-300">
      {verb} moves the voiceover under {one ? "1 lip-synced clip" : `${impact.stale_count} lip-synced clips`} (
      {shotList(orders)}), so {one ? "it goes" : "they go"} out of date. The next Generate video makes{" "}
      {one ? "it" : "them"} again: about {usd(impact.stale_cost)}.
    </li>
  );
}

/** "+ Add shot": a new shot with no clip. Nothing is generated or paid for. */
export function AddShotDialog({
  project,
  after,
  onClose,
  onDone,
}: {
  project: Project;
  /** Insert after this shot; null = at the end (before the end card). */
  after: Shot | null;
  onClose: () => void;
  onDone: (r: ShotEdit) => void;
}) {
  const toast = useToast();
  const [shotType, setShotType] = useState<AddShotType>("b_roll");
  const [prompt, setPrompt] = useState("");
  const [duration, setDuration] = useState(5);
  const [busy, setBusy] = useState(false);

  const add = async () => {
    setBusy(true);
    try {
      const r = await api.addShot(project.id, {
        after_shot_id: after?.id ?? null,
        shot_type: shotType,
        prompt,
        duration_seconds: duration,
      });
      toast.success(
        `Shot ${r.shot?.shot_order ?? ""} added${r.captions_resynced ? " · captions resynced to the new length" : ""}`
      );
      onDone(r);
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't add the shot"));
      setBusy(false);
    }
  };

  const where = after && after.shot_type !== "end_card" ? `after shot ${after.shot_order}` : "at the end";
  const impact = useEditImpact(project.id, {
    op: "add",
    after_shot_id: after?.id ?? null,
    duration_seconds: duration >= 1 && duration <= 60 ? duration : 5,
    shot_type: shotType,
  });

  return (
    <Dialog title={`Add a shot ${where}`} onClose={onClose}>
      <div>
        <div className="label">Shot type</div>
        <div className="flex flex-wrap gap-2">
          {ADD_TYPES.map((t) => (
            <button
              key={t}
              type="button"
              className={clsx("chip text-xs", shotType === t && "chip-active")}
              onClick={() => setShotType(t)}
              aria-pressed={shotType === t}
            >
              {shotTypeLabel(t)}
            </button>
          ))}
        </div>
      </div>
      <div>
        <label className="label" htmlFor="add-shot-prompt">Prompt</label>
        <AutoTextarea
          id="add-shot-prompt"
          className="input min-h-[80px]"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="What this shot shows. You can also write it later."
        />
      </div>
      <div>
        <label className="label" htmlFor="add-shot-duration">Duration (seconds)</label>
        <input
          id="add-shot-duration"
          type="number"
          className="input w-28"
          min={1}
          max={60}
          step={0.5}
          value={duration}
          onChange={(e) => setDuration(parseFloat(e.target.value) || 1)}
        />
      </div>
      <ul className="text-xs text-ink-300 list-disc pl-5 space-y-1">
        <li>
          Adding it is free: the shot starts without a clip. Its clip is made (and paid for
          {impact && impact.new_clip_cost > 0 ? `, about ${usd(impact.new_clip_cost)}` : ""}) when you generate the
          video or regenerate the shot.
        </li>
        <EditImpactNote impact={impact} verb="Adding it" />
        <li>The video gets longer, so captions are spread over the new length.</li>
      </ul>
      <div className="flex justify-end gap-2">
        <button className="btn-ghost text-xs" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn-primary text-xs disabled:opacity-50"
          onClick={add}
          disabled={busy || !(duration >= 1 && duration <= 60)}
        >
          {busy ? "Adding…" : "Add shot"}
        </button>
      </div>
    </Dialog>
  );
}

/** Asks before deleting a shot; says when paid clips are thrown away. */
export function DeleteShotDialog({
  project,
  shot,
  onClose,
  onDone,
}: {
  project: Project;
  shot: Shot;
  onClose: () => void;
  onDone: (r: ShotEdit) => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const takes = shot.takes_count ?? 0;
  const paidClip = shot.clip_path !== "" || takes > 0;
  const clipCount = Math.max(takes, shot.clip_path ? 1 : 0);
  const endCard = shot.shot_type === "end_card";
  const impact = useEditImpact(project.id, { op: "delete", shot_id: shot.id });

  const del = async () => {
    setBusy(true);
    try {
      const r = await api.deleteShot(project.id, shot.id);
      toast.success(
        `Shot deleted${r.clips_discarded > 0 ? ` · ${r.clips_discarded} clip${r.clips_discarded === 1 ? "" : "s"} removed` : ""}${
          r.captions_resynced ? " · captions resynced to the new length" : ""
        }`
      );
      onDone(r);
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't delete the shot"));
      setBusy(false);
    }
  };

  return (
    <Dialog title={`Delete shot ${shot.shot_order}?`} onClose={onClose}>
      <ul className="text-sm text-ink-200 list-disc pl-5 space-y-1">
        {paidClip ? (
          <li className="text-amber-300">
            {clipCount === 1
              ? "Its clip was paid for and is thrown away."
              : `Its ${clipCount} clips (the current one and earlier takes) were paid for and are thrown away.`}{" "}
            This can&apos;t be undone.
          </li>
        ) : (
          <li>It has no clip yet, so nothing paid for is lost.</li>
        )}
        {!endCard && <EditImpactNote impact={impact} verb="Deleting it" />}
        {!endCard && <li>The video gets shorter, so captions are spread over the new length.</li>}
        <li>Finished videos in the render history stay as they are.</li>
      </ul>
      <div className="flex justify-end gap-2">
        <button className="btn-ghost text-xs" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn-primary text-xs bg-red-600 hover:bg-red-500 disabled:opacity-50"
          onClick={del}
          disabled={busy}
        >
          {busy ? "Deleting…" : "Delete shot"}
        </button>
      </div>
    </Dialog>
  );
}

/** Asks before duplicating a shot when that puts lip-synced clips out of date. */
export function DuplicateShotDialog({
  project,
  shot,
  impact,
  onClose,
  onConfirm,
}: {
  project: Project;
  shot: Shot;
  impact: ShotEditImpact | null;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog title={`Duplicate shot ${shot.shot_order}?`} onClose={onClose}>
      <ul className="text-sm text-ink-200 list-disc pl-5 space-y-1">
        <li>
          The copy has no clip yet. Its clip is made (and paid for
          {impact && impact.new_clip_cost > 0 ? `, about ${usd(impact.new_clip_cost)}` : ""}) when you generate the video.
        </li>
        <EditImpactNote impact={impact} verb="The copy" />
        <li>The video gets longer, so captions are spread over the new length.</li>
      </ul>
      <div className="flex justify-end gap-2">
        <button className="btn-ghost text-xs" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn-primary text-xs disabled:opacity-50"
          disabled={busy || project.shots.every((x) => x.id !== shot.id)}
          onClick={async () => {
            setBusy(true);
            await onConfirm();
            setBusy(false);
          }}
        >
          {busy ? "Duplicating…" : "Duplicate shot"}
        </button>
      </div>
    </Dialog>
  );
}

/** Every kept clip of a shot; pick the one the video uses. */
export function TakesDialog({
  project,
  shot,
  onClose,
  onChanged,
  onRecompose,
}: {
  project: Project;
  shot: Shot;
  onClose: () => void;
  /** The shot's clip or its takes changed: reload the project. */
  onChanged: () => void;
  /** Free re-compose; offered when a finished video exists. */
  onRecompose?: () => void;
}) {
  const toast = useToast();
  const [takes, setTakes] = useState<Take[] | null>(null);
  const [keep, setKeep] = useState(10);
  const [busy, setBusy] = useState<number | null>(null);
  const [switched, setSwitched] = useState(false);
  const [checking, setChecking] = useState<number | null>(null);
  const inFlight = ["submitted", "polling", "running"].includes(shot.status);
  const checkSettings = useClipCheckSettings();
  const checkBlocked = checkBlockedWhy(checkSettings);
  const checkCost = checkCostText(checkSettings?.cost_per_check);

  const load = async () => {
    try {
      const r = await api.shotTakes(project.id, shot.id);
      setTakes(r.takes);
      setKeep(r.keep);
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't load the takes"));
      setTakes([]);
    }
  };
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, shot.id]);

  const use = async (t: Take) => {
    setBusy(t.id);
    try {
      await api.useTake(project.id, shot.id, t.id);
      toast.success(
        `Shot ${shot.shot_order} now uses this take. ${
          project.status === "completed" ? "Re-compose (free) builds the video with it." : "Nothing was paid for."
        }`
      );
      setSwitched(true);
      await load();
      onChanged();
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't switch to this take"));
    } finally {
      setBusy(null);
    }
  };

  // Paid (about cost_per_check): one look by the vision model at a few frames.
  const check = async (t: Take) => {
    setChecking(t.id);
    try {
      const r = await api.checkClip(project.id, shot.id, t.id);
      const v = r.inspection.verdict;
      if (v === "ok") toast.success("The check says this take looks right");
      else if (v === "problem")
        toast.info(`The check flagged: ${r.inspection.issues[0]?.detail || r.inspection.summary || "something"}`);
      else toast.info(r.inspection.summary || "Couldn't check this clip");
      await load();
      if (t.current) onChanged();
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't check this take"));
    } finally {
      setChecking(null);
    }
  };

  const remove = async (t: Take) => {
    if (!window.confirm("Delete this take? Its clip file is removed and can't be brought back.")) return;
    setBusy(t.id);
    try {
      await api.deleteTake(project.id, shot.id, t.id);
      toast.success("Take deleted");
      await load();
      onChanged();
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't delete the take"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog title={`Takes of shot ${shot.shot_order}`} onClose={onClose} className="max-w-3xl">
      <p className="text-xs text-ink-400">
        Every clip made for this shot is kept, so a re-roll never loses a good one. Switching takes is free.
        The newest {keep} are kept per shot; older ones are deleted, never the one in use. The clip check&apos;s
        verdict on each take helps you pick; it&apos;s advice only.
      </p>
      {inFlight && (
        <p className="text-xs text-amber-300">
          A new clip for this shot is being made. You can switch takes once it is back.
        </p>
      )}
      {switched && project.status === "completed" && onRecompose && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 p-2 text-xs text-emerald-200">
          <span>The finished video still has the old clip. Re-compose (free) builds it with this take.</span>
          <button
            className="btn-ghost text-xs"
            onClick={() => {
              onRecompose();
              onClose();
            }}
          >
            Re-compose (free)
          </button>
        </div>
      )}
      {takes === null ? (
        <div className="h-24 skeleton" aria-busy="true" />
      ) : takes.length === 0 ? (
        <div className="text-sm text-ink-300">No clips kept for this shot yet.</div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {takes.map((t, i) => (
            <div
              key={t.id}
              className={clsx(
                "rounded-md border p-2 space-y-1.5",
                t.current ? "border-accent" : "border-ink-700"
              )}
            >
              {t.file_exists ? (
                <video
                  src={`${api.mediaUrl(t.url)}#t=0.1`}
                  preload="metadata"
                  controls
                  playsInline
                  className="w-full rounded-sm bg-black max-h-56 object-contain"
                />
              ) : (
                <div className="w-full h-24 rounded-sm bg-ink-800 text-[11px] text-ink-400 flex items-center justify-center">
                  File is gone
                </div>
              )}
              <div className="flex items-center justify-between gap-1">
                <span className="text-xs font-medium text-ink-100">Take {takes.length - i}</span>
                {t.current && <span className="chip text-[10px] py-0.5 chip-active">In use</span>}
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                <InspectionChip inspection={t.inspection} />
                {t.file_exists && (
                  <button
                    type="button"
                    className="text-[11px] text-ink-300 underline-offset-2 hover:underline disabled:opacity-50 disabled:no-underline"
                    onClick={() => check(t)}
                    disabled={checking !== null || !!checkBlocked}
                    title={
                      checkBlocked ||
                      `A vision model looks at a few frames of this take and says if anything looks off${
                        checkCost ? ` (${checkCost})` : ""
                      }. Advice only.`
                    }
                  >
                    {checking === t.id
                      ? "Checking…"
                      : `${t.inspection && t.inspection.verdict !== "skipped" ? "Check again" : "Check this take"}${
                          checkCost ? ` · ${checkCost}` : ""
                        }`}
                  </button>
                )}
              </div>
              <div className="text-[11px] text-ink-300 break-words">
                {t.provider_model || t.provider || "Unknown model"}
                {t.aspect_ratio ? ` · ${t.aspect_ratio}` : ""}
              </div>
              <div className="text-[11px] text-ink-400">
                {t.cost_usd != null ? usd(t.cost_usd) : "Cost unknown"}
                {t.created_at ? ` · ${new Date(t.created_at).toLocaleString()}` : ""}
              </div>
              {t.aspect_ratio && t.aspect_ratio !== project.aspect_ratio && (
                <div className="text-[11px] text-amber-300">
                  Made at {t.aspect_ratio}; the project is {project.aspect_ratio}.
                </div>
              )}
              {t.note && <div className="text-[11px] text-ink-400">{t.note}</div>}
              {!t.current && (
                <div className="flex gap-1.5">
                  <button
                    className="btn-primary text-[11px] px-2 py-1 flex-1 disabled:opacity-50"
                    onClick={() => use(t)}
                    disabled={busy !== null || inFlight || !t.file_exists}
                  >
                    Use this take
                  </button>
                  <button
                    className="btn-ghost text-[11px] px-2 py-1 disabled:opacity-50"
                    onClick={() => remove(t)}
                    disabled={busy !== null}
                    aria-label={`Delete take ${takes.length - i}`}
                  >
                    Delete
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="flex justify-end">
        <button className="btn-ghost text-xs" onClick={onClose}>
          Close
        </button>
      </div>
    </Dialog>
  );
}
