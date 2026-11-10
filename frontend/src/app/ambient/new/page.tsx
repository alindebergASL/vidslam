"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { useToast } from "@/components/Toaster";
import { IMAGE_ACCEPT, isHeicFile } from "@/components/Dropzone";
import { EnhanceButton } from "@/components/EnhanceButton";
import { PickedImage, PicturePicker, pictureUse } from "@/components/ambient/ScenePicture";
import {
  ChipRow,
  LookControls,
  MUSIC_LENGTHS,
  ModelControls,
  PhotoRewriteControl,
  type ModelOption,
  OverlayControls,
  PlanSummary,
  SceneArt,
  SoundControls,
  VarietyControls,
  fmtDuration,
  presetFor,
  previewSrc,
} from "@/components/ambient/shared";
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
  // What happens after Create. "picture": make only the picture, so it can be
  // checked before the video is bought (video scenes without their own picture).
  const [after, setAfter] = useState<"picture" | "generate" | "save">("picture");
  const [phase, setPhase] = useState<"" | "creating" | "picture">("");
  const [promptTouched, setPromptTouched] = useState(false);
  const [musicTouched, setMusicTouched] = useState(false);
  const [estimate, setEstimate] = useState<CostEstimate | null>(null);
  const [imageModels, setImageModels] = useState<ModelOption[]>([]);
  const [videoModels, setVideoModels] = useState<ModelOption[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [genPreviews, setGenPreviews] = useState(false);
  const [previewProgress, setPreviewProgress] = useState<{ done: number; total: number } | null>(null);
  // Optional picture to build the scene from: a file to upload, or a
  // Studio/Cast image. Attached right after the scene is created.
  const [picture, setPicture] = useState<{ file?: File; picked?: PickedImage; url: string } | null>(null);
  const [picking, setPicking] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // Studio → "Ambient scene" lands here with ?studio_job=<id>.
  useEffect(() => {
    const id = Number(new URLSearchParams(window.location.search).get("studio_job"));
    if (Number.isFinite(id) && id > 0) {
      setPicture({ picked: { kind: "studio", id, url: api.studioPreview(id), label: "From Studio" },
                   url: api.studioPreview(id) });
    }
  }, []);
  useEffect(() => () => {
    if (picture?.file && picture.url) URL.revokeObjectURL(picture.url);
  }, [picture]);

  useEffect(() => {
    api.ambientPresets().then(setPresets).catch((e) => setError(e.message));
  }, []);

  // Every image and video model, after the suggested ones (see ModelControls).
  useEffect(() => {
    const pick = (m: { id: string; name: string }[]) => m.map((x) => ({ id: x.id, name: x.name }));
    api.imageModels().then((m) => setImageModels(pick(m))).catch(() => setImageModels([]));
    api.videoModels().then((m) => setVideoModels(pick(m))).catch(() => setVideoModels([]));
  }, []);

  // Debounced live cost preview.
  const estTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (estTimer.current) clearTimeout(estTimer.current);
    estTimer.current = setTimeout(() => {
      api
        .ambientEstimate({ ...cfg, picture_origin: picture ? (picture.file ? "upload" : picture.picked!.kind) : "" })
        .then(setEstimate)
        .catch(() => {});
    }, 250);
    return () => {
      if (estTimer.current) clearTimeout(estTimer.current);
    };
  }, [cfg, picture]);

  const preset = presetFor(presets?.scenes ?? null, cfg.scene_preset);
  const mood = presets?.music_moods.find((m) => m.id === cfg.music_mood);
  const plan = estimate?.plan;
  const set = <K extends keyof AmbientConfig>(k: K, v: AmbientConfig[K]) =>
    setCfg((c) => ({ ...c, [k]: v }));
  // A video model isn't a still model: clear it only on a real switch, so
  // re-clicking the current source keeps the chosen model.
  const pickSource = (source: AmbientSource) =>
    setCfg((c) => (c.source === source ? c : { ...c, source, model_override: "" }));

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
  // With a picture, the picture is the scene: no description needed.
  const canSubmit =
    !submitting &&
    (!!picture || (effectivePrompt.trim().length > 0 && (cfg.scene_preset !== "custom" || cfg.scene_prompt.trim())));

  // Picture first only when the video is built from a generated picture: with
  // Still + drift the still is the whole cost, your own picture is already
  // seen, and some video models work from text alone.
  const pictureFirstOk = cfg.source === "video" && !picture && plan?.picture_usable !== false;
  const mode = after === "picture" && !pictureFirstOk ? "generate" : after;
  const generateNow = mode === "generate";

  const submit = async () => {
    setSubmitting(true);
    setPhase("creating");
    setError(null);
    try {
      // With your own picture the text only describes motion: don't rewrite it as a place.
      const config = picture
        ? {
            ...cfg,
            rewrite_prompt: false,
            scene_prompt:
              cfg.scene_preset === "custom" && !cfg.scene_prompt.trim() ? "the scene in the given image" : cfg.scene_prompt,
          }
        : cfg;
      // With a picture: create without generating, attach it, then generate,
      // so the loop is built from the picture rather than a generated still.
      const scene = await api.createAmbient({
        title,
        aspect_ratio: aspect,
        config,
        generate: generateNow && !picture,
      });
      if (picture) {
        try {
          if (picture.file) await api.uploadAmbientPicture(scene.id, picture.file);
          else
            await api.ambientPictureFrom(
              scene.id,
              picture.picked!.kind === "studio" ? { studio_job_id: picture.picked!.id } : { asset_id: picture.picked!.id }
            );
        } catch (e: any) {
          toast.error(`Scene created, but the picture failed: ${e.message || e}`);
          router.push(`/ambient/${scene.id}`);
          return;
        }
        if (generateNow) await api.generateAmbientLoop(scene.id);
      }
      if (mode === "picture") {
        // One image call. The loop is made from the scene page, after a look.
        setPhase("picture");
        try {
          await api.generateAmbientPicture(scene.id);
          toast.success("Picture ready. Check it, then make the loop.");
        } catch (e: any) {
          toast.error(`Scene created, but the picture failed: ${e.message || e}. Try again on the scene page.`);
        }
        router.push(`/ambient/${scene.id}`);
        return;
      }
      toast.success(generateNow ? "Scene created — generating the loop" : "Scene created");
      router.push(`/ambient/${scene.id}`);
    } catch (e: any) {
      setError(e.message || "Could not create scene");
      setSubmitting(false);
      setPhase("");
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
            <div className="flex items-start justify-between gap-3">
              <StepHeading n={1} title="Scene" hint="Where are we?" />
              {presets && presets.scenes.some((p) => p.prompt && !p.preview_url) && (
                <button
                  type="button"
                  className="btn-ghost text-xs shrink-0"
                  disabled={genPreviews}
                  onClick={async () => {
                    setGenPreviews(true);
                    try {
                      const r = await api.generatePresetPreviews();
                      const want = new Set(r.presets);
                      // Inline in dev; with a worker it runs in the background, one
                      // image at a time (about 2 minutes each on the best model), so
                      // keep checking until every requested preview is there.
                      let done = 0;
                      setPreviewProgress({ done: 0, total: want.size });
                      for (let i = 0; i < 360 && want.size > 0; i++) {
                        const p = await api.ambientPresets();
                        setPresets(p);
                        done = p.scenes.filter((x) => want.has(x.id) && x.preview_url).length;
                        setPreviewProgress({ done, total: want.size });
                        if (done === want.size) break;
                        await new Promise((res) => setTimeout(res, 5000));
                      }
                      if (done === want.size) {
                        toast.success(`Generated ${done} preview image${done === 1 ? "" : "s"}`);
                      } else {
                        toast.success(`${done} of ${want.size} previews ready; the rest are still being made`);
                      }
                    } catch (e: any) {
                      toast.error(e.message || "Preview generation failed");
                    } finally {
                      setGenPreviews(false);
                      setPreviewProgress(null);
                    }
                  }}
                  title="One image-model call per preset; cached afterwards"
                >
                  {genPreviews
                    ? previewProgress
                      ? `Generating… ${previewProgress.done} of ${previewProgress.total}`
                      : "Generating…"
                    : "Generate preview images"}
                </button>
              )}
            </div>
            {!presets ? (
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-3" aria-hidden>
                {Array.from({ length: 10 }).map((_, i) => (
                  <div key={i} className="aspect-4/3 skeleton" />
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
                      <SceneArt palette={p.palette} emoji={p.emoji} image={previewSrc(p)} still={!active} className="aspect-4/3" />
                      <div className="px-2.5 py-2">
                        <div className="text-xs font-medium truncate">{p.label}</div>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}

            <label className="label mt-5" htmlFor="amb-prompt">
              {picture ? "Describe the motion (optional)" : "Scene description"}
            </label>
            <textarea
              id="amb-prompt"
              className="input min-h-[88px]"
              value={cfg.scene_prompt}
              placeholder={
                picture
                  ? "e.g. gentle ripples on the lake, clouds drifting slowly"
                  : preset?.prompt || "Describe the place in a sentence or two…"
              }
              onChange={(e) => {
                setPromptTouched(true);
                set("scene_prompt", e.target.value);
              }}
            />
            <p className="text-xs text-ink-400 mt-1">
              {picture
                ? "With a picture, this only guides what moves; the picture sets the scene and its style."
                : "Leave blank to use the preset text."}{" "}
              We add &ldquo;static camera, no cuts, no people, seamless loop&rdquo; directives automatically.
            </p>
            <EnhanceButton
              className="mt-2"
              text={cfg.scene_prompt}
              kind="scene"
              model={cfg.still_model || null}
              onUse={(t) => {
                setPromptTouched(true);
                set("scene_prompt", t);
              }}
            />
            <PhotoRewriteControl
              cfg={{ ...cfg, picture_origin: picture ? (picture.file ? "upload" : picture.picked!.kind) : "" }}
              set={set}
              idPrefix="new-rewrite"
            />

            <div className="label mt-5">Start from a picture (optional)</div>
            <div className="flex flex-col sm:flex-row gap-3 sm:items-center">
              {picture &&
                (picture.file && isHeicFile(picture.file) ? (
                  <div
                    className="w-full sm:w-40 aspect-video rounded-md border border-ink-700 bg-ink-800 flex flex-col items-center justify-center p-2 text-center"
                    title={picture.file.name}
                  >
                    <span className="text-xs text-ink-100 truncate max-w-full">{picture.file.name}</span>
                    <span className="text-[11px] text-ink-400 mt-1">Preview after upload (HEIC)</span>
                  </div>
                ) : (
                  <img src={picture.url} alt="Chosen picture" className="w-full sm:w-40 aspect-video object-cover rounded-md border border-ink-700" />
                ))}
              <div className="min-w-0">
                <div className="flex flex-wrap gap-2">
                  <button type="button" className="btn-ghost text-xs" onClick={() => fileRef.current?.click()}>
                    {picture ? "Upload another" : "Upload"}
                  </button>
                  <button type="button" className="btn-ghost text-xs" onClick={() => setPicking(true)}>
                    Choose from Studio / Cast
                  </button>
                  {picture && (
                    <button type="button" className="text-xs text-ink-400 hover:text-ink-100 px-2" onClick={() => setPicture(null)}>
                      Remove
                    </button>
                  )}
                </div>
                <p className="text-xs text-ink-400 mt-1">
                  {picture
                    ? pictureUse(cfg.source)
                    : "Use your own photo or artwork instead of a generated image."}
                </p>
              </div>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept={IMAGE_ACCEPT}
              className="hidden"
              aria-label="Upload a scene picture"
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.target.value = "";
                // Most browsers cannot decode HEIC/HEIF, so skip the object URL and show a placeholder tile.
                if (f) setPicture({ file: f, url: isHeicFile(f) ? "" : URL.createObjectURL(f) });
              }}
            />
            {picking && (
              <PicturePicker
                onClose={() => setPicking(false)}
                onPick={(p) => {
                  setPicking(false);
                  setPicture({ picked: p, url: p.url });
                }}
              />
            )}

            <div className="label mt-5">Source</div>
            <div role="radiogroup" aria-label="Scene source" className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <SourceOption
                active={cfg.source === "video"}
                onClick={() => pickSource("video")}
                title="Video clip"
                desc="Real motion (water, flames). Long loops are chained clips; they close on an anchor frame when the model supports it."
                badge="Best look"
              />
              <SourceOption
                active={cfg.source === "image_drift"}
                onClick={() => pickSource("image_drift")}
                title="Still + slow drift"
                desc="One generated image with a gentle, periodic push-in. Perfectly seamless by construction, ~10× cheaper."
                badge="Cheapest"
              />
            </div>
          </section>

          {/* 2. Look & light */}
          <section className="card p-5">
            <StepHeading n={2} title="Look & light" hint="Realistic or artsy, any time of day, and the models that make it." />
            <LookControls cfg={cfg} set={set} presets={presets} idPrefix="new-look" />
            <div className="mt-5 pt-4 border-t border-ink-800">
              <ModelControls
                cfg={{ ...cfg, picture_origin: picture ? (picture.file ? "upload" : picture.picked!.kind) : "" }}
                set={set}
                presets={presets}
                imageModels={imageModels}
                videoModels={videoModels}
                idPrefix="new-models"
              />
            </div>
          </section>

          {/* 3. Length */}
          <section className="card p-5">
            <StepHeading n={3} title="Length" hint="One loop, repeated — or a few, for less repetition." />
            <div className="mb-5">
              <VarietyControls cfg={cfg} set={set} labels={plan?.variant_labels} />
            </div>
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
              <div>
                <div className="label">Output</div>
                <ChipRow
                  label="Output resolution"
                  options={["1080p", "4k"] as const}
                  value={cfg.output_resolution}
                  onChange={(v) => set("output_resolution", v)}
                  render={(v) => (v === "4k" ? "4K (upscaled)" : "1080p")}
                />
                <p className="text-xs text-ink-400 mt-2">
                  4K is a free FFmpeg upscale; YouTube streams 4K uploads at a higher bitrate.
                </p>
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

          {/* 4. Music & sound */}
          <section className="card p-5">
            <StepHeading n={4} title="Music & sound" hint="Spa music, nature sounds, both, or silence." />
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
                  className="w-full accent-iris"
                />
                <div className="label mt-4">Music length</div>
                <ChipRow
                  label="Music bed length"
                  options={MUSIC_LENGTHS.map((m) => m.secs)}
                  value={cfg.music_bed_seconds}
                  onChange={(v) => set("music_bed_seconds", v)}
                  render={(v) => MUSIC_LENGTHS.find((m) => m.secs === v)?.label ?? `${v}s`}
                />
                <p className="text-xs text-ink-400 mt-2">
                  Longer beds repeat less over an hour. Beds longer than one music call are made from
                  several tracks crossfaded together.
                </p>
              </>
            )}
            <div className="mt-5">
              <SoundControls cfg={cfg} set={set} presets={presets} idPrefix="new-sound" />
            </div>
          </section>

          {/* 5. Overlays */}
          <section className="card p-5">
            <StepHeading n={5} title="Overlays" hint="Optional title, watermark or timer." />
            <OverlayControls cfg={cfg} set={set} idPrefix="new-overlay" />
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
                    className="w-full accent-iris disabled:opacity-40"
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
                    className="w-full accent-iris"
                  />
                  <p className="text-xs text-ink-400 mt-1">From and to black (and silence) at the very ends.</p>
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
              image={previewSrc(preset)}
              className={clsx(aspect === "9:16" ? "aspect-9/16 max-h-72" : aspect === "1:1" ? "aspect-square" : "aspect-video")}
            >
              <div className="absolute bottom-2 right-2 rounded-md bg-ink-950/70 backdrop-blur-sm px-2 py-0.5 text-xs font-medium text-ink-100">
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

              <fieldset className="space-y-2">
                <legend className="text-ink-300 uppercase tracking-wider text-[10px] mb-2">After creating</legend>
                {pictureFirstOk && (
                  <AfterOption
                    checked={mode === "picture"}
                    onChange={() => setAfter("picture")}
                    title="Make the picture first"
                    cost={estimate ? money(estimate.picture_cost) : ""}
                    desc={`The video is built from this picture. You check it, then make the loop${
                      estimate?.after_picture_total != null ? ` (${money(estimate.after_picture_total)})` : ""
                    }. Nothing else is charged until then.`}
                  />
                )}
                <AfterOption
                  checked={mode === "generate"}
                  onChange={() => setAfter("generate")}
                  title={pictureFirstOk ? "Skip the check and generate now" : "Generate now"}
                  cost={estimate ? money(estimate.total) : ""}
                  desc={
                    pictureFirstOk
                      ? "Makes the picture, the loop and the music in one go. You pay for the video before seeing the picture."
                      : cfg.source === "video" && !picture
                      ? "This video model works from text alone, so there is no picture to check first."
                      : "Makes the loop and the music straight away."
                  }
                />
                <AfterOption
                  checked={mode === "save"}
                  onChange={() => setAfter("save")}
                  title="Just save the scene"
                  cost="free"
                  desc="Nothing is generated or charged yet."
                />
              </fieldset>

              <button
                type="button"
                className="btn-primary w-full disabled:opacity-50"
                disabled={!canSubmit}
                onClick={submit}
              >
                {phase === "picture"
                  ? "Making the picture…"
                  : submitting
                  ? "Creating…"
                  : mode === "picture"
                  ? "Create and make the picture"
                  : generateNow
                  ? "Create and generate"
                  : "Create scene"}
              </button>
              {phase === "picture" && (
                <p className="text-[11px] text-ink-400" role="status">
                  This can take a couple of minutes. The scene page opens when the picture is ready.
                </p>
              )}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

/** A rate-card estimate: "about $4.50", or "free". */
const money = (n: number | undefined) => (n == null ? "" : n < 0.005 ? "free" : `about $${n.toFixed(2)}`);

function AfterOption({
  checked,
  onChange,
  title,
  cost,
  desc,
}: {
  checked: boolean;
  onChange: () => void;
  title: string;
  cost: string;
  desc: string;
}) {
  return (
    <label
      className={clsx(
        "flex items-start gap-2 rounded-lg border p-2.5 cursor-pointer",
        checked ? "border-iris bg-iris/10" : "border-ink-700 hover:border-ink-500"
      )}
    >
      <input type="radio" name="amb-after" checked={checked} onChange={onChange} className="accent-iris mt-0.5" />
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline justify-between gap-2 text-sm text-ink-100">
          <span>{title}</span>
          <span className="tabular-nums text-xs text-ink-300 shrink-0">{cost}</span>
        </span>
        <span className="block text-xs text-ink-400 mt-0.5">{desc}</span>
      </span>
    </label>
  );
}

function StepHeading({ n, title, hint }: { n: number; title: string; hint: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mb-4">
      <span className="grad text-ink-950 text-[11px] font-bold rounded-full w-5 h-5 inline-flex items-center justify-center">
        {n}
      </span>
      <h2 className="text-base font-medium whitespace-nowrap">{title}</h2>
      <span className="text-xs text-ink-400 basis-full sm:basis-auto">{hint}</span>
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
