import assert from "node:assert/strict";
import test from "node:test";
import { godModeRequestLine } from "./godModeRequest.ts";

test("status line", () => {
  assert.match(godModeRequestLine({ status: "requested", requested_at: "2026-10-05T10:00:00Z" }), /^Requested on .*2026, we'll enable it for you$/);
  assert.match(godModeRequestLine({ status: "approved", requested_at: "2026-10-05T10:00:00Z" }), /^Approved/);
  assert.equal(godModeRequestLine({ status: "none", requested_at: null }), "");
});
