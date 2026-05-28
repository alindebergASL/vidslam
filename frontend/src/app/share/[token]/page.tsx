"use client";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";

type Meta = { title: string; aspect_ratio: string; created_at: string; disclosure: string };

export default function SharePage() {
  const params = useParams();
  const token = String(params?.token || "");
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    api
      .publicRenderMeta(token)
      .then(setMeta)
      .catch(() => setError("This share link is invalid or the video is no longer available."));
  }, [token]);

  return (
    <div className="min-h-screen bg-ink-950 flex flex-col items-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="flex items-center justify-between mb-6">
          <div className="text-lg font-semibold tracking-tight">AvatarVideoStudio</div>
          <span className="text-xs text-ink-400">Shared video</span>
        </div>

        {error ? (
          <div className="card p-8 text-center text-ink-300 text-sm">{error}</div>
        ) : (
          <>
            <div className="card overflow-hidden bg-black mb-4">
              <video
                src={api.publicRender(token)}
                poster={api.publicRenderThumb(token)}
                controls
                playsInline
                autoPlay
                muted
                className="w-full max-h-[78vh] mx-auto"
              />
            </div>
            <div className="px-1">
              <h1 className="text-xl font-semibold">{meta?.title || "Loading…"}</h1>
              <div className="flex items-center gap-2 mt-2 text-xs text-ink-400">
                {meta && (
                  <>
                    <span className="chip text-[10px] py-0.5">{meta.disclosure}</span>
                    <span>{new Date(meta.created_at).toLocaleDateString()}</span>
                  </>
                )}
              </div>
            </div>
            <div className="mt-6 text-center">
              <a
                href={api.publicRender(token)}
                download
                className="btn-ghost text-xs"
              >
                Download MP4
              </a>
            </div>
          </>
        )}

        <div className="mt-10 text-center text-[10px] text-ink-500">
          Created with AvatarVideoStudio · AI-generated virtual creator content
        </div>
      </div>
    </div>
  );
}
