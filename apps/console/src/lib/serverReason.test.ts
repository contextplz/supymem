import assert from "node:assert/strict";
import test from "node:test";
import { serverReason } from "./serverReason.ts";

const fail = (data: unknown) => ({ ok: false, status: 422, data });

test("shows the server reason from either error shape", () => {
  assert.equal(serverReason(fail({ error: { code: "invalid_llm_settings", message: "Bad base URL." } }), "x"), "Bad base URL.");
  assert.equal(serverReason(fail({ detail: { error: { message: "Already paused." } } }), "x"), "Already paused.");
});

test("falls back when there is no reason", () => {
  assert.equal(serverReason(fail(null), "Couldn’t save."), "Couldn’t save.");
  assert.equal(serverReason(fail({ detail: "nope" }), "Couldn’t save."), "Couldn’t save.");
});
