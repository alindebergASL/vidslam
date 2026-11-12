"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { api, ContentMode } from "@/lib/api";
import { useToast } from "@/components/Toaster";
import { SafetyMemory } from "@/components/SafetyMemory";

/** "deepseek/deepseek-v4-flash" → "deepseek-v4-flash". */
function shortModel(id: string): string {
  return id.replace(/^~/, "").split("/").pop() || id;
}

/**
 * Settings → Adult content: one switch. On, text work (storyboard planning,
 * ✨ Enhance, ambient scene rewrites) uses the adult text model and the
 * picture/video filters follow their switches in Settings → API keys; off,
 * the planner and the filters on. Also says where prompts are sent. Other
 * panels listen for "content-mode-changed".
 */
export function AdultModePanel() {
  const toast = useToast();
  const [mode, setMode] = useState<ContentMode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    setError(null);
    api
      .getContentMode()
      .then(setMode)
      .catch((e) => setError(e.message || "Couldn't load the adult content setting"));
  };
  useEffect(() => {
    load();
    // The planner changed in Models (or the switch elsewhere): re-read.
    window.addEventListener("content-mode-changed", load);
    return () => window.removeEventListener("content-mode-changed", load);
  }, []);

  const toggle = async () => {
    if (!mode || busy) return;
    const next = !mode.adult;
    if (
      next &&
      !window.confirm(
        `Turn on Adult mode? Storyboard planning, ✨ Enhance and scene rewrites move to ${shortModel(
          mode.text_model
        )}, and Runware's content check and Sogni's safe filter follow their switches in Settings → API keys. Each new picture, clip and lip-sync prompt is first sent to ${
          shortModel(mode.safety_check?.model || mode.planner) || "the planner"
        } for a safety check. Prompts that sexualise minors are always refused.`
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.setContentMode(next);
      setMode(r);
      toast.success(r.adult ? "Adult mode is on" : "Adult mode is off");
      window.dispatchEvent(new Event("content-mode-changed"));
    } catch (e: any) {
      toast.error(e.message || "Couldn't change Adult mode");
    } finally {
      setBusy(false);
    }
  };

  const on = Boolean(mode?.adult);
  return (
    <section className="card p-4 mb-6" aria-labelledby="adult-heading">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="adult-heading" className="text-lg font-medium">
            Adult content
          </h2>
          <p className="text-xs text-ink-400">One switch for adult (18+) prompts, pictures and clips.</p>
        </div>
        {mode && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              role="switch"
              aria-checked={on}
              aria-labelledby="adult-heading"
              aria-busy={busy}
              onClick={toggle}
              className={clsx(
                "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors",
                on ? "border-accent/70 bg-accent/40" : "border-ink-700 bg-ink-800"
              )}
              title={on ? "Turn Adult mode off" : "Turn Adult mode on"}
            >
              <span
                aria-hidden="true"
                className={clsx(
                  "inline-block h-4 w-4 rounded-full bg-white transition-transform",
                  on ? "translate-x-6" : "translate-x-1"
                )}
              />
            </button>
            <span className="text-xs text-ink-300 w-20">{busy ? "…" : on ? "Adult mode" : "Off"}</span>
          </div>
        )}
      </div>

      {error && (
        <p className="text-xs text-accent mt-2">
          {error}{" "}
          <button type="button" className="underline" onClick={load}>
            Retry
          </button>
        </p>
      )}

      {mode && (
        <ul className="mt-3 space-y-1 text-xs text-ink-300 list-disc pl-5">
          {on ? (
            <>
              <li>
                Storyboard planning, ✨ Enhance and scene rewrites use <b>{shortModel(mode.text_model)}</b>, which
                rewrites adult prompts instead of refusing them.
              </li>
              <li>
                Runware&rsquo;s content check and Sogni&rsquo;s safe filter follow their switches in{" "}
                <a href="/settings#keys" className="underline">
                  Settings → API keys
                </a>
                . Runware&rsquo;s vendor safety settings are loosened where a model offers them. Vendor models
                (Google, Kling, Wan…) stay moderated by their vendor.
              </li>
              <li>
                The pictures on the card of a Cast character ticked &ldquo;Adult content allowed&rdquo; that the
                safety check looked at follow these settings like any other picture, however they were made
                (uploaded, made in Studio, upscaled or cut out). A picture added later keeps the filter until it is
                checked.
              </li>
            </>
          ) : (
            <>
              <li>
                Text work uses the planner from Models (<b>{shortModel(mode.planner)}</b>), which may refuse or tone
                down adult prompts.
              </li>
              <li>Runware&rsquo;s content check and Sogni&rsquo;s safe filter are on.</li>
            </>
          )}
          <li>
            Always: prompts that sexualise minors are refused. Sogni&rsquo;s safe filter and Runware&rsquo;s
            content check are on for a prompt that mentions a child, and for pictures of Cast characters that
            aren&rsquo;t ticked &ldquo;Adult content allowed&rdquo; (also when upscaled, cut out or used as an
            ambient scene&rsquo;s picture).
          </li>
        </ul>
      )}

      {mode && <WhereTextGoes mode={mode} />}

      {mode?.safety_check && (
        <p className="mt-3 text-xs text-ink-300" data-testid="safety-check-line">
          {/* The sentence comes from the server (safety_check.note): what waits
              while the check can't run depends on the mode. */}
          {mode.safety_check.available ? (
            <>
              Safety check (<b>{shortModel(mode.safety_check.model)}</b>):{" "}
              {mode.safety_check.note ||
                "It checks prompts for minors before they're sent (about $0.0005 per new prompt)."}
            </>
          ) : (
            <span className="text-amber-200">
              Safety check (<b>{shortModel(mode.safety_check.model) || "the planner"}</b>):{" "}
              {mode.safety_check.note || "It can't run right now, so sexual prompts are paused until it can."} Check
              the planner in{" "}
              <a href="/settings#models" className="underline">
                Settings → Models
              </a>
              .
            </span>
          )}
        </p>
      )}
      <SafetyMemory />
    </section>
  );
}

/** Where prompts and scripts are sent, in this mode. Short and factual:
 * the safety check (the planner) and the text model run through OpenRouter. */
function WhereTextGoes({ mode }: { mode: ContentMode }) {
  const checker = shortModel(mode.safety_check?.model || mode.planner) || "the planner";
  return (
    <div className="mt-3" data-testid="where-text-goes">
      <h3 className="text-xs font-medium text-ink-200">Where your text goes</h3>
      <ul className="mt-1 space-y-1 text-xs text-ink-300 list-disc pl-5">
        {mode.adult ? (
          <>
            <li>
              Safety check: each new picture, clip and lip-sync prompt (with its avoid list) goes to{" "}
              <b>{checker}</b> (the planner from Settings → Models) through OpenRouter. So does any sexual voiceover
              script.
            </li>
            <li>
              Storyboard planning, ✨ Enhance and scene rewrites go to <b>{shortModel(mode.text_model)}</b> through
              OpenRouter. If it declines, Enhance asks a second adult text model.
            </li>
          </>
        ) : (
          <>
            <li>
              Storyboard planning, ✨ Enhance and scene rewrites go to the planner (<b>{shortModel(mode.planner)}</b>)
              through OpenRouter.
            </li>
            <li>
              Safety check: prompts that look risky (plainly sexual, or mentioning a child) and sexual voiceover
              scripts go to <b>{checker}</b> through OpenRouter.
            </li>
          </>
        )}
        <li>
          Ticking a Cast character &ldquo;Adult content allowed&rdquo; sends its name, description, persona and look,
          and a small copy of each picture on its card, to <b>{checker}</b> through OpenRouter. So does each later
          change to that text while it&rsquo;s ticked, and &ldquo;Check new pictures&rdquo;.
        </li>
        <li>Prompts, scripts and pictures go to the picture, video, lip-sync or voice provider you pick.</li>
        <li>
          The safety check&rsquo;s verdicts stay in this app&rsquo;s database: a fingerprint of the text or picture
          (not the text or picture itself), the verdict, its reason and which model gave it.
        </li>
      </ul>
    </div>
  );
}

/** The Adult mode setting, kept current when the switch (or a Cast mark) is
 * changed on the page. A failed refetch keeps the last good value. */
export function useContentModeState(): { mode: ContentMode | null; failed: boolean; reload: () => void } {
  const [mode, setMode] = useState<ContentMode | null>(null);
  const [failed, setFailed] = useState(false);
  const reload = () => {
    api
      .getContentMode()
      .then((m) => {
        setMode(m);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  };
  useEffect(() => {
    reload();
    window.addEventListener("content-mode-changed", reload);
    return () => window.removeEventListener("content-mode-changed", reload);
  }, []);
  return { mode, failed, reload };
}

/** The current Adult mode setting (null while loading or unavailable). */
export function useContentMode(): ContentMode | null {
  return useContentModeState().mode;
}
