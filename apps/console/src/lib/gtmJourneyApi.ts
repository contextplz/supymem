import { api, type ApiResult } from "./client.ts";
import { usageLimitMessage } from "./credits.ts";
import type { GtmOwner } from "./gtmDiagnosisIntent.ts";
import type { ResearchProgress } from "./durableJobs.ts";
import { checkedGmailMailboxes, type GmailMailbox } from "./gmailMailboxes.ts";
import { isLocalConsole } from "./localMode.ts";
import { apiFailure } from "./apiFailure.ts";
import { errorCodeCopy } from "./errorCopy.ts";

export type JourneySignal = { kind: string; quote: string; source_url: string; published_at: string | null; observed_at: string; confidence: "observed" | "inferred"; evidence_label?: "possible_fit"; company_evidence_url: string | null; why_now: string; author?: string; author_url?: string; draft?: string; recency?: "recent" | "older" | "unknown"; date_status?: "published" | "approximate" | "unknown" };
export type JourneyChannel = { name: string; url: string; source_url?: string; quote: string; why_fits: string; participation: string; draft: string; rules_url?: string; contact_url?: string | null; channel_role?: string };
export type JourneyProspect = { id: string; name: string; company?: string; email?: string; email_route?: ReachRoute | null; source_url?: string; why?: string; why_now?: string; confidence?: "observed" | "inferred"; signals?: JourneySignal[]; signal?: JourneySignal; website?: string; score?: number; person?: string; person_role?: string; person_source_url?: string; person_quote?: string; contact_source_url?: string; contact_quote?: string; email_status?: "published" | "none"; evidence_label?: string; blocked_reason?: string | null; country?: string; country_source?: string | null; skipped?: boolean; imported?: boolean; status?: string; last_contacted?: string; continuation_action?: string; email_check?: string };
export type JourneyDraft = { doc_id: string; contact_id: string | null; to: string; subject: string; body: string; state: string; batch_id: string; review_state: string; revision: string; channel?: string; continuation?: string; day?: number; kind?: string; awaiting_recipient?: boolean; channel_url?: string | null; source_url?: string | null; founder_import?: boolean; email_route?: ReachRoute | null; mailbox_id?: string | null; sender?: { mailbox_id: string; email: string }; sender_locked?: boolean };
export type JourneyDraftEdits = { subject?: string; body?: string; mailbox_id?: string };
export type JourneyGeoOption = { code: string; note_required: boolean; default_basis: "consent" | "legitimate_interest" };
export type ProspectConfirmation = { contact_id: string; country?: string; lawful_basis?: string; lawful_basis_note?: string; skip?: boolean };
export type JourneyBatch = { batch_id: string; approved_count: number; approval_digest: string; cap?: number; credit_status?: string; credit_estimate_micro?: number | null; batch_estimate_micro?: number | null };
export type ReachRoute = { kind: "booking" | "email" | "contact_form" | "linkedin" | "x" | "forum" | "phone" | "website"; url?: string; email?: string; published_as?: string; email_kind?: "named_person" | "business_role" | "published_business"; route_type?: "published" | "pattern" | "guess"; confidence?: number; confidence_label?: string; pattern?: string; mx_status?: "mx" | "unchecked"; value?: string; source_url: string; quote: string; display_quote?: string; profile?: boolean; company_contact?: boolean };
export type ReachableLead = { expert_role?: string; lead_type?: "customer" | "insight" | "unclassified"; judging_status?: "judged" | "pending"; evidence_label?: string; signals?: Array<{ quote: string; source_url: string; confidence: string; recency: string; published_at?: string | null }>; id: string; name: string; company: string; role: string; location: string; fit: number; why: string; value_exchange?: string; handle?: string; profile_url?: string; quote: string; display_quote?: string; source_url: string; routes: ReachRoute[]; person?: string; person_role?: string; draft: string; connection_note: string; x_note?: string; draft_source?: "model" | "template"; draft_status?: string; outreach_hint?: { country: string; subscriber: string; note: string; source_url?: string }; registry?: { attribution?: string; registry_url?: string; url?: string; provider?: string }; linkedin_search?: string | null; people?: Array<{ name: string; role: string; company: string; source_url: string; quote: string; email?: string | null; linkedin_search: string }>; playbook_messages?: Array<{ playbook: string; title: string; subject: string; email: string; connection_note: string; linkedin_dm: string }> };
export type OutreachPlaybook = { key: string; title: string; why: string };
export type GtmJourney = {
  manual_outreach?: Array<{ id: string; name: string; source_url?: string; sent_at: string; booked_at: string | null }>;
  people_search_notice?: string;
  research_progress?: ResearchProgress | null; discovery_notice?: string | null;
  reachable_leads?: ReachableLead[];
  playbooks?: OutreachPlaybook[];
  industry_experts?: ReachableLead[];
  sending_alert?: string | null;
  sending_pause?: { sent: number; bounced: number; send_resumed_at: string | null } | null;
  venture_name?: string; discovery_started?: boolean; discovery_note?: string; discovery_summary?: string;
  venture_id: string; state: string; phase: string; job_id?: string | null; error_code?: string | null; message?: string;
  icp: { who: string; suggested_who?: string; problem?: string; problem_basis?: string; known_checks?: { audience?: KnownCheck; problem?: KnownCheck }; fact_confirmations?: { audience?: string; problem?: string }; basis: string; confirmed: boolean; needs_input?: boolean; search_queries?: string[]; mode?: "consumer" | "company"; goal?: string };
  channels?: JourneyChannel[]; need_signals?: JourneySignal[]; source_access?: string; recruitment_opt_in?: boolean;
  customer_start?: { referral_draft: string; review_draft: string; lookalikes_note: string } | null;
  will_search?: boolean; geo_options?: JourneyGeoOption[];
  prospects: JourneyProspect[]; drafts: JourneyDraft[]; batches: JourneyBatch[];
  results: { sent: number; replied: number; interested: number; negative: number; replies: Array<{ id?: string; contact_id?: string; manual?: boolean; from?: string; body?: string; classification?: string; outcome?: string; received_at?: string }> };
  gmail: { connected: boolean; sender?: string; multi_gmail_v1?: boolean; mailboxes?: GmailMailbox[] }; daily_cap: number; allow_sending?: boolean;
  can_send?: boolean; send_blocked_reason?: string | null; can_prepare?: boolean;
  sender_postal_address?: string | null;
  simulation_enabled?: boolean;
  processing_blocked_reason?: string | null;
  receipt?: { kind: string; body_md: string };
  estimate: { ceiling_micro: number; basis: string };
  booking_link?: string | null;
  waitlist_signups?: number;
  contact_work?: Record<string, { brief?: string | null; call_notes?: string | null; learned?: boolean; referral_ask?: string | null; follow_up?: string | null; reply_attention?: string | null }>;
  learned?: { calls: number; pains_confirmed: string[]; pains_refuted: string[]; objections: string[]; would_pay: number };
  next_step?: { kind: "talk" | "experiment"; text: string };
};
export type ReplyOutcome = "interested" | "not_now" | "not_a_fit" | "opted_out" | "wrong_person" | "question" | "escalate";
export type ContactAction = "reply" | "reply-draft" | "brief" | "call-notes" | "sent" | "call-booked";

export function journeySendReason(code: string | null | undefined): string {
  if (code?.endsWith("insufficient_credits")) return usageLimitMessage();
  return code ? journeyErrorCopy(code) || "Sending needs attention. Review your mailbox and contact eligibility in Find customers, then try again." : "";
}

function journeyErrorCopy(code: string): string | undefined {
  if (isLocalConsole()) {
    if (["gtm_gmail_missing", "gtm_gmail_required"].includes(code)) return "Open Keys and connections > Email and save your outgoing mail host, port, username and app password, then try sending your reviewed emails again.";
    if (code === "outbound_execution_disabled") return "Turn on email sending in Find customers, or turn on Send email in Capabilities. If setup is needed, open Keys and connections > Email and save your mailbox settings. Your drafts stay saved; nothing was sent.";
    if (code.startsWith("outbound_rate_")) return "Open Keys and connections > Email and check your outgoing mail settings, then return to Find customers and refresh your saved batch before retrying. Your drafts stay saved.";
    if (code === "gtm_booking_link_required") return "Add your HTTPS scheduling page in Keys and connections > Email > Booking link, then draft your reply again.";
    if (code === "gtm_booking_link_invalid") return "Use a full https link in Keys and connections > Email > Booking link, then save it and draft your reply again.";
    if (code === "gtm_send_blocked") return "Open Find customers and review your saved batch, daily sending limit and contact eligibility before retrying. For sample contacts, copy the invitation instead; sample emails are never sent.";
    if (code === "gtm_llm_unavailable") return errorCodeCopy(code);
    if (["company_delivery_unconfirmed", "gtm_reply_thread_unconfirmed"].includes(code)) return "Check this contact's conversation in your mailbox and refresh its saved status in Find customers before retrying. Do not repeat an unconfirmed send.";
  }
  return copy[code] || (code.startsWith("outbound_rate_") ? copy.outbound_rate_unconfigured : undefined);
}

const copy: Record<string, string> = {
  send_paused: "Workspace sending is paused. Check bounced addresses first, then click Resume sending; your drafts stay saved.",
  gtm_sender_address_required: "Add your business mailing address in Sending address for your footer, then click Save sending address. Your drafts stay saved.",
  gtm_recipient_consent_required: "Record this recipient’s consent and how they agreed in their country/permission details before sending. Cold email to UK sole traders, ordinary partnerships or consumers is blocked.",
  gtm_sender_identity_required: "Keep your founder name or team identity in the email, then review and approve the exact text again.",
  gtm_bounce_review_required: "Check the bounced addresses first, then confirm that review before resuming sending.",
  dpa_required: "Review and accept your workspace data processing agreement in Find customers before resuming sending.",
  company_contact_hold: "Wait five business days after contacting their colleague, or choose a different company.",
  company_contact_limit: "Three people at this company have already been contacted. Skip this contact and choose another company.",
  company_delivery_unconfirmed: "A colleague’s delivery is still unconfirmed. Check the saved send in Gmail; do not send to another person here yet.",
  follow_up_not_due: "Respect their requested follow-up date. Review the saved reply, or choose another contact meanwhile.",
  gtm_draft_changed: "This message changed. Refresh saved work and review the current message before deciding.",
  gtm_approval_changed: "This batch changed. Review the current drafts before sending.",
  gtm_stale_approval: "This batch changed. Review the current drafts before sending.",
  gtm_gmail_missing: "Connect Gmail before sending approved emails.",
  gtm_gmail_required: "Connect Gmail before sending approved emails.",
  outbound_execution_disabled: "Email sending is not switched on for your workspace yet. Your drafts are saved.",
  gtm_send_not_allowed: "Allow sending for this venture before approving a batch.",
  gtm_allow_sending_required: "Allow sending for this venture before approving a batch.",
  outbound_rate_unconfigured: "Email sending is not ready for your workspace yet. Your drafts are saved.",
  gtm_source_url_invalid: "Use a public company source URL. Private addresses and social profile scraping are not supported.",
  gtm_email_invalid: "Enter a valid email address from your source.",
  gtm_contact_already_in_workspace: "This contact already belongs to another venture in this workspace. Review the original venture’s outreach.",
  gtm_draft_honesty_or_length: "Keep the message under 120 words and remove unsupported claims or personal praise.",
  gtm_draft_recipient_required: "Add an evidence-backed prospect before approving this email. Nothing was sent.",
  gtm_draft_one_ask_and_optout_required: "Use one question and keep the original no-thanks opt-out sentence in the message.",
  gtm_source_disclosure_required: "Keep the original Source URL in the message so the recipient can see where you found their contact information.",
  gtm_sender_address_changed: "Update each draft with your current mailing address and approve the messages again.",
  gtm_prospect_blocked: "This contact is no longer eligible. Review their source, country and permission to contact.",
  gtm_preparation_in_progress: "Background preparation is running. Wait for it to finish before changing the audience.",
  gtm_search_queries_invalid: "Use eight bounded signal searches, or one to three short search phrases.",
  gtm_booking_link_invalid: "Use a full https link, such as your Cal.com or Calendly page.",
  gtm_booking_link_required: "Add your booking link in Settings first, then draft the reply.",
  gtm_reply_invalid: "Paste what they wrote and choose how they responded.",
  gtm_reply_needs_sent_message: "Log a reply after the message to this person has been sent.",
  gtm_reply_thread_unconfirmed: "This reply could not be matched to the message you sent. Check the conversation in Gmail.",
  gtm_reply_not_interested: "This is only available for people you marked as interested.",
  gtm_llm_unavailable: "Your draft could not be written just now. Your input is saved. Check your connection in Settings, then try again.",
  gtm_call_notes_required: "Add your call notes first.",
  gtm_country_unsupported: "We cannot send to that country yet. Pick a supported country, or skip this prospect.",
  gtm_lawful_basis_note_required: "This country needs a note on how the contact agreed to hear from you. Add it, or skip this prospect.",
  gtm_invalid_input: "Choose a country or skip this prospect.",
  gtm_send_in_progress: "A send is already in progress. Refresh its saved status before trying again.",
};
function object(value: unknown): Record<string, unknown> { return value && typeof value === "object" ? value as Record<string, unknown> : {}; }
export function journeyReviewRequested(ventureId: string, search: string): boolean {
  const params = new URLSearchParams(search);
  return params.get("venture") === ventureId && params.get("step") === "review";
}

export function journeyInitialStage(journey: GtmJourney, search: string): number {
  if (journeyReviewRequested(journey.venture_id, search)) return 2;
  return !journey.icp.confirmed ? 0 : journey.manual_outreach?.length ? 4 : journey.icp.mode === "consumer" ? 1 : journey.results.sent > 0 ? 4 : journey.drafts.length ? 2 : 1;
}

export function checkedJourney(value: unknown, owner: GtmOwner): GtmJourney {
  const row = value as GtmJourney;
  if (!row || row.venture_id !== owner.ventureId || !row.icp || !Array.isArray(row.prospects) || !Array.isArray(row.drafts)
    || !Array.isArray(row.batches) || !row.gmail || !row.results || !Array.isArray(row.results.replies)
    || !row.estimate || !Number.isFinite(row.daily_cap)
    || !row.drafts.every(d => d && typeof d.revision === "string" && /^[a-f0-9]{64}$/.test(d.revision))) throw new Error("The GTM response did not match this venture. Refresh to verify its saved work.");
  if (row.gmail.multi_gmail_v1 === true) checkedGmailMailboxes({ ...row.gmail, workspace_id: owner.workspaceId }, owner.workspaceId);
  return row;
}
export function safeEvidenceUrl(value: string | undefined): string | undefined {
  if (!value) return;
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && url.hostname !== "invalid" && !url.hostname.endsWith(".invalid") ? url.href : undefined; } catch { return; }
}
export function createJourneyClient(transport: (path: string, init?: RequestInit) => Promise<ApiResult> = api) {
  async function request(owner: GtmOwner, suffix = "", body?: unknown, method = "POST"): Promise<GtmJourney> {
    if (!owner.actorId || !owner.workspaceId || !owner.ventureId) throw new Error("Choose an authenticated venture first.");
    const result = await transport(`/api/api/v1/ventures/${encodeURIComponent(owner.ventureId)}/gtm/journey${suffix}?workspace_id=${encodeURIComponent(owner.workspaceId)}`, {
      method: body === undefined ? "GET" : method, headers: { "X-Workspace-ID": owner.workspaceId },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!result.ok) {
      const data = object(result.data), detail = object(data.detail);
      const code = String(object(data.error).code || detail.code || detail.blocked || (typeof data.detail === "string" ? data.detail : ""));
      if (result.status === 402 || code.endsWith("insufficient_credits")) throw new Error(usageLimitMessage());
      if ([408, 504].includes(result.status) || code === "upstream_timeout" || /timed?\s*out|timeout/i.test(result.error || "")) {
        throw new Error(body === undefined ? "Find customers took too long to load. Your saved work is unchanged; retry loading."
          : "This action timed out and could not be confirmed. Refresh saved work before retrying; it may already have completed.");
      }
      const recovery = isLocalConsole() && typeof detail.message === "string" ? detail.message.trim() : "";
      throw new Error(recovery || journeyErrorCopy(code) || (result.status && result.status < 500
        ? apiFailure(result, "your Find customers request")
        : "This action could not be confirmed. Refresh saved work, then retry."));
    }
    return checkedJourney(result.data, owner);
  }
  return {
    get: (owner: GtmOwner) => request(owner),
    prepare: (owner: GtmOwner, retry = false, note?: string, recruitmentOptIn = false) => request(owner, "/prepare", { retry, ...(note === undefined ? {} : { note }), ...(recruitmentOptIn ? { recruitment_opt_in: true } : {}) }),
    icp: (owner: GtmOwner, who: string, search_queries?: string[], note?: string, recruitmentOptIn = false, problem?: string) => request(owner, "/icp", { who, ...(search_queries ? { search_queries } : {}), ...(note === undefined ? {} : { note }), ...(recruitmentOptIn ? { recruitment_opt_in: true } : {}), ...(problem === undefined ? {} : { problem }) }, "PATCH"),
    review: (owner: GtmOwner, draft: JourneyDraft, decision: "approved" | "skipped" | "draft", edits?: JourneyDraftEdits) => {
      if (edits?.mailbox_id !== undefined && (decision !== "draft" || draft.state !== "draft" || draft.sender_locked || draft.kind === "reply" || draft.day === 3 || draft.day === 6)) {
        throw new Error("Changing your sender needs a new draft review. Sent messages and existing conversations keep their original inbox.");
      }
      return request(owner, `/drafts/${encodeURIComponent(draft.doc_id)}`, { expected_revision: draft.revision, decision, ...edits }, "PATCH");
    },
    prospect: (owner: GtmOwner, body: { name: string; email: string; company: string; source_url: string; country: string; lawful_basis: string; lawful_basis_note: string }) => request(owner, "/prospects", body),
    eligibility: (owner: GtmOwner, contactId: string, body: { country?: string; lawful_basis?: string; lawful_basis_note?: string; skip?: boolean }) => request(owner, `/prospects/${encodeURIComponent(contactId)}`, body, "PATCH"),
    confirmProspects: (owner: GtmOwner, items: ProspectConfirmation[]) => request(owner, "/prospects/confirm", { items }),
    settings: (owner: GtmOwner, allow_sending: boolean) => request(owner, "/settings", { allow_sending }, "PATCH"),
    senderAddress: (owner: GtmOwner, sender_postal_address: string) => request(owner, "/settings", { sender_postal_address }, "PATCH"),
    policy: (owner: GtmOwner, body: { aup_accepted?: boolean; dpa_accepted?: boolean }) => request(owner, "/settings", body, "PATCH"),
    contact: async (owner: GtmOwner, contactId: string, action: ContactAction, body: { text?: string; outcome?: ReplyOutcome } = {}) => {
      if (/^https?:\/\//.test(contactId)) {
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(contactId));
        contactId = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("").slice(0, 24);
      }
      return request(owner, `/prospects/${encodeURIComponent(contactId)}/${action}`, body);
    },
    askSending: (owner: GtmOwner) => request(owner, "/ask-sending", {}),
    resumeSending: (owner: GtmOwner) => request(owner, "/resume-sending", { reviewed_bounced_addresses: true }),
    bookingLink: (owner: GtmOwner, booking_link: string) => request(owner, "/settings", { booking_link }, "PATCH"),
    send: (owner: GtmOwner, batch: JourneyBatch, draftIds: string[], cap: number, selfOnly = false) => request(owner, "/send", { batch_id: batch.batch_id, draft_ids: draftIds, approval_digest: batch.approval_digest, cap, ...(selfOnly ? { self_only: true } : {}) }),
  };
}
export const journeyClient = createJourneyClient();
import type { KnownCheck } from "@/components/chat/KnownAnswer";
