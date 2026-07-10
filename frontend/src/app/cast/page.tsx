"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { Dropzone } from "@/components/Dropzone";
import { TrainPanel } from "@/components/TrainPanel";
import { api, Asset, Avatar, Ingredient, Project } from "@/lib/api";

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
              heroUrl:
                i.assets[0]?.public_token && api.publicAsset(i.assets[0].public_token),
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
          <div className="aspect-[9/16] skeleton" />
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
          <div className="aspect-[9/16] bg-ink-800 relative">
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
            disabled={!name.trim()}
            onClick={async () => {
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
            }}
          >
            Create
          </button>
        </div>
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
  const [voices, setVoices] = useState<{ voice_id: string; name: string }[]>([]);

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
              {(kind === "avatar"
                ? ["hero", "reference_sheet", "expression_sheet", "outfit_sheet", "lifestyle", "logo", "other"]
                : ["hero", "reference", "other"]
              ).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <Dropzone
            disabled={!rights}
            hint={
              rights
                ? "PNG, JPEG, or WEBP up to 15 MB each. EXIF is stripped on upload."
                : "Confirm rights above to enable uploads."
            }
            onUpload={(file) => upload(file)}
            onSettled={() => {
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
                  const updated = await api.updateAvatar(id, {
                    elevenlabs_voice_id: next,
                    default_voice_provider: next ? "elevenlabs" : "mock",
                  });
                  setEntity(updated);
                  onChange();
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
            </div>
            <div className="text-[10px] text-ink-400">
              {voices.length === 0
                ? "Mock TTS only — set ELEVENLABS_API_KEY to pick a real voice."
                : `${voices.length} voice${voices.length === 1 ? "" : "s"} available from the configured TTS provider.`}
            </div>
          </div>
        )}

        <TrainPanel kind={kind} id={id} assets={(entity as any).assets} />

        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {(entity as any).assets.map((a: Asset) => (
            <div
              key={a.id}
              className="card overflow-hidden aspect-square bg-ink-800 relative"
            >
              <img
                src={api.publicAsset(a.public_token)}
                className="w-full h-full object-cover"
                alt={a.asset_type}
              />
              <div className="absolute bottom-0 inset-x-0 p-2 bg-black/70 text-[10px] text-ink-100">
                {a.asset_type} · {a.source}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
