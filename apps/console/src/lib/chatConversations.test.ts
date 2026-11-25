import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { ChatActionPendingError } from "./chatConfirmError.ts";
import {chatConversationCatalog, chatConversationItem, chatRouteCurrent, conversationChatPath, conversationListPath, conversationResponseMatches, conversationSelectionKey, historyGroups, type ChatConversation} from "./chatConversations.ts";
import {chatFlight, waitForChatRequest } from "./chatStop.ts";
import {isClientChatAction} from "./chatPrefill.ts";

const item = (id: string, visibility: "private" | "legacy_shared" = "private") => ({id, venture_id: "ven_one", workspace_id: "wsp_one", visibility,
  title: "Saved chat", created_at: "2026-10-04T00:00:00Z", updated_at: "2026-10-04T00:00:00Z"});
const catalog = () => ({venture_id: "ven_one", workspace_id: "wsp_one", actor_id: "usr_one", legacy_conversation_id: "chat_legacy",
  items: [item("chat_legacy", "legacy_shared"), item("chat_private")]});
const plain = (value: unknown) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred<T>() {let resolve!: (value: T) => void; const promise = new Promise<T>(done => {resolve = done;}); return {promise, resolve};}

test("conversation catalogs bind actor/workspace/venture even when the empty legacy chat is omitted", () => {
  assert.ok(chatConversationCatalog(catalog(), "ven_one", "usr_one"));
  assert.ok(chatConversationCatalog({...catalog(), items: []}, "ven_one", "usr_one"));
  assert.ok(chatConversationCatalog({...catalog(), items: [item("chat_private")]}, "ven_one", "usr_one"));
  for (const changed of [{actor_id: "usr_other"}, {venture_id: "ven_other"}, {legacy_conversation_id: "guessed"},
    {items: [item("chat_legacy")]}, {items: [item("chat_legacy", "legacy_shared"), item("chat_legacy")]},
    {items: [item("chat_legacy", "legacy_shared"), {...item("chat_private"), workspace_id: "wsp_other"}]}]) {
    assert.equal(chatConversationCatalog({...catalog(), ...changed}, "ven_one", "usr_one"), null);
  }
  assert.equal(chatConversationItem({...item("saved"), id: "../other"}, "ven_one", "wsp_one"), null);
  for (const changed of [{pinned: "yes"}, {archived: 1}, {folder: {name: "Plans"}}]) {
    assert.equal(chatConversationItem({...item("saved"), ...changed}, "ven_one", "wsp_one"), null);
  }
  assert.ok(chatConversationCatalog({...catalog(), actor_id: "per_local000000"}, "ven_one", "founder"));
});

test("history groups pinned chats before folders and days, with current/archived list queries", async () => {
  const now = new Date(2026, 9, 23, 12), today = new Date(2026, 9, 23, 10).toISOString();
  const rows = [
    {id: "today", text: "Today", at: today},
    {id: "folder", text: "Plan", at: today, folder: "Plans"},
    {id: "pinned_folder", text: "Pinned plan", at: "2025-01-01", pinned: true, folder: "Plans"},
    {id: "yesterday", text: "Yesterday", at: new Date(2026, 9, 22, 10).toISOString()},
    {id: "missing", text: "Undated", at: "invalid"},
    {id: "folder_second", text: "Older plan", at: "2025-01-01", folder: "Plans"},
    {id: "folder_named_pinned", text: "Folder", at: today, folder: "Pinned"},
  ];
  const groups = historyGroups(rows, now);
  assert.deepEqual(groups.map(group => group.key), ["pinned", "folder:Pinned", "folder:Plans", "day:2026-9-23", "day:2026-9-22", "day:earlier"]);
  assert.deepEqual(groups.map(group => group.label), ["Pinned", "Pinned", "Plans", "Today", "Yesterday", "Earlier"]);
  assert.deepEqual(groups[0].items.map(row => row.id), ["pinned_folder"]);
  assert.deepEqual(groups[2].items.map(row => row.id), ["folder", "folder_second"]);
  assert.equal(groups[2].folder, true);
  assert.equal(groups.flatMap(group => group.items).length, rows.length);
  assert.equal(rows[0].id, "today");
  assert.equal(conversationListPath("ven_one"), "/api/ventures/ven_one/chat/conversations");
  assert.equal(conversationListPath("ven one", "chat/current", true), "/api/ventures/ven%20one/chat/conversations?current=chat%2Fcurrent&archived=1");
  const page = conversationsPage();
  page.storage.set(conversationSelectionKey("usr_one", "ven_one"), "chat_private");
  page.render("ven_one"); await flush();
  assert.equal(page.calls[0].path, "/api/ventures/ven_one/chat/conversations?current=chat_private");
  const view = page.render("ven_one"); await view.reloadConversations();
  assert.equal(page.calls.at(-1)!.path, "/api/ventures/ven_one/chat/conversations?current=chat_private");
  await view.reloadConversations(true);
  assert.equal(page.calls.at(-1)!.path, "/api/ventures/ven_one/chat/conversations?current=chat_private&archived=1");
  assert.equal(page.render("ven_one").conversationId, "chat_private");
});

test("conversation paths/receipts and live routes never borrow another conversation", () => {
  assert.equal(conversationChatPath("ven_one", "chat_private", "msg_old"), "/api/ventures/ven_one/chat?conversation_id=chat_private&before=msg_old");
  assert.equal(conversationChatPath("ven_one", null), "/api/ventures/ven_one/chat");
  assert.equal(conversationResponseMatches({conversation_id: "chat_private"}, "chat_private"), true);
  assert.equal(conversationResponseMatches({conversation_id: "chat_other"}, "chat_private"), false);
  assert.equal(conversationResponseMatches({}, "chat_private"), false);
  assert.equal(conversationResponseMatches({}, null), true);
  assert.equal(chatRouteCurrent("ven_one", "/v/ven_one/builds", "/v/ven_one/settings"), true);
  assert.equal(chatRouteCurrent("ven_one", "/v/ven_one/builds", "/v/ven_other/builds"), false);
  assert.equal(chatRouteCurrent("ven_one", "/new", "/new"), true);
  assert.equal(chatRouteCurrent("ven_one", "/new", "/company"), false);
  assert.equal(chatRouteCurrent("ven_one", "/connections", "/connections", "?workspace=wsp_one&venture=ven_one", "?workspace=wsp_other&venture=ven_one"), false);
  assert.equal(chatRouteCurrent("ven_one", "/connections", "/connections", "?venture=ven_one", "?venture=ven_other"), false);
  assert.notEqual(conversationSelectionKey("usr_one", "ven_one"), conversationSelectionKey("usr_other", "ven_one"));
});

function functions(file: string, names: string[]) {
  const source = fs.readFileSync(new URL(file, import.meta.url), "utf8"), tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  return names.map(name => {
    const declaration = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === name);
    assert.ok(declaration, name); return declaration.getText(tree).replace(/^export /, "");
  }).join("\n");
}

function hookHarness(code: string, name: string, globals: Record<string, unknown>) {
  const slots: any[] = [], effects: Array<() => void> = [], cleanups: (() => void)[] = [];
  let cursor = 0;
  const same = (a?: unknown[], b?: unknown[]) => !!a && !!b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const context = vm.createContext({...globals,
    useActivityRefresh: () => null, isLocalConsole: () => false,
    useRef: (value: unknown) => {const at = cursor++; return slots[at] ||= {current: value};},
    useState: (initial: unknown) => {const at = cursor++; if (!(at in slots)) slots[at] = typeof initial === "function" ? (initial as () => unknown)() : initial;
      return [slots[at], (next: any) => {slots[at] = typeof next === "function" ? next(slots[at]) : next;}];},
    useCallback: (callback: unknown, deps: unknown[]) => {const at = cursor++; if (!slots[at] || !same(slots[at].deps, deps)) slots[at] = {callback, deps}; return slots[at].callback;},
    useEffect: (effect: () => (() => void) | void, deps?: unknown[]) => {const at = cursor++, previous = slots[at];
      if (!previous || !same(previous.deps, deps)) {slots[at] = {deps}; effects.push(() => {previous?.cleanup?.(); const cleanup = effect(); if (cleanup) {slots[at].cleanup = cleanup; cleanups.push(cleanup);}});}},
  });
  vm.runInContext(ts.transpileModule(`${code}\nglobalThis.actualHook=${name};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
  return {render: (...args: unknown[]) => {cursor = 0; const result = context.actualHook(...args); for (const run of effects.splice(0)) run(); return result;},
    unmount: () => cleanups.forEach(cleanup => cleanup()), context};
}

function conversationsPage() {
  const owner = {actor: "usr_one" as string | null, epoch: 1};
  const listeners = new Map<string, Set<() => void>>(), storage = new Map<string, string>();
  const location = {pathname: "/v/ven_one/builds", search: ""};
  let workspace = "wsp_one";
  const calls: Array<{path: string; method?: string; body?: Record<string, unknown>}> = [];
  let apiRead = async (path: string, init?: {body?: string}) => {
    const saved = catalog();
    if (new URLSearchParams(path.split("?")[1]).get("current") === "chat_new") saved.items.push(item("chat_new"));
    return {ok: true, status: 200, data: init ? item("chat_new") : saved};
  };
  const hook = hookHarness(functions("../components/chat/useChatConversations.ts", ["useChatConversations"]), "useChatConversations", {
    chatConversationCatalog, chatConversationItem, chatRouteCurrent, conversationListPath, conversationSelectionKey, JSON, encodeURIComponent, crypto,
    URLSearchParams, AbortController, setTimeout: () => 0, clearTimeout: () => {}, document: {hidden: false, addEventListener() {}, removeEventListener() {}},
    isCompanyOperationsPath: (path: string) => /^\/(connections|gtm)(\/|$)/.test(path), getOpsWorkspace: () => workspace,
    window: {location, sessionStorage: {getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => {storage.set(key, value);}},
      addEventListener: (type: string, callback: () => void) => {if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type)!.add(callback);},
      removeEventListener: (type: string, callback: () => void) => listeners.get(type)?.delete(callback)},
    verifiedSessionSubject: () => owner.actor, verifiedSessionEpoch: () => owner.epoch, ensureSession: async () => ({ok: true}),
    api: async (path: string, init?: {method?: string; body?: string}) => {calls.push({path, ...(init?.method ? {method: init.method} : {}), ...(init?.body ? {body: JSON.parse(init.body)} : {})}); return apiRead(path, init);},
    apiLive: async (path: string) => {calls.push({path}); return apiRead(path);},
  });
  return {...hook, owner, location, calls, storage, setWorkspace: (value: string) => {workspace = value;}, setApi: (next: typeof apiRead) => {apiRead = next;},
    retire: () => {owner.epoch++; for (const listener of listeners.get("supymem-session-changed") || []) listener();}};
}

async function loadedConversations() {const page = conversationsPage(); page.render("ven_one"); await flush(); return {page, view: page.render("ven_one")};}

test("first session/catalog completion preserves the dock key; selecting another chat remounts it", async () => {
  const page = conversationsPage(), history = deferred<any>();
  page.owner.actor = null;
  page.setApi(async () => history.promise);
  const initial = page.render("ven_one");
  page.owner.actor = "usr_one"; page.retire();
  assert.equal(page.render("ven_one").conversationKey, initial.conversationKey);
  page.storage.set(conversationSelectionKey("usr_one", "ven_one"), "chat_private");
  history.resolve({ok: true, status: 200, data: catalog()}); await flush();
  const loaded = page.render("ven_one");
  assert.equal(loaded.conversationKey, initial.conversationKey);
  assert.equal(loaded.conversationDraftId, "chat_private");
  loaded.selectConversation("chat_legacy");
  assert.notEqual(page.render("ven_one").conversationKey, initial.conversationKey);
  page.owner.actor = "usr_other"; page.retire();
  assert.notEqual(page.render("ven_one").conversationKey, loaded.conversationKey);
});

test("actual New Chat creates one actor-scoped private record, selects returned ID and never queues work", async () => {
  const {page, view} = await loadedConversations();
  assert.equal(view.conversationId, "chat_legacy");
  const pending = view.newConversation(); view.newConversation();
  assert.equal(await pending, true);
  const next = page.render("ven_one");
  assert.equal(next.conversationId, "chat_new"); assert.equal(next.conversationDraftId, "chat_new");
  assert.equal(next.conversationItems.length, 3);
  const posts = page.calls.filter(call => call.body);
  assert.equal(posts.length, 1); assert.equal(posts[0].path, "/api/ventures/ven_one/chat/conversations");
  assert.deepEqual(Object.keys(posts[0].body!), ["idempotency_key"]);
  assert.equal(page.storage.get(conversationSelectionKey("usr_one", "ven_one")), "chat_new");
});

test("new chats refresh with current so earlier empty chats disappear", async () => {
  const page = conversationsPage();
  let rows = [item("chat_legacy", "legacy_shared"), {...item("empty_old"), title: "New chat"}];
  page.storage.set(conversationSelectionKey("usr_one", "ven_one"), "empty_old");
  page.setApi(async (path, init) => {
    if (init?.body) {
      const created = {...item(`empty_${rows.length}`), title: "New chat"}; rows.push(created);
      return {ok: true, status: 200, data: created};
    }
    const current = new URL(path, "https://console.test").searchParams.get("current");
    return {ok: true, status: 200, data: {...catalog(), items: rows.filter(row => row.title !== "New chat" || row.id === current)}};
  });
  page.render("ven_one"); await flush();
  for (let attempt = 0; attempt < 2; attempt++) {
    assert.equal(await page.render("ven_one").newConversation(), true);
    const view = page.render("ven_one");
    assert.deepEqual(plain(view.conversationItems.filter((row: ChatConversation) => row.title === "New chat").map((row: ChatConversation) => row.id)), [view.conversationId]);
    assert.ok(page.calls.at(-1)!.path.endsWith(`?current=${view.conversationId}`));
  }
});

test("chat PATCH refreshes metadata and archives without retiring the open chat; stale lists cannot undo it", async () => {
  const {page, view} = await loadedConversations();
  view.selectConversation("chat_private");
  let rows = catalog().items.map(row => ({...row, pinned: false, archived: false, folder: null as string | null}));
  page.setApi(async (path, init) => {
    if (init?.body) {
      const patch = JSON.parse(init.body); rows = rows.map(row => path.endsWith(`/${row.id}`) ? {...row, ...patch} : row);
      return {ok: true, status: 200, data: null as any};
    }
    const archived = new URL(path, "https://console.test").searchParams.get("archived") === "1";
    return {ok: true, status: 200, data: {...catalog(), items: rows.filter(row => row.archived === archived)}};
  });
  let selected = page.render("ven_one");
  assert.equal(await selected.updateConversation("chat_private", {title: "Plan", pinned: true, folder: "Plans"}), true);
  const patch = page.calls.find(call => call.method === "PATCH")!;
  assert.equal(patch.path, "/api/ventures/ven_one/chat/conversations/chat_private");
  assert.deepEqual(patch.body, {title: "Plan", pinned: true, folder: "Plans"});
  selected = page.render("ven_one");
  assert.equal(selected.conversationItems.find((row: ChatConversation) => row.id === "chat_private")!.title, "Plan");
  const key = selected.conversationKey;
  assert.equal(await selected.updateConversation("chat_private", {archived: true}), true);
  selected = page.render("ven_one");
  assert.equal(selected.conversationId, "chat_private"); assert.equal(selected.conversationDraftId, "chat_private");
  assert.equal(selected.conversationKey, key);
  assert.deepEqual(plain(selected.conversationItems.map((row: ChatConversation) => row.id)), ["chat_legacy"]);
  assert.deepEqual(plain(selected.archivedConversationItems.map((row: ChatConversation) => row.id)), ["chat_private"]);
  selected.selectConversation("chat_legacy"); selected = page.render("ven_one"); selected.selectConversation("chat_private");
  selected = page.render("ven_one"); assert.equal(selected.conversationDraftId, "chat_private");
  assert.equal(await selected.updateConversation("chat_private", {archived: false, folder: ""}), true);
  selected = page.render("ven_one"); assert.equal(selected.archivedConversationItems.length, 0);
  assert.equal(selected.conversationItems.find((row: ChatConversation) => row.id === "chat_private")!.folder, null);
  const delayed = deferred<any>();
  page.setApi(async () => delayed.promise); const old = selected.reloadConversations();
  page.setApi(async () => ({ok: true, status: 200, data: {...catalog(), items: rows.map(row => ({...row, title: "Newest"}))}}));
  await selected.reloadConversations();
  delayed.resolve({ok: true, status: 200, data: catalog()}); await old;
  assert.equal(page.render("ven_one").conversationItems.find((row: ChatConversation) => row.id === "chat_private")!.title, "Newest");
});

test("actual catalog and retained New Chat/selection callbacks refuse retired actors, ABA and live routes", async () => {
  for (const kind of ["actor", "ABA", "route", "unmount"] as const) {
    const {page, view} = await loadedConversations(), before = page.calls.length;
    if (kind === "actor") page.owner.actor = "usr_other";
    if (kind === "ABA") page.retire();
    if (kind === "route") page.location.pathname = "/v/ven_other/settings";
    if (kind === "unmount") page.unmount();
    assert.equal(await view.newConversation(), false); assert.equal(await view.updateConversation("chat_private", {archived: true}), false);
    view.selectConversation("chat_private"); await view.reloadConversations();
    assert.equal(page.calls.length, before);
  }
});

test("actual foreign catalog, private creation receipt and unavailable legacy Core never grant a guessed chat", async () => {
  const page = conversationsPage();
  page.setApi(async () => ({ok: true, status: 200, data: {...catalog(), actor_id: "usr_other"}}));
  page.render("ven_one"); await flush();
  let view = page.render("ven_one"); assert.equal(view.conversationReady, false); assert.equal(view.conversationsAvailable, false);
  page.setApi(async () => ({ok: false, status: 404, data: null as any}));
  await view.reloadConversations(); view = page.render("ven_one");
  assert.equal(view.conversationReady, true); assert.equal(view.conversationId, null); assert.equal(await view.newConversation(), false);
  const current = await loadedConversations();
  current.page.setApi(async () => ({ok: true, status: 200, data: {...item("foreign"), venture_id: "ven_other"} as any}));
  assert.equal(await current.view.newConversation(), false);
  assert.equal(current.page.render("ven_one").conversationId, "chat_legacy");
});

test("actual delayed catalog cannot switch back after selection and failed creation retries use the same request key", async () => {
  const {page, view} = await loadedConversations(), delayed = deferred<any>();
  page.setApi(async () => delayed.promise);
  const refresh = view.reloadConversations(); view.selectConversation("chat_private");
  delayed.resolve({ok: true, status: 200, data: catalog()}); await refresh;
  let selected = page.render("ven_one"); assert.equal(selected.conversationId, "chat_private");
  page.setApi(async () => ({ok: false, status: 0, data: null as any}));
  assert.equal(await selected.newConversation(), false); selected = page.render("ven_one");
  page.setApi(async () => ({ok: true, status: 200, data: item("confirmed_new")}));
  assert.equal(await selected.newConversation(), true);
  const posts = page.calls.filter(call => call.body);
  assert.equal(posts.length, 2); assert.equal(posts[0].body!.idempotency_key, posts[1].body!.idempotency_key);
});

test("actual delayed creation cannot take over a newer selection or account", async () => {
  for (const retired of ["selection", "actor"] as const) {
    const {page, view} = await loadedConversations(), delayed = deferred<any>();
    page.setApi(async () => delayed.promise); const pending = view.newConversation();
    if (retired === "selection") view.selectConversation("chat_private"); else page.retire();
    delayed.resolve({ok: true, status: 200, data: item("old_new")});
    assert.equal(await pending, false);
    const shown = page.render("ven_one");
    assert.notEqual(shown.conversationId, "old_new");
    if (retired === "selection") {assert.equal(shown.conversationId, "chat_private"); assert.equal(shown.conversationBusy, false);}
  }
});

test("actual unknown owner never reads chats, and an old catalog cannot expose private items after retirement", async () => {
  const unknown = conversationsPage(); unknown.owner.actor = null;
  const empty = unknown.render("ven_one"); await flush();
  assert.equal(unknown.calls.length, 0); assert.equal(await empty.newConversation(), false);
  const page = conversationsPage(), delayed = deferred<any>();
  page.setApi(async () => delayed.promise); page.render("ven_one"); page.retire(); page.owner.actor = "usr_other";
  delayed.resolve({ok: true, status: 200, data: catalog()}); await flush();
  assert.deepEqual(plain(page.render("ven_one").conversationItems), []);
});

test("actual company chat refuses changed live query and workspace selection before rerender", async () => {
  for (const kind of ["query", "workspace"] as const) {
    const page = conversationsPage(); page.location.pathname = "/connections"; page.location.search = "?workspace=wsp_one&venture=ven_one";
    page.render("ven_one"); await flush(); const old = page.render("ven_one"), count = page.calls.length;
    if (kind === "query") page.location.search = "?workspace=wsp_one&venture=ven_other"; else page.setWorkspace("wsp_other");
    assert.equal(await old.newConversation(), false); await old.reloadConversations(); old.selectConversation("chat_private");
    assert.equal(page.calls.length, count);
  }
  const unknown = conversationsPage(); unknown.location.pathname = "/connections"; unknown.setWorkspace("");
  unknown.render("ven_one"); await flush(); assert.equal(unknown.calls.length, 0);
});

test("shipped launcher takes New chat and saved selection from the sidebar without sending, and retires composer state by scope", async () => {
  const source = fs.readFileSync(new URL("../components/chat/VentureChatLauncher.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("VentureChatLauncher.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = tree.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(tree).replace(/^export /, "")).join("\n");
  const historyHook = functions("../components/chat/useChatHistory.ts", ["useChatHistory"]);
  const calls: unknown[] = [], elements: any[] = [], published: any[] = [], listeners = new Map<string, (event: any) => void>();
  const chat = {managerName: "Charlie", conversationsAvailable: true, conversationBusy: false, conversationId: "opaque_saved" as string | null, conversationKey: "scope_one",
    conversationDraftId: "opaque_saved", conversationItems: [item("opaque_saved")], archivedConversationItems: [], conversationError: "", newConversation: () => calls.push("new"), messages: [] as unknown[],
    selectConversation: (id: string) => calls.push(id), sendChat: () => calls.push("send"), reloadConversations: async () => {}, reloadHistory: () => calls.push("reload"), historyReady: true};
  const context = vm.createContext({useVentureChat: () => chat, isLocalConsole: () => false, useEffect: (effect: () => void) => effect(), useState: (value: unknown) => [value, () => {}], useRef: (current: unknown) => ({current}), PREFILL_ACTION: "prefill", styles: {},
    useContext: (context: {value: string}) => context.value, FounderUIContext: {value: "home"},
    CustomEvent: class { type: string; detail: unknown; constructor(type: string, init?: {detail?: unknown}) { this.type = type; this.detail = init?.detail; } },
    window: {addEventListener: (name: string, listener: (event: any) => void) => listeners.set(name, listener), removeEventListener() {}, dispatchEvent: (event: any) => published.push(event)},
    CharlieMascot: "mascot", ChatDock: "dock", ErrorNotice: "error", React: {createElement: (type: unknown, props: any, ...children: unknown[]) => {
      const element = {type, props: props || {}, children}; elements.push(element); return element;
    }}});
  vm.runInContext(ts.transpileModule(`${historyHook}\n${body}\nglobalThis.launcher=VentureChatLauncher;`, {compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText, context);
  const render = () => {elements.length = 0; context.launcher({ventureId: "ven_one", size: {width: 400, height: 500}, onSizeChange() {}, onMinimize() {}});};
  render(); assert.deepEqual(calls, []);
  assert.equal(published.find(event => event.type === "supymem:chat-history").detail.current, "opaque_saved");
  listeners.get("supymem:chat-new")!({}); assert.deepEqual(calls, ["new"]);
  listeners.get("supymem:chat-jump")!({detail: {id: "opaque_saved"}});
  assert.deepEqual(calls, ["new", "opaque_saved"]);
  const firstDock = elements.find(element => element.type === "dock");
  assert.equal(firstDock.props.key, "scope_one"); assert.equal(firstDock.props.conversationId, "opaque_saved");
  assert.equal(firstDock.props.voiceConversationId, "opaque_saved"); assert.equal(firstDock.props.onReloadHistory, chat.reloadHistory);
  chat.conversationKey = "scope_two"; chat.conversationDraftId = "waiting_for_catalog"; chat.conversationId = null; chat.conversationsAvailable = false; render();
  assert.equal(elements.find(element => element.type === "dock").props.key, "scope_two");
  assert.equal(elements.find(element => element.type === "dock").props.voiceConversationId, null);
  assert.equal(elements.some(element => element.type === "select"), false);
  listeners.get("supymem:chat-new")!({}); assert.deepEqual(calls, ["new", "opaque_saved"]);
  assert.equal(calls.filter(call => call === "send").length, 0);
});

test("venture voice forwards the admitted chat and saved-history refresh through the dock and mic, while entry keeps preview defaults", async () => {
  const dockSource = fs.readFileSync(new URL("../components/chat/ChatDock.tsx", import.meta.url), "utf8");
  const dockTree = ts.createSourceFile("ChatDock.tsx", dockSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let micElement: ts.JsxSelfClosingElement | undefined;
  function visit(node: ts.Node) {
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(dockTree) === "VoiceMic") micElement = node;
    ts.forEachChild(node, visit);
  }
  visit(dockTree); assert.ok(micElement);
  const sends: string[] = [], reply = {body_md: "Preview saved"}, calls: any[] = [];
  let reloads = 0;
  const onVoiceSend = async (text: string) => {sends.push(text); return reply;}, onReloadHistory = () => {reloads++;};
  const dock = vm.createContext({ventureId: "ven_one", conversationId: "synthetic_draft", voiceConversationId: "chat_admitted", blocked: false, visible: true, homePanel: true, pathname: "/new", activeQuestion: null, voiceScreen: undefined, voiceWaiting: false, preparedActions: [],
    onVoiceSend, onReloadHistory, VoiceMic: "mic", React: {createElement: (type: unknown, props: unknown) => ({type, props})}});
  vm.runInContext(ts.transpileModule(`globalThis.micElement=${micElement.getText(dockTree)};`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText, dock);
  assert.equal(dock.micElement.props.conversationId, "chat_admitted");
  assert.equal(dock.micElement.props.active, true);
  assert.equal(dock.micElement.props.onToolsComplete, onReloadHistory);
  const micSource = fs.readFileSync(new URL("../components/chat/VoiceMic.tsx", import.meta.url), "utf8");
  const micTree = ts.createSourceFile("VoiceMic.tsx", micSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const component = micTree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "VoiceMic");
  assert.ok(component);
  const mic = vm.createContext({isDesktopShell: () => false, useVoiceChat: (...args: any[]) => {calls.push(args); return {phase: "idle", enabled: false};},
    useRef: (current: unknown) => ({current}), useCallback: (callback: unknown) => callback, useEffect() {}, useVoiceKeys() {}});
  vm.runInContext(ts.transpileModule(`${component.getText(micTree).replace(/^export /, "")}\nglobalThis.mic=VoiceMic;`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React},
  }).outputText, mic);
  mic.mic(dock.micElement.props);
  assert.equal(calls[0][0], "ven_one"); assert.equal(calls[0][1], onVoiceSend);
  assert.equal(calls[0][2].conversationId, "chat_admitted");
  assert.equal(calls[0][2].active, true);
  assert.equal(calls[0][2].autoStart, undefined);
  assert.equal(calls[0][2].microphoneGranted, undefined);
  assert.equal(typeof dock.micElement.props.onTypeInstead, "function");
  calls[0][2].onToolsComplete(); assert.equal(reloads, 1);
  assert.equal(await calls[0][1]("Change my direction"), reply); assert.deepEqual(sends, ["Change my direction"]);
  mic.mic({ventureId: null, onVoiceSend});
  assert.equal(calls[1][0], null); assert.equal(calls[1][1], onVoiceSend);
  assert.equal(calls[1][2].conversationId, undefined); assert.equal(calls[1][2].onToolsComplete, undefined);
});

function ventureChat() {
  const active = {id: "chat_private", version: 1, actor: "usr_one", epoch: 1, route: "/v/ven_one/builds", ready: true};
  const requests: Array<{path: string; body?: Record<string, unknown>}> = [], navigations: string[] = [];
  const timers: Array<() => unknown> = [];
  const requestStorage = new Map<string, string>();
  let response = async (_path: string, init?: {body?: string}) => ({ok: true, status: 200, data: init ?
    {id: "reply_current", role: "assistant", body_md: "Saved reply", conversation_id: active.id} :
    {items: [{id: "saved_current", role: "assistant", body_md: "Saved history"}], next_cursor: null, conversation_id: active.id}});
  const router = {push: (href: string) => navigations.push(href)};
  const hook = hookHarness(functions("../components/chat/useVentureChat.ts", ["chatPayloadDigest", "sameVentureScreen", "useChatScopeState", "latestManagerName", "deepAnswerProgress", "startRequestedToolBuild", "useVentureChat"]), "useVentureChat", {
    JSON, crypto, TextEncoder, encodeURIComponent, conversationChatPath, conversationResponseMatches, setTimeout, chatFlight, waitForChatRequest, AbortSignal, AbortController, isClientChatAction, ChatActionPendingError,
    URL, VENTURE_SCREENS: new Set(["", "idea", "now", "tasks", "org", "site", "research", "market", "feedback", "charter", "plan", "builds", "analytics", "import", "evolution", "people", "connections", "settings"]),
    useRouter: () => router, verifiedSessionSubject: () => active.actor, verifiedSessionEpoch: () => active.epoch,
    useChatConversations: () => {
      const actor = active.actor, epoch = active.epoch;
      return {contextCurrent: () => active.actor === actor && active.epoch === epoch && active.route.startsWith("/v/ven_one/"),
        conversationId: active.id, conversationVersion: active.version, conversationReady: active.ready,
        conversationCurrent: (id: string, version: number) => active.actor === actor && active.epoch === epoch && id === active.id && version === active.version && active.route.startsWith("/v/ven_one/"),
      };
    },
    ensureSession: async () => ({ok: true}), api: async (path: string, init?: {body?: string}) => {requests.push({path, ...(init?.body ? {body: JSON.parse(init.body)} : {})}); return response(path, init);},
    apiLive: async (path: string) => {requests.push({path}); return response(path);},
    parseChatItems: (data: any) => data.items, parseEmployeeStamp: (text: string) => ({text}), withLocalMacTaskObservation: async (message: unknown) => message,
    mergeChat: (old: any[], incoming: any[]) => [...old, ...incoming.filter(row => !old.some(item => item.id === row.id))],
    ideaPollStart: () => null, shouldAppendMacTasksHint: () => false, bareMacTaskConfirmation: () => false, browserTaskRequest: () => false, reviewableDeviceRequest: () => false,
    window: {localStorage: {getItem: (key: string) => requestStorage.get(key) ?? null,
        setItem: (key: string, value: string) => {requestStorage.set(key, value);}, removeItem: (key: string) => {requestStorage.delete(key);}},
      location: {pathname: "/v/ven_one/builds", origin: "https://app.supymem.com"}, setInterval: (fn: () => unknown) => {timers.push(fn); return timers.length;}, clearInterval: () => undefined,
      setTimeout: (fn: () => unknown) => {timers.push(fn); return timers.length;}, clearTimeout: () => undefined,
      addEventListener: () => undefined, removeEventListener: () => undefined, dispatchEvent: () => undefined},
    document: {hidden: false, addEventListener: () => undefined, removeEventListener: () => undefined},
    CustomEvent: class {constructor(_type: string, _data: unknown) {}}, failedSince: () => false,
  });
  return {...hook, active, requests, timers, navigations, setApi: (next: typeof response) => {response = next;}};
}

test("actual chat history/send/confirm carry the selected saved ID, and New Chat renders empty until its own history", async () => {
  const page = ventureChat(); page.render("ven_one"); await flush();
  let chat = page.render("ven_one"); assert.equal(chat.messages[0].body_md, "Saved history");
  assert.equal(await chat.sendChat("Read saved state only"), true);
  assert.equal(await chat.confirmChatAction("blocker.explain", {code: "safe"}), true);
  assert.deepEqual(page.requests.filter(call => call.body).map(call => [call.path, call.body!.conversation_id]), [
    ["/api/ventures/ven_one/chat", "chat_private"], ["/api/ventures/ven_one/chat/confirm", "chat_private"],
  ]);
  page.active.id = "new_empty"; page.active.version++;
  const delayed = deferred<any>(); page.setApi(async () => delayed.promise);
  chat = page.render("ven_one"); assert.deepEqual(plain(chat.messages), []); assert.equal(chat.thinking, false);
  delayed.resolve({ok: true, status: 200, data: {items: [], next_cursor: null, conversation_id: "new_empty"}}); await flush();
  chat = page.render("ven_one"); assert.deepEqual(plain(chat.messages), []);
  assert.ok(page.requests.some(call => call.path === "/api/ventures/ven_one/chat?conversation_id=new_empty"));
});

test("actual retired send/confirm/poll callbacks do no new IO and delayed replies cannot enter a newer chat", async () => {
  const page = ventureChat(); page.render("ven_one"); await flush(); const old = page.render("ven_one");
  const delayed = deferred<any>(); page.setApi(async () => delayed.promise);
  const pending = old.sendChat("Old chat request");
  page.active.id = "new_chat"; page.active.version++;
  const before = page.requests.length;
  assert.equal(await old.sendChat("must refuse"), false); assert.equal(await old.confirmChatAction("task.run", {id: "task"}), false);
  for (const poll of [...page.timers]) await poll(); assert.equal(page.requests.length, before);
  delayed.resolve({ok: true, status: 200, data: {id: "old_reply", role: "assistant", body_md: "Private old reply", conversation_id: "chat_private"}});
  assert.equal(await pending, false);
  page.setApi(async () => ({ok: true, status: 200, data: {items: [], next_cursor: null, conversation_id: "new_chat"}}));
  const next = page.render("ven_one"); assert.deepEqual(plain(next.messages), []); await flush();
  assert.equal(page.render("ven_one").messages.some((row: any) => row.id === "old_reply"), false);
});

test("actual foreign conversation reply and actor/route retirement never become a confirmation or navigation", async () => {
  const page = ventureChat(); page.render("ven_one"); await flush(); const chat = page.render("ven_one");
  page.setApi(async () => ({ok: true, status: 200, data: {id: "foreign", role: "assistant", body_md: "Foreign reply", conversation_id: "other_chat"}}));
  assert.equal(await chat.sendChat("hello"), false);
  assert.equal(page.render("ven_one").messages.some((row: any) => row.id === "foreign"), false);
  for (const retire of [() => {page.active.epoch += 2;}, () => {page.active.route = "/v/ven_other/builds";}]) {
    const before = page.requests.length; retire(); assert.equal(await chat.sendChat("no effect"), false); assert.equal(page.requests.length, before);
  }
  assert.deepEqual(page.navigations, []);
});

test("queued alternative confirmation returns promptly and polling shows its saved result without replay", async () => {
  const page = ventureChat(); page.render("ven_one"); await flush();
  page.render("ven_one"); await flush();
  const chat = page.render("ven_one");
  page.setApi(async (_path, init) => ({ok: true, status: 200, data: init
    ? {id: "recovery_pending", role: "assistant", kind: "recovery_pending", body_md: "Preparing another way.", conversation_id: page.active.id}
    : {items: [{id: "recovery_result", role: "assistant", body_md: "Your saved alternative is ready."}], next_cursor: null, conversation_id: page.active.id}}));
  await chat.confirmChatAction("recovery.alternative", {step: "idea.clarify_turn"});
  page.render("ven_one");
  assert.equal(page.render("ven_one").thinking, false);
  for (const poll of [...page.timers]) await poll();
  await flush();
  const messages = page.render("ven_one").messages;
  assert.ok(messages.some((row: any) => row.id === "recovery_result"));
  assert.equal(messages.some((row: any) => /couldn't reach/i.test(row.body_md)), false);
  assert.equal(page.requests.filter(request => request.path.endsWith("/chat/confirm")).length, 1);
});
