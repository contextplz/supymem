import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";

const require = createRequire(import.meta.url);
const ts = require("typescript");
type Node = { type: unknown; props: Record<string, any> };

function compile(path: string, resolve: (id: string) => unknown, browser?: object): any {
  const output = ts.transpileModule(readFileSync(path, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const exports = {};
  vm.runInNewContext(output, { exports, require: resolve, URL, window: browser, requestAnimationFrame() {} }, { filename: path });
  return exports;
}

async function page(state: string, activeStatus?: string, currentRevision = "current", finishedStatus?: string,
  options: { summary?: Record<string, unknown>; findings?: Record<string, unknown[]>; navigate?: (path: string) => void;
    read?: (path: string, body?: { method?: string; body?: string }) => void | Promise<void>;
    serverStep?: Record<string, unknown> | null; recovery?: { state?: string; personal?: boolean; error?: string }; recoveryWait?: Promise<void>;
    kind?: "deep" | "validation"; detail?: "brief" | "detailed"; afterLoad?: () => void;
    scope?: { owner: string | null; epoch: number; path: string }; captureCleanup?: (cleanup: () => void) => void; captureGeneration?: (advance: () => void) => void;
    feedbackResult?: unknown; jobProgress?: Record<string, unknown>; observedJobId?: string } = {}) {
  const scope = options.scope || { owner: "owner", epoch: 1, path: "/v/venture/research" };
  const stepOwner = { owner: scope.owner, epoch: scope.epoch };
  const serverStep = options.serverStep === undefined ? { key: "site.build", stage: "Page", title: "Choose your next step",
    cta_label: "Build the landing page and explore customers", detail: "A private preview or customers first.", href: "/v/venture/site?build=1",
    chat_action: null, payload: null, alternatives: [
      { choice: "build", label: "Build the landing page", href: "/v/venture/site?build=1" },
      { choice: "skip", label: "Find customers first", href: "/gtm?venture=venture" },
    ] } : options.serverStep;
  const browser = { location: { get pathname() { return scope.path; } } };
  let cursor = 0;
  let finish: ((job: unknown) => void) | undefined;
  const hooks: any[] = [], effects: (() => void | (() => void))[] = [], cleanups: (() => void)[] = [];
  const report = { id: "report", kind: options.kind || "deep", revision_id: "current", state, job_id: "job_private",
    findings: options.findings || {}, summary: { coverage_gaps: ["An evidence gap"], verdict: { title: "Not yet recommended", verdict: "not_yet",
      why: ["Saved reason"], why_not: ["Research did not finish"], next_step: "Continue to plan", source_count: 0, lead_count: 0 }, ...options.summary } };
  const recovery = { state: "confirmed", revisionId: currentRevision, personal: false, jobId: activeStatus ? "job_private" : null, error: "", ...options.recovery };
  const jsx = (type: unknown, props: Node["props"]) => ({ type, props });
  const react = {
    useState(initial: any) { const i = cursor++; if (!(i in hooks)) hooks[i] = typeof initial === "function" ? initial() : initial;
      return [hooks[i], (value: any) => { hooks[i] = typeof value === "function" ? value(hooks[i]) : value; }]; },
    useRef(initial: any) { const i = cursor++; const reference = hooks[i] ||= { current: initial };
      if (initial === 0) options.captureGeneration?.(() => reference.current++);
      return reference; },
    useEffect(effect: () => void | (() => void), deps: unknown[]) { const i = cursor++; if (!(i in hooks)) { hooks[i] = deps; effects.push(effect); } },
  };
  const primitive = (names: string[]) => Object.fromEntries(names.map(name => [name, name]));
  const resolve = (id: string): unknown => {
    if (id === "react") return react;
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
    if (id === "next/link") return { default: "a" };
    if (id === "next/navigation") return { useParams: () => ({ ventureId: "venture" }), useSearchParams: () => new URLSearchParams(), usePathname: () => scope.path, useRouter: () => ({ push: options.navigate || (() => {}) }) };
    if (id === "@/components/layout/NextStepStrip") return { NextStepStrip: "NextStepStrip",
      NextStepButton: "NextStepButton",
      useNextStep: () => scope.owner && scope.owner === stepOwner.owner && scope.epoch === stepOwner.epoch ? serverStep : null };
    if (id === "@/lib/client") return { ensureSession: async () => {}, verifiedSessionSubject: () => scope.owner, verifiedSessionEpoch: () => scope.epoch, api: async (path: string, body?: { method?: string; body?: string }) => { await options.read?.(path, body); return { ok: true,
      data: path.endsWith("/research/dossiers") ? { items: [report] } : path.includes("/research/dossiers/") ? report
        : path.endsWith("/plans") ? { items: [] } : path.endsWith("/feedback") ? options.feedbackResult || { id: "fb_fixture" } : { workspace_id: "workspace" } }; } };
    if (["@/lib/researchPresentation", "@/lib/researchBrief", "@/lib/researchNextStep", "@/lib/nextStep"].includes(id))
      return compile(fileURLToPath(new URL(`./${id.split("/").at(-1)}.ts`, import.meta.url)), resolve);
    if (id === "@/lib/researchRecovery") return { unknownResearchRecovery: () => ({ state: "unknown", error: "" }),
      loadResearchRecovery: async () => { await options.recoveryWait; return recovery; }, researchEmptyMessage: () => "No saved report", researchReceiptMatches: () => true };
    if (id === "@/lib/jobCopy") return { plainJob: () => ({ title: "Research didn't finish", body: "Review saved sources or run research again." }) };
    if (id === "@/lib/durableJobs") return { jobFailure: () => "Research didn't finish", reportJobFailure() {} };
    if (id === "@/lib/credits") return require("./credits.ts");
    if (id === "@/hooks/useDurableJob") return { jobSucceeded: (job: { status: string }) => job.status === "succeeded",
      useDurableJob: (_id: unknown, callback: (job: unknown) => void) => { finish = callback;
        return { job: activeStatus ? { id: options.observedJobId || "job_private", status: activeStatus, progress: options.jobProgress } : null, label: "Queued", error: "" }; } };
    if (id === "@/lib/detailLevel") return { useDetailLevel: () => [options.detail || "brief"] };
    if (id === "@/lib/localMode") return { isLocalConsole: () => true };
    if (id.endsWith(".module.css")) return { default: new Proxy({}, { get: (_, key) => key }) };
    if (id.endsWith("/ResearchBrief")) return { ...primitive(["CoverageMeter", "KeyLines", "Reveal", "RiskLine", "RunningShimmer", "SourceChip", "Stat", "StrengthBar", "VerdictBadge"]),
      displayResearchText: compile(fileURLToPath(new URL("../components/research/ResearchBrief.tsx", import.meta.url)), resolve).displayResearchText };
    if (id.endsWith("/OneCard")) return primitive(["OneCard", "OneCardStack"]);
    return primitive([id.split("/").at(-1)!]);
  };
  const Page = compile(fileURLToPath(new URL("../app/v/[ventureId]/research/page.tsx", import.meta.url)), resolve, browser).default;
  cursor = 0; Page(); effects.splice(0).forEach(effect => { const cleanup = effect(); if (cleanup) cleanups.push(cleanup); });
  options.captureCleanup?.(() => cleanups.forEach(cleanup => cleanup()));
  await new Promise(setImmediate);
  if (finishedStatus) {
    cursor = 0; Page();
    report.state = "failed"; recovery.jobId = null;
    finish!({ id: "job_private", kind: "research.deep", venture_id: "venture", revision_id: "current", status: finishedStatus });
    await new Promise(setImmediate);
  }
  options.afterLoad?.();
  cursor = 0;
  return Page();
}

function nodes(tree: any): Node[] {
  if (!tree || typeof tree !== "object") return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return tree.props ? [tree, ...nodes(tree.props.children), ...nodes(tree.props.details), ...nodes(tree.props.actions)] : [];
}

test("running research shows saved job progress, with dashboard progress while job reads catch up", async () => {
  const progress = { state: "leased", percent: 37, step_label: "Reading customer complaints", started_at: null };
  const dashboard = { key: "research.running", stage: "Research", title: "Researching", cta_label: "View progress",
    href: "/v/venture/research", researchProgress: progress };
  for (const [activeStatus, jobProgress, observedJobId] of [
    [undefined, undefined, undefined], ["leased", progress, undefined], ["leased", { ...progress, percent: 80 }, "old_job"],
  ] as const) {
    const rendered = nodes(await page("running", activeStatus, "current", undefined, { serverStep: dashboard, jobProgress, observedJobId }));
    assert.deepEqual(rendered.find(node => node.type === "ResearchProgress")?.props.progress, progress);
  }
  const newer = { ...progress, percent: 50 };
  const rendered = nodes(await page("running", "leased", "current", undefined, { serverStep: dashboard, jobProgress: newer }));
  assert.deepEqual(rendered.find(node => node.type === "ResearchProgress")?.props.progress, newer);
});

test("completed research passes graveyard matches to the adjacent optional card", async () => {
  const startup = { name: "CrumbQueue", founded: "2018", ended: "2022", cash_burned: "$2 million",
    why_failed: "Too few shops paid.", suggestion: "Test paid bakery order intake.", entry_url: "https://www.loot-drop.io/startup/42-crumbqueue" };
  const rendered = nodes(await page("complete", undefined, "current", undefined, { summary: { startup_graveyard: [startup] } }));
  const prior = rendered.findIndex(node => node.type === "PriorArt");
  const card = rendered.findIndex(node => node.type === "StartupGraveyard");
  assert.ok(card > prior);
  assert.equal(rendered[card].props.startups[0], startup);
});

test("traction research acknowledges demand and offers customers before the website audit", async () => {
  const rendered = nodes(await page("partial", undefined, "current", undefined, {
    serverStep: { key: "gtm.outreach", stage: "Growth", title: "Grow what's already working", detail: "Test the next channel",
      href: "/gtm?venture=venture", cta_label: "Find customers", has_customers: true, has_website: true,
      chat_action: "outreach.draft", payload: {} },
    findings: { source: [{ id: "source", title: "A related page", category: "source" }] },
    summary: { evidence_dimensions: { customer_demand: { status: "unknown" } }, verdict: {
      title: "Grow what's already working", why: ["Your paying customers are demand evidence."],
      why_not: ["The next acquisition channel is still unproven."], source_count: 1, lead_count: 0 } },
  }));
  assert.ok(rendered.some(node => node.type === "h1" && node.props.children === "Grow what's already working"));
  const card = rendered.find(node => node.type === "OneCard")!;
  assert.equal(card.props.title, "Grow what's already working");
  assert.equal(card.props.body, undefined);
  assert.ok(!rendered.some(node => node.type === "RiskLine"));
  const links = rendered.filter(node => node.type === "a" && node.props["data-usage-control"] === "research_next_step");
  assert.equal(links[0].props.href, "/gtm?venture=venture");
  assert.equal(Array.from(links[0].props.children).join(""), "Find customers →");
  assert.equal(links[1].props.children, "Audit your website");
});

test("an active landing build replaces research page choices with its progress link", async () => {
  const rendered = nodes(await page("complete", undefined, "current", undefined, {
    serverStep: { key: "site.building", stage: "Page", title: "Building your landing page…", detail: "Watch your page build",
      href: "/v/venture/site", cta_label: "View Website", runState: "working", has_customers: true, has_website: true },
  }));
  assert.ok(!rendered.some(node => node.type === "PageChoices"));
  const link = rendered.find(node => node.type === "a" && node.props["data-usage-control"] === "research_next_step")!;
  assert.equal(link.props.href, "/v/venture/site");
  assert.equal(Array.from(link.props.children).join(""), "View Website →");
});

test("report detail loads while saved-job recovery is still pending", async () => {
  let release!: () => void;
  const recoveryWait = new Promise<void>(resolve => { release = resolve; });
  const reads: string[] = [];
  await page("complete", undefined, "current", undefined, { recoveryWait,
    read: path => { reads.push(path); if (/research\/dossiers\/report$/.test(path)) release(); } });
  assert.ok(reads.some(path => /research\/dossiers\/report$/.test(path)));
});

for (const activeStatus of ["scheduled", "leased", "retry_wait"]) {
  test(`actual research page hides the verdict, risk and forward actions during ${activeStatus}`, async () => {
    const rendered = nodes(await page("complete", activeStatus));
    const cards = rendered.filter(node => node.type === "OneCard");
    assert.equal(cards.length, 1);
    assert.equal(cards[0].props.title, "Researching…");
    assert.equal(cards[0].props.body, activeStatus === "retry_wait" ? "Retrying in a moment" : "Queued");
    assert.ok(!rendered.some(node => ["VerdictBadge", "RiskLine", "NextStepStrip"].includes(String(node.type))));
    assert.ok(!rendered.some(node => node.props["data-usage-control"] === "research_next_step"));
  });
}

for (const state of ["queued", "pending", "retry_wait", "running"]) {
  test(`persisted ${state} research stays pending before its job observation restores`, async () => {
    const rendered = nodes(await page(state));
    const cards = rendered.filter(node => node.type === "OneCard");
    assert.equal(cards.length, 1);
    assert.equal(cards[0].props.title, "Researching…");
    assert.equal(cards[0].props.body, state === "retry_wait" ? "Retrying in a moment" : state === "running" ? "Research is running" : "Queued");
    assert.ok(!rendered.some(node => ["VerdictBadge", "RiskLine", "NextStepStrip"].includes(String(node.type))));
    assert.ok(!rendered.some(node => node.props["data-usage-control"] === "research_next_step"));
  });
}

for (const [state, revision, title] of [["failed", "current", "Research didn't finish"], ["complete", "changed", "Research is outdated"]]) {
  test(`actual research page shows ${title} with an actionable retry`, async () => {
    const rendered = nodes(await page(state, undefined, revision));
    assert.equal(rendered.find(node => node.type === "OneCard")?.props.title, title);
    assert.ok(!rendered.some(node => ["VerdictBadge", "RiskLine"].includes(String(node.type))));
    assert.ok(rendered.some(node => node.type === "button" && node.props.children === "Run research again" && typeof node.props.onClick === "function"));
    assert.ok(!rendered.some(node => node.props["data-usage-control"] === "research_next_step"));
  });
}

test("actual completed and partial research offer page or customers and one report entry", async () => {
  for (const state of ["complete", "partial"]) {
    const rendered = nodes(await page(state));
    const choices = rendered.find(node => node.type === "PageChoices");
    assert.equal(choices?.props.ventureId, "venture");
    assert.ok(!rendered.some(node => node.props["data-usage-control"] === "research_next_step"));
    assert.equal(rendered.filter(node => node.type === "summary" && String(node.props.children).includes("full research report")).length, 1);
  }
});

test("prior art stays visible in the sidebar in Simple and Detailed research", async () => {
  const priorArt = { companies: [], lessons: [], people: [], coverage_gaps: [] };
  for (const detail of ["brief", "detailed"] as const) {
    const tree = await page("complete", undefined, "current", undefined, { summary: { prior_art: priorArt }, detail });
    const rendered = nodes(tree), prior = rendered.find(node => node.type === "PriorArt");
    assert.ok(prior, detail);
    assert.equal(prior.props.priorArt, priorArt);
    assert.ok(nodes(rendered.find(node => node.type === "aside")).includes(prior));
    assert.ok(!nodes(rendered.find(node => node.props.className === "fullReport")).includes(prior));
  }
  assert.ok(!nodes(await page("complete", "leased", "current", undefined, { summary: { prior_art: priorArt } })).some(node => node.type === "PriorArt"));
});

test("research uses compact flow cards and keeps the report and sources in separate columns", async () => {
  const running = nodes(await page("running", "leased"));
  const stack = running.find(node => node.type === "OneCardStack");
  assert.equal(stack?.props.flow, true);
  assert.equal(nodes(stack)[1].props.title, "Researching…");
  const rendered = nodes(await page("complete"));
  const main = rendered.find(node => node.props.className === "main");
  const sidebar = rendered.find(node => node.type === "aside");
  assert.ok(nodes(main).some(node => node.type === "OneCard"));
  assert.ok(!nodes(sidebar).some(node => node.type === "OneCard"));
  assert.ok(nodes(sidebar).some(node => node.props.id === "case-studies"));
  assert.ok(nodes(sidebar).some(node => node.type === "select"));
});

test("research feedback posts one scoped rating and requires a saved receipt", async () => {
  const posted: { path: string; payload: any }[] = [];
  const tree = await page("complete", undefined, "current", undefined, {
    summary: { prior_art: { companies: [], lessons: [], people: [] } },
    read: (path, body) => { if (body?.method === "POST") posted.push({ path, payload: JSON.parse(body.body || "{}") }); },
  });
  const rating = nodes(tree).find(node => node.type === "PriorArtRating")!;
  assert.ok(rating);
  assert.equal(await rating.props.onSubmit({ useful: 4, new: 3, actionable: 5 }, "Missing a pricing source"), true);
  assert.equal(posted[0].path, "/api/ventures/venture/feedback");
  assert.equal(posted[0].payload.source, "founder");
  assert.deepEqual(JSON.parse(posted[0].payload.text), { kind: "research_prior_art_usefulness", dossier_id: "report",
    ratings: { useful: 4, new: 3, actionable: 5 }, note: "Missing a pricing source" });
  assert.equal(await rating.props.onSubmit({ useful: 0, new: 3, actionable: 5 }, ""), false);
  assert.equal(posted.length, 1);
  const absent = nodes(await page("complete", undefined, "current", undefined, { summary: { prior_art: { companies: [] } }, feedbackResult: {} })).find(node => node.type === "PriorArtRating")!;
  assert.equal(await absent.props.onSubmit({ useful: 4, new: 3, actionable: 5 }, ""), false);
});

test("research feedback refuses retired scope and never applies retired responses", async () => {
  for (const retired of ["account", "account-return", "path", "unmount", "generation"]) {
    const scope = { owner: "owner", epoch: 1, path: "/v/venture/research" }, posted: string[] = [];
    let cleanup = () => {}, advance = () => {}, finish!: () => void;
    const rendered = nodes(await page("complete", undefined, "current", undefined, {
      summary: { prior_art: { companies: [] } }, scope, captureCleanup: saved => cleanup = saved, captureGeneration: saved => advance = saved,
      read: async (path, body) => { if (body?.method === "POST") { posted.push(path); await new Promise<void>(resolve => finish = resolve); } },
    }));
    const rating = rendered.find(node => node.type === "PriorArtRating")!;
    const first = rating.props.onSubmit({ useful: 4, new: 3, actionable: 5 }, "");
    assert.equal(await rating.props.onSubmit({ useful: 4, new: 3, actionable: 5 }, ""), false);
    if (retired === "account") { scope.owner = "other"; scope.epoch++; }
    if (retired === "account-return") scope.epoch += 2;
    if (retired === "path") scope.path = "/v/other/research";
    if (retired === "unmount") cleanup();
    if (retired === "generation") advance();
    assert.equal(await rating.props.onSubmit({ useful: 4, new: 3, actionable: 5 }, ""), false);
    finish();
    assert.equal(await first, false);
    assert.equal(posted.length, 1, retired);
  }
  assert.ok(!nodes(await page("outdated", undefined, "different", undefined, { summary: { prior_art: { companies: [] } } })).some(node => node.type === "PriorArtRating"));
});

test("finished Research uses the scoped server route when no page pair is eligible", async () => {
  for (const [key, href, label] of [["site.connect_tool", "/v/venture/connections", "Connect a coding tool"],
    ["site.build", "/v/venture/site", "Build"],
    ["gtm.outreach", "/gtm?venture=venture", "Find customers"], ["site.publish", "/v/venture/site", "Open the page"]]) {
    const rendered = nodes(await page("complete", undefined, "current", undefined, {
      serverStep: { key, href, cta_label: label, stage: "Next step", title: label, detail: "Saved next step", chat_action: null, payload: null },
    }));
    assert.ok(!rendered.some(node => node.type === "PageChoices"), key);
    const action = rendered.find(node => node.props["data-usage-control"] === "research_next_step");
    assert.equal(action?.props.href, href, key);
    assert.equal(action?.props.className, "primary", key);
    assert.ok(String(action?.props.children).includes(label), key);
  }
});

test("complete and partial reports keep customers primary and research reruns secondary", async () => {
  for (const state of ["complete", "partial"]) {
    const step = { key: "gtm.outreach", href: "/gtm?venture=venture", cta_label: "Find customers",
      stage: "Customers", title: "Find your first customers", detail: "A plan is optional.",
      chat_action: "outreach.draft", payload: null };
    const rendered = nodes(await page(state, undefined, "current", undefined, { serverStep: step,
      findings: { source: [{ id: "source", title: "Saved related page", category: "source" }] },
      summary: { evidence_dimensions: { pricing: { status: "unknown" } } } }));
    const action = rendered.find(node => node.props["data-usage-control"] === "research_customer_interviews");
    assert.equal(action?.props.href, "/gtm?venture=venture");
    assert.equal(action?.props.className, "primary");
    assert.ok(!rendered.some(node => node.type === "NextStepButton" && node.props.step.chat_action === "plan.generate"));
    const rerun = rendered.find(node => node.props["data-usage-control"] === "research_rerun");
    assert.equal(rerun?.props.children, "Run research again");
    assert.notEqual(rerun?.props.className, "primary");
  }
});

test("personal and repo projects with a saved deep report keep the local build handoff", async () => {
  for (const state of ["complete", "partial"]) {
    const rendered = nodes(await page(state, undefined, "current", undefined, { recovery: { personal: true },
      summary: { evidence_dimensions: { pricing: { status: "unknown" } } },
      findings: { source: [{ id: "lead", title: "Related page", category: "source" }] },
    }));
    assert.ok(!rendered.some(node => node.type === "PageChoices"));
    assert.ok(!rendered.some(node => node.props["data-usage-control"] === "research_customer_interviews"));
    const action = rendered.find(node => node.props["data-usage-control"] === "research_next_step");
    assert.equal(action?.props.href, "/v/venture/idea");
    assert.ok(String(action?.props.children).includes("Review local build"));
  }
});

test("page choices require confirmed current deep research and a usable server receipt", async () => {
  for (const options of [{ serverStep: null }, { recovery: { state: "unknown" } },
    { recovery: { state: "clarifying" } }, { recovery: { error: "Saved idea could not be checked" } },
    ...["working", "queued"].map(runState => ({ serverStep: { key: "site.build", stage: "Page", title: "Choose",
      cta_label: "Build", href: "/v/venture/site?build=1", detail: "Saved next step", runState,
      alternatives: [{ choice: "build" }, { choice: "skip" }] } }))]) {
    const rendered = nodes(await page("complete", undefined, "current", undefined, options));
    assert.ok(!rendered.some(node => node.type === "PageChoices"));
    assert.ok(!rendered.some(node => node.props["data-usage-control"] === "research_next_step"));
  }
  for (const revision of ["changed", ""]) {
    const rendered = nodes(await page("complete", undefined, revision));
    assert.ok(!rendered.some(node => node.type === "PageChoices"));
  }
  const validation = nodes(await page("complete", undefined, "current", undefined, { kind: "validation" }));
  assert.ok(!validation.some(node => node.type === "PageChoices"));
  assert.equal(validation.find(node => node.props["data-usage-control"] === "research_next_step")?.props.href, "/v/venture/idea");
});

test("a server receipt retired by owner or epoch cannot expose page choices", async () => {
  for (const retired of ["account", "epoch", "signed-out"]) {
    const scope = { owner: "owner" as string | null, epoch: 1, path: "/v/venture/research" };
    const rendered = nodes(await page("complete", undefined, "current", undefined, { scope, afterLoad() {
      if (retired === "account") scope.owner = "another-owner";
      if (retired === "epoch") scope.epoch++;
      if (retired === "signed-out") scope.owner = null;
    } }));
    assert.ok(!rendered.some(node => node.type === "PageChoices"), retired);
  }
});

test("failed observation displays its saved failure card without repeating the same error as a notice", async () => {
  const rendered = nodes(await page("running", "leased", "current", "failed_dead"));
  assert.equal(rendered.filter(node => node.type === "OneCard" && node.props.title === "Research didn't finish").length, 1);
  assert.ok(!rendered.some(node => node.type === "p" && node.props.children === "Research didn't finish"));
});


test("research replaces empty topics with one constructive step and a customer action", async () => {
  const rendered = nodes(await page("complete", undefined, "current", undefined, {
    summary: { evidence_dimensions: { pricing: { status: "unknown" }, customer_demand: { status: "unknown" } } },
    findings: { source: [{ id: "one", title: "Related page", category: "source" }, { id: "two", title: "Another page", category: "source" }] },
  }));
  assert.ok(rendered.some(node => node.type === "OneCard" && node.props.body?.includes("Your next move: share a relevant link")));
  assert.equal(rendered.filter(node => node.props.className === "dimension").length, 0);
  const action = rendered.find(node => node.props["data-usage-control"] === "research_customer_interviews");
  assert.equal(action?.props.href, "/gtm?venture=venture");
});

test("research reopen control posts only the current saved session and revision before navigation", async () => {
  const requests: { path: string; body?: { method?: string; body?: string } }[] = [], navigated: string[] = [];
  const rendered = nodes(await page("complete", undefined, "current", undefined, {
    summary: { assumed_questions: true, idea_session_id: "session", idea_revision_id: "revision" },
    read: (path, body) => { requests.push({ path, body }); }, navigate: path => navigated.push(path),
  }));
  const action = rendered.find(node => node.props["data-usage-control"] === "research_questions_reopen");
  assert.ok(action);
  await action.props.onClick();
  await new Promise(setImmediate);
  const posted = requests.find(request => request.body?.method === "POST");
  assert.equal(posted?.path, "/api/ventures/venture/idea/sessions/session/reopen-questions");
  assert.deepEqual(JSON.parse(posted?.body?.body || "{}"), { revision_id: "revision" });
  assert.deepEqual(navigated, ["/v/venture/idea"]);
});


test("retired research reopen controls refuse effects at entry", async () => {
  for (const retired of ["account", "account-return", "path", "unmount", "generation"]) {
    const scope = { owner: "owner", epoch: 1, path: "/v/venture/research" };
    const posted: string[] = [];
    let cleanup = () => {}, advance = () => {};
    const rendered = nodes(await page("complete", undefined, "current", undefined, {
      summary: { assumed_questions: true, idea_session_id: "session", idea_revision_id: "revision" }, scope,
      read: (path, body) => { if (body?.method === "POST") posted.push(path); }, captureCleanup: saved => cleanup = saved, captureGeneration: saved => advance = saved,
    }));
    if (retired === "account") { scope.owner = "other"; scope.epoch++; }
    if (retired === "account-return") scope.epoch += 2;
    if (retired === "path") scope.path = "/v/other/research";
    if (retired === "unmount") cleanup();
    if (retired === "generation") advance();
    rendered.find(node => node.props["data-usage-control"] === "research_questions_reopen")!.props.onClick();
    await new Promise(setImmediate);
    assert.deepEqual(posted, [], retired);
  }
});

test("retired research reopen responses never navigate and duplicate clicks post once", async () => {
  for (const retired of ["account", "account-return", "path", "unmount", "generation"]) {
    const scope = { owner: "owner", epoch: 1, path: "/v/venture/research" };
    const posted: string[] = [], navigated: string[] = [];
    let cleanup = () => {}, advance = () => {}, finish!: () => void;
    const rendered = nodes(await page("complete", undefined, "current", undefined, {
      summary: { assumed_questions: true, idea_session_id: "session", idea_revision_id: "revision" }, scope,
      read: async (path, body) => { if (body?.method === "POST") { posted.push(path); await new Promise<void>(resolve => finish = resolve); } },
      captureCleanup: saved => cleanup = saved, captureGeneration: saved => advance = saved, navigate: path => navigated.push(path),
    }));
    const action = rendered.find(node => node.props["data-usage-control"] === "research_questions_reopen")!;
    action.props.onClick(); action.props.onClick();
    assert.equal(posted.length, 1);
    if (retired === "account") { scope.owner = "other"; scope.epoch++; }
    if (retired === "account-return") scope.epoch += 2;
    if (retired === "path") scope.path = "/v/other/research";
    if (retired === "unmount") cleanup();
    if (retired === "generation") advance();
    finish(); await new Promise(setImmediate);
    assert.deepEqual(navigated, [], retired);
  }
});

function ideaReopen() {
  const path = fileURLToPath(new URL("../app/v/[ventureId]/idea/page.tsx", import.meta.url));
  const source = readFileSync(path, "utf8"), parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let declaration: any;
  function visit(node: any) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === "reopenQuestions") declaration = node;
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  assert.ok(declaration);
  const scopeStart = source.indexOf("  const reopenOwner");
  const code = `exports.action = (() => { ${source.slice(scopeStart >= 0 ? scopeStart : declaration.getStart(parsed), declaration.end)}; return reopenQuestions; })();`;
  const scope = { owner: "owner", epoch: 1, path: "/v/venture/idea" };
  const requests: { path: string; body: { revision_id: string } }[] = [], updates: unknown[] = [], cleanups: (() => void)[] = [];
  const loadVersion = { current: 1 }, activeVenture = { current: "venture" }, pendingAction = { current: false };
  let finish!: (result: unknown) => void;
  const exports: any = {};
  const output = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(output, { exports, scope, loadVersion, activeVenture, pendingAction, ventureId: "venture",
    session: { id: "session", current_revision: { id: "revision" } }, busy: false, jobId: null, syncError: "", reconciling: false, hydrating: { current: false },
    verifiedSessionSubject: () => scope.owner, verifiedSessionEpoch: () => scope.epoch, usePathname: () => scope.path,
    window: { location: { get pathname() { return scope.path; } } }, useRef: (value: unknown) => ({ current: value }),
    useEffect: (callback: () => () => void) => cleanups.push(callback()), setBusy() {}, setNotice() {},
    setSession: (value: unknown) => updates.push(value), setPlanState() {}, setAnswer() {}, setSelectedOptions() {}, apiFailure: () => "failed",
    api: (path: string, body: { body: string }) => {
      requests.push({ path, body: JSON.parse(body.body) });
      return new Promise(resolve => finish = resolve);
    },
  });
  return { action: exports.action as () => Promise<void>, scope, requests, updates, loadVersion,
    cleanup: () => cleanups.forEach(cleanup => cleanup()), finish: () => finish({ ok: true, data: { id: "reopened" } }) };
}

test("idea reopening applies the returned review only in its current scope", async () => {
  const view = ideaReopen(), done = view.action();
  assert.deepEqual(view.requests, [{ path: "/api/ventures/venture/idea/sessions/session/reopen-questions", body: { revision_id: "revision" } }]);
  view.finish(); await done;
  assert.equal((view.updates[0] as { id: string }).id, "reopened");
});

test("retired idea reopen controls refuse entry and response effects", async () => {
  for (const retired of ["account", "account-return", "path", "unmount", "generation"]) {
    function retire(view: ReturnType<typeof ideaReopen>) {
      if (retired === "account") { view.scope.owner = "other"; view.scope.epoch++; }
      if (retired === "account-return") view.scope.epoch += 2;
      if (retired === "path") view.scope.path = "/v/other/idea";
      if (retired === "unmount") view.cleanup();
      if (retired === "generation") view.loadVersion.current++;
    }
    const entry = ideaReopen(); retire(entry); const ignored = entry.action();
    if (entry.requests.length) entry.finish();
    await ignored; assert.deepEqual(entry.requests, [], retired);
    const response = ideaReopen(), done = response.action();
    void response.action();
    assert.equal(response.requests.length, 1);
    retire(response); response.finish(); await done;
    assert.deepEqual(response.updates, [], retired);
  }
});
