"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { Dialog } from "@/components/Dialog";
import { ASSET_SLOTS, OwnerKind } from "@/components/AssignDialog";
import { useToast } from "@/components/Toaster";
import { api, Avatar, CharacterSheetView, Ingredient, StudioJob, errorText } from "@/lib/api";

/** A model in a Studio picker; price_text is the rough price in plain words ("" = unknown). */
export type ModelOption = { id: string; name: string; price_text?: string };

/** "$0.04", "~$0.04" for an estimate, "free" for 0, null when nothing is recorded. */
export function costText(job: Pick<StudioJob, "cost_usd" | "cost_is_estimate">): string | null {
  const c = job.cost_usd;
  if (c === null || c === undefined) return null;
  if (c === 0) return "free";
  const amount = c >= 0.01 ? c.toFixed(2) : c.toFixed(3);
  return `${job.cost_is_estimate ? "~" : ""}$${amount}`;
}

const STATUS_LABELS: Record<string, string> = {
  pending: "Queued",
  submitted: "Starting",
  polling: "Making…",
  failed: "Failed",
  completed: "Done",
  saved: "Saved",
};

/** A Studio job's status in plain words ("Making…" for polling); the raw value goes in a title. */
export function studioStatusLabel(job: Pick<StudioJob, "status" | "error">): string {
  if (job.status === "failed" && job.error === "Cancelled") return "Cancelled";
  return STATUS_LABELS[job.status] || job.status;
}

/** One line for a bulk-save result: "Saved 3, skipped 1 (not an image)". */
export function bulkSaveSummary(saved: number, skipped: { reason: string }[]): string {
  if (skipped.length === 0) return `Saved ${saved}`;
  const reasons = Array.from(new Set(skipped.map((s) => s.reason).filter(Boolean)));
  return `Saved ${saved}, skipped ${skipped.length}${reasons.length ? ` (${reasons.join("; ")})` : ""}`;
}

/** Pick a model to re-run a Studio job with; the list is the prompt bar's list for that output kind. */
export function ModelPickerDialog({
  job,
  models,
  onPick,
  onClose,
}: {
  job: StudioJob;
  models: ModelOption[];
  onPick: (model: string) => Promise<void> | void;
  onClose: () => void;
}) {
  const others = models.filter((m) => m.id !== job.provider_model);
  const [model, setModel] = useState(others[0]?.id || models[0]?.id || "");
  const [busy, setBusy] = useState(false);
  const kind = job.output_kind === "image" ? "image" : "clip";
  const picked = models.find((m) => m.id === model);
  const isOp = Boolean(job.operation);
  return (
    <Dialog title="Try another model" onClose={onClose}>
      <p className="text-xs text-ink-300 line-clamp-3">{job.prompt || `Studio ${kind}`}</p>
      {job.provider_model && (
        <p className="text-[11px] text-ink-400">Made with {job.provider_model}</p>
      )}
      {models.length === 0 ? (
        <p className="text-sm text-ink-400">No {kind} models are available right now.</p>
      ) : (
        <div>
          <label className="label" htmlFor="retry-model">Model</label>
          <select id="retry-model" className="input" value={model} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name || m.id}
                {m.price_text ? ` · ${m.price_text}` : ""}
                {m.id === job.provider_model ? " (same as before)" : ""}
              </option>
            ))}
          </select>
          {picked?.price_text && (
            <p className="text-xs text-ink-200 mt-1.5">
              {picked.price_text.charAt(0).toUpperCase() + picked.price_text.slice(1)}
            </p>
          )}
        </div>
      )}
      <p className="text-[11px] text-ink-400">
        {isOp
          ? "Runs the same step again on the same picture with this model. The original stays."
          : `Makes a new ${kind} with the same prompt, avoid list, references and aspect. The original stays.`}
      </p>
      <div className="flex justify-end gap-2 pt-1">
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn-primary disabled:opacity-50"
          disabled={busy || !model}
          onClick={async () => {
            setBusy(true);
            try {
              await onPick(model);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Starting…" : "Generate →"}
        </button>
      </div>
    </Dialog>
  );
}

/** Save several Studio images to one character or scene, in one slot. */
export function BulkSaveDialog({
  jobs,
  avatars,
  ingredients,
  defaultOwner,
  defaultSlot,
  onClose,
  onDone,
}: {
  jobs: StudioJob[];
  avatars: Avatar[];
  ingredients: Ingredient[];
  /** "avatar:12" / "ingredient:3"; else the first job's owner. */
  defaultOwner?: string;
  defaultSlot?: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const first = jobs[0];
  const [target, setTarget] = useState(
    defaultOwner || (first ? `${first.owner_kind}:${first.owner_id}` : "")
  );
  const [kind, idStr] = target.split(":") as [OwnerKind | "", string];
  const ownerKind: OwnerKind = kind === "ingredient" ? "ingredient" : "avatar";
  const ownerId = Number(idStr);
  // Several main pictures at once would fight over which one generation uses,
  // so the main-picture slot stays with "Assign to…" on a single image.
  const slots = ASSET_SLOTS[ownerKind].filter((s) => s.value !== "hero");
  const [slot, setSlot] = useState(defaultSlot || slots[0].value);
  useEffect(() => {
    if (!slots.some((s) => s.value === slot)) setSlot(slots[0].value);
  }, [ownerKind]); // eslint-disable-line react-hooks/exhaustive-deps
  const [busy, setBusy] = useState(false);

  const images = jobs.filter((j) => j.output_kind === "image").length;
  const others = jobs.length - images;
  const ownerExists =
    kind === "avatar"
      ? avatars.some((a) => a.id === ownerId)
      : kind === "ingredient"
      ? ingredients.some((i) => i.id === ownerId)
      : false;

  const save = async () => {
    if (!ownerExists) return;
    setBusy(true);
    try {
      const res = await api.bulkSaveStudioJobs({
        ids: jobs.map((j) => j.id),
        owner_kind: ownerKind,
        owner_id: ownerId,
        asset_type: slot,
      });
      const msg = bulkSaveSummary(res.saved.length, res.skipped);
      if (res.saved.length > 0) toast.success(msg);
      else toast.error(msg);
      onDone();
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't save the pictures"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={`Save ${jobs.length === 1 ? "1 result" : `${jobs.length} results`} to the cast`} onClose={onClose}>
      <p className="text-xs text-ink-300">
        {images === 1 ? "1 image" : `${images} images`} will be copied to the cast
        {others > 0 ? `; ${others === 1 ? "1 clip is" : `${others} clips are`} skipped (images only)` : ""}.
      </p>
      <div className="grid sm:grid-cols-2 gap-3">
        <div>
          <label className="label" htmlFor="bulk-save-owner">Character or scene</label>
          <select
            id="bulk-save-owner"
            className="input"
            value={ownerExists ? target : ""}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="" disabled>
              — pick one —
            </option>
            {avatars.length > 0 && (
              <optgroup label="Characters">
                {avatars.map((a) => (
                  <option key={`av-${a.id}`} value={`avatar:${a.id}`}>{a.name}</option>
                ))}
              </optgroup>
            )}
            {ingredients.length > 0 && (
              <optgroup label="Scenes & ingredients">
                {ingredients.map((i) => (
                  <option key={`ing-${i.id}`} value={`ingredient:${i.id}`}>
                    {i.name} ({i.kind})
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="bulk-save-slot">Use them as</label>
          <select id="bulk-save-slot" className="input" value={slot} onChange={(e) => setSlot(e.target.value)}>
            {slots.map((s) => (
              <option key={s.value} value={s.value}>{s.label}</option>
            ))}
          </select>
        </div>
      </div>
      <p className="text-[11px] text-ink-400">
        To make one of them the main picture, use “Assign to…” on that image.
      </p>
      <div className="flex justify-end gap-2 pt-1">
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="btn-primary disabled:opacity-50" disabled={busy || !ownerExists || images === 0} onClick={save}>
          {busy ? "Saving…" : `Save ${images === 1 ? "1 image" : `${images} images`}`}
        </button>
      </div>
    </Dialog>
  );
}

const DEFAULT_SHEET_VIEWS = ["front", "three_quarter", "profile", "back"];

/** Generate a set of consistent views of one cast member from their own pictures. */
export function CharacterSheetDialog({
  avatar,
  models,
  defaultModel,
  onClose,
  onCreated,
}: {
  avatar: Avatar;
  models: ModelOption[];
  defaultModel: string;
  onClose: () => void;
  onCreated: (res: { batch_ids: number[]; jobs: StudioJob[] }) => void;
}) {
  const toast = useToast();
  const [views, setViews] = useState<CharacterSheetView[] | null>(null);
  const [picked, setPicked] = useState<string[]>(DEFAULT_SHEET_VIEWS);
  const [model, setModel] = useState(
    models.some((m) => m.id === defaultModel) ? defaultModel : models[0]?.id || ""
  );
  const [style, setStyle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .characterSheetViews()
      .then((vs) => {
        setViews(vs);
        setPicked((cur) => cur.filter((id) => vs.some((v) => v.id === id)));
      })
      .catch((e: any) => {
        setViews([]);
        setError(errorText(e, "Couldn't load the views"));
      });
  }, []);

  const hasPictures = avatar.assets.some((a) => (a.mime_type || "image/").startsWith("image/"));
  const toggle = (id: string) =>
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  const generate = async () => {
    if (picked.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const order = (views || []).map((v) => v.id);
      const res = await api.makeCharacterSheet({
        avatar_id: avatar.id,
        views: [...picked].sort((a, b) => order.indexOf(a) - order.indexOf(b)),
        model: model || null,
        ...(style.trim() ? { style: style.trim() } : {}),
      });
      toast.success(`Making ${res.jobs.length} views of ${avatar.name}`);
      onCreated(res);
    } catch (e: any) {
      setError(errorText(e, "Couldn't start the character sheet"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={`Character sheet: ${avatar.name}`} onClose={onClose} className="max-w-xl">
      <p className="text-xs text-ink-300">
        Makes one image per view, each using {avatar.name}’s pictures as references so the face, hair, body and
        outfit stay the same.
      </p>
      {!hasPictures && (
        <p className="text-xs text-accent">
          {avatar.name} has no pictures yet. Add one in <a href="/cast" className="underline">Cast</a> first.
        </p>
      )}
      <fieldset>
        <legend className="label">Views</legend>
        {views === null ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : (
          <div className="grid grid-cols-2 gap-1.5">
            {views.map((v) => (
              <label
                key={v.id}
                className={clsx(
                  "flex items-center gap-2 text-sm rounded-md border px-2 py-1.5 cursor-pointer",
                  picked.includes(v.id) ? "border-accent" : "border-ink-700 hover:border-ink-500"
                )}
              >
                <input type="checkbox" checked={picked.includes(v.id)} onChange={() => toggle(v.id)} />
                <span className="flex-1">{v.label}</span>
                <span className="text-[10px] text-ink-400">{v.aspect_ratio}</span>
              </label>
            ))}
          </div>
        )}
      </fieldset>
      <div className="grid sm:grid-cols-2 gap-3">
        {models.length > 0 && (
          <div>
            <label className="label" htmlFor="sheet-model">Image model</label>
            <select id="sheet-model" className="input" value={model} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option key={m.id} value={m.id}>{m.name || m.id}</option>
              ))}
            </select>
          </div>
        )}
        <div>
          <label className="label" htmlFor="sheet-style">Style (optional)</label>
          <input
            id="sheet-style"
            className="input"
            value={style}
            maxLength={300}
            onChange={(e) => setStyle(e.target.value)}
            placeholder="e.g. studio photo, plain grey background"
          />
        </div>
      </div>
      <p className="text-xs text-ink-300">
        {picked.length === 1 ? "1 image" : `${picked.length} images`}
        {picked.length > 1 ? `: about ${picked.length}× the cost of one` : ""}
      </p>
      {error && <p className="text-xs text-accent">{error}</p>}
      <div className="flex justify-end gap-2 pt-1">
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn-primary disabled:opacity-50"
          disabled={busy || picked.length === 0 || !hasPictures}
          onClick={generate}
        >
          {busy ? "Starting…" : `Generate ${picked.length === 1 ? "1 view" : `${picked.length} views`} →`}
        </button>
      </div>
    </Dialog>
  );
}

/** The slot a character sheet is usually saved to. */
export function sheetSlot(kind: OwnerKind): string {
  const slots = ASSET_SLOTS[kind].map((s) => s.value);
  return slots.includes("reference") ? "reference" : slots.includes("reference_sheet") ? "reference_sheet" : slots[0];
}
