"use client";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { IMAGE_ACCEPT } from "@/components/Dropzone";
import { askBeforeDiscard } from "@/components/ambient/shared";
import { api, AmbientScene, errorText } from "@/lib/api";

export type PickedImage = { kind: "studio" | "asset"; id: number; url: string; label: string };

const ORIGIN_LABEL: Record<string, string> = {
  upload: "Your upload",
  generated: "Generated preview",
  studio: "From Studio",
  asset: "From your Cast",
};

/** Studio image results + Cast images, for the "Choose" dialog. */
async function loadChoices(): Promise<PickedImage[]> {
  const [jobs, avatars, ingredients] = await Promise.all([
    api.listStudioJobs({ output_kind: "image", status: "completed", limit: 200 }).catch(() => []),
    api.listAvatars().catch(() => []),
    api.listIngredients().catch(() => []),
  ]);
  const studio: PickedImage[] = jobs
    .filter((j) => j.output_kind === "image" && ["completed", "saved"].includes(j.status))
    .map((j) => ({ kind: "studio", id: j.id, url: api.studioPreview(j.id), label: j.prompt || "Studio image" }));
  const cast: PickedImage[] = [...ingredients, ...avatars].flatMap((o) =>
    (o.assets ?? [])
      .filter((a) => (a.mime_type || "").startsWith("image/"))
      .map((a) => ({ kind: "asset" as const, id: a.id, url: api.publicAsset(a.public_token), label: `${o.name} · ${a.asset_type}` }))
  );
  return [...studio, ...cast];
}

/** Modal grid of Studio + Cast images. */
export function PicturePicker({ onPick, onClose }: { onPick: (p: PickedImage) => void; onClose: () => void }) {
  const [items, setItems] = useState<PickedImage[] | null>(null);
  useEffect(() => {
    loadChoices().then(setItems);
  }, []);
  return (
    <div className="fixed inset-0 z-30 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="picker-h"
        className="card w-full max-w-3xl p-5 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2 id="picker-h" className="text-lg font-medium">Choose a picture</h2>
          <button className="text-ink-300 hover:text-white" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        {items === null ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : items.length === 0 ? (
          <div className="text-sm text-ink-400">
            No images yet. Generate one in Studio, or add images to your Cast, then come back.
          </div>
        ) : (
          <ul className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
            {items.map((it) => (
              <li key={`${it.kind}-${it.id}`}>
                <button
                  type="button"
                  onClick={() => onPick(it)}
                  className="group block w-full text-left"
                  title={it.label}
                >
                  <div className="aspect-video rounded-md overflow-hidden bg-ink-800 border border-ink-700 group-hover:border-iris/60">
                    <img src={it.url} alt="" className="w-full h-full object-cover" loading="lazy" />
                  </div>
                  <div className="text-[11px] text-ink-300 mt-1 truncate">
                    <span className="text-ink-400">{it.kind === "studio" ? "Studio" : "Cast"} · </span>
                    {it.label}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** What happens to the picture, for the scene's source. */
export function pictureUse(source: string): string {
  return source === "image_drift"
    ? "Your picture is the still, with a slow drift. No image is generated."
    : "Your picture becomes the loop's first and last frame, so the video is built from it.";
}

/** Scene page card: show, upload, choose, generate or remove the scene picture. */
export function ScenePicture({
  scene,
  onChange,
  disabled,
}: {
  scene: AmbientScene;
  onChange: (s: AmbientScene) => void;
  disabled?: boolean;
}) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const origin = scene.config.picture_origin;
  const has = !!scene.picture_ready;

  // Every picture change needs a new loop: when one is already made, ask
  // first and say about what it cost (the server refuses without the flag).
  const run = async (key: string, action: string, fn: (discardLoop: boolean) => Promise<AmbientScene>, ok: string) => {
    setBusy(key);
    try {
      const next = await askBeforeDiscard(scene, true, action, fn);
      if (!next) return;
      onChange(next);
      toast.success(ok);
    } catch (e: any) {
      toast.error(errorText(e, "Picture update failed"));
    } finally {
      setBusy(null);
    }
  };
  const lock = disabled || busy !== null;

  return (
    <section className="card p-4" aria-labelledby="picture-h">
      <div className="flex flex-col sm:flex-row gap-4">
        <div
          className={clsx(
            "shrink-0 rounded-md overflow-hidden bg-ink-800 border border-ink-700 flex items-center justify-center",
            scene.aspect_ratio === "9:16" ? "w-28 aspect-9/16" : scene.aspect_ratio === "1:1" ? "w-36 aspect-square" : "w-full sm:w-56 aspect-video"
          )}
        >
          {has ? (
            <img
              src={api.ambientPictureUrl(scene.id, scene.picture_version || scene.updated_at)}
              alt="Scene picture"
              className="w-full h-full object-cover"
            />
          ) : (
            <span className="text-[11px] text-ink-400 px-3 text-center">Generated from the prompt</span>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <h2 id="picture-h" className="text-base font-medium">Picture</h2>
          <p className="text-xs text-ink-400 mt-0.5">
            {has
              ? `${ORIGIN_LABEL[origin] ?? "Your picture"}. ${pictureUse(scene.config.source)}`
              : "Made from the scene prompt when you generate the loop. Or start from a picture of your own."}
          </p>
          {has && scene.loop_status === "pending" && (
            <p className="text-[11px] text-amber-300 mt-1">
              No video has been made from this picture yet. If it looks right, make the loop below.
            </p>
          )}
          <div className="flex flex-wrap gap-2 mt-3">
            <button type="button" className="btn-ghost text-xs" disabled={lock} onClick={() => fileRef.current?.click()}>
              {busy === "upload" ? "Uploading…" : has ? "Upload another" : "Upload"}
            </button>
            <button type="button" className="btn-ghost text-xs" disabled={lock} onClick={() => setPicking(true)}>
              Choose from Studio / Cast
            </button>
            <button
              type="button"
              className="btn-ghost text-xs"
              disabled={lock}
              title="One image call: preview the scene before paying for any video"
              onClick={() =>
                run("gen", "Generating a new picture", (d) => api.generateAmbientPicture(scene.id, d), "Picture generated")
              }
            >
              {busy === "gen" ? "Generating…" : origin === "generated" ? "Regenerate preview" : "Generate preview"}
            </button>
            {has && (
              <button
                type="button"
                className="text-xs text-ink-400 hover:text-ink-100 px-2"
                disabled={lock}
                onClick={() =>
                  run("clear", "Removing the picture", (d) => api.clearAmbientPicture(scene.id, d), "Picture removed")
                }
              >
                Remove
              </button>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept={IMAGE_ACCEPT}
            className="hidden"
            aria-label="Upload a scene picture"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f)
                run("upload", "Uploading a new picture", (d) => api.uploadAmbientPicture(scene.id, f, d), "Picture uploaded");
            }}
          />
        </div>
      </div>
      {picking && (
        <PicturePicker
          onClose={() => setPicking(false)}
          onPick={(p) => {
            setPicking(false);
            run(
              "pick",
              "Choosing another picture",
              (d) =>
                api.ambientPictureFrom(scene.id, p.kind === "studio" ? { studio_job_id: p.id } : { asset_id: p.id }, d),
              "Picture set"
            );
          }}
        />
      )}
    </section>
  );
}
