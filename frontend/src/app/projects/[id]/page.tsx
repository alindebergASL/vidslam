"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { StatusBadge } from "@/components/StatusBadge";
import { AuthGate } from "@/components/AuthGate";
import { PreflightModal, renderPlanLines } from "@/components/PreflightModal";
import { Dialog } from "@/components/Dialog";
import { AudioPanel } from "@/components/AudioPanel";
import { RenderHistory } from "@/components/RenderHistory";
import { GenerationProgress } from "@/components/GenerationProgress";
import { useToast } from "@/components/Toaster";
import { EnhanceButton } from "@/components/EnhanceButton";
import { ProjectSettingsCard } from "@/components/ProjectSettingsCard";
import { AddShotDialog, DeleteShotDialog, DuplicateShotDialog, TakesDialog } from "@/components/ShotEditing";
import { checkBlockedWhy, checkCostText, InspectionChip, useClipCheckSettings } from "@/components/InspectionChip";
import { formatDuration, modeLabel, shotTypeLabel } from "@/lib/labels";
import { api, Asset, ClipCheckSettings, CostEstimate, Project, RenderPlan, RenderPlanShot, Shot, ShotEdit, ShotEditImpact, Render } from "@/lib/api";
import { useKeyboardShortcuts } from "@/lib/useKeyboardShortcuts";

// Project states in which a render (or a re-plan) can be started.
const CAN_GENERATE = ["planned", "failed", "completed", "cancelled"];
const usd = (n: number) => `$${n.toFixed(2)}`;
const IN_FLIGHT = ["submitted", "polling", "running"];

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
  // What "Generate video" would make, reuse and cost right now.
  const [renderPlan, setRenderPlan] = useState<RenderPlan | null>(null);
  const [showReplan, setShowReplan] = useState(false);
  // Storyboard editing: add (after a shot, or null = at the end), delete, takes.
  const [adding, setAdding] = useState<{ after: Shot | null } | null>(null);
  const [deleting, setDeleting] = useState<Shot | null>(null);
  const [takesFor, setTakesFor] = useState<Shot | null>(null);
  const [duplicating, setDuplicating] = useState<{ shot: Shot; impact: ShotEditImpact | null } | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [videoModels, setVideoModels] = useState<
    { id: string; name: string; description: string }[]
  >([]);
  const router = useRouter();
  const timer = useRef<NodeJS.Timeout | null>(null);
  // Clip check: cost per check and whether checks can run (fetched once).
  const clipCheck = useClipCheckSettings();

  // Models list (base + trained LoRAs) is stable during a session — fetch
  // once, not per poll.
  useEffect(() => {
    api.videoModels().then(setVideoModels).catch(() => setVideoModels([]));
  }, []);

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
        !["completed", "failed", "cancelled"].includes(st.latest_render.status);
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
        if (p.generated_plan_json) {
          try {
            setRenderPlan(await api.renderPlan(projectId));
          } catch {
            setRenderPlan(null);
          }
        }
      }
      const renderActive =
        !!st.latest_render &&
        !["completed", "failed", "cancelled"].includes(st.latest_render.status);
      // "regenerating" (shots re-rolling before a recompose) counts as active.
      const projectActive = !["completed", "failed", "draft", "planned", "cancelled"].includes(
        st.project_status
      );
      // Clip-only re-rolls leave the project status alone; keep polling until
      // the shot's video job lands (webhook or sweeper).
      const shotsActive = p.shots.some((s) =>
        ["submitted", "polling", "running"].includes(s.status)
      );
      setPolling(renderActive || projectActive || shotsActive);
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
  // Lazy-render the shot list past ~12. Most projects emit 3–5 shots so
  // this only matters when a user has heavily tweaked + re-planned, but
  // when it matters (40+ shots) react re-renders + textarea autosize
  // adds up. The expander reveals everything atomically; no incremental
  // window or scroll-anchoring trickery — just enough to keep the editor
  // snappy at the upper end.
  const SHOTS_BEFORE_EXPAND = 12;
  const [shotsExpanded, setShotsExpanded] = useState(false);
  useKeyboardShortcuts(
    [
      {
        key: "Enter",
        meta: true,
        label: "Generate video (with preflight)",
        run: () => {
          if (project && CAN_GENERATE.includes(project.status)) {
            setShowPreflight(true);
          }
        },
      },
      {
        key: "Enter",
        ctrl: true,
        label: "Generate video (with preflight)",
        run: () => {
          if (project && CAN_GENERATE.includes(project.status)) {
            setShowPreflight(true);
          }
        },
      },
      {
        key: "g",
        label: "Re-plan storyboard (asks first)",
        // Only opens the question: re-planning replaces work and is a paid call.
        run: () => {
          if (project?.generated_plan_json && CAN_GENERATE.includes(project.status)) setShowReplan(true);
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
          if (showReplan) return false; // the dialog closes itself on Escape
          if (showShortcuts) setShowShortcuts(false);
          else if (showPreflight) setShowPreflight(false);
          else return false;  // let the browser handle it
        },
      },
    ],
    [project, projectId, showPreflight, showShortcuts, showReplan]
  );

  const recompose = async () => {
    try {
      await api.recompose(projectId);
      toast.info("Re-composing the video…");
      setPolling(true);
    } catch (e: any) {
      toast.error(e.message || "Re-compose failed");
    }
  };

  // A shot was added, duplicated or deleted: show the renumbered storyboard
  // now, then reload the render plan and checks.
  const applyShotEdit = (r: ShotEdit) => {
    setProject(r.project);
    setSelected((cur) => cur.filter((id) => r.project.shots.some((s) => s.id === id)));
    refresh();
  };

  // Duplicating asks first only when it puts lip-synced clips out of date.
  const duplicateShot = async (shot: Shot, confirmed = false) => {
    if (!confirmed) {
      try {
        const impact = await api.shotEditImpact(projectId, { op: "duplicate", shot_id: shot.id });
        if (impact.stale_count > 0) {
          setDuplicating({ shot, impact });
          return;
        }
      } catch {
        // Can't tell what it affects: ask, and say so.
        setDuplicating({ shot, impact: null });
        return;
      }
    }
    try {
      const r = await api.duplicateShot(projectId, shot.id);
      toast.success(
        `Shot ${shot.shot_order} duplicated as shot ${r.shot?.shot_order ?? ""}, without a clip${
          r.captions_resynced ? " · captions resynced to the new length" : ""
        }`
      );
      applyShotEdit(r);
    } catch (e: any) {
      toast.error(e.message || "Couldn't duplicate the shot");
    }
  };

  const replan = async (preserve: boolean) => {
    setShowReplan(false);
    try {
      await api.generatePlan(projectId, { preserveEdits: preserve });
      toast.info(
        preserve
          ? "Re-planning. Your spoken script, end card, captions and shot edits are kept"
          : "Re-planning from scratch. The script, captions and every shot are replaced"
      );
      setPolling(true);
    } catch (err: any) {
      toast.error(err.message || "Re-plan failed");
    }
  };

  // The latest video no longer matches the editor: the voice or some clips
  // would be made new by the next render.
  const hasClips = !!project?.shots.some((s) => s.clip_path && s.shot_type !== "end_card");
  const outOfDate =
    !!renderPlan &&
    !!project &&
    CAN_GENERATE.includes(project.status) &&
    hasClips &&
    (renderPlan.voiceover_changed || project.shots.some((s) => s.clip_stale));
  const planRows = new Map<number, RenderPlanShot>((renderPlan?.shots || []).map((s) => [s.id, s]));
  const flagged = renderPlan?.flagged ?? [];
  // A flagged shot may sit past the collapsed list: open it, then scroll there.
  const goToShot = (shotId: number) => {
    setShotsExpanded(true);
    setTimeout(() => {
      const el = document.getElementById(`shot-${shotId}`);
      el?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 50);
  };
  // Ticked shots a re-roll can't fix (lip-synced, voiceover out of date).
  const blockedSelected = selected
    .map((sid) => planRows.get(sid))
    .filter((r): r is RenderPlanShot => !!r?.needs_new_voiceover)
    .map((r) => r.shot_order)
    .sort((a, b) => a - b);

  // Adding, duplicating or deleting shots waits while a render or re-plan runs.
  // A clip still being made (a re-roll, or one left from a cancelled run) counts too.
  const clipsInFlight = !!project?.shots.some((s) => IN_FLIGHT.includes(s.status));
  const canEditShots = !!project && CAN_GENERATE.includes(project.status) && !clipsInFlight;
  const editLockedWhy = clipsInFlight
    ? "Wait until the clips being made are back (they are already paid for)."
    : "Wait until the current step (planning or making the video) finishes.";
  const bodyCount = project ? project.shots.filter((s) => s.shot_type !== "end_card").length : 0;
  const deleteBlockedWhy = (s: Shot): string | null => {
    if (!project) return null;
    if (s.shot_type === "end_card") {
      return project.cta_text.trim()
        ? "The end card shows your end-card text. Clear the end-card text in Project settings to drop it."
        : null;
    }
    if (bodyCount <= 1) return "A video needs at least one shot. Add another shot before deleting this one.";
    if (IN_FLIGHT.includes(s.status))
      return "This shot's clip is still being made (and is already paid for). Wait until it is back.";
    return null;
  };

  if (error)
    return <div className="p-8 text-accent">{error}</div>;
  if (!project)
    return (
      <div className="p-8 max-w-6xl space-y-6" aria-busy="true" aria-label="Loading project">
        <div className="space-y-2">
          <div className="h-3 skeleton w-24" />
          <div className="h-8 skeleton w-2/3" />
          <div className="h-3 skeleton w-1/3" />
        </div>
        <div className="card p-4 space-y-2">
          <div className="h-3 skeleton w-20" />
          <div className="h-24 skeleton" />
        </div>
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="card p-4 h-24 skeleton" />
          ))}
        </div>
      </div>
    );

  return (
    <div className="p-4 sm:p-8 max-w-6xl w-full min-w-0">
      <header className="mb-6 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div className="min-w-0">
          <Link href="/projects" className="text-xs text-ink-300 hover:text-ink-100">
            ← Projects
          </Link>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight mt-1 break-words">
            {project.title || "Untitled"}
          </h1>
          <div className="text-sm text-ink-300 mt-1">
            {modeLabel(project.mode)} · {project.aspect_ratio} · {formatDuration(project.target_duration_seconds)}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
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
          {project.generated_plan_json && CAN_GENERATE.includes(project.status) ? (
            <button
              className={project.status === "completed" && !outOfDate ? "btn-ghost" : "btn-primary"}
              onClick={() => setShowPreflight(true)}
              title="Shows what is made new, what is reused and what it costs before anything starts. Keyboard: ⌘/Ctrl + Enter"
            >
              Generate video
            </button>
          ) : null}
          {project.status === "completed" && render && (
            <>
              <button
                className="btn-ghost"
                title="Puts the video together again from the clips and voiceover that already exist. Free. It does not make a new voiceover or new clips."
                onClick={recompose}
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
        <label className="label" htmlFor="proj-voice-script">
          {project.generated_plan_json ? "Original script" : "Script"}
        </label>
        {project.generated_plan_json && (
          <p className="text-[11px] text-ink-400 -mt-1 mb-2">
            What you wrote. The planner turns it into the spoken script below. Changes here reach the spoken
            script only when you re-plan and choose to start fresh.
          </p>
        )}
        <textarea
          id="proj-voice-script"
          aria-label="Original script"
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
        <EnhanceButton
          className="mt-2"
          text={project.original_script}
          kind="script"
          onUse={(t) => {
            setProject({ ...project, original_script: t });
            api
              .updateProject(projectId, { original_script: t })
              .catch((e) => toast.error(e.message || "Couldn't save the script"));
          }}
        />
        <label className="label mt-4 block" htmlFor="proj-creative-direction">Creative direction</label>
        <textarea
          id="proj-creative-direction"
          aria-label="Creative direction"
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

      <ProjectSettingsCard
        project={project}
        onSaved={(p) => {
          setProject(p);
          refresh(); // the aspect or cast may change what Generate video makes
        }}
        onReplan={() => {
          if (project.generated_plan_json && CAN_GENERATE.includes(project.status)) setShowReplan(true);
          else toast.info("Re-plan is available once the current step finishes");
        }}
        onRecompose={render ? recompose : undefined}
      />

      <AudioPanel
        project={project}
        onChange={(p) => setProject(p)}
        renderPlan={renderPlan}
        onRefresh={refresh}
      />

      {project.generated_plan_json && (
        <StoryboardScript
          project={project}
          onChange={(p) => {
            setProject(p);
            refresh(); // the voiceover and lip-sync clips may now be out of date
          }}
        />
      )}

      {outOfDate && renderPlan && (
        <div className="card p-4 mb-6 border-amber-500/40 bg-amber-500/10">
          <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-amber-300">
                {project.status === "completed"
                  ? "The finished video is out of date"
                  : "Some clips no longer match their shots"}
              </div>
              <p className="text-xs text-amber-200/90 mt-1">
                You edited the script, the voice or some shots after their voiceover or clips were made.
                Generate video would do this:
              </p>
              <ul className="text-xs text-amber-200/90 mt-1 list-disc pl-5 space-y-0.5">
                {renderPlanLines(renderPlan).map((l, i) => (
                  <li key={i}>{l}</li>
                ))}
              </ul>
              {project.status === "completed" && (
                <p className="text-[11px] text-amber-200/70 mt-2">
                  Re-compose is free but keeps the old voiceover and the old clips.
                </p>
              )}
              {flagged.length > 0 && (
                <FlaggedLine flagged={flagged} onGo={goToShot} className="mt-2 text-amber-200/90" />
              )}
            </div>
            <button
              className="btn-primary text-xs whitespace-nowrap"
              onClick={() => setShowPreflight(true)}
            >
              Generate video · about {usd(renderPlan.total)}
            </button>
          </div>
        </div>
      )}

      {!(outOfDate && renderPlan) && flagged.length > 0 && (
        <div className="card p-3 mb-6 border-amber-500/40 bg-amber-500/10">
          <FlaggedLine flagged={flagged} onGo={goToShot} className="text-amber-200" />
          <p className="text-[11px] text-amber-200/70 mt-1">
            The check is advice only: the video is still put together with these clips. Open a shot&apos;s takes to
            pick another one, or regenerate it.
          </p>
        </div>
      )}

      <section className="mb-6">
        <div className="flex flex-wrap items-baseline justify-between gap-2 mb-3">
          <h2 className="text-lg font-medium">Storyboard</h2>
          <div className="flex flex-wrap items-center gap-2 sm:gap-3">
            {selected.length > 0 ? (
              <>
                <span className="text-xs text-ink-300">{selected.length} selected</span>
                {blockedSelected.length > 0 && (
                  <span className="text-xs text-amber-300">
                    {blockedSelected.length === 1 ? "Shot" : "Shots"} {blockedSelected.join(", ")}{" "}
                    {blockedSelected.length === 1 ? "is" : "are"} lip-synced and the voiceover changed: use
                    Generate video, or untick {blockedSelected.length === 1 ? "it" : "them"}.
                  </span>
                )}
                <button
                  className="btn-primary text-xs disabled:opacity-50"
                  disabled={blockedSelected.length > 0}
                  title={
                    blockedSelected.length > 0
                      ? "A re-roll would lip-sync these shots to the old voiceover, so you would pay for them twice. Generate video makes the new voiceover and the clips together."
                      : undefined
                  }
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
                  <span className="hidden sm:inline text-xs text-ink-400">Drag to reorder · tick to bulk re-roll</span>
                )}
                {project.generated_plan_json && (
                  <button
                    className="btn-ghost text-xs disabled:opacity-50"
                    disabled={!canEditShots}
                    title={
                      canEditShots
                        ? "Adds a shot without a clip. Adding is free; the dialog says if lip-synced clips after it go out of date."
                        : editLockedWhy
                    }
                    onClick={() => setAdding({ after: null })}
                  >
                    + Add shot
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setShowShortcuts(true)}
                  className="hidden md:inline text-xs text-ink-400 hover:text-ink-100"
                  title="Show keyboard shortcuts"
                  aria-label="Show keyboard shortcuts"
                >
                  Shortcuts <kbd className="px-1 rounded-sm bg-ink-800 border border-ink-700 font-mono text-[10px]">?</kbd>
                </button>
                <button
                  className="btn-ghost text-xs"
                  title="Asks what to keep before anything is replaced. One paid planner call."
                  onClick={() => setShowReplan(true)}
                >
                  Re-plan…
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
            {(shotsExpanded
              ? project.shots
              : project.shots.slice(0, SHOTS_BEFORE_EXPAND)
            ).map((s, idx) => (
              <div key={s.id} id={`shot-${s.id}`} className="scroll-mt-24">
              <ShotRow
                index={idx}
                projectId={projectId}
                shot={s}
                projectAspect={project.aspect_ratio}
                editLocked={canEditShots ? null : editLockedWhy}
                deleteBlocked={deleteBlockedWhy(s)}
                onDuplicate={() => duplicateShot(s)}
                onDelete={() => setDeleting(s)}
                onTakes={() => setTakesFor(s)}
                planRow={planRows.get(s.id)}
                clipCheck={clipCheck}
                castAssets={castAssets}
                videoModels={videoModels}
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
                  const before = project;
                  setProject({ ...project, shots: next });
                  setDragIndex(null);
                  try {
                    await api.reorderShots(projectId, next.map((x) => x.id));
                    // A new order moves lip-synced shots within the voiceover:
                    // reload so their out-of-date chips and the cost show.
                    await refresh();
                  } catch (err: any) {
                    setProject(before);
                    toast.error(err.message || "Couldn't reorder the shots");
                  }
                }}
                onDragEnd={() => setDragIndex(null)}
              />
              {s.shot_type !== "end_card" && canEditShots && project.generated_plan_json && (
                <div className="group flex items-center gap-2 h-3 my-0.5">
                  <div className="flex-1 border-t border-transparent group-hover:border-ink-700 group-focus-within:border-ink-700" />
                  <button
                    type="button"
                    className="text-[11px] text-ink-500 hover:text-ink-100 focus:text-ink-100 opacity-60 group-hover:opacity-100 focus:opacity-100 px-1"
                    onClick={() => setAdding({ after: s })}
                    aria-label={`Add a shot after shot ${s.shot_order}`}
                    title={`Add a shot after shot ${s.shot_order}. No clip is made until you generate; the dialog says if lip-synced clips after it go out of date.`}
                  >
                    + Add shot here
                  </button>
                  <div className="flex-1 border-t border-transparent group-hover:border-ink-700 group-focus-within:border-ink-700" />
                </div>
              )}
              </div>
            ))}
            {!shotsExpanded && project.shots.length > SHOTS_BEFORE_EXPAND && (
              <button
                type="button"
                onClick={() => setShotsExpanded(true)}
                className="btn-ghost w-full text-sm"
                aria-label={`Show the remaining ${project.shots.length - SHOTS_BEFORE_EXPAND} shots`}
              >
                Show {project.shots.length - SHOTS_BEFORE_EXPAND} more shot
                {project.shots.length - SHOTS_BEFORE_EXPAND === 1 ? "" : "s"}
              </button>
            )}
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

      {adding && (
        <AddShotDialog
          project={project}
          after={adding.after}
          onClose={() => setAdding(null)}
          onDone={(r) => {
            setAdding(null);
            applyShotEdit(r);
          }}
        />
      )}

      {deleting && (
        <DeleteShotDialog
          project={project}
          shot={deleting}
          onClose={() => setDeleting(null)}
          onDone={(r) => {
            setDeleting(null);
            applyShotEdit(r);
          }}
        />
      )}

      {duplicating && (
        <DuplicateShotDialog
          project={project}
          shot={duplicating.shot}
          impact={duplicating.impact}
          onClose={() => setDuplicating(null)}
          onConfirm={async () => {
            const shot = duplicating.shot;
            setDuplicating(null);
            await duplicateShot(shot, true);
          }}
        />
      )}

      {takesFor && (
        <TakesDialog
          project={project}
          shot={project.shots.find((x) => x.id === takesFor.id) || takesFor}
          onClose={() => setTakesFor(null)}
          onChanged={refresh}
          onRecompose={render ? recompose : undefined}
        />
      )}

      {showReplan && (
        <ReplanDialog project={project} onClose={() => setShowReplan(false)} onChoose={replan} />
      )}

      <RenderHistory projectId={projectId} />

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
                { keys: ["G"], label: "Re-plan storyboard (asks what to keep first)" },
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
                        className="px-1.5 py-0.5 text-[10px] rounded-sm bg-ink-800 border border-ink-700 text-ink-100 font-mono"
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

function ReplanDialog({
  project,
  onClose,
  onChoose,
}: {
  project: Project;
  onClose: () => void;
  onChoose: (keepEdits: boolean) => void;
}) {
  const [cost, setCost] = useState<CostEstimate | null>(null);
  useEffect(() => {
    api.costEstimate(project.id).then(setCost).catch(() => setCost(null));
  }, [project.id]);

  const body = project.shots.filter((s) => s.shot_type !== "end_card");
  const withClips = body.filter((s) => s.clip_path).length;
  const planCall = cost?.lines.find((l) => l.item.startsWith("Storyboard plan"));
  const clipLines = cost?.lines.filter((l) => / clips \(/.test(l.item)) || [];
  const clipCost = clipLines.reduce((sum, l) => sum + l.cost, 0);
  const shots = (n: number) => `${n} shot${n === 1 ? "" : "s"}`;

  return (
    <Dialog title="Re-plan the storyboard?" onClose={onClose}>
      <p className="text-sm text-ink-200">
        The planner reads your original script and creative direction again and writes a new storyboard.
        Either choice is one paid planner call{planCall ? ` (about ${usd(planCall.cost)})` : ""}.
      </p>

      <div className="rounded-md border border-ink-700 p-3 space-y-2">
        <div className="text-sm font-medium text-ink-100">Keep my edits</div>
        <ul className="text-xs text-ink-300 list-disc pl-4 space-y-1">
          <li>
            Kept: the spoken script, end-card text and captions, and each shot&apos;s prompt, avoid text,
            duration, references, caption, model and clip.
          </li>
          <li>
            Replaced: each shot&apos;s type, and the camera direction of shots that have no clip yet. A shot
            with a clip keeps its camera direction, so its clip stays current and is not paid for again.
            Shots are matched by position (shot 1 stays shot 1), so new ones are added or the last ones
            removed if the new plan has a different number.
          </li>
          <li>
            Changes to the original script do not reach the spoken script this way. Edit the spoken script
            yourself, or start fresh.
          </li>
        </ul>
        <button className="btn-primary text-xs" onClick={() => onChoose(true)}>
          Re-plan and keep my edits
        </button>
      </div>

      <div className="rounded-md border border-red-500/40 p-3 space-y-2">
        <div className="text-sm font-medium text-ink-100">Start fresh</div>
        <ul className="text-xs text-ink-300 list-disc pl-4 space-y-1">
          <li>
            Replaced: everything. The spoken script, end-card text, captions and all {shots(project.shots.length)}{" "}
            with their edits are written again from the original script.
          </li>
          {withClips > 0 && (
            <li>
              The {withClips === 1 ? "1 clip" : `${withClips} clips`} already made {withClips === 1 ? "is" : "are"}{" "}
              discarded. Making clips for the new storyboard is paid again when you generate the video
              {clipCost > 0 ? ` (about ${usd(clipCost)} for a storyboard this size)` : ""}.
            </li>
          )}
          <li>Finished videos in the render history stay.</li>
        </ul>
        <button className="btn-ghost text-xs" onClick={() => onChoose(false)}>
          Replace everything
        </button>
      </div>

      <div className="flex justify-end">
        <button className="btn-ghost text-xs" onClick={onClose}>
          Cancel
        </button>
      </div>
    </Dialog>
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
      <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-1 mb-2">
        <h2 className="text-lg font-medium">Voiceover & captions</h2>
        <span className="text-[11px] text-ink-400">
          What is spoken and shown on screen. After saving, use Generate video to hear a changed script: it
          makes a new voiceover and keeps the clips that still match. Re-compose keeps the old voice.
        </span>
      </div>
      <label className="label" htmlFor="proj-spoken-script">Spoken script</label>
      <textarea
        id="proj-spoken-script"
        aria-label="Spoken script"
        className="input min-h-[90px]"
        value={script}
        onChange={(e) => setScript(e.target.value)}
      />
      <EnhanceButton className="mt-2" text={script} kind="script" onUse={setScript} />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
        <div>
          <label className="label" htmlFor="proj-end-card-text">End-card text</label>
          <input
            id="proj-end-card-text"
            aria-label="End-card text"
            className="input"
            value={endCard}
            onChange={(e) => setEndCard(e.target.value)}
          />
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
  projectAspect,
  editLocked,
  deleteBlocked,
  onDuplicate,
  onDelete,
  onTakes,
  planRow,
  clipCheck,
  castAssets,
  videoModels,
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
  projectAspect: string;
  /** Why adding/duplicating/deleting is off right now (null = allowed). */
  editLocked: string | null;
  /** Why this shot can't be deleted (null = it can). */
  deleteBlocked: string | null;
  onDuplicate: () => void;
  onDelete: () => void;
  onTakes: () => void;
  planRow?: RenderPlanShot;
  clipCheck: ClipCheckSettings | null;
  castAssets: Asset[];
  videoModels: { id: string; name: string; description: string }[];
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
  const [avoid, setAvoid] = useState(shot.negative_prompt || "");
  const [dur, setDur] = useState(shot.duration_seconds);
  const [refs, setRefs] = useState<number[]>(shot.reference_asset_ids_json || []);
  const [modelOverride, setModelOverride] = useState(shot.model_override || "");
  const [regenBusy, setRegenBusy] = useState(false);
  const toast = useToast();
  // Lip-synced and the voiceover is out of date: a re-roll would lip-sync to
  // the old voiceover, so only Generate video can make a clip that matches.
  const needsNewVoice = !!planRow?.needs_new_voiceover;
  const voiceFirst =
    "Off for this shot: the spoken script or voice changed, and a re-roll would lip-sync to the old voiceover, so you would pay for the clip twice. Generate video makes the new voiceover and this clip together.";

  const [checkBusy, setCheckBusy] = useState(false);
  const hasClip = shot.status === "completed" && !!shot.clip_path && shot.shot_type !== "end_card";
  const checkBlocked = checkBlockedWhy(clipCheck);
  const checkCost = checkCostText(clipCheck?.cost_per_check);
  const checked = !!shot.inspection && shot.inspection.verdict !== "skipped";
  // Paid: a vision model looks at a few frames of the current clip. Advice only.
  const checkClip = async () => {
    setCheckBusy(true);
    try {
      const r = await api.checkClip(projectId, shot.id);
      const v = r.inspection.verdict;
      if (v === "ok") toast.success(`Shot ${shot.shot_order}: looks right`);
      else if (v === "problem")
        toast.info(
          `Shot ${shot.shot_order}: ${r.inspection.issues[0]?.detail || r.inspection.summary || "something looks off"}`
        );
      else toast.info(`Shot ${shot.shot_order}: ${r.inspection.summary || "couldn't check this clip"}`);
      onChange();
    } catch (e: any) {
      toast.error(e.message || "Couldn't check the clip");
    } finally {
      setCheckBusy(false);
    }
  };

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
    onChange(); // the clip may now be out of date
  };

  return (
    <div
      className={`card p-4 transition ${dragIndex === index ? "opacity-50" : ""} ${
        selected ? "border-accent" : ""
      }`}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-2">
            {shot.shot_type !== "end_card" && (
              <input
                type="checkbox"
                checked={selected}
                onChange={onToggleSelect}
                title="Select for bulk re-roll"
                aria-label={`Select shot ${shot.shot_order} for bulk re-roll`}
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
            <span className="chip text-[10px] py-0.5">{shotTypeLabel(shot.shot_type)}</span>
            <StatusBadge status={shot.status} />
            {hasClip && <InspectionChip inspection={shot.inspection} />}
            {shot.clip_stale && (
              <span
                className="chip text-[10px] py-0.5 bg-amber-500/10 border-amber-500/40 text-amber-300"
                title={
                  (planRow?.reason ? `Why: ${planRow.reason}. ` : "") +
                  "The clip shown is the old one. " +
                  (needsNewVoice
                    ? "Generate video makes the new voiceover and a new clip to match it"
                    : "Generate video, or Regenerate shot, makes a new clip") +
                  (planRow && planRow.cost > 0 ? ` (about ${usd(planRow.cost)}).` : ".") +
                  (needsNewVoice
                    ? " Regenerate shot is off for this shot: it would use the old voiceover and you would pay for the clip twice."
                    : "") +
                  " Re-compose keeps the old clip."
                }
              >
                {/* The label follows the actual reason; a changed shape comes first (it remakes every clip). */}
                {shot.clip_aspect_ratio && shot.clip_aspect_ratio !== projectAspect
                  ? `Clip is ${shot.clip_aspect_ratio}`
                  : shot.provider === "lipsync" &&
                    (needsNewVoice || (planRow?.reason || "").includes("voiceover, which changed"))
                  ? "Voiceover changed since this clip"
                  : shot.provider === "lipsync"
                  ? "Moved or edited since this clip"
                  : "Edited since this clip"}
                {planRow && planRow.cost > 0 ? ` · new clip about ${usd(planRow.cost)}` : ""}
              </span>
            )}
          </div>
          <div className="flex gap-3">
            {shot.status === "completed" && shot.clip_path && shot.shot_type !== "end_card" && (
              <video
                key={shot.clip_path}
                src={`${api.shotClip(projectId, shot.id)}?v=${encodeURIComponent(shot.clip_path.split("/").pop() || "")}#t=0.1`}
                preload="metadata"
                className="w-20 sm:w-24 shrink-0 rounded-sm bg-black aspect-9/16 object-cover"
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
              aria-label={`Shot ${shot.shot_order} prompt`}
              className="input min-h-[60px] flex-1 min-w-0"
            />
          </div>
          {shot.shot_type !== "end_card" && (
            <div className="mt-2 space-y-2">
              <EnhanceButton
                text={prompt}
                kind="video"
                model={modelOverride || shot.provider_model || null}
                onUse={(t) => {
                  setPrompt(t);
                  api
                    .updateShot(projectId, shot.id, { prompt: t })
                    .then(onChange)
                    .catch((e) => toast.error(e.message || "Couldn't save the prompt"));
                }}
              />
              <div className="flex items-center gap-2">
                <label htmlFor={`shot-${shot.id}-avoid`} className="text-xs text-ink-300 shrink-0">
                  Avoid
                </label>
                <input
                  id={`shot-${shot.id}-avoid`}
                  value={avoid}
                  maxLength={1000}
                  onChange={(e) => setAvoid(e.target.value)}
                  onBlur={() => {
                    if (avoid === (shot.negative_prompt || "")) return;
                    api
                      .updateShot(projectId, shot.id, { negative_prompt: avoid })
                      .then(onChange)
                      .catch((e) => toast.error(e.message || "Couldn't save the avoid list"));
                  }}
                  placeholder="e.g. crowds, text on screen"
                  title="Your defaults from Settings → Prompting are added automatically"
                  className="input text-xs py-1 flex-1 min-w-0"
                />
              </div>
            </div>
          )}
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
        <div className="w-full sm:w-44 shrink-0 space-y-2 text-xs">
          <label className="label" htmlFor={`shot-${shot.id}-duration`}>Duration</label>
          <input
            id={`shot-${shot.id}-duration`}
            aria-label={`Shot ${shot.shot_order} duration in seconds`}
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
            className="btn-ghost w-full text-xs disabled:opacity-50"
            onClick={() => regenerate(true)}
            disabled={regenBusy || needsNewVoice}
            title={
              needsNewVoice
                ? voiceFirst
                : "Regenerate just this shot's clip, then re-stitch the final video using existing audio + other shots."
            }
          >
            {regenBusy ? "Regenerating…" : "Regenerate shot"}
          </button>
          <button
            className="w-full text-[11px] text-ink-400 hover:text-ink-100 disabled:opacity-50 py-0.5"
            onClick={() => regenerate(false)}
            disabled={regenBusy || needsNewVoice}
            title={
              needsNewVoice
                ? voiceFirst
                : "Just remake the clip; click Re-compose at the top when you want to update the final video."
            }
          >
            Regenerate clip only
          </button>
          {(shot.takes_count ?? 0) > 0 && shot.shot_type !== "end_card" && (
            <button
              className="btn-ghost w-full text-xs"
              onClick={onTakes}
              title="Every clip made for this shot. Pick the one the video uses; switching is free."
            >
              Takes ({shot.takes_count})
            </button>
          )}
          {hasClip && (
            <button
              className="btn-ghost w-full text-xs disabled:opacity-50"
              onClick={checkClip}
              disabled={checkBusy || !!checkBlocked}
              title={
                checkBlocked ||
                `A vision model looks at a few frames of this clip and says if anything looks off (wrong face, broken hands, text, not what the prompt asks)${
                  checkCost ? `. Costs ${checkCost}` : ""
                }. Advice only: it never stops the video.`
              }
            >
              {checkBusy ? "Checking…" : checked ? "Check again" : "Check this clip"}
              {!checkBusy && checkCost ? ` · ${checkCost}` : ""}
            </button>
          )}
          <div className="flex gap-1.5">
            {shot.shot_type !== "end_card" && (
              <button
                className="btn-ghost flex-1 text-[11px] px-2 py-1 disabled:opacity-50"
                onClick={onDuplicate}
                disabled={!!editLocked}
                title={
                  editLocked ||
                  "A copy of this shot's settings right after it, without a clip. Asks first if lip-synced clips after it would go out of date."
                }
              >
                Duplicate
              </button>
            )}
            <button
              className="btn-ghost flex-1 text-[11px] px-2 py-1 disabled:opacity-50"
              onClick={onDelete}
              disabled={!!editLocked || !!deleteBlocked}
              title={editLocked || deleteBlocked || "Delete this shot (asks first)"}
              aria-label={`Delete shot ${shot.shot_order}`}
            >
              Delete
            </button>
          </div>
          {shot.shot_type !== "end_card" && videoModels.length > 0 && (
            <div>
              <label className="label" htmlFor={`shot-${shot.id}-model`}>
                Model
              </label>
              <select
                id={`shot-${shot.id}-model`}
                aria-label={`Shot ${shot.shot_order} model override`}
                className="input text-xs"
                value={modelOverride}
                onChange={async (e) => {
                  const next = e.target.value;
                  setModelOverride(next);
                  try {
                    await api.updateShot(projectId, shot.id, { model_override: next });
                    toast.info(
                      next
                        ? "Model set. The next render or re-roll makes a new clip for this shot"
                        : "Model cleared. The next render or re-roll makes a new clip on the default model"
                    );
                    onChange();
                  } catch (err: any) {
                    toast.error(err.message || "Failed to set model");
                  }
                }}
              >
                <option value="">Default model</option>
                {videoModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id.startsWith("custom:") ? `★ ${m.name}` : m.name}
                  </option>
                ))}
                {modelOverride && !videoModels.some((m) => m.id === modelOverride) && (
                  // Kept on the shot but no longer offered (e.g. its provider is
                  // switched off in Settings): show it, so Default can clear it.
                  <option value={modelOverride}>{modelOverride} (not available)</option>
                )}
              </select>
              {modelOverride.startsWith("custom:") && (
                <div className="text-[10px] text-ink-400 mt-1">
                  Trained adapter. Used for this shot&apos;s next clip.
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      {shot.error && (
        <div className="text-xs text-accent mt-2">Error: {shot.error}</div>
      )}
    </div>
  );
}

/** "2 clips flagged by the check: shot 3 (face doesn't match Maya), shot 5 (…)" with links to the shots. */
function FlaggedLine({
  flagged,
  onGo,
  className = "",
}: {
  flagged: NonNullable<RenderPlan["flagged"]>;
  onGo: (shotId: number) => void;
  className?: string;
}) {
  return (
    <p className={`text-xs ${className}`}>
      {flagged.length === 1 ? "1 clip" : `${flagged.length} clips`} flagged by the check:{" "}
      {flagged.map((f, i) => (
        <span key={f.shot_id}>
          {i > 0 ? ", " : ""}
          <button
            type="button"
            className="underline underline-offset-2 hover:text-amber-100"
            onClick={() => onGo(f.shot_id)}
            title={f.summary || f.first_issue}
          >
            shot {f.shot_order}
          </button>
          {f.first_issue ? ` (${f.first_issue})` : ""}
        </span>
      ))}
      .
    </p>
  );
}
