import * as chatRoutes from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import test from "node:test";
import { editableIdeaAnswer } from "./ideaAnswer.ts";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { gtmRequestFence } from "./gtmDiagnosisIntent.ts";
import { contactsImportRequested } from "./founderAssetsApi.ts";
import { isLocalConsole } from "./localMode.ts";
import { journeyInitialStage, journeyReviewRequested, journeySendReason, safeEvidenceUrl } from "./gtmJourneyApi.ts";
import { plainText } from "./researchBrief.ts";
import { mailboxCanSend } from "./gmailMailboxes.ts";
import { reachAction, guessedEmailWarning } from "./reachableLeads.ts";
import * as gtmProgress from "./gtmProgress.ts";

const owner = { actorId: "usr_fixture", workspaceId: "ws_fixture", ventureId: "ven_fixture" };
const fixture = (error_code: string | null, prospects: any[] = []) => ({ venture_id: owner.ventureId, state: error_code ? "needs_input" : "ready", phase: "prospects",
  error_code, message: "Legacy ambiguous failure copy", icp: { who: "Independent bakery owners", problem: "Managing bakery orders by phone", confirmed: true, needs_input: false },
  prospects, drafts: [], batches: [], results: { sent: 0, replied: 0, replies: [] }, gmail: { connected: false }, daily_cap: 5,
  estimate: { basis: "Public research" } });

function harness(errorCode: string | null, prospects: any[] = [], overrides: Record<string, any> = {}, environment: { href?: string; icp?: () => Promise<any>; resume?: () => Promise<any>; container?: any } = {}) {
  const source = fs.readFileSync(new URL("../components/gtm/GtmJourney.tsx", import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  let hooks: any[] = [], result = { ...fixture(errorCode, prospects), ...overrides };
  const effects: (() => void)[] = [], calls: any[] = [], timers: (() => Promise<void>)[] = [], events: Record<string, () => void> = {};
  const confirmations: any[] = [];
  const reads: any[] = [], replaced: string[] = [], copied: string[] = [], opened: string[] = [];
  const location = environment.href ? { href: environment.href } : undefined;
  let cursor = 0;
  let currentOwner = owner;
  const same = (a: any[], b: any[]) => a?.length === b?.length && a.every((v, i) => v === b[i]);
  const react = {
    useState(initial: any) { const i = cursor++; hooks[i] ||= { value: initial }; return [hooks[i].value, (v: any) => { hooks[i].value = v; }]; },
    useRef(initial: any) { const i = cursor++; return hooks[i] ||= { current: initial }; },
    useCallback(fn: any, deps: any[]) { const i = cursor++; if (!hooks[i] || !same(hooks[i].deps, deps)) hooks[i] = { fn, deps }; return hooks[i].fn; },
    useEffect(fn: () => void, deps: any[]) { const i = cursor++; if (!hooks[i] || !same(hooks[i].deps, deps)) { hooks[i] = { deps }; effects.push(fn); } },
  };
  const jsx = (type: any, props: any) => ({ type, props });
  function importedComponent(filename: string): Record<string, any> {
    const exports: Record<string, any> = {};
    const source = fs.readFileSync(new URL(`../components/gtm/${filename}.tsx`, import.meta.url), "utf8");
    vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
      exports, navigator: { clipboard: { writeText: async (text: string) => { copied.push(text); } } },
      require(id: string) {
    if (id === "@/components/chat/ChatLink") return { default: "link", ChatAnchor: "a", captureChatLink() {} };

        if (id === "react") return react;
        if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
        if (id.endsWith("/gtmJourneyApi")) return { safeEvidenceUrl };
        if (id.endsWith("/gmailMailboxes")) return { mailboxCanSend };
        if (id.endsWith("/reachableLeads")) return { guessedEmailWarning };
        if (id.endsWith("/ErrorNotice")) return { ErrorNotice: "error", useReportedError() {} };
        if (id.endsWith("/DictateButton")) return { DictateButton: "dictate" };
        if (id.endsWith(".css")) return { default: { button: "button", secondary: "secondary" } };
        throw new Error(id);
      },
    });
    return exports;
  }
  const draftComponents = importedComponent("OutreachDraft"), callComponents = importedComponent("CallNotesForm");
  const knownComponents = importedComponent("../chat/KnownAnswer");
  const client = { get: async () => { reads.push(result); return result; },
    prepare: async (...args: any[]) => { calls.push(["prepare", ...args]); return { ...fixture(null), state: "queued" }; },
    icp: async (...args: any[]) => { calls.push(["icp", ...args]); return environment.icp ? environment.icp() : { ...fixture(null), state: "queued" }; },
    policy: async (...args: any[]) => { calls.push(["policy", ...args]); return { ...result, processing_blocked_reason: null }; },
    askSending: async (...args: any[]) => { calls.push(["askSending", ...args]); return result; },
    resumeSending: async (...args: any[]) => { calls.push(["resumeSending", ...args]); return environment.resume ? environment.resume() : { ...result, sending_pause: null, sending_alert: null }; },
    contact: async (...args: any[]) => { calls.push(["contact", ...args]); return result; },
    eligibility: async (...args: any[]) => { calls.push(["eligibility", ...args]); return result; },
    confirmProspects: async (...args: any[]) => { calls.push(["confirmProspects", ...args]); return result; } };
  const exports: any = {};
  vm.runInNewContext(code, { exports, URL, CustomEvent: class { detail: unknown; constructor(_name: string, options: any) { this.detail = options.detail; } }, navigator: { clipboard: { writeText: async (text: string) => { copied.push(text); } } }, setTimeout: (fn: () => Promise<void>) => { timers.push(fn); return timers.length; }, clearTimeout() {},
    window: { location, dispatchEvent(event: any) { confirmations.push(event.detail); }, open(url: string) { opened.push(url); }, history: { replaceState(_state: unknown, _title: string, url: URL) { replaced.push(String(url)); } }, addEventListener(name: string, fn: () => void) { events[name] = fn; }, removeEventListener(name: string) { delete events[name]; } },
    require(id: string) {
    if (id === "@/components/chat/ChatLink") return { default: "link", ChatAnchor: "a", captureChatLink() {} };

      if (id === "@/lib/gtmProgress") return gtmProgress;
      if (id === "@/lib/client") return { verifiedSessionEpoch: () => 1 };
      if (id === "@/lib/observability") return { observeFounder(detail: unknown) { confirmations.push(detail); } };
      if (id === "react") return react;
      if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (id === "next/link") return { default: "link" };
      if (id.endsWith("/ResearchProgress")) return { ResearchProgress: "research-progress" };
      if (id.endsWith("/gtmDiagnosisIntent")) return { gtmRequestFence };
      if (id.endsWith("/gtmJourneyApi")) return { journeyClient: client, journeyInitialStage, journeyReviewRequested, safeEvidenceUrl, journeySendReason };
      if (id.endsWith("/localMode")) return { isLocalConsole };
      if (id.endsWith("/reachableLeads")) return { reachAction, guessedEmailWarning };
      if (id.endsWith("/researchBrief")) return { plainText };
      if (id.endsWith("/runStatusPresentation")) return { RUN_STATUS_REFRESH: "supymem-run-status-refresh" };
      if (id.endsWith("/ideaAnswer")) return { editableIdeaAnswer };
      if (id.endsWith("/DictateButton")) return { DictateButton: "dictate" };
      if (id.endsWith("/NextStepCard")) return { NextStepCard: "next-step" };
      if (id.endsWith("/OutreachDraft")) return draftComponents;
      if (id.endsWith("/CallNotesForm")) return callComponents;
      if (id.endsWith("/OneCard")) return { OneCard: "card" };
      if (id.endsWith("/KnownAnswer")) return knownComponents;
      if (id.endsWith("/ErrorNotice")) return { ErrorNotice: "error", useReportedError() {} };
      if (id.endsWith("/SimulatedReaderCheck")) return { SimulatedReaderCheck: "simulation" };
      if (id.endsWith("/ImportContacts")) return { ImportContacts: "contactImport" };
      if (id.endsWith("/founderAssetsApi")) return { contactsImportRequested, founderAssetsClient: { contactHistory: async (...args: any[]) => { calls.push(["history", ...args]); return result; }, verifyContactEmail: async (...args: any[]) => { calls.push(["verifyEmail", ...args]); return result; } } };
      if (id.endsWith(".css")) return { default: { button: "button", secondary: "secondary" } };
      throw new Error(id);
    },
  });
  const render = () => {
    cursor = 0;
    const tree = exports.GtmJourney({ owner: currentOwner });
    if (tree.props.ref) tree.props.ref.current = environment.container || null;
    while (effects.length) effects.shift()!();
    return tree;
  };
  const flush = () => new Promise<void>(resolve => setImmediate(resolve));
  function expanded(node: any): any {
    if (Array.isArray(node)) return node.map(expanded);
    if (!node?.props) return node;
    if (typeof node.type === "function") {
      const saved = hooks, position = cursor;
      hooks = []; cursor = 0;
      let child;
      try { child = node.type(node.props); } finally { hooks = saved; cursor = position; }
      return expanded(child);
    }
    return { ...node, props: Object.fromEntries(Object.entries(node.props).map(([key, value]) => [key, expanded(value)])) };
  }
  return { render, expanded, calls, timers, reads, replaced, confirmations, copied, opened, switchOwner(next: typeof owner) { currentOwner = next; result = { ...fixture(errorCode, prospects), ...overrides, venture_id: next.ventureId }; }, focus: () => events.focus?.(), navigate(href: string) { if (location) location.href = href; }, retire() { events["supymem-session-changed"]?.(); }, async mount() { render(); await flush(); return render(); }, flush,
    update(next: Record<string, any>) { result = { ...result, ...next }; },
    async poll() { if (timers.length) await timers.shift()!(); else events.focus?.(); await flush(); return render(); } };
}

function nodes(value: any): any[] {
  if (!value || typeof value !== "object") return [];
  if (Array.isArray(value)) return value.flatMap(nodes);
  return [value, ...Object.values(value.props || {}).flatMap(nodes)];
}
const action = (tree: any, text: string) => nodes(tree).find(n => n.type === "button" && [n.props.children].flat(Infinity).join("") === text);
const card = (tree: any) => nodes(tree).find(n => n.type === "card");

test("consumer possible signals and reachable cards render separately below strong results", async () => {
  const signal = { source_url: "https://forum.example/possible", quote: "Possible need quote", evidence_label: "possible_fit", recency: "recent",
    draft: "Does this problem apply to your experience?", confidence: "observed" };
  const strong = { ...signal, source_url: "https://forum.example/strong", quote: "Strong need quote", evidence_label: undefined, draft: "What have you tried?" };
  const lead = { id: "possible", name: "Possible Person", company: "", role: "", location: "", fit: 95, why: "Assumed problem", quote: "Possible lead quote",
    source_url: signal.source_url, evidence_label: "possible_fit", routes: [], draft: signal.draft, connection_note: signal.draft };
  const strongLead = { ...lead, id: "strong", name: "Strong Person", quote: "Strong lead quote", evidence_label: "need_passage" };
  const h = harness(null, [], { icp: { who: "Parents", confirmed: true, mode: "consumer" }, need_signals: [signal, strong], reachable_leads: [lead, strongLead] });
  const tree = h.expanded(await h.mount());
  const groups = nodes(tree).filter(node => node.type === "section" && nodes(node).some(child => child.type === "h4" && child.props.children === "Possible fit, check before reaching out"));
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.flatMap(group => nodes(group).filter(node => node.type === "blockquote").map(node => node.props.children)).sort(), ["Possible lead quote", "Possible need quote"]);
  assert.equal(nodes(tree).find(node => node.type === "h4")!.props.children, "Possible fit, check before reaching out");
  const quotes = nodes(tree).filter(node => node.type === "blockquote").map(node => node.props.children);
  assert.ok(quotes.indexOf("Strong need quote") < quotes.indexOf("Possible need quote"));
  assert.ok(quotes.indexOf("Strong lead quote") < quotes.indexOf("Possible lead quote"));
  assert.ok(nodes(groups).some(node => node.type === "p" && JSON.stringify(node.props).includes("Does this problem apply")));
  assert.ok(!JSON.stringify(groups).includes("Assumed problem"));
  assert.ok(!JSON.stringify(groups).includes("—"));
  action(tree, "Review reachable leads").props.onClick();
  const review = h.expanded(h.render());
  assert.equal(nodes(review).filter(node => node.type === "h4" && node.props.children === "Possible fit, check before reaching out").length, 2);
  assert.match(renderedText(review), /Does this problem apply to your experience\?/);
});

test("D16 only offers reachable-lead review when saved people exist", async () => {
  for (const mode of ["consumer", "business"]) {
    const icp = { who: "Bakeries", confirmed: true, mode };
    const empty = harness(null, [], { icp, reachable_leads: [], industry_experts: [], channels: [], need_signals: [] });
    assert.equal(action(await empty.mount(), "Review reachable leads"), undefined);
    const community = harness(null, [], { icp, channels: [{ url: "https://example.com/bakers", name: "Bakers" }] });
    const communityTree = await community.mount();
    assert.equal(action(communityTree, "Review reachable leads"), undefined);
    assert.equal(action(communityTree, "Review communities").props.disabled, false);
    const people = harness(null, [], { icp, reachable_leads: [{ id: "baker", name: "Ada", person: "Ada" }] });
    assert.equal(action(await people.mount(), "Review reachable leads").props.disabled, false);
  }
});

test("Find customers renders the quoted cost note once, inside the current card", async () => {
  const basis = "Uses a small amount of credits for public searches. Nothing is posted or sent.";
  for (const state of ["needs_input", "running"]) {
    const h = harness(null, [], { state, discovery_started: false, estimate: { basis }, icp: { who: "Bakeries", problem: "Lost orders", confirmed: false, needs_input: true } });
    const tree = await h.mount();
    assert.equal((renderedText(tree) + renderedText(card(tree).props.details)).split(basis).length - 1, 1);
  }
});

test("legacy assumed prefills hide wire prefixes and an empty Confirmed idea label", async () => {
  const h = harness(null, [], { state: "idle", discovery_started: false,
    icp: { who: "Assumed: First-time parents in the US", problem: "Assumed: New parents experience anxiety",
      problem_basis: "Confirmed idea", confirmed: false, needs_input: true } });
  const tree = await h.mount();
  assert.match(renderedText(tree), /First-time parents in the US/);
  assert.match(renderedText(tree), /New parents experience anxiety/);
  assert.equal(nodes(tree).some(node => ["Who should we target?", "What problem do you solve?"].includes(node.props?.["aria-label"])), false);
  assert.doesNotMatch(renderedText(tree), /Confirmed idea|Assumed:/);
});

test("unknown audience and problem defaults stay empty, with audience suggestions only as placeholders", async () => {
  for (const answer of ["Not sure", "I don't know", "unsure", "n/a", "skip", "-", "", "Assumed: Not sure"]) {
    const h = harness(null, [], { state: "idle", discovery_started: false,
      icp: { who: answer, suggested_who: "Independent bakery owners", problem: answer,
        problem_basis: "From your saved description.", confirmed: false, needs_input: true } });
    const tree = await h.mount();
    const audience = nodes(tree).find(node => node.props?.["aria-label"] === "Who should we target?");
    assert.equal(audience.props.value, "");
    assert.equal(audience.props.placeholder, "Independent bakery owners");
    assert.equal(nodes(tree).some(node => node.props?.["aria-label"] === "What problem do you solve?"), false);
    assert.doesNotMatch(renderedText(tree), /From your saved description\./);
    assert.equal(action(tree, "Continue").props.disabled, true);
  }
});

test("U30 hides an empty clarification label", async () => {
  const h = harness(null, [], { state: "idle", discovery_started: false,
    icp: { who: "Busy US parents", problem: "", problem_basis: "Your clarification", confirmed: false, needs_input: true } });
  assert.doesNotMatch(renderedText(await h.mount()), /Your clarification/);
});

test("U30 hides bare provenance labels even with an unrelated non-empty problem", async () => {
  for (const basis of ["Your clarification", "Your saved idea", "from your saved description", "Founder supplied"]) {
    const h = harness(null, [], { state: "idle", discovery_started: false,
      icp: { who: "Busy US parents", problem: "Parents lose sleep logs", problem_basis: basis, confirmed: false, needs_input: true } });
    assert.ok(!renderedText(await h.mount()).includes(basis));
  }
});

test("U30 shows a helpful basis only while its non-empty prefill is unchanged", async () => {
  const basis = "From your clarification answer.";
  const h = harness(null, [], { state: "idle", discovery_started: false,
    icp: { who: "Busy US parents", problem: "Parents lose sleep logs", problem_basis: basis, confirmed: false, needs_input: true } });
  const tree = await h.mount();
  assert.ok(renderedText(tree).includes(basis));
  action(tree, "Edit problem").props.onClick();
  nodes(h.render()).find(node => node.props?.["aria-label"] === "What problem do you solve?").props.onChange({ target: { value: "A different problem" } });
  assert.ok(!renderedText(h.render()).includes(basis));
});

test("U34 consumer review count matches its five-step progress", async () => {
  const h = harness(null, [], { phase: "drafts", icp: { who: "Busy US parents", problem: "Planning toddler meals", mode: "consumer", confirmed: true },
    channels: [{ name: "r/toddlers", url: "https://www.reddit.com/r/toddlers/", draft: "Open to a call?", participation: "Read the rules." }] });
  const initial = await h.mount();
  action(initial, "Review communities").props.onClick();
  await h.flush();
  const tree = h.render();
  assert.equal(nodes(tree).find(node => node.type === "card" && node.props.title === "Book your first conversation").props.step, "3 of 5 · Review");
  const progress = nodes(tree).find(node => node.type === "ol" && node.props["aria-label"] === "Go-to-market progress");
  assert.equal(nodes(progress).filter(node => node.type === "li").length, 5);
  assert.deepEqual(h.calls, []);
});

test("results scroll and focus the heading, not the receipt above it, in the persistent workspace", async () => {
  const scrolls: any[] = [], focuses: any[] = [];
  let tree: any;
  const heading = { tabIndex: 0, scrollIntoView: (options: any) => scrolls.push(["heading", options]), focus: (options: any) => focuses.push(options) };
  const container = {
    scrollIntoView: (options: any) => scrolls.push(["wrapper", options]),
    querySelector(selector: string) {
      assert.equal(selector, "#gtm-journey-results h2");
      return heading;
    },
  };
  const h = harness(null, [], { results: { sent: 1, replied: 0, replies: [] }, receipt: { body_md: "Saved send receipt" } }, { container });
  tree = h.render();
  await h.flush();
  tree = h.render();
  assert.equal(card(tree).props.title, "What happened after outreach");
  assert.equal(card(tree).props.id, "gtm-journey-results");
  assert.deepEqual(JSON.parse(JSON.stringify(scrolls)), [["heading", { block: "center", behavior: "auto" }]]);
  assert.equal(heading.tabIndex, -1);
  assert.deepEqual(JSON.parse(JSON.stringify(focuses)), [{ preventScroll: true }]);
  scrolls.length = 0;
  action(tree, "Review more outreach").props.onClick();
  h.render();
  assert.deepEqual(JSON.parse(JSON.stringify(scrolls)), [["wrapper", { block: "start", behavior: "auto" }]]);
});

test("signal leads expose why-now, source, confidence and separately published contact", async () => {
  const h = harness(null, [{ id: "signal", name: "Bakery", company: "Acme", email: "hello@acme.example",
    why_now: "2026-10-01: Hiring need (inferred) — We need order help", confidence: "inferred", source_url: "https://forum.example/post",
    signal: { quote: "We need order help" }, person: "Jane Doe", person_role: "Founder", person_source_url: "https://acme.example/team",
    contact_source_url: "https://acme.example/contact", email_status: "published", blocked_reason: "unknown_jurisdiction" }]);
  const tree = await h.mount(), text = renderedText(tree);
  assert.match(text, /Hiring need \(inferred\)/);
  assert.match(text, /Inferred signal/);
  assert.match(text, /Jane Doe/);
  assert.match(text, /Published · hello@acme.example/);
  const links = nodes(tree).filter(n => n.type === "a").map(n => n.props.href);
  assert.ok(links.includes("https://forum.example/post"));
  assert.ok(links.includes("https://acme.example/team"));
  assert.ok(links.includes("https://acme.example/contact"));
});

test("Find customers shows progress and first leads before the run finishes", async () => {
  const progress = { state: "running", percent: 42, step_label: "Checking customer fit", started_at: null };
  const h = harness(null, [], { state: "running", discovery_started: true, research_progress: progress,
    discovery_notice: "We also checked public web sources.", reachable_leads: [{ id: "early", name: "Engineering owner",
      company: "Design Consultancy", role: "Owner", quote: "Reviews structural drawings", source_url: "https://company.example/",
      draft: "Open to a short call?", connection_note: "Open to a short call?", x_note: "Open to a short call?", routes: [{ kind: "linkedin", url: "https://www.linkedin.com/in/owner", source_url: "https://www.linkedin.com/in/owner" }] }] });
  const tree = await h.mount();
  const current = nodes(tree).find(node => node.type === "research-progress");
  assert.equal(current.props.progress, progress);
  assert.match(renderedText(h.expanded(tree)), /Engineering owner/);
  assert.match(renderedText(tree), /public web sources/);
  assert.ok(action(tree, "Refresh saved work"));
  assert.equal(h.calls.length, 0);
});

function renderedText(tree: any): string {
  if (typeof tree === "string") return tree;
  if (Array.isArray(tree)) return tree.map(renderedText).join(" ");
  if (!tree?.props) return "";
  return [tree.props.body, tree.props.title, tree.props.children, tree.props.actions].map(renderedText).join(" ");
}

test("fresh discovery requires a click, survives refresh and focus, and includes the optional note", async () => {
  const fresh = { state: "idle", discovery_started: false, venture_name: "Supymem" };
  const h = harness(null, [], fresh), tree = await h.mount();
  assert.equal(card(tree).props.title, "Find customers for Supymem");
  assert.match(card(tree).props.body, /people who might need your product.*public sources.*Nothing is sent/);
  assert.equal(h.calls.length, 0);
  h.focus(); await h.flush(); await h.poll();
  assert.equal(h.calls.length, 0);
  assert.equal(h.timers.length, 0);
  const note = nodes(tree).find(n => n.type === "textarea" && n.props.maxLength === 2000);
  assert.equal(note.props.required, undefined);
  note.props.onChange({ target: { value: "  Avoid Acme, an existing customer  " } });
  action(h.render(), "Find customers").props.onClick();
  action(h.render(), "Find customers").props.onClick();
  await h.flush();
  assert.deepEqual(h.calls, [["prepare", owner, false, "Avoid Acme, an existing customer", true]]);
  assert.equal(card(h.render()).props.status, "working");
  const refreshed = harness(null, [], fresh); await refreshed.mount();
  assert.equal(refreshed.calls.length, 0);
});

test("a recorded audience can be edited and started once without a second confirmation", async () => {
  const h = harness(null, [], { state: "idle", discovery_started: false, icp: { ...fixture(null).icp, confirmed: false } });
  const tree = await h.mount();
  action(tree, "Edit audience").props.onClick();
  const audience = nodes(h.render()).find(n => n.type === "textarea" && n.props.maxLength === 600);
  assert.equal(audience.props.value, "Independent bakery owners");
  audience.props.onChange({ target: { value: "  Independent bakery owners managing orders by phone  " } });
  nodes(h.render()).find(n => n.type === "textarea" && n.props.maxLength === 2000).props.onChange({ target: { value: "Avoid chains" } });
  action(h.render(), "Find customers").props.onClick();
  action(h.render(), "Find customers").props.onClick();
  await h.flush();
  assert.deepEqual(h.calls, [["icp", owner, "Independent bakery owners managing orders by phone", undefined, "Avoid chains", true, undefined]]);
  h.update({ state: "ready", discovery_started: true, prospects: [{ id: "source", name: "Source only", blocked_reason: "published_email_missing" }] });
  await h.poll();
  assert.equal(card(h.render()).props.title, "1 prospect to review");
  assert.equal(action(h.render(), "Save audience and prepare"), undefined);
});

test("policy acceptance and delayed reads do not start fresh discovery", async () => {
  const h = harness(null, [], { state: "idle", discovery_started: false, processing_blocked_reason: "aup_required" });
  await h.mount();
  nodes(h.render()).find(n => n.type === "input" && n.props.type === "checkbox").props.onChange({ target: { checked: true } });
  action(h.render(), "Save policy acceptance").props.onClick(); await h.flush();
  assert.deepEqual(h.calls.map(call => call[0]), ["policy"]);
  assert.ok(action(h.render(), "Find customers"));
  h.focus(); await h.flush();
  assert.deepEqual(h.calls.map(call => call[0]), ["policy"]);
});

test("an existing discovery shows saved progress and only polls", async () => {
  const h = harness(null, [], { state: "running", discovery_started: true, message: "Checking public details: 2 of 15 companies complete." });
  const tree = await h.mount();
  assert.equal(card(tree).props.status, "working");
  assert.match(card(tree).props.body, /2 of 15/);
  assert.equal(action(tree, "Find customers"), undefined);
  assert.equal(h.calls.length, 0);
  assert.equal(h.timers.length, 1);
});

test("missing audience and optional guidance start together only from Find customers", async () => {
  const h = harness(null, [], { state: "idle", discovery_started: false, icp: { ...fixture(null).icp, who: "", needs_input: true } });
  await h.mount();
  assert.equal(action(h.render(), "Find customers").props.disabled, true);
  const fields = nodes(h.render()).filter(node => node.type === "textarea");
  fields[0].props.onChange({ target: { value: "Independent bakery owners" } });
  fields[1].props.onChange({ target: { value: "Avoid existing customer Acme" } });
  action(h.render(), "Find customers").props.onClick(); await h.flush();
  assert.deepEqual(h.calls, [["icp", owner, "Independent bakery owners", undefined, "Avoid existing customer Acme", true, undefined]]);
});

test("saved-description problem is editable in the start card without hiding the recorded audience", async () => {
  const h = harness(null, [], { state: "idle", discovery_started: false, venture_name: "BrewBox",
    icp: { who: "Home espresso enthusiasts", problem: "Fresh coffee subscriptions", problem_basis: "From your saved description.", confirmed: false, needs_input: false } });
  const tree = await h.mount();
  assert.match(renderedText(tree), /From your saved description\./);
  action(tree, "Edit problem").props.onClick();
  const problem = nodes(h.render()).find(node => node.props?.["aria-label"] === "What problem do you solve?");
  assert.equal(problem.props.value, "Fresh coffee subscriptions");
  assert.equal(problem.props["data-usage-control"], "gtm_journey_problem");
  assert.equal(action(tree, "Find customers").props.disabled, false);
  problem.props.onChange({ target: { value: "  Finding fresh coffee for a home espresso machine  " } });
  action(h.render(), "Find customers").props.onClick(); await h.flush();
  assert.deepEqual(h.calls, [["icp", owner, "Home espresso enthusiasts", undefined, "", true, "Finding fresh coffee for a home espresso machine"]]);
});

test("a missing problem keeps the saved audience and shows an editable required field", async () => {
  const h = harness(null, [], { state: "needs_input", discovery_started: false,
    icp: { ...fixture(null).icp, problem: "", needs_input: true } });
  const tree = await h.mount();
  assert.match(renderedText(tree), /Independent bakery owners/);
  assert.equal(nodes(tree).some(node => node.props?.["aria-label"] === "Who should we target?"), false);
  assert.equal(action(tree, "Find customers").props.disabled, true);
  assert.match(renderedText(tree), /Tell us what problem you solve/);
  const problem = nodes(tree).find(node => node.props?.["aria-label"] === "What problem do you solve?");
  assert.equal(problem.props.required, true);
  problem.props.onChange({ target: { value: "Managing orders without lost phone messages" } });
  assert.equal(action(h.render(), "Find customers").props.disabled, false);
  action(h.render(), "Find customers").props.onClick(); await h.flush();
  assert.equal(h.calls[0][2], "Independent bakery owners");
  assert.equal(h.calls[0][6], "Managing orders without lost phone messages");
});

test("existing-customer drafts appear before discovery and remain available after it", async () => {
  const customer_start = { referral_draft: "A permission-first referral", review_draft: "An honest review", lookalikes_note: "Find similar needs" };
  const h = harness(null, [], { state: "idle", discovery_started: false, customer_start });
  let tree = h.expanded(await h.mount());
  assert.equal(card(tree).props.title, "Start from your existing customers");
  assert.ok(action(tree, "Find customers"));
  assert.equal(nodes(tree).filter(node => node.type === "card" && node.props.title === "Start from your existing customers").length, 1);
  assert.deepEqual(h.calls, []);
  h.update({ state: "ready", discovery_started: true, customer_start });
  h.focus(); await h.flush();
  tree = h.expanded(h.render());
  assert.equal(card(tree).props.title, "Start from your existing customers");
  assert.equal(nodes(tree).filter(node => node.type === "card" && node.props.title === "Start from your existing customers").length, 1);
});

test("failed public search is shown once and primary Retry search submits the unchanged audience once", async () => {
  const h = harness("public_search_unavailable"), tree = await h.mount();
  const message = "Public search is busy. Retry search in a moment.";
  assert.equal(renderedText(tree).split(message).length - 1, 1);
  assert.doesNotMatch(renderedText(tree), /Legacy ambiguous|Retry preparation|Edit audience|public_search_unavailable/);
  const retry = action(tree, "Retry search");
  assert.ok(retry); assert.ok(!retry.props.className.includes("secondary"));
  retry.props.onClick(); retry.props.onClick(); await h.flush();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], "prepare"); assert.equal(h.calls[0][1], owner); assert.equal(h.calls[0][2], true);
  assert.equal(card(h.render()).props.status, "working");
});

test("blocked public search has no red status or retry and makes contact entry primary", async () => {
  const h = harness("public_search_blocked"), tree = await h.mount();
  assert.equal(card(tree).props.status, undefined);
  assert.match(card(tree).props.body, /Add a contact you know/);
  assert.equal(action(tree, "Retry search"), undefined);
  const add = action(tree, "Add a contact you know");
  assert.ok(add && !add.props.className.includes("secondary"));
  assert.equal(h.timers.length, 0);
});

test("friendly search progress is displayed and searches remain editable", async () => {
  const h = harness("no_public_candidates", [], { discovery_started: true, discovery_summary: "Searching business websites in Bengaluru.", discovery_queries: ['site:linkedin.com/in intitle:"owner" -jobs'], icp: { ...fixture(null).icp, search_queries: ["keyword fallback"] } });
  const tree = await h.mount();
  assert.match(renderedText(tree), /Searching business websites in Bengaluru/);
  assert.ok(!renderedText(tree).includes("site:") && !renderedText(tree).includes("-jobs"));
  assert.ok(!renderedText(tree).includes("keyword fallback"));
  action(tree, "Edit searches").props.onClick();
  const editing = h.render();
  const field = nodes(editing).find(n => n.type === "textarea" && n.props["aria-label"] === "Search queries");
  assert.ok(field);
  field.props.onChange({ target: { value: "bakeries Pune\nbakery order management" } });
  action(h.render(), "Save searches and search").props.onClick(); await h.flush();
  assert.deepEqual(h.calls[0].slice(0, 3), ["icp", owner, "Independent bakery owners"]);
  assert.equal(JSON.stringify(h.calls[0][3]), JSON.stringify(["bakeries Pune", "bakery order management"]));
});

test("polling runs only while preparation is active and restarts after a queued action", async () => {
  const h = harness("public_search_unavailable");
  const tree = await h.mount();
  assert.equal(h.timers.length, 0);
  action(tree, "Retry search").props.onClick(); await h.flush(); h.render();
  assert.equal(h.timers.length, 1);
});

test("a legacy skip link requests founder confirmation and never silently saves the choice", async () => {
  const h = harness(null, [], {}, { href: "https://console.example/gtm?venture=ven_fixture&skip_page=1" });
  await h.mount();
  assert.deepEqual(h.calls, []);
  assert.equal(h.confirmations[0].action, "journey.skip_step");
  assert.equal(h.confirmations[0].payload.step, "landing_page");
  assert.equal(h.reads.length, 1);
  assert.equal(h.replaced[0], "https://console.example/gtm?venture=ven_fixture");
});

test("generic or mismatched GTM visits cannot persist a page skip", async () => {
  for (const href of ["https://console.example/gtm?venture=ven_fixture", "https://console.example/gtm?venture=other&skip_page=1"]) {
    const h = harness(null, [], {}, { href }); await h.mount();
    assert.equal(h.calls.length, 0); assert.equal(h.replaced.length, 0);
  }
});

test("retired legacy skip-link callbacks cannot ask on another account", async () => {
  const h = harness(null, [], {}, { href: "https://console.example/gtm?venture=ven_fixture&skip_page=1" });
  await h.mount(); h.retire();
  const before = h.confirmations.length;
  h.focus(); await h.flush();
  assert.equal(h.confirmations.length, before);
});

test("retired focus and polling callbacks cannot start old-owner transport", async () => {
  const h = harness(null, [], { state: "queued" }); await h.mount();
  const before = h.reads.length;
  h.retire(); h.focus(); await h.flush(); await h.poll();
  assert.equal(h.reads.length, before);
});

test("legacy skip-link focus callback cannot overwrite navigation away from GTM", async () => {
  const h = harness(null, [], {}, { href: "https://console.example/gtm?venture=ven_fixture&skip_page=1" });
  await h.mount(); const before = h.reads.length;
  h.navigate("https://console.example/site?venture=ven_fixture"); h.focus(); await h.flush();
  assert.equal(h.reads.length, before);
});

test("legacy skip-link refresh preserves changed query parameters", async () => {
  const h = harness(null, [], {}, { href: "https://console.example/gtm?venture=ven_fixture&skip_page=1" });
  await h.mount(); const before = h.replaced.length;
  h.navigate("https://console.example/gtm?venture=ven_fixture&view=summary"); h.focus(); await h.flush();
  assert.equal(h.replaced.length, before);
});

test("a retired query-save response cannot clear the new account's search editor", async () => {
  let finish!: (next: any) => void;
  const pending = new Promise(resolve => { finish = resolve; });
  const h = harness("no_public_candidates", [], {}, { href: "https://console.example/gtm?venture=ven_fixture", icp: () => pending });
  action(await h.mount(), "Edit searches").props.onClick();
  action(h.render(), "Save searches and search").props.onClick(); await h.flush();
  h.retire();
  const nextOwner = { actorId: "next_actor", workspaceId: "next_workspace", ventureId: "next_venture" };
  h.switchOwner(nextOwner); h.navigate("https://console.example/gtm?venture=next_venture");
  h.render(); await h.flush();
  action(h.render(), "Edit searches").props.onClick();
  const field = nodes(h.render()).find(n => n.type === "textarea" && n.props["aria-label"] === "Search queries");
  field.props.onChange({ target: { value: "clinic owners Pune" } });
  finish({ ...fixture(null), state: "queued" }); await h.flush();
  const saved = nodes(h.render()).find(n => n.type === "textarea" && n.props["aria-label"] === "Search queries");
  assert.equal(saved?.props.value, "clinic owners Pune");
});

test("no candidates offers audience refinement and explicitly searches after saving it", async () => {
  const h = harness("no_public_candidates"), tree = await h.mount();
  assert.match(card(tree).props.body, /Refine the audience or add a contact/);
  assert.ok(!action(tree, "Refine audience").props.className.includes("secondary"));
  assert.ok(action(tree, "Search again").props.className.includes("secondary"));
  action(tree, "Refine audience").props.onClick();
  const editing = h.render(); assert.equal(card(editing).props.title, "Who should hear from you?");
  assert.equal(h.calls.length, 0); assert.ok(action(editing, "Save audience and search"));
  action(editing, "Save audience and search").props.onClick(); await h.flush();
  assert.equal(h.calls[0][0], "icp"); assert.equal(h.calls[0][2], "Independent bakery owners");
});

test("a saved contact clears stale search failure and describes reuse of those contacts honestly", async () => {
  const h = harness("no_public_candidates", [{ id: "contact_fixture", name: "Bakery Fixture" }]), tree = await h.mount();
  assert.equal(card(tree).props.status, undefined);
  assert.doesNotMatch(renderedText(tree), /No public company pages|Retry search|Refine audience|Search again/);
  assert.ok(action(tree, "Prepare drafts again"));
  action(tree, "Edit audience and prepare").props.onClick();
  assert.ok(action(h.render(), "Save audience and prepare"));
});

const savedDraft = { doc_id: "draft_fixture", to: "ada@bakery.example", subject: "A bakery question", body: "Synthetic saved draft",
  state: "draft", review_state: "approved", batch_id: "batch_fixture", revision: "revision_fixture" };
const savedProspect = { id: "contact_fixture", name: "Bakery Fixture", blocked_reason: "unknown_jurisdiction" };
const batch = { batch_id: "batch_fixture", approved_count: 1 };
const primaryControls = (tree: any) => nodes(tree).filter(n => ["button", "link"].includes(n.type)
  && n.props.className?.split(/\s+/).includes("button") && !n.props.className.includes("secondary"));

test("suggestion Review link keeps consumer outreach manual despite legacy recipient-less drafts", async () => {
  const h = harness("gtm_preparation_failed", [], { state: "failed", icp: { who: "Parents", confirmed: false, mode: "consumer" },
    drafts: [{ ...savedDraft, contact_id: null, to: "", awaiting_recipient: true, review_state: "draft" }], batches: [] },
    { href: "https://console.example/gtm?venture=ven_fixture&step=review" });
  const tree = await h.mount(), expanded = h.expanded(tree);
  assert.equal(card(tree).props.title, "Book your first conversation");
  assert.match(renderedText(expanded), /send it yourself/);
  assert.equal(action(expanded, "Approve message"), undefined);
  assert.equal(action(tree, "Review sending · 0 approved"), undefined);
});

test("a failed preparation loaded with saved drafts offers recovery once and still allows draft review", async () => {
  const h = harness("gtm_preparation_failed", [savedProspect], { state: "failed", drafts: [savedDraft], batches: [batch] });
  const tree = await h.mount();
  assert.equal(card(tree).props.status, "error");
  assert.equal(renderedText(tree).split("Preparation could not finish. Your saved work is kept.").length - 1, 1);
  assert.ok(action(tree, "Retry preparation"));
  assert.equal(primaryControls(h.expanded(tree)).length, 1, "expanded contact/address/permission helpers remain secondary");
  action(tree, "Review 1 draft").props.onClick();
  assert.equal(card(h.render()).props.title, "Make the first message count");
  assert.equal(card(await h.poll()).props.title, "Make the first message count", "the same failure does not repeatedly pull the founder away from saved drafts");
});

test("failure arriving during saved draft review moves to recovery and retry posts once", async () => {
  const h = harness(null, [savedProspect], { drafts: [savedDraft], batches: [batch] });
  assert.equal(card(await h.mount()).props.title, "Make the first message count");
  h.update({ state: "failed", error_code: "gtm_preparation_failed", job_id: "job_failed_fixture" });
  const failed = await h.poll();
  assert.equal(card(failed).props.status, "error");
  action(failed, "Retry preparation").props.onClick();
  action(failed, "Retry preparation").props.onClick(); await h.flush();
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0][0], "prepare"); assert.equal(h.calls[0][2], true);
});

test("review and mailbox setup each retain one primary with expanded draft helpers", async () => {
  const h = harness(null, [savedProspect], { drafts: [savedDraft, { ...savedDraft, doc_id: "draft_second" }], batches: [batch] });
  const review = await h.mount();
  assert.equal(primaryControls(h.expanded(review)).length, 1);
  action(review, "Review sending · 2 approved").props.onClick();
  const send = h.expanded(h.render());
  assert.equal(primaryControls(send).length, 1);
  assert.equal(primaryControls(send)[0].props.children, "Connect Gmail");
});

test("imported contact history is visible and manual continuations cannot enter an email batch", async () => {
  const h = harness(null, [{ ...savedProspect, imported: true, status: "replied", last_contacted: "2026-10-01", continuation_action: "reply" }],
    { drafts: [{ ...savedDraft, channel: "manual", continuation: "reply" }], batches: [batch] });
  const tree = await h.mount(), expanded = h.expanded(tree);
  assert.match(renderedText(expanded), /Continue in your existing conversation/);
  assert.ok(action(expanded, "Copy draft")); assert.equal(action(expanded, "Approve message"), undefined);
  const reviewSending = action(tree, "Review sending · 0 approved"); assert.equal(reviewSending.props.disabled, true);
  action(tree, "Back to prospects").props.onClick();
  assert.match(renderedText(h.render()), /Imported contact.*replied.*2026-10-01/);
});
test("a retained imported-history editor cannot patch after its GTM session retires", async () => {
  const h = harness(null, [{ ...savedProspect, imported: true, status: "new" }]);
  const tree = await h.mount();
  const editor = nodes(tree).find(node => typeof node.type === "function" && node.props.contact?.imported);
  assert.ok(editor); h.retire(); await editor.props.save({ status: "replied", last_contacted: "2026-10-01" });
  assert.equal(h.calls.length, 0);
});
test("a manual continuation's copy permission expires with its exact GTM render", async () => {
  const h = harness(null, [savedProspect], { drafts: [{ ...savedDraft, channel: "manual" }], batches: [batch] });
  const tree = await h.mount(), draft = nodes(tree).find(node => typeof node.type === "function" && node.props.draft?.channel === "manual");
  assert.equal(draft.props.current(), true); h.retire(); assert.equal(draft.props.current(), false);
});
test("the explicit chat import destination shows the importer with unconfirmed audience and performs no discovery", async () => {
  for (const state of ["idle", "queued"]) {
    const h = harness(null, [], { state, icp: { ...fixture(null).icp, confirmed: false } }, { href: "https://console.example/gtm?venture=ven_fixture&import=1" });
    const tree = await h.mount(); assert.ok(nodes(tree).find(node => node.type === "contactImport")); assert.equal(h.calls.length, 0, state);
    assert.equal(action(tree, "Search this audience"), undefined);
  }
});
test("policy acceptance returns an explicit import visit to preview without starting public discovery", async () => {
  const h = harness(null, [], { state: "idle", processing_blocked_reason: "aup_required", icp: { ...fixture(null).icp, confirmed: false } }, { href: "https://console.example/gtm?venture=ven_fixture&import=1" });
  const tree = await h.mount(); assert.equal(nodes(tree).some(node => node.type === "contactImport"), false);
  nodes(tree).find(node => node.type === "input" && node.props.type === "checkbox").props.onChange({ target: { checked: true } });
  action(h.render(), "Save policy acceptance").props.onClick(); await h.flush();
  assert.deepEqual(h.calls.map(call => call[0]), ["policy"]); assert.ok(nodes(h.render()).find(node => node.type === "contactImport"));
});
test("only imported unknown-domain contacts offer a scoped email recheck without sending", async () => {
  for (const [imported, email_check] of [[false, "unknown"], [true, "mx"], [true, "missing_mx"], [true, "unknown"]] as const) {
    const h = harness(null, [{ ...savedProspect, email: "qa@example.test", imported, email_check }]); const tree = await h.mount();
    const check = action(tree, "Check email again"); assert.equal(Boolean(check), imported && email_check === "unknown");
    if (check) { check.props.onClick(); await h.flush(); assert.equal(h.calls.length, 1); assert.deepEqual(h.calls[0], ["verifyEmail", owner, savedProspect.id]); }
  }
  const h = harness(null, [{ ...savedProspect, email: "qa@example.test", imported: true, email_check: "unknown" }]);
  const check = action(await h.mount(), "Check email again"); h.retire(); check.props.onClick(); await h.flush(); assert.equal(h.calls.length, 0);
});

const sentDraft = { ...savedDraft, state: "sent", contact_id: "contact_fixture" };
const sentContact = { id: "contact_fixture", name: "Ada", company: "Ada Bakery" };
const link = (tree: any, href: string) => nodes(tree).find(n => n.type === "link" && n.props.href === href);

test("follow-up drafts are announced in Review and tagged", async () => {
  const h = harness(null, [savedProspect], { drafts: [{ ...savedDraft, day: 3, channel: "email", review_state: "draft" }, { ...savedDraft, doc_id: "other", day: 1, channel: "email", review_state: "draft" }], batches: [batch] });
  const text = renderedText(h.expanded(await h.mount()));
  assert.match(text, /1 follow-up ready to review/);
  assert.match(text, /Follow-up/);
});

test("a blocked send explains itself and offers a settings recovery without usage numbers", async () => {
  const h = harness(null, [savedProspect], { drafts: [savedDraft], batches: [{ ...batch, credit_status: "outbound_rate_unconfigured" }], send_blocked_reason: "outbound_execution_disabled" });
  const review = await h.mount();
  action(review, "Review sending · 1 approved").props.onClick();
  const send = h.expanded(h.render()), text = renderedText(send);
  assert.match(text, /not switched on for your workspace/);
  assert.doesNotMatch(text, /paused|outbound_|Credit check/);
  assert.match(text, /sending connection is not ready/);
  assert.doesNotMatch(text, /credits|price of sending/);
  assert.ok(link(send, "/v/ven_fixture/settings"));
  action(send, "Ask Supymem to turn on sending").props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.calls.filter(c => c[0] === "askSending").map(c => c[1]), [owner]);
  assert.equal(primaryControls(send).filter(n => n.props.children === "Ask Supymem to turn on sending").length, 0);
});

test("no ask-Supymem button when sending is blocked for a reason the founder can fix", async () => {
  const h = harness(null, [savedProspect], { drafts: [savedDraft], batches: [batch], send_blocked_reason: "gtm_gmail_required" });
  action(await h.mount(), "Review sending · 1 approved").props.onClick();
  assert.equal(action(h.expanded(h.render()), "Ask Supymem to turn on sending"), undefined);
});

test("Results before any reply guides the founder with the next-step engine", async () => {
  const h = harness(null, [sentContact], { drafts: [sentDraft], batches: [batch], results: { sent: 1, replied: 0, interested: 0, replies: [] },
    waitlist_signups: 0, learned: { calls: 0, pains_confirmed: [], pains_refuted: [], objections: [], would_pay: 0 }, next_step: { kind: "talk", text: "Talk to 5 more people before choosing what to build next." } });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.match(text, /Log a reply/); assert.match(text, /What you learned/); assert.match(text, /After a call, add your notes/);
  assert.match(text, /0.*waitlist signups/);
  assert.ok(nodes(tree).some(node => node.type === "next-step" && node.props.ventureId === owner.ventureId));
  assert.equal(link(tree, "/v/ven_fixture/experiments"), undefined);
  assert.equal(action(tree, "Prepare for the call"), undefined);
});

test("Results with an interested reply offers call tools, learnings and the engine", async () => {
  const reply = { id: "r1", contact_id: "contact_fixture", from: "ada@bakery.example", classification: "human_positive", body: "Yes, tell me more" };
  const h = harness(null, [sentContact], { drafts: [sentDraft], batches: [batch], results: { sent: 1, replied: 1, interested: 1, replies: [reply] }, booking_link: null,
    waitlist_signups: 3, contact_work: { contact_fixture: { brief: "Who they are\nAda runs a bakery." } },
    learned: { calls: 1, pains_confirmed: ["Orders get lost"], pains_refuted: ["Price"], objections: ["No time"], would_pay: 1 },
    next_step: { kind: "experiment", text: "People said they would pay. Run a small test." } });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.equal(action(tree, "Draft a reply").props.disabled, true);
  assert.ok(link(tree, "/v/ven_fixture/settings")); assert.match(text, /add your booking link in/);
  assert.match(text, /Ada runs a bakery/); assert.ok(action(tree, "Refresh call brief"));
  assert.match(text, /Confirmed.*Orders get lost/); assert.match(text, /Not confirmed.*Price/); assert.match(text, /Objections.*No time/); assert.match(text, /1 of 1 call/);
  assert.match(text, /3.*waitlist signups/);
  assert.ok(nodes(tree).some(node => node.type === "next-step"));
  const withLink = h.expanded(await (h.update({ booking_link: "https://cal.com/ada" }), h.poll()));
  assert.equal(action(withLink, "Draft a reply").props.disabled, false);
  action(withLink, "Prepare for the call"); // present
});

const geo = [{ code: "US", note_required: false, default_basis: "legitimate_interest" }, { code: "DE", note_required: true, default_basis: "consent" }];
const waiting = (id: string, company: string, country?: string) => ({ id, name: company, company, blocked_reason: "unknown_jurisdiction", country, country_source: country ? "postal_address" : undefined });
const stuck = (prospects: any[], extra: Record<string, any> = {}) => harness(null, prospects, { message: "We found 2 companies but cannot draft yet. Confirm each country so we can check what outreach is allowed.", geo_options: geo, ...extra });

test("prospects waiting on a country show the server message and one primary confirm button", async () => {
  const h = stuck([waiting("c1", "Acme"), waiting("c2", "Beta", "DE")]), tree = await h.mount();
  assert.match(card(tree).props.body, /Confirm each country/);
  const confirm = action(tree, "Confirm country for 2 prospects");
  assert.ok(confirm && !confirm.props.className.includes("secondary"));
  assert.ok(action(tree, "Prepare drafts again").props.className.includes("secondary"));
  confirm.props.onClick();
  const form = nodes(h.expanded(h.render())).find(n => n.props?.["aria-label"] === "Confirm prospect countries");
  assert.ok(form);
  assert.equal(action(h.render(), "Confirm country for 2 prospects"), undefined);
});

test("bulk confirm prefills enrichment countries and asks for a note only where the server does", async () => {
  const h = stuck([waiting("c1", "Acme", "US"), waiting("c2", "Beta", "DE")]), tree = await h.mount();
  action(tree, "Confirm country for 2 prospects").props.onClick();
  const form = h.expanded(h.render());
  const selects = nodes(form).filter(n => n.type === "select" && /^Country for/.test(n.props["aria-label"] || ""));
  assert.deepEqual(selects.map(n => n.props.value), ["US", "DE"]);
  const notes = nodes(form).filter(n => n.type === "textarea");
  assert.equal(notes.length, 1);
  assert.equal(action(form, "Save and check eligibility").props.disabled, true);
});

test("bulk confirm submits country, default basis and note in one request", async () => {
  const h = stuck([waiting("c1", "Acme", "US")]), tree = await h.mount();
  action(tree, "Confirm country for 1 prospect").props.onClick();
  const save = action(h.expanded(h.render()), "Save and check eligibility");
  assert.equal(save.props.disabled, false);
  save.props.onClick(); await h.flush();
  assert.equal(h.calls[0][0], "confirmProspects");
  assert.equal(JSON.stringify(h.calls[0][2]), JSON.stringify([{ contact_id: "c1", country: "US", lawful_basis: "legitimate_interest", lawful_basis_note: "" }]));
});

test("a single prospect can be skipped from its card", async () => {
  const h = stuck([waiting("c1", "Acme")]), tree = await h.mount();
  const skip = nodes(tree).find(n => n.type === "button" && [n.props.children].flat().join("") === "Skip");
  skip.props.onClick(); await h.flush();
  assert.deepEqual(h.calls[0].slice(0, 3), ["eligibility", owner, "c1"]);
  assert.equal(JSON.stringify(h.calls[0][3]), JSON.stringify({ skip: true }));
});

test("skipped prospects leave the list and can be restored", async () => {
  const h = stuck([waiting("c1", "Acme"), { ...waiting("c2", "Beta"), skipped: true, blocked_reason: "skipped" }]), tree = await h.mount();
  assert.ok(action(tree, "Confirm country for 1 prospect"));
  const restore = action(tree, "Restore 1 skipped");
  restore.props.onClick(); await h.flush();
  assert.equal(JSON.stringify(h.calls[0][2]), JSON.stringify([{ contact_id: "c2", skip: false }]));
});

test("the search button is truthful about whether discovery will run", async () => {
  const prepared = await harness(null, [{ id: "c1", name: "Acme", blocked_reason: "us_postal_address_required" }], { will_search: false }).mount();
  assert.ok(action(prepared, "Prepare drafts again"));
  assert.equal(action(prepared, "Search again"), undefined);
  const changed = await harness(null, [{ id: "c1", name: "Acme", blocked_reason: "us_postal_address_required" }], { will_search: true }).mount();
  assert.ok(action(changed, "Search again"));
});

test("consumer channels and anonymous needs have sources and copy-only drafts", async () => {
  const h = harness(null, [{ id: "bad", name: "Trade Coffee", email: "sales@trade.example" }], {
    icp: { who: "home espresso enthusiasts", confirmed: true, mode: "consumer" }, recruitment_opt_in: true,
    channels: [{ name: "Espresso Club Forum", url: "https://forum.example/espresso", contact_url: "https://forum.example/contact", quote: "Home espresso enthusiasts discuss brewing", why_fits: "Home espresso discussions", participation: "Ask moderators; no sales", draft: "Could we talk about your experience? Not a sales pitch." }],
    need_signals: [{ quote: "I need help choosing coffee for espresso", source_url: "https://forum.example/need", published_at: "2026-10-01T00:00:00Z", confidence: "observed" }],
  });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.ok(text.includes("Where your customers gather") && text.includes("People describing this problem"));
  assert.ok(text.includes("2026-10-01") && text.includes("Posting rules:"));
  assert.ok(!text.includes("sales@trade.example") && !text.includes("Add a contact") && !text.includes("Confirm country"));
  assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === "https://forum.example/need"));
  assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === "https://forum.example/contact"));
  await action(tree, "Copy draft").props.onClick();
  assert.deepEqual(h.copied, ["Could we talk about your experience? Not a sales pitch."]);
  assert.deepEqual(h.calls, []);
});

test("consumer discovery requires an explicit recruitment opt-in and always offers a next action", async () => {
  const h = harness("public_search_unavailable", [], { icp: { who: "Bangalore parents", confirmed: true, mode: "consumer" }, source_access: "unavailable" });
  const tree = await h.mount();
  assert.ok(renderedText(tree).includes("Ask a current customer") && renderedText(tree).includes("Sources could not be inspected"));
  assert.ok(action(tree, "Refine audience or location"));
  const find = action(tree, "Find where my customers gather");
  assert.equal(find.props["data-usage-control"], "gtm_journey_find_gatherings");
  find.props.onClick(); await h.flush();
  assert.deepEqual(h.calls[0], ["prepare", owner, true, undefined, true]);
});

test("queries stay hidden before discovery and quotes display as plain text", async () => {
  const fresh = await harness(null, [], { state: "idle", discovery_started: false,
    discovery_summary: "Searching public sources.", icp: { ...fixture(null).icp, search_queries: ["keyword fallback"] } }).mount();
  assert.ok(!renderedText(fresh).includes("Searching public sources"));
  const h = harness(null, [], { discovery_started: true, discovery_summary: "Searching public communities and discussions.",
    icp: { ...fixture(null).icp, mode: "consumer", search_queries: ["keyword fallback"] }, recruitment_opt_in: true,
    channels: [{ name: "Forum", url: "https://forum.example", source_url: "https://forum.example/thread",
      quote: "## Our **community** [rules](https://forum.example/rules)", why_fits: "Audience fit", participation: "Read rules", draft: "A draft" }],
    need_signals: [{ quote: "**I** need [help](https://forum.example/help)", source_url: "https://forum.example/thread" }],
  });
  const tree = await h.mount(), text = renderedText(tree);
  assert.match(text, /Searching public communities and discussions/);
  assert.ok(!text.includes("keyword fallback"));
  assert.ok(text.includes("Audience fit") && text.includes("I need help") && !text.includes("Our community rules"));
  assert.ok(!text.includes("##") && !text.includes("**") && !text.includes("](http"));
  assert.ok(text.includes("Posting rules:") && !text.includes("View quoted evidence"));
});

test("a fresh consumer journey opts in through the single Find customers start", async () => {
  const h = harness(null, [], { state: "idle", discovery_started: false, icp: { who: "Bangalore parents", confirmed: true, mode: "consumer" } });
  const tree = await h.mount();
  assert.equal(h.calls.length, 0);
  assert.ok(renderedText(tree).includes("Nothing is posted or sent"));
  action(tree, "Find customers").props.onClick(); await h.flush();
  assert.deepEqual(h.calls, [["prepare", owner, false, "", true]]);
  assert.equal(card(h.render()).props.status, "working");
});

test("businesses can use anonymous older needs and evidenced peer communities", async () => {
  const h = harness(null, [], { icp: { who: "dental clinic owners", confirmed: true, mode: "company" },
    channels: [{ name: "r/Dentistry", url: "https://www.reddit.com/r/Dentistry/", source_url: "https://www.reddit.com/r/Dentistry/comments/example/", quote: "Dental front desk discussion", why_fits: "Dental staff", participation: "Check rules", draft: "May I ask about recall?" }],
    need_signals: [{ quote: "We need recall help", source_url: "https://forum.example/need", published_at: "2026-04-01T00:00:00Z", confidence: "observed", recency: "older" }] });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.ok(text.includes("People describing this problem") && text.includes("Older") && text.includes("rules before replying publicly"));
  assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === "https://www.reddit.com/r/Dentistry/"));
  assert.deepEqual(h.calls, []);
});

test("U18 community cards show audience reason and rules link, never scraped rules twice", async () => {
  const raw = "Rules 1 Don't Be a Jerk Remember the human on the. Treat everyone";
  const h = harness(null, [], { icp: { who: "First-time parents in the US", confirmed: true, mode: "consumer" },
    channels: [{ name: "r/NewParents", url: "https://www.reddit.com/r/NewParents/", quote: raw, participation: raw,
      rules_url: "https://www.reddit.com/r/NewParents/about/rules/", why_fits: "First-time parents share their experiences here.", draft: "Open to a short research call?" }] });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.ok(text.includes("First-time parents share their experiences here.") && text.includes("Posting rules:"));
  assert.ok(!text.includes(raw));
  assert.equal(nodes(tree).filter(node => node.type === "a" && node.props.href === "https://www.reddit.com/r/NewParents/about/rules/").length, 1);
});

test("U20 public Reddit author/date has a scoped, manual reply draft action", async () => {
  const h = harness(null, [], { icp: { who: "First-time parents in the US", confirmed: true, mode: "consumer" },
    need_signals: [{ quote: "I need help with baby sleep", source_url: "https://www.reddit.com/r/NewParents/comments/abc123/",
      author: "nap_parent", published_at: "2026-10-01T12:00:00Z", recency: "recent", confidence: "observed", draft: "Hi, open to a 15-minute research call?" }] });
  const tree = h.expanded(await h.mount());
  assert.match(renderedText(tree), /u\/nap_parent ·\s*2026-10-01/);
  await action(tree, "Draft a reply").props.onClick();
  assert.deepEqual(h.copied, ["Hi, open to a 15-minute research call?"]);
  assert.deepEqual(h.calls, []);
  h.retire();
  await action(tree, "Draft a reply").props.onClick();
  assert.equal(h.copied.length, 1);
});

test("U20 older and undated needs remain evidence, not reply targets", async () => {
  for (const recency of ["older", "unknown"]) {
    const h = harness(null, [], { icp: { who: "First-time parents in the US", confirmed: true, mode: "consumer" },
      need_signals: [{ quote: "I need help with baby sleep", source_url: "https://www.reddit.com/r/NewParents/comments/abc123/",
        recency, published_at: recency === "older" ? "2026-01-01T12:00:00Z" : null, confidence: "observed", draft: "Stale saved draft" }] });
    let tree = h.expanded(await h.mount());
    assert.equal(action(tree, "Draft a reply").props.disabled, true);
    assert.ok(!renderedText(tree).includes("Stale saved draft"));
    action(tree, "Review communities").props.onClick(); await h.flush();
    tree = h.expanded(h.render());
    assert.equal(action(tree, "Draft a reply").props.disabled, true);
    await action(tree, "Draft a reply").props.onClick();
    assert.deepEqual(h.copied, []);
  }
});

test("U21 consumer communities with zero businesses advance to manual Review", async () => {
  const h = harness(null, [], { icp: { who: "First-time parents in the US", confirmed: true, mode: "consumer" }, recruitment_opt_in: true,
    people_search_notice: "No public profiles found for LinkedIn and X.",
    channels: [{ name: "r/NewParents", url: "https://www.reddit.com/r/NewParents/", why_fits: "Parents share experience", draft: "Open to a first call?" }] });
  const tree = await h.mount();
  assert.ok(renderedText(h.expanded(tree)).includes("No public profiles found for LinkedIn and X."));
  assert.ok(action(tree, "Retry community search").props.className.includes("secondary"));
  action(tree, "Review communities").props.onClick(); await h.flush();
  const review = h.expanded(h.render()), text = renderedText(review);
  assert.equal(card(h.render()).props.step, "3 of 5 · Review");
  assert.ok(text.includes("Book your first conversation") && text.includes("Add your booking link in Settings"));
  assert.ok(!text.includes("No eligible drafts yet") && !text.includes("Review sending") && !text.includes("Approve batch and send"));
  assert.deepEqual(h.calls, []);
});

test("U21 expert-only consumer results have published routes and a Review action", async () => {
  const expert = { id: "sleep-expert", name: "Jane Doe", company: "", role: "Sleep consultant", location: "US", fit: 90,
    expert_role: "pediatric sleep consultant", lead_type: "insight", why: "Serves first-time parents", quote: "Jane helps parents with sleep",
    source_url: "https://sleep-expert.example/", routes: [{ kind: "booking", url: "https://calendly.com/sleep-expert/intro", source_url: "https://sleep-expert.example/", quote: "Book a call" }],
    draft: "Open to a research conversation?", connection_note: "Open to a call?" };
  const h = harness(null, [], { icp: { who: "First-time parents in the US", confirmed: true, mode: "consumer" }, reachable_leads: [], industry_experts: [expert] });
  const tree = await h.mount();
  assert.ok(renderedText(h.expanded(tree)).includes("Industry experts"));
  action(tree, "Review reachable leads").props.onClick(); await h.flush();
  assert.ok(renderedText(h.expanded(h.render())).includes("Book your first conversation"));
  assert.ok(renderedText(h.expanded(h.render())).includes("Jane Doe"));
  assert.deepEqual(h.calls, []);
});

test("unknown need dates and physical partner channels stay copy-only", async () => {
  const h = harness(null, [], { icp: { who: "Bangalore parents", confirmed: true, mode: "consumer" },
    channels: [{ name: "Swimming Academy", url: "https://academy.example/", quote: "Kids swimming classes in Bangalore", why_fits: "Parents gather here", participation: "Ask permission", draft: "May we ask parents for voluntary conversations?", channel_role: "partner/channel to approach" }],
    need_signals: [{ quote: "We need rebooking help", source_url: "https://forum.example/need", published_at: null, recency: "unknown", date_status: "unknown", confidence: "observed" }] });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.ok(text.includes("Date unknown") && text.includes("Partner/channel to approach"));
  assert.ok(!text.includes("Recent") && !text.includes("Send email"));
  assert.deepEqual(h.calls, []);
});

test("existing customer growth is the first card with copy-only referral and testimonial drafts", async () => {
  const h = harness(null, [], { customer_start: { referral_draft: "Please ask before sharing a referral", review_draft: "May I quote your honest review?", lookalikes_note: "Find similar needs, not scraped profiles" } });
  const tree = h.expanded(await h.mount());
  assert.equal(card(tree).props.title, "Start from your existing customers");
  assert.ok(renderedText(tree).includes("Find lookalikes"));
  const copies = nodes(tree).filter(node => node.type === "button" && node.props.children === "Copy draft");
  await copies[0].props.onClick(); await copies[1].props.onClick();
  assert.deepEqual(h.copied, ["Please ask before sharing a referral", "May I quote your honest review?"]);
  assert.deepEqual(h.calls, []);
});

test("retired customer drafts cannot copy into a different session", async () => {
  const h = harness(null, [], { customer_start: { referral_draft: "Private founder draft", review_draft: "Review draft", lookalikes_note: "Similar needs" } });
  const tree = h.expanded(await h.mount()), copy = action(tree, "Copy draft");
  h.retire();
  await copy.props.onClick();
  assert.deepEqual(h.copied, []);
});

test("reachable lead evidence and copy stay manual and scoped to the active founder", async () => {
  const lead = { id: "reachable", name: "Jane Doe", company: "Dental Practice", role: "Owner", location: "US", fit: 95,
    why: "Independent practice matches the customer and workflow.", quote: "Independent dental practice", source_url: "https://practice.example/about",
    routes: [{ kind: "email", email: "jane@practice.example", email_kind: "named_person", source_url: "https://practice.example/contact", quote: "jane@practice.example" }],
    draft: "Open to a 15-minute call? Reply with a time.", connection_note: "Open to a conversation?",
    outreach_hint: { country: "GB", subscriber: "unverified_individual", note: "Likely sole trader: consent needed; provide a privacy notice." } };
  const h = harness(null, [], { reachable_leads: [lead], message: "Review reachable leads" });
  const tree = h.expanded(await h.mount());
  assert.match(renderedText(tree), /Reachable leads/);
  assert.match(renderedText(tree), /Named person/);
  assert.match(renderedText(tree), /Only contact people about their own experience/);
  assert.match(renderedText(tree), /Likely sole trader: consent needed/);
  assert.match(renderedText(tree), /jane@practice.example/);
  assert.ok(action(tree, "Copy email + open mail"));
  await action(tree, "Copy draft").props.onClick();
  assert.deepEqual(h.copied, [lead.draft]);
  assert.equal(h.calls.length, 0);
  h.retire();
  await action(tree, "Copy draft").props.onClick();
  assert.deepEqual(h.copied, [lead.draft]);
});

test("guessed lead displays honest confidence and blocks mail opening until reviewed", async () => {
  const lead = { id: "guessed", name: "Jane Doe", company: "Practice Ltd", role: "Owner", location: "US", fit: 90,
    why: "Workflow fit", quote: "Jane Doe, Owner", source_url: "https://practice.example/team",
    routes: [{ kind: "email", email: "jane.doe@practice.example", route_type: "guess", confidence: 40,
      confidence_label: "Guessed business address", source_url: "https://practice.example/team", quote: "Jane Doe, Owner" }],
    draft: "Open to a short call?", connection_note: "Open to a call?" };
  const h = harness(null, [], { reachable_leads: [lead] });
  const tree = h.expanded(await h.mount());
  assert.match(renderedText(tree), /Guessed business address.*40% confidence/);
  assert.match(renderedText(tree), /We guessed this address \(40% confidence\). It may bounce./);
  const mail = action(tree, "Copy email + open mail");
  assert.equal(mail.props.disabled, true);
  mail.props.onClick();
  assert.deepEqual(h.opened, []);
  assert.deepEqual(h.calls, []);
});

test("reachable email evidence renders text-only snippets and the actual value sentence", async () => {
  const lead = { id: "clinic", name: "Clinic Ltd", company: "Clinic Ltd", role: "", location: "UK", fit: 90,
    why: "Business fit", value_exchange: "Independent UK practice handling bookings.", quote: "Independent veterinary practice", source_url: "https://clinic.example/",
    routes: [{ kind: "email", email: "info@clinic.example", source_url: "https://clinic.example/contact", quote: '<a href="mailto:info@clinic.example" class="wixui-rich-text__text">info@clinic.example</a>',
      display_quote: "Contact & enquiries info@clinic.example today", published_as: 'mailto:info@clinic.example" class="wixui-rich-text__text' }],
    draft: "Could we discuss clinic bookings?", connection_note: "Open to a conversation?" };
  const h = harness(null, [], { reachable_leads: [lead] });
  const text = renderedText(h.expanded(await h.mount()));
  assert.match(text, /Potential value:\s+Independent UK practice handling bookings/);
  assert.match(text, /Contact & enquiries info@clinic.example today/);
  assert.ok(!text.includes("<a") && !text.includes("wixui-rich-text"));
});

test("Find customers progress adopts the same higher receipt as the banner", async () => {
  const progress = { run_id: "physio-ui", state: "running", percent: 94, step_label: "Preparing outreach drafts", started_at: "2026-10-15T10:00:00Z" };
  const h = harness(null, [], { state: "running", discovery_started: true, research_progress: progress });
  await h.mount();
  gtmProgress.shareGtmProgress(gtmProgress.gtmProgressOwner(owner.actorId, 1, owner.ventureId), { ...progress, percent: 99 });
  assert.equal(nodes(h.render()).find(node => node.type === "research-progress").props.progress.percent, 99);
  h.update({ research_progress: { ...progress, percent: 95 } });
  const tree = await h.poll();
  assert.equal(nodes(tree).find(node => node.type === "research-progress").props.progress.percent, 99);
  h.retire();
});

test("PhysioRemind evidence uses cleaned saved excerpts and honest draft fallback status", async () => {
  const raw = '.5 62.9-34.5 67.2 0 79.7 44.3 79.7 101.9V416z"> <a href="mailto:CareStudio@Consumer.Example" aria-label="Email 1">CareStudio@Consumer.Example</a> All Rights Reserved. Terms & Conditions · Privacy Policy · Refund Policy · Partner With Us';
  const lead = { id: "quantum", name: "Sample Physical Therapy", company: "Sample Physical Therapy", role: "", location: "Bangalore", fit: 90,
    why: "Physiotherapy clinic handling appointments", quote: raw, display_quote: "Physiotherapy clinic in Bangalore", source_url: "https://sample-physical-therapy.test/contact",
    routes: [{ kind: "email", email: "carestudio@consumer.example", source_url: "https://sample-physical-therapy.test/contact", quote: raw, display_quote: "Contact Sample Physical Therapy: CareStudio@Consumer.Example" }],
    draft: "Open to a 15-minute call? Reply with a time.", connection_note: "Open to a short call?", draft_source: "template", draft_status: "outcome_uncertain" };
  const h = harness(null, [], { reachable_leads: [lead] });
  const text = renderedText(h.expanded(await h.mount()));
  assert.match(text, /Contact Sample Physical Therapy: CareStudio@Consumer.Example/);
  assert.match(text, /personalisation could not be confirmed; no automatic retry/);
  assert.doesNotMatch(text, /V416z|aria-label|mailto:|All Rights Reserved|Privacy Policy|Partner With Us/);
  assert.ok(action(h.expanded(h.render()), "Copy draft"));
  h.retire();
});

test("fit-only templates explain the honest question without hiding personalisation failures", async () => {
  const lead = { id: "clinic", name: "Willow Physiotherapy", company: "Willow Physiotherapy", role: "", location: "Bristol", fit: 90,
    why: "Fits your audience; no public sign of this problem yet", quote: "Independent physiotherapy clinic in Bristol", evidence_label: "fit_only",
    source_url: "https://willowphysio.example/", routes: [], draft: "Does the problem I'm researching affect your work?", connection_note: "Open to a call?", draft_source: "template" };
  for (const [status, label] of [
    [{}, "asks whether the problem applies to them"],
    [{ evidence_label: "need_passage" }, "personalisation could not be prepared"],
    [{ draft_status: "draft_failed" }, "personalisation could not be prepared"],
    [{ draft_status: "model_failed" }, "personalisation could not be prepared"],
    [{ draft_status: "provider_unavailable" }, "personalisation could not be prepared"],
    [{ draft_status: "credits_required" }, "personalisation needs credits"],
    [{ draft_status: "outcome_uncertain" }, "personalisation could not be confirmed; no automatic retry"],
    [{ draft_status: "output_rejected" }, "the personalised draft did not pass review"],
    [{ judging_status: "pending" }, "personalisation waits for relevance review"],
  ] as const) {
    const h = harness(null, [], { reachable_leads: [{ ...lead, ...status }] });
    const tree = h.expanded(await h.mount());
    const note = nodes(tree).find(node => node.type === "small" && renderedText(node).includes("Template draft"));
    assert.equal(note.props.children.join(""), `Template draft · ${label}. Review or edit before using.`);
    h.retire();
  }
});

test("Open X copies only the short DM and opens the profile without sending", async () => {
  const lead = { id: "x-person", name: "Sam Rao", handle: "@frontdesk", company: "", role: "", location: "US", fit: 90,
    why: "Runs dental reception and asks for help reducing patient no-shows.", quote: "I run dental reception and need help reducing patient no-shows.",
    source_url: "https://x.com/frontdesk/status/123", routes: [{ kind: "x", url: "https://x.com/frontdesk", source_url: "https://x.com/frontdesk/status/123", quote: "I run dental reception" }],
    draft: "Long founder draft", connection_note: "Connection note", x_note: "Hi, I'm researching no-shows. Open to a 15-minute call? Reply with a time." };
  const h = harness(null, [], { reachable_leads: [lead] });
  const tree = h.expanded(await h.mount());
  assert.match(renderedText(tree), /@frontdesk/);
  assert.match(renderedText(tree), /Runs dental reception/);
  await action(tree, "Open X").props.onClick();
  assert.deepEqual(h.copied, [lead.x_note]);
  assert.deepEqual(h.opened, ["https://x.com/frontdesk"]);
  assert.equal(h.calls.length, 0);
  h.retire();
  await action(tree, "Open X").props.onClick();
  assert.equal(h.opened.length, 1);
  assert.equal(h.copied.length, 1);
});

test("insight and unchecked contacts retain evidence and one-click call drafts", async () => {
  const lead = { id: "insight", name: "Meera Rao", company: "", role: "Structural engineer", location: "India", fit: 85,
    lead_type: "insight", judging_status: "pending", why: "Published drawing review experience", quote: "Meera reviews structural drawings",
    source_url: "https://meera.example/", routes: [{ kind: "contact_form", url: "https://meera.example/contact", source_url: "https://meera.example/contact", quote: "Contact Meera" }],
    draft: "Hi Meera, open to a 15-minute call about drawing reviews?", connection_note: "Open to a 15-minute call?" };
  const h = harness(null, [], { reachable_leads: [lead] });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.match(text, /Industry insight contact/);
  assert.match(text, /Relevance not yet checked/);
  assert.match(text, /Published contact page/);
  await action(tree, "Open contact form").props.onClick();
  assert.deepEqual(h.opened, ["https://meera.example/contact"]);
  assert.deepEqual(h.copied, [lead.draft]);
  assert.deepEqual(h.calls, []);
});

test("published booking routes open for founder review and hiring stays inferred", async () => {
  const lead = { id: "booking", name: "Drawing Consultancy", company: "Drawing Consultancy", role: "", location: "India", fit: 90,
    why: "Published drawing work", quote: "Drawing Consultancy", source_url: "https://drawing.example/",
    signals: [{ confidence: "inferred", recency: "unknown", quote: "Structural Engineer", source_url: "https://drawing.example/careers" }],
    routes: [{ kind: "booking", url: "https://cal.com/drawing/intro", source_url: "https://drawing.example/", quote: "Book a call" }],
    draft: "Open to a 15-minute call?", connection_note: "Open to a call?" };
  const h = harness(null, [], { reachable_leads: [lead] });
  const tree = h.expanded(await h.mount());
  assert.match(renderedText(tree), /Published booking page/);
  assert.match(renderedText(tree), /Hiring observation \(inferred; date not confirmed\)/);
  await action(tree, "Open booking page").props.onClick();
  assert.deepEqual(h.opened, ["https://cal.com/drawing/intro"]);
  assert.deepEqual(h.copied, [lead.draft]);
  assert.deepEqual(h.calls, []);
  assert.equal(reachAction(lead as any, { ...lead.routes[0], url: "https://cal.com.evil.example/drawing" } as any), null);
});

test("bounce pause shows counts before explicit resume and never sends", async () => {
  const h = harness(null, [savedProspect], { phase: "drafts", can_send: false, allow_sending: true, send_blocked_reason: "send_paused", batches: [batch],
    sending_alert: "Sending paused after bounced contacts.", sending_pause: { sent: 10, bounced: 1, send_resumed_at: null },
    drafts: [savedDraft] });
  let tree = await h.mount();
  assert.match(renderedText(tree), /Sending paused after bounced contacts/);
  const alert = nodes(tree).find(node => node.props?.role === "alert");
  assert.deepEqual(Array.from(alert.props.children).filter((value: unknown) => typeof value === "number"), [1, 10]);
  assert.match(renderedText(tree), /Check the bounced addresses first/);
  assert.ok(nodes(tree).some(node => node.props?.role === "alert"));
  assert.equal(action(tree, "Resume sending").props.disabled, true);
  const checkbox = nodes(tree).find(node => node.type === "input" && node.props?.type === "checkbox");
  checkbox.props.onChange({ target: { checked: true } }); tree = h.render();
  action(tree, "Resume sending").props.onClick();
  await h.flush();
  assert.deepEqual(h.calls, [["resumeSending", owner]]);
  assert.equal(action(h.render(), "Resume sending"), undefined);
});

test("resume failure leaves a retry action and stale owner cannot resume", async () => {
  const h = harness(null, [], { sending_pause: { sent: 10, bounced: 1, send_resumed_at: null } },
    { resume: async () => { throw new Error("Could not resume. Try again."); } });
  let tree = await h.mount();
  nodes(tree).find(node => node.type === "input" && node.props?.type === "checkbox").props.onChange({ target: { checked: true } });
  tree = h.render(); const stale = action(tree, "Resume sending");
  await stale.props.onClick(); await h.flush(); tree = h.render();
  assert.match(renderedText(tree), /Refresh and retry/);
  assert.equal(action(tree, "Resume sending").props.disabled, false);
  const calls = h.calls.length; h.retire(); await stale.props.onClick(); await h.flush();
  assert.equal(h.calls.length, calls);
});

test("reply taxonomy shows referral, date and sensitive-reply actions without auto send", async () => {
  const contacts = [{ id: "jane", name: "Jane", email: "jane@drawing.example", company: "Drawing", eligible: true }];
  const h = harness(null, contacts, { phase: "results", drafts: [{ doc_id: "sent", contact_id: "jane", state: "sent", body: "Hello", to: contacts[0].email }],
    results: { sent: 1, replied: 1, interested: 0, negative: 0, replies: [{ contact_id: "jane", classification: "human_neutral", outcome: "wrong_person" }] },
    contact_work: { jane: { referral_ask: "Who would be the right person?", follow_up: "Follow up no earlier than 2027-03-15.", reply_attention: "Contact stopped. Review in Gmail." } } });
  const tree = h.expanded(await h.mount()), text = renderedText(tree);
  assert.match(text, /Who would be the right person/);
  assert.match(text, /2027-03-15/);
  assert.match(text, /Contact stopped/);
  assert.match(text, /Wrong person/);
  assert.match(text, /Has a question/);
  assert.match(text, /Angry or legal objection/);
  assert.ok(action(tree, "Prepare referral ask for review"));
  assert.deepEqual(h.calls, []);
});

const dogExpert = { id: "tara", name: "Tara's School for Dogs", person: "Tara's School for Dogs", company: "Tara's School for Dogs", role: "", location: "US", fit: 80,
  lead_type: "insight", why: "Published puppy training", value_exchange: "A call could help you learn about puppy training.", quote: "Puppy training", source_url: "https://taras.example/",
  routes: [{ kind: "contact_form", url: "https://taras.example/contact", source_url: "https://taras.example/contact", quote: "Contact" }],
  draft: "Hi Tara's School for Dogs team,\n\nOpen to a call? Reply with a time.", connection_note: "Open to a call?" };
const puppyChannel = { id: "puppy-community", name: "Puppy community", url: "https://www.reddit.com/r/puppy101/", why_fits: "Puppy owners gather here.", draft: "Open to a call?", channel_role: "community" };

test("U39 business introductions never address an organiser", async () => {
  const h = harness(null, [], { channels: [{ ...puppyChannel, channel_role: "partner/channel to approach", name: "Dog school" }] });
  const tree = h.expanded(await h.mount());
  assert.match(renderedText(tree), /Introduction draft/);
  assert.doesNotMatch(renderedText(tree), /organiser/);
});

test("U40 missing needs explain the next step in founder language", async () => {
  const h = harness(null, [], { channels: [puppyChannel] });
  const text = renderedText(h.expanded(await h.mount()));
  assert.match(text, /We didn't find people publicly describing this problem yet\. Ask in the communities above, or retry later\./);
  assert.doesNotMatch(text, /attributable first-person/);
});

test("unavailable community searches do not claim nobody described the problem", async () => {
  for (const status of [{ error_code: "public_search_unavailable" }, { source_access: "unavailable" }]) {
    const h = harness(null, [], { icp: { who: "First-time parents in the US", confirmed: true, mode: "consumer" }, recruitment_opt_in: true, ...status });
    const tree = h.expanded(await h.mount()), text = renderedText(tree);
    assert.match(text, /Search is temporarily unavailable, so we couldn't look for people this time\. Nothing was lost\. Try again in a few minutes\./);
    assert.doesNotMatch(text, /We didn't find people publicly describing this problem yet/);
    await action(tree, "Retry community search").props.onClick();
    assert.deepEqual(h.calls.find(call => call[0] === "prepare"), ["prepare", owner, true, undefined, true]);
  }
});

test("U41 expert identity appears once with a separated role", async () => {
  const h = harness(null, [], { icp: { who: "Dog owners", confirmed: true, mode: "consumer" }, industry_experts: [dogExpert] });
  const tree = h.expanded(await h.mount());
  const heading = nodes(tree).find(node => node.type === "strong" && node.props.children === dogExpert.name);
  assert.ok(heading);
  const lead = nodes(tree).find(node => node.type === "li" && nodes(node).includes(heading));
  assert.equal(nodes(lead).filter(node => node.type === "span" && renderedText(node).includes(dogExpert.company)).length, 0);
  assert.ok(nodes(lead).some(node => node.type === "small" && node.props.children === " · Industry insight contact · ask about their experience"));
});

test("U44 contact guidance appears once per list and preserves admin hints", async () => {
  const hint = { country: "US", subscriber: "unverified", note: "Commercial email needs accurate headers, identification, a postal address and opt-out; honour suppression. Guessed business addresses may bounce." };
  const leads = [dogExpert, { ...dogExpert, id: "tonya", name: "Tonya Wilhelm", person: "Tonya Wilhelm" }].map(lead => ({ ...lead, outreach_hint: hint }));
  const h = harness(null, [], { industry_experts: leads, icp: { who: "Dog owners", confirmed: true, mode: "consumer" } });
  const text = renderedText(h.expanded(await h.mount()));
  assert.equal(text.split("Only contact people about their own experience; include your address and an opt-out line in emails.").length - 1, 1);
  assert.doesNotMatch(text, /accurate headers|honour suppression/);
  assert.equal(leads[0].outreach_hint.note, hint.note);
});

test("U46 manual Send and Results persist sent and booked actions without sending", async () => {
  const h = harness(null, [], { industry_experts: [dogExpert], icp: { who: "Dog owners", confirmed: true, mode: "consumer" }, channels: [puppyChannel] });
  let tree = await h.mount();
  action(tree, "Review reachable leads").props.onClick(); await h.flush();
  action(h.render(), "Continue to Send").props.onClick(); await h.flush();
  assert.deepEqual(JSON.parse(JSON.stringify(h.confirmations.filter(item => item?.event === "journey.step_completed"))),
    [{ event: "journey.step_completed", step: "review_leads", ventureId: owner.ventureId, workspaceId: owner.workspaceId }]);
  assert.equal(card(h.render()).props.step, "4 of 5 · Send");
  tree = h.expanded(h.render());
  const sent = { id: dogExpert.id, name: dogExpert.name, source_url: dogExpert.source_url, sent_at: "2026-10-16T10:00:00Z", booked_at: null };
  h.update({ manual_outreach: [sent] });
  action(tree, "Mark as sent").props.onClick(); await h.flush();
  assert.equal(card(h.render()).props.step, "5 of 5 · Results");
  tree = h.expanded(h.render());
  assert.match(renderedText(tree), /0 calls booked/);
  assert.equal(action(tree, "Marked as sent ✓").props.disabled, true);
  h.update({ manual_outreach: [{ ...sent, booked_at: "2026-10-16T11:00:00Z" }] });
  action(tree, "Call booked").props.onClick(); await h.flush();
  assert.match(renderedText(h.expanded(h.render())), /1 call booked/);
  assert.equal(action(h.expanded(h.render()), "Call booked ✓").props.disabled, true);
  assert.deepEqual(h.calls, [["contact", owner, dogExpert.id, "sent"], ["contact", owner, dogExpert.id, "call-booked"]]);
  assert.deepEqual(h.opened, []);
  assert.deepEqual(h.copied, []);
  const restored = harness(null, [], { manual_outreach: [{ ...sent, booked_at: "2026-10-16T11:00:00Z" }], icp: { who: "Dog owners", confirmed: true, mode: "consumer" } });
  assert.equal(card(await restored.mount()).props.step, "5 of 5 · Results");
  const community = harness(null, [], { channels: [puppyChannel], icp: { who: "Dog owners", confirmed: true, mode: "consumer" } });
  action(await community.mount(), "Review communities").props.onClick(); await community.flush();
  action(community.expanded(community.render()), "Mark as sent").props.onClick(); await community.flush();
  assert.deepEqual(community.calls, [["contact", owner, puppyChannel.url, "sent"]]);
});

test("U63 zero-person B2B discovery has one stage-two card and useful next steps", async () => {
  const h = harness(null, [], { channels: [puppyChannel], icp: { who: "Solo technical founders", confirmed: true, mode: "company" } });
  const tree = await h.mount(), text = renderedText(h.expanded(tree));
  assert.equal(nodes(tree).filter(node => node.type === "card" && node.props.step?.startsWith("2 of")).length, 1);
  assert.equal(nodes(tree).filter(node => node.type === "card" && node.props.title === "Where your customers gather").length, 0);
  assert.match(text, /We found no people yet/);
  assert.match(text, /add a contact you know with evidence/);
  assert.equal(action(tree, "Review reachable leads"), undefined);
  assert.ok(action(tree, "Review communities"));
  assert.ok(nodes(h.expanded(tree)).some(node => node.type === "summary" && renderedText(node).includes("Add a contact")));
});

test("discovery notices render only on the Prospects stage", async () => {
  const notices = { discovery_notice: "No reachable matches were verified.", people_search_notice: "No public profiles found." };
  for (const { overrides, visible } of [
    { overrides: {}, visible: true },
    { overrides: { icp: { ...fixture(null).icp, confirmed: false } }, visible: false },
    { overrides: { drafts: [savedDraft], batches: [batch] }, visible: false },
    { overrides: { results: { sent: 1, replied: 0, replies: [] } }, visible: false },
  ]) {
    const h = harness(null, [], { ...notices, ...overrides });
    let tree = await h.mount();
    for (const notice of Object.values(notices)) assert.equal(renderedText(h.expanded(tree)).includes(notice), visible);
    if (overrides.drafts) {
      action(tree, "Review sending · 1 approved").props.onClick();
      tree = h.render();
      for (const notice of Object.values(notices)) assert.ok(!renderedText(h.expanded(tree)).includes(notice));
    }
  }
});

test("a recorded booked call hides discovery notices even when returning to Prospects", async () => {
  const notices = { discovery_notice: "No reachable matches were verified.", people_search_notice: "No public profiles found." };
  for (const goal of [undefined, "book a first call"]) {
    const h = harness(null, [], { ...notices, manual_outreach: [{ id: "known_contact", name: "Clinic owner", booked_at: "2026-10-17T10:00:00Z" }],
      icp: { who: "Clinic owners", confirmed: true, mode: "company", goal } });
    const tree = await h.mount();
    const text = renderedText(h.expanded(tree));
    if (goal) assert.match(text, /Goal reached: you recorded your first booked call/);
    assert.doesNotMatch(text, /We found no people yet/);
    assert.equal(card(tree).props.step, "5 of 5 · Results");
    for (const notice of Object.values(notices)) assert.ok(!text.includes(notice));
    action(tree, "Review more outreach").props.onClick();
    const prospects = h.render();
    assert.equal(card(prospects).props.step, "2 of 5 · Prospects");
    for (const notice of Object.values(notices)) assert.ok(!renderedText(h.expanded(prospects)).includes(notice));
  }
});

test("outreach missing address has an actionable saved-draft recovery", async () => {
  const h = harness(null, [savedProspect], { drafts: [savedDraft], batches: [batch], can_send: false,
    send_blocked_reason: "gtm_sender_address_required", allow_sending: true, gmail: { connected: true } });
  await h.mount();
  action(h.render(), "Review sending · 1 approved").props.onClick(); await h.flush();
  assert.match(renderedText(h.expanded(h.render())), /Add sending address/);
  action(h.render(), "Add sending address").props.onClick(); await h.flush();
  assert.ok(action(h.expanded(h.render()), "Save sending address"));
  assert.deepEqual(h.opened, []);
});

test("consent requires a dated permission note even when country rules do not", async () => {
  const h = stuck([waiting("c1", "Acme", "GB")], { geo_options: [{ code: "GB", note_required: false, default_basis: "consent" }] });
  const tree = await h.mount();
  action(tree, "Confirm country for 1 prospect").props.onClick();
  const form = h.expanded(h.render());
  assert.equal(nodes(form).filter(node => node.type === "textarea").length, 1);
  assert.equal(action(form, "Save and check eligibility").props.disabled, true);
});
