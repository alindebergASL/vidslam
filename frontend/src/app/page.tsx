"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { StatusBadge } from "@/components/StatusBadge";
import { api, Avatar, Ingredient, Project } from "@/lib/api";

export default function DashboardPage() {
  return (
    <AuthGate>
      <Dashboard />
    </AuthGate>
  );
}

function Dashboard() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [providerStatus, setProviderStatus] = useState<Record<string, any> | null>(null);

  useEffect(() => {
    api.listProjects().then(setProjects);
    api.listAvatars().then(setAvatars);
    api.listIngredients().then(setIngredients);
    api.providerStatus().then(setProviderStatus);
  }, []);

  return (
    <div className="p-8 max-w-6xl">
      <header className="mb-8 flex items-end justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-ink-300 mt-1">
            Build a cast, generate assets in the Studio, then turn them into short-form video.
          </p>
        </div>
        <Link href="/projects/new" className="btn-primary">
          + New Project
        </Link>
      </header>

      {providerStatus && (
        <div className="card p-4 mb-8 flex flex-wrap gap-3 text-sm">
          {Object.entries(providerStatus).map(([k, v]) => (
            <span key={k} className="chip">
              <span className="text-ink-300">{k}:</span>
              <span className={v === "mock" || v === true ? "text-ink-200" : "text-accent"}>
                {String(v)}
              </span>
            </span>
          ))}
        </div>
      )}

      <section className="mb-10">
        <h2 className="text-lg font-medium mb-3">Cast</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Tile href="/cast" label="Characters" count={avatars.length} />
          <Tile
            href="/cast?filter=scene"
            label="Scenes"
            count={ingredients.filter((i) => i.kind === "scene").length}
          />
          <Tile
            href="/cast?filter=style"
            label="Styles"
            count={ingredients.filter((i) => i.kind === "style").length}
          />
          <Tile
            href="/cast?filter=object"
            label="Objects & Props"
            count={ingredients.filter((i) => i.kind === "object" || i.kind === "prop").length}
          />
        </div>
      </section>

      <section>
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-lg font-medium">Recent projects</h2>
          <Link href="/projects" className="text-sm text-ink-300 hover:text-ink-100">
            View all →
          </Link>
        </div>
        {projects.length === 0 ? (
          <div className="card p-6 text-ink-300 text-sm">
            No projects yet. Run <code className="text-ink-100">make seed</code> for a demo,
            or click <em>New Project</em>.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {projects.slice(0, 6).map((p) => (
              <Link
                href={`/projects/${p.id}`}
                key={p.id}
                className="card p-4 hover:border-ink-600 transition"
              >
                <div className="text-sm font-medium">{p.title || "Untitled"}</div>
                <div className="text-xs text-ink-300 mt-1">
                  {p.mode} · {p.target_duration_seconds}s
                </div>
                <StatusBadge status={p.status} className="mt-3" />
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function Tile({ href, label, count }: { href: string; label: string; count: number }) {
  return (
    <Link href={href} className="card p-4 hover:border-ink-600 transition">
      <div className="text-2xl font-semibold">{count}</div>
      <div className="text-sm text-ink-300 mt-1">{label}</div>
    </Link>
  );
}

