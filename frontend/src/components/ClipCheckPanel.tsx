"use client";
import { useEffect, useState } from "react";
import { useToast } from "@/components/Toaster";
import { useAdultFeatures } from "@/components/AdultModePanel";
import { checkCostText } from "@/components/InspectionChip";
import { api, ClipCheckModel, ClipCheckSettings } from "@/lib/api";

/** Settings → Clip check: the automatic check after each storyboard clip, and its model. */
export function ClipCheckPanel() {
  const toast = useToast();
  const [s, setS] = useState<ClipCheckSettings | null>(null);
  // Adult content on this deployment. The server never reports adult_mode on
  // a clean deployment, so the note also shows while this is still unknown.
  const adult = useAdultFeatures();
  const [models, setModels] = useState<ClipCheckModel[] | null>(null);
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.clipCheckSettings().then(setS).catch((e) => setError(e.message));
    api
      .clipCheckModels()
      .then((r) => setModels(r.models))
      .catch(() => setModels([]));
  }, []);

  const save = async (body: Parameters<typeof api.setClipCheckSettings>[0], ok?: string) => {
    setBusy(true);
    try {
      setS(await api.setClipCheckSettings(body));
      if (ok) toast.success(ok);
      return true;
    } catch (e: any) {
      toast.error(e.message || "Couldn't save the clip check settings");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const cost = checkCostText(s?.cost_per_check);
  const listed = !!models?.some((m) => m.id === s?.model);

  return (
    <section className="card p-4 mb-6">
      <h2 className="text-lg font-medium">Clip check</h2>
      <p className="text-xs text-ink-400 mt-1">
        After a storyboard clip is made, a vision model looks at a few frames and says if anything looks off: a face
        that doesn&apos;t match the cast member, broken hands, stray text, or not what the shot asks for. It&apos;s
        advice only: the video is always put together as usual.
      </p>
      {error && <div className="text-sm text-accent mt-2">{error}</div>}
      {!s ? (
        !error && <div className="h-16 skeleton mt-3" aria-busy="true" />
      ) : (
        <div className="mt-3 space-y-4">
          {adult !== false && s.adult_mode && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-200">
              Clips aren&apos;t checked while adult mode is on: the vision models refuse adult frames (and still
              charge for it) or judge the content instead of the clip.
            </div>
          )}
          {!s.available && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-200">
              Clips aren&apos;t checked: add an OpenRouter key in Settings → API keys.
            </div>
          )}
          <label className="flex items-start gap-2 text-sm cursor-pointer">
            <input
              type="checkbox"
              className="accent-accent mt-1"
              checked={s.auto_check}
              disabled={busy}
              onChange={(e) =>
                save(
                  { auto_check: e.target.checked },
                  e.target.checked ? "Each new clip will be checked" : "Clips won't be checked automatically"
                )
              }
            />
            <span>
              Check each clip automatically
              <span className="text-ink-400"> · {cost || "cost unknown"} per clip</span>
              <span className="block text-xs text-ink-400">
                Skipped when the monthly budget is reached. With this off, each shot still has a &quot;Check this
                clip&quot; button.
              </span>
            </span>
          </label>

          <div>
            <label className="label" htmlFor="clip-check-model">
              Model
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <select
                id="clip-check-model"
                className="input max-w-md"
                value={listed ? s.model : ""}
                disabled={busy || !models || models.length === 0}
                onChange={(e) => e.target.value && save({ model: e.target.value }, "Clip check model changed")}
              >
                {!listed && <option value="">{s.model}</option>}
                {(models || []).map((m) => (
                  <option key={m.id} value={m.id} disabled={!m.usable}>
                    {m.name}
                    {m.id === s.default_model ? " (default)" : ""} · {checkCostText(m.cost_per_check) || "cost unknown"}
                    {!m.usable ? " · provider off" : ""}
                  </option>
                ))}
              </select>
              {!s.is_default && (
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  disabled={busy}
                  onClick={() => save({ reset_model: true }, "Back to the default model")}
                >
                  Use the default
                </button>
              )}
            </div>
            <div className="text-[11px] text-ink-400 mt-1 break-all">
              In use: {s.model}
              {s.is_default ? " (default)" : ` · default: ${s.default_model}`}
              {s.takes_images === false ? " · the catalog says it can't look at pictures" : ""}
              {s.takes_images === null && !s.model.startsWith("mock/") ? " · not in the catalog" : ""}
            </div>
            {models && models.length === 0 && (
              <div className="text-[11px] text-ink-400 mt-1">
                The model catalog lists no chat models that take pictures. Refresh the catalog under Models, or enter
                a model id below.
              </div>
            )}
            <form
              className="flex flex-wrap gap-2 mt-2"
              onSubmit={async (e) => {
                e.preventDefault();
                const id = custom.trim();
                if (!id) return;
                if (await save({ model: id, allow_unlisted: true }, "Clip check model changed")) setCustom("");
              }}
            >
              <input
                className="input max-w-xs text-xs"
                placeholder="Other model id, e.g. provider/model"
                value={custom}
                onChange={(e) => setCustom(e.target.value)}
                aria-label="Other clip check model id"
              />
              <button type="submit" className="btn-ghost text-xs" disabled={busy || !custom.trim()}>
                Use this model
              </button>
            </form>
          </div>
        </div>
      )}
    </section>
  );
}
