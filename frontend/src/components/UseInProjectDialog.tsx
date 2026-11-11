"use client";
import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { Dialog } from "@/components/Dialog";
import { api, errorText, Project, Shot, StudioJob, TakeFromStudio } from "@/lib/api";
import { shotTypeLabel } from "@/lib/labels";

/** Shots that are still being made can't take a Studio clip yet. */
const IN_FLIGHT = ["submitted", "polling"];

function seconds(n: number | null | undefined): string {
  return n === null || n === undefined ? "" : `${Number.isInteger(n) ? n : n.toFixed(1)} s`;
}

/** Why a shot can't use the clip ("" = it can). */
function shotBlocked(shot: Shot): string {
  if (shot.shot_type === "end_card") return "The end card can't use a clip";
  if (shot.shot_type.startsWith("ambient")) return "Not a storyboard shot";
  if (IN_FLIGHT.includes(shot.status)) return "A new clip is still being made";
  return "";
}

/**
 * "Use in project…" for a finished Studio clip: pick a storyboard project,
 * then a shot. The clip is copied into the project as that shot's current
 * take (free); Re-compose puts it in the video.
 */
export function UseInProjectDialog({ job, onClose }: { job: StudioJob; onClose: () => void }) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [search, setSearch] = useState("");
  const [project, setProject] = useState<Project | null>(null);
  const [loadingShots, setLoadingShots] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ take: TakeFromStudio; shot: Shot; project: Project } | null>(null);

  useEffect(() => {
    api
      .listProjects()
      .then((list) =>
        setProjects(list.filter((p) => p.mode !== "ambient_loop").sort((a, b) => b.id - a.id))
      )
      .catch((e) => {
        setProjects([]);
        setError(errorText(e, "Couldn't load your projects"));
      });
  }, []);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (projects || []).filter((p) => !q || (p.title || "").toLowerCase().includes(q));
  }, [projects, search]);

  const pick = async (p: Project) => {
    setError(null);
    setLoadingShots(true);
    try {
      setProject(await api.getProject(p.id));
    } catch (e) {
      setError(errorText(e, "Couldn't load that project"));
    } finally {
      setLoadingShots(false);
    }
  };

  const use = async (shot: Shot) => {
    if (!project) return;
    setBusy(shot.id);
    setError(null);
    try {
      const take = await api.takeFromStudio(project.id, shot.id, job.id);
      setDone({ take, shot, project });
    } catch (e) {
      setError(errorText(e, "Couldn't use this clip"));
    } finally {
      setBusy(null);
    }
  };

  const clipLen = job.result_duration_seconds ?? job.duration_seconds;
  const shots = project ? [...project.shots].sort((a, b) => a.shot_order - b.shot_order) : [];

  return (
    <Dialog title="Use in project" onClose={onClose}>
      {done ? (
        <div className="space-y-3">
          <p className="text-sm" role="status">
            Shot {done.shot.shot_order} of &ldquo;{done.project.title || `Project #${done.project.id}`}&rdquo; now
            uses this clip.
          </p>
          {done.take.warning && (
            <p className="text-sm text-amber-200 bg-amber-500/10 border border-amber-500/30 rounded-md p-2">
              {done.take.warning}
            </p>
          )}
          <p className="text-xs text-ink-300">Free. Re-compose the project to put it in the video.</p>
          <div className="flex justify-end gap-2 pt-1">
            <button className="btn-ghost" onClick={onClose}>
              Done
            </button>
            <a className="btn-primary" href={`/projects/${done.project.id}`}>
              Open project
            </a>
          </div>
        </div>
      ) : !project ? (
        <div className="space-y-3">
          <p className="text-xs text-ink-300">
            Pick a storyboard project, then the shot this clip should replace. Free. Re-compose to put it in the
            video.
          </p>
          <input
            type="search"
            className="input text-sm"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search projects"
            aria-label="Search projects"
          />
          {projects === null ? (
            <p className="text-sm text-ink-400">Loading…</p>
          ) : shown.length === 0 ? (
            <p className="text-sm text-ink-400">
              {projects.length === 0 ? "No storyboard projects yet." : "No projects match."}
            </p>
          ) : (
            <ul className="max-h-[45vh] overflow-y-auto divide-y divide-ink-800 border border-ink-800 rounded-md">
              {shown.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => pick(p)}
                    disabled={loadingShots}
                    className="w-full text-left px-3 py-2.5 hover:bg-ink-800 focus-visible:bg-ink-800 disabled:opacity-50"
                  >
                    <span className="block text-sm truncate">{p.title || `Project #${p.id}`}</span>
                    <span className="block text-[11px] text-ink-400">
                      {p.aspect_ratio} · {p.shots?.length ?? 0} shots
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <button type="button" className="chip text-xs" onClick={() => setProject(null)}>
              ← Projects
            </button>
            <span className="text-sm truncate min-w-0">{project.title || `Project #${project.id}`}</span>
          </div>
          <p className="text-xs text-ink-300">
            This clip is {seconds(clipLen)}
            {job.result_width && job.result_height ? ` (${job.result_width}×${job.result_height})` : ""}; the
            project is {project.aspect_ratio}. Pick the shot it replaces. Free. Re-compose to put it in the video.
          </p>
          {shots.length === 0 ? (
            <p className="text-sm text-ink-400">This project has no shots yet. Plan its storyboard first.</p>
          ) : (
            <ul className="max-h-[45vh] overflow-y-auto divide-y divide-ink-800 border border-ink-800 rounded-md">
              {shots.map((s) => {
                const why = shotBlocked(s);
                return (
                  <li key={s.id}>
                    <button
                      type="button"
                      onClick={() => use(s)}
                      disabled={Boolean(why) || busy !== null}
                      aria-describedby={why ? `shot-why-${s.id}` : undefined}
                      className={clsx(
                        "w-full text-left px-3 py-2 flex items-center gap-3",
                        why ? "opacity-60 cursor-not-allowed" : "hover:bg-ink-800 focus-visible:bg-ink-800"
                      )}
                    >
                      <span className="w-10 h-14 shrink-0 rounded bg-ink-800 overflow-hidden flex items-center justify-center text-[10px] text-ink-500">
                        {s.clip_path ? (
                          <video
                            src={`${api.shotClip(project.id, s.id)}#t=0.1`}
                            muted
                            playsInline
                            preload="metadata"
                            className="w-full h-full object-cover"
                            aria-hidden="true"
                          />
                        ) : (
                          "no clip"
                        )}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm">
                          Shot {s.shot_order} · {shotTypeLabel(s.shot_type)} · {seconds(s.duration_seconds)}
                        </span>
                        <span className="block text-[11px] text-ink-400 truncate">
                          {why ? <span id={`shot-why-${s.id}`}>{why}</span> : s.prompt}
                        </span>
                      </span>
                      {busy === s.id && <span className="text-xs text-ink-300">Copying…</span>}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
      {error && (
        <p className="text-sm text-accent" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  );
}
