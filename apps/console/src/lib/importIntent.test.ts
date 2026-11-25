import test from "node:test";
import assert from "node:assert/strict";
import { activeImport, importIntentKey, readImportIntent, saveImportIntent } from "./importIntent.ts";
import { contentRequestFence } from "./contentIntent.ts";
test("import intent is bound to account and exact selected app", () => {
  const owner = { actor: "one", workspace: "w", venture: "v", app: "a" };
  const request = { idempotency_key: "same-request", source: { source: "github_public" as const, repo_url: "https://github.com/pallets/flask" } };
  const raw = saveImportIntent(owner, request);
  assert.deepEqual(readImportIntent(raw, owner), request);
  for (const key of ["actor", "workspace", "venture", "app"]) {
    const changed = { ...owner, [key]: "other" };
    assert.notEqual(importIntentKey(changed), importIntentKey(owner));
    assert.equal(readImportIntent(raw, changed), null);
  }
  assert.equal(readImportIntent("{broken", owner), null);
});
test("account changes invalidate async requests even when returning to same company", () => {
  const fence = contentRequestFence(); fence.activate("owner:app"); const current = fence.capture();
  assert.equal(current(), true); fence.invalidate(); fence.activate("owner:app"); assert.equal(current(), false);
  assert.equal(activeImport("report_complete"), false); assert.equal(activeImport("needs_reconciliation"), false); assert.equal(activeImport("queued"), true);
});
