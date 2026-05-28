"use client";
import { useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { api, SystemInfo } from "@/lib/api";

export default function SettingsPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

type Health = {
  ok: boolean;
  results: { group: string; mode: string; ok: boolean; message: string; latency_ms?: number }[];
};

function Inner() {
  const [info, setInfo] = useState<SystemInfo | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.systemInfo().then(setInfo).catch((e) => setError(e.message));
  }, []);

  const runHealth = async () => {
    setBusy(true);
    try {
      setHealth(await api.healthCheck());
    } catch (e: any) {
      setHealth({ ok: false, results: [{ group: "error", mode: "", ok: false, message: e.message }] });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="p-8 max-w-4xl">
      <header className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight">Settings & Status</h1>
        <p className="text-ink-300 mt-1">Deployment overview and provider connectivity.</p>
      </header>

      {error && <div className="text-sm text-accent mb-4">{error}</div>}

      {/* Provider connectivity */}
      <section className="card p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-medium">Provider connectivity</h2>
          <button className="btn-ghost text-xs" disabled={busy} onClick={runHealth}>
            {busy ? "Testing…" : "Test provider keys"}
          </button>
        </div>
        {!health ? (
          <div className="text-xs text-ink-400">
            Runs a lightweight no-generation call to each provider (OpenRouter <code>/models</code>,
            ElevenLabs <code>/voices</code>). In mock mode it reports <code>mock</code>.
          </div>
        ) : (
          <div className="space-y-1.5">
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
      </section>

      {info && (
        <>
          <section className="card p-4 mb-6">
            <h2 className="text-lg font-medium mb-3">Deployment</h2>
            <dl className="grid grid-cols-2 md:grid-cols-3 gap-y-3 gap-x-4 text-sm">
              <Row label="Version" value={info.version} />
              <Row label="Mode" value={info.mock_providers ? "Mock providers" : "Live providers"} />
              <Row label="Redis" value={info.redis_configured ? "Configured" : "Inline jobs"} />
              <Row label="FFmpeg" value={info.ffmpeg_available ? "Available" : "Missing"} bad={!info.ffmpeg_available} />
              <Row label="FFprobe" value={info.ffprobe_available ? "Available" : "Missing"} bad={!info.ffprobe_available} />
              <Row label="Data dir" value={info.data_dir} />
              <Row label="OpenRouter key" value={info.keys.openrouter ? "Set" : "Not set"} bad={!info.keys.openrouter && !info.mock_providers} />
              <Row label="ElevenLabs key" value={info.keys.elevenlabs ? "Set" : "Not set"} />
              <Row label="Public base" value={info.public_base_url} />
            </dl>
          </section>

          <section className="card p-4 mb-6">
            <h2 className="text-lg font-medium mb-3">Configured models</h2>
            <dl className="grid grid-cols-2 md:grid-cols-3 gap-y-3 gap-x-4 text-sm">
              <Row label="Chat" value={info.models.chat || "—"} />
              <Row label="Image" value={info.models.image || "auto"} />
              <Row label="Video" value={info.models.video || "auto"} />
              <Row label="TTS" value={info.models.tts || "—"} />
              <Row label="Music" value={info.models.music || "default"} />
            </dl>
          </section>

          <section className="card p-4">
            <h2 className="text-lg font-medium mb-3">Library</h2>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <Stat label="Avatars" value={info.counts.avatars} />
              <Stat label="Ingredients" value={info.counts.ingredients} />
              <Stat label="Brand kits" value={info.counts.brand_kits} />
              <Stat label="Projects" value={info.counts.projects} />
              <Stat label="Renders" value={info.counts.renders} />
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function Row({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wider text-ink-400">{label}</dt>
      <dd className={`mt-0.5 break-all ${bad ? "text-accent" : "text-ink-100"}`}>{value}</dd>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="card bg-ink-800 p-3 text-center">
      <div className="text-2xl font-semibold">{value}</div>
      <div className="text-xs text-ink-300 mt-1">{label}</div>
    </div>
  );
}
