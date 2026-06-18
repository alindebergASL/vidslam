"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { AuthGate } from "@/components/AuthGate";
import { useToast } from "@/components/Toaster";
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
  const compareRenderId = search?.get("compare") ? Number(search.get("compare")) : null;
  const [project, setProject] = useState<Project | null>(null);
  const [render, setRender] = useState<Render | null>(null);
  const [compareRender, setCompareRender] = useState<Render | null>(null);
  const [allRenders, setAllRenders] = useState<Render[]>([]);
  const toast = useToast();

  useEffect(() => {
    api.getProject(projectId).then(setProject);
    api.listRenders(projectId).then((rs) => {
      setAllRenders(rs);
      const primary = explicitRenderId
        ? rs.find((r) => r.id === explicitRenderId) || rs[0] || null
        : rs[0] || null;
      setRender(primary);
      const secondary = compareRenderId
        ? rs.find((r) => r.id === compareRenderId) || null
        : null;
      setCompareRender(secondary);
    });
  }, [projectId, explicitRenderId, compareRenderId]);

  if (!project) return <div className="p-8 text-ink-300">Loading…</div>;

  const versionOf = (r: Render) =>
    allRenders.length - allRenders.findIndex((x) => x.id === r.id);
  const otherCompleted = allRenders.filter(
    (r) => r.id !== render?.id && r.status === "completed"
  );
  const comparing = render && compareRender && render.status === "completed";

  return (
    <div className={`p-8 ${comparing ? "max-w-6xl" : "max-w-3xl"}`}>
      <Link
        href={`/projects/${projectId}`}
        className="text-xs text-ink-300 hover:text-ink-100"
      >
        ← {project.title || "Project"}
      </Link>
      <h1 className="text-3xl font-semibold tracking-tight mt-1 mb-2">
        Render
        {render ? ` · v${versionOf(render)}` : ""}
        {comparing ? ` vs v${versionOf(compareRender!)}` : ""}
      </h1>

      {allRenders.length > 1 && (
        <div className="flex flex-wrap items-center gap-2 mb-6">
          {allRenders.map((r, idx) => {
            const isCurrent = render?.id === r.id;
            const isCompare = compareRender?.id === r.id;
            return (
              <Link
                key={r.id}
                href={`/projects/${projectId}/render?id=${r.id}`}
                className={`chip ${
                  isCurrent ? "chip-active" : isCompare ? "border-accent" : ""
                }`}
                aria-current={isCurrent ? "page" : undefined}
              >
                v{allRenders.length - idx}
                <span className="text-[10px] text-ink-400">
                  {new Date(r.created_at).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
                {isCompare && (
                  <span className="text-[10px] text-accent ml-1">compare</span>
                )}
              </Link>
            );
          })}

          {/* Compare picker — only shows when there's >=1 other completed
              render that isn't already on screen. */}
          {render?.status === "completed" && otherCompleted.length > 0 && (
            <div className="ml-2 flex items-center gap-2 text-xs">
              {comparing ? (
                <Link
                  href={`/projects/${projectId}/render?id=${render!.id}`}
                  className="btn-ghost text-xs"
                  title="Hide the comparison render"
                >
                  Stop comparing
                </Link>
              ) : (
                <>
                  <span className="text-ink-400">Compare with</span>
                  <select
                    className="input text-xs py-1"
                    aria-label="Compare with another render"
                    defaultValue=""
                    onChange={(e) => {
                      const id = e.target.value;
                      if (id) {
                        window.location.href = `/projects/${projectId}/render?id=${render!.id}&compare=${id}`;
                      }
                    }}
                  >
                    <option value="" disabled>
                      pick a version…
                    </option>
                    {otherCompleted.map((r) => (
                      <option key={r.id} value={r.id}>
                        v{versionOf(r)} · {new Date(r.created_at).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </option>
                    ))}
                  </select>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {!render || render.status !== "completed" ? (
        <div className="card p-6 text-ink-300">
          {render?.status || "no render yet"}
        </div>
      ) : comparing ? (
        <ComparePane
          left={render}
          right={compareRender!}
          leftLabel={`v${versionOf(render)}`}
          rightLabel={`v${versionOf(compareRender!)}`}
        />
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
          {(render.estimated_cost > 0 || render.actual_cost > 0) && (
            <div className="text-xs text-ink-400 mb-3">
              Cost: actual ${render.actual_cost.toFixed(2)} · estimated $
              {render.estimated_cost.toFixed(2)}
              {render.actual_cost === 0 && " (mock mode — nothing billed)"}
            </div>
          )}
          <div className="flex flex-wrap gap-3 items-center">
            <a
              href={api.renderDownload(render.id)}
              className="btn-primary"
              download={`avatar-video-${render.id}.mp4`}
            >
              Download MP4
            </a>
            <button
              className="btn-ghost"
              onClick={async () => {
                const url = `${window.location.origin}/share/${render.share_token}`;
                try {
                  await navigator.clipboard.writeText(url);
                  toast.success("Share link copied to clipboard");
                } catch {
                  window.prompt("Copy this shareable link:", url);
                }
              }}
              title="Anyone with this link can watch the video without logging in"
            >
              Copy share link
            </button>
            <Link href={`/projects/${projectId}`} className="btn-ghost">
              Edit shots
            </Link>
            <button
              className="btn-ghost"
              onClick={async () => {
                try {
                  const dup = await api.duplicateProject(project);
                  window.location.href = `/projects/${dup.id}`;
                } catch (e: any) {
                  toast.error(e.message || "Duplicate failed");
                }
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

// Side-by-side player that mirrors play/pause/seek between two <video>
// elements so a creator can A/B vertical clips frame-by-frame. A small
// "guard" ref blocks the mirroring from triggering itself recursively
// when we set state on the other video.
function ComparePane({
  left,
  right,
  leftLabel,
  rightLabel,
}: {
  left: Render;
  right: Render;
  leftLabel: string;
  rightLabel: string;
}) {
  const leftRef = useRef<HTMLVideoElement | null>(null);
  const rightRef = useRef<HTMLVideoElement | null>(null);
  const syncing = useRef(false);
  const [linked, setLinked] = useState(true);

  useEffect(() => {
    if (!linked) return;
    const a = leftRef.current;
    const b = rightRef.current;
    if (!a || !b) return;

    const mirror = (src: HTMLVideoElement, dst: HTMLVideoElement) => {
      const onPlay = () => {
        if (syncing.current) return;
        syncing.current = true;
        dst.currentTime = src.currentTime;
        dst.play().catch(() => {});
        syncing.current = false;
      };
      const onPause = () => {
        if (syncing.current) return;
        syncing.current = true;
        dst.pause();
        syncing.current = false;
      };
      const onSeeked = () => {
        if (syncing.current) return;
        // Tolerate small drift from manual scrub — only resync when meaningful.
        if (Math.abs(src.currentTime - dst.currentTime) > 0.15) {
          syncing.current = true;
          dst.currentTime = src.currentTime;
          syncing.current = false;
        }
      };
      src.addEventListener("play", onPlay);
      src.addEventListener("pause", onPause);
      src.addEventListener("seeked", onSeeked);
      return () => {
        src.removeEventListener("play", onPlay);
        src.removeEventListener("pause", onPause);
        src.removeEventListener("seeked", onSeeked);
      };
    };
    const off1 = mirror(a, b);
    const off2 = mirror(b, a);
    return () => {
      off1();
      off2();
    };
  }, [linked, left.id, right.id]);

  return (
    <div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-3">
        {[
          { r: left, ref: leftRef, label: leftLabel },
          { r: right, ref: rightRef, label: rightLabel },
        ].map(({ r, ref, label }) => (
          <div key={r.id} className="card overflow-hidden bg-black">
            <div className="px-3 py-1.5 text-xs text-ink-200 bg-ink-900/80 flex items-center justify-between">
              <span className="font-medium">{label}</span>
              <span className="text-ink-400">
                {new Date(r.created_at).toLocaleString()}
              </span>
            </div>
            <video
              ref={ref}
              src={api.renderDownload(r.id)}
              controls
              playsInline
              poster={api.renderThumb(r.id)}
              className="w-full max-h-[70vh] mx-auto bg-black"
            />
          </div>
        ))}
      </div>
      <label className="flex items-center gap-2 text-xs text-ink-300 mb-3">
        <input
          type="checkbox"
          checked={linked}
          onChange={(e) => setLinked(e.target.checked)}
          className="accent-accent"
        />
        Linked playback — play / pause / seek on either video mirrors to the other.
      </label>
    </div>
  );
}
