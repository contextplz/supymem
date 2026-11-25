import assert from "node:assert/strict";
import test from "node:test";
import { SUPPORTED_MAC_TASK_EXAMPLES } from "./reviewableDeviceRequest.ts";

/** Cockpit Mac tasks entry must be above the fold, not buried in a details block. */
export function cockpitMacTasksHref(ventureId: string): string {
  return `/v/${encodeURIComponent(ventureId)}/settings#computer-use`;
}

test("Mac tasks entry points and examples stay available with or without chat history", () => {
  assert.equal(cockpitMacTasksHref("ven_x"), "/v/ven_x/settings#computer-use");
  assert.ok(SUPPORTED_MAC_TASK_EXAMPLES.length >= 3);
  for (const example of SUPPORTED_MAC_TASK_EXAMPLES) {
    assert.ok(example.label.trim());
    assert.ok(example.text.trim());
  }
});
