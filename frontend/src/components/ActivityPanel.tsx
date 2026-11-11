"use client";
import Link from "next/link";
import clsx from "clsx";
import { Dialog } from "@/components/Dialog";
import { useNotifications } from "@/components/NotificationsProvider";
import type { AppEvent } from "@/lib/api";

/** The server sends naive UTC timestamps; read them as UTC. */
export function parseServerTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const withZone = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z";
  const t = Date.parse(withZone);
  return Number.isFinite(t) ? t : null;
}

export function relativeTime(iso: string | null | undefined): string {
  const t = parseServerTime(iso);
  if (t === null) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function statusIcon(ev: AppEvent): { icon: string; cls: string; label: string } {
  switch (ev.status) {
    case "completed":
      return ev.severity === "warning"
        ? { icon: "!", cls: "text-amber-300", label: "Done, with a warning" }
        : { icon: "✓", cls: "text-emerald-300", label: "Done" };
    case "failed":
      return { icon: "✕", cls: "text-red-300", label: "Failed" };
    case "cancelled":
      return { icon: "⊘", cls: "text-ink-400", label: "Cancelled" };
    case "started":
      return { icon: "▶", cls: "text-iris-soft", label: "Started" };
    default:
      return ev.severity === "warning"
        ? { icon: "!", cls: "text-amber-300", label: "Warning" }
        : { icon: "•", cls: "text-ink-300", label: "Progress" };
  }
}

/** The bell's panel: what is running now, recent events, mark all read, and
 *  the browser-notification switch. Opened from the bell in the SideNav or
 *  the phone top bar; rendered once by NotificationsProvider. */
export function ActivityPanel() {
  const n = useNotifications();
  if (!n) return null;
  const items = n.activity?.items ?? [];
  const studioJobs = n.activity?.studio.jobs ?? [];

  return (
    <Dialog title="Activity" onClose={n.closePanel} className="max-w-xl">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        {n.notifySupported ? (
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              className="accent-iris"
              checked={n.notifyOn}
              onChange={(e) => n.setNotifyOn(e.target.checked)}
            />
            <span>Notify me when things finish</span>
          </label>
        ) : (
          <span className="text-ink-400 text-xs">This browser can&apos;t show notifications.</span>
        )}
        <span className={clsx("text-xs", n.connected ? "text-emerald-300" : "text-ink-400")}>
          {n.connected ? "Live" : "Reconnecting…"}
        </span>
      </div>
      {n.notifyBlocked && (
        <p className="text-xs text-amber-300">
          Notifications are blocked for this site. Allow them in the browser&apos;s site settings, then switch this on.
        </p>
      )}
      {n.notifyOn && (
        <p className="text-xs text-ink-400">You&apos;ll get a notification when this tab is in the background.</p>
      )}

      <section aria-labelledby="activity-running">
        <h3 id="activity-running" className="text-xs uppercase tracking-wider text-ink-400 mb-2">
          In progress
        </h3>
        {items.length === 0 ? (
          <p className="text-sm text-ink-400">Nothing running.</p>
        ) : (
          <ul className="space-y-1.5">
            {items.map((it) => (
              <li key={`${it.kind}:${it.entity_type}:${it.entity_id}`}>
                <Link
                  href={it.url || "/"}
                  onClick={n.closePanel}
                  className="flex items-start gap-2 rounded-md px-2 py-1.5 -mx-2 hover:bg-ink-800/70"
                >
                  <span
                    aria-hidden
                    className="mt-1 inline-block w-3 h-3 shrink-0 rounded-full border-2 border-iris-soft border-t-transparent animate-spin"
                  />
                  <span className="flex-1 min-w-0">
                    <span className="block text-sm truncate">{it.title}</span>
                    <span className="block text-xs text-ink-400">
                      {it.stage}
                      {it.started_at ? ` · started ${relativeTime(it.started_at)}` : ""}
                    </span>
                    {it.kind === "studio_job" && studioJobs.length > 0 && (
                      <span className="block text-xs text-ink-400 truncate">
                        {studioJobs
                          .map((j) => j.label || j.prompt || `#${j.id}`)
                          .slice(0, 3)
                          .join(" · ")}
                      </span>
                    )}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="activity-recent">
        <div className="flex items-center justify-between gap-2 mb-2">
          <h3 id="activity-recent" className="text-xs uppercase tracking-wider text-ink-400">
            Recent
          </h3>
          <button
            type="button"
            className="text-xs text-ink-300 hover:text-white disabled:opacity-50"
            onClick={n.markAllRead}
            disabled={n.unread === 0 && n.recent.every((e) => e.read)}
          >
            Mark all read
          </button>
        </div>
        {n.recent.length === 0 ? (
          <p className="text-sm text-ink-400">{n.recentLoading ? "Loading…" : "Nothing yet."}</p>
        ) : (
          <ul className="space-y-1">
            {n.recent.map((ev) => {
              const s = statusIcon(ev);
              const unread = !ev.read && (ev.status === "completed" || ev.status === "failed");
              const body = (
                <>
                  <span aria-hidden className={clsx("w-4 shrink-0 text-center mt-0.5", s.cls)}>
                    {s.icon}
                  </span>
                  <span className="sr-only">{s.label}: </span>
                  <span className="flex-1 min-w-0">
                    <span className={clsx("block text-sm wrap-break-word", unread ? "text-white font-medium" : "text-ink-200")}>
                      {ev.title}
                      {unread && <span className="sr-only"> (unread)</span>}
                    </span>
                    {ev.detail && <span className="block text-xs text-ink-400 line-clamp-2 wrap-break-word">{ev.detail}</span>}
                  </span>
                  <span className="shrink-0 text-[11px] text-ink-400 mt-0.5 whitespace-nowrap">
                    {relativeTime(ev.created_at)}
                  </span>
                  {unread && <span aria-hidden className="mt-1.5 w-1.5 h-1.5 shrink-0 rounded-full bg-iris-soft" />}
                </>
              );
              return (
                <li key={ev.id}>
                  {ev.url ? (
                    <Link
                      href={ev.url}
                      onClick={n.closePanel}
                      className="flex items-start gap-2 rounded-md px-2 py-1.5 -mx-2 hover:bg-ink-800/70"
                    >
                      {body}
                    </Link>
                  ) : (
                    <div className="flex items-start gap-2 px-2 py-1.5 -mx-2">{body}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {n.hasMore && (
          <button
            type="button"
            className="btn-ghost text-xs mt-2"
            onClick={n.loadMore}
            disabled={n.recentLoading}
          >
            {n.recentLoading ? "Loading…" : "Load more"}
          </button>
        )}
      </section>
    </Dialog>
  );
}

function BellIcon() {
  return (
    <svg aria-hidden viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </svg>
  );
}

/** The bell button with the unread count (SideNav and the phone top bar). */
export function ActivityBell({ className }: { className?: string }) {
  const n = useNotifications();
  if (!n || !n.authed) return null;
  const running = n.activity?.items.length ?? 0;
  const label =
    `Activity` +
    (n.unread > 0 ? `, ${n.unread} unread` : "") +
    (running > 0 ? `, ${running} running` : "");
  return (
    <>
      <button
        type="button"
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={n.panelOpen}
        onClick={n.openPanel}
        className={clsx(
          "relative inline-flex items-center justify-center w-9 h-9 rounded-lg text-ink-200 hover:text-white hover:bg-ink-800/70 transition",
          className
        )}
      >
        <BellIcon />
        {running > 0 && n.unread === 0 && (
          <span aria-hidden className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-iris-soft animate-pulse" />
        )}
        {n.unread > 0 && (
          <span
            aria-hidden
            className="absolute -top-0.5 -right-0.5 min-w-4 h-4 px-1 rounded-full bg-accent text-[10px] leading-4 font-semibold text-ink-950 text-center"
          >
            {n.unread > 99 ? "99+" : n.unread}
          </span>
        )}
      </button>
      <span className="sr-only" aria-live="polite">
        {n.unread > 0 ? `${n.unread} unread in Activity` : ""}
      </span>
    </>
  );
}
