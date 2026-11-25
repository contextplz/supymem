import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { scopedSuggestions, suggestionsPath } from "./suggestions.ts";

const source = fs.readFileSync(new URL("../app/suggestions/overlay/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("overlay.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const component = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "SuggestionsOverlayPage")!;
assert.ok(component?.body, "exercise the actual overlay callbacks and commit effects");
const helpers = tree.statements.filter(node => ts.isFunctionDeclaration(node) && ["sameCustody", "overlayInvoke"].includes(node.name?.text || "")).map(node => node.getText(tree)).join("\n");
const body = component.body.statements.filter(node => !ts.isReturnStatement(node)
  && !(ts.isIfStatement(node) && ts.isReturnStatement(node.thenStatement))).map(node => node.getText(tree)).join("\n");
const ui = component.body.statements.filter(node => ts.isReturnStatement(node)
  || (ts.isIfStatement(node) && ts.isReturnStatement(node.thenStatement))).map(node => node.getText(tree)).join("\n");
const code = ts.transpileModule(`${helpers}\nglobalThis.component=function(){${body}\nreturn {load,decide,details,later,collapse,setExpanded,expanded,snoozed,visible,retire,items,loaded,loadedOwner,busy,errors,confirmed,loadError,shown,owner,presentation,root,ui:()=>{${ui}}};};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  fileName: "overlay.tsx",
}).outputText;
type Custody = { subject: string; device_id: string; auth_epoch: number; presentation_id: string; venture_id: string | null; viewport_width: number };
const A: Custody = { subject: "usr_a", device_id: "device_a", auth_epoch: 7, presentation_id: "presentation_a", venture_id: "ven_a", viewport_width: 1000 };
const B: Custody = { subject: "usr_b", device_id: "device_b", auth_epoch: 8, presentation_id: "presentation_b", venture_id: "ven_b", viewport_width: 1000 };
const card = { id: "sug_a", status: "new", venture_id: "ven_a", title: "A private task" };
const result = (items = [card]) => ({ status: 200, data: { items } });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
const flush = () => new Promise<void>(done => setImmediate(done));

function fixture(storage = new Map<string, string>()) {
  let current: Custody | null = A, cursor = 0;
  const slots: any[] = [], effects: (() => void)[] = [], listeners = new Map<string, (payload: unknown) => void>();
  const calls: { name: string; args: any }[] = [], stopped: string[] = [];
  let api: (args: any) => Promise<unknown> = async () => result();
  let ready = true;
  let custodyBlocked = false;
  const timers = new Map<number, { callback: () => void; delay: number }>();
  const telemetry: any[] = [];
  let timerId = 0;
  const keyListeners = new Map<string, (event: { key: string }) => void>();
  const focus: string[] = [];
  const same = (args: any) => !!current && args.presentationId === current.presentation_id && args.subject === current.subject
    && args.deviceId === current.device_id && args.authEpoch === current.auth_epoch;
  const context = vm.createContext({
    URLSearchParams, Set, Number, Math, Array, scopedSuggestions, suggestionsPath,
    setTimeout: (callback: () => void, delay: number) => {
      const id = ++timerId;
      if (delay === 500) setImmediate(callback); else timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout: (id: number) => timers.delete(id),
    reportUserError: (event: any) => telemetry.push(event), ErrorNotice: "ErrorNotice",
    suggestionsSnoozed: (subject: string) => Number(storage.get(`supymem-suggestions-later:${subject}`)) > Date.now(),
    snoozeSuggestions: (subject: string) => {
      const until = new Date(); until.setHours(24, 0, 0, 0);
      storage.set(`supymem-suggestions-later:${subject}`, String(until.getTime()));
    },
    React: { createElement: (type: unknown, props: any, ...children: any[]) => ({ type, props, children }) },
    styles: {}, SuggestionCard: "SuggestionCard",
    useState: (initial: unknown) => {
      const index = cursor++; slots[index] ??= { value: initial };
      slots[index].set ??= (value: unknown) => { slots[index].value = typeof value === "function" ? value(slots[index].value) : value; };
      return [slots[index].value, slots[index].set];
    },
    useRef: (initial: unknown) => { const index = cursor++; slots[index] ??= { current: initial }; return slots[index]; },
    useCallback: (callback: unknown, deps: unknown[]) => {
      const index = cursor++, previous = slots[index];
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) slots[index] = { callback, deps };
      return slots[index].callback;
    },
    useEffect: (callback: () => (() => void) | undefined, deps: unknown[]) => {
      const index = cursor++, previous = slots[index];
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) {
        slots[index] = { deps, cleanup: previous?.cleanup };
        effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = callback(); });
      }
    },
    document: { documentElement: { style: {} }, body: { style: {} } },
    window: { location: { search: "?presentation=presentation_a" }, setInterval: () => 1, clearInterval: () => undefined,
      addEventListener: (name: string, callback: (event: { key: string }) => void) => keyListeners.set(name, callback),
      removeEventListener: (name: string) => keyListeners.delete(name) },
    ResizeObserver: class { observe() {} disconnect() {} },
    desktopListen: async (name: string, listener: (payload: unknown) => void) => { listeners.set(name, listener); return () => { listeners.delete(name); stopped.push(name); }; },
    desktopInvokeOrThrow: async (name: string, args: any) => {
      calls.push({ name, args });
      if (name === "suggestions_overlay_custody") return custodyBlocked ? new Promise(() => {}) : current && args.presentationId === current.presentation_id ? { ...current } : null;
      if (name === "suggestions_overlay_request") return same(args) ? api(args) : { status: 403, data: null };
      if (name === "suggestions_overlay_ready") return ready && same(args);
      return true;
    },
  });
  vm.runInContext(code, context);
  function render() { cursor = 0; const view = context.component(); view.root.current = {
    getBoundingClientRect: () => ({ height: 140.2, width: view.expanded ? 400 : 140 }),
    querySelector: () => ({ focus: () => focus.push("pill") }),
  }; return view; }
  function commit() { while (effects.length) effects.shift()!(); }
  async function mount() { render(); commit(); await flush(); return render(); }
  return { calls, stopped, storage, focus, render, commit, mount, telemetry,
    blockCustody: (blocked: boolean) => { custodyBlocked = blocked; },
    timeout: () => { for (const [id, timer] of timers) { timers.delete(id); timer.callback(); } },
    setOwner: (owner: Custody | null) => { current = owner; },
    setApi: (handler: typeof api) => { api = handler; }, setReady: (value: boolean) => { ready = value; },
    emit: (name: string) => listeners.get(name)?.({}),
    key: (key: string) => keyListeners.get("keydown")?.({ key }),
    dispose: () => { for (const slot of slots) slot?.cleanup?.(); },
  };
}

test("actual fresh overlay load remains hidden/unshown until its DOM commit and exact native readiness", async () => {
  const f = fixture(), view = await f.mount();
  assert.equal(view.items.length, 1); assert.equal(view.loadedOwner.subject, "usr_a");
  assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_ready").length, 0);
  assert.equal(f.calls.filter(call => call.args?.path === "/api/suggestions/shown").length, 0);
  f.commit(); await flush();
  const ready = f.calls.find(call => call.name === "suggestions_overlay_ready")!;
  assert.deepEqual(JSON.parse(JSON.stringify(ready.args)), { presentationId: A.presentation_id, subject: A.subject, deviceId: A.device_id, authEpoch: A.auth_epoch, height: 141, width: 140 });
  assert.equal(f.calls.filter(call => call.args?.path === "/api/suggestions/shown").length, 0);
  assert.equal(f.calls.filter(call => call.args?.path?.endsWith("/decision")).length, 0, "display is never opt-in execution");
});

test("native readiness refusal never marks loaded cards shown", async () => {
  const f = fixture(); f.setReady(false); await f.mount(); f.commit(); await flush();
  assert.equal(f.calls.filter(call => call.args?.path === "/api/suggestions/shown").length, 0);
});

test("same-account connected/busy completion does not retire a valid delayed fresh load", async () => {
  const f = fixture(), delayed = deferred<unknown>(); f.setApi(async () => delayed.promise);
  await f.mount(); f.emit("supymem-native-auth"); await flush();
  delayed.resolve(result()); await flush(); const view = f.render();
  assert.equal(view.items.length, 1); assert.equal(view.loadedOwner.subject, A.subject);
  assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_request").length, 1, "status observation never replays the pending GET");
  f.commit(); await flush();
  assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_ready").length, 1);
});

test("failed and empty fresh lists do not request native presentation", async () => {
  for (const reply of [{ status: 503, data: null }, result([])]) {
    const f = fixture(); f.setApi(async () => reply); await f.mount(); f.commit(); await flush();
    assert.equal(f.render().items.length, 0);
    assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_ready").length, 0);
  }
});

test("actual native retirement clears private cards, shown IDs, busy/errors and loaded ownership", async () => {
  const f = fixture(); await f.mount(); f.commit(); await flush();
  f.render().setExpanded(true); f.render(); f.commit(); await flush();
  f.setApi(async args => args.path.endsWith("/decision") ? { status: 503, data: null } : result());
  await f.render().decide(card.id, "accept");
  assert.ok(f.render().errors[card.id]); assert.equal(f.render().shown.current.size, 1);
  f.setOwner(B); f.emit("supymem-native-auth"); await flush();
  const view = f.render();
  assert.equal(view.items.length, 0); assert.equal(view.loaded, false); assert.equal(view.loadedOwner, null);
  assert.equal(view.shown.current.size, 0); assert.equal(Object.keys(view.busy).length, 0); assert.equal(Object.keys(view.errors).length, 0);
  assert.equal(view.presentation.current, A.presentation_id, "old document never adopts B's presentation");
});

test("delayed old-account loads and A-B-A replies cannot restore cached cards", async () => {
  for (const next of [B, { ...A, auth_epoch: 9, presentation_id: "presentation_a_new" }]) {
    const f = fixture(), delayed = deferred<unknown>(); f.setApi(async () => delayed.promise);
    await f.mount(); f.setOwner(next); f.emit("supymem-native-auth"); delayed.resolve(result()); await flush();
    assert.equal(f.render().items.length, 0); assert.equal(f.render().loadedOwner, null);
    assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_ready").length, 0);
  }
});

test("actual delayed decision response cannot mutate a retired owner or reintroduce busy/errors", async () => {
  const f = fixture(); await f.mount(); f.commit(); await flush();
  const delayed = deferred<unknown>(); f.setApi(async () => delayed.promise);
  const pending = f.render().decide(card.id, "accept"); await flush();
  assert.equal(f.render().busy[card.id], true);
  f.setOwner(B); f.emit("supymem-native-auth"); delayed.resolve({ status: 500, data: null }); await pending; await flush();
  const view = f.render(); assert.equal(view.items.length, 0); assert.equal(Object.keys(view.busy).length, 0); assert.equal(Object.keys(view.errors).length, 0);
});

test("actual explicit choice dispatches once and binds native API admission to exact presentation custody", async () => {
  const f = fixture(); await f.mount(); f.commit(); await flush();
  const delayed = deferred<unknown>(); f.setApi(async () => delayed.promise);
  const view = f.render(), first = view.decide(card.id, "accept"), duplicate = view.decide(card.id, "accept");
  await flush();
  const dispatches = f.calls.filter(call => call.args?.path?.endsWith("/decision")); assert.equal(dispatches.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(dispatches[0].args)), { presentationId: A.presentation_id, subject: A.subject, deviceId: A.device_id,
    authEpoch: A.auth_epoch, method: "POST", path: "/api/suggestions/sug_a/decision", body: { decision: "accept" } });
  delayed.resolve({ status: 200, data: {} }); await Promise.all([first, duplicate]);
  assert.equal(f.render().items.length, 0);
});

test("transient native scope conflict retains an undecided card; only saved already-decided proof removes it", async () => {
  for (const data of [{ error: { code: "suggestions_owner_changed" } }, { error: { code: "unknown_conflict" } }, null]) {
    const f = fixture(); await f.mount(); f.setApi(async () => ({ status: 409, data }));
    await f.render().decide(card.id, "accept");
    assert.equal(f.render().items.length, 1); assert.ok(f.render().errors[card.id]); assert.equal(f.render().busy[card.id], undefined);
  }
  for (const data of [{ error: { code: "already_decided" } }, { detail: { error: { code: "already_decided" } } }]) {
    const f = fixture(); await f.mount(); f.setApi(async () => ({ status: 409, data }));
    await f.render().decide(card.id, "accept"); assert.equal(f.render().items.length, 0);
  }
});

test("unmounted overlay rejects delayed loads and removes both native listeners", async () => {
  const f = fixture(), delayed = deferred<unknown>(); f.setApi(async () => delayed.promise);
  await f.mount(); f.dispose(); delayed.resolve(result()); await flush();
  assert.equal(f.render().items.length, 0);
  assert.deepEqual(f.stopped.sort(), ["suggestions-collapse", "suggestions-refresh", "supymem-native-auth"]);
});

test("retired details callback cannot navigate or close another presentation", async () => {
  const f = fixture(); await f.mount(); const view = f.render();
  f.setOwner(B); f.emit("supymem-native-auth"); await view.details(card); await flush();
  assert.equal(f.calls.filter(call => call.name === "suggestions_open_venture").length, 0);
  assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_close").length, 0);
});

function elements(element: any): any[] {
  return element && typeof element === "object" ? [element, ...(element.children || []).flat(Infinity).flatMap(elements)] : [];
}

test("decision custody timeout clears busy, reports command and Retry dispatches after recovery", async () => {
  const f = fixture(); await f.mount(); f.render().setExpanded(true); f.blockCustody(true);
  const pending = f.render().decide(card.id, "accept");
  assert.equal(f.render().busy[card.id], true);
  f.timeout(); await pending;
  assert.equal(f.render().busy[card.id], undefined);
  assert.match(f.render().errors[card.id], /too long/);
  assert.equal(f.telemetry[0].serverCode, "native_timeout");
  assert.equal(f.telemetry[0].source, "suggestions_overlay_custody");
  assert.equal(f.calls.filter(call => call.args?.path?.endsWith("/decision")).length, 0);
  f.blockCustody(false); f.setApi(async () => ({ status: 200, data: {} }));
  elements(f.render().ui()).find(element => element.type === "SuggestionCard").props.onRetry();
  await flush(); await flush();
  assert.equal(f.render().items.length, 0);
});

test("Later hides immediately without custody even while a decision waits on custody", async () => {
  const f = fixture(); await f.mount(); f.render().setExpanded(true); f.blockCustody(true);
  const pending = f.render().decide(card.id, "accept");
  const custodyCalls = f.calls.filter(call => call.name === "suggestions_overlay_custody").length;
  void f.render().later();
  assert.equal(f.render().snoozed, true); assert.equal(f.render().expanded, false);
  assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_custody").length, custodyCalls);
  assert.equal(f.calls.at(-1)?.name, "suggestions_overlay_close");
  f.timeout(); await pending;
});

test("server-confirmed approval shows Approved without another custody wait before removal", async () => {
  const f = fixture(); await f.mount();
  f.setApi(async () => { f.blockCustody(true); return { status: 200, data: {} }; });
  const pending = f.render().decide(card.id, "accept");
  await flush();
  assert.equal(f.render().confirmed[card.id], "accept");
  assert.equal(f.render().items.length, 1);
  await pending;
  assert.equal(f.render().items.length, 0);
});

test("uncertain decision timeout retries saved status without replaying approval", async () => {
  const f = fixture(); await f.mount();
  f.setApi(async () => new Promise(() => {}));
  const pending = f.render().decide(card.id, "accept"); await flush(); f.timeout(); await pending;
  assert.equal(f.render().busy[card.id], undefined);
  f.setApi(async () => result([])); await f.render().decide(card.id, "accept");
  assert.equal(f.render().items.length, 0);
  assert.equal(f.calls.filter(call => call.args?.path?.endsWith("/decision")).length, 1);
});

test("native dispatch busy with explicit not-sent proof leaves Retry able to send once recovered", async () => {
  const f = fixture(); await f.mount(); f.render().setExpanded(true);
  f.setApi(async () => ({ status: 503, data: { error: { code: "suggestions_send_busy", request_sent: false, automatic_retry_allowed: true } } }));
  await f.render().decide(card.id, "accept");
  assert.equal(f.render().busy[card.id], undefined);
  assert.match(f.render().errors[card.id], /Nothing was sent/);
  f.setApi(async () => ({ status: 200, data: {} }));
  elements(f.render().ui()).find(element => element.type === "SuggestionCard").props.onRetry();
  await flush(); await flush();
  assert.equal(f.render().items.length, 0);
  assert.equal(f.calls.filter(call => call.args?.path?.endsWith("/decision")).length, 2);
});

test("dispatch busy without explicit not-sent proof never replays a decision", async () => {
  const f = fixture(); await f.mount();
  f.setApi(async () => ({ status: 503, data: { error: { code: "suggestions_send_busy" } } }));
  await f.render().decide(card.id, "accept");
  f.setApi(async () => result([])); await f.render().decide(card.id, "accept");
  assert.equal(f.calls.filter(call => call.args?.path?.endsWith("/decision")).length, 1);
});

test("load and details timeouts leave retry actions and no unhandled rejection", async () => {
  const f = fixture(); await f.mount(); f.blockCustody(true);
  const load = f.render().load(); f.timeout(); await load;
  assert.match(f.render().loadError, /too long/);
  const details = f.render().details(card); f.timeout(); await details;
  assert.match(f.render().errors[card.id], /too long/);
});

test("twelve ventures render a collapsed current-venture pill and at most one expanded card", async () => {
  const f = fixture();
  f.setApi(async () => result([card, { ...card, id: "sug_next" }, ...Array.from({ length: 12 }, (_, index) => ({ ...card, id: `sug_foreign_${index}`, venture_id: `ven_${index}` }))]));
  await f.mount(); f.commit(); await flush();
  let view = f.render();
  assert.equal(view.items.length, 2);
  assert.ok(f.calls.some(call => call.args?.path === "/api/suggestions?venture_id=ven_a"));
  let tree = elements(view.ui());
  assert.equal(tree.filter(element => element.type === "SuggestionCard").length, 0);
  assert.equal(tree.find(element => element.type === "button").props["aria-expanded"], false);
  tree.find(element => element.type === "button").props.onClick();
  view = f.render(); f.commit(); await flush(); tree = elements(view.ui());
  assert.equal(tree.filter(element => element.type === "SuggestionCard").length, 1);
  assert.equal(tree.find(element => element.type === "SuggestionCard").props.item.id, card.id);
  assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_ready").at(-1)?.args.width, 400);
  assert.deepEqual(Array.from(view.shown.current), [card.id]);
  assert.equal(f.calls.filter(call => call.args?.path === "/api/suggestions/shown").length, 1);
  await view.decide(card.id, "dismiss");
  assert.equal(f.render().visible.id, "sug_next");
});

test("Escape, native main-window Escape and explicit close collapse without dismissing", async () => {
  const f = fixture(); await f.mount(); f.commit(); await flush();
  for (const close of [() => f.key("Escape"), () => f.emit("suggestions-collapse"), () => {
    elements(f.render().ui()).find(element => element.props?.["aria-label"] === "Collapse suggestions").props.onClick();
  }]) {
    f.render().setExpanded(true); f.render(); f.commit(); await flush();
    f.key("Enter"); assert.equal(f.render().expanded, true);
    close(); assert.equal(f.render().expanded, false); assert.equal(f.render().items.length, 1);
  }
  assert.equal(f.focus.length, 3);
  assert.equal(f.calls.filter(call => call.args?.path?.endsWith("/decision")).length, 0);
});

test("Later survives reload for only its user until next local day without deciding cards", async () => {
  const f = fixture(); await f.mount(); await f.render().later();
  assert.equal(f.render().snoozed, true);
  const reload = fixture(f.storage); await reload.mount(); reload.commit(); await flush();
  assert.equal(reload.render().snoozed, true);
  assert.equal(reload.calls.filter(call => call.name === "suggestions_overlay_ready").length, 0);
  const other = fixture(f.storage); other.setOwner({ ...A, subject: "usr_other" }); await other.mount();
  assert.equal(other.render().snoozed, false);
  f.storage.set(`supymem-suggestions-later:${A.subject}`, String(Date.now() - 1));
  const tomorrow = fixture(f.storage); await tomorrow.mount(); assert.equal(tomorrow.render().snoozed, false);
  assert.equal(f.calls.filter(call => call.args?.path?.endsWith("/decision")).length, 0);
});

test("narrow windows never cover content, and a missing current venture shows no foreign cards", async () => {
  for (const next of [{ ...A, viewport_width: 600 }, { ...A, venture_id: null }]) {
    const f = fixture(); f.setOwner(next); await f.mount(); f.commit(); await flush();
    assert.equal(f.calls.filter(call => call.name === "suggestions_overlay_ready").length, 0);
    assert.equal(elements(f.render().ui()).filter(element => element.type === "SuggestionCard" || element.type === "button").length, 0);
  }
});

test("venture navigation clears expanded old cards before a delayed scoped response commits", async () => {
  const f = fixture(); await f.mount(); f.render().setExpanded(true); f.render(); f.commit(); await flush();
  const pending = deferred<unknown>(); f.setApi(async () => pending.promise);
  f.setOwner({ ...A, venture_id: "ven_b" }); f.emit("suggestions-refresh"); await flush();
  assert.equal(f.render().expanded, false); assert.equal(f.render().items.length, 0);
  pending.resolve(result([{ ...card, venture_id: "ven_b", id: "sug_b" }])); await flush();
  assert.equal(f.render().items[0].id, "sug_b"); assert.equal(f.render().expanded, false);
});

test("Pause for today still delegates to the native persisted preference", async () => {
  const f = fixture(); await f.mount(); f.render().setExpanded(true);
  elements(f.render().ui()).find(element => element.props?.["data-usage-control"] === "suggestions_pause_today").props.onClick();
  assert.equal(f.calls.filter(call => call.name === "suggestions_pause_today").length, 1);
});

test("desktop boot never overwrites the main venture from the overlay and forwards main-window Escape", () => {
  const boot = fs.readFileSync(new URL("../components/desktop/DesktopBoot.tsx", import.meta.url), "utf8");
  const bootTree = ts.createSourceFile("boot.tsx", boot, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = bootTree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "DesktopBoot")!;
  const effect = component.body!.statements.find(node => node.getText(bootTree).includes('desktopInvoke("set_active_venture"'))!;
  const calls: any[] = [], listeners = new Map<string, (event: { key: string }) => void>();
  const context = vm.createContext({ native: true, pathname: "/suggestions/overlay",
    useEffect: (callback: () => void) => callback(),
    desktopInvoke: (name: string, args: any) => calls.push({ name, args }),
    window: { addEventListener: (name: string, callback: (event: { key: string }) => void) => listeners.set(name, callback), removeEventListener: () => {} },
  });
  const script = ts.transpileModule(effect.getText(bootTree), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInContext(script, context); assert.equal(calls.length, 0);
  context.pathname = "/v/ven_a/site"; vm.runInContext(script, context);
  assert.equal(calls[0].args.ventureId, "ven_a");
  listeners.get("keydown")!({ key: "Escape" });
  assert.equal(calls[1].args.collapseSuggestions, true); assert.equal(calls[1].args.ventureId, "ven_a");
});
