import assert from "node:assert/strict";
import test from "node:test";
import { clearManualIdeaDraft, readManualIdeaDraft, writeManualIdeaDraft } from "./manualIdeaDraft.ts";

test("manual review survives an auth-gate remount and stays scoped to its revision", () => {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) || null,
    setItem: (key: string, value: string) => { data.set(key, value); },
    removeItem: (key: string) => { data.delete(key); },
  };
  const typed = { problem: "My bot", customer: "Only me", offer: "A personal helper" };
  writeManualIdeaDraft(storage, "ven_one", "rev_one", typed);
  assert.deepEqual(readManualIdeaDraft(storage, "ven_one", "rev_one"), typed);
  assert.equal(readManualIdeaDraft(storage, "ven_one", "rev_two"), null);
  assert.equal(readManualIdeaDraft(storage, "ven_two", "rev_one"), null);
  clearManualIdeaDraft(storage, "ven_one", "rev_one");
  assert.equal(readManualIdeaDraft(storage, "ven_one", "rev_one"), null);
});

test("invalid or unavailable storage never breaks manual review", () => {
  const broken = { getItem: () => "{", setItem: () => { throw Error("full"); }, removeItem: () => { throw Error("denied"); } };
  assert.equal(readManualIdeaDraft(broken, "ven", "rev"), null);
  assert.doesNotThrow(() => writeManualIdeaDraft(broken, "ven", "rev", { problem: "x", customer: "y", offer: "z" }));
  assert.doesNotThrow(() => clearManualIdeaDraft(broken, "ven", "rev"));
});
