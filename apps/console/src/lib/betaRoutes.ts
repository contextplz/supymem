import { isLocalConsole } from "./localMode.ts";

/**
 * Single source of truth for surfaces that are not in the limited beta.
 * SideNav hides them and middleware.ts 404s them, so a typed URL cannot reach them.
 * Global paths match the prefix; venture paths match /v/<id>/<segment>.
 * Browser computer-use reviews (/v/<id>/device-work-orders/*) are reachable; native Mac control stays off server-side.
 * /ops/* is hidden; /admin/* is operator-gated separately and is not listed.
 */
export const BETA_HIDDEN_GLOBAL = [
  "/lab", "/content", "/marketing", "/community", "/media", "/ads", "/ops", "/invite", "/setup/permissions",
] as const;

export const BETA_HIDDEN_VENTURE_SEGMENTS = [
  "org", "evolution", "simulate", "import", "people", "market", "feedback", "connections", "connect",
] as const;

export function isBetaHiddenPath(pathname: string, local = isLocalConsole()): boolean {
  const path = pathname.split("?", 1)[0];
  if (local && (path === "/content" || path.startsWith("/content/"))) return false;
  if (BETA_HIDDEN_GLOBAL.some((p) => path === p || path.startsWith(`${p}/`))) return true;
  const m = /^\/v\/[^/]+\/([^/]+)/.exec(path);
  return !!m && (BETA_HIDDEN_VENTURE_SEGMENTS as readonly string[]).includes(m[1]);
}
