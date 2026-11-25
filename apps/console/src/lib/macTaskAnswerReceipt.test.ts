import assert from "node:assert/strict";
import test from "node:test";
import { localMacTaskObservation, macTaskAnswerOrderId } from "./macTaskAnswerReceipt.ts";

test("only the selected venture order can supplement a chat answer", () => {
  const message = { id: "msg_1", role: "assistant" as const,
    actions: [{ label: "Review Mac task", href: "/v/ven_one/device-work-orders/dwo_clock" }] };
  assert.equal(macTaskAnswerOrderId(message, "ven_one"), "dwo_clock");
  assert.equal(macTaskAnswerOrderId(message, "ven_two"), null);
  assert.equal(macTaskAnswerOrderId({ ...message, actions: [{ label: "Review Mac task",
    href: "/v/ven_one/device-work-orders/dwo_clock?other=1" }] }, "ven_one"), null);
});

test("stopped task chat post is plain: screenshot facts, no driver receipts, never verified", () => {
  const receipt = { order_id: "dwo_clock", state: "stopped", task_verified: false,
    failure_code: "desktop_cua_observation_unpinned", evidence: { steps: [
      { kind: "effect", operation: "type", target_label: "smart search field", effect: "unverifiable" },
      { kind: "effect", operation: "key", target_label: "Press Return", effect: "unverifiable" },
      { kind: "visual_review", after_effect: "Timers", visible_facts: ["Timers is selected"] },
    ], visual_review: { verdict: "done", visible_facts: ["The World Clock tab is selected", "A clock list is visible"] } } };
  const text = localMacTaskObservation(receipt, "dwo_clock") || "";
  assert.match(text, /Timers is selected/);
  assert.match(text, /Review Mac task/);
  assert.doesNotMatch(text, /driver effect|receipts|checkpoints|stop code|desktop_cua|smart search field/);
  assert.equal(text.split(/(?<=\.)\s/).length <= 2, true);
  assert.equal(localMacTaskObservation(receipt, "dwo_other"), null);
});

test("without screenshot facts the post says so and stays plain", () => {
  const receipt = { order_id: "dwo_t", state: "stopped", failure_code: "bad code <script>", evidence: { steps: [
    { kind: "effect", operation: "click", target_label: "Cancel", effect: "unverifiable" },
  ], visual_review: null } };
  const text = localMacTaskObservation(receipt, "dwo_t") || "";
  assert.match(text, /could not|before Supymem could confirm/);
  assert.doesNotMatch(text, /Cancel|unverifiable|bad code/);
});

test("facts are bounded, de-marked and absent on unfinished receipt", () => {
  const receipt = { order_id: "dwo_clock", state: "stopped", evidence: {
    visual_review: { verdict: "blocked", visible_facts: [
      "**Clock** showed user@example.com and password: secretvalue",
    ] },
  } };
  const text = localMacTaskObservation(receipt, "dwo_clock") || "";
  assert.match(text, /Clock showed \[redacted-email\]/);
  assert.doesNotMatch(text, /secretvalue|\*\*/);
  assert.equal(localMacTaskObservation({ ...receipt, state: "acting" }, "dwo_clock"), null);
});
