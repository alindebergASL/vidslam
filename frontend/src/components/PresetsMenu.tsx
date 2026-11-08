"use client";
import { useEffect, useId, useRef, useState } from "react";
import { api, PromptPreset, PromptPresetIn } from "@/lib/api";
import { useToast } from "@/components/Toaster";

/**
 * Saved prompt presets: apply one (the caller fills its prompt bar), save the
 * current settings as a new preset, or delete one. A small disclosure that
 * opens upward (it lives in the bottom prompt bar); Escape closes it.
 */
export function PresetsMenu({
  current,
  onApply,
}: {
  /** What "Save current as preset…" stores (minus the name). */
  current: () => Omit<PromptPresetIn, "name">;
  onApply: (p: PromptPreset) => void;
}) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [presets, setPresets] = useState<PromptPreset[] | null>(null);
  const [busy, setBusy] = useState(false);
  const panelId = useId();
  const btnRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const load = async () => {
    try {
      setPresets(await api.listPresets());
    } catch (e: any) {
      setPresets([]);
      toast.error(e.message || "Couldn't load presets");
    }
  };

  useEffect(() => {
    if (open && presets === null) load();
  }, [open]);

  // Close on a click outside.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const close = () => {
    setOpen(false);
    btnRef.current?.focus();
  };

  const saveCurrent = async () => {
    const cur = current();
    if (!cur.prompt.trim()) {
      toast.error("Write a prompt first, then save it as a preset.");
      return;
    }
    const name = window.prompt("Name this preset", cur.prompt.slice(0, 40))?.trim();
    if (!name) return;
    setBusy(true);
    try {
      const p = await api.createPreset({ ...cur, name: name.slice(0, 80) });
      setPresets((list) => [p, ...(list || [])]);
      toast.success(`Saved preset "${p.name}"`);
    } catch (e: any) {
      toast.error(e.message || "Couldn't save the preset");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (p: PromptPreset) => {
    if (!window.confirm(`Delete the preset "${p.name}"?`)) return;
    try {
      await api.deletePreset(p.id);
      setPresets((list) => (list || []).filter((x) => x.id !== p.id));
    } catch (e: any) {
      toast.error(e.message || "Couldn't delete the preset");
    }
  };

  return (
    <div
      ref={wrapRef}
      className="relative"
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.stopPropagation();
          close();
        }
      }}
    >
      <button
        ref={btnRef}
        type="button"
        className="chip text-xs"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        title="Saved prompts: apply one, or save what you have now"
      >
        Presets ▾
      </button>
      {open && (
        <div
          id={panelId}
          className="absolute bottom-full mb-2 left-0 z-20 w-72 max-w-[85vw] card p-2 shadow-xl text-xs"
        >
          <button
            type="button"
            onClick={saveCurrent}
            disabled={busy}
            className="w-full text-left px-2 py-1.5 rounded-sm hover:bg-ink-800 focus:bg-ink-800 disabled:opacity-50"
          >
            {busy ? "Saving…" : "Save current as preset…"}
          </button>
          <div className="border-t border-ink-800 my-1" />
          {presets === null ? (
            <div className="px-2 py-1.5 text-ink-400">Loading…</div>
          ) : presets.length === 0 ? (
            <div className="px-2 py-1.5 text-ink-400">No presets yet.</div>
          ) : (
            <ul className="max-h-64 overflow-y-auto" aria-label="Saved presets">
              {presets.map((p) => (
                <li key={p.id} className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => {
                      onApply(p);
                      close();
                    }}
                    className="flex-1 min-w-0 text-left px-2 py-1.5 rounded-sm hover:bg-ink-800 focus:bg-ink-800"
                    title={p.prompt}
                  >
                    <span className="block truncate text-ink-100">{p.name}</span>
                    <span className="block truncate text-[10px] text-ink-400">
                      {p.kind === "image" ? "Image" : "Video"}
                      {p.aspect_ratio ? ` · ${p.aspect_ratio}` : ""}
                      {p.model ? ` · ${p.model}` : ""}
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => remove(p)}
                    className="px-2 py-1 text-ink-400 hover:text-accent"
                    aria-label={`Delete preset ${p.name}`}
                    title="Delete preset"
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
