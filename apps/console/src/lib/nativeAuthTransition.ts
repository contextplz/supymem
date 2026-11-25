/** Keep the current venture on a same-account reconnect. A different verified
 * account must leave the previous account's venture route.
 */
export function shouldResetPortfolio(
  previous: string | undefined, next: string,
  previousSubject?: string | null, nextSubject?: string | null,
): boolean {
  return next === "connected" && previous !== undefined
    && Boolean(previousSubject && nextSubject && previousSubject !== nextSubject);
}
