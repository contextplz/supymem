export function isLocalConsole(): boolean {
  return process.env.NEXT_PUBLIC_SUPYMEM_MODE?.trim().toLowerCase() === "local"
    || (typeof window !== "undefined" && window.__SUPYMEM__?.mode === "local");
}

export function isHostedOnlyPath(pathname: string): boolean {
  const path = pathname.split(/[?#]/, 1)[0];
  const hosted = ["/admin", "/invite", "/invites", "/invitations", "/join", "/welcome", "/download", "/auth", "/build", "/builds",
    "/builder", "/product-builder", "/lab/diagrams", "/pagebuilder-lab"];
  return hosted.some(base => path === base || path.startsWith(`${base}/`))
    || /^\/v\/[^/]+\/(?:invite|invites|invitations|people|build|builder|product-builder|import|settings)(?:\/|$)/.test(path);
}

export function localHomeHref(pathname: string, ventureId?: string | null): string {
  const venture = /^\/v\/([^/?#]+)/.exec(pathname)?.[1];
  const home = venture ? `/v/${venture}` : ventureId ? `/v/${encodeURIComponent(ventureId)}` : "/";
  return `${home}?local_notice=1`;
}
