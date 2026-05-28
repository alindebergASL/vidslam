"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { StatusBadge } from "@/components/StatusBadge";
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
  const reload = () => api.listProjects().then(setProjects);
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
      {projects.length === 0 ? (
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
              <button
                className="absolute top-2 right-2 text-ink-400 hover:text-accent opacity-0 group-hover:opacity-100 transition text-xs"
                title="Delete project"
                onClick={async () => {
                  if (
                    !window.confirm(
                      `Delete project "${p.title || "Untitled"}"? This cannot be undone.`
                    )
                  )
                    return;
                  await api.deleteProject(p.id);
                  reload();
                }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
