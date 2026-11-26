"use client";
import clsx from "clsx";
import { Fragment, type ReactNode } from "react";
import type { CallPrice, SfxDensity, StoryEnhanceAction, StoryParse } from "@/lib/api";

/** Story styles (reading speed and mood). "adult" ones are hidden on a clean deployment. */
export const STORY_STYLES: { id: string; label: string; hint: string; adult?: boolean }[] = [
  { id: "asmr", label: "ASMR whisper", hint: "Close, soft, slow" },
  { id: "sleep", label: "Sleep story", hint: "Calm and drowsy" },
  { id: "meditation", label: "Meditation", hint: "Guided and slow" },
  { id: "cozy", label: "Cozy", hint: "Warm and gentle" },
  { id: "spooky", label: "Spooky", hint: "Quiet suspense" },
  { id: "drama", label: "Drama", hint: "Characters and tension" },
  { id: "adventure", label: "Adventure", hint: "Brisk and vivid" },
  { id: "podcast", label: "Podcast", hint: "Narrated, conversational" },
  { id: "romance", label: "Romance", hint: "Between adults", adult: true },
];

export const SFX_DENSITIES: { id: SfxDensity; label: string }[] = [
  { id: "none", label: "None" },
  { id: "low", label: "A few" },
  { id: "medium", label: "Some" },
  { id: "high", label: "Lots" },
];

export const ENHANCE_ACTIONS: { id: StoryEnhanceAction; label: string; hint: string }[] = [
  { id: "tighten", label: "Tighten", hint: "Cut repetition and filler" },
  { id: "lengthen", label: "Lengthen", hint: "Add description to reach N minutes" },
  { id: "asmr", label: "Whisper / ASMR", hint: "Add whisper tags and close detail" },
  { id: "sfx", label: "Add sound cues", hint: "Add [sfx: …] cues and ambience" },
  { id: "sleep", label: "Slow for sleep", hint: "Calmer wording and pauses" },
];

export const usd = (n: number) => (n > 0 && n < 0.01 ? "<$0.01" : `$${n.toFixed(2)}`);

export function fmtMinutes(seconds: number): string {
  const m = seconds / 60;
  if (m < 1) return `${Math.round(seconds)} s`;
  return `${m < 10 ? m.toFixed(1) : Math.round(m)} min`;
}

export function storyStatusLabel(status: string, task?: string | null): string {
  if (status === "scripting") return task === "enhance" ? "Enhancing" : "Writing";
  return (
    {
      draft: "Draft",
      ready: "Script ready",
      generating: "Generating",
      rendering: "Rendering",
      completed: "Finished",
      failed: "Stopped",
      cancelled: "Cancelled",
    } as Record<string, string>
  )[status] ?? status;
}

export function storyStatusTone(status: string): string {
  if (status === "failed") return "bg-red-500/15 text-red-300 border-red-500/40";
  if (status === "ready" || status === "completed") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
  if (status === "draft" || status === "cancelled") return "bg-ink-700 text-ink-200 border-ink-600";
  return "bg-amber-500/15 text-amber-300 border-amber-500/40";
}

export function StoryStatus({ status, task }: { status: string; task?: string | null }) {
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs", storyStatusTone(status))}>
      <span className="w-1.5 h-1.5 rounded-full bg-current" />
      {storyStatusLabel(status, task)}
    </span>
  );
}

/** A price line: "About $0.04 (7 writer calls)". */
export function PriceNote({ price, what = "writer call" }: { price: CallPrice | null | undefined; what?: string }) {
  if (!price) return <span className="text-ink-400">Working out the price…</span>;
  return (
    <span>
      About <strong className="text-ink-100">{usd(price.cost)}</strong> ({price.calls} {what}
      {price.calls === 1 ? "" : "s"})
    </span>
  );
}

// ---------- the script with its markup highlighted ----------

const TAG_SPLIT = /(\[[^\]\n]*\])/;
const IS_TAG = /^\[[^\]\n]*\]$/;

function inline(text: string, key: string): ReactNode[] {
  return text.split(TAG_SPLIT).map((part, i) => {
    if (!part) return null;
    if (IS_TAG.test(part)) {
      const sfx = /^\[\s*sfx\s*:/i.test(part);
      return (
        <span
          key={`${key}-${i}`}
          className={sfx ? "text-sky-300 bg-sky-500/10 rounded px-0.5" : "text-violet-300"}
        >
          {part}
        </span>
      );
    }
    return <Fragment key={`${key}-${i}`}>{part}</Fragment>;
  });
}

/** One line of the script, coloured by what it is (heading, cast, bed, cue, pause, speaker, narration). */
export function ScriptLine({ line, n, warn }: { line: string; n: number; warn?: string }) {
  const t = line.trim();
  let body: ReactNode;
  if (t.startsWith("#")) body = <span className="font-semibold text-ink-50">{line}</span>;
  else if (t.startsWith("//")) body = <span className="text-ink-500 italic">{line}</span>;
  else if (/^@cast\b/i.test(t)) body = <span className="text-emerald-300">{line}</span>;
  else if (/^@bed\b/i.test(t)) body = <span className="text-teal-300">{line}</span>;
  else if (/^\(\s*pause\b/i.test(t)) body = <span className="text-ink-400">{line}</span>;
  else {
    const m = /^([^\W\d_][\w .'’-]{0,39}?)(\s*(?:\[[^\]]*\]\s*)*):(\s.*|)$/u.exec(line);
    if (m && m[1] === m[1].toUpperCase()) {
      body = (
        <>
          <span className="font-semibold text-amber-200">{m[1]}</span>
          {inline(m[2], `t${n}`)}:{inline(m[3], `b${n}`)}
        </>
      );
    } else body = inline(line, `l${n}`);
  }
  return (
    <div className={clsx("flex gap-3", warn && "bg-amber-500/10")} title={warn}>
      <span className="w-10 shrink-0 select-none text-right text-ink-600">{n}</span>
      <span className="whitespace-pre-wrap break-words min-w-0">{body || " "}</span>
    </div>
  );
}

export function ScriptView({ text, parsed }: { text: string; parsed?: StoryParse | null }) {
  const warns = new Map<number, string>();
  for (const w of parsed?.warnings ?? []) warns.set(w.line, w.message);
  const lines = text.split("\n");
  return (
    <div className="font-mono text-[13px] leading-6 text-ink-200 overflow-x-auto">
      {lines.map((ln, i) => (
        <ScriptLine key={i} line={ln} n={i + 1} warn={warns.get(i + 1)} />
      ))}
    </div>
  );
}

// ---------- before / after ----------

/** A line diff (longest common subsequence), for an Enhance proposal. */
export function lineDiff(before: string, after: string): { op: "same" | "del" | "add"; text: string }[] {
  const a = before.split("\n");
  const b = after.split("\n");
  // Very long texts: show them whole rather than spend seconds on a table.
  if (a.length * b.length > 4_000_000) {
    return [...a.map((text) => ({ op: "del" as const, text })), ...b.map((text) => ({ op: "add" as const, text }))];
  }
  const dp: Uint32Array[] = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: { op: "same" | "del" | "add"; text: string }[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ op: "same", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ op: "del", text: a[i++] });
    else out.push({ op: "add", text: b[j++] });
  }
  while (i < a.length) out.push({ op: "del", text: a[i++] });
  while (j < b.length) out.push({ op: "add", text: b[j++] });
  return out;
}

export function DiffView({ before, after }: { before: string; after: string }) {
  const rows = lineDiff(before, after);
  return (
    <div className="font-mono text-[12.5px] leading-6 max-h-[28rem] overflow-auto rounded-lg border border-ink-700 bg-ink-950/60">
      {rows.map((r, k) => (
        <div
          key={k}
          className={clsx(
            "px-3 whitespace-pre-wrap break-words",
            r.op === "add" && "bg-emerald-500/10 text-emerald-200",
            r.op === "del" && "bg-red-500/10 text-red-300 line-through decoration-red-400/50"
          )}
        >
          <span className="select-none text-ink-500 mr-2">{r.op === "add" ? "+" : r.op === "del" ? "−" : " "}</span>
          {r.text || " "}
        </div>
      ))}
    </div>
  );
}
