/** The guided idea journey keeps chat closed by default; the header launcher still opens it. */
export function isIdeaJourneyPath(pathname: string): boolean {
  return /^\/v\/[^/]+\/idea\/?$/.test(pathname);
}

/** Reading pages support brief/detail; chat and action pages always use full text. */
export function showsDetailToggle(pathname: string): boolean {
  return /^\/v\/[^/]+(?:\/(?:site|research|plan|idea))?\/?$/.test(pathname);
}

/**
 * Chat-first (founder_ui "home"): these screens keep their code and routes, but founders are sent to
 * Charlie's chat instead. The "classic" founder_ui switch still shows them.
 * Kept reachable: the company tabs (/v/<id>?office=…), idea/current, linkedin, connect (OAuth return) and
 * device-work-orders (Mac reviews opened from chat).
 */
export const CHAT_FIRST_HIDDEN_SEGMENTS = [
  "now", "work", "outreach", "tasks", "plan", "charter", "research", "site", "builds", "analytics", "experiments", "suggestions",
  "context", "settings", "org", "people", "market", "feedback", "evolution", "import", "simulate", "connections",
] as const;

export function isLocalWorkPage(href: string): boolean {
  return /^\/v\/[^/?#]+\/(?:builds|experiments|research)\/?(?:[?#]|$)/.test(href);
}

export function chatFirstHidden(href: string, local = false): boolean {
  if (local && isLocalWorkPage(href)) return false;
  const path = href.split(/[?#]/, 1)[0];
  if (isIdeaJourneyPath(path)) return true;
  if (path === "/gtm" || path.startsWith("/gtm/")) return true;
  const m = /^\/v\/[^/]+\/([^/]+)/.exec(path);
  return !!m && (CHAT_FIRST_HIDDEN_SEGMENTS as readonly string[]).includes(m[1]);
}
