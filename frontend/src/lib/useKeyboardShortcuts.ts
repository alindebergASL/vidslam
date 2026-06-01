import { useEffect } from "react";

export type Shortcut = {
  // Key as reported by KeyboardEvent.key (e.g. "g", "Enter", "Escape", "?").
  key: string;
  // Optional modifier requirements.
  meta?: boolean; // ⌘ on macOS, Win key on Windows
  ctrl?: boolean;
  shift?: boolean;
  alt?: boolean;
  // Action to run; preventDefault is called automatically when this returns truthy/undefined.
  run: (e: KeyboardEvent) => void | boolean;
  label?: string;
};

function _matches(e: KeyboardEvent, s: Shortcut): boolean {
  if (e.key.toLowerCase() !== s.key.toLowerCase()) return false;
  if ((s.meta ?? false) !== e.metaKey) return false;
  if ((s.ctrl ?? false) !== e.ctrlKey) return false;
  if ((s.shift ?? false) !== e.shiftKey) return false;
  if ((s.alt ?? false) !== e.altKey) return false;
  return true;
}

const TYPING_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);

/**
 * Register a set of keyboard shortcuts on the document while the component is
 * mounted. Skips firing while the user is typing in an input/textarea so plain
 * letter shortcuts don't fight text entry — except when the shortcut uses a
 * modifier (cmd/ctrl/alt), where it's safe to fire even while typing.
 */
export function useKeyboardShortcuts(shortcuts: Shortcut[], deps: unknown[] = []): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        !!target &&
        (TYPING_TAGS.has(target.tagName) || target.isContentEditable);
      for (const s of shortcuts) {
        if (!_matches(e, s)) continue;
        const usesModifier = s.meta || s.ctrl || s.alt;
        if (typing && !usesModifier) continue;
        const result = s.run(e);
        if (result !== false) e.preventDefault();
        return;
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
