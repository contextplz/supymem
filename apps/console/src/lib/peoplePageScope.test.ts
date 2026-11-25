import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";

// Shipped callbacks and scope projection; fake API never emails or changes membership.
const source = fs.readFileSync(new URL("../app/v/[ventureId]/people/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const component = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "PeoplePage")!;
const body = component.body!.statements.filter(node => !ts.isReturnStatement(node)).map(node => node.getText(tree)).join("\n");
const code = ts.transpileModule(`globalThis.page=function(){${body}\nreturn {load,invite,changeRole,remove,workspaceId,members,visibleMembers,visiblePlan,ready,inviting,msg,inviteLink,setEmail,email};};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;
const flush = () => new Promise<void>(done => setImmediate(done));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
const ok = (data: unknown) => ({ ok: true, status: 200, data });
function fixture() {
  let ventureId = "ven_a", subject: string | null = "usr_a", epoch = 1, cursor = 0;
  const slots: any[] = [], effects: (() => void)[] = [], listeners = new Map<string, () => void>();
  const calls: { path: string; options: any }[] = [];
  const response = async (path: string) => {
    if (path.startsWith("/api/ventures/")) return ok({ workspace_id: `wsp_${path.split("/").at(-1)}` });
    if (path.endsWith("/members")) return ok({ items: [{ id: `member_${path.split("/").at(-2)}`, role: "viewer", person_id: subject }, { id: "owner", role: "owner" }] });
    if (path.startsWith("/api/entitlement")) return ok({ tier: "free", seats_used: 2 });
    return ok({ link: "/owned-fixture-recovery", delivery: { status: "unconfirmed" } });
  };
  let handler: (path: string, options: any) => Promise<any> = response;
  const context = vm.createContext({
    String, JSON, Promise, Boolean, Array, encodeURIComponent, ROLES: ["admin", "maintainer", "contributor", "viewer"],
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
    ensureSession: async () => ({ ok: Boolean(subject), data: { authenticated: Boolean(subject), subject } }),
    verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
    serverReason: () => "Membership change refused",
    api: async (path: string, options: any) => { calls.push({ path, options }); return handler(path, options); },
    window: { addEventListener: (name: string, callback: () => void) => listeners.set(name, callback), removeEventListener: (name: string) => listeners.delete(name) },
  });
  vm.runInContext(code, context);
  const render = () => { cursor = 0; return context.page(); };
  const commit = () => { while (effects.length) effects.shift()!(); };
  return { render, commit, calls, response, setHandler: (value: typeof handler) => { handler = value; },
    navigate: (id: string) => { ventureId = id; },
    retire: (next: string | null) => { subject = next; ++epoch; listeners.get("supymem-session-changed")?.(); },
    mount: async () => { render(); commit(); await flush(); return render(); },
    mutations: () => calls.filter(call => call.options?.method),
  };
}

test("venture navigation hides old members and refuses current and retained old mutation callbacks", async () => {
  const f = fixture(), old = await f.mount(); old.setEmail("fixture@example.test"); const oldReady = f.render();
  assert.equal(oldReady.ready, true);
  f.navigate("ven_b"); const beforeCommit = f.render();
  assert.equal(beforeCommit.ready, false); assert.equal(beforeCommit.visibleMembers.length, 0); assert.equal(beforeCommit.visiblePlan, null);
  for (const view of [beforeCommit, oldReady]) {
    await view.invite(); await view.changeRole("member_wsp_ven_a", "admin"); await view.remove("member_wsp_ven_a");
  }
  assert.equal(f.mutations().length, 0);
  f.commit(); await flush(); const current = f.render(); current.setEmail("fixture@example.test"); await f.render().invite();
  assert.equal(f.mutations()[0].path, "/api/workspaces/wsp_ven_b/invites");
});

test("a delayed old venture load cannot replace freshly loaded current membership", async () => {
  const f = fixture(); await f.mount(); const old = deferred<any>();
  f.setHandler(async path => path === "/api/ventures/ven_a" ? old.promise : f.response(path));
  const pending = f.render().load(); await flush(); f.navigate("ven_b"); f.render(); f.commit(); await flush();
  old.resolve(ok({ workspace_id: "wsp_ven_a" })); await pending; await flush();
  const view = f.render(); assert.equal(view.workspaceId, "wsp_ven_b"); assert.equal(view.visibleMembers[0].id, "member_wsp_ven_b");
});

test("account retirement and ABA reject old decisions and never restore private recovery links", async () => {
  for (const owner of ["usr_b", "usr_a", null]) {
    const f = fixture(); await f.mount(); f.render().setEmail("fixture@example.test");
    const oldView = f.render(), old = deferred<any>();
    f.setHandler(async (path, options) => options?.method ? old.promise : f.response(path));
    const pending = oldView.invite(); await flush(); f.retire(owner); await flush();
    assert.equal(f.render().inviteLink, ""); assert.equal(f.render().email, "");
    const count = f.mutations().length;
    await oldView.invite(); await oldView.remove("member_wsp_ven_a");
    assert.equal(f.mutations().length, count);
    old.resolve(ok({ link: "/old-private-token", delivery: { status: "sent" } })); await pending; await flush();
    assert.equal(f.render().inviteLink, ""); assert.equal(f.render().msg, ""); assert.equal(f.render().inviting, false);
  }
});

test("an old account's delayed membership response is discarded after retirement", async () => {
  const f = fixture(); await f.mount(); const old = deferred<any>();
  f.setHandler(async path => path.endsWith("/members") ? old.promise : f.response(path));
  const pending = f.render().load(); await flush();
  f.setHandler(f.response); f.retire("usr_b"); await flush();
  old.resolve(ok({ items: [{ id: "retired-owner-secret", role: "viewer" }] })); await pending;
  assert.equal(f.render().visibleMembers.some((member: any) => member.id === "retired-owner-secret"), false);
});

test("concurrent invite callbacks dispatch only once and delivery uncertainty stays honest", async () => {
  const f = fixture(); await f.mount(); f.render().setEmail("fixture@example.test"); const view = f.render(), hold = deferred<any>();
  f.setHandler(async (path, options) => options?.method ? hold.promise : f.response(path));
  const first = view.invite(), second = view.invite(); await flush(); assert.equal(f.mutations().length, 1);
  hold.resolve(ok({ link: "/owned-recovery", delivery: { status: "unconfirmed" } })); await Promise.all([first, second]); await flush();
  assert.match(f.render().msg, /email delivery was not confirmed/); assert.doesNotMatch(f.render().msg, /Invite emailed/);
});

test("membership load failure refuses changes until exact current membership is refreshed", async () => {
  const f = fixture(); f.setHandler(async path => path.endsWith("/members") ? { ok: false, status: 503 } : f.response(path));
  const view = await f.mount(); view.setEmail("fixture@example.test"); await f.render().invite(); await f.render().remove("member_wsp_ven_a");
  assert.equal(f.render().ready, false); assert.equal(f.mutations().length, 0);
  f.setHandler(f.response); await f.render().load(); assert.equal(f.render().ready, true);
});

test("only loaded non-owner members and declared roles mutate; server refusal is not presented as saved", async () => {
  const f = fixture(), view = await f.mount();
  await view.remove("foreign-member"); await view.changeRole("owner", "admin"); await view.changeRole("member_wsp_ven_a", "owner");
  assert.equal(f.mutations().length, 0);
  f.setHandler(async (path, options) => options?.method ? { ok: false, status: 403 } : f.response(path));
  await view.changeRole("member_wsp_ven_a", "admin");
  assert.equal(f.mutations().length, 1); assert.equal(f.mutations()[0].path, "/api/members/member_wsp_ven_a");
  assert.equal(f.render().msg, "Membership change refused"); assert.equal(f.render().inviting, false);
});

test("current explicit role and removal controls remain callable after refreshed membership", async () => {
  const f = fixture(); await f.mount();
  await f.render().changeRole("member_wsp_ven_a", "admin"); await flush();
  await f.render().remove("member_wsp_ven_a"); await flush();
  assert.deepEqual(f.mutations().map(call => [call.path, call.options.method]), [
    ["/api/members/member_wsp_ven_a", "PATCH"], ["/api/members/member_wsp_ven_a", "DELETE"],
  ]);
  assert.deepEqual(JSON.parse(f.mutations()[0].options.body), { role: "admin" });
  assert.match(f.render().msg, /Member removal saved/);
});
