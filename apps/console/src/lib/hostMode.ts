export type HostMode = "desktop" | "portal" | "admin" | "marketing";

export type SupymemRuntime = {
  mode: "hosted" | "local";
  gatewayUrl: string;
  bridgeUrl: string;
  bridgeToken: string;
  version: string;
};

declare global {
  interface Window {
    __SUPYMEM__?: SupymemRuntime;
  }
}

export function resolveHostMode(host: string, hasTauri: boolean): HostMode {
  if (hasTauri) return "desktop";
  const h = (host || "").toLowerCase().split(":")[0];
  if (h.startsWith("admin.")) return "admin";
  if (h.startsWith("app.")) return "portal";
  if (h === "supymem.com" || h.startsWith("www.")) return "marketing";
  return "desktop";
}

/** Apex waitlist, /welcome preview, or ?marketing=1. Desktop shell never uses this. */
export function isMarketingSurface(
  host: string,
  path = "/",
  search = "",
  hasTauri = false,
): boolean {
  if (hasTauri) return false;
  if (resolveHostMode(host, false) === "marketing") return true;
  if (path === "/welcome" || path.startsWith("/welcome/")) return true;
  try {
    return new URLSearchParams(search).get("marketing") === "1";
  } catch {
    return false;
  }
}
