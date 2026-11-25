import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const flush = () => new Promise<void>(resolve => setImmediate(resolve));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function fixture() {
  const file = "../components/site/PublishConceptPage.tsx";
  const source = fs.readFileSync(new URL(file, import.meta.url), "utf8");
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = tree.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(tree).replace(/^export /, "")).join("\n");
  const code = ts.transpileModule(body + "\nglobalThis.component = PublishConceptPage;", { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  let actor: string | null = "usr_a", epoch = 1, ventureId = "ven_a", cursor = 0;
  const slots: any[] = [], effects: (() => void)[] = [], elements: any[] = [], calls: any[] = [];
  const listeners = new Map<string, () => void>(), timers = new Map<number, () => void>();
  let timerId = 0;
  const publication = (id = ventureId, extra: any = {}) => ({ state: "publishable", draft: { site_version_id: `sv_${id}`, version: 1, sha256: id.repeat(8), headline: `private_${id}` }, blockers: [], ...extra });
  const response = async (_path: string, options?: any) => ({ ok: true, status: 200, data: options?.method === "POST" ? { state: "pending" } : publication() });
  let handler = response;
  const context = vm.createContext({ URL, String, Boolean, Error, JSON, Set, encodeURIComponent, decodeURIComponent,
    React: { createElement: (type: any, props: any, ...children: any[]) => { const element = { type, props: props || {}, children }; elements.push(element); return element; } },
    useState: (initial: any) => { const i = cursor++; slots[i] ??= { value: initial }; return [slots[i].value, (value: any) => { slots[i].value = typeof value === "function" ? value(slots[i].value) : value; }]; },
    useRef: (initial: any) => { const i = cursor++; return slots[i] ??= { current: initial }; },
    useCallback: (fn: any, deps: any[]) => { const i = cursor++, previous = slots[i]; if (!previous || deps.some((value, j) => value !== previous.deps[j])) slots[i] = { fn, deps }; return slots[i].fn; },
    useEffect: (fn: () => any, deps: any[]) => { const i = cursor++, previous = slots[i]; if (!previous || deps.some((value, j) => value !== previous.deps[j])) { slots[i] = { deps, cleanup: previous?.cleanup }; effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = fn(); }); } },
    api: async (path: string, options?: any) => { calls.push({ path, options }); return handler(path, options); },
    verifiedSessionSubject: () => actor, verifiedSessionEpoch: () => epoch, ErrorNotice: "ErrorNotice", SiteAddress: "SiteAddress", styles: { note: "note" },
    window: { location: { pathname: "/v/ven_a/site" }, setInterval: (fn: () => void) => { timers.set(++timerId, fn); return timerId; }, clearInterval: (id: number) => timers.delete(id),
      addEventListener: (name: string, fn: () => void) => listeners.set(name, fn), removeEventListener: (name: string) => listeners.delete(name) },
  });
  vm.runInContext(code, context);
  const render = () => { cursor = 0; elements.length = 0; context.component({ ventureId }); return [...elements]; };
  const commit = () => { while (effects.length) effects.shift()!(); };
  const text = () => render().flatMap(element => [element.props.error || "", ...element.children.flat(Infinity).filter((value: any) => typeof value === "string")]).join(" ");
  const button = (label: string) => { const element = render().find(element => element.type === "button" && element.children.includes(label)); assert.ok(element, `Missing ${label}: ${text()}`); return element; };
  const confirmed = () => { button("Publish landing page").props.onClick(); return button("Yes, publish this landing page"); };
  return { calls, publication, response, render, commit, text, button, confirmed,
    mount: async () => { render(); commit(); await flush(); return render(); },
    handler: (value: typeof handler) => { handler = value; },
    venture: (value: string) => { ventureId = value; context.window.location.pathname = `/v/${value}/site`; },
    liveRoute: (value: string) => { context.window.location.pathname = `/v/${value}/site`; },
    retire: (value: string | null) => { actor = value; epoch++; listeners.get("supymem-session-changed")?.(); },
    tick: () => timers.forEach(fn => fn()),
    unmount: () => slots.forEach(slot => slot?.cleanup?.()),
  };
}

test("current confirmation publishes the exact saved draft only once and waits for live readback", async () => {
  const f = fixture(); await f.mount(); const pending = deferred<any>();
  f.handler(async (path, options) => options?.method === "POST" ? pending.promise : f.response(path, options));
  const yes = f.confirmed(); yes.props.onClick(); yes.props.onClick();
  const posts = f.calls.filter(call => call.options?.method === "POST"); assert.equal(posts.length, 1);
  assert.equal(posts[0].path, "/api/ventures/ven_a/site/publication");
  assert.deepEqual(JSON.parse(posts[0].options.body), { site_version_id: "sv_ven_a", public_sha256: "ven_a".repeat(8) });
  assert.doesNotMatch(f.text(), /Live at/);
  pending.resolve({ ok: true, status: 200, data: { state: "pending" } }); await flush();
  assert.match(f.text(), /Publishing is queued/); assert.doesNotMatch(f.text(), /Live at/);
});

test("retained confirmations and preview reads refuse route retirement before React renders", async () => {
  const f = fixture(); await f.mount(); const preview = f.button("Preview the public landing page"), yes = f.confirmed(), count = f.calls.length;
  f.liveRoute("ven_b"); yes.props.onClick(); preview.props.onClick(); await flush();
  assert.equal(f.calls.length, count); assert.doesNotMatch(f.text(), /private_ven_a|sv_ven_a/);
});

test("retained publication callbacks refuse actor changes and same-actor ABA", async () => {
  for (const next of ["usr_b", null, "usr_a"]) { const f = fixture(); await f.mount(); const yes = f.confirmed(), count = f.calls.length; f.retire("usr_other"); f.retire(next); yes.props.onClick(); await flush(); assert.equal(f.calls.length, count); assert.doesNotMatch(f.text(), /private_ven_a/); }
});

test("unknown identity cannot read or prepare publication", async () => {
  const f = fixture(); f.retire(null); await f.mount(); assert.equal(f.calls.length, 0); assert.equal(f.text(), "");
});

test("late venture-A initial read cannot replace the loaded venture-B page", async () => {
  const f = fixture(), old = deferred<any>(); f.handler(async () => old.promise); f.render(); f.commit();
  f.venture("ven_b"); f.handler(f.response); f.render(); f.commit(); await flush();
  old.resolve({ ok: true, status: 200, data: f.publication("ven_a", { state: "live", live_url: "https://obsolete.example/" }) }); await flush();
  assert.match(f.text(), /private_ven_b/); assert.doesNotMatch(f.text(), /ven_a|obsolete/);
});

test("late private preview is withheld on route retirement during its read", async () => {
  const f = fixture(); await f.mount(); const pending = deferred<any>(); f.handler(async () => pending.promise);
  f.button("Preview the public landing page").props.onClick(); f.liveRoute("ven_b");
  pending.resolve({ ok: true, status: 200, data: f.publication("ven_a", { preview_html: "<p>obsolete private preview</p>" }) }); await flush();
  assert.equal(f.render().some(element => element.type === "iframe"), false); assert.doesNotMatch(f.text(), /private_ven_a/);
});

test("a slower status poll cannot overwrite a newer verified live receipt", async () => {
  const f = fixture(); f.handler(async () => ({ ok: true, status: 200, data: f.publication("ven_a", { state: "blocked", deployment: { state: "pending" } }) })); await f.mount(); f.commit();
  const old = deferred<any>(); f.handler(async () => old.promise); f.tick();
  f.handler(async () => ({ ok: true, status: 200, data: f.publication("ven_a", { state: "live", live_url: "https://current.example/" }) })); f.tick(); await flush();
  old.resolve({ ok: true, status: 200, data: f.publication() }); await flush();
  assert.match(f.text(), /current\.example/); assert.equal(f.render().some(element => element.children.includes("Publish landing page")), false);
});

test("old publication completion cannot install its URL, start another read, or clear new venture's busy action", async () => {
  const f = fixture(); await f.mount(); const a = deferred<any>(), b = deferred<any>();
  f.handler(async (path, options) => options?.method === "POST" ? a.promise : f.response(path, options)); f.confirmed().props.onClick();
  f.venture("ven_b"); f.handler(f.response); f.render(); f.commit(); await flush();
  f.handler(async (path, options) => options?.method === "POST" ? b.promise : f.response(path, options)); f.confirmed().props.onClick(); const count = f.calls.length;
  a.resolve({ ok: true, status: 200, data: { state: "live", url: "https://obsolete.example/" } }); await flush();
  assert.equal(f.calls.length, count); assert.equal(f.button("Yes, publish this landing page").props.disabled, true); assert.doesNotMatch(f.text(), /queued|obsolete/);
  b.resolve({ ok: true, status: 200, data: { state: "pending" } }); await flush(); assert.match(f.text(), /queued/);
});

test("a saved page refresh retires the old draft confirmation before another effect", async () => {
  const f = fixture(); await f.mount(); const yes = f.confirmed();
  f.handler(async () => ({ ok: true, status: 200, data: f.publication("ven_a", { draft: { site_version_id: "sv_new", version: 2, sha256: "new", headline: "new page" } }) }));
  f.button("Preview the public landing page").props.onClick(); await flush(); yes.props.onClick(); await flush();
  assert.equal(f.calls.filter(call => call.options?.method === "POST").length, 0); assert.match(f.text(), /new page/);
});

test("transport failures and unconfirmed POST refusals require status recovery without a false unpublished claim", async () => {
  for (const mode of ["throw", "refuse"]) { const f = fixture(); await f.mount(); f.handler(async () => { if (mode === "throw") throw new Error("lost response"); return { ok: false, status: 503, data: {} }; }); const yes = f.confirmed(); yes.props.onClick(); await flush();
    assert.match(f.text(), /could not be confirmed|may have reached hosting/); assert.doesNotMatch(f.text(), /Nothing.*public|queued/);
    const count = f.calls.length; yes.props.onClick(); await flush(); assert.equal(f.calls.length, count); assert.ok(f.button("Refresh publishing status"));
  }
});

test("failed preview reads are reported and current status can recover without an unhandled promise", async () => {
  const f = fixture(); await f.mount(); f.handler(async () => { throw new Error("read failure"); }); f.button("Preview the public landing page").props.onClick(); await flush();
  assert.match(f.text(), /could not be loaded/); assert.ok(f.render().some(element => element.type === "ErrorNotice" && element.props.source === "PublishConceptPage.request" && element.props.error));
  f.handler(f.response); f.button("Refresh publishing status").props.onClick(); await flush(); assert.ok(f.button("Publish landing page"));
});

test("uncertain public state remains visible even when no current draft can be read", async () => {
  const f = fixture(); f.handler(async () => ({ ok: true, status: 200, data: { state: "blocked", draft: null, blockers: [{ code: "hosting_public_state_uncertain", message: "Public state could not be confirmed. Review before retrying." }] } })); await f.mount();
  assert.match(f.text(), /could not be confirmed/); assert.equal(f.render().some(element => element.children.includes("Publish landing page")), false); assert.doesNotMatch(f.text(), /Nothing.*public/);
});

test("unmounted publication callbacks and responses cannot dispatch or restore the page", async () => {
  const f = fixture(); await f.mount(); const yes = f.confirmed(), count = f.calls.length; f.unmount(); yes.props.onClick(); await flush(); assert.equal(f.calls.length, count); assert.equal(f.text(), "");
});

test("publish saves the chosen address first and stops with the server's plain words when it is taken", async () => {
  const f = fixture();
  await f.mount();
  const field = () => f.render().find(element => element.type === "SiteAddress");
  assert.ok(field(), "the address field shows before the first publish");
  field()!.props.onReady("my-bakery", null);
  f.handler(async (path: string, options?: any) => path.endsWith("/site-address")
    ? { ok: false, status: 409, data: { detail: { message: "That address is already taken.", suggestion: "my-bakery-2" } } }
    : f.response(path, options));
  f.confirmed().props.onClick(); await flush();
  assert.deepEqual(f.calls.filter(c => c.options?.method).map(c => [c.options.method, c.path.split("/").pop()]), [["PUT", "site-address"]]);
  assert.match(f.text(), /already taken\. Try my-bakery-2/);
  f.handler(f.response);
  f.confirmed().props.onClick(); await flush();
  assert.deepEqual(f.calls.filter(c => c.options?.method).slice(-2).map(c => c.options.method), ["PUT", "POST"]);
  assert.deepEqual(JSON.parse(f.calls.filter(c => c.options?.method === "PUT").at(-1).options.body), { slug: "my-bakery" });
});
