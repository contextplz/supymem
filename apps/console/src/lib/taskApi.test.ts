import * as chatRoutes from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import * as tasks from "./taskApi.ts";
import * as presentation from "./runStatusPresentation.ts";
import * as nextStep from "./nextStep.ts";
import * as markdown from "./chatMarkdown.ts";
import * as gtmProgress from "./gtmProgress.ts";

// Synthetic transport and component checks. Real backend/native journeys belong to R1.
const fixture: tasks.TaskView = {
  id: "internal-task-fixture", title: "Summarize replies", goal: "Every Friday, summarize replies.", kind: "scheduled",
  updated_at: "2026-10-09T08:05:00Z",
  state: "approval_required", deadline_at: "2026-10-09T09:00:00Z", deadline_is_estimate: true,
  next_fire_at: "2026-10-09T08:00:00Z", blocker: { class: "needs_approval", reason: "This draft needs your approval.", unblock_action: "Review the exact draft." },
  steps: [{ id: "internal-step-fixture", title: "Prepare a summary", state: "completed", deadline_at: "2026-10-09T09:00:00Z" }],
  events: [{ id: "internal-event-fixture", kind: "blocker", message: "Draft saved for your review.", created_at: "2026-10-09T08:05:00Z" }],
};
const contract: tasks.TaskContract = { title: "Summarize replies", goal: "Every Friday, summarize replies.", kind: "scheduled",
  trigger: {}, done_test: { type: "deterministic", spec: "A summary is saved" }, estimate_seconds: 900,
  steps: [{ title: "Prepare a summary", skill: "gtm.prepare", runs_on: "server" }], budget: { cost_micro: 500000, tokens: 100000 } };

test("preview, idempotent start, and lifecycle requests preserve the accepted contract", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = tasks.createTaskClient(async (path, init) => {
    calls.push({ path, init });
    return { ok: true, status: 200, data: path.endsWith("/preview") ? contract : fixture };
  });
  const plan = await client.preview("venture/fixture", contract.goal, "Asia/Kolkata");
  await client.create("venture/fixture", plan, "Asia/Kolkata", "start-fixture");
  for (const action of ["pause", "resume", "cancel"] as const) await client.act("venture/fixture", fixture.id, action);
  assert.equal(calls[0].path, "/api/ventures/venture%2Ffixture/handoffs/preview");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { goal: contract.goal, timezone: "Asia/Kolkata" });
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { goal: contract.goal, timezone: "Asia/Kolkata", contract, start: true });
  assert.equal(new Headers(calls[1].init?.headers).get("Idempotency-Key"), "start-fixture");
  assert.deepEqual(calls.slice(2).map(call => call.path.split("/").at(-1)), ["pause", "resume", "cancel"]);
});

test("an unconfirmed start is never automatically repeated and a manual retry preserves its key", async () => {
  const calls: RequestInit[] = [];
  const client = tasks.createTaskClient(async (_path, init) => {
    calls.push(init!);
    return calls.length === 1 ? { ok: false, status: 0, data: null } : { ok: true, status: 200, data: fixture };
  });
  await assert.rejects(client.create("venture", contract, "UTC", "same-start"), /could not be confirmed/);
  assert.equal(calls.length, 1);
  await client.create("venture", contract, "UTC", "same-start");
  assert.deepEqual(calls.map(call => new Headers(call.headers).get("Idempotency-Key")), ["same-start", "same-start"]);
});

test("Continue binds the current saved version and one request key, without client budget amounts", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const client = tasks.createTaskClient(async (path, init) => {
    calls.push({ path, init }); return { ok: true, status: 200, data: fixture };
  });
  await assert.rejects(client.act("venture", fixture.id, "continue"), /Refresh saved progress/);
  assert.equal(calls.length, 0);
  await client.act("venture", fixture.id, "continue", fixture.updated_at, "continue-once");
  assert.equal(calls[0].path, `/api/ventures/venture/handoffs/${fixture.id}/resume`);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { continue_budget: true, expected_updated_at: fixture.updated_at });
  assert.equal(new Headers(calls[0].init?.headers).get("Idempotency-Key"), "continue-once");
});

test("missing ownership, malformed receipts, and provider internals cannot populate task UI", async () => {
  let called = false;
  const client = tasks.createTaskClient(async () => { called = true; return { ok: true, status: 200, data: { items: [null] } }; });
  await assert.rejects(client.list(""), /Choose a venture/);
  assert.equal(called, false);
  await assert.rejects(client.list("venture"), /could not be confirmed/);
  const unsafe = tasks.createTaskClient(async () => ({ ok: false, status: 500, data: { detail: "ProviderError internal_job_123 cost_micro=500" } }));
  await assert.rejects(unsafe.preview("venture", "Task", "UTC"), error => error instanceof Error && !/ProviderError|internal_job|cost_micro/.test(error.message));
  const clarify = tasks.createTaskClient(async () => ({ ok: false, status: 422, data: { detail: "What exact frequency or event should start this task?" } }));
  await assert.rejects(clarify.preview("venture", "Task every few days", "UTC"), /What exact frequency or event/);
});

test("deadlines remain estimates, task states are plain English, and actions stay in the same venture", () => {
  assert.equal(tasks.estimatedDeadline(null), "being checked");
  assert.equal(tasks.estimatedDeadline("invalid"), "being checked");
  assert.match(tasks.estimatedDeadline(fixture.deadline_at), /^by /);
  assert.equal(tasks.taskStateLabel("approval_required"), "Needs your approval");
  assert.equal(tasks.taskStateLabel("unknown_internal_state"), "Checking saved progress");
  for (const href of ["javascript:alert(1)", "//outside.test/v/venture", "/v/other/tasks", "/v/venture/../../other"]) {
    assert.equal(tasks.taskActionHref(href, "venture"), null);
  }
  assert.equal(tasks.taskActionHref("/v/venture?chat=1", "venture"), "/v/venture?chat=1");
});

const nodes = (value: any): any[] => !value || typeof value !== "object" ? []
  : [value, ...[value.props?.children].flat(6).flatMap(nodes)];
const textOf = (value: any): string => Array.isArray(value) ? value.map(textOf).join(" ")
  : value && typeof value === "object" ? textOf(value.props?.children) : typeof value === "string" || typeof value === "number" ? String(value) : "";

function componentHarness(overrides: Record<string, any> = {}, component = "WorkingForYou") {
  const hooks: any[] = [], effects: Array<() => void> = [], listeners = new Map<string, () => void>();
  let cursor = 0, subject = "actor-fixture", epoch = 1;
  const same = (left: any[] = [], right: any[] = []) => left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const react = {
    useState(initial: any) { const index = cursor++; hooks[index] ??= { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (value: any) => { hooks[index].value = typeof value === "function" ? value(hooks[index].value) : value; }]; },
    useRef(current: any) { const index = cursor++; return hooks[index] ??= { current }; },
    useId() { cursor++; return "synthetic-goal-label"; },
    useCallback(fn: any, deps: any[]) { const index = cursor++; if (!hooks[index] || !same(hooks[index].deps, deps)) hooks[index] = { fn, deps }; return hooks[index].fn; },
    useEffect(fn: any, deps: any[]) { const index = cursor++; if (!hooks[index] || !same(hooks[index].deps, deps)) {
      const previous = hooks[index]; hooks[index] = { deps };
      effects.push(() => { previous?.cleanup?.(); hooks[index].cleanup = fn(); });
    } },
  };
  const client = { list: async () => [fixture], preview: async () => contract, create: async () => fixture,
    preferences: async (_venture: string) => ({ timezone: "Asia/Kolkata", confirmed: true }), savePreferences: async (_venture: string, timezone: string) => ({ timezone, confirmed: true }),
    edit: async () => ({ ...fixture, state: "paused" }),
    result: async () => ({ title: "Saved result", body_md: "A saved update", status: "partial", partial: true, artifacts: [] }),
    act: async (_venture: string, _id: string, action: tasks.TaskAction) => ({ ...fixture, can_resume: action === "pause", state: action === "cancel" ? "canceled" : action === "pause" ? "paused" : "working" }), ...overrides };
  const jsx = (type: any, props: any) => typeof type === "function" ? type(props) : { type, props };
  const dashboard = { venture: { id: "venture", name: "Fixture" }, needs_you: [],
    run_status: { state: "working", stage: "Your task", activity: "Preparing the saved summary", deadline_at: fixture.deadline_at } };
  const mocks: Record<string, any> = {
    react, "react/jsx-runtime": { jsx, jsxs: jsx },
    "@/lib/client": { ensureSession: async () => {}, verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
      api: async () => ({ ok: true, data: { god_mode: false } }), apiSwr: async (_path: string, update: (response: any) => void) => update({ ok: true, data: dashboard }) },
    "@/lib/taskApi": { ...tasks, taskClient: client, captureFounderTimezone: async (ventureId: string) => client.preferences(ventureId) }, "@/lib/runStatusPresentation": { ...presentation, RUN_STATUS_REFRESH: "synthetic-status-refresh" },
    "@/lib/nextStep": nextStep, "@/lib/apiCache": { cacheGet: () => ({ data: dashboard }) },
    "@/lib/gtmProgress": gtmProgress,
    "next/navigation": { usePathname: () => "/v/venture/tasks" },
    "@/components/desktop/EmergencyStopButton": { EmergencyStopButton: () => null }, "@/components/primitives/ResearchProgress": { ResearchProgress: () => null },
    "./RunStatusBar.module.css": { default: new Proxy({}, { get: (_, name) => name }) },
    "@/lib/chatMarkdown": markdown,
    "@/components/primitives/ErrorNotice": { ErrorNotice: ({ error }: { error: string }) => error ? jsx("p", { children: error }) : null },
    "@/app/v/[ventureId]/tasks/tasks.module.css": { default: new Proxy({}, { get: (_, name) => name }) },
  };
  const source = fs.readFileSync(new URL(`../components/${component === "RunStatusBar" ? "layout" : "tasks"}/${component}.tsx`, import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports: Record<string, any> = {};
  vm.runInNewContext(code, { exports, require: (id: string) => { if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    return mocks[id]; }, Intl, Date,
    crypto: { randomUUID: () => "synthetic-start-key" }, Event: class {},
    window: { setInterval: () => 1, clearInterval() {}, addEventListener: (name: string, fn: () => void) => listeners.set(name, fn), removeEventListener() {}, dispatchEvent() {} },
    document: { hidden: false, addEventListener() {}, removeEventListener() {} },
    CustomEvent: class { type: string; detail: any; constructor(type: string, options: any) { this.type = type; this.detail = options.detail; } },
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
  });
  return {
    render(ventureId = "venture") { cursor = 0; return exports[component]({ ventureId }); },
    async flush() { while (effects.length) effects.shift()!(); await new Promise(resolve => setImmediate(resolve)); },
    switchAccount() { subject = "different-actor"; epoch++; listeners.get("supymem-session-changed")?.(); },
  };
}
const control = (tree: any, name: string) => nodes(tree).find(node => node.props["data-usage-control"] === name);

test("eligible budget stop offers Continue as its unblocking action and retires its old actor callback", async () => {
  let called = 0;
  const budget = { ...fixture, state: "input_required", can_continue: true,
    blocker: { class: "budget_exhausted", reason: "The task reached its agreed limit.", unblock_action: "Review the partial result and choose whether to continue." } };
  const harness = componentHarness({ list: async () => [budget], act: async () => { called++; return fixture; } });
  harness.render(); await harness.flush();
  const tree = harness.render(), button = control(tree, "task_handoff_continue");
  assert.ok(button); assert.equal(control(tree, "task_handoff_action"), undefined);
  assert.doesNotMatch(textOf(tree), /tokens|cost_micro|credits|internal-task-fixture/i);
  harness.switchAccount(); await button.props.onClick(); await harness.flush();
  assert.equal(called, 0);
});

test("task cards and steps show estimates, partial results, and reversible pause controls without internal metadata", async () => {
  const harness = componentHarness(); harness.render(); await harness.flush();
  const tree = harness.render(), text = textOf(tree);
  assert.match(text, /Needs your approval/);
  assert.match(text, /Review the exact draft/);
  assert.match(text, /Draft saved for your review/);
  assert.equal((text.match(/Deadline estimate:/g) || []).length, 2);
  assert.doesNotMatch(text, /internal-task|internal-step|internal-event|cost_micro|tokens|credits/i);
  assert.equal(control(tree, "task_handoff_pause"), undefined);
  const working = componentHarness({ list: async () => [{ ...fixture, state: "working", blocker: null }] });
  working.render(); await working.flush();
  await control(working.render(), "task_handoff_pause").props.onClick(); await working.flush();
  const paused = working.render();
  assert.ok(control(paused, "task_handoff_resume"));
  assert.match(textOf(paused), /saved progress stays available/);
});

test("preview is reviewable with one primary action, and unconfirmed start keeps the same accepted key", async () => {
  const starts: string[] = [];
  const harness = componentHarness({ create: async (_venture: string, _contract: tasks.TaskContract, _timezone: string, key: string) => {
    starts.push(key); if (starts.length === 1) throw new Error("This action could not be confirmed."); return fixture;
  } });
  harness.render(); await harness.flush();
  let tree = harness.render();
  nodes(tree).find(node => node.type === "textarea").props.onChange({ target: { value: contract.goal } });
  tree = harness.render();
  nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  await harness.flush(); tree = harness.render();
  assert.equal(nodes(tree).filter(node => node.props.className === "primary").length, 1);
  assert.match(textOf(tree), /Completion estimate: about\s+15\s+minutes/);
  assert.match(textOf(tree), /will wait for your approval/);
  assert.doesNotMatch(textOf(tree), /cost_micro|500000|100000|credits/i);
  control(tree, "task_handoff_start").props.onClick(); await harness.flush();
  tree = harness.render(); assert.ok(control(tree, "task_handoff_start"));
  control(tree, "task_handoff_start").props.onClick(); await harness.flush();
  assert.deepEqual(starts, ["synthetic-start-key", "synthetic-start-key"]);
  assert.match(textOf(harness.render()), /Standing task saved/);
});

test("an account change retires an in-flight preview and cannot expose another actor's plan", async () => {
  let resolve: (plan: tasks.TaskContract) => void = () => {};
  const harness = componentHarness({ preview: () => new Promise(done => { resolve = done; }) });
  harness.render(); await harness.flush();
  let tree = harness.render();
  nodes(tree).find(node => node.type === "textarea").props.onChange({ target: { value: contract.goal } });
  tree = harness.render(); nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  harness.switchAccount(); resolve({ ...contract, title: "Old actor's private plan" }); await harness.flush();
  tree = harness.render();
  assert.equal(control(tree, "task_handoff_start"), undefined);
  assert.doesNotMatch(textOf(tree), /Old actor's private plan/);
});

test("a late task list cannot replace fresh work after leaving and returning to the venture", async () => {
  let resolve: (items: tasks.TaskView[]) => void = () => {}, calls = 0;
  const harness = componentHarness({ list: async () => {
    if (++calls === 1) return new Promise(done => { resolve = done; });
    return [{ ...fixture, title: "Fresh saved work" }];
  } });
  harness.render("venture"); await harness.flush();
  harness.render("other-venture"); await harness.flush();
  harness.render("venture"); await harness.flush();
  resolve([{ ...fixture, title: "Stale private work" }]); await harness.flush();
  const text = textOf(harness.render("venture"));
  assert.match(text, /Fresh saved work/);
  assert.doesNotMatch(text, /Stale private work/);
});

test("the persistent status bar and its details label task deadlines as estimates", () => {
  const snapshot = { venture: { id: "venture", name: "Fixture" }, needs_you: [],
    run_status: { state: "working", stage: "Task", activity: "Preparing the summary", deadline_at: fixture.deadline_at } };
  let state = 0;
  const jsx = (type: any, props: any) => typeof type === "function" ? type(props) : { type, props };
  const mocks: Record<string, any> = {
    react: { useState: (initial: any) => [state++ === 4 ? true : typeof initial === "function" ? initial() : initial, () => {}], useEffect() {} },
    "react/jsx-runtime": { jsx, jsxs: jsx }, "next/navigation": { usePathname: () => "/v/venture/tasks" },
    "@/lib/client": { verifiedSessionSubject: () => "fixture", verifiedSessionEpoch: () => 1 }, "@/lib/apiCache": { cacheGet: () => ({ data: snapshot }) },
    "@/lib/gtmProgress": gtmProgress,
    "@/lib/runStatusPresentation": presentation, "@/lib/nextStep": nextStep, "@/lib/taskApi": tasks,
    "@/components/desktop/EmergencyStopButton": { EmergencyStopButton: () => null }, "@/components/primitives/ResearchProgress": { ResearchProgress: () => null },
    "./RunStatusBar.module.css": { default: new Proxy({}, { get: (_, name) => name }) },
  };
  const source = fs.readFileSync(new URL("../components/layout/RunStatusBar.tsx", import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports: Record<string, any> = {};
  vm.runInNewContext(code, { exports, require: (id: string) => { if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    return mocks[id]; } });
  const tree = exports.RunStatusBar({ ventureId: "venture" });
  assert.equal((textOf(tree).match(/Deadline estimate:/g) || []).length, 2);
});

test("preferences capture condition and standing edits preserve scope and the exact concurrency token", async () => {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const idle = { ...fixture, state: "paused" };
  const client = tasks.createTaskClient(async (path, init) => {
    calls.push({ path, init });
    return { ok: true, status: 200, data: path.endsWith("task-preferences") ? { timezone: "America/New_York", confirmed: true } : idle };
  });
  await client.preferences("venture");
  await client.savePreferences("venture", "America/New_York", true);
  await client.edit("venture", idle, contract, "America/New_York");
  assert.equal(calls[0].path, "/api/ventures/venture/task-preferences");
  assert.equal(calls[1].init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { timezone: "America/New_York", only_if_unconfirmed: true });
  assert.deepEqual(JSON.parse(String(calls[2].init?.body)), { goal: contract.goal, timezone: "America/New_York", contract, expected_updated_at: fixture.updated_at });
  assert.equal(calls[2].init?.method, "PATCH");
  await assert.rejects(client.edit("venture", { ...idle, kind: "one_time" }, contract, "UTC"), /Finish or stop/);
  await assert.rejects(client.edit("venture", { ...idle, can_edit: false }, contract, "UTC"), /Finish or stop/);
  assert.equal(calls.length, 3);
});

test("timezone capture respects manual preference and retires after an actor change", async () => {
  let subject = "actor-a", epoch = 1, writes = 0;
  const mocks = { api() {}, verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch };
  const source = fs.readFileSync(new URL("./taskApi.ts", import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports: Record<string, any> = {};
  vm.runInNewContext(code, { exports, require: () => mocks, Intl, URL });
  const client = { preferences: async () => ({ timezone: "America/New_York", confirmed: true }), savePreferences: async () => { writes++; return { timezone: "UTC", confirmed: true }; } };
  assert.equal((await exports.captureFounderTimezone("venture", client)).timezone, "America/New_York");
  assert.equal(writes, 0);
  client.preferences = async () => { subject = "actor-b"; return { timezone: "UTC", confirmed: false }; };
  await assert.rejects(exports.captureFounderTimezone("venture", client), /account changed/);
  assert.equal(writes, 0);
  subject = "actor-a";
  client.preferences = async () => { epoch += 2; return { timezone: "UTC", confirmed: false }; };
  await assert.rejects(exports.captureFounderTimezone("venture", client), /account changed/);
  assert.equal(writes, 0);
});

test("future-only edit uses the reviewed card and never starts or resumes a run", async () => {
  const idle = { ...fixture, state: "paused", can_edit: true };
  const edits: Array<{ task: tasks.TaskView; contract: tasks.TaskContract; timezone: string }> = [];
  let starts = 0;
  const harness = componentHarness({ list: async () => [idle], preferences: async () => ({ timezone: "America/New_York", confirmed: true }),
    create: async () => { starts++; return fixture; },
    edit: async (_venture: string, task: tasks.TaskView, contract: tasks.TaskContract, timezone: string) => {
      edits.push({ task, contract, timezone }); return { ...idle, goal: contract.goal };
    } });
  harness.render(); await harness.flush();
  let tree = harness.render();
  control(tree, "task_handoff_edit_future").props.onClick();
  tree = harness.render();
  assert.match(textOf(tree), /Changes apply to future runs/);
  nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  await harness.flush(); tree = harness.render();
  assert.ok(control(tree, "task_handoff_save_edit"));
  assert.equal(control(tree, "task_handoff_start"), undefined);
  control(tree, "task_handoff_save_edit").props.onClick(); await harness.flush();
  assert.equal(edits.length, 1);
  assert.equal(edits[0].task.updated_at, idle.updated_at);
  assert.equal(edits[0].timezone, "America/New_York");
  assert.equal(starts, 0);
  assert.match(textOf(harness.render()), /stays paused until you resume/);
});

test("timezone Settings edits only timezone and clear saved data across actors", async () => {
  const saved: string[] = [];
  const harness = componentHarness({ savePreferences: async (_venture: string, timezone: string) => {
    saved.push(timezone); return { timezone, confirmed: true };
  } }, "TaskPreferences");
  harness.render(); await harness.flush();
  let tree = harness.render();
  nodes(tree).find(node => node.type === "input").props.onChange({ target: { value: "America/New_York" } });
  tree = harness.render(); nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  await harness.flush();
  assert.deepEqual(saved, ["America/New_York"]);
  assert.match(textOf(harness.render()), /Existing schedules keep their saved timezone/);
  harness.switchAccount(); tree = harness.render();
  assert.equal(nodes(tree).find(node => node.type === "input").props.value, "");
});

test("result requests keep only safe exact-venture links and reject malformed completion claims", async () => {
  let path = "";
  const result: tasks.TaskResult = { title: "Saved result", body_md: "Safe saved prose", status: "verified", partial: false,
    artifacts: [{ label: "Research", href: "/v/venture/research" }, { label: "GTM", href: "/gtm?venture=venture" },
      { label: "Bad", href: "javascript:alert(1)" }, { label: "Wrong", href: "/gtm?venture=other" }] };
  const client = tasks.createTaskClient(async value => { path = value; return { ok: true, status: 200, data: result }; });
  assert.equal((await client.result("venture", "task")).artifacts.length, 2);
  assert.equal(path, "/api/ventures/venture/handoffs/task/result");
  result.partial = true;
  await assert.rejects(client.result("venture", "task"), /could not be confirmed/);
  assert.equal(tasks.taskResultHref("/gtm?venture=venture&venture=other", "venture"), null);
});

test("saved result dialog renders escaped Markdown and retires when the actor changes", async () => {
  const harness = componentHarness({ result: async () => ({ title: "Saved result", body_md: "**A saved update** <script>alert(1)</script>",
    status: "partial", partial: true, artifacts: [] }) });
  harness.render(); await harness.flush();
  let tree = harness.render();
  control(tree, "task_handoff_result_open").props.onClick(); await harness.flush();
  tree = harness.render();
  assert.ok(nodes(tree).find(node => node.type === "dialog"));
  const html = nodes(tree).find(node => node.props.dangerouslySetInnerHTML)?.props.dangerouslySetInnerHTML.__html;
  assert.match(html, /<strong>A saved update<\/strong>/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  harness.switchAccount(); tree = harness.render();
  assert.equal(nodes(tree).find(node => node.type === "dialog"), undefined);
});

test("interrupted research modal starts a fresh run at once, offers skip, and saved sources", async () => {
  const requests: string[] = [], actions: string[] = [];
  const interrupted = { ...fixture, kind: "one_time", state: "input_required", title: "Research your idea",
    research_recovery: { revision_id: "rev_current", job_id: "job_old", saved_href: "/v/venture/research" },
    blocker: { class: "needs_input", reason: "Research was interrupted.", unblock_action: "Run research again, or skip for now." } };
  const harness = componentHarness({ list: async () => [interrupted],
    result: async () => ({ title: "Saved task result", body_md: "No saved result is available for this run yet.", status: "pending", partial: true, artifacts: [] }),
    rerunResearch: async (_venture: string, task: tasks.TaskView, key: string) => {
      assert.equal(task.research_recovery?.job_id, "job_old"); requests.push(key); return "job_new";
    }, act: async (_venture: string, _id: string, action: string) => { actions.push(action); return { ...interrupted, state: "canceled" }; } });
  harness.render(); await harness.flush();
  control(harness.render(), "task_handoff_result_open").props.onClick(); await harness.flush();
  let tree = harness.render();
  assert.ok(control(tree, "task_result_research_rerun"));
  assert.ok(control(tree, "task_result_research_skip"));
  assert.match(textOf(tree), /Research was interrupted/);
  assert.doesNotMatch(textOf(tree), /No saved result is available|reconciliation/);
  assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === "/v/venture/research"));
  await control(tree, "task_result_research_rerun").props.onClick(); await harness.flush();
  assert.equal(requests.length, 1); assert.deepEqual(actions, ["cancel"]);
  assert.doesNotMatch(textOf(harness.render()), /credits/);
});

test("interrupted research skip stops the old task, while a retired actor cannot rerun", async () => {
  let calls = 0;
  const interrupted = { ...fixture, state: "input_required", research_recovery: { revision_id: "rev_current", job_id: "job_old" } };
  const harness = componentHarness({ list: async () => [interrupted],
    act: async () => { calls++; return { ...interrupted, state: "canceled" }; }, rerunResearch: async () => { calls++; return "job_new"; } });
  harness.render(); await harness.flush();
  control(harness.render(), "task_handoff_result_open").props.onClick(); await harness.flush();
  await control(harness.render(), "task_result_research_skip").props.onClick(); await harness.flush();
  assert.equal(calls, 1);
  const rerun = control(harness.render(), "task_research_rerun");
  harness.switchAccount(); await rerun.props.onClick(); await harness.flush();
  assert.equal(calls, 1);
});

test("research retry uses the existing new-run endpoint and never the old failed-job continuation", async () => {
  const sent: any[] = [];
  const client = tasks.createTaskClient(async (path, init) => { sent.push({ path, body: JSON.parse(String(init?.body)) });
    return { ok: true, status: 200, data: { job_id: "job_new" } }; });
  const task = { ...fixture, research_recovery: { revision_id: "rev_current", job_id: "job_old" } };
  assert.equal(await client.rerunResearch("venture", task, "click_key"), "job_new");
  assert.deepEqual(sent, [{ path: "/api/ventures/venture/research/dossier/regenerate", body: { expected_revision_id: "rev_current", request_key: "click_key" } }]);
  const old = tasks.createTaskClient(async () => ({ ok: true, status: 200, data: { job_id: "job_old" } }));
  await assert.rejects(old.rerunResearch("venture", task, "key"), /still finishing/);
});

test("a failed research start retains one new-run key across a retry", async () => {
  const keys: string[] = [];
  const task = { ...fixture, state: "input_required", research_recovery: { revision_id: "rev_current", job_id: "job_old" } };
  const harness = componentHarness({ list: async () => [task], rerunResearch: async (_venture: string, _task: tasks.TaskView, key: string) => {
    keys.push(key); if (keys.length === 1) throw new Error("The research request could not be confirmed."); return "job_new";
  } });
  harness.render(); await harness.flush();
  await control(harness.render(), "task_research_rerun").props.onClick(); await harness.flush();
  assert.equal(keys.length, 1);
  await control(harness.render(), "task_research_rerun").props.onClick(); await harness.flush();
  assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]);
});

test("task deadlines and first scheduled preview use saved manual timezone, regardless of device timezone", async () => {
  const harness = componentHarness({ preferences: async () => ({ timezone: "America/New_York", confirmed: true }),
    preview: async () => ({ ...contract, trigger: { first_fire_at: "2026-10-09T09:00:00Z" } }) });
  harness.render(); await harness.flush();
  let tree = harness.render();
  assert.match(textOf(tree), /Deadline estimate:.*5:00/);
  assert.doesNotMatch(tasks.estimatedDeadline(fixture.deadline_at, "America/New_York"), /2:30/);
  nodes(tree).find(node => node.type === "textarea").props.onChange({ target: { value: contract.goal } });
  tree = harness.render(); nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  await harness.flush(); tree = harness.render();
  assert.match(textOf(tree), /First run estimate:.*5:00/);
});

test("preview shows structured count checks and rubric criteria before one primary Start action", async () => {
  const checks = [
    { done_test: { type: "deterministic", spec: { predicate: "at_least", metric: "source_count", count: 3 } }, expected: /at least\s+3\s+verified sources/ },
    { done_test: { type: "rubric", spec: { predicate: "rubric", criteria: ["Cite observed sources", "Explain the practical tradeoffs"] } }, expected: /Cite observed sources/ },
  ];
  for (const check of checks) {
    const harness = componentHarness({ preview: async () => ({ ...contract, done_test: check.done_test, tolerance: 0.9 }) });
    harness.render(); await harness.flush();
    nodes(harness.render()).find(node => node.type === "textarea").props.onChange({ target: { value: contract.goal } });
    nodes(harness.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    await harness.flush();
    const tree = harness.render(), text = textOf(tree);
    assert.match(text, check.expected);
    if (check.done_test.type === "rubric") assert.match(text, /Explain the practical tradeoffs[\s\S]*90\s*% or higher/);
    assert.equal(nodes(tree).filter(node => node.props.className === "primary").length, 1);
    assert.doesNotMatch(text, /source_count|predicate|cost_micro|credits/);
  }
});

test("status bar reads current owner's manual timezone for both deadline views", async () => {
  const harness = componentHarness({ preferences: async () => ({ timezone: "America/New_York", confirmed: true }) }, "RunStatusBar");
  harness.render(); await harness.flush();
  const expected = tasks.estimatedDeadline(fixture.deadline_at, "America/New_York");
  assert.ok(textOf(harness.render()).includes(expected));
  control(harness.render(), "run_status_details").props.onClick();
  const text = textOf(harness.render());
  assert.equal(text.split(expected).length - 1, 2);
  assert.ok(!text.includes(tasks.estimatedDeadline(fixture.deadline_at, "Asia/Kolkata")));
});

test("an old actor's delayed timezone callback cannot replace the status bar's current clock", async () => {
  let resolve: (preferences: tasks.TaskPreferences) => void = () => {};
  const harness = componentHarness({ preferences: () => new Promise(done => { resolve = done; }) }, "RunStatusBar");
  harness.render(); await harness.flush();
  harness.switchAccount();
  resolve({ timezone: "Pacific/Auckland", confirmed: true });
  await harness.flush();
  const text = textOf(harness.render());
  assert.ok(!text.includes(tasks.estimatedDeadline(fixture.deadline_at, "Pacific/Auckland")));
  assert.ok(text.includes(tasks.estimatedDeadline(fixture.deadline_at, "UTC")));
});
