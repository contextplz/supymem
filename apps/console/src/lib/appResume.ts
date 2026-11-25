import { chatFirstHidden } from "./chatVisibility.ts";

function resumeHref(value: string): string | null {
  try {
    if (!value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u0020]/.test(value)) return null;
    const url = new URL(value, "https://console.invalid");
    if (url.origin !== "https://console.invalid" || !/^(?:\/new(?:\/|$)|\/v\/[^/]+(?:\/|$)|\/gtm$)/.test(url.pathname)) return null;
    if (url.pathname === "/gtm" && !url.searchParams.get("venture")) return null;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch { return null; }
}

export function appRouteVentureId(href: string): string | null {
  const route = resumeHref(href);
  if (!route) return null;
  try {
    const url = new URL(route, "https://console.invalid");
    const pathVenture = /^\/v\/([^/]+)/.exec(url.pathname)?.[1];
    return pathVenture ? decodeURIComponent(pathVenture) : url.searchParams.get("venture");
  } catch { return null; }
}

export function forgetAppRoute(storage: Pick<Storage, "getItem" | "removeItem"> | null, subject: string | null, ventureId: string): void {
  if (!subject) return;
  try {
    const key = `supymem.lastRoute.v1:${encodeURIComponent(subject)}`;
    if (appRouteVentureId(storage?.getItem(key) || "") === ventureId) storage?.removeItem(key);
  } catch { /* A denied storage read must not prevent navigation. */ }
}

export function resumeAppRoute(storage: Pick<Storage, "getItem" | "setItem"> | null, subject: string | null, href: string, firstNavigation: boolean, founderUI = "classic", local = false): string | null {
  if (!subject) return null;
  try {
    const key = `supymem.lastRoute.v1:${encodeURIComponent(subject)}`;
    const route = resumeHref(href);
    if (route) storage?.setItem(key, route);
    if (firstNavigation && href === "/") {
      const saved = resumeHref(storage?.getItem(key) || "");
      if (saved && founderUI !== "classic" && chatFirstHidden(saved, local)) {
        const venture = appRouteVentureId(saved);
        return venture ? `/v/${encodeURIComponent(venture)}` : null;
      }
      return saved;
    }
  } catch { /* A denied storage read must not prevent navigation. */ }
  return null;
}
