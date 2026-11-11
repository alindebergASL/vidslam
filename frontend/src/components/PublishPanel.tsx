"use client";
import Link from "next/link";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { PublishStateBadge, PublishWarning } from "@/components/PublishState";
import { api, ApiError, PublishJob, YouTubeStatus } from "@/lib/api";

const CATEGORIES = [
  { id: "10", label: "Music" },
  { id: "22", label: "People & Blogs" },
  { id: "24", label: "Entertainment" },
  { id: "27", label: "Education" },
  { id: "26", label: "Howto & Style" },
];

function fmtBytes(n: number) {
  if (n > 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n > 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  return `${Math.round(n / 1e3)} KB`;
}

/** Upload a finished render to the connected YouTube channel. */
export function PublishPanel({ projectId, renderId }: { projectId: number; renderId: number }) {
  const toast = useToast();
  const [yt, setYt] = useState<YouTubeStatus | null>(null);
  const [jobs, setJobs] = useState<PublishJob[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [category, setCategory] = useState("22");
  const [privacy, setPrivacy] = useState<"private" | "unlisted" | "public">("private");
  const [schedule, setSchedule] = useState("");
  const [synthetic, setSynthetic] = useState(true);
  const [kids, setKids] = useState(false);
  const [thumb, setThumb] = useState(true);
  // Set when the backend refused a second upload of this render (409): holds
  // its plain-language question until the user confirms or cancels.
  const [dupPrompt, setDupPrompt] = useState<string | null>(null);
  // The user chose "Upload again" on purpose, so the next submit confirms it.
  const [uploadAgain, setUploadAgain] = useState(false);
  const [checking, setChecking] = useState<number | null>(null);

  // The render page switches versions without remounting this panel: drop
  // replies for a render that is no longer shown.
  const shownRender = useRef(renderId);
  useLayoutEffect(() => {
    shownRender.current = renderId;
  }, [renderId]);
  const loadJobs = useCallback(
    () =>
      api
        .renderPublishJobs(renderId)
        .then((js) => {
          if (shownRender.current === renderId) setJobs(js);
        })
        .catch(() => {}),
    [renderId]
  );
  useEffect(() => {
    setJobs([]);
    setOpen(false);
    setDupPrompt(null);
    setUploadAgain(false);
    api.youtubeStatus().then(setYt).catch(() => setYt(null));
    loadJobs();
    api.publishMeta(projectId).then((m) => {
      setTitle(m.title);
      setDescription(m.description);
      setTags(m.tags.join(", "));
      setCategory(m.category_id);
      setSynthetic(m.contains_synthetic_media);
      setKids(m.made_for_kids);
    }).catch(() => {});
  }, [projectId, renderId, loadJobs]);

  const active = jobs.some((j) => j.status === "queued" || j.status === "uploading");
  useEffect(() => {
    if (!active) return;
    const t = setInterval(loadJobs, 2500);
    return () => clearInterval(t);
  }, [active, loadJobs]);

  // A render already on YouTube (or on its way) gets "open / upload again"
  // instead of a live "Publish…" button. Removed videos don't count.
  const live = jobs.find(
    (j) => j.external_url && j.state !== "failed" && j.state !== "uploading" && j.state !== "removed" && !j.yt_removed
  );
  const inFlight = jobs.find((j) => j.state === "uploading" || j.status === "queued");

  const submit = async (confirmDuplicate = false) => {
    setBusy(true);
    try {
      await api.publishYouTube(renderId, {
        title, description, category_id: category, privacy,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        publish_at: schedule ? new Date(schedule).toISOString() : null,
        made_for_kids: kids, contains_synthetic_media: synthetic, set_thumbnail: thumb,
        confirm_duplicate: confirmDuplicate || uploadAgain,
      });
      toast.success(schedule ? "Upload queued — scheduled to go live" : "Upload queued");
      setOpen(false);
      setDupPrompt(null);
      setUploadAgain(false);
      loadJobs();
    } catch (e: any) {
      // Only the duplicate refusal carries X-Duplicate-Of; other 409s (file
      // expired, YouTube not connected) are plain errors.
      if (e instanceof ApiError && e.status === 409 && !confirmDuplicate && e.headers.get("X-Duplicate-Of")) {
        setDupPrompt(e.detail || "This version is already on YouTube. Upload it again as a separate video?");
        loadJobs();
      } else {
        toast.error(e.message || "Publish failed");
      }
    } finally {
      setBusy(false);
    }
  };

  const retry = async (jobId: number) => {
    try {
      await api.retryPublishJob(jobId);
    } catch (e: any) {
      toast.error(e instanceof ApiError ? e.detail || e.message : e.message || "Retry failed");
    }
    loadJobs();
  };

  const checkOnYouTube = async (jobId: number) => {
    setChecking(jobId);
    try {
      await api.refreshPublishJob(jobId);
      await loadJobs();
    } catch (e: any) {
      toast.error(e.message || "Couldn't check YouTube");
    } finally {
      setChecking(null);
    }
  };

  return (
    <section className="card p-4" aria-labelledby={`pub-${renderId}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id={`pub-${renderId}`} className="text-base font-medium">Publish to YouTube</h2>
          <p className="text-xs text-ink-400 mt-0.5">
            {yt?.connected
              ? `Uploads to ${yt.channel?.title ?? "your channel"} with the AI-content disclosure set.`
              : yt?.configured
              ? "Connect a channel in Settings to upload directly."
              : "Set up YouTube in Settings (needs a Google OAuth client)."}
          </p>
        </div>
        {yt?.connected ? (
          live && !open ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-emerald-300">Already on YouTube</span>
              <a href={live.external_url} target="_blank" rel="noreferrer" className="btn-ghost text-sm">
                Open
              </a>
              <button
                type="button"
                className="btn-ghost text-sm"
                onClick={() => {
                  setUploadAgain(true);
                  setOpen(true);
                }}
              >
                Upload again…
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="btn-primary text-sm"
              onClick={() => {
                setOpen((v) => !v);
                setDupPrompt(null);
                setUploadAgain(false);
              }}
              aria-expanded={open}
            >
              {open ? "Close" : "Publish…"}
            </button>
          )
        ) : (
          <Link href="/settings#youtube" className="btn-ghost text-sm">Set up YouTube</Link>
        )}
      </div>

      {open && yt?.connected && (
        <div className="mt-4 space-y-3">
          {(uploadAgain || (inFlight && !dupPrompt)) && (
            <p className="text-xs text-amber-200 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2">
              {live || uploadAgain
                ? "This version is already on YouTube. Uploading again makes a separate video on your channel."
                : "This version is already uploading to YouTube. Uploading again makes a separate video."}
            </p>
          )}
          <div>
            <label className="label" htmlFor={`t-${renderId}`}>Title · {title.length}/100</label>
            <input id={`t-${renderId}`} className="input" maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor={`d-${renderId}`}>Description</label>
            <textarea id={`d-${renderId}`} className="input min-h-[120px]" maxLength={5000} value={description}
                      onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor={`g-${renderId}`}>Tags (comma separated)</label>
              <input id={`g-${renderId}`} className="input" value={tags} onChange={(e) => setTags(e.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor={`c-${renderId}`}>Category</label>
              <select id={`c-${renderId}`} className="input" value={category} onChange={(e) => setCategory(e.target.value)}>
                {CATEGORIES.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
              </select>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <div className="label">Visibility</div>
              <div role="group" aria-label="Visibility" className="flex gap-2">
                {(["private", "unlisted", "public"] as const).map((p) => (
                  <button key={p} type="button" aria-pressed={privacy === p} disabled={!!schedule}
                          onClick={() => setPrivacy(p)} className={clsx("chip text-xs capitalize", privacy === p && !schedule && "chip-active")}>
                    {p}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="label" htmlFor={`s-${renderId}`}>Schedule (optional)</label>
              <input id={`s-${renderId}`} type="datetime-local" className="input" value={schedule}
                     onChange={(e) => setSchedule(e.target.value)} />
              {schedule && <p className="text-[11px] text-ink-400 mt-1">Uploads as private and goes public at this time.</p>}
            </div>
          </div>
          <div className="space-y-1.5 text-sm">
            <label className="flex items-start gap-2">
              <input type="checkbox" className="accent-iris mt-1" checked={synthetic} onChange={(e) => setSynthetic(e.target.checked)} />
              <span>
                Disclose altered or synthetic content <span className="text-ink-400 text-xs">(YouTube requires this for realistic AI-generated video)</span>
              </span>
            </label>
            {!synthetic && <p className="text-[11px] text-amber-200 ml-6">Only untick this if the video isn&apos;t realistic AI content.</p>}
            <label className="flex items-center gap-2">
              <input type="checkbox" className="accent-iris" checked={kids} onChange={(e) => setKids(e.target.checked)} />
              Made for kids
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" className="accent-iris" checked={thumb} onChange={(e) => setThumb(e.target.checked)} />
              Use this render&apos;s thumbnail <span className="text-ink-400 text-xs">(custom thumbnails need a verified channel)</span>
            </label>
          </div>
          {dupPrompt ? (
            <div role="alertdialog" aria-label="Upload again?" className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 space-y-2">
              <p className="text-sm text-amber-100">{dupPrompt}</p>
              <div className="flex flex-wrap gap-2">
                <button type="button" className="btn-primary text-sm disabled:opacity-50" disabled={busy} onClick={() => submit(true)}>
                  {busy ? "Queuing…" : "Upload again as a separate video"}
                </button>
                <button type="button" className="btn-ghost text-sm" onClick={() => { setDupPrompt(null); setOpen(false); }}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="btn-primary w-full disabled:opacity-50" disabled={busy || !title.trim()} onClick={() => submit()}>
              {busy ? "Queuing…" : schedule ? "Upload & schedule" : `Upload as ${privacy}`}
            </button>
          )}
        </div>
      )}

      {jobs.length > 0 && (
        <ul className="mt-4 space-y-2" aria-label="Uploads">
          {jobs.map((j) => {
            const pct = j.bytes_total ? Math.round((100 * j.bytes_sent) / j.bytes_total) : 0;
            return (
              <li key={j.id} className="rounded-lg border border-ink-800 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium truncate min-w-0">{j.yt_title || j.title}</span>
                  <PublishStateBadge state={j.state} scheduledFor={j.yt_publish_at || j.publish_at} />
                </div>
                <PublishWarning text={j.warning} className="mt-1.5" />
                {j.status === "uploading" && (
                  <div className="mt-2">
                    <div className="h-1.5 rounded-full bg-ink-800 overflow-hidden">
                      <div className="h-full grad" style={{ width: `${pct}%` }} />
                    </div>
                    <div className="text-[11px] text-ink-400 mt-1">{fmtBytes(j.bytes_sent)} / {fmtBytes(j.bytes_total)}</div>
                  </div>
                )}
                {j.external_url && (
                  <a href={j.external_url} target="_blank" rel="noreferrer" className="text-xs text-iris-soft hover:underline break-all">
                    {j.external_url}
                  </a>
                )}
                {j.error && <div className="text-[11px] text-ink-300 mt-1">{j.error}</div>}
                {j.external_id && j.state !== "uploading" && j.state !== "failed" && (
                  <button type="button" className="btn-ghost text-xs mt-2 mr-2 disabled:opacity-50" disabled={checking === j.id}
                          onClick={() => checkOnYouTube(j.id)}>
                    {checking === j.id ? "Checking…" : "Check on YouTube"}
                  </button>
                )}
                {j.status === "failed" && j.external_id && (
                  // YouTube has (and rejected) this video: a retry would be a
                  // new video. Publish… above uploads again on purpose.
                  <div className="text-[11px] text-ink-400 mt-1">
                    YouTube has this upload already, so it can&apos;t be retried. Use Publish… to upload it again.
                  </div>
                )}
                {j.status === "failed" && !j.external_id && (live || inFlight) && (
                  <div className="text-[11px] text-ink-400 mt-1">
                    {live ? "This version is already on YouTube" : "This version is uploading"} from another
                    attempt, so this one isn&apos;t retried.
                  </div>
                )}
                {j.status === "failed" && !j.external_id && !live && !inFlight && (
                  <button type="button" className="btn-ghost text-xs mt-2" onClick={() => retry(j.id)}>
                    Retry upload
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
