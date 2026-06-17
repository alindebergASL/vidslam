"use client";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { api, Avatar, BrandKit, CastMember, Ingredient } from "@/lib/api";

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

// One-click briefs that prefill creative direction (and sensible mode/caption/disclosure)
// for common verticals — the engine is not dating-app specific.
const USE_CASE_PRESETS: {
  label: string;
  direction: string;
  mode?: string;
  caption?: string;
  disclosure?: string;
}[] = [
  {
    label: "Social creator",
    direction:
      "Punchy short-form social hook with a strong first line, fast energy, casual tone.",
    mode: "reel_montage",
    caption: "influencer_bold",
  },
  {
    label: "Product demo",
    direction:
      "Calm, trustworthy product demo. Show the value clearly, clean visuals, confident neutral tone.",
    mode: "reel_montage",
    caption: "clean_white",
    disclosure: "AI-generated demo",
  },
  {
    label: "Tutorial / explainer",
    direction:
      "Patient step-by-step tutorial. Clear sequencing, instructional cadence, no hype.",
    mode: "talking_head_beta",
    caption: "minimal_lower_third",
    disclosure: "AI-generated explainer",
  },
  {
    label: "Fitness",
    direction:
      "High-energy fitness motivation, fast cuts, bold and encouraging tone.",
    mode: "reel_montage",
    caption: "influencer_bold",
  },
  {
    label: "Music promo",
    direction:
      "Moody music-promo aesthetic, rhythmic cuts that hit the beat, atmospheric and stylish.",
    mode: "reel_montage",
    caption: "minimal_lower_third",
  },
  {
    label: "Brand mascot",
    direction:
      "Friendly brand-mascot spot. Warm, playful, on-brand and memorable.",
    mode: "static_motion",
    caption: "clean_white",
    disclosure: "AI-generated mascot",
  },
];

export default function NewProjectPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const router = useRouter();
  const search = useSearchParams();
  const seedOwnerKind = search?.get("owner_kind") as "avatar" | "ingredient" | null;
  const seedOwnerId = search?.get("owner_id") ? Number(search.get("owner_id")) : null;

  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [title, setTitle] = useState("");
  const [script, setScript] = useState("");
  const [mode, setMode] = useState("reel_montage");
  const [duration, setDuration] = useState(25);
  const [aspectRatio, setAspectRatio] = useState("9:16");
  const [cta, setCta] = useState("");
  const [captionStyle, setCaptionStyle] = useState("clean_white");
  const [disclosure, setDisclosure] = useState(true);
  const [disclosureText, setDisclosureText] = useState("");
  const [creativeDirection, setCreativeDirection] = useState("");
  const [cast, setCast] = useState<CastMember[]>([]);
  const [primary, setPrimary] = useState<number | null>(null);
  const [brandKits, setBrandKits] = useState<BrandKit[]>([]);
  const [brandKitId, setBrandKitId] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listAvatars().then((avs) => {
      setAvatars(avs);
      // Seed from query params if present, else pick the first avatar.
      if (seedOwnerKind === "avatar" && seedOwnerId) {
        const av = avs.find((a) => a.id === seedOwnerId);
        if (av) {
          setPrimary(av.id);
          setCast((cur) => {
            if (cur.some((c) => c.member_kind === "avatar" && c.avatar_id === av.id))
              return cur;
            return [...cur, { member_kind: "avatar", avatar_id: av.id, role: "host" }];
          });
          return;
        }
      }
      if (avs[0] && !seedOwnerKind) {
        setPrimary(avs[0].id);
        setCast([{ member_kind: "avatar", avatar_id: avs[0].id, role: "host" }]);
      }
    });
    api.listBrandKits().then(setBrandKits);
    api.listIngredients().then((ings) => {
      setIngredients(ings);
      if (seedOwnerKind === "ingredient" && seedOwnerId) {
        const ing = ings.find((i) => i.id === seedOwnerId);
        if (ing) {
          setCast((cur) => {
            if (
              cur.some((c) => c.member_kind === "ingredient" && c.ingredient_id === ing.id)
            )
              return cur;
            return [
              ...cur,
              {
                member_kind: "ingredient",
                ingredient_id: ing.id,
                role: ing.kind === "scene" ? "location" : ing.kind,
              },
            ];
          });
        }
      }
    });
  }, [seedOwnerKind, seedOwnerId]);

  const toggleAvatar = (av: Avatar, role = "host") => {
    setCast((cur) => {
      const exists = cur.find((c) => c.member_kind === "avatar" && c.avatar_id === av.id);
      if (exists) return cur.filter((c) => !(c.member_kind === "avatar" && c.avatar_id === av.id));
      return [...cur, { member_kind: "avatar", avatar_id: av.id, role }];
    });
  };

  const toggleIngredient = (ing: Ingredient) => {
    setCast((cur) => {
      const exists = cur.find(
        (c) => c.member_kind === "ingredient" && c.ingredient_id === ing.id
      );
      if (exists)
        return cur.filter(
          (c) => !(c.member_kind === "ingredient" && c.ingredient_id === ing.id)
        );
      return [
        ...cur,
        {
          member_kind: "ingredient",
          ingredient_id: ing.id,
          role: ing.kind === "scene" ? "location" : ing.kind,
        },
      ];
    });
  };

  const submit = async () => {
    if (!script.trim()) {
      setError("Write a short script first.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const project = await api.createProject({
        title: title || "Untitled",
        original_script: script,
        mode,
        target_duration_seconds: duration,
        aspect_ratio: aspectRatio,
        cta_text: cta,
        caption_style: captionStyle,
        include_disclosure: disclosure,
        disclosure_text: disclosureText,
        creative_direction: creativeDirection,
        primary_avatar_id: primary,
        brand_kit_id: brandKitId,
        cast,
      });
      router.push(`/projects/${project.id}`);
    } catch (e: any) {
      setError(e.message);
      setSubmitting(false);
    }
  };

  return (
    <div className="p-8 max-w-4xl">
      <h1 className="text-3xl font-semibold mb-6">New Project</h1>

      <div className="space-y-6">
        <div className="card p-4">
          <label className="label">Title</label>
          <input
            className="input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Kissmet — Specific Detail"
          />
        </div>

        <div className="card p-4">
          <label className="label">Script</label>
          <textarea
            className="input min-h-[140px]"
            value={script}
            onChange={(e) => setScript(e.target.value)}
            placeholder="If your dating bio says food, travel, and music…"
          />
          <div className="text-xs text-ink-400 mt-2">
            The planner will tighten this into a short-form voiceover.
          </div>
        </div>

        <div className="card p-4">
          <label className="label">Creative direction (optional)</label>
          <div className="flex flex-wrap gap-1.5 mb-2">
            {USE_CASE_PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                className="chip text-[11px] py-1"
                title={p.direction}
                onClick={() => {
                  setCreativeDirection(p.direction);
                  if (p.mode) setMode(p.mode);
                  if (p.caption) setCaptionStyle(p.caption);
                  if (p.disclosure !== undefined) setDisclosureText(p.disclosure);
                }}
              >
                {p.label}
              </button>
            ))}
          </div>
          <textarea
            className="input min-h-[80px]"
            value={creativeDirection}
            onChange={(e) => setCreativeDirection(e.target.value)}
            placeholder="Describe the use case + tone so the planner adapts — e.g. 'Calm product demo for a B2B SaaS dashboard, clean and trustworthy' or 'High-energy fitness hook, fast cuts'."
          />
          <div className="text-xs text-ink-400 mt-2">
            Steers the storyboard for any vertical (demo, education, fitness, promo…),
            not just social-creator content.
          </div>
        </div>

        <div className="card p-4">
          <div className="label">Cast</div>
          <div className="text-xs text-ink-400 mb-2">
            Pick one or more characters. Add scene/style/object ingredients for richer prompts.
          </div>
          <div className="text-xs uppercase text-ink-300 mt-3 mb-1">Characters</div>
          <div className="flex flex-wrap gap-2">
            {avatars.length === 0 && (
              <span className="text-xs text-ink-400">No characters yet — create one in Cast.</span>
            )}
            {avatars.map((av) => {
              const on = cast.some(
                (c) => c.member_kind === "avatar" && c.avatar_id === av.id
              );
              const hero = av.assets.find((a) => a.asset_type === "hero") || av.assets[0];
              return (
                <button
                  key={av.id}
                  onClick={() => {
                    toggleAvatar(av);
                    if (!primary || primary === av.id) setPrimary(av.id);
                  }}
                  className={clsx("chip flex items-center gap-2 pl-1", on && "chip-active")}
                  title={av.persona || av.name}
                >
                  {hero ? (
                    <img
                      src={api.publicAsset(hero.public_token)}
                      alt=""
                      className="w-6 h-6 rounded-full object-cover border border-ink-700"
                    />
                  ) : (
                    <span
                      aria-hidden
                      className="w-6 h-6 rounded-full bg-ink-700 text-[10px] flex items-center justify-center text-ink-300"
                    >
                      {av.name.slice(0, 1).toUpperCase()}
                    </span>
                  )}
                  <span>{av.name}</span>
                </button>
              );
            })}
          </div>
          <div className="text-xs uppercase text-ink-300 mt-4 mb-1">Ingredients</div>
          <div className="flex flex-wrap gap-2">
            {ingredients.length === 0 && (
              <span className="text-xs text-ink-400">None yet.</span>
            )}
            {ingredients.map((ing) => {
              const on = cast.some(
                (c) => c.member_kind === "ingredient" && c.ingredient_id === ing.id
              );
              const hero = ing.assets[0];
              return (
                <button
                  key={ing.id}
                  onClick={() => toggleIngredient(ing)}
                  className={clsx("chip flex items-center gap-2 pl-1", on && "chip-active")}
                  title={ing.visual_identity || ing.name}
                >
                  {hero ? (
                    <img
                      src={api.publicAsset(hero.public_token)}
                      alt=""
                      className="w-6 h-6 rounded object-cover border border-ink-700"
                    />
                  ) : (
                    <span
                      aria-hidden
                      className="w-6 h-6 rounded bg-ink-700 text-[10px] flex items-center justify-center text-ink-300"
                    >
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

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="card p-4 md:col-span-2">
            <div className="label">Mode</div>
            <div className="flex flex-col gap-2">
              {MODES.map((m) => (
                <button
                  key={m.id}
                  onClick={() => setMode(m.id)}
                  className={clsx(
                    "text-left p-3 rounded-md border transition",
                    mode === m.id
                      ? "border-accent bg-accent/10"
                      : "border-ink-700 hover:border-ink-500"
                  )}
                >
                  <div className="text-sm font-medium">{m.label}</div>
                  <div className="text-xs text-ink-300 mt-0.5">{m.desc}</div>
                </button>
              ))}
            </div>
          </div>
          <div className="card p-4 space-y-4">
            <div>
              <label className="label">Duration</label>
              <input
                type="range"
                min={10}
                max={60}
                step={5}
                value={duration}
                onChange={(e) => setDuration(parseInt(e.target.value))}
                className="w-full"
              />
              <div className="text-sm">{duration}s</div>
            </div>
            <div>
              <label className="label">Aspect ratio</label>
              <div className="flex gap-2">
                {[
                  { id: "9:16", label: "9:16 Vertical" },
                  { id: "1:1", label: "1:1 Square" },
                  { id: "16:9", label: "16:9 Wide" },
                ].map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    onClick={() => setAspectRatio(a.id)}
                    className={clsx("chip text-xs", aspectRatio === a.id && "chip-active")}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="label">Caption style</label>
              <select
                className="input"
                value={captionStyle}
                onChange={(e) => setCaptionStyle(e.target.value)}
              >
                {CAPTION_STYLES.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
            <label className="text-sm flex items-center gap-2">
              <input
                type="checkbox"
                checked={disclosure}
                onChange={(e) => setDisclosure(e.target.checked)}
              />
              Show disclosure overlay
            </label>
            {disclosure && (
              <div>
                <label className="label">Disclosure text</label>
                <input
                  className="input"
                  value={disclosureText}
                  onChange={(e) => setDisclosureText(e.target.value)}
                  placeholder="AI-generated virtual creator"
                />
              </div>
            )}
          </div>
        </div>

        <div className="card p-4">
          <label className="label">CTA / end-card text</label>
          <input
            className="input"
            value={cta}
            onChange={(e) => setCta(e.target.value)}
            placeholder="Kissmet dating app coming soon"
          />
        </div>

        <div className="card p-4">
          <label className="label">Brand kit (optional)</label>
          {brandKits.length === 0 ? (
            <div className="text-xs text-ink-400">
              No brand kits yet — create one under <a href="/brand" className="underline">Brand</a> to
              apply a logo + colors to the end card.
            </div>
          ) : (
            <select
              className="input"
              value={brandKitId ?? ""}
              onChange={(e) => setBrandKitId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">— None —</option>
              {brandKits.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          )}
        </div>

        {error && <div className="text-sm text-accent">{error}</div>}

        <div className="flex justify-end">
          <button className="btn-primary" disabled={submitting} onClick={submit}>
            {submitting ? "Creating…" : "Create + Generate Plan →"}
          </button>
        </div>
      </div>
    </div>
  );
}
