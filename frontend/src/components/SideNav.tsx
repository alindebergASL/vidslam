"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { api } from "@/lib/api";

const items = [
  { href: "/", label: "Dashboard", icon: "▦" },
  { href: "/studio", label: "Studio", icon: "✦" },
  { href: "/projects", label: "Projects", icon: "▷" },
  { href: "/cast", label: "Cast", icon: "☺" },
  { href: "/brand", label: "Brand", icon: "◆" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];

function isActive(path: string | null | undefined, href: string): boolean {
  if (!path) return false;
  return href === "/" ? path === "/" : path.startsWith(href);
}

async function doLogout(router: ReturnType<typeof useRouter>) {
  try {
    await api.logout();
  } catch {
    /* even a failed logout should send the user back to the gate */
  }
  // Hard reload so AuthGate's authStatus runs fresh against the cleared cookie.
  if (typeof window !== "undefined") window.location.href = "/";
  else router.push("/");
}

export function SideNav() {
  const path = usePathname();
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);

  // Close the mobile drawer when the route changes.
  useEffect(() => {
    setMobileOpen(false);
  }, [path]);

  // Public share pages render without the app chrome.
  if (path?.startsWith("/share/")) return null;

  return (
    <>
      {/* Mobile top bar (hidden on md+). Stays in-flow so content sits below. */}
      <div className="md:hidden sticky top-0 z-20 flex items-center justify-between bg-ink-900 border-b border-ink-800 px-4 h-12">
        <button
          type="button"
          aria-label="Open navigation menu"
          aria-expanded={mobileOpen}
          onClick={() => setMobileOpen(true)}
          className="text-ink-100 text-xl px-1 -ml-1"
        >
          ☰
        </button>
        <div className="text-sm font-semibold">AvatarVideoStudio</div>
        <div className="w-6" aria-hidden />
      </div>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div
          className="md:hidden fixed inset-0 z-30 bg-black/60"
          onClick={() => setMobileOpen(false)}
          role="presentation"
        >
          <nav
            aria-label="Primary"
            className="absolute inset-y-0 left-0 w-64 bg-ink-900 border-r border-ink-800 p-4 flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <div className="text-lg font-semibold tracking-tight">AvatarVideoStudio</div>
              <button
                type="button"
                aria-label="Close navigation menu"
                onClick={() => setMobileOpen(false)}
                className="text-ink-300 hover:text-white text-lg"
              >
                ✕
              </button>
            </div>
            <div className="flex flex-col gap-1">
              {items.map((it) => (
                <Link
                  key={it.href}
                  href={it.href}
                  aria-current={isActive(path, it.href) ? "page" : undefined}
                  className={clsx(
                    "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition",
                    isActive(path, it.href)
                      ? "bg-ink-700 text-white"
                      : "text-ink-200 hover:bg-ink-800"
                  )}
                >
                  <span className="w-5 text-center text-ink-400" aria-hidden>{it.icon}</span>
                  <span>{it.label}</span>
                </Link>
              ))}
            </div>
            <div className="mt-auto pt-3 border-t border-ink-800 space-y-2">
              <button
                type="button"
                className="w-full text-left px-3 py-2 text-sm rounded-md text-ink-200 hover:bg-ink-800"
                onClick={() => doLogout(router)}
              >
                Sign out
              </button>
              <div className="text-[10px] text-ink-500 px-3">v0.1 MVP</div>
            </div>
          </nav>
        </div>
      )}

      {/* Desktop side nav (≥md) */}
      <aside
        aria-label="Primary"
        className="w-56 shrink-0 border-r border-ink-800 bg-ink-900 p-4 hidden md:flex md:flex-col"
      >
        <div className="px-2 py-3 mb-4">
          <div className="text-lg font-semibold tracking-tight">AvatarVideoStudio</div>
          <div className="text-xs text-ink-400 mt-0.5">Cast → Studio → Video</div>
        </div>
        <nav className="flex flex-col gap-1">
          {items.map((it) => (
            <Link
              key={it.href}
              href={it.href}
              aria-current={isActive(path, it.href) ? "page" : undefined}
              className={clsx(
                "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition",
                isActive(path, it.href)
                  ? "bg-ink-700 text-white"
                  : "text-ink-200 hover:bg-ink-800"
              )}
            >
              <span className="w-5 text-center text-ink-400" aria-hidden>{it.icon}</span>
              <span>{it.label}</span>
            </Link>
          ))}
        </nav>
        <div className="mt-auto pt-3 border-t border-ink-800 space-y-2">
          <button
            type="button"
            className="w-full text-left px-3 py-2 text-sm rounded-md text-ink-300 hover:text-white hover:bg-ink-800"
            onClick={() => doLogout(router)}
          >
            Sign out
          </button>
          <div className="text-xs text-ink-400 px-3">v0.1 MVP</div>
        </div>
      </aside>
    </>
  );
}
