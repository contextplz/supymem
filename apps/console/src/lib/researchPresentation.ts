export type ResearchSource = {
  id: string;
  title: string;
  category: string;
  evidence_kind?: string;
  summary?: string;
  search_intents?: string[];
  display_topic?: ResearchTopic | null;
};

export type CitedResearchAssessment = { category: string; source_ids: string[] };

export type ResearchTopic = "Direct competitor evidence" | "Related products and context" | "Problem and demand" | "Pricing and buying" | "Search leads to review" | "Market insights and alternatives";

const categoryTopics: Record<string, ResearchTopic> = {
  direct: "Related products and context",
  adjacent: "Related products and context",
  alternatives: "Related products and context",
  funded: "Related products and context",
  accelerator: "Related products and context",
  emerging: "Related products and context",
  competitor_direct: "Related products and context",
  competitor_alternative: "Related products and context",
  competitor_funded: "Related products and context",
  competitor_accelerator: "Related products and context",
  competitor_emerging: "Related products and context",
  adjacent_solution: "Related products and context",
  services: "Market insights and alternatives",
  oss: "Market insights and alternatives",
  problem: "Problem and demand",
  community: "Problem and demand",
  customer_request: "Problem and demand",
  workaround: "Problem and demand",
  customer_demand: "Problem and demand",
  pricing: "Pricing and buying",
  buyer_budget: "Pricing and buying",
  willingness_to_pay: "Pricing and buying",
  risks: "Market insights and alternatives",
};

export const researchTopics: ResearchTopic[] = ["Problem and demand", "Direct competitor evidence", "Related products and context", "Pricing and buying", "Search leads to review", "Market insights and alternatives"];

function citedCategories(source: ResearchSource, assessments: CitedResearchAssessment[]): string[] {
  return assessments.filter((assessment) => assessment.source_ids.includes(source.id)).map((assessment) => assessment.category);
}

export function topicForSource(source: ResearchSource, assessments: CitedResearchAssessment[] = []): ResearchTopic {
  if (source.display_topic && researchTopics.includes(source.display_topic)) return source.display_topic;
  if (source.category === "review_lead" || source.evidence_kind === "search_lead_unverified") return "Search leads to review";
  const cited = citedCategories(source, assessments);
  if (cited.includes("competitor_direct")) return "Direct competitor evidence";
  if (cited.some((category) => category.startsWith("competitor_") || category === "adjacent_solution")) return "Related products and context";
  if (categoryTopics[source.category]) return categoryTopics[source.category];
  if (cited.length) {
    const first = cited[0];
    if (first === "community") return "Problem and demand";
    if (first === "api_service" || first === "saas_service") return "Market insights and alternatives";
  }
  const intents = source.search_intents || [];
  if (intents.some((intent) => ["direct", "alternatives", "funded", "accelerator", "emerging", "product_details"].includes(intent))) return "Search leads to review";
  if (intents.some((intent) => ["pricing", "buyer_budget"].includes(intent))) return "Pricing and buying";
  if (intents.some((intent) => ["community", "customer_voice", "customer_request", "workaround"].includes(intent))) return "Problem and demand";
  return "Market insights and alternatives";
}

export function researchSourceRelevance(source: ResearchSource, assessments: CitedResearchAssessment[] = []): string {
  if (source.category === "review_lead" || source.evidence_kind === "search_lead_unverified") return "unverified search lead; relevance has not been established";
  const cited = citedCategories(source, assessments);
  if (cited.includes("competitor_direct")) return "cited as a direct competitor in the research assessment";
  if (cited.includes("adjacent_solution")) return "cited as adjacent context in the research assessment";
  if (cited.some((category) => category.startsWith("competitor_"))) return "cited as a possible competitor or alternative in the research assessment";
  if (source.category === "direct" || source.category === "competitor_direct") return "saved as a direct competitor lead; not confirmed by a cited assessment";
  return source.category === "source" ? "retrieved search lead" : `saved as ${source.category.replace(/_/g, " ")}`;
}

export function groupResearchSources<T extends ResearchSource>(sources: T[], assessments: CitedResearchAssessment[] = []): Record<ResearchTopic, T[]> {
  const grouped: Record<ResearchTopic, T[]> = {
    "Problem and demand": [],
    "Direct competitor evidence": [],
    "Related products and context": [],
    "Pricing and buying": [],
    "Search leads to review": [],
    "Market insights and alternatives": [],
  };
  for (const source of sources) grouped[topicForSource(source, assessments)].push(source);
  return grouped;
}

export function conciseFinding(summary?: string, maxLength = 170): string {
  const clean = (summary || "").replace(/\s+/g, " ").trim();
  if (!clean) return "No finding was recorded for this source yet.";
  if (clean.length <= maxLength) return clean;
  const at = clean.lastIndexOf(" ", maxLength);
  return `${clean.slice(0, at > maxLength / 2 ? at : maxLength).trimEnd()}…`;
}

// This is only a display preview. The complete saved excerpt remains available in
// the research card, so shortening it never changes the underlying evidence.
export function previewResearchQuote(quote: string, maxLength = 180): string {
  const clean = quote
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+)/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  if (clean.length <= maxLength) return clean;
  const at = clean.lastIndexOf(" ", maxLength);
  return `${clean.slice(0, at > maxLength / 2 ? at : maxLength).trimEnd()}…`;
}

export function researchGapTargets(gaps: string[]): { competitors: boolean; pricing: boolean; demand: boolean } {
  const text = gaps.join(" ").toLowerCase();
  return {
    competitors: /competit|alternative|existing solution/.test(text),
    pricing: /pric|pay|budget|procurement|monetiz/.test(text),
    demand: /demand|customer|buyer|problem|interview/.test(text),
  };
}

export type ResearchEvidenceDimension = { status?: string };

/** Current server-qualified excerpts, independently of saved model prose. */
export function researchPricingSummary(
  pricing: { support?: { finding?: string }[] } | undefined,
  review: { service_checks?: { pricing_quote?: string | null }[]; competitor_checks?: { pricing_quote?: string | null }[] } | null | undefined,
): { headline: string; detail: string } {
  const quotes = [...(pricing?.support || []).map(row => row.finding),
    ...(review?.service_checks || []).map(row => row.pricing_quote),
    ...(review?.competitor_checks || []).map(row => row.pricing_quote)];
  const hasOfferPrice = quotes.some(quote => typeof quote === "string" && quote.trim().length > 0);
  return {
    headline: hasOfferPrice
      ? "Saved source excerpts include advertised offer-price leads."
      : "No explicit advertised offer-price excerpt was identified in this bounded report.",
    detail: "Cited offer prices are not a price for this venture or evidence that its customers will pay. Willingness to pay for this venture still needs customer validation.",
  };
}

/** Related sources alone cannot answer customer or price questions. */
export function researchAllUnknownSummary(
  dimensions: Record<string, ResearchEvidenceDimension> | undefined,
  sourceCount: number,
  hasCustomers = false,
): string | null {
  if (hasCustomers) return null;
  const areas = Object.values(dimensions || {});
  if (sourceCount < 1 || !areas.length || areas.some(area => area.status !== "unknown")) return null;
  return `We found ${sourceCount} related page${sourceCount === 1 ? "" : "s"} but no customer conversations or prices yet. Next: talk to 5 potential customers.`;
}

/** A proposed experiment for a source-poor report, never a market finding. */
export function researchUnknownNextTest(
  state: string,
  dimensions: Record<string, ResearchEvidenceDimension> | undefined,
  qualifiedSourceCount: number,
): { hypothesis: string; action: string; decision: string } | null {
  const assessed = Object.values(dimensions || {});
  if (!["complete", "partial"].includes(state) || qualifiedSourceCount > 0 ||
      assessed.length === 0 || assessed.some((area) => area.status !== "unknown")) return null;
  return {
    hypothesis: "People matching your intended customer have this problem and will pay to have it solved.",
    action: "Ask five prospective customers for a recent example and their current workaround. Offer to solve one case manually, then ask for a paid commitment before building.",
    decision: "Treat the hypothesis as promising only if at least two show a recent example and one agrees to pay. Otherwise revise the customer, problem, or offer.",
  };
}

export type ResearchVerdict = { verdict: "proceed" | "test_first" | "not_yet" | "no_sources" | "in_progress" | "failed" | "outdated"; title: string; why: string[]; why_not: string[];
  next_step: string; source_count: number; lead_count: number };

const MISSING_AREA = /^No sources (?:qualified as evidence|returned) for (.+?)\.?$/;

/** Fold the per-category "No sources ... for X" gaps into one line; other gaps are untouched. */
export function collapseResearchGaps(gaps: string[]): string[] {
  const areas: string[] = [];
  const rest: string[] = [];
  for (const gap of gaps) {
    const match = MISSING_AREA.exec(gap.trim());
    if (match) areas.push(match[1].replace(/_/g, " "));
    else if (/^Omitted \d+ /i.test(gap.trim())) {
      const count = Number(/^Omitted (\d+)/i.exec(gap.trim())![1]);
      rest.push(`${count} research assessment${count === 1 ? "" : "s"} lacked source citations.`);
    }
    else rest.push(gap.replace(/\b[a-z]+(?:_[a-z]+)+\b/g, token => token.replace(/_/g, " ")));
  }
  const unique = [...new Set(areas)];
  return unique.length ? [`${unique.length} search${unique.length === 1 ? "" : "es"} found no pages specific to your idea.`, ...new Set(rest)] : [...new Set(rest)];
}

/** A display-only summary of saved evidence. Search leads never become proof here. */
export function researchEvidenceSnapshot(
  dimensions: Record<string, ResearchEvidenceDimension> | undefined,
  sourceCount: number,
  gapCount: number,
): { headline: string; detail: string } {
  const values = Object.values(dimensions || {});
  const support = values.filter((item) => item.status === "supporting_leads" || item.status === "mixed_leads").length;
  const challenge = values.filter((item) => item.status === "challenging_leads" || item.status === "mixed_leads").length;
  const leadLabel = `${sourceCount} saved source lead${sourceCount === 1 ? "" : "s"}`;
  const gapLabel = `${gapCount} open question${gapCount === 1 ? "" : "s"}`;
  if (!values.length) return {
    headline: `This report has ${leadLabel} and ${gapLabel}.`,
    detail: "It has no structured evidence assessment. The missing assessment does not decide whether your idea can work.",
  };
  if (!support && !challenge) return {
    headline: `This report has ${leadLabel} and ${gapLabel}.`,
    detail: `All ${values.length} assessed area${values.length === 1 ? " remains" : "s remain"} unknown. Unknown is a prompt to test, not a verdict against your idea.`,
  };
  return {
    headline: `This report has ${leadLabel} and ${gapLabel}.`,
    detail: `Saved excerpts include leads in ${support} supporting and ${challenge} challenging area${challenge === 1 ? "" : "s"}. These are directions to investigate, not verified customer demand.`,
  };
}
