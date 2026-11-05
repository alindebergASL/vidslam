// Human-readable names for the internal ids the API returns, so the UI never
// shows `reel_montage`, `b_roll` or `3600s`.

const MODES: Record<string, string> = {
  reel_montage: "Reel montage",
  talking_head: "Talking head",
  talking_head_beta: "Talking head",
  static_motion: "Static motion",
  ambient_loop: "Ambient scene",
};

const SHOT_TYPES: Record<string, string> = {
  hero: "Hero",
  b_roll: "B-roll",
  talking_head: "Talking head",
  product: "Product",
  end_card: "End card",
  ambient_segment: "Loop segment",
};

const STATUSES: Record<string, string> = {
  generating_audio: "voiceover",
  generating_shots: "generating clips",
  regenerating: "re-rolling shots",
  submitting: "submitting",
  polling: "generating",
  rendering: "rendering",
};

function humanize(id: string): string {
  const s = (id || "").replace(/_/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : "";
}

export const modeLabel = (mode: string) => MODES[mode] ?? humanize(mode);
export const shotTypeLabel = (t: string) => SHOT_TYPES[t] ?? humanize(t);
export const statusLabel = (s: string) => STATUSES[s] ?? (s || "").replace(/_/g, " ");

/** 25 → "25s", 600 → "10 min", 3600 → "1 h", 5400 → "1 h 30 min". */
export function formatDuration(seconds: number | null | undefined): string {
  const total = Math.round(Number(seconds) || 0);
  if (total < 60) return `${total}s`;
  const h = Math.floor(total / 3600);
  const m = Math.round((total % 3600) / 60);
  if (h === 0) return `${m} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}
