"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { StatusBadge } from "@/components/StatusBadge";
import { AuthGate } from "@/components/AuthGate";
import { PreflightModal } from "@/components/PreflightModal";
import { AudioPanel } from "@/components/AudioPanel";
import { RenderHistory } from "@/components/RenderHistory";
import { api, Asset, Project, Shot, Render } from "@/lib/api";

export default function ProjectEditorPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const params = useParams();
  const projectId = Number(params?.id);
  const [project, setProject] = useState<Project | null>(null);
  const [render, setRender] = useState<Render | null>(null);
  const [castAssets, setCastAssets] = useState<Asset[]>([]);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPreflight, setShowPreflight] = useState(false);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const router = useRouter();
  const timer = useRef<NodeJS.Timeout | null>(null);

  const refresh = async () => {
    try {
      const p = await api.getProject(projectId);
      setProject(p);
      const st = await api.projectStatus(projectId);
      setRender(st.latest_render);
      const ca = await api.castAssets(projectId);
      setCastAssets(ca);
      if (!["completed", "failed", "draft", "planned"].includes(st.project_status)) {
        setPolling(true);
      } else {
        setPolling(false);
      }
    } catch (e: any) {
      setError(e.message);
    }
  };

  useEffect(() => {
    refresh();
  }, [projectId]);

  useEffect(() => {
    if (!polling) return;
    timer.current = setInterval(refresh, 2000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [polling]);

  // Auto-kick storyboard generation on first load if the project has no plan yet.
  const autoKicked = useRef(false);
  useEffect(() => {
    if (!project || autoKicked.current) return;
    if (project.status === "draft" && !project.generated_plan_json) {
      autoKicked.current = true;
      api.generatePlan(projectId).then(() => setPolling(true)).catch((e) => setError(e.message));
    }
  }, [project, projectId]);

  if (error)
    return <div className="p-8 text-accent">{error}</div>;
  if (!project) return <div className="p-8 text-ink-300">Loading…</div>;

  return (
    <div className="p-8 max-w-6xl">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <Link href="/projects" className="text-xs text-ink-300 hover:text-ink-100">
            ← Projects
          </Link>
          <h1 className="text-3xl font-semibold tracking-tight mt-1">
            {project.title || "Untitled"}
          </h1>
          <div className="text-sm text-ink-300 mt-1">
            {project.mode} · {project.aspect_ratio} · {project.target_duration_seconds}s
          </div>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge status={project.status} />
          {project.status === "planned" || project.status === "failed" ? (
            <button
              className="btn-primary"
              onClick={() => setShowPreflight(true)}
            >
              Generate Video
            </button>
          ) : null}
          {project.status === "completed" && render && (
            <>
              <button
                className="btn-ghost"
                title="Re-run only the FFmpeg compose step using existing clips + audio"
                onClick={async () => {
                  await api.recompose(projectId);
                  setPolling(true);
                }}
              >
                Re-compose
              </button>
              <Link href={`/projects/${projectId}/render`} className="btn-primary">
                Open Render →
              </Link>
            </>
          )}
        </div>
      </header>

      <section className="card p-4 mb-6">
        <div className="label">Voice script</div>
        <textarea
          className="input min-h-[100px]"
          value={project.original_script}
          onChange={(e) =>
            setProject({ ...project, original_script: e.target.value })
          }
          onBlur={() =>
            api.updateProject(projectId, {
              original_script: project.original_script,
            })
          }
        />
      </section>

      {project.generated_plan_json?.content_warning_notes && (
        <div className="card p-4 mb-6 border-amber-500/40 bg-amber-500/10">
          <div className="text-sm font-medium text-amber-300 flex items-center gap-2">
            <span>⚠</span> Content warning from the planner
          </div>
          <div className="text-xs text-amber-200/90 mt-1">
            {project.generated_plan_json.content_warning_notes}
          </div>
        </div>
      )}

      <AudioPanel project={project} onChange={(p) => setProject(p)} />

      <section className="mb-6">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-lg font-medium">Storyboard</h2>
          <div className="flex items-center gap-3">
            {project.shots.length > 1 && (
              <span className="text-xs text-ink-400">Drag a shot to reorder</span>
            )}
            <button
              className="btn-ghost text-xs"
              onClick={async () => {
                await api.generatePlan(projectId);
                setPolling(true);
              }}
            >
              Re-plan
            </button>
          </div>
        </div>
        {project.shots.length === 0 ? (
          <div className="card p-6 text-ink-300 text-sm">
            No shots yet. {project.status === "planning" ? "Planning…" : ""}
          </div>
        ) : (
          <div className="space-y-3">
            {project.shots.map((s, idx) => (
              <ShotRow
                key={s.id}
                index={idx}
                projectId={projectId}
                shot={s}
                castAssets={castAssets}
                onChange={refresh}
                dragIndex={dragIndex}
                onDragStart={() => setDragIndex(idx)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={async () => {
                  if (dragIndex === null || dragIndex === idx) {
                    setDragIndex(null);
                    return;
                  }
                  const next = [...project.shots];
                  const [moved] = next.splice(dragIndex, 1);
                  next.splice(idx, 0, moved);
                  setProject({ ...project, shots: next });
                  setDragIndex(null);
                  await api.reorderShots(projectId, next.map((x) => x.id));
                }}
                onDragEnd={() => setDragIndex(null)}
              />
            ))}
          </div>
        )}
      </section>

      {showPreflight && (
        <PreflightModal
          projectId={projectId}
          onClose={() => setShowPreflight(false)}
          onConfirm={async (force) => {
            setShowPreflight(false);
            try {
              await api.generateVideo(projectId, force);
              setPolling(true);
            } catch (e: any) {
              setError(e.message);
            }
          }}
        />
      )}

      <RenderHistory projectId={projectId} />

      {render?.error && (
        <section className="card p-4 text-sm">
          <div className="font-medium mb-2">Latest render error</div>
          <pre className="p-3 bg-ink-800 text-xs text-accent whitespace-pre-wrap rounded">
            {render.error}
          </pre>
        </section>
      )}
    </div>
  );
}

function ShotRow({
  index,
  projectId,
  shot,
  castAssets,
  onChange,
  dragIndex,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: {
  index: number;
  projectId: number;
  shot: Shot;
  castAssets: Asset[];
  onChange: () => void;
  dragIndex: number | null;
  onDragStart: () => void;
  onDragOver: (e: React.DragEvent) => void;
  onDrop: () => void;
  onDragEnd: () => void;
}) {
  const [prompt, setPrompt] = useState(shot.prompt);
  const [dur, setDur] = useState(shot.duration_seconds);
  const [refs, setRefs] = useState<number[]>(shot.reference_asset_ids_json || []);
  const [regenBusy, setRegenBusy] = useState(false);

  const regenerate = async (recompose: boolean) => {
    setRegenBusy(true);
    try {
      await api.regenerateShot(projectId, shot.id, recompose);
      onChange();
    } finally {
      setRegenBusy(false);
    }
  };

  const toggleRef = async (assetId: number) => {
    const next = refs.includes(assetId)
      ? refs.filter((x) => x !== assetId)
      : [...refs, assetId];
    setRefs(next);
    await api.updateShot(projectId, shot.id, { reference_asset_ids_json: next });
  };

  return (
    <div
      className={`card p-4 transition ${dragIndex === index ? "opacity-50" : ""}`}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            <span
              draggable
              onDragStart={onDragStart}
              onDragEnd={onDragEnd}
              className="cursor-grab active:cursor-grabbing text-ink-400 hover:text-ink-200 select-none px-1"
              title="Drag to reorder"
            >
              ⠿
            </span>
            <span className="text-xs font-medium text-ink-200">Shot {shot.shot_order}</span>
            <span className="chip text-[10px] py-0.5">{shot.shot_type}</span>
            <StatusBadge status={shot.status} />
          </div>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onBlur={() =>
              api.updateShot(projectId, shot.id, { prompt }).then(onChange)
            }
            className="input min-h-[60px]"
          />
          {castAssets.length > 0 && shot.shot_type !== "end_card" && (
            <div className="mt-3">
              <div className="label flex items-center gap-2">
                <span>References</span>
                {refs.length === 0 && (
                  <span className="text-[10px] text-ink-400 normal-case tracking-normal">
                    (auto: first 2 of each cast member)
                  </span>
                )}
              </div>
              <div className="flex flex-wrap gap-2 mt-1">
                {castAssets.map((a) => {
                  const on = refs.includes(a.id);
                  const label =
                    a.owner_kind === "avatar"
                      ? `Char #${a.avatar_id}`
                      : `Scene #${a.ingredient_id}`;
                  return (
                    <button
                      key={a.id}
                      onClick={() => toggleRef(a.id)}
                      title={`${label} · ${a.asset_type}`}
                      className={`w-12 h-12 rounded overflow-hidden border transition ${
                        on
                          ? "border-accent ring-2 ring-accent/60"
                          : "border-ink-700 hover:border-ink-500 opacity-70"
                      }`}
                    >
                      <img
                        src={api.publicAsset(a.public_token)}
                        alt={a.asset_type}
                        className="w-full h-full object-cover"
                      />
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
        <div className="w-44 space-y-2 text-xs">
          <label className="label">Duration</label>
          <input
            type="number"
            className="input"
            value={dur}
            min={1}
            step={0.5}
            onChange={(e) => setDur(parseFloat(e.target.value))}
            onBlur={() =>
              api
                .updateShot(projectId, shot.id, { duration_seconds: dur })
                .then(onChange)
            }
          />
          <button
            className="btn-primary w-full text-xs disabled:opacity-50"
            onClick={() => regenerate(true)}
            disabled={regenBusy}
            title="Regenerate just this shot's clip, then re-stitch the final video using existing audio + other shots."
          >
            {regenBusy ? "Regenerating…" : "Regenerate + re-compose"}
          </button>
          <button
            className="btn-ghost w-full text-xs disabled:opacity-50"
            onClick={() => regenerate(false)}
            disabled={regenBusy}
            title="Just remake the clip; click Re-compose at the top when you want to update the final video."
          >
            Regenerate clip only
          </button>
        </div>
      </div>
      {shot.error && (
        <div className="text-xs text-accent mt-2">Error: {shot.error}</div>
      )}
    </div>
  );
}
