/** Founder-facing helpers for the Content page: no JSON, no developer setup. */
type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? v as Obj : {});
const text = (v: unknown) => String(v ?? "").trim();
const list = (v: unknown): string[] => (Array.isArray(v) ? v : v ? [v] : []).map(text).filter(Boolean);
const lines = (value: string) => value.split("\n").map((l) => l.trim()).filter(Boolean);
const firstSegment = (icp: unknown): Obj => {
  const segments = obj(icp).segments;
  return obj(Array.isArray(segments) ? segments[0] : undefined);
};

export type BriefForm = {
  name: string; oneLiner: string; audience: string; problems: string; differentiators: string; tone: string;
};

export function briefForm(brief: Obj | null | undefined): BriefForm {
  const b = obj(brief), product = obj(b.product), seg = firstSegment(b.icp);
  return {
    name: text(product.name), oneLiner: text(product.one_liner), audience: text(seg.name),
    problems: list(seg.pains).join("\n"), differentiators: list(product.differentiators).join("\n"),
    tone: text(obj(b.voice).tone),
  };
}

/** Fields the founder did not touch (competitors, claims, cadence) are carried over unchanged. */
export function briefFromForm(base: Obj | null | undefined, form: BriefForm): Obj {
  const root = structuredClone(obj(base)) as Obj;
  const product = obj(root.product), icp = obj(root.icp), seg = firstSegment(icp);
  root.product = { ...product, name: form.name.trim(), one_liner: form.oneLiner.trim(), differentiators: lines(form.differentiators) };
  root.icp = { ...icp, segments: [{ ...seg, name: form.audience.trim(), pains: lines(form.problems) }] };
  root.voice = { ...obj(root.voice), ...(form.tone.trim() ? { tone: form.tone.trim() } : {}) };
  return root;
}

/** What is still missing before the brief can be saved, in founder words. */
export function briefGaps(form: BriefForm): string[] {
  const gaps: string[] = [];
  if (!form.name.trim()) gaps.push("the product name");
  if (!form.oneLiner.trim()) gaps.push("a one-line description");
  if (!form.audience.trim()) gaps.push("who it is for");
  return gaps;
}

/** The API's developer-style brief errors, in plain words. */
export function friendlyBriefError(message: string): string {
  const m = message.toLowerCase();
  if (m.includes("product.name")) return "Add the product name.";
  if (m.includes("one_liner")) return "Add a one-line description of the product.";
  if (m.includes("icp.segments")) return "Say who this is for.";
  return message;
}

export type DraftGate = { disabled: boolean; reason: string };
/** One place that says why "Draft posts" is unavailable, so no button is silently disabled. */
export function draftGate(i: { hasVenture: boolean; hasBrief: boolean; busy: boolean; running: boolean; pending: boolean }): DraftGate {
  if (!i.hasVenture) return { disabled: true, reason: "Open Content from one of your companies so drafts are saved to it." };
  if (!i.hasBrief) return { disabled: true, reason: "Finish the brief above first. Drafts are written from it." };
  if (i.running) return { disabled: true, reason: "A draft is being written. It will appear below in a moment." };
  if (i.pending) return { disabled: true, reason: "Your submitted request is saved. Choose Recover submitted request below to check its result without starting a second draft." };
  if (i.busy) return { disabled: true, reason: "Working on it…" };
  return { disabled: false, reason: "" };
}

const NAMES: Record<string, string> = { linkedin: "LinkedIn", x: "X", reddit: "Reddit", bluesky: "Bluesky", mastodon: "Mastodon", devto: "DEV" };
export const channelName = (platform: string) => NAMES[platform] || platform;

/** Publishing is offered only for a channel with an active, connected account. */
export function publishOffer(platform: string, accounts: Array<{ platform: string; status: string }>): { canPublish: boolean; hint: string } {
  const connected = accounts.some((a) => a.platform === platform && a.status === "active");
  return connected
    ? { canPublish: true, hint: "" }
    : { canPublish: false, hint: `Connect ${channelName(platform)} to post directly, or copy the draft.` };
}

export function postStatusLabel(state: string): string {
  return ({ draft: "Draft to review", refused: "Needs changes", scheduled: "Ready", approved: "Approved",
    published: "Published", failed: "Not published" } as Record<string, string>)[state] || state;
}

export const APPROVE_DEADLINE_MS = 30_000;
export class ApproveTimeout extends Error {}
/** Rejects with ApproveTimeout if `work` has not settled in time; `work` itself keeps running. */
export function withDeadline<T>(work: Promise<T>, ms = APPROVE_DEADLINE_MS): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new ApproveTimeout("timeout")), ms);
    work.then(v => { clearTimeout(timer); resolve(v); }, e => { clearTimeout(timer); reject(e); });
  });
}

/** Why "Approve and write" is unavailable. A failed earlier run is never a reason: a fresh quote starts a new run. */
export function approveGate(i: { busy: boolean; sufficient: boolean; pending: boolean }): DraftGate {
  if (i.busy) return { disabled: true, reason: "Sending your approval…" };
  if (!i.sufficient) return { disabled: true, reason: "There are not enough credits for this draft." };
  if (i.pending) return { disabled: true, reason: "An earlier approval is waiting for confirmation. Use Recover below." };
  return { disabled: false, reason: "" };
}

/** Missing angles after a brief edit trigger a new quote, never an unapproved paid run. */
export async function quoteContentWithTopicRecovery<R extends { kind: "topics" | "draft"; platform?: string }, Q>(
  request: R, quote: (request: R) => Promise<Q>,
): Promise<{ request: R; quote: Q; plannedTopics: boolean }> {
  try { return { request, quote: await quote(request), plannedTopics: false }; }
  catch (error) {
    if (request.kind !== "draft" || !(error instanceof Error) || !/\bcontent_company_topic_required\b/.test(error.message)) throw error;
    const topics = { ...request, kind: "topics" as const };
    return { request: topics, quote: await quote(topics), plannedTopics: true };
  }
}
