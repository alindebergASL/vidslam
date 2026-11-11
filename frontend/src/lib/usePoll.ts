"use client";
import { useCallback, useEffect, useRef } from "react";
import { useLiveConnected } from "@/components/NotificationsProvider";

/** How long to wait between checks while the live stream is connected (events
 *  trigger refetches; the timer is only a fallback). */
export const LIVE_FALLBACK_MS = 12_000;

/**
 * A page poller that is gentle on the server:
 * - runs `fn` every `interval` ms while `enabled`, slowed to `liveInterval`
 *   (default 12 s) while the live event stream is connected;
 * - never overlaps itself (a slow tick skips the next one);
 * - pauses while the tab is hidden, and catches up once when it is shown;
 * - returns `poke()` to run now (e.g. on a matching event). A poke while the
 *   tab is hidden is remembered and runs when the tab is shown.
 */
export function usePoll(
  fn: () => unknown | Promise<unknown>,
  opts: { enabled: boolean; interval: number; liveInterval?: number }
): () => void {
  const connected = useLiveConnected();
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const busy = useRef(false);
  const stale = useRef(false);
  const again = useRef(false);

  const run = useCallback(async () => {
    if (typeof document !== "undefined" && document.hidden) {
      stale.current = true;
      return;
    }
    if (busy.current) {
      // A poke during a tick: run once more right after it.
      again.current = true;
      return;
    }
    busy.current = true;
    stale.current = false;
    try {
      await fnRef.current();
    } catch {
      /* the caller reports its own errors; keep polling */
    } finally {
      busy.current = false;
    }
    if (again.current) {
      again.current = false;
      run();
    }
  }, []);

  const tick = useCallback(() => {
    // Timer ticks never queue a follow-up; only pokes do.
    if (busy.current) return;
    run();
  }, [run]);

  const { enabled, interval } = opts;
  const live = Math.max(interval, opts.liveInterval ?? LIVE_FALLBACK_MS);
  const ms = connected ? live : interval;
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  useEffect(() => {
    if (!enabled) return;
    const t = setInterval(tick, ms);
    return () => clearInterval(t);
  }, [enabled, ms, tick]);

  useEffect(() => {
    const onVisible = () => {
      if (document.hidden) return;
      if (enabledRef.current || stale.current) run();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [run]);

  return run;
}
