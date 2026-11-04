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
  presetFor,
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
        <Link href="/ambient/new" className="btn-primary">
          + New scene
        </Link>
      </header>

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
                        still={i > 5}
                        className="aspect-video"
                      />
                    )}
                    <span
                      className={clsx(
                        "absolute top-2 left-2 inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] backdrop-blur",
                        statusTone(s)
                      )}
                    >
                      {s.status === "generating" && (
                        <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
                      )}
                      {sceneStatusLabel(s)}
                    </span>
                    <span className="absolute bottom-2 right-2 rounded-md bg-ink-950/70 backdrop-blur px-2 py-0.5 text-xs font-medium text-ink-100">
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
                  className="absolute top-2 right-2 text-ink-100/80 hover:text-accent text-xs bg-ink-950/60 backdrop-blur rounded-full w-6 h-6 flex items-center justify-center opacity-70 md:opacity-0 md:group-hover:opacity-100 focus:opacity-100 transition"
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
            <SceneArt palette={p.palette} emoji={p.emoji} className="aspect-video" />
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
