/** Same-app routes the Mac shell may open from a desktop notice. */
export function trustedDesktopNoticeTarget(path: string | null | undefined): string | null {
  if (!path || path.includes("?") || path.includes("..") || path.includes("%")) return null;
  if (/^\/v\/ven_[A-Za-z0-9_-]+\/(?:now|work|outreach|connections|analytics|settings|suggestions)$/.test(path)) {
    return path;
  }
  if (/^\/v\/ven_[A-Za-z0-9_-]+\/device-work-orders\/dwo_[A-Za-z0-9_-]+$/.test(path)) {
    return path;
  }
  if (/^\/v\/ven_[A-Za-z0-9_-]+#attention-ht_[A-Za-z0-9_-]+$/.test(path)) {
    return path;
  }
  return null;
}

/** A notice's trusted destination is independent of the selected venture.
 * The shell's venture (e.g. /new?venture=... during idea clarification) counts as selected, not only /v/ paths. */
export function isOtherVentureNotice(target: string | null | undefined, currentPath: string, currentVentureId?: string | null): boolean {
  const href = trustedDesktopNoticeTarget(target);
  if (!href) return false;
  const destination = /^\/v\/(ven_[A-Za-z0-9_-]+)(?:\/|#|$)/.exec(href)?.[1];
  const selected = /^\/v\/(ven_[A-Za-z0-9_-]+)(?:\/|$)/.exec(currentPath)?.[1] || currentVentureId || undefined;
  return Boolean(destination && destination !== selected && (selected || currentPath === "/new"));
}
