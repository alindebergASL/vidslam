"use client";
import { useRef, useState } from "react";
import clsx from "clsx";
import { api, Project } from "@/lib/api";

export function AudioPanel({
  project,
  onChange,
}: {
  project: Project;
  onChange: (next: Project) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const voiceInput = useRef<HTMLInputElement>(null);
  const musicInput = useRef<HTMLInputElement>(null);

  const setSource = async (src: "tts" | "upload" | "silent") => {
    setError(null);
    try {
      if (src === "upload" && !project.voiceover_upload_path) {
        // No file yet — open the picker.
        voiceInput.current?.click();
        return;
      }
      const next = await api.updateProject(project.id, { voiceover_source: src });
      onChange(next);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleVoice = async (file: File) => {
    setBusy("voice");
    setError(null);
    try {
      const next = await api.uploadVoiceover(project.id, file);
      onChange(next);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const handleMusic = async (file: File) => {
    setBusy("music");
    setError(null);
    try {
      const next = await api.uploadMusic(project.id, file);
      onChange(next);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const removeVoice = async () => {
    setBusy("voice");
    try {
      const next = await api.deleteVoiceover(project.id);
      onChange(next);
    } finally {
      setBusy(null);
    }
  };

  const removeMusic = async () => {
    setBusy("music");
    try {
      const next = await api.deleteMusic(project.id);
      onChange(next);
    } finally {
      setBusy(null);
    }
  };

  const setMusicVolume = async (v: number) => {
    onChange({ ...project, music_volume: v });
    await api.updateProject(project.id, { music_volume: v });
  };

  return (
    <section className="card p-4 mb-6">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-lg font-medium">Audio</h2>
        <span className="text-xs text-ink-400">
          Voiceover and an optional music bed mixed under it.
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <div className="label">Voiceover source</div>
          <div className="flex flex-wrap gap-2 mb-2">
            {(["tts", "upload", "silent"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setSource(s)}
                className={clsx("chip capitalize", project.voiceover_source === s && "chip-active")}
                disabled={busy === "voice"}
              >
                {s === "tts" ? "Generate (TTS)" : s === "upload" ? "Upload file" : "Silent"}
              </button>
            ))}
          </div>
          {project.voiceover_upload_path && (
            <div className="flex items-center gap-2">
              <audio
                controls
                src={api.voiceoverUrl(project.id)}
                className="flex-1 h-8 max-w-full"
              />
              <button
                className="btn-ghost text-xs"
                onClick={removeVoice}
                disabled={busy === "voice"}
              >
                Remove
              </button>
            </div>
          )}
          <input
            ref={voiceInput}
            type="file"
            hidden
            accept="audio/mpeg,audio/mp3,audio/mp4,audio/m4a,audio/x-m4a,audio/aac,audio/wav,audio/x-wav"
            onChange={(e) => e.target.files && handleVoice(e.target.files[0])}
          />
          {project.voiceover_source === "tts" && (
            <div className="text-xs text-ink-400 mt-1">
              Uses ElevenLabs if configured; otherwise mock silent audio.
            </div>
          )}
        </div>

        <div>
          <div className="label">Background music</div>
          <div className="flex flex-wrap gap-2 mb-2">
            {project.music_upload_path ? (
              <>
                <span className="chip chip-active">Music attached</span>
                <button
                  className="btn-ghost text-xs"
                  onClick={removeMusic}
                  disabled={busy === "music"}
                >
                  Remove
                </button>
                <button
                  className="btn-ghost text-xs"
                  onClick={() => musicInput.current?.click()}
                  disabled={busy === "music"}
                >
                  Replace
                </button>
              </>
            ) : (
              <button
                className="btn-ghost text-xs"
                onClick={() => musicInput.current?.click()}
                disabled={busy === "music"}
              >
                + Upload music track
              </button>
            )}
          </div>
          <input
            ref={musicInput}
            type="file"
            hidden
            accept="audio/mpeg,audio/mp3,audio/mp4,audio/m4a,audio/x-m4a,audio/aac,audio/wav,audio/x-wav"
            onChange={(e) => e.target.files && handleMusic(e.target.files[0])}
          />
          {project.music_upload_path && (
            <>
              <audio
                controls
                src={api.musicUrl(project.id)}
                className="w-full h-8 max-w-full mt-1"
              />
              <div className="mt-3">
                <label className="label">
                  Music volume under voice ({Math.round((project.music_volume || 0.25) * 100)}%)
                </label>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={project.music_volume || 0.25}
                  onChange={(e) => setMusicVolume(parseFloat(e.target.value))}
                  className="w-full"
                />
              </div>
            </>
          )}
          {!project.music_upload_path && (
            <div className="text-xs text-ink-400">
              Music auto-loops to cover the full video and ducks under the voiceover.
            </div>
          )}
        </div>
      </div>

      {error && <div className="text-xs text-accent mt-3">{error}</div>}
    </section>
  );
}
