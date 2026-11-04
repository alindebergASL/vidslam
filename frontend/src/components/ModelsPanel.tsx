"use client";
import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { api, ActiveModel, CatalogEntry, ModelCatalog, ModelKind } from "@/lib/api";

const KINDS: { id: ModelKind; label: string; hint: string }[] = [
  { id: "video", label: "Video", hint: "Shot clips and ambient loops." },
  { id: "image", label: "Image", hint: "Studio images, ambient anchor stills and Still + drift." },
  { id: "chat", label: "Planner", hint: "Writes storyboards (script, shots, captions)." },
  { id: "tts", label: "Voice", hint: "Voiceovers. ElevenLabs, or an OpenRouter speech / gpt-audio model." },
  { id: "music", label: "Music", hint: "Music beds. ElevenLabs, or Google Lyria — Lyria 3 Pro / Clip through OpenRouter (same key), Lyria 3.5 through Google (GEMINI_API_KEY)." },
  { id: "lipsync", label: "Lip-sync", hint: "Talking-head shots from the avatar's photo + voiceover. Off = regular video model." },
];

const PROVIDER_LABEL: Record<string, string> = {
  openrouter: "OpenRouter",
  gemini: "Google",
  elevenlabs: "ElevenLabs",
  mock: "mock",
  off: "off",
};

function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function priceChip(e: CatalogEntry): string | null {
  const p = e.pricing || {};
  if (p.prompt || p.completion) {
    const m = (x?: number) => (x ? `$${(x * 1e6).toFixed(x * 1e6 < 1 ? 2 : 1)}` : "—");
    return `${m(p.prompt)} / ${m(p.completion)} per M tok`;
  }
  const skus = e.pricing_skus || {};
  const perSec = Object.entries(skus)
    .filter(([k]) => k.startsWith("duration_seconds"))
    .map(([, v]) => Number(v))
    .filter((v) => v > 0);
  if (perSec.length) {
    const lo = Math.min(...perSec), hi = Math.max(...perSec);
    return lo === hi ? `$${lo}/s` : `$${lo}–${hi}/s`;
  }
  if (p.image) return `$${p.image}/image`;
  return null;
}

function chips(e: CatalogEntry): string[] {
  const out: string[] = [];
  if (e.max_seconds) out.push(`up to ${e.max_seconds}s`);
  if (e.frame_images?.includes("last_frame")) out.push("first+last frame");
  else if (e.frame_images?.includes("first_frame")) out.push("first frame");
  if (e.context_length) out.push(`${Math.round(e.context_length / 1000)}k ctx`);
  if (e.kind === "chat" && e.supported_parameters?.length)
    out.push(e.supported_parameters.includes("response_format") ? "JSON mode" : "plain JSON");
  if (e.supported_voices?.length) out.push(`${e.supported_voices.length} voices`);
  if (e.also_via === "gemini") out.push("also via Google");
  if (e.kind === "tts" && e.output_modalities?.includes("audio") && !e.output_modalities.includes("speech"))
    out.push("chat audio");
  const price = priceChip(e);
  if (price) out.push(price);
  return out;
}

export function ModelsPanel() {
  const toast = useToast();
  const [cat, setCat] = useState<ModelCatalog | null>(null);
  const [kind, setKind] = useState<ModelKind>("video");
  const [q, setQ] = useState("");
  const [onlyUsable, setOnlyUsable] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [custom, setCustom] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.modelCatalog().then(setCat).catch((e) => setError(e.message));
  }, []);

  const active: ActiveModel | undefined = cat?.active?.[kind];
  const entries = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (cat?.entries ?? [])
      .filter((e) => e.kind === kind)
      .filter((e) => !onlyUsable || e.usable)
      .filter((e) => !needle || e.id.toLowerCase().includes(needle) || e.name.toLowerCase().includes(needle))
      .sort((a, b) =>
        Number(b.id === active?.model) - Number(a.id === active?.model) ||
        Number(b.new) - Number(a.new) ||
        Number(b.usable) - Number(a.usable) ||
        (b.created ?? 0) - (a.created ?? 0) ||
        a.name.localeCompare(b.name)
      );
  }, [cat, kind, q, onlyUsable, active?.model]);

  const newByKind = (k: ModelKind) => (cat?.entries ?? []).filter((e) => e.kind === k && e.new).length;

  const refresh = async () => {
    setBusy("refresh");
    try {
      const c = await api.refreshModels();
      setCat(c);
      toast.success(c.new_count ? `Catalog refreshed — ${c.new_count} new model${c.new_count === 1 ? "" : "s"}` : "Catalog refreshed");
    } catch (e: any) {
      toast.error(e.message || "Refresh failed");
    } finally {
      setBusy(null);
    }
  };

  const choose = async (model: string | null, opts: { allow_unlisted?: boolean; options?: Record<string, string> } = {}) => {
    setBusy(model ?? "reset");
    try {
      const act = await api.setActiveModel(kind, model, opts);
      setCat((c) => (c ? { ...c, active: act } : c));
      toast.success(model === null ? "Reset to default" : model === "" ? "Lip-sync off" : `Now using ${model}`);
    } catch (e: any) {
      toast.error(e.message || "Switch failed");
    } finally {
      setBusy(null);
    }
  };

  const voices = useMemo(() => {
    if (kind !== "tts" || !active) return [];
    const e = cat?.entries.find((x) => x.kind === "tts" && x.id === active.model);
    if (e?.supported_voices?.length) return e.supported_voices;
    // Chat-audio models (gpt-audio) don't list voices; offer OpenAI's
    // documented set (the adapter defaults to "alloy").
    const chatAudio = e?.output_modalities?.includes("audio") && !e.output_modalities.includes("speech");
    return chatAudio ? ["alloy", "echo", "fable", "onyx", "nova", "shimmer"] : [];
  }, [cat, kind, active]);

  return (
    <section className="card p-4 mb-6" aria-labelledby="models-h">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
        <div>
          <h2 id="models-h" className="text-lg font-medium flex items-center gap-2">
            Models
            {!!cat?.new_count && (
              <span className="chip text-[11px] py-0.5 grad border-transparent text-ink-950">
                {cat.new_count} new
              </span>
            )}
          </h2>
          <p className="text-xs text-ink-400 mt-0.5">
            Live from OpenRouter{cat?.entries.some((e) => e.provider === "gemini" && e.usable) ? " and Google" : ""} ·
            polled {ago(cat?.fetched_at ?? null)} · refreshes every few hours. Switching applies to the next job.
          </p>
        </div>
        <button type="button" className="btn-ghost text-xs" disabled={busy !== null} onClick={refresh}>
          {busy === "refresh" ? "Polling…" : "Refresh now"}
        </button>
      </div>

      {error && <div role="alert" className="text-sm text-accent mb-3">{error}</div>}
      {cat && Object.keys(cat.errors || {}).length > 0 && (
        <div className="text-[11px] text-amber-200 border border-amber-500/30 bg-amber-500/10 rounded-md px-3 py-2 mb-3">
          Some sources failed on the last poll (showing their previous models):{" "}
          {Object.entries(cat.errors).map(([k, v]) => `${k}: ${v.slice(0, 80)}`).join(" · ")}
        </div>
      )}

      <div role="group" aria-label="Model kind" className="flex flex-wrap gap-2 mb-4">
        {KINDS.map((k) => (
          <button
            key={k.id}
            type="button"
            aria-pressed={kind === k.id}
            onClick={() => { setKind(k.id); setQ(""); setCustom(""); }}
            className={clsx("chip text-xs", kind === k.id && "chip-active")}
          >
            {k.label}
            {newByKind(k.id) > 0 && <span className="text-[10px] text-accent font-semibold">+{newByKind(k.id)}</span>}
          </button>
        ))}
      </div>

      {!cat ? (
        <div className="space-y-2" aria-hidden>
          {Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-12 skeleton" />)}
        </div>
      ) : (
        <>
          <div className="rounded-lg border border-iris/40 bg-iris/10 p-3 mb-3">
            <div className="text-[10px] uppercase tracking-wider text-ink-300">
              Active {KINDS.find((k) => k.id === kind)?.label.toLowerCase()} model
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 mt-1">
              <div className="min-w-0">
                <div className="text-sm font-medium break-all">
                  {active?.model || (kind === "lipsync" ? "Off" : "—")}
                </div>
                <div className="text-[11px] text-ink-300">
                  served by {PROVIDER_LABEL[active?.provider ?? ""] ?? active?.provider}
                  {active?.is_default ? " · .env default" : ` · default is ${active?.default || "off"}`}
                </div>
              </div>
              <div className="flex items-center gap-2">
                {kind === "tts" && voices.length > 0 && (
                  <label className="text-xs text-ink-300 flex items-center gap-1.5">
                    Voice
                    <select
                      className="input py-1 text-xs w-32"
                      value={active?.options?.voice || voices[0]}
                      onChange={(e) => choose(active!.model, { options: { voice: e.target.value } })}
                    >
                      {voices.map((v) => <option key={v} value={v}>{v}</option>)}
                    </select>
                  </label>
                )}
                {kind === "lipsync" && active?.model && (
                  <button type="button" className="btn-ghost text-xs" disabled={busy !== null} onClick={() => choose("")}>
                    Turn off
                  </button>
                )}
                {!active?.is_default && (
                  <button type="button" className="btn-ghost text-xs" disabled={busy !== null} onClick={() => choose(null)}>
                    Reset to default
                  </button>
                )}
              </div>
            </div>
            {active?.provider === "mock" && active.model && !active.model.startsWith("mock/") && (
              <p className="text-[11px] text-amber-200 mt-2">
                Selected, but mocked: MOCK_PROVIDERS is on or this provider&apos;s key isn&apos;t set.
              </p>
            )}
          </div>

          <p className="text-xs text-ink-400 mb-2">{KINDS.find((k) => k.id === kind)?.hint}</p>
          <div className="flex flex-wrap items-center gap-3 mb-3">
            <input
              className="input text-sm py-1.5 flex-1 min-w-48"
              placeholder={`Search ${entries.length} ${KINDS.find((k) => k.id === kind)?.label.toLowerCase()} models…`}
              aria-label="Search models"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <label className="text-xs text-ink-300 flex items-center gap-1.5">
              <input type="checkbox" checked={onlyUsable} onChange={(e) => setOnlyUsable(e.target.checked)} className="accent-iris" />
              Only usable with my keys
            </label>
          </div>

          <ul className="space-y-1.5 max-h-112 overflow-y-auto pr-1" aria-label="Available models">
            {entries.length === 0 && (
              <li className="text-sm text-ink-300 px-3 py-6 text-center border border-dashed border-ink-700 rounded-lg">
                No models{q ? ` matching “${q}”` : ""}. Try Refresh now.
              </li>
            )}
            {entries.map((e) => {
              const isActive = e.id === active?.model;
              return (
                <li
                  key={`${e.provider}:${e.id}`}
                  className={clsx(
                    "rounded-lg border px-3 py-2 flex items-start justify-between gap-3",
                    isActive ? "border-iris/60 bg-iris/5" : "border-ink-800 bg-ink-900/60"
                  )}
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-sm font-medium">{e.name}</span>
                      {e.new && <span className="text-[10px] font-semibold uppercase tracking-wider text-accent">new</span>}
                      {!e.usable && <span className="text-[10px] text-ink-400">needs {PROVIDER_LABEL[e.provider]} key</span>}
                    </div>
                    <div className="text-[11px] text-ink-400 break-all">{e.id} · {PROVIDER_LABEL[e.provider]}</div>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {chips(e).map((c) => (
                        <span key={c} className="rounded-sm border border-ink-700 px-1.5 py-px text-[10px] text-ink-300">{c}</span>
                      ))}
                    </div>
                  </div>
                  <button
                    type="button"
                    className={clsx("text-xs shrink-0", isActive ? "btn-ghost opacity-60" : "btn-ghost")}
                    disabled={isActive || busy !== null}
                    onClick={() => choose(e.id)}
                    aria-label={isActive ? `${e.name} is active` : `Use ${e.name}`}
                  >
                    {isActive ? "Active" : busy === e.id ? "…" : "Use"}
                  </button>
                </li>
              );
            })}
          </ul>

          <form
            className="flex flex-wrap items-center gap-2 mt-3"
            onSubmit={(ev) => {
              ev.preventDefault();
              if (custom.trim()) choose(custom.trim(), { allow_unlisted: true });
            }}
          >
            <label htmlFor="custom-model" className="text-xs text-ink-300">Model id not listed?</label>
            <input
              id="custom-model"
              className="input text-xs py-1 flex-1 min-w-48"
              placeholder="provider/model-id"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
            />
            <button type="submit" className="btn-ghost text-xs" disabled={!custom.trim() || busy !== null}>
              Use anyway
            </button>
          </form>
        </>
      )}
    </section>
  );
}
