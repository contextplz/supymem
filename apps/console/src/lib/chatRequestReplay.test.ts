import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import {webcrypto} from "node:crypto";
import {canStartSend, clearChatDraftIfMatches, draftAfterFailedSend, writeChatDraft} from "./chatComposer.ts";
import {chatFlight} from "./chatStop.ts";
import {mergeChat} from "./chatMerge.ts";
import {apiErrorCode, apiFailure} from "./apiFailure.ts";
import {activeMacTaskReviewPath} from "./macTaskPrepareFailure.ts";
import {AI_SETTINGS_HREF, usageLimitMessage} from "./credits.ts";

const source = fs.readFileSync(new URL("../components/chat/useVentureChat.ts", import.meta.url), "utf8");
const tree = ts.createSourceFile("chat", source, ts.ScriptTarget.Latest, true);
let callback = "";
const checkpointCallbacks: Record<string, string> = {};
function visit(node: ts.Node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "sendChat" && node.initializer && ts.isCallExpression(node.initializer)) callback = node.initializer.arguments[0].getText(tree);
  if (ts.isVariableDeclaration(node) && ["resolveCheckpoint", "showCheckpoint"].includes(node.name.getText(tree)) && node.initializer && ts.isCallExpression(node.initializer)) checkpointCallbacks[node.name.getText(tree)] = node.initializer.arguments[0].getText(tree);
  ts.forEachChild(node, visit);
}
visit(tree);
assert.ok(callback);
const sendHelpers = ["startRequestedToolBuild", "latestManagerName", "sendRefusal", "chatPayloadDigest"].map(name => {
  const declaration = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(declaration, name);
  return declaration.getText(tree);
}).join("\n");

function harness(replies: Array<unknown | (() => Promise<unknown>)>, storage = new Map<string, string>(), actor = "usr_owned", cid = "cnv_owned",
  storageFailure?: "get" | "set" | "remove") {
  let active = true, nonce = 0;
  let messages: Array<Record<string, unknown>> = [];
  const calls: Array<Record<string, unknown>> = [];
  const thinking: boolean[] = [];
  let onCall = () => {};
  const firstCall = new Promise<void>(resolve => {onCall = resolve;});
  const chatRequestRef: {current: {scope: string; attempt: {payload: string; key: string; inFlight: boolean; uncertain: boolean} | null}} = {current: {scope: "owned", attempt: null}};
  const checkpointScope = JSON.stringify([actor, "ven_owned", cid]);
  const send = vm.runInNewContext(ts.transpileModule(`${sendHelpers}; const resolveCheckpoint = ${checkpointCallbacks.resolveCheckpoint}; const showCheckpoint = ${checkpointCallbacks.showCheckpoint}; (${callback})`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, {
    checkpointScope, checkpointKey: `supymem.chatRequest.v1:${encodeURIComponent(checkpointScope)}`,
    current: () => active, conversationId: cid, ventureId: "ven_owned", chatRequestRef, flight: {current: chatFlight()},
    verifiedSessionSubject: () => actor, TextEncoder, apiErrorCode, apiFailure, activeMacTaskReviewPath, AI_SETTINGS_HREF, usageLimitMessage,
    shouldAppendMacTasksHint: () => false, bareMacTaskConfirmation: () => false, browserTaskRequest: () => false, reviewableDeviceRequest: () => false,
    crypto: {randomUUID: () => `${actor}-${cid}-nonce-${++nonce}`, subtle: webcrypto.subtle},
    setMessages: (update: (old: Array<Record<string, unknown>>) => Array<Record<string, unknown>>) => {if (active) messages = update(messages);},
    setThinking: (value: boolean) => {if (active) thinking.push(value);},
    api: async (_path: string, init?: {body: string}) => {if (!init) return {ok: true, data: {state: "unknown"}}; calls.push(JSON.parse(init.body)); onCall(); const reply = replies.shift(); return typeof reply === "function" ? await reply() : reply;},
    conversationResponseMatches: (data: {conversation_id: string}) => data?.conversation_id === cid,
    withLocalMacTaskObservation: async (data: unknown) => data, mergeChat,
    parseEmployeeStamp: (text: string) => ({text}),
    window: {setTimeout: () => {}, localStorage: {
      getItem: (key: string) => {if (storageFailure === "get") throw new Error("denied"); return storage.get(key) ?? null;},
      setItem: (key: string, value: string) => {if (storageFailure === "set") throw new Error("denied"); storage.set(key, value);},
      removeItem: (key: string) => {if (storageFailure === "remove") throw new Error("denied"); storage.delete(key);},
    }}, load: async () => [], navigateOnResponse: false,
    startIdeaPoll: () => {},
  }) as (body: string, opts?: unknown) => Promise<boolean>;
  return {send, calls, firstCall, thinking, ref: chatRequestRef, storage, messages: () => messages, retire: () => {active = false;}};
}
const completed = {ok: true, status: 200, data: {id: "msg_saved", conversation_id: "cnv_owned", role: "assistant", body_md: "Whole saved answer."}};
async function dispatched(h: ReturnType<typeof harness>) {
  await h.firstCall; // event-driven: async hashing duration varies with load
  assert.equal(h.calls.length, 1);
}

test("actual502 ambiguity preserves nonce for explicit same-message reconciliation", async () => {
  const h = harness([{ok: false, status: 502}, completed, completed]);
  assert.equal(await h.send("What is saved?"), false);
  assert.equal(draftAfterFailedSend({failedText: "What is saved?", currentDraft: ""}), "What is saved?");
  assert.match(String(h.messages().at(-1)?.body_md), /unconfirmed/);
  assert.equal(await h.send("What is saved?"), true);
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[0].idempotency_key, h.calls[1].idempotency_key);
  assert.equal(h.messages().filter(m => m.id === "msg_saved").length, 1);
  assert.equal(await h.send("A new question."), true);
  assert.notEqual(h.calls[2].idempotency_key, h.calls[0].idempotency_key);
});

test("unknown turn refuses changed message or question authority without anotherPOST", async () => {
  const h = harness([{ok: false, status: 409, data: {detail: {error: {code: "chat_request_outcome_unknown"}}}}]);
  await h.send("What is saved?");
  assert.equal(await h.send("Start another task."), false);
  assert.equal(await h.send("What is saved?", {replyToQuestion: true, questionId: "qid_changed"}), false);
  assert.equal(h.calls.length, 1);
  assert.match(String(h.messages().at(-1)?.body_md), /earlier request/);
});

test("post-admission409 preserves original nonce through explicit unknown reconciliation", async () => {
  const unknown = {ok: false, status: 409, data: {detail: {error: {code: "chat_request_outcome_unknown"}}}};
  const h = harness([unknown, unknown]);
  await h.send("What is saved?");
  await h.send("What is saved?");
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[0].idempotency_key, h.calls[1].idempotency_key);
  assert.match(String(h.messages().at(-1)?.body_md), /unconfirmed/);
});

test("concurrent retained callback does not send or clear the first flight", async () => {
  let release!: (value: unknown) => void;
  const pending = new Promise(resolve => {release = resolve;});
  const h = harness([() => pending]);
  const first = h.send("What is saved?");
  await dispatched(h);
  assert.equal(await h.send("What is saved?"), false);
  assert.equal(h.calls.length, 1);
  assert.equal(h.thinking.includes(false), false);
  release(completed);
  assert.equal(await first, true);
  assert.equal(h.thinking.at(-1), false);
});

test("retired actor/conversation reply cannot publish or clear a replacement flight", async () => {
  let release!: (value: unknown) => void;
  const h = harness([() => new Promise(resolve => {release = resolve;})]);
  const first = h.send("What is saved?");
  await dispatched(h);
  h.retire();
  const replacement = {payload: "other", key: "fresh", inFlight: true, uncertain: false};
  h.ref.current = {scope: "new-owner", attempt: replacement};
  const count = h.messages().length;
  release(completed);
  assert.equal(await first, false);
  assert.equal(h.messages().length, count);
  assert.equal(h.ref.current.attempt, replacement);
  assert.equal(replacement.inFlight, true);
});

test("remount/app restart restores only scoped nonce+SHA, never message content", async () => {
  const storage = new Map<string, string>();
  const first = harness([{ok: false, status: 502}], storage);
  await first.send("What is saved?");
  const saved = JSON.parse([...storage.values()][0]);
  assert.deepEqual(Object.keys(saved).sort(), ["nonce", "payload_digest", "scope", "version"]);
  assert.match(saved.payload_digest, /^[a-f0-9]{64}$/);
  assert.equal([...storage.values()].some(value => value.includes("What is saved?")), false);
  const remounted = harness([completed], storage);
  assert.equal(await remounted.send("What is saved?"), true);
  assert.equal(remounted.calls[0].idempotency_key, first.calls[0].idempotency_key);
  assert.equal(storage.size, 0);
});

test("remount refuses changed payload without overwriting uncertain checkpoint", async () => {
  const storage = new Map<string, string>();
  await harness([{ok: false, status: 0}], storage).send("Original whole message.");
  const before = [...storage.entries()];
  const next = harness([completed], storage);
  assert.equal(await next.send("Changed task."), false);
  assert.equal(next.calls.length, 0);
  assert.deepEqual([...storage.entries()], before);
  assert.match(String(next.messages().at(-1)?.body_md), /Show status or start fresh/);
  assert.equal(await next.send("Original whole message."), true);
  assert.equal(next.calls[0].idempotency_key, JSON.parse(before[0][1]).nonce);
});

test("malformed or wrong private-scope checkpoints refuse beforePOST", async () => {
  const key = `supymem.chatRequest.v1:${encodeURIComponent(JSON.stringify(["usr_owned", "ven_owned", "cnv_owned"]))}`;
  for (const value of ["{broken", JSON.stringify({version: 1, scope: "foreign", nonce: "same", payload_digest: "a".repeat(64)}),
    JSON.stringify({version: 1, scope: JSON.stringify(["usr_owned", "ven_owned", "cnv_owned"]), nonce: "x", payload_digest: "invalid"})]) {
    const storage = new Map([[key, value]]), h = harness([], storage);
    assert.equal(await h.send("Original."), false);
    assert.equal(h.calls.length, 0);
    assert.equal(storage.get(key), value);
  }
});

test("storage read/write/clear errors never silently mint a replacement nonce", async () => {
  for (const failure of ["get", "set"] as const) {
    const h = harness([], new Map(), "usr_owned", "cnv_owned", failure);
    assert.equal(await h.send("Original."), false);
    assert.equal(await h.send("Original."), false);
    assert.equal(h.calls.length, 0);
  }
  const storage = new Map<string, string>(), h = harness([completed], storage, "usr_owned", "cnv_owned", "remove");
  assert.equal(await h.send("Original."), false);
  const nonce = h.calls[0].idempotency_key;
  const remounted = harness([completed], storage);
  await remounted.send("Original.");
  assert.equal(remounted.calls[0].idempotency_key, nonce);
});

test("different actors/private conversations never borrow an uncertain nonce", async () => {
  const storage = new Map<string, string>(), first = harness([{ok: false, status: 502}], storage);
  await first.send("Original.");
  for (const [actor, cid] of [["usr_other", "cnv_owned"], ["usr_owned", "cnv_other"]]) {
    const next = harness([{...completed, data: {...completed.data, conversation_id: cid}}], storage, actor, cid);
    assert.equal(await next.send("Original."), true);
    assert.notEqual(next.calls[0].idempotency_key, first.calls[0].idempotency_key);
  }
  assert.equal(storage.size, 1);
});

test("retirement during safe hashing refuses before checkpoint orPOST", async () => {
  const h = harness([]), pending = h.send("Original.");
  h.retire();
  assert.equal(await pending, false);
  assert.equal(h.calls.length, 0);
  assert.equal(h.storage.size, 0);
});

test("missing or foreign reply identity remains uncertain with original nonce", async () => {
  for (const data of [null, {id: "foreign", conversation_id: "cnv_other", body_md: "Foreign"}]) {
    const h = harness([{ok: true, status: 200, data}, completed]);
    await h.send("What is saved?");
    await h.send("What is saved?");
    assert.equal(h.calls[0].idempotency_key, h.calls[1].idempotency_key);
    assert.equal(h.messages().filter(m => m.id === "foreign").length, 0);
  }
});

// Execute the actual composer callback together with the actual saved-request
// callback above: a returned false already has a scoped notice from its owner.
function composer(onSend: (body: string) => Promise<boolean>) {
  const source = fs.readFileSync(new URL("../components/chat/useChatComposer.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("composer", source, ts.ScriptTarget.Latest, true);
  let callback = "", sendFailed = "";
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "submit" && node.initializer && ts.isCallExpression(node.initializer)) callback = node.initializer.arguments[0].getText(tree);
    if (ts.isFunctionDeclaration(node) && node.name?.text === "sendFailed") sendFailed = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(callback && sendFailed);
  const values = new Map<string, string>();
  const storage = {getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {values.set(key, value);},
    removeItem: (key: string) => {values.delete(key);}};
  let draft = "", error = "";
  const draftRef = {current: ""}, pendingRef = {current: false};
  const submit = vm.runInNewContext(ts.transpileModule(`${sendFailed}
(${callback})`,
    {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, {
    onSend, canStartSend, clearChatDraftIfMatches, draftAfterFailedSend, writeChatDraft,
    draftRef, pendingRef, restoredDraftRef: {current: null}, sendEpoch: {current: 0}, previousKeyRef: {current: "owned"}, activeKeyRef: {current: "owned"}, draftKey: "owned", storage,
    announceUsageControl: () => {}, setPending: () => {},
    setDraft: (value: string) => {draft = value;}, setError: (value: string) => {error = value;},
  }) as (body: string) => Promise<boolean>;
  return {submit, draft: () => draft, error: () => error};
}

test("unknown saved chat outcome is not overwritten by a composer fresh-retry toast", async () => {
  const unknown = {ok: false, status: 409, data: {detail: {error: {code: "chat_request_outcome_unknown"}}}};
  for (const draft of ["Find freight brokers", "Find balcony gardeners"]) {
    const h = harness([unknown, completed]);
    const c = composer(h.send);
    assert.equal(await c.submit(draft), false);
    assert.equal(c.draft(), draft);
    assert.equal(c.error(), "");
    assert.match(String(h.messages().at(-1)?.body_md), /unconfirmed.*start fresh/);
    assert.equal(await c.submit("A changed question."), false);
    assert.equal(h.calls.length, 1);
    assert.equal(c.error(), "");
    assert.equal(await c.submit(draft), true);
    assert.equal(h.calls[0].idempotency_key, h.calls[1].idempotency_key);
  }
});

test("known admission refusal keeps its owning chat notice and restored draft", async () => {
  for (const draft of ["Find freight brokers", "Find balcony gardeners"]) {
    const h = harness([{ok: false, status: 400, data: {detail: {error: {code: "invalid_request"}}}}]);
    const c = composer(h.send);
    assert.equal(await c.submit(draft), false);
    assert.equal(c.draft(), draft);
    assert.equal(c.error(), "");
    const notice = h.messages().at(-1);
    assert.match(String(notice?.body_md), /could not complete sending your message/i);
    assert.equal(notice?.kind, "needs_you");
    assert.deepEqual(JSON.parse(JSON.stringify(notice?.actions)), [{label: "Edit your request", action: "chat.prefill", payload: {text: draft}, style: "primary"}]);
    assert.equal(h.ref.current.attempt, null);
  }
});

test("uncaught send errors preserve the draft without a blind retry instruction", async () => {
  const c = composer(async () => {throw new Error("owned transport failure");});
  assert.equal(await c.submit("What is saved?"), false);
  assert.equal(c.draft(), "What is saved?");
  assert.match(c.error(), /could not be confirmed/i);
  assert.doesNotMatch(c.error(), /please try again/i);
});
