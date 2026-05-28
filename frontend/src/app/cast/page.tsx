"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { api, Asset, Avatar, Ingredient } from "@/lib/api";

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
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);
  const [openKind, setOpenKind] = useState<"avatar" | "ingredient">("avatar");

  const reload = async () => {
    setAvatars(await api.listAvatars());
    setIngredients(await api.listIngredients());
  };
  useEffect(() => {
    reload();
  }, []);

  const tabIngKind = tab === "scenes" ? "scene" : tab === "styles" ? "style" : "object";

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

      <div className="flex flex-wrap gap-2 mb-6">
        {(["characters", "scenes", "styles", "objects"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={clsx("chip capitalize", tab === t && "chip-active")}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "characters" ? (
        <Grid
          items={avatars.map((a) => ({
            id: a.id,
            kind: "avatar" as const,
            title: a.name,
            sub: a.persona || a.brand || "—",
            heroUrl:
              a.assets.find((x) => x.asset_type === "hero")?.public_token &&
              api.publicAsset(a.assets.find((x) => x.asset_type === "hero")!.public_token),
            count: a.assets.length,
          }))}
          onOpen={(id) => {
            setOpenKind("avatar");
            setOpenId(id);
          }}
        />
      ) : (
        <Grid
          items={ingredients
            .filter((i) =>
              tabIngKind === "object" ? i.kind === "object" || i.kind === "prop" : i.kind === tabIngKind
            )
            .map((i) => ({
              id: i.id,
              kind: "ingredient" as const,
              title: i.name,
              sub: i.visual_identity || i.kind,
              heroUrl:
                i.assets[0]?.public_token && api.publicAsset(i.assets[0].public_token),
              count: i.assets.length,
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

function Grid({
  items,
  onOpen,
}: {
  items: { id: number; kind: "avatar" | "ingredient"; title: string; sub: string; heroUrl?: string | null | false; count: number }[];
  onOpen: (id: number) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="card p-8 text-ink-300 text-sm text-center">
        Nothing here yet. Click <em>+ New</em> above.
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
      {items.map((it) => (
        <button
          key={`${it.kind}-${it.id}`}
          onClick={() => onOpen(it.id)}
          className="card overflow-hidden hover:border-ink-600 transition text-left"
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
          <label className="label">Name</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
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

  const upload = async (file: File) => {
    if (!rights) {
      setError("Please confirm you have rights to use this asset.");
      return;
    }
    setError(null);
    try {
      if (kind === "avatar") await api.uploadAvatarAsset(id, file, assetType, true);
      else await api.uploadIngredientAsset(id, file, assetType, true);
      await reload();
      onChange();
    } catch (e: any) {
      setError(e.message || "Upload failed");
    }
  };

  if (!entity) return null;

  return (
    <div className="fixed inset-0 z-30 bg-black/60 flex" onClick={onClose}>
      <div
        className="ml-auto h-full w-full max-w-xl bg-ink-900 border-l border-ink-800 p-6 overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="text-xl font-semibold">{(entity as any).name}</div>
            <div className="text-xs text-ink-400 mt-0.5">
              {kind === "avatar" ? "Character" : `${(entity as Ingredient).kind} ingredient`}
            </div>
          </div>
          <button className="btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="card p-4 mb-4 space-y-3">
          <label className="block text-xs font-medium text-ink-200">
            <input
              type="checkbox"
              className="mr-2 align-middle"
              checked={rights}
              onChange={(e) => setRights(e.target.checked)}
            />
            I own or have rights to use these avatar assets.
          </label>
          <div className="flex gap-2 items-center">
            <select
              className="input flex-1"
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
            <label className="btn-primary cursor-pointer">
              <input
                type="file"
                hidden
                accept="image/png,image/jpeg,image/webp"
                onChange={(e) => e.target.files && upload(e.target.files[0])}
              />
              Upload
            </label>
          </div>
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
              </select>
            </div>
            <div className="text-[10px] text-ink-400">
              {voices.length === 0
                ? "Mock TTS only — set ELEVENLABS_API_KEY to pick a real voice."
                : `${voices.length} voice${voices.length === 1 ? "" : "s"} available from the configured TTS provider.`}
            </div>
          </div>
        )}

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
