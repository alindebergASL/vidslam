"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { Dropzone } from "@/components/Dropzone";
import { api, Asset, Avatar, Ingredient, StudioJob } from "@/lib/api";

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
};

function Studio() {
  const router = useRouter();
  const [filter, setFilter] = useState<Filter>("all");
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [jobs, setJobs] = useState<StudioJob[]>([]);
  const [selectedRefs, setSelectedRefs] = useState<number[]>([]);
  const [pendingJobIds, setPendingJobIds] = useState<number[]>([]);

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
            label: `${av.name} · ${a.asset_type}`,
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
            label: `${ing.name} · ${a.asset_type}`,
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

      <div className="flex-1 overflow-y-auto px-6 py-4 pb-40">
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
                onSaved={reload}
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

      <PromptBar
        avatars={avatars}
        ingredients={ingredients}
        selectedRefs={selectedRefs}
        selectedAssets={selectedAssets}
        onCreated={(j) => {
          setJobs((prev) => [j, ...prev]);
          setPendingJobIds((prev) => [...prev, j.id]);
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
  onSaved,
  onUseInProject,
}: {
  tile: Tile;
  selected: boolean;
  onSelect: () => void;
  onSaved: () => void;
  onUseInProject: (jobId: number) => void;
}) {
  return (
    <div
      className={clsx(
        "group relative aspect-[9/16] rounded-lg overflow-hidden bg-ink-800 border transition",
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

      <div className="absolute inset-x-0 bottom-0 p-2 bg-gradient-to-t from-black/85 to-transparent">
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
              ? "bg-accent border-accent text-ink-950"
              // Touch devices have no hover; show at 60% so it's discoverable,
              // then full opacity on hover for crisp desktop affordance.
              : "bg-black/60 border-ink-300 text-ink-100 opacity-60 group-hover:opacity-100 transition"
          )}
          title={selected ? "Selected as reference" : "Use as reference"}
        >
          {selected ? "✓" : "+"}
        </button>
      )}

      {tile.kind === "job" && tile.status === "completed" && tile.jobId && (
        // Always visible (touch devices have no hover); a subtle scrim keeps
        // them readable over the image.
        <div className="absolute top-2 right-2 flex flex-col gap-1">
          <button
            onClick={async () => {
              try {
                await api.saveStudioJob(tile.jobId!);
              } catch {
                /* already saved */
              }
              onSaved();
            }}
            className="chip text-xs py-1 bg-ink-900/85 border-ink-700"
            title="Save to cast"
            aria-label="Save this generation to the cast"
          >
            Save
          </button>
          <button
            onClick={() => onUseInProject(tile.jobId!)}
            className="chip text-xs py-1 bg-ink-900/85 border-ink-700"
            title="Start a new project pre-loaded with this asset's owner"
            aria-label="Start a new project pre-loaded with this asset's owner"
          >
            New Project →
          </button>
        </div>
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
  onCreated,
  onAddRefs,
  onReload,
}: {
  avatars: Avatar[];
  ingredients: Ingredient[];
  selectedRefs: number[];
  selectedAssets: Asset[];
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
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadOwner, setUploadOwner] = useState<string>("");

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
    setModel(currentModels[0]?.id || "");
  }, [outputKind, imageModels, videoModels]);

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
        ...(model ? { model } : {}),
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
    <div className="fixed bottom-0 left-0 md:left-56 right-0 z-10 bg-ink-900/95 border-t border-ink-800 backdrop-blur">
      <div className="max-w-5xl mx-auto px-6 py-3">
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
                  ? "PNG/JPEG/WEBP up to 15MB. Uploaded assets auto-attach as references for your next generation."
                  : avatars.length + ingredients.length === 0
                  ? "Create a cast member first (Cast → New)."
                  : "Pick a target above."
              }
              onUpload={uploadRef}
              onSettled={() => onReload()}
            />
          </div>
        )}

        <div className="card flex items-center gap-2 p-2">
          <button
            onClick={() => setOutputKind(outputKind === "image" ? "video_clip" : "image")}
            className="chip text-xs"
            title="Toggle output kind"
          >
            {outputKind === "image" ? "Image" : "Video · 5s"}
          </button>
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
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What do you want to create?"
            className="flex-1 bg-transparent outline-none text-sm px-2"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <button
            onClick={submit}
            disabled={submitting || !prompt.trim()}
            className="btn-primary text-sm disabled:opacity-50"
          >
            {submitting ? "…" : "Generate →"}
          </button>
        </div>
        {error && <div className="text-xs text-accent mt-2">{error}</div>}
      </div>
    </div>
  );
}
