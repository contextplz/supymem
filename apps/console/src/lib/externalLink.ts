/** Absolute http(s)/mailto URL that leaves the app origin, else null. Desktop opens these in the system browser. */
export function externalUrl(href: string | null | undefined, appOrigin: string): string | null {
  if (!href) return null;
  try {
    const u = new URL(href, appOrigin);
    if (!["http:", "https:", "mailto:"].includes(u.protocol)) return null;
    return u.protocol !== "mailto:" && u.origin === new URL(appOrigin).origin ? null : u.href;
  } catch {
    return null;
  }
}
