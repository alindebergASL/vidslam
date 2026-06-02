"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { StatusBadge } from "@/components/StatusBadge";
import { AuthGate } from "@/components/AuthGate";
import { PreflightModal } from "@/components/PreflightModal";
import { AudioPanel } from "@/components/AudioPanel";
import { RenderHistory } from "@/components/RenderHistory";
import { GenerationProgress } from "@/components/GenerationProgress";
import { useToast } from "@/components/Toaster";
import { api, Asset, Project, Shot, Render } from "@/lib/api";
import { useKeyboardShortcuts } from "@/lib/useKeyboardShortcuts";

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
  const toast = useToast();
  const [preflight, setPreflight] = useState<{
    ok: boolean;
    summary: "ok" | "warn" | "fail";
    blockers: string[];
  } | null>(null);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPreflight, setShowPreflight] = useState(false);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
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
      // Fetch a lightweight preflight summary so the editor can surface blockers
      // before the user even clicks Generate Video. Skip while a render is
      // actively in flight (cheap call, but pointless every 2s during polling).
      const activeRender = st.latest_render &&
        !["completed", "failed"].includes(st.latest_render.status);
      if (!activeRender) {
        try {
          const pf = await api.preflight(projectId);
          setPreflight({
            ok: pf.ok,
            summary: pf.summary,
            blockers: pf.checks
              .filter((c) => c.status === "fail")
              .map((c) => `${c.label}: ${c.message}`),
          });
        } catch {
          setPreflight(null);
        }
      }
      const renderActive =
        !!st.latest_render &&
        !["completed", "failed"].includes(st.latest_render.status);
      const projectActive = !["completed", "failed", "draft", "planned"].includes(
        st.project_status
      );
      setPolling(renderActive || projectActive);
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

  const [showShortcuts, setShowShortcuts] = useState(false);
  useKeyboardShortcuts(
    [
      {
        key: "Enter",
        meta: true,
        label: "Generate video (with preflight)",
        run: () => {
          if (project && ["planned", "failed", "completed"].includes(project.status)) {
            setShowPreflight(true);
          }
        },
      },
      {
        key: "Enter",
        ctrl: true,
        label: "Generate video (with preflight)",
        run: () => {
          if (project && ["planned", "failed", "completed"].includes(project.status)) {
            setShowPreflight(true);
          }
        },
      },
      {
        key: "g",
        label: "Re-plan storyboard",
        run: () => {
          api.generatePlan(projectId).then(() => setPolling(true)).catch((e) => setError(e.message));
        },
      },
      {
        key: "r",
        label: "Re-compose latest render",
        run: () => {
          if (project?.status !== "completed") return;
          api.recompose(projectId).then(() => setPolling(true)).catch((e) => setError(e.message));
        },
      },
      {
        key: "?",
        shift: true,
        label: "Show keyboard shortcuts",
        run: () => setShowShortcuts((v) => !v),
      },
      {
        key: "Escape",
        label: "Close preflight / shortcuts overlay",
        run: () => {
          if (showShortcuts) setShowShortcuts(false);
          else if (showPreflight) setShowPreflight(false);
          else return false;  // let the browser handle it
        },
      },
    ],
    [project, projectId, showPreflight, showShortcuts]
  );

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
          {preflight && project.generated_plan_json && (
            <button
              className={`chip text-xs ${
                preflight.summary === "fail"
                  ? "bg-red-500/10 border-red-500/40 text-red-300"
                  : preflight.summary === "warn"
                  ? "bg-amber-500/10 border-amber-500/40 text-amber-300"
                  : "bg-emerald-500/10 border-emerald-500/40 text-emerald-300"
              }`}
              onClick={() => setShowPreflight(true)}
              title={
                preflight.summary === "ok"
                  ? "Preflight checks pass"
                  : preflight.blockers.join(" · ") || "Click for details"
              }
            >
              {preflight.summary === "ok" ? "Preflight ✓" : preflight.summary === "warn" ? "Preflight ⚠" : "Preflight ✕"}
            </button>
          )}
          {project.status === "planned" || project.status === "failed" ? (
            <button
              className="btn-primary"
              onClick={() => setShowPreflight(true)}
              title="Keyboard: ⌘/Ctrl + Enter"
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
                  try {
                    await api.recompose(projectId);
                    toast.info("Re-composing the video…");
                    setPolling(true);
                  } catch (e: any) {
                    toast.error(e.message || "Re-compose failed");
                  }
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

      {render &&
        ["pending", "planning", "generating_audio", "generating_shots", "polling", "rendering", "failed", "cancelled"].includes(
          render.status
        ) && (
          <GenerationProgress
            render={render}
            shots={project.shots}
            projectId={projectId}
            onRetry={() => setPolling(true)}
          />
        )}

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
        <div className="label mt-4">Creative direction</div>
        <textarea
          className="input min-h-[60px]"
          placeholder="Use case + tone for the planner (demo, tutorial, promo…). Re-plan to apply."
          value={project.creative_direction}
          onChange={(e) =>
            setProject({ ...project, creative_direction: e.target.value })
          }
          onBlur={() =>
            api.updateProject(projectId, {
              creative_direction: project.creative_direction,
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

      {preflight && !preflight.ok && preflight.blockers.length > 0 && (
        <div className="card p-4 mb-6 border-red-500/40 bg-red-500/10">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-medium text-red-300 flex items-center gap-2">
                <span>✕</span> {preflight.blockers.length} blocker{preflight.blockers.length === 1 ? "" : "s"} before you can generate
              </div>
              <ul className="text-xs text-red-200/90 mt-1 list-disc pl-5 space-y-0.5">
                {preflight.blockers.map((b, i) => (
                  <li key={i}>{b}</li>
                ))}
              </ul>
            </div>
            <button className="btn-ghost text-xs whitespace-nowrap" onClick={() => setShowPreflight(true)}>
              See all checks
            </button>
          </div>
        </div>
      )}

      <AudioPanel project={project} onChange={(p) => setProject(p)} />

      {project.generated_plan_json && (
        <StoryboardScript project={project} onChange={(p) => setProject(p)} />
      )}

      <section className="mb-6">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-lg font-medium">Storyboard</h2>
          <div className="flex items-center gap-3">
            {selected.length > 0 ? (
              <>
                <span className="text-xs text-ink-300">{selected.length} selected</span>
                <button
                  className="btn-primary text-xs"
                  onClick={async () => {
                    const count = selected.length;
                    try {
                      await api.regenerateShotsBulk(projectId, selected);
                      toast.info(`Re-rolling ${count} shot${count === 1 ? "" : "s"}…`);
                    } catch (e: any) {
                      toast.error(e.message || "Bulk re-roll failed");
                    }
                    setSelected([]);
                    setPolling(true);
                  }}
                >
                  Re-roll selected →
                </button>
                <button className="btn-ghost text-xs" onClick={() => setSelected([])}>
                  Clear
                </button>
              </>
            ) : (
              <>
                {project.shots.length > 1 && (
                  <span className="text-xs text-ink-400">Drag to reorder · tick to bulk re-roll</span>
                )}
                <button
                  className="btn-ghost text-xs"
                  onClick={async () => {
                    try {
                      await api.generatePlan(projectId);
                      toast.info("Re-planning the storyboard…");
                      setPolling(true);
                    } catch (e: any) {
                      toast.error(e.message || "Re-plan failed");
                    }
                  }}
                >
                  Re-plan
                </button>
              </>
            )}
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
                selected={selected.includes(s.id)}
                onToggleSelect={() =>
                  setSelected((cur) =>
                    cur.includes(s.id) ? cur.filter((x) => x !== s.id) : [...cur, s.id]
                  )
                }
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

      <button
        type="button"
        onClick={() => setShowShortcuts(true)}
        className="fixed bottom-4 right-4 chip text-[11px] py-1 opacity-60 hover:opacity-100"
        title="Show keyboard shortcuts"
      >
        ? shortcuts
      </button>

      {showShortcuts && (
        <div
          className="fixed inset-0 z-30 bg-black/70 flex items-center justify-center p-4"
          onClick={() => setShowShortcuts(false)}
        >
          <div
            className="card w-full max-w-md p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-3">
              <div className="text-lg font-semibold">Keyboard shortcuts</div>
              <button className="text-ink-300 hover:text-white" onClick={() => setShowShortcuts(false)}>
                ✕
              </button>
            </div>
            <dl className="text-sm divide-y divide-ink-800">
              {[
                { keys: ["⌘", "Enter"], label: "Generate video (with preflight)" },
                { keys: ["Ctrl", "Enter"], label: "Generate video (with preflight)" },
                { keys: ["G"], label: "Re-plan storyboard" },
                { keys: ["R"], label: "Re-compose latest render" },
                { keys: ["?"], label: "Toggle this overlay" },
                { keys: ["Esc"], label: "Close overlays" },
              ].map((s, i) => (
                <div key={i} className="flex items-center justify-between py-2">
                  <span className="text-ink-200">{s.label}</span>
                  <span className="flex gap-1">
                    {s.keys.map((k) => (
                      <kbd
                        key={k}
                        className="px-1.5 py-0.5 text-[10px] rounded bg-ink-800 border border-ink-700 text-ink-100 font-mono"
                      >
                        {k}
                      </kbd>
                    ))}
                  </span>
                </div>
              ))}
            </dl>
            <div className="text-[10px] text-ink-400 mt-3">
              Letter shortcuts don&apos;t fire while typing in a text field.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function StoryboardScript({
  project,
  onChange,
}: {
  project: Project;
  onChange: (p: Project) => void;
}) {
  const plan = project.generated_plan_json || {};
  const [script, setScript] = useState<string>(plan.cleaned_voice_script || "");
  const [endCard, setEndCard] = useState<string>(plan.end_card_text || "");
  const [saving, setSaving] = useState(false);
  const toast = useToast();

  const dirty =
    script !== (plan.cleaned_voice_script || "") || endCard !== (plan.end_card_text || "");

  const save = async (resync: boolean) => {
    setSaving(true);
    try {
      const updated = await api.editPlan(project.id, {
        cleaned_voice_script: script,
        end_card_text: endCard,
        resync_captions: resync,
      });
      onChange(updated);
      toast.success(resync ? "Saved · captions resynced" : "Saved");
    } catch (e: any) {
      toast.error(e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const captionCount = Array.isArray(plan.caption_chunks) ? plan.caption_chunks.length : 0;

  return (
    <section className="card p-4 mb-6">
      <div className="flex items-baseline justify-between mb-2">
        <h2 className="text-lg font-medium">Voiceover & captions</h2>
        <span className="text-[11px] text-ink-400">
          What&apos;s actually spoken (TTS) and shown on screen — edits apply on the next render.
        </span>
      </div>
      <label className="label">Spoken script</label>
      <textarea
        className="input min-h-[90px]"
        value={script}
        onChange={(e) => setScript(e.target.value)}
      />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
        <div>
          <label className="label">End-card text</label>
          <input className="input" value={endCard} onChange={(e) => setEndCard(e.target.value)} />
        </div>
        <div className="flex items-end">
          <span className="text-xs text-ink-400">
            {captionCount} caption chunk{captionCount === 1 ? "" : "s"} currently set
          </span>
        </div>
      </div>
      <div className="flex items-center gap-2 mt-3">
        <button
          className="btn-ghost text-xs disabled:opacity-50"
          disabled={saving || !dirty}
          onClick={() => save(false)}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          className="btn-primary text-xs disabled:opacity-50"
          disabled={saving}
          onClick={() => save(true)}
          title="Save and regenerate caption chunks from the spoken script"
        >
          Save + resync captions
        </button>
      </div>
    </section>
  );
}

function ShotRow({
  index,
  projectId,
  shot,
  castAssets,
  onChange,
  selected,
  onToggleSelect,
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
  selected: boolean;
  onToggleSelect: () => void;
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
  const toast = useToast();

  const regenerate = async (recompose: boolean) => {
    setRegenBusy(true);
    try {
      await api.regenerateShot(projectId, shot.id, recompose);
      toast.info(
        recompose
          ? `Re-rolling shot ${shot.shot_order}, then recomposing`
          : `Re-rolling shot ${shot.shot_order}`
      );
      onChange();
    } catch (e: any) {
      toast.error(e.message || "Regenerate failed");
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
      className={`card p-4 transition ${dragIndex === index ? "opacity-50" : ""} ${
        selected ? "border-accent" : ""
      }`}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            {shot.shot_type !== "end_card" && (
              <input
                type="checkbox"
                checked={selected}
                onChange={onToggleSelect}
                title="Select for bulk re-roll"
                className="accent-accent"
              />
            )}
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
          <div className="flex gap-3">
            {shot.status === "completed" && shot.clip_path && shot.shot_type !== "end_card" && (
              <video
                key={shot.clip_path}
                src={api.shotClip(projectId, shot.id)}
                className="w-24 shrink-0 rounded bg-black aspect-[9/16] object-cover"
                muted
                loop
                playsInline
                onMouseEnter={(e) => (e.currentTarget as HTMLVideoElement).play()}
                onMouseLeave={(e) => {
                  const v = e.currentTarget as HTMLVideoElement;
                  v.pause();
                  v.currentTime = 0;
                }}
                title="Hover to preview this shot's clip"
              />
            )}
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onBlur={() =>
                api.updateShot(projectId, shot.id, { prompt }).then(onChange)
              }
              className="input min-h-[60px] flex-1"
            />
          </div>
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
