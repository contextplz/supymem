import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = readFileSync(join(root, "components/layout/UsageTracker.tsx"), "utf8");

test("UsageTracker persists a durable offline queue without content fields", () => {
  assert.match(source, /supymem\.usage\.queue\.v1/);
  assert.match(source, /localStorage/);
  assert.match(source, /No chat text/);
  assert.doesNotMatch(source, /innerHTML|document\.body\.textContent|prompt\b/);
  assert.match(source, /MAX_QUEUE = 256/);
});
