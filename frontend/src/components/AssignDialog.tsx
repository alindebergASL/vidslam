"use client";
import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { api, Asset, Avatar, Ingredient, StudioJob } from "@/lib/api";

export type OwnerKind = "avatar" | "ingredient";

// Slots per owner kind, in the order the backend validates them. The main
// picture comes first because that's what people usually mean by "assign".
export const ASSET_SLOTS: Record<OwnerKind, { value: string; label: string }[]> = {
  avatar: [
    { value: "hero", label: "Main picture (hero)" },
    { value: "reference_sheet", label: "Reference sheet" },
    { value: "expression_sheet", label: "Expression sheet" },
    { value: "outfit_sheet", label: "Outfit sheet" },
    { value: "lifestyle", label: "Lifestyle shot" },
    { value: "logo", label: "Logo" },
    { value: "other", label: "Other" },
  ],
  ingredient: [
    { value: "hero", label: "Main picture (hero)" },
    { value: "reference", label: "Reference" },
    { value: "other", label: "Other" },
  ],
};

export function slotLabel(kind: OwnerKind, value: string): string {
  return ASSET_SLOTS[kind].find((s) => s.value === value)?.label || value;
}

/** Studio image jobs that have a picture to save (saved ones can be saved again). */
export function finishedImageJobs(jobs: StudioJob[]): StudioJob[] {
  return jobs.filter((j) => j.output_kind === "image" && ["completed", "saved"].includes(j.status));
}

/**
 * Generation reads the FIRST hero, so a new main picture only takes effect
 * if older heroes step aside. Moves them to the plain reference slot.
 */
export async function demoteOtherHeroes(kind: OwnerKind, assets: Asset[], keepId: number): Promise<number> {
  const others = assets.filter((a) => a.asset_type === "hero" && a.id !== keepId);
  const fallback = kind === "avatar" ? "reference_sheet" : "reference";
  await Promise.all(others.map((a) => api.updateAsset(a.id, { asset_type: fallback })));
  return others.length;
}

function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

/**
 * Save a finished Studio image to a character or scene, in a chosen slot.
 *
 * - From Studio: pass `job`; the owner is picked here (default: the job's owner).
 * - From Cast: pass `owner`; the Studio image is picked here.
 * Each save makes a new asset with its own copy of the picture.
 */
export function AssignDialog({
  job,
  owner,
  onClose,
  onSaved,
}: {
  job?: StudioJob;
  owner?: { kind: OwnerKind; id: number; name: string };
  onClose: () => void;
  onSaved: (asset: Asset) => void;
}) {
  const toast = useToast();
  useEscape(onClose);
  const [avatars, setAvatars] = useState<Avatar[] | null>(null);
  const [ingredients, setIngredients] = useState<Ingredient[] | null>(null);
  const [jobs, setJobs] = useState<StudioJob[] | null>(job ? [job] : null);
  const [pickedJobId, setPickedJobId] = useState<number | null>(job?.id ?? null);
  const [target, setTarget] = useState<string>(
    owner ? `${owner.kind}:${owner.id}` : job ? `${job.owner_kind}:${job.owner_id}` : ""
  );
  const [slot, setSlot] = useState("hero");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listAvatars().then(setAvatars).catch(() => setAvatars([]));
    api.listIngredients().then(setIngredients).catch(() => setIngredients([]));
    if (!job) {
      api
        .listStudioJobs()
        .then((js) => setJobs(finishedImageJobs(js)))
        .catch(() => setJobs([]));
    }
  }, [job]);

  const [kind, idStr] = target.split(":") as [OwnerKind | "", string];
  const ownerId = Number(idStr);
  const ownerKind: OwnerKind = kind === "ingredient" ? "ingredient" : "avatar";
  const slots = ASSET_SLOTS[ownerKind];
  // Keep the slot valid when switching between a character and a scene.
  useEffect(() => {
    if (!slots.some((s) => s.value === slot)) setSlot("hero");
  }, [ownerKind]); // eslint-disable-line react-hooks/exhaustive-deps

  const ownerEntity = useMemo(() => {
    if (!kind) return null;
    const list: (Avatar | Ingredient)[] = (kind === "avatar" ? avatars : ingredients) || [];
    return list.find((o) => o.id === ownerId) || null;
  }, [kind, ownerId, avatars, ingredients]);
  const ownerMissing = !owner && kind !== "" && avatars !== null && ingredients !== null && !ownerEntity;

  const save = async () => {
    if (!pickedJobId || !kind || !ownerId) return;
    setBusy(true);
    let asset: Asset;
    try {
      asset = await api.saveStudioJob(pickedJobId, {
        owner_kind: ownerKind,
        owner_id: ownerId,
        asset_type: slot,
      });
    } catch (e: any) {
      toast.error(e.message || "Couldn't save the picture");
      setBusy(false);
      return;
    }
    // The asset exists now; a demotion failure must not look like a failed
    // save, or a retry would make a duplicate (second) main picture.
    const refLabel = slotLabel(ownerKind, ownerKind === "avatar" ? "reference_sheet" : "reference");
    let demoted = 0;
    let demoteFailed = false;
    if (slot === "hero") {
      try {
        // Re-read the owner so heroes added since the dialog opened count too.
        const fresh = ownerKind === "avatar" ? await api.getAvatar(ownerId) : await api.getIngredient(ownerId);
        demoted = await demoteOtherHeroes(ownerKind, fresh.assets, asset.id);
      } catch {
        demoteFailed = true;
      }
    }
    const name = owner?.name || ownerEntity?.name || "cast";
    toast.success(
      `Saved to ${name} as ${slotLabel(ownerKind, slot)}` +
        (demoted ? ` (previous main picture moved to ${refLabel})` : "")
    );
    if (demoteFailed) toast.error(`Saved, but couldn't move the old main picture to ${refLabel}`);
    setBusy(false);
    onSaved(asset);
  };

  const titleId = "assign-dialog-h";
  return (
    <div className="fixed inset-0 z-40 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="card w-full max-w-2xl p-5 max-h-[85vh] overflow-y-auto space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3">
          <h2 id={titleId} className="text-lg font-medium">
            {owner ? `Add a Studio image to ${owner.name}` : "Assign to a character or scene"}
          </h2>
          <button className="text-ink-300 hover:text-white p-2 -m-2" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        {job ? (
          <div className="flex gap-3 items-start">
            <img
              src={api.studioPreview(job.id)}
              alt=""
              className="w-20 aspect-9/16 object-cover rounded-md bg-ink-800 border border-ink-700 shrink-0"
            />
            <div className="text-xs text-ink-300 line-clamp-4">{job.prompt || "Studio image"}</div>
          </div>
        ) : (
          <fieldset>
            <legend className="label">Studio image</legend>
            {jobs === null ? (
              <div className="text-sm text-ink-400">Loading…</div>
            ) : jobs.length === 0 ? (
              <div className="text-sm text-ink-400">
                No finished Studio images yet. Generate one in{" "}
                <a href="/studio" className="underline">Studio</a>, then come back.
              </div>
            ) : (
              <ul className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                {jobs.map((j) => (
                  <li key={j.id}>
                    <button
                      type="button"
                      onClick={() => setPickedJobId(j.id)}
                      aria-pressed={pickedJobId === j.id}
                      title={j.prompt}
                      className={clsx(
                        "block w-full aspect-9/16 rounded-md overflow-hidden bg-ink-800 border-2 transition",
                        pickedJobId === j.id ? "border-accent" : "border-ink-700 hover:border-ink-500"
                      )}
                    >
                      <img src={api.studioPreview(j.id)} alt={j.prompt || "Studio image"} className="w-full h-full object-cover" loading="lazy" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </fieldset>
        )}

        <div className="grid sm:grid-cols-2 gap-3">
          {!owner && (
            <div>
              <label className="label" htmlFor="assign-owner">Character or scene</label>
              <select
                id="assign-owner"
                className="input"
                value={ownerMissing ? "" : target}
                onChange={(e) => setTarget(e.target.value)}
              >
                <option value="" disabled>
                  — pick one —
                </option>
                {(avatars || []).length > 0 && (
                  <optgroup label="Characters">
                    {avatars!.map((a) => (
                      <option key={`av-${a.id}`} value={`avatar:${a.id}`}>{a.name}</option>
                    ))}
                  </optgroup>
                )}
                {(ingredients || []).length > 0 && (
                  <optgroup label="Scenes & ingredients">
                    {ingredients!.map((i) => (
                      <option key={`ing-${i.id}`} value={`ingredient:${i.id}`}>
                        {i.name} ({i.kind})
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </div>
          )}
          <div>
            <label className="label" htmlFor="assign-slot">Use it as</label>
            <select id="assign-slot" className="input" value={slot} onChange={(e) => setSlot(e.target.value)}>
              {slots.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>
        </div>
        {slot === "hero" && (
          <p className="text-[11px] text-ink-400">
            The main picture is the face/look used for generation. Any current main picture moves to the reference slot.
          </p>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary disabled:opacity-50"
            disabled={busy || !pickedJobId || !kind || !ownerId || ownerMissing}
            onClick={save}
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
