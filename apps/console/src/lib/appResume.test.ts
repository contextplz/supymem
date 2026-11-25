import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { resumeAppRoute } from "./appResume.ts";
import { routeInChat } from "./chatRouteAction.ts";

function storage() {
  const values = new Map<string, string>();
  return {values, getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {values.set(key, value);}};
}

test("resume routes are durable, subject scoped, and preserve query and hash", () => {
  const store = storage();
  for (const href of ["/new", "/new?venture=ven_one#answer", "/v/ven_one/idea?chat=1#review", "/gtm?venture=ven_one&workspace=ws_one#results"]) {
    assert.equal(resumeAppRoute(store, "usr_one", href, false), null);
    assert.equal(resumeAppRoute(store, "usr_one", "/", true), href);
    assert.equal(resumeAppRoute(store, "usr_two", "/", true), null);
    assert.equal(resumeAppRoute(store, "usr_one", "/", false), null);
  }
  const size = store.values.size;
  resumeAppRoute(store, null, "/new", true);
  assert.equal(store.values.size, size);
});

test("non-workspace routes never replace the resume point, and unsafe saved routes are rejected", () => {
  const store = storage();
  resumeAppRoute(store, "usr_one", "/new", false);
  for (const href of ["/", "/admin", "/auth/device", "/operator", "/gtm", "/newspaper", "/v/", "//evil.test/new", "/\\evil.test/new", "https://evil.test/new"]) {
    resumeAppRoute(store, "usr_one", href, false);
    assert.equal(resumeAppRoute(store, "usr_one", "/", true), "/new");
    store.setItem("supymem.lastRoute.v1:usr_two", href);
    assert.equal(resumeAppRoute(store, "usr_two", "/", true), null);
  }
  const denied = {getItem: () => {throw new Error("denied");}, setItem: () => {throw new Error("quota");}};
  assert.equal(resumeAppRoute(denied, "usr_one", "/", true), null);
  assert.equal(resumeAppRoute(denied, "usr_one", "/new", false), null);
});

test("the shipped shell restores only the first navigation, even across shell remounts", () => {
  const source = fs.readFileSync(new URL("../components/layout/AppShell.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("AppShell.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "AppShellInner")!;
  const effect = component.body!.statements.find(node => node.getText(tree).includes("const firstNavigation = !resumeNavigationChecked"))!;
  const store = storage(), navigated: string[] = [];
  resumeAppRoute(store, "usr_one", "/new?venture=ven_one", false);
  const context = vm.createContext({resumeNavigationChecked: false, resumeAppRoute, routeInChat,
    pathname: "/", browserEntry: false, local: false, authed: false, founderUI: "home", founderUILoaded: false, sessionSubject: undefined, searchParams: {},
    verifiedSessionSubject: () => context.sessionSubject,
    window: {localStorage: store, location: {pathname: "/", search: "", hash: ""}},
    router: {replace: (href: string) => navigated.push(href)}, useEffect: (run: () => void) => run()});
  const code = ts.transpileModule(effect.getText(tree), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  const run = (pathname: string) => {
    context.pathname = pathname; context.window.location.pathname = pathname;
    vm.runInContext(code, context);
  };
  run("/"); assert.deepEqual(navigated, []);
  context.authed = true; context.sessionSubject = "usr_one"; run("/");
  assert.deepEqual(navigated, []);
  context.founderUILoaded = true; run("/");
  assert.deepEqual(navigated, ["/new?venture=ven_one"]);
  run("/new"); run("/"); assert.equal(navigated.length, 1);
  context.sessionSubject = "usr_two"; run("/"); assert.equal(navigated.length, 1);
  context.resumeNavigationChecked = false; run("/auth/device");
  context.sessionSubject = "usr_one"; run("/"); assert.equal(navigated.length, 1);
  context.browserEntry = true; context.resumeNavigationChecked = false;
  run("/auth"); assert.equal(context.resumeNavigationChecked, false); assert.equal(navigated.length, 1);
});

test("a chat-first restart restores the company chat, never the saved legacy journey", () => {
  const store = storage();
  for (const href of ["/v/library/idea?chat=1#review", "/v/clinic/idea?edit_previous=1", "/v/clinic/research", "/gtm?venture=clinic"]) {
    resumeAppRoute(store, "usr_one", href, false);
    const venture = href.includes("library") ? "library" : "clinic";
    assert.equal(resumeAppRoute(store, "usr_one", "/", true, "home"), `/v/${venture}`);
    assert.equal(resumeAppRoute(store, "usr_one", "/", true, "classic"), href);
  }
  resumeAppRoute(store, "usr_one", "/v/library/idea/current", false);
  assert.equal(resumeAppRoute(store, "usr_one", "/", true, "home"), "/v/library/idea/current");
});
