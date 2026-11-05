"use client";
import clsx from "clsx";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import type { AmbientConfig, AmbientPresets, AmbientScene, LoopPlan, ScenePreset } from "@/lib/api";

/** "3600" → "1h", "5400" → "1h 30m", "600" → "10m", "20" → "20s". */
export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h && m) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (h) return `${h}h`;
  if (m && sec) return `${m}m ${String(sec).padStart(2, "0")}s`;
  if (m) return `${m}m`;
  return `${sec}s`;
}

export function sceneStatusLabel(s: AmbientScene): string {
  if (s.status === "generating") {
    return s.loop_status === "completed" ? "Rendering…" : "Generating loop…";
  }
  if (s.status === "completed") return "Rendered";
  if (s.status === "planned") return "Loop ready";
  if (s.status === "failed") return "Failed";
  if (s.status === "cancelled") return "Cancelled";
  return "Draft";
}

export function statusTone(s: AmbientScene): string {
  if (s.status === "completed") return "bg-emerald-500/20 text-emerald-300 border-emerald-500/40";
  if (s.status === "failed") return "bg-red-500/20 text-red-300 border-red-500/40";
  if (s.status === "generating") return "bg-amber-500/20 text-amber-300 border-amber-500/40";
  if (s.status === "planned") return "bg-iris/20 text-iris-soft border-iris/40";
  return "bg-ink-700 text-ink-200 border-ink-600";
}

export function presetFor(presets: ScenePreset[] | null, id: string): ScenePreset | undefined {
  return presets?.find((p) => p.id === id);
}

const FALLBACK_PALETTE: [string, string] = ["#1b1e2d", "#7c5cff"];

/**
 * Gradient "scene art" used wherever a scene has no thumbnail yet. Two
 * layered radial washes from the preset palette, breathing very slowly —
 * the same barely-there motion the finished loops have. Pass `still` to
 * freeze it (dense grids, reduced-motion contexts).
 */
export function SceneArt({
  palette,
  emoji,
  className,
  still = false,
  image,
  children,
}: {
  palette?: [string, string];
  emoji?: string;
  className?: string;
  still?: boolean;
  /** Generated preset preview (absolute URL). Replaces the gradient art. */
  image?: string | null;
  children?: ReactNode;
}) {
  const [a, b] = palette || FALLBACK_PALETTE;
  if (image) {
    return (
      <div className={clsx("relative overflow-hidden bg-ink-900", className)} aria-hidden>
        <img
          src={image}
          alt=""
          className={clsx("absolute inset-0 w-full h-full object-cover", !still && "animate-breathe motion-reduce:animate-none")}
        />
        {children}
      </div>
    );
  }
  return (
    <div className={clsx("relative overflow-hidden bg-ink-900", className)} aria-hidden>
      <div
        className={clsx("absolute inset-[-8%]", !still && "animate-breathe motion-reduce:animate-none")}
        style={{
          background: [
            `radial-gradient(70% 60% at 22% 18%, ${b}cc, transparent 62%)`,
            `radial-gradient(60% 55% at 80% 85%, ${b}66, transparent 60%)`,
            `linear-gradient(150deg, ${a} 0%, #05060b 100%)`,
          ].join(","),
        }}
      />
      {/* Horizon haze: a faint band that reads as atmosphere, not texture. */}
      <div
        className="absolute inset-x-0 bottom-0 h-1/2"
        style={{ background: "linear-gradient(to top, rgba(5,6,11,0.55), transparent)" }}
      />
      {emoji && (
        <div className="absolute inset-0 flex items-center justify-center text-4xl md:text-5xl drop-shadow-[0_6px_24px_rgba(0,0,0,0.6)] select-none">
          {emoji}
        </div>
      )}
      {children}
    </div>
  );
}

/** Pill-row chooser used for loop/total/aspect/mood choices. */
export function ChipRow<T extends string | number>({
  options,
  value,
  onChange,
  label,
  render,
  className,
}: {
  options: T[];
  value: T;
  onChange: (v: T) => void;
  label: string;
  render?: (v: T) => ReactNode;
  className?: string;
}) {
  return (
    <div role="group" aria-label={label} className={clsx("flex flex-wrap gap-2", className)}>
      {options.map((o) => (
        <button
          key={String(o)}
          type="button"
          onClick={() => onChange(o)}
          aria-pressed={value === o}
          className={clsx("chip", value === o && "chip-active")}
        >
          {render ? render(o) : String(o)}
        </button>
      ))}
    </div>
  );
}

const fmtSec = (n: number) => `${n % 1 ? n.toFixed(1) : n.toFixed(0)}s`;

/**
 * The loop recipe from the server's plan: which clips the model will make,
 * how they join, and how many times the seamless unit tiles. Shown in the
 * builder (live, from the cost estimate) and on the scene page.
 */
export function PlanSummary({
  plan,
  total,
  compact = false,
}: {
  plan: LoopPlan | null | undefined;
  total: number;
  compact?: boolean;
}) {
  if (!plan) {
    return <div className="h-12 skeleton" aria-hidden />;
  }
  const drift = plan.strategy === "drift";
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5" aria-label="Loop clips">
        {plan.anchored && (
          <span className="chip text-[11px] py-0.5 px-2 border-iris/40 text-iris-soft" title="Anchor still: first frame of clip 1 and last frame of the final clip">
            ◎ anchor
          </span>
        )}
        {plan.segments.map((s, i) => (
          <span key={i} className="inline-flex items-center gap-1.5">
            {i > 0 && <span className="text-ink-400 text-xs" aria-hidden>+</span>}
            <span className="rounded-md border border-ink-600 bg-ink-800 px-2 py-0.5 text-xs font-medium tabular-nums">
              {drift ? `still · ${s}s` : `${s}s`}
            </span>
          </span>
        ))}
        <span className="text-ink-400 text-xs">→</span>
        <span className="text-xs text-ink-200">
          {fmtSec(plan.unit_seconds)} unit × <span className="text-grad font-semibold">{plan.repeats}</span> ={" "}
          {fmtDuration(total)}
        </span>
      </div>
      {!compact && (
        <div className="flex flex-wrap gap-1.5 text-[11px]">
          <span
            className={clsx(
              "rounded-full border px-2 py-0.5",
              plan.closed
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                : "border-amber-500/40 bg-amber-500/10 text-amber-200"
            )}
          >
            {plan.closed ? "Closed loop" : `Open loop · ${fmtSec(plan.seam_crossfade)} crossfade`}
          </span>
          {!drift && (
            <span className="rounded-full border border-ink-600 px-2 py-0.5 text-ink-300">
              model max {plan.max_clip_seconds}s{plan.caps_known ? "" : " (assumed)"}
            </span>
          )}
          <span className="rounded-full border border-ink-600 px-2 py-0.5 text-ink-300">
            {plan.provider_calls} provider call{plan.provider_calls === 1 ? "" : "s"}
          </span>
        </div>
      )}
      {!compact && plan.notes.length > 0 && (
        <ul className="text-[11px] text-ink-400 space-y-0.5">
          {plan.notes.map((n) => (
            <li key={n}>· {n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Absolute URL for a preset's cached preview, if it has one. */
export function previewSrc(preset?: ScenePreset | null): string | null {
  if (!preset?.preview_url) return null;
  return api.presetPreviewUrl(preset.preview_url);
}

export const MUSIC_LENGTHS: { secs: number; label: string }[] = [
  { secs: 0, label: "Auto (~1 min)" },
  { secs: 180, label: "3 min" },
  { secs: 600, label: "10 min" },
  { secs: 1200, label: "20 min" },
];

// ---------- Look, light & weather, nature sound (shared by new + scene pages) ----------

type CfgSetter = <K extends keyof AmbientConfig>(k: K, v: AmbientConfig[K]) => void;

const FALLBACK_LOOKS = [
  { id: "photoreal", label: "Photoreal" }, { id: "cinematic", label: "Cinematic film" },
  { id: "painterly", label: "Painterly" }, { id: "watercolor", label: "Watercolor" },
  { id: "anime", label: "Hand-drawn anime" }, { id: "claymation", label: "Claymation" },
  { id: "dreamy", label: "Dreamy" },
];

function strengthLabel(v: number): string {
  return v < 0.34 ? "Subtle" : v <= 0.67 ? "Balanced" : "Strong";
}

/** Art style (realistic → stylized), its strength, and light/weather. */
export function LookControls({
  cfg,
  set,
  presets,
  idPrefix = "look",
}: {
  cfg: AmbientConfig;
  set: CfgSetter;
  presets: AmbientPresets | null;
  idPrefix?: string;
}) {
  const looks = presets?.looks?.length ? presets.looks : FALLBACK_LOOKS;
  const times = presets?.times_of_day ?? [];
  const weathers = presets?.weathers ?? [];
  return (
    <div className="space-y-3">
      <div>
        <div className="label">Look</div>
        <ChipRow
          label="Look"
          options={looks.map((l) => l.id as AmbientConfig["look"])}
          value={cfg.look}
          onChange={(v) => set("look", v)}
          render={(id) => looks.find((l) => l.id === id)?.label ?? id}
          className="text-xs"
        />
        {cfg.look !== "photoreal" && (
          <>
            <label className="label mt-3" htmlFor={`${idPrefix}-strength`}>
              Style strength · {strengthLabel(cfg.look_strength)}
            </label>
            <input
              id={`${idPrefix}-strength`}
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={cfg.look_strength}
              onChange={(e) => set("look_strength", Number(e.target.value))}
              className="w-full accent-iris"
              aria-valuetext={strengthLabel(cfg.look_strength)}
            />
            <div className="flex justify-between text-[10px] text-ink-400">
              <span>Mostly realistic</span>
              <span>Fully stylized</span>
            </div>
          </>
        )}
      </div>
      {times.length > 0 && (
        <div>
          <div className="label">Time of day</div>
          <ChipRow
            label="Time of day"
            options={times.map((t) => t.id)}
            value={cfg.time_of_day}
            onChange={(v) => set("time_of_day", v)}
            render={(id) => times.find((t) => t.id === id)?.label ?? id}
            className="text-xs"
          />
        </div>
      )}
      {weathers.length > 0 && (
        <div>
          <div className="label">Weather</div>
          <ChipRow
            label="Weather"
            options={weathers.map((w) => w.id)}
            value={cfg.weather}
            onChange={(v) => set("weather", v)}
            render={(id) => weathers.find((w) => w.id === id)?.label ?? id}
            className="text-xs"
          />
        </div>
      )}
    </div>
  );
}

/** Nature-sound bed (rain, waves, fire…), separate from the music. */
export function SoundControls({
  cfg,
  set,
  presets,
  idPrefix = "sound",
}: {
  cfg: AmbientConfig;
  set: CfgSetter;
  presets: AmbientPresets | null;
  idPrefix?: string;
}) {
  const sounds = presets?.sounds ?? [];
  const options = ["none", "auto", ...sounds.map((s) => s.id)];
  const label = (id: string) =>
    id === "none" ? "Off" : id === "auto" ? "Match the scene" : sounds.find((s) => s.id === id)?.label ?? id;
  return (
    <div>
      <div className="label">Nature sound</div>
      <ChipRow
        label="Nature sound"
        options={options}
        value={cfg.sound}
        onChange={(v) => set("sound", v)}
        render={label}
        className="text-xs"
      />
      {cfg.sound !== "none" && (
        <>
          <label className="label mt-3" htmlFor={`${idPrefix}-volume`}>
            Sound volume · {Math.round(cfg.sound_volume * 100)}%
          </label>
          <input
            id={`${idPrefix}-volume`}
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={cfg.sound_volume}
            onChange={(e) => set("sound_volume", Number(e.target.value))}
            className="w-full accent-iris"
          />
          <p className="text-[11px] text-ink-400 mt-1">
            Mixed under the music, or on its own with music set to &ldquo;No music&rdquo;.
          </p>
        </>
      )}
    </div>
  );
}
