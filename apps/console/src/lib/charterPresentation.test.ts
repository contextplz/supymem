import assert from "node:assert/strict";
import test from "node:test";
import { hasVisibleCharter } from "./charterPresentation.ts";

test("an empty saved charter does not hide the execution-plan fallback", () => {
  assert.equal(hasVisibleCharter(null), false);
  assert.equal(hasVisibleCharter({}), false);
  assert.equal(hasVisibleCharter({ one_liner: "  ", mvp_scope: [] }), false);
});

test("a charter with founder-visible content is shown", () => {
  assert.equal(hasVisibleCharter({ one_liner: "Recover unbilled scope" }), true);
  assert.equal(hasVisibleCharter({ mvp_scope: ["Paste a SOW"] }), true);
});
