"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { api, Avatar, BrandKit, CastMember, Ingredient, Project, RenderPlan } from "@/lib/api";

const usd = (n: number) => `$${n.toFixed(2)}`;

// Same choices as the New Project page.
const MODES = [
  { id: "reel_montage", label: "Reel Montage", desc: "2–5 short cuts (most reliable)" },
  { id: "talking_head_beta", label: "Talking Head", desc: "Beta — lip sync may not be perfect" },
  { id: "static_motion", label: "Static Motion", desc: "Hero image with subtle motion" },
];
const CAPTION_STYLES = [
  { id: "clean_white", label: "Clean White" },
  { id: "influencer_bold", label: "Influencer Bold" },
  { id: "minimal_lower_third", label: "Minimal Lower Third" },
];
const ASPECTS = [
  { id: "9:16", label: "9:16 Vertical" },
  { id: "1:1", label: "1:1 Square" },
  { id: "16:9", label: "16:9 Wide" },
];

type Draft = {
  title: string;
  mode: string;
  aspect_ratio: string;
  target_duration_seconds: number;
  caption_style: string;
  include_disclosure: boolean;
  disclosure_text: string;
  cta_text: string;
  brand_kit_id: number | null;
  cast: CastMember[];
};

const fromProject = (p: Project): Draft => ({
  title: p.title,
  mode: p.mode,
  aspect_ratio: p.aspect_ratio,
  target_duration_seconds: p.target_duration_seconds,
  caption_style: p.caption_style,
  include_disclosure: p.include_disclosure,
  disclosure_text: p.disclosure_text,
  cta_text: p.cta_text,
  brand_kit_id: p.brand_kit_id,
  cast: p.cast_members.map((c) => ({
    member_kind: c.member_kind,
    avatar_id: c.avatar_id ?? null,
    ingredient_id: c.ingredient_id ?? null,
    role: c.role,
  })),
});

const castKey = (cast: CastMember[]) =>
  cast
    .map((c) => `${c.member_kind}:${c.member_kind === "avatar" ? c.avatar_id : c.ingredient_id}:${c.role}`)
    .sort()
    .join("|");

// Fields whose change only needs the video put together again (free).
const RECOMPOSE_FIELDS: (keyof Draft)[] = [
  "title",
  "caption_style",
  "include_disclosure",
  "disclosure_text",
  "cta_text",
  "brand_kit_id",
];
const FIELD_LABEL: Partial<Record<keyof Draft, string>> = {
  title: "title",
  caption_style: "caption style",
  include_disclosure: "disclosure",
  disclosure_text: "disclosure text",
  cta_text: "end-card text",
  brand_kit_id: "brand kit",
  mode: "mode",
  target_duration_seconds: "target length",
  cast: "cast",
};

type Outcome = {
  recompose: string[];
  replan: string[];
  aspect: { from: string; to: string } | null;
  /** Clips already made that this save put out of date (from the render plan
   *  before and after saving); null when it could not be worked out. */
  stale: { count: number; cost: number; orders: number[] } | null;
  castChanged: boolean;
};

// Fields clips or the voiceover are made from: refused while the video is being made.
const CLIP_FIELDS: (keyof Draft)[] = ["aspect_ratio", "mode", "cast"];

/** Shots that have a clip which the next Generate video would make again. */
const staleRows = (rp: RenderPlan | null) =>
  new Map(
    (rp?.shots || [])
      .filter((r) => r.action === "generate" && r.reason !== "no clip yet")
      .map((r) => [r.id, r] as const)
  );

/**
 * "Project settings": everything chosen on the New Project page, editable
 * after creation. Saving says what the change needs (re-compose, new clips,
 * re-plan) and never starts a paid step by itself.
 */
export function ProjectSettingsCard({
  project,
  onSaved,
  onReplan,
  onRecompose,
}: {
  project: Project;
  onSaved: (p: Project) => void;
  /** Opens the re-plan question (does not re-plan). */
  onReplan: () => void;
  /** Free re-compose; offered only when a finished video exists. */
  onRecompose?: () => void;
}) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => fromProject(project));
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [brandKits, setBrandKits] = useState<BrandKit[]>([]);
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  // Load the pickers the first time the card is opened.
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!open || loaded) return;
    setLoaded(true);
    api.listAvatars().then(setAvatars).catch(() => setAvatars([]));
    api.listIngredients().then(setIngredients).catch(() => setIngredients([]));
    api.listBrandKits().then(setBrandKits).catch(() => setBrandKits([]));
  }, [open, loaded]);

  // Opening the card starts from what the project has now.
  const toggle = () => {
    if (!open) {
      setDraft(fromProject(project));
      setOutcome(null);
    }
    setOpen(!open);
  };

  const saved = fromProject(project);
  const changed = (Object.keys(draft) as (keyof Draft)[]).filter((k) =>
    k === "cast" ? castKey(draft.cast) !== castKey(saved.cast) : draft[k] !== saved[k]
  );

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));

  const hasAvatar = (id: number) => draft.cast.some((c) => c.member_kind === "avatar" && c.avatar_id === id);
  const hasIngredient = (id: number) =>
    draft.cast.some((c) => c.member_kind === "ingredient" && c.ingredient_id === id);
  const toggleAvatar = (av: Avatar) =>
    set(
      "cast",
      hasAvatar(av.id)
        ? draft.cast.filter((c) => !(c.member_kind === "avatar" && c.avatar_id === av.id))
        : [...draft.cast, { member_kind: "avatar", avatar_id: av.id, ingredient_id: null, role: "host" }]
    );
  const toggleIngredient = (ing: Ingredient) =>
    set(
      "cast",
      hasIngredient(ing.id)
        ? draft.cast.filter((c) => !(c.member_kind === "ingredient" && c.ingredient_id === ing.id))
        : [
            ...draft.cast,
            {
              member_kind: "ingredient",
              avatar_id: null,
              ingredient_id: ing.id,
              role: ing.kind === "scene" ? "location" : ing.kind,
            },
          ]
    );

  const clipsMade = project.shots.filter((s) => s.clip_path && s.shot_type !== "end_card").length;
  // A render or re-roll is running: fields clips are made from wait (the server refuses them too).
  const busy =
    !["draft", "planned", "completed", "failed", "cancelled"].includes(project.status) ||
    project.shots.some((x) => ["submitted", "polling", "running"].includes(x.status));
  const blockedFields = busy ? changed.filter((k) => CLIP_FIELDS.includes(k)) : [];

  const save = async () => {
    if (changed.length === 0 || blockedFields.length > 0) return;
    setSaving(true);
    try {
      let before: RenderPlan | null = null;
      if (project.generated_plan_json) {
        try {
          before = await api.renderPlan(project.id);
        } catch {
          before = null;
        }
      }
      const body: Record<string, unknown> = {};
      for (const k of changed) if (k !== "cast") body[k] = draft[k];
      // Keep the main character in the cast (its voice and look lead the video).
      const castAvatars = draft.cast.filter((c) => c.member_kind === "avatar").map((c) => c.avatar_id);
      if (changed.includes("cast") && castAvatars.length > 0 && !castAvatars.includes(project.primary_avatar_id)) {
        body.primary_avatar_id = castAvatars[0];
      }
      let next = project;
      if (Object.keys(body).length > 0) next = await api.updateProject(project.id, body);
      if (changed.includes("cast")) next = await api.updateCast(project.id, draft.cast);
      onSaved(next);

      const result: Outcome = {
        recompose: changed.filter((k) => RECOMPOSE_FIELDS.includes(k)).map((k) => FIELD_LABEL[k] || k),
        replan: changed
          .filter((k) => k === "cast" || k === "mode" || k === "target_duration_seconds")
          .map((k) => FIELD_LABEL[k] || k),
        aspect: changed.includes("aspect_ratio") ? { from: saved.aspect_ratio, to: draft.aspect_ratio } : null,
        stale: clipsMade > 0 ? null : { count: 0, cost: 0, orders: [] },
        castChanged: changed.includes("cast"),
      };
      if (clipsMade > 0 && project.generated_plan_json && before) {
        try {
          const after = await api.renderPlan(project.id);
          const was = staleRows(before);
          const now = [...staleRows(after).values()].filter((r) => !was.has(r.id));
          result.stale = {
            count: now.length,
            cost: now.reduce((sum, r) => sum + r.cost, 0),
            orders: now.map((r) => r.shot_order).sort((a, b) => a - b),
          };
        } catch {
          result.stale = null;
        }
      }
      setOutcome(result);
      toast.success("Project settings saved");
    } catch (e: any) {
      toast.error(e.message || "Couldn't save the settings");
    } finally {
      setSaving(false);
    }
  };

  const hasVideo = project.status === "completed" && !!onRecompose;

  return (
    <section className="card p-4 mb-6">
      <button
        type="button"
        className="w-full flex items-center justify-between gap-2 text-left"
        onClick={toggle}
        aria-expanded={open}
      >
        <h2 className="text-lg font-medium">Project settings</h2>
        <span className="text-xs text-ink-400">{open ? "Hide" : "Title, cast, mode, aspect, captions, end card…"}</span>
      </button>

      {open && (
        <div className="mt-4 space-y-5">
          <div>
            <label className="label" htmlFor="ps-title">Title</label>
            <input id="ps-title" className="input" value={draft.title} onChange={(e) => set("title", e.target.value)} />
          </div>

          <div>
            <div className="label">Cast</div>
            <div className="text-xs uppercase text-ink-300 mt-1 mb-1">Characters</div>
            <div className="flex flex-wrap gap-2">
              {avatars.length === 0 && <span className="text-xs text-ink-400">No characters yet — create one in Cast.</span>}
              {avatars.map((av) => {
                const hero = av.assets.find((a) => a.asset_type === "hero") || av.assets[0];
                return (
                  <button
                    key={av.id}
                    type="button"
                    onClick={() => toggleAvatar(av)}
                    className={clsx("chip flex items-center gap-2 pl-1", hasAvatar(av.id) && "chip-active")}
                    title={av.persona || av.name}
                    aria-pressed={hasAvatar(av.id)}
                  >
                    {hero ? (
                      <img src={api.publicAsset(hero.public_token)} alt="" className="w-6 h-6 rounded-full object-cover border border-ink-700" />
                    ) : (
                      <span aria-hidden className="w-6 h-6 rounded-full bg-ink-700 text-[10px] flex items-center justify-center text-ink-300">
                        {av.name.slice(0, 1).toUpperCase()}
                      </span>
                    )}
                    <span>{av.name}</span>
                  </button>
                );
              })}
            </div>
            <div className="text-xs uppercase text-ink-300 mt-3 mb-1">Scenes and ingredients</div>
            <div className="flex flex-wrap gap-2">
              {ingredients.length === 0 && <span className="text-xs text-ink-400">None yet.</span>}
              {ingredients.map((ing) => {
                const hero = ing.assets[0];
                return (
                  <button
                    key={ing.id}
                    type="button"
                    onClick={() => toggleIngredient(ing)}
                    className={clsx("chip flex items-center gap-2 pl-1", hasIngredient(ing.id) && "chip-active")}
                    title={ing.visual_identity || ing.name}
                    aria-pressed={hasIngredient(ing.id)}
                  >
                    {hero ? (
                      <img src={api.publicAsset(hero.public_token)} alt="" className="w-6 h-6 rounded-sm object-cover border border-ink-700" />
                    ) : (
                      <span aria-hidden className="w-6 h-6 rounded-sm bg-ink-700 text-[10px] flex items-center justify-center text-ink-300">
                        {ing.kind.slice(0, 1).toUpperCase()}
                      </span>
                    )}
                    <span>{ing.name}</span>
                    <span className="text-[10px] text-ink-400">{ing.kind}</span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <div className="label">Mode</div>
              <div className="flex flex-col gap-2">
                {MODES.map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => set("mode", m.id)}
                    className={clsx(
                      "text-left p-2.5 rounded-md border transition",
                      draft.mode === m.id ? "border-accent bg-accent/10" : "border-ink-700 hover:border-ink-500"
                    )}
                    aria-pressed={draft.mode === m.id}
                  >
                    <div className="text-sm font-medium">{m.label}</div>
                    <div className="text-xs text-ink-300 mt-0.5">{m.desc}</div>
                  </button>
                ))}
              </div>
            </div>
            <div className="space-y-4">
              <div>
                <label className="label" htmlFor="ps-length">Target length</label>
                <input
                  id="ps-length"
                  type="range"
                  min={10}
                  max={60}
                  step={5}
                  value={draft.target_duration_seconds}
                  onChange={(e) => set("target_duration_seconds", parseInt(e.target.value))}
                  className="w-full"
                />
                <div className="text-sm">{draft.target_duration_seconds}s</div>
              </div>
              <div>
                <div className="label">Aspect ratio</div>
                <div className="flex flex-wrap gap-2">
                  {ASPECTS.map((a) => (
                    <button
                      key={a.id}
                      type="button"
                      onClick={() => set("aspect_ratio", a.id)}
                      className={clsx("chip text-xs", draft.aspect_ratio === a.id && "chip-active")}
                      aria-pressed={draft.aspect_ratio === a.id}
                    >
                      {a.label}
                    </button>
                  ))}
                </div>
                {draft.aspect_ratio !== saved.aspect_ratio && clipsMade > 0 && (
                  <div className="text-[11px] text-amber-300 mt-1">
                    Every clip already made ({clipsMade}) is for {saved.aspect_ratio}. The next Generate video makes
                    them all again (paid).
                  </div>
                )}
              </div>
              <div>
                <label className="label" htmlFor="ps-caption">Caption style</label>
                <select id="ps-caption" className="input" value={draft.caption_style} onChange={(e) => set("caption_style", e.target.value)}>
                  {CAPTION_STYLES.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                  {!CAPTION_STYLES.some((c) => c.id === draft.caption_style) && (
                    <option value={draft.caption_style}>{draft.caption_style}</option>
                  )}
                </select>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <label className="text-sm flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={draft.include_disclosure}
                  onChange={(e) => set("include_disclosure", e.target.checked)}
                />
                Show disclosure overlay
              </label>
              {draft.include_disclosure && (
                <input
                  className="input"
                  aria-label="Disclosure text"
                  value={draft.disclosure_text}
                  onChange={(e) => set("disclosure_text", e.target.value)}
                  placeholder="AI-generated virtual creator"
                />
              )}
            </div>
            <div>
              <label className="label" htmlFor="ps-cta">End-card text</label>
              <input id="ps-cta" className="input" value={draft.cta_text} onChange={(e) => set("cta_text", e.target.value)} />
              <div className="text-[11px] text-ink-400 mt-1">Clear it to drop the end card.</div>
            </div>
          </div>

          <div>
            <label className="label" htmlFor="ps-brand">Brand kit</label>
            {brandKits.length === 0 ? (
              <div className="text-xs text-ink-400">
                No brand kits yet — create one under <a href="/brand" className="underline">Brand</a>.
              </div>
            ) : (
              <>
                <select
                  id="ps-brand"
                  className="input"
                  value={draft.brand_kit_id ?? ""}
                  onChange={(e) => set("brand_kit_id", e.target.value ? Number(e.target.value) : null)}
                >
                  {/* The project API can set a brand kit but not clear one. */}
                  <option value="" disabled={saved.brand_kit_id != null}>
                    — None —
                  </option>
                  {brandKits.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </select>
                {saved.brand_kit_id != null && (
                  <div className="text-[11px] text-ink-400 mt-1">You can switch to another kit; removing it is not possible here yet.</div>
                )}
              </>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button className="btn-primary text-xs disabled:opacity-50" onClick={save} disabled={saving || changed.length === 0 || blockedFields.length > 0}>
              {saving ? "Saving…" : "Save settings"}
            </button>
            <button
              className="btn-ghost text-xs disabled:opacity-50"
              onClick={() => setDraft(fromProject(project))}
              disabled={saving || changed.length === 0}
            >
              Undo changes
            </button>
            <span className="text-[11px] text-ink-400">Saving is free. Nothing is generated until you ask.</span>
          </div>
          {blockedFields.length > 0 && (
            <div className="text-[11px] text-amber-300">
              The {list(blockedFields.map((k) => FIELD_LABEL[k] || (k === "aspect_ratio" ? "aspect ratio" : k)))} can&apos;t
              change while the video or a clip is being made: the clips on their way would be out of date and paid for
              again. Wait until it finishes, or undo that change to save the rest.
            </div>
          )}

          {outcome && (outcome.recompose.length > 0 || outcome.replan.length > 0 || outcome.aspect) && (
            <div className="rounded-md border border-ink-700 p-3 space-y-2 text-xs text-ink-200">
              <div className="text-sm font-medium text-ink-100">What the change needs</div>
              {outcome.recompose.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  <span>
                    {cap(list(outcome.recompose))}:{" "}
                    {hasVideo ? "Re-compose (free) puts the video together again with it." : "used the next time the video is put together. Free."}
                  </span>
                  {hasVideo && (
                    <button className="btn-ghost text-xs" onClick={onRecompose}>
                      Re-compose (free)
                    </button>
                  )}
                </div>
              )}
              {outcome.aspect && (
                <div>
                  Aspect ratio {outcome.aspect.from} → {outcome.aspect.to}: clips are made at the new shape when you
                  generate the video.
                </div>
              )}
              {(outcome.aspect || outcome.castChanged) && (
                <div className={clsx(outcome.stale && outcome.stale.count > 0 && "text-amber-300")}>
                  {outcome.stale === null
                    ? "Couldn't work out which clips are now out of date: the shot list shows them, with their price."
                    : outcome.stale.count === 0
                    ? "No clip already made went out of date."
                    : `${outcome.stale.count === 1 ? "1 clip" : `${outcome.stale.count} clips`} already made ${
                        outcome.stale.count === 1 ? "is" : "are"
                      } now out of date (${outcome.stale.count === 1 ? "shot" : "shots"} ${outcome.stale.orders.join(
                        ", "
                      )}${
                        outcome.castChanged && !outcome.aspect
                          ? ": they have no pictures of their own, so they were made from the old cast's"
                          : ""
                      }). The next Generate video makes ${outcome.stale.count === 1 ? "it" : "them"} again: about ${usd(
                        outcome.stale.cost
                      )}. Re-compose keeps the old ${outcome.stale.count === 1 ? "clip" : "clips"}.`}
                </div>
              )}
              {outcome.replan.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  <span>
                    {cap(list(outcome.replan))}: the storyboard was planned for the old {list(outcome.replan)}. Re-plan to
                    write it again (one paid planner call; it asks what to keep first).
                  </span>
                  <button className="btn-ghost text-xs" onClick={onReplan}>
                    Re-plan…
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const list = (xs: string[]) =>
  xs.length <= 1 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`;
