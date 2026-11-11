"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import clsx from "clsx";
import { AuthGate } from "@/components/AuthGate";
import { Dropzone } from "@/components/Dropzone";
import { AssignDialog, slotLabel } from "@/components/AssignDialog";
import { useToast } from "@/components/Toaster";
import { EnhanceButton } from "@/components/EnhanceButton";
import { PresetsMenu } from "@/components/PresetsMenu";
import { StudioViewer, ViewerItem, downloadName, mayBeTransparent } from "@/components/StudioViewer";
import {
  OpModels,
  UpscaleDialog,
  priceForFactor,
  priceFromText,
  removeBgLabel,
  sourceChipText,
  useOpModels,
  usd,
  withKnownPrice,
} from "@/components/ImageOps";
import { TileMenu, TileMenuItem } from "@/components/TileMenu";
import { UseInProjectDialog } from "@/components/UseInProjectDialog";
import {
  BulkSaveDialog,
  CharacterSheetDialog,
  ModelOption,
  ModelPickerDialog,
  costText,
  sheetSlot,
  studioStatusLabel,
} from "@/components/StudioLibrary";
import {
  api,
  ApiError,
  errorText,
  Asset,
  Avatar,
  ImageOp,
  ImageOpSource,
  Ingredient,
  PromptPreset,
  StudioAspect,
  StudioJob,
  StudioJobsQuery,
  VideoModelOption,
} from "@/lib/api";
import { downloadFile } from "@/lib/download";
import { usePoll } from "@/lib/usePoll";
import { useActivity, useEvents } from "@/components/NotificationsProvider";

const ASPECTS: { id: StudioAspect; label: string }[] = [
  { id: "", label: "Auto" },
  { id: "9:16", label: "9:16" },
  { id: "16:9", label: "16:9" },
  { id: "1:1", label: "1:1" },
];

/** What Remix or a preset loads into the prompt bar. `nonce` makes a repeat apply. */
type PromptLoad = {
  nonce: number;
  outputKind: "image" | "video_clip";
  prompt: string;
  negative_prompt: string;
  model: string;
  aspect_ratio: StudioAspect;
  duration_seconds?: number;
};

function asAspect(v: string | undefined | null): StudioAspect {
  return v === "9:16" || v === "16:9" || v === "1:1" ? v : "";
}

/** Clip lengths the Studio bar offers. */
const CLIP_SECONDS = [5, 8, 10, 15, 20, 30] as const;

/** Snap any duration (the API allows 1-60s) to the nearest offered clip length, so the select shows what gets billed. */
function snapClipSeconds(d: number): number {
  return CLIP_SECONDS.reduce<number>((a, b) => (Math.abs(b - d) < Math.abs(a - d) ? b : a), CLIP_SECONDS[0]);
}

/** Placeholder rows for every job a create request made, so all show as pending tiles. */
function batchPlaceholders(first: StudioJob): StudioJob[] {
  const ids = first.batch_ids && first.batch_ids.length > 0 ? first.batch_ids : [first.id];
  const all = ids.includes(first.id) ? ids : [first.id, ...ids];
  // Siblings share the first job's status (the backend creates the whole batch as "pending").
  return all.map((id) => (id === first.id ? first : { ...first, id, batch_ids: [] }));
}

const FINISHED = ["completed", "saved"];
const TERMINAL = ["completed", "failed", "saved"];
/** How often in-flight jobs are checked (one batched request). */
const POLL_MS = 3000;
/** Most ids one batched check may carry (the backend's limit). */
const POLL_MAX_IDS = 200;

/** The picture a clip starts from ("Animate this"). */
type FirstFrame = { kind: "job" | "asset"; id: number; thumbUrl: string; label: string };

/** Ids of the jobs in `rows` that are still running. */
function runningIds(rows: StudioJob[]): number[] {
  return rows.filter((j) => !TERMINAL.includes(j.status)).map((j) => j.id);
}

/** The cancel / delete note for a job that is still running. */
function chargeNote(job: StudioJob): string {
  if (job.status === "pending") return "It hasn't started, so nothing is charged.";
  const c = job.cost_usd;
  const about = c && c > 0 ? ` (about $${c >= 0.01 ? c.toFixed(2) : c.toFixed(3)})` : "";
  return `The provider may still finish and charge for it${about}.`;
}

/** "1 reference picture was deleted, so it's left out". */
function missingRefsText(n: number): string {
  return n === 1
    ? "1 reference picture was deleted, so it's left out"
    : `${n} reference pictures were deleted, so they're left out`;
}

type Filter = "all" | "images" | "videos" | "characters" | "scenes" | "uploads";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All Media" },
  { id: "images", label: "Images" },
  { id: "videos", label: "Videos" },
  { id: "characters", label: "Characters" },
  { id: "scenes", label: "Scenes" },
  { id: "uploads", label: "Uploads" },
];

/** Library chips: they filter the Studio results on the server. */
type LibKind = "all" | "favorites" | "images" | "clips" | "failed";

const LIB_KINDS: { id: LibKind; label: string }[] = [
  { id: "all", label: "All" },
  { id: "favorites", label: "★ Favorites" },
  { id: "images", label: "Images" },
  { id: "clips", label: "Clips" },
  { id: "failed", label: "Failed" },
];

const PAGE_SIZE = 50;

export default function StudioPage() {
  return (
    <AuthGate>
      <Studio />
    </AuthGate>
  );
}

type Tile = {
  key: string;
  kind: "asset" | "job";
  imageUrl?: string;
  videoUrl?: string;
  label: string;
  sub: string;
  status?: string;
  selectable?: boolean;
  assetId?: number;
  jobId?: number;
  job?: StudioJob;
  ownerKind?: "avatar" | "ingredient";
  asset?: Asset;
};

function Studio() {
  const router = useRouter();
  const [filter, setFilter] = useState<Filter>("all");
  const [avatars, setAvatars] = useState<Avatar[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [jobs, setJobs] = useState<StudioJob[]>([]);
  const [selectedRefs, setSelectedRefs] = useState<number[]>([]);
  const [pendingJobIds, setPendingJobIds] = useState<number[]>([]);
  const [assigning, setAssigning] = useState<StudioJob | null>(null);
  const [load, setLoad] = useState<PromptLoad | null>(null);
  const [imageModels, setImageModels] = useState<ModelOption[]>([]);
  const [videoModels, setVideoModels] = useState<VideoModelOption[]>([]);
  // "Animate this": the picture the next clip starts from.
  const [firstFrame, setFirstFrame] = useState<FirstFrame | null>(null);
  // "Use in project…": the finished clip being placed in a shot.
  const [placing, setPlacing] = useState<StudioJob | null>(null);
  // Library: search, chips, owner filter and paging.
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [libKind, setLibKind] = useState<LibKind>("all");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Multi-select.
  const [selectMode, setSelectMode] = useState(false);
  const [checked, setChecked] = useState<number[]>([]);
  const [bulkSave, setBulkSave] = useState<{ owner?: string; slot?: string } | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [retrying, setRetrying] = useState<StudioJob | null>(null);
  // Picture operations: the models offered (with prices) and the Upscale dialog.
  const opModels = useOpModels();
  const [upscaling, setUpscaling] = useState<{ source: ImageOpSource; label: string } | null>(null);
  // The last character sheet made here, for "Select this sheet".
  const [lastSheet, setLastSheet] = useState<{ avatarId: number; ids: number[] } | null>(null);
  const toast = useToast();

  const query = useMemo<StudioJobsQuery>(() => {
    const out: StudioJobsQuery = {};
    if (q) out.q = q;
    if (libKind === "favorites") out.favorite = true;
    if (libKind === "images") out.output_kind = "image";
    if (libKind === "clips") out.output_kind = "video_clip";
    if (libKind === "failed") out.status = "failed";
    const [ok, oid] = ownerFilter.split(":");
    if ((ok === "avatar" || ok === "ingredient") && Number(oid)) {
      out.owner_kind = ok;
      out.owner_id = Number(oid);
    }
    return out;
  }, [q, libKind, ownerFilter]);
  const filtersActive = Object.keys(query).length > 0;
  // What the server is asked for: the library filters plus the top Images /
  // Videos tab, so paging reaches older results of that kind.
  const listQuery = useMemo<StudioJobsQuery>(() => {
    if (query.output_kind) return query;
    if (filter === "images") return { ...query, output_kind: "image" };
    if (filter === "videos") return { ...query, output_kind: "video_clip" };
    return query;
  }, [query, filter]);
  const queryRef = useRef(listQuery);
  queryRef.current = listQuery;
  // Only the newest list request may write the list (typing fires several).
  const listSeq = useRef(0);

  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  // The newest copy of every job the batched check saw, including jobs not
  // in the list right now (another filter or page): merged in when a list
  // load brings them back, so a load never shows an older state.
  const latest = useRef(new Map<number, StudioJob>());
  const newest = (j: StudioJob): StudioJob => {
    const l = latest.current.get(j.id);
    return l && (l.updated_at || "") > (j.updated_at || "") ? { ...j, ...l } : j;
  };
  // Every job a list load shows still running is checked until it finishes.
  const trackRunning = (rows: StudioJob[]) => {
    const ids = runningIds(rows);
    if (ids.length > 0) setPendingJobIds((prev) => Array.from(new Set([...prev, ...ids])));
  };
  // fresh: the first page (new filters). Otherwise (a reload after an
  // action) as many as are loaded, so "Load more" pages don't disappear
  // under the viewer or the user's scroll position.
  const loadJobs = async (fresh = false) => {
    const seq = ++listSeq.current;
    const limit = fresh ? PAGE_SIZE : Math.min(200, Math.max(PAGE_SIZE, jobsRef.current.length + 10));
    const jb = await api.listStudioJobs({ ...queryRef.current, limit });
    if (seq !== listSeq.current) return;
    const rows = jb.map(newest);
    setJobs(rows);
    setHasMore(jb.length === limit);
    trackRunning(rows);
  };

  const loadMore = async () => {
    if (jobs.length === 0) return;
    setLoadingMore(true);
    const seq = listSeq.current;
    try {
      const before_id = Math.min(...jobs.map((j) => j.id));
      const page = (await api.listStudioJobs({ ...queryRef.current, limit: PAGE_SIZE, before_id })).map(newest);
      if (seq !== listSeq.current) return;
      setJobs((cur) => [...cur, ...page.filter((p) => !cur.some((c) => c.id === p.id))]);
      setHasMore(page.length === PAGE_SIZE);
      trackRunning(page);
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't load more results"));
    } finally {
      setLoadingMore(false);
    }
  };

  // Remix: put a finished result's settings back into the prompt bar.
  const remix = (job: StudioJob) => {
    const missing = new Set(job.missing_reference_ids || []);
    setSelectedRefs((job.reference_asset_ids || []).filter((id) => !missing.has(id)));
    if (missing.size > 0) toast.info(missingRefsText(missing.size), { duration: 5000 });
    // A clip that started from a picture starts from it again.
    if (job.output_kind === "video_clip" && job.first_frame_job_id) {
      setFirstFrame({
        kind: "job",
        id: job.first_frame_job_id,
        thumbUrl: api.studioPreview(job.first_frame_job_id),
        label: `Studio #${job.first_frame_job_id}`,
      });
    } else if (job.output_kind === "video_clip" && job.first_frame_asset_id) {
      const a = assetById.get(job.first_frame_asset_id);
      if (a) {
        setFirstFrame({ kind: "asset", id: a.id, thumbUrl: api.publicAsset(a.public_token), label: `Cast #${a.id}` });
      } else {
        setFirstFrame(null);
        toast.info("The picture this clip started from was deleted, so it's left out", { duration: 5000 });
      }
    } else {
      setFirstFrame(null);
    }
    setLoad({
      nonce: Date.now(),
      outputKind: job.output_kind,
      prompt: job.prompt,
      negative_prompt: job.negative_prompt || "",
      model: job.provider_model || "",
      aspect_ratio: asAspect(job.aspect_ratio),
      duration_seconds: job.output_kind === "video_clip" ? job.duration_seconds : undefined,
    });
  };

  // "Animate this": a clip that starts from this picture, with its prompt
  // as a starting point.
  const animate = (item: { job?: StudioJob; asset?: Asset; assetId?: number; label: string }) => {
    if (item.job) {
      const j = item.job;
      setFirstFrame({ kind: "job", id: j.id, thumbUrl: api.studioPreview(j.id), label: `Studio #${j.id}` });
    } else if (item.asset || item.assetId) {
      const a = item.asset || assetById.get(item.assetId!);
      if (!a) return;
      setFirstFrame({ kind: "asset", id: a.id, thumbUrl: api.publicAsset(a.public_token), label: item.label });
    } else {
      return;
    }
    setLoad({
      nonce: Date.now(),
      outputKind: "video_clip",
      prompt: item.job?.prompt || "",
      negative_prompt: item.job?.negative_prompt || "",
      model: "",
      aspect_ratio: asAspect(item.job?.aspect_ratio),
    });
  };

  const reload = async () => {
    const [av, ing] = await Promise.all([api.listAvatars(), api.listIngredients(), loadJobs()]);
    setAvatars(av);
    setIngredients(ing);
  };

  useEffect(() => {
    reload();
    api.imageModels().then((ms) => setImageModels(ms.map(withKnownPrice))).catch(() => setImageModels([]));
    api.videoModels().then((ms) => setVideoModels(ms.map(withKnownPrice))).catch(() => setVideoModels([]));
    // Cast → "Open in Studio" lands here with ?job=<id>: open that result.
    const linked = Number(new URLSearchParams(window.location.search).get("job"));
    if (linked > 0) {
      api
        .getStudioJob(linked)
        .then((j) => {
          setJobs((cur) => (cur.some((x) => x.id === j.id) ? cur : [j, ...cur]));
          if (!TERMINAL.includes(j.status)) setPendingJobIds((cur) => Array.from(new Set([...cur, j.id])));
          // A finished one opens in the viewer; a running one is the newest tile at the top.
          if (FINISHED.includes(j.status)) setViewKey(`job-${j.id}`);
        })
        .catch(() => toast.info("That result was deleted"));
    }
  }, []);

  // Debounce the search box.
  useEffect(() => {
    const t = setTimeout(() => setQ(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  // New filters: fresh first page, and nothing stays selected.
  const firstQuery = useRef(true);
  useEffect(() => {
    if (firstQuery.current) {
      firstQuery.current = false;
      return;
    }
    setChecked([]);
    loadJobs(true).catch((e: any) => toast.error(errorText(e, "Couldn't load results")));
  }, [listQuery]);

  // Check every in-flight job in one request every few seconds while any
  // is running, paused while the tab is hidden (and once more on coming
  // back). A finished job, or one that is gone (deleted), stops being
  // checked; an error keeps them all for the next tick.
  const pendingRef = useRef(pendingJobIds);
  pendingRef.current = pendingJobIds;
  const pollBusy = useRef(false);
  const pollNow = async () => {
    const ids = pendingRef.current.slice(0, POLL_MAX_IDS);
    if (ids.length === 0 || pollBusy.current) return;
    if (typeof document !== "undefined" && document.hidden) return;
    pollBusy.current = true;
    try {
      const fresh = await api.studioJobsByIds(ids);
      const seen = new Map(fresh.map((j) => [j.id, j]));
      for (const j of fresh) latest.current.set(j.id, j);
      const done = new Set(ids.filter((id) => !seen.has(id) || TERMINAL.includes(seen.get(id)!.status)));
      if (fresh.length > 0) setJobs((cur) => cur.map((j) => (seen.has(j.id) ? { ...j, ...seen.get(j.id)! } : j)));
      // Deleted elsewhere: drop the tile too.
      const gone = ids.filter((id) => !seen.has(id));
      if (gone.length > 0) setJobs((cur) => cur.filter((j) => !gone.includes(j.id)));
      if (done.size > 0) setPendingJobIds((cur) => cur.filter((id) => !done.has(id)));
    } catch {
      /* keep them; the next tick tries again */
    } finally {
      pollBusy.current = false;
    }
  };
  const polling = pendingJobIds.length > 0;
  // Every few seconds while any job runs (every 12 s while the live stream is
  // connected), paused while the tab is hidden; a studio_job event checks
  // right away.
  const pokePoll = usePoll(pollNow, { enabled: polling, interval: POLL_MS });
  useEvents({ kind: "studio_job" }, (ev) => {
    if (ev === null || pendingRef.current.includes(ev.entity_id)) pokePoll();
  });

  // Show freshly created jobs as pending tiles at the top.
  const { refresh: refreshActivity } = useActivity();
  const addCreated = (rows: StudioJob[]) => {
    // The SideNav's "running" count has no "started" event for Studio jobs.
    if (rows.length > 0) refreshActivity();
    const ids = rows.map((r) => r.id);
    setJobs((prev) => [...rows, ...prev.filter((p) => !ids.includes(p.id))]);
    setPendingJobIds((prev) => Array.from(new Set([...prev, ...ids])));
  };

  const retry = async (job: StudioJob, model?: string) => {
    try {
      const j = await api.retryStudioJob(job.id, model ? { model } : {});
      addCreated(batchPlaceholders(j));
      toast.success(model ? "Trying another model" : "Retrying");
      return true;
    } catch (e: any) {
      toast.error(errorText(e, "Retry failed"));
      // Queue down (503): the new job is already marked failed; show it.
      if (e instanceof ApiError && e.status === 503) await loadJobs().catch(() => undefined);
      return false;
    }
  };

  // Remove background / upscale: a new pending tile at the top.
  const opSource = (item: { job?: StudioJob; assetId?: number }): ImageOpSource | null =>
    item.job ? { jobId: item.job.id } : item.assetId ? { assetId: item.assetId } : null;
  const opStarted = (op: ImageOp, j: StudioJob) => {
    addCreated(batchPlaceholders(j));
    toast.success(op === "remove_background" ? "Removing the background…" : "Upscaling…");
  };
  const removeBackground = async (item: { job?: StudioJob; assetId?: number }) => {
    const source = opSource(item);
    if (!source) return;
    try {
      opStarted("remove_background", await api.runImageOp("remove_background", source));
    } catch (e) {
      toast.error(errorText(e, "Couldn't remove the background"));
    }
  };
  const startUpscale = (item: { job?: StudioJob; assetId?: number; label: string }) => {
    const source = opSource(item);
    if (source) setUpscaling({ source, label: item.job ? `#${item.job.id}` : item.label });
  };

  // Rough price of running a job again with this model ("" = unknown).
  const modelOptionsFor = (job: StudioJob): ModelOption[] =>
    job.operation
      ? opModels[job.operation].map((m) => withKnownPrice({ id: m.id, name: m.name, price_text: m.free ? "free" : m.price_text }))
      : job.output_kind === "image"
      ? imageModels
      : job.first_frame_job_id || job.first_frame_asset_id
      ? videoModels.filter((m) => m.first_frame)
      : videoModels;
  const retryPrice = (job: StudioJob): string => {
    const m = modelOptionsFor(job).find((x) => x.id === job.provider_model);
    if (!m?.price_text) return "";
    if (m.price_text === "free") return "free";
    // An upscale runs again at its own factor (×2 and ×4 cost differently).
    const p =
      job.operation === "upscale"
        ? priceForFactor(m.price_text, job.options?.factor || 2)
        : priceFromText(m.price_text);
    if (p === null) return "";
    // Clips are priced per 5 s in the picker.
    const n = job.output_kind === "video_clip" && !job.operation ? p * ((job.duration_seconds || 5) / 5) : p;
    return `about ${usd(n)}`;
  };

  // Stop waiting for a running job. The provider may still finish (and
  // bill) a clip it already has.
  const cancelJob = async (job: StudioJob) => {
    if (!window.confirm(`Cancel this ${job.output_kind === "image" ? "picture" : "clip"}? ${chargeNote(job)}`)) return;
    try {
      const fresh = await api.cancelStudioJob(job.id);
      latest.current.set(fresh.id, fresh);
      setJobs((cur) => cur.map((j) => (j.id === fresh.id ? { ...j, ...fresh } : j)));
      if (TERMINAL.includes(fresh.status)) setPendingJobIds((cur) => cur.filter((x) => x !== fresh.id));
      toast.success(fresh.error === "Cancelled" ? "Cancelled" : "It had already finished");
    } catch (e) {
      toast.error(errorText(e, "Couldn't cancel it"));
    }
  };

  const toggleFavorite = async (job: StudioJob) => {
    const favorite = !job.favorite;
    setJobs((cur) => cur.map((j) => (j.id === job.id ? { ...j, favorite } : j)));
    try {
      const fresh = await api.updateStudioJob(job.id, { favorite });
      setJobs((cur) => cur.map((j) => (j.id === fresh.id ? { ...j, ...fresh } : j)));
    } catch (e: any) {
      setJobs((cur) => cur.map((j) => (j.id === job.id ? { ...j, favorite: !favorite } : j)));
      toast.error(errorText(e, "Couldn't update the favorite"));
    }
  };

  const showResults = filter === "all" || filter === "images" || filter === "videos";

  const jobTiles = useMemo<Tile[]>(() => {
    const out: Tile[] = [];
    if (!showResults) return out;
    // Studio jobs (newest first; already ordered by API).
    for (const j of jobs) {
      if (filter === "images" && j.output_kind !== "image") continue;
      if (filter === "videos" && j.output_kind !== "video_clip") continue;
      const isImage = j.output_kind === "image";
      const ready = FINISHED.includes(j.status);
      out.push({
        key: `job-${j.id}`,
        kind: "job",
        imageUrl: ready && isImage ? api.studioPreview(j.id) : undefined,
        videoUrl: ready && !isImage ? api.studioPreview(j.id) : undefined,
        label: j.prompt.slice(0, 60) || `${j.output_kind} job`,
        sub: `${j.output_kind} · ${j.owner_kind} #${j.owner_id}`,
        status: j.status,
        jobId: j.id,
        job: j,
        ownerKind: j.owner_kind,
      });
    }
    return out;
  }, [jobs, filter, showResults]);

  const assetTiles = useMemo<Tile[]>(() => {
    const out: Tile[] = [];
    // Library filters narrow Studio results; on those tabs cast pictures
    // would only get in the way. The Characters / Scenes / Uploads tabs don't
    // show Studio results (or their filters), so they always list the cast.
    if (filtersActive && showResults) return out;
    const showAvatarAssets = filter === "all" || filter === "characters" || filter === "uploads";
    const showIngredientAssets =
      filter === "all" || filter === "scenes" || filter === "uploads";
    if (showAvatarAssets) {
      for (const av of avatars) {
        for (const a of av.assets) {
          if (filter === "uploads" && a.source !== "upload") continue;
          out.push({
            key: `av-${a.id}`,
            kind: "asset",
            imageUrl: api.publicAsset(a.public_token),
            label: `${av.name} · ${slotLabel("avatar", a.asset_type)}`,
            sub: `Character · ${a.source}`,
            selectable: true,
            assetId: a.id,
            asset: a,
          });
        }
      }
    }
    if (showIngredientAssets) {
      for (const ing of ingredients) {
        if (filter === "scenes" && ing.kind !== "scene") continue;
        for (const a of ing.assets) {
          if (filter === "uploads" && a.source !== "upload") continue;
          out.push({
            key: `ing-${a.id}`,
            kind: "asset",
            imageUrl: api.publicAsset(a.public_token),
            label: `${ing.name} · ${slotLabel("ingredient", a.asset_type)}`,
            sub: `${ing.kind} · ${a.source}`,
            selectable: true,
            assetId: a.id,
            asset: a,
          });
        }
      }
    }
    return out;
  }, [avatars, ingredients, filter, filtersActive, showResults]);

  const tiles = useMemo(() => [...jobTiles, ...assetTiles], [jobTiles, assetTiles]);

  // The full-screen viewer: the tile key it shows, null = closed. It steps
  // through every tile that has a picture or clip, in grid order.
  const [viewKey, setViewKey] = useState<string | null>(null);
  const viewItems = useMemo(() => tiles.filter((t) => t.imageUrl || t.videoUrl), [tiles]);

  const toggleRef = (assetId: number) => {
    setSelectedRefs((cur) =>
      cur.includes(assetId) ? cur.filter((x) => x !== assetId) : [...cur, assetId]
    );
  };

  // Another media chip hides some results, so nothing stays selected.
  const firstFilter = useRef(true);
  useEffect(() => {
    if (firstFilter.current) {
      firstFilter.current = false;
      return;
    }
    setChecked([]);
  }, [filter]);

  const toggleChecked = (jobId: number) =>
    setChecked((cur) => (cur.includes(jobId) ? cur.filter((x) => x !== jobId) : [...cur, jobId]));

  const leaveSelect = () => {
    setSelectMode(false);
    setChecked([]);
  };

  // Esc leaves select mode (an open dialog takes Esc for itself).
  useEffect(() => {
    if (!selectMode) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || document.querySelector('[role="dialog"]')) return;
      leaveSelect();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectMode]);

  const checkedJobs = useMemo(() => jobs.filter((j) => checked.includes(j.id)), [jobs, checked]);

  // The last character sheet, once all its views are done.
  const sheetJobs = useMemo(
    () => (lastSheet ? jobs.filter((j) => lastSheet.ids.includes(j.id)) : []),
    [jobs, lastSheet]
  );
  const sheetReady =
    sheetJobs.length > 0 && sheetJobs.every((j) => TERMINAL.includes(j.status)) &&
    sheetJobs.some((j) => FINISHED.includes(j.status));

  const selectSheet = () => {
    if (!lastSheet) return;
    setSelectMode(true);
    setChecked(sheetJobs.filter((j) => FINISHED.includes(j.status)).map((j) => j.id));
    setBulkSave(null);
  };
  const sheetSelected =
    lastSheet !== null &&
    checked.length > 0 &&
    checked.every((id) => lastSheet.ids.includes(id));

  const bulkFavorite = async (favorite: boolean) => {
    const ids = [...checked];
    setBulkBusy(true);
    try {
      const res = await api.bulkFavoriteStudioJobs(ids, favorite);
      setJobs((cur) => cur.map((j) => (res.updated.includes(j.id) ? { ...j, favorite } : j)));
      const n = res.updated.length;
      toast.success(`${favorite ? "Favorited" : "Unfavorited"} ${n === 1 ? "1 result" : `${n} results`}`);
      setChecked([]);
      if (!favorite && libKind === "favorites") await loadJobs();
    } catch (e: any) {
      toast.error(errorText(e, "Couldn't update favorites"));
      // Large selections go in several requests; show what did change.
      await loadJobs().catch(() => undefined);
    } finally {
      setBulkBusy(false);
    }
  };

  const bulkDelete = async () => {
    const ids = [...checked];
    if (!window.confirm(`Delete ${ids.length === 1 ? "1 result" : `${ids.length} results`}? This can't be undone.`)) return;
    setBulkBusy(true);
    try {
      const res = await api.bulkDeleteStudioJobs(ids);
      const gone = [...res.deleted, ...res.missing];
      setJobs((cur) => cur.filter((j) => !gone.includes(j.id)));
      setPendingJobIds((cur) => cur.filter((x) => !gone.includes(x)));
      toast.success(`Deleted ${res.deleted.length === 1 ? "1 result" : `${res.deleted.length} results`}`);
      setChecked([]);
    } catch (e: any) {
      toast.error(errorText(e, "Delete failed"));
      await loadJobs().catch(() => undefined);
    } finally {
      setBulkBusy(false);
    }
  };

  // Studio results and cast images are separate files, so deleting one
  // never removes the other.
  // onDeleted runs as soon as the delete succeeds, before the reload (the
  // viewer moves to the next result while this one still exists).
  const deleteTile = async (t: Tile, onDeleted?: () => void) => {
    const what =
      t.kind === "job"
        ? "this Studio result"
        : `"${t.label}" from the cast (projects that use it lose this picture)`;
    const running = t.job && !TERMINAL.includes(t.job.status) ? ` ${chargeNote(t.job)}` : "";
    if (!window.confirm(`Delete ${what}? This can't be undone.${running}`)) return;
    try {
      if (t.kind === "job" && t.jobId) {
        await api.deleteStudioJob(t.jobId);
        setPendingJobIds((cur) => cur.filter((x) => x !== t.jobId));
      } else if (t.assetId) {
        await api.deleteAsset(t.assetId);
        setSelectedRefs((cur) => cur.filter((x) => x !== t.assetId));
      }
      onDeleted?.();
      toast.success("Deleted");
    } catch (e: any) {
      toast.error(errorText(e, "Delete failed"));
    }
    await reload();
  };

  const quickSave = async (jobId: number) => {
    try {
      // The backend picks the slot from the job's owner, so report what it chose.
      const asset = await api.saveStudioJob(jobId);
      toast.success(`Saved to the cast as ${slotLabel(asset.owner_kind, asset.asset_type)}`);
    } catch (e: any) {
      toast.error(errorText(e, "Save failed"));
    }
    await reload();
  };

  const assetById = useMemo(() => {
    const map = new Map<number, Asset>();
    for (const av of avatars) for (const a of av.assets) map.set(a.id, a);
    for (const ing of ingredients) for (const a of ing.assets) map.set(a.id, a);
    return map;
  }, [avatars, ingredients]);
  // An operation result's original: its URL (Compare) and its tile key.
  const sourceUrl = (job: StudioJob): string | null => {
    if (job.source_job_id) return api.studioPreview(job.source_job_id);
    const a = job.source_asset_id ? assetById.get(job.source_asset_id) : undefined;
    return a ? api.publicAsset(a.public_token) : null;
  };
  // Show the picture an operation ran on in the viewer (loaded first when
  // it isn't in the list right now).
  const openSource = async (job: StudioJob) => {
    if (job.source_asset_id) {
      const key = [`av-${job.source_asset_id}`, `ing-${job.source_asset_id}`].find((k) => tiles.some((t) => t.key === k));
      if (key) setViewKey(key);
      else toast.info(assetById.has(job.source_asset_id) ? "Pick All Media to see the Cast picture" : "That Cast picture was deleted");
      return;
    }
    const id = job.source_job_id;
    if (!id) return;
    const key = `job-${id}`;
    if (tiles.some((t) => t.key === key)) {
      setViewKey(key);
      return;
    }
    try {
      const src = await api.getStudioJob(id);
      setJobs((cur) => (cur.some((j) => j.id === src.id) ? cur : [...cur, src]));
      setViewKey(key);
    } catch (e) {
      toast.error(errorText(e, "That picture was deleted"));
    }
  };
  const selectedAssets: Asset[] = useMemo(
    () => selectedRefs.map((id) => assetById.get(id)).filter((x): x is Asset => Boolean(x)),
    [selectedRefs, assetById]
  );

  const clearFilters = () => {
    setSearch("");
    setQ("");
    setLibKind("all");
    setOwnerFilter("");
  };

  const startProject = async (job: StudioJob) => {
    // Save image results back to the cast first so the project
    // can pick them up as a reference; clip results stay in /studio.
    if (job.output_kind === "image" && job.status === "completed") {
      try {
        await api.saveStudioJob(job.id);
        await reload();
      } catch {
        /* already saved */
      }
    }
    const params = new URLSearchParams({
      owner_kind: job.owner_kind,
      owner_id: String(job.owner_id),
    });
    router.push(`/projects/new?${params.toString()}`);
  };

  const renderTile = (t: Tile) => (
    <TileCard
      key={t.key}
      tile={t}
      selected={t.assetId ? selectedRefs.includes(t.assetId) : false}
      selectMode={selectMode && t.kind === "job"}
      checked={t.jobId ? checked.includes(t.jobId) : false}
      onToggleChecked={() => t.jobId && toggleChecked(t.jobId)}
      onSelect={() => t.assetId && toggleRef(t.assetId)}
      onQuickSave={quickSave}
      onAssign={(jobId) => setAssigning(jobs.find((j) => j.id === jobId) || null)}
      onDelete={() => deleteTile(t)}
      onFavorite={() => t.job && toggleFavorite(t.job)}
      onRetry={() => t.job && retry(t.job)}
      onTryModel={() => t.job && setRetrying(t.job)}
      onRemix={(jobId) => {
        const job = jobs.find((j) => j.id === jobId);
        if (job) remix(job);
      }}
      onNewProject={(jobId) => {
        const job = jobs.find((j) => j.id === jobId);
        if (job) startProject(job);
      }}
      onUseInProject={() => t.job && setPlacing(t.job)}
      onAnimate={() => animate(t)}
      onCancel={() => t.job && cancelJob(t.job)}
      onDownload={() => {
        const url = t.imageUrl || t.videoUrl;
        if (!url) return;
        const n = downloadName(t as ViewerItem);
        downloadFile(url, n.base, n.ext).then((ok) => {
          if (!ok)
            toast.error("Couldn't download the file", { action: { label: "Open", href: url }, key: "download" });
        });
      }}
      onOpen={() => setViewKey(t.key)}
      opModels={opModels}
      onRemoveBackground={() => removeBackground(t)}
      onUpscale={() => startUpscale(t)}
      onOpenSource={() => t.job && openSource(t.job)}
      retryPrice={t.job ? retryPrice(t.job) : ""}
    />
  );

  const gridCls = "grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3";

  const moreButton = showResults && hasMore && (
    <div className="flex justify-center py-4">
      <button className="btn-ghost text-sm disabled:opacity-50" onClick={loadMore} disabled={loadingMore}>
        {loadingMore ? "Loading…" : "Load more"}
      </button>
    </div>
  );

  return (
    <div className="flex flex-col h-screen">
      <header className="border-b border-ink-800 px-6 py-4 flex items-center gap-3">
        <h1 className="text-lg font-semibold">Studio</h1>
        <span className="text-xs text-ink-400 ml-2">
          Browse your cast, generate new images and clips, save them back.
        </span>
      </header>

      <div className="px-6 pt-4 flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={clsx("chip", filter === f.id && "chip-active")}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto px-3 sm:px-6 py-4 pb-44">
        {showResults && (
          <div className="mb-3 flex flex-wrap items-center gap-2" role="toolbar" aria-label="Studio results">
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search prompts and labels"
              aria-label="Search Studio results"
              className="input text-sm py-1.5 w-full sm:w-64"
            />
            {LIB_KINDS.map((k) => (
              <button
                key={k.id}
                onClick={() => setLibKind(k.id)}
                aria-pressed={libKind === k.id}
                className={clsx("chip text-xs", libKind === k.id && "chip-active")}
              >
                {k.label}
              </button>
            ))}
            <select
              value={ownerFilter}
              onChange={(e) => setOwnerFilter(e.target.value)}
              className="chip text-xs bg-ink-800 max-w-[180px] truncate"
              aria-label="Filter by cast member"
            >
              <option value="">All cast</option>
              {avatars.length > 0 && (
                <optgroup label="Characters">
                  {avatars.map((a) => (
                    <option key={`av-${a.id}`} value={`avatar:${a.id}`}>{a.name}</option>
                  ))}
                </optgroup>
              )}
              {ingredients.length > 0 && (
                <optgroup label="Scenes & ingredients">
                  {ingredients.map((i) => (
                    <option key={`ing-${i.id}`} value={`ingredient:${i.id}`}>
                      {i.name} ({i.kind})
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            <div className="flex flex-wrap gap-2 sm:ml-auto">
              {lastSheet && sheetReady && (
                <button
                  onClick={selectSheet}
                  className="chip text-xs"
                  title="Select the views of the character sheet you just made"
                >
                  Select this sheet
                </button>
              )}
              <button
                onClick={() => (selectMode ? leaveSelect() : setSelectMode(true))}
                aria-pressed={selectMode}
                className={clsx("chip text-xs", selectMode && "chip-active")}
                title="Select several results to favorite, save or delete them together (Esc to leave)"
              >
                {selectMode ? "Done" : "Select"}
              </button>
            </div>
          </div>
        )}

        {selectMode && showResults && (
          <div className="sticky top-0 z-20 mb-3 card flex flex-wrap items-center gap-2 p-2 text-xs">
            <span className="font-medium px-1" aria-live="polite">
              {checked.length} selected
            </span>
            <button
              className="chip text-xs"
              onClick={() => setChecked(jobTiles.map((t) => t.jobId!).filter(Boolean))}
              disabled={jobTiles.length === 0}
            >
              Select all (visible)
            </button>
            <button className="chip text-xs" onClick={() => setChecked([])} disabled={checked.length === 0}>
              Clear
            </button>
            <span className="flex-1" />
            <button
              className="chip text-xs disabled:opacity-50"
              disabled={bulkBusy || checked.length === 0}
              onClick={() => bulkFavorite(true)}
            >
              ★ Favorite
            </button>
            <button
              className="chip text-xs disabled:opacity-50"
              disabled={bulkBusy || checked.length === 0}
              onClick={() => bulkFavorite(false)}
            >
              Unfavorite
            </button>
            <button
              className="chip text-xs disabled:opacity-50"
              disabled={bulkBusy || !checkedJobs.some((j) => j.output_kind === "image")}
              title="Images only; clips are skipped"
              onClick={() =>
                setBulkSave(
                  sheetSelected && lastSheet
                    ? { owner: `avatar:${lastSheet.avatarId}`, slot: sheetSlot("avatar") }
                    : {}
                )
              }
            >
              Save to Cast…
            </button>
            <button
              className="chip text-xs hover:text-accent disabled:opacity-50"
              disabled={bulkBusy || checked.length === 0}
              onClick={bulkDelete}
            >
              Delete
            </button>
            <button className="chip text-xs" onClick={leaveSelect} aria-label="Leave select mode">
              ✕
            </button>
          </div>
        )}

        {tiles.length === 0 ? (
          filtersActive && showResults ? (
            <div className="text-ink-300 text-sm py-12 text-center">
              No results match —{" "}
              <button className="underline" onClick={clearFilters}>
                clear filters
              </button>
            </div>
          ) : (
            <div className="text-ink-300 text-sm py-12 text-center">
              Nothing yet. Add characters or scenes in <a href="/cast" className="underline">Cast</a>,
              or use the prompt bar below to generate.
            </div>
          )
        ) : (
          <>
            {jobTiles.length > 0 && <div className={gridCls}>{jobTiles.map(renderTile)}</div>}
            {moreButton}
            {assetTiles.length > 0 && (
              <div className={clsx(gridCls, jobTiles.length > 0 && "mt-3")}>{assetTiles.map(renderTile)}</div>
            )}
          </>
        )}
        {/* A page with nothing to show here can still have older results. */}
        {tiles.length === 0 && moreButton}
      </div>

      {viewKey && (
        <StudioViewer
          items={viewItems}
          openKey={viewKey}
          onNavigate={setViewKey}
          onClose={() => setViewKey(null)}
          actions={{
            onFavorite: (job) => {
              // Un-starring under the Favorites filter drops it from the list:
              // step to the next result instead of closing.
              if (job.favorite && libKind === "favorites") {
                const i = viewItems.findIndex((x) => x.jobId === job.id);
                const neighbour = viewItems[i + 1] || viewItems[i - 1];
                setViewKey(neighbour ? neighbour.key : null);
              }
              toggleFavorite(job);
            },
            onQuickSave: (job) => quickSave(job.id),
            onAssign: setAssigning,
            onRemix: remix,
            onTryModel: setRetrying,
            onNewProject: startProject,
            onUseInProject: setPlacing,
            onAnimate: animate,
            onDelete: (item) => {
              const t = tiles.find((x) => x.key === item.key);
              if (!t) return;
              const i = viewItems.findIndex((x) => x.key === item.key);
              const neighbour = viewItems[i + 1] || viewItems[i - 1];
              deleteTile(t, () => setViewKey(neighbour ? neighbour.key : null));
            },
            onToggleRef: toggleRef,
            isRef: (id) => selectedRefs.includes(id),
            onRemoveBackground: removeBackground,
            removeBackgroundLabel: removeBgLabel(opModels),
            onUpscale: startUpscale,
            sourceUrl,
            onOpenSource: openSource,
          }}
        />
      )}

      {assigning && (
        <AssignDialog
          job={assigning}
          onClose={() => setAssigning(null)}
          onSaved={async () => {
            setAssigning(null);
            await reload();
          }}
        />
      )}

      {placing && <UseInProjectDialog job={placing} onClose={() => setPlacing(null)} />}

      {retrying && (
        <ModelPickerDialog
          job={retrying}
          models={modelOptionsFor(retrying)}
          onClose={() => setRetrying(null)}
          onPick={async (model) => {
            if (await retry(retrying, model)) setRetrying(null);
          }}
        />
      )}

      {upscaling && (
        <UpscaleDialog
          source={upscaling.source}
          sourceLabel={upscaling.label}
          models={opModels.upscale}
          modelsLoaded={opModels.loaded}
          onClose={() => setUpscaling(null)}
          onStarted={(j) => {
            setUpscaling(null);
            opStarted("upscale", j);
          }}
        />
      )}

      {bulkSave && (
        <BulkSaveDialog
          jobs={checkedJobs}
          avatars={avatars}
          ingredients={ingredients}
          defaultOwner={bulkSave.owner}
          defaultSlot={bulkSave.slot}
          onClose={() => setBulkSave(null)}
          onDone={async () => {
            setBulkSave(null);
            setChecked([]);
            await reload();
          }}
        />
      )}

      <PromptBar
        avatars={avatars}
        ingredients={ingredients}
        imageModels={imageModels}
        videoModels={videoModels}
        selectedRefs={selectedRefs}
        selectedAssets={selectedAssets}
        load={load}
        onCreated={(j) => addCreated(batchPlaceholders(j))}
        onSheetCreated={(res, avatarId) => {
          addCreated(res.jobs);
          setLastSheet({ avatarId, ids: res.batch_ids.length ? res.batch_ids : res.jobs.map((j) => j.id) });
        }}
        onAddRefs={(ids) =>
          setSelectedRefs((cur) => Array.from(new Set([...cur, ...ids])))
        }
        firstFrame={firstFrame}
        onClearFirstFrame={() => setFirstFrame(null)}
        onReload={reload}
      />
    </div>
  );
}

function TileCard({
  tile,
  selected,
  selectMode,
  checked,
  onToggleChecked,
  onSelect,
  onQuickSave,
  onAssign,
  onDelete,
  onNewProject,
  onUseInProject,
  onAnimate,
  onCancel,
  onDownload,
  onRemix,
  onFavorite,
  onRetry,
  onTryModel,
  onOpen,
  opModels,
  onRemoveBackground,
  onUpscale,
  onOpenSource,
  retryPrice,
}: {
  tile: Tile;
  selected: boolean;
  selectMode: boolean;
  checked: boolean;
  onToggleChecked: () => void;
  onSelect: () => void;
  onQuickSave: (jobId: number) => void;
  onAssign: (jobId: number) => void;
  onDelete: () => void;
  onNewProject: (jobId: number) => void;
  onUseInProject: () => void;
  onAnimate: () => void;
  onCancel: () => void;
  onDownload: () => void;
  onRemix: (jobId: number) => void;
  onFavorite: () => void;
  onRetry: () => void;
  onTryModel: () => void;
  onOpen: () => void;
  opModels: OpModels;
  onRemoveBackground: () => void;
  onUpscale: () => void;
  onOpenSource: () => void;
  /** "about $0.04" for running it again with the same model; "" = unknown. */
  retryPrice: string;
}) {
  const finished = tile.status === "completed" || tile.status === "saved";
  const videoRef = useRef<HTMLVideoElement>(null);
  const viewable = Boolean(tile.imageUrl || tile.videoUrl);
  const failed = tile.status === "failed";
  const running = tile.kind === "job" && Boolean(tile.status) && !finished && !failed;
  const job = tile.job;
  const cost = job ? costText(job) : null;
  const statusText = job ? studioStatusLabel(job) : tile.status || "";
  const checker = mayBeTransparent(tile);
  const fromText = job ? sourceChipText(job) : null;
  // Landscape and square pictures are shown whole (not cropped to the
  // portrait tile); known sizes decide at once, others once loaded.
  const knownW = job?.result_width || tile.asset?.width || 0;
  const knownH = job?.result_height || tile.asset?.height || 0;
  const [wide, setWide] = useState<boolean | null>(knownW && knownH ? knownW / knownH > 0.62 : null);
  const contain = wide === true || checker;
  const opMenu: TileMenuItem[] = [
    {
      label: removeBgLabel(opModels),
      onSelect: onRemoveBackground,
      title: "Make a copy with the background cut out (transparent PNG); the original stays",
    },
    { label: "Upscale…", onSelect: onUpscale, title: "Make a larger copy (×2 or ×4); the price shows before it runs" },
  ];

  // The same actions as before, per state, in the "⋯" menu. Delete is last.
  const menu: TileMenuItem[] = [];
  if (tile.kind === "job" && job) {
    if (running) {
      menu.push({ label: "Cancel…", onSelect: onCancel, title: "Stop waiting for this result" });
    }
    if (failed) {
      menu.push({
        label: retryPrice ? `Retry · ${retryPrice}` : "Retry",
        onSelect: onRetry,
        title: job.operation ? "Run this step again on the same picture" : "Run this generation again with the same settings",
      });
    }
    if (finished && tile.imageUrl) {
      menu.push(...opMenu);
      menu.push({ label: "Animate this", onSelect: onAnimate, title: "Make a clip that starts from this picture" });
    }
    if (finished && tile.videoUrl) {
      menu.push({
        label: "Use in project…",
        onSelect: onUseInProject,
        title: "Use this clip as a shot in a storyboard project (free)",
      });
    }
    if (tile.status === "completed" && tile.imageUrl) {
      menu.push({
        label: "Save",
        onSelect: () => onQuickSave(job.id),
        title: `Save to this job's ${
          tile.ownerKind === "ingredient" ? "scene as a reference" : "character as a lifestyle shot"
        }`,
      });
    }
    if (finished && tile.imageUrl) {
      menu.push({
        label: "Assign to…",
        onSelect: () => onAssign(job.id),
        title: "Save this image to a character or scene, as its main picture or another slot",
      });
    }
    if (finished && !job.operation) {
      menu.push({
        label: "Remix",
        onSelect: () => onRemix(job.id),
        title: "Load this result's prompt, avoid list, references, model and aspect into the prompt bar",
      });
    }
    if (finished || failed) {
      menu.push({
        label: "Try another model…",
        onSelect: onTryModel,
        title: "Make it again with a different model; the original stays",
      });
    }
    if (finished) {
      menu.push({
        label: "New project →",
        onSelect: () => onNewProject(job.id),
        title: "Start a new project with this result's character or scene",
      });
    }
    if (finished && tile.imageUrl) {
      menu.push({
        label: "Ambient scene →",
        href: `/ambient/new?studio_job=${job.id}`,
        title: "Make an hour-long ambient scene from this image",
      });
    }
    if (viewable) menu.push({ label: "Download", onSelect: onDownload, title: "Download the full-size file" });
    menu.push({ label: "Delete", onSelect: onDelete, danger: true, title: "Delete this Studio result" });
  } else if (tile.kind === "asset" && tile.assetId) {
    if (tile.selectable) {
      menu.push({ label: selected ? "Stop using as reference" : "Use as reference", onSelect: onSelect });
    }
    if (viewable) {
      menu.push(...opMenu);
      menu.push({ label: "Animate this", onSelect: onAnimate, title: "Make a clip that starts from this picture" });
      menu.push({ label: "Download", onSelect: onDownload, title: "Download the full-size file" });
    }
    menu.push({ label: "Delete", onSelect: onDelete, danger: true, title: "Delete this image from the cast" });
  }
  return (
    <div
      className={clsx(
        "group relative aspect-9/16 rounded-lg overflow-hidden bg-ink-800 border transition",
        selected || (selectMode && checked) ? "border-accent" : "border-ink-800 hover:border-ink-600",
        selectMode && "cursor-pointer"
      )}
      onClick={selectMode ? onToggleChecked : undefined}
      // Clips preview on hover with a mouse (a tap would start it behind the viewer).
      onPointerEnter={(e) => e.pointerType === "mouse" && videoRef.current?.play().catch(() => {})}
      onPointerLeave={() => videoRef.current?.pause()}
    >
      {tile.imageUrl && (
        <img
          src={tile.imageUrl}
          alt={tile.label}
          onLoad={(e) => {
            const im = e.currentTarget;
            if (im.naturalWidth && im.naturalHeight) setWide(im.naturalWidth / im.naturalHeight > 0.62);
          }}
          className={clsx("w-full h-full", contain ? "object-contain" : "object-cover", checker && "checker")}
        />
      )}
      {tile.videoUrl && (
        <video
          ref={videoRef}
          // #t=0.1: iOS Safari shows the first frame instead of a blank tile.
          src={`${tile.videoUrl}#t=0.1`}
          className="w-full h-full object-cover"
          muted
          loop
          playsInline
          preload="metadata"
        />
      )}
      {viewable && !selectMode && (
        // The whole tile opens the full-size viewer; the action buttons and
        // the favorite star sit above it. A link, so right-click, long-press
        // and Cmd/Ctrl-click still reach the file itself.
        <a
          href={tile.imageUrl || tile.videoUrl}
          data-view-key={tile.key}
          onClick={(e) => {
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            videoRef.current?.pause();
            onOpen();
          }}
          className="absolute inset-0 cursor-zoom-in focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent"
          aria-label={`Open ${tile.label}`}
          title="Open full size"
        />
      )}
      {tile.videoUrl && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-10 h-10 rounded-full bg-black/55 border border-white/30 text-white text-sm flex items-center justify-center transition md:group-hover:opacity-0"
        >
          ▶
        </span>
      )}
      {!tile.imageUrl && !tile.videoUrl && (
        <div className="w-full h-full flex flex-col items-center justify-center gap-1 text-ink-400 text-xs p-3 text-center">
          <span title={tile.status}>{statusText || "Preview unavailable"}</span>
          {failed && job?.error && <span className="text-[10px] line-clamp-4">{job.error}</span>}
        </div>
      )}

      <div className="pointer-events-none absolute inset-x-0 bottom-0 p-2 bg-linear-to-t from-black/85 to-transparent flex items-end gap-1">
        <div className="min-w-0 flex-1">
          <div className="text-xs text-white font-medium truncate">{tile.label}</div>
          <div className="text-[10px] text-ink-300 truncate">
            {tile.sub}
            {cost && (
              <span
                className="pointer-events-auto ml-1 text-ink-200"
                title={job?.cost_is_estimate ? "Estimated cost" : "Recorded cost"}
              >
                · {cost}
              </span>
            )}
          </div>
        </div>
        {job && !selectMode && (
          <button
            onClick={onFavorite}
            aria-pressed={Boolean(job.favorite)}
            aria-label={job.favorite ? "Unfavorite" : "Favorite"}
            title={job.favorite ? "Unfavorite" : "Favorite"}
            className={clsx(
              "pointer-events-auto shrink-0 w-6 h-6 rounded-full text-sm leading-none flex items-center justify-center",
              job.favorite ? "text-yellow-300" : "text-ink-200 hover:text-white"
            )}
          >
            {job.favorite ? "★" : "☆"}
          </button>
        )}
        {job && selectMode && job.favorite && (
          <span className="shrink-0 text-sm text-yellow-300" aria-label="Favorite">
            ★
          </span>
        )}
      </div>

      {tile.kind === "asset" && tile.selectable && (
        <button
          onClick={onSelect}
          aria-label={selected ? "Deselect reference" : "Use as reference"}
          aria-pressed={selected}
          className={clsx(
            "absolute top-2 left-2 w-7 h-7 pointer-coarse:w-9 pointer-coarse:h-9 rounded-full border text-xs flex items-center justify-center",
            selected
              ? "grad border-transparent text-ink-950"
              // Touch devices have no hover; show at 60% so it's discoverable,
              // then full opacity on hover for crisp desktop affordance.
              : "bg-black/60 border-ink-300 text-ink-100 opacity-60 group-hover:opacity-100 transition"
          )}
          title={selected ? "Selected as reference" : "Use as reference"}
        >
          {selected ? "✓" : "+"}
        </button>
      )}

      {selectMode && tile.jobId && (
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggleChecked}
          onClick={(e) => e.stopPropagation()}
          aria-label={`Select ${tile.label}`}
          className="absolute top-2 right-2 w-5 h-5 accent-current"
        />
      )}

      {!selectMode && menu.length > 0 && (
        // One "⋯" button: on hover / focus with a mouse on wide screens, always
        // on touch screens and narrow windows.
        <div className="absolute top-2 right-2">
          <TileMenu
            items={menu}
            label={`Actions for ${tile.label}`}
            className="transition md:opacity-0 md:group-hover:opacity-100 md:focus-visible:opacity-100 pointer-coarse:opacity-100"
          />
        </div>
      )}

      {tile.kind === "job" && (
        <div className="absolute top-2 left-2 flex flex-col items-start gap-1 max-w-[60%] pointer-events-none">
          {tile.status && tile.status !== "completed" && (
            <div
              className={clsx(
                "chip text-[10px] py-0.5 bg-ink-900/85",
                failed && "text-red-200 border-red-500/40"
              )}
              title={`Status: ${tile.status}`}
            >
              {statusText}
            </div>
          )}
          {job?.label && (
            <div className="chip text-[10px] py-0.5 bg-ink-900/85 truncate max-w-full" title={job.label}>
              {job.label}
            </div>
          )}
          {fromText && !selectMode && (
            <button
              type="button"
              onClick={onOpenSource}
              className="pointer-events-auto chip text-[10px] py-0.5 bg-ink-900/85 hover:text-white truncate max-w-full"
              title="Show the picture this was made from"
              aria-label={`Show the original picture (${fromText})`}
            >
              {fromText}
            </button>
          )}
        </div>
      )}
    </div>
  );
}


function PromptBar({
  avatars,
  ingredients,
  imageModels,
  videoModels,
  selectedRefs,
  selectedAssets,
  load,
  onCreated,
  onSheetCreated,
  onAddRefs,
  firstFrame,
  onClearFirstFrame,
  onReload,
}: {
  avatars: Avatar[];
  ingredients: Ingredient[];
  imageModels: ModelOption[];
  videoModels: VideoModelOption[];
  selectedRefs: number[];
  selectedAssets: Asset[];
  load: PromptLoad | null;
  onCreated: (j: StudioJob) => void;
  onSheetCreated: (res: { batch_ids: number[]; jobs: StudioJob[] }, avatarId: number) => void;
  onAddRefs: (assetIds: number[]) => void;
  firstFrame: FirstFrame | null;
  onClearFirstFrame: () => void;
  onReload: () => Promise<void> | void;
}) {
  const [outputKind, setOutputKind] = useState<"image" | "video_clip">("image");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [model, setModel] = useState<string>("");
  const [clipSeconds, setClipSeconds] = useState<number>(5);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadOwner, setUploadOwner] = useState<string>("");
  const [avoid, setAvoid] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [aspect, setAspect] = useState<StudioAspect>("");
  const [variations, setVariations] = useState(1);
  const [sheetOpen, setSheetOpen] = useState(false);
  const promptRef = useRef<HTMLInputElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  // A model to select once the list for the (possibly new) output kind is
  // in place; otherwise switching kinds resets to that kind's first model.
  const wantModel = useRef<string | null>(null);

  // Default upload target = first selected character → first selected
  // ingredient → first avatar → first ingredient. Re-derived as the cast
  // loads and as the user's reference selection changes — but only when
  // the user hasn't already picked a target manually.
  useEffect(() => {
    if (uploadOwner) return;
    const pick = () => {
      for (const a of selectedAssets) {
        if (a.owner_kind === "avatar" && a.avatar_id)
          return `avatar:${a.avatar_id}`;
      }
      for (const a of selectedAssets) {
        if (a.owner_kind === "ingredient" && a.ingredient_id)
          return `ingredient:${a.ingredient_id}`;
      }
      if (avatars[0]) return `avatar:${avatars[0].id}`;
      if (ingredients[0]) return `ingredient:${ingredients[0].id}`;
      return "";
    };
    const next = pick();
    if (next) setUploadOwner(next);
  }, [avatars, ingredients, selectedAssets, uploadOwner]);

  // A clip that starts from a picture: only models that can.
  const startsFromPicture = outputKind === "video_clip" && firstFrame !== null;
  const frameModels = useMemo(() => videoModels.filter((m) => m.first_frame), [videoModels]);
  const currentModels: ModelOption[] =
    outputKind === "image" ? imageModels : startsFromPicture ? frameModels : videoModels;
  useEffect(() => {
    // Reset to default when switching output kind so we don't carry a video
    // model into an image generation request; a starting picture keeps the
    // chosen model when it can start from one.
    const want = wantModel.current;
    if (want !== null && currentModels.length > 0) wantModel.current = null;
    if (want && currentModels.some((m) => m.id === want)) setModel(want);
    else if (firstFrame && currentModels.some((m) => m.id === model)) return;
    else setModel(currentModels[0]?.id || "");
  }, [outputKind, imageModels, videoModels, firstFrame?.kind, firstFrame?.id]);

  // "About $0.08" next to Generate: the picked model's rough price, times
  // the variations (images) or scaled to the clip length (priced per 5 s).
  const pickedModel = currentModels.find((m) => m.id === model);
  const unitPrice = priceFromText(pickedModel?.price_text);
  const total =
    unitPrice === null ? null : outputKind === "image" ? unitPrice * variations : unitPrice * (clipSeconds / 5);
  const aboutText = total === null ? "" : total === 0 ? "Free" : `About ${usd(total)}`;
  const priceTitle = pickedModel?.price_text
    ? `${pickedModel.name}: ${pickedModel.price_text}${outputKind === "image" && variations > 1 ? ` × ${variations}` : ""}`
    : undefined;

  // Fill the bar from Remix or a preset.
  const apply = (p: Omit<PromptLoad, "nonce">) => {
    setPrompt(p.prompt);
    setAvoid(p.negative_prompt);
    if (p.negative_prompt) setMoreOpen(true);
    setAspect(p.aspect_ratio);
    if (p.duration_seconds) setClipSeconds(snapClipSeconds(p.duration_seconds));
    if (p.outputKind !== outputKind) {
      wantModel.current = p.model;
      setOutputKind(p.outputKind);
    } else if (p.model && currentModels.some((m) => m.id === p.model)) {
      setModel(p.model);
    }
    barRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    promptRef.current?.focus();
  };

  useEffect(() => {
    if (load) apply(load);
  }, [load?.nonce]);

  const applyPreset = (p: PromptPreset) =>
    apply({
      outputKind: p.kind === "video" ? "video_clip" : "image",
      prompt: p.prompt,
      negative_prompt: p.negative_prompt,
      model: p.model,
      aspect_ratio: p.aspect_ratio,
    });

  const isImage = outputKind === "image";

  // Who a generation is for: the first selected character; else the first
  // selected ingredient; else the first avatar that exists; else the first ingredient.
  const owner = useMemo((): { kind: "avatar" | "ingredient"; id: number } | null => {
    let owner_kind: "avatar" | "ingredient" = "avatar";
    let owner_id: number | undefined = undefined;
    for (const a of selectedAssets) {
      if (a.owner_kind === "avatar" && a.avatar_id) {
        owner_kind = "avatar";
        owner_id = a.avatar_id;
        break;
      }
    }
    if (!owner_id) {
      for (const a of selectedAssets) {
        if (a.owner_kind === "ingredient" && a.ingredient_id) {
          owner_kind = "ingredient";
          owner_id = a.ingredient_id;
          break;
        }
      }
    }
    if (!owner_id) {
      if (avatars[0]) {
        owner_kind = "avatar";
        owner_id = avatars[0].id;
      } else if (ingredients[0]) {
        owner_kind = "ingredient";
        owner_id = ingredients[0].id;
      }
    }
    return owner_id ? { kind: owner_kind, id: owner_id } : null;
  }, [selectedAssets, avatars, ingredients]);
  const sheetAvatar = owner?.kind === "avatar" ? avatars.find((a) => a.id === owner.id) || null : null;

  const submit = async () => {
    if (!prompt.trim()) return;
    const owner_kind = owner?.kind || "avatar";
    const owner_id = owner?.id;
    if (!owner_id) {
      setError("Create a Character or Scene first (Cast → New).");
      return;
    }
    if (startsFromPicture && frameModels.length === 0) {
      setError("None of your video models can start from a picture. Remove the picture or add a model that can.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const frame =
        startsFromPicture && firstFrame
          ? firstFrame.kind === "job"
            ? { first_frame_job_id: firstFrame.id }
            : { first_frame_asset_id: firstFrame.id }
          : {};
      const body = {
        owner_kind,
        owner_id,
        prompt,
        reference_asset_ids: selectedRefs,
        aspect_ratio: aspect || null,
        ...(avoid.trim() ? { negative_prompt: avoid.trim() } : {}),
        ...(model ? { model } : {}),
        ...(outputKind === "video_clip" ? { duration_seconds: clipSeconds, ...frame } : { variations }),
      };
      const job =
        outputKind === "image"
          ? await api.generateImage(body)
          : await api.generateClip(body);
      onCreated(job);
      setPrompt("");
      if (startsFromPicture) onClearFirstFrame();
    } catch (e: any) {
      // A refusal (e.g. the safety check, a model that can't start from a
      // picture) comes back as the server's own sentence.
      setError(errorText(e, "Generation failed"));
      // Queue down (503): the jobs were stored as failed; show them.
      if (e instanceof ApiError && e.status === 503) onReload();
    } finally {
      setSubmitting(false);
    }
  };

  // Uploads a single file to the chosen owner. Throws on failure so the
  // Dropzone can flag it red. Auto-selects the resulting asset as a reference
  // via `onAddRefs` so the user can immediately hit Generate.
  const uploadRef = async (file: File): Promise<Asset> => {
    if (!uploadOwner) throw new Error("Pick a cast member to attach uploads to");
    const [kind, idStr] = uploadOwner.split(":");
    const id = Number(idStr);
    const asset =
      kind === "avatar"
        ? await api.uploadAvatarAsset(id, file, "reference_sheet", true)
        : await api.uploadIngredientAsset(id, file, "reference", true);
    onAddRefs([asset.id]);
    return asset;
  };

  return (
    <div
      ref={barRef}
      className="fixed bottom-0 left-0 md:left-56 right-0 z-10 bg-ink-900/95 border-t border-ink-800 backdrop-blur-sm"
    >
      <div className="max-w-5xl mx-auto px-3 sm:px-6 py-3">
        {selectedRefs.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5 text-xs text-ink-300">
            <span>References:</span>
            {selectedAssets.map((a) => (
              <span key={a.id} className="chip py-0.5 text-[11px]">
                {a.owner_kind === "avatar" ? "Char" : "Scene"} #{a.id} · {a.asset_type}
              </span>
            ))}
          </div>
        )}

        {startsFromPicture && firstFrame && (
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-ink-300">
            <span className="chip py-0.5 pl-0.5 pr-1 text-[11px] flex items-center gap-1.5">
              <img
                src={firstFrame.thumbUrl}
                alt=""
                className="w-6 h-6 rounded-full object-cover"
              />
              <span>Starts from: {firstFrame.label}</span>
              <button
                type="button"
                onClick={onClearFirstFrame}
                className="w-6 h-6 rounded-full text-ink-300 hover:text-white hover:bg-white/10"
                aria-label="Remove the starting picture"
                title="Remove the starting picture"
              >
                ✕
              </button>
            </span>
            {frameModels.length === 0 ? (
              <span className="text-amber-200">
                None of your video models can start from a picture. Pick one in{" "}
                <a href="/settings#models" className="underline">
                  Settings → Models
                </a>
                .
              </span>
            ) : (
              <span className="text-ink-400">Only models that can start from a picture are listed.</span>
            )}
          </div>
        )}

        {uploadOpen && (
          <div className="card p-3 mb-2 space-y-2">
            <div className="flex items-center gap-2 text-xs text-ink-300">
              <span className="font-medium text-ink-200">Upload references to</span>
              <select
                className="input text-xs py-1 flex-1 max-w-xs"
                value={uploadOwner}
                onChange={(e) => setUploadOwner(e.target.value)}
              >
                <option value="" disabled>
                  — pick a cast member —
                </option>
                {avatars.length > 0 && <optgroup label="Characters">
                  {avatars.map((a) => (
                    <option key={`av-${a.id}`} value={`avatar:${a.id}`}>{a.name}</option>
                  ))}
                </optgroup>}
                {ingredients.length > 0 && <optgroup label="Ingredients">
                  {ingredients.map((i) => (
                    <option key={`ing-${i.id}`} value={`ingredient:${i.id}`}>
                      {i.name} ({i.kind})
                    </option>
                  ))}
                </optgroup>}
              </select>
              <button
                type="button"
                onClick={() => setUploadOpen(false)}
                className="text-ink-400 hover:text-ink-100 text-xs"
                aria-label="Close upload tray"
              >
                ✕
              </button>
            </div>
            <Dropzone
              disabled={!uploadOwner}
              hint={
                uploadOwner
                  ? "HEIC/PNG/JPEG/WebP up to 50 MB. Uploaded assets auto-attach as references for your next generation."
                  : avatars.length + ingredients.length === 0
                  ? "Create a cast member first (Cast → New)."
                  : "Pick a target above."
              }
              onUpload={uploadRef}
              onSettled={() => onReload()}
            />
          </div>
        )}

        {/* Phones: the prompt gets its own full-width row; the controls wrap under it. */}
        <div className="card flex flex-wrap items-center gap-2 p-2">
          <button
            onClick={() => {
              // A picture to start from only applies to clips.
              if (outputKind === "video_clip" && firstFrame) onClearFirstFrame();
              setOutputKind(outputKind === "image" ? "video_clip" : "image");
            }}
            className="chip text-xs"
            title="Toggle output kind"
          >
            {outputKind === "image" ? "Image" : "Video"}
          </button>
          {outputKind === "video_clip" && (
            <select
              value={clipSeconds}
              onChange={(e) => setClipSeconds(Number(e.target.value))}
              className="chip text-xs bg-ink-800"
              title="Clip length (the model snaps it to the nearest length it supports)"
              aria-label="Clip length"
            >
              {CLIP_SECONDS.map((n) => (
                <option key={n} value={n}>
                  {n}s
                </option>
              ))}
            </select>
          )}
          {currentModels.length > 0 && (
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="chip text-xs bg-ink-800 max-w-[180px] truncate"
              title="Provider model"
              aria-label="Provider model"
            >
              {currentModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name || m.id}
                  {m.price_text ? ` · ${m.price_text}` : ""}
                </option>
              ))}
            </select>
          )}
          <select
            value={aspect}
            onChange={(e) => setAspect(asAspect(e.target.value))}
            className="chip text-xs bg-ink-800"
            title="Aspect ratio. Auto: characters 9:16, scenes 16:9"
            aria-label="Aspect ratio"
          >
            {ASPECTS.map((a) => (
              <option key={a.id || "auto"} value={a.id}>
                {a.label}
              </option>
            ))}
          </select>
          {isImage && (
            <select
              value={variations}
              onChange={(e) => setVariations(Number(e.target.value))}
              className="chip text-xs bg-ink-800"
              title="How many images to make from this prompt. Each one costs the same as a single image."
              aria-label="Variations"
            >
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n === 1 ? "1 image" : `${n} images`}
                </option>
              ))}
            </select>
          )}
          {sheetAvatar && (
            <button
              type="button"
              onClick={() => setSheetOpen(true)}
              className="chip text-xs"
              title={`Make front, side, back and other views of ${sheetAvatar.name} that match their pictures`}
            >
              Character sheet…
            </button>
          )}
          <PresetsMenu
            current={() => ({
              kind: isImage ? "image" : "video",
              prompt,
              negative_prompt: avoid.trim(),
              model,
              aspect_ratio: aspect,
            })}
            onApply={applyPreset}
          />
          <button
            type="button"
            onClick={() => setMoreOpen((v) => !v)}
            className="chip text-xs"
            aria-expanded={moreOpen}
            aria-controls="studio-more-row"
            title="Things to avoid in this generation"
          >
            {avoid.trim() ? "Avoid •" : "Avoid"}
          </button>
          <button
            type="button"
            onClick={() => setUploadOpen((v) => !v)}
            className="chip text-xs"
            title="Upload reference images and auto-attach them to this generation"
            aria-pressed={uploadOpen}
            aria-label="Upload reference images"
          >
            📎
          </button>
          <input
            ref={promptRef}
            aria-label="Prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What do you want to create?"
            className="flex-1 min-w-0 basis-full order-first bg-transparent outline-hidden text-sm px-2 py-2"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <EnhanceButton
            className="contents"
            text={prompt}
            kind={isImage ? "image" : "video"}
            model={model || null}
            disabled={submitting}
            onUse={(t) => {
              setPrompt(t);
              promptRef.current?.focus();
            }}
          />
          {aboutText && (
            <span
              className="text-xs text-ink-300 ml-auto sm:ml-0 whitespace-nowrap"
              title={priceTitle}
              aria-label={`Rough cost: ${aboutText}`}
            >
              {aboutText}
            </span>
          )}
          <button
            onClick={submit}
            disabled={submitting || !prompt.trim() || (startsFromPicture && frameModels.length === 0)}
            className="btn-primary text-sm disabled:opacity-50 ml-auto sm:ml-0"
            title={
              isImage && variations > 1
                ? `Makes ${variations} images: about ${variations}× the cost of one`
                : undefined
            }
          >
            {submitting ? "…" : isImage && variations > 1 ? `Generate ×${variations} →` : "Generate →"}
          </button>
          {moreOpen && (
            <div id="studio-more-row" className="basis-full flex flex-wrap items-center gap-2 px-2 pt-1">
              <label htmlFor="studio-avoid" className="text-xs text-ink-300">
                Avoid
              </label>
              <input
                id="studio-avoid"
                value={avoid}
                onChange={(e) => setAvoid(e.target.value)}
                maxLength={1000}
                placeholder="e.g. text overlays, crowds, cartoon look"
                className="input text-xs py-1 flex-1 min-w-[12rem]"
              />
              <span className="text-[11px] text-ink-400 basis-full">
                Your defaults from{" "}
                <a href="/settings#prompting" className="underline">
                  Settings → Prompting
                </a>{" "}
                are added automatically.
              </span>
            </div>
          )}
        </div>
        {error && <div className="text-xs text-accent mt-2">{error}</div>}
        {sheetOpen && sheetAvatar && (
          <CharacterSheetDialog
            avatar={sheetAvatar}
            models={imageModels}
            defaultModel={isImage ? model : imageModels[0]?.id || ""}
            onClose={() => setSheetOpen(false)}
            onCreated={(res) => {
              setSheetOpen(false);
              onSheetCreated(res, sheetAvatar.id);
            }}
          />
        )}
      </div>
    </div>
  );
}
