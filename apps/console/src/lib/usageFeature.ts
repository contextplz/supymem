/** Canonical feature labels only; never emit pathname IDs, queries or DOM text. Values are slugs of the
 *  server feature catalog (featureCatalog.test.ts checks every page route maps to one). */
const ventureFeatures: Record<string, string> = {
  "": "ventures", idea: "idea", context: "onboarding", research: "research", market: "research", plan: "planning",
  execution: "execution", builds: "builds", build: "builds", website: "website", site: "website", analytics: "analytics",
  charter: "planning", tasks: "execution", org: "team", people: "team", connections: "settings",
  connect: "settings", evolution: "execution", simulate: "simulate",
  "go-to-market": "gtm", gtm: "gtm", content: "content", community: "community",
  settings: "settings", team: "team", feedback: "feedback", import: "import", work: "execution", now: "execution",
  leads: "gtm", linkedin: "outreach", "device-work-orders": "computer_use", experiments: "experiments", suggestions: "suggestions", guide: "settings",
  "market-map": "experiments",
};
const sharedFeatures: Record<string, string> = {
  "/pagebuilder-lab": "website",
  "/operator": "account",
  "/guide": "settings",
  "/settings/local": "settings", "/settings/local/capabilities": "settings", "/settings/local/support": "settings", "/setup": "onboarding",
  "/new": "onboarding", "/welcome": "onboarding", "/lab": "idea",
  "/gtm": "gtm", "/gtm/lab": "gtm", "/content": "content", "/community": "community",
  "/connections": "settings", "/marketing": "gtm", "/media": "content", "/ads": "gtm",
  "/ops": "execution", "/ops/agents": "team", "/ops/tasks": "execution", "/ops/settings": "settings",
  "/ops/ads": "gtm", "/ops/ai-gtm": "gtm", "/ops/outreach": "outreach", "/ops/social": "gtm",
  "/ops/finance": "payments", "/ops/memory": "execution",
  "/setup/permissions": "permissions", "/download": "account", "/suggestions/overlay": "suggestions",
};
export function usageFeature(pathname: string): string | null {
  if (sharedFeatures[pathname]) return sharedFeatures[pathname];
  if (pathname === "/ventures" || pathname === "/") return "ventures";
  if (pathname === "/settings") return "settings";
  if (pathname === "/account") return "account";
  if (pathname === "/auth" || pathname.startsWith("/auth/")) return "account";
  if (pathname.startsWith("/invite/")) return "team";
  const parts = pathname.split("/");
  if (parts[1] !== "v" || !parts[2]) return null;
  return ventureFeatures[parts[3] || ""] || null;
}

/** Unmapped desktop screens still need a closed label for sanitized failures. */
export function diagnosticUsageFeature(pathname: string): string {
  return usageFeature(pathname) || "shell";
}
