"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { fmtLocal, PublishStateBadge } from "@/components/PublishState";
import { useToast } from "@/components/Toaster";
import { errText, VideoThumb } from "@/components/VideoThumb";
import { api, HookTest, ResultGroup, ResultRow, ResultsGroupBy, ResultsOut } from "@/lib/api";

export default function ResultsPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

const GROUP_OPTIONS: { id: ResultsGroupBy; label: string }[] = [
  { id: "type", label: "Video type" },
  { id: "preset", label: "Ambient preset" },
  { id: "time_weather", label: "Time of day + weather" },
  { id: "length", label: "Video length" },
  { id: "character", label: "Main character" },
  { id: "model", label: "Video model" },
  { id: "aspect", label: "Aspect ratio" },
  { id: "hook", label: "Opening (hook) style" },
];

type SortKey =
  | "title"
  | "published_at"
  | "age_days"
  | "views"
  | "likes"
  | "comments"
  | "views_per_day"
  | "cost_usd"
  | "cost_per_1k_views"
  | "views_change";

const COLUMNS: { key: SortKey; label: string; numeric: boolean; hint?: string }[] = [
  { key: "title", label: "Video", numeric: false },
  { key: "published_at", label: "Published", numeric: false },
  { key: "age_days", label: "Age", numeric: true },
  { key: "views", label: "Views", numeric: true },
  { key: "likes", label: "Likes", numeric: true },
  { key: "comments", label: "Comments", numeric: true },
  { key: "views_per_day", label: "Views/day", numeric: true },
  { key: "cost_usd", label: "Cost", numeric: true, hint: "What this video cost to make (whole project)" },
  { key: "cost_per_1k_views", label: "Per 1k views", numeric: true, hint: "Cost per 1,000 views" },
  { key: "views_change", label: "Trend", numeric: true, hint: "Views gained since about a week ago" },
];

const nf = new Intl.NumberFormat();
const fmtNum = (n: number | null | undefined) => (n == null ? "—" : nf.format(Math.round(n)));
const fmtRate = (n: number | null | undefined) =>
  n == null ? "—" : n >= 100 ? nf.format(Math.round(n)) : n.toFixed(1);
function fmtMoney(n: number | null | undefined, estimate = false) {
  if (n == null) return "—";
  const digits = n !== 0 && Math.abs(n) < 1 ? 3 : 2;
  return `${estimate ? "~" : ""}$${n.toFixed(digits)}`;
}

function sortValue(r: ResultRow, k: SortKey): number | string | null {
  switch (k) {
    case "title":
      return r.title.toLowerCase();
    case "published_at":
      return r.published_at;
    default:
      return r[k];
  }
}

function Inner() {
  const toast = useToast();
  const [groupBy, setGroupBy] = useState<ResultsGroupBy>("type");
  const [data, setData] = useState<ResultsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "published_at", dir: "desc" });

  // Only the latest request may land: switching "Group by" quickly (or a
  // refresh racing a switch) must not show an older grouping.
  const loadSeq = useRef(0);
  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    return api
      .results(groupBy)
      .then((d) => {
        if (seq !== loadSeq.current) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (seq !== loadSeq.current) return;
        setError(errText(e, "Couldn't load results"));
      });
  }, [groupBy]);

  useEffect(() => {
    load();
  }, [load]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await api.refreshResults();
      toast.success(
        `Updated numbers for ${r.videos} video${r.videos === 1 ? "" : "s"}` +
          (r.removed ? ` · ${r.removed} removed on YouTube` : "")
      );
      await load();
    } catch (e) {
      toast.error(errText(e, "Couldn't refresh numbers"));
      // The failed try is recorded: reload so "Last try … failed" shows now.
      await load().catch(() => {});
    } finally {
      setRefreshing(false);
    }
  };

  const rows = useMemo(() => {
    const list = [...(data?.rows ?? [])];
    const dir = sort.dir === "asc" ? 1 : -1;
    list.sort((a, b) => {
      const va = sortValue(a, sort.key);
      const vb = sortValue(b, sort.key);
      // Missing numbers always sink to the bottom, whichever way we sort.
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (va < vb) return -dir;
      if (va > vb) return dir;
      return 0;
    });
    return list;
  }, [data, sort]);

  const toggleSort = (key: SortKey) =>
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "title" ? "asc" : "desc" }
    );

  const connected = !!data?.configured && !!data?.connected;

  return (
    <div className="p-4 md:p-8 max-w-6xl">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">Results</h1>
          <p className="text-sm text-ink-300 mt-1">
            How your uploads are doing on YouTube, and what they cost to make.
          </p>
        </div>
        <div className="flex flex-col items-start sm:items-end gap-1">
          <button
            type="button"
            className="btn-primary text-sm disabled:opacity-50"
            onClick={refresh}
            disabled={!connected || refreshing}
          >
            {refreshing ? "Refreshing…" : "Refresh numbers"}
          </button>
          {data?.last_refreshed_at && (
            <span className="text-[11px] text-ink-400">Last updated {fmtLocal(data.last_refreshed_at)}</span>
          )}
          {data?.last_attempt_failed && (
            <span className="text-[11px] text-amber-200" title={data.last_attempt_error || undefined}>
              Last try{data.last_attempt_at ? ` (${fmtLocal(data.last_attempt_at)})` : ""} failed
              {data.last_attempt_error ? `: ${data.last_attempt_error}` : ""}
            </span>
          )}
        </div>
      </header>

      {error && <div className="card p-3 mb-4 text-sm text-red-300">{error}</div>}

      {!data ? (
        !error && (
          <div className="space-y-3" aria-hidden>
            <div className="h-24 skeleton rounded-xl" />
            <div className="h-64 skeleton rounded-xl" />
          </div>
        )
      ) : !connected && data.rows.length === 0 ? (
        <Empty
          title="Connect YouTube in Settings"
          body="Once a channel is connected, uploads and their views, likes and comments show up here."
          action={<Link href="/settings#youtube" className="btn-primary text-sm">Open Settings</Link>}
        />
      ) : data.rows.length === 0 ? (
        <Empty
          title="No uploads yet"
          body="Publish a finished render to YouTube and it will appear here."
          action={<Link href="/publishing" className="btn-ghost text-sm">See what's ready to post</Link>}
        />
      ) : (
        <>
          {!connected && (
            <div className="card p-3 mb-4 text-sm text-amber-200 flex flex-wrap items-center justify-between gap-2" role="status">
              <span>YouTube isn&apos;t connected — numbers stop updating until you reconnect</span>
              <Link href="/settings#youtube" className="btn-ghost text-sm">Open Settings</Link>
            </div>
          )}
          {connected && !data.has_numbers && (
            <div className="card p-3 mb-4 text-sm text-ink-200">
              Numbers appear a few hours after the first upload.
            </div>
          )}

          <section aria-labelledby="rollups" className="mb-6">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
              <h2 id="rollups" className="text-base font-medium">By {data.group_label.toLowerCase()}</h2>
              <label className="flex items-center gap-2 text-sm">
                <span className="text-ink-300">Group by</span>
                <select
                  className="input py-1.5 w-auto"
                  value={groupBy}
                  onChange={(e) => setGroupBy(e.target.value as ResultsGroupBy)}
                >
                  {GROUP_OPTIONS.map((o) => (
                    <option key={o.id} value={o.id}>{o.label}</option>
                  ))}
                </select>
              </label>
            </div>
            {data.groups.length === 0 ? (
              <div className="card p-4 text-sm text-ink-300">Nothing to group yet.</div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {data.groups.map((g) => (
                  <GroupCard key={g.key} g={g} />
                ))}
              </div>
            )}
          </section>

          {(data.hook_tests?.length ?? 0) > 0 && <HookTests tests={data.hook_tests!} />}

          <section aria-labelledby="per-video">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
              <h2 id="per-video" className="text-base font-medium">Every video</h2>
              {/* Phones have no column headers to click, so sorting lives here. */}
              <label className="md:hidden flex items-center gap-2 text-sm">
                <span className="text-ink-300">Sort</span>
                <select
                  className="input py-1.5 w-auto"
                  value={`${sort.key}:${sort.dir}`}
                  onChange={(e) => {
                    const [key, dir] = e.target.value.split(":") as [SortKey, "asc" | "desc"];
                    setSort({ key, dir });
                  }}
                >
                  {COLUMNS.flatMap((c) => [
                    <option key={`${c.key}:desc`} value={`${c.key}:desc`}>
                      {c.label} {c.key === "title" ? "(Z–A)" : c.key === "published_at" ? "(newest)" : "(high → low)"}
                    </option>,
                    <option key={`${c.key}:asc`} value={`${c.key}:asc`}>
                      {c.label} {c.key === "title" ? "(A–Z)" : c.key === "published_at" ? "(oldest)" : "(low → high)"}
                    </option>,
                  ])}
                </select>
              </label>
            </div>
            <p className="text-[11px] text-ink-400 mb-2">
              Cost is what this video cost to make (whole project). For another opening made in the hook lab it
              is only what that opening cost. “~$” means some of it is an estimate.
            </p>

            {/* Desktop table */}
            <div className="hidden md:block card overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-ink-800 text-[11px] uppercase tracking-wider text-ink-400">
                    {COLUMNS.map((c) => {
                      const active = sort.key === c.key;
                      return (
                        <th
                          key={c.key}
                          scope="col"
                          aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                          className={clsx("px-3 py-2 font-normal whitespace-nowrap", c.numeric ? "text-right" : "text-left")}
                        >
                          <button
                            type="button"
                            onClick={() => toggleSort(c.key)}
                            title={c.hint}
                            className={clsx("uppercase tracking-wider hover:text-ink-100", active && "text-ink-100")}
                          >
                            {c.label}
                            <span aria-hidden className="ml-1">{active ? (sort.dir === "asc" ? "▲" : "▼") : ""}</span>
                          </button>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-800">
                  {rows.map((r) => (
                    <tr key={r.publish_job_id} className="align-middle">
                      <td className="px-3 py-2 max-w-[18rem]">
                        <VideoCell r={r} />
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap text-ink-300">{fmtLocal(r.published_at, false)}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-ink-300">{r.age_days}d</td>
                      <td className="px-3 py-2 text-right tabular-nums">{fmtNum(r.views)}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{fmtNum(r.likes)}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{fmtNum(r.comments)}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{fmtRate(r.views_per_day)}</td>
                      <td className="px-3 py-2 text-right tabular-nums" title={costHint(r)}>
                        {fmtMoney(r.cost_usd, r.cost_is_estimate)}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {fmtMoney(r.cost_per_1k_views, r.cost_is_estimate && r.cost_per_1k_views != null)}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <Trend r={r} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Phone: stacked rows */}
            <ul className="md:hidden space-y-2" aria-label="Every video">
              {rows.map((r) => (
                <li key={r.publish_job_id} className="card p-3">
                  <VideoCell r={r} />
                  <dl className="grid grid-cols-3 gap-x-3 gap-y-2 mt-3 text-xs">
                    <Stat label="Views" value={fmtNum(r.views)} />
                    <Stat label="Likes" value={fmtNum(r.likes)} />
                    <Stat label="Comments" value={fmtNum(r.comments)} />
                    <Stat label="Views/day" value={fmtRate(r.views_per_day)} />
                    <Stat label="Cost" value={fmtMoney(r.cost_usd, r.cost_is_estimate)} />
                    <Stat
                      label="Per 1k views"
                      value={fmtMoney(r.cost_per_1k_views, r.cost_is_estimate && r.cost_per_1k_views != null)}
                    />
                    <Stat label="Published" value={fmtLocal(r.published_at, false)} />
                    <Stat label="Age" value={`${r.age_days} days`} />
                    <div>
                      <dt className="text-ink-400">Trend</dt>
                      <dd className="mt-0.5"><Trend r={r} /></dd>
                    </div>
                  </dl>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}

function costHint(r: ResultRow): string {
  return r.cost_scope === "render"
    ? "What this opening cost to make (the rest of the video was reused)"
    : "What this video cost to make (whole project)";
}

/** Each base video with other openings on YouTube, compared at equal age. */
function HookTests({ tests }: { tests: HookTest[] }) {
  return (
    <section aria-labelledby="hook-tests" className="mb-6">
      <h2 id="hook-tests" className="text-base font-medium">Hook tests</h2>
      <p className="text-[11px] text-ink-400 mt-0.5 mb-3">
        The same video with different openings. Views per day are compared at the same age (days since upload),
        so a video posted earlier doesn&apos;t win just by being older.
      </p>
      <div className="space-y-3">
        {tests.map((t) => (
          <div key={t.base_render_id} className="card p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div className="min-w-0">
                <Link href={t.project_url} className="text-sm font-medium hover:underline">
                  {t.project_title || "Untitled"}
                </Link>
                <span className="text-xs text-ink-400"> · {t.base_version_label}</span>
              </div>
              <span className="text-[11px] text-ink-400">
                {t.equal_age_days != null ? `Compared at ${t.equal_age_days} day${t.equal_age_days === 1 ? "" : "s"} old` : "Not enough numbers yet"}
              </span>
            </div>
            <p
              className={clsx(
                "mt-2 text-sm rounded-md px-3 py-2 border",
                t.ready && t.leader
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                  : "border-ink-700 bg-ink-900/60 text-ink-200"
              )}
              role="status"
            >
              {t.verdict}
            </p>
            <ul className="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-2">
              {t.entries.map((e) => (
                <li
                  key={e.publish_job_id}
                  className={clsx(
                    "rounded-lg border px-3 py-2",
                    t.leader && e.hook_label === t.leader ? "border-emerald-500/40" : "border-ink-800"
                  )}
                >
                  <div className="flex items-start gap-3 min-w-0">
                    <VideoThumb url={e.thumbnail_url} className="w-16 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="chip text-[10px] py-0.5 font-semibold">{e.hook_label || "?"}</span>
                        <span className="text-[11px] text-ink-300">{e.hook_style}</span>
                        {e.external_url && (
                          <a href={e.external_url} target="_blank" rel="noreferrer" className="text-[11px] text-iris-soft hover:underline">
                            Open on YouTube
                          </a>
                        )}
                      </div>
                      <p className="text-xs text-ink-100 mt-1 break-words">“{e.hook_text}”</p>
                    </div>
                  </div>
                  <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1 mt-2 text-xs">
                    <Stat
                      label={t.equal_age_days ? `Views/day (day ${t.equal_age_days})` : "Views/day"}
                      value={fmtRate(e.views_per_day_equal_age)}
                    />
                    <Stat label="Views" value={fmtNum(e.views)} />
                    <Stat label="Likes per 1k views" value={fmtRate(e.likes_per_1k_views)} />
                    <Stat
                      label={e.cost_scope === "render" ? "Opening cost" : "Video cost"}
                      value={fmtMoney(e.cost_usd, e.cost_is_estimate)}
                    />
                  </dl>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

function Empty({ title, body, action }: { title: string; body: string; action?: React.ReactNode }) {
  return (
    <div className="card p-8 text-center">
      <div className="text-sm font-medium">{title}</div>
      <div className="text-xs text-ink-300 mt-1">{body}</div>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-ink-400">{label}</dt>
      <dd className="mt-0.5 tabular-nums truncate">{value}</dd>
    </div>
  );
}

function VideoCell({ r }: { r: ResultRow }) {
  return (
    <div className="flex items-center gap-3 min-w-0">
      <VideoThumb url={r.thumbnail_url} className="w-16 shrink-0" />
      <div className="min-w-0">
        {r.external_url ? (
          <a href={r.external_url} target="_blank" rel="noreferrer" className="font-medium hover:underline truncate block" title={r.title}>
            {r.title}
          </a>
        ) : (
          <div className="font-medium truncate" title={r.title}>{r.title}</div>
        )}
        <div className="text-[11px] text-ink-400 truncate">
          <Link href={r.project_url} className="hover:underline">{r.project_title || "Untitled"}</Link>
          {r.render_version_label
            ? ` · ${r.render_version_label}`
            : r.render_version != null && ` · Version ${r.render_version}`}
        </div>
        {r.state !== "published" && (
          <PublishStateBadge state={r.state} className="mt-1 text-[10px]" />
        )}
      </div>
    </div>
  );
}

function Trend({ r }: { r: ResultRow }) {
  if (r.views_change == null) {
    return <span className="text-ink-500 text-xs" title="Needs two days of numbers">—</span>;
  }
  const up = r.views_change > 0;
  return (
    <span
      className="inline-flex items-center gap-1.5 justify-end"
      title={`${r.views_change >= 0 ? "+" : ""}${nf.format(r.views_change)} views over ${r.trend_days ?? "?"} days`}
    >
      <Sparkline points={r.sparkline} />
      <span className={clsx("tabular-nums text-xs", up ? "text-emerald-300" : "text-ink-300")}>
        {up ? "+" : ""}
        {nf.format(r.views_change)}
      </span>
    </span>
  );
}

/** Tiny inline-SVG line of daily view counts (oldest first). */
function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) return null;
  const w = 48;
  const h = 16;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - 1 - ((v - min) / span) * (h - 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden className="text-iris-soft shrink-0">
      <path d={d} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function GroupCard({ g }: { g: ResultGroup }) {
  return (
    <div className={clsx("card p-4", g.too_few && "opacity-80")}>
      <div className="flex items-start justify-between gap-2">
        <div className="text-sm font-medium break-words min-w-0">{g.key}</div>
        <span className="text-[11px] text-ink-400 whitespace-nowrap">
          {g.videos} video{g.videos === 1 ? "" : "s"}
          {g.videos_with_numbers < g.videos && ` · ${g.videos_with_numbers} with numbers`}
        </span>
      </div>
      {g.too_few && <div className="text-[11px] text-amber-200 mt-1">Too few to tell</div>}
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 mt-3 text-xs">
        <Stat label="Total views" value={fmtNum(g.total_views)} />
        <Stat label="Median views" value={fmtNum(g.median_views)} />
        <Stat label="Total cost" value={fmtMoney(g.total_cost_usd, g.cost_is_estimate)} />
        <Stat
          label="Per 1k views"
          value={fmtMoney(g.cost_per_1k_views, g.cost_is_estimate && g.cost_per_1k_views != null)}
        />
      </dl>
    </div>
  );
}
