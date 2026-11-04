"use client";
import { useEffect, useState } from "react";
import { useToast } from "@/components/Toaster";
import { api, YouTubeStatus } from "@/lib/api";

export function YouTubeCard() {
  const toast = useToast();
  const [st, setSt] = useState<YouTubeStatus | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.youtubeStatus().then(setSt).catch(() => {});
    // Back from Google's consent screen?
    const q = new URLSearchParams(window.location.search);
    if (q.get("youtube") === "connected") toast.success("YouTube channel connected");
    if (q.get("youtube") === "error") toast.error(`YouTube: ${q.get("reason") || "connection failed"}`);
  }, []);

  const connect = async () => {
    setBusy(true);
    try {
      const { url } = await api.youtubeConnectUrl();
      window.location.href = url;
    } catch (e: any) {
      toast.error(e.message || "Couldn't start the connection");
      setBusy(false);
    }
  };

  return (
    <section id="youtube" className="card p-4 mb-6" aria-labelledby="yt-h">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="yt-h" className="text-lg font-medium">YouTube</h2>
          <p className="text-xs text-ink-400 mt-0.5">
            {st?.connected
              ? `Connected to ${st.channel?.title ?? "your channel"}. Renders can be uploaded and scheduled from their pages.`
              : "Upload and schedule renders straight to a channel."}
          </p>
        </div>
        {st?.connected ? (
          <button type="button" className="btn-ghost text-xs" disabled={busy}
                  onClick={async () => { setSt(await api.youtubeDisconnect()); toast.info("YouTube disconnected"); }}>
            Disconnect
          </button>
        ) : (
          <button type="button" className="btn-primary text-sm disabled:opacity-50" disabled={busy || !st?.configured} onClick={connect}>
            {busy ? "Opening Google…" : "Connect channel"}
          </button>
        )}
      </div>
      {st && !st.configured && (
        <div className="mt-3 text-xs text-ink-300 space-y-1">
          <p>To enable: create an OAuth client (type <em>Web application</em>) in Google Cloud with the YouTube Data API v3 enabled, then set:</p>
          <pre className="bg-ink-950 border border-ink-800 rounded-md p-2 overflow-x-auto">YOUTUBE_CLIENT_ID=…{"\n"}YOUTUBE_CLIENT_SECRET=…</pre>
          <p>Authorized redirect URI: <code className="break-all">{st.redirect_uri}</code></p>
        </div>
      )}
      {st?.connected && (
        <p className="text-[11px] text-ink-400 mt-2">
          Google&apos;s default quota allows 100 uploads per day. Uploads disclose AI content (containsSyntheticMedia) unless you untick it.
        </p>
      )}
    </section>
  );
}
