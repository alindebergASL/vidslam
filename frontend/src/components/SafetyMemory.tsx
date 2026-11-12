"use client";
import { useEffect, useState } from "react";
import { api, errorText, SafetyMemory as Memory } from "@/lib/api";
import { useToast } from "@/components/Toaster";

/**
 * Settings → Adult content: how many refusals the AI safety check
 * remembers, and a way to forget them. A refusal is remembered until it's
 * cleared, so a prompt (or a Cast character) it got wrong stays refused.
 * Clearing makes the check look again; prompts the word filter refuses stay
 * refused.
 */
export function SafetyMemory() {
  const toast = useToast();
  const [memory, setMemory] = useState<Memory | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .getSafetyMemory()
      .then(setMemory)
      .catch(() => setMemory(null));
  }, []);

  if (!memory) return null;
  const n = memory.refusals;

  const clear = async () => {
    if (
      busy ||
      !window.confirm(
        `Forget ${n === 1 ? "the 1 refusal" : `all ${n} refusals`} the safety check remembers?\n\nIt will check those prompts and characters again the next time they're used, and may refuse them again. Prompts the word filter refuses stay refused.`
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.clearSafetyRefusals();
      setMemory(r);
      toast.success(r.cleared === 1 ? "Cleared 1 remembered refusal" : `Cleared ${r.cleared} remembered refusals`);
    } catch (e) {
      toast.error(errorText(e, "Couldn't clear them"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <p className="mt-2 text-xs text-ink-300" data-testid="safety-memory">
      Safety check memory: {n === 0 ? "no refusals remembered" : `${n} refusal${n === 1 ? "" : "s"} remembered`}
      {n > 0 && (
        <>
          {" · "}
          <button type="button" className="underline" onClick={clear} disabled={busy}>
            {busy ? "Clearing…" : "Clear"}
          </button>
        </>
      )}
    </p>
  );
}
