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
    const text = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
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
  created_at: string;
  updated_at: string;
};

export type Project = {
  id: number;
  title: string;
  original_script: string;
  mode: "reel_montage" | "talking_head_beta" | "static_motion";
  aspect_ratio: string;
  target_duration_seconds: number;
  cta_text: string;
  caption_style: string;
  include_disclosure: boolean;
  status: string;
  primary_avatar_id: number | null;
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

export type StudioJob = {
  id: number;
  owner_kind: "avatar" | "ingredient";
  owner_id: number;
  output_kind: "image" | "video_clip";
  prompt: string;
  provider: string;
  provider_model: string;
  status: string;
  result_path: string;
  result_asset_id: number | null;
  error: string;
  created_at: string;
  updated_at: string;
};

export const api = {
  base: API_BASE,
  publicAsset: (token: string) => `${API_BASE}/api/public-assets/${token}`,
  studioPreview: (jobId: number) => `${API_BASE}/api/studio/jobs/${jobId}/preview`,
  renderDownload: (id: number) => `${API_BASE}/api/renders/${id}/download`,
  renderThumb: (id: number) => `${API_BASE}/api/renders/${id}/thumbnail`,

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
  captionStyles: () => req<string[]>("/api/providers/caption-styles"),

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

  // projects
  listProjects: () => req<Project[]>("/api/projects"),
  getProject: (id: number) => req<Project>(`/api/projects/${id}`),
  createProject: (body: any) =>
    req<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
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
  generatePlan: (id: number) =>
    req<any>(`/api/projects/${id}/generate-plan`, { method: "POST" }),
  preflight: (id: number) =>
    req<PreflightResult>(`/api/projects/${id}/preflight`),
  generateVideo: (id: number, force = false) =>
    req<any>(
      `/api/projects/${id}/generate-video${force ? "?force=true" : ""}`,
      { method: "POST" }
    ),
  recompose: (id: number) =>
    req<any>(`/api/projects/${id}/recompose`, { method: "POST" }),
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
};
