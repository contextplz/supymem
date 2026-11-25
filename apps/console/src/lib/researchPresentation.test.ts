import assert from "node:assert/strict";
import test from "node:test";
import { collapseResearchGaps, conciseFinding,groupResearchSources, previewResearchQuote, researchEvidenceSnapshot, researchGapTargets, researchPricingSummary, researchSourceRelevance, researchUnknownNextTest, researchAllUnknownSummary } from "./researchPresentation.ts";

test("research sources group by evidence topic without dropping unknown categories", () => {
  const groups = groupResearchSources([
    { id: "one", title: "A", category: "direct" },
    { id: "two", title: "B", category: "pricing" },
    { id: "three", title: "C", category: "unrecognized" },
    { id: "four", title: "D", category: "alternatives" },
    { id: "five", title: "E", category: "community" },
    { id: "six", title: "F", category: "customer_request" },
    { id: "seven", title: "G", category: "workaround" },
    { id: "eight", title: "H", category: "buyer_budget" },
  ]);
  assert.deepEqual(groups["Related products and context"].map((source) => source.id), ["one", "four"]);
  assert.deepEqual(groups["Direct competitor evidence"], []);
  assert.deepEqual(groups["Problem and demand"].map((source) => source.id), ["five", "six", "seven"]);
  assert.deepEqual(groups["Pricing and buying"].map((source) => source.id), ["two", "eight"]);
  assert.deepEqual(groups["Market insights and alternatives"].map((source) => source.id), ["three"]);
});

test("server display topics override legacy community classification without changing stored categories", () => {
  const rows = [
    {id: "ad", title: "Launch HN Salem Robotics", category: "community", display_topic: "Related products and context" as const},
    {id: "irrelevant", title: "Robots", category: "community", display_topic: "Search leads to review" as const},
    {id: "pain", title: "Collision repair estimating", category: "source", display_topic: "Problem and demand" as const},
  ];
  const groups = groupResearchSources(rows, [{category: "community", source_ids: ["ad", "irrelevant"]}]);
  assert.deepEqual(groups["Problem and demand"].map(row => row.id), ["pain"]);
  assert.deepEqual(groups["Related products and context"].map(row => row.id), ["ad"]);
  assert.deepEqual(groups["Search leads to review"].map(row => row.id), ["irrelevant"]);
  assert.equal(rows[0].category, "community");
});

test("generic saved sources use recorded search intent and cited assessment, not an invented competitor category", () => {
  const groups = groupResearchSources([
    { id: "competitor-lead", title: "A", category: "source", search_intents: ["direct"] },
    { id: "funding-lead", title: "B", category: "source", search_intents: ["funded"] },
    { id: "demand-lead", title: "C", category: "source", search_intents: ["customer_request"] },
    { id: "unclassified", title: "D", category: "source" },
    { id: "cited-alternative", title: "E", category: "source", search_intents: ["community"] },
  ], [{ category: "competitor_alternative", source_ids: ["cited-alternative"] }]);
  assert.deepEqual(groups["Related products and context"].map((source) => source.id), ["cited-alternative"]);
  assert.deepEqual(groups["Search leads to review"].map((source) => source.id), ["competitor-lead", "funding-lead"]);
  assert.deepEqual(groups["Problem and demand"].map((source) => source.id), ["demand-lead"]);
  assert.deepEqual(groups["Market insights and alternatives"].map((source) => source.id), ["unclassified"]);
});

test("generic job articles remain search leads even when several broad intents match", () => {
  const groups = groupResearchSources([
    { id: "job-article", title: "Hiring trends", category: "source", search_intents: ["direct", "alternatives", "funded"] },
    { id: "jobwizard", title: "JobWizard", category: "source", search_intents: ["direct"] },
  ], [{ category: "competitor_alternative", source_ids: ["jobwizard"] }]);
  assert.deepEqual(groups["Related products and context"].map((source) => source.id), ["jobwizard"]);
  assert.deepEqual(groups["Search leads to review"].map((source) => source.id), ["job-article"]);
});

test("unverified fallback rows stay in review leads even if a stale assessment cites them", () => {
  const lead = { id: "lead", title: "Nursing shift report", category: "review_lead",
    evidence_kind: "search_lead_unverified", search_intents: ["direct"] };
  const groups = groupResearchSources([lead], [{ category: "competitor_direct", source_ids: ["lead"] }]);
  assert.deepEqual(groups["Direct competitor evidence"], []);
  assert.deepEqual(groups["Search leads to review"].map((source) => source.id), ["lead"]);
  assert.equal(researchSourceRelevance(lead), "unverified search lead; relevance has not been established");
});

test("downgraded editorial citations stay visible as adjacent context, separate from direct competitor evidence", () => {
  const editorial = { id: "editorial", title: "AppSec hiring article", category: "source", search_intents: ["direct"] };
  const offering = { id: "offering", title: "AppSec product", category: "source", search_intents: ["direct"] };
  const uncited = { id: "uncited", title: "Legacy classified result", category: "competitor_direct" };
  const assessments = [
    { category: "adjacent_solution", source_ids: ["editorial"] },
    { category: "competitor_direct", source_ids: ["offering"] },
  ];
  const groups = groupResearchSources([editorial, offering, uncited], assessments);
  assert.deepEqual(groups["Direct competitor evidence"].map((source) => source.id), ["offering"]);
  assert.deepEqual(groups["Related products and context"].map((source) => source.id), ["editorial", "uncited"]);
  assert.equal(researchSourceRelevance(editorial, assessments), "cited as adjacent context in the research assessment");
  assert.equal(researchSourceRelevance(offering, assessments), "cited as a direct competitor in the research assessment");
  assert.equal(researchSourceRelevance(uncited), "saved as a direct competitor lead; not confirmed by a cited assessment");
});

test("source summaries remain scannable and do not invent a finding", () => {
  assert.equal(conciseFinding(""), "No finding was recorded for this source yet.");
  assert.match(conciseFinding("word ".repeat(80)), /…$/);
});

test("competitor excerpt previews hide markdown scaffolding and shorten only the display", () => {
  const quote = `## Product\n- Helps applicants track applications.\n\n## Customers\n- Job seekers ${"who apply daily ".repeat(30)}`;
  const preview = previewResearchQuote(quote);
  assert.ok(preview.startsWith("Product Helps applicants track applications."));
  assert.ok(preview.endsWith("…"));
  assert.ok(preview.length <= 181);
  assert.equal(previewResearchQuote("Free plan: 10 applications per day."), "Free plan: 10 applications per day.");
  assert.match(quote, /^## Product/);
});

test("research gaps expose the right investigations", () => {
  assert.deepEqual(researchGapTargets(["No public pricing evidence", "Customer interviews needed"]),
    { competitors: false, pricing: true, demand: true });
});

test("saved reports with unknown dimensions remain questions, not negative verdicts", () => {
  const snapshot = researchEvidenceSnapshot({ customer_demand: { status: "unknown" }, pricing: { status: "unknown" } }, 7, 14);
  assert.match(snapshot.headline, /7 saved source leads and 14 open questions/);
  assert.match(snapshot.detail, /All 2 assessed areas remain unknown/);
  assert.doesNotMatch(snapshot.detail, /fail|invalid|no market/i);
});

test("support and challenge counts only reflect recorded dimension status", () => {
  const snapshot = researchEvidenceSnapshot({
    problem_validation: { status: "supporting_leads" },
    customer_demand: { status: "mixed_leads" },
    pricing: { status: "challenging_leads" },
  }, 3, 1);
  assert.match(snapshot.detail, /2 supporting and 2 challenging/);
  assert.match(snapshot.detail, /not verified customer demand/);
  assert.match(snapshot.headline, /3 saved source leads and 1 open question/);
});

test("legacy dossiers lacking dimensions get a neutral summary", () => {
  const snapshot = researchEvidenceSnapshot(undefined, 0, 0);
  assert.match(snapshot.detail, /no structured evidence assessment/i);
  assert.match(snapshot.detail, /does not decide whether your idea can work/);
});

test("source-poor unknown research offers a measurable experiment without claiming a signal", () => {
  const dimensions = { problem_validation: { status: "unknown" }, customer_demand: { status: "unknown" } };
  const experiment = researchUnknownNextTest("partial", dimensions, 0);
  assert.ok(experiment);
  assert.match(experiment.action, /five prospective customers/);
  assert.match(experiment.decision, /one agrees to pay/);
  assert.equal(researchUnknownNextTest("partial", dimensions, 1), null);
  assert.equal(researchUnknownNextTest("failed", dimensions, 0), null);
  assert.equal(researchUnknownNextTest("complete", { customer_demand: { status: "supporting_leads" } }, 0), null);
  assert.equal(researchUnknownNextTest("complete", undefined, 0), null);
});

test("per-category missing-evidence gaps collapse into one line", () => {
  const out = collapseResearchGaps([
    "Search excerpts have not been verified against full source pages.",
    "No sources qualified as evidence for direct.",
    "No sources returned for funded.",
    "No sources qualified as evidence for direct.",
  ]);
  assert.deepEqual(out, ["2 searches found no pages specific to your idea.", "Search excerpts have not been verified against full source pages."]);
  assert.deepEqual(collapseResearchGaps(["Other gap."]), ["Other gap."]);
});

test("current pricing leads do not disappear because a saved service assessment is empty", () => {
  const summary = researchPricingSummary({ support: [{ finding: "Plans start at $12 per month." }] }, { service_checks: [] });
  assert.match(summary.headline, /include advertised offer-price leads/);
  assert.doesNotMatch(summary.headline, /no pricing|remain unknown/i);
  assert.match(summary.detail, /not a price for this venture/);
  assert.match(summary.detail, /Willingness to pay.*still needs customer validation/);
});

test("saved competitor price and service price quotations are included independently", () => {
  for (const review of [{ competitor_checks: [{ pricing_quote: "Pro $29/month" }] },
    { service_checks: [{ pricing_quote: "The free tier includes 20 searches." }] }]) {
    assert.match(researchPricingSummary(undefined, review).headline, /include advertised offer-price leads/);
  }
});

test("status alone cannot fabricate an offer price and blank legacy quotes remain unknown", () => {
  for (const pricing of [undefined, { support: [] }, { support: [{ finding: " " }] }]) {
    const summary = researchPricingSummary(pricing, { service_checks: [{ pricing_quote: null }], competitor_checks: [] });
    assert.match(summary.headline, /No explicit advertised offer-price excerpt/);
    assert.doesNotMatch(summary.detail, /customers will not pay/);
  }
});


test("related pages with all unknown areas show one honest next step", () => {
  const dimensions = { problem_validation: { status: "unknown" }, pricing: { status: "unknown" } };
  assert.equal(researchAllUnknownSummary(dimensions, 7), "We found 7 related pages but no customer conversations or prices yet. Next: talk to 5 potential customers.");
  assert.equal(researchAllUnknownSummary(dimensions, 7, true), null);
  assert.equal(researchAllUnknownSummary(dimensions, 0), null);
  assert.equal(researchAllUnknownSummary({ pricing: { status: "supporting_leads" } }, 7), null);
  assert.equal(researchAllUnknownSummary(undefined, 7), null);
});

test("legacy research gap copy never shows internal tokens", () => {
  assert.deepEqual(collapseResearchGaps(["Omitted 1 uncited assessment for competitor_funded.", "No sources returned for job_tool."]),
    ["1 search found no pages specific to your idea.", "1 research assessment lacked source citations."]);
});
