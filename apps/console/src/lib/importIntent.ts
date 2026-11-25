/** A retained intake request never moves to another account, company or app. */
export type ImportOwner = { actor: string; workspace: string; venture: string; app: string };
export type ImportSource = { source: "github_public"; repo_url: string; ref?: string; subdirectory?: string } |
  { source: "upload_zip"; filename: string; size_bytes: number; sha256: string; root?: string };
export type ImportIntent = { idempotency_key: string; source: ImportSource };
export const importIntentKey = (owner: ImportOwner) => `supymem.import.intent:v1:${JSON.stringify([owner.actor, owner.workspace, owner.venture, owner.app])}`;
export const saveImportIntent = (owner: ImportOwner, request: ImportIntent) => JSON.stringify({ version: 1, owner, request });
export function readImportIntent(raw: string | null, owner: ImportOwner): ImportIntent | null {
  try {
    const value = JSON.parse(raw || "null");
    if (value?.version !== 1 || importIntentKey(value.owner) !== importIntentKey(owner) || typeof value.request?.idempotency_key !== "string") return null;
    if (!["github_public", "upload_zip"].includes(value.request?.source?.source)) return null;
    return value.request;
  } catch { return null; }
}
export const activeImport = (state: string) => ["uploading", "queued", "acquiring", "validating", "analyzing", "cancel_requested"].includes(state);
export function importState(state: string) {
  const labels: Record<string, string> = { deleted: "Source access removed", awaiting_upload: "Waiting for your ZIP", uploading: "Receiving source", queued: "Queued for analysis", acquiring: "Downloading pinned source", validating: "Checking archive safety", analyzing: "Reading project metadata", report_complete: "Static report ready", partial_report: "Report ready with gaps", blocked: "Source needs attention", failed_definitive: "Analysis stopped", needs_reconciliation: "Checking storage outcome", cancelled: "Cancelled", expired: "Upload window expired", cancel_requested: "Reconciling before cancellation" };
  return labels[state] || "Status unavailable";
}
