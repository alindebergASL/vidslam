"use client";
import clsx from "clsx";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import type {
  AmbientConfig,
  AmbientPresets,
  AmbientScene,
  Corner,
  LoopPlan,
  ModelChoice,
  PhotoRewrite,
  ScenePreset,
} from "@/lib/api";

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
      {(plan.variants ?? 1) > 1 && (
        <div className="text-xs text-ink-200">
          <span className="text-grad font-semibold">{plan.variants}</span> variations
          {plan.variant_labels?.length ? ` (${plan.variant_labels.join(" → ")})` : ""}
          {plan.repeat_every_seconds && plan.repeat_every_seconds > plan.unit_seconds
            ? ` · the picture repeats every ${fmtDuration(Math.round(plan.repeat_every_seconds))}`
            : ""}
        </div>
      )}
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
      {times.length > 0 && cfg.variety === "day_night" ? (
        <div>
          <div className="label">Time of day</div>
          <p className="text-xs text-ink-400">Set by Day to night (Less repetition, below).</p>
        </div>
      ) : times.length > 0 && (
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

const VARIETY: { id: AmbientConfig["variety"]; label: string; desc: string }[] = [
  { id: "none", label: "One loop", desc: "The same loop, repeated for the whole video." },
  {
    id: "takes",
    label: "Mix of takes",
    desc: "Several clips of the scene, played in turn, so the picture repeats far less often.",
  },
  {
    id: "day_night",
    label: "Day to night",
    desc: "The same place as the light changes, from day into night, with a slow dissolve.",
  },
];

/** Less repetition: one loop, several takes, or day → night. */
export function VarietyControls({
  cfg,
  set,
  labels,
}: {
  cfg: AmbientConfig;
  set: CfgSetter;
  labels?: string[];
}) {
  // Takes need a video model; a drift scene is one still.
  const options = VARIETY.filter((v) => v.id !== "takes" || cfg.source === "video");
  const current = VARIETY.find((v) => v.id === cfg.variety) ?? VARIETY[0];
  return (
    <div>
      <div className="label">Less repetition</div>
      <ChipRow
        label="Less repetition"
        options={options.map((v) => v.id)}
        value={cfg.variety}
        onChange={(v) => set("variety", v)}
        render={(id) => VARIETY.find((v) => v.id === id)?.label ?? id}
        className="text-xs"
      />
      <p className="text-xs text-ink-400 mt-2">{current.desc}</p>
      {cfg.variety !== "none" && (
        <div className="mt-3">
          <div className="label">Variations</div>
          <ChipRow
            label="Number of variations"
            options={[2, 3, 4, 5]}
            value={cfg.variations}
            onChange={(v) => set("variations", v)}
            className="text-xs"
          />
          <p className="text-xs text-ink-400 mt-2">
            {labels?.length ? `${labels.join(" → ")}. ` : ""}
            Each variation is its own loop clip, so the video model cost is ×{cfg.variations}. The full-length
            render is still free.
          </p>
        </div>
      )}
    </div>
  );
}

const CORNERS: { id: Corner; label: string }[] = [
  { id: "top_left", label: "Top left" },
  { id: "top_right", label: "Top right" },
  { id: "bottom_left", label: "Bottom left" },
  { id: "bottom_right", label: "Bottom right" },
];

/** Title, watermark and timer drawn over the video (render-time). */
export function OverlayControls({
  cfg,
  set,
  idPrefix = "overlay",
}: {
  cfg: AmbientConfig;
  set: CfgSetter;
  idPrefix?: string;
}) {
  return (
    <div className="space-y-3">
      <div>
        <label className="label" htmlFor={`${idPrefix}-title`}>
          Title
        </label>
        <input
          id={`${idPrefix}-title`}
          className="input w-full"
          maxLength={120}
          placeholder="None — e.g. Rainy forest · 1 hour"
          value={cfg.overlay_title}
          onChange={(e) => set("overlay_title", e.target.value)}
        />
        {cfg.overlay_title.trim() && (
          <>
            <label className="label mt-2" htmlFor={`${idPrefix}-title-secs`}>
              Shown for the first {cfg.title_seconds}s
            </label>
            <input
              id={`${idPrefix}-title-secs`}
              type="range"
              min={2}
              max={30}
              step={1}
              value={cfg.title_seconds}
              onChange={(e) => set("title_seconds", Number(e.target.value))}
              className="w-full accent-iris"
            />
          </>
        )}
      </div>
      <div>
        <label className="label" htmlFor={`${idPrefix}-wm`}>
          Watermark
        </label>
        <input
          id={`${idPrefix}-wm`}
          className="input w-full"
          maxLength={60}
          placeholder="None — e.g. @yourchannel"
          value={cfg.watermark}
          onChange={(e) => set("watermark", e.target.value)}
        />
        {cfg.watermark.trim() && (
          <ChipRow
            label="Watermark position"
            options={CORNERS.map((c) => c.id)}
            value={cfg.watermark_position}
            onChange={(v) => set("watermark_position", v)}
            render={(id) => CORNERS.find((c) => c.id === id)?.label ?? id}
            className="text-xs mt-2"
          />
        )}
      </div>
      <div>
        <div className="label">Timer</div>
        <ChipRow
          label="Timer"
          options={["none", "countdown", "elapsed"] as AmbientConfig["timer"][]}
          value={cfg.timer}
          onChange={(v) => set("timer", v)}
          render={(v) => (v === "none" ? "None" : v === "countdown" ? "Countdown" : "Elapsed")}
          className="text-xs"
        />
        {cfg.timer !== "none" && (
          <>
            <ChipRow
              label="Timer position"
              options={CORNERS.map((c) => c.id)}
              value={cfg.timer_position}
              onChange={(v) => set("timer_position", v)}
              render={(id) => CORNERS.find((c) => c.id === id)?.label ?? id}
              className="text-xs mt-2"
            />
            <p className="text-xs text-ink-400 mt-2">
              The clock changes every second, so the finished video is re-encoded once: rendering takes several
              minutes longer per hour. No provider cost.
            </p>
          </>
        )}
      </div>
      <p className="text-[11px] text-ink-400">Title and watermark are free; changing overlays only needs a re-render.</p>
    </div>
  );
}

/** Status of each variation's clip on the scene page. */
export function VariantStrip({ scene }: { scene: AmbientScene }) {
  const labels = scene.plan.variant_labels ?? [];
  if (labels.length < 2 || !scene.loop_shot) return null;
  const shots = [scene.loop_shot, ...(scene.variant_shots ?? [])];
  // The first clip's status reads "completed" only once every variation is done.
  const done = (i: number) => (i === 0 ? !!shots[0]?.clip_path : shots[i]?.status === "completed");
  return (
    <ul className="flex flex-wrap gap-1.5 text-[11px]" aria-label="Variations">
      {labels.map((label, i) => (
        <li
          key={label}
          className={clsx(
            "rounded-full border px-2 py-0.5",
            done(i)
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
              : shots[i]?.status === "failed"
              ? "border-red-500/40 bg-red-500/10 text-red-200"
              : "border-ink-600 text-ink-300"
          )}
        >
          {done(i) ? "✓ " : ""}
          {label}
        </li>
      ))}
    </ul>
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

/** SHA-1 of a string's UTF-8 bytes, hex. Synchronous (crypto.subtle is
 *  async and missing outside secure contexts), for matching the backend's
 *  rewrite fingerprint while rendering. */
function sha1Hex(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const len = bytes.length;
  const words = new Uint32Array((((len + 8) >> 6) + 1) * 16);
  for (let i = 0; i < len; i++) words[i >> 2] |= bytes[i] << (24 - (i % 4) * 8);
  words[len >> 2] |= 0x80 << (24 - (len % 4) * 8);
  words[words.length - 1] = len * 8; // strings here are far below 512 MB
  let [h0, h1, h2, h3, h4] = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0];
  const w = new Uint32Array(80);
  const rotl = (x: number, n: number) => (x << n) | (x >>> (32 - n));
  for (let off = 0; off < words.length; off += 16) {
    for (let t = 0; t < 16; t++) w[t] = words[off + t];
    for (let t = 16; t < 80; t++) w[t] = rotl(w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16], 1);
    let [a, b, c, d, e] = [h0, h1, h2, h3, h4];
    for (let t = 0; t < 80; t++) {
      const [f, k] =
        t < 20 ? [(b & c) | (~b & d), 0x5a827999]
        : t < 40 ? [b ^ c ^ d, 0x6ed9eba1]
        : t < 60 ? [(b & c) | (b & d) | (c & d), 0x8f1bbcdc]
        : [b ^ c ^ d, 0xca62c1d6];
      const tmp = (rotl(a, 5) + f + e + k + w[t]) >>> 0;
      e = d;
      d = c;
      c = rotl(b, 30) >>> 0;
      b = a;
      a = tmp;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
  }
  return [h0, h1, h2, h3, h4].map((h) => h.toString(16).padStart(8, "0")).join("");
}

/** services.ambient._text_fingerprint: which text a stored rewrite was made from. */
export function textFingerprint(text: string): string {
  return sha1Hex(text.trim()).slice(0, 16);
}

const OWN_PICTURE = ["upload", "studio", "asset"];

/** Whether a photo rewrite is made for this config (services.ambient.ensure_photo_rewrite). */
export function rewriteApplies(cfg: AmbientConfig): boolean {
  return (
    cfg.look === "photoreal" && cfg.rewrite_prompt && !!cfg.scene_prompt.trim() && !OWN_PICTURE.includes(cfg.picture_origin)
  );
}

/** The stored rewrite when it was made from exactly this text (services.ambient.active_rewrite). */
export function activeRewrite(cfg: AmbientConfig): PhotoRewrite | null {
  const rw = cfg.photo;
  if (!rewriteApplies(cfg) || !rw || !rw.scene.trim() || rw.source !== textFingerprint(cfg.scene_prompt)) return null;
  return rw;
}

/** "Rewrite it as a real photograph" for a typed description (photoreal only),
 *  and, on a saved scene, what the image model actually receives. */
export function PhotoRewriteControl({
  cfg,
  set,
  idPrefix = "rewrite",
  saved,
}: {
  cfg: AmbientConfig;
  set: CfgSetter;
  idPrefix?: string;
  /** The saved scene's config: shows what the picture model reads while it matches. */
  saved?: AmbientConfig;
}) {
  // A picture you brought (not a generated preview) sets the scene; the text only describes motion.
  const ownPicture = OWN_PICTURE.includes(cfg.picture_origin);
  if (cfg.look !== "photoreal" || !cfg.scene_prompt.trim() || ownPicture) return null;
  // Only a saved rewrite of exactly this text is what the picture model reads:
  // one kept from older text (say, saved under another look) waits for a new one.
  const rw = saved ? activeRewrite({ ...cfg, photo: saved.photo }) : null;
  const current = !!saved && saved.rewrite_prompt && !!rw;
  const chosenLight = cfg.time_of_day !== "any" || cfg.weather !== "any";
  const showResult = !!saved;
  return (
    <div className="mt-2 space-y-1.5">
      <label className="flex items-start gap-2 text-xs text-ink-200" htmlFor={`${idPrefix}-on`}>
        <input
          id={`${idPrefix}-on`}
          type="checkbox"
          className="accent-iris mt-0.5"
          checked={cfg.rewrite_prompt}
          onChange={(e) => set("rewrite_prompt", e.target.checked)}
        />
        <span>
          Rewrite it as a real photograph for the picture model (recommended). Words like
          &ldquo;magical&rdquo;, &ldquo;8k&rdquo; or &ldquo;turquoise&rdquo; make images look AI-made; your text is kept as typed.
        </span>
      </label>
      {showResult && current && rw && (
        <p className="text-xs text-ink-400 border-l-2 border-ink-700 pl-2">
          <span className="text-ink-300">The picture model reads:</span> {rw.scene}{" "}
          {chosenLight ? "(light from your time of day and weather)" : rw.light} {rw.camera}
        </p>
      )}
      {showResult && !current && cfg.rewrite_prompt && (
        <p className="text-xs text-ink-400">The rewrite is made when you save.</p>
      )}
    </div>
  );
}

/** A catalog model, as the model dropdowns list it. */
export type ModelOption = { id: string; name: string };

function choiceLabel(c: ModelChoice): string {
  const tags = [c.recommended && "recommended", c.experimental && "experimental"].filter(Boolean).join(", ");
  return tags ? `${c.label} (${tags})` : c.label;
}

function others(list: ModelOption[], curated: ModelChoice[]): ModelOption[] {
  return list.filter((m) => !curated.some((c) => c.id === m.id));
}

/** Picture (still) model and video model for a scene: a few suggested
 *  choices with notes first, then every other model in the catalog. */
export function ModelControls({
  cfg,
  set,
  presets,
  imageModels,
  videoModels,
  idPrefix = "models",
}: {
  cfg: AmbientConfig;
  set: CfgSetter;
  presets: AmbientPresets | null;
  imageModels: ModelOption[];
  videoModels: ModelOption[];
  idPrefix?: string;
}) {
  const stills = presets?.still_models ?? [];
  const videos = presets?.video_models ?? [];
  const drift = cfg.source === "image_drift";
  // Older Still + drift scenes kept their image model in model_override; a
  // video model left there from before the switch is ignored for stills
  // (services.ambient.still_model_chain), so don't show it as the Picture model.
  const legacyIsVideo =
    !!cfg.model_override &&
    (videos.some((c) => c.id === cfg.model_override) ||
      videoModels.some((m) => m.id === cfg.model_override) ||
      cfg.model_override === presets?.default_video_model);
  const stillValue = cfg.still_model || (drift && !legacyIsVideo ? cfg.model_override : "");
  const defaultStill = stills.find((c) => c.id === presets?.default_still_model);
  const defaultVideo = videos.find((c) => c.id === presets?.default_video_model);
  const stillChoice = stills.find((c) => c.id === stillValue) ?? (stillValue ? undefined : defaultStill);
  const videoChoice = videos.find((c) => c.id === cfg.model_override) ?? (cfg.model_override ? undefined : defaultVideo);
  const known = (v: string, curated: ModelChoice[], list: ModelOption[]) =>
    !v || curated.some((c) => c.id === v) || list.some((m) => m.id === v);
  const dayNight = cfg.variety === "day_night" && cfg.variations > 1;
  const usage = !cfg.picture_origin
    ? ""
    : dayNight
      ? "Makes the other times of day from your picture."
      : cfg.picture_origin === "generated"
        ? "Used when you regenerate the scene picture."
        : "Not used: the scene starts from your picture.";
  const stillHelp = [
    usage,
    usage.startsWith("Not used") ? "" : stillChoice?.note,
    usage.startsWith("Not used") || stillValue ? "" : "If a call fails, the scene retries with the cheaper model.",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <div>
        <label className="label" htmlFor={`${idPrefix}-still`}>
          Picture model
        </label>
        <select
          id={`${idPrefix}-still`}
          className="input"
          value={stillValue}
          onChange={(e) => {
            set("still_model", e.target.value);
            if (drift && cfg.model_override) set("model_override", "");
          }}
        >
          <option value="">Recommended{defaultStill ? ` · ${defaultStill.label}` : ""}</option>
          {stills.length > 0 && (
            <optgroup label="Suggested">
              {stills.map((c) => (
                <option key={c.id} value={c.id}>
                  {choiceLabel(c)}
                </option>
              ))}
            </optgroup>
          )}
          {others(imageModels, stills).length > 0 && (
            <optgroup label="All image models">
              {others(imageModels, stills).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </optgroup>
          )}
          {!known(stillValue, stills, imageModels) && <option value={stillValue}>{stillValue}</option>}
        </select>
        {stillHelp && <p className="text-xs text-ink-400 mt-1">{stillHelp}</p>}
      </div>
      {!drift && (
        <div>
          <label className="label" htmlFor={`${idPrefix}-video`}>
            Video model
          </label>
          <select
            id={`${idPrefix}-video`}
            className="input"
            value={cfg.model_override}
            onChange={(e) => set("model_override", e.target.value)}
          >
            <option value="">
              Default{defaultVideo ? ` · ${defaultVideo.label}` : presets?.default_video_model ? ` · ${presets.default_video_model}` : ""}
            </option>
            {videos.length > 0 && (
              <optgroup label="Suggested">
                {videos.map((c) => (
                  <option key={c.id} value={c.id}>
                    {choiceLabel(c)}
                  </option>
                ))}
              </optgroup>
            )}
            {others(videoModels, videos).length > 0 && (
              <optgroup label="All video models">
                {others(videoModels, videos).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </optgroup>
            )}
            {!known(cfg.model_override, videos, videoModels) && (
              <option value={cfg.model_override}>{cfg.model_override}</option>
            )}
          </select>
          {videoChoice?.note && <p className="text-xs text-ink-400 mt-1">{videoChoice.note}</p>}
        </div>
      )}
    </div>
  );
}
