"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { useEvents } from "@/components/NotificationsProvider";
import { useToast } from "@/components/Toaster";
import { StoryStatus } from "@/components/sound/shared";
import { usePoll } from "@/lib/usePoll";
import { api, errorText, type SoundStorySummary } from "@/lib/api";

export default function SoundStudioPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

const LIVE = [
  {
    name: "Sound Effects",
    href: "/sound/effects",
    text: "One-off effects and loops from a description, up to four takes, saved to your library.",
  },
];

const COMING = [
  { name: "Music", text: "Background music and beds for stories and videos." },
  { name: "Vocal tracks", text: "Sung or spoken vocal tracks." },
];

function Inner() {
  const toast = useToast();
  const [stories, setStories] = useState<SoundStorySummary[]>([]);
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(
    () =>
      api
        .listStories()
        .then((rows) => {
          setStories(rows);
          setLoaded(true);
        })
        .catch((e) => {
          setLoaded(true);
          toast.error(errorText(e, "Couldn't load your stories"));
        }),
    [toast]
  );
  useEffect(() => {
    reload();
  }, [reload]);
  const busy = stories.some((s) => s.status === "scripting" || s.status === "generating");
  const poke = usePoll(reload, { enabled: busy, interval: 3000 });
  useEvents({ kind: "sound_story" }, () => poke());

  const remove = async (s: SoundStorySummary) => {
    if (!window.confirm(`Delete “${s.title || "Untitled story"}”?`)) return;
    try {
      await api.deleteStory(s.id);
      reload();
    } catch (e) {
      toast.error(errorText(e, "Delete failed"));
    }
  };

  return (
    <div className="p-4 sm:p-8 max-w-6xl">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Sound <span className="text-grad">Studio</span>
          </h1>
          <p className="text-sm text-ink-300 mt-1 max-w-xl">
            Audio stories from a few minutes to an hour and a half: ASMR, sleep stories, cozy and spooky tales,
            drama. Write it yourself or let the writer draft it chapter by chapter, then shape it with Enhance.
          </p>
        </div>
        <Link href="/sound/stories/new" className="btn-primary">
          + New story
        </Link>
      </header>

      <section aria-labelledby="stories-h" className="mb-10">
        <h2 id="stories-h" className="text-lg font-semibold mb-3">
          Audio stories
        </h2>
        {!loaded ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton h-28 rounded-xl" />
            ))}
          </div>
        ) : stories.length === 0 ? (
          <div className="card p-6 text-sm text-ink-300">
            No stories yet.{" "}
            <Link href="/sound/stories/new" className="text-violet-300 underline">
              Start one
            </Link>{" "}
            from a premise, or paste a script you wrote.
          </div>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {stories.map((s) => (
              <li key={s.id} className="card card-hover p-4 flex flex-col gap-2">
                <div className="flex items-start justify-between gap-2">
                  <Link href={`/sound/stories/${s.id}`} className="font-medium text-ink-50 hover:underline line-clamp-2">
                    {s.title || "Untitled story"}
                  </Link>
                  <StoryStatus status={s.status} task={s.task} />
                </div>
                <p className="text-xs text-ink-400 line-clamp-2">{s.premise || "No premise"}</p>
                <div className="mt-auto flex items-center justify-between text-xs text-ink-400">
                  <span>
                    {s.target_minutes} min target
                    {s.status === "scripting" && s.writer.chapters
                      ? ` · chapter ${s.writer.chapters_done} of ${s.writer.chapters}`
                      : ""}
                  </span>
                  <button
                    type="button"
                    className="text-ink-400 hover:text-red-300 disabled:opacity-40"
                    disabled={s.status === "scripting" || s.status === "generating"}
                    onClick={() => remove(s)}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="tools-h">
        <h2 id="tools-h" className="text-lg font-semibold mb-3">
          More tools
        </h2>
        <ul className="grid gap-3 sm:grid-cols-3">
          {LIVE.map((t) => (
            <li key={t.name}>
              <Link href={t.href} className="card card-hover p-4 block h-full">
                <span className="font-medium">{t.name}</span>
                <p className="text-xs text-ink-400 mt-2">{t.text}</p>
              </Link>
            </li>
          ))}
          {COMING.map((t) => (
            <li key={t.name} className="card p-4 opacity-70" aria-disabled="true">
              <div className="flex items-center justify-between">
                <span className="font-medium">{t.name}</span>
                <span className="chip text-xs">Coming</span>
              </div>
              <p className="text-xs text-ink-400 mt-2">{t.text}</p>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
