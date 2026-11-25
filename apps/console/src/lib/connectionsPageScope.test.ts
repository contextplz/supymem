import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";

// Compile the shipped renderer and callbacks; all API replies are owned fixtures.
// No provider authorization, credential, network request or revoke occurs.
const source = fs.readFileSync(new URL("../app/v/[ventureId]/connections/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const body = tree.statements.filter(node => !ts.isImportDeclaration(node))
  .map(node => node.getText(tree).replace(/^export default /, "")).join("\n")
  .replace("if (!ready) return <", "globalThis.view={load,revoke,openReceipt,refreshSaved,workspaceId,items,events,openId,msg,busy,ready,loadError};\nif (!ready) return <");
assert.ok(body.includes("globalThis.view="));
const code = ts.transpileModule(body + "\nglobalThis.component=ConnectionsPage;", {
  compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
}).outputText;
const flush = () => new Promise<void>(done => setImmediate(done));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
const ok = (data: unknown) => ({ ok: true, status: 200, data });
function fixture() {
  let ventureId = "ven_a", subject: string | null = "usr_a", epoch = 1, cursor = 0;
  const slots: any[] = [], effects: (() => void)[] = [], elements: any[] = [], listeners = new Map<string, Set<() => void>>();
  const calls: { path: string; options: any }[] = [];
  const row = (wid: string, suffix = "one") => ({ id: `conn_${wid}_${suffix}`, workspace_id: wid, provider: "github", status: "active", external_account: `account_${wid}`, health: "unknown" });
  const response = async (path: string, options?: any) => {
    if (path.startsWith("/api/ventures/")) return ok({ workspace_id: `wsp_${path.split("/").at(-1)}` });
    if (path.endsWith("/connections")) { const wid = path.split("/").at(-2)!; return ok({ items: [row(wid), row(wid, "two")] }); }
    if (path.endsWith("/events")) return ok({ items: [{ id: `evt_${path.split("/").at(-2)}`, at: "fixture", direction: "read", operation: "profile", outcome: "completed" }] });
    if (options?.method === "POST" && path.endsWith("/revoke")) {
      const id = path.split("/").at(-2)!; return ok({ id, workspace_id: id.slice(5).replace(/_(one|two)$/, ""), status: "revoked" });
    }
    throw new Error("Unexpected owned fixture path");
  };
  let handler: (path: string, options: any) => Promise<any> = response;
  const context = vm.createContext({
    String, JSON, Promise, Boolean, Array, encodeURIComponent, AppShell: "shell",
    React: { createElement: (type: unknown, props: any, ...children: any[]) => { const element = { type, props: props || {}, children }; elements.push(element); return element; } },
    useParams: () => ({ ventureId }),
    useState: (initial: unknown) => { const index = cursor++; slots[index] ??= { value: initial }; return [slots[index].value, (value: unknown) => { slots[index].value = typeof value === "function" ? value(slots[index].value) : value; }]; },
    useRef: (initial: unknown) => { const index = cursor++; return slots[index] ??= { current: initial }; },
    useCallback: (callback: unknown, deps: unknown[]) => { const index = cursor++, previous = slots[index]; if (!previous || deps.some((value, i) => value !== previous.deps[i])) slots[index] = { callback, deps }; return slots[index].callback; },
    useEffect: (callback: () => (() => void) | undefined, deps: unknown[]) => { const index = cursor++, previous = slots[index]; if (!previous || deps.some((value, i) => value !== previous.deps[i])) { slots[index] = { deps, cleanup: previous?.cleanup }; effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = callback(); }); } },
    ensureSession: async () => ({ ok: Boolean(subject), data: { authenticated: Boolean(subject), subject } }),
    verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
    api: async (path: string, options: any) => { calls.push({ path, options }); return handler(path, options); },
    window: { addEventListener: (name: string, callback: () => void) => { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name)!.add(callback); }, removeEventListener: (name: string, callback: () => void) => listeners.get(name)?.delete(callback) },
  });
  vm.runInContext(code, context);
  const render = () => { cursor = 0; elements.length = 0; context.component(); return { ...context.view, elements: [...elements] }; };
  const commit = () => { while (effects.length) effects.shift()!(); };
  return { render, commit, calls, response, row, setHandler: (next: typeof handler) => { handler = next; },
    navigate: (id: string) => { ventureId = id; },
    retire: (next: string | null, notify = true) => { subject = next; ++epoch; if (notify) listeners.get("supymem-session-changed")?.forEach(callback => callback()); },
    mount: async () => { render(); commit(); await flush(); return render(); },
    mutations: () => calls.filter(call => call.options?.method),
    text: () => render().elements.flatMap((element: any) => element.children.flat(Infinity).filter((value: unknown) => typeof value === "string")).join(" "),
  };
}

test("actual retained Revoke callback cannot act on venture A from the newly rendered B page", async () => {
  const f = fixture(), old = await f.mount();
  const revoke = old.elements.find((element: any) => element.type === "button" && element.children.includes("Revoke"));
  f.navigate("ven_b"); const hidden = f.render();
  assert.equal(hidden.ready, false); assert.doesNotMatch(f.text(), /account_wsp_ven_a/);
  revoke.props.onClick(); await hidden.revoke("conn_wsp_ven_a_one"); await flush();
  assert.equal(f.mutations().length, 0);
  f.commit(); await flush(); const current = f.render(); await current.revoke("conn_wsp_ven_b_one");
  assert.deepEqual(f.mutations().map(call => call.path), ["/api/connections/conn_wsp_ven_b_one/revoke"]);
});

test("delayed A workspace and account reads cannot populate current B", async () => {
  for (const delayedPath of ["/api/ventures/ven_a", "/api/workspaces/wsp_ven_a/connections"]) {
    const f = fixture(); await f.mount(); const old = deferred<any>();
    f.setHandler(async (path, options) => path === delayedPath ? old.promise : f.response(path, options));
    const pending = f.render().load(); await flush(); f.navigate("ven_b"); f.render(); f.commit(); await flush();
    old.resolve(delayedPath.includes("/workspaces/") ? ok({ items: [f.row("wsp_ven_a")] }) : ok({ workspace_id: "wsp_ven_a" }));
    await pending; assert.equal(f.render().workspaceId, "wsp_ven_b"); assert.doesNotMatch(f.text(), /account_wsp_ven_a/);
  }
});

test("account retirement and same-subject ABA refuse retained effects and hide prior account content", async () => {
  for (const next of ["usr_b", null, "usr_a"]) {
    const f = fixture(), old = await f.mount();
    f.retire("usr_other", false); f.retire(next, false);
    assert.equal(f.render().ready, false); assert.doesNotMatch(f.text(), /account_wsp_ven_a/);
    const count = f.calls.length; await old.revoke("conn_wsp_ven_a_one"); await old.openReceipt("conn_wsp_ven_a_one"); old.refreshSaved(); await flush();
    assert.equal(f.calls.length, count);
  }
});

test("a stale revoke completion cannot claim success or clear a new owner's pending action", async () => {
  const f = fixture(), old = await f.mount(), a = deferred<any>(), b = deferred<any>();
  f.setHandler(async (path, options) => options?.method ? a.promise : f.response(path, options));
  const first = old.revoke("conn_wsp_ven_a_one"); await flush();
  f.retire("usr_b"); await flush();
  f.setHandler(async (path, options) => options?.method ? b.promise : f.response(path, options));
  const second = f.render().revoke("conn_wsp_ven_a_two"); await flush();
  a.resolve(ok({ id: "conn_wsp_ven_a_one", workspace_id: "wsp_ven_a", status: "revoked" })); await first;
  assert.equal(f.render().busy, true); assert.equal(f.render().msg, "");
  b.resolve({ ok: false, status: 503 }); await second;
  assert.match(f.render().msg, /not confirmed/); assert.equal(f.render().busy, false);
});

test("foreign IDs, failed list reads and mismatched workspace rows cannot confer action authority", async () => {
  const f = fixture(), view = await f.mount(); const count = f.calls.length;
  await view.revoke("foreign"); await view.openReceipt("foreign"); assert.equal(f.calls.length, count);
  for (const bad of [{ ok: false, status: 503 }, ok({ items: [f.row("wsp_foreign")] }), ok({ items: [{}] })]) {
    f.setHandler(async (path, options) => path.endsWith("/connections") ? bad : f.response(path, options));
    await f.render().load(); assert.equal(f.render().ready, false);
    await f.render().revoke("conn_wsp_ven_a_one"); assert.equal(f.mutations().length, 0);
  }
  f.setHandler(f.response); await f.render().load(); assert.equal(f.render().ready, true);
});

test("duplicate current Revoke callbacks dispatch once and only an exact saved revoked receipt claims success", async () => {
  const f = fixture(), view = await f.mount(), pending = deferred<any>();
  f.setHandler(async (path, options) => options?.method ? pending.promise : f.response(path, options));
  const first = view.revoke("conn_wsp_ven_a_one"), second = view.revoke("conn_wsp_ven_a_one");
  assert.equal(f.mutations().length, 1);
  pending.resolve(ok({ id: "foreign", workspace_id: "wsp_ven_a", status: "revoked" })); await Promise.all([first, second]);
  assert.match(f.render().msg, /not confirmed/); assert.doesNotMatch(f.render().msg, /marked revoked/);
  for (const receipt of [{ id: "conn_wsp_ven_a_one", workspace_id: "wsp_foreign", status: "revoked" }, { id: "conn_wsp_ven_a_one", workspace_id: "wsp_ven_a", status: "active" }]) {
    f.setHandler(async (path, options) => options?.method ? ok(receipt) : f.response(path, options));
    await f.render().revoke("conn_wsp_ven_a_one"); assert.match(f.render().msg, /not confirmed/);
  }
  f.setHandler(f.response); await f.render().revoke("conn_wsp_ven_a_one");
  assert.match(f.render().msg, /Saved connection marked revoked/);
});

test("receipt selection and retirement withhold late old item bodies", async () => {
  const f = fixture(), old = await f.mount(), pending = deferred<any>();
  f.setHandler(async (path, options) => path.includes("_one/events") ? pending.promise : f.response(path, options));
  const first = old.openReceipt("conn_wsp_ven_a_one"); await old.openReceipt("conn_wsp_ven_a_two");
  pending.resolve(ok({ items: [{ id: "obsolete_private_receipt" }] })); await first;
  assert.equal(f.render().openId, "conn_wsp_ven_a_two"); assert.equal(f.render().events[0].id, "evt_conn_wsp_ven_a_two");
  const another = deferred<any>(); f.setHandler(async () => another.promise);
  const second = f.render().openReceipt("conn_wsp_ven_a_one"); f.retire("usr_b");
  another.resolve(ok({ items: [{ id: "retired_private_receipt" }] })); await second;
  assert.equal(f.render().events.length, 0); assert.equal(f.render().openId, "");
});

test("malformed new receipt cannot relabel an earlier item's private event body", async () => {
  const f = fixture(), view = await f.mount(); await view.openReceipt("conn_wsp_ven_a_one");
  f.setHandler(async () => ok(null)); await f.render().openReceipt("conn_wsp_ven_a_two");
  assert.equal(f.render().openId, "conn_wsp_ven_a_one"); assert.equal(f.render().events[0].id, "evt_conn_wsp_ven_a_one");
  assert.match(f.render().msg, /could not be loaded/);
});

test("unsupported legacy credential form is replaced by an exact company setup link and read recovery", async () => {
  const f = fixture(), view = await f.mount();
  const link = view.elements.find((element: any) => element.type === "a");
  assert.equal(link.props.href, "/connections?workspace=wsp_ven_a&venture=ven_a");
  assert.match(f.text(), /Credential entry is unavailable on this page/);
  assert.doesNotMatch(f.text(), /Save securely|Connect account|Personal access token/);
  const oldRecovery = view.refreshSaved; f.navigate("ven_b"); f.render();
  const count = f.calls.length; oldRecovery(); await flush(); assert.equal(f.calls.length, count);
});
