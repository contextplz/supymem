const CHUNK_RE =
  /Loading chunk .* failed|Loading CSS chunk|Failed to fetch dynamically imported module/i;
const KEY = "supymem.chunkReloadAt";
const WINDOW_MS = 30_000;

export function isChunkLoadError(err: unknown): boolean {
  const e = err as { name?: unknown; message?: unknown } | null | undefined;
  if (e?.name === "ChunkLoadError") return true;
  const msg = typeof err === "string" ? err : typeof e?.message === "string" ? e.message : "";
  return CHUNK_RE.test(msg);
}

/** True when a reload is allowed (none in the last 30s); pure for testing. */
export function canReloadNow(last: number, now: number): boolean {
  return !(now - last >= 0 && now - last < WINDOW_MS);
}

/**
 * Hard-reloads once per 30s window after a chunk failure (stale deploy).
 * Returns true if a reload was triggered; false means show the error UI.
 */
export function reloadOnceForChunkError(): boolean {
  try {
    const now = Date.now();
    const last = Number(sessionStorage.getItem(KEY) || 0);
    if (!canReloadNow(last, now)) return false;
    sessionStorage.setItem(KEY, String(now));
  } catch {
    return false; // no storage: can't guard against loops, so don't reload
  }
  window.location.reload();
  return true;
}
