"use client";
import clsx from "clsx";
import type { ReactNode } from "react";
import type { AmbientConfig, AmbientScene, ScenePreset } from "@/lib/api";

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

/** Mirror of services.ambient._effective_unit_seconds so the creator can show
 *  the loop math live without a round trip. */
export function loopMath(cfg: AmbientConfig) {
  const base = cfg.loop_seconds * (cfg.source === "video" ? cfg.slow_factor : 1);
  const cf = cfg.source === "image_drift" ? 0 : cfg.crossfade_seconds;
  const unit = Math.max(1, base - cf);
  const repeats = Math.max(1, Math.ceil(cfg.total_seconds / unit));
  return { unit, repeats };
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
  children,
}: {
  palette?: [string, string];
  emoji?: string;
  className?: string;
  still?: boolean;
  children?: ReactNode;
}) {
  const [a, b] = palette || FALLBACK_PALETTE;
  return (
    <div className={clsx("relative overflow-hidden bg-ink-900", className)} aria-hidden>
      <div
        className={clsx("absolute -inset-[8%]", !still && "animate-breathe motion-reduce:animate-none")}
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
