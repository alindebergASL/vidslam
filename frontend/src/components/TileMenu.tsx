"use client";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";

export type TileMenuItem = {
  label: string;
  onSelect?: () => void;
  /** A link instead of an action. */
  href?: string;
  title?: string;
  danger?: boolean;
};

/**
 * The "⋯" actions button of a Studio tile and its menu. The menu is drawn
 * above the page (tiles clip their content), next to the button, and flips
 * up when there is no room below. Keyboard: Enter / Space / ↓ open it on the
 * first item, ↑ on the last; arrows, Home and End move; Escape closes it and
 * focus goes back to the button; Tab or a click outside closes it.
 */
export function TileMenu({
  items,
  label = "Actions",
  className,
}: {
  items: TileMenuItem[];
  label?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number; maxHeight: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const focusAt = useRef<"first" | "last">("first");
  const menuId = useId();

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) btnRef.current?.focus();
  }, []);

  const place = useCallback(() => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b) return;
    const vh = window.innerHeight;
    const below = vh - b.bottom - 8;
    const above = b.top - 8;
    const menuH = menuRef.current?.scrollHeight || 0;
    const right = Math.max(8, window.innerWidth - b.right);
    if (menuH > below && above > below) {
      const maxHeight = Math.min(above, Math.max(menuH, 120));
      setPos({ top: Math.max(8, b.top - 4 - Math.min(menuH, maxHeight)), right, maxHeight });
    } else {
      setPos({ top: b.bottom + 4, right, maxHeight: Math.max(120, below) });
    }
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    place();
  }, [open, place]);

  // Focus the first (or last) item once the menu is placed.
  useEffect(() => {
    if (!open || !pos) return;
    const list = menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]');
    if (!list || list.length === 0) return;
    if (!menuRef.current?.contains(document.activeElement)) {
      (focusAt.current === "last" ? list[list.length - 1] : list[0]).focus();
    }
  }, [open, pos]);

  // A click outside, a scroll or a resize closes it.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (menuRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      close(false);
    };
    const onScroll = (e: Event) => {
      if (menuRef.current && e.target instanceof Node && menuRef.current.contains(e.target)) return;
      close(false);
    };
    const onResize = () => close(false);
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [open, close]);

  const onMenuKey = (e: React.KeyboardEvent) => {
    const list = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || []);
    const i = list.indexOf(document.activeElement as HTMLElement);
    const go = (n: number) => {
      e.preventDefault();
      list[(n + list.length) % list.length]?.focus();
    };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i < 0 ? list.length - 1 : i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(list.length - 1);
    else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close(true);
    } else if (e.key === "Tab") {
      // The menu is portalled to the end of the page and removed on close:
      // move focus back to the ⋯ button first, so Tab continues from the
      // tile instead of from the top of the page.
      btnRef.current?.focus();
      close(false);
    }
  };

  const choose = (item: TileMenuItem) => {
    close(!item.href);
    item.onSelect?.();
  };

  if (items.length === 0) return null;

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        title={label}
        onClick={(e) => {
          e.stopPropagation();
          focusAt.current = "first";
          setOpen((v) => !v);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            focusAt.current = e.key === "ArrowUp" ? "last" : "first";
            setOpen(true);
          }
        }}
        className={clsx(
          "w-8 h-8 pointer-coarse:w-10 pointer-coarse:h-10 rounded-full bg-ink-900/85 border border-ink-700 text-ink-100 text-base leading-none flex items-center justify-center hover:text-white hover:border-ink-500",
          open && "opacity-100!",
          className
        )}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {open &&
        createPortal(
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            aria-label={label}
            onKeyDown={onMenuKey}
            style={
              pos
                ? { top: pos.top, right: pos.right, maxHeight: pos.maxHeight }
                : { top: -9999, right: 0, visibility: "hidden" }
            }
            className="fixed z-50 min-w-44 max-w-[calc(100vw-16px)] overflow-y-auto rounded-md border border-ink-700 bg-ink-900 py-1 shadow-xl text-sm"
          >
            {items.map((it) =>
              it.href ? (
                <a
                  key={it.label}
                  role="menuitem"
                  tabIndex={-1}
                  href={it.href}
                  title={it.title}
                  onClick={() => choose(it)}
                  className="block px-3 py-2 pointer-coarse:py-2.5 text-ink-100 hover:bg-ink-800 focus:bg-ink-800 outline-hidden"
                >
                  {it.label}
                </a>
              ) : (
                <button
                  key={it.label}
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  title={it.title}
                  onClick={() => choose(it)}
                  className={clsx(
                    "block w-full text-left px-3 py-2 pointer-coarse:py-2.5 hover:bg-ink-800 focus:bg-ink-800 outline-hidden",
                    it.danger ? "text-red-300 border-t border-ink-800 mt-1" : "text-ink-100"
                  )}
                >
                  {it.label}
                </button>
              )
            )}
          </div>,
          document.body
        )}
    </>
  );
}
