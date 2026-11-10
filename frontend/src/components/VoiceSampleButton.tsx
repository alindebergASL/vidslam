"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";

// One shared player, so starting a sample stops the one already playing.
let player: HTMLAudioElement | null = null;
let playingUrl: string | null = null;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((fn) => fn());

function stop() {
  player?.pause();
  playingUrl = null;
  notify();
}

/**
 * ▶ plays the voice provider's free sample of a voice (no paid call). Shows
 * nothing useful when the voice has no sample: the button is disabled.
 */
export function VoiceSampleButton({
  url,
  name,
  className,
}: {
  url?: string | null;
  name?: string;
  className?: string;
}) {
  const [, tick] = useState(0);
  useEffect(() => {
    const fn = () => tick((n) => n + 1);
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  }, []);
  // Stop the sample when the picker goes away.
  useEffect(() => () => {
    if (url && playingUrl === url) stop();
  }, [url]);

  const playing = !!url && playingUrl === url;
  const toggle = () => {
    if (!url) return;
    if (playing) {
      stop();
      return;
    }
    if (!player) {
      player = new Audio();
      player.addEventListener("ended", () => {
        playingUrl = null;
        notify();
      });
    }
    player.pause();
    player.src = url;
    playingUrl = url;
    notify();
    player.play().catch(() => {
      playingUrl = null;
      notify();
    });
  };

  return (
    <button
      type="button"
      className={clsx("btn-ghost text-xs px-2 shrink-0 disabled:opacity-40", className)}
      onClick={toggle}
      disabled={!url}
      aria-label={playing ? "Stop the voice sample" : `Play a sample of ${name || "this voice"}`}
      title={
        url
          ? playing
            ? "Stop"
            : "Play the voice provider's free sample of this voice. Nothing is paid for."
          : "This voice has no free sample to play."
      }
    >
      {playing ? "■" : "▶"}
    </button>
  );
}
