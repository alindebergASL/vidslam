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
  useEffect(() => {
    api.listProjects().then(setProjects);
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
            <Link
              key={p.id}
              href={`/projects/${p.id}`}
              className="card p-4 hover:border-ink-600 transition"
            >
              <div className="text-sm font-medium">{p.title || "Untitled"}</div>
              <div className="text-xs text-ink-300 mt-1">
                {p.mode} · {p.target_duration_seconds}s · {p.aspect_ratio}
              </div>
              <StatusBadge status={p.status} className="mt-3" />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
