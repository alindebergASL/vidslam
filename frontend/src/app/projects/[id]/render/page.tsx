"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { AuthGate } from "@/components/AuthGate";
import { api, Project, Render } from "@/lib/api";

export default function RenderPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const params = useParams();
  const search = useSearchParams();
  const projectId = Number(params?.id);
  const explicitRenderId = search?.get("id") ? Number(search.get("id")) : null;
  const [project, setProject] = useState<Project | null>(null);
  const [render, setRender] = useState<Render | null>(null);
  const [allRenders, setAllRenders] = useState<Render[]>([]);

  useEffect(() => {
    api.getProject(projectId).then(setProject);
    api.listRenders(projectId).then((rs) => {
      setAllRenders(rs);
      if (explicitRenderId) {
        const match = rs.find((r) => r.id === explicitRenderId);
        setRender(match || rs[0] || null);
      } else {
        setRender(rs[0] || null);
      }
    });
  }, [projectId, explicitRenderId]);

  if (!project) return <div className="p-8 text-ink-300">Loading…</div>;

  return (
    <div className="p-8 max-w-3xl">
      <Link
        href={`/projects/${projectId}`}
        className="text-xs text-ink-300 hover:text-ink-100"
      >
        ← {project.title || "Project"}
      </Link>
      <h1 className="text-3xl font-semibold tracking-tight mt-1 mb-2">
        Render{render ? ` · v${allRenders.length - allRenders.findIndex((r) => r.id === render.id)}` : ""}
      </h1>
      {allRenders.length > 1 && (
        <div className="flex flex-wrap gap-2 mb-6">
          {allRenders.map((r, idx) => {
            const isCurrent = render?.id === r.id;
            return (
              <Link
                key={r.id}
                href={`/projects/${projectId}/render?id=${r.id}`}
                className={`chip ${isCurrent ? "chip-active" : ""}`}
              >
                v{allRenders.length - idx}
                <span className="text-[10px] text-ink-400">
                  {new Date(r.created_at).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
              </Link>
            );
          })}
        </div>
      )}

      {!render || render.status !== "completed" ? (
        <div className="card p-6 text-ink-300">
          {render?.status || "no render yet"}
        </div>
      ) : (
        <>
          <div className="card overflow-hidden mb-4 bg-black">
            <video
              src={api.renderDownload(render.id)}
              controls
              playsInline
              className="w-full max-h-[80vh] mx-auto"
              poster={api.renderThumb(render.id)}
            />
          </div>
          <div className="flex flex-wrap gap-3">
            <a
              href={api.renderDownload(render.id)}
              className="btn-primary"
              download={`avatar-video-${render.id}.mp4`}
            >
              Download MP4
            </a>
            <Link href={`/projects/${projectId}`} className="btn-ghost">
              Edit shots
            </Link>
            <button
              className="btn-ghost"
              onClick={async () => {
                const dup = await api.createProject({
                  title: `${project.title} (copy)`,
                  original_script: project.original_script,
                  mode: project.mode,
                  aspect_ratio: project.aspect_ratio,
                  target_duration_seconds: project.target_duration_seconds,
                  cta_text: project.cta_text,
                  caption_style: project.caption_style,
                  include_disclosure: project.include_disclosure,
                  primary_avatar_id: project.primary_avatar_id,
                  cast: project.cast_members,
                });
                window.location.href = `/projects/${dup.id}`;
              }}
            >
              Duplicate project
            </button>
          </div>
        </>
      )}
    </div>
  );
}
