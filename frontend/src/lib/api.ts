// Typed wrapper around the FastAPI backend. All calls go through the browser
// (with credentials so the MVP password cookie travels with the request).

const API_BASE =
  typeof window !== "undefined"
    ? (window as any).__API_BASE ||
      process.env.NEXT_PUBLIC_API_BASE ||
      "http://localhost:8000"
    : process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

/** Error thrown for a non-2xx response; the message keeps the old
 *  "<status>: <detail>" shape, and status / detail / headers are exposed
 *  for callers that branch on them (e.g. the duplicate-upload 409). */
export class ApiError extends Error {
  status: number;
  detail: string;
  headers: Headers;
  constructor(message: string, status: number, detail: string, headers: Headers) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.headers = headers;
  }
}

/** What to show the user for a failed call: the server's own sentence for a
 *  refusal it explains (a 4xx such as the safety check's 422, or a 503 when
 *  the queue is down), else the error's message. */
export function errorText(e: unknown, fallback = "Something went wrong"): string {
  if (e instanceof ApiError) {
    if (e.status >= 400 && e.status < 600 && e.status !== 500 && e.detail && !e.detail.trim().startsWith("[")) {
      return e.detail;
    }
    return e.message || fallback;
  }
  const m = (e as { message?: unknown } | null)?.message;
  return typeof m === "string" && m ? m : fallback;
}

async function req<T>(
  path: string,
  init: RequestInit = {},
  raw = false
): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
    ...init,
  });
  if (!r.ok) {
    // Session expiry mid-flow: bounce back to the AuthGate so the user can
    // re-authenticate instead of seeing a wall of 401 toasts. Skip for the
    // auth/status probe itself (which is *expected* to 401 when signed out).
    if (r.status === 401 && typeof window !== "undefined" && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new CustomEvent("avs:unauthenticated"));
    }
    const text = await r.text();
    const rid = r.headers.get("X-Request-ID");
    // Try to surface the backend's structured `detail` instead of dumping raw
    // JSON at the user, and tag the request id so an error toast becomes
    // copy-pasteable into a support ticket / log search.
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      if (parsed?.detail) detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
    } catch {
      /* not JSON; fall through with the raw text */
    }
    const suffix = rid ? ` (req ${rid.slice(0, 8)}…)` : "";
    throw new ApiError(`${r.status}: ${detail}${suffix}`, r.status, detail, r.headers);
  }
  if (raw) return (await r.blob()) as unknown as T;
  if (r.status === 204) return undefined as unknown as T;
  return (await r.json()) as T;
}

export type Asset = {
  id: number;
  owner_kind: "avatar" | "ingredient";
  avatar_id: number | null;
  ingredient_id: number | null;
  asset_type: string;
  original_filename: string;
  mime_type: string;
  width: number;
  height: number;
  public_token: string;
  source: "upload" | "generated";
  created_at: string;
};

export type Avatar = {
  id: number;
  name: string;
  description: string;
  persona: string;
  visual_identity: string;
  default_disclosure_text: string;
  default_voice_provider: string;
  elevenlabs_voice_id: string;
  brand: string;
  created_at: string;
  updated_at: string;
  assets: Asset[];
};

export type Ingredient = {
  id: number;
  name: string;
  kind: "object" | "scene" | "style" | "prop";
  description: string;
  visual_identity: string;
  created_at: string;
  updated_at: string;
  assets: Asset[];
};

export type ApiKeySource = "app" | "env" | "none" | "unreadable";

export type ApiKeyStatus = {
  name: string;
  label: string;
  env_var: string;
  powers: string;
  docs_url: string;
  set: boolean;
  source: ApiKeySource;
  last4: string | null;
  updated_at: string | null;
  /** A problem visible from the key's shape alone (e.g. a Sogni worker key). */
  warning?: string | null;
  /** On/off switch: off hides the provider's models everywhere and refuses new work that needs it. */
  enabled?: boolean;
  /** True: on unless switched off. null: on once a key is set. */
  enabled_default?: boolean | null;
  enabled_explicit?: boolean;
  /** Set on the reply to switching off: active model kinds this provider was serving. */
  in_use?: string[];
  /** Provider options, e.g. Runware's { check_content }. */
  options?: Record<string, boolean>;
};

export type ApiKeysResponse = {
  mock_providers: boolean;
  keys: ApiKeyStatus[];
};

export type ApiKeyTestResult = { ok: boolean; detail: string };

export type SystemInfo = {
  version: string;
  mock_providers: boolean;
  ffmpeg_available: boolean;
  ffprobe_available: boolean;
  data_dir: string;
  public_base_url: string;
  redis_configured: boolean;
  keys: { openrouter: boolean; elevenlabs: boolean; gemini?: boolean; replicate?: boolean; sogni?: boolean; runware?: boolean };
  models: {
    chat: string | null;
    image: string | null;
    video: string | null;
    tts: string | null;
    music: string | null;
  };
  counts: {
    avatars: number;
    ingredients: number;
    brand_kits: number;
    projects: number;
    renders: number;
  };
};

export type BrandKit = {
  id: number;
  name: string;
  primary_color: string;
  end_card_bg_color: string;
  end_card_text_color: string;
  default_cta_text: string;
  default_disclosure_text: string;
  logo_public_token: string;
  created_at: string;
  updated_at: string;
};

export type CastMember = {
  id?: number;
  member_kind: "avatar" | "ingredient";
  avatar_id?: number | null;
  ingredient_id?: number | null;
  role: string;
};

export type Shot = {
  id: number;
  shot_order: number;
  shot_type: string;
  prompt: string;
  negative_prompt: string;
  duration_seconds: number;
  reference_asset_ids_json: number[] | null;
  reference_strategy: string;
  caption_text: string;
  camera_direction: string;
  model_override: string;
  provider: string;
  provider_model: string;
  status: string;
  clip_path: string;
  error: string;
  /** Edited since its clip was made: the next render makes a new clip. */
  clip_stale?: boolean;
  /** Aspect ratio the current clip was made at ("" = unknown or no clip). */
  clip_aspect_ratio?: string;
  /** Clips kept for this shot, the current one included (0 = no clip). */
  takes_count?: number;
  /** The clip check's verdict on the current clip; null = not checked. */
  inspection?: Inspection | null;
};

export type InspectionIssueKind = "likeness" | "anatomy" | "text" | "prompt_mismatch" | "artifact" | "other";

/** The clip check's verdict on one clip (a vision model looked at a few frames). */
export type Inspection = {
  /** skipped = the automatic check didn't run; summary says why. */
  verdict: "ok" | "problem" | "unsure" | "skipped";
  issues: { kind: InspectionIssueKind; detail: string }[];
  summary: string;
  checked_at: string | null;
  model: string;
  /** True = the automatic check after the clip landed. */
  auto: boolean;
  cost_estimate: number;
};

export type ClipCheckSettings = {
  auto_check: boolean;
  model: string;
  default_model: string;
  is_default: boolean;
  provider: string;
  /** null = the model isn't in the catalog. */
  takes_images: boolean | null;
  /** false = no OpenRouter key, so checks don't run. */
  available: boolean;
  /** true = adult mode is on: no clip is checked (automatic or manual). */
  adult_mode?: boolean;
  cost_per_check: number;
  currency: string;
};

export type ClipCheckModel = { id: string; name: string; usable: boolean; cost_per_check: number };

export type ClipCheckResult = { shot_id: number; take_id: number; inspection: Inspection; shot: Shot };

/** Result of adding, duplicating or deleting a shot. */
export type ShotEdit = {
  project: Project;
  /** The new shot; null after a delete. */
  shot: Shot | null;
  captions_resynced: boolean;
  /** Delete only: clip files thrown away. */
  clips_discarded: number;
};

export type AddShotType = "hero" | "b_roll" | "talking_head" | "transition";

/** Before adding, duplicating or deleting a shot: what it would put out of date. */
export type ShotEditImpact = {
  /** Lip-synced clips whose slice of the voiceover would move. */
  stale_shots: { id: number; shot_order: number; cost: number }[];
  stale_count: number;
  /** What the next Generate video would pay to make them again. */
  stale_cost: number;
  /** Add / duplicate: the new shot's own clip, made at the next Generate video. */
  new_clip_cost: number;
  currency: string;
  is_estimate: boolean;
};

/** One kept clip of a shot. */
export type Take = {
  id: number;
  shot_id: number;
  current: boolean;
  provider: string;
  provider_model: string;
  cost_usd: number | null;
  note: string;
  aspect_ratio: string;
  file_exists: boolean;
  /** Path under the API, e.g. /api/projects/1/shots/2/takes/3/clip */
  url: string;
  /** The clip check's verdict on this take; null = not checked. */
  inspection?: Inspection | null;
  created_at: string | null;
};

export type Voice = {
  voice_id: string;
  name: string;
  /** Free sample from the voice provider; "" when there is none. */
  preview_url?: string;
};

/** The voiceover the editor can play before any clip is paid for. */
export type VoiceoverPreview = {
  /** Path under the API, with a cache-buster. */
  url: string;
  source: "preview" | "render";
  fingerprint: string;
  /** Still matches the spoken script, voice and voice model. */
  current: boolean;
  duration_seconds: number;
  video_seconds: number;
  difference_seconds: number;
  fit: "fits" | "too_long" | "too_short";
  fit_text: string;
  cost_estimate: number;
  /** POST only: an identical voiceover already existed, so nothing was paid. */
  reused: boolean;
  created_at: string | null;
};

/** What "Generate video" would do right now, before anything is paid for. */
export type RenderPlanShot = {
  id: number;
  shot_order: number;
  action: "reuse" | "in_flight" | "generate";
  reason: string;
  lipsync: boolean;
  cost: number;
  /** Lip-synced and the voiceover is out of date: only Generate video fixes it, a re-roll would not. */
  needs_new_voiceover?: boolean;
};
export type RenderPlan = {
  voiceover: "silent" | "upload" | "reuse" | "new";
  /** The voice in the latest video no longer matches the spoken script or voice. */
  voiceover_changed: boolean;
  voiceover_cost: number;
  shots: RenderPlanShot[];
  reuse: number;
  in_flight: number;
  generate: number;
  clip_cost: number;
  total: number;
  currency: string;
  is_estimate: boolean;
  /** Body shots whose current clip the check flagged, by shot order. */
  flagged?: { shot_id: number; shot_order: number; summary: string; first_issue: string }[];
  flagged_count?: number;
};

export type Render = {
  id: number;
  project_id: number;
  status: string;
  audio_path: string;
  final_video_path: string;
  thumbnail_path: string;
  render_log: string;
  error: string;
  share_token: string;
  estimated_cost: number;
  actual_cost: number;
  created_at: string;
  updated_at: string;
};

export type Project = {
  id: number;
  title: string;
  original_script: string;
  mode: "reel_montage" | "talking_head_beta" | "static_motion" | "ambient_loop";
  aspect_ratio: string;
  target_duration_seconds: number;
  /** Newest completed render, for list thumbnails (GET /projects only). */
  latest_render_id?: number | null;
  cta_text: string;
  caption_style: string;
  include_disclosure: boolean;
  disclosure_text: string;
  creative_direction: string;
  voiceover_source: "tts" | "upload" | "silent";
  voiceover_upload_path: string;
  music_upload_path: string;
  music_volume: number;
  status: string;
  primary_avatar_id: number | null;
  brand_kit_id: number | null;
  /** The project's own voice; null = the main character's voice. */
  voice_id?: string | null;
  generated_plan_json: any;
  created_at: string;
  updated_at: string;
  cast_members: CastMember[];
  shots: Shot[];
};

export type PreflightCheck = {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail";
  message: string;
  detail: string;
};

export type PreflightResult = {
  ok: boolean;
  summary: "ok" | "warn" | "fail";
  checks: PreflightCheck[];
};

export type CostLine = {
  item: string;
  qty: number;
  unit: string;
  unit_cost: number;
  cost: number;
};

export type CostEstimate = {
  currency: string;
  total: number;
  lines: CostLine[];
  is_estimate: boolean;
  note: string;
  plan?: LoopPlan; // ambient estimates only
  /** Ambient: what the scene's picture alone costs, and what generating costs once it has one. */
  picture_cost?: number;
  after_picture_total?: number;
};

/** How an ambient loop is produced on the configured video model. */
export type LoopPlan = {
  model: string;
  max_clip_seconds: number;
  caps_known: boolean;
  supports_last_frame: boolean;
  strategy: "single" | "chain" | "drift";
  segments: number[];
  anchored: boolean;
  closed: boolean;
  join_crossfade: number;
  seam_crossfade: number;
  loop_seconds: number;
  unit_seconds: number;
  repeats: number;
  provider_calls: number;
  notes: string[];
  /** Less repetition: loop clips in the scene, their labels, exact-repeat period. */
  variants?: number;
  variant_labels?: string[];
  repeat_every_seconds?: number;
  /** False: the video model works from text alone, so a picture isn't used for the video. */
  picture_usable?: boolean;
};

export type StudioJob = {
  id: number;
  owner_kind: "avatar" | "ingredient";
  owner_id: number;
  output_kind: "image" | "video_clip";
  prompt: string;
  duration_seconds: number;
  provider: string;
  provider_model: string;
  status: string;
  result_path: string;
  result_asset_id: number | null;
  error: string;
  created_at: string;
  updated_at: string;
  /** What the user asked to avoid (the Settings defaults are added server-side). */
  negative_prompt?: string;
  reference_asset_ids?: number[];
  /** "" = the default for the owner (character 9:16, scene 16:9). */
  aspect_ratio?: string;
  /** Only on the create response: every job id the request made (incl. this one). */
  batch_ids?: number[];
  favorite?: boolean;
  /** Short display tag, e.g. "Front view" on a character-sheet job; "" otherwise. */
  label?: string;
  /** Money recorded for this job so far; null = nothing recorded yet. */
  cost_usd?: number | null;
  cost_is_estimate?: boolean;
  /** "Animate this": the Studio picture or Cast picture the clip starts from. */
  first_frame_job_id?: number | null;
  first_frame_asset_id?: number | null;
  /** A finished clip's real size and length; null = not measured (show the requested values). */
  result_width?: number | null;
  result_height?: number | null;
  result_duration_seconds?: number | null;
  /** Ids in reference_asset_ids whose picture was deleted since. */
  missing_reference_ids?: number[];
};

/** Filters for the Studio library list. All optional and combinable; newest first. */
export type StudioJobsQuery = {
  /** Case-insensitive substring of the prompt or label. */
  q?: string;
  favorite?: boolean;
  output_kind?: "image" | "video_clip";
  /** Both or neither. */
  owner_kind?: "avatar" | "ingredient";
  owner_id?: number;
  /** "active" = still pending/running; "completed" includes saved ones. */
  status?: "completed" | "failed" | "active";
  /** 1..200, default 50. */
  limit?: number;
  /** Only jobs with a smaller id (the next page). */
  before_id?: number;
};

export type StudioBulkSaveResult = {
  saved: Asset[];
  skipped: { id: number; reason: string }[];
};

export type StudioRetryBody = {
  /** ""/null = keep the source job's model. */
  model?: string | null;
  prompt?: string | null;
  negative_prompt?: string | null;
  aspect_ratio?: "9:16" | "16:9" | "1:1" | null;
};

export type CharacterSheetView = { id: string; label: string; aspect_ratio: string };

export type CharacterSheetBody = {
  avatar_id: number;
  views?: string[];
  /** null = the avatar's own pictures. */
  reference_asset_ids?: number[] | null;
  model?: string | null;
  style?: string;
  negative_prompt?: string;
};

export type StudioAspect = "" | "9:16" | "16:9" | "1:1";

export type StudioGenerateBody = {
  owner_kind: "avatar" | "ingredient";
  owner_id: number;
  prompt: string;
  negative_prompt?: string;
  reference_asset_ids?: number[];
  model?: string;
  duration_seconds?: number;
  /** null = the owner's default aspect. */
  aspect_ratio?: "9:16" | "16:9" | "1:1" | null;
  /** 1..4, images only. */
  variations?: number;
  /** Clips only, at most one: start from a finished Studio picture or a Cast picture. */
  first_frame_job_id?: number | null;
  first_frame_asset_id?: number | null;
};

/** A Studio clip filed as a shot's take (POST …/takes/from-studio). */
export type TakeFromStudio = Take & {
  /** "" = the clip fits; otherwise one or two plain sentences. */
  warning: string;
  /** The length kept (a longer clip is trimmed to the shot). */
  duration_seconds: number | null;
};

/** One entry of the video model list. */
export type VideoModelOption = {
  id: string;
  name: string;
  description: string;
  /** The model can start a clip from a picture ("Animate this"). */
  first_frame?: boolean;
};

export type PromptDefaults = { image: string; video: string };
export type EnhanceKind = "image" | "video" | "script" | "scene";

/** Adult mode (Settings → Adult content). */
export type ContentMode = {
  adult: boolean;
  /** ADULT_MODE: what null goes back to. */
  default: boolean;
  /** Set in Settings (not following the default). */
  explicit: boolean;
  /** The text model adult mode uses (planning, Enhance, scene rewrites). */
  text_model: string;
  /** The planner chosen in Settings → Models (used while adult mode is off). */
  planner: string;
  /** The model that checks prompts for minors before they're sent. */
  safety_check?: { model: string; available: boolean };
  /** Cast characters marked "adult content allowed" (fictional, or the person consented). */
  adult_ok_avatars?: number[];
};

export type PromptPreset = {
  id: string;
  name: string;
  kind: "image" | "video";
  prompt: string;
  negative_prompt: string;
  model: string;
  aspect_ratio: StudioAspect;
  created_at: string;
};
export type PromptPresetIn = Omit<PromptPreset, "id" | "created_at">;

export type CustomModel = {
  id: number;
  name: string;
  kind: "character_lora" | "style_lora" | "voice_clone";
  owner_kind: "avatar" | "ingredient";
  owner_id: number;
  training_asset_ids_json: number[];
  config_json: Record<string, unknown>;
  provider: string;
  provider_model_id: string;
  provider_job_id: string;
  status: "pending" | "training" | "completed" | "failed" | "cancelled";
  progress: number;
  error: string;
  cost_usd: number;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
};

// --- Ambient (hour-long seamless-loop scenes) ---

export type AmbientSource = "video" | "image_drift";
export type AmbientLook = "photoreal" | "cinematic" | "painterly" | "watercolor" | "anime" | "claymation" | "dreamy";

/** A typed scene description rewritten as a real photograph (server-made). */
export type PhotoRewrite = { source: string; scene: string; light: string; camera: string };

export type AmbientConfig = {
  scene_preset: string;
  scene_prompt: string;
  /** Photoreal: rewrite a typed description as a photograph for the image model. */
  rewrite_prompt: boolean;
  photo?: PhotoRewrite | null;
  source: AmbientSource;
  loop_seconds: number;
  total_seconds: number;
  crossfade_seconds: number;
  slow_factor: number;
  fade_seconds: number;
  music_mood: string;
  music_prompt: string;
  music_volume: number;
  /** Video model for the loop clip ("" = the deployment default). */
  model_override: string;
  /** Image model for the scene's stills ("" = recommended, with a fallback). */
  still_model: string;
  chain_segments: boolean;
  music_bed_seconds: number;
  output_resolution: "1080p" | "4k";
  look: AmbientLook;
  look_strength: number;
  time_of_day: string;
  weather: string;
  sound: string; // "none" | "auto" | sound preset id
  sound_prompt: string;
  sound_volume: number;
  /** Set by the /picture endpoints: "" = generated at loop time. */
  picture_origin: "" | "upload" | "generated" | "studio" | "asset";
  /** Less repetition: several takes of the scene, or day → night. */
  variety: "none" | "takes" | "day_night";
  variations: number;
  /** Overlays (render-time; changing them keeps the loop). */
  overlay_title: string;
  title_seconds: number;
  watermark: string;
  watermark_position: Corner;
  watermark_opacity: number;
  timer: "none" | "countdown" | "elapsed";
  timer_position: Corner;
};

export type Corner = "top_left" | "top_right" | "bottom_left" | "bottom_right";

export type ScenePreset = {
  id: string;
  label: string;
  emoji: string;
  tagline: string;
  prompt: string;
  music_mood: string;
  palette: [string, string];
  preview_url: string | null;
};

export type MusicMood = { id: string; label: string; prompt: string };
export type AmbientOption = { id: string; label: string };

/** A model offered up front in the scene builder, with what it's like. */
export type ModelChoice = {
  id: string;
  label: string;
  note: string;
  est_cost: number | null;
  recommended: boolean;
  experimental: boolean;
};

export type AmbientPresets = {
  scenes: ScenePreset[];
  music_moods: MusicMood[];
  sounds?: MusicMood[];
  looks?: AmbientOption[];
  times_of_day?: AmbientOption[];
  weathers?: AmbientOption[];
  loop_options: number[];
  total_options: number[];
  aspect_options: string[];
  still_models?: ModelChoice[];
  video_models?: ModelChoice[];
  /** What "" means on this deployment. */
  default_still_model?: string;
  default_video_model?: string;
  /** The default's provider is switched off in Settings → API keys. */
  default_still_off?: boolean;
  default_video_off?: boolean;
};

export type LoopLevel = "good" | "warn" | "bad";

/** Local measurement of one loop clip: how it joins, flickers, freezes, drifts. */
export type LoopHealth = {
  shot_id: number;
  measured_at: string;
  clip_seconds: number;
  sample_fps: number;
  seam: {
    verdict: "seamless" | "slight_jump" | "visible_jump";
    level: LoopLevel;
    text: string;
    difference: number;
    typical: number;
    ratio: number;
  };
  flicker: { verdict: "none" | "slight" | "strong"; level: LoopLevel; text: string; percent: number };
  frozen: {
    verdict: "none" | "some" | "long" | "not_checked";
    level: LoopLevel;
    text: string;
    longest_seconds: number;
    total_seconds: number;
    at_seconds: number | null;
  };
  drift: { verdict: "none" | "slight" | "strong"; level: LoopLevel; text: string; distance: number };
  /** Path under the API: the last seconds of the loop, then its first seconds. */
  seam_preview_url: string;
  /** Where the join plays in the seam preview. */
  join_at_seconds: number;
};

export type LoopHealthResult = { loop_health: LoopHealth | null; variant_loop_health: (LoopHealth | null)[] };

export type AmbientScene = {
  id: number;
  title: string;
  status: string; // draft | generating | planned | completed | failed | cancelled
  aspect_ratio: string;
  total_seconds: number;
  config: AmbientConfig;
  loop_status: string; // pending | submitted | polling | completed | failed
  loop_error: string;
  loop_shot: Shot | null;
  /** Variations 2..N (variation 1 is loop_shot). */
  variant_shots?: Shot[];
  /** Loop health of the loop clip as it is now; null = not measured, or the clip changed since. */
  loop_health?: LoopHealth | null;
  /** One per variant_shots item, same order. */
  variant_loop_health?: (LoopHealth | null)[];
  music_ready: boolean;
  sound_ready?: boolean;
  picture_ready?: boolean;
  /** Changes whenever the picture file is replaced (image cache-bust). */
  picture_version?: string;
  repeats: number;
  plan: LoopPlan;
  /** Rate-card estimate of what Generate loop would buy now (USD). */
  generate_cost?: number;
  /** Loop clips already made: a new picture or a setting that needs a new clip discards them. */
  loop_at_stake?: { cost: number; from_ledger: boolean; partial: boolean } | null;
  /** A loop that failed part-way and can be continued from the failed clip. */
  resume?: {
    clip: number;
    clips: number;
    take: number;
    takes: number;
    kept_clips: number;
    cost: number;
    error: string;
    /** Resuming keeps the video model the kept clips were made with. */
    model?: string;
    /** Starting over would use a different model (the default changed since). */
    model_changed?: boolean;
  } | null;
  latest_render: Render | null;
  render_count: number;
  created_at: string;
  updated_at: string;
};

export const DEFAULT_AMBIENT_CONFIG: AmbientConfig = {
  scene_preset: "waterfall",
  scene_prompt: "",
  rewrite_prompt: true,
  photo: null,
  source: "video",
  loop_seconds: 30,
  total_seconds: 3600,
  crossfade_seconds: 2,
  slow_factor: 1,
  fade_seconds: 4,
  music_mood: "spa",
  music_prompt: "",
  music_volume: 0.7,
  model_override: "",
  still_model: "",
  chain_segments: true,
  music_bed_seconds: 0,
  output_resolution: "1080p",
  look: "photoreal",
  look_strength: 0.6,
  time_of_day: "any",
  weather: "any",
  sound: "none",
  sound_prompt: "",
  sound_volume: 0.6,
  picture_origin: "",
  variety: "none",
  variations: 3,
  overlay_title: "",
  title_seconds: 8,
  watermark: "",
  watermark_position: "bottom_right",
  watermark_opacity: 0.6,
  timer: "none",
  timer_position: "top_right",
};

export type PublishMeta = {
  title: string;
  description: string;
  tags: string[];
  category_id: string;
  contains_synthetic_media: boolean;
  made_for_kids: boolean;
};

export type YouTubeStatus = {
  configured: boolean;
  connected: boolean;
  channel: { id: string; title: string; thumbnail?: string | null } | null;
  redirect_uri: string;
  connected_at?: string | null;
};

export type PublishJob = {
  id: number;
  render_id: number;
  platform: string;
  status: "queued" | "uploading" | "processing" | "scheduled" | "published" | "failed";
  title: string;
  privacy: string;
  publish_at: string | null;
  contains_synthetic_media: boolean;
  bytes_total: number;
  bytes_sent: number;
  external_id: string;
  external_url: string;
  error: string;
  created_at: string;
  updated_at: string;
  uploaded_at: string | null;
  /** What YouTube says now; read this instead of `status` for the label. */
  state: PublishState;
  /** "" or a plain-language mismatch warning (private lock, removed, processing). */
  warning: string;
  yt_privacy_status: string;
  yt_upload_status: string;
  yt_publish_at: string | null;
  yt_title: string;
  yt_removed: boolean;
  yt_checked_at: string | null;
};

export type PublishState =
  | "uploading" | "published" | "scheduled" | "private" | "processing" | "removed" | "failed";

export type ProjectKind = "storyboard" | "ambient";

export type PublishFilter = "all" | "scheduled" | "published" | "failed" | "uploading";

export type PublishOverviewItem = {
  id: number;
  render_id: number;
  render_version: number | null;
  project_id: number;
  project_title: string;
  project_kind: ProjectKind;
  project_url: string;
  /** The page with the publish form for this job's render. */
  render_url: string;
  title: string;
  youtube_title: string;
  thumbnail_url: string | null;
  status: PublishJob["status"];
  state: PublishState;
  warning: string;
  error: string;
  privacy: string;
  youtube_privacy: string;
  youtube_upload_status: string;
  publish_at: string | null;
  youtube_publish_at: string | null;
  youtube_reason: string;
  removed: boolean;
  external_id: string;
  external_url: string;
  bytes_total: number;
  bytes_sent: number;
  uploaded_at: string | null;
  checked_at: string | null;
  created_at: string;
};

export type PublishOverviewPage = {
  items: PublishOverviewItem[];
  next_before_id: number | null;
  counts: Record<PublishFilter, number>;
};

export type NotPostedItem = {
  render_id: number;
  render_version: number | null;
  project_id: number;
  project_title: string;
  project_kind: ProjectKind;
  project_url: string;
  publish_url: string;
  thumbnail_url: string | null;
  file_available: boolean;
  failed_attempts: number;
  created_at: string;
};

export type NotPostedPage = { items: NotPostedItem[]; next_before_id: number | null };

export type PublishRefreshResult = {
  items: PublishOverviewItem[];
  checked: number;
  removed: number;
  skipped: number;
};

export type ResultsGroupBy = "type" | "preset" | "time_weather" | "length" | "character" | "model" | "aspect";

export type ResultRow = {
  publish_job_id: number;
  render_id: number;
  render_version: number | null;
  project_id: number;
  project_title: string;
  project_kind: ProjectKind;
  project_url: string;
  title: string;
  external_url: string;
  thumbnail_url: string | null;
  state: PublishState;
  warning: string;
  published_at: string;
  age_days: number;
  views: number | null;
  likes: number | null;
  comments: number | null;
  views_per_day: number | null;
  stats_fetched_at: string | null;
  /** Whole-project ledger total. */
  cost_usd: number;
  cost_is_estimate: boolean;
  cost_per_1k_views: number | null;
  views_before: number | null;
  views_change: number | null;
  trend_days: number | null;
  /** Up to the last 30 daily view counts, oldest first. */
  sparkline: number[];
  duration_seconds: number | null;
  length_bucket: string;
  preset: string;
  time_weather: string;
  character: string;
  model: string;
  aspect_ratio: string;
};

export type ResultGroup = {
  key: string;
  videos: number;
  /** Videos with YouTube numbers: views, median, cost per 1k and too_few use only these. */
  videos_with_numbers: number;
  total_views: number;
  median_views: number | null;
  total_cost_usd: number;
  cost_is_estimate: boolean;
  cost_per_1k_views: number | null;
  too_few: boolean;
};

export type ResultsOut = {
  configured: boolean;
  connected: boolean;
  /** Last successful fetch of the numbers. */
  last_refreshed_at: string | null;
  /** Last attempt (successful or not); failed = it ended in an error. */
  last_attempt_at?: string | null;
  last_attempt_failed?: boolean;
  last_attempt_error?: string;
  has_numbers: boolean;
  rows: ResultRow[];
  group_by: ResultsGroupBy;
  group_label: string;
  groups: ResultGroup[];
};

export type StatsRefreshOut = {
  videos: number;
  snapshots: number;
  removed: number;
  skipped: string;
  refreshed_at: string;
};

// --- Model catalog + runtime switching ---

export type ModelKind = "chat" | "image" | "video" | "tts" | "music" | "lipsync";

export type CatalogEntry = {
  id: string;
  kind: ModelKind;
  name: string;
  provider: "openrouter" | "gemini" | "elevenlabs" | "sogni" | "runware" | (string & {});
  description: string;
  usable: boolean;
  /** False when its provider is switched off in Settings → API keys (pickers hide it). */
  enabled?: boolean;
  new: boolean;
  active?: boolean;
  created?: number | null;
  context_length?: number | null;
  pricing?: Record<string, number>;
  supported_parameters?: string[];
  supported_voices?: string[];
  output_modalities?: string[];
  durations?: number[];
  max_seconds?: number | null;
  frame_images?: string[];
  resolutions?: string[];
  pricing_skus?: Record<string, string>;
  also_via?: string;
  /** Short catalog note (e.g. Sogni: "Premium Spark", "first frame"). */
  notes?: string;
};

export type ActiveModel = {
  model: string;
  provider: string;
  default: string;
  is_default: boolean;
  options: Record<string, string>;
};

export type ModelCatalog = {
  fetched_at: string | null;
  entries: CatalogEntry[];
  errors: Record<string, string>;
  counts: Partial<Record<ModelKind, number>>;
  new_count: number;
  active: Record<ModelKind, ActiveModel>;
};

export type SpendBucket = { key: string; cost_usd: number; count: number };
export type SpendBudget = { monthly_usd: number; hard_stop: boolean; alert_pcts: number[] };
export type SpendSummary = {
  month: string;
  currency: string;
  total_usd: number;
  estimated_usd: number;
  pending: number;
  budget: SpendBudget;
  budget_pct: number | null;
  blocked: boolean;
  mock_mode: boolean;
  by_kind: SpendBucket[];
  by_provider: SpendBucket[];
  by_model: SpendBucket[];
  top_projects: { project_id: number; title: string; cost_usd: number }[];
  daily: { date: string; cost_usd: number }[];
};
export type SpendEntry = {
  id: number;
  created_at: string;
  provider: string;
  kind: string;
  model: string;
  project_id: number | null;
  render_id: number | null;
  ref: string;
  cost_usd: number;
  is_estimate: boolean;
  source: string;
  note: string;
};
export type OpenRouterAccount = {
  available: boolean;
  reason?: string;
  usage?: number;
  usage_daily?: number;
  usage_monthly?: number;
  limit?: number | null;
  limit_remaining?: number | null;
};

// Most ids one Studio bulk request may carry (the backend's limit).
export const STUDIO_BULK_MAX = 200;

function chunks<T>(xs: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < xs.length; i += size) out.push(xs.slice(i, i + size));
  return out;
}

// ---------- live progress (events + activity) ----------

export type AppEventKind =
  | "render"
  | "plan"
  | "ambient_loop"
  | "ambient_render"
  | "studio_job"
  | "training"
  | "publish"
  | "budget";

export type AppEventStatus = "started" | "progress" | "completed" | "failed" | "cancelled";

/** One row of the events table, as the stream and /api/events/recent send it. */
export type AppEvent = {
  id: number;
  kind: AppEventKind | string;
  entity_type: string;
  entity_id: number;
  project_id: number | null;
  status: AppEventStatus | string;
  title: string;
  detail: string;
  url: string;
  severity: "info" | "warning" | "error" | string;
  read: boolean;
  created_at: string | null;
};

export type RecentEvents = {
  events: AppEvent[];
  has_more: boolean;
  /** Unread finished / failed events (the badge). */
  unread: number;
  latest_id: number;
};

export type ActivityItem = {
  kind: AppEventKind | string;
  entity_type: string;
  entity_id: number;
  project_id: number | null;
  title: string;
  url: string;
  stage: string;
  status: string;
  started_at: string | null;
};

export type Activity = {
  items: ActivityItem[];
  studio: {
    count: number;
    jobs: { id: number; output_kind: string; status: string; stage: string; prompt: string; label: string; started_at: string | null }[];
  };
  counts: { projects: number; ambient: number; studio: number; training: number; publish: number };
};

export const api = {
  base: API_BASE,

  // live progress
  /** The SSE stream's URL (`after` resumes after that event id). */
  eventStreamUrl: (after?: number | null) =>
    `${API_BASE}/api/events/stream${after ? `?after=${after}` : ""}`,
  recentEvents: (opts: { limit?: number; before_id?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.before_id) q.set("before_id", String(opts.before_id));
    return req<RecentEvents>(`/api/events/recent?${q}`);
  },
  markEventsRead: (body: { ids: number[] } | { all: true }) =>
    req<{ updated: number; unread: number }>("/api/events/read", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  activeWork: () => req<Activity>("/api/activity/active"),

  // model catalog
  modelCatalog: (kind?: ModelKind) =>
    req<ModelCatalog>(`/api/models/catalog${kind ? `?kind=${kind}` : ""}`),
  refreshModels: () => req<ModelCatalog>("/api/models/refresh", { method: "POST" }),
  activeModels: () => req<Record<ModelKind, ActiveModel>>("/api/models/active"),
  setActiveModel: (
    kind: ModelKind,
    model: string | null,
    opts: { allow_unlisted?: boolean; options?: Record<string, string> } = {}
  ) =>
    req<Record<ModelKind, ActiveModel>>("/api/models/active", {
      method: "PUT",
      body: JSON.stringify({ kind, model, allow_unlisted: !!opts.allow_unlisted, options: opts.options }),
    }),

  // ambient
  ambientPresets: () => req<AmbientPresets>("/api/ambient/presets"),
  ambientEstimate: (config: AmbientConfig) =>
    req<CostEstimate>("/api/ambient/estimate", {
      method: "POST",
      body: JSON.stringify({ config }),
    }),
  listAmbient: () => req<AmbientScene[]>("/api/ambient"),
  getAmbient: (id: number) => req<AmbientScene>(`/api/ambient/${id}`),
  createAmbient: (body: {
    title?: string;
    aspect_ratio?: string;
    config: AmbientConfig;
    generate?: boolean;
  }) => req<AmbientScene>("/api/ambient", { method: "POST", body: JSON.stringify(body) }),
  // discardLoop: go ahead even though the change discards loop clips that are
  // already made (the server answers 409 without it). Same for the picture calls.
  updateAmbient: (
    id: number,
    body: { title?: string; aspect_ratio?: string; config?: AmbientConfig },
    discardLoop = false
  ) =>
    req<AmbientScene>(`/api/ambient/${id}${discardLoop ? "?discard_loop=true" : ""}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  generateAmbientLoop: (id: number, regenerateMusic = true) =>
    req<{ scene_id: number; job_id: string; status: string }>(
      `/api/ambient/${id}/generate-loop?regenerate_music=${regenerateMusic}`,
      { method: "POST" }
    ),
  resumeAmbientLoop: (id: number) =>
    req<{ scene_id: number; job_id: string; status: string }>(`/api/ambient/${id}/resume-loop`, {
      method: "POST",
    }),
  regenerateAmbientMusic: (id: number) =>
    req<AmbientScene>(`/api/ambient/${id}/music`, { method: "POST" }),
  regenerateAmbientSound: (id: number) =>
    req<AmbientScene>(`/api/ambient/${id}/sound`, { method: "POST" }),
  uploadAmbientPicture: async (id: number, file: File, discardLoop = false) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`${API_BASE}/api/ambient/${id}/picture${discardLoop ? "?discard_loop=true" : ""}`, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    if (!r.ok) {
      let detail = await r.text();
      try {
        detail = JSON.parse(detail).detail ?? detail;
      } catch {
        /* not JSON */
      }
      throw new Error(`${r.status}: ${detail}`);
    }
    return (await r.json()) as AmbientScene;
  },
  generateAmbientPicture: (id: number, discardLoop = false) =>
    req<AmbientScene>(`/api/ambient/${id}/picture/generate${discardLoop ? "?discard_loop=true" : ""}`, {
      method: "POST",
    }),
  ambientPictureFrom: (id: number, body: { asset_id?: number; studio_job_id?: number }, discardLoop = false) =>
    req<AmbientScene>(`/api/ambient/${id}/picture/from${discardLoop ? "?discard_loop=true" : ""}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  clearAmbientPicture: (id: number, discardLoop = false) =>
    req<AmbientScene>(`/api/ambient/${id}/picture${discardLoop ? "?discard_loop=true" : ""}`, { method: "DELETE" }),
  ambientPictureUrl: (id: number, v?: string | number) =>
    `${API_BASE}/api/ambient/${id}/picture${v ? `?v=${encodeURIComponent(String(v))}` : ""}`,
  ambientSoundUrl: (id: number, v?: string) => `${API_BASE}/api/ambient/${id}/sound${v ? `?v=${encodeURIComponent(v)}` : ""}`,
  generatePresetPreviews: (missingOnly = true) =>
    req<{ presets: string[]; job_id: string | null; status: string }>(
      `/api/ambient/presets/previews?missing_only=${missingOnly}`,
      { method: "POST" }
    ),
  presetPreviewUrl: (path: string) => `${API_BASE}${path}`,
  ambientBatch: (body: { presets: string[]; aspect_ratio: string; config: AmbientConfig; render: boolean }) =>
    req<{ scene_ids: number[]; job_id: string; estimated_total: number; scenes: AmbientScene[] }>(
      "/api/ambient/batch",
      { method: "POST", body: JSON.stringify(body) }
    ),

  // publishing
  publishMeta: (projectId: number) => req<PublishMeta>(`/api/projects/${projectId}/publish-meta`),
  youtubeStatus: () => req<YouTubeStatus>("/api/youtube/status"),
  youtubeConnectUrl: () => req<{ url: string }>("/api/youtube/connect"),
  youtubeDisconnect: () => req<YouTubeStatus>("/api/youtube/disconnect", { method: "POST" }),
  publishYouTube: (
    renderId: number,
    body: {
      title: string;
      description: string;
      tags: string[];
      category_id: string;
      privacy: "private" | "unlisted" | "public";
      publish_at?: string | null;
      made_for_kids: boolean;
      contains_synthetic_media: boolean;
      set_thumbnail: boolean;
      /** Required to upload a render that is already on (or uploading to) YouTube. */
      confirm_duplicate?: boolean;
    }
  ) =>
    req<PublishJob>(`/api/renders/${renderId}/publish/youtube`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  renderPublishJobs: (renderId: number) => req<PublishJob[]>(`/api/renders/${renderId}/publish-jobs`),
  /** Resume a failed upload. `confirmNewUpload` is needed when it would make
   * a new YouTube video (YouTube rejected the old one, or the render is
   * already on YouTube by another upload). */
  retryPublishJob: (jobId: number, confirmNewUpload = false) =>
    req<PublishJob>(`/api/publish-jobs/${jobId}/retry`, {
      method: "POST",
      body: JSON.stringify({ confirm_new_upload: confirmNewUpload }),
    }),
  publishingJobs: (
    opts: { status?: PublishFilter; q?: string; limit?: number; before_id?: number | null; ids?: number[] } = {}
  ) => {
    const p = new URLSearchParams();
    if (opts.status && opts.status !== "all") p.set("status", opts.status);
    if (opts.q) p.set("q", opts.q);
    if (opts.limit) p.set("limit", String(opts.limit));
    if (opts.before_id != null) p.set("before_id", String(opts.before_id));
    for (const id of opts.ids ?? []) p.append("ids", String(id));
    const qs = p.toString();
    return req<PublishOverviewPage>(`/api/publishing/jobs${qs ? `?${qs}` : ""}`);
  },
  publishingNotPosted: (opts: { limit?: number; before_id?: number | null } = {}) => {
    const p = new URLSearchParams();
    if (opts.limit) p.set("limit", String(opts.limit));
    if (opts.before_id != null) p.set("before_id", String(opts.before_id));
    const qs = p.toString();
    return req<NotPostedPage>(`/api/publishing/not-posted${qs ? `?${qs}` : ""}`);
  },
  refreshPublishJob: (jobId: number) =>
    req<PublishOverviewItem>(`/api/publish-jobs/${jobId}/refresh`, { method: "POST" }),
  refreshPublishJobs: (ids: number[]) =>
    req<PublishRefreshResult>("/api/publish-jobs/refresh", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  results: (groupBy: ResultsGroupBy = "type") =>
    req<ResultsOut>(`/api/results?group_by=${encodeURIComponent(groupBy)}`),
  refreshResults: () => req<StatsRefreshOut>("/api/results/refresh", { method: "POST" }),
  /** Backend-relative paths (e.g. thumbnail_url) → absolute URL. */
  backendUrl: (path: string) => `${API_BASE}${path}`,

  renderAmbient: (id: number) =>
    req<{ scene_id: number; job_id: string; status: string }>(`/api/ambient/${id}/render`, {
      method: "POST",
    }),
  publicAsset: (token: string) => `${API_BASE}/api/public-assets/${token}`,
  studioPreview: (jobId: number) => `${API_BASE}/api/studio/jobs/${jobId}/preview`,
  renderDownload: (id: number) => `${API_BASE}/api/renders/${id}/download`,
  renderThumb: (id: number) => `${API_BASE}/api/renders/${id}/thumbnail`,
  exportRendersUrl: (projectId: number) =>
    `${API_BASE}/api/projects/${projectId}/renders/export`,
  shotClip: (projectId: number, shotId: number) =>
    `${API_BASE}/api/projects/${projectId}/shots/${shotId}/clip`,
  publicRender: (token: string) => `${API_BASE}/api/public-renders/${token}`,
  publicRenderThumb: (token: string) => `${API_BASE}/api/public-renders/${token}/thumbnail`,
  publicRenderMeta: (token: string) =>
    req<{ title: string; aspect_ratio: string; created_at: string; disclosure: string }>(
      `/api/public-renders/${token}/meta`
    ),

  // auth
  authStatus: () => req<{ authenticated: boolean }>("/api/auth/status"),
  login: (password: string) =>
    req<{ ok: boolean }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  logout: () => req<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),

  // providers
  providerStatus: () => req<Record<string, any>>("/api/providers/status"),
  videoModels: () => req<VideoModelOption[]>(
    "/api/providers/openrouter/video-models"
  ),
  imageModels: () => req<{ id: string; name: string; description: string }[]>(
    "/api/providers/openrouter/image-models"
  ),
  voices: () => req<Voice[]>("/api/providers/elevenlabs/voices"),
  captionStyles: () => req<string[]>("/api/providers/caption-styles"),
  healthCheck: () =>
    req<{
      ok: boolean;
      results: {
        group: string;
        mode: string;
        ok: boolean;
        message: string;
        latency_ms?: number;
      }[];
    }>("/api/providers/health-check", { method: "POST" }),
  systemInfo: () => req<SystemInfo>("/api/system/info"),

  // in-app API keys (values are write-only: the server never returns them)
  listApiKeys: () => req<ApiKeysResponse>("/api/settings/keys"),
  setApiKey: (name: string, value: string) =>
    req<ApiKeyStatus>(`/api/settings/keys/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  clearApiKey: (name: string) =>
    req<ApiKeyStatus>(`/api/settings/keys/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testApiKey: (name: string) =>
    req<ApiKeyTestResult>(`/api/settings/keys/${encodeURIComponent(name)}/test`, { method: "POST" }),
  setProviderOptions: (name: string, options: Record<string, boolean | null>) =>
    req<ApiKeyStatus>(`/api/settings/keys/${encodeURIComponent(name)}/options`, {
      method: "PUT",
      body: JSON.stringify(options),
    }),
  setProviderEnabled: (name: string, enabled: boolean | null) =>
    req<ApiKeyStatus>(`/api/settings/keys/${encodeURIComponent(name)}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  seedDemo: () =>
    req<{ ok: boolean; already_seeded: boolean; counts: { avatars: number; projects: number } }>(
      "/api/system/seed",
      { method: "POST" }
    ),

  // avatars
  listAvatars: () => req<Avatar[]>("/api/avatars"),
  getAvatar: (id: number) => req<Avatar>(`/api/avatars/${id}`),
  createAvatar: (body: Partial<Avatar>) =>
    req<Avatar>("/api/avatars", { method: "POST", body: JSON.stringify(body) }),
  updateAvatar: (id: number, body: Partial<Avatar>) =>
    req<Avatar>(`/api/avatars/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteAvatar: (id: number) => req<void>(`/api/avatars/${id}`, { method: "DELETE" }),
  uploadAvatarAsset: async (
    id: number,
    file: File,
    asset_type: string,
    rights: boolean
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("asset_type", asset_type);
    fd.append("rights_confirmed", String(rights));
    const r = await fetch(`${API_BASE}/api/avatars/${id}/assets`, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()) as Asset;
  },

  // ingredients
  listIngredients: () => req<Ingredient[]>("/api/ingredients"),
  getIngredient: (id: number) => req<Ingredient>(`/api/ingredients/${id}`),
  createIngredient: (body: Partial<Ingredient>) =>
    req<Ingredient>("/api/ingredients", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateIngredient: (id: number, body: Partial<Ingredient>) =>
    req<Ingredient>(`/api/ingredients/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteIngredient: (id: number) =>
    req<void>(`/api/ingredients/${id}`, { method: "DELETE" }),
  uploadIngredientAsset: async (
    id: number,
    file: File,
    asset_type: string,
    rights: boolean
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("asset_type", asset_type);
    fd.append("rights_confirmed", String(rights));
    const r = await fetch(`${API_BASE}/api/ingredients/${id}/assets`, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()) as Asset;
  },

  // brand kits
  listBrandKits: () => req<BrandKit[]>("/api/brand-kits"),
  createBrandKit: (body: Partial<BrandKit>) =>
    req<BrandKit>("/api/brand-kits", { method: "POST", body: JSON.stringify(body) }),
  updateBrandKit: (id: number, body: Partial<BrandKit>) =>
    req<BrandKit>(`/api/brand-kits/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteBrandKit: (id: number) =>
    req<void>(`/api/brand-kits/${id}`, { method: "DELETE" }),
  brandLogoUrl: (id: number) => `${API_BASE}/api/brand-kits/${id}/logo`,
  uploadBrandLogo: async (id: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`${API_BASE}/api/brand-kits/${id}/logo`, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()) as BrandKit;
  },

  // projects
  listProjects: () => req<Project[]>("/api/projects"),
  getProject: (id: number) => req<Project>(`/api/projects/${id}`),
  createProject: (body: any) =>
    req<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  duplicateProject: (p: Project) =>
    req<Project>("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        title: `${p.title || "Untitled"} (copy)`,
        original_script: p.original_script,
        mode: p.mode,
        aspect_ratio: p.aspect_ratio,
        target_duration_seconds: p.target_duration_seconds,
        cta_text: p.cta_text,
        caption_style: p.caption_style,
        include_disclosure: p.include_disclosure,
        disclosure_text: p.disclosure_text,
        creative_direction: p.creative_direction,
        primary_avatar_id: p.primary_avatar_id,
        brand_kit_id: p.brand_kit_id,
        cast: p.cast_members,
      }),
    }),
  updateProject: (id: number, body: any) =>
    req<Project>(`/api/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  updateCast: (id: number, cast: CastMember[]) =>
    req<Project>(`/api/projects/${id}/cast`, {
      method: "PATCH",
      body: JSON.stringify(cast),
    }),
  updateShot: (projectId: number, shotId: number, body: any) =>
    req<Shot>(`/api/projects/${projectId}/shots/${shotId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  reorderShots: (projectId: number, shotIds: number[]) =>
    req<Project>(`/api/projects/${projectId}/shots/reorder`, {
      method: "POST",
      body: JSON.stringify({ shot_ids: shotIds }),
    }),
  addShot: (
    projectId: number,
    body: { after_shot_id?: number | null; shot_type?: AddShotType; prompt?: string; duration_seconds?: number }
  ) =>
    req<ShotEdit>(`/api/projects/${projectId}/shots`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  shotEditImpact: (
    projectId: number,
    q: {
      op: "add" | "duplicate" | "delete";
      shot_id?: number | null;
      after_shot_id?: number | null;
      duration_seconds?: number;
      shot_type?: string;
    }
  ) => {
    const params = new URLSearchParams({ op: q.op });
    if (q.shot_id != null) params.set("shot_id", String(q.shot_id));
    if (q.after_shot_id != null) params.set("after_shot_id", String(q.after_shot_id));
    if (q.duration_seconds != null) params.set("duration_seconds", String(q.duration_seconds));
    if (q.shot_type) params.set("shot_type", q.shot_type);
    return req<ShotEditImpact>(`/api/projects/${projectId}/shots/edit-impact?${params.toString()}`);
  },
  duplicateShot: (projectId: number, shotId: number) =>
    req<ShotEdit>(`/api/projects/${projectId}/shots/${shotId}/duplicate`, { method: "POST" }),
  deleteShot: (projectId: number, shotId: number) =>
    req<ShotEdit>(`/api/projects/${projectId}/shots/${shotId}`, { method: "DELETE" }),
  shotTakes: (projectId: number, shotId: number) =>
    req<{ takes: Take[]; keep: number }>(`/api/projects/${projectId}/shots/${shotId}/takes`),
  useTake: (projectId: number, shotId: number, takeId: number) =>
    req<Shot>(`/api/projects/${projectId}/shots/${shotId}/takes/${takeId}/use`, { method: "POST" }),
  deleteTake: (projectId: number, shotId: number, takeId: number) =>
    req<void>(`/api/projects/${projectId}/shots/${shotId}/takes/${takeId}`, { method: "DELETE" }),
  /** Check a clip now (paid, about cost_per_check). Without takeId: the shot's current clip. */
  checkClip: (projectId: number, shotId: number, takeId?: number) =>
    req<ClipCheckResult>(`/api/projects/${projectId}/shots/${shotId}/check`, {
      method: "POST",
      body: JSON.stringify(takeId != null ? { take_id: takeId } : {}),
    }),
  clipCheckSettings: () => req<ClipCheckSettings>("/api/settings/clip-check"),
  setClipCheckSettings: (body: { auto_check?: boolean; model?: string; reset_model?: boolean; allow_unlisted?: boolean }) =>
    req<ClipCheckSettings>("/api/settings/clip-check", { method: "PUT", body: JSON.stringify(body) }),
  clipCheckModels: () => req<{ models: ClipCheckModel[]; active: string }>("/api/settings/clip-check/models"),
  /** Measure the loop (and each variation) again. Free. */
  measureLoopHealth: (id: number) => req<LoopHealthResult>(`/api/ambient/${id}/loop-health`, { method: "POST" }),
  /** A path the API returned (e.g. a take's or a voiceover's `url`) as a full URL. */
  mediaUrl: (path: string) => `${API_BASE}${path}`,
  voiceoverPreview: (id: number) => req<VoiceoverPreview>(`/api/projects/${id}/voiceover/preview`),
  makeVoiceoverPreview: (id: number) =>
    req<VoiceoverPreview>(`/api/projects/${id}/voiceover/preview`, { method: "POST" }),
  castAssets: (projectId: number) =>
    req<Asset[]>(`/api/projects/${projectId}/cast-assets`),
  deleteProject: (id: number) =>
    req<void>(`/api/projects/${id}`, { method: "DELETE" }),

  voiceoverUrl: (id: number) => `${API_BASE}/api/projects/${id}/voiceover`,
  musicUrl: (id: number) => `${API_BASE}/api/projects/${id}/music`,
  uploadVoiceover: async (id: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`${API_BASE}/api/projects/${id}/voiceover`, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()) as Project;
  },
  deleteVoiceover: (id: number) =>
    req<Project>(`/api/projects/${id}/voiceover`, { method: "DELETE" }),
  uploadMusic: async (id: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`${API_BASE}/api/projects/${id}/music`, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()) as Project;
  },
  deleteMusic: (id: number) =>
    req<Project>(`/api/projects/${id}/music`, { method: "DELETE" }),
  generateMusic: (id: number, prompt: string, duration_seconds?: number) =>
    req<Project>(`/api/projects/${id}/music/generate`, {
      method: "POST",
      body: JSON.stringify({ prompt, duration_seconds }),
    }),
  generatePlan: (id: number, opts: { preserveEdits?: boolean } = {}) =>
    req<any>(
      `/api/projects/${id}/generate-plan${opts.preserveEdits ? "?preserve_edits=true" : ""}`,
      { method: "POST" }
    ),
  editPlan: (
    id: number,
    body: {
      cleaned_voice_script?: string;
      end_card_text?: string;
      disclosure_text?: string;
      resync_captions?: boolean;
    }
  ) => req<Project>(`/api/projects/${id}/plan`, { method: "PATCH", body: JSON.stringify(body) }),
  preflight: (id: number) =>
    req<PreflightResult>(`/api/projects/${id}/preflight`),
  costEstimate: (id: number) =>
    req<CostEstimate>(`/api/projects/${id}/cost-estimate`),
  renderPlan: (id: number) => req<RenderPlan>(`/api/projects/${id}/render-plan`),
  regenerateShotsBulk: (id: number, shotIds: number[]) =>
    req<any>(`/api/projects/${id}/shots/regenerate-bulk`, {
      method: "POST",
      body: JSON.stringify({ shot_ids: shotIds }),
    }),
  generateVideo: (id: number, force = false) =>
    req<any>(
      `/api/projects/${id}/generate-video${force ? "?force=true" : ""}`,
      { method: "POST" }
    ),
  recompose: (id: number) =>
    req<any>(`/api/projects/${id}/recompose`, { method: "POST" }),
  retryRender: (id: number) =>
    req<{
      project_id: number;
      job_id: string;
      action: "recompose" | "resume";
      status: string;
      clips_in_flight?: number;
      voiceover?: "silent" | "upload" | "reuse" | "new";
      voiceover_cost?: number;
    }>(`/api/projects/${id}/retry`, { method: "POST" }),
  cancelRender: (id: number) =>
    req<{
      render_id: number | null;
      status: string;
      action: "noop" | "signalled" | "released";
      clips_in_flight?: number;
    }>(
      `/api/projects/${id}/cancel`,
      { method: "POST" }
    ),
  listRenders: (id: number) => req<Render[]>(`/api/projects/${id}/renders`),
  recentRenders: (limit = 12) =>
    req<
      {
        render_id: number;
        project_id: number;
        project_title: string;
        share_token: string;
        created_at: string;
      }[]
    >(`/api/renders/recent?limit=${limit}`),
  getRender: (id: number) => req<Render>(`/api/renders/${id}`),
  projectStatus: (id: number) =>
    req<{
      project_id: number;
      project_status: string;
      latest_render: Render | null;
      shots: Shot[];
    }>(`/api/projects/${id}/status`),
  regenerateShot: (projectId: number, shotId: number, recompose = true) =>
    req<any>(
      `/api/projects/${projectId}/shots/${shotId}/regenerate?recompose=${recompose}`,
      { method: "POST" }
    ),

  // custom model training
  startTraining: (
    ownerKind: "avatar" | "ingredient",
    ownerId: number,
    body: {
      name: string;
      kind: CustomModel["kind"];
      training_asset_ids: number[];
      config?: Record<string, unknown>;
    }
  ) =>
    req<CustomModel>(`/api/cast/${ownerKind}/${ownerId}/train`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listTrainingJobs: (q?: {
    owner_kind?: "avatar" | "ingredient";
    owner_id?: number;
    kind?: CustomModel["kind"];
  }) => {
    const params = new URLSearchParams();
    if (q?.owner_kind) params.set("owner_kind", q.owner_kind);
    if (q?.owner_id != null) params.set("owner_id", String(q.owner_id));
    if (q?.kind) params.set("kind", q.kind);
    const qs = params.toString();
    return req<CustomModel[]>(`/api/training-jobs${qs ? `?${qs}` : ""}`);
  },
  getTrainingJob: (id: number) => req<CustomModel>(`/api/training-jobs/${id}`),
  cancelTraining: (id: number) =>
    req<CustomModel>(`/api/training-jobs/${id}/cancel`, { method: "POST" }),
  deleteTraining: (id: number) =>
    req<void>(`/api/training-jobs/${id}`, { method: "DELETE" }),

  // prompting: default avoid lists, enhance, presets
  getPromptDefaults: () => req<PromptDefaults>("/api/prompts/defaults"),
  setPromptDefaults: (body: PromptDefaults) =>
    req<PromptDefaults>("/api/prompts/defaults", { method: "PUT", body: JSON.stringify(body) }),
  resetPromptDefaults: () => req<PromptDefaults>("/api/prompts/defaults", { method: "DELETE" }),
  getContentMode: () => req<ContentMode>("/api/settings/content"),
  /** null = back to ADULT_MODE. */
  setContentMode: (adult: boolean | null) =>
    req<ContentMode>("/api/settings/content", { method: "PUT", body: JSON.stringify({ adult }) }),
  setAvatarAdultOk: (avatarId: number, adultOk: boolean) =>
    req<ContentMode>(`/api/settings/content/avatars/${avatarId}`, {
      method: "PUT",
      body: JSON.stringify({ adult_ok: adultOk }),
    }),
  enhancePrompt: (body: { text: string; kind: EnhanceKind; model?: string | null }) =>
    req<{ text: string; model: string }>("/api/prompts/enhance", {
      method: "POST",
      body: JSON.stringify({ text: body.text, kind: body.kind, model: body.model || null }),
    }),
  listPresets: () => req<PromptPreset[]>("/api/prompts/presets"),
  createPreset: (p: PromptPresetIn) =>
    req<PromptPreset>("/api/prompts/presets", { method: "POST", body: JSON.stringify(p) }),
  updatePreset: (id: string, p: PromptPresetIn) =>
    req<PromptPreset>(`/api/prompts/presets/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(p),
    }),
  deletePreset: (id: string) =>
    req<void>(`/api/prompts/presets/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // studio
  listStudioJobs: (params: StudioJobsQuery = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      q.set(k, String(v));
    }
    const qs = q.toString();
    return req<StudioJob[]>(`/api/studio/jobs${qs ? `?${qs}` : ""}`);
  },
  updateStudioJob: (id: number, body: { favorite?: boolean; label?: string }) =>
    req<StudioJob>(`/api/studio/jobs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  // The bulk endpoints take at most STUDIO_BULK_MAX ids, so bigger
  // selections go in several requests and the answers are merged.
  bulkDeleteStudioJobs: async (ids: number[]) => {
    const out = { deleted: [] as number[], missing: [] as number[] };
    for (const part of chunks(ids, STUDIO_BULK_MAX)) {
      const r = await req<{ deleted: number[]; missing: number[] }>("/api/studio/jobs/bulk-delete", {
        method: "POST",
        body: JSON.stringify({ ids: part }),
      });
      out.deleted.push(...r.deleted);
      out.missing.push(...r.missing);
    }
    return out;
  },
  bulkFavoriteStudioJobs: async (ids: number[], favorite: boolean) => {
    const out = { updated: [] as number[], missing: [] as number[] };
    for (const part of chunks(ids, STUDIO_BULK_MAX)) {
      const r = await req<{ updated: number[]; missing: number[] }>("/api/studio/jobs/bulk-favorite", {
        method: "POST",
        body: JSON.stringify({ ids: part, favorite }),
      });
      out.updated.push(...r.updated);
      out.missing.push(...r.missing);
    }
    return out;
  },
  bulkSaveStudioJobs: async (body: {
    ids: number[];
    owner_kind: "avatar" | "ingredient";
    owner_id: number;
    asset_type: string | null;
  }) => {
    const out: StudioBulkSaveResult = { saved: [], skipped: [] };
    for (const part of chunks(body.ids, STUDIO_BULK_MAX)) {
      const r = await req<StudioBulkSaveResult>("/api/studio/jobs/bulk-save", {
        method: "POST",
        body: JSON.stringify({ ...body, ids: part }),
      });
      out.saved.push(...r.saved);
      out.skipped.push(...r.skipped);
    }
    return out;
  },
  // Makes a NEW job from a finished or failed one, with any overrides.
  retryStudioJob: (id: number, body: StudioRetryBody = {}) =>
    req<StudioJob>(`/api/studio/jobs/${id}/retry`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  characterSheetViews: () => req<CharacterSheetView[]>("/api/studio/character-sheet/views"),
  makeCharacterSheet: (body: CharacterSheetBody) =>
    req<{ batch_ids: number[]; jobs: StudioJob[] }>("/api/studio/character-sheet", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  generateImage: (body: StudioGenerateBody) =>
    req<StudioJob>("/api/studio/generate-image", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  generateClip: (body: StudioGenerateBody) =>
    req<StudioJob>("/api/studio/generate-clip", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getStudioJob: (id: number) => req<StudioJob>(`/api/studio/jobs/${id}`),
  /** These jobs only (at most 200; deleted ones are left out), whatever the filters. */
  studioJobsByIds: (ids: number[]) =>
    ids.length === 0
      ? Promise.resolve([] as StudioJob[])
      : req<StudioJob[]>(`/api/studio/jobs?ids=${ids.slice(0, 200).join(",")}`),
  /** Stop waiting for a running job (it becomes failed, "Cancelled"); a finished one comes back unchanged. */
  cancelStudioJob: (id: number) => req<StudioJob>(`/api/studio/jobs/${id}/cancel`, { method: "POST" }),
  /** File a finished Studio clip as this shot's current take (free). */
  takeFromStudio: (projectId: number, shotId: number, studioJobId: number) =>
    req<TakeFromStudio>(`/api/projects/${projectId}/shots/${shotId}/takes/from-studio`, {
      method: "POST",
      body: JSON.stringify({ studio_job_id: studioJobId }),
    }),
  // Files a COPY of a finished Studio image as a new cast asset. With no
  // options it goes to the job's own owner in the backend's default slot; a
  // bare string is still accepted as the slot for older callers.
  saveStudioJob: (
    id: number,
    opts?: string | { owner_kind?: "avatar" | "ingredient"; owner_id?: number; asset_type?: string }
  ) => {
    const body = typeof opts === "string" ? { asset_type: opts } : opts || {};
    return req<Asset>(`/api/studio/jobs/${id}/save`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  deleteStudioJob: (id: number) =>
    req<void>(`/api/studio/jobs/${id}`, { method: "DELETE" }),

  // cast assets (images on a character / scene)
  updateAsset: (id: number, body: { asset_type: string }) =>
    req<Asset>(`/api/assets/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteAsset: (id: number) => req<void>(`/api/assets/${id}`, { method: "DELETE" }),
  spendSummary: (month?: string) =>
    req<SpendSummary>(`/api/spend/summary${month ? `?month=${month}` : ""}`),
  spendEntries: (opts: { month?: string; project_id?: number; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.month) q.set("month", opts.month);
    if (opts.project_id) q.set("project_id", String(opts.project_id));
    if (opts.limit) q.set("limit", String(opts.limit));
    return req<SpendEntry[]>(`/api/spend/entries?${q}`);
  },
  setSpendBudget: (b: SpendBudget) =>
    req<SpendBudget>("/api/spend/budget", { method: "PUT", body: JSON.stringify(b) }),
  openRouterAccount: () => req<OpenRouterAccount>("/api/spend/openrouter"),
};
