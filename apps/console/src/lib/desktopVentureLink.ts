/** Explicit browser-to-app handoff. The native app independently authorizes the venture. */
export function desktopVentureLink(origin: string, path: string): string | null {
  const scheme = origin === "https://app.example.invalid" ? "supymem"
    : origin === "https://app.staging.example.invalid" ? "supymem-staging" : null;
  if (!scheme || path.length > 512 || !path.startsWith("/") || path.startsWith("//") || /\.\.|[%\\\s]/.test(path)) return null;
  const url = new URL(path, origin);
  const pairs = Array.from(url.searchParams);
  const venture = "ven_[A-Za-z0-9_-]{1,124}";
  const plain = pairs.length === 0 && !url.hash;
  let valid = url.pathname === "/" && plain;
  if (["/new", "/gtm"].includes(url.pathname)) {
    valid = !url.hash && ((url.pathname === "/new" && pairs.length === 0)
      || (pairs.length === 1 && pairs[0][0] === "venture" && new RegExp(`^${venture}$`).test(pairs[0][1])));
  } else if (new RegExp(`^/v/${venture}$`).test(url.pathname)) {
    valid = !url.hash && (pairs.length === 0 || (pairs.length === 1 && pairs[0][0] === "chat" && pairs[0][1] === "1"));
  } else if (new RegExp(`^/v/${venture}/site$`).test(url.pathname)) {
    valid = !url.hash && (pairs.length === 0 || (pairs.length === 1 && pairs[0][0] === "create" && pairs[0][1] === "1"));
  } else if (new RegExp(`^/v/${venture}/builds$`).test(url.pathname)) {
    valid = (!url.hash || url.hash === "#build-review") && (pairs.length === 0 || (pairs.length === 2
      && pairs.some(([k, v]) => k === "app" && /^[A-Za-z0-9_-]{1,128}$/.test(v))
      && pairs.some(([k, v]) => k === "run" && /^[A-Za-z0-9_-]{1,128}$/.test(v))));
  } else if (new RegExp(`^/v/${venture}/(?:now|work|outreach|connections|analytics|settings|suggestions|research|idea|plan|tasks|experiments|device-work-orders/dwo_[A-Za-z0-9_-]{1,124})$`).test(url.pathname)) {
    valid = plain;
  }
  return valid ? `${scheme}://open?path=${encodeURIComponent(path)}` : null;
}

export function desktopLinkError(value: unknown): string | null {
  if (value === "sign_in") return "Connect your account in Supymem for Mac, then click Open in Supymem in your browser again.";
  if (value === "not_available") return "This venture is not available to the account connected on this Mac. Connect the account that received the email, then try again.";
  if (value === "unavailable") return "Supymem could not open this link. Finish any account request, then click Open in Supymem in your browser again.";
  return null;
}

/** Read at the user action so same-page query/hash changes cannot reuse an earlier link. */
export function desktopVentureLinkAtLocation(location: Pick<Location, "origin" | "pathname" | "search" | "hash">): string | null {
  return desktopVentureLink(location.origin, location.pathname + location.search + location.hash);
}

/** Tab-local route data for browser sign-in; never an authorization or a credential. */
export const DESKTOP_LINK_INTENT_KEY = "supymem.desktop.link.intent:v1";
type LinkStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const INTENT_TTL_MS = 15 * 60 * 1000;

function tabStorage(): LinkStorage | null {
  try { return typeof window === "undefined" ? null : window.sessionStorage; } catch { return null; }
}

export function rememberDesktopLink(origin: string, path: string, storage: LinkStorage | null = tabStorage(), now = Date.now()): void {
  if (!storage || !desktopVentureLink(origin, path) || !Number.isSafeInteger(now) || now < 0) return;
  try { storage.setItem(DESKTOP_LINK_INTENT_KEY, JSON.stringify({ version: 1, origin, path, issued_at: now })); } catch { /* Browser storage can be unavailable; the literal launch still works. */ }
}

export function takeDesktopLink(origin: string, storage: LinkStorage | null = tabStorage(), now = Date.now()): string | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(DESKTOP_LINK_INTENT_KEY);
    storage.removeItem(DESKTOP_LINK_INTENT_KEY);
    const value = JSON.parse(raw || "null");
    if (!value || Object.keys(value).sort().join(",") !== "issued_at,origin,path,version"
      || value.version !== 1 || value.origin !== origin || typeof value.path !== "string"
      || !Number.isSafeInteger(value.issued_at) || !Number.isSafeInteger(now)
      || value.issued_at < 0 || now < value.issued_at || now - value.issued_at > INTENT_TTL_MS
      || !desktopVentureLink(origin, value.path)) return null;
    return value.path;
  } catch { return null; }
}

/** The published production installer; staging never hands the user a production app. */
export function desktopDownloadLink(origin: string): string | null {
  return origin === "https://app.example.invalid" ? "https://example.invalid/downloads/Supymem.dmg" : null;
}
