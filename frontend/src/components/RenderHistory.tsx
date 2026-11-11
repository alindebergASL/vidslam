"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, Render, renderLabel } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";
import { useEvents, useLiveConnected } from "./NotificationsProvider";
import { usePoll } from "@/lib/usePoll";
import { statusLabel } from "@/lib/labels";

const DONE = ["completed", "failed", "cancelled", "expired"];

export function RenderHistory({ projectId }: { projectId: number }) {
  const [renders, setRenders] = useState<Render[]>([]);

  const reload = () =>
    api
      .listRenders(projectId)
      .then(setRenders)
      .catch(() => {
        /* keep what is shown */
      });
  useEffect(() => {
    reload();
  }, [projectId]);
  // While the live stream is connected, a render event for this project
  // refetches and a slow fallback runs only while a render is in flight.
  // Without the stream nothing would announce a new render, so a slow poll
  // keeps running.
  const connected = useLiveConnected();
  const active = renders.some((r) => !DONE.includes(r.status));
  const poke = usePoll(reload, { enabled: active || !connected, interval: 8000, liveInterval: 15_000 });
  useEvents(
    (ev) =>
      ev.project_id === projectId &&
      (ev.kind === "render" || ev.kind === "ambient_render" || ev.kind === "hook_lab"),
    () => poke()
  );

  if (renders.length === 0) {
    return null;
  }

  const completedCount = renders.filter((r) => r.status === "completed").length;
  // The newest render of the video itself (other openings are its variants).
  const latest = renders.find((r) => !r.hook_variant_id) ?? renders[0];

  return (
    <section className="mb-6">
      <div className="flex items-baseline justify-between mb-3 gap-3">
        <h2 className="text-lg font-medium">Renders</h2>
        <div className="flex items-baseline gap-3">
          <span className="text-xs text-ink-400 hidden sm:inline">
            A new version is created every time you re-compose or regenerate a shot.
          </span>
          {completedCount > 0 && (
            <a
              href={api.exportRendersUrl(projectId)}
              className="btn-ghost text-xs whitespace-nowrap"
              title="Download every completed render (MP4 + thumbnail) as a zip"
            >
              Export all ({completedCount})
            </a>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        {renders.map((r) => {
          const completed = r.status === "completed";
          const isLatest = r.id === latest.id;
          const label = renderLabel(r);
          const canCompare =
            !isLatest && completed && latest && latest.status === "completed";
          return (
            <div
              key={r.id}
              className="card overflow-hidden card-hover flex flex-col relative"
            >
              <Link
                href={`/projects/${projectId}/render?id=${r.id}`}
                className="block"
              >
                <div className="aspect-9/16 max-h-44 bg-ink-800 relative">
                  {completed ? (
                    <img
                      src={api.renderThumb(r.id)}
                      alt={label}
                      className="w-full h-full object-cover"
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center text-ink-400 text-xs px-2 text-center">
                      {statusLabel(r.status)}
                    </div>
                  )}
                  {isLatest && (
                    <span className="absolute top-2 left-2 chip text-[10px] py-0.5 grad border-transparent text-ink-950">
                      Latest
                    </span>
                  )}
                </div>
                <div className="p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium min-w-0 truncate" title={label}>{label}</span>
                    <StatusBadge status={r.status} />
                  </div>
                  <div className="text-[10px] text-ink-400 mt-1">
                    {new Date(r.created_at).toLocaleString()}
                  </div>
                  {r.error && (
                    <div className="text-[10px] text-accent mt-1 truncate" title={r.error}>
                      {r.error}
                    </div>
                  )}
                </div>
              </Link>
              {canCompare && (
                <Link
                  href={`/projects/${projectId}/render?id=${latest.id}&compare=${r.id}`}
                  className="absolute top-2 right-2 chip text-[10px] py-0.5 bg-ink-900/80 border-ink-700 hover:border-accent hover:text-accent transition"
                  title={`Compare ${label} side by side with the latest version`}
                  aria-label={`Compare ${label} with the latest version`}
                  onClick={(e) => e.stopPropagation()}
                >
                  ⇄ vs latest
                </Link>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
