import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import {
  bareMacTaskConfirmation, browserTaskRequest, desktopControlRequest, reviewableDeviceRequest, shouldAppendMacTasksHint,
} from "./reviewableDeviceRequest.ts";
import { desktopExecutionAvailability, desktopExecutionMessage } from "./desktopExecutionAvailability.ts";
import { MAC_TASK_PERMISSION_EVENT, macTaskPermissionsGranted } from "./macPermissions.ts";
import { activeMacTaskReviewPath, macTaskPrepareFailure } from "./macTaskPrepareFailure.ts";
import { chatFlight } from "./chatStop.ts";
import { apiErrorCode } from "./apiFailure.ts";

const source = fs.readFileSync(new URL("../components/chat/useVentureChat.ts", import.meta.url), "utf8");
const tree = ts.createSourceFile("useVentureChat.ts", source, ts.ScriptTarget.Latest, true);

function actualReplyHandler(sendChat: (text: string, opts?: unknown) => Promise<void>, globals: Record<string, unknown> = {}) {
  let callback = "";
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "runChatAction"
      && node.initializer && ts.isCallExpression(node.initializer)) callback = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(callback);
  return vm.runInNewContext(ts.transpileModule(`(${callback})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, { sendChat, isDesktopShell: () => true, current: () => true, conversationId: null, browserTaskRequest, desktopControlRequest, macTaskPermissionsGranted, MAC_TASK_PERMISSION_EVENT, ...globals }) as (action: string, payload: unknown) => Promise<void>;
}

const DIRECT_MAC_REQUEST = "Use my Mac to open TextEdit and read the visible project notes. Read-only: do not change or save anything.";

test("actual Mac prepare action retains the exact request through existing permissions gate", async () => {
  const request = actualSendHandler("/v/v_one/device-work-orders/dwo_current");
  await actualReplyHandler(async text => { await request.handler(text); })("mac.task.prepare", {text: DIRECT_MAC_REQUEST});
  assert.deepEqual(request.stages, ["availability", "permissions", "inventory", "prepare"]);
  assert.equal(request.requests[0].body.instruction, DIRECT_MAC_REQUEST);
});

function actualSendHandler(reviewPath: string, availability: unknown = { paused: false, execution_available: true }, retire?: "venture" | "actor", preparedFailure?: Record<string, unknown>, permissionSnapshot: unknown = {"screen-recording": "granted", accessibility: "granted"}, desktop = true) {
  let callback = "";
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "sendChat"
      && node.initializer && ts.isCallExpression(node.initializer)) callback = node.initializer.arguments[0].getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(callback);
  let messages: Array<Record<string, unknown>> = [{ role: "assistant", actions: [{ href: "/v/v_one/device-work-orders/dwo_previous" }] }];
  const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
  const stages: string[] = [];
  const events: Array<{type: string; detail: {resume: () => Promise<void>}}> = [];
  const currentVenture = { current: "v_one" };
  let epoch = 7;
  const scope = { target_application: "com.google.Chrome", target_pid: 77, target_process_start: "123456789",
    target_window: 8, target_window_title: "New Tab" };
  const refusal = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "sendRefusal");
  assert.ok(refusal);
  const handler = vm.runInNewContext(ts.transpileModule(`${refusal.getText(tree)}; (${callback})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    bareMacTaskConfirmation, browserTaskRequest, desktopControlRequest, reviewableDeviceRequest, shouldAppendMacTasksHint,
    isDesktopShell: () => desktop,
    desktopExecutionAvailability, desktopExecutionMessage, currentVenture, verifiedSessionEpoch: () => epoch,
    current: () => currentVenture.current === "v_one" && epoch === 7, conversationId: null,
    activeMacTaskReviewPath, macTaskPrepareFailure, apiErrorCode,
    ventureId: "v_one", setThinking: () => {}, flight: {current: chatFlight()},
    setMessages: (update: (old: Array<Record<string, unknown>>) => Array<Record<string, unknown>>) => { messages = update(messages); },
    selectDesktopApplication: async (goal: string) => { stages.push("inventory"); assert.equal(goal, DIRECT_MAC_REQUEST); return scope; },
    api: async (path: string, request?: { body: string }) => {
      if (path.endsWith("/device-desktop-pause")) {
        stages.push("availability");
        if (retire === "venture") currentVenture.current = "v_other";
        if (retire === "actor") epoch++;
        return { ok: true, data: availability };
      }
      stages.push("prepare");
      assert.ok(request);
      requests.push({ path, body: JSON.parse(request.body) });
      if (preparedFailure) return preparedFailure;
      return { ok: true, data: { id: "dwo_current", review_path: reviewPath, title: "Read-only browser task" } };
    },
    latestManagerName: () => "Charlie",
    desktopInvoke: async (command: string) => { stages.push("permissions"); assert.equal(command, "permissions_status"); return permissionSnapshot; },
    window: {dispatchEvent: (event: typeof events[number]) => {events.push(event);}},
    CustomEvent: class {type: string; detail: unknown; constructor(type: string, init: {detail: unknown}) {this.type = type; this.detail = init.detail;}},
    sendChat: (body: string) => handler(body),
    macTaskPermissionsGranted, MAC_TASK_PERMISSION_EVENT,
  }) as (text: string) => Promise<boolean>;
  return { handler, requests, stages, scope, events, currentVenture, permissions: (snapshot: unknown) => {permissionSnapshot = snapshot;}, messages: () => messages };
}

test("actual sendChat posts scoped exact goal and uses only this request's saved review identity", async () => {
  const review = actualSendHandler("/v/v_one/device-work-orders/dwo_current");
  assert.equal(await review.handler(DIRECT_MAC_REQUEST), true);
  assert.deepEqual(review.stages, ["availability", "permissions", "inventory", "prepare"]);
  assert.deepEqual(review.requests, [{ path: "/api/ventures/v_one/device-work-orders/from-chat",
    body: { instruction: DIRECT_MAC_REQUEST, ...review.scope } }]);
  const reply = review.messages().at(-1)!;
  assert.equal(reply.kind, "confirmation");
  assert.match(String(reply.body_md), /Nothing runs on your Mac until you open the review and approve it/);
  assert.deepEqual(JSON.parse(JSON.stringify(reply.actions)), [{ label: "Review Mac task",
    href: "/v/v_one/device-work-orders/dwo_current", style: "primary" }]);
});

test("browser-shaped Mac task prepares a review without native permissions or client digest", async () => {
  const review = actualSendHandler("/v/v_one/device-work-orders/dwo_current", {paused: false, execution_available: false});
  const goal = "Use my Mac to check https://example.test/pricing and report prices.";
  assert.equal(await review.handler(goal), true);
  assert.deepEqual(review.stages, ["prepare"]);
  assert.deepEqual(review.requests[0].body, {instruction: goal});
  assert.equal(review.messages().at(-1)!.kind, "confirmation");
});

test("browser request without a URL asks for its site and prepares nothing", async () => {
  const review = actualSendHandler("/v/v_one/device-work-orders/dwo_current");
  assert.equal(await review.handler("Use my browser to compare pricing"), true);
  assert.deepEqual(review.stages, []);
  assert.match(String(review.messages().at(-1)!.body_md), /Which website/);
});

test("ordinary browser Mac requests never prepare, inspect permissions or invent a device review", async () => {
  for (const goal of [DIRECT_MAC_REQUEST, "Go to resend.com and prepare the signup form without submitting or entering credentials", "Build the landing page using Codex."]) {
    const review = actualSendHandler("/v/v_one/device-work-orders/dwo_current", undefined, undefined, undefined, undefined, false);
    assert.equal(await review.handler(goal), true);
    assert.deepEqual(review.stages, []); assert.deepEqual(review.requests, []); assert.deepEqual(review.events, []);
    assert.match(String(review.messages().at(-1)!.body_md), /No Mac task was prepared or started/);
    assert.equal(review.messages().at(-1)!.actions, undefined);
  }
  const sent: string[] = [];
  await actualReplyHandler(async text => { sent.push(text); }, {
    isDesktopShell: () => false,
    desktopInvoke: async () => { throw new Error("browser must not inspect native permissions"); },
  })("mac.task.prepare", { text: DIRECT_MAC_REQUEST });
  assert.deepEqual(sent, [DIRECT_MAC_REQUEST]);
});

test("actual coding send preserves overlength guidance and editable goal without native launch or a fake review", async () => {
  const message = "Shorten this coding request to 1200 characters or fewer. Name the approved task and a concise repair focus; Supymem includes its saved approved contract and verified QA findings when available. No coding task was prepared or started.";
  for (const audience of ["freight operators", "weekend cyclists"]) {
    const goal = `Use Codex to edit the approved landing page for ${audience}. ` + "x".repeat(1300);
    const request = actualSendHandler("", undefined, undefined, {
      ok: false, status: 409, data: { detail: { code: "device_coding_focus_too_long", message } },
    });
    assert.equal(await request.handler(goal), false);
    assert.deepEqual(request.stages, ["prepare"]);
    assert.deepEqual(request.requests, [{ path: "/api/ventures/v_one/device-work-orders/from-chat", body: { instruction: goal } }]);
    const reply = request.messages().at(-1)!;
    assert.equal(reply.body_md, `<!--employee:Charlie:Manager:manager-->\n${message}`);
    assert.deepEqual(JSON.parse(JSON.stringify(reply.actions)), [{label: "Edit your request", action: "chat.prefill", payload: {text: goal}, style: "primary"}]);
    assert.equal(request.messages().filter(item => item.kind === "confirmation").length, 0);
    assert.equal(request.messages().at(-2)!.body_md, goal);
  }
});

test("actual chat native task refuses unavailable, paused and unknown before app inventory or preparation", async () => {
  for (const [metadata, expected] of [
    [{ paused: false, execution_available: false, unavailability_code: "device_desktop_execution_unavailable" }, /unavailable in this beta/],
    [{ paused: true, execution_available: false, unavailability_code: "device_desktop_execution_unavailable" }, /unavailable in this beta/],
    [{ paused: true, execution_available: false }, /paused on this device/],
    [{ paused: false }, /Could not confirm/],
    [{ paused: false, execution_available: "true" }, /Could not confirm/],
  ] as const) {
    const review = actualSendHandler("/v/v_one/device-work-orders/dwo_current", metadata);
    assert.equal(await review.handler(DIRECT_MAC_REQUEST), true);
    assert.deepEqual(review.stages, ["availability"]);
    assert.equal(review.requests.length, 0);
    assert.match(String(review.messages().at(-1)!.body_md), expected);
  }
});

test("actual chat rejects retired venture and actor availability replies before app inventory", async () => {
  for (const retired of ["venture", "actor"] as const) {
    const review = actualSendHandler("/v/v_one/device-work-orders/dwo_current", { paused: false, execution_available: true }, retired);
    assert.equal(await review.handler(DIRECT_MAC_REQUEST), false);
    assert.deepEqual(review.stages, ["availability"]);
    assert.equal(review.requests.length, 0);
  }
});

test("actual sendChat refuses a review identity from another venture", async () => {
  const review = actualSendHandler("/v/v_other/device-work-orders/dwo_current");
  assert.equal(await review.handler(DIRECT_MAC_REQUEST), false);
  const reply = review.messages().at(-1)!;
  assert.match(String(reply.body_md), /could not verify the saved Mac review link/);
  assert.equal(reply.actions, undefined);
});

test("actual setup and experiment follow-up buttons send commands, not idea answers", async () => {
  const sent: Array<{ text: string; opts?: unknown }> = [];
  const reply = actualReplyHandler(async (text, opts) => { sent.push({ text, opts }); });
  await reply("chat.reply", { text: "connect my gmail" });
  await reply("chat.reply", { text: "show my outreach drafts" });
  await reply("chat.reply", { text: "revise the idea", question_id: "" });
  assert.deepEqual(sent, [
    { text: "connect my gmail", opts: undefined },
    { text: "show my outreach drafts", opts: undefined },
    { text: "revise the idea", opts: undefined },
  ]);
});

test("actual idea chip preserves the exact saved question identity", async () => {
  const sent: Array<{ text: string; opts?: unknown }> = [];
  await actualReplyHandler(async (text, opts) => { sent.push({ text, opts }); })("chat.reply", {
    text: "Independent gym owners", question_id: "cit_saved_question",
  });
  assert.deepEqual(JSON.parse(JSON.stringify(sent)), [{ text: "Independent gym owners",
    opts: { replyToQuestion: true, questionId: "cit_saved_question" } }]);
});


test("Mac prepare keeps denied and unavailable status inline without navigating or inventory", async () => {
  for (const snapshot of [null, {"screen-recording": "not_determined", accessibility: "granted"}, {"screen-recording": "granted", accessibility: "not_determined"}]) {
    const request = actualSendHandler("/v/v_one/device-work-orders/dwo_current", undefined, undefined, undefined, snapshot);
    await actualReplyHandler(async text => { await request.handler(text); })("mac.task.prepare", {text: DIRECT_MAC_REQUEST});
    assert.deepEqual(request.stages, ["availability", "permissions"]);
    assert.equal(request.requests.length, 0);
    assert.equal(request.events.length, 1);
    assert.equal(request.events[0].type, MAC_TASK_PERMISSION_EVENT);
    request.permissions({"screen-recording": "granted", accessibility: "granted"});
    await request.events[0].detail.resume();
    assert.equal(request.requests.length, 1);
    assert.equal(request.requests[0].body.instruction, DIRECT_MAC_REQUEST);
  }
});


test("direct Mac chat demand never inventories before permission and resumes the exact goal", async () => {
  const request = actualSendHandler("/v/v_one/device-work-orders/dwo_current", undefined, undefined, undefined,
    {"screen-recording": "not_determined", accessibility: "granted"});
  assert.equal(await request.handler(DIRECT_MAC_REQUEST), true);
  assert.deepEqual(request.stages, ["availability", "permissions"]);
  assert.equal(request.requests.length, 0);
  assert.equal(request.events[0].type, MAC_TASK_PERMISSION_EVENT);
  request.permissions({"screen-recording": "granted", accessibility: "granted"});
  await request.events[0].detail.resume();
  assert.equal(request.requests.length, 1);
  assert.equal(request.requests[0].body.instruction, DIRECT_MAC_REQUEST);
  assert.equal(request.messages().filter(message => message.role === "founder").length, 1);
});

test("retired permission demand cannot resume a Mac task into another venture", async () => {
  const request = actualSendHandler("/v/v_one/device-work-orders/dwo_current", undefined, undefined, undefined, null);
  await request.handler(DIRECT_MAC_REQUEST);
  request.currentVenture.current = "v_other";
  await request.events[0].detail.resume();
  assert.equal(request.requests.length, 0);
  assert.deepEqual(request.stages, ["availability", "permissions"]);
});


test("isolated browser and coding preparation do not ask for Mac screen access", async () => {
  const sent: string[] = [];
  for (const goal of ["Go to resend.com and prepare the signup form without submitting or entering credentials", "Build the landing page using Codex."]) {
    await actualReplyHandler(async text => {sent.push(text);}, {desktopInvoke: async () => {throw new Error("No native permission check for this task");}})("mac.task.prepare", {text: goal});
  }
  assert.equal(sent.length, 2);
});
