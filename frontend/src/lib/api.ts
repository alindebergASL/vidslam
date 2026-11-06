// Typed wrapper around the FastAPI backend. All calls go through the browser
// (with credentials so the MVP password cookie travels with the request).

const API_BASE =
  typeof window !== "undefined"
    ? (window as any).__API_BASE ||
      process.env.NEXT_PUBLIC_API_BASE ||
      "http://localhost:8000"
    : process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

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
    throw new Error(`${r.status}: ${detail}${suffix}`);
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

export type SystemInfo = {
  version: string;
  mock_providers: boolean;
  ffmpeg_available: boolean;
  ffprobe_available: boolean;
  data_dir: string;
  public_base_url: string;
  redis_configured: boolean;
  keys: { openrouter: boolean; elevenlabs: boolean; gemini?: boolean };
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
};

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

export type AmbientConfig = {
  scene_preset: string;
  scene_prompt: string;
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
};

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
  music_ready: boolean;
  sound_ready?: boolean;
  picture_ready?: boolean;
  /** Changes whenever the picture file is replaced (image cache-bust). */
  picture_version?: string;
  repeats: number;
  plan: LoopPlan;
  latest_render: Render | null;
  render_count: number;
  created_at: string;
  updated_at: string;
};

export const DEFAULT_AMBIENT_CONFIG: AmbientConfig = {
  scene_preset: "waterfall",
  scene_prompt: "",
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
};

// --- Model catalog + runtime switching ---

export type ModelKind = "chat" | "image" | "video" | "tts" | "music" | "lipsync";

export type CatalogEntry = {
  id: string;
  kind: ModelKind;
  name: string;
  provider: "openrouter" | "gemini" | "elevenlabs";
  description: string;
  usable: boolean;
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

export const api = {
  base: API_BASE,

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
  updateAmbient: (
    id: number,
    body: { title?: string; aspect_ratio?: string; config?: AmbientConfig }
  ) => req<AmbientScene>(`/api/ambient/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  generateAmbientLoop: (id: number, regenerateMusic = true) =>
    req<{ scene_id: number; job_id: string; status: string }>(
      `/api/ambient/${id}/generate-loop?regenerate_music=${regenerateMusic}`,
      { method: "POST" }
    ),
  regenerateAmbientMusic: (id: number) =>
    req<AmbientScene>(`/api/ambient/${id}/music`, { method: "POST" }),
  regenerateAmbientSound: (id: number) =>
    req<AmbientScene>(`/api/ambient/${id}/sound`, { method: "POST" }),
  uploadAmbientPicture: async (id: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`${API_BASE}/api/ambient/${id}/picture`, {
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
  generateAmbientPicture: (id: number) =>
    req<AmbientScene>(`/api/ambient/${id}/picture/generate`, { method: "POST" }),
  ambientPictureFrom: (id: number, body: { asset_id?: number; studio_job_id?: number }) =>
    req<AmbientScene>(`/api/ambient/${id}/picture/from`, { method: "POST", body: JSON.stringify(body) }),
  clearAmbientPicture: (id: number) =>
    req<AmbientScene>(`/api/ambient/${id}/picture`, { method: "DELETE" }),
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
    }
  ) =>
    req<PublishJob>(`/api/renders/${renderId}/publish/youtube`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  renderPublishJobs: (renderId: number) => req<PublishJob[]>(`/api/renders/${renderId}/publish-jobs`),
  retryPublishJob: (jobId: number) =>
    req<PublishJob>(`/api/publish-jobs/${jobId}/retry`, { method: "POST" }),

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
  videoModels: () => req<{ id: string; name: string; description: string }[]>(
    "/api/providers/openrouter/video-models"
  ),
  imageModels: () => req<{ id: string; name: string; description: string }[]>(
    "/api/providers/openrouter/image-models"
  ),
  voices: () => req<{ voice_id: string; name: string }[]>("/api/providers/elevenlabs/voices"),
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
    }>(`/api/projects/${id}/retry`, { method: "POST" }),
  cancelRender: (id: number) =>
    req<{ render_id: number; status: string; action: "noop" | "signalled" }>(
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

  // studio
  listStudioJobs: () => req<StudioJob[]>("/api/studio/jobs"),
  generateImage: (body: any) =>
    req<StudioJob>("/api/studio/generate-image", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  generateClip: (body: any) =>
    req<StudioJob>("/api/studio/generate-clip", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getStudioJob: (id: number) => req<StudioJob>(`/api/studio/jobs/${id}`),
  saveStudioJob: (id: number, asset_type: string = "lifestyle") =>
    req<Asset>(`/api/studio/jobs/${id}/save?asset_type=${asset_type}`, {
      method: "POST",
    }),
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
