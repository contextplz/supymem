/** Navigation selectors only; every destination must recheck account ownership. */
export type OperationsScope = { workspaceId: string; ventureId?: string; actorId?: string };

/**
 * Path venture wins. On company ops pages, the URL `?venture=` keeps shell context
 * even while OpsWorkspaceGate briefly clears its async scope.
 */
export function shellVentureId(input: {
  pathVentureId?: string | null;
  queryVentureId?: string | null;
  scopeVentureId?: string | null;
}): string | undefined {
  return input.pathVentureId || input.queryVentureId || input.scopeVentureId || undefined;
}

export function ventureQueryId(search: string | URLSearchParams | null | undefined): string | undefined {
  if (!search) return undefined;
  const params = typeof search === "string"
    ? new URLSearchParams(search.startsWith("?") ? search.slice(1) : search)
    : search;
  const value = params.get("venture")?.trim();
  return value || undefined;
}

export function isCompanyOperationsPath(pathname: string): boolean {
  return /^\/(gtm|content|connections|marketing|community|media|ops|ads)(\/|$)/.test(pathname);
}

/** Company overview is the portfolio home, never a single venture cockpit. */
export function companyOverviewHref(): string {
  return "/";
}

/** Venture Growth destinations — company surfaces keep the venture query. */
export function ventureGrowthHrefs(ventureId: string) {
  const q = encodeURIComponent(ventureId);
  return {
    gtm: `/gtm?venture=${q}`,
    content: `/content?venture=${q}`,
    community: `/community?venture=${q}`,
    connections: `/connections?venture=${q}`,
    execution: `/v/${ventureId}/plan`,
    plan: `/v/${ventureId}/charter`,
  } as const;
}

export function operationsHref(path: string, scope: OperationsScope | null): string {
  if (!scope || !/^\/(gtm|content|connections|marketing|community|media|ops|ads)(\/|\?|$)/.test(path)) return path;
  const url = new URL(path, "https://scope.invalid");
  url.searchParams.set("workspace", scope.workspaceId);
  if (scope.ventureId) url.searchParams.set("venture", scope.ventureId);
  else url.searchParams.delete("venture");
  return url.pathname + url.search + url.hash;
}

/** Only implemented company-scoped GET routes receive a venture selector. */
export function operationsReadPath(path: string, method: string, ventureId?: string): string {
  const url = new URL(path, "https://scope.invalid");
  if (url.origin !== "https://scope.invalid" || url.hash) throw new Error("Invalid operations path.");
  const supported = /^(\/tasks(?:\/[^/]+)?|\/dashboard\/summary|\/agents\/status)$/.test(url.pathname);
  if (method.toUpperCase() === "GET" && supported) {
    if (ventureId) url.searchParams.set("venture_id", ventureId);
    else url.searchParams.delete("venture_id");
  }
  return url.pathname + url.search;
}
