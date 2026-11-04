"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { useToast } from "@/components/Toaster";
import {
  SceneArt,
  fmtDuration,
  ChipRow,
  presetFor,
  previewSrc,
  sceneStatusLabel,
  statusTone,
} from "@/components/ambient/shared";
import {
  api,
  AmbientPresets,
  AmbientScene,
  DEFAULT_AMBIENT_CONFIG,
  ScenePreset,
} from "@/lib/api";

export default function AmbientPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

const QUICK_START = ["waterfall", "fireplace", "ocean"];

function Inner() {
  const [scenes, setScenes] = useState<AmbientScene[]>([]);
  const [presets, setPresets] = useState<AmbientPresets | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busyPreset, setBusyPreset] = useState<string | null>(null);
  const [showBatch, setShowBatch] = useState(false);
  const toast = useToast();
  const router = useRouter();

  const reload = () =>
    api.listAmbient().then((rows) => {
      setScenes(rows);
      setLoaded(true);
    });

  useEffect(() => {
    reload();
    api.ambientPresets().then(setPresets).catch(() => {});
  }, []);

  // Keep statuses live while anything is generating/rendering.
  useEffect(() => {
    if (!scenes.some((s) => s.status === "generating")) return;
    const t = setInterval(reload, 3000);
    return () => clearInterval(t);
  }, [scenes]);

  const quickStart = async (preset: ScenePreset) => {
    setBusyPreset(preset.id);
    try {
      const scene = await api.createAmbient({
        aspect_ratio: "16:9",
        config: {
          ...DEFAULT_AMBIENT_CONFIG,
          scene_preset: preset.id,
          music_mood: preset.music_mood,
        },
        generate: true,
      });
      router.push(`/ambient/${scene.id}`);
    } catch (e: any) {
      toast.error(e.message || "Could not create scene");
      setBusyPreset(null);
    }
  };

  const remove = async (s: AmbientScene) => {
    if (!window.confirm(`Delete “${s.title}”? Renders are removed too.`)) return;
    try {
      await api.deleteProject(s.id);
      toast.success(`Deleted “${s.title}”`);
      reload();
    } catch (e: any) {
      toast.error(e.message || "Delete failed");
    }
  };

  return (
    <div className="p-8 max-w-6xl">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Ambient <span className="text-grad">scenes</span>
          </h1>
          <p className="text-sm text-ink-300 mt-1 max-w-xl">
            Hour-long calm, landscape and spa videos. One short scene that barely moves,
            looped seamlessly under soft music — you pay for the loop, not the hour.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className="btn-ghost" aria-expanded={showBatch} onClick={() => setShowBatch((v) => !v)}>
            Batch create
          </button>
          <Link href="/ambient/new" className="btn-primary">
            + New scene
          </Link>
        </div>
      </header>

      {showBatch && presets && (
        <BatchPanel
          presets={presets}
          onDone={(n) => {
            setShowBatch(false);
            toast.success(`Created ${n} scenes — generating and rendering one after another`);
            reload();
          }}
        />
      )}

      {!loaded ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" aria-hidden>
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="card overflow-hidden">
              <div className="aspect-video skeleton rounded-none" />
              <div className="p-4 space-y-2">
                <div className="h-4 skeleton w-2/3" />
                <div className="h-3 skeleton w-1/2" />
              </div>
            </div>
          ))}
        </div>
      ) : scenes.length === 0 ? (
        <EmptyState presets={presets} busy={busyPreset} onQuickStart={quickStart} />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {scenes.map((s, i) => {
            const preset = presetFor(presets?.scenes ?? null, s.config.scene_preset);
            const rendered = s.latest_render?.status === "completed";
            return (
              <div
                key={s.id}
                className="card overflow-hidden card-hover group relative animate-fade-up"
                style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
              >
                <Link href={`/ambient/${s.id}`} className="block">
                  <div className="relative">
                    {rendered && s.latest_render ? (
                      <img
                        src={api.renderThumb(s.latest_render.id)}
                        alt=""
                        className="block w-full aspect-video object-cover bg-black"
                      />
                    ) : (
                      <SceneArt
                        palette={preset?.palette}
                        emoji={preset?.emoji}
                        image={previewSrc(preset)}
                        still={i > 5}
                        className="aspect-video"
                      />
                    )}
                    <span
                      className={clsx(
                        "absolute top-2 left-2 inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] backdrop-blur-sm",
                        statusTone(s)
                      )}
                    >
                      {s.status === "generating" && (
                        <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
                      )}
                      {sceneStatusLabel(s)}
                    </span>
                    <span className="absolute bottom-2 right-2 rounded-md bg-ink-950/70 backdrop-blur-sm px-2 py-0.5 text-xs font-medium text-ink-100">
                      {fmtDuration(s.total_seconds)}
                    </span>
                  </div>
                  <div className="p-4">
                    <div className="text-sm font-medium pr-6 truncate">{s.title || "Untitled"}</div>
                    <div className="text-xs text-ink-300 mt-1">
                      {s.config.loop_seconds}s loop × {s.repeats} · {s.aspect_ratio} ·{" "}
                      {s.config.music_mood === "none" ? "silent" : s.config.music_mood.replace("_", " ")}
                    </div>
                  </div>
                </Link>
                <button
                  className="absolute top-2 right-2 text-ink-100/80 hover:text-accent text-xs bg-ink-950/60 backdrop-blur-sm rounded-full w-6 h-6 flex items-center justify-center opacity-70 md:opacity-0 md:group-hover:opacity-100 focus:opacity-100 transition"
                  title="Delete scene"
                  aria-label={`Delete scene "${s.title || "Untitled"}"`}
                  onClick={() => remove(s)}
                >
                  ✕
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function EmptyState({
  presets,
  busy,
  onQuickStart,
}: {
  presets: AmbientPresets | null;
  busy: string | null;
  onQuickStart: (p: ScenePreset) => void;
}) {
  const quick = (presets?.scenes ?? []).filter((p) => QUICK_START.includes(p.id));
  return (
    <div className="card p-8">
      <div className="max-w-xl">
        <div className="text-lg font-medium">Start with a one-hour classic</div>
        <p className="text-sm text-ink-300 mt-1">
          Each of these creates a scene and immediately generates its 30-second loop and a
          matching music bed. Rendering the full hour takes seconds — it&apos;s all local.
        </p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-6">
        {quick.map((p) => (
          <button
            key={p.id}
            type="button"
            disabled={busy !== null}
            onClick={() => onQuickStart(p)}
            className="card overflow-hidden card-hover text-left disabled:opacity-60"
          >
            <SceneArt palette={p.palette} emoji={p.emoji} image={previewSrc(p)} className="aspect-video" />
            <div className="p-4">
              <div className="text-sm font-medium">
                {busy === p.id ? "Creating…" : `${p.label} — 1h`}
              </div>
              <div className="text-xs text-ink-300 mt-0.5">{p.tagline}</div>
            </div>
          </button>
        ))}
      </div>
      <div className="mt-6 text-sm text-ink-300">
        Want a different place, length or music?{" "}
        <Link href="/ambient/new" className="text-iris-soft hover:text-white underline-offset-2 hover:underline">
          Open the scene builder →
        </Link>
      </div>
    </div>
  );
}

function BatchPanel({ presets, onDone }: { presets: AmbientPresets; onDone: (n: number) => void }) {
  const toast = useToast();
  const choices = presets.scenes.filter((p) => p.id !== "custom");
  const [picked, setPicked] = useState<string[]>([]);
  const [total, setTotal] = useState(3600);
  const [render, setRender] = useState(true);
  const [res, setRes] = useState<"1080p" | "4k">("1080p");
  const [busy, setBusy] = useState(false);
  const [estimate, setEstimate] = useState<number | null>(null);

  useEffect(() => {
    if (!picked.length) return setEstimate(null);
    // Same settings per scene → one estimate × count.
    api.ambientEstimate({ ...DEFAULT_AMBIENT_CONFIG, total_seconds: total, output_resolution: res })
      .then((e) => setEstimate(e.total * picked.length)).catch(() => setEstimate(null));
  }, [picked, total, res]);

  const toggle = (id: string) =>
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  const submit = async () => {
    setBusy(true);
    try {
      const r = await api.ambientBatch({
        presets: picked,
        aspect_ratio: "16:9",
        config: { ...DEFAULT_AMBIENT_CONFIG, total_seconds: total, output_resolution: res },
        render,
      });
      onDone(r.scene_ids.length);
    } catch (e: any) {
      toast.error(e.message || "Batch failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card p-5 mb-6" aria-labelledby="batch-h">
      <h2 id="batch-h" className="text-base font-medium">Batch create</h2>
      <p className="text-xs text-ink-400 mt-0.5">
        One scene per preset with the same length and settings. They generate one at a time so the
        provider isn&apos;t flooded.
      </p>
      <div role="group" aria-label="Presets to create" className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2 mt-4">
        {choices.map((p) => (
          <button key={p.id} type="button" aria-pressed={picked.includes(p.id)} onClick={() => toggle(p.id)}
                  className={clsx("rounded-xl overflow-hidden border text-left transition",
                    picked.includes(p.id) ? "border-iris shadow-glow" : "border-ink-800 hover:border-ink-500")}>
            <SceneArt palette={p.palette} emoji={p.emoji} image={previewSrc(p)} still className="aspect-4/3" />
            <div className="px-2.5 py-1.5 text-xs font-medium flex items-center justify-between">
              {p.label}
              {picked.includes(p.id) && <span className="text-iris-soft" aria-hidden>✓</span>}
            </div>
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-6 mt-4">
        <div>
          <div className="label">Length</div>
          <ChipRow label="Batch length" options={presets.total_options} value={total} onChange={setTotal}
                   render={(v) => fmtDuration(v)} className="text-xs" />
        </div>
        <div>
          <div className="label">Output</div>
          <ChipRow label="Batch output" options={["1080p", "4k"] as const} value={res} onChange={setRes}
                   render={(v) => (v === "4k" ? "4K" : "1080p")} className="text-xs" />
        </div>
        <label className="flex items-center gap-2 text-sm text-ink-200">
          <input type="checkbox" className="accent-iris" checked={render} onChange={(e) => setRender(e.target.checked)} />
          Render each when its loop is ready
        </label>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 mt-4">
        <div className="text-sm text-ink-300">
          {picked.length ? `${picked.length} scene${picked.length === 1 ? "" : "s"}` : "Pick presets"}
          {estimate !== null && <> · est. <span className="text-ink-100 font-medium">${estimate.toFixed(2)}</span></>}
        </div>
        <button type="button" className="btn-primary disabled:opacity-50" disabled={!picked.length || busy} onClick={submit}>
          {busy ? "Creating…" : `Create ${picked.length || ""} scene${picked.length === 1 ? "" : "s"}`}
        </button>
      </div>
    </section>
  );
}
