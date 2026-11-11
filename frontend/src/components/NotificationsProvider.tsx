"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { api, Activity, AppEvent } from "@/lib/api";
import { useToast } from "@/components/Toaster";
import { ActivityPanel } from "@/components/ActivityPanel";

/**
 * Live progress for the whole app: one EventSource per tab (opened only once
 * the session is signed in), the activity list (what is running now), the
 * recent-events list behind the bell, toasts and browser notifications for
 * finished work, and the unread count in the document title.
 *
 * Pages subscribe with `useEvents(filter, cb)`; `cb(null)` means "the stream
 * may have missed something (reconnect / reset): refetch". `useActivity()`
 * gives the in-flight work and whether the stream is connected (pollers slow
 * down while it is).
 */

export type EventFilter =
  | ((ev: AppEvent) => boolean)
  | {
      kind?: string | string[];
      project_id?: number | null;
      entity_type?: string;
      entity_id?: number;
    };

type Listener = (ev: AppEvent | null) => void;

type NotificationsCtx = {
  /** Signed in (the stream only runs then). */
  authed: boolean;
  connected: boolean;
  activity: Activity | null;
  refreshActivity: () => void;
  subscribe: (cb: Listener) => () => void;
  // Bell and panel
  unread: number;
  recent: AppEvent[];
  hasMore: boolean;
  recentLoading: boolean;
  loadMore: () => void;
  markAllRead: () => void;
  panelOpen: boolean;
  openPanel: () => void;
  closePanel: () => void;
  // Browser notifications
  notifySupported: boolean;
  notifyOn: boolean;
  notifyBlocked: boolean;
  setNotifyOn: (on: boolean) => void;
};

const Ctx = createContext<NotificationsCtx | null>(null);

const NOTIFY_KEY = "avs:notify";
const BASE_TITLE = "AvatarVideoStudio";
const TITLE_PREFIX = /^\(\d+\)\s+/;
// Finished work: what toasts, notifies and counts as unread.
const FINISHED = ["completed", "failed"];
const STUDIO_GROUP_MS = 1500;

function matches(f: EventFilter, ev: AppEvent): boolean {
  if (typeof f === "function") return f(ev);
  if (f.kind !== undefined) {
    const kinds = Array.isArray(f.kind) ? f.kind : [f.kind];
    if (!kinds.includes(ev.kind)) return false;
  }
  if (f.project_id !== undefined && ev.project_id !== f.project_id) return false;
  if (f.entity_type !== undefined && ev.entity_type !== f.entity_type) return false;
  if (f.entity_id !== undefined && ev.entity_id !== f.entity_id) return false;
  return true;
}

/** Is the user already looking at the page this event links to? */
export function onEventPage(url: string, path: string | null): boolean {
  if (!url || !path) return false;
  const target = url.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";
  const here = path.replace(/\/+$/, "") || "/";
  return here === target || here.startsWith(target + "/");
}

function readNotifyPref(): boolean {
  try {
    return window.localStorage.getItem(NOTIFY_KEY) === "1";
  } catch {
    return false;
  }
}

export function NotificationsProvider({ children }: { children: React.ReactNode }) {
  const toast = useToast();
  const router = useRouter();
  const path = usePathname();
  const pathRef = useRef(path);
  pathRef.current = path;

  const [authed, setAuthed] = useState(false);
  const [connected, setConnected] = useState(false);
  const [activity, setActivity] = useState<Activity | null>(null);
  const [unread, setUnread] = useState(0);
  const [titleCount, setTitleCount] = useState(0);
  const [recent, setRecent] = useState<AppEvent[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [recentLoading, setRecentLoading] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [notifyOn, setNotifyOnState] = useState(false);
  const [notifyBlocked, setNotifyBlocked] = useState(false);
  const [notifySupported, setNotifySupported] = useState(false);

  const listeners = useRef(new Set<Listener>());
  // Where a reconnect resumes: the stream's own SSE id (it never passes an
  // event that may still be committed late), or the snapshot's latest id.
  const lastId = useRef(0);
  // Events already counted in `unread` by the server (listed by loadRecent):
  // when the stream delivers one of them too it isn't counted or announced again.
  const listed = useRef(new Set<number>());
  const seen = useRef(new Set<number>());
  const panelOpenRef = useRef(false);
  panelOpenRef.current = panelOpen;
  const notifyRef = useRef(false);
  notifyRef.current = notifyOn;

  // ---------- auth ----------
  useEffect(() => {
    let alive = true;
    const check = () =>
      api
        .authStatus()
        .then((r) => alive && setAuthed(!!r.authenticated))
        .catch(() => alive && setAuthed(false));
    check();
    const onOut = () => setAuthed(false);
    const onIn = () => check();
    window.addEventListener("avs:unauthenticated", onOut);
    window.addEventListener("avs:authenticated", onIn);
    return () => {
      alive = false;
      window.removeEventListener("avs:unauthenticated", onOut);
      window.removeEventListener("avs:authenticated", onIn);
    };
  }, []);

  // ---------- browser notification preference ----------
  useEffect(() => {
    const supported = typeof window !== "undefined" && "Notification" in window;
    setNotifySupported(supported);
    if (!supported) return;
    setNotifyBlocked(Notification.permission === "denied");
    setNotifyOnState(readNotifyPref() && Notification.permission === "granted");
  }, []);

  const setNotifyOn = useCallback((on: boolean) => {
    const save = (v: boolean) => {
      setNotifyOnState(v);
      try {
        window.localStorage.setItem(NOTIFY_KEY, v ? "1" : "0");
      } catch {
        /* private mode: the toggle still works for this tab */
      }
    };
    if (!on || !("Notification" in window)) {
      save(false);
      return;
    }
    if (Notification.permission === "granted") {
      save(true);
      return;
    }
    if (Notification.permission === "denied") {
      setNotifyBlocked(true);
      save(false);
      return;
    }
    // Ask only here, on the user's click.
    Notification.requestPermission()
      .then((p) => {
        setNotifyBlocked(p === "denied");
        save(p === "granted");
      })
      .catch(() => save(false));
  }, []);

  // ---------- activity ----------
  const activityBusy = useRef(false);
  const refreshActivity = useCallback(() => {
    if (activityBusy.current) return;
    activityBusy.current = true;
    api
      .activeWork()
      .then(setActivity)
      .catch(() => {
        /* keep the last one */
      })
      .finally(() => {
        activityBusy.current = false;
      });
  }, []);
  const activityTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshActivitySoon = useCallback(() => {
    if (activityTimer.current) return;
    activityTimer.current = setTimeout(() => {
      activityTimer.current = null;
      refreshActivity();
    }, 400);
  }, [refreshActivity]);

  // On sign-in, on each page change (a page may just have started something),
  // and as a fallback: every 15 s without the stream, every 30 s with it while
  // something runs (training % and upload % have no event per step).
  useEffect(() => {
    if (authed) refreshActivity();
  }, [authed, path, refreshActivity]);
  const anyRunning = !!activity && activity.items.length > 0;
  useEffect(() => {
    if (!authed) return;
    if (connected && !anyRunning) return;
    const ms = connected ? 30_000 : 15_000;
    const t = setInterval(() => {
      if (!document.hidden) refreshActivity();
    }, ms);
    const onVisible = () => {
      if (!document.hidden) refreshActivity();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [authed, connected, anyRunning, refreshActivity]);

  // ---------- recent events (bell) ----------
  const loadRecent = useCallback(async (more: boolean) => {
    setRecentLoading(true);
    try {
      const before = more ? recentRef.current[recentRef.current.length - 1]?.id : undefined;
      const r = await api.recentEvents({ limit: 30, before_id: before });
      setUnread(r.unread);
      if (!more) setTitleCount((c) => (panelOpenRef.current ? 0 : Math.max(c, r.unread)));
      setRecent((cur) => {
        if (!more) return r.events;
        const ids = new Set(cur.map((e) => e.id));
        return [...cur, ...r.events.filter((e) => !ids.has(e.id))];
      });
      setHasMore(r.has_more);
      for (const e of r.events) listed.current.add(e.id);
      if (listed.current.size > 2000) listed.current = new Set([...listed.current].slice(-500));
    } catch {
      /* the panel shows what it has */
    } finally {
      setRecentLoading(false);
    }
  }, []);
  const recentRef = useRef(recent);
  recentRef.current = recent;

  useEffect(() => {
    if (authed) loadRecent(false);
    else {
      setRecent([]);
      setUnread(0);
      setTitleCount(0);
      setActivity(null);
    }
  }, [authed, loadRecent]);

  const loadMore = useCallback(() => {
    loadRecent(true);
  }, [loadRecent]);

  const markAllRead = useCallback(() => {
    setUnread(0);
    setTitleCount(0);
    setRecent((cur) => cur.map((e) => ({ ...e, read: true })));
    api
      .markEventsRead({ all: true })
      .then((r) => setUnread(r.unread))
      .catch(() => {
        /* the next load shows the server's count */
      });
  }, []);

  const openPanel = useCallback(() => {
    setPanelOpen(true);
    setTitleCount(0);
    loadRecent(false);
    refreshActivity();
  }, [loadRecent, refreshActivity]);
  const closePanel = useCallback(() => setPanelOpen(false), []);

  // ---------- announcing finished work ----------
  const browserNotify = useCallback(
    (title: string, body: string, url: string, tag: string) => {
      if (!notifyRef.current || typeof document === "undefined") return;
      if (document.visibilityState !== "hidden") return;
      if (!("Notification" in window) || Notification.permission !== "granted") return;
      try {
        // The tag makes two open tabs show one notification, not two.
        const n = new Notification(title, { body, tag });
        n.onclick = () => {
          window.focus();
          if (url) router.push(url);
          n.close();
        };
      } catch {
        /* some mobile browsers only allow notifications from a service worker */
      }
    },
    [router]
  );

  const announce = useCallback(
    (ev: AppEvent, title: string, key: string) => {
      const failed = ev.status === "failed";
      const kind = failed ? "error" : ev.severity === "warning" ? "info" : "success";
      if (!onEventPage(ev.url, pathRef.current)) {
        toast.toast(title, kind, {
          key,
          action: ev.url ? { label: "Open", href: ev.url } : undefined,
          duration: failed ? 10_000 : 8000,
        });
      }
      browserNotify(title, ev.detail || "", ev.url, `avs-${key}`);
    },
    [toast, browserNotify]
  );

  // Studio batches finish close together: one toast per burst.
  const studioBuf = useRef<Map<string, AppEvent[]>>(new Map());
  const studioTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushStudio = useCallback(() => {
    studioTimer.current = null;
    const groups = studioBuf.current;
    studioBuf.current = new Map();
    for (const [status, evs] of groups) {
      if (evs.length === 0) continue;
      const last = evs[evs.length - 1];
      if (evs.length === 1) {
        announce(last, last.title, `studio:${status}:${last.entity_id}`);
        continue;
      }
      const pics = evs.filter((e) => /picture/i.test(e.title)).length;
      const what = pics === evs.length ? "pictures" : pics === 0 ? "clips" : "jobs";
      const verb = status === "failed" ? "failed" : "ready";
      announce(last, `Studio: ${evs.length} ${what} ${verb}`, `studio:${status}`);
    }
  }, [announce]);

  const handleEvent = useCallback(
    (ev: AppEvent) => {
      if (!ev || typeof ev.id !== "number" || seen.current.has(ev.id)) return;
      seen.current.add(ev.id);
      if (seen.current.size > 2000) seen.current = new Set([...seen.current].slice(-500));
      // Already in the list loadRecent fetched: counted by the server and shown.
      const known = listed.current.has(ev.id);
      setRecent((cur) => (cur.some((e) => e.id === ev.id) ? cur : [ev, ...cur]));
      const finished = FINISHED.includes(ev.status);
      if (finished && !ev.read && !known) {
        setUnread((n) => n + 1);
        if (!panelOpenRef.current) setTitleCount((n) => n + 1);
      }
      refreshActivitySoon();
      for (const cb of listeners.current) {
        try {
          cb(ev);
        } catch {
          /* one page's handler must not stop the others */
        }
      }
      if (ev.read || known) return;
      if (finished || (ev.severity === "warning" && ev.kind === "budget")) {
        if (ev.kind === "studio_job") {
          const buf = studioBuf.current.get(ev.status) || [];
          buf.push(ev);
          studioBuf.current.set(ev.status, buf);
          if (!studioTimer.current) studioTimer.current = setTimeout(flushStudio, STUDIO_GROUP_MS);
        } else {
          announce(ev, ev.title, `${ev.kind}:${ev.entity_type}:${ev.entity_id}:${ev.status}`);
        }
      }
    },
    [announce, flushStudio, refreshActivitySoon]
  );
  const handleRef = useRef(handleEvent);
  handleRef.current = handleEvent;

  const resync = useCallback(() => {
    for (const cb of listeners.current) {
      try {
        cb(null);
      } catch {
        /* ignore */
      }
    }
  }, []);

  // ---------- the stream ----------
  useEffect(() => {
    if (!authed || typeof window === "undefined" || !("EventSource" in window)) return;
    let es: EventSource | null = null;
    let stopped = false;
    let errors = 0;
    let wasOpen = false;
    let retry: ReturnType<typeof setTimeout> | null = null;

    const open = () => {
      if (stopped) return;
      // A fresh connect gets a snapshot; after a dropped connection resume
      // from the last id we saw so nothing is missed.
      es = new EventSource(api.eventStreamUrl(wasOpen ? lastId.current : null), { withCredentials: true });
      es.onopen = () => {
        errors = 0;
        setConnected(true);
        if (wasOpen) {
          // Back after a gap: pages refetch once, the activity list too.
          resync();
          refreshActivity();
        }
        wasOpen = true;
      };
      es.onmessage = (m) => {
        const resumeAt = Number(m.lastEventId);
        if (Number.isFinite(resumeAt) && resumeAt > 0) lastId.current = resumeAt;
        try {
          handleRef.current(JSON.parse(m.data) as AppEvent);
        } catch {
          /* malformed line: skip it */
        }
      };
      es.addEventListener("snapshot", (m) => {
        try {
          const snap = JSON.parse((m as MessageEvent).data) as { latest_id: number; active: Activity };
          lastId.current = snap.latest_id || 0;
          if (snap.active) setActivity(snap.active);
        } catch {
          /* ignore */
        }
      });
      es.addEventListener("reset", () => {
        // The server no longer has the events after our resume point: start over.
        seen.current = new Set();
        resync();
        loadRecent(false);
      });
      es.onerror = () => {
        setConnected(false);
        errors += 1;
        const closed = es?.readyState === EventSource.CLOSED;
        // The browser retries by itself while the stream is CONNECTING. A
        // CLOSED stream (e.g. a 401: EventSource can't read the status) or
        // repeated failures: check the session, then reopen with a backoff.
        if (!closed && errors < 3) return;
        es?.close();
        es = null;
        api
          .authStatus()
          .then((r) => {
            if (stopped) return;
            if (!r.authenticated) {
              window.dispatchEvent(new CustomEvent("avs:unauthenticated"));
              setAuthed(false);
              return;
            }
            scheduleReopen();
          })
          .catch(() => scheduleReopen());
      };
    };
    const scheduleReopen = () => {
      if (stopped || retry) return;
      const delay = Math.min(60_000, 2000 * 2 ** Math.min(errors, 5));
      retry = setTimeout(() => {
        retry = null;
        open();
      }, delay);
    };

    open();
    return () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      es?.close();
      setConnected(false);
    };
  }, [authed, loadRecent, refreshActivity, resync]);

  // ---------- document title ----------
  useEffect(() => {
    const apply = () => {
      const base = document.title.replace(TITLE_PREFIX, "") || BASE_TITLE;
      const next = titleCount > 0 ? `(${titleCount}) ${base}` : base;
      if (document.title !== next) document.title = next;
    };
    apply();
    // Next may set the title again after a client-side navigation.
    const t = setTimeout(apply, 300);
    return () => clearTimeout(t);
  }, [titleCount, path]);

  const subscribe = useCallback((cb: Listener) => {
    listeners.current.add(cb);
    return () => {
      listeners.current.delete(cb);
    };
  }, []);

  const value = useMemo<NotificationsCtx>(
    () => ({
      authed,
      connected,
      activity,
      refreshActivity,
      subscribe,
      unread,
      recent,
      hasMore,
      recentLoading,
      loadMore,
      markAllRead,
      panelOpen,
      openPanel,
      closePanel,
      notifySupported,
      notifyOn,
      notifyBlocked,
      setNotifyOn,
    }),
    [
      authed,
      connected,
      activity,
      refreshActivity,
      subscribe,
      unread,
      recent,
      hasMore,
      recentLoading,
      loadMore,
      markAllRead,
      panelOpen,
      openPanel,
      closePanel,
      notifySupported,
      notifyOn,
      notifyBlocked,
      setNotifyOn,
    ]
  );

  return (
    <Ctx.Provider value={value}>
      {children}
      {panelOpen && authed && <ActivityPanel />}
    </Ctx.Provider>
  );
}

export function useNotifications(): NotificationsCtx | null {
  return useContext(Ctx);
}

/** In-flight work across the app, and whether the live stream is connected. */
export function useActivity(): { activity: Activity | null; connected: boolean; refresh: () => void } {
  const ctx = useContext(Ctx);
  return {
    activity: ctx?.activity ?? null,
    connected: !!ctx?.connected,
    refresh: ctx?.refreshActivity ?? (() => {}),
  };
}

/** Is the live stream connected? (Pollers slow down while it is.) */
export function useLiveConnected(): boolean {
  return !!useContext(Ctx)?.connected;
}

/**
 * Call `cb` for each live event matching `filter` (null = none), and with
 * `null` when the stream may have missed events (reconnect / reset), so the
 * page refetches. The callback and filter may change every render.
 */
export function useEvents(filter: EventFilter | null, cb: (ev: AppEvent | null) => void): void {
  const subscribe = useContext(Ctx)?.subscribe;
  const cbRef = useRef(cb);
  cbRef.current = cb;
  const filterRef = useRef(filter);
  filterRef.current = filter;
  useEffect(() => {
    if (!subscribe) return;
    return subscribe((ev) => {
      const f = filterRef.current;
      if (f === null) return;
      if (ev !== null && !matches(f, ev)) return;
      cbRef.current(ev);
    });
  }, [subscribe]);
}
