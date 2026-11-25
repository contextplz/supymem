import assert from "node:assert/strict";
import test from "node:test";

import { macNoticeDiagnosticLine } from "./macNoticeDiagnostic.ts";

test("mac notice diagnostic lines match Settings copy", () => {
  assert.equal(macNoticeDiagnosticLine(null), "Mac notifications: checking…");
  assert.equal(
    macNoticeDiagnosticLine({ status: "working" }),
    "Mac notifications: working",
  );
  assert.equal(
    macNoticeDiagnosticLine({ status: "blocked_in_system_settings" }),
    "Mac notifications: blocked in System Settings",
  );
  assert.equal(
    macNoticeDiagnosticLine({ status: "failed", reason: "main_thread" }),
    "Mac notifications: failed (main_thread)",
  );
  assert.equal(
    macNoticeDiagnosticLine({ status: "failed" }),
    "Mac notifications: failed (unknown)",
  );
});
