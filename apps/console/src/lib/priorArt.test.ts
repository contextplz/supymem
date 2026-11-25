import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
function compile(path: string): any {
  const exports = {};
  const output = ts.transpileModule(readFileSync(path, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(output, { exports, URL, Intl, require(id: string) {
    if (id === "@/lib/researchBrief") return compile(fileURLToPath(new URL("./researchBrief.ts", import.meta.url)));
    if (id === "./ResearchBrief") return compile(fileURLToPath(new URL("../components/research/ResearchBrief.tsx", import.meta.url)));
    if (id === "@/lib/cx") return { cx: (...values: unknown[]) => values.filter(Boolean).join(" ") };
    if (id.endsWith(".module.css")) return { default: new Proxy({}, { get: (_, key) => String(key) }) };
    return require(id);
  } });
  return exports;
}
const { PriorArt } = compile(fileURLToPath(new URL("../components/research/PriorArt.tsx", import.meta.url)));
const findings = [
  { id: "alpha", title: "Alpha launch", url: "https://news.ycombinator.com/item?id=1", summary: "Alpha builds green bean inventory software.", additional_excerpts: [{ text: "Pro is $29 per month. Importing a roast log takes too long." }] },
  { id: "beta", title: "Beta directory", url: "https://www.ycombinator.com/companies/beta", summary: "Beta is an inactive inventory company." },
];
function priorArt(): any {
  return {
    companies: [
      { key: "alpha.example", name: "Alpha", kind: "direct", status: "live", finding_ids: ["alpha"],
        status_evidence: [{ finding_id: "alpha", basis: "launch" }], first_seen: "2021-04", last_seen: "2026-09-30",
        what_they_built: { finding_id: "alpha", quote: "Alpha builds green bean inventory software." },
        pricing_now: [{ finding_id: "alpha", quote: "Pro is $29 per month." }],
        complaints: [{ finding_id: "alpha", quote: "Importing a roast log takes too long." }],
        traction_signals: [{ finding_id: "alpha", type: "hn_points", value: 212 }] },
      { key: "beta.example", name: "Beta", kind: "dead", status: "inactive", finding_ids: ["beta"], status_evidence: [{ finding_id: "beta", basis: "yc_status" }],
        what_they_built: { finding_id: "beta", quote: "Beta is an inactive inventory company." } },
    ],
    lessons: [{ text: "Test whether imports are the main obstacle.", kind: "pattern", company_keys: ["alpha.example", "beta.example"], citations: [{ finding_id: "alpha", quote_index: 0 }, { finding_id: "beta", quote_index: 0 }] }],
    people: [{ name: "Public builder", relation: "Posted the Alpha launch", public_url: "https://news.ycombinator.com/user?id=builder", role_claim_finding_id: "alpha" }],
    freshness: { newest_source: "2026-09-30", oldest_used: "2021-04", stale_companies: ["beta.example"] },
    coverage_gaps: ["No failure write-up was found."],
  };
}

test("old scraped product quotes are cleaned on render without weakening citations", () => {
  const raw = "[Skip to main content](https://beanbox.com/#main-content) [**Free bag when you join today! ►**](https://beanbox.com/deals)\n![Beanbox Curator](https://assets.example/image.jpg)\n[Coffee subscriptions](https://beanbox.com/coffee) from independent roasters. Fresh beans delivered monthly. Third sentence.";
  const prior = priorArt();
  prior.companies[0].what_they_built.quote = raw;
  const html = renderToStaticMarkup(React.createElement(PriorArt, { priorArt: prior, findings: [{ ...findings[0], summary: raw }] }));
  assert.match(html, /Coffee subscriptions from independent roasters. Fresh beans delivered monthly./);
  assert.doesNotMatch(html, /Skip to|Free bag|Curator|assets.example|Third sentence/);
  prior.companies[0].what_they_built.quote = "Invented offering";
  prior.companies[0].what_they_built.description = "Invented offering";
  assert.doesNotMatch(renderToStaticMarkup(React.createElement(PriorArt, { priorArt: prior, findings })), /Invented offering/);
});
const render = (summary: any = priorArt(), sources: any = findings) => renderToStaticMarkup(React.createElement(PriorArt, { priorArt: summary, findings: sources }));

test("every prior-art quote is plain text, including older markdown and newly stored quotes", () => {
  const summary = priorArt();
  const price = "- [go pro](https://meetings.hubspot.com/alpha) - **$29 per month**.";
  const complaint = "- Imports are **broken**. ![screenshot](https://assets.example/image.png)";
  const positioning = "## [Built for roasters](https://alpha.example)";
  const sources = [{ ...findings[0], additional_excerpts: [{ text: price }, { text: complaint }, { text: positioning }] }];
  summary.companies[0].pricing_now[0].quote = price;
  summary.companies[0].complaints[0].quote = complaint;
  summary.companies[0].positioning = { finding_id: "alpha", quote: positioning };
  summary.lessons[0].text = "Test [imports](https://alpha.example) **first**.";
  summary.lessons[0].citations = [{ finding_id: "alpha", quote_index: 0 }];
  const html = render(summary, sources);
  for (const text of ["go pro $29 per month.", "Imports are broken.", "Built for roasters", "Test imports first."]) assert.ok(html.includes(text), text);
  assert.doesNotMatch(html, /meetings.hubspot|assets.example|\*\*|\[go pro\]|- go pro/);
  summary.companies[0].pricing_now[0].quote = "go pro $29 per month.";
  assert.ok(render(summary, sources).includes("go pro $29 per month."));
  summary.companies[0].pricing_now[0].quote = "Invented price is $99 per month.";
  assert.doesNotMatch(render(summary, sources), /Invented price/);
});

test("entries without any resolved core evidence disappear", () => {
  const summary = priorArt();
  summary.companies[0].what_they_built = null;
  summary.companies[0].pricing_now = [];
  summary.companies[0].complaints = [];
  summary.companies[1].what_they_built = { finding_id: "beta", quote: "Invented product" };
  const html = render(summary);
  assert.ok(!html.includes('<article') && !html.includes('Invented product'));
});

test("prior art renders saved company evidence, cited inference and public profiles", () => {
  const html = render();
  for (const text of ["Who tried this before", "Alpha", "Live", "Inactive", "Apr 2021", "Sep 2026", "Pro is $29 per month.", "Importing a roast log takes too long.", "Pattern", "Inference", "212", "Public adoption signals", "People to learn from", "Public builder", "respect their choice to decline", "no source from the past two years"])
    assert.ok(html.includes(text), text);
  assert.ok(!html.includes("No failure write-up was found."));
  assert.ok(html.includes('href="https://news.ycombinator.com/item?id=1"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
  assert.ok(html.includes('<a data-usage-control="research_prior_source" href="https://news.ycombinator.com/item?id=1" target="_blank" rel="noopener noreferrer">Alpha launch</a>'));
  assert.ok(html.includes('<a data-usage-control="research_prior_profile" href="https://news.ycombinator.com/user?id=builder" target="_blank" rel="noopener noreferrer">Public builder</a>'));
  assert.ok(!html.includes('data-usage-control="research_prior_timeline"'));
  assert.ok(!html.includes("Dead"));
  assert.ok(!html.includes("<button"));
});

test("prior art hides old reports and empty searches", () => {
  assert.equal(render(null), "");
  const html = render({ companies: [], lessons: [], people: [], coverage_gaps: [] });
  assert.equal(html, "");
});

test("malformed optional saved rows stay unknown without crashing research", () => {
  const summary = priorArt();
  summary.companies.push(null, { name: "Bad", finding_ids: "not an array" });
  summary.companies[0].pricing_now = [null, { finding_id: "alpha", quote: 1 }];
  summary.companies[0].traction_signals = [null];
  summary.lessons = [null, { text: "No evidence", citations: [null] }];
  summary.people = [null, { name: 1 }];
  summary.coverage_gaps = "bad array";
  summary.freshness.stale_companies = null;
  const html = render(summary);
  assert.ok(html.includes("Who tried this before"));
  assert.ok(!html.includes("No evidence"));
});

test("unresolved quotes and status remain unknown and unsupported companies disappear", () => {
  const summary = priorArt();
  summary.companies[0].what_they_built.quote = "Invented product claim";
  summary.companies[0].pricing_now[0].quote = "A made up price";
  summary.companies[0].status_evidence = [{ finding_id: "missing", basis: "launch" }];
  summary.companies.push({ key: "missing.example", name: "Unconfirmed model seed", kind: "direct", status: "live", finding_ids: ["missing"] } as any);
  const html = render(summary);
  assert.ok(!html.includes("Status unknown"));
  assert.ok(!html.includes("Unknown from the saved sources"));
  assert.ok(!html.includes("Invented product claim"));
  assert.ok(!html.includes("A made up price"));
  assert.ok(!html.includes("Unconfirmed model seed"));
});

test("excluded and unverified findings cannot support companies, lessons or people", () => {
  for (const invalid of [{ founder_flag: "exclude" }, { category: "review_lead" }, { evidence_kind: "search_lead_unverified" }, { evidence_kind: "name_source" }]) {
    const html = render(priorArt(), findings.map(source => ({ ...source, ...invalid })));
    assert.ok(!html.includes("Alpha launch"));
    assert.ok(!html.includes("Public builder"));
    assert.ok(!html.includes("Test whether imports"));
    assert.equal(html, "");
  }
});

test("a pattern needs resolved citations for two distinct companies", () => {
  const summary = priorArt();
  summary.lessons[0].citations = [{ finding_id: "alpha", quote_index: 0 }];
  assert.ok(render(summary).includes("Anecdote"));
  assert.ok(!render(summary).includes("Pattern"));
  summary.lessons[0].citations.push({ finding_id: "missing", quote_index: 0 });
  assert.ok(!render(summary).includes("Test whether imports"));
});

test("public source and profile links reject local URLs, credentials and contact details", () => {
  for (const url of ["javascript:alert(1)", "file:///tmp/source", "mailto:builder@example.com", "http://127.0.0.1/private", "http://[::1]/private", "http://localhost/private", "http://localhost./private", "https://host.local./private", "https://host.internal/private", "https://localhost.public.example/private", "https://public.example:8443/private", "https://user:password@public.example/private", "https://public.example/?auth=private", "https://public.example/?email=person%40example.com"]) {
    const summary = priorArt(); summary.people[0].public_url = url;
    const html = render(summary, findings.map(source => ({ ...source, url })));
    assert.ok(!html.includes("href="), url);
    assert.ok(!html.includes("Public builder"), url);
  }
  const summary = priorArt(); summary.people[0].relation = "Email builder@example.com or call +1 415 555 1234";
  assert.ok(!render(summary).includes("builder@example.com"));
  assert.ok(!render(summary).includes("555 1234"));
  summary.people[0].relation = "Public contributor";
  summary.people[0].name = '<img src=x onerror="alert(1)">';
  summary.lessons[0].text = "Lesson <script>alert(1)</script>";
  const escaped = render(summary, findings.map(source => ({ ...source, title: 'Source "><img src=x>' })));
  assert.ok(!escaped.includes("<img"));
  assert.ok(!escaped.includes("<script"));
  assert.ok(escaped.includes("&lt;img"));
  assert.ok(escaped.includes("Lesson"));
  assert.ok(!escaped.includes("alert(1)</p>"));
});

test("failure causes and timelines require resolved evidence, never inactive status alone", () => {
  const summary = priorArt();
  summary.companies[1].why_failed = { finding_id: "beta", quote: "An invented failure cause" };
  summary.companies[1].timeline = [{ finding_id: "missing", date: "2021-04-01", event: "Uncited launch" }, { finding_id: "beta", date: "not a date", event: "Invalid event" }];
  let html = render(summary);
  assert.ok(!html.includes("Why it struggled"));
  assert.ok(!html.includes("What happened"));
  summary.companies[1].why_failed.quote = "Beta stopped after support costs became unsustainable.";
  summary.companies[1].timeline.push({ finding_id: "beta", date: "2021-04-01", event: "Source published" });
  html = render(summary, findings.map(source => source.id === "beta" ? { ...source,
    additional_excerpts: [{ text: "Beta stopped after support costs became unsustainable." }] } : source));
  assert.ok(html.includes("Why it struggled"));
  assert.ok(html.includes("What happened"));
  assert.ok(html.includes('<summary data-usage-control="research_prior_timeline">What happened</summary>'));
  assert.ok(html.includes('<time dateTime="2021-04-01">Apr 2021</time>'));
  assert.ok(html.includes("Source published"));
  assert.ok(!html.includes("Uncited launch"));
  assert.ok(!html.includes("Invalid event"));
});

test("a fetched page only establishes public page availability", () => {
  const summary = priorArt();
  summary.companies[0].status_evidence = [{ finding_id: "alpha", basis: "page_available" }];
  assert.ok(render(summary).includes("Public page available"));
  assert.ok(!render(summary).includes(">Live<"));
  summary.companies[0].status_evidence.push({ finding_id: "beta", basis: "yc_status" });
  assert.ok(render(summary).includes(">Live<"));
});

test("profiles allow public community hosts and the cited company domain only", () => {
  const summary = priorArt();
  for (const public_url of ["https://github.com/builder", "https://www.ycombinator.com/people/builder", "https://alpha.example/team/builder", "https://team.alpha.example/builder"]) {
    summary.people[0].public_url = public_url;
    assert.ok(render(summary).includes("Public builder"), public_url);
  }
  for (const public_url of ["https://unrelated.example/builder", "https://github.com.unrelated.example/builder", "https://beta.example/team/builder"]) {
    summary.people[0].public_url = public_url;
    assert.ok(!render(summary).includes("Public builder"), public_url);
  }
  summary.people[0].public_url = "https://github.com/builder";
  for (const ref of [undefined, null, "", "foreign"]) {
    summary.people[0].role_claim_finding_id = ref;
    assert.ok(!render(summary).includes("Public builder"), String(ref));
  }
});
