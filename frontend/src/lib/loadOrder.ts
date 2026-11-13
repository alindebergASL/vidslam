/** Keeps one component's reads of a setting in order. Each read gets a
 * number when it is sent; an answer counts only if no newer read has already
 * answered. A newer success and a newer failure each make older answers
 * stale, kept as two separate marks so a failure never moves the "shown"
 * mark: a slow old success arriving after a newer failed read is dropped
 * instead of being shown as if it were current. */
export function createLoadOrder() {
  let sent = 0;
  let shown = 0;
  let failed = 0;
  const stale = (n: number) => n < shown || n < failed;
  return {
    /** Number the next read. */
    next: () => ++sent,
    /** Whether a successful answer to read `n` should be shown. */
    acceptSuccess(n: number): boolean {
      if (stale(n)) return false;
      shown = n;
      return true;
    },
    /** Whether a failed answer to read `n` should be reported. */
    acceptFailure(n: number): boolean {
      if (stale(n)) return false;
      failed = n;
      return true;
    },
  };
}
