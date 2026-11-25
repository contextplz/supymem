import assert from "node:assert/strict";
import test from "node:test";
import { shouldResetPortfolio } from "./nativeAuthTransition.ts";

test("initial connected state preserves the current desktop route", () => {
  assert.equal(shouldResetPortfolio(undefined, "connected"), false);
});

test("repeated connected events preserve the current desktop route", () => {
  assert.equal(shouldResetPortfolio("connected", "connected"), false);
});

test("same-account reconnect keeps the venture and a different account resets it", () => {
  assert.equal(shouldResetPortfolio("reauth_required", "connected", "usr_one", "usr_one"), false);
  assert.equal(shouldResetPortfolio("signed_out", "connected", "usr_one", "usr_one"), false);
  assert.equal(shouldResetPortfolio("reauth_required", "connected", "usr_one", "usr_two"), true);
  assert.equal(shouldResetPortfolio("signed_out", "connected", "usr_one", "usr_two"), true);
  assert.equal(shouldResetPortfolio("connected", "connected", "usr_one", "usr_two"), true);
  assert.equal(shouldResetPortfolio("connected", "connected", "usr_one", "usr_one"), false);
  assert.equal(shouldResetPortfolio("offline", "connected", "usr_one", "usr_two"), true);
});
