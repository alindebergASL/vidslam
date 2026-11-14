"use client";
import { useEffect, useRef, useState } from "react";
import { Dialog } from "@/components/Dialog";
import { api, Avatar, errorText, FRAME_VIDEO_MAX_MB, FrameAt, Ingredient, StudioJob } from "@/lib/api";

/** A player's time to send for "Save this frame": to the microsecond,
 *  rounded down, so it never crosses into the next frame (the server saves
 *  the frame showing at that time: the last one that starts at or before it). */
export function frameSeconds(t: number): number {
  return Math.max(0, Math.floor(t * 1e6 + 1e-6) / 1e6);
}

/** "0:03.2" for 3.2 seconds. */
export function clockText(t: number): string {
  const s = Math.max(0, Math.round(t * 10) / 10);
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
}

/**
 * Save a frame of a video from this device as a Studio picture. The video
 * is only previewed here; the server grabs the frame, the picture safety
 * check looks at it (a vision call that costs a fraction of a cent), and
 * only the frame is kept.
 */
export function FrameFromVideoDialog({
  avatars,
  ingredients,
  defaultOwner,
  onClose,
  onSaved,
}: {
  avatars: Avatar[];
  ingredients: Ingredient[];
  /** "avatar:3" or "ingredient:5"; "" = the first character. */
  defaultOwner?: string;
  onClose: () => void;
  onSaved: (job: StudioJob) => void;
}) {
  const first = avatars[0] ? `avatar:${avatars[0].id}` : ingredients[0] ? `ingredient:${ingredients[0].id}` : "";
  const [owner, setOwner] = useState(defaultOwner || first);
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  // The browser can't play some files (e.g. some phone videos): the first
  // and last frame can still be saved, the server reads them.
  const [playable, setPlayable] = useState<boolean | null>(null);
  const [time, setTime] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (!owner && first) setOwner(first);
  }, [first, owner]);
  useEffect(() => {
    if (!file) {
      setUrl(null);
      return;
    }
    const u = URL.createObjectURL(file);
    setUrl(u);
    setPlayable(null);
    setTime(0);
    return () => URL.revokeObjectURL(u);
  }, [file]);

  const save = async (at: FrameAt) => {
    if (!file || !owner) return;
    if (file.size > FRAME_VIDEO_MAX_MB * 1024 * 1024) {
      setError(`This video is larger than ${FRAME_VIDEO_MAX_MB} MB. Pick a shorter or smaller one.`);
      return;
    }
    const [kind, id] = owner.split(":");
    setBusy(true);
    setError(null);
    try {
      const job = await api.frameFromVideo(file, { kind: kind as "avatar" | "ingredient", id: Number(id) }, at);
      onSaved(job);
    } catch (e) {
      setError(errorText(e, "Couldn't save the frame"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="Save a frame from a video" onClose={onClose}>
      <p className="text-xs text-ink-300">
        Pick a video from this device and the moment you want. Only that frame is saved, as a new picture; the video
        isn&apos;t kept. A safety check looks at the frame first (it costs a fraction of a cent), and frames that show
        anyone under 18 can&apos;t be saved.
      </p>
      <OwnerSelect id="frame-owner" label="Save it for" avatars={avatars} ingredients={ingredients} value={owner} onChange={setOwner} />
      <div>
        <label className="label" htmlFor="frame-file">
          Video (MP4, MOV, WebM, MKV or AVI, up to {FRAME_VIDEO_MAX_MB} MB)
        </label>
        <input
          id="frame-file"
          type="file"
          accept="video/mp4,video/quicktime,video/webm,video/*"
          className="block w-full text-sm text-ink-300 file:mr-3 file:chip file:text-xs"
          onChange={(e) => {
            const f = e.target.files?.[0] || null;
            setFile(f);
            setError(
              f && f.size > FRAME_VIDEO_MAX_MB * 1024 * 1024
                ? `This video is larger than ${FRAME_VIDEO_MAX_MB} MB. Pick a shorter or smaller one.`
                : null,
            );
          }}
        />
      </div>
      {url && (
        <div className="space-y-1">
          <video
            ref={videoRef}
            src={url}
            className="w-full max-h-[40dvh] rounded-md bg-black object-contain"
            controls
            playsInline
            muted
            preload="metadata"
            onLoadedData={() => setPlayable(true)}
            onError={() => setPlayable(false)}
            onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
            onSeeked={(e) => setTime(e.currentTarget.currentTime)}
          />
          {playable === false ? (
            <p className="text-[11px] text-ink-400">
              This browser can&apos;t preview that video. You can still save its first or last frame.
            </p>
          ) : (
            <p className="text-[11px] text-ink-400">Pause where you want, then save this frame.</p>
          )}
        </div>
      )}
      {error && (
        <p className="text-sm text-red-300" role="alert">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2 justify-end">
        <button type="button" className="btn-ghost text-sm" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="btn-ghost text-sm disabled:opacity-50"
          disabled={!file || !owner || busy}
          onClick={() => save("first")}
        >
          First frame
        </button>
        <button
          type="button"
          className="btn-ghost text-sm disabled:opacity-50"
          disabled={!file || !owner || busy}
          onClick={() => save("last")}
        >
          Last frame
        </button>
        <button
          type="button"
          className="btn-primary text-sm disabled:opacity-50"
          disabled={!file || !owner || busy || !playable}
          onClick={() => {
            videoRef.current?.pause();
            save(frameSeconds(videoRef.current ? videoRef.current.currentTime : time));
          }}
        >
          {busy ? "Saving…" : `Save this frame${playable ? ` (${clockText(time)})` : ""}`}
        </button>
      </div>
    </Dialog>
  );
}

/** The character or scene a picture or clip from this device goes to. */
function OwnerSelect({
  id,
  label,
  avatars,
  ingredients,
  value,
  onChange,
}: {
  id: string;
  label: string;
  avatars: Avatar[];
  ingredients: Ingredient[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {avatars.length > 0 && (
          <optgroup label="Characters">
            {avatars.map((a) => (
              <option key={`a${a.id}`} value={`avatar:${a.id}`}>
                {a.name}
              </option>
            ))}
          </optgroup>
        )}
        {ingredients.length > 0 && (
          <optgroup label="Scenes and objects">
            {ingredients.map((i) => (
              <option key={`i${i.id}`} value={`ingredient:${i.id}`}>
                {i.name}
              </option>
            ))}
          </optgroup>
        )}
      </select>
      {!value && <p className="text-[11px] text-ink-400 mt-1">Add a character or scene in Cast first.</p>}
    </div>
  );
}

const TOO_LARGE = `This video is larger than ${FRAME_VIDEO_MAX_MB} MB. Pick a shorter or smaller one.`;

/**
 * Upload a video from this device as a Studio clip, so it can be continued,
 * joined or have its frames saved like any clip. Free. A safety check looks
 * at frames across it first; it is refused if any shows someone under 18.
 */
export function UploadVideoDialog({
  avatars,
  ingredients,
  defaultOwner,
  onClose,
  onSaved,
}: {
  avatars: Avatar[];
  ingredients: Ingredient[];
  /** "avatar:3" or "ingredient:5"; "" = the first character. */
  defaultOwner?: string;
  onClose: () => void;
  onSaved: (job: StudioJob) => void;
}) {
  const first = avatars[0] ? `avatar:${avatars[0].id}` : ingredients[0] ? `ingredient:${ingredients[0].id}` : "";
  const [owner, setOwner] = useState(defaultOwner || first);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!owner && first) setOwner(first);
  }, [first, owner]);

  const upload = async () => {
    if (!file || !owner) return;
    if (file.size > FRAME_VIDEO_MAX_MB * 1024 * 1024) {
      setError(TOO_LARGE);
      return;
    }
    const [kind, id] = owner.split(":");
    setBusy(true);
    setError(null);
    try {
      onSaved(await api.uploadVideo(file, { kind: kind as "avatar" | "ingredient", id: Number(id) }));
    } catch (e) {
      setError(errorText(e, "Couldn't upload the video"));
      setBusy(false);
    }
  };

  return (
    <Dialog title="Upload a video" onClose={onClose}>
      <p className="text-xs text-ink-300">
        Adds a video from this device to Studio as a clip, so you can continue it, join it with other clips or save
        its frames. Free. A safety check looks at frames across the video first; videos that show anyone under 18
        can&apos;t be added.
      </p>
      <OwnerSelect id="upload-owner" label="Add it to" avatars={avatars} ingredients={ingredients} value={owner} onChange={setOwner} />
      <div>
        <label className="label" htmlFor="upload-file">
          Video (MP4, MOV, WebM, MKV or AVI, up to {FRAME_VIDEO_MAX_MB} MB)
        </label>
        <input
          id="upload-file"
          type="file"
          accept="video/mp4,video/quicktime,video/webm,video/*"
          className="block w-full text-sm text-ink-300 file:mr-3 file:chip file:text-xs"
          onChange={(e) => {
            const f = e.target.files?.[0] || null;
            setFile(f);
            setError(f && f.size > FRAME_VIDEO_MAX_MB * 1024 * 1024 ? TOO_LARGE : null);
          }}
        />
      </div>
      {busy && (
        <p className="text-[11px] text-ink-400" role="status">
          Uploading and checking… a long video can take a minute.
        </p>
      )}
      {error && (
        <p className="text-sm text-red-300" role="alert">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2 justify-end">
        <button type="button" className="btn-ghost text-sm" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="btn-primary text-sm disabled:opacity-50"
          disabled={!file || !owner || busy}
          onClick={upload}
        >
          {busy ? "Uploading…" : "Upload · free"}
        </button>
      </div>
    </Dialog>
  );
}
