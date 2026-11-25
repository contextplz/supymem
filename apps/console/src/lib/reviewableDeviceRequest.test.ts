import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  MAC_TASKS_HINT,
  SUPPORTED_MAC_TASK_EXAMPLES,
  bareMacTaskConfirmation,
  desktopControlRequest,
  readonlyBrowserTaskRequest,
  reviewableDeviceRequest,
  shouldAppendMacTasksHint,
  ventureChatDeviceRoute,
} from "./reviewableDeviceRequest.ts";

const fixturePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../../../tests/fixtures/device_chat_intent_parity.json",
);

test("CLI product coding does not grant screen capture or desktop control", () => {
  for (const goal of ["Build my app with Codex on this Mac and write its tests",
    "Please implement my app with Claude Code on this Mac, then write the tests",
    "Use Codex to build my app on this Mac and write its API",
    "I want to build my app with Claude Code on this Mac and write tests",
    "I'd like to use Codex to build my app on this Mac and write tests",
    "Help me build my app with Codex on this Mac and write tests",
    "Let's build my app with Claude Code on this Mac and write tests"]) {
    assert.equal(desktopControlRequest(goal), false, goal);
    assert.equal(ventureChatDeviceRoute(goal), "from-chat", goal);
  }
  assert.equal(desktopControlRequest("Open Codex and click its Settings button on this Mac"), true);
  assert.equal(desktopControlRequest("Use Claude Code to click Settings on this Mac"), true);
});

const ORDINARY_CHAT_EXAMPLES = [
  "Type a mail to our first 5 prospects",
  "Remind me what we decided about pricing",
  "Open the Slack connection page",
  "How does computer use work?",
  "Can Supymem run on my Mac while I sleep?",
  "Launch the waitlist campaign on Slack",
  "Draft a Notion doc for the plan",
  "What does Claude think of our ICP?",
];

test("explicit free-form read-only browser requests reach a specific refusal without native inventory", () => {
  for (const request of [
    "Use my Mac for a read-only background browser task. Allowed sites are https://audit-qa.supymem.com and https://gateway.supymem.com. Open https://audit-qa.supymem.com, report its page title and main heading, follow Join the early-access waitlist to https://gateway.supymem.com/public/waitlists/ven_0sf17pcf8dt6, report its heading, then return to the landing page. Do not type, submit any forms, send messages, download or change anything.",
    "Use my Mac for a read-only browser task: open https://audit-qa.supymem.com in the background browser, read the page title and main heading, compare all three How it works steps, and report the exact early-access button label. Stay on this landing page. Do not type, submit forms, send messages, follow external links, download or change anything.",
    "Could you please use this Mac for a read only browser task: compare https://example.test and https://second.test?",
    "Prepare a Mac task: please use the Mac for a readonly background browser task. Do not change anything.",
  ]) {
    assert.equal(readonlyBrowserTaskRequest(request), true, request);
    assert.equal(ventureChatDeviceRoute(request), "from-chat", request);
    assert.equal(desktopControlRequest(request), false, request);
    assert.equal(shouldAppendMacTasksHint(request), false, request);
  }
  for (const request of [
    "Did you use my Mac for a read-only browser task to open https://example.test?",
    "Do not use my Mac for a read-only browser task: open https://example.test.",
    'The note says "Use my Mac for a read-only browser task: open my browser and prepare the signup form at https://example.test".',
    '"Use my Mac for a read-only background browser task"',
    "How can I use my Mac for a read-only browser task?",
    "I prepared a read-only browser task yesterday. What happened?",
  ]) {
    assert.equal(readonlyBrowserTaskRequest(request), false, request);
    assert.equal(ventureChatDeviceRoute(request), "chat", request);
  }
});

test("explicit use-my-Mac read-only goal reaches native scoped preparation", () => {
  const request = "Use my Mac to open https://example.com in your browser and report its page title. This is read-only: do not edit, submit forms, or send messages.";
  assert.equal(desktopControlRequest(request), true);
  assert.equal(ventureChatDeviceRoute(request), "from-chat");
  assert.equal(shouldAppendMacTasksHint(request), false);
  assert.equal(ventureChatDeviceRoute("Can you use my Mac to open https://example.com and report its title?"), "from-chat");
  for (const politeRequest of [
    "Can you use my Mac to open Chrome?",
    "Can you please use my Mac to open Chrome?",
    "Could you please use my Mac to open Chrome?",
  ]) {
    assert.equal(desktopControlRequest(politeRequest), true, politeRequest);
    assert.equal(ventureChatDeviceRoute(politeRequest), "from-chat", politeRequest);
  }
  for (const text of [
    "Can Supymem use my Mac to open a browser automatically?",
    "Can you use my Mac?",
    "Are you able to use my Mac?",
    "How do you use my Mac to open a browser?",
    "Should I use my Mac to open a browser?",
    "Do not use my Mac to open https://example.com in your browser.",
    "Please don't use my Mac to open https://example.com.",
    'The note says "Use my Mac to open https://example.com in your browser".',
    '"Use my Mac to open https://example.com in your browser"',
    "Do not use my Mac to open https://example.com in my browser.",
    'The note says "Use my Mac to open https://example.com in my browser".',
  ]) {
    assert.equal(reviewableDeviceRequest(text), false, text);
    assert.equal(ventureChatDeviceRoute(text), "chat", text);
  }
});

test("ordinary founder chat stays on /chat with no Mac-task hijack", () => {
  for (const request of ORDINARY_CHAT_EXAMPLES) {
    assert.equal(ventureChatDeviceRoute(request), "chat", request);
    assert.equal(reviewableDeviceRequest(request), false, request);
    assert.equal(shouldAppendMacTasksHint(request), false, request);
  }
});

test("explicit named Mac-control ask reaches a reviewed task", () => {
  const request = "Open Notes on this Mac and type a reminder";
  assert.equal(ventureChatDeviceRoute(request), "from-chat");
  assert.equal(reviewableDeviceRequest(request), true);
  assert.equal(shouldAppendMacTasksHint(request), false);
  assert.match(MAC_TASKS_HINT, /Mac tasks require a reviewed scope/);
});

test("a named app followed by another imperative sentence reaches review", () => {
  const request = "Open Calculator. Press its visible Clear control once, then report the display.";
  assert.equal(ventureChatDeviceRoute(request), "from-chat");
  assert.equal(reviewableDeviceRequest(request), true);
  assert.equal(ventureChatDeviceRoute("Open Calculator. What does the Clear control do?"), "chat");
});

test("read-only Builds navigation with a saved-evidence question stays in venture chat", () => {
  const request = "Open Builds in Supymem and report what build evidence is saved for LabHire. Do not start or modify any build.";
  assert.equal(ventureChatDeviceRoute(request), "chat");
  assert.equal(reviewableDeviceRequest(request), false);
  assert.equal(shouldAppendMacTasksHint(request), false);
  assert.equal(ventureChatDeviceRoute("Open Builds in Supymem and click Delete"), "from-chat");
});

test("first-party saved Analytics and Builds views take precedence over Mac control", () => {
  for (const request of [
    "Open analytics in Supymem and tell me what the founder dashboard actually shows. Answer only; do not start work.",
    "Open LabHire Analytics in Supymem and report what the founder dashboard shows. Do not start a task.",
    "Open Builds in Supymem and report what build evidence is saved for LabHire. Do not start or modify any build.",
    "Can you open Builds in Supymem?",
    "Open Builds in Supymem. Do not modify data.",
    "Use my Mac to open Builds in Supymem. Do not modify data.",
    "Use my Mac to open Analytics in Supymem. Do not modify data.",
    "Use my Mac to open Settings in Supymem. Do not modify data.",
  ]) {
    assert.equal(ventureChatDeviceRoute(request), "chat", request);
    assert.equal(shouldAppendMacTasksHint(request), false, request);
  }
  for (const request of [
    "Open Calculator and tell me exactly what number is shown. Do not press any buttons.",
    "Open Builds in Supymem and click Delete",
  ]) assert.equal(ventureChatDeviceRoute(request), "from-chat", request);
});

test("questions about an approved Mac task never prepare another order", () => {
  const question = "What was the outcome of the latest approved Supymem Mac task? Did it open Builds, was that verified, and how many credits were settled? Do not start another task.";
  assert.equal(ventureChatDeviceRoute(question), "chat");
  assert.equal(reviewableDeviceRequest(question), false);
  assert.equal(shouldAppendMacTasksHint(question), false);
  assert.equal(ventureChatDeviceRoute("Can you open Builds in Supymem?"), "chat");
  assert.equal(ventureChatDeviceRoute("Will you open Notes and type a reminder?"), "from-chat");
});

test("leading installed-app imperative reaches review without magic Mac wording", () => {
  const request = "In TextEdit, click Cancel on the visible Open dialog and report what happened.";
  assert.equal(ventureChatDeviceRoute(request), "from-chat");
  assert.equal(shouldAppendMacTasksHint(request), false);
  for (const question of [
    "In TextEdit, what would happen if I clicked Cancel?",
    "In TextEdit, click Cancel?",
    "In TextEdit, delete all documents.",
  ]) assert.equal(ventureChatDeviceRoute(question), "chat", question);
  assert.equal(ventureChatDeviceRoute(
    "In TextEdit's current Open dialog on this Mac, click Cancel and report what remains."), "from-chat");
  assert.equal(ventureChatDeviceRoute(
    "In the current Open dialog in TextEdit, click Cancel and report what remains."), "from-chat");
  assert.equal(ventureChatDeviceRoute(
    "In TextEdit's current dialog, delete all documents."), "chat");
});

test("conditional installed-app instruction reaches the reviewed native path", () => {
  const request = "In TextEdit, if an Open dialog is visible, click Cancel and report which window remains. If no Open dialog is visible, only report what you see. Do not open, create, edit, save, or delete any document.";
  assert.equal(ventureChatDeviceRoute(request), "from-chat");
  assert.equal(shouldAppendMacTasksHint(request), false);
  assert.equal(ventureChatDeviceRoute("In TextEdit, if an Open dialog is visible, should I click Cancel?"), "chat");
  const conditionFirst = "If TextEdit is showing an Open dialog, click Cancel and report which window remains. If not, only describe what you see. Do not open or edit a document.";
  assert.equal(ventureChatDeviceRoute(conditionFirst), "from-chat");
  assert.equal(ventureChatDeviceRoute("If TextEdit is showing an Open dialog, should I click Cancel?"), "chat");
});

test("each supported Mac-task example routes to from-chat", () => {
  for (const example of SUPPORTED_MAC_TASK_EXAMPLES) {
    assert.equal(ventureChatDeviceRoute(example.text), "from-chat", example.text);
    assert.equal(reviewableDeviceRequest(example.text), true, example.text);
    assert.equal(shouldAppendMacTasksHint(example.text), false, example.text);
  }
});

test("parity fixture: client reviewable matches expected and is never broader than server", () => {
  const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as {
    phrases: Array<{
      text: string;
      server_intent: string | null;
      client_reviewable: boolean;
      intentional_difference?: string;
    }>;
  };
  assert.ok(fixture.phrases.length >= 15, "parity fixture needs 15+ phrases");
  for (const phrase of fixture.phrases) {
    assert.equal(
      reviewableDeviceRequest(phrase.text),
      phrase.client_reviewable,
      phrase.text,
    );
    if (phrase.client_reviewable) {
      assert.notEqual(
        phrase.server_intent,
        null,
        `client must not be broader than server: ${phrase.text}`,
      );
    }
  }
});

test("questions about computer use never get the Mac-tasks hint", () => {
  for (const request of [
    "How does computer use work?",
    "Can Supymem run on my Mac while I sleep?",
    "What is a mac task?",
  ]) {
    assert.equal(shouldAppendMacTasksHint(request), false, request);
  }
});

test("'Prepare a Mac task: open TextEdit ...' reaches review and its confirmation is recognised", () => {
  const request = 'Prepare a Mac task: open TextEdit, create a new plain text document, type these three lines: "a", "b", then save it as note.txt in ~/Documents/X.';
  assert.equal(ventureChatDeviceRoute(request), "from-chat");
  assert.equal(shouldAppendMacTasksHint(request), false);
  const confirm = "Yes, that folder is right. Prepare the Mac task for review.";
  assert.equal(reviewableDeviceRequest(confirm), false);
  assert.equal(bareMacTaskConfirmation(confirm), true);
  assert.equal(bareMacTaskConfirmation("Should I prepare the Mac task?"), false);
  assert.equal(bareMacTaskConfirmation(request), false);
});

test("named app creation and save requests reach review without a type verb", () => {
  for (const request of [
    "Prepare a Mac task: open TextEdit, create a plain text document containing Supymem test note. Save it as supymem-note.txt in ~/Documents/SupymemCUTest.",
    "Open TextEdit. Create a plain text document containing Supymem test note.",
    "In TextEdit, save the current document as supymem-note.txt.",
  ]) {
    assert.equal(ventureChatDeviceRoute(request), "from-chat", request);
    assert.equal(shouldAppendMacTasksHint(request), false, request);
  }
  for (const question of ["How can I create a document in TextEdit?", "Can TextEdit save plain text?",
    "Create a landing page for my startup", "Save my startup idea for later"]) {
    assert.equal(ventureChatDeviceRoute(question), "chat", question);
  }
});

test("a named site with no Mac is a chat read; Mac asks and site actions keep their review", () => {
  for (const text of ["Open compliancy-group.com and tell me what they charge and which customers they name.",
    "Can you visit www.spire.com/pricing and summarize it?", "Open https://ops.example/plans. Do not change anything."]) {
    assert.equal(reviewableDeviceRequest(text), false, text);
    assert.equal(desktopControlRequest(text), false, text);
  }
  for (const text of ["On my Mac, open https://ops.example/plans and read it", "Use my Mac to open https://spire.com",
    "Go to resend.com and prepare the signup form without submitting or entering credentials"]) {
    assert.equal(reviewableDeviceRequest(text), true, text);
  }
  assert.equal(reviewableDeviceRequest("Open Calculator and tell me exactly what number is shown. Do not press any buttons."), true);
});
