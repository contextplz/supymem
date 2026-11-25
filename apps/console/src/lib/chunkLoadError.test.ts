import assert from "node:assert/strict";
import test from "node:test";

import { canReloadNow, isChunkLoadError } from "./chunkLoadError.ts";

test("detects chunk load failures", () => {
  assert.equal(isChunkLoadError({ name: "ChunkLoadError", message: "x" }), true);
  assert.equal(isChunkLoadError(new Error("Loading chunk 1306 failed.\n(error: https://a/_next/x.js)")), true);
  assert.equal(isChunkLoadError(new Error("Loading CSS chunk 9 failed")), true);
  assert.equal(isChunkLoadError("Failed to fetch dynamically imported module: https://a/b.js"), true);
});

test("ignores other errors", () => {
  assert.equal(isChunkLoadError(new Error("boom")), false);
  assert.equal(isChunkLoadError(null), false);
});

test("reload guard allows once per 30s", () => {
  assert.equal(canReloadNow(0, 1_700_000_000_000), true);
  assert.equal(canReloadNow(1000, 5000), false);
  assert.equal(canReloadNow(1000, 31_001), true);
});
