"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, Render } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";

export function RenderHistory({ projectId }: { projectId: number }) {
  const [renders, setRenders] = useState<Render[]>([]);

  const reload = () => api.listRenders(projectId).then(setRenders);
  useEffect(() => {
    reload();
    const t = setInterval(reload, 4000);
    return () => clearInterval(t);
  }, [projectId]);

  if (renders.length === 0) {
    return null;
  }

  const completedCount = renders.filter((r) => r.status === "completed").length;

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
        {renders.map((r, idx) => {
          const completed = r.status === "completed";
          const isLatest = idx === 0;
          return (
            <Link
              key={r.id}
              href={`/projects/${projectId}/render?id=${r.id}`}
              className="card overflow-hidden hover:border-ink-600 transition flex flex-col"
            >
              <div className="aspect-[9/16] max-h-44 bg-ink-800 relative">
                {completed ? (
                  <img
                    src={api.renderThumb(r.id)}
                    alt={`Render ${r.id}`}
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-ink-400 text-xs px-2 text-center">
                    {r.status}
                  </div>
                )}
                {isLatest && (
                  <span className="absolute top-2 left-2 chip text-[10px] py-0.5 bg-accent border-accent text-white">
                    Latest
                  </span>
                )}
              </div>
              <div className="p-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium">v{renders.length - idx}</span>
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
          );
        })}
      </div>
    </section>
  );
}
