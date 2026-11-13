"use client";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef } from "react";
import type { CSSProperties, TextareaHTMLAttributes } from "react";
import clsx from "clsx";

/** True when the browser sizes a textarea to its text by itself (CSS field-sizing: content). */
function cssSizesToContent(): boolean {
  try {
    return typeof CSS !== "undefined" && CSS.supports("field-sizing", "content");
  } catch {
    return false;
  }
}

type Props = TextareaHTMLAttributes<HTMLTextAreaElement> & {
  /** How tall the box may grow before it scrolls. About a third of the screen by default. */
  maxHeight?: string;
};

/**
 * A prompt box that grows with its text: it starts one line tall (or at the
 * height its min-h class sets), grows as you type, and scrolls once it reaches
 * `maxHeight`. It also resizes when the text changes from outside (a preset,
 * Enhance, Undo or Clear), because the size is worked out again whenever
 * `value` changes. Browsers that support CSS field-sizing do this themselves;
 * the others get the height set here.
 */
export const AutoTextarea = forwardRef<HTMLTextAreaElement, Props>(function AutoTextarea(
  { className, style, maxHeight = "30dvh", rows = 1, value, onChange, ...rest },
  ref,
) {
  const inner = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => inner.current as HTMLTextAreaElement);

  const fit = useCallback(() => {
    const el = inner.current;
    if (!el || cssSizesToContent()) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const borders = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
    const max = parseFloat(cs.maxHeight);
    const want = el.scrollHeight + borders;
    const capped = Number.isFinite(max) && want > max;
    el.style.height = `${capped ? max : want}px`;
    el.style.overflowY = capped ? "auto" : "hidden";
  }, []);

  // Before paint, so the box never flashes at the wrong height.
  useLayoutEffect(fit, [fit, value]);

  // A wider or narrower box wraps the text differently: fit it again when the width changes.
  useEffect(() => {
    const el = inner.current;
    if (!el || cssSizesToContent() || typeof ResizeObserver === "undefined") return;
    let width = el.clientWidth;
    const ro = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [fit]);

  const merged: CSSProperties = { maxHeight, ...style };
  return (
    <textarea
      ref={inner}
      rows={rows}
      value={value}
      onChange={(e) => {
        onChange?.(e);
        // Uncontrolled boxes (no `value`) still need to grow as you type.
        if (value === undefined) fit();
      }}
      className={clsx("resize-none overflow-y-auto field-sizing-content", className)}
      style={merged}
      {...rest}
    />
  );
});
