"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { useToast } from "@/components/Toaster";
import { usd } from "@/components/sound/shared";
import { api, errorText, type CostEstimate, type SoundAsset } from "@/lib/api";

export default function SoundEffectsPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const toast = useToast();
  const [prompt, setPrompt] = useState("");
  const [seconds, setSeconds] = useState(3);
  const [loop, setLoop] = useState(false);
  const [variations, setVariations] = useState(2);
  const [price, setPrice] = useState<CostEstimate | null>(null);
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<SoundAsset[]>([]);
  const [library, setLibrary] = useState<SoundAsset[] | null>(null);
  const [filter, setFilter] = useState("");

  const loadLibrary = useCallback(
    () =>
      api
        .soundAssets()
        .then(setLibrary)
        .catch((e) => {
          setLibrary([]);
          toast.error(errorText(e, "Couldn't load the library"));
        }),
    [toast]
  );
  useEffect(() => {
    loadLibrary();
  }, [loadLibrary]);

  // The price as the form changes (free: no provider is called).
  useEffect(() => {
    const p = prompt.trim();
    if (!p) {
      setPrice(null);
      return;
    }
    const t = setTimeout(() => {
      api
        .effectsEstimate({ prompt: p, seconds, loop, variations })
        .then(setPrice)
        .catch(() => setPrice(null));
    }, 300);
    return () => clearTimeout(t);
  }, [prompt, seconds, loop, variations]);

  const make = async () => {
    const p = prompt.trim();
    if (!p) return;
    setBusy(true);
    try {
      const r = await api.makeEffects({ prompt: p, seconds, loop, variations });
      setMade(r.assets);
      const cached = r.assets.filter((a) => a.cached).length;
      toast.success(
        cached === r.assets.length
          ? "Already in your library: nothing paid."
          : `Saved ${r.assets.length} sound${r.assets.length === 1 ? "" : "s"} to the library.`
      );
      loadLibrary();
    } catch (e) {
      toast.error(errorText(e, "Couldn't make the sound"));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (a: SoundAsset) => {
    if (!window.confirm(`Remove “${a.name || "this sound"}” from the library?`)) return;
    try {
      await api.deleteSoundAsset(a.id);
      setMade((m) => m.filter((x) => x.id !== a.id));
      loadLibrary();
    } catch (e) {
      toast.error(errorText(e, "Couldn't remove it"));
    }
  };

  const shown = (library ?? []).filter(
    (a) => !filter.trim() || `${a.name} ${a.prompt}`.toLowerCase().includes(filter.trim().toLowerCase())
  );

  return (
    <div className="p-4 sm:p-8 max-w-5xl">
      <nav className="text-sm text-ink-400 mb-3">
        <Link href="/sound" className="hover:underline">
          Sound Studio
        </Link>{" "}
        / Sound effects
      </nav>
      <h1 className="text-3xl font-semibold tracking-tight mb-1">Sound effects</h1>
      <p className="text-sm text-ink-300 mb-6 max-w-xl">
        Describe a sound and get up to four takes, saved to your library. The same sound asked for again is free,
        and a story that asks for it uses the same one.
      </p>

      <section className="card p-4 flex flex-col gap-3 mb-8" aria-labelledby="make-h">
        <h2 id="make-h" className="sr-only">
          Make a sound
        </h2>
        <label className="text-sm flex flex-col gap-1">
          <span className="text-ink-300">Describe the sound</span>
          <input
            className="input"
            value={prompt}
            maxLength={400}
            placeholder="An old wooden door creaks open slowly"
            onChange={(e) => setPrompt(e.target.value)}
          />
        </label>
        <div className="flex flex-wrap gap-4 items-end">
          <label className="text-sm flex flex-col gap-1">
            <span className="text-ink-300">Length (seconds)</span>
            <input
              className="input w-28"
              type="number"
              min={0.5}
              max={30}
              step={0.5}
              value={seconds}
              onChange={(e) => setSeconds(Math.max(0.5, Math.min(30, Number(e.target.value) || 3)))}
            />
          </label>
          <label className="text-sm flex flex-col gap-1">
            <span className="text-ink-300">Takes</span>
            <select className="input w-24" value={variations} onChange={(e) => setVariations(Number(e.target.value))}>
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm inline-flex items-center gap-2 pb-2">
            <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} />
            Seamless loop (an ambience bed)
          </label>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-sm text-ink-300">
            {price ? (
              <>
                About <strong className="text-ink-100">{usd(price.total)}</strong>
                {price.total === 0 ? " (already in your library)" : ""}
              </>
            ) : prompt.trim() ? (
              "Working out the price…"
            ) : (
              " "
            )}
          </span>
          <button type="button" className="btn-primary" disabled={busy || !prompt.trim()} onClick={make}>
            {busy ? "Making…" : `Make ${variations === 1 ? "it" : `${variations} takes`}${price ? ` (${usd(price.total)})` : ""}`}
          </button>
        </div>
        {made.length > 0 && (
          <ul className="grid gap-2 sm:grid-cols-2 border-t border-ink-800 pt-3">
            {made.map((a, i) => (
              <li key={a.id} className="flex items-center gap-2 text-sm">
                <span className="w-14 text-ink-400">Take {i + 1}</span>
                <audio controls preload="none" className="h-8 flex-1 min-w-0" src={api.soundUrl(a.url, a.id)} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="lib-h">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <h2 id="lib-h" className="text-lg font-semibold">
            Library
          </h2>
          <input
            className="input text-sm w-56"
            placeholder="Search the library"
            aria-label="Search the library"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        {library === null ? (
          <div className="skeleton h-24 rounded-xl" />
        ) : shown.length === 0 ? (
          <p className="card p-4 text-sm text-ink-400">No sounds yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {shown.map((a) => (
              <li key={a.id} className="card p-3 flex flex-wrap items-center gap-3 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-ink-100 truncate">{a.name || a.prompt}</div>
                  <div className="text-xs text-ink-400">
                    #{a.id} · {a.seconds} s{a.loop ? " · loop" : ""}
                    {a.story_id ? " · from a story" : ""}
                  </div>
                </div>
                <audio controls preload="none" className="h-8 w-56 max-w-full" src={api.soundUrl(a.url, a.id)} />
                <button type="button" className="text-xs text-ink-400 hover:text-red-300" onClick={() => remove(a)}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-ink-500 mt-3">
          Use a library sound in a story with <code>[sfx: #id]</code>.
        </p>
      </section>
    </div>
  );
}
