"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { Dialog } from "@/components/Dialog";
import { ASSET_SLOTS, OwnerKind } from "@/components/AssignDialog";
import { useToast } from "@/components/Toaster";
import { usd } from "@/components/ImageOps";
import {
  api,
  Asset,
  Avatar,
  CharacterSheetView,
  Ingredient,
  PictureIntent,
  SogniEditor,
  StudioJob,
  WithPictures,
  errorText,
} from "@/lib/api";

/** A model in a Studio picker; price_text is the rough price in plain words ("" = unknown).
 *  with_pictures: the Sogni editor it runs as when it gets reference pictures. */
export type ModelOption = { id: string; name: string; price_text?: string; with_pictures?: WithPictures | null };

/** What reference pictures are for, as the server decides: a Cast
 *  character's picture (not its logo) keeps a likeness; anything else guides. */
export function pictureIntent(assets: Pick<Asset, "owner_kind" | "asset_type">[]): PictureIntent | null {
  if (assets.length === 0) return null;
  return assets.some((a) => a.owner_kind === "avatar" && a.asset_type !== "logo") ? "identity" : "guide";
}

/** The Sogni editor a model really runs as with `count` reference pictures; null = the model itself runs. */
export function editorFor(m: ModelOption | undefined, intent: PictureIntent | null, count: number): SogniEditor | null {
  const e = intent && count > 0 ? m?.with_pictures?.[intent] : null;
  if (!e) return null;
  return count > e.max_refs && e.over_max ? e.over_max : e;
}

/** This editor's price with `count` reference pictures, as Spend counts it:
 *  "about $0.07 per image", or "free (Free Spark)" for a Sogni editor on Free Spark. */
export function editorPriceText(e: SogniEditor, count: number): string {
  const total = e.usd + e.usd_per_ref * Math.max(count - 1, 0);
  return total > 0 ? `about ${usd(total)} per image` : e.price_text || "free";
}

/** Each Sogni editor you can get once, picked through a model that runs as it (its own model first). */
export function editorChoices(
  models: ModelOption[],
  intent: PictureIntent | null,
  count: number
): { id: string; editor: SogniEditor }[] {
  const out = new Map<string, { id: string; editor: SogniEditor }>();
  for (const m of models) {
    const e = editorFor(m, intent, count);
    if (!e) continue;
    const cur = out.get(e.id);
    if (!cur || (e.own && !cur.editor.own)) out.set(e.id, { id: m.id, editor: e });
  }
  return Array.from(out.values());
}

/** What a Studio picture job's reference pictures are for, and how many are
 *  still there (deleted ones aren't sent); no intent for clips and operations. */
export function jobPictures(
  job: Pick<StudioJob, "output_kind" | "operation" | "ref_intent" | "reference_asset_ids" | "missing_reference_ids">
): { intent: PictureIntent | null; count: number } {
  const intent = job.output_kind === "image" && !job.operation ? job.ref_intent ?? null : null;
  const missing = new Set(job.missing_reference_ids || []);
  const count = intent ? (job.reference_asset_ids || []).filter((id) => !missing.has(id)).length : 0;
  return { intent, count };
}

/** "Ran on Krea 2 Identity Edit (Sogni)" when a Sogni editor made it, not the
 *  picked model; "Tried on …" when it started there and then failed. */
export function ranOnText(
  job: Pick<StudioJob, "ran_on_name" | "ran_on_editor" | "provider_model" | "status">
): string | null {
  if (!job.ran_on_name) return null;
  if (job.ran_on_editor && `sogni/${job.ran_on_editor}` === job.provider_model) return null;
  return `${job.status === "failed" ? "Tried on" : "Ran on"} ${job.ran_on_name} (Sogni)`;
}

/** The options of a Studio model picker. With reference pictures, each Sogni
 *  model says which Sogni editor it runs as, and each editor you can get is
 *  listed once on top. `note` adds a few words to an option. */
export function ModelOptions({
  models,
  intent,
  count,
  prices = true,
  note,
}: {
  models: ModelOption[];
  intent: PictureIntent | null;
  count: number;
  prices?: boolean;
  note?: (id: string, editor: SogniEditor | null) => string;
}) {
  const editors = editorChoices(models, intent, count);
  const price = (text?: string) => (prices && text ? ` · ${text}` : "");
  const items = models.map((m) => {
    const e = editorFor(m, intent, count);
    return (
      <option key={m.id} value={m.id}>
        {m.name || m.id}
        {e ? ` — with your pictures runs as ${e.name}${price(editorPriceText(e, count))}` : price(m.price_text)}
        {note?.(m.id, e) || ""}
      </option>
    );
  });
  if (editors.length === 0) return <>{items}</>;
  return (
    <>
      <optgroup label="Sogni editors (what runs with your pictures)">
        {editors.map(({ id, editor }) => (
          <option key={`editor-${editor.id}`} value={id}>
            {editor.name} (Sogni){price(editorPriceText(editor, count))}
            {note?.(id, editor) || ""}
          </option>
        ))}
      </optgroup>
      <optgroup label="All models">{items}</optgroup>
    </>
  );
}

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
  // With reference pictures a Sogni model runs on one of Sogni's editors:
  // the one this job ran on, else the one its model runs as now.
  const isOp = Boolean(job.operation);
  const { intent, count } = jobPictures(job);
  const jobEditor =
    (intent && job.ran_on_editor) || editorFor(models.find((m) => m.id === job.provider_model), intent, count)?.id || null;
  const sameEditor = (e: SogniEditor | null) => Boolean(jobEditor) && e?.id === jobEditor;
  // The first choice is a model that would make it differently.
  const others = models.filter((m) => m.id !== job.provider_model);
  const fresh = others.filter((m) => !sameEditor(editorFor(m, intent, count)));
  const [model, setModel] = useState(fresh[0]?.id || others[0]?.id || models[0]?.id || "");
  const [busy, setBusy] = useState(false);
  const kind = job.output_kind === "image" ? "image" : "clip";
  const picked = models.find((m) => m.id === model);
  const pickedEditor = editorFor(picked, intent, count);
  const pickedPrice = pickedEditor ? editorPriceText(pickedEditor, count) : picked?.price_text;
  const ranOn = ranOnText(job);
  const note = (id: string, e: SogniEditor | null) =>
    id === job.provider_model ? " (same as before)" : sameEditor(e) ? " (same Sogni editor as before)" : "";
  return (
    <Dialog title="Try another model" onClose={onClose}>
      <p className="text-xs text-ink-300 line-clamp-3">{job.prompt || `Studio ${kind}`}</p>
      {job.provider_model && (
        <p className="text-[11px] text-ink-400">
          Made with {job.provider_model}
          {ranOn ? ` · ${ranOn}` : ""}
        </p>
      )}
      {models.length === 0 ? (
        <p className="text-sm text-ink-400">No {kind} models are available right now.</p>
      ) : (
        <div>
          <label className="label" htmlFor="retry-model">Model</label>
          <select id="retry-model" className="input" value={model} onChange={(e) => setModel(e.target.value)}>
            <ModelOptions models={models} intent={intent} count={count} note={note} />
          </select>
          {pickedEditor && (
            <p className="text-xs text-ink-200 mt-1.5">
              With these reference pictures, Sogni runs this as {pickedEditor.name}
              {sameEditor(pickedEditor) ? ", the same editor as before." : "."}
            </p>
          )}
          {pickedPrice && (
            <p className="text-xs text-ink-200 mt-1.5">
              {pickedPrice.charAt(0).toUpperCase() + pickedPrice.slice(1)}
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
  // The views use up to 4 of the character's own pictures (never the logo),
  // so a Sogni model runs as its likeness editor.
  const sheetRefs = Math.min(
    4,
    avatar.assets.filter((a) => (a.mime_type || "image/").startsWith("image/") && a.asset_type !== "logo").length
  );
  const sheetEditor = editorFor(models.find((m) => m.id === model), "identity", sheetRefs);
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
              <ModelOptions models={models} intent="identity" count={sheetRefs} prices={false} />
            </select>
            {sheetEditor && (
              <p className="text-[11px] text-ink-400 mt-1">Sogni runs this as {sheetEditor.name}.</p>
            )}
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
