// The API counts string offsets in code points (as Python does); a JS
// string and a textarea's selection count UTF-16 units. An emoji (or any
// character outside the Basic Multilingual Plane) is one code point but
// two units, so offsets are converted at the API boundary.

/** The UTF-16 offset `units` in `s` as a code-point offset. */
export function toCodePoints(s: string, units: number): number {
  let n = 0;
  let i = 0;
  const end = Math.min(Math.max(0, units), s.length);
  while (i < end) {
    const c = s.codePointAt(i) ?? 0;
    i += c > 0xffff ? 2 : 1;
    n++;
  }
  return n;
}

/** The code-point offset `points` in `s` as a UTF-16 offset. */
export function toUnits(s: string, points: number): number {
  let n = 0;
  let i = 0;
  while (n < points && i < s.length) {
    const c = s.codePointAt(i) ?? 0;
    i += c > 0xffff ? 2 : 1;
    n++;
  }
  return i;
}

/** `s` with the code-point range [start, end) replaced by `insert`. */
export function spliceCodePoints(s: string, start: number, end: number, insert: string): string {
  return s.slice(0, toUnits(s, start)) + insert + s.slice(toUnits(s, end));
}
