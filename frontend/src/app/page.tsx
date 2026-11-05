"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { OnboardingChecklist } from "@/components/OnboardingChecklist";
import { StatusBadge } from "@/components/StatusBadge";
import { useToast } from "@/components/Toaster";
import { api, Avatar, Ingredient, Project } from "@/lib/api";
import { formatDuration, modeLabel } from "@/lib/labels";

const PROVIDER_NAMES: Record<string, string> = {
  openrouter: "OpenRouter", elevenlabs: "ElevenLabs", gemini: "Google", replicate: "Replicate",
};
// Every kind that can call a paid provider, or "nothing is billed" could lie
// (e.g. a live lip-sync model while the video model is still a mock).
const KIND_NAMES: Record<string, string> = {
  chat: "planner", image: "images", video: "video", tts: "voice", music: "music",
  lipsync: "lip-sync", training_lora: "LoRA training", training_voice: "voice cloning",
};

/** One sentence for the provider status instead of raw flags. */
function providerSummary(st: Record<string, any>): { live: boolean; text: string } {
  const kinds = Object.keys(KIND_NAMES).filter((k) => typeof st[k] === "string");
  const real = kinds.filter((k) => st[k] !== "mock" && st[k] !== "off");
  if (st.mock_providers_env === true || real.length === 0) {
    return { live: false, text: "Demo mode: videos use placeholder clips and nothing is billed." };
  }
  const names = [...new Set(real.map((k) => PROVIDER_NAMES[st[k]] || st[k]))].join(" + ");
  // Only the everyday kinds are worth calling out as placeholders; optional
  // extras (lip-sync off, no training key) would just add noise.
  const mocked = ["chat", "image", "video", "tts", "music"]
    .filter((k) => st[k] === "mock")
    .map((k) => KIND_NAMES[k]);
  return {
    live: true,
    text: `Live with ${names}.` + (mocked.length ? ` Still placeholder: ${mocked.join(", ")}.` : ""),
  };
}

export default function DashboardPage() {
  return (
    <AuthGate>
      <Dashboard />
    </AuthGate>
  );
}

function Dashboard() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [thumbnails, setThumbnails] = useState<Record<number, number | null>>({});
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [providerStatus, setProviderStatus] = useState<Record<string, any> | null>(null);
  const [health, setHealth] = useState<{
    ok: boolean;
    results: { group: string; mode: string; ok: boolean; message: string; latency_ms?: number }[];
  } | null>(null);
  const [healthBusy, setHealthBusy] = useState(false);
  const [recent, setRecent] = useState<
    { render_id: number; project_id: number; project_title: string; created_at: string }[]
  >([]);

  const [seeding, setSeeding] = useState(false);
  // `loaded` gates the empty-state card so it never flashes during the first
  // fetch (when avatars/projects/ingredients are still empty arrays).
  const [loaded, setLoaded] = useState(false);
  const toast = useToast();

  const loadAll = async () => {
    api.recentRenders(12).then(setRecent).catch(() => setRecent([]));
    const ps = await api.listProjects();
    setProjects(ps);
    setThumbnails(Object.fromEntries(ps.map((p) => [p.id, p.latest_render_id ?? null])));
    setAvatars(await api.listAvatars());
    setIngredients(await api.listIngredients());
    setLoaded(true);
  };

  const seedDemo = async () => {
    setSeeding(true);
    try {
      const r = await api.seedDemo();
      await loadAll();
      toast.success(
        r.already_seeded
          ? "Library already had content — nothing to add"
          : "Demo cast + project loaded"
      );
    } catch (e: any) {
      toast.error(e.message || "Seed failed");
    } finally {
      setSeeding(false);
    }
  };

  useEffect(() => {
    loadAll();
    api.providerStatus().then(setProviderStatus);
  }, []);

  const isEmpty = loaded && avatars.length === 0 && projects.length === 0 && ingredients.length === 0;

  return (
    <div className="p-8 max-w-6xl">
      <header className="mb-8 flex items-end justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-ink-300 mt-1">
            Build a cast, generate assets in the Studio, then turn them into short-form video.
          </p>
        </div>
        <Link href="/projects/new" className="btn-primary">
          + New Project
        </Link>
      </header>

      <OnboardingChecklist />

      {isEmpty && (
        <div className="card p-6 mb-8 border-accent/40 bg-linear-to-br from-accent/10 via-ink-900 to-ink-900">
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
            <div>
              <div className="text-xl font-semibold">Get started in one click</div>
              <p className="text-sm text-ink-300 mt-1 max-w-xl">
                Your library is empty. Load the demo cast — two avatars (Naina + Arjun),
                a rooftop scene + 35mm film style, a Kissmet brand kit, and a sample
                project — to render a real video in mock mode without uploading anything.
              </p>
            </div>
            <div className="flex flex-col sm:flex-row gap-2 shrink-0">
              <button className="btn-primary" disabled={seeding} onClick={seedDemo}>
                {seeding ? "Loading demo…" : "Load demo cast + project"}
              </button>
              <Link href="/cast" className="btn-ghost">
                Or build your own
              </Link>
            </div>
          </div>
        </div>
      )}

      {providerStatus && (
        <div className="card p-4 mb-8">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {(() => {
              const sum = providerSummary(providerStatus);
              return (
                <span className="flex items-center gap-2 text-ink-200">
                  <span
                    aria-hidden
                    className={`w-2 h-2 rounded-full ${sum.live ? "bg-emerald-400" : "bg-amber-400"}`}
                  />
                  {sum.text}
                </span>
              );
            })()}
            <Link href="/settings#models" className="text-xs text-ink-400 hover:text-ink-100">
              Models &amp; keys →
            </Link>
            <button
              className="btn-ghost text-xs ml-auto"
              disabled={healthBusy}
              onClick={async () => {
                setHealthBusy(true);
                try {
                  setHealth(await api.healthCheck());
                } catch (e: any) {
                  setHealth({ ok: false, results: [{ group: "error", mode: "", ok: false, message: e.message }] });
                } finally {
                  setHealthBusy(false);
                }
              }}
            >
              {healthBusy ? "Testing…" : "Test provider keys"}
            </button>
          </div>
          {health && (
            <div className="mt-3 space-y-1.5">
              {health.results.map((r) => (
                <div
                  key={r.group}
                  className={`flex items-center gap-2 text-xs rounded-md px-3 py-1.5 border ${
                    r.ok
                      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                      : "border-red-500/40 bg-red-500/10 text-red-300"
                  }`}
                >
                  <span className="w-4 text-center">{r.ok ? "✓" : "✕"}</span>
                  <span className="font-medium capitalize">{r.group}</span>
                  <span className="text-ink-300">({r.mode})</span>
                  <span className="text-ink-200">{r.message}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {recent.length > 0 && (
        <section className="mb-10">
          <h2 className="text-lg font-medium mb-3">Recent videos</h2>
          <div className="flex gap-3 overflow-x-auto pb-2 -mx-1 px-1">
            {recent.map((r) => (
              <Link
                key={r.render_id}
                href={`/projects/${r.project_id}/render?id=${r.render_id}`}
                className="shrink-0 w-32 group"
                title={r.project_title}
              >
                <div className="aspect-9/16 rounded-lg overflow-hidden bg-ink-800 border border-ink-800 transition group-hover:border-iris/40 group-hover:shadow-glow relative">
                  <img
                    src={api.renderThumb(r.render_id)}
                    alt=""
                    aria-hidden
                    className="w-full h-full object-cover"
                  />
                  <div className="absolute inset-x-0 bottom-0 p-1.5 bg-linear-to-t from-black/85 to-transparent">
                    <div className="text-[11px] text-white truncate">{r.project_title}</div>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      <section className="mb-10">
        <h2 className="text-lg font-medium mb-3">Cast</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Tile href="/cast" label="Characters" count={avatars.length} />
          <Tile
            href="/cast?filter=scene"
            label="Scenes"
            count={ingredients.filter((i) => i.kind === "scene").length}
          />
          <Tile
            href="/cast?filter=style"
            label="Styles"
            count={ingredients.filter((i) => i.kind === "style").length}
          />
          <Tile
            href="/cast?filter=object"
            label="Objects & Props"
            count={ingredients.filter((i) => i.kind === "object" || i.kind === "prop").length}
          />
        </div>
      </section>

      <section>
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-lg font-medium">Recent projects</h2>
          <Link href="/projects" className="text-sm text-ink-300 hover:text-ink-100">
            View all →
          </Link>
        </div>
        {projects.length === 0 ? (
          <div className="card p-6 text-ink-300 text-sm">
            No projects yet — click <em>New Project</em>{" "}
            {isEmpty && "or use the one-click demo loader above"}.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {projects.slice(0, 6).map((p) => {
              const rid = thumbnails[p.id];
              return (
                <Link
                  href={p.mode === "ambient_loop" ? `/ambient/${p.id}` : `/projects/${p.id}`}
                  key={p.id}
                  className="card overflow-hidden card-hover flex flex-col"
                >
                  <div className="aspect-9/16 max-h-44 bg-ink-800 overflow-hidden">
                    {rid ? (
                      <img
                        src={api.renderThumb(rid)}
                        alt=""
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-ink-300 text-xs">
                        No video yet
                      </div>
                    )}
                  </div>
                  <div className="p-4">
                    <div className="text-sm font-medium">{p.title || "Untitled"}</div>
                    <div className="text-xs text-ink-300 mt-1">
                      {p.mode === "ambient_loop" ? (
                        <span className="text-iris-soft">∿ Ambient scene</span>
                      ) : (
                        modeLabel(p.mode)
                      )}{" "}
                      · {formatDuration(p.target_duration_seconds)}
                    </div>
                    <StatusBadge status={p.status} className="mt-3" />
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function Tile({ href, label, count }: { href: string; label: string; count: number }) {
  return (
    <Link href={href} className="card p-4 card-hover">
      <div className="text-2xl font-semibold">{count}</div>
      <div className="text-sm text-ink-300 mt-1">{label}</div>
    </Link>
  );
}

