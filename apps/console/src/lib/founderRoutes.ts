import { isBetaHiddenPath } from "./betaRoutes";
import { isHostedOnlyPath } from "./localMode.ts";

export const FOUNDER_GLOBAL_ROUTES = [
  "/", "/new", "/account", "/guide", "/connections", "/gtm", "/download",
  "/content", "/lab", "/marketing", "/media", "/community", "/ads", "/settings/local", "/settings/local/capabilities",
] as const;
export const FOUNDER_VENTURE_ROUTES = [
  "", "idea", "context", "now", "tasks", "suggestions", "site", "research", "experiments", "market-map",
  "charter", "plan", "builds", "analytics", "settings", "guide", "org", "people", "market",
  "feedback", "evolution", "import", "connections", "simulate", "leads", "linkedin",
] as const;

const TITLES: Record<string, string> = {
  "/": "All ventures", "/new": "New venture", "/gtm": "Find customers", "": "Home",
  now: "Right now", linkedin: "LinkedIn", site: "Website", org: "Team", plan: "Execution", charter: "Plan",
  "/settings/local": "Settings",
  "/settings/local/capabilities": "Features",
};

export function founderRouteLinks(ventureId?: string, local = false) {
  const query = ventureId ? `?venture=${encodeURIComponent(ventureId)}` : "";
  const global = FOUNDER_GLOBAL_ROUTES.filter(path => local || !path.startsWith("/settings/local")).map(path => ({
    href: path + (["/gtm", "/connections", "/guide"].includes(path) ? query : ""),
    label: TITLES[path] || path.slice(1).replace(/-/g, " ").replace(/^./, letter => letter.toUpperCase()),
  }));
  const venture = ventureId ? FOUNDER_VENTURE_ROUTES.map(path => ({
    href: `/v/${encodeURIComponent(ventureId)}` + (path ? `/${path}` : ""),
    label: TITLES[path] || path.replace(/-/g, " ").replace(/^./, letter => letter.toUpperCase()),
  })) : [];
  return [...venture, ...global].filter(link => !isBetaHiddenPath(link.href, local) && (!local || !isHostedOnlyPath(link.href)));
}
