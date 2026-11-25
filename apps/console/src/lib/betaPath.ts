/** The founder path the restricted beta delivers. `title` <=6 words, `what` <=140 chars. */
export type BetaPathStep = { key: string; label: string; title: string; what: string; time: string; href: (ventureId: string) => string };

const v = (id: string) => `/v/${encodeURIComponent(id)}`;
export const BETA_PATH: readonly BetaPathStep[] = [
  { key: "idea", label: "Idea", title: "Confirm your idea", what: "Answer a few short questions, then review your answers.", time: "about 5 min", href: (id) => `${v(id)}/idea` },
  { key: "page", label: "Landing page", title: "Your concept landing page", what: "Ready in about 10 seconds. Private until you publish.", time: "about 15 sec", href: (id) => `${v(id)}/site?create=1` },
  { key: "publish", label: "Publish", title: "Publish your landing page", what: "Review the saved version, then publish it yourself.", time: "about 1 min", href: (id) => `${v(id)}/site` },
  { key: "waitlist", label: "Waitlist", title: "Collect sign-ups", what: "Your landing page collects consented sign-ups. Try it with your own email.", time: "about 3 min", href: (id) => `${v(id)}/site` },
  { key: "outreach", label: "Outreach", title: "Draft outreach emails", what: "Three short emails for your review. Nothing is sent.", time: "about 5 min", href: (id) => `/gtm?venture=${encodeURIComponent(id)}` },
  { key: "research", label: "Research (optional)", title: "Research your market", what: "Sourced market research you can open and check.", time: "several minutes", href: (id) => `${v(id)}/research` },
];

export const CREDIT_EXPLAINER =
  "When you ask Charlie to do something, it runs without a confirmation card. Work you did not ask for does not start on its own.";

/**
 * Which step of the path the server's next-step key belongs to; null when the
 * key (paused, running work, experiments) says nothing about position. Published
 * pages and saved founder progress make Outreach available without a waitlist gate.
 */
export function betaStageIndex(key: string | null | undefined, pagePublished = false, founderStage?: string): number | null {
  const index = stageForKey(key);
  const observed = pagePublished || ["page_live", "outreach", "traction"].includes(founderStage || "") ? 4
    : founderStage === "building" ? 1 : null;
  return observed === null ? index : Math.max(index ?? observed, observed);
}

function stageForKey(key: string | null | undefined): number | null {
  if (!key) return null;
  if (key.startsWith("idea.") || key === "venture.review" || key === "venture.context") return 0;
  if (key === "site.draft" || key.startsWith("plan.") || key.startsWith("build.") || key === "work.next_task") return 1;
  if (key === "site.publish") return 2;
  if (key === "site.ready") return 3;
  if (key.startsWith("gtm.")) return 4;
  if (key.startsWith("research.")) return 5;
  return null;
}
