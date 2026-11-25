const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const engine = {};
vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, "../../lib/nextStepEngine.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: engine, require: id => { if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    return id === "./client.ts" ? {} : require(id); } });

const decision = { decision_id: "decision", snapshot_hash: "a".repeat(64), venture_id: "ven_a", workspace_id: "ws_a",
  source: "rules", refining: false,
  hero: { action_id: "prep_call_brief", title: "Prepare the call", component: "call_brief", risk: "none", draft_only: false, params: { contact_id: "contact" },
    why: [{ text: "A booked call is tomorrow", evidence_ids: ["booking"] }], required_evidence_ids: ["booking"] }, alternatives: [],
  evidence: { booking: { id: "booking", kind: "call", summary: "Internal booking", display_summary: "Ada tomorrow <script>alert(1)</script>", contact_id: "contact" } } };

function harness(real = false) {
  let hooks = [], cursor = 0, subject = "usr_a", epoch = 1;
  const requests = [], outcomes = [], effects = [], listeners = new Map(), timers = new Map();
  let nextTimer = 0;
  const react = real ? React : {
    useState(value) { const index = cursor++; hooks[index] ||= { value }; return [hooks[index].value, next => { hooks[index].value = typeof next === "function" ? next(hooks[index].value) : next; }]; },
    useRef(value) { const index = cursor++; return hooks[index] ||= { current: value }; },
    useEffect(callback, deps) { const index = cursor++; if (!hooks[index] || !deps.every((value, offset) => value === hooks[index].deps[offset])) { hooks[index]?.cleanup?.(); hooks[index] = { deps }; effects.push(() => { hooks[index].cleanup = callback(); }); } },
  };
  const exported = {};
  const source = readFileSync(join(__dirname, "NextStepCard.tsx"), "utf8");
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
    exports: exported, Set, Object, Error, CustomEvent: class {}, window: { dispatchEvent() {}, addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name),
      setTimeout(callback) { const identifier = ++nextTimer; timers.set(identifier, callback); return identifier; }, clearTimeout: identifier => timers.delete(identifier) },
    require(id) {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };

      if (id === "react") return react;
      if (id === "react/jsx-runtime") return real ? require(id) : { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
      if (id === "next/link") return { default: ({ children, ...props }) => React.createElement("a", props, children) };
      if (id.endsWith("/OneCard")) return { OneCard: ({ title, body, actions, children }) => React.createElement("section", null, React.createElement("h2", null, title), React.createElement("p", null, body), children, actions) };
      if (id.endsWith("/ErrorNotice")) return { ErrorNotice: ({ error }) => error ? React.createElement("p", { role: "alert" }, error) : null };
      if (id === "./NextStepTools") return { NEXT_STEP_TOOLS: { call_brief: () => React.createElement("div", null, "Embedded call brief"), journey_entry: () => React.createElement("div", null, "Open the existing tool", "Check completion") } };
      if (id === "@/lib/client") return { ensureSession: async () => {}, verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch };
      if (id === "@/lib/nextStepEngine") return {
        evidenceLabel: engine.evidenceLabel, evidenceSummary: engine.evidenceSummary,
        validCard: action => ["call_brief", "journey_entry"].includes(action.component), stepDestination: (action, venture) => `/gtm?venture=${encodeURIComponent(venture)}${action === "review_leads" ? "&step=review" : ""}`, nextStepClient: {
          get: venture => new Promise((resolve, reject) => requests.push({ venture, resolve, reject })),
          outcome: async (...args) => { outcomes.push(args); },
        },
      };
      throw new Error(id);
    },
  });
  return { exported, requests, outcomes, render(ventureId = "ven_a") { cursor = 0; const view = exported.NextStepCard({ ventureId }); effects.splice(0).forEach(callback => callback()); return view; },
    focus() { listeners.get("focus")?.(); }, tick() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(callback => callback()); },
    retire() { subject = "usr_b"; epoch++; listeners.get("supymem-session-changed")?.(); }, close() { hooks.forEach(hook => hook.cleanup?.()); } };
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const viewNode = tree => tree.props.children[0];

test("hero renders an embedded tool, citations, override controls and escaped evidence", () => {
  const { NextStepView } = harness(true).exported;
  const html = renderToStaticMarkup(React.createElement(NextStepView, { decision, opened: true, onOpen() {}, onOutcome: async () => {}, onRetry() {} }));
  for (const text of ["Prepare the call", "Why this?", "Do it", "Skip for now", "Not relevant", "Embedded call brief", "Your call"]) assert.ok(html.includes(text), text);
  assert.ok(html.includes("&lt;script&gt;") && !html.includes("<script>"));
  const unsupported = renderToStaticMarkup(React.createElement(NextStepView, { decision, action: { ...decision.hero, component: "html" }, onOpen() {}, onOutcome: async () => {}, onRetry() {} }));
  assert.match(unsupported, /not supported/);
});

test("evidence displays plain labels and clean text, never machine summaries or identifiers", () => {
  const { NextStepView } = harness(true).exported;
  const evidence = {
    ids_b6df9c33nvxz: { kind: "idea", summary: "Idea state: confirmed; current revision: idv_4s726p0bm2gt", display_summary: "Your idea is confirmed." },
    "ven_xjjvnhy0ftv1:fact:audience": { kind: "founder_fact", summary: "audience: First-time parents (clarification; quote: First-time parents)", display_summary: "Audience: First-time parents" },
    "ven_xjjvnhy0ftv1:review": { kind: "review", summary: "Founder priorities: outreach_leads, other; unresolved:", display_summary: "Your priorities: finding customers" },
    dwo_abcdef123456: { kind: "learnings", summary: "{pains_confirmed: []}", display_summary: "Your call learnings are saved." },
    task_abcdef123456: { kind: "future_pipeline_kind", summary: "Pipeline notes: tsk_abcdef123456" },
  };
  const identifiers = Object.keys(evidence);
  const supplied = { ...decision, evidence, hero: { ...decision.hero, why: [{ text: "Based on what you shared.", evidence_ids: identifiers }], required_evidence_ids: identifiers } };
  const html = renderToStaticMarkup(React.createElement(NextStepView, { decision: supplied, onOpen() {}, onOutcome: async () => {}, onRetry() {} }));
  const visible = html.replace(/<[^>]*>/g, "");
  for (const label of ["Your idea", "What you told us", "Your review", "What you learned", "Saved evidence"]) assert.ok(visible.includes(label), label);
  assert.doesNotMatch(visible, /\b(?:ids|idv|ven|dwo|task|tsk)_[a-z0-9]{6,}|founder_fact|future_pipeline_kind|outreach_leads|Idea state|revision|clarification|quote:|unresolved|pains_confirmed|Pipeline notes|—/);
  assert.doesNotMatch(html, /<strong>(?:idea|founder_fact|review|learnings)<\/strong>|<small>/);
  assert.equal((visible.match(/First-time parents/g) || []).length, 1);
  assert.match(html, /id="next-evidence-ids_b6df9c33nvxz"/);
});

test("embedded briefs and learning source links never display evidence identifiers", () => {
  const tools = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, "NextStepTools.tsx"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, { exports: tools, require(id) {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };

    if (id === "react" || id === "react/jsx-runtime") return require(id);
    if (id === "@/lib/nextStepEngine") return engine;
    if (id.endsWith("/CallNotesForm")) return { CallBriefView: ({ text }) => React.createElement("pre", null, text) };
    return {};
  } });
  const identifier = "dwo_abcdef123456";
  const supplied = { ...decision, evidence: { [identifier]: { id: identifier, kind: "learnings", summary: "Pipeline notes: idv_4s726p0bm2gt",
    display_summary: "Parents need reliable advice.", observations: { pains_confirmed: ["Advice is hard to trust"], pains_refuted: ["Parents already have a solution"] } } } };
  const props = { decision: supplied, action: { ...decision.hero, required_evidence_ids: [identifier] }, disabled: false, current: () => true, onDone: async () => {} };
  for (const component of ["call_brief", "learning_summary"]) {
    const html = renderToStaticMarkup(React.createElement(tools.NEXT_STEP_TOOLS[component], props));
    assert.doesNotMatch(html.replace(/<[^>]*>/g, ""), /\b(?:ids|idv|ven|dwo|task|tsk)_[a-z0-9]{6,}|Pipeline notes/);
    assert.ok(html.includes(component === "call_brief" ? "Parents need reliable advice." : "Source 1"));
  }
});

test("risky cards stay drafts and never expose a send button", () => {
  const { NextStepView } = harness(true).exported;
  const html = renderToStaticMarkup(React.createElement(NextStepView, { decision, action: { ...decision.hero, risk: "sends", draft_only: true }, onOpen() {}, onOutcome: async () => {}, onRetry() {} }));
  assert.match(html, /Your draft stays private\. Ask to send or publish when you are ready\./);
  assert.doesNotMatch(html, /—/);
  assert.doesNotMatch(html, />Send<|dangerouslySetInnerHTML|<iframe/);
});

test("navigation tools link directly from Do it without a completion panel", () => {
  const { NextStepView } = harness(true).exported;
  const action = { ...decision.hero, action_id: "review_leads", component: "journey_entry", params: { context: "Saved prospects" } };
  const render = busy => renderToStaticMarkup(React.createElement(NextStepView, { decision, action, opened: true, busy, onOpen() { assert.fail("Navigation must not expand a panel"); }, onOutcome: async () => { assert.fail("Navigation must not mark completion"); }, onRetry() {} }));
  assert.match(render(false), /<a href="\/gtm\?venture=ven_a&amp;step=review">Do it<\/a>/);
  assert.doesNotMatch(render(false), /Open the existing tool|Check completion|aria-expanded/);
  assert.match(render(true), /<button type="button" disabled="">Do it<\/button>/);
});

test("card posts a skip then reloads without marking do-it complete", async () => {
  const fixture = harness(); fixture.render(); await settle(); fixture.requests[0].resolve(decision); await settle();
  const view = viewNode(fixture.render()); view.props.onOpen(); assert.equal(viewNode(fixture.render()).props.opened, true);
  assert.equal(fixture.outcomes.length, 0);
  const pending = view.props.onOutcome("skip"); await settle();
  assert.equal(fixture.outcomes[0][2], "skip"); assert.equal(fixture.outcomes[0][0].venture_id, "ven_a");
  fixture.requests.at(-1).resolve({ ...decision, hero: null }); await pending;
  assert.equal(viewNode(fixture.render()).props.decision.hero, null); fixture.close();
});

test("stale account and venture responses cannot render or post outcomes", async () => {
  const fixture = harness(); fixture.render(); await settle();
  const old = fixture.requests[0]; fixture.retire(); await settle(); old.resolve(decision); await settle();
  assert.equal(viewNode(fixture.render()).props.decision, null);
  fixture.requests.at(-1).resolve(decision); await settle(); const oldView = viewNode(fixture.render());
  fixture.render("ven_b"); await settle(); await oldView.props.onOutcome("done", { viewed: true });
  assert.equal(fixture.outcomes.length, 0); fixture.close();
});

test("an old render's outcome permission expires on account change in the same venture", async () => {
  const fixture = harness(); fixture.render(); await settle(); fixture.requests[0].resolve(decision); await settle();
  const oldView = viewNode(fixture.render()); fixture.retire(); await settle();
  await oldView.props.onOutcome("skip"); assert.equal(fixture.outcomes.length, 0);
  fixture.close();
});

test("pending refinement refreshes once on timer or focus without loading flicker", async () => {
  for (const trigger of ["tick", "focus"]) {
    const fixture = harness(); fixture.render(); await settle();
    fixture.requests[0].resolve({ ...decision, refining: true }); await settle(); fixture.render();
    fixture[trigger](); await settle();
    assert.equal(fixture.requests.length, 2);
    assert.equal(viewNode(fixture.render()).props.busy, false);
    assert.equal(viewNode(fixture.render()).props.decision.hero.title, "Prepare the call");
    const refined = { ...decision, source: "llm", hero: { ...decision.hero, title: "Refined call preparation" } };
    fixture.requests[1].resolve(refined); await settle();
    assert.equal(viewNode(fixture.render()).props.decision, refined);
    fixture.tick(); fixture.focus(); await settle();
    assert.equal(fixture.requests.length, 2); fixture.close();
  }
});

test("opening a tool before or during refinement preserves the founder's hero", async () => {
  for (const during of [false, true]) {
    const fixture = harness(); fixture.render(); await settle();
    fixture.requests[0].resolve({ ...decision, refining: true }); await settle();
    let tree = fixture.render();
    if (during) { fixture.tick(); await settle(); tree = fixture.render(); }
    viewNode(tree).props.onOpen();
    fixture.focus(); fixture.tick(); await settle();
    if (during) { fixture.requests[1].resolve({ ...decision, source: "llm", hero: { ...decision.hero, title: "Different hero" } }); await settle(); }
    const active = viewNode(fixture.render());
    assert.equal(active.props.decision.hero.title, "Prepare the call");
    assert.equal(active.props.opened, true);
    assert.equal(fixture.requests.length, during ? 2 : 1); fixture.close();
  }
});

test("evidence interaction also freezes automatic refinement", async () => {
  const fixture = harness(); fixture.render(); await settle();
  fixture.requests[0].resolve({ ...decision, refining: true }); await settle();
  const tree = fixture.render(); tree.props.onKeyDownCapture();
  fixture.tick(); fixture.focus(); await settle();
  assert.equal(fixture.requests.length, 1); fixture.close();
});

test("failed or still-pending refinement stays silent and never polls endlessly", async () => {
  for (const failure of [false, true]) {
    const fixture = harness(); fixture.render(); await settle();
    const pending = { ...decision, refining: true };
    fixture.requests[0].resolve(pending); await settle(); fixture.render();
    fixture.tick(); await settle();
    if (failure) fixture.requests[1].reject(new Error("offline"));
    else fixture.requests[1].resolve({ ...pending });
    await settle(); const active = viewNode(fixture.render());
    assert.equal(active.props.error, ""); assert.equal(active.props.busy, false);
    assert.equal(active.props.decision.hero.title, "Prepare the call");
    fixture.focus(); fixture.tick(); await settle();
    assert.equal(fixture.requests.length, 2); fixture.close();
  }
});

test("tool registry is fixed and reuses safe forms, drafts and waitlist", () => {
  const source = readFileSync(join(__dirname, "NextStepTools.tsx"), "utf8");
  for (const name of ["CallNotesForm", "CallBriefView", "CopyDraft", "Draft", "VentureWaitlist", "NEXT_STEP_TOOLS"]) assert.ok(source.includes(name), name);
  assert.doesNotMatch(source, /dangerouslySetInnerHTML|eval\(|new Function|journeyClient\.send|window\.open/);
});
