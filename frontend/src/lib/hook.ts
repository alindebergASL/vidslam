// The opening line (hook) of a spoken script: its first sentence. The same
// rule as the backend (services/hook_lab.py), so the editor shows the line
// the hook lab and Results will call the opening.

// A sentence ends at . ! ? … (or a run of them), optionally followed by
// closing quotes or brackets, then whitespace or the end. A line break ends
// the opening line too.
const END = /[.!?…]+["'”’»)\]]*(?=\s|$)|\n/;

export function splitFirstSentence(text: string): [string, string] {
  const t = (text || "").trim();
  if (!t) return ["", ""];
  const m = END.exec(t);
  if (!m) return [t, ""];
  const end = m.index + m[0].length;
  const first = t.slice(0, end).trim();
  const rest = t.slice(end).trim();
  if (!first) return splitFirstSentence(rest);
  return [first, rest];
}

export const firstSentence = (text: string) => splitFirstSentence(text)[0];

/** `hook` ends a sentence (so the rest of the script stays its own sentence). */
const ENDS = /[.!?…]+["'”’»)\]]*$/;

/** `script` with its opening line replaced by `hook`. A hook typed without
 * end punctuation gets a period, so it isn't merged into the next sentence. */
export function replaceFirstSentence(script: string, hook: string): string {
  const rest = splitFirstSentence(script)[1];
  let h = (hook || "").trim();
  if (!h) return rest;
  if (rest && !ENDS.test(h)) h = `${h}.`;
  return rest ? `${h} ${rest}` : h;
}

/** $0.004 → "$0.004", 0.85 → "$0.85", 0 → "free". */
export function fmtPrice(n: number | null | undefined, freeWord = "free"): string {
  const v = Number(n) || 0;
  if (v <= 0) return freeWord;
  if (v < 0.01) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(2)}`;
}
