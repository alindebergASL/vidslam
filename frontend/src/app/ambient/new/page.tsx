"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { useToast } from "@/components/Toaster";
import { ChipRow, PlanSummary, SceneArt, fmtDuration, presetFor } from "@/components/ambient/shared";
import {
  api,
  AmbientConfig,
  AmbientPresets,
  AmbientSource,
  CostEstimate,
  DEFAULT_AMBIENT_CONFIG,
} from "@/lib/api";

export default function NewAmbientPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

const STILLNESS: { factor: number; label: string; hint: string }[] = [
  { factor: 1, label: "Natural", hint: "As generated" },
  { factor: 1.5, label: "Slower", hint: "⅔ speed" },
  { factor: 2, label: "Barely moves", hint: "Half speed" },
];

function Inner() {
  const router = useRouter();
  const toast = useToast();
  const [presets, setPresets] = useState<AmbientPresets | null>(null);
  const [cfg, setCfg] = useState<AmbientConfig>(DEFAULT_AMBIENT_CONFIG);
  const [title, setTitle] = useState("");
  const [aspect, setAspect] = useState("16:9");
  const [generateNow, setGenerateNow] = useState(true);
  const [promptTouched, setPromptTouched] = useState(false);
  const [musicTouched, setMusicTouched] = useState(false);
  const [estimate, setEstimate] = useState<CostEstimate | null>(null);
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);

  useEffect(() => {
    api.ambientPresets().then(setPresets).catch((e) => setError(e.message));
  }, []);

  // Model list follows the source: video models for clips, image models for stills.
  useEffect(() => {
    const load = cfg.source === "image_drift" ? api.imageModels() : api.videoModels();
    load.then((m) => setModels(m.map((x) => ({ id: x.id, name: x.name })))).catch(() => setModels([]));
  }, [cfg.source]);

  // Debounced live cost preview.
  const estTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (estTimer.current) clearTimeout(estTimer.current);
    estTimer.current = setTimeout(() => {
      api.ambientEstimate(cfg).then(setEstimate).catch(() => {});
    }, 250);
    return () => {
      if (estTimer.current) clearTimeout(estTimer.current);
    };
  }, [cfg]);

  const preset = presetFor(presets?.scenes ?? null, cfg.scene_preset);
  const mood = presets?.music_moods.find((m) => m.id === cfg.music_mood);
  const plan = estimate?.plan;
  const set = <K extends keyof AmbientConfig>(k: K, v: AmbientConfig[K]) =>
    setCfg((c) => ({ ...c, [k]: v }));

  const choosePreset = (id: string) => {
    const p = presetFor(presets?.scenes ?? null, id);
    setCfg((c) => ({
      ...c,
      scene_preset: id,
      // Keep a prompt the user has actually typed; otherwise follow the preset.
      scene_prompt: promptTouched ? c.scene_prompt : "",
      music_mood: musicTouched || !p ? c.music_mood : p.music_mood,
    }));
  };

  const effectivePrompt = cfg.scene_prompt || preset?.prompt || "";
  const canSubmit =
    !submitting && effectivePrompt.trim().length > 0 && (cfg.scene_preset !== "custom" || cfg.scene_prompt.trim());

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const scene = await api.createAmbient({
        title,
        aspect_ratio: aspect,
        config: cfg,
        generate: generateNow,
      });
      toast.success(generateNow ? "Scene created — generating the loop" : "Scene created");
      router.push(`/ambient/${scene.id}`);
    } catch (e: any) {
      setError(e.message || "Could not create scene");
      setSubmitting(false);
    }
  };

  return (
    <div className="p-8 max-w-6xl">
      <nav className="text-xs text-ink-400 mb-3" aria-label="Breadcrumb">
        <Link href="/ambient" className="hover:text-ink-100">
          Ambient
        </Link>{" "}
        / New scene
      </nav>
      <header className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight">New ambient scene</h1>
        <p className="text-sm text-ink-300 mt-1">
          Pick a place, a loop length and a mood. Everything past the loop is free.
        </p>
      </header>

      {error && (
        <div role="alert" className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* ---------------- left: builder ---------------- */}
        <div className="lg:col-span-2 space-y-6">
          {/* 1. Scene */}
          <section className="card p-5">
            <StepHeading n={1} title="Scene" hint="Where are we?" />
            {!presets ? (
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-3" aria-hidden>
                {Array.from({ length: 10 }).map((_, i) => (
                  <div key={i} className="aspect-[4/3] skeleton" />
                ))}
              </div>
            ) : (
              <div role="radiogroup" aria-label="Scene preset" className="grid grid-cols-2 sm:grid-cols-5 gap-3">
                {presets.scenes.map((p) => {
                  const active = p.id === cfg.scene_preset;
                  return (
                    <button
                      key={p.id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => choosePreset(p.id)}
                      className={clsx(
                        "rounded-xl overflow-hidden border text-left transition",
                        active
                          ? "border-iris shadow-glow"
                          : "border-ink-800 hover:border-ink-500"
                      )}
                    >
                      <SceneArt palette={p.palette} emoji={p.emoji} still={!active} className="aspect-[4/3]" />
                      <div className="px-2.5 py-2">
                        <div className="text-xs font-medium truncate">{p.label}</div>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}

            <label className="label mt-5" htmlFor="amb-prompt">
              Scene description
            </label>
            <textarea
              id="amb-prompt"
              className="input min-h-[88px]"
              value={cfg.scene_prompt}
              placeholder={preset?.prompt || "Describe the place in a sentence or two…"}
              onChange={(e) => {
                setPromptTouched(true);
                set("scene_prompt", e.target.value);
              }}
            />
            <p className="text-xs text-ink-400 mt-1">
              Leave blank to use the preset text. We add &ldquo;static camera, no cuts, no people,
              seamless loop&rdquo; directives automatically.
            </p>

            <div className="label mt-5">Source</div>
            <div role="radiogroup" aria-label="Scene source" className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <SourceOption
                active={cfg.source === "video"}
                onClick={() => set("source", "video")}
                title="Video clip"
                desc="Real motion (water, flames). Long loops are chained clips; they close on an anchor frame when the model supports it."
                badge="Best look"
              />
              <SourceOption
                active={cfg.source === "image_drift"}
                onClick={() => set("source", "image_drift")}
                title="Still + slow drift"
                desc="One generated image with a gentle, periodic push-in. Perfectly seamless by construction, ~10× cheaper."
                badge="Cheapest"
              />
            </div>
          </section>

          {/* 2. Length */}
          <section className="card p-5">
            <StepHeading n={2} title="Length" hint="One loop, repeated." />
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div>
                <div className="label">Loop clip</div>
                <ChipRow
                  label="Loop clip length"
                  options={presets?.loop_options ?? [25, 30, 60]}
                  value={cfg.loop_seconds}
                  onChange={(v) => set("loop_seconds", v)}
                  render={(v) => `${v}s`}
                />
                {plan && cfg.source === "video" && plan.max_clip_seconds < cfg.loop_seconds ? (
                  <div className="mt-2 space-y-2">
                    <p className="text-xs text-ink-400">
                      This model makes up to {plan.max_clip_seconds}s per clip.
                    </p>
                    <div role="radiogroup" aria-label="When the loop is longer than one clip" className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        role="radio"
                        aria-checked={cfg.chain_segments}
                        onClick={() => set("chain_segments", true)}
                        className={clsx("chip text-xs", cfg.chain_segments && "chip-active")}
                        title="Each clip starts on the previous one's last frame"
                      >
                        Chain {Math.ceil(cfg.loop_seconds / plan.max_clip_seconds)} clips
                      </button>
                      <button
                        type="button"
                        role="radio"
                        aria-checked={!cfg.chain_segments}
                        onClick={() => set("chain_segments", false)}
                        className={clsx("chip text-xs", !cfg.chain_segments && "chip-active")}
                        title="Cheaper; the scene repeats more often"
                      >
                        One {plan.max_clip_seconds}s clip
                      </button>
                    </div>
                  </div>
                ) : (
                  <p className="text-xs text-ink-400 mt-2">
                    {plan && cfg.source === "video"
                      ? `Fits in one clip on this model (max ${plan.max_clip_seconds}s).`
                      : "Longer loops repeat less often."}
                  </p>
                )}
              </div>
              <div>
                <div className="label">Total video</div>
                <ChipRow
                  label="Total video length"
                  options={presets?.total_options ?? [600, 1800, 3600, 7200, 10800]}
                  value={cfg.total_seconds}
                  onChange={(v) => set("total_seconds", v)}
                  render={(v) => fmtDuration(v)}
                />
              </div>
              <div>
                <div className="label">Aspect</div>
                <ChipRow
                  label="Aspect ratio"
                  options={presets?.aspect_options ?? ["16:9", "9:16", "1:1"]}
                  value={aspect}
                  onChange={setAspect}
                  render={(v) => (
                    <span className="inline-flex items-center gap-2">
                      <span
                        aria-hidden
                        className={clsx(
                          "border border-current rounded-[2px]",
                          v === "16:9" ? "w-4 h-2.5" : v === "9:16" ? "w-2.5 h-4" : "w-3 h-3"
                        )}
                      />
                      {v}
                    </span>
                  )}
                />
              </div>
              {cfg.source === "video" && (
                <div>
                  <div className="label">Stillness</div>
                  <div role="group" aria-label="Stillness" className="flex flex-wrap gap-2">
                    {STILLNESS.map((s) => (
                      <button
                        key={s.factor}
                        type="button"
                        aria-pressed={cfg.slow_factor === s.factor}
                        onClick={() => set("slow_factor", s.factor)}
                        className={clsx("chip", cfg.slow_factor === s.factor && "chip-active")}
                        title={s.hint}
                      >
                        {s.label}
                      </button>
                    ))}
                  </div>
                  <p className="text-xs text-ink-400 mt-2">
                    Slows the generated clip down so motion reads as drift, not action.
                  </p>
                </div>
              )}
            </div>
          </section>

          {/* 3. Music */}
          <section className="card p-5">
            <StepHeading n={3} title="Music" hint="Spa, zen, or nothing at all." />
            <ChipRow
              label="Music mood"
              options={(presets?.music_moods ?? []).map((m) => m.id)}
              value={cfg.music_mood}
              onChange={(v) => {
                setMusicTouched(true);
                set("music_mood", v);
              }}
              render={(id) => presets?.music_moods.find((m) => m.id === id)?.label ?? id}
            />
            {cfg.music_mood !== "none" && (
              <>
                <label className="label mt-4" htmlFor="amb-music-prompt">
                  Music prompt <span className="text-ink-400 normal-case tracking-normal">(optional)</span>
                </label>
                <input
                  id="amb-music-prompt"
                  className="input"
                  value={cfg.music_prompt}
                  placeholder={mood?.prompt || ""}
                  onChange={(e) => set("music_prompt", e.target.value)}
                />
                <label className="label mt-4" htmlFor="amb-volume">
                  Volume · {Math.round(cfg.music_volume * 100)}%
                </label>
                <input
                  id="amb-volume"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={cfg.music_volume}
                  onChange={(e) => set("music_volume", Number(e.target.value))}
                  className="w-full accent-[#7C5CFF]"
                />
              </>
            )}
          </section>

          {/* Advanced */}
          <section className="card p-5">
            <button
              type="button"
              className="w-full flex items-center justify-between text-left"
              aria-expanded={showAdvanced}
              onClick={() => setShowAdvanced((v) => !v)}
            >
              <span className="text-sm font-medium">Advanced</span>
              <span className="text-ink-400 text-xs">{showAdvanced ? "Hide" : "Show"}</span>
            </button>
            {showAdvanced && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-5 mt-4">
                <div>
                  <label className="label" htmlFor="amb-crossfade">
                    Loop crossfade · {cfg.crossfade_seconds.toFixed(1)}s
                  </label>
                  <input
                    id="amb-crossfade"
                    type="range"
                    min={0}
                    max={6}
                    step={0.5}
                    value={cfg.crossfade_seconds}
                    disabled={cfg.source === "image_drift"}
                    onChange={(e) => set("crossfade_seconds", Number(e.target.value))}
                    className="w-full accent-[#7C5CFF] disabled:opacity-40"
                  />
                  <p className="text-xs text-ink-400 mt-1">
                    {cfg.source === "image_drift"
                      ? "Not needed — the drift is periodic and already seamless."
                      : "How much of the clip's tail blends into its head to hide the seam."}
                  </p>
                </div>
                <div>
                  <label className="label" htmlFor="amb-fade">
                    Fade in / out · {cfg.fade_seconds.toFixed(0)}s
                  </label>
                  <input
                    id="amb-fade"
                    type="range"
                    min={0}
                    max={15}
                    step={1}
                    value={cfg.fade_seconds}
                    onChange={(e) => set("fade_seconds", Number(e.target.value))}
                    className="w-full accent-[#7C5CFF]"
                  />
                  <p className="text-xs text-ink-400 mt-1">From and to black (and silence) at the very ends.</p>
                </div>
                <div className="md:col-span-2">
                  <label className="label" htmlFor="amb-model">
                    {cfg.source === "image_drift" ? "Image model" : "Video model"}
                  </label>
                  <select
                    id="amb-model"
                    className="input"
                    value={cfg.model_override}
                    onChange={(e) => set("model_override", e.target.value)}
                  >
                    <option value="">Provider default</option>
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            )}
          </section>
        </div>

        {/* ---------------- right: preview + cost ---------------- */}
        <aside className="lg:sticky lg:top-6 self-start space-y-4">
          <div className="card overflow-hidden">
            <SceneArt
              palette={preset?.palette}
              emoji={preset?.emoji}
              className={clsx(aspect === "9:16" ? "aspect-[9/16] max-h-72" : aspect === "1:1" ? "aspect-square" : "aspect-video")}
            >
              <div className="absolute bottom-2 right-2 rounded-md bg-ink-950/70 backdrop-blur px-2 py-0.5 text-xs font-medium text-ink-100">
                {fmtDuration(cfg.total_seconds)}
              </div>
            </SceneArt>
            <div className="p-4 space-y-3">
              <div>
                <label className="label" htmlFor="amb-title">
                  Title
                </label>
                <input
                  id="amb-title"
                  className="input"
                  value={title}
                  placeholder={preset ? `${preset.label} — ${fmtDuration(cfg.total_seconds)}` : "Untitled scene"}
                  onChange={(e) => setTitle(e.target.value)}
                />
              </div>

              <div className="rounded-lg bg-ink-800/60 border border-ink-700 p-3 text-xs">
                <div className="text-ink-300 uppercase tracking-wider text-[10px] mb-2">Loop recipe</div>
                <PlanSummary plan={plan} total={cfg.total_seconds} />
              </div>

              <div>
                <div className="flex items-baseline justify-between">
                  <div className="text-ink-300 uppercase tracking-wider text-[10px]">Estimated cost</div>
                  <div className="text-base font-semibold">
                    {estimate ? `$${estimate.total.toFixed(2)}` : "—"}
                  </div>
                </div>
                {estimate && (
                  <ul className="mt-1.5 space-y-1 text-xs text-ink-300">
                    {estimate.lines.map((l) => (
                      <li key={l.item} className="flex justify-between gap-3">
                        <span className="truncate">{l.item}</span>
                        <span className="tabular-nums shrink-0">
                          {l.cost === 0 ? "free" : `$${l.cost.toFixed(2)}`}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="text-[11px] text-ink-400 mt-2">
                  Same price for 10 minutes or 3 hours — only the loop clips and music hit a provider.
                </p>
              </div>

              <label className="flex items-center gap-2 text-sm text-ink-200">
                <input
                  type="checkbox"
                  checked={generateNow}
                  onChange={(e) => setGenerateNow(e.target.checked)}
                  className="accent-[#7C5CFF]"
                />
                Generate the loop right away
              </label>

              <button
                type="button"
                className="btn-primary w-full disabled:opacity-50"
                disabled={!canSubmit}
                onClick={submit}
              >
                {submitting ? "Creating…" : generateNow ? "Create & generate" : "Create scene"}
              </button>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

function StepHeading({ n, title, hint }: { n: number; title: string; hint: string }) {
  return (
    <div className="flex items-baseline gap-3 mb-4">
      <span className="grad text-ink-950 text-[11px] font-bold rounded-full w-5 h-5 inline-flex items-center justify-center">
        {n}
      </span>
      <h2 className="text-base font-medium">{title}</h2>
      <span className="text-xs text-ink-400">{hint}</span>
    </div>
  );
}

function SourceOption({
  active,
  onClick,
  title,
  desc,
  badge,
}: {
  active: boolean;
  onClick: () => void;
  title: string;
  desc: string;
  badge?: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      className={clsx(
        "rounded-xl border p-3 text-left transition",
        active ? "border-iris bg-iris/10 shadow-glow" : "border-ink-700 hover:border-ink-500"
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{title}</span>
        {badge && (
          <span className="text-[10px] uppercase tracking-wider text-ink-300 border border-ink-600 rounded-full px-1.5 py-0.5">
            {badge}
          </span>
        )}
      </div>
      <div className="text-xs text-ink-300 mt-1">{desc}</div>
    </button>
  );
}
