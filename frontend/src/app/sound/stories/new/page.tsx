"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { AutoTextarea } from "@/components/AutoTextarea";
import { useToast } from "@/components/Toaster";
import { PriceNote, SFX_DENSITIES, STORY_STYLES, ScriptView, fmtMinutes } from "@/components/sound/shared";
import { api, errorText, type SfxDensity, type SoundConfig, type StoryEstimate, type StoryParse } from "@/lib/api";

export default function NewStoryPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

type How = "write" | "paste";

function Inner() {
  const router = useRouter();
  const toast = useToast();
  const [cfg, setCfg] = useState<SoundConfig | null>(null);
  const [title, setTitle] = useState("");
  const [premise, setPremise] = useState("");
  const [minutes, setMinutes] = useState(20);
  const [style, setStyle] = useState("sleep");
  const [density, setDensity] = useState<SfxDensity>("medium");
  const [how, setHow] = useState<How>("write");
  const [script, setScript] = useState("");
  const [parsed, setParsed] = useState<StoryParse | null>(null);
  const [price, setPrice] = useState<StoryEstimate | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.soundConfig().then(setCfg).catch(() => {});
  }, []);
  const max = cfg?.max_minutes ?? 90;
  const styles = STORY_STYLES.filter((s) => !cfg || cfg.styles.includes(s.id));

  // The writer's price for this length (free to ask).
  useEffect(() => {
    const t = setTimeout(() => {
      api
        .storyEstimate({ premise, target_minutes: minutes, settings: { style, sfx_density: density } })
        .then(setPrice)
        .catch(() => setPrice(null));
    }, 300);
    return () => clearTimeout(t);
  }, [premise, minutes, style, density]);

  // A pasted script: what the parser makes of it.
  useEffect(() => {
    if (how !== "paste" || !script.trim()) {
      setParsed(null);
      return;
    }
    const t = setTimeout(() => {
      api.parseStory(script, style).then(setParsed).catch(() => setParsed(null));
    }, 400);
    return () => clearTimeout(t);
  }, [how, script, style]);

  const create = async () => {
    if (how === "write" && !premise.trim()) {
      toast.error("Write a premise first: what is the story about?");
      return;
    }
    if (how === "paste" && !script.trim()) {
      toast.error("Paste a script first.");
      return;
    }
    setBusy(true);
    try {
      const story = await api.createStory({
        title: title.trim(),
        kind: style === "asmr" ? "asmr" : "story",
        premise: premise.trim(),
        target_minutes: minutes,
        settings: { style, sfx_density: density },
        script_text: how === "paste" ? script : "",
      });
      if (how === "write") {
        try {
          await api.writeStory(story.id);
        } catch (e) {
          toast.error(errorText(e, "Couldn't start the writer"));
        }
      }
      router.push(`/sound/stories/${story.id}`);
    } catch (e) {
      toast.error(errorText(e, "Couldn't create the story"));
      setBusy(false);
    }
  };

  return (
    <div className="p-4 sm:p-8 max-w-3xl">
      <nav className="text-sm text-ink-400 mb-4">
        <Link href="/sound" className="hover:underline">
          Sound Studio
        </Link>{" "}
        / New story
      </nav>
      <h1 className="text-2xl font-semibold tracking-tight mb-6">New audio story</h1>

      <div className="card p-5 flex flex-col gap-5">
        <label className="flex flex-col gap-1">
          <span className="label">Title (optional)</span>
          <input className="input" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
        </label>

        <label className="flex flex-col gap-1">
          <span className="label">Premise</span>
          <AutoTextarea
            className="input min-h-24"
            value={premise}
            maxLength={4000}
            placeholder="A lighthouse keeper keeps the light through a long, rainy night…"
            onChange={(e) => setPremise(e.target.value)}
          />
          <span className="text-xs text-ink-400">
            Who is in it, where it happens and the mood. Every character in anything romantic is an adult.
          </span>
        </label>

        <label className="flex flex-col gap-1">
          <span className="label">
            Length: <strong className="text-ink-100">{minutes} min</strong>
          </span>
          <input
            type="range"
            min={1}
            max={max}
            value={Math.min(minutes, max)}
            onChange={(e) => setMinutes(Number(e.target.value))}
            aria-valuetext={`${minutes} minutes`}
          />
          <span className="text-xs text-ink-400">Up to {max} minutes.</span>
        </label>

        <fieldset>
          <legend className="label mb-2">Style</legend>
          <div className="flex flex-wrap gap-2">
            {styles.map((s) => (
              <button
                key={s.id}
                type="button"
                aria-pressed={style === s.id}
                className={clsx("chip", style === s.id && "chip-active")}
                title={s.hint}
                onClick={() => setStyle(s.id)}
              >
                {s.label}
              </button>
            ))}
          </div>
        </fieldset>

        <fieldset>
          <legend className="label mb-2">Sound effects</legend>
          <div className="flex flex-wrap gap-2">
            {SFX_DENSITIES.map((d) => (
              <button
                key={d.id}
                type="button"
                aria-pressed={density === d.id}
                className={clsx("chip", density === d.id && "chip-active")}
                onClick={() => setDensity(d.id)}
              >
                {d.label}
              </button>
            ))}
          </div>
        </fieldset>

        <fieldset>
          <legend className="label mb-2">The script</legend>
          <div className="flex flex-wrap gap-2 mb-3">
            <button
              type="button"
              aria-pressed={how === "write"}
              className={clsx("chip", how === "write" && "chip-active")}
              onClick={() => setHow("write")}
            >
              Write it for me
            </button>
            <button
              type="button"
              aria-pressed={how === "paste"}
              className={clsx("chip", how === "paste" && "chip-active")}
              onClick={() => setHow("paste")}
            >
              Paste my script
            </button>
          </div>
          {how === "write" ? (
            <p className="text-sm text-ink-300">
              The writer plans the story, then writes it chapter by chapter; you can cancel at any point and keep
              the chapters already written. <PriceNote price={price?.write} />.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              <AutoTextarea
                className="input font-mono text-[13px] min-h-48"
                value={script}
                placeholder={"@cast MARA [left]: adult lighthouse keeper, low voice\n\n# Chapter 1: The light\nThe rain had not stopped for three days.\nMARA [whispers]: Did you hear that?\n[sfx: old door creaks | 3s]\n(pause 2)"}
                onChange={(e) => setScript(e.target.value)}
              />
              {parsed && (
                <div className="text-xs text-ink-400">
                  About {fmtMinutes(parsed.est_seconds)} · {parsed.chapters.length} chapter
                  {parsed.chapters.length === 1 ? "" : "s"} · {parsed.sfx} sound cue{parsed.sfx === 1 ? "" : "s"}
                  {parsed.warnings.length > 0 && (
                    <span className="text-amber-300"> · {parsed.warnings.length} warning(s)</span>
                  )}
                </div>
              )}
              {parsed && parsed.warnings.length > 0 && (
                <ul className="text-xs text-amber-300 list-disc pl-5">
                  {parsed.warnings.slice(0, 8).map((w, i) => (
                    <li key={i}>
                      Line {w.line}: {w.message}
                    </li>
                  ))}
                </ul>
              )}
              {script.trim() && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-ink-400">Preview with highlighting</summary>
                  <div className="mt-2 max-h-80 overflow-auto rounded-lg border border-ink-700 p-2">
                    <ScriptView text={script} parsed={parsed} />
                  </div>
                </details>
              )}
            </div>
          )}
        </fieldset>

        <div className="flex flex-wrap items-center justify-end gap-3 pt-2 border-t border-ink-700">
          <Link href="/sound" className="btn-ghost">
            Cancel
          </Link>
          <button type="button" className="btn-primary" disabled={busy} onClick={create}>
            {busy ? "Starting…" : how === "write" ? `Write it for me${price ? ` (about $${price.write.cost.toFixed(2)})` : ""}` : "Create story"}
          </button>
        </div>
      </div>
    </div>
  );
}
