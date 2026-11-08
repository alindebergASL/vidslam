"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { Dropzone } from "@/components/Dropzone";
import { AssignDialog, slotLabel } from "@/components/AssignDialog";
import { useToast } from "@/components/Toaster";
import { EnhanceButton } from "@/components/EnhanceButton";
import { PresetsMenu } from "@/components/PresetsMenu";
import { api, Asset, Avatar, Ingredient, PromptPreset, StudioAspect, StudioJob } from "@/lib/api";

const ASPECTS: { id: StudioAspect; label: string }[] = [
  { id: "", label: "Auto" },
  { id: "9:16", label: "9:16" },
  { id: "16:9", label: "16:9" },
  { id: "1:1", label: "1:1" },
];

/** What Remix or a preset loads into the prompt bar. `nonce` makes a repeat apply. */
type PromptLoad = {
  nonce: number;
  outputKind: "image" | "video_clip";
  prompt: string;
  negative_prompt: string;
  model: string;
  aspect_ratio: StudioAspect;
  duration_seconds?: number;
};

function asAspect(v: string | undefined | null): StudioAspect {
  return v === "9:16" || v === "16:9" || v === "1:1" ? v : "";
}

/** Clip lengths the Studio bar offers. */
const CLIP_SECONDS = [5, 8, 10, 15, 20, 30] as const;

/** Snap any duration (the API allows 1-60s) to the nearest offered clip length, so the select shows what gets billed. */
function snapClipSeconds(d: number): number {
  return CLIP_SECONDS.reduce<number>((a, b) => (Math.abs(b - d) < Math.abs(a - d) ? b : a), CLIP_SECONDS[0]);
}

/** Placeholder rows for every job a create request made, so all show as pending tiles. */
function batchPlaceholders(first: StudioJob): StudioJob[] {
  const ids = first.batch_ids && first.batch_ids.length > 0 ? first.batch_ids : [first.id];
  const all = ids.includes(first.id) ? ids : [first.id, ...ids];
  // Siblings share the first job's status (the backend creates the whole batch as "pending").
  return all.map((id) => (id === first.id ? first : { ...first, id, batch_ids: [] }));
}

type Filter = "all" | "images" | "videos" | "characters" | "scenes" | "uploads";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All Media" },
  { id: "images", label: "Images" },
  { id: "videos", label: "Videos" },
  { id: "characters", label: "Characters" },
  { id: "scenes", label: "Scenes" },
  { id: "uploads", label: "Uploads" },
];

export default function StudioPage() {
  return (
    <AuthGate>
      <Studio />
    </AuthGate>
  );
}

type Tile = {
  key: string;
  kind: "asset" | "job";
  imageUrl?: string;
  videoUrl?: string;
  label: string;
  sub: string;
  status?: string;
  selectable?: boolean;
  assetId?: number;
  jobId?: number;
  ownerKind?: "avatar" | "ingredient";
};

function Studio() {
  const router = useRouter();
  const [filter, setFilter] = useState<Filter>("all");
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [jobs, setJobs] = useState<StudioJob[]>([]);
  const [selectedRefs, setSelectedRefs] = useState<number[]>([]);
  const [pendingJobIds, setPendingJobIds] = useState<number[]>([]);
  const [assigning, setAssigning] = useState<StudioJob | null>(null);
  const [load, setLoad] = useState<PromptLoad | null>(null);
  const toast = useToast();

  // Remix: put a finished result's settings back into the prompt bar.
  const remix = (job: StudioJob) => {
    setSelectedRefs(job.reference_asset_ids || []);
    setLoad({
      nonce: Date.now(),
      outputKind: job.output_kind,
      prompt: job.prompt,
      negative_prompt: job.negative_prompt || "",
      model: job.provider_model || "",
      aspect_ratio: asAspect(job.aspect_ratio),
      duration_seconds: job.output_kind === "video_clip" ? job.duration_seconds : undefined,
    });
  };

  const reload = async () => {
    const [av, ing, jb] = await Promise.all([
      api.listAvatars(),
      api.listIngredients(),
      api.listStudioJobs(),
    ]);
    setAvatars(av);
    setIngredients(ing);
    setJobs(jb);
  };

  useEffect(() => {
    reload();
  }, []);

  // Poll for in-flight jobs.
  useEffect(() => {
    if (pendingJobIds.length === 0) return;
    const interval = setInterval(async () => {
      const refreshed = await api.listStudioJobs();
      setJobs(refreshed);
      const stillPending = refreshed
        .filter((j) => pendingJobIds.includes(j.id))
        .filter((j) => !["completed", "failed", "saved"].includes(j.status))
        .map((j) => j.id);
      setPendingJobIds(stillPending);
    }, 1500);
    return () => clearInterval(interval);
  }, [pendingJobIds]);

  const tiles = useMemo<Tile[]>(() => {
    const out: Tile[] = [];
    // Studio jobs (newest first; already ordered by API).
    for (const j of jobs) {
      if (filter !== "all" && filter !== "images" && filter !== "videos") continue;
      if (filter === "images" && j.output_kind !== "image") continue;
      if (filter === "videos" && j.output_kind !== "video_clip") continue;
      const isImage = j.output_kind === "image";
      const ready = j.status === "completed" || j.status === "saved";
      out.push({
        key: `job-${j.id}`,
        kind: "job",
        imageUrl: ready && isImage ? api.studioPreview(j.id) : undefined,
        videoUrl: ready && !isImage ? api.studioPreview(j.id) : undefined,
        label: j.prompt.slice(0, 60) || `${j.output_kind} job`,
        sub: `${j.output_kind} · ${j.owner_kind} #${j.owner_id}`,
        status: j.status,
        jobId: j.id,
        ownerKind: j.owner_kind,
      });
    }
    // Cast assets.
    const showAvatarAssets = filter === "all" || filter === "characters" || filter === "uploads";
    const showIngredientAssets =
      filter === "all" || filter === "scenes" || filter === "uploads";
    if (showAvatarAssets) {
      for (const av of avatars) {
        for (const a of av.assets) {
          if (filter === "uploads" && a.source !== "upload") continue;
          out.push({
            key: `av-${a.id}`,
            kind: "asset",
            imageUrl: api.publicAsset(a.public_token),
            label: `${av.name} · ${slotLabel("avatar", a.asset_type)}`,
            sub: `Character · ${a.source}`,
            selectable: true,
            assetId: a.id,
          });
        }
      }
    }
    if (showIngredientAssets) {
      for (const ing of ingredients) {
        if (filter === "scenes" && ing.kind !== "scene") continue;
        for (const a of ing.assets) {
          if (filter === "uploads" && a.source !== "upload") continue;
          out.push({
            key: `ing-${a.id}`,
            kind: "asset",
            imageUrl: api.publicAsset(a.public_token),
            label: `${ing.name} · ${slotLabel("ingredient", a.asset_type)}`,
            sub: `${ing.kind} · ${a.source}`,
            selectable: true,
            assetId: a.id,
          });
        }
      }
    }
    return out;
  }, [avatars, ingredients, jobs, filter]);

  const toggleRef = (assetId: number) => {
    setSelectedRefs((cur) =>
      cur.includes(assetId) ? cur.filter((x) => x !== assetId) : [...cur, assetId]
    );
  };

  // Studio results and cast images are separate files, so deleting one
  // never removes the other.
  const deleteTile = async (t: Tile) => {
    const what =
      t.kind === "job"
        ? "this Studio result"
        : `"${t.label}" from the cast (projects that use it lose this picture)`;
    if (!window.confirm(`Delete ${what}? This can't be undone.`)) return;
    try {
      if (t.kind === "job" && t.jobId) {
        await api.deleteStudioJob(t.jobId);
        setPendingJobIds((cur) => cur.filter((x) => x !== t.jobId));
      } else if (t.assetId) {
        await api.deleteAsset(t.assetId);
        setSelectedRefs((cur) => cur.filter((x) => x !== t.assetId));
      }
      toast.success("Deleted");
    } catch (e: any) {
      toast.error(e.message || "Delete failed");
    }
    await reload();
  };

  const quickSave = async (jobId: number) => {
    try {
      // The backend picks the slot from the job's owner, so report what it chose.
      const asset = await api.saveStudioJob(jobId);
      toast.success(`Saved to the cast as ${slotLabel(asset.owner_kind, asset.asset_type)}`);
    } catch (e: any) {
      toast.error(e.message || "Save failed");
    }
    await reload();
  };

  const selectedAssets: Asset[] = useMemo(() => {
    const map = new Map<number, Asset>();
    for (const av of avatars) for (const a of av.assets) map.set(a.id, a);
    for (const ing of ingredients) for (const a of ing.assets) map.set(a.id, a);
    return selectedRefs
      .map((id) => map.get(id))
      .filter((x): x is Asset => Boolean(x));
  }, [selectedRefs, avatars, ingredients]);

  return (
    <div className="flex flex-col h-screen">
      <header className="border-b border-ink-800 px-6 py-4 flex items-center gap-3">
        <h1 className="text-lg font-semibold">Studio</h1>
        <span className="text-xs text-ink-400 ml-2">
          Browse your cast, generate new images and clips, save them back.
        </span>
      </header>

      <div className="px-6 pt-4 flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={clsx("chip", filter === f.id && "chip-active")}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto px-3 sm:px-6 py-4 pb-44">
        {tiles.length === 0 ? (
          <div className="text-ink-300 text-sm py-12 text-center">
            Nothing yet. Add characters or scenes in <a href="/cast" className="underline">Cast</a>,
            or use the prompt bar below to generate.
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
            {tiles.map((t) => (
              <TileCard
                key={t.key}
                tile={t}
                selected={t.assetId ? selectedRefs.includes(t.assetId) : false}
                onSelect={() => t.assetId && toggleRef(t.assetId)}
                onQuickSave={quickSave}
                onAssign={(jobId) => setAssigning(jobs.find((j) => j.id === jobId) || null)}
                onDelete={() => deleteTile(t)}
                onRemix={(jobId) => {
                  const job = jobs.find((j) => j.id === jobId);
                  if (job) remix(job);
                }}
                onUseInProject={async (jobId) => {
                  const job = jobs.find((j) => j.id === jobId);
                  if (!job) return;
                  // Save image results back to the cast first so the project
                  // can pick them up as a reference; clip results stay in /studio.
                  if (job.output_kind === "image" && job.status === "completed") {
                    try {
                      await api.saveStudioJob(job.id);
                      await reload();
                    } catch {
                      /* already saved */
                    }
                  }
                  const params = new URLSearchParams({
                    owner_kind: job.owner_kind,
                    owner_id: String(job.owner_id),
                  });
                  router.push(`/projects/new?${params.toString()}`);
                }}
              />
            ))}
          </div>
        )}
      </div>

      {assigning && (
        <AssignDialog
          job={assigning}
          onClose={() => setAssigning(null)}
          onSaved={async () => {
            setAssigning(null);
            await reload();
          }}
        />
      )}

      <PromptBar
        avatars={avatars}
        ingredients={ingredients}
        selectedRefs={selectedRefs}
        selectedAssets={selectedAssets}
        load={load}
        onCreated={(j) => {
          const rows = batchPlaceholders(j);
          const ids = rows.map((r) => r.id);
          setJobs((prev) => [...rows, ...prev.filter((p) => !ids.includes(p.id))]);
          setPendingJobIds((prev) => Array.from(new Set([...prev, ...ids])));
        }}
        onAddRefs={(ids) =>
          setSelectedRefs((cur) => Array.from(new Set([...cur, ...ids])))
        }
        onReload={reload}
      />
    </div>
  );
}

function TileCard({
  tile,
  selected,
  onSelect,
  onQuickSave,
  onAssign,
  onDelete,
  onUseInProject,
  onRemix,
}: {
  tile: Tile;
  selected: boolean;
  onSelect: () => void;
  onQuickSave: (jobId: number) => void;
  onAssign: (jobId: number) => void;
  onDelete: () => void;
  onUseInProject: (jobId: number) => void;
  onRemix: (jobId: number) => void;
}) {
  const finished = tile.status === "completed" || tile.status === "saved";
  const actionCls = "chip text-xs py-1 bg-ink-900/85 border-ink-700";
  return (
    <div
      className={clsx(
        "group relative aspect-9/16 rounded-lg overflow-hidden bg-ink-800 border transition",
        selected ? "border-accent" : "border-ink-800 hover:border-ink-600"
      )}
    >
      {tile.imageUrl && (
        <img src={tile.imageUrl} alt={tile.label} className="w-full h-full object-cover" />
      )}
      {tile.videoUrl && (
        <video
          src={tile.videoUrl}
          className="w-full h-full object-cover"
          muted
          loop
          playsInline
          onMouseEnter={(e) => (e.currentTarget as HTMLVideoElement).play()}
          onMouseLeave={(e) => (e.currentTarget as HTMLVideoElement).pause()}
        />
      )}
      {!tile.imageUrl && !tile.videoUrl && (
        <div className="w-full h-full flex items-center justify-center text-ink-400 text-xs p-3 text-center">
          {tile.status || "preview unavailable"}
        </div>
      )}

      <div className="absolute inset-x-0 bottom-0 p-2 bg-linear-to-t from-black/85 to-transparent">
        <div className="text-xs text-white font-medium truncate">{tile.label}</div>
        <div className="text-[10px] text-ink-300 truncate">{tile.sub}</div>
      </div>

      {tile.kind === "asset" && tile.selectable && (
        <button
          onClick={onSelect}
          aria-label={selected ? "Deselect reference" : "Use as reference"}
          aria-pressed={selected}
          className={clsx(
            "absolute top-2 right-2 w-6 h-6 rounded-full border text-xs flex items-center justify-center",
            selected
              ? "grad border-transparent text-ink-950"
              // Touch devices have no hover; show at 60% so it's discoverable,
              // then full opacity on hover for crisp desktop affordance.
              : "bg-black/60 border-ink-300 text-ink-100 opacity-60 group-hover:opacity-100 transition"
          )}
          title={selected ? "Selected as reference" : "Use as reference"}
        >
          {selected ? "✓" : "+"}
        </button>
      )}

      {tile.kind === "job" && tile.jobId && (
        // Desktop: shown on hover/focus so they don't cover the image.
        // Touch screens have no hover, so there they stay visible.
        <div className="absolute top-2 right-2 flex flex-col items-end gap-1 transition opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-within:opacity-100">
          {finished && tile.imageUrl && (
            <button
              onClick={() => onAssign(tile.jobId!)}
              className={actionCls}
              title="Save this image to a character or scene, as its main picture or another slot"
            >
              Assign to…
            </button>
          )}
          {tile.status === "completed" && tile.imageUrl && (
            <button
              onClick={() => onQuickSave(tile.jobId!)}
              className={actionCls}
              title={`Save to this job's ${
                tile.ownerKind === "ingredient" ? "scene as a reference" : "character as a lifestyle shot"
              }`}
              aria-label="Quick save this generation to the cast"
            >
              Save
            </button>
          )}
          {finished && (
            <button
              onClick={() => onUseInProject(tile.jobId!)}
              className={actionCls}
              title="Start a new project pre-loaded with this asset's owner"
              aria-label="Start a new project pre-loaded with this asset's owner"
            >
              New Project →
            </button>
          )}
          {finished && (
            <button
              onClick={() => onRemix(tile.jobId!)}
              className={actionCls}
              title="Load this result's prompt, avoid list, references, model and aspect into the prompt bar"
              aria-label="Remix: load this result's settings into the prompt bar"
            >
              Remix
            </button>
          )}
          {finished && tile.imageUrl && (
            <a
              href={`/ambient/new?studio_job=${tile.jobId}`}
              className={actionCls}
              title="Make an hour-long ambient scene from this image"
            >
              Ambient scene →
            </a>
          )}
          <button
            onClick={onDelete}
            className={clsx(actionCls, "hover:text-accent")}
            title="Delete this Studio result"
            aria-label="Delete this Studio result"
          >
            Delete
          </button>
        </div>
      )}

      {tile.kind === "asset" && tile.assetId && (
        <button
          onClick={onDelete}
          className={clsx(
            actionCls,
            "absolute top-2 left-2 hover:text-accent transition opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-visible:opacity-100"
          )}
          title="Delete this image from the cast"
          aria-label={`Delete ${tile.label} from the cast`}
        >
          Delete
        </button>
      )}

      {tile.status === "saved" && (
        <div className="absolute top-2 left-2 chip text-[10px] py-0.5">saved</div>
      )}

      {tile.kind === "job" && tile.status && !["completed", "saved"].includes(tile.status) && (
        <div className="absolute top-2 left-2 chip text-[10px] py-0.5">
          {tile.status}
        </div>
      )}
    </div>
  );
}

function PromptBar({
  avatars,
  ingredients,
  selectedRefs,
  selectedAssets,
  load,
  onCreated,
  onAddRefs,
  onReload,
}: {
  avatars: Avatar[];
  ingredients: Ingredient[];
  selectedRefs: number[];
  selectedAssets: Asset[];
  load: PromptLoad | null;
  onCreated: (j: StudioJob) => void;
  onAddRefs: (assetIds: number[]) => void;
  onReload: () => Promise<void> | void;
}) {
  const [outputKind, setOutputKind] = useState<"image" | "video_clip">("image");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [imageModels, setImageModels] = useState<{ id: string; name: string }[]>([]);
  const [videoModels, setVideoModels] = useState<{ id: string; name: string }[]>([]);
  const [model, setModel] = useState<string>("");
  const [clipSeconds, setClipSeconds] = useState<number>(5);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadOwner, setUploadOwner] = useState<string>("");
  const [avoid, setAvoid] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [aspect, setAspect] = useState<StudioAspect>("");
  const [variations, setVariations] = useState(1);
  const promptRef = useRef<HTMLInputElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  // A model to select once the list for the (possibly new) output kind is
  // in place; otherwise switching kinds resets to that kind's first model.
  const wantModel = useRef<string | null>(null);

  // Default upload target = first selected character → first selected
  // ingredient → first avatar → first ingredient. Re-derived as the cast
  // loads and as the user's reference selection changes — but only when
  // the user hasn't already picked a target manually.
  useEffect(() => {
    if (uploadOwner) return;
    const pick = () => {
      for (const a of selectedAssets) {
        if (a.owner_kind === "avatar" && a.avatar_id)
          return `avatar:${a.avatar_id}`;
      }
      for (const a of selectedAssets) {
        if (a.owner_kind === "ingredient" && a.ingredient_id)
          return `ingredient:${a.ingredient_id}`;
      }
      if (avatars[0]) return `avatar:${avatars[0].id}`;
      if (ingredients[0]) return `ingredient:${ingredients[0].id}`;
      return "";
    };
    const next = pick();
    if (next) setUploadOwner(next);
  }, [avatars, ingredients, selectedAssets, uploadOwner]);

  useEffect(() => {
    api.imageModels().then(setImageModels).catch(() => setImageModels([]));
    api.videoModels().then(setVideoModels).catch(() => setVideoModels([]));
  }, []);

  const currentModels = outputKind === "image" ? imageModels : videoModels;
  useEffect(() => {
    // Reset to default when switching output kind so we don't carry a video
    // model into an image generation request.
    const want = wantModel.current;
    if (want !== null && currentModels.length > 0) wantModel.current = null;
    if (want && currentModels.some((m) => m.id === want)) setModel(want);
    else setModel(currentModels[0]?.id || "");
  }, [outputKind, imageModels, videoModels]);

  // Fill the bar from Remix or a preset.
  const apply = (p: Omit<PromptLoad, "nonce">) => {
    setPrompt(p.prompt);
    setAvoid(p.negative_prompt);
    if (p.negative_prompt) setMoreOpen(true);
    setAspect(p.aspect_ratio);
    if (p.duration_seconds) setClipSeconds(snapClipSeconds(p.duration_seconds));
    if (p.outputKind !== outputKind) {
      wantModel.current = p.model;
      setOutputKind(p.outputKind);
    } else if (p.model && currentModels.some((m) => m.id === p.model)) {
      setModel(p.model);
    }
    barRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    promptRef.current?.focus();
  };

  useEffect(() => {
    if (load) apply(load);
  }, [load?.nonce]);

  const applyPreset = (p: PromptPreset) =>
    apply({
      outputKind: p.kind === "video" ? "video_clip" : "image",
      prompt: p.prompt,
      negative_prompt: p.negative_prompt,
      model: p.model,
      aspect_ratio: p.aspect_ratio,
    });

  const isImage = outputKind === "image";

  const submit = async () => {
    if (!prompt.trim()) return;
    // Pick an owner: prefer the first selected character; else first selected ingredient;
    // else the first avatar that exists; else first ingredient.
    let owner_kind: "avatar" | "ingredient" = "avatar";
    let owner_id: number | undefined = undefined;
    for (const a of selectedAssets) {
      if (a.owner_kind === "avatar" && a.avatar_id) {
        owner_kind = "avatar";
        owner_id = a.avatar_id;
        break;
      }
    }
    if (!owner_id) {
      for (const a of selectedAssets) {
        if (a.owner_kind === "ingredient" && a.ingredient_id) {
          owner_kind = "ingredient";
          owner_id = a.ingredient_id;
          break;
        }
      }
    }
    if (!owner_id) {
      if (avatars[0]) {
        owner_kind = "avatar";
        owner_id = avatars[0].id;
      } else if (ingredients[0]) {
        owner_kind = "ingredient";
        owner_id = ingredients[0].id;
      }
    }
    if (!owner_id) {
      setError("Create a Character or Scene first (Cast → New).");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const body = {
        owner_kind,
        owner_id,
        prompt,
        reference_asset_ids: selectedRefs,
        aspect_ratio: aspect || null,
        ...(avoid.trim() ? { negative_prompt: avoid.trim() } : {}),
        ...(model ? { model } : {}),
        ...(outputKind === "video_clip" ? { duration_seconds: clipSeconds } : { variations }),
      };
      const job =
        outputKind === "image"
          ? await api.generateImage(body)
          : await api.generateClip(body);
      onCreated(job);
      setPrompt("");
    } catch (e: any) {
      setError(e.message || "Generation failed");
    } finally {
      setSubmitting(false);
    }
  };

  // Uploads a single file to the chosen owner. Throws on failure so the
  // Dropzone can flag it red. Auto-selects the resulting asset as a reference
  // via `onAddRefs` so the user can immediately hit Generate.
  const uploadRef = async (file: File): Promise<Asset> => {
    if (!uploadOwner) throw new Error("Pick a cast member to attach uploads to");
    const [kind, idStr] = uploadOwner.split(":");
    const id = Number(idStr);
    const asset =
      kind === "avatar"
        ? await api.uploadAvatarAsset(id, file, "reference_sheet", true)
        : await api.uploadIngredientAsset(id, file, "reference", true);
    onAddRefs([asset.id]);
    return asset;
  };

  return (
    <div
      ref={barRef}
      className="fixed bottom-0 left-0 md:left-56 right-0 z-10 bg-ink-900/95 border-t border-ink-800 backdrop-blur-sm"
    >
      <div className="max-w-5xl mx-auto px-3 sm:px-6 py-3">
        {selectedRefs.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5 text-xs text-ink-300">
            <span>References:</span>
            {selectedAssets.map((a) => (
              <span key={a.id} className="chip py-0.5 text-[11px]">
                {a.owner_kind === "avatar" ? "Char" : "Scene"} #{a.id} · {a.asset_type}
              </span>
            ))}
          </div>
        )}

        {uploadOpen && (
          <div className="card p-3 mb-2 space-y-2">
            <div className="flex items-center gap-2 text-xs text-ink-300">
              <span className="font-medium text-ink-200">Upload references to</span>
              <select
                className="input text-xs py-1 flex-1 max-w-xs"
                value={uploadOwner}
                onChange={(e) => setUploadOwner(e.target.value)}
              >
                <option value="" disabled>
                  — pick a cast member —
                </option>
                {avatars.length > 0 && <optgroup label="Characters">
                  {avatars.map((a) => (
                    <option key={`av-${a.id}`} value={`avatar:${a.id}`}>{a.name}</option>
                  ))}
                </optgroup>}
                {ingredients.length > 0 && <optgroup label="Ingredients">
                  {ingredients.map((i) => (
                    <option key={`ing-${i.id}`} value={`ingredient:${i.id}`}>
                      {i.name} ({i.kind})
                    </option>
                  ))}
                </optgroup>}
              </select>
              <button
                type="button"
                onClick={() => setUploadOpen(false)}
                className="text-ink-400 hover:text-ink-100 text-xs"
                aria-label="Close upload tray"
              >
                ✕
              </button>
            </div>
            <Dropzone
              disabled={!uploadOwner}
              hint={
                uploadOwner
                  ? "HEIC/PNG/JPEG/WebP up to 50 MB. Uploaded assets auto-attach as references for your next generation."
                  : avatars.length + ingredients.length === 0
                  ? "Create a cast member first (Cast → New)."
                  : "Pick a target above."
              }
              onUpload={uploadRef}
              onSettled={() => onReload()}
            />
          </div>
        )}

        {/* Phones: the prompt gets its own full-width row; the controls wrap under it. */}
        <div className="card flex flex-wrap items-center gap-2 p-2">
          <button
            onClick={() => setOutputKind(outputKind === "image" ? "video_clip" : "image")}
            className="chip text-xs"
            title="Toggle output kind"
          >
            {outputKind === "image" ? "Image" : "Video"}
          </button>
          {outputKind === "video_clip" && (
            <select
              value={clipSeconds}
              onChange={(e) => setClipSeconds(Number(e.target.value))}
              className="chip text-xs bg-ink-800"
              title="Clip length (the model snaps it to the nearest length it supports)"
              aria-label="Clip length"
            >
              {CLIP_SECONDS.map((n) => (
                <option key={n} value={n}>
                  {n}s
                </option>
              ))}
            </select>
          )}
          {currentModels.length > 0 && (
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="chip text-xs bg-ink-800 max-w-[180px] truncate"
              title="Provider model"
              aria-label="Provider model"
            >
              {currentModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name || m.id}
                </option>
              ))}
            </select>
          )}
          <select
            value={aspect}
            onChange={(e) => setAspect(asAspect(e.target.value))}
            className="chip text-xs bg-ink-800"
            title="Aspect ratio. Auto: characters 9:16, scenes 16:9"
            aria-label="Aspect ratio"
          >
            {ASPECTS.map((a) => (
              <option key={a.id || "auto"} value={a.id}>
                {a.label}
              </option>
            ))}
          </select>
          {isImage && (
            <select
              value={variations}
              onChange={(e) => setVariations(Number(e.target.value))}
              className="chip text-xs bg-ink-800"
              title="How many images to make from this prompt. Each one costs the same as a single image."
              aria-label="Variations"
            >
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n === 1 ? "1 image" : `${n} images`}
                </option>
              ))}
            </select>
          )}
          <PresetsMenu
            current={() => ({
              kind: isImage ? "image" : "video",
              prompt,
              negative_prompt: avoid.trim(),
              model,
              aspect_ratio: aspect,
            })}
            onApply={applyPreset}
          />
          <button
            type="button"
            onClick={() => setMoreOpen((v) => !v)}
            className="chip text-xs"
            aria-expanded={moreOpen}
            aria-controls="studio-more-row"
            title="Things to avoid in this generation"
          >
            {avoid.trim() ? "Avoid •" : "Avoid"}
          </button>
          <button
            type="button"
            onClick={() => setUploadOpen((v) => !v)}
            className="chip text-xs"
            title="Upload reference images and auto-attach them to this generation"
            aria-pressed={uploadOpen}
            aria-label="Upload reference images"
          >
            📎
          </button>
          <input
            ref={promptRef}
            aria-label="Prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What do you want to create?"
            className="flex-1 min-w-0 basis-full order-first bg-transparent outline-hidden text-sm px-2 py-2"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <EnhanceButton
            className="contents"
            text={prompt}
            kind={isImage ? "image" : "video"}
            model={model || null}
            disabled={submitting}
            onUse={(t) => {
              setPrompt(t);
              promptRef.current?.focus();
            }}
          />
          <button
            onClick={submit}
            disabled={submitting || !prompt.trim()}
            className="btn-primary text-sm disabled:opacity-50 ml-auto sm:ml-0"
            title={
              isImage && variations > 1
                ? `Makes ${variations} images: about ${variations}× the cost of one`
                : undefined
            }
          >
            {submitting ? "…" : isImage && variations > 1 ? `Generate ×${variations} →` : "Generate →"}
          </button>
          {moreOpen && (
            <div id="studio-more-row" className="basis-full flex flex-wrap items-center gap-2 px-2 pt-1">
              <label htmlFor="studio-avoid" className="text-xs text-ink-300">
                Avoid
              </label>
              <input
                id="studio-avoid"
                value={avoid}
                onChange={(e) => setAvoid(e.target.value)}
                maxLength={1000}
                placeholder="e.g. text overlays, crowds, cartoon look"
                className="input text-xs py-1 flex-1 min-w-[12rem]"
              />
              <span className="text-[11px] text-ink-400 basis-full">
                Your defaults from{" "}
                <a href="/settings#prompting" className="underline">
                  Settings → Prompting
                </a>{" "}
                are added automatically.
              </span>
            </div>
          )}
        </div>
        {error && <div className="text-xs text-accent mt-2">{error}</div>}
      </div>
    </div>
  );
}
