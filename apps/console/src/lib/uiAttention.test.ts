import assert from "node:assert/strict";
import test from "node:test";
import { attentionKind } from "./uiAttention.ts";

test("setup response codes become founder attention without reading messages", () => {
  assert.equal(attentionKind({ error: { code: "composio_key_required", message: "never inspect this" } }), "provider_connection");
  assert.equal(attentionKind({ error: { code: "composio_api_key_missing" } }), "provider_connection");
  assert.equal(attentionKind({ detail: { code: "accessibility_permission_required" } }), "mac_permission");
  assert.equal(attentionKind({ error: { code: "build_worker_failed" } }), null);
});
