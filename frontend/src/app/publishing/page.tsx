"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { fmtLocal, PublishStateBadge, PublishWarning } from "@/components/PublishState";
import { useToast } from "@/components/Toaster";
import { errText, VideoThumb } from "@/components/VideoThumb";
import {
  api,
  NotPostedItem,
  PublishFilter,
  PublishOverviewItem,
  YouTubeStatus,
} from "@/lib/api";

export default function PublishingPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

const FILTERS: { id: PublishFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "scheduled", label: "Scheduled" },
  { id: "published", label: "Published" },
  { id: "failed", label: "Failed" },
  { id: "uploading", label: "Uploading" },
];

const PAGE = 30;
const REFRESH_CHUNK = 200;

type Tab = "uploads" | "not_posted";

function Inner() {
  const [tab, setTab] = useState<Tab>("uploads");
  const [yt, setYt] = useState<YouTubeStatus | null>(null);

  useEffect(() => {
    api.youtubeStatus().then(setYt).catch(() => setYt(null));
  }, []);

  return (
    <div className="p-4 md:p-8 max-w-6xl">
      <header className="mb-5">
        <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">Publishing</h1>
        <p className="text-sm text-ink-300 mt-1">
          Every upload to YouTube from your projects and ambient scenes, and what YouTube says about it now.
        </p>
      </header>

      {yt && !yt.connected && (
        <div className="card p-3 mb-4 text-sm flex flex-wrap items-center justify-between gap-2">
          <span className="text-ink-200">
            {yt.configured
              ? "YouTube isn't connected, so uploads can't be checked."
              : "YouTube isn't set up yet."}
          </span>
          <Link href="/settings#youtube" className="btn-ghost text-sm">Connect YouTube in Settings</Link>
        </div>
      )}

      <div role="tablist" aria-label="Publishing views" className="flex gap-1 border-b border-ink-800 mb-4">
        {([
          ["uploads", "Uploads"],
          ["not_posted", "Not posted yet"],
        ] as [Tab, string][]).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={clsx(
              "px-3 py-2 text-sm -mb-px border-b-2 transition",
              tab === id ? "border-iris text-white" : "border-transparent text-ink-300 hover:text-ink-100"
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "uploads" ? <Uploads connected={!!yt?.connected} /> : <NotPosted />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Uploads tab

function Uploads({ connected }: { connected: boolean }) {
  const toast = useToast();
  const [filter, setFilter] = useState<PublishFilter>("all");
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [items, setItems] = useState<PublishOverviewItem[]>([]);
  const [counts, setCounts] = useState<Record<PublishFilter, number> | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState<Set<number>>(new Set());
  const [refreshingAll, setRefreshingAll] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  // Debounce the search box so typing doesn't fire a request per key.
  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  const load = useCallback(
    async (limit = PAGE) => {
      const mine = ++seq.current;
      try {
        const page = await api.publishingJobs({ status: filter, q, limit });
        if (mine !== seq.current) return;
        setItems(page.items);
        setNext(page.next_before_id);
        setCounts(page.counts);
        setError(null);
      } catch (e) {
        if (mine === seq.current) setError(errText(e, "Couldn't load uploads"));
      } finally {
        if (mine === seq.current) setLoaded(true);
      }
    },
    [filter, q]
  );

  useEffect(() => {
    setLoaded(false);
    load();
  }, [load]);

  // While anything visible is still uploading, refetch just those rows and
  // merge them in by id: the rest of the list (and "Load more" pages) stays.
  const uploadingIds = items
    .filter((i) => i.status === "queued" || i.status === "uploading")
    .map((i) => i.id)
    .slice(0, 100);
  const uploadingKey = uploadingIds.join(",");
  useEffect(() => {
    if (!uploadingKey) return;
    const ids = uploadingKey.split(",").map(Number);
    const listSeq = seq.current;
    const t = setInterval(async () => {
      try {
        const page = await api.publishingJobs({ ids, limit: 100 });
        if (listSeq !== seq.current) return; // filter or search changed meanwhile
        setItems((prev) => prev.map((p) => page.items.find((f) => f.id === p.id) ?? p));
        setCounts(page.counts);
      } catch {
        // The next tick tries again.
      }
    }, 3000);
    return () => clearInterval(t);
  }, [uploadingKey]);

  const loadMore = async () => {
    if (next == null) return;
    setLoadingMore(true);
    const mine = seq.current;
    try {
      const page = await api.publishingJobs({ status: filter, q, limit: PAGE, before_id: next });
      if (mine !== seq.current) return;
      setItems((prev) => [...prev, ...page.items.filter((i) => !prev.some((p) => p.id === i.id))]);
      setNext(page.next_before_id);
      setCounts(page.counts);
    } catch (e) {
      toast.error(errText(e, "Couldn't load more"));
    } finally {
      setLoadingMore(false);
    }
  };

  const merge = (fresh: PublishOverviewItem[]) =>
    setItems((prev) => prev.map((p) => fresh.find((f) => f.id === p.id) ?? p));

  const refreshOne = async (id: number) => {
    setRefreshing((s) => new Set(s).add(id));
    try {
      const item = await api.refreshPublishJob(id);
      merge([item]);
      if (item.removed) toast.info("YouTube no longer has this video");
    } catch (e) {
      toast.error(errText(e, "Couldn't check YouTube"));
    } finally {
      setRefreshing((s) => {
        const n = new Set(s);
        n.delete(id);
        return n;
      });
    }
  };

  const checkable = items.filter((i) => i.external_id);
  const refreshAll = async () => {
    if (!checkable.length) return;
    setRefreshingAll(true);
    try {
      let checked = 0;
      let removed = 0;
      const ids = checkable.map((i) => i.id);
      for (let i = 0; i < ids.length; i += REFRESH_CHUNK) {
        const res = await api.refreshPublishJobs(ids.slice(i, i + REFRESH_CHUNK));
        merge(res.items);
        checked += res.checked;
        removed += res.removed;
      }
      toast.success(
        `Checked ${checked} video${checked === 1 ? "" : "s"} on YouTube` +
          (removed ? ` · ${removed} removed` : "")
      );
    } catch (e) {
      toast.error(errText(e, "Couldn't check YouTube"));
    } finally {
      setRefreshingAll(false);
    }
  };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-4">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => setFilter(f.id)}
            aria-pressed={filter === f.id}
            className={clsx("chip text-xs", filter === f.id && "chip-active")}
          >
            {f.label}
            {counts && <span className="text-ink-400 ml-1">{counts[f.id] ?? 0}</span>}
          </button>
        ))}
        <div className="relative w-full sm:w-64 sm:ml-auto">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by title…"
            aria-label="Search uploads by title"
            className="input text-sm py-1.5 pl-8 pr-8"
          />
          <span aria-hidden className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-400 text-xs">⌕</span>
          {query && (
            <button
              type="button"
              aria-label="Clear search"
              onClick={() => setQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-400 hover:text-ink-100 text-xs"
            >
              ✕
            </button>
          )}
        </div>
        <button
          type="button"
          className="btn-ghost text-sm w-full sm:w-auto disabled:opacity-50"
          onClick={refreshAll}
          disabled={!connected || refreshingAll || checkable.length === 0}
          title={connected ? "Ask YouTube for the current state of every video shown" : "Connect YouTube in Settings first"}
        >
          {refreshingAll ? "Checking YouTube…" : "Refresh from YouTube"}
        </button>
      </div>

      {error && <div className="card p-3 mb-3 text-sm text-red-300">{error}</div>}

      {!loaded ? (
        <div className="space-y-2" aria-hidden>
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card p-3 flex gap-3">
              <div className="w-24 aspect-video skeleton" />
              <div className="flex-1 space-y-2">
                <div className="h-4 skeleton w-1/2" />
                <div className="h-3 skeleton w-1/3" />
              </div>
            </div>
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="card p-8 text-center">
          <div className="text-sm font-medium">
            {q ? `No uploads match “${q}”` : filter === "all" ? "Nothing uploaded yet" : "Nothing here"}
          </div>
          <div className="text-xs text-ink-300 mt-1">
            {filter === "all" && !q
              ? "Finish a render, then use Publish… on its page. The “Not posted yet” tab lists what's ready."
              : "Try another filter or clear the search."}
          </div>
        </div>
      ) : (
        <div className="card overflow-hidden">
          {/* Column headings: desktop only; phones get stacked rows. */}
          <div
            className="hidden md:grid grid-cols-[96px_minmax(0,2fr)_minmax(0,1.4fr)_minmax(0,1fr)_auto] gap-4 px-4 py-2 border-b border-ink-800 text-[11px] uppercase tracking-wider text-ink-400"
            aria-hidden
          >
            <span />
            <span>Video</span>
            <span>Status</span>
            <span>Visibility · uploaded</span>
            <span className="w-28" />
          </div>
          <ul aria-label="Uploads" className="divide-y divide-ink-800">
            {items.map((it) => (
              <UploadRow
                key={it.id}
                it={it}
                connected={connected}
                refreshing={refreshing.has(it.id) || refreshingAll}
                onRefresh={() => refreshOne(it.id)}
              />
            ))}
          </ul>
        </div>
      )}

      {next != null && items.length > 0 && (
        <div className="mt-4 text-center">
          <button type="button" className="btn-ghost text-sm disabled:opacity-50" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </div>
  );
}

function visibilityText(it: PublishOverviewItem): string {
  const asked = it.publish_at ? "private until scheduled" : it.privacy;
  if (it.youtube_privacy && it.youtube_privacy !== it.privacy) {
    return `Asked ${asked} · YouTube: ${it.youtube_privacy}`;
  }
  return it.youtube_privacy || asked;
}

function UploadRow({
  it,
  connected,
  refreshing,
  onRefresh,
}: {
  it: PublishOverviewItem;
  connected: boolean;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const title = it.youtube_title || it.title;
  const pct = it.bytes_total ? Math.round((100 * it.bytes_sent) / it.bytes_total) : 0;
  const reason = it.youtube_reason || (it.state === "failed" ? it.error : "");
  return (
    <li className="grid grid-cols-[72px_minmax(0,1fr)] md:grid-cols-[96px_minmax(0,2fr)_minmax(0,1.4fr)_minmax(0,1fr)_auto] gap-x-3 md:gap-x-4 gap-y-2 px-3 md:px-4 py-3 items-start">
      <VideoThumb url={it.thumbnail_url} className="row-span-3 md:row-span-1" />

      <div className="min-w-0">
        <div className="text-sm font-medium truncate" title={title}>{title}</div>
        {it.youtube_title && it.youtube_title !== it.title && (
          <div className="text-[11px] text-ink-400 truncate">Asked for “{it.title}”</div>
        )}
        <div className="text-xs text-ink-300 mt-0.5 truncate">
          <Link href={it.project_url} className="hover:underline text-ink-200">{it.project_title || "Untitled"}</Link>
          <span className="text-ink-500">
            {" · "}
            {it.project_kind === "ambient" ? "Ambient" : "Storyboard"}
            {it.render_version != null && ` · version ${it.render_version}`}
          </span>
        </div>
      </div>

      <div className="min-w-0 space-y-1">
        <PublishStateBadge state={it.state} scheduledFor={it.youtube_publish_at || it.publish_at} />
        {it.state === "uploading" && it.bytes_total > 0 && (
          <div className="h-1.5 rounded-full bg-ink-800 overflow-hidden max-w-[12rem]" aria-label={`${pct}% sent`}>
            <div className="h-full grad" style={{ width: `${pct}%` }} />
          </div>
        )}
        <PublishWarning text={it.warning} />
        {reason && <div className="text-[11px] text-red-300 break-words">{reason}</div>}
      </div>

      <div className="min-w-0 text-xs text-ink-300 space-y-0.5 col-start-2 md:col-start-auto">
        <div className="first-letter:uppercase">{visibilityText(it)}</div>
        {it.uploaded_at ? (
          <div className="text-ink-400">Uploaded {fmtLocal(it.uploaded_at)}</div>
        ) : (
          <div className="text-ink-400">Started {fmtLocal(it.created_at)}</div>
        )}
        {it.checked_at && <div className="text-ink-500">Checked {fmtLocal(it.checked_at)}</div>}
      </div>

      <div className="flex flex-wrap md:flex-col items-start md:items-end gap-2 col-start-2 md:col-start-auto md:w-28">
        {it.external_url && !it.removed && (
          <a href={it.external_url} target="_blank" rel="noreferrer" className="btn-ghost text-xs px-3 py-1">
            Open on YouTube
          </a>
        )}
        {it.external_id && (
          <button
            type="button"
            className="btn-ghost text-xs px-3 py-1 disabled:opacity-50"
            onClick={onRefresh}
            disabled={!connected || refreshing}
            title={connected ? "Ask YouTube for this video's current state" : "Connect YouTube in Settings first"}
          >
            {refreshing ? "Checking…" : "Refresh"}
          </button>
        )}
        {it.state === "failed" && (
          <Link href={it.render_url} className="btn-ghost text-xs px-3 py-1">Open render</Link>
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Not posted yet tab

function NotPosted() {
  const toast = useToast();
  const [items, setItems] = useState<NotPostedItem[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .publishingNotPosted({ limit: PAGE })
      .then((p) => {
        setItems(p.items);
        setNext(p.next_before_id);
      })
      .catch((e) => setError(errText(e, "Couldn't load renders")))
      .finally(() => setLoaded(true));
  }, []);

  const loadMore = async () => {
    if (next == null) return;
    setLoadingMore(true);
    try {
      const p = await api.publishingNotPosted({ limit: PAGE, before_id: next });
      setItems((prev) => [...prev, ...p.items.filter((i) => !prev.some((x) => x.render_id === i.render_id))]);
      setNext(p.next_before_id);
    } catch (e) {
      toast.error(errText(e, "Couldn't load more"));
    } finally {
      setLoadingMore(false);
    }
  };

  if (!loaded) {
    return (
      <div className="space-y-2" aria-hidden>
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="card p-3 h-20 skeleton" />
        ))}
      </div>
    );
  }
  if (error) return <div className="card p-3 text-sm text-red-300">{error}</div>;
  if (items.length === 0) {
    return (
      <div className="card p-8 text-center">
        <div className="text-sm font-medium">Everything finished is posted</div>
        <div className="text-xs text-ink-300 mt-1">New renders show up here until they're uploaded.</div>
      </div>
    );
  }

  return (
    <div>
      <p className="text-xs text-ink-400 mb-3">The latest finished render of each project or scene that isn&apos;t on YouTube yet.</p>
      <ul aria-label="Not posted yet" className="card divide-y divide-ink-800 overflow-hidden">
        {items.map((it) => (
          <li
            key={it.render_id}
            className="grid grid-cols-[72px_minmax(0,1fr)] sm:grid-cols-[96px_minmax(0,1fr)_auto] gap-x-3 sm:gap-x-4 gap-y-2 px-3 sm:px-4 py-3 items-center"
          >
            <VideoThumb url={it.thumbnail_url} className="row-span-2 sm:row-span-1" />
            <div className="min-w-0">
              <Link href={it.project_url} className="text-sm font-medium hover:underline truncate block">
                {it.project_title || "Untitled"}
              </Link>
              <div className="text-xs text-ink-400 mt-0.5">
                {it.project_kind === "ambient" ? "Ambient" : "Storyboard"}
                {it.render_version != null && ` · version ${it.render_version}`}
                {" · finished "}
                {fmtLocal(it.created_at, false)}
              </div>
              {it.failed_attempts > 0 && (
                <div className="text-[11px] text-red-300 mt-0.5">
                  {it.failed_attempts} failed upload{it.failed_attempts === 1 ? "" : "s"}
                </div>
              )}
              {!it.file_available && (
                <div className="text-[11px] text-amber-200 mt-0.5">The video file is gone — render it again before publishing.</div>
              )}
            </div>
            <div className="col-start-2 sm:col-start-auto">
              <Link href={it.publish_url} className="btn-primary text-xs px-3 py-1.5">Publish…</Link>
            </div>
          </li>
        ))}
      </ul>
      {next != null && (
        <div className="mt-4 text-center">
          <button type="button" className="btn-ghost text-sm disabled:opacity-50" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </div>
  );
}
