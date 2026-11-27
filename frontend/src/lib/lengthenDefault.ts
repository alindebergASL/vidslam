// The minutes Lengthen starts from for a scope: the part's own current length
// (rounded up), never the whole story's target. null means "the selection's
// own length", which the server works out from the text it rewrites.

export type LengthenScope = "selection" | "chapter" | "story";

export function lengthenDefault(
  scope: LengthenScope,
  chapters: { line: number; est_seconds: number }[],
  chapterLine: number | undefined,
  storySeconds: number,
  maxMinutes: number,
): number | null {
  const fit = (s: number) => Math.max(1, Math.min(maxMinutes, Math.ceil(s / 60)));
  if (scope === "story") return fit(storySeconds);
  if (scope === "chapter") {
    const c = chapters.find((x) => x.line === chapterLine) ?? chapters[0];
    return c ? fit(c.est_seconds) : null;
  }
  return null;
}
