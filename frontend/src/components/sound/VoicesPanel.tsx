"use client";
import { useEffect, useState } from "react";
import { useToast } from "@/components/Toaster";
import { api, errorText, type CastVoice, type SoundConfig, type SoundStory } from "@/lib/api";

const ELEVEN_LABEL: Record<string, string> = {
  eleven_v4: "ElevenLabs v4 (default)",
  eleven_v3: "ElevenLabs v3",
  eleven_v4_turbo: "ElevenLabs v4 Turbo (cheaper)",
};

/** A voice route as one select value: "elevenlabs|eleven_v4", "openrouter|openai/gpt-4o-mini-tts", "" (inherit). */
const routeOf = (provider?: string, model?: string) => (provider || model ? `${provider || "elevenlabs"}|${model || ""}` : "");

function RouteSelect({
  value,
  onChange,
  config,
  inheritLabel,
  disabled,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  config: SoundConfig | null;
  inheritLabel: string;
  disabled: boolean;
  label: string;
}) {
  const v = config?.voices;
  return (
    <select className="input text-xs" aria-label={label} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      <option value="">{inheritLabel}</option>
      {(v?.elevenlabs_models ?? ["eleven_v4", "eleven_v3", "eleven_v4_turbo"]).map((m) => (
        <option key={m} value={`elevenlabs|${m}`}>
          {ELEVEN_LABEL[m] ?? m}
        </option>
      ))}
      {(v?.openrouter_models ?? []).map((m) => (
        <option key={m.id} value={`openrouter|${m.id}`}>
          OpenRouter: {m.name}
        </option>
      ))}
    </select>
  );
}

/**
 * The story's voice model and each character's own voice (provider and
 * model, voice id). Tags are read in place by ElevenLabs v3/v4 and become
 * spoken directions for OpenRouter voice models. A change makes the
 * affected clips new (they are priced again before Generate).
 */
export function VoicesPanel({
  story,
  config,
  disabled,
  onSaved,
}: {
  story: SoundStory;
  config: SoundConfig | null;
  disabled: boolean;
  onSaved: (s: SoundStory) => void;
}) {
  const toast = useToast();
  const [route, setRoute] = useState(routeOf(story.settings.voice_provider, story.settings.voice_model));
  const [cast, setCast] = useState<Record<string, CastVoice>>(story.cast_voices ?? {});
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setRoute(routeOf(story.settings.voice_provider, story.settings.voice_model));
    setCast(story.cast_voices ?? {});
  }, [story.id, story.updated_at]);

  const names = story.parsed.cast.map((c) => c.name);
  const setMember = (name: string, patch: Partial<CastVoice>) =>
    setCast((c) => ({ ...c, [name]: { ...(c[name] ?? {}), ...patch } }));

  const save = async () => {
    const [provider, model] = route ? route.split("|") : ["", ""];
    setSaving(true);
    try {
      const s = await api.updateStory(story.id, {
        settings: { ...story.settings, voice_provider: provider, voice_model: model },
        cast,
      });
      onSaved(s);
      toast.success("Voices saved");
    } catch (e) {
      toast.error(errorText(e, "Couldn't save the voices"));
    } finally {
      setSaving(false);
    }
  };

  const d = config?.voices?.default;
  return (
    <section className="card p-3 flex flex-col gap-2" aria-labelledby="voices-h">
      <h2 id="voices-h" className="text-sm font-semibold">
        Voices
      </h2>
      <label className="text-xs flex flex-col gap-1">
        <span className="text-ink-400">Story voice model</span>
        <RouteSelect
          value={route}
          onChange={setRoute}
          config={config}
          disabled={disabled}
          label="Story voice model"
          inheritLabel={`Default${d ? ` (${ELEVEN_LABEL[d.model] ?? d.model})` : ""}`}
        />
      </label>
      {names.map((name) => {
        const m = cast[name] ?? {};
        return (
          <div key={name} className="text-xs flex flex-col gap-1 border-t border-ink-800 pt-2">
            <span className="font-semibold text-amber-200">{name}</span>
            <RouteSelect
              value={routeOf(m.provider, m.model)}
              onChange={(v) => {
                const [provider, model] = v ? v.split("|") : ["", ""];
                setMember(name, { provider, model });
              }}
              config={config}
              disabled={disabled}
              label={`${name}'s voice model`}
              inheritLabel="As the story"
            />
            <input
              className="input text-xs"
              placeholder="Voice id (empty: the default voice)"
              aria-label={`${name}'s voice id`}
              value={m.voice_id ?? ""}
              disabled={disabled}
              maxLength={120}
              onChange={(e) => setMember(name, { voice_id: e.target.value.trim() })}
            />
          </div>
        );
      })}
      <p className="text-xs text-ink-500">
        ElevenLabs v3 and v4 read tags like [whispers] in place; OpenRouter voices get them as spoken directions.
        Changing a voice makes its lines new clips.
      </p>
      <button type="button" className="btn-ghost" disabled={disabled || saving} onClick={save}>
        {saving ? "Saving…" : "Save voices"}
      </button>
    </section>
  );
}
