"use client";
import { useEffect, useState } from "react";
import { api, ClipCheckSettings, Inspection } from "@/lib/api";

/** Rough price of one clip check: a fraction of a cent, so show more digits. */
export function checkCostText(n: number | null | undefined): string {
  if (n == null) return "";
  if (n <= 0) return "free";
  if (n < 0.01) return `about $${n.toFixed(3)}`;
  return `about $${n.toFixed(2)}`;
}

const TONE: Record<string, string> = {
  ok: "bg-emerald-500/10 border-emerald-500/40 text-emerald-300",
  problem: "bg-amber-500/10 border-amber-500/40 text-amber-300",
  unsure: "bg-ink-800 border-ink-600 text-ink-300",
  none: "bg-ink-800 border-ink-700 text-ink-400",
};

/** The chip's words and its tooltip for a verdict (null = never checked). */
export function inspectionLabel(ins: Inspection | null | undefined): { text: string; title: string; tone: string } {
  const when = ins?.checked_at ? ` Checked ${new Date(ins.checked_at).toLocaleString()}` : "";
  const by = ins?.model ? ` with ${ins.model}` : "";
  if (!ins) return { text: "Not checked", title: "The clip check hasn't looked at this clip.", tone: TONE.none };
  if (ins.verdict === "ok")
    return {
      text: "Looks right",
      title: `${ins.summary || "The clip check found nothing wrong."}${when}${by}.`,
      tone: TONE.ok,
    };
  if (ins.verdict === "problem") {
    const first = ins.issues[0]?.detail || ins.summary || "something looks off";
    const all = ins.issues.length
      ? ins.issues.map((i) => `• ${i.detail}`).join("\n")
      : ins.summary;
    return {
      text: `Check: ${first}`,
      title: `The clip check flagged:\n${all}${ins.summary && ins.issues.length ? `\n\n${ins.summary}` : ""}\n${when}${by}. It's advice only: the video is still put together with this clip.`,
      tone: TONE.problem,
    };
  }
  if (ins.verdict === "skipped")
    return { text: "Not checked", title: ins.summary || "The automatic check didn't run.", tone: TONE.none };
  return {
    text: "Couldn't check",
    title: `${ins.summary || "Couldn't check this clip"}.${when}${by}`,
    tone: TONE.unsure,
  };
}

export function InspectionChip({ inspection, className = "" }: { inspection: Inspection | null | undefined; className?: string }) {
  const l = inspectionLabel(inspection);
  return (
    <span
      className={`chip text-[10px] py-0.5 max-w-[16rem] truncate ${l.tone} ${className}`}
      title={l.title}
    >
      {l.text}
    </span>
  );
}

/** Clip check settings, fetched once per mount (cost per check, whether checks can run). */
export function useClipCheckSettings(): ClipCheckSettings | null {
  const [s, setS] = useState<ClipCheckSettings | null>(null);
  useEffect(() => {
    let live = true;
    api
      .clipCheckSettings()
      .then((r) => live && setS(r))
      .catch(() => live && setS(null));
    return () => {
      live = false;
    };
  }, []);
  return s;
}

/** Why a manual check can't run right now (null = it can). */
export function checkBlockedWhy(s: ClipCheckSettings | null): string | null {
  if (s && !s.available) return "Add an OpenRouter key in Settings → API keys to check clips.";
  if (s && s.provider === "unavailable") return "The clip check's model is switched off in Settings → API keys.";
  return null;
}
