import assert from "node:assert/strict";
import test from "node:test";
import { answeredState, failedSince, settleClick } from "./chatAnswered.ts";

const q = { role: "assistant", body_md: "Which?" };
const opts = ["Dentists", "Gyms"];

test("open until a founder message follows", () => {
  assert.equal(answeredState([q], 0, opts), null);
  assert.equal(answeredState([q, { role: "assistant", body_md: "hi" }], 0, opts), null);
});

test("a clicked chip is marked chosen, even after reload (stored founder message)", () => {
  assert.deepEqual(answeredState([q, { role: "founder", body_md: "Gyms" }], 0, opts), { chosen: "Gyms", typed: null });
  assert.deepEqual(answeredState([q, { role: "user", text: " Dentists " }], 0, opts), { chosen: "Dentists", typed: null });
});

test("a typed reply answers the question without marking a chip", () => {
  assert.deepEqual(answeredState([q, { role: "founder", body_md: "Vets" }], 0, opts), { chosen: null, typed: "Vets" });
});

test("only the first later founder message counts", () => {
  const thread = [q, { role: "founder", body_md: "Gyms" }, { role: "founder", body_md: "Dentists" }];
  assert.equal(answeredState(thread, 0, opts)?.chosen, "Gyms");
});

test("a failed click is released for retry; a successful one stays disabled", () => {
  const clicked = new Set(["m1:Yes"]);
  assert.equal(settleClick(clicked, "m1:Yes", true).has("m1:Yes"), true);
  assert.equal(settleClick(clicked, "m1:Yes", false).has("m1:Yes"), false);
});

test("failure is detected only from errors raised after the click", () => {
  assert.equal(failedSince([{ id: "local-err-100" }], 200), false);
  assert.equal(failedSince([{ id: "local-err-300" }], 200), true);
  assert.equal(failedSince([{ id: "msg_1" }, { id: "local-out-300" }], 200), false);
});
