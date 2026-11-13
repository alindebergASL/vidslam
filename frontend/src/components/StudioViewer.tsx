"use client";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import type { Asset, FrameAt, StudioJob } from "@/lib/api";
import { costText, ranOnText, studioStatusLabel } from "@/components/StudioLibrary";
import { downloadFile, extFromMime, extFromPath } from "@/lib/download";
import { useToast } from "@/components/Toaster";
import { sourceChipText } from "@/components/ImageOps";
import { clockText, frameSeconds } from "@/components/FrameFromVideo";

/** One thing the viewer can show: a Studio result or a cast picture. */
export type ViewerItem = {
  key: string;
  kind: "asset" | "job";
  imageUrl?: string;
  videoUrl?: string;
  label: string;
  sub: string;
  status?: string;
  job?: StudioJob;
  assetId?: number;
  selectable?: boolean;
  ownerKind?: "avatar" | "ingredient";
  /** Cast pictures: the asset (for its real file type). */
  asset?: Asset;
};

export type ViewerActions = {
  onFavorite: (job: StudioJob) => void;
  onQuickSave: (job: StudioJob) => void;
  onAssign: (job: StudioJob) => void;
  onRemix: (job: StudioJob) => void;
  onTryModel: (job: StudioJob) => void;
  /** Start a new project with this result's owner. */
  onNewProject: (job: StudioJob) => void;
  /** Finished clips: use it as a storyboard shot's clip. */
  onUseInProject: (job: StudioJob) => void;
  /** Finished pictures (Studio or Cast): start a clip from it. */
  onAnimate: (item: ViewerItem) => void;
  onDelete: (item: ViewerItem) => void;
  /** Cast pictures: add to / remove from the prompt bar's references. */
  onToggleRef?: (assetId: number) => void;
  isRef?: (assetId: number) => boolean;
  /** Finished pictures (Studio or Cast): remove the background now. */
  onRemoveBackground?: (item: ViewerItem) => void;
  /** "Remove background · about $X". */
  removeBackgroundLabel?: string;
  /** Finished pictures: open the Upscale dialog. */
  onUpscale?: (item: ViewerItem) => void;
  /** Operation results: the picture it was made from (null = gone or unknown). */
  sourceUrl?: (job: StudioJob) => string | null;
  /** Operation results: show the picture it was made from. */
  onOpenSource?: (job: StudioJob) => void;
  /** Finished clips: save a frame ("first", "last" or the playhead's time) as a new picture. */
  onSaveFrame?: (job: StudioJob, at: FrameAt) => void;
};

/** Transparent pictures (cutouts) get a checkerboard behind them. */
export function mayBeTransparent(item: { job?: StudioJob; asset?: Asset }): boolean {
  return item.job?.operation === "remove_background" || item.asset?.mime_type === "image/png";
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])';

/** The file name (without extension) and the extension a download gets;
 *  "" = take it from the file's type once fetched. */
export function downloadName(item: ViewerItem): { base: string; ext: string } {
  if (item.job) return { base: `studio-${item.job.id}`, ext: extFromPath(item.job.result_path) };
  const a = item.asset;
  return {
    base: `cast-${item.assetId ?? "picture"}`,
    ext: extFromMime(a?.mime_type) || extFromPath(a?.original_filename),
  };
}

/** "9:16" / "16:9" / "1:1" nearest to a real size. */
export function nearestAspect(w: number, h: number): string {
  const r = w / h;
  const opts: [string, number][] = [
    ["9:16", 9 / 16],
    ["1:1", 1],
    ["16:9", 16 / 9],
  ];
  return opts.reduce((a, b) => (Math.abs(b[1] - r) < Math.abs(a[1] - r) ? b : a))[0];
}

function secondsText(n: number): string {
  return `${Number.isInteger(n) ? n : n.toFixed(1)} s`;
}

function when(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || /[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

/**
 * Full-screen viewer for Studio results and cast pictures: the whole picture
 * (not the cropped tile), clips with controls and sound, the prompt and
 * details, and the same actions as the tile. ← / → or a swipe step through
 * the list; Escape or ✕ closes it and focus goes back to the tile.
 */
export function StudioViewer({
  items,
  openKey,
  onNavigate,
  onClose,
  actions,
}: {
  items: ViewerItem[];
  openKey: string;
  onNavigate: (key: string) => void;
  onClose: () => void;
  actions: ViewerActions;
}) {
  const titleId = useId();
  const toast = useToast();
  const boxRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const swipe = useRef<{ x: number; y: number } | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [compare, setCompare] = useState(false);
  // An original that wouldn't load (deleted since): no Compare for it.
  const [brokenOriginal, setBrokenOriginal] = useState<string | null>(null);
  // A clip paused here: the time "Save this frame" saves (null while it plays).
  const [frameTime, setFrameTime] = useState<number | null>(null);
  // Phone: the first/last frame buttons are shown.
  const [moreFrames, setMoreFrames] = useState(false);
  // The key shown now, for returning focus to its tile on close.
  const keyRef = useRef(openKey);
  keyRef.current = openKey;

  const idx = items.findIndex((i) => i.key === openKey);
  const item = idx >= 0 ? items[idx] : null;
  useEffect(() => {
    setFrameTime(null);
    setMoreFrames(false);
  }, [openKey]);
  const prev = idx > 0 ? items[idx - 1] : null;
  const next = idx >= 0 && idx < items.length - 1 ? items[idx + 1] : null;

  // The item went away without the viewer being told where to go (a reload
  // or a filter change): close rather than show something the user didn't
  // pick. (Deleting from the viewer moves to the next result first.)
  useEffect(() => {
    if (!item) onClose();
  }, [item, onClose]);

  // Keyboard: Escape closes, arrows step (unless a control has them), Tab
  // stays inside; the page behind doesn't scroll; focus returns on close.
  const navRef = useRef({ prev, next, onClose, onNavigate });
  navRef.current = { prev, next, onClose, onNavigate };
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      const n = navRef.current;
      if (e.key === "Escape") {
        e.preventDefault();
        n.onClose();
        return;
      }
      const t = e.target as HTMLElement | null;
      const inControl = t && (t.tagName === "VIDEO" || t.tagName === "INPUT" || t.tagName === "TEXTAREA");
      if (!inControl && e.key === "ArrowLeft" && n.prev) {
        e.preventDefault();
        n.onNavigate(n.prev.key);
        return;
      }
      if (!inControl && e.key === "ArrowRight" && n.next) {
        e.preventDefault();
        n.onNavigate(n.next.key);
        return;
      }
      const box = boxRef.current;
      if (e.key !== "Tab" || !box) return;
      const list = Array.from(box.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (list.length === 0) return;
      const first = list[0];
      const last = list[list.length - 1];
      if (e.shiftKey && (document.activeElement === first || !box.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !box.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      // Back to the tile of the result shown last (it may differ from the
      // one opened), else whatever opened the viewer.
      const tile = document.querySelector<HTMLElement>(`[data-view-key="${CSS.escape(keyRef.current)}"]`);
      if (tile) tile.focus();
      else if (opener && document.contains(opener)) opener.focus?.();
    };
  }, []);

  // A focused control that went away (Next on the last result, Save once
  // saved, Delete): keep focus inside the viewer.
  useEffect(() => {
    const box = boxRef.current;
    if (box && !box.contains(document.activeElement)) closeRef.current?.focus();
  });

  // Clips start playing; where sound isn't allowed without a tap (iPhone),
  // they play muted instead of sitting still (the controls unmute).
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    v.play().catch(() => {
      v.muted = true;
      v.play().catch(() => {});
    });
  }, [openKey]);

  // Each result opens on its own; Compare is per result.
  useEffect(() => setCompare(false), [openKey]);

  // Warm the neighbours so stepping through pictures is instant.
  useEffect(() => {
    for (const n of [prev, next]) {
      if (n?.imageUrl) {
        const img = new Image();
        img.src = n.imageUrl;
      }
    }
  }, [prev, next]);

  if (!item) return null;

  const job = item.job;
  const isVideo = Boolean(item.videoUrl);
  const url = item.videoUrl || item.imageUrl || "";
  const finished = item.status === "completed" || item.status === "saved";
  const cost = job ? costText(job) : null;
  const act = "chip text-xs py-1.5";
  const checker = mayBeTransparent(item);
  const fromText = job ? sourceChipText(job) : null;
  // A saved frame's source is a clip (or a video that wasn't kept): there is
  // no picture to compare it with.
  const sourceOf =
    job?.operation && job.operation !== "extract_frame" && finished && actions.sourceUrl ? actions.sourceUrl(job) : null;
  const originalUrl = sourceOf && sourceOf !== brokenOriginal ? sourceOf : null;
  const showCompare = compare && Boolean(originalUrl) && Boolean(item.imageUrl);
  const isPicture = !isVideo && Boolean(item.imageUrl) && (item.kind === "asset" || finished);
  // Closing first keeps a dialog the action opens on top, and Remix shows
  // the prompt bar it filled.
  const closeThen = (fn: () => void) => () => {
    onClose();
    fn();
  };

  const details: string[] = [];
  if (job) {
    // The real length and size once the clip landed; older jobs only know
    // what was asked for.
    if (isVideo) {
      details.push(
        job.result_duration_seconds
          ? `Clip · ${secondsText(job.result_duration_seconds)}`
          : `Clip · ${Math.round(job.duration_seconds || 0)} s (requested)`
      );
    } else {
      details.push("Image");
    }
    if (job.provider_model) details.push(job.provider_model);
    // Reference pictures sent a Sogni model to one of Sogni's editors.
    const ranOn = ranOnText(job);
    if (ranOn) details.push(ranOn);
    if (job.result_width && job.result_height) {
      details.push(`${job.result_width}×${job.result_height} (${nearestAspect(job.result_width, job.result_height)})`);
    } else if (job.aspect_ratio) {
      details.push(isVideo ? `${job.aspect_ratio} (requested)` : job.aspect_ratio);
    } else {
      // Auto: a clip is made at the model's own default shape.
      details.push(isVideo ? "Model default" : job.owner_kind === "avatar" ? "9:16 (default)" : "16:9 (default)");
    }
    if (cost) details.push(cost);
    if (job.status === "saved") details.push("saved to the cast");
    else if (!finished) details.push(studioStatusLabel(job));
    const made = when(job.created_at);
    if (made) details.push(made);
  } else {
    details.push(item.sub);
  }

  return createPortal(
    <div
      ref={boxRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-40 bg-ink-950 flex flex-col text-ink-100"
    >
      <div className="flex items-center gap-3 px-3 sm:px-5 py-2 border-b border-white/10">
        <span className="text-xs text-ink-400 tabular-nums shrink-0" aria-live="polite" aria-atomic="true">
          {idx + 1} / {items.length}
        </span>
        <h2 id={titleId} className="text-sm font-medium truncate flex-1 min-w-0">
          {job?.label ? `${job.label} · ` : ""}
          {item.label}
        </h2>
        {url && (
          <a
            href={url}
            onClick={(e) => {
              if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
              e.preventDefault();
              const n = downloadName(item);
              downloadFile(url, n.base, n.ext).then((ok) => {
                if (!ok)
                  toast.error("Couldn't download the file", { action: { label: "Open", href: url }, key: "download" });
              });
            }}
            className="chip text-xs py-1 shrink-0"
            title="Download the full-size file"
          >
            Download
          </a>
        )}
        <button
          ref={closeRef}
          onClick={onClose}
          className="shrink-0 w-9 h-9 rounded-full text-lg text-ink-200 hover:text-white hover:bg-white/10"
          aria-label="Close preview"
          title="Close (Esc)"
        >
          ✕
        </button>
      </div>

      <div
        // pan-y: horizontal touch moves stay with the page (a swipe), vertical
        // scroll and pinch-zoom stay with the browser.
        className="relative flex-1 min-h-0 flex items-center justify-center p-2 sm:px-16 touch-pan-y touch-pinch-zoom"
        onPointerDown={(e) => {
          // Not on the clip: dragging its timeline isn't a swipe.
          if (e.pointerType === "mouse" || !e.isPrimary || (e.target as HTMLElement).tagName === "VIDEO") return;
          swipe.current = { x: e.clientX, y: e.clientY };
        }}
        onPointerUp={(e) => {
          const s = swipe.current;
          swipe.current = null;
          if (!s) return;
          const dx = e.clientX - s.x;
          const dy = e.clientY - s.y;
          if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
          const to = dx < 0 ? next : prev;
          if (to) onNavigate(to.key);
        }}
        onPointerCancel={() => (swipe.current = null)}
      >
        {isVideo ? (
          <video
            ref={videoRef}
            key={item.key}
            onPause={(e) => setFrameTime(e.currentTarget.currentTime)}
            onSeeked={(e) => e.currentTarget.paused && setFrameTime(e.currentTarget.currentTime)}
            onPlay={() => setFrameTime(null)}
            // #t=0.1: a frame shows on iPhones even before it plays.
            src={`${item.videoUrl}#t=0.1`}
            className="max-w-full max-h-full object-contain"
            controls
            autoPlay
            loop
            playsInline
            aria-label={item.label}
          />
        ) : showCompare ? (
          <div className="w-full h-full flex flex-col sm:flex-row gap-2 min-h-0">
            {[
              { src: originalUrl!, cap: "Original", tr: false },
              { src: item.imageUrl!, cap: "Result", tr: checker },
            ].map((p) => (
              <figure key={p.cap} className="flex-1 min-h-0 min-w-0 flex flex-col items-center gap-1">
                <div className="flex-1 min-h-0 w-full flex items-center justify-center">
                  <img
                    src={p.src}
                    alt={`${p.cap}: ${item.label}`}
                    className={clsx("max-w-full max-h-full object-contain select-none", p.tr && "checker")}
                    draggable={false}
                    onError={
                      p.cap === "Original"
                        ? () => {
                            setBrokenOriginal(p.src);
                            setCompare(false);
                            toast.info("The original was deleted", { key: "original-gone" });
                          }
                        : undefined
                    }
                  />
                </div>
                <figcaption className="text-[11px] text-ink-300">{p.cap}</figcaption>
              </figure>
            ))}
          </div>
        ) : item.imageUrl ? (
          <img
            key={item.key}
            src={item.imageUrl}
            alt={item.label}
            className={clsx("max-w-full max-h-full object-contain select-none", checker && "checker")}
            draggable={false}
          />
        ) : (
          <span className="text-sm text-ink-400">Preview unavailable</span>
        )}
        {prev && (
          <button
            onClick={() => onNavigate(prev.key)}
            className="absolute left-1 sm:left-3 top-1/2 -translate-y-1/2 w-10 h-10 sm:w-11 sm:h-11 rounded-full bg-black/60 border border-white/15 text-xl text-white hover:bg-black/80"
            aria-label="Previous"
            title="Previous (←)"
          >
            ‹
          </button>
        )}
        {next && (
          <button
            onClick={() => onNavigate(next.key)}
            className="absolute right-1 sm:right-3 top-1/2 -translate-y-1/2 w-10 h-10 sm:w-11 sm:h-11 rounded-full bg-black/60 border border-white/15 text-xl text-white hover:bg-black/80"
            aria-label="Next"
            title="Next (→)"
          >
            ›
          </button>
        )}
      </div>

      <div className="border-t border-white/10 px-3 sm:px-5 py-3 max-h-[38vh] overflow-y-auto space-y-2">
        <div className="text-[11px] text-ink-400 flex flex-wrap gap-x-3 gap-y-1">
          {details.map((d) => (
            <span key={d} className={d === cost && job?.cost_is_estimate ? "italic" : undefined}>
              {d}
            </span>
          ))}
          {job && fromText && (
            actions.onOpenSource && (job.source_job_id || job.source_asset_id) ? (
              <button
                type="button"
                onClick={() => actions.onOpenSource!(job)}
                className="underline underline-offset-2 hover:text-ink-100"
                title="Show the picture this was made from"
              >
                {fromText}
              </button>
            ) : (
              <span>{fromText}</span>
            )
          )}
        </div>
        {job?.prompt && <p className="text-sm whitespace-pre-wrap break-words">{job.prompt}</p>}
        {job?.negative_prompt && (
          <p className="text-xs text-ink-400 whitespace-pre-wrap break-words">Avoid: {job.negative_prompt}</p>
        )}
        <div className="flex flex-wrap gap-2 pt-1">
          {job && (
            <button
              onClick={() => actions.onFavorite(job)}
              aria-pressed={Boolean(job.favorite)}
              className={clsx(act, job.favorite && "text-yellow-300")}
            >
              {job.favorite ? "★ Favorite" : "☆ Favorite"}
            </button>
          )}
          {job && job.status === "completed" && !isVideo && (
            <button
              onClick={() => actions.onQuickSave(job)}
              className={act}
              title={`Save to this job's ${
                job.owner_kind === "ingredient" ? "scene as a reference" : "character as a lifestyle shot"
              }`}
            >
              Save
            </button>
          )}
          {job && finished && !isVideo && (
            <button onClick={closeThen(() => actions.onAssign(job))} className={act}>
              Assign to…
            </button>
          )}
          {originalUrl && item.imageUrl && (
            <button
              onClick={() => setCompare((v) => !v)}
              aria-pressed={compare}
              className={clsx(act, compare && "chip-active")}
              title="Show the original next to this result"
            >
              Compare with original
            </button>
          )}
          {isPicture && actions.onRemoveBackground && (
            <button
              onClick={closeThen(() => actions.onRemoveBackground!(item))}
              className={act}
              title="Make a copy with the background cut out (transparent PNG); the original stays"
            >
              {actions.removeBackgroundLabel || "Remove background"}
            </button>
          )}
          {isPicture && actions.onUpscale && (
            <button
              onClick={closeThen(() => actions.onUpscale!(item))}
              className={act}
              title="Make a larger copy (×2 or ×4); the price shows before it runs"
            >
              Upscale…
            </button>
          )}
          {job && finished && !job.operation && (
            <button
              onClick={closeThen(() => actions.onRemix(job))}
              className={act}
              title="Load this result's prompt, avoid list, references, model and aspect into the prompt bar"
            >
              Remix
            </button>
          )}
          {job && finished && isVideo && actions.onSaveFrame && (
            <>
              <button
                onClick={() => {
                  const v = videoRef.current;
                  v?.pause();
                  actions.onSaveFrame!(job, v ? frameSeconds(v.currentTime) : "first");
                }}
                className={act}
                title="Make a new picture from the frame showing now (free); pause where you want first"
              >
                Save this frame{frameTime !== null ? ` (${clockText(frameTime)})` : ""}
              </button>
              {/* On a phone the other frame buttons wait under More. */}
              <button
                type="button"
                onClick={() => setMoreFrames((v) => !v)}
                aria-expanded={moreFrames}
                className={clsx(act, "sm:hidden")}
              >
                {moreFrames ? "Less" : "More…"}
              </button>
              <button
                onClick={() => actions.onSaveFrame!(job, "last")}
                className={clsx(act, !moreFrames && "max-sm:hidden")}
                title="Make a new picture from the clip's last frame (free), e.g. to start the next clip from it"
              >
                Save last frame as picture
              </button>
              <button
                onClick={() => actions.onSaveFrame!(job, "first")}
                className={clsx(act, !moreFrames && "max-sm:hidden")}
                title="Make a new picture from the clip's first frame (free)"
              >
                Save first frame
              </button>
            </>
          )}
          {job && finished && job.operation !== "extract_frame" && (
            <button onClick={closeThen(() => actions.onTryModel(job))} className={act}>
              Try another model…
            </button>
          )}
          {job && finished && !isVideo && (
            <a href={`/ambient/new?studio_job=${job.id}`} className={act}>
              Ambient scene →
            </a>
          )}
          {job && finished && isVideo && (
            <button
              onClick={closeThen(() => actions.onUseInProject(job))}
              className={act}
              title="Use this clip as a shot in a storyboard project (free)"
            >
              Use in project…
            </button>
          )}
          {((job && finished && !isVideo) || (item.kind === "asset" && item.imageUrl)) && (
            <button
              onClick={closeThen(() => actions.onAnimate(item))}
              className={act}
              title="Make a clip that starts from this picture"
            >
              Animate this
            </button>
          )}
          {job && finished && (
            <button onClick={closeThen(() => actions.onNewProject(job))} className={act}>
              New project →
            </button>
          )}
          {item.kind === "asset" && item.selectable && item.assetId && actions.onToggleRef && (
            <button onClick={() => actions.onToggleRef!(item.assetId!)} className={act}>
              {actions.isRef?.(item.assetId) ? "✓ Used as reference" : "+ Use as reference"}
            </button>
          )}
          <button onClick={() => actions.onDelete(item)} className={clsx(act, "hover:text-accent")}>
            Delete
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
