import assert from "node:assert/strict";
import test from "node:test";
import { FRESH_MS, MAX_STALE_MS, cacheClear, cacheGet, cacheInvalidate, cachePut, cacheable, mutationInvalidates, ventureOf } from "./apiCache.ts";

test("entries are scoped per subject", () => {
  cacheClear();
  cachePut("usr_a", "/api/ventures", { n: 1 });
  assert.deepEqual(cacheGet("usr_a", "/api/ventures")?.data, { n: 1 });
  assert.equal(cacheGet("usr_b", "/api/ventures"), null);
  assert.equal(cacheGet(null, "/api/ventures"), null);
});

test("fresh, stale and expired by TTL", () => {
  cacheClear();
  cachePut("s", "/api/x", 1, 1000);
  assert.equal(cacheGet("s", "/api/x", 1000 + FRESH_MS - 1)?.fresh, true);
  assert.equal(cacheGet("s", "/api/x", 1000 + FRESH_MS + 1)?.fresh, false);
  assert.equal(cacheGet("s", "/api/x", 1000 + MAX_STALE_MS + 1), null);
});

test("mutation invalidates only the venture's entries; no venture clears the scope", () => {
  cacheClear();
  cachePut("s", "/api/ventures/v1/tasks", 1);
  cachePut("s", "/api/ventures/v2/tasks", 2);
  cachePut("t", "/api/ventures/v1/tasks", 3);
  assert.equal(ventureOf("/api/ventures/v1/idea/sessions"), "v1");
  cacheInvalidate("s", "v1");
  assert.equal(cacheGet("s", "/api/ventures/v1/tasks"), null);
  assert.equal(cacheGet("s", "/api/ventures/v2/tasks")?.data, 2);
  assert.equal(cacheGet("t", "/api/ventures/v1/tasks")?.data, 3);
  cacheInvalidate("s");
  assert.equal(cacheGet("s", "/api/ventures/v2/tasks"), null);
});

test("auth and secret paths are never cached; clear forgets everything", () => {
  cacheClear();
  for (const p of ["/auth/session", "/api/telemetry", "/api/ventures/v1/api-keys", "/api/ventures/v1/connections/token"]) {
    assert.equal(cacheable(p), false, p);
    cachePut("s", p, 1);
    assert.equal(cacheGet("s", p), null);
  }
  cachePut("s", "/api/ventures", 1);
  cacheClear();
  assert.equal(cacheGet("s", "/api/ventures"), null);
});

test("telemetry posts do not invalidate the cache, other mutations do", () => {
  assert.equal(mutationInvalidates("/api/telemetry"), false);
  assert.equal(mutationInvalidates("/api/ventures/ven_1/plans"), true);
  assert.equal(mutationInvalidates("/gov/autonomy"), true);
});
