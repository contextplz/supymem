import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { DECISION_LABEL, scopedSuggestions, suggestionPlan, suggestionsPath } from "./suggestions.ts";

const source = fs.readFileSync(new URL("../app/v/[ventureId]/suggestions/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const component = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "SuggestionsPage")!;
const body = component.body!.statements.filter(node => !ts.isReturnStatement(node)).map(node => node.getText(tree)).join("\n");
const code = ts.transpileModule(`globalThis.component=function(){${body}\nreturn {load,decide,refresh,more,nextOpen,nextHistory,open,history,busy,error,visible,visibleHistory,view,setChoice,waiting};};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;
const owned = { id: "sug_a", venture_id: "ven_a", status: "new", title: "Owned" };
const foreign = { id: "sug_b", venture_id: "ven_b", status: "new", title: "Foreign" };
const reply = (items = [owned, foreign]) => ({ ok: true, status: 200, data: { items, new: items.length, saved: 0 } });
const flush = () => new Promise<void>(done => setImmediate(done));

test("actual sidebar badge counts its venture and labels the global scope", async () => {
  const badgeSource = fs.readFileSync(new URL("../components/layout/SuggestionsBadge.tsx", import.meta.url), "utf8");
  const badgeTree = ts.createSourceFile("badge.tsx", badgeSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const badge = badgeTree.statements.find((node): node is ts.FunctionDeclaration =>
    ts.isFunctionDeclaration(node) && node.name?.text === "SuggestionsBadge")!;
  const badgeCode = ts.transpileModule(`globalThis.badge=${badge.getText(badgeTree).replace(/^export /, "")}`, {
    fileName: "badge.tsx",
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  let count = 0, cleanup: (() => void) | undefined;
  const calls: string[] = [];
  const context = vm.createContext({
    React: { createElement: (_tag: string, props: any) => props },
    useState: () => [count, (value: number) => { count = value; }],
    useEffect: (effect: () => () => void) => { cleanup?.(); cleanup = effect(); },
    api: async (path: string) => { calls.push(path); return { ok: true, data: { new: 61, saved: 2 } }; },
    badgeLabel: (value: number) => value ? "9+" : "", suggestionsPath, SUGGESTIONS_CHANGED: "suggestions-changed",
    window: { setInterval: () => 1, clearInterval: () => undefined,
      addEventListener: () => undefined, removeEventListener: () => undefined },
  });
  vm.runInContext(badgeCode, context);
  context.badge({ ventureId: "ven_a" }); await flush();
  assert.equal(calls[0], "/api/suggestions?venture_id=ven_a");
  assert.equal(context.badge({ ventureId: "ven_a" })["aria-label"], "61 waiting in this venture");
  context.badge({}); await flush();
  assert.equal(calls.at(-1), "/api/suggestions");
  assert.equal(context.badge({})["aria-label"], "61 waiting across all ventures");
  cleanup?.();
});

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }

function fixture() {
  let ventureId = "ven_a", epoch = 1, cursor = 0;
  const slots: any[] = [], effects: (() => void)[] = [], listeners = new Map<string, () => void>();
  const calls: { path: string; options: any }[] = [];
  let handler: (path: string, options: any) => Promise<any> = async () => reply();
  const context = vm.createContext({
    Set, String, Promise, Array, JSON, Event, encodeURIComponent,
    useParams: () => ({ ventureId }),
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
    window: { addEventListener: (name: string, callback: () => void) => listeners.set(name, callback),
      removeEventListener: (name: string) => listeners.delete(name), dispatchEvent: () => undefined },
    api: async (path: string, options: any) => { calls.push({ path, options }); return handler(path, options); },
    ensureSession: async () => undefined, verifiedSessionEpoch: () => epoch, serverReason: () => "Saved status unavailable",
    scopedSuggestions, suggestionsPath, SUGGESTIONS_CHANGED: "suggestions-changed",
  });
  vm.runInContext(code, context);
  const render = () => { cursor = 0; return context.component(); };
  const commit = () => { while (effects.length) effects.shift()!(); };
  return { render, commit, calls, setHandler: (value: typeof handler) => { handler = value; },
    navigate: (id: string) => { ventureId = id; }, retire: () => { ++epoch; listeners.get("supymem-session-changed")?.(); },
    mount: async () => { render(); commit(); await flush(); return render(); } };
}

test("actual venture page reads scoped open/history and rejects ignored-filter foreign cards", async () => {
  const f = fixture(), view = await f.mount();
  assert.deepEqual(f.calls.map(c => c.path), ["/api/suggestions?venture_id=ven_a", "/api/suggestions?venture_id=ven_a&status=history"]);
  assert.deepEqual(Array.from(view.visible, (i: any) => i.id), [owned.id]);
  assert.deepEqual(Array.from(view.visibleHistory, (i: any) => i.id), [owned.id]);
  await view.decide(foreign.id, "accept");
  assert.equal(f.calls.filter(c => c.options?.method === "POST").length, 0);
});

test("explicit all-ventures view still refuses foreign decisions, while exact current save dispatches once", async () => {
  const f = fixture(); await f.mount();
  f.render().setChoice({ ventureId: "ven_a", view: "all" }); f.render(); f.commit(); await flush();
  const view = f.render(); assert.equal(view.visible.length, 2);
  assert.ok(f.calls.some(c => c.path === "/api/suggestions"));
  await view.decide(foreign.id, "accept");
  await Promise.all([view.decide(owned.id, "save"), view.decide(owned.id, "save")]);
  const decisions = f.calls.filter(c => c.path.endsWith("/decision"));
  assert.equal(decisions.length, 1); assert.equal(decisions[0].path, "/api/suggestions/sug_a/decision");
  assert.deepEqual(JSON.parse(decisions[0].options.body), { decision: "save" });
});

test("navigation resets the default scope and rejects a delayed previous-venture response", async () => {
  const f = fixture(); await f.mount(); const old = deferred<any>(); f.setHandler(async () => old.promise);
  const pending = f.render().load(); await flush();
  f.navigate("ven_b"); const beforeCommit = f.render(); assert.equal(beforeCommit.visible, null); assert.equal(beforeCommit.view, "venture");
  f.setHandler(async () => reply([foreign])); f.commit(); await flush();
  old.resolve(reply([owned])); await pending; await flush();
  assert.deepEqual(Array.from(f.render().visible, (i: any) => i.id), [foreign.id]);
});

test("retired-account loads and delayed decisions cannot restore old cards, busy flags or errors", async () => {
  for (const operation of ["load", "decision"]) {
    const f = fixture(); await f.mount(); const old = deferred<any>(); f.setHandler(async () => old.promise);
    const pending = operation === "load" ? f.render().load() : f.render().decide(owned.id, "accept"); await flush();
    f.setHandler(async () => reply([])); f.retire(); await flush();
    old.resolve({ ok: false, status: 503, data: null }); await pending; await flush();
    const view = f.render(); assert.equal(view.visible.length, 0); assert.equal(Object.keys(view.busy).length, 0); assert.equal(view.error, "");
  }
});

test("page refresh during approval always retires busy state", async () => {
  const f = fixture(); await f.mount(); const delayed = deferred<any>();
  f.setHandler(async () => delayed.promise);
  const pending = f.render().decide(owned.id, "accept"); await flush();
  f.setHandler(async () => reply()); await f.render().load();
  delayed.resolve({ ok: true, status: 200, data: {} }); await pending;
  assert.equal(Object.keys(f.render().busy).length, 0);
});

test("pagination fetches cards beyond fifty in the exact scope", async () => {
  const f = fixture();
  f.setHandler(async () => ({ ...reply([owned]), data: { ...reply([owned]).data, next_offset: 50 } }));
  await f.mount();
  const later = { ...owned, id: "sug_51" };
  f.setHandler(async () => reply([later]));
  await f.render().more(false);
  assert.ok(f.calls.some(call => call.path === "/api/suggestions?venture_id=ven_a&offset=50"));
  assert.deepEqual(Array.from(f.render().visible, (item: any) => item.id), [owned.id, later.id]);
  assert.equal(f.render().nextOpen, null);
});

test("actual card renders foreign navigation without decision buttons and describes the enforced approval limit", () => {
  const cardSource = fs.readFileSync(new URL("../components/suggestions/SuggestionCard.tsx", import.meta.url), "utf8");
  const cardTree = ts.createSourceFile("card.tsx", cardSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const card = cardTree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node))!;
  const cardCode = ts.transpileModule(`${card.getText(cardTree).replace(/^export /, "")};globalThis.card=SuggestionCard;`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  const elements: any[] = [];
  const context = vm.createContext({ DECISION_LABEL, suggestionPlan, styles: {}, ChatAnchor: "a", ErrorNotice: () => null,
    React: { createElement: (type: any, props: any, ...children: any[]) => { const element = { type, props, children }; elements.push(element); return element; } } });
  vm.runInContext(cardCode, context);
  context.card({ item: { ...foreign, est_credits: 0 }, reviewHref: "/v/ven_b/suggestions", onDecide: () => assert.fail("foreign decision") });
  assert.equal(elements.filter(e => e.type === "button").length, 0);
  assert.equal(elements.find(e => e.type === "a")?.props.href, "/v/ven_b/suggestions");
  assert.equal(elements.some(e => e.children.includes("No credits")), false);
  elements.length = 0;
  context.card({ item: owned, busy: true, pendingDecision: "accept", onDecide: () => {} });
  assert.ok(elements.some(element => element.type === "button" && element.children.includes("Approving…")));
  elements.length = 0;
  context.card({ item: owned, confirmedDecision: "accept", onDecide: () => {} });
  assert.ok(elements.some(element => element.props?.role === "status" && element.children.includes("Approved")));
  assert.ok(elements.filter(element => element.type === "button").every(element => element.props.disabled));
  elements.length = 0;
  let retries = 0;
  context.card({ item: owned, error: "Timed out", onRetry: () => retries++, onDecide: () => {} });
  elements.find(element => element.type === "button" && element.children.includes("Retry")).props.onClick();
  assert.equal(retries, 1);
  elements.length = 0;
  context.card({item: {...owned, action_kind: "browser.task.prepare"}, onDecide: () => {}});
  assert.ok(elements.some(element => element.type === "button" && element.children.includes("Prepare for review")));
  elements.length = 0;
  context.card({item: {...owned, action_kind: "browser.task.prepare"}, confirmedDecision: "accept", onDecide: () => {}});
  assert.ok(elements.some(element => element.props?.role === "status" && element.children.includes("Prepared for review; nothing has run")));
});
