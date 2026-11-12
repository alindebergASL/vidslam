"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { Dropzone } from "@/components/Dropzone";
import { TrainPanel } from "@/components/TrainPanel";
import { AssignDialog, ASSET_SLOTS, demoteOtherHeroes, slotLabel } from "@/components/AssignDialog";
import { useToast } from "@/components/Toaster";
import { VoiceSampleButton } from "@/components/VoiceSampleButton";
import { useContentModeState } from "@/components/AdultModePanel";
import { api, Asset, Avatar, ImageOp, Ingredient, Project, StudioJob, Voice, errorText } from "@/lib/api";
import { UpscaleDialog, removeBgLabel, useOpModels } from "@/components/ImageOps";
import { usePoll } from "@/lib/usePoll";
import { useEvents } from "@/components/NotificationsProvider";

/** A remove-background / upscale run started from a Cast picture. */
type OpRun = { job: StudioJob; assetType: string; saved: boolean };
const OP_DONE = ["completed", "saved", "failed"];

type Tab = "characters" | "scenes" | "styles" | "objects";

export default function CastPage() {
  return (
    <AuthGate>
      <CastInner />
    </AuthGate>
  );
}

function CastInner() {
  const [tab, setTab] = useState<Tab>("characters");
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);
  const [openKind, setOpenKind] = useState<"avatar" | "ingredient">("avatar");
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");

  const reload = async () => {
    const [av, ing, ps] = await Promise.all([
      api.listAvatars(),
      api.listIngredients(),
      api.listProjects(),
    ]);
    setAvatars(av);
    setIngredients(ing);
    setProjects(ps);
    setLoaded(true);
  };
  useEffect(() => {
    reload();
  }, []);

  // Count how many projects reference each cast member by id+kind so the grid
  // can surface "Used in N" — answers "is this character actually used?" without
  // forcing the user to open each project.
  const usage = (() => {
    const av: Record<number, number> = {};
    const ing: Record<number, number> = {};
    for (const p of projects) {
      const seenAv = new Set<number>();
      const seenIng = new Set<number>();
      for (const m of p.cast_members || []) {
        if (m.member_kind === "avatar" && m.avatar_id != null && !seenAv.has(m.avatar_id)) {
          av[m.avatar_id] = (av[m.avatar_id] || 0) + 1;
          seenAv.add(m.avatar_id);
        }
        if (
          m.member_kind === "ingredient" &&
          m.ingredient_id != null &&
          !seenIng.has(m.ingredient_id)
        ) {
          ing[m.ingredient_id] = (ing[m.ingredient_id] || 0) + 1;
          seenIng.add(m.ingredient_id);
        }
      }
    }
    return { avatar: av, ingredient: ing };
  })();

  const tabIngKind = tab === "scenes" ? "scene" : tab === "styles" ? "style" : "object";

  const q = query.trim().toLowerCase();
  const matches = (haystack: string[]) =>
    !q || haystack.some((s) => s && s.toLowerCase().includes(q));

  return (
    <div className="p-8 max-w-6xl">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Cast</h1>
          <p className="text-ink-300 mt-1">
            Reusable characters, scenes, styles, and props used across projects and the Studio.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setCreating(true)}>
          + New {tab === "characters" ? "Character" : "Ingredient"}
        </button>
      </header>

      <div className="flex flex-wrap items-center gap-2 mb-6">
        {(["characters", "scenes", "styles", "objects"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={clsx("chip capitalize", tab === t && "chip-active")}
          >
            {t}
          </button>
        ))}
        <div className="ml-auto relative">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search…"
            aria-label="Search cast"
            className="input text-sm py-1.5 pl-8 pr-8 w-56"
          />
          <span aria-hidden className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-400 text-xs">
            ⌕
          </span>
          {query && (
            <button
              type="button"
              aria-label="Clear search"
              onClick={() => setQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-400 hover:text-ink-100 text-xs"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {tab === "characters" ? (
        <Grid
          loaded={loaded}
          query={q}
          items={avatars
            .filter((a) => matches([a.name, a.persona, a.brand]))
            .map((a) => ({
              id: a.id,
              kind: "avatar" as const,
              title: a.name,
              sub: a.persona || a.brand || "—",
              heroUrl:
                a.assets.find((x) => x.asset_type === "hero")?.public_token &&
                api.publicAsset(a.assets.find((x) => x.asset_type === "hero")!.public_token),
              count: a.assets.length,
              usedInProjects: usage.avatar[a.id] || 0,
            }))}
          onOpen={(id) => {
            setOpenKind("avatar");
            setOpenId(id);
          }}
        />
      ) : (
        <Grid
          loaded={loaded}
          query={q}
          items={ingredients
            .filter((i) =>
              tabIngKind === "object" ? i.kind === "object" || i.kind === "prop" : i.kind === tabIngKind
            )
            .filter((i) => matches([i.name, i.visual_identity, i.kind]))
            .map((i) => ({
              id: i.id,
              kind: "ingredient" as const,
              title: i.name,
              sub: i.visual_identity || i.kind,
              // Same pick as characters: the main picture, else the first image.
              heroUrl: (() => {
                const pic = i.assets.find((x) => x.asset_type === "hero") || i.assets[0];
                return pic?.public_token && api.publicAsset(pic.public_token);
              })(),
              count: i.assets.length,
              usedInProjects: usage.ingredient[i.id] || 0,
            }))}
          onOpen={(id) => {
            setOpenKind("ingredient");
            setOpenId(id);
          }}
        />
      )}

      {creating && (
        <CreateModal
          kind={tab === "characters" ? "avatar" : "ingredient"}
          ingredientKind={tabIngKind as any}
          onClose={() => setCreating(false)}
          onCreated={async (kind, id) => {
            setCreating(false);
            await reload();
            setOpenKind(kind);
            setOpenId(id);
          }}
        />
      )}

      {openId && (
        <DetailDrawer
          kind={openKind}
          id={openId}
          onClose={() => setOpenId(null)}
          onChange={reload}
        />
      )}
    </div>
  );
}

function GridSkeleton() {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3" aria-hidden>
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="card overflow-hidden">
          <div className="aspect-9/16 skeleton" />
          <div className="p-3 space-y-2">
            <div className="h-3 skeleton w-3/4" />
            <div className="h-2 skeleton w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

function Grid({
  items,
  loaded,
  query,
  onOpen,
}: {
  items: { id: number; kind: "avatar" | "ingredient"; title: string; sub: string; heroUrl?: string | null | false; count: number; usedInProjects: number }[];
  loaded: boolean;
  query?: string;
  onOpen: (id: number) => void;
}) {
  if (!loaded) return <GridSkeleton />;
  if (items.length === 0) {
    if (query) {
      return (
        <div className="card p-10 text-center">
          <div className="text-base font-medium mb-1">No matches for “{query}”</div>
          <div className="text-xs text-ink-300">
            Try a different name, persona, or visual-identity keyword.
          </div>
        </div>
      );
    }
    return (
      <div className="card p-10 text-center">
        <div className="text-3xl mb-2">☺</div>
        <div className="text-base font-medium mb-1">Nothing here yet</div>
        <div className="text-xs text-ink-300 max-w-sm mx-auto">
          A cast member is a reusable visual reference — a Character with a hero
          portrait, or an Ingredient (scene, style, object, prop). Add one, then
          you can pull it into any project.
        </div>
        <div className="text-[11px] text-ink-400 mt-3">
          Tip: click <em>+ New</em> at the top, or load the demo cast from the Dashboard.
        </div>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
      {items.map((it) => (
        <button
          key={`${it.kind}-${it.id}`}
          onClick={() => onOpen(it.id)}
          className="card overflow-hidden card-hover text-left"
        >
          <div className="aspect-9/16 bg-ink-800 relative">
            {it.heroUrl && (
              <img src={String(it.heroUrl)} alt={it.title} className="w-full h-full object-cover" />
            )}
            <div className="absolute top-2 right-2 chip text-[10px] py-0.5">
              {it.count} asset{it.count === 1 ? "" : "s"}
            </div>
          </div>
          <div className="p-3">
            <div className="text-sm font-medium truncate">{it.title}</div>
            <div className="text-xs text-ink-300 truncate mt-0.5">{it.sub}</div>
            <div className="text-[10px] text-ink-400 mt-1">
              {it.usedInProjects > 0
                ? `Used in ${it.usedInProjects} project${it.usedInProjects === 1 ? "" : "s"}`
                : "Not used yet"}
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}

function CreateModal({
  kind,
  ingredientKind,
  onClose,
  onCreated,
}: {
  kind: "avatar" | "ingredient";
  ingredientKind: "object" | "scene" | "style" | "prop";
  onClose: () => void;
  onCreated: (kind: "avatar" | "ingredient", id: number) => void;
}) {
  const [name, setName] = useState("");
  const [persona, setPersona] = useState("");
  const [visual, setVisual] = useState("");
  const [iKind, setIKind] = useState(ingredientKind);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  return (
    <div className="fixed inset-0 z-30 bg-black/60 flex items-center justify-center p-4">
      <div className="card w-full max-w-md p-6 space-y-4">
        <div className="text-lg font-semibold">
          New {kind === "avatar" ? "Character" : "Ingredient"}
        </div>
        <div>
          <label className="label" htmlFor="cast-name">Name</label>
          <input
            id="cast-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            autoFocus
            placeholder={kind === "avatar" ? "e.g. Naina" : "e.g. Brooklyn rooftop"}
          />
        </div>
        {kind === "avatar" ? (
          <>
            <div>
              <label className="label">Persona</label>
              <input
                className="input"
                value={persona}
                onChange={(e) => setPersona(e.target.value)}
                placeholder="Witty dating-app creator…"
              />
            </div>
            <div>
              <label className="label">Visual identity</label>
              <textarea
                className="input min-h-[80px]"
                value={visual}
                onChange={(e) => setVisual(e.target.value)}
                placeholder="Mid-20s, warm brown skin, hoop earrings…"
              />
            </div>
          </>
        ) : (
          <>
            <div>
              <label className="label">Kind</label>
              <select
                className="input"
                value={iKind}
                onChange={(e) => setIKind(e.target.value as any)}
              >
                <option value="scene">Scene</option>
                <option value="style">Style</option>
                <option value="object">Object</option>
                <option value="prop">Prop</option>
              </select>
            </div>
            <div>
              <label className="label">Visual identity</label>
              <textarea
                className="input min-h-[80px]"
                value={visual}
                onChange={(e) => setVisual(e.target.value)}
              />
            </div>
          </>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={!name.trim() || creating}
            onClick={async () => {
              setCreating(true);
              setCreateError(null);
              try {
                if (kind === "avatar") {
                  const a = await api.createAvatar({ name, persona, visual_identity: visual });
                  onCreated("avatar", a.id);
                } else {
                  const i = await api.createIngredient({
                    name,
                    kind: iKind,
                    visual_identity: visual,
                  });
                  onCreated("ingredient", i.id);
                }
              } catch (e) {
                // e.g. the safety check refused the description (422).
                setCreateError(errorText(e, "Couldn't create it"));
              } finally {
                setCreating(false);
              }
            }}
          >
            {creating ? "Creating…" : "Create"}
          </button>
        </div>
        {createError && (
          <p className="text-xs text-accent" role="alert">
            {createError}
          </p>
        )}
      </div>
    </div>
  );
}

function DetailDrawer({
  kind,
  id,
  onClose,
  onChange,
}: {
  kind: "avatar" | "ingredient";
  id: number;
  onClose: () => void;
  onChange: () => void;
}) {
  const [entity, setEntity] = useState<Avatar | Ingredient | null>(null);
  const [assetType, setAssetType] = useState(kind === "avatar" ? "hero" : "hero");
  const [rights, setRights] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [addingFromStudio, setAddingFromStudio] = useState(false);
  const [busyAsset, setBusyAsset] = useState<number | null>(null);
  const toast = useToast();
  // Remove background / upscale on a picture: the results land in Studio
  // (owned by this cast member) and are listed here until the drawer closes.
  const opModels = useOpModels();
  const [upscaling, setUpscaling] = useState<Asset | null>(null);
  const [runs, setRuns] = useState<OpRun[]>([]);
  const running = runs.filter((r) => !OP_DONE.includes(r.job.status));
  // The latest runs, for handlers called from an older render (the toast's
  // action, a poll rerun before React re-renders).
  const runsRef = useRef(runs);
  runsRef.current = runs;
  // Saves under way (each save files a new copy, so never twice), results
  // already announced, and pictures whose operation is being started.
  const savingRef = useRef<Set<number>>(new Set());
  const [savingIds, setSavingIds] = useState<number[]>([]);
  const announcedRef = useRef<Set<number>>(new Set());
  const startingRef = useRef<Set<number>>(new Set());
  const [startingIds, setStartingIds] = useState<number[]>([]);

  const saveBack = async (jobId: number) => {
    const run = runsRef.current.find((r) => r.job.id === jobId);
    if (!run || run.saved || savingRef.current.has(jobId)) return;
    savingRef.current.add(jobId);
    setSavingIds((cur) => [...cur, jobId]);
    const markSaved = (saved: boolean) =>
      setRuns((cur) => cur.map((r) => (r.job.id === jobId ? { ...r, saved } : r)));
    markSaved(true);
    try {
      // Same slot as the picture it was made from; the result is saved full size.
      let asset: Asset;
      try {
        asset = await api.saveStudioJob(jobId, { asset_type: run.assetType });
      } catch (e) {
        markSaved(false);
        throw e;
      }
      // One main picture per cast member: the old one moves to the reference slot.
      let demoted = 0;
      if (run.assetType === "hero" && entity) {
        demoted = await demoteOtherHeroes(kind, entity.assets, asset.id).catch(() => 0);
      }
      toast.success(
        `Saved as ${slotLabel(kind, run.assetType)}` +
          (demoted ? " (the previous main picture moved to the reference slot)" : "")
      );
      await reload();
      onChange();
    } catch (e) {
      toast.error(errorText(e, "Couldn't save it"));
    } finally {
      savingRef.current.delete(jobId);
      setSavingIds((cur) => cur.filter((x) => x !== jobId));
    }
  };
  const saveBackRef = useRef(saveBack);
  saveBackRef.current = saveBack;

  const checkRuns = async () => {
    const ids = runsRef.current.filter((r) => !OP_DONE.includes(r.job.status)).map((r) => r.job.id);
    if (ids.length === 0) return;
    const fresh = await api.studioJobsByIds(ids);
    for (const j of fresh) {
      const was = runsRef.current.find((r) => r.job.id === j.id);
      if (!was || OP_DONE.includes(was.job.status) || !OP_DONE.includes(j.status)) continue;
      // A poll rerun can see the same finish before React re-renders: once.
      if (announcedRef.current.has(j.id)) continue;
      announcedRef.current.add(j.id);
      if (j.status === "failed") {
        toast.error(`${j.label || "It"} failed: ${j.error || "unknown error"}`, { action: { label: "Open in Studio", href: `/studio?job=${j.id}` } });
      } else {
        toast.success(`${j.label || "Result"} is ready`, {
          action: { label: "Save back to this slot", onClick: () => saveBackRef.current(j.id) },
          duration: 10000,
        });
      }
    }
    const next = runsRef.current.map((r) => ({ ...r, job: fresh.find((j) => j.id === r.job.id) || r.job }));
    runsRef.current = next;
    setRuns((cur) => cur.map((r) => ({ ...r, job: fresh.find((j) => j.id === r.job.id) || r.job })));
  };
  const pokeRuns = usePoll(checkRuns, { enabled: running.length > 0, interval: 3000 });
  useEvents({ kind: "studio_job" }, (ev) => {
    if (ev === null || runs.some((r) => r.job.id === ev.entity_id)) pokeRuns();
  });

  const startOp = async (op: ImageOp, a: Asset, job?: StudioJob) => {
    // One start at a time per picture: a double click would queue (and bill) two.
    if (!job && startingRef.current.has(a.id)) return;
    if (!job) {
      startingRef.current.add(a.id);
      setStartingIds((cur) => [...cur, a.id]);
    }
    try {
      const j = job || (await api.runImageOp(op, { assetId: a.id }));
      setRuns((cur) => [{ job: j, assetType: a.asset_type, saved: false }, ...cur]);
      toast.info(op === "remove_background" ? "Removing the background…" : "Upscaling…", {
        action: { label: "Open in Studio", href: `/studio?job=${j.id}` },
      });
    } catch (e) {
      toast.error(errorText(e, op === "remove_background" ? "Couldn't remove the background" : "Couldn't upscale it"));
    } finally {
      if (!job) {
        startingRef.current.delete(a.id);
        setStartingIds((cur) => cur.filter((x) => x !== a.id));
      }
    }
  };

  useEffect(() => {
    if (kind === "avatar") {
      api.voices().then(setVoices).catch(() => setVoices([]));
    }
  }, [kind, id]);

  const reload = async () => {
    if (kind === "avatar") setEntity(await api.getAvatar(id));
    else setEntity(await api.getIngredient(id));
  };
  useEffect(() => {
    reload();
  }, [id]);

  // Uploads a single file; throws on failure so the Dropzone can flag it red.
  // The Dropzone batches and calls onSettled to trigger a single reload.
  const upload = async (file: File) => {
    if (!rights) throw new Error("Please confirm you have rights first.");
    setError(null);
    if (kind === "avatar") await api.uploadAvatarAsset(id, file, assetType, true);
    else await api.uploadIngredientAsset(id, file, assetType, true);
  };

  const changeSlot = async (a: Asset, next: string) => {
    if (next === a.asset_type || !entity) return;
    setBusyAsset(a.id);
    try {
      await api.updateAsset(a.id, { asset_type: next });
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't change the slot"));
      setBusyAsset(null);
      return;
    }
    // Separate step so a demotion failure isn't reported as a failed slot change.
    try {
      // One main picture per cast member: generation reads the first hero.
      const demoted = next === "hero" ? await demoteOtherHeroes(kind, entity.assets, a.id) : 0;
      toast.success(
        `Now the ${slotLabel(kind, next)}` + (demoted ? " (previous main picture moved to the reference slot)" : "")
      );
    } catch {
      toast.error(
        `Now the ${slotLabel(kind, next)}, but couldn't move the old main picture to the reference slot`
      );
    } finally {
      setBusyAsset(null);
    }
    await reload();
    onChange();
  };

  const removeAsset = async (a: Asset) => {
    if (!window.confirm(`Delete this ${slotLabel(kind, a.asset_type).toLowerCase()} image? Projects that use it lose this picture. This can't be undone.`)) return;
    setBusyAsset(a.id);
    try {
      await api.deleteAsset(a.id);
      toast.success("Image deleted");
    } catch (e: any) {
      toast.error(errorText(e, "Delete failed"));
    } finally {
      setBusyAsset(null);
    }
    await reload();
    onChange();
  };

  if (!entity) return null;

  return (
    <div className="fixed inset-0 z-30 bg-black/60 flex" onClick={onClose}>
      <div
        className="ml-auto h-full w-full max-w-xl bg-ink-900 border-l border-ink-800 p-6 overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4 gap-3">
          <div className="min-w-0">
            <div className="text-xl font-semibold truncate">{(entity as any).name}</div>
            <div className="text-xs text-ink-400 mt-0.5">
              {kind === "avatar" ? "Character" : `${(entity as Ingredient).kind} ingredient`}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Link
              href={`/projects/new?owner_kind=${kind}&owner_id=${id}`}
              className="btn-ghost text-xs"
              title="Start a new project with this cast member pre-selected"
            >
              New project →
            </Link>
            <button className="btn-ghost" onClick={onClose}>
              Close
            </button>
          </div>
        </div>

        <div className="card p-4 mb-4 space-y-3">
          <label className="block text-xs font-medium text-ink-200">
            <input
              type="checkbox"
              className="mr-2 align-middle"
              checked={rights}
              onChange={(e) => setRights(e.target.checked)}
            />
            I own or have rights to use these {kind === "avatar" ? "avatar" : "ingredient"} assets.
          </label>
          <div>
            <label className="label">Asset type</label>
            <select
              className="input"
              value={assetType}
              onChange={(e) => setAssetType(e.target.value)}
            >
              {ASSET_SLOTS[kind].map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
          <Dropzone
            disabled={!rights}
            multiple
            capture
            // One at a time: phone uploads are big and the list shows each file's progress.
            concurrency={1}
            hint={
              rights
                ? "Pick several at once. HEIC (iPhone), PNG, JPEG or WebP up to 50 MB each. EXIF is stripped on upload."
                : "Confirm rights above to enable uploads."
            }
            onUpload={(file) => upload(file)}
            onSettled={(results) => {
              const ok = results.filter((r) => r.ok).length;
              const failed = results.length - ok;
              if (failed === 0) toast.success(ok === 1 ? "Uploaded 1 image" : `Uploaded ${ok} images`);
              else toast.error(`${failed} of ${results.length} uploads failed. See the list for why.`);
              reload();
              onChange();
            }}
          />
          {error && <div className="text-xs text-accent">{error}</div>}
        </div>

        {kind === "avatar" && (
          <div className="card p-4 mb-4 space-y-3">
            <div className="label">Voice</div>
            <div className="flex items-center gap-2">
              <select
                className="input flex-1"
                value={(entity as Avatar).elevenlabs_voice_id || ""}
                onChange={async (e) => {
                  const next = e.target.value;
                  try {
                    const updated = await api.updateAvatar(id, {
                      elevenlabs_voice_id: next,
                      default_voice_provider: next ? "elevenlabs" : "mock",
                    });
                    setEntity(updated);
                    onChange();
                  } catch (err) {
                    toast.error(errorText(err, "Couldn't change the voice"));
                  }
                }}
              >
                <option value="">— No specific voice (use TTS default) —</option>
                {voices.map((v) => (
                  <option key={v.voice_id} value={v.voice_id}>
                    {v.name} ({v.voice_id.slice(0, 8)}…)
                  </option>
                ))}
                {/* Surface the avatar's currently-bound voice as a tagged
                    option when it isn't in the catalog — covers cloned
                    voices auto-written by a completed voice_clone training
                    job, or voices added in the ElevenLabs UI after page
                    load. Without this the dropdown would silently
                    "reset" to "no voice" because the value doesn't match
                    any option. */}
                {(entity as Avatar).elevenlabs_voice_id &&
                  !voices.find((v) => v.voice_id === (entity as Avatar).elevenlabs_voice_id) && (
                    <option value={(entity as Avatar).elevenlabs_voice_id}>
                      Custom clone ({(entity as Avatar).elevenlabs_voice_id.slice(0, 8)}…)
                    </option>
                  )}
              </select>
              {(() => {
                const cur = voices.find((v) => v.voice_id === (entity as Avatar).elevenlabs_voice_id);
                return <VoiceSampleButton url={cur?.preview_url} name={cur?.name} />;
              })()}
            </div>
            <div className="text-[10px] text-ink-400">
              {voices.length === 0
                ? "Mock TTS only — set ELEVENLABS_API_KEY to pick a real voice."
                : `${voices.length} voice${voices.length === 1 ? "" : "s"} available from the configured TTS provider.`}
            </div>
          </div>
        )}

        {kind === "avatar" && <AdultOkCard avatarId={id} name={entity.name} />}

        <TrainPanel kind={kind} id={id} assets={(entity as any).assets} />

        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="label mb-0">Images ({entity.assets.length})</div>
          <button className="btn-ghost text-xs" onClick={() => setAddingFromStudio(true)}>
            + Add from Studio
          </button>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {entity.assets.map((a: Asset) => (
            <div key={a.id} className={clsx("card overflow-hidden", busyAsset === a.id && "opacity-60")}>
              <div className="aspect-square bg-ink-800 relative">
                <img
                  src={api.publicAsset(a.public_token)}
                  className={clsx("w-full h-full", a.mime_type === "image/png" ? "object-contain checker" : "object-cover")}
                  alt={slotLabel(kind, a.asset_type)}
                />
                {a.asset_type === "hero" && (
                  <div className="absolute top-2 left-2 chip text-[10px] py-0.5">Main</div>
                )}
                <button
                  onClick={() => removeAsset(a)}
                  disabled={busyAsset === a.id}
                  className="absolute top-2 right-2 chip text-xs py-1 bg-ink-900/85 border-ink-700 hover:text-accent"
                  aria-label={`Delete ${slotLabel(kind, a.asset_type)} image`}
                  title="Delete this image"
                >
                  Delete
                </button>
              </div>
              <div className="p-2 space-y-1">
                <select
                  className="input text-xs py-1.5"
                  value={a.asset_type}
                  disabled={busyAsset === a.id}
                  onChange={(e) => changeSlot(a, e.target.value)}
                  aria-label="Image slot"
                >
                  {ASSET_SLOTS[kind].map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                  {/* Keep legacy/unknown slots visible instead of silently showing another. */}
                  {!ASSET_SLOTS[kind].some((t) => t.value === a.asset_type) && (
                    <option value={a.asset_type}>{a.asset_type}</option>
                  )}
                </select>
                <div className="text-[10px] text-ink-400">{a.source === "generated" ? "Generated" : "Uploaded"}</div>
                <div className="flex flex-col gap-1 pt-0.5">
                  <button
                    type="button"
                    className="chip text-[11px] py-1 w-full justify-center text-center"
                    disabled={busyAsset === a.id || startingIds.includes(a.id)}
                    onClick={() => startOp("remove_background", a)}
                    title="Make a copy with the background cut out (transparent PNG). It lands in Studio; this picture stays."
                    aria-label={`${removeBgLabel(opModels)}: ${slotLabel(kind, a.asset_type)} image`}
                  >
                    {removeBgLabel(opModels)}
                  </button>
                  <button
                    type="button"
                    className="chip text-[11px] py-1 w-full justify-center text-center"
                    disabled={busyAsset === a.id || startingIds.includes(a.id)}
                    onClick={() => setUpscaling(a)}
                    title="Make a larger copy (×2 or ×4). It lands in Studio; this picture stays."
                    aria-label={`Upscale ${slotLabel(kind, a.asset_type)} image`}
                  >
                    Upscale…
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>

        {runs.length > 0 && (
          <div className="mt-4">
            <div className="label">New versions</div>
            <ul className="space-y-2" aria-label="Background removal and upscale results">
              {runs.map(({ job: j, assetType, saved }) => {
                const done = j.status === "completed" || j.status === "saved";
                return (
                  <li key={j.id} className="card p-2 flex items-center gap-3">
                    <div
                      className={clsx(
                        "w-14 h-14 shrink-0 rounded-sm overflow-hidden bg-ink-800 flex items-center justify-center",
                        done && j.operation === "remove_background" && "checker"
                      )}
                    >
                      {done ? (
                        <img src={api.studioPreview(j.id)} alt={j.label || "Result"} className="w-full h-full object-contain" />
                      ) : (
                        <span className="text-[10px] text-ink-400">{j.status === "failed" ? "Failed" : "Making…"}</span>
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm truncate">{j.label || j.prompt}</div>
                      <div className="text-[11px] text-ink-400 truncate">
                        {j.status === "failed"
                          ? j.error || "Failed"
                          : done
                          ? j.result_width && j.result_height
                            ? `${j.result_width} × ${j.result_height} px`
                            : "Ready"
                          : "Working on it…"}
                      </div>
                    </div>
                    <div className="flex flex-wrap justify-end gap-1 shrink-0">
                      {done && (
                        <button
                          type="button"
                          className="chip text-[11px] py-1 disabled:opacity-50"
                          disabled={saved || savingIds.includes(j.id)}
                          onClick={() => saveBack(j.id)}
                          title={`Save it to this ${kind === "avatar" ? "character" : "cast member"} as ${slotLabel(kind, assetType)}`}
                        >
                          {savingIds.includes(j.id) ? "Saving…" : saved ? "Saved ✓" : "Save back to this slot"}
                        </button>
                      )}
                      <Link href={`/studio?job=${j.id}`} className="chip text-[11px] py-1">
                        Open in Studio
                      </Link>
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {upscaling && (
          <UpscaleDialog
            source={{ assetId: upscaling.id }}
            sourceLabel={`this ${slotLabel(kind, upscaling.asset_type).toLowerCase()} picture`}
            models={opModels.upscale}
            modelsLoaded={opModels.loaded}
            onClose={() => setUpscaling(null)}
            onStarted={(j) => {
              const a = upscaling;
              setUpscaling(null);
              startOp("upscale", a, j);
            }}
          />
        )}

        {addingFromStudio && (
          <AssignDialog
            owner={{ kind, id, name: (entity as Avatar | Ingredient).name }}
            onClose={() => setAddingFromStudio(false)}
            onSaved={async () => {
              setAddingFromStudio(false);
              await reload();
              onChange();
            }}
          />
        )}
      </div>
    </div>
  );
}


/**
 * "Adult content allowed" for one Cast character: an adult (18+), fictional
 * or a real adult who consented. While Adult mode is on, every picture on a
 * ticked character's card skips the content filter that's forced on for Cast
 * pictures (Sogni's safe filter, Runware's content check), however it was
 * made, and follows the filter settings like any other picture. Pictures of
 * unticked characters always get it, and so does a prompt that mentions a
 * child. Minors are always refused.
 */
function AdultOkCard({ avatarId, name }: { avatarId: number; name: string }) {
  const toast = useToast();
  const { mode, failed, reload } = useContentModeState();
  const [busy, setBusy] = useState(false);
  if (!mode) {
    return failed ? (
      <div className="card p-4 mb-4 text-xs text-ink-400">
        Couldn&rsquo;t load the adult content setting.{" "}
        <button type="button" className="underline" onClick={reload}>
          Retry
        </button>
      </div>
    ) : null;
  }
  const allowed = (mode.adult_ok_avatars ?? []).includes(avatarId);

  const toggle = async (next: boolean) => {
    if (
      next &&
      !window.confirm(
        `Allow adult content with ${name}'s pictures?\n\nConfirm that ${name} is an adult (18 or older), either fictional or a real adult who has consented, and that the pictures you put on ${name}'s card show ${name}.\n\nWhile Adult mode is on, every picture on ${name}'s card then skips the filter that's forced on for Cast pictures (Sogni's safe filter, Runware's content check), however it was made (uploaded, made in Studio, upscaled or cut out). It follows your Sogni and Runware filter settings, like any other picture. Pictures of characters that aren't ticked always get the filter, and so does a prompt that mentions a child. Prompts involving minors are always refused.`
      )
    )
      return;
    setBusy(true);
    try {
      await api.setAvatarAdultOk(avatarId, next);
      window.dispatchEvent(new Event("content-mode-changed"));
      toast.success(next ? `${name}: adult content allowed` : `${name}: pictures are always checked`);
    } catch (err) {
      toast.error(errorText(err, "Couldn't change the setting"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card p-4 mb-4 space-y-2">
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-1"
          checked={allowed}
          disabled={busy}
          onChange={(e) => toggle(e.target.checked)}
        />
        <span>
          <span className="font-medium">Adult content allowed</span>
          <span className="block text-xs text-ink-400">
            {name} is an adult (18+), fictional or a real adult who consented. While Adult mode is on, every picture
            on {name}&rsquo;s card skips the filter that&rsquo;s forced on for Cast pictures (Sogni&rsquo;s safe
            filter, Runware&rsquo;s content check), however it was made, and follows your filter settings like any
            other picture. Pictures of characters that aren&rsquo;t ticked always get the filter, and so does a
            prompt that mentions a child.
            {mode.adult ? "" : " Adult mode is off, so this has no effect right now."} Prompts involving minors are
            always refused.
          </span>
        </span>
      </label>
    </div>
  );
}
