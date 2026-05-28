"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { api, Avatar, CastMember, Ingredient } from "@/lib/api";

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

export default function NewProjectPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const router = useRouter();
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [title, setTitle] = useState("");
  const [script, setScript] = useState("");
  const [mode, setMode] = useState("reel_montage");
  const [duration, setDuration] = useState(25);
  const [cta, setCta] = useState("");
  const [captionStyle, setCaptionStyle] = useState("clean_white");
  const [disclosure, setDisclosure] = useState(true);
  const [cast, setCast] = useState<CastMember[]>([]);
  const [primary, setPrimary] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listAvatars().then((avs) => {
      setAvatars(avs);
      if (avs[0]) {
        setPrimary(avs[0].id);
        setCast([{ member_kind: "avatar", avatar_id: avs[0].id, role: "host" }]);
      }
    });
    api.listIngredients().then(setIngredients);
  }, []);

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
        cta_text: cta,
        caption_style: captionStyle,
        include_disclosure: disclosure,
        primary_avatar_id: primary,
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
              return (
                <button
                  key={av.id}
                  onClick={() => {
                    toggleAvatar(av);
                    if (!primary || primary === av.id) setPrimary(av.id);
                  }}
                  className={clsx("chip", on && "chip-active")}
                >
                  {av.name}
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
              return (
                <button
                  key={ing.id}
                  onClick={() => toggleIngredient(ing)}
                  className={clsx("chip", on && "chip-active")}
                >
                  {ing.name}
                  <span className="text-[10px] text-ink-400 ml-1">{ing.kind}</span>
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
