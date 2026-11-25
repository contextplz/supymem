import { errorCodeCopy } from "./errorCopy.ts";
/** Keep a revision run scoped to its app when a venture has multiple products. */
export function revisionBuildHref(ventureId: string, appId: string, runId: string): string {
  return `/v/${encodeURIComponent(ventureId)}/site?app=${encodeURIComponent(appId)}&run=${encodeURIComponent(runId)}#build-review`;
}

/** "" means nothing to show: the page is not published yet, so there is nothing to revise. */
export function revisionLoadFailure(code: string | null, _status: number): string {
  if (code === "hosting_live_static_site_required") return "";
  return "Couldn’t load the published-page revision controls. Refresh and try again.";
}

/** The write contract accepts only these fields; GET baselines carry more evidence. */
export function revisionRequestBody(
  baseline: { deployment_id: string; source_sha256: string },
  instruction: string,
): { deployment_id: string; source_sha256: string; instruction: string } {
  return {
    deployment_id: baseline.deployment_id,
    source_sha256: baseline.source_sha256,
    instruction: instruction.trim(),
  };
}

export function revisionSaveFailure(status: number, code: string | null): string {
  return errorCodeCopy(code) ?? (status === 422
    ? "Supymem could not validate the revision request. Refresh the page and try again."
    : "Couldn’t save the revision request. Refresh the page and try again.");
}
