"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";

const items = [
  { href: "/", label: "Dashboard", icon: "▦" },
  { href: "/studio", label: "Studio", icon: "✦" },
  { href: "/projects", label: "Projects", icon: "▷" },
  { href: "/cast", label: "Cast", icon: "☺" },
  { href: "/brand", label: "Brand", icon: "◆" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];

export function SideNav() {
  const path = usePathname();
  // Public share pages render without the app chrome.
  if (path?.startsWith("/share/")) return null;
  return (
    <aside className="w-56 shrink-0 border-r border-ink-800 bg-ink-900 p-4 hidden md:flex md:flex-col">
      <div className="px-2 py-3 mb-4">
        <div className="text-lg font-semibold tracking-tight">AvatarVideoStudio</div>
        <div className="text-xs text-ink-400 mt-0.5">Cast → Studio → Video</div>
      </div>
      <nav className="flex flex-col gap-1">
        {items.map((it) => {
          const active =
            it.href === "/" ? path === "/" : path?.startsWith(it.href);
          return (
            <Link
              key={it.href}
              href={it.href}
              className={clsx(
                "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition",
                active
                  ? "bg-ink-700 text-white"
                  : "text-ink-200 hover:bg-ink-800"
              )}
            >
              <span className="w-5 text-center text-ink-400">{it.icon}</span>
              <span>{it.label}</span>
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto text-xs text-ink-400 px-2">
        <div>v0.1 MVP</div>
      </div>
    </aside>
  );
}
