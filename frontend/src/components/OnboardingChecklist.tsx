"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

type Step = {
  id: string;
  label: string;
  cta: string;
  href: string;
  done: boolean;
};

const DISMISS_KEY = "avs.onboardingDismissed";

// Surfaces a progress checklist of the five things a creator needs to do at
// least once to ship their first video. Each row reflects real backend state
// (avatars, ingredients, projects, completed renders) rather than a stored
// "have you clicked this" flag, so it survives interruption + reflects work
// done outside the tour (e.g. via the demo seeder or another tab).
//
// Renders nothing until either the user dismisses it via "Got it" *or* all
// five steps are complete (in which case the auto-hide path takes over —
// no permanent banner). The dismiss flag is per-browser via localStorage.
export function OnboardingChecklist() {
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [dismissed, setDismissed] = useState(true);

  useEffect(() => {
    // Read dismissal flag client-side only — initial false on the server,
    // then sync once mounted to avoid hydration mismatch.
    try {
      setDismissed(localStorage.getItem(DISMISS_KEY) === "1");
    } catch {
      setDismissed(false);
    }
    let cancelled = false;
    const load = async () => {
      const [avatars, ingredients, projects, renders] = await Promise.all([
        api.listAvatars().catch(() => []),
        api.listIngredients().catch(() => []),
        api.listProjects().catch(() => []),
        api.recentRenders(1).catch(() => []),
      ]);
      if (cancelled) return;
      const completedRender = renders.length > 0;
      setSteps([
        {
          id: "character",
          label: "Add a character",
          cta: "Open Cast",
          href: "/cast",
          done: avatars.length > 0,
        },
        {
          id: "ingredient",
          label: "Add a scene, style, or object",
          cta: "Open Cast",
          href: "/cast",
          done: ingredients.length > 0,
        },
        {
          id: "project",
          label: "Create a project",
          cta: "New project",
          href: "/projects/new",
          done: projects.length > 0,
        },
        {
          id: "render",
          label: "Render your first video",
          cta: projects.length > 0 ? "Open project" : "Create a project first",
          href: projects.length > 0 ? `/projects/${projects[0].id}` : "/projects/new",
          done: completedRender,
        },
      ]);
    };
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  if (dismissed || !steps) return null;
  const doneCount = steps.filter((s) => s.done).length;
  if (doneCount === steps.length) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(DISMISS_KEY, "1");
    } catch {
      /* private mode etc. — fall through; checklist just re-appears on reload */
    }
    setDismissed(true);
  };

  return (
    <section
      className="card p-5 mb-8 border-accent/30 bg-gradient-to-br from-ink-900 via-ink-900 to-accent/5"
      aria-label="Getting-started checklist"
    >
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <div className="text-lg font-semibold">Getting started</div>
          <div className="text-xs text-ink-300 mt-0.5">
            {doneCount} of {steps.length} steps done — this card hides itself
            once you finish the last one.
          </div>
        </div>
        <button
          type="button"
          onClick={dismiss}
          className="text-ink-400 hover:text-ink-100 text-xs"
          aria-label="Dismiss the getting-started checklist"
          title="Hide this card. You can clear localStorage to bring it back."
        >
          Got it ✕
        </button>
      </div>
      <div
        className="h-1.5 bg-ink-800 rounded-full overflow-hidden mb-4"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={steps.length}
        aria-valuenow={doneCount}
        aria-label="Onboarding progress"
      >
        <div
          className="h-full bg-accent transition-all"
          style={{ width: `${(doneCount / steps.length) * 100}%` }}
        />
      </div>
      <ul className="space-y-2">
        {steps.map((s) => (
          <li
            key={s.id}
            className={`flex items-center justify-between rounded-md px-3 py-2 text-sm ${
              s.done ? "bg-emerald-500/5 text-ink-300" : "bg-ink-800/60 text-ink-100"
            }`}
          >
            <div className="flex items-center gap-2">
              <span
                aria-hidden
                className={`inline-flex w-5 h-5 items-center justify-center rounded-full text-[10px] ${
                  s.done ? "bg-emerald-500/30 text-emerald-300" : "bg-ink-700 text-ink-300"
                }`}
              >
                {s.done ? "✓" : ""}
              </span>
              <span className={s.done ? "line-through" : ""}>{s.label}</span>
            </div>
            {!s.done && (
              <Link href={s.href} className="btn-ghost text-xs">
                {s.cta} →
              </Link>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
