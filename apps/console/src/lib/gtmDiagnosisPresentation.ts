import type { Diagnosis } from "./gtmDiagnosisIntent";

/** A short excerpt for the first screen; the complete model wording stays in the saved report. */
export function diagnosisExcerpt(value: string, limit = 210): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  const boundary = text.lastIndexOf(" ", limit);
  return `${text.slice(0, boundary > limit / 2 ? boundary : limit).trimEnd()}…`;
}

/** Older saved reports may predate the next-actions field. */
export function diagnosisFirstAction(diagnosis: { next_actions?: Diagnosis["next_actions"] } | null | undefined) {
  return diagnosis?.next_actions?.[0];
}

/** A saved recommendation points to the relevant venture-scoped review or note-taking page. */
export function diagnosisNextStep(kind: Diagnosis["next_actions"][number]["kind"], ventureId: string) {
  const venture = `/v/${encodeURIComponent(ventureId)}`;
  switch (kind) {
    case "find_customers":
      return {
        href: `/gtm?venture=${encodeURIComponent(ventureId)}`, label: "Find your customers", control: "gtm_journey_find_customers",
        guidance: "Find people in your audience and review the public sources. Nothing is sent until you ask.",
      };
    case "draft_outreach":
      return {
        href: `/gtm?venture=${encodeURIComponent(ventureId)}`, label: "Draft your outreach", control: "gtm_journey_find_customers",
        guidance: "Review your saved prospects and draft outreach. Sending requires your saved email settings and a request from you.",
      };
    case "build_page":
      return {
        href: `${venture}/site`, label: "Build your page", control: "website_build_page",
        guidance: "Build and preview your page with your saved model key. Publishing is separate and uses your own hosting settings.",
      };
    case "research":
      return {
        href: `${venture}/research`, label: "Review this company's research", control: "gtm_next_research",
        guidance: "Review your saved sources and run a scan to check your evidence gaps. Built-in search and page reading need no extra key.",
      };
    case "human_interview":
      return {
        href: `${venture}/feedback`, label: "Record interview findings", control: "gtm_next_human_interview",
        guidance: "Founder task: conduct the interviews described above yourself. Ask about a recent real instance, what the person did, and its cost. Record what you actually heard after each conversation; no interview has been scheduled or conducted here.",
      };
    case "strategy_review":
      return {
        href: `${venture}/plan`, label: "Review this company's plan", control: "gtm_next_strategy_review",
        guidance: "Compare this recommendation with any saved milestones and evidence gaps. Review any plan change before approving it; this diagnosis did not execute a strategy.",
      };
    default:
      return null;
  }
}
