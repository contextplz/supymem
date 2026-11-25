/** First-look drafts use the authenticated JSON preview, never a public iframe. */
export function firstlookPreviewPath(src: string, ventureId?: string): string | null {
  if (/^\/api\/ventures\/[^/?#\s\\]+\/pagebuilder\/jobs\/[^/?#\s\\]+\/preview(?:\?variant=[0-2])?$/.test(src)) {
    return src.replace(/\/preview(?=\?|$)/, "/preview-document");
  }
  let url: URL;
  try {
    url = new URL(src, "https://preview.invalid");
  } catch {
    return null;
  }
  const match = /^\/w\/([A-Za-z0-9_-]+)\/site\/(?:index\.html)?$/.exec(url.pathname);
  if (!match) return null;
  const version = url.searchParams.get("v");
  if (version !== null && !/^\d+$/.test(version)) return null;
  const query = new URLSearchParams();
  if (version !== null) query.set("v", version);
  if (ventureId !== undefined) query.set("venture_id", ventureId);
  return `/api/workspaces/${match[1]}/site/preview${query.size ? `?${query}` : ""}`;
}

export function ventureSitePath(workspaceId: string, ventureId: string): string {
  return `/api/workspaces/${encodeURIComponent(workspaceId)}/site?${new URLSearchParams({ venture_id: ventureId })}`;
}

export function scopedSiteMatches(value: unknown, workspaceId: string, ventureId: string): boolean {
  if (!value || typeof value !== "object") return false;
  const site = value as { workspace_id?: unknown; venture_id?: unknown };
  return Boolean(workspaceId && ventureId && site.workspace_id === workspaceId && site.venture_id === ventureId);
}

/** Static private drafts grant no script, navigation, form, or origin sandbox exceptions. */
export function firstlookPreviewSandbox(previewPath: string | null, fallback: string): string {
  return previewPath ? "" : fallback;
}
