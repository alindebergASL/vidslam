"use client";
import { useEffect, useState } from "react";
import { api, PromptDefaults } from "@/lib/api";
import { useToast } from "@/components/Toaster";

const MAX = 1000;

/** Settings → Prompting: the default "avoid" lists added to every generation. */
export function PromptingPanel() {
  const toast = useToast();
  const [saved, setSaved] = useState<PromptDefaults | null>(null);
  const [draft, setDraft] = useState<PromptDefaults>({ image: "", video: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getPromptDefaults()
      .then((d) => {
        setSaved(d);
        setDraft(d);
      })
      .catch((e) => setError(e.message || "Couldn't load the prompt defaults"));
  }, []);

  const dirty = saved !== null && (draft.image !== saved.image || draft.video !== saved.video);

  const save = async (body: PromptDefaults | "reset") => {
    setBusy(true);
    try {
      // Reset clears the saved lists on the server, so the built-ins apply
      // (and keep following the backend's built-in lists).
      const d = body === "reset" ? await api.resetPromptDefaults() : await api.setPromptDefaults(body);
      setSaved(d);
      setDraft(d);
      toast.success(body === "reset" ? "Prompt defaults reset to built-in" : "Prompt defaults saved");
    } catch (e: any) {
      toast.error(e.message || "Couldn't save the prompt defaults");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card p-4 mb-6" aria-labelledby="prompting-heading">
      <h2 id="prompting-heading" className="text-lg font-medium mb-1">
        Prompting
      </h2>
      <p className="text-xs text-ink-400 mb-3">
        Things to avoid, added after your own &ldquo;Avoid&rdquo; text on every Studio generation and
        storyboard shot. Ambient scenes use their own list.
      </p>
      {error && <div className="text-xs text-accent mb-2">{error}</div>}
      <div className="grid gap-3 md:grid-cols-2">
        {(["image", "video"] as const).map((k) => (
          <div key={k}>
            <label className="label" htmlFor={`neg-default-${k}`}>
              {k === "image" ? "Images: avoid" : "Video clips: avoid"}
            </label>
            <textarea
              id={`neg-default-${k}`}
              className="input min-h-[110px] text-xs"
              maxLength={MAX}
              value={draft[k]}
              disabled={saved === null}
              onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
            />
            <div className="text-[10px] text-ink-500 text-right">
              {draft[k].length}/{MAX}
            </div>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-2 mt-2">
        <button
          type="button"
          className="btn-primary text-xs"
          disabled={busy || !dirty}
          onClick={() => save(draft)}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="btn-ghost text-xs"
          disabled={busy || saved === null}
          onClick={() => {
            if (window.confirm("Replace both lists with the built-in defaults?")) save("reset");
          }}
        >
          Reset to built-in
        </button>
      </div>
    </section>
  );
}
