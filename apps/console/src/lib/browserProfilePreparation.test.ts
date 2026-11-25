import { routeInChat } from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { browserProfileCatalog } from "./browserProfileCatalog.ts";
import { browserExecutionProfileDigest, browserReadOnlyAuthority, browserReadOnlyRequested } from "./browserExecutionProfile.ts";
import { macTaskPrepareFailure } from "./macTaskPrepareFailure.ts";
import { browserProfileWireSupported, BROWSER_PROFILE_WIRE_MESSAGE } from "./browserProfileWireAvailability.ts";

const role = (limit: number) => ({provider: "openai", model: "gpt-6-sol", service_tier: "default", reasoning: "none", max_output_tokens: limit});
const profile = {version: "browser_cuai_text_v1", backend: "cuai", policy_version: 1, roles: {plan: role(700), actor: role(600), verify: role(300)},
  pricing_digest: "a".repeat(64), max_provider_calls: 64, max_request_bytes: 65536, completion_policy: "browser_semantic_review_v1"};
const digest = "aaf39dea79539023f76b1242e9ce355658380b864f484ca5a49fce4999e36925";
const catalog = {ok: true, data: {available: true, execution_profile: profile, execution_profile_digest: digest}};
const savedReview = (ventureId = "ven_a") => ({ok:true,data:{id:"dwo_owned",venture_id:ventureId,review_digest:"d".repeat(64),
  review_path:`/v/${ventureId}/device-work-orders/dwo_owned`,manifest:{general_browser_verification:{execution_profile:profile}}}});
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => {resolve = done;}); return {promise, resolve}; }
const flush = () => new Promise<void>(done => setImmediate(done));
const source = fs.readFileSync(new URL("../components/desktop/BrowserProfilePreparation.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("prepare.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const component = tree.statements.find((n): n is ts.FunctionDeclaration => ts.isFunctionDeclaration(n) && n.name?.text === "BrowserProfilePreparation")!;
const prefix = component.body!.statements.slice(0, component.body!.statements.findIndex(ts.isReturnStatement)).map(n => n.getText(tree)).join("\n");
const code = ts.transpileModule(`globalThis.callbacks=function({ventureId,disabled=false}){${prefix}\nreturn {load,prepare,choose,changeInstruction,ready,listed,selected,instruction,busy,loading,err};};${component.getText(tree).replace(/^export /, "")};globalThis.renderComponent=BrowserProfilePreparation;`,
  {compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText;
function fixture() {
  let ventureId = "ven_a", subject: string | null = "usr_a", epoch = 1, disabled = false, cursor = 0;
  let localWire: unknown = {browser_cuai_text_v1: true}, wireCalls = 0;
  let wireHandler = async (version = "browser_cuai_text_v1", readOnly = false) => {wireCalls++; return browserProfileWireSupported(localWire, version, readOnly);};
  const location = {pathname: "/v/ven_a/settings", assign: (path: string) => navigations.push(path)};
  let handler = async (path: string, options?: any): Promise<any> => options?.method === "POST"
    ? savedReview(ventureId) : catalog;
  const slots: any[] = [], effects: Array<() => void> = [], calls: Array<{path: string; options?: any}> = [], navigations: string[] = [], elements: any[] = [], listeners = new Map<string, Set<() => void>>();
  const context = vm.createContext({routeInChat, browserProfileCatalog, browserExecutionProfileDigest, browserReadOnlyRequested, browserReadOnlyAuthority, macTaskPrepareFailure, isLocalConsole: () => false, UNAVAILABLE: "Configured OpenAI browser models are unavailable",
    browserProfileWireAvailable: (version?: string, readOnly?: boolean) => wireHandler(version, readOnly), BROWSER_PROFILE_WIRE_MESSAGE,
    verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
    api: async (path: string, options?: any) => {calls.push({path,options}); return handler(path,options);},
    useRef: (initial: any) => {const i = cursor++; slots[i] ??= {current: initial}; return slots[i];},
    useState: (initial: any) => {const i = cursor++; slots[i] ??= {value: initial}; return [slots[i].value, (v: any) => {slots[i].value = typeof v === "function" ? v(slots[i].value) : v;}];},
    useEffect: (effect: () => (() => void) | undefined, deps: any[]) => {const i = cursor++; const old = slots[i]; if (!old || deps.some((d,j) => !Object.is(d,old.deps[j]))) {slots[i] = {deps}; effects.push(() => {old?.cleanup?.(); slots[i].cleanup = effect();});}},
    React: {createElement: (type: any, props: any, ...children: any[]) => {const node = {type,props,children}; elements.push(node); return node;}},
    BrowserExecutionProfileReview: () => null, ErrorNotice: () => null,
    window: {location,
      addEventListener: (name: string, fn: () => void) => {const set = listeners.get(name) || new Set(); set.add(fn); listeners.set(name,set);},
      removeEventListener: (name: string, fn: () => void) => listeners.get(name)?.delete(fn)},
  });
  vm.runInContext(code, context);
  const render = () => {cursor = 0; return context.callbacks({ventureId,disabled});};
  const commit = () => {while (effects.length) effects.shift()!();};
  return {calls,navigations,render,commit, setHandler: (next: typeof handler) => {handler = next;},
    setWire: (value: unknown) => {localWire = value;}, setWireHandler: (next: typeof wireHandler) => {wireHandler = next;}, wireCalls: () => wireCalls,
    route: (next: string) => {ventureId = next;location.pathname=`/v/${next}/settings`;}, liveRoute: (next: string) => {location.pathname=next;}, disable: () => {disabled = true;},
    retire: (next: string | null, notify = false) => {subject = next; epoch++; if (notify) listeners.get("supymem-session-changed")?.forEach(fn => fn());},
    mount: async () => {render();commit();for(let i=0;i<30 && render().loading;i++)await new Promise(done=>setTimeout(done,1));return render();},
    selected: async () => {await render().load();await flush();render().choose(true);render().changeInstruction("Go to https://fixture.invalid/setup and compare its plan limits.");return render();},
    nodes: () => {cursor = 0;elements.length = 0; context.renderComponent({ventureId,disabled});return elements;},
  };
}

test("actual V7 read-only preparation requires compiled mode support and exact returned authority", async () => {
  const goal = "Open https://trail-notebook.example/ and report the main headline. Read only.";
  for (const valid of [false, true]) {
    const f = fixture(); await f.mount(); await f.selected();
    f.render().changeInstruction(goal);
    await f.render().prepare();
    assert.equal(f.calls.filter(call => call.options?.method === "POST").length, 0);
    assert.match(f.render().err, /Update the Mac app/);
    f.setWire({browser_cuai_text_v1: true, browser_cuai_read_only_v1: true});
    await f.selected();
    f.render().changeInstruction(goal);
    f.setHandler(async (_path, options) => options?.method === "POST" ? {ok: true, data: {
      ...savedReview().data, manifest: {actions: valid ? ["browser.read"] : ["browser.read", "browser.interact"],
        general_browser_verification: {execution_profile: profile, allow_type: false, fill_values: {}}},
    }} : catalog);
    await f.render().prepare();
    assert.equal(f.calls.filter(call => call.options?.method === "POST").length, 1);
    assert.equal(f.navigations.length, valid ? 1 : 0);
    if (!valid) assert.match(f.render().err, /did not confirm read-only/);
  }
});

test("old or unknown local native wire suppresses the profile choice before catalog or preparation", async () => {
  for (const value of [null, {}, {available: true}, {browser_cuai_text_v1: false}, {browser_cuai_text_v1: "true"}]) {
    const f = fixture(); f.setWire(value); const view = await f.mount();
    assert.equal(view.ready, false); assert.equal(f.calls.length, 0); assert.equal(f.wireCalls(), 1);
    assert.match(view.err, /Update the Mac app/); assert.equal(f.nodes().some(node => node.type === "input"), false);
    await view.prepare(); assert.equal(f.calls.length, 0); assert.deepEqual(f.navigations, []);
  }
});

test("settings browser card is expanded, uses plain copy and examples fill its goal", async () => {
  const f = fixture(); await f.mount();
  assert.equal(f.nodes().some(node => node.type === "details"), false);
  const example = f.nodes().find(node => node.type === "button" && node.children?.some((child: unknown) => typeof child === "string" && child.startsWith("Visit https://stripe.com")));
  assert.ok(example); example.props.onClick();
  assert.match(f.render().instruction, /^Visit https:\/\/stripe.com\/pricing/);
  assert.match(source, /You review every task before it runs/);
  assert.doesNotMatch(source.slice(source.indexOf("  return <section")), /OpenAI|model/);
});

test("local wire is checked again immediately before saving a selected profile", async () => {
  const f = fixture(); await f.mount(); const view = await f.selected(); f.setWire(null);
  await view.prepare(); assert.equal(f.calls.filter(call => call.options?.method === "POST").length, 0);
  assert.equal(f.render().selected, false); assert.match(f.render().err, /Update the Mac app/);
});

test("delayed local wire replies cannot display or save a retired owner profile", async () => {
  const f = fixture(), pending = deferred<boolean>(); f.setWireHandler(async () => pending.promise);
  f.render(); f.commit(); f.retire("usr_b"); f.render(); pending.resolve(true); await flush();
  assert.equal(f.render().ready, false); assert.equal(f.calls.length, 0);
  const b = fixture(); await b.mount(); const selected = await b.selected(), fresh = deferred<boolean>();
  b.setWireHandler(async () => fresh.promise); const save = selected.prepare(); await flush();
  b.retire("usr_b"); fresh.resolve(true); await save;
  assert.equal(b.calls.filter(call => call.options?.method === "POST").length, 0); assert.deepEqual(b.navigations, []);
});

test("catalog choice requires closed exact profile and digest; old or unavailable catalogs grant nothing", async () => {
  assert.equal((await browserProfileCatalog(catalog))?.digest, digest);
  for (const item of [{ok: false}, {ok: true, data: {available: true}}, {...catalog, data: {...catalog.data, available: "true"}},
    {...catalog, data: {...catalog.data, execution_profile_digest: "b".repeat(64)}},
    {...catalog, data: {...catalog.data, execution_profile: {...profile, extra: true}}}]) assert.equal(await browserProfileCatalog(item), null);
});
test("mount and fresh catalog never automatically select or prepare a browser task", async () => {
  const f = fixture(), view = await f.mount(); assert.equal(view.ready,true); assert.equal(view.selected,false);
  await view.prepare(); assert.equal(f.calls.filter(c => c.options?.method === "POST").length,0); assert.deepEqual(f.navigations,[]);
  const checkbox = f.nodes().find(n => n.type === "input"); assert.equal(checkbox.props.checked,false);
});
test("explicit user choice sends exact whole goal and only saved profile digest then opens separate review", async () => {
  const f = fixture();await f.mount();const view = await f.selected();await view.prepare();
  const posts = f.calls.filter(c => c.options?.method === "POST"); assert.equal(posts.length,1);
  assert.equal(posts[0].path,"/api/ventures/ven_a/device-work-orders/from-chat");
  assert.deepEqual(JSON.parse(posts[0].options.body), {instruction: "Go to https://fixture.invalid/setup and compare its plan limits.",browser_execution_profile_digest:digest});
  assert.deepEqual(f.navigations,["/v/ven_a/device-work-orders/dwo_owned"]);
  assert.equal(f.calls.some(c => /approve|claim|start|effects|desktop-pause/.test(c.path)),false);
});

test("v2 catalog and fresh preparation require the exact compiled policy marker", async () => {
  const p={...profile,version:"browser_cuai_text_v2",policy_version:2};
  const d=await browserExecutionProfileDigest(p);
  const fresh={ok:true,data:{available:true,execution_profile:p,execution_profile_digest:d}};
  const f=fixture();f.setHandler(async()=>fresh);await f.mount();
  assert.equal(f.render().ready,false);assert.match(f.render().err,/Update the Mac app/);
  f.setWire({browser_cuai_text_v1:true,browser_cuai_text_v2:true});
  const ready=await f.selected();assert.equal(ready.ready,true);
  f.setWire({browser_cuai_text_v1:true});await ready.prepare();
  assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,0);
  assert.equal(f.render().selected,false);
});
test("Settings preparation displays a specific code-only refusal without approving, starting or navigating", async () => {
  for (const [code, expected] of [
    ["device_terminal_task_unavailable", /open approved plan task/],
    ["device_browser_ai_request_invalid", /exact HTTPS websites/],
    ["device_browser_ai_provider_or_usage_unconfirmed", /do not repeat/],
  ] as const) {
    const f = fixture(); await f.mount(); const view = await f.selected();
    f.setHandler(async (_path, options) => options?.method === "POST"
      ? {ok: false, status: 409, data: {detail: {code}}} : catalog);
    await view.prepare();
    assert.match(f.render().err, expected);
    assert.doesNotMatch(f.render().err, /workflow state changed/);
    assert.equal(f.nodes().find(node => node.props?.source === "browser-profile-preparation")?.props.error, f.render().err);
    assert.equal(f.calls.filter(c => c.options?.method === "POST").length, 1);
    assert.equal(f.calls.some(c => /approve|claim|start|effects/.test(c.path)), false);
    assert.deepEqual(f.navigations, []);
  }
});
test("a delayed refusal cannot show an old venture's error after live route retirement", async () => {
  const f = fixture(); await f.mount(); const view = await f.selected(), response = deferred<any>();
  f.setHandler(async (_path, options) => options?.method === "POST" ? response.promise : catalog);
  const preparing = view.prepare(); await flush(); f.liveRoute("/v/ven_b/settings");
  response.resolve({ok: false, status: 409, data: {detail: {code: "device_terminal_task_unavailable"}}});
  await preparing;
  assert.equal(f.render().err, ""); assert.equal(f.render().ready, false);
  assert.equal(f.nodes().find(node => node.props?.source === "browser-profile-preparation")?.props.error, "");
  assert.deepEqual(f.navigations, []);
});

test("v2 catalog and fresh preparation require the exact compiled policy marker", async () => {
  const p={...profile,version:"browser_cuai_text_v2",policy_version:2};
  const d=await browserExecutionProfileDigest(p);
  const fresh={ok:true,data:{available:true,execution_profile:p,execution_profile_digest:d}};
  const f=fixture();f.setHandler(async()=>fresh);await f.mount();
  assert.equal(f.render().ready,false);assert.match(f.render().err,/Update the Mac app/);
  f.setWire({browser_cuai_text_v1:true,browser_cuai_text_v2:true});
  const ready=await f.selected();assert.equal(ready.ready,true);
  f.setWire({browser_cuai_text_v1:true});await ready.prepare();
  assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,0);
  assert.equal(f.render().selected,false);
});
test("changed pricing or unavailable fresh profile refuses preparation without fallback", async () => {
  for (const fresh of [{ok:false}, {...catalog,data:{...catalog.data,available:false}},
    {...catalog,data:{...catalog.data,execution_profile_digest:"b".repeat(64)}}]) {
    const f=fixture();await f.mount();const view=await f.selected();f.setHandler(async()=>fresh);await view.prepare();
    assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,0);assert.equal(f.render().selected,false);assert.match(f.render().err,/no replacement model/);
  }
});
test("actual selected-profile refresh recomputes digest after a valid pricing change", async () => {
  const f=fixture();await f.mount();const view=await f.selected();const changed={...profile,pricing_digest:"b".repeat(64)};
  f.setHandler(async()=>({ok:true,data:{available:true,execution_profile:changed,execution_profile_digest:await browserExecutionProfileDigest(changed)}}));
  await view.prepare();assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,0);
});
test("retained prepare/choice/refresh callbacks refuse route or account retirement and same-account ABA",async()=>{
  for(const change of [(f:ReturnType<typeof fixture>)=>f.route("ven_b"),(f:ReturnType<typeof fixture>)=>f.retire("usr_b"),(f:ReturnType<typeof fixture>)=>{f.retire("usr_other");f.retire("usr_a");}]){
    const f=fixture();await f.mount();const old=await f.selected();change(f);f.render();const n=f.calls.length;
    old.choose(true);old.changeInstruction("retired goal");await old.load();await old.prepare();assert.equal(f.calls.length,n);assert.equal(f.render().ready,false);
  }
});
test("retirement during catalog read cannot show a previous actor's profile",async()=>{
  const f=fixture(),p=deferred<any>();f.setHandler(async()=>p.promise);const old=f.render();f.commit();f.retire("usr_b");f.render();p.resolve(catalog);await flush();
  assert.equal(f.render().ready,false);old.choose(true);await old.prepare();assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,0);
});
test("retirement during digest preflight prevents POST before rerender",async()=>{
  const f=fixture();await f.mount();const view=await f.selected();f.setHandler(async()=>{queueMicrotask(()=>f.retire("usr_b"));return catalog;});
  await view.prepare();assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,0);assert.deepEqual(f.navigations,[]);
});
test("live route change before rerender retires retained reads and preparation",async()=>{
  for(const pathname of ["/v/ven_b/settings", "/company/connections", "/v/%ZZ/settings"]){
    const f=fixture();await f.mount();const view=await f.selected();f.liveRoute(pathname);const n=f.calls.length;
    view.choose(true);view.changeInstruction("Retired route goal");await view.load();await view.prepare();
    assert.equal(f.calls.length,n);assert.equal(f.render().ready,false);assert.deepEqual(f.navigations,[]);
  }
});
test("in-flight catalog and saved review responses cannot restore or navigate after live route retirement",async()=>{
  const f=fixture();await f.mount();const view=await f.selected(),p=deferred<any>();f.setHandler(async()=>p.promise);
  const prepare=view.prepare();f.liveRoute("/v/ven_b/settings");p.resolve(catalog);await prepare;
  assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,0);assert.equal(f.render().ready,false);
  const b=fixture();await b.mount();const ready=await b.selected(),response=deferred<any>();
  b.setHandler(async(_path,options)=>options?.method==="POST"?response.promise:catalog);
  const send=ready.prepare();await flush();b.liveRoute("/v/ven_b/settings");response.resolve(savedReview());await send;
  assert.deepEqual(b.navigations,[]);assert.equal(b.render().ready,false);
});
test("parallel clicks dedupe, catalog refresh cannot reopen an in-flight preparation",async()=>{
  const f=fixture();await f.mount();const view=await f.selected(),p=deferred<any>();f.setHandler(async(_path,options)=>options?.method==="POST"?p.promise:catalog);
  const a=view.prepare();await flush();const n=f.calls.length;await view.prepare();await view.load();assert.equal(f.calls.length,n);
  p.resolve(savedReview());await a;assert.equal(f.calls.filter(c=>c.options?.method==="POST").length,1);
});
test("late old preparation response cannot navigate or report into a new owner",async()=>{
  const f=fixture();await f.mount();const view=await f.selected(),p=deferred<any>();f.setHandler(async(_path,options)=>options?.method==="POST"?p.promise:catalog);
  const a=view.prepare();await flush();f.retire("usr_b",true);f.render();p.resolve(savedReview());await a;
  assert.deepEqual(f.navigations,[]);assert.equal(f.render().instruction,"");assert.equal(f.render().selected,false);
});
test("unknown or foreign success body and transport errors do not claim or open a saved review",async()=>{
  for(const result of [{ok:true,data:{}},{ok:true,data:{review_path:"/v/ven_b/device-work-orders/dwo_foreign"}},{ok:true,data:{review_path:"/v/ven_a/device-work-orders/dwo_owned?start=1"}},null]){
    const f=fixture();await f.mount();const view=await f.selected();f.setHandler(async(_path,options)=>{if(!options)return catalog;if(result===null)throw new Error("owned transport");return result;});await view.prepare();
    assert.deepEqual(f.navigations,[]);assert.match(f.render().err,/unconfirmed|did not confirm/);
  }
});
test("missing verified actor and busy parent cannot prepare or borrow an account",async()=>{
  const f=fixture();f.retire(null);await f.mount();assert.equal(f.calls.length,0);assert.equal(f.render().ready,false);
  const b=fixture();await b.mount();const view=await b.selected();b.disable();b.render();const n=b.calls.length;await view.prepare();await b.render().prepare();assert.equal(b.calls.length,n);
});
test("same-venture saved review with a dropped or changed model profile is not substituted",async()=>{
  for(const value of [null,{...profile,pricing_digest:"b".repeat(64)}]){
    const f=fixture();await f.mount();const view=await f.selected();f.setHandler(async(_path,options)=>options?.method==="POST"
      ? {...savedReview(),data:{...savedReview().data,manifest:{general_browser_verification:{execution_profile:value}}}}:catalog);
    await view.prepare();assert.deepEqual(f.navigations,[]);assert.match(f.render().err,/did not confirm the selected browser profile/);
  }
});
test("normal installed Mac entry exposes explicit configured profile review without changing legacy send",()=>{
  const entry=fs.readFileSync(new URL("../components/desktop/AgentSessionPane.tsx",import.meta.url),"utf8");
  assert.match(entry,/<BrowserProfilePreparation ventureId=\{ventureId\} disabled=\{busy \|\| phase === "running"\}/);
  assert.ok(entry.includes("void startOrSend(input)"));assert.match(source,/then run your task/);
});

test("review shows remaining time alongside hard expiry and fresh-request action", () => {
  const page = fs.readFileSync(new URL("../app/v/[ventureId]/device-work-orders/[orderId]/page.tsx", import.meta.url), "utf8");
  assert.match(page, /Time left to review/);
  assert.match(page, /reviewMinutes % 60/);
  assert.match(page, /Prepare a fresh request/);
  assert.match(page, /now >= expires/);
});
