import assert from "node:assert/strict";
import { test } from "node:test";
import { consoleErrorEvent } from "./errorEvent.ts";

test("console server error line has the standard shape and no request data", () => {
  const line = consoleErrorEvent(new TypeError("secret token abc"), "POST", "/ventures/[id]/waitlist", "route");
  assert.ok(line.startsWith("error_event "));
  const body = JSON.parse(line.slice("error_event ".length));
  assert.deepEqual(Object.keys(body).sort(), ["app_version", "code", "component", "intent", "source", "status"]);
  assert.equal(body.code, "TypeError");
  assert.equal(body.intent, "post_ventures_id_waitlist");
  assert.ok(!line.includes("secret"));
});
