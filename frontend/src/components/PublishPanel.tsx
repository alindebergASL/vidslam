"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { api, PublishJob, YouTubeStatus } from "@/lib/api";

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

  const loadJobs = () => api.renderPublishJobs(renderId).then(setJobs).catch(() => {});
  useEffect(() => {
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
  }, [projectId, renderId]);

  const active = jobs.some((j) => j.status === "queued" || j.status === "uploading");
  useEffect(() => {
    if (!active) return;
    const t = setInterval(loadJobs, 2500);
    return () => clearInterval(t);
  }, [active]);

  const submit = async () => {
    setBusy(true);
    try {
      await api.publishYouTube(renderId, {
        title, description, category_id: category, privacy,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        publish_at: schedule ? new Date(schedule).toISOString() : null,
        made_for_kids: kids, contains_synthetic_media: synthetic, set_thumbnail: thumb,
      });
      toast.success(schedule ? "Upload queued — scheduled to go live" : "Upload queued");
      setOpen(false);
      loadJobs();
    } catch (e: any) {
      toast.error(e.message || "Publish failed");
    } finally {
      setBusy(false);
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
          <button type="button" className="btn-primary text-sm" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
            {open ? "Close" : "Publish…"}
          </button>
        ) : (
          <Link href="/settings#youtube" className="btn-ghost text-sm">Set up YouTube</Link>
        )}
      </div>

      {open && yt?.connected && (
        <div className="mt-4 space-y-3">
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
              <input type="checkbox" className="accent-[#7C5CFF] mt-1" checked={synthetic} onChange={(e) => setSynthetic(e.target.checked)} />
              <span>
                Disclose altered or synthetic content <span className="text-ink-400 text-xs">(YouTube requires this for realistic AI-generated video)</span>
              </span>
            </label>
            {!synthetic && <p className="text-[11px] text-amber-200 ml-6">Only untick this if the video isn&apos;t realistic AI content.</p>}
            <label className="flex items-center gap-2">
              <input type="checkbox" className="accent-[#7C5CFF]" checked={kids} onChange={(e) => setKids(e.target.checked)} />
              Made for kids
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" className="accent-[#7C5CFF]" checked={thumb} onChange={(e) => setThumb(e.target.checked)} />
              Use this render&apos;s thumbnail <span className="text-ink-400 text-xs">(custom thumbnails need a verified channel)</span>
            </label>
          </div>
          <button type="button" className="btn-primary w-full disabled:opacity-50" disabled={busy || !title.trim()} onClick={submit}>
            {busy ? "Queuing…" : schedule ? "Upload & schedule" : `Upload as ${privacy}`}
          </button>
        </div>
      )}

      {jobs.length > 0 && (
        <ul className="mt-4 space-y-2" aria-label="Uploads">
          {jobs.map((j) => {
            const pct = j.bytes_total ? Math.round((100 * j.bytes_sent) / j.bytes_total) : 0;
            return (
              <li key={j.id} className="rounded-lg border border-ink-800 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium truncate">{j.title}</span>
                  <span className={clsx("text-xs rounded-full border px-2 py-0.5",
                    j.status === "failed" ? "border-red-500/40 text-red-300" :
                    j.status === "published" || j.status === "scheduled" ? "border-emerald-500/40 text-emerald-300" :
                    "border-amber-500/40 text-amber-300")}>
                    {j.status}{j.status === "scheduled" && j.publish_at ? ` · ${new Date(j.publish_at + "Z").toLocaleString()}` : ""}
                  </span>
                </div>
                {j.status === "uploading" && (
                  <div className="mt-2">
                    <div className="h-1.5 rounded-full bg-ink-800 overflow-hidden">
                      <div className="h-full grad" style={{ width: `${pct}%` }} />
                    </div>
                    <div className="text-[11px] text-ink-400 mt-1">{fmtBytes(j.bytes_sent)} / {fmtBytes(j.bytes_total)}</div>
                  </div>
                )}
                {j.external_url && (
                  <a href={j.external_url} target="_blank" rel="noreferrer" className="text-xs text-iris-soft hover:underline">
                    {j.external_url}
                  </a>
                )}
                {j.error && <div className="text-[11px] text-ink-300 mt-1">{j.error}</div>}
                {j.status === "failed" && (
                  <button type="button" className="btn-ghost text-xs mt-2"
                          onClick={() => api.retryPublishJob(j.id).then(loadJobs).catch((e) => toast.error(e.message))}>
                    Retry (resumes the upload)
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
