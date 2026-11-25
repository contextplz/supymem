import assert from "node:assert/strict";
import { test } from "node:test";
import { formatCreditBalance, formatCredits, formatMicroCreditUsage } from "./credits.ts";

test("founder-facing credit amounts use the full unit name", () => {
  assert.equal(formatCredits(1500), "1,500 Credits");
  assert.equal(formatCreditBalance(498.79), "498.79 Credits");
  assert.equal(formatCreditBalance(NaN), "0 Credits");
});

test("usage receipts retain the canonical 10,000 micro-units per credit", () => {
  assert.equal(formatMicroCreditUsage(10_000), "1 credits");
  assert.equal(formatMicroCreditUsage(1_000_000), "100 credits");
  assert.equal(formatMicroCreditUsage(1), "Under 0.01 credits");
  assert.equal(formatMicroCreditUsage(3_081_536), "308.15 credits");
});

test("invalid usage is not displayed as a free operation", () => {
  for (const value of [NaN, Infinity, -1, 0.5]) assert.equal(formatMicroCreditUsage(value), "Usage unavailable");
  assert.equal(formatMicroCreditUsage(0), "0 credits");
});
