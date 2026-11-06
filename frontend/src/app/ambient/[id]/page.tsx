"use client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { RenderHistory } from "@/components/RenderHistory";
import { PublishPanel } from "@/components/PublishPanel";
import { useToast } from "@/components/Toaster";
import { ScenePicture } from "@/components/ambient/ScenePicture";
import {
  ChipRow,
  LookControls,
  ModelControls,
  PhotoRewriteControl,
  type ModelOption,
  OverlayControls,
  MUSIC_LENGTHS,
  PlanSummary,
  previewSrc,
  SceneArt,
  fmtDuration,
  presetFor,
  sceneStatusLabel,
  SoundControls,
  VariantStrip,
  VarietyControls,
  statusTone,
} from "@/components/ambient/shared";
import { api, AmbientConfig, AmbientPresets, AmbientScene } from "@/lib/api";

export default function AmbientScenePage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const params = useParams<{ id: string }>();
  const id = Number(params?.id);
  const router = useRouter();
  const toast = useToast();

  const [scene, setScene] = useState<AmbientScene | null>(null);
  const [presets, setPresets] = useState<AmbientPresets | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [cacheBust, setCacheBust] = useState(0);

  const reload = useCallback(async () => {
    try {
      const s = await api.getAmbient(id);
      setScene((prev) => {
        // Bust media caches when the loop/music/render identity changes.
        if (
          prev &&
          (prev.loop_shot?.clip_path !== s.loop_shot?.clip_path ||
            prev.music_ready !== s.music_ready ||
            prev.latest_render?.id !== s.latest_render?.id ||
            prev.status !== s.status)
        ) {
          setCacheBust(Date.now());
        }
        return s;
      });
    } catch (e: any) {
      if (String(e.message || "").startsWith("404")) setNotFound(true);
    }
  }, [id]);

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    reload();
    api.ambientPresets().then(setPresets).catch(() => {});
  }, [id, reload]);

  const inFlight =
    scene?.status === "generating" ||
    scene?.loop_status === "submitted" ||
    scene?.loop_status === "polling";
  useEffect(() => {
    if (!inFlight) return;
    const t = setInterval(reload, 2500);
    return () => clearInterval(t);
  }, [inFlight, reload]);

  const run = async (key: string, fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(key);
    try {
      await fn();
      if (okMsg) toast.success(okMsg);
      // A regenerated bed keeps the same URL and "ready" flag, so reload()
      // sees no change: bump the version or the player keeps the old audio.
      if (key === "music" || key === "sound") setCacheBust(Date.now());
      await reload();
    } catch (e: any) {
      toast.error(e.message || "Request failed");
    } finally {
      setBusy(null);
    }
  };

  if (notFound) {
    return (
      <div className="p-8">
        <div className="card p-8 text-center text-sm">
          Scene not found.{" "}
          <Link href="/ambient" className="text-iris-soft underline-offset-2 hover:underline">
            Back to Ambient
          </Link>
        </div>
      </div>
    );
  }
  if (!scene) {
    return (
      <div className="p-8 max-w-6xl space-y-4" aria-busy="true">
        <div className="h-8 skeleton w-72" />
        <div className="aspect-video skeleton max-w-3xl" />
      </div>
    );
  }

  const preset = presetFor(presets?.scenes ?? null, scene.config.scene_preset);
  const loopReady = scene.loop_status === "completed" && !!scene.loop_shot?.clip_path;
  const loopGenerating = scene.status === "generating" && !loopReady;
  const rendering = scene.status === "generating" && loopReady;
  const render = scene.latest_render;
  const rendered = render?.status === "completed";
  const plan = scene.plan;
  const moodLabel =
    scene.config.music_mood === "none"
      ? "No music"
      : presets?.music_moods.find((m) => m.id === scene.config.music_mood)?.label ??
        scene.config.music_mood;

  return (
    <div className="p-8 max-w-6xl">
      <nav className="text-xs text-ink-400 mb-3" aria-label="Breadcrumb">
        <Link href="/ambient" className="hover:text-ink-100">
          Ambient
        </Link>{" "}
        / {scene.title || "Untitled"}
      </nav>

      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <TitleEditor
            title={scene.title}
            onSave={(t) => run("title", () => api.updateAmbient(id, { title: t }), "Renamed")}
          />
          <div className="flex flex-wrap items-center gap-2 mt-2 text-xs text-ink-300">
            <span
              className={clsx(
                "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5",
                statusTone(scene)
              )}
            >
              {scene.status === "generating" && (
                <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
              )}
              {sceneStatusLabel(scene)}
            </span>
            <span className="chip text-xs py-0.5">
              {preset?.emoji} {preset?.label ?? scene.config.scene_preset}
            </span>
            <span className="chip text-xs py-0.5">{fmtDuration(scene.total_seconds)}</span>
            <span className="chip text-xs py-0.5">{scene.aspect_ratio}</span>
            <span className="chip text-xs py-0.5">
              {scene.config.source === "image_drift" ? "Still + drift" : "Video clip"}
            </span>
            <span className="chip text-xs py-0.5">{moodLabel}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {rendered && render && (
            <Link href={`/projects/${id}/render?id=${render.id}`} className="btn-ghost text-sm">
              Share &amp; versions
            </Link>
          )}
          <button
            type="button"
            className="btn-ghost text-sm text-ink-300 hover:text-accent"
            onClick={async () => {
              if (!window.confirm(`Delete “${scene.title}”? Renders are removed too.`)) return;
              try {
                await api.deleteProject(id);
                toast.success("Scene deleted");
                router.push("/ambient");
              } catch (e: any) {
                toast.error(e.message || "Delete failed");
              }
            }}
          >
            Delete
          </button>
        </div>
      </header>

      {/* Step strip */}
      <ol className="grid grid-cols-3 gap-2 mb-6" aria-label="Progress">
        <Step n={1} label="Loop clip" state={loopReady ? "done" : loopGenerating ? "active" : scene.loop_status === "failed" ? "failed" : "todo"} />
        <Step
          n={2}
          label="Music bed"
          state={scene.config.music_mood === "none" ? "skipped" : scene.music_ready ? "done" : loopGenerating ? "active" : "todo"}
        />
        <Step
          n={3}
          label={`Render ${fmtDuration(scene.total_seconds)}`}
          state={rendered ? "done" : rendering ? "active" : render?.status === "failed" ? "failed" : "todo"}
        />
      </ol>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          {/* ---- Result (when rendered) sits on top: it's what the user came for ---- */}
          {render && (rendered || rendering || render.status === "failed" || render.status === "cancelled") && (
            <section className="card overflow-hidden">
              <div className="p-4 flex items-center justify-between gap-3 border-b border-ink-800">
                <div>
                  <h2 className="text-base font-medium">Full-length render</h2>
                  <div className="text-xs text-ink-400 mt-0.5">
                    {renderSummary(render.render_log, `${fmtDuration(scene.total_seconds)} · ${scene.aspect_ratio}`)}
                  </div>
                </div>
                {rendered && (
                  <a href={api.renderDownload(render.id)} className="btn-primary text-sm" download>
                    Download MP4
                  </a>
                )}
                {rendering && (
                  <button
                    type="button"
                    className="btn-ghost text-sm"
                    disabled={busy !== null}
                    onClick={() => run("cancel", () => api.cancelRender(id), "Cancel signalled")}
                  >
                    Cancel
                  </button>
                )}
              </div>
              {rendered ? (
                <div className="bg-black">
                  <video
                    key={`r-${render.id}-${cacheBust}`}
                    src={api.renderDownload(render.id)}
                    poster={api.renderThumb(render.id)}
                    controls
                    playsInline
                    preload="metadata"
                    className={clsx("mx-auto max-h-[70vh]", scene.aspect_ratio === "9:16" ? "h-[70vh]" : "w-full")}
                  />
                </div>
              ) : rendering ? (
                <div className="p-6">
                  <div className="flex items-center gap-3 text-sm">
                    <span className="w-2 h-2 rounded-full grad animate-pulse" />
                    Tiling {plan.repeats} loops into {fmtDuration(scene.total_seconds)} and mixing the music bed…
                  </div>
                  <div className="h-1.5 rounded-full bg-ink-800 overflow-hidden mt-4">
                    <div className="h-full w-1/2 grad animate-shimmer" style={{ backgroundSize: "200% 100%" }} />
                  </div>
                  <p className="text-xs text-ink-400 mt-3">
                    Stream-copy assembly — this is seconds, not minutes, even for three hours.
                  </p>
                </div>
              ) : (
                <div className="p-6 text-sm">
                  <div className="text-red-300">
                    {render.status === "cancelled" ? "Render cancelled." : `Render failed: ${render.error || "unknown error"}`}
                  </div>
                  <button
                    type="button"
                    className="btn-ghost text-sm mt-3"
                    disabled={!loopReady || busy !== null}
                    onClick={() => run("render", () => api.renderAmbient(id), "Render started")}
                  >
                    Try again
                  </button>
                </div>
              )}
            </section>
          )}

          {/* ---- 0. Picture: the image the loop is built from ---- */}
          <ScenePicture
            scene={scene}
            disabled={inFlight}
            onChange={(s) => {
              setScene(s);
              setCacheBust(Date.now());
            }}
          />

          {/* ---- 1. Loop ---- */}
          <section className="card overflow-hidden">
            <div className="p-4 flex items-center justify-between gap-3 border-b border-ink-800">
              <div>
                <h2 className="text-base font-medium">Loop clip</h2>
                <div className="mt-1.5">
                  <PlanSummary plan={plan} total={scene.total_seconds} compact />
                </div>
                <div className="mt-1.5">
                  <VariantStrip scene={scene} />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="btn-ghost text-sm"
                  disabled={inFlight || busy !== null}
                  onClick={() =>
                    run(
                      "loop",
                      // Regenerating the clip keeps an existing music bed; a
                      // first generation (or a missing bed) produces one too.
                      () => api.generateAmbientLoop(id, !scene.music_ready),
                      loopReady ? "Regenerating the loop clip" : "Generating loop and music"
                    )
                  }
                >
                  {loopReady ? "Regenerate loop" : "Generate loop"}
                </button>
              </div>
            </div>
            {loopReady && scene.loop_shot ? (
              <div className="bg-black">
                <video
                  key={`c-${scene.loop_shot.id}-${cacheBust}`}
                  src={api.shotClip(id, scene.loop_shot.id)}
                  autoPlay
                  loop
                  muted
                  playsInline
                  controls
                  preload="metadata"
                  className={clsx("mx-auto max-h-[60vh]", scene.aspect_ratio === "9:16" ? "h-[60vh]" : "w-full")}
                  aria-label="Loop clip preview (muted, looping)"
                />
              </div>
            ) : loopGenerating ? (
              <div className="relative">
                <SceneArt palette={preset?.palette} emoji={preset?.emoji} image={previewSrc(preset)} className="aspect-video" />
                <div className="absolute inset-x-0 bottom-0 p-4 bg-linear-to-t from-ink-950/90 to-transparent text-sm">
                  <span className="inline-flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full grad animate-pulse" />
                    {scene.loop_status === "polling"
                      ? "Waiting on the video model…"
                      : scene.config.source === "image_drift"
                      ? "Generating the still and drifting it…"
                      : "Generating the loop clip…"}
                  </span>
                </div>
              </div>
            ) : (
              <div className="relative">
                <SceneArt palette={preset?.palette} emoji={preset?.emoji} image={previewSrc(preset)} className="aspect-video" />
                <div className="absolute inset-x-0 bottom-0 p-4 bg-linear-to-t from-ink-950/90 to-transparent text-sm">
                  {scene.loop_status === "failed" ? (
                    <span className="text-red-300">Loop failed: {scene.loop_error || "unknown error"}</span>
                  ) : (
                    <span className="text-ink-200">No loop yet — generate one to preview it here.</span>
                  )}
                </div>
              </div>
            )}
            <div className="px-4 py-3 border-t border-ink-800">
              <PlanSummary plan={plan} total={scene.total_seconds} />
            </div>
            {scene.loop_shot?.prompt && (
              <details className="px-4 py-3 text-xs text-ink-300 border-t border-ink-800">
                <summary className="cursor-pointer text-ink-200">Prompt sent to the model</summary>
                <p className="mt-2 leading-relaxed">{scene.loop_shot.prompt}</p>
                {scene.loop_shot.provider_model && (
                  <p className="mt-1 text-ink-400">model: {scene.loop_shot.provider_model}</p>
                )}
              </details>
            )}
          </section>

          {/* ---- 2. Music ---- */}
          <section className="card p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-base font-medium">Music bed</h2>
                <div className="text-xs text-ink-400 mt-0.5">
                  {scene.config.music_mood === "none"
                    ? "This scene renders silent."
                    : `${moodLabel} · ${Math.round(scene.config.music_volume * 100)}% · crossfaded into a seamless loop at render`}
                </div>
              </div>
              {scene.config.music_mood !== "none" && (
                <button
                  type="button"
                  className="btn-ghost text-sm"
                  disabled={inFlight || busy !== null}
                  onClick={() =>
                    run(
                      "music",
                      () => api.regenerateAmbientMusic(id),
                      "Music regenerated"
                    )
                  }
                >
                  {busy === "music" ? "Generating…" : scene.music_ready ? "Regenerate music" : "Generate music"}
                </button>
              )}
            </div>
            {scene.music_ready && scene.config.music_mood !== "none" && (
              <audio
                key={`m-${cacheBust}`}
                controls
                loop
                preload="none"
                src={`${api.musicUrl(id)}?v=${cacheBust}`}
                className="w-full mt-3"
                aria-label="Music bed preview"
              />
            )}
          </section>

          {/* ---- 2b. Nature sound ---- */}
          {scene.config.sound !== "none" && (
            <section className="card p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h2 className="text-base font-medium">Nature sound</h2>
                  <div className="text-xs text-ink-400 mt-0.5">
                    {(scene.config.sound === "auto"
                      ? "Matches the scene"
                      : presets?.sounds?.find((x) => x.id === scene.config.sound)?.label ?? "Custom")}
                    {` · ${Math.round(scene.config.sound_volume * 100)}% · 30 s seamless loop`}
                    {scene.config.music_mood === "none" ? " · no music" : " · under the music"}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn-ghost text-sm"
                  disabled={inFlight || busy !== null}
                  onClick={() => run("sound", () => api.regenerateAmbientSound(id), "Nature sound regenerated")}
                >
                  {busy === "sound" ? "Generating…" : scene.sound_ready ? "Regenerate sound" : "Generate sound"}
                </button>
              </div>
              {scene.sound_ready && (
                <audio
                  key={`s-${cacheBust}`}
                  controls
                  loop
                  preload="none"
                  src={api.ambientSoundUrl(id, String(cacheBust))}
                  className="w-full mt-3"
                  aria-label="Nature sound preview"
                />
              )}
            </section>
          )}

          {/* ---- 3. Render CTA (when nothing rendered yet) ---- */}
          {!render && (
            <section className="card p-5 flex flex-wrap items-center justify-between gap-4">
              <div>
                <h2 className="text-base font-medium">Render the full video</h2>
                <p className="text-xs text-ink-400 mt-0.5">
                  {loopReady
                    ? `Tiles the loop ${plan.repeats}× into ${fmtDuration(scene.total_seconds)} with fades and music. Nothing is billed.`
                    : "Generate the loop clip first."}
                </p>
              </div>
              <button
                type="button"
                className="btn-primary disabled:opacity-50"
                disabled={!loopReady || inFlight || busy !== null}
                onClick={() => run("render", () => api.renderAmbient(id), "Render started")}
              >
                Render {fmtDuration(scene.total_seconds)}
              </button>
            </section>
          )}
          {render && rendered && (
            <div className="flex justify-end">
              <button
                type="button"
                className="btn-ghost text-sm"
                disabled={!loopReady || inFlight || busy !== null}
                onClick={() => run("render", () => api.renderAmbient(id), "Render started")}
              >
                Re-render {fmtDuration(scene.total_seconds)}
              </button>
            </div>
          )}

          {rendered && render && <PublishPanel projectId={id} renderId={render.id} />}

          <RenderHistory projectId={id} />
        </div>

        {/* ---- Settings ---- */}
        <aside className="space-y-4 lg:sticky lg:top-6 self-start">
          <SettingsPanel
            scene={scene}
            presets={presets}
            disabled={inFlight}
            onSave={(cfg, aspect) =>
              run("settings", () => api.updateAmbient(id, { config: cfg, aspect_ratio: aspect }), "Settings saved")
            }
          />
        </aside>
      </div>
    </div>
  );
}

/** "1 h · 1080p · seamless loop · 3 clips" from the render log's summary line
 *  (`ambient: clips=3 unit=28.50s repeats=127 total=3600s res=1080p seam=closed`). */
function renderSummary(log: string | undefined, fallback: string): string {
  const line = (log || "").split("\n")[0] || "";
  if (!line.startsWith("ambient:")) return fallback;
  const kv = Object.fromEntries(
    line.replace("ambient:", "").trim().split(/\s+/).map((p) => p.split("=") as [string, string])
  );
  const parts = [fallback];
  if (kv.res) parts.push(kv.res);
  if (kv.seam) parts.push(kv.seam === "closed" ? "seamless loop" : "crossfaded loop");
  if (kv.clips && Number(kv.clips) > 1) parts.push(`${kv.clips} clips`);
  return parts.join(" · ");
}

function Step({
  n,
  label,
  state,
}: {
  n: number;
  label: string;
  state: "todo" | "active" | "done" | "failed" | "skipped";
}) {
  return (
    <li
      className={clsx(
        "rounded-lg border px-3 py-2 flex items-center gap-2 text-sm",
        state === "done" && "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
        state === "active" && "border-iris/50 bg-iris/10 text-ink-100 shadow-glow",
        state === "failed" && "border-red-500/40 bg-red-500/10 text-red-200",
        state === "skipped" && "border-ink-800 text-ink-400",
        state === "todo" && "border-ink-800 text-ink-300"
      )}
      aria-current={state === "active" ? "step" : undefined}
    >
      <span
        className={clsx(
          "w-5 h-5 rounded-full inline-flex items-center justify-center text-[11px] font-bold shrink-0",
          state === "done" ? "bg-emerald-400 text-ink-950" : state === "active" ? "grad text-ink-950" : "bg-ink-800 text-ink-300"
        )}
      >
        {state === "done" ? "✓" : state === "skipped" ? "–" : n}
      </span>
      <span className="truncate">{label}</span>
    </li>
  );
}

function TitleEditor({ title, onSave }: { title: string; onSave: (t: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(title);
  useEffect(() => setVal(title), [title]);
  if (!editing) {
    return (
      <h1 className="text-3xl font-semibold tracking-tight flex items-center gap-2 min-w-0">
        <span className="truncate">{title || "Untitled scene"}</span>
        <button
          type="button"
          className="text-ink-400 hover:text-ink-100 text-base"
          aria-label="Rename scene"
          onClick={() => setEditing(true)}
        >
          ✎
        </button>
      </h1>
    );
  }
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        setEditing(false);
        if (val.trim() !== title) onSave(val.trim());
      }}
    >
      <input
        className="input text-xl font-semibold w-80"
        value={val}
        autoFocus
        aria-label="Scene title"
        onChange={(e) => setVal(e.target.value)}
        onBlur={() => {
          setEditing(false);
          if (val.trim() !== title) onSave(val.trim());
        }}
      />
    </form>
  );
}

function SettingsPanel({
  scene,
  presets,
  disabled,
  onSave,
}: {
  scene: AmbientScene;
  presets: AmbientPresets | null;
  disabled: boolean;
  onSave: (cfg: AmbientConfig, aspect: string) => void;
}) {
  const [cfg, setCfg] = useState<AmbientConfig>(scene.config);
  const [aspect, setAspect] = useState(scene.aspect_ratio);
  const [imageModels, setImageModels] = useState<ModelOption[]>([]);
  const [videoModels, setVideoModels] = useState<ModelOption[]>([]);
  useEffect(() => {
    setCfg(scene.config);
    setAspect(scene.aspect_ratio);
  }, [scene.config, scene.aspect_ratio]);
  useEffect(() => {
    const pick = (m: { id: string; name: string }[]) => m.map((x) => ({ id: x.id, name: x.name }));
    api.imageModels().then((m) => setImageModels(pick(m))).catch(() => setImageModels([]));
    api.videoModels().then((m) => setVideoModels(pick(m))).catch(() => setVideoModels([]));
  }, []);

  const set = <K extends keyof AmbientConfig>(k: K, v: AmbientConfig[K]) =>
    setCfg((c) => ({ ...c, [k]: v }));
  const dirty = JSON.stringify(cfg) !== JSON.stringify(scene.config) || aspect !== scene.aspect_ratio;
  // Changes that force a new loop clip (mirrors services.ambient.update_scene).
  const invalidates =
    cfg.scene_prompt !== scene.config.scene_prompt ||
    cfg.scene_preset !== scene.config.scene_preset ||
    // Turning the rewrite off (or on) changes the prompt only when a rewrite applies.
    (cfg.rewrite_prompt !== scene.config.rewrite_prompt && !!scene.config.photo?.scene && cfg.look === "photoreal" &&
      !["upload", "studio", "asset"].includes(cfg.picture_origin)) ||
    cfg.source !== scene.config.source ||
    cfg.loop_seconds !== scene.config.loop_seconds ||
    cfg.model_override !== scene.config.model_override ||
    ((cfg.still_model ?? "") !== (scene.config.still_model ?? "") &&
      (!cfg.picture_origin || (cfg.variety === "day_night" && cfg.variations > 1))) ||
    cfg.chain_segments !== scene.config.chain_segments ||
    cfg.variety !== scene.config.variety ||
    (cfg.variety !== "none" && cfg.variations !== scene.config.variations) ||
    cfg.look !== scene.config.look ||
    cfg.look_strength !== scene.config.look_strength ||
    cfg.time_of_day !== scene.config.time_of_day ||
    cfg.weather !== scene.config.weather;
  const preset = presetFor(presets?.scenes ?? null, cfg.scene_preset);

  return (
    <section className="card p-4 space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-base font-medium">Settings</h2>
        {dirty && (
          <span className="text-[11px] text-ink-400">
            {invalidates ? "needs a new loop" : "re-render to apply"}
          </span>
        )}
      </div>

      <LookControls cfg={cfg} set={set} presets={presets} idPrefix="set-look" />

      <VarietyControls cfg={cfg} set={set} labels={scene.plan.variant_labels} />

      <div>
        <div className="label">Total length</div>
        <ChipRow
          label="Total length"
          options={presets?.total_options ?? [600, 1800, 3600, 7200, 10800]}
          value={cfg.total_seconds}
          onChange={(v) => set("total_seconds", v)}
          render={(v) => fmtDuration(v)}
          className="text-xs"
        />
      </div>

      <div>
        <div className="label">Music</div>
        <ChipRow
          label="Music mood"
          options={(presets?.music_moods ?? []).map((m) => m.id)}
          value={cfg.music_mood}
          onChange={(v) => set("music_mood", v)}
          render={(mid) => presets?.music_moods.find((m) => m.id === mid)?.label ?? mid}
          className="text-xs"
        />
        {cfg.music_mood !== "none" && (
          <>
            <label className="label mt-3" htmlFor="set-volume">
              Volume · {Math.round(cfg.music_volume * 100)}%
            </label>
            <input
              id="set-volume"
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={cfg.music_volume}
              onChange={(e) => set("music_volume", Number(e.target.value))}
              className="w-full accent-iris"
            />
          </>
        )}
      </div>

      <SoundControls cfg={cfg} set={set} presets={presets} idPrefix="set-sound" />

      <OverlayControls cfg={cfg} set={set} idPrefix="set-overlay" />
      {cfg.sound !== scene.config.sound && (
        <p className="text-[11px] text-ink-400 -mt-2">Save, then Generate sound to apply.</p>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="label" htmlFor="set-crossfade">
            Crossfade · {cfg.crossfade_seconds.toFixed(1)}s
          </label>
          <input
            id="set-crossfade"
            type="range"
            min={0}
            max={6}
            step={0.5}
            value={cfg.crossfade_seconds}
            disabled={cfg.source === "image_drift"}
            onChange={(e) => set("crossfade_seconds", Number(e.target.value))}
            className="w-full accent-iris disabled:opacity-40"
          />
          {scene.plan.closed && cfg.source === "video" && (
            <p className="text-[11px] text-ink-400 mt-1">
              Closed loop: only {scene.plan.seam_crossfade}s is used at the seam.
            </p>
          )}
        </div>
        <div>
          <label className="label" htmlFor="set-fade">
            Fade · {cfg.fade_seconds.toFixed(0)}s
          </label>
          <input
            id="set-fade"
            type="range"
            min={0}
            max={15}
            step={1}
            value={cfg.fade_seconds}
            onChange={(e) => set("fade_seconds", Number(e.target.value))}
            className="w-full accent-iris"
          />
        </div>
      </div>

      {cfg.music_mood !== "none" && (
        <div>
          <div className="label">Music length</div>
          <ChipRow
            label="Music bed length"
            options={MUSIC_LENGTHS.map((m) => m.secs)}
            value={cfg.music_bed_seconds}
            onChange={(v) => set("music_bed_seconds", v)}
            render={(v) => MUSIC_LENGTHS.find((m) => m.secs === v)?.label ?? `${v}s`}
            className="text-xs"
          />
          {cfg.music_bed_seconds !== scene.config.music_bed_seconds && (
            <p className="text-[11px] text-ink-400 mt-1">Save, then Regenerate music to apply.</p>
          )}
        </div>
      )}

      <div>
        <div className="label">Output</div>
        <ChipRow
          label="Output resolution"
          options={["1080p", "4k"] as const}
          value={cfg.output_resolution}
          onChange={(v) => set("output_resolution", v)}
          render={(v) => (v === "4k" ? "4K" : "1080p")}
          className="text-xs"
        />
      </div>

      <div>
        <div className="label">Aspect</div>
        <ChipRow
          label="Aspect ratio"
          options={presets?.aspect_options ?? ["16:9", "9:16", "1:1"]}
          value={aspect}
          onChange={setAspect}
          className="text-xs"
        />
      </div>

      <details className="text-xs">
        <summary className="cursor-pointer text-ink-200">Scene &amp; loop (needs a new clip)</summary>
        <div className="mt-3 space-y-3">
          <div>
            <div className="label">Preset</div>
            <select
              className="input"
              aria-label="Scene preset"
              value={cfg.scene_preset}
              onChange={(e) => set("scene_preset", e.target.value)}
            >
              {(presets?.scenes ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.emoji} {p.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="set-prompt">
              Scene description
            </label>
            <textarea
              id="set-prompt"
              className="input min-h-[80px]"
              value={cfg.scene_prompt}
              placeholder={preset?.prompt || ""}
              onChange={(e) => set("scene_prompt", e.target.value)}
            />
            <PhotoRewriteControl cfg={cfg} set={set} idPrefix="set-rewrite" saved={scene.config} />
          </div>
          <ModelControls
            cfg={cfg}
            set={set}
            presets={presets}
            imageModels={imageModels}
            videoModels={videoModels}
            idPrefix="set-models"
          />
          <div>
            <div className="label">Loop clip</div>
            <ChipRow
              label="Loop clip length"
              options={presets?.loop_options ?? [25, 30, 60]}
              value={cfg.loop_seconds}
              onChange={(v) => set("loop_seconds", v)}
              render={(v) => `${v}s`}
              className="text-xs"
            />
          </div>
          <div>
            <div className="label">Source</div>
            <ChipRow
              label="Scene source"
              options={["video", "image_drift"] as const}
              value={cfg.source}
              onChange={(v) => { set("source", v); set("model_override", ""); }}
              render={(v) => (v === "video" ? "Video clip" : "Still + drift")}
              className="text-xs"
            />
          </div>
          {cfg.source === "video" && (
            <div>
              <div className="label">Longer than one clip</div>
              <ChipRow
                label="When the loop is longer than one clip"
                options={["chain", "single"] as const}
                value={cfg.chain_segments ? "chain" : "single"}
                onChange={(v) => set("chain_segments", v === "chain")}
                render={(v) => (v === "chain" ? "Chain clips" : "One clip")}
                className="text-xs"
              />
            </div>
          )}
          {cfg.source === "video" && (
            <div>
              <div className="label">Stillness</div>
              <ChipRow
                label="Stillness"
                options={[1, 1.5, 2]}
                value={cfg.slow_factor}
                onChange={(v) => set("slow_factor", v)}
                render={(v) => (v === 1 ? "Natural" : v === 1.5 ? "Slower" : "Barely moves")}
                className="text-xs"
              />
            </div>
          )}
        </div>
      </details>

      <button
        type="button"
        className="btn-primary w-full disabled:opacity-50"
        disabled={!dirty || disabled}
        onClick={() => onSave(cfg, aspect)}
      >
        Save settings
      </button>
    </section>
  );
}
