"use client";
import clsx from "clsx";
import { useState } from "react";
import { api } from "@/lib/api";

/** Small 16:9 render thumbnail from a backend-relative URL; blank tile when missing. */
export function VideoThumb({ url, className }: { url: string | null | undefined; className?: string }) {
  const [broken, setBroken] = useState(false);
  return (
    <div className={clsx("aspect-video rounded-md bg-ink-800 overflow-hidden flex items-center justify-center", className)}>
      {url && !broken ? (
        <img
          src={api.backendUrl(url)}
          alt=""
          loading="lazy"
          className="w-full h-full object-cover"
          onError={() => setBroken(true)}
        />
      ) : (
        <span className="text-ink-500 text-lg" aria-hidden>▷</span>
      )}
    </div>
  );
}

/** Message for an API failure: the backend's plain-language detail when there is one. */
export function errText(e: unknown, fallback: string): string {
  const any = e as { detail?: unknown; message?: unknown } | null;
  if (any && typeof any.detail === "string" && any.detail) return any.detail;
  if (any && typeof any.message === "string" && any.message) return any.message;
  return fallback;
}
