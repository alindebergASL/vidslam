// Saving Studio results and cast pictures with the right file extension.

const MIME_EXT: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/jpg": "jpg",
  "image/webp": "webp",
  "image/gif": "gif",
  "image/heic": "heic",
  "video/mp4": "mp4",
  "video/webm": "webm",
  "video/quicktime": "mov",
};

/** "jpg" for "image/jpeg"; "" when unknown. */
export function extFromMime(mime: string | null | undefined): string {
  return MIME_EXT[(mime || "").split(";")[0].trim().toLowerCase()] || "";
}

/** "webp" for ".../abc.webp"; "" when the path has no extension. */
export function extFromPath(path: string | null | undefined): string {
  const m = /\.([a-z0-9]{2,5})$/i.exec(path || "");
  if (!m) return "";
  const ext = m[1].toLowerCase();
  return ext === "jpeg" ? "jpg" : ext;
}

/**
 * Download `url` as `<baseName>.<ext>`. The file is fetched first, so the
 * name (and extension, from the file's real type when `ext` is empty) is
 * kept even when the API is on another origin, where the `download`
 * attribute is ignored. If the fetch fails the file opens in a new tab;
 * that tab is opened after an await, so a popup blocker may stop it: the
 * result is then false and the caller offers a link instead.
 */
export async function downloadFile(url: string, baseName: string, ext = ""): Promise<boolean> {
  try {
    const r = await fetch(url, { credentials: "include" });
    if (!r.ok) throw new Error(String(r.status));
    const blob = await r.blob();
    const suffix = ext || extFromMime(blob.type) || extFromMime(r.headers.get("Content-Type")) || "bin";
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = `${baseName}.${suffix}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10_000);
    return true;
  } catch {
    // Not "noopener" in the features: with it window.open always returns null.
    const w = window.open(url, "_blank");
    if (!w) return false;
    w.opener = null;
    return true;
  }
}
