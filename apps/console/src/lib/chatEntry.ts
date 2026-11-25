import type { FounderAssets, FounderStage } from "./founderAssetsApi.ts";
import { editableIdeaAnswer, founderChoiceWords } from "./ideaAnswer.ts";
import { usageLimitMessage } from "./credits.ts";
import { clearChatDraftIfMatches, readChatDraft, writeChatDraft } from "./chatComposer.ts";

export function founderWords(text: string, previous = ""): string {
  if (previous && text.startsWith(previous + "\n")) text = text.slice(previous.length + 1);
  return founderChoiceWords(editableIdeaAnswer(text.replace(/^(?:Audience|Clarification|Context):[ \t]*/gim, "")));
}

export type EntryRoute = "startup" | "gtm" | "personal_build";
export type EntryProposal = {
  files?: Array<{ evidence_id: string; source_label: string; size: number; status: "read" }>;
  id: string;
  route: EntryRoute | "clarify";
  message: string;
  button_label?: string;
  venture_name?: string;
  website_answers?: Record<string, string>;
  intake_path?: string;
  skipped_steps: string[];
  next_step: "idea" | "gtm" | "build" | "clarify";
  target_venture_id?: string;
  expires_at: string;
  state?: "classifying" | "pending" | "committing" | "committed" | "cancelled" | "clarify";
  choices?: string[];
  onboarding_question?: string | null;
  onboarding_choices?: string[];
  onboarding_field?: "offer" | "customer" | "context" | null;
  receipt?: EntryCommit | null;
  asset_questions?: Array<{ id: keyof FounderAssets; question: string; recommended: string; skip: boolean; value?: string }>;
  asset_prefill?: Partial<FounderAssets>;
  founder_stage?: FounderStage;
};
export type EntryPreview =
  | { status: "reply"; body_md: string; actions: Array<{ label: string; href: string; style: "primary" }> }
  | { status: "proposal"; proposal: EntryProposal }
  | { status: "clarify"; id?: string; question: string; choices: string[]; venture_name?: string | null; website_answers?: Record<string, string>; onboarding_question?: string | null; onboarding_choices?: string[]; onboarding_field?: "offer" | "customer" | "context" | null };
export type EntryCommit = {
  status: "committed";
  route: EntryRoute;
  venture_id: string;
  workspace_id: string;
  next_step: string;
  intake_path?: string;
  action: { kind: "idea" | "gtm_review" | "build_review"; venture_id: string; href: string };
  message: string;
};
export type EntryHistoryItem = { text: string; proposal: EntryProposal };

/** A new venture starts blank: split the saved thread at the last committed proposal. Earlier items stay reachable. */
export function splitEntryHistory(items: EntryHistoryItem[]): { earlier: EntryHistoryItem[]; fresh: EntryHistoryItem[] } {
  let cut = -1;
  items.forEach((item, index) => { if (item.proposal.state === "committed" || item.proposal.receipt) cut = index; });
  return { earlier: items.slice(0, cut + 1), fresh: items.slice(cut + 1) };
}

/** Accept only the committed venture's implemented in-app destination. */
export function committedEntryHref(commit: EntryCommit): string {
  if (commit.status !== "committed" || !commit.venture_id || !commit.workspace_id ||
      !["startup", "personal_build", "gtm"].includes(commit.route) ||
      !["idea", "build", "gtm"].includes(commit.next_step)) throw new Error("The saved conversation could not be verified.");
  const next = commit.route === "gtm" ? "gtm" : commit.route === "personal_build" ? "build" : "idea";
  const kind = commit.route === "gtm" ? "gtm_review" : commit.route === "personal_build" ? "build_review" : "idea";
  if (commit.next_step !== next || commit.action.kind !== kind) throw new Error("The saved conversation could not be verified.");
  const venture = encodeURIComponent(commit.venture_id);
  const expected = commit.action.kind === "gtm_review"
    ? `/gtm?venture=${venture}`
    : `/v/${venture}/idea`;
  if (commit.action.venture_id !== commit.venture_id || commit.action.href !== expected) {
    throw new Error("The saved next action does not match this venture.");
  }
  return `${expected}${expected.includes("?") ? "&" : "?"}chat=1`;
}

/** The confirmed founder journey continues in the same full-page chat. */
export function committedChatHref(commit: EntryCommit): string {
  committedEntryHref(commit); // Validate the receipt before trusting its venture ID.
  return `/new?venture=${encodeURIComponent(commit.venture_id)}`;
}

export function entryReviewLabel(commit: EntryCommit): string {
  if (commit.action.kind === "gtm_review") return "Review go-to-market details";
  if (commit.action.kind === "build_review") return "Review the build direction";
  return "Review idea details";
}

export function entryError(data: unknown, fallback: string): string {
  if (entryConfirmationCode(data) === "insufficient_credits") return usageLimitMessage();
  if (!data || typeof data !== "object") return fallback;
  const detail = "detail" in data ? data.detail : data;
  if (!detail || typeof detail !== "object") return fallback;
  const error = "error" in detail ? detail.error : undefined;
  if (!error || typeof error !== "object" || !("message" in error)) return fallback;
  return typeof error.message === "string" ? error.message : fallback;
}

export function resumableEntry(proposal: EntryProposal): boolean {
  return proposal.state === "pending" || proposal.state === "committing";
}

export function checkedEntryCommit(saved: EntryProposal, receipt: EntryCommit): EntryCommit {
  committedChatHref(receipt);
  if (receipt.route !== saved.route || (saved.target_venture_id && saved.target_venture_id !== receipt.venture_id))
    throw new Error("The saved conversation could not be verified.");
  return receipt;
}

function entryConfirmationCode(data: unknown): unknown {
  const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
  const value = detail && typeof detail === "object" && "error" in detail ? detail.error : detail;
  return value && typeof value === "object" && "code" in value ? value.code : null;
}

export function entryConfirmationFailure(data: unknown): Error & { code: unknown } {
  return Object.assign(new Error(entryConfirmationError(data)), { code: entryConfirmationCode(data) });
}

export function entryConfirmationError(data: unknown): string {
  const code = entryConfirmationCode(data);
  if (code === "insufficient_credits") return usageLimitMessage();
  if (code === "wallet_setup_unavailable") return "Your idea is saved, but account setup could not finish. Continue to try again without creating another venture.";
  if (code === "initial_clarification_unavailable") return "Your idea is saved, but the first question could not start. Choose Continue on Add Venture to try again.";
  if (code === "credential_not_allowed") return entryError(data, "Remove the key or password and save it in Settings > Keys and connections.");
  if (code === "proposal_busy") return "This saved direction is still being confirmed. Check its saved outcome shortly.";
  if (code === "proposal_expired" || code === "proposal_not_pending") return "This saved direction is no longer available for confirmation. Refresh its saved outcome.";
  return "The confirmation outcome is unconfirmed. Check the saved direction before continuing.";
}

/** A fresh same-ID read precedes continuation; only explicit expiry can refresh the last answer. */
export async function continueSavedEntry(
  original: EntryProposal,
  read: (id: string) => Promise<EntryProposal>,
  confirm: (id: string) => Promise<EntryCommit>,
  current: () => boolean,
  resend?: () => Promise<void>,
): Promise<EntryCommit | null> {
  const refuse = () => { throw new Error("The saved conversation could not be verified."); };
  if (!current() || !original.id) return refuse();
  let refreshable = original.state === "pending";
  try {
    const saved = await read(original.id);
    refreshable &&= saved.state === "pending";
    if (!current() || saved.id !== original.id || saved.route !== original.route ||
        (original.target_venture_id && saved.target_venture_id !== original.target_venture_id)) return refuse();
    if (saved.state === "committed") return saved.receipt ? checkedEntryCommit(saved, saved.receipt) : refuse();
    if (!resumableEntry(saved)) return refuse();
    const receipt = await confirm(saved.id);
    if (!current()) return refuse();
    return checkedEntryCommit(saved, receipt);
  } catch (error) {
    if (!current() || !refreshable || !resend || !error || typeof error !== "object" ||
        !("code" in error) || error.code !== "proposal_expired") throw error;
    await resend();
    return null;
  }
}

export function entryOwnerKey(subject: string | null, epoch: number, location: {pathname: string; search: string}, target?: string): string | null {
  if (!subject || !/^\/new\/?$/.test(location.pathname)) return null;
  const query = new URLSearchParams(location.search);
  if ((query.get("venture") || undefined) !== target) return null;
  return JSON.stringify([subject, epoch, location.pathname, location.search, target ?? null]);
}

export function entryDraftKey(subject: string | null, location: {pathname: string; search: string}, target?: string): string | null {
  const owner = entryOwnerKey(subject, 0, location, target);
  return owner ? `supymem.entryDraft.v1:${owner}` : null;
}

export function entryDraftResumeKey(subject: string | null, proposal: string): string | null {
  return subject ? `supymem.entryDraft.resume.v1:${JSON.stringify([subject, proposal])}` : null;
}


const ENTRY_CREDENTIAL = /\b(?:sk-ant-|sk-proj-|sk-or-|sk_test_|sk_live_|sk-|zn_|ghp_|github_pat_|gho_|glpat-|sb_(?:secret|publishable)_[a-z0-9_-]{10,}|postgres(?:ql)?:\/\/[^\s:/]+:[^\s@]+@|AKIA[0-9a-z]{8,}|lin_api_|vck_[a-z0-9_-]{20,}|apikey_[a-f0-9]{32}_[a-f0-9]{64}|rzp_live_|rzp_test_|rzp_|AIza[a-z0-9_-]{20,}|xai-[a-z0-9]{20,}|whsec_|Bearer [a-z0-9._-]{20,}|(?:api[_-]?key|secret[_-]?key|access[_-]?token|password|private[_-]?key)\s*(?:[:=]|\bis\b)\s*\S{12,})/i;
const unsentEntryKey = (subject: string | null) => subject ? `supymem.entryDraft.unsent.v1:${JSON.stringify(subject)}` : null;
const freshEntryRoute = /^\?fresh=[a-zA-Z0-9_-]{1,128}$/;

export function readEntryDraft(storage: Storage | null, key: string | null): string {
  const draft = readChatDraft(storage, key);
  if (draft.trim().length < 12 || !ENTRY_CREDENTIAL.test(draft)) return draft;
  clearChatDraftIfMatches(storage, key, draft);
  return "";
}

export function writeEntryDraft(storage: Storage | null, key: string | null, draft: string,
                                subject: string | null, search: string, target?: string): void {
  const safe = draft.trim().length >= 12 && ENTRY_CREDENTIAL.test(draft) ? "" : draft;
  writeChatDraft(storage, key, safe);
  if (!target && key && freshEntryRoute.test(search)) {
    const pointer = unsentEntryKey(subject);
    if (safe.trim() && readChatDraft(storage, key) === safe) writeChatDraft(storage, pointer, search);
    else clearChatDraftIfMatches(storage, pointer, search);
  }
}

export function clearUnsentEntryDraft(storage: Storage | null, subject: string | null, search: string): void {
  if (freshEntryRoute.test(search) && !unsentEntryDraftHref(storage, subject)) {
    clearChatDraftIfMatches(storage, unsentEntryKey(subject), search);
  }
}

export function unsentEntryDraftHref(storage: Storage | null, subject: string | null): string | null {
  const search = readChatDraft(storage, unsentEntryKey(subject));
  return freshEntryRoute.test(search) && readEntryDraft(storage, entryDraftKey(subject, {pathname: "/new", search})).trim()
    ? `/new${search}` : null;
}
