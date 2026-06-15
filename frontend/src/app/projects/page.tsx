"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { StatusBadge } from "@/components/StatusBadge";
import { useToast } from "@/components/Toaster";
import { api, Project } from "@/lib/api";

export default function ProjectsPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loaded, setLoaded] = useState(false);
  const toast = useToast();
  const reload = () =>
    api.listProjects().then((rows) => {
      setProjects(rows);
      setLoaded(true);
    });
  useEffect(() => {
    reload();
  }, []);
  return (
    <div className="p-8 max-w-6xl">
      <header className="mb-6 flex items-end justify-between">
        <h1 className="text-3xl font-semibold tracking-tight">Projects</h1>
        <Link href="/projects/new" className="btn-primary">
          + New Project
        </Link>
      </header>
      {!loaded ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" aria-hidden>
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="card p-4 space-y-3">
              <div className="h-4 bg-ink-800 rounded animate-pulse w-2/3" />
              <div className="h-3 bg-ink-800 rounded animate-pulse w-1/3" />
              <div className="h-5 bg-ink-800 rounded animate-pulse w-20" />
            </div>
          ))}
        </div>
      ) : projects.length === 0 ? (
        <div className="card p-8 text-ink-300 text-sm text-center">No projects yet.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {projects.map((p) => (
            <div key={p.id} className="card p-4 hover:border-ink-600 transition group relative">
              <Link href={`/projects/${p.id}`} className="block">
                <div className="text-sm font-medium pr-6">{p.title || "Untitled"}</div>
                <div className="text-xs text-ink-300 mt-1">
                  {p.mode} · {p.target_duration_seconds}s · {p.aspect_ratio}
                </div>
                <StatusBadge status={p.status} className="mt-3" />
              </Link>
              {/* Always-visible icon buttons (touch-friendly); aria-labels
                  give screen readers something to announce beyond the glyph. */}
              <div className="absolute top-2 right-2 flex items-center gap-2 opacity-70 md:opacity-0 md:group-hover:opacity-100 focus-within:opacity-100 transition">
                <button
                  className="text-ink-400 hover:text-ink-100 text-xs"
                  title="Duplicate project"
                  aria-label={`Duplicate project "${p.title || "Untitled"}"`}
                  onClick={async () => {
                    try {
                      const dup = await api.duplicateProject(p);
                      window.location.href = `/projects/${dup.id}`;
                    } catch (e: any) {
                      toast.error(e.message || "Duplicate failed");
                    }
                  }}
                >
                  ⧉
                </button>
                <button
                  className="text-ink-400 hover:text-accent text-xs"
                  title="Delete project"
                  aria-label={`Delete project "${p.title || "Untitled"}"`}
                  onClick={async () => {
                    if (
                      !window.confirm(
                        `Delete project "${p.title || "Untitled"}"? This cannot be undone.`
                      )
                    )
                      return;
                    try {
                      await api.deleteProject(p.id);
                      toast.success(`Deleted "${p.title || "Untitled"}"`);
                      reload();
                    } catch (e: any) {
                      toast.error(e.message || "Delete failed");
                    }
                  }}
                >
                  ✕
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
