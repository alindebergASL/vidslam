"use client";
import { useRef, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { api, Project } from "@/lib/api";

const MUSIC_PRESETS: { label: string; prompt: string }[] = [
  { label: "Lo-fi", prompt: "warm lo-fi hip hop, mellow Rhodes piano, soft drums, 70 bpm" },
  { label: "Cinematic", prompt: "cinematic orchestral pad, slow swelling strings, hopeful, 80 bpm" },
  { label: "Upbeat", prompt: "upbeat indie pop with claps and bright synths, 120 bpm" },
  { label: "Ambient", prompt: "soft ambient pad, no drums, dreamy, slow evolving texture" },
  { label: "Tense", prompt: "tense electronic underscore with subtle pulse, 90 bpm" },
  { label: "Acoustic", prompt: "warm acoustic guitar fingerpicking with light percussion, 85 bpm" },
];

export function AudioPanel({
  project,
  onChange,
}: {
  project: Project;
  onChange: (next: Project) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const toast = useToast();
  const [musicTab, setMusicTab] = useState<"generate" | "upload">("generate");
  const [musicPrompt, setMusicPrompt] = useState("");
  const voiceInput = useRef<HTMLInputElement>(null);
  const musicInput = useRef<HTMLInputElement>(null);

  const generateMusic = async () => {
    if (!musicPrompt.trim()) return;
    setBusy("music");
    try {
      const next = await api.generateMusic(project.id, musicPrompt.trim());
      onChange(next);
    } catch (e: any) {
      toast.error(e.message || "Action failed");
    } finally {
      setBusy(null);
    }
  };

  const setSource = async (src: "tts" | "upload" | "silent") => {
    try {
      if (src === "upload" && !project.voiceover_upload_path) {
        // No file yet — open the picker.
        voiceInput.current?.click();
        return;
      }
      const next = await api.updateProject(project.id, { voiceover_source: src });
      onChange(next);
    } catch (e: any) {
      toast.error(e.message || "Action failed");
    }
  };

  const handleVoice = async (file: File) => {
    setBusy("voice");
    try {
      const next = await api.uploadVoiceover(project.id, file);
      onChange(next);
    } catch (e: any) {
      toast.error(e.message || "Action failed");
    } finally {
      setBusy(null);
    }
  };

  const handleMusic = async (file: File) => {
    setBusy("music");
    try {
      const next = await api.uploadMusic(project.id, file);
      onChange(next);
    } catch (e: any) {
      toast.error(e.message || "Action failed");
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

          <div className="flex gap-2 mb-3">
            <button
              onClick={() => setMusicTab("generate")}
              className={clsx("chip", musicTab === "generate" && "chip-active")}
              disabled={busy === "music"}
            >
              Generate from prompt
            </button>
            <button
              onClick={() => setMusicTab("upload")}
              className={clsx("chip", musicTab === "upload" && "chip-active")}
              disabled={busy === "music"}
            >
              Upload file
            </button>
          </div>

          {musicTab === "generate" ? (
            <div className="space-y-2">
              <div className="flex flex-wrap gap-1.5">
                {MUSIC_PRESETS.map((p) => (
                  <button
                    key={p.label}
                    onClick={() => setMusicPrompt(p.prompt)}
                    className="chip text-[11px] py-1"
                    title={p.prompt}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              <textarea
                className="input min-h-[64px] text-sm"
                placeholder='e.g. "warm cinematic lo-fi with mellow piano, 80 bpm"'
                value={musicPrompt}
                onChange={(e) => setMusicPrompt(e.target.value)}
              />
              <div className="flex items-center gap-2">
                <button
                  className="btn-primary text-xs disabled:opacity-50"
                  onClick={generateMusic}
                  disabled={busy === "music" || !musicPrompt.trim()}
                >
                  {busy === "music" ? "Generating…" : "Generate music →"}
                </button>
                <span className="text-[10px] text-ink-400">
                  ~{project.target_duration_seconds}s, matches video length
                </span>
              </div>
            </div>
          ) : (
            <div className="flex flex-wrap gap-2">
              {project.music_upload_path ? (
                <>
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
          )}

          <input
            ref={musicInput}
            type="file"
            hidden
            accept="audio/mpeg,audio/mp3,audio/mp4,audio/m4a,audio/x-m4a,audio/aac,audio/wav,audio/x-wav"
            onChange={(e) => e.target.files && handleMusic(e.target.files[0])}
          />

          {project.music_upload_path ? (
            <div className="mt-3">
              <div className="flex items-center gap-2">
                <audio
                  controls
                  src={api.musicUrl(project.id)}
                  className="flex-1 h-8 max-w-full"
                />
                <button
                  className="btn-ghost text-xs"
                  onClick={removeMusic}
                  disabled={busy === "music"}
                >
                  Remove
                </button>
              </div>
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
            </div>
          ) : (
            <div className="text-xs text-ink-400 mt-2">
              Music auto-loops to cover the full video and ducks under the voiceover.
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
