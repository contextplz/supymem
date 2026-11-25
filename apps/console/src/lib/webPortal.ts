/** Presentation boundary only. API authorization remains server-enforced. */
export function isWebPortalPath(pathname: string): boolean {
  return ["/download", "/privacy", "/terms"].includes(pathname) || pathname.startsWith("/invite/")
    || ["/auth", "/admin"].some(base => pathname === base || pathname.startsWith(`${base}/`));
}
