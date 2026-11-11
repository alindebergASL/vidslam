"use client";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useToast } from "@/components/Toaster";
import { VoiceSampleButton } from "@/components/VoiceSampleButton";
import { api, Project, RenderPlan, Voice, VoiceoverPreview } from "@/lib/api";

const usd = (n: number) => `$${n.toFixed(2)}`;

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
  renderPlan,
  onRefresh,
}: {
  project: Project;
  onChange: (next: Project) => void;
  /** What Generate video would do now; prices "Make voiceover". */
  renderPlan?: RenderPlan | null;
  /** Reload the project and render plan (the voice changed, a voiceover was made). */
  onRefresh?: () => void;
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

  // ---- project voice + hearing the voiceover before paying for video ----
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceover, setVoiceover] = useState<VoiceoverPreview | null>(null);
  useEffect(() => {
    api.voices().then(setVoices).catch(() => setVoices([]));
  }, []);
  const plan = project.generated_plan_json;
  const spoken: string = plan?.cleaned_voice_script || "";
  const bodySeconds = project.shots
    .filter((s) => s.shot_type !== "end_card")
    .reduce((sum, s) => sum + (s.duration_seconds || 0), 0);
  // Fetch again whenever something the verdict depends on changes.
  const voiceoverKey = `${project.updated_at}|${project.voice_id ?? ""}|${spoken}|${bodySeconds}|${project.voiceover_source}`;
  useEffect(() => {
    if (!plan) {
      setVoiceover(null);
      return;
    }
    let live = true;
    api
      .voiceoverPreview(project.id)
      .then((v) => {
        if (live) setVoiceover(v);
      })
      .catch(() => {
        if (live) setVoiceover(null);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, voiceoverKey]);

  const tts = project.voiceover_source === "tts";
  const newVoiceoverCost =
    renderPlan && renderPlan.voiceover === "new" ? renderPlan.voiceover_cost : null;
  const voiceoverIsCurrent = !!voiceover?.current;
  const pickedVoice = voices.find((v) => v.voice_id === project.voice_id);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [characterVoiceId, setCharacterVoiceId] = useState<string>("");
  useEffect(() => {
    if (!project.primary_avatar_id) {
      setCharacterVoiceId("");
      return;
    }
    api
      .listAvatars()
      .then((avs) => setCharacterVoiceId(avs.find((a) => a.id === project.primary_avatar_id)?.elevenlabs_voice_id || ""))
      .catch(() => setCharacterVoiceId(""));
  }, [project.primary_avatar_id]);
  const characterVoice = voices.find((v) => !!characterVoiceId && v.voice_id === characterVoiceId);
  const currentVoiceName = project.voice_id
    ? pickedVoice?.name || `Custom voice (${project.voice_id.slice(0, 8)}…)`
    : `Character's voice (default)${characterVoice ? `: ${characterVoice.name}` : ""}`;
  // The voice can't change while clips are being made: they would be out of date before they land.
  const voiceLocked = ["draft", "planned", "completed", "failed", "cancelled"].includes(project.status)
    && !project.shots.some((x) => ["submitted", "polling", "running"].includes(x.status))
    ? null
    : "Wait until the video (or the clips being made) is finished, then change the voice.";

  const setVoice = async (voiceId: string) => {
    setBusy("voice-pick");
    try {
      const next = await api.updateProject(project.id, { voice_id: voiceId || null });
      onChange(next);
      onRefresh?.();
      setPickerOpen(false);
      toast.info(
        voiceover
          ? "Voice changed. The next video needs a new voiceover: make it below to hear it first"
          : "Voice changed"
      );
    } catch (e: any) {
      toast.error(e.message || "Couldn't change the voice");
    } finally {
      setBusy(null);
    }
  };

  const makeVoiceover = async () => {
    setBusy("voiceover");
    try {
      const v = await api.makeVoiceoverPreview(project.id);
      setVoiceover(v);
      toast.success(
        v.reused
          ? "This voiceover already existed, so nothing was paid. Generate video uses it"
          : `Voiceover made${v.cost_estimate > 0 ? ` (about ${usd(v.cost_estimate)})` : ""}. Generate video uses it at no extra charge`
      );
      onRefresh?.();
    } catch (e: any) {
      toast.error(e.message || "Couldn't make the voiceover");
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
      <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-1 mb-3">
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
          {tts && (
            <div className="text-xs text-ink-400 mt-1">
              Read aloud by the voice model chosen in Settings → Models.
            </div>
          )}
          {tts && (
            <div className="mt-3">
              <label className="label" htmlFor={`project-${project.id}-voice`}>
                Voice
              </label>
              <div className="flex items-center gap-2">
                <span id={`project-${project.id}-voice`} className="text-sm text-ink-100 flex-1 min-w-0 truncate">
                  {currentVoiceName}
                </span>
                <VoiceSampleButton
                  url={project.voice_id ? pickedVoice?.preview_url : characterVoice?.preview_url}
                  name={currentVoiceName}
                />
                <button
                  type="button"
                  className="btn-ghost text-xs disabled:opacity-50"
                  onClick={() => setPickerOpen(!pickerOpen)}
                  aria-expanded={pickerOpen}
                  disabled={!!voiceLocked}
                  title={voiceLocked || "Listen to the voices, then pick one"}
                >
                  {pickerOpen ? "Close" : "Change voice…"}
                </button>
              </div>
              {pickerOpen && (
                <ul
                  className="mt-2 max-h-64 overflow-y-auto rounded-md border border-ink-700 divide-y divide-ink-800"
                  aria-label="Voices"
                >
                  {[{ voice_id: "", name: "Character's voice (default)", preview_url: characterVoice?.preview_url }, ...voices].map(
                    (v) => {
                      const inUse = (project.voice_id || "") === v.voice_id;
                      return (
                        <li key={v.voice_id || "default"} className="flex items-center gap-2 px-2 py-1.5">
                          <VoiceSampleButton url={v.preview_url} name={v.name} />
                          <span className="text-sm flex-1 min-w-0 truncate">{v.name}</span>
                          {inUse ? (
                            <span className="chip text-[10px] py-0.5 chip-active">In use</span>
                          ) : (
                            <button
                              type="button"
                              className="btn-ghost text-xs disabled:opacity-50"
                              disabled={busy === "voice-pick" || !!voiceLocked}
                              onClick={() => setVoice(v.voice_id)}
                            >
                              Use this voice
                            </button>
                          )}
                        </li>
                      );
                    }
                  )}
                </ul>
              )}
              <div className="text-[11px] text-ink-400 mt-1">
                For this project only. ▶ plays the voice&apos;s free sample (nothing is paid for, nothing
                changes); &quot;Use this voice&quot; picks it. A different voice needs a new voiceover (paid) and
                new clips for lip-synced shots.
              </div>
            </div>
          )}
          {tts && plan && (
            <div className="mt-3 rounded-md border border-ink-700 p-3 space-y-2">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="text-sm font-medium text-ink-100">Hear the voiceover</div>
                {voiceover && (
                  <span className="text-[11px] text-ink-400">
                    {voiceover.source === "preview" ? "Made here" : "From the last video"} ·{" "}
                    {voiceover.duration_seconds.toFixed(1)} s
                  </span>
                )}
              </div>
              {voiceover ? (
                <>
                  <audio key={voiceover.url} controls src={api.mediaUrl(voiceover.url)} className="w-full h-8" />
                  {voiceoverIsCurrent ? (
                    <div className={clsx("text-xs", voiceover.fit === "fits" ? "text-emerald-300" : "text-amber-300")}>
                      {voiceover.fit === "fits"
                        ? `Fits the ${voiceover.video_seconds.toFixed(1)} s of shots.`
                        : `The voiceover is ${voiceover.fit_text}.`}
                      {voiceover.fit !== "fits" && (
                        <span className="text-ink-400"> Change shot lengths or the spoken script to fix it.</span>
                      )}
                    </div>
                  ) : (
                    <div className="text-xs text-amber-300">
                      Out of date: the spoken script or the voice changed after this voiceover was made.
                    </div>
                  )}
                </>
              ) : (
                <div className="text-xs text-ink-400">
                  Make the voiceover to hear it and check its length before any clip is paid for.
                </div>
              )}
              {voiceoverIsCurrent ? (
                <div className="text-[11px] text-ink-400">
                  Generate video uses this voiceover; it is not paid for again.
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    className="btn-primary text-xs disabled:opacity-50"
                    onClick={makeVoiceover}
                    disabled={busy === "voiceover" || !spoken.trim() || !!voiceLocked}
                    title="Reads the spoken script aloud with this project's voice. Paid text-to-speech, no video."
                  >
                    {busy === "voiceover"
                      ? "Making voiceover…"
                      : `${voiceover ? "Remake voiceover" : "Make voiceover"}${
                          newVoiceoverCost != null && newVoiceoverCost > 0 ? ` · about ${usd(newVoiceoverCost)}` : ""
                        }`}
                  </button>
                  <span className="text-[11px] text-ink-400">Generate video then uses it at no extra charge.</span>
                </div>
              )}
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
