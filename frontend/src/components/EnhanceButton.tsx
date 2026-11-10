"use client";
import { useState } from "react";
import clsx from "clsx";
import { api, EnhanceKind } from "@/lib/api";
import { useToast } from "@/components/Toaster";

/** Turns an API error into one plain sentence for a toast. */
export function enhanceErrorMessage(e: unknown): string {
  const msg = (e as { message?: string })?.message || "";
  if (msg.startsWith("402")) return "Monthly budget reached. Raise it in Settings → Spend to enhance prompts.";
  if (msg.startsWith("429")) return "Too many requests right now. Try again in a minute.";
  // The model declined, or the prompt or rewrite was refused: the server says why.
  if (/^422: (?:The (?:planner|text model) \(|This (?:prompt|script) was refused|The rewrite was refused)/.test(msg))
    return msg.replace(/^422:\s*/, "");
  if (msg.startsWith("422") && /string_too_long|at most 4000/i.test(msg)) return "Too long to enhance (4000 characters at most).";
  if (msg.startsWith("422")) return "Write something first, then enhance it.";
  return msg || "Enhance failed";
}

const KIND_LABEL: Record<EnhanceKind, string> = {
  image: "image prompt",
  video: "video prompt",
  script: "script",
  scene: "scene description",
};

/** "deepseek/deepseek-v4-flash" → "deepseek-v4-flash" (the suggestion's byline). */
function shortModel(id: string): string {
  return id.replace(/^~/, "").split("/").pop() || id;
}

/**
 * "✨ Enhance": asks the text model (the planner; DeepSeek in Adult mode) to improve `text` for its `kind`, shows
 * the suggestion inline with Use it / Keep mine, and after Use it offers Undo
 * (restores the text from before) for as long as the text is unchanged.
 *
 * The wrapper is a block by default; pass `className="contents"` inside a
 * flex-wrap row so the suggestion panel (basis-full) takes a row of its own.
 */
export function EnhanceButton({
  text,
  kind,
  model,
  onUse,
  disabled,
  className,
  buttonClassName,
}: {
  text: string;
  kind: EnhanceKind;
  model?: string | null;
  onUse: (newText: string) => void;
  disabled?: boolean;
  className?: string;
  buttonClassName?: string;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [suggestion, setSuggestion] = useState<string | null>(null);
  const [byModel, setByModel] = useState("");
  // Set after "Use it": what to restore, and the text it was replaced with.
  const [undo, setUndo] = useState<{ before: string; after: string } | null>(null);

  const empty = !text.trim();
  const canUndo = undo !== null && text === undo.after;

  const run = async () => {
    if (empty || busy) return;
    setBusy(true);
    try {
      const r = await api.enhancePrompt({ text, kind, model: model || null });
      setSuggestion(r.text);
      setByModel(r.model || "");
    } catch (e) {
      toast.error(enhanceErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const use = () => {
    if (suggestion === null) return;
    setUndo({ before: text, after: suggestion });
    onUse(suggestion);
    setSuggestion(null);
  };

  return (
    <div className={className}>
      <span className="inline-flex items-center gap-1">
        <button
          type="button"
          onClick={run}
          disabled={disabled || empty || busy}
          className={clsx("chip text-xs disabled:opacity-50", buttonClassName)}
          title={`Rewrite this ${KIND_LABEL[kind]} with the text model (the planner, or DeepSeek in Adult mode). You can keep yours.`}
          aria-label={`Enhance ${KIND_LABEL[kind]}`}
        >
          {busy ? "Enhancing…" : "✨ Enhance"}
        </button>
        {canUndo && (
          <button
            type="button"
            onClick={() => {
              onUse(undo!.before);
              setUndo(null);
            }}
            className="chip text-xs"
            title="Put back the text you had before enhancing"
          >
            Undo
          </button>
        )}
      </span>
      <div
        aria-live="polite"
        // Kept mounted (empty) so screen readers announce the suggestion; it
        // only takes space, as a row of its own, while there is one.
        className={suggestion !== null ? "basis-full w-full order-last" : "sr-only"}
      >
        {suggestion !== null && (
          <div className="mt-2 rounded-md border border-ink-700 bg-ink-900 p-2 text-xs">
            <div className="text-ink-400 mb-1">
              Suggested {KIND_LABEL[kind]}
              {byModel && <span className="text-ink-500"> · {shortModel(byModel)}</span>}:
            </div>
            <p className="whitespace-pre-wrap text-ink-100">{suggestion}</p>
            <div className="mt-2 flex gap-2">
              <button type="button" onClick={use} className="btn-primary text-xs py-1">
                Use it
              </button>
              <button type="button" onClick={() => setSuggestion(null)} className="btn-ghost text-xs py-1">
                Keep mine
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
