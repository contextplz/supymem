/** Private, in-memory summary shared by the Website and Go to market views. */
export type WaitlistSummary = {
  venture_id: string;
  enabled: boolean;
  url: string;
  submission_verified: boolean;
  verified_at?: string;
  total: number;
  probe_retry_after_seconds?: number;
  published_site?: {source: "deployment" | "site_version"; id: string; url: string; observed_at?: string | null} | null;
};

export type Saved = {data: WaitlistSummary; cooldownUntil: number};
const lastLoaded = new Map<string, Saved>();
const storagePrefix = "supymem.waitlist-status.v1:";

/** Only the server's exact venture destination may become a founder-facing link. */
export function scopedWaitlistUrl(data: WaitlistSummary | null): string | undefined {
  if (!data) return undefined;
  try {
    const url = new URL(data.url);
    if (url.protocol === "https:" && !url.username && !url.password
      && url.pathname === `/public/waitlists/${data.venture_id}` && !url.search && !url.hash) return url.href;
  } catch { /* The status did not contain a usable public destination. */ }
  return undefined;
}

/** Share only from a confirmed response, after publication and the public form probe. */
export function verifiedPublishedSiteUrl(data: WaitlistSummary | null, statusConfirmed: boolean): string | undefined {
  if (!statusConfirmed || !data?.enabled || !data.submission_verified || !scopedWaitlistUrl(data)) return undefined;
  const publication = data.published_site;
  if (!publication || !["deployment", "site_version"].includes(publication.source) || !publication.id) return undefined;
  try {
    const url = new URL(publication.url);
    if (url.protocol === "https:" && url.hostname.includes(".") && !url.username && !url.password
      && !url.hash) return url.href;
  } catch { /* Publication did not contain a usable URL. */ }
  return undefined;
}

function key(subject: string, ventureId: string): string {
  return JSON.stringify([subject, ventureId]);
}

/** A successful mutation can supply the committed status without another rate-limited GET. */
export function scopedWaitlistStatus(value: unknown, ventureId: string): WaitlistSummary | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Partial<WaitlistSummary>;
  return row.venture_id === ventureId && typeof row.enabled === "boolean"
    && typeof row.submission_verified === "boolean" && typeof row.url === "string"
    && typeof row.total === "number" && Number.isInteger(row.total) && row.total >= 0
    ? row as WaitlistSummary : null;
}

export function rememberWaitlistStatus(subject: string, data: WaitlistSummary, checkedAt: number): Saved {
  // Deliberately omit recent lead emails and consent text from this cache.
  const {venture_id, enabled, url, submission_verified, verified_at, total, probe_retry_after_seconds, published_site} = data;
  const seconds = Number.isFinite(probe_retry_after_seconds)
    ? Math.max(0, Math.ceil(probe_retry_after_seconds || 0)) : 0;
  const saved = {
    data: {venture_id, enabled, url, submission_verified, verified_at, total, probe_retry_after_seconds, published_site},
    cooldownUntil: checkedAt + seconds * 1000,
  };
  lastLoaded.set(key(subject, venture_id), saved);
  // A route change can recreate the console bundle. Keep only the summary,
  // never lead emails, and only for the server's short probe cooldown.
  try {
    if (typeof sessionStorage !== "undefined") {
      if (seconds > 0) sessionStorage.setItem(storagePrefix + key(subject, venture_id), JSON.stringify(saved));
      else sessionStorage.removeItem(storagePrefix + key(subject, venture_id));
    }
  } catch { /* Storage is optional; the in-memory copy still works. */ }
  return saved;
}

export function lastWaitlistStatus(subject: string, ventureId: string): Saved | undefined {
  const cacheKey = key(subject, ventureId);
  const inMemory = lastLoaded.get(cacheKey);
  if (inMemory?.cooldownUntil && inMemory.cooldownUntil > Date.now()) return inMemory;
  try {
    const raw = typeof sessionStorage !== "undefined" ? sessionStorage.getItem(storagePrefix + cacheKey) : null;
    if (!raw) return inMemory;
    const saved = JSON.parse(raw) as Saved;
    if (saved?.data?.venture_id === ventureId && Number.isFinite(saved.cooldownUntil)
      && saved.cooldownUntil > Date.now()) {
      lastLoaded.set(cacheKey, saved);
      return saved;
    }
    sessionStorage.removeItem(storagePrefix + cacheKey);
  } catch { /* Ignore invalid or unavailable session storage. */ }
  return inMemory;
}

export function waitlistStatusDuringCooldown(subject: string, ventureId: string, now: number): Saved | undefined {
  const saved = lastWaitlistStatus(subject, ventureId);
  return saved && saved.cooldownUntil > now ? saved : undefined;
}

export function forgetWaitlistStatus(subject: string, ventureId: string): void {
  const cacheKey = key(subject, ventureId);
  lastLoaded.delete(cacheKey);
  try { if (typeof sessionStorage !== "undefined") sessionStorage.removeItem(storagePrefix + cacheKey); } catch { /* Optional storage. */ }
}

export function waitlistReadFailure(status: number, lastStatus: WaitlistSummary | null): {data: WaitlistSummary | null; notice: string} | null {
  if (status === 429) return {
    data: lastStatus,
    notice: lastStatus
      ? "The request limit was reached. Showing the last loaded status. Wait a moment, then refresh."
      : "The request limit was reached. Wait a moment, then refresh the waitlist status.",
  };
  if (lastStatus && (status === 0 || status >= 500)) return {
    data: lastStatus,
    notice: "The waitlist status could not be refreshed. Showing the last loaded status; try again shortly.",
  };
  return null;
}
