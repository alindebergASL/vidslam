import clsx from "clsx";
import type { PublishState } from "@/lib/api";

/** UTC ISO string from the backend (naive) → local "Mar 3, 4:05 PM". */
export function fmtLocal(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "";
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return "";
  return withTime
    ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
    : d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

/** Plain-language label for what YouTube says about an upload. */
export function publishStateLabel(state: PublishState | string, scheduledFor?: string | null): string {
  switch (state) {
    case "uploading":
      return "Uploading";
    case "published":
      return "Published";
    case "scheduled":
      return scheduledFor ? `Scheduled for ${fmtLocal(scheduledFor)}` : "Scheduled";
    case "private":
      return "Private";
    case "processing":
      return "Processing on YouTube";
    case "removed":
      return "Removed on YouTube";
    case "failed":
      return "Failed";
    default:
      return String(state);
  }
}

export function PublishStateBadge({
  state,
  scheduledFor,
  className,
}: {
  state: PublishState | string;
  scheduledFor?: string | null;
  className?: string;
}) {
  const tone =
    state === "failed" || state === "removed"
      ? "border-red-500/40 text-red-300 bg-red-500/10"
      : state === "published" || state === "scheduled"
      ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
      : state === "private"
      ? "border-ink-600 text-ink-200 bg-ink-800"
      : "border-amber-500/40 text-amber-300 bg-amber-500/10";
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs", tone, className)}>
      <span className="w-1.5 h-1.5 rounded-full bg-current shrink-0" aria-hidden />
      {publishStateLabel(state, scheduledFor)}
    </span>
  );
}

/** Amber notice for a mismatch between what was asked and what YouTube did. */
export function PublishWarning({ text, className }: { text: string; className?: string }) {
  if (!text) return null;
  return (
    <div
      role="note"
      className={clsx("rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200", className)}
    >
      {text}
    </div>
  );
}
