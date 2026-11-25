import * as chatRoutes from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { chatFirstHidden } from "./chatVisibility.ts";
import { historyGroups } from "./chatConversations.ts";

const flush = () => new Promise<void>(done => setImmediate(done));
const read = (path: string) => fs.readFileSync(new URL(path, import.meta.url), "utf8");
function compile(path: string, modules: Record<string, any>, globals: Record<string, any> = {}) {
  const exports: Record<string, any> = {};
  vm.runInNewContext(ts.transpileModule(read(path), { fileName: path, compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText,
    { exports, require: (id: string) => { if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    if (id === "@/lib/chatRouteAction") return modules[id] || chatRoutes;
    if (id === "./chatVisibility.ts") return compile(id, {}, globals);
    if (id === "./localMode.ts") return { isLocalConsole: () => false };
    if (id === "./operationsScope.ts") return compile(id, {}, globals);
    if (id === "@/components/settings/AccountSettings") return { AccountSettings: "account-settings" };
    if (id === "@/components/layout/LocalModeBoundary") return { useLocalMode: () => false, useFeatureVisibility: () => () => true };
    return modules[id] || (id.endsWith("ventureCards") ? compile("../components/ventures/ventureCards.ts", {}, globals) : { default: {} }); }, URL, URLSearchParams, Date, Map, Set, ...globals });
  return exports;
}
function hooks() {
  let cursor = 0;
  const slots: any[] = [], pending: (() => void)[] = [], cleanups: (() => void)[] = [];
  const react = {
    useId: () => 'saved-document-title',
    useState: (initial: any) => { const index = cursor++; slots[index] ??= { value: initial }; return [slots[index].value, (value: any) => { slots[index].value = typeof value === "function" ? value(slots[index].value) : value; }]; },
    useRef: (initial: any) => { const index = cursor++; return slots[index] ??= { current: initial }; },
    useEffect: (effect: () => void, deps: any[]) => { const index = cursor++; if (!slots[index] || deps.some((value, position) => value !== slots[index].deps[position])) { slots[index]?.cleanup?.(); slots[index] = { deps }; pending.push(() => { const cleanup = effect(); slots[index].cleanup = cleanup; if (typeof cleanup === "function") cleanups.push(cleanup); }); } },
    useContext: (context: any) => context.value,
    createContext: (value: any) => ({ value, Provider: "provider" }),
  };
  return { react, render: <T>(body: () => T): T => { cursor = 0; const result = body(); while (pending.length) pending.shift()!(); return result; }, close: () => cleanups.forEach(cleanup => cleanup()) };
}
const jsx = (type: any, props: any) => ({ type, props: props || {} });
const nodes = (node: any): any[] => !node || typeof node !== "object" ? [] : [node, ...[node.props?.children].flat(10).flatMap(nodes)];
const text = (node: any): string => (typeof node === "string" || typeof node === "number" ? String(node) : node && typeof node === "object" ? [node.props?.children].flat(10).map(text).join(" ") : "").replace(/\s+/g, " ").trim();

function dataFixture(cold = false) {
  const runtime = hooks(), calls: any[] = [], listeners = new Map<string, () => void>(), storage = new Map<string, string>();
  let subject: string | null = cold ? null : "usr_home", epoch = 1, venture = "ven_home", handler: (path: string, options?: any) => any;
  const hub = { workspace_id: "wsp_home", order: ["gmail", "x"], connections: { gmail: { name: "Gmail", connected: true, ready: true, action: "sync" }, x: { name: "X", connected: true, ready: false, action: "connect" } } };
  const respond = (path: string) => {
    if (path === "/api/ventures/ven_home") return { workspace_id: "wsp_home", created_at: "2026-10-10T00:00:00Z" };
    if (path.endsWith("/org")) return { agents: [{ id: "agent_real", name: "Ada", role: "researcher", work: { did: "Reviewed real interviews" } }] };
    if (path.endsWith("/agents")) return { items: [{ id: "agent_real", status: "paused" }] };
    if (path.endsWith("/idea/sessions")) return {items: [{state: "confirmed"}]};
    if (path.includes("/builds")) return {items: []};
    if (path.endsWith("/ui-pins")) return { venture_id: "ven_home", pins: ["Research"] };
    if (path.endsWith("/ui-archive")) return { venture_id: "ven_home", open_work: 3, items: [{ id: "doc_real", folder: "knowledge", title: "Saved context" }] };
    if (path.endsWith("/waitlist")) return { total: 4 };
    if (path.endsWith("/apps")) return { items: [{ id: "app_one" }, { id: "app_two" }] };
    if (path.endsWith("/analytics/overview")) return { today: { visits: path.includes("app_one") ? 2 : 7, signups: 1 }, days: [{ day: "2026-10-14", visits: 4 }] };
    if (path.endsWith("/analytics/funnel")) return { steps: ["landing_view"], counts: { landing_view: 4 } };
    if (path === "/api/workspaces/wsp_home/connections") return { items: [{ workspace_id: "wsp_home", provider: "github", status: "active", health: "ok", simulated: false }, { workspace_id: "wsp_home", provider: "linear", status: "active", health: "ok", simulated: true }] };
    throw new Error(path);
  };
  handler = (path: string) => ({ ok: true, data: respond(path) });
  const module = compile("./companyHome.ts", { react: runtime.react, "./client": { api: async (path: string, options?: any) => { calls.push({ path, options }); return handler(path, options); }, ensureSession: async () => { if (cold && !subject) { subject = "usr_home"; epoch++; } return { ok: true, data: { authenticated: true, subject, email: "ada@example.test" } }; }, verifiedSessionEpoch: () => epoch, verifiedSessionSubject: () => subject }, "./opsApi": { getConnectionsStatus: async (workspace: string) => { calls.push({ workspace }); return hub; } } }, { window: { localStorage: {getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value)}, addEventListener: (name: string, callback: () => void) => listeners.set(name, callback), removeEventListener: (name: string) => listeners.delete(name) } });
  return { module, calls, hub, storage, render: () => runtime.render(() => module.useCompanyHomeData(venture)), respond, setHandler: (next: typeof handler) => { handler = next; }, change: (next: string) => { subject = next; epoch++; listeners.get("supymem-session-changed")?.(); }, venture: (next: string) => { venture = next; }, close: runtime.close };
}

test("U65 reload hydrates saved archives after cold session verification", async () => {
  const fixture = dataFixture(true);
  fixture.render(); await flush(); await flush();
  assert.equal(fixture.render().data.archives[0].title, "Saved context");
  assert.equal(fixture.render().data.subject, "usr_home");
  assert.equal(fixture.calls.filter(call => call.path?.endsWith("/ui-archive")).length, 1);
  fixture.close();
});

test("home uses saved departments, founding date, healthy accounts and all applications", async () => {
  const fixture = dataFixture(); fixture.render(); await flush(); await flush();
  const { data } = fixture.render();
  assert.equal(data.views, 9); assert.equal(data.signups, 2); assert.equal(data.waitlist, 4); assert.equal(data.openWork, 3);
  assert.equal(data.days[0].visits, 8); assert.equal(data.funnel.counts.landing_view, 8);
  assert.equal(data.agents[0].status, "paused"); assert.equal(data.agents[0].work.did, "Reviewed real interviews");
  assert.equal(fixture.module.departmentFor(data.agents[0]), "Research");
  assert.equal(fixture.module.departmentFor({ role: "builder", department: "Saved team" }), "Saved team");
  assert.equal(fixture.module.departmentFor({ kind: "builder" }), "Product & Engineering");
  assert.equal(fixture.module.foundedDays("2026-10-10T00:00:00Z", Date.parse("2026-10-15T00:00:00Z")), 5);
  assert.equal(fixture.module.foundedDays("2026-10-10T00:00:00", Date.parse("2026-10-15T00:00:00Z")), 5);
  assert.equal(fixture.module.foundedDays("2026-10-10T00:00:00+00:00Z", Date.parse("2026-10-15T00:00:00Z")), 5);
  assert.equal(fixture.module.foundedDays("bad"), null);
  assert.equal(fixture.module.liveToolkitCount(data.cards), 2);
  assert.equal(data.cards.find((card: any) => card.id === "linear").connected, false);
  assert.equal(fixture.calls.filter(call => call.path?.endsWith("analytics/overview")).length, 2);
  fixture.close();
});

test("failed reads never masquerade as zero; empty saved data stays empty", async () => {
  const fixture = dataFixture();
  fixture.setHandler(path => path.endsWith("/apps") ? { ok: false } : { ok: true, data: fixture.respond(path) });
  fixture.render(); await flush(); await flush();
  assert.equal(fixture.render().data.views, null); assert.ok(fixture.render().data.errors.includes("Visitors"));
  fixture.setHandler(path => ({ ok: true, data: path.endsWith("/org") ? { agents: [] } : path.endsWith("/apps") ? { items: [] } : fixture.respond(path) }));
  fixture.render().retry(); fixture.render(); await flush(); await flush();
  assert.equal(fixture.render().data.views, 0); assert.equal(fixture.render().data.agents.length, 0);
  fixture.close();
});

test("pins use one scoped server mutation and reject stale account replies", async () => {
  const fixture = dataFixture(); fixture.render(); await flush(); await flush();
  let resolve!: (value: any) => void;
  fixture.setHandler((path, options) => options ? new Promise(done => { resolve = done; }) : ({ ok: true, data: fixture.respond(path) }));
  const pending = fixture.render().pin("Research", false);
  const call = fixture.calls.at(-1);
  assert.equal(call.path, "/api/ventures/ven_home/ui-pins"); assert.deepEqual(JSON.parse(call.options.body), { department: "Research", pinned: false });
  await fixture.render().pin("Operations", true);
  assert.equal(fixture.calls.filter(call => call.options).length, 1);
  fixture.change("usr_other"); fixture.render();
  resolve({ ok: true, data: { venture_id: "ven_home", pins: ["Private old pin"] } }); await pending; await flush();
  assert.ok(!fixture.render().data.pins?.includes("Private old pin"));
  fixture.close();
});

test("cached pins are account-and-venture scoped, read-only until the server confirms them", async () => {
  const fixture = dataFixture();
  fixture.storage.set("supymem.sidebar-pins.usr_home.ven_home", JSON.stringify(["Strategy"]));
  fixture.storage.set("supymem.sidebar-pins.usr_other.ven_home", JSON.stringify(["Private other pin"]));
  fixture.setHandler(path => path.endsWith("/ui-pins") ? ({ok: false}) : ({ok: true, data: fixture.respond(path)}));
  fixture.render(); await flush(); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(fixture.render().data.pins)), ["Strategy"]);
  assert.ok(fixture.render().data.errors.includes("Quick access"));
  assert.equal(fixture.render().pinBusy, true);
  await fixture.render().pin("Research", true);
  assert.ok(!fixture.calls.some(call => call.options?.method === "POST"));
  fixture.setHandler(path => ({ok: true, data: fixture.respond(path)}));
  fixture.render().retry(); fixture.render(); await flush(); await flush();
  assert.deepEqual(JSON.parse(fixture.storage.get("supymem.sidebar-pins.usr_home.ven_home")!), ["Research"]);
  fixture.change("usr_other"); fixture.render();
  assert.ok(!fixture.render().data.pins?.includes("Research"));
  fixture.close();
});

function viewFixture(tab: string, patch: any = {}, department?: string) {
  const runtime = hooks(), calls: any[] = [], imports = dataFixture().module;
  const routes = compile("./founderRoutes.ts", { "./betaRoutes": compile("./betaRoutes.ts", {}) });
  let epoch = 1;
  const data = { ventureId: "ven_home", workspaceId: "wsp_home", createdAt: "2026-10-10", agents: [{ id: "agent_real", name: "Ada", role: "researcher", status: "active", work: { did: "Reviewed real interviews", live_state: "working", doing: "Interviewing real buyers" } }], cards: [{ id: "gmail", name: "Gmail", connected: true, ready: true, action: "connect" }], archives: [{ id: "doc_real", folder: "knowledge", title: "Saved thesis", href: "/v/ven_home/context", created_at: "2026-10-12", updated_at: "2026-10-14", document: true }], pins: [], openWork: 2, waitlist: 3, views: 9, signups: 2, days: [], errors: [], ...patch };
  const owner = { epoch };
  const state = { data, owner, current: (candidate: any) => candidate.epoch === epoch, retry: () => calls.push({ action: "retry" }), pin: (name: string, pinned: boolean) => calls.push({ action: "pin", name, pinned }), pinBusy: false, pinNote: "" };
  const module = compile("../components/office/CompanyHome.tsx", { react: runtime.react, "react/jsx-runtime": { jsx, jsxs: jsx }, "next/link": { default: "a" }, "@/lib/founderRoutes": routes, "./CompanyOffice.module.css": { default: { primaryBtn: "primary" } }, "@/lib/companyHome": imports, "next/navigation": {useRouter: () => ({push: (href: string) => calls.push({href})})}, "@/lib/jobCopy": {plainJob: ({status}: any) => ({title: status})}, "@/lib/format": { agentStatusLabel: (status: string) => status === "active" ? "Available" : status }, "@/lib/client": { api: async (path: string, options: any) => { calls.push({ path, options }); return { ok: true, data: path.endsWith("doc_real") ? { id: "doc_real", title: "Saved thesis", text: "Real knowledge" } : { id: "connection_real" } }; } }, "@/lib/chatPrefill": { prefillChat: (value: string) => calls.push({ prefill: value }) }, "@/lib/opsApi": { connectHubProvider: async (...args: any[]) => { calls.push({ action: "connect", args }); return { authorize_url: "https://connect.composio.dev/link/fixture" }; }, syncHubProvider: async (...args: any[]) => calls.push({ action: "sync", args }) }, "@/components/connections/connectionCopy": { connectionActionLabel: () => "Connect", connectionState: (card: any) => card.connected ? "Connected" : "Not connected" }, "@/lib/desktop": { isDesktopShell: () => false }, "./avatars": { EmployeePortrait: "portrait" }, "./OfficeProvider": { useOfficeOptional: () => ({ org: { agents: data.agents }, setSelectedAgent: (agent: any) => calls.push({ agent: agent.id }) }) } }, { window: { open: (url: string) => calls.push({ url }) } });
  module.CompanyHomeContext.value = state;
  const render = () => runtime.render(() => module.CompanyHome({ tab, department, children: "Production next step" }));
  const click = (label: string) => { const tree = render(); const button = nodes(tree).find(node => node.type === "button" && text(node) === label); assert.ok(button, `missing ${label}`); return button.props.onClick({stopPropagation() {}}); };
  return { render, click, calls, module, data, runtime, change: () => { epoch++; } };
}

test("Home removes the dashboard but preserves its notices and company controls", () => {
  const fixture = viewFixture("home");
  assert.match(text(fixture.render()), /Production next step/);
  assert.doesNotMatch(text(fixture.render()), /Page views|Visitors|Milestones|Agents on the floor/);
  assert.ok(!nodes(fixture.render()).some(node => node.props["data-company-home"] === "home"));
});

test("departments show real counts and work on their floor, with working pin and agent actions", () => {
  const chooser = viewFixture("departments"); assert.match(text(chooser.render()), /Research 1 agent/);
  const research = nodes(chooser.render()).find(node => node.type === "article" && node.props["data-dept-id"] === "Research");
  nodes(research).find(node => node.type === "button").props.onClick({stopPropagation() {}}); assert.deepEqual(chooser.calls.at(-1), { action: "pin", name: "Research", pinned: true });
  const floor = viewFixture("departments", {}, "Research"); assert.match(text(floor.render()), /Reviewed real interviews/); assert.match(text(floor.render()), /Interviewing real buyers/);
  nodes(floor.render()).find(node => node.props["aria-label"] === "Open Ada").props.onClick(); assert.deepEqual(floor.calls.at(-1), { agent: "agent_real" });
  const empty = viewFixture("departments", { agents: [] }); assert.match(text(empty.render()), /No saved work/); const floorEmpty = viewFixture("departments", {agents: []}, "Research"); assert.match(text(floorEmpty.render()), /No one is seated/); floorEmpty.click("Ask Charlie"); assert.equal(floorEmpty.calls.at(-1).prefill, "Help me plan the Research team");
});

test("D32 Marketing & GTM always offers the command palette's Find customers route", () => {
  const routes = compile("./founderRoutes.ts", { "./betaRoutes": compile("./betaRoutes.ts", {}) });
  for (const patch of [
    { ventureId: "ven empty &", agents: [], archives: [], pins: [] },
    { ventureId: "ven saved", agents: [], archives: [{ id: "outreach", folder: "outreach", title: "Saved outreach", href: "/gtm" }], pins: ["Marketing & GTM"] },
    { ventureId: "ven staffed", agents: [{ id: "marketer", name: "Ada", role: "builder", department: "Marketing & GTM" }], archives: [] },
    { ventureId: "ven loading", agents: null, archives: null, errors: ["Departments"] },
  ]) {
    const floor = viewFixture("departments", patch, "Marketing & GTM");
    assert.equal(nodes(floor.render()).find(node => node.type === "button" && text(node) === "Find customers").props.className, "primary");
    floor.click("Find customers");
    assert.equal(floor.calls.at(-1).href, routes.founderRouteLinks(patch.ventureId).find((route: any) => route.label === "Find customers").href);
    if (patch.agents?.length === 0) {
      assert.match(text(floor.render()), /No one is seated in this department yet/);
      floor.click("Ask Charlie");
      assert.equal(floor.calls.at(-1).prefill, "Help me plan the Marketing & GTM team");
    }
    floor.runtime.close();
  }
  for (const department of dataFixture().module.companyDepartments([])) {
    if (department.name === "Marketing & GTM") continue;
    const floor = viewFixture("departments", { agents: [] }, department.name);
    assert.doesNotMatch(text(floor.render()), /Find customers/);
    floor.click("Ask Charlie");
    assert.equal(floor.calls.at(-1).prefill, `Help me plan the ${department.name} team`);
    floor.runtime.close();
  }
});

test("archives open real dated records, original destinations and private saved text", async () => {
  const fixture = viewFixture("archives");
  nodes(fixture.render()).find(node => node.type === "button" && text(node).startsWith("Strategy") && node.props["aria-pressed"] !== undefined).props.onClick();
  let body = fixture.render(); assert.match(text(body), /Saved thesis/); assert.ok(nodes(body).some(node => node.type === "time" && node.props.dateTime === "2026-10-12"));
  assert.ok(nodes(body).some(node => node.type === "a" && node.props.href === "/v/ven_home/context"));
  await fixture.click("Read saved document"); await flush(); assert.equal(fixture.calls.at(-1).path, "/api/ventures/ven_home/ui-archive/doc_real"); assert.match(text(fixture.render()), /Real knowledge/);
  nodes(fixture.render()).find(node => node.type === "button" && text(node).startsWith("Operations") && node.props["aria-pressed"] !== undefined).props.onClick(); assert.match(text(fixture.render()), /No saved records/);
});

test("Tuesday Archives clicks open the server-loaded folder view and return to chat without recommendations", async () => {
  const fixture = dataFixture(true);
  fixture.setHandler(path => ({ ok: true, data: path.endsWith("/ui-archive") ? { venture_id: "ven_home", open_work: 1, items: [
    { id: "doc_real", folder: "knowledge", title: "Saved customer interviews", href: "/v/ven_home/context", created_at: "2026-10-12", document: true },
  ] } : fixture.respond(path) }));
  fixture.render(); await flush(); await flush();
  const state = fixture.render();
  const runtime = hooks(), events: any[] = [], paths: string[] = [], listeners = new Map();
  let search = new URLSearchParams();
  const globals = { URL, HTMLElement: class {}, requestAnimationFrame() {}, CustomEvent: class { type: string; detail: any; constructor(type: string, options?: any) { this.type = type; this.detail = options?.detail; } },
    window: { location: { href: "https://app.supymem.com/v/ven_home", origin: "https://app.supymem.com" },
      addEventListener: (name: string, callback: any) => listeners.set(name, callback), removeEventListener: (name: string) => listeners.delete(name),
      dispatchEvent: (event: any) => events.push(event) },
    document: { documentElement: { dataset: {} }, querySelector: () => ({ dataset: { ventureId: "ven_home" } }) } };
  const routes = compile("./chatRouteAction.ts", {}, globals);
  const nativeRouter = { push: (href: string) => { paths.push(href); search = new URL(href, globals.window.location.href).searchParams; } };
  const office = compile("../components/office/CompanyOffice.tsx", { react: runtime.react, "react/jsx-runtime": { jsx, jsxs: jsx },
    "@/lib/chatRouteAction": routes, "next/navigation": { useRouter: () => nativeRouter, useSearchParams: () => search },
    "@/lib/companyHome": { useCompanyHomeData: () => state }, "@/lib/founderRoutes": { founderRouteLinks: () => [{ href: "/v/ven_home", label: "Chat" }] },
    "@/lib/chatVisibility": { chatFirstHidden }, "@/lib/chatConversations": { historyGroups },
    "./CompanyHome": { HOME_TABS: { home: "Home", archives: "Tuesday Archives", toolkit: "Toolkit", departments: "Departments" },
      CompanyHomeContext: { Provider: "provider" }, CompanyHome: "home" } }, globals);
  const render = () => runtime.render(() => office.CompanyOffice({ ventureId: "ven_home", pathname: "/v/ven_home", chatAvailable: true, onSettings() {}, children: "Chat" }));
  const row = nodes(render()).find(node => node.type === "button" && text(node) === "Tuesday Archives");
  assert.ok(row);
  for (let click = 0; click < 3; click++) row.props.onClick();
  assert.deepEqual(paths, Array(3).fill("/v/ven_home?office=archives"));
  assert.equal(events.filter(event => event.type === routes.HOME_NAVIGATION_EVENT).length, 0);
  assert.ok(nodes(render()).some(node => node.type === "home" && node.props.tab === "archives"));
  const shell = read("../components/layout/AppShell.tsx");
  const officeTabExpression = shell.match(/  const officeTab = ([^;]+);/)![1];
  assert.equal(vm.runInNewContext(officeTabExpression, { founderUI: "home", isVentureOverview: true, searchParams: search }), true);
  assert.equal(fixture.calls.filter(call => call.path?.endsWith("/ui-archive")).length, 1);
  const archive = viewFixture("archives", state.data);
  const body = archive.render();
  assert.ok(nodes(body).some(node => node.props["data-company-home"] === "archives"));
  assert.match(text(body), /Tuesday Archives.*Folders.*Saved customer interviews/);
  await archive.click("Read saved document"); await flush();
  assert.equal(archive.calls.at(-1).path, "/api/ventures/ven_home/ui-archive/doc_real");
  assert.match(text(archive.render()), /Real knowledge/);
  const back = nodes(body).find(node => node.type === "a" && text(node) === "Back to chat");
  assert.ok(back); assert.equal(back.props.href, "/v/ven_home"); assert.equal(back.props["data-chat-route-native"], true);
  const links = compile("../components/chat/ChatLink.tsx", { "@/lib/chatRouteAction": routes, "next/link": { default: "a" }, "react/jsx-runtime": { jsx, jsxs: jsx } }, globals);
  const anchor = links.default(back.props);
  const target = { hasAttribute: (name: string) => Boolean(anchor.props[name]), closest() { return this; }, getAttribute: () => back.props.href };
  const event = { target, currentTarget: target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
  anchor.props.onClick(event); links.captureChatLink(event);
  assert.equal(event.defaultPrevented, false);
  nativeRouter.push(back.props.href);
  assert.ok(nodes(render()).some(node => node.type === "home" && node.props.tab === "home"));
  assert.equal(vm.runInNewContext(officeTabExpression, { founderUI: "home", isVentureOverview: true, searchParams: search }), false);
  assert.equal(events.filter(event => event.type === routes.HOME_NAVIGATION_EVENT).length, 0);
  assert.ok(!nodes(render()).some(node => node.type === "button" && text(node) === "Refer a friend"));
  for (const label of ["Idea", "Leads", "Connections", "LinkedIn", "All ventures", "Settings"]) {
    const button = nodes(render()).find(node => node.type === "button" && text(node) === label);
    assert.ok(button, label);
    for (let click = 0; click < 3; click++) button.props.onClick();
  }
  listeners.get("keydown")!({ key: "k", metaKey: true, preventDefault() {} });
  const command = nodes(render()).find(node => node.props.role === "option" && text(node).startsWith("Go to Chat"));
  assert.ok(command); command.props.onClick(); assert.equal(paths.at(-1), "/v/ven_home");
  render();
  listeners.get("keydown")!({ key: "h", code: "KeyH", altKey: true, preventDefault() {} });
  assert.equal(paths.at(-1), "/v/ven_home");
  assert.equal(events.filter(event => event.detail?.action === "next_moves").length, 0);
  runtime.close(); archive.runtime.close(); fixture.close();
});

test("Toolkit tiles start scoped OAuth, sync status and real GitHub credential flow", async () => {
  const fixture = viewFixture("toolkit");
  await nodes(fixture.render()).find(node => node.props["aria-label"] === "Gmail: Connect").props.onClick();
  await flush();
  assert.equal(fixture.calls[0].action, "connect"); assert.equal(fixture.calls[0].args[0], "gmail"); assert.equal(fixture.calls[0].args[2], "wsp_home");
  assert.equal(fixture.calls[1].url, "https://connect.composio.dev/link/fixture");
  const syncing = viewFixture("toolkit", { cards: [{ id: "gmail", name: "Gmail", connected: true, ready: true, action: "sync" }] });
  await nodes(syncing.render()).find(node => node.type === "button" && node.props["aria-label"]).props.onClick(); assert.equal(syncing.calls[0].action, "sync");
  const github = viewFixture("toolkit", { cards: [{ id: "github", name: "GitHub", connected: false, action: "connect" }] });
  await nodes(github.render()).find(node => node.props["aria-label"] === "GitHub: Connect").props.onClick();
  let inputs = nodes(github.render()).filter(node => node.type === "input"); inputs[0].props.onChange({ target: { value: "real-org" } });
  inputs = nodes(github.render()).filter(node => node.type === "input"); inputs[1].props.onChange({ target: { value: "test-token" } });
  nodes(github.render()).find(node => node.type === "form").props.onSubmit({ preventDefault() {} }); await flush();
  assert.equal(github.calls[0].path, "/api/workspaces/wsp_home/connections"); assert.equal(JSON.parse(github.calls[0].options.body).provider, "github");
});

test("archive filters use saved status, integration, created dates and departments; absent fields stay disabled", () => {
  const fixture = viewFixture("archives", {archives: [
    {id: "report", folder: "research", title: "Real research", href: "/v/ven_home/research", status: "done", integration: "gmail", created_at: new Date().toISOString(), agent_id: "agent_real"},
    {id: "old", folder: "tasks", title: "Old task", href: "/v/ven_home/tasks", status: "open", created_at: "2020-01-01T00:00:00Z"},
  ]});
  const filter = (prefix: string) => nodes(fixture.render()).find(node => node.type === "button" && text(node).startsWith(prefix));
  assert.equal(filter("Integrations:").props.disabled, false);
  filter("Integrations:").props.onClick();
  assert.match(text(fixture.render()), /Real research/); assert.doesNotMatch(text(fixture.render()), /Old task/);
  filter("Integrations:").props.onClick(); filter("Date:").props.onClick();
  assert.doesNotMatch(text(fixture.render()), /Old task/);
  filter("Status:").props.onClick(); assert.match(text(fixture.render()), /Real research/);
  filter("Tags:").props.onClick(); assert.match(text(fixture.render()), /Tags: Research/);
  const portrait = nodes(fixture.render()).find(node => node.type === "portrait" && node.props.size === 24);
  assert.equal(portrait.props.seed, "agent_real");
  const empty = viewFixture("archives", {archives: []});
  assert.equal(nodes(empty.render()).filter(node => node.type === "button" && /^(Status|Integrations|Date|Tags):/.test(text(node))).filter(node => node.props.disabled).length, 4);
});

test("Home leaves analytics offscreen while founding data stays available to other tabs", async () => {
  const fixture = dataFixture(); fixture.render(); await flush(); await flush();
  assert.equal(fixture.render().data.createdAt, fixture.respond("/api/ventures/ven_home").created_at);
  assert.doesNotMatch(text(viewFixture("home", {createdAt: fixture.render().data.createdAt}).render()), /Founded|Milestones|Page views/);
  fixture.close();
});

test("archive grouping uses real owner roles and distinguishes customers from outreach, never inventing a portrait", () => {
  const imports = dataFixture().module;
  assert.equal(imports.archiveDepartment({folder: "outreach", kind: "gtm_contacts"}, []), "Customers & Support");
  assert.equal(imports.archiveDepartment({folder: "website", agent_role: "researcher"}, [{id: "one", role: "researcher"}]), "Research");
  assert.equal(imports.archiveDepartment({folder: "website", agent_role: "researcher"}, [{id: "one", role: "researcher"}, {id: "two", role: "researcher"}]), "Product & Engineering");
  const archive = viewFixture("archives", {archives: [{id: "unknown", folder: "knowledge", title: "Unattributed saved note", href: "/v/ven_home/context"}]});
  assert.ok(!nodes(archive.render()).some(node => node.type === "portrait"));
});

test("new UI code contains no demo data and CSS parses with the PR folder/tab shapes", () => {
  for (const path of ["./companyHome.ts", "../components/office/CompanyHome.tsx", "../components/office/CompanyOffice.tsx"]) assert.doesNotMatch(read(path), /TEMP_BYPASS_AUTH|Todoist|Founded Day 0|Talking to prospects|Building and testing|Status: All|Date: Any/);
  assert.match(read("./companyHome.ts"), /supymem\.sidebar-pins\.\$\{candidate\.subject\}\.\$\{ventureId\}/);
  assert.match(read("./companyHome.ts"), /!pinsReady\.current/);
  const css = read("../components/office/CompanyOffice.module.css");
  assert.equal((css.match(/\{/g) || []).length, (css.match(/\}/g) || []).length);
  assert.match(css, /\.pocketFront/); assert.match(css, /grid-template-columns: repeat\(3/);
});

test("PR #1248 sidebar rows, chat history and palette use real actions; hidden pages stay out of the palette", async () => {
  const runtime = hooks(), calls: any[] = [], listeners = new Map<string, (event: any) => void>();
  const data = {name: "Real", agents: [{id: "agent_real", role: "researcher"}], pins: ["Research"], errors: []};
  const owner = {epoch: 1, subject: "usr_home"};
  const imports = dataFixture().module;
  const dataset: Record<string, string> = {};
  const module = compile("../components/office/CompanyOffice.tsx", {react: runtime.react, "react/jsx-runtime": {jsx, jsxs: jsx}, "next/link": {default: "a"}, "next/navigation": {useRouter: () => ({push: (href: string) => calls.push({href})}), useSearchParams: () => new URLSearchParams()}, "@/lib/founderRoutes": {founderRouteLinks: () => [{href: "/gtm?venture=ven_home", label: "Find customers"}, {href: "/new", label: "New venture"}]}, "@/lib/chatVisibility": {chatFirstHidden}, "@/lib/chatConversations": {historyGroups}, "@/lib/companyHome": {...imports, useCompanyHomeData: () => ({data, owner, pin: (name: string, pinned: boolean) => calls.push({name, pinned}), pinBusy: false, pinNote: ""})}, "@/lib/chatPrefill": {prefillChat: (value: string) => calls.push({prefill: value})}, "./CompanyHome": {HOME_TABS: {home: "Home", archives: "Tuesday Archives", toolkit: "Toolkit", departments: "Departments"}, CompanyHomeContext: {Provider: "provider"}, CompanyHome: "home"}}, {requestAnimationFrame() {}, HTMLElement: class {}, CustomEvent: class { type: string; detail: any; constructor(type: string, init?: any) { this.type = type; this.detail = init?.detail; } }, window: {addEventListener: (name: string, callback: (event: any) => void) => listeners.set(name, callback), removeEventListener: (name: string) => listeners.delete(name), dispatchEvent: (event: any) => calls.push({event: event.type, detail: event.detail}), setTimeout: (callback: () => void) => callback(), getSelection: () => null}, document: {addEventListener() {}, removeEventListener() {}, documentElement: {dataset, classList: {add() {}, remove() {}}}, querySelector: () => ({getBoundingClientRect: () => ({left: 0, right: 420, top: 0, bottom: 600})})}});
  const render = () => runtime.render(() => module.CompanyOffice({ventureId: "ven_home", pathname: "/v/ven_home", onSettings: () => calls.push({settings: true}), chatAvailable: true, children: "Page"}));
  let tree = render();
  assert.equal(dataset.officeTab, "home");
  assert.equal(calls.find(call => call.event === "supymem:conversation")?.detail.title, "Real");
  const row = (label: string) => nodes(tree).find(node => node.type === "button" && node.props.className === "cos-row" && text(node).endsWith(label));
  assert.equal(row("Company"), undefined);
  assert.equal(row("Toolkits"), undefined);
  for (const [label, href] of [["Tuesday Archives", "/v/ven_home?office=archives"], ["Idea", "/v/ven_home/idea/current"], ["LinkedIn", "/v/ven_home/linkedin"]]) {
    row(label).props.onClick(); assert.deepEqual(calls.at(-1), {href}, label);
  }
  assert.equal(row("New chat").props.disabled, true);
  listeners.get("supymem:chat-history")!({detail: {ventureId: "ven_home", items: [], current: "", saved: true,
    newConversation: async () => {calls.push({newConversation: true}); return true;}}}); tree = render();
  await row("New chat").props.onClick();
  assert.ok(calls.some(call => call.newConversation)); assert.equal(calls.at(-1).event, "supymem-chat-talk");
  row("Settings").props.onClick(); assert.deepEqual(calls.at(-1), {settings: true});
  listeners.get("supymem:open-settings")!({}); assert.deepEqual(calls.at(-1), {settings: true});
  assert.ok(nodes(tree).some(node => node.props.className === "cos-empty"));
  listeners.get("supymem:chat-history")!({detail: {ventureId: "ven_home", items: [{id: "c1", text: "Find   gyms in Pune", at: new Date().toISOString()}], current: "c1"}}); tree = render();
  assert.ok(nodes(tree).some(node => node.props.className === "cos-day" && text(node) === "Today"));
  const saved = nodes(tree).find(node => node.props.className === "cos-chat");
  assert.equal(text(saved), "Find gyms in Pune"); assert.equal(saved.props["aria-current"], "true");
  saved.props.onClick(); assert.deepEqual([calls.at(-1).event, calls.at(-1).detail.id], ["supymem:chat-jump", "c1"]);
  listeners.get("keydown")!({key: "k", metaKey: true, preventDefault() {}}); tree = render();
  assert.ok(nodes(tree).some(node => node.props.role === "listbox"));
  assert.ok(!nodes(tree).some(node => node.props.role === "option" && text(node) === "Go to Find customers"));
  const mouseCommand = nodes(tree).find(node => node.props.role === "option" && text(node) === "Go to New venture");
  let prevented = false;
  mouseCommand.props.onMouseDown?.({preventDefault() { prevented = true; }});
  assert.equal(prevented, true, "D23 mouse activation keeps input focus until the click runs");
  mouseCommand.props.onClick(); assert.deepEqual(calls.at(-1), {href: "/new"});
  assert.ok(!nodes(render()).some(node => node.props.role === "listbox"));
  listeners.get("keydown")!({key: "k", metaKey: true, preventDefault() {}}); tree = render();
  nodes(tree).find(node => node.type === "input").props.onChange({target: {value: "My real question"}}); tree = render();
  nodes(tree).find(node => node.type === "button" && text(node) === "Ask Charlie: My real question").props.onClick(); assert.deepEqual(calls.at(-1), {prefill: "My real question"});
  assert.ok(!nodes(render()).some(node => node.props.role === "listbox"));
  const updates: {id: string; patch: Record<string, string>}[] = [];
  let succeeds = true;
  listeners.get("supymem:chat-history")!({detail: {ventureId: "ven_home", saved: true, current: "c1",
    items: [{id: "c1", text: "Find gyms", at: new Date().toISOString(), folder: "Existing"}],
    update: async (id: string, patch: Record<string, string>) => {updates.push({id, patch}); return succeeds;}}});
  const startEdit = (label: string) => nodes(render()).find(node => node.type === "button" && text(node) === label).props.onClick({currentTarget: {closest: () => null}});
  const input = () => nodes(render()).find(node => node.type === "input");
  const submit = () => nodes(render()).find(node => node.type === "form").props.onSubmit({preventDefault() {}});
  startEdit("New folder...");
  assert.equal(input().props["aria-label"], "Folder name"); assert.equal(input().props.autoFocus, true); assert.equal(input().props.required, false);
  assert.equal(input().props.value, "Existing");
  input().props.onChange({target: {value: "  Plans  "}}); await submit();
  assert.deepEqual(JSON.parse(JSON.stringify(updates.at(-1))), {id: "c1", patch: {folder: "Plans"}});
  assert.equal(input(), undefined);
  startEdit("New folder..."); input().props.onChange({target: {value: "   "}}); await submit();
  assert.deepEqual(JSON.parse(JSON.stringify(updates.at(-1))), {id: "c1", patch: {folder: ""}});
  startEdit("New folder..."); input().props.onChange({target: {value: "Cancel me"}});
  input().props.onKeyDown({key: "Escape", preventDefault() {}});
  assert.equal(input(), undefined); assert.equal(updates.length, 2);
  succeeds = false; startEdit("New folder..."); input().props.onChange({target: {value: "Keep draft"}}); await submit();
  assert.equal(input().props.value, "Keep draft"); input().props.onKeyDown({key: "Escape", preventDefault() {}});
  succeeds = true; startEdit("Rename"); assert.equal(input().props.required, true); assert.equal(input().props.maxLength, 120);
  input().props.onChange({target: {value: "  Renamed   chat  "}}); await submit();
  assert.deepEqual(JSON.parse(JSON.stringify(updates.at(-1))), {id: "c1", patch: {title: "Renamed chat"}});
  assert.doesNotMatch(read("../components/office/CompanyOffice.tsx"), /window\.(?:prompt|confirm|alert)\s*\(/);
  runtime.close();
});

test("navigation shortcuts require modifiers and leave form-card answers untouched", () => {
  const runtime = hooks(), routes: string[] = [], listeners = new Map<string, (event: any) => void>();
  const imports = dataFixture().module;
  class Element {
    tagName = "BUTTON";
    isContentEditable = false;
    parentElement = null;
    formCard = false;
    closest(selector: string) { return this.formCard && selector.includes("section") ? { querySelector: () => ({ value: "saved answer" }) } : null; }
  }
  const module = compile("../components/office/CompanyOffice.tsx", { react: runtime.react, "react/jsx-runtime": { jsx, jsxs: jsx },
    "next/link": { default: "a" }, "next/navigation": { useRouter: () => ({ push: (href: string) => routes.push(href) }), useSearchParams: () => new URLSearchParams() },
    "@/lib/founderRoutes": { founderRouteLinks: () => [] }, "@/lib/companyHome": { ...imports, useCompanyHomeData: () => ({ data: { agents: [], pins: [], errors: [] }, owner: "founder" }) },
    "./CompanyHome": { HOME_TABS: { home: "Home" }, CompanyHomeContext: { Provider: "provider" } } },
    { HTMLElement: Element, CustomEvent: class {}, document: { documentElement: { dataset: {} } }, window: { addEventListener: (name: string, listener: any) => listeners.set(name, listener), removeEventListener() {}, dispatchEvent() {} } });
  runtime.render(() => module.CompanyOffice({ ventureId: "ven_forms", pathname: "/v/ven_forms/idea", children: "Answer card", onSettings() {} }));
  const target = new Element();
  const press = (key: string, modifiers: Record<string, boolean | string> = {}) => listeners.get("keydown")!({ key, target, preventDefault() {}, ...modifiers });
  for (const key of "high") press(key);
  assert.deepEqual(routes, []);
  target.formCard = true;
  press("˙", { altKey: true, code: "KeyH" });
  assert.deepEqual(routes, []);
  target.formCard = false;
  press("h", { altKey: true });
  assert.deepEqual(routes, ["/v/ven_forms"]);
  press("g", { altKey: true }); press("i");
  assert.equal(routes.length, 1);
  press("i", { altKey: true });
  assert.deepEqual(routes, ["/v/ven_forms", "/v/ven_forms/research"]);
  runtime.close();
});

test("connection helpers bind an explicit company without changing classic operations selection", async () => {
  const calls: any[] = [];
  const module = compile("./opsApi.ts", { "@/lib/client": { api: async (path: string, options: any) => { calls.push({ path, headers: Object.fromEntries(options.headers), method: options.method }); return { ok: true, data: { workspace_id: "wsp_home", connections: {}, order: [] } }; } }, "@/lib/operationsScope": { operationsReadPath: (path: string) => path } }, { Headers });
  module.setOpsWorkspace("wsp_classic", "ven_classic");
  await module.getConnectionsStatus("wsp_home"); await module.connectHubProvider("gmail", {}, "wsp_home"); await module.syncHubProvider("gmail", "wsp_home");
  assert.equal(module.getOpsWorkspace(), "wsp_classic");
  assert.ok(calls.every(call => call.path.endsWith("workspace_id=wsp_home") && call.headers["x-workspace-id"] === "wsp_home"));
  await module.getConnectionsStatus(); assert.match(calls.at(-1).path, /workspace_id=wsp_classic$/);
});
