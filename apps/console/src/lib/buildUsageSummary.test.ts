import assert from "node:assert/strict";
import test from "node:test";
import { buildUsageSummary } from "./buildUsageSummary.ts";

test("terminal runs hide zero awaiting reconciliation", () => {
  assert.equal(
    buildUsageSummary({
      reserved: "0 credits",
      consumed: "15.3594 credits",
      awaiting: "0 credits",
      unresolvedMicro: 0,
      terminal: true,
    }),
    "Reserved: 0 credits · Consumed: 15.3594 credits",
  );
  assert.equal(
    buildUsageSummary({
      reserved: "1 credits",
      consumed: "0 credits",
      awaiting: "1 credits",
      unresolvedMicro: 1_000_000,
      terminal: true,
    }),
    "Reserved: 1 credits · Consumed: 0 credits · Awaiting reconciliation: 1 credits",
  );
});
