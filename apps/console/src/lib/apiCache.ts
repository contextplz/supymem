/** Stale-while-revalidate store for GET responses. Entries are scoped to the signed-in subject
 * and live in memory plus sessionStorage. Auth and secret-bearing paths are never stored. */
export const FRESH_MS = 5_000; // younger than this: skip the network refresh
export const MAX_STALE_MS = 5 * 60_000; // older than this: treated as absent
const PREFIX = "supymem.swr.v1:";
const MAX_ENTRY_CHARS = 200_000;
const NEVER = /^\/(auth|api\/telemetry)|secret|token|api[-_]?key|credential|password|\/keys?(\/|\?|$)/i;

type Entry = { at: number; data: unknown };
const mem = new Map<string, Entry>();

export const cacheKey = (scope: string, path: string) => `${PREFIX}${scope}|${path}`;
export const cacheable = (path: string) => !NEVER.test(path);
export const isVentureList = (path: string) => /^\/api\/ventures(?:\/deleted)?(?:\?|$)/.test(path);

function store(): Storage | null {
  try { return typeof sessionStorage === "undefined" ? null : sessionStorage; } catch { return null; }
}

export function cacheGet(scope: string | null, path: string, now = Date.now()): { data: unknown; fresh: boolean } | null {
  if (!scope || !cacheable(path)) return null;
  const key = cacheKey(scope, path);
  let entry = mem.get(key);
  if (!entry) {
    try {
      const raw = store()?.getItem(key);
      if (raw) { entry = JSON.parse(raw) as Entry; mem.set(key, entry); }
    } catch { /* unreadable storage or corrupt entry: behave as a miss */ }
  }
  if (!entry || now - entry.at > MAX_STALE_MS) return null;
  return { data: entry.data, fresh: now - entry.at < FRESH_MS };
}

export function cachePut(scope: string | null, path: string, data: unknown, now = Date.now()): void {
  if (!scope || !cacheable(path)) return;
  const key = cacheKey(scope, path);
  const entry: Entry = { at: now, data };
  mem.set(key, entry);
  try {
    const raw = JSON.stringify(entry);
    if (raw.length <= MAX_ENTRY_CHARS) store()?.setItem(key, raw);
  } catch { /* quota or private mode: memory copy still serves this renderer */ }
}

function drop(match: (key: string) => boolean): void {
  for (const key of [...mem.keys()]) if (match(key)) mem.delete(key);
  try {
    const s = store();
    if (!s) return;
    for (let i = s.length - 1; i >= 0; i--) {
      const key = s.key(i);
      if (key?.startsWith(PREFIX) && match(key)) s.removeItem(key);
    }
  } catch { /* nothing more to clear */ }
}

/** Drop a scope's entries whose path mentions `needle` (a venture id); no needle drops the whole scope. */
export function cacheInvalidate(scope: string | null, needle?: string, ventureLists = false): void {
  if (!scope) return;
  const head = `${PREFIX}${scope}|`;
  drop((key) => {
    if (!key.startsWith(head)) return false;
    const path = key.slice(head.length);
    return !needle || path.includes(needle) || (ventureLists && isVentureList(path));
  });
}

/** Sign-out or account switch: forget everything. */
export function cacheClear(): void {
  drop(() => true);
}

/** Whether a successful mutation on `path` may change cached data. Telemetry names no venture,
 * so without this every usage flush dropped the caller's whole cache scope. */
export const mutationInvalidates = (path: string) => !/^\/api\/telemetry/.test(path);

/** The venture id a mutation path targets, if any. */
export function ventureOf(path: string): string | undefined {
  return /\/ventures\/([^/?]+)/.exec(path)?.[1];
}
