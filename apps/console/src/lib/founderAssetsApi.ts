import { api, type ApiResult } from "./client.ts";
import { checkedJourney, type GtmJourney } from "./gtmJourneyApi.ts";
import type { GtmOwner } from "./gtmDiagnosisIntent.ts";

export const FOUNDER_STAGES = { exploring: "Exploring", idea: "Idea", building: "Building", page_live: "Landing page live", outreach: "Outreach", traction: "Traction" } as const;
export type FounderStageName = keyof typeof FOUNDER_STAGES;
export type FounderStage = { stage: FounderStageName; confidence: number; evidence: Array<{ source: string; timestamp?: string; link?: string; detail?: string }>; confirmed_by_founder: boolean; updated_at: string; history: unknown[]; message?: string };
export type FounderAssets = { website: string; contacts: string; built: string; tried: string };
export type ImportedSite = { url: string; status: "verified" | "unverified"; note?: string; audit: { headline?: string; cta?: boolean | string; contact?: boolean | string; analytics?: boolean | string; fetch_ms?: number; mobile?: string; lighthouse?: string; recommendations: string[] } | null };
export type SiteImport = { site: ImportedSite | null; founder_stage: FounderStage };
export type ContactPreviewRow = { row_number: number; name: string; email: string; company: string; title?: string; linkedin_url?: string; notes?: string; status: string; last_contacted?: string; country?: string; email_check: string; errors: string[]; duplicate: boolean };
export type ContactPreview = { preview_id: string; columns: string[]; mapping: Record<string, string>; rows: ContactPreviewRow[]; total_rows: number; valid_rows: number; duplicates: number; invalid_rows: number; expires_at: string };
export type ContactImport = { imported: number; skipped: number; founder_stage: FounderStage; journey: GtmJourney; drafts_status?: "ready" | "pending"; message?: string };
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" ? value as Record<string, unknown> : {};
export function checkedFounderStage(value: unknown): FounderStage {
  const row = value as FounderStage;
  if (!row || !Object.hasOwn(FOUNDER_STAGES, row.stage) || !Number.isFinite(row.confidence) || row.confidence < 0 || row.confidence > 1
    || !Array.isArray(row.evidence) || !row.evidence.every(item => item && typeof item.source === "string")
    || typeof row.confirmed_by_founder !== "boolean" || typeof row.updated_at !== "string" || !Array.isArray(row.history))
    throw new Error("Your saved stage could not be read. Refresh and try again.");
  return row;
}
export function checkedSiteImport(value: unknown): SiteImport {
  const row = value as SiteImport;
  checkedFounderStage(row?.founder_stage);
  if (row.site !== null && (!row.site || !safeSiteUrl(row.site.url) || !["verified", "unverified"].includes(row.site.status)
    || (row.site.status === "verified" && (!Array.isArray(row.site.audit?.recommendations) || !row.site.audit.recommendations.every(item => typeof item === "string")))))
    throw new Error("Your website audit could not be read. Try checking it again.");
  return row;
}
export function safeSiteUrl(value: string): string | undefined {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; } catch { return; }
}
export function siteImportFromSearch(search: string): string | undefined { return safeSiteUrl(new URLSearchParams(search).get("import") || ""); }
export function contactsImportRequested(search: string): boolean { return new URLSearchParams(search).get("import") === "1"; }
export function checkedContactPreview(value: unknown): ContactPreview {
  const row = value as ContactPreview;
  if (!row || typeof row.preview_id !== "string" || !row.preview_id || !Array.isArray(row.columns) || !row.columns.every(item => typeof item === "string")
    || !Array.isArray(row.rows) || row.rows.length > 2000 || !row.rows.every(item => item && Array.isArray(item.errors) && item.errors.every(error => typeof error === "string") && typeof item.duplicate === "boolean")
    || ![row.total_rows, row.valid_rows, row.duplicates, row.invalid_rows].every(count => Number.isSafeInteger(count) && count >= 0)
    || !Number.isFinite(Date.parse(row.expires_at)) || !row.mapping || typeof row.mapping !== "object")
    throw new Error("The contact preview could not be read. Preview your file again.");
  return row;
}
export function createFounderAssetsClient(transport: (path: string, init?: RequestInit) => Promise<ApiResult> = api) {
  async function request(owner: GtmOwner, suffix: string, body?: unknown, method = "POST") {
    if (!owner.actorId || !owner.workspaceId || !owner.ventureId) throw new Error("Choose an authenticated venture first.");
    const result = await transport(`/api/ventures/${encodeURIComponent(owner.ventureId)}/${suffix}`, {
      method: body === undefined ? "GET" : method, headers: { "X-Workspace-ID": owner.workspaceId },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!result.ok) {
      const detail = object(object(result.data).detail), code = String(detail.code || object(detail.error).code || "");
      const copy: Record<string, string> = { founder_import_preview_expired: "This preview expired. Preview your contacts again before importing.", founder_import_consent_required: "Confirm your basis for contacting these people before importing.", asset_site_url_invalid: "Enter a public website URL starting with https://.", founder_import_too_large: "Use a CSV with at most 2,000 contacts and a file smaller than 2 MB." };
      throw new Error(copy[code] || "This action could not be confirmed. Refresh saved work and try again.");
    }
    return result.data;
  }
  return {
    stage: async (owner: GtmOwner) => checkedFounderStage(await request(owner, "founder-stage")),
    confirmStage: async (owner: GtmOwner, stage: FounderStageName) => checkedFounderStage(await request(owner, "founder-stage", { stage, confirmed: true }, "PATCH")),
    site: async (owner: GtmOwner) => checkedSiteImport(await request(owner, "assets/site")),
    importSite: async (owner: GtmOwner, url: string) => {
      if (!safeSiteUrl(url)) throw new Error("Enter a public website URL starting with https://.");
      return checkedSiteImport(await request(owner, "assets/site", { url }));
    },
    previewContacts: async (owner: GtmOwner, text: string, format: "csv" | "paste", mapping?: Record<string, string>) =>
      checkedContactPreview(await request(owner, "gtm/contacts/import/preview", { text, format, ...(mapping ? { mapping } : {}) })),
    importContacts: async (owner: GtmOwner, preview: ContactPreview, attested: boolean, lawful_basis: "consent" | "legitimate_interest", lawful_basis_note: string) => {
      if (!attested || Date.parse(preview.expires_at) <= Date.now()) throw new Error("Preview your contacts and confirm your basis for contacting them before importing.");
      const row = await request(owner, "gtm/contacts/import", { preview_id: preview.preview_id, lawful_basis_attested: true, lawful_basis, lawful_basis_note }) as ContactImport;
      checkedFounderStage(row?.founder_stage); checkedJourney(row?.journey, owner);
      if (!Number.isSafeInteger(row.imported) || row.imported < 0 || !Number.isSafeInteger(row.skipped) || row.skipped < 0) throw new Error("The import outcome could not be read. Refresh your saved contacts before retrying.");
      return row;
    },
    contactHistory: async (owner: GtmOwner, contactId: string, body: { status: "new" | "contacted" | "replied" | "interested"; last_contacted: string | null }) => {
      const row = await request(owner, `gtm/contacts/${encodeURIComponent(contactId)}/history`, body, "PATCH") as { journey: GtmJourney; founder_stage: FounderStage };
      checkedFounderStage(row?.founder_stage);
      return checkedJourney(row?.journey, owner);
    },
    verifyContactEmail: async (owner: GtmOwner, contactId: string) => {
      const row = await request(owner, `gtm/contacts/${encodeURIComponent(contactId)}/verify-email`, {}) as { journey: GtmJourney; founder_stage: FounderStage };
      checkedFounderStage(row?.founder_stage);
      return checkedJourney(row?.journey, owner);
    },
  };
}
export const founderAssetsClient = createFounderAssetsClient();
