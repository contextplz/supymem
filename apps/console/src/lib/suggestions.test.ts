import assert from "node:assert/strict";
import test from "node:test";
import {
  DECISION_LABEL, DEFAULT_PREFS, badgeLabel, hourLabel, pausedNote, sanitizePrefs, statusLabel,
  snoozeSuggestions, suggestionsSnoozed, suggestionPlan, type Suggestion,
} from "./suggestions.ts";

test("the three buttons keep the founder's words", () => {
  assert.deepEqual(DECISION_LABEL, { accept: "Sure, go ahead", save: "Save for later", dismiss: "Dismiss" });
});

test("Later is stored per user until next local midnight and tolerates unavailable storage", () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "window");
  const values = new Map<string, string>();
  const now = new Date(2026, 9, 11, 15).getTime();
  const midnight = new Date(2026, 9, 12).getTime();
  try {
    Object.defineProperty(globalThis, "window", { configurable: true, value: { localStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    } } });
    snoozeSuggestions("usr_a", now);
    assert.equal(suggestionsSnoozed("usr_a", now), true);
    assert.equal(suggestionsSnoozed("usr_b", now), false);
    assert.equal(suggestionsSnoozed("usr_a", midnight), false);
    values.set("supymem-suggestions-later:usr_a", "bad");
    assert.equal(suggestionsSnoozed("usr_a", now), false);
    Object.defineProperty(globalThis, "window", { configurable: true, value: {
      get localStorage() { throw new Error("Storage denied"); },
    } });
    assert.doesNotThrow(() => snoozeSuggestions("usr_a", now));
    assert.equal(suggestionsSnoozed("usr_a", now), false);
  } finally {
    if (previous) Object.defineProperty(globalThis, "window", previous);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("legacy tautological plans use step detail, while concrete plans remain unchanged", () => {
  const item = { title: "Grow what's already working", why: "Test the next repeatable acquisition channel." } as Suggestion;
  assert.equal(suggestionPlan({ ...item, plan_text: `Runs the step '${item.title}' for you.` }), item.why);
  assert.equal(suggestionPlan({ ...item, plan_text: item.title }), item.why);
  assert.equal(suggestionPlan({ ...item, plan_text: "Draft outreach messages. Nothing is sent." }), "Draft outreach messages. Nothing is sent.");
});

test("badge hides at zero and caps at 9+", () => {
  assert.equal(badgeLabel(0), "");
  assert.equal(badgeLabel(-3), "");
  assert.equal(badgeLabel(Number.NaN), "");
  assert.equal(badgeLabel(3), "3");
  assert.equal(badgeLabel(10), "9+");
});

test("preferences are clamped to what the Mac app accepts", () => {
  assert.deepEqual(sanitizePrefs(null), DEFAULT_PREFS);
  const p = sanitizePrefs({ min_gap_minutes: 1, quiet_start_hour: 99, quiet_end_hour: -1, paused_until: -5, enabled: false });
  assert.equal(p.min_gap_minutes, 15);
  assert.equal(p.quiet_start_hour, DEFAULT_PREFS.quiet_start_hour);
  assert.equal(p.quiet_end_hour, DEFAULT_PREFS.quiet_end_hour);
  assert.equal(p.paused_until, 0);
  assert.equal(p.enabled, false);
  assert.equal(sanitizePrefs({ min_gap_minutes: 99999 }).min_gap_minutes, 720);
});

test("pause note only while the pause is in the future", () => {
  assert.equal(pausedNote({ ...DEFAULT_PREFS, paused_until: 500 }, 1_000_000), "");
  assert.match(pausedNote({ ...DEFAULT_PREFS, paused_until: 2000 }, 1_000), /Paused for today/);
});

test("labels read plainly", () => {
  assert.equal(hourLabel(0), "12:00 AM");
  assert.equal(hourLabel(21), "9:00 PM");
  assert.equal(hourLabel(12), "12:00 PM");
  assert.equal(statusLabel("failed"), "Did not finish");
});

test("history distinguishes committed completion from queued, blocked, uncertain and legacy outcomes", () => {
  assert.equal(statusLabel("done", "completion_verified"), "Done");
  assert.equal(statusLabel("done", "done"), "Completion unconfirmed");
  assert.equal(statusLabel("done"), "Completion unconfirmed");
  assert.equal(statusLabel("accepted", "queued"), "Needs you · verify the saved outcome");
  assert.equal(statusLabel("accepted", "in_progress"), "Needs you · verify the saved outcome");
  assert.equal(statusLabel("accepted", "needs_you"), "Needs you");
  assert.equal(statusLabel("accepted", "outcome_unconfirmed"), "Outcome unconfirmed");
  assert.equal(statusLabel("accepted", "mac_review_required"), "Ready for Mac review");
  assert.equal(statusLabel("accepted", "mac_prepare_required"), "Mac review must be prepared");
});

test("live outcomes are explicit and never infer running from an old acceptance", () => {
  const accepted = { status: "accepted", result_state: "running" } as const;
  assert.equal(statusLabel(accepted.result_state || accepted.status), "Running");
  assert.equal(statusLabel("running"), "Running");
  assert.equal(statusLabel("needs_you"), "Needs you");
  assert.equal(statusLabel("noop"), "Already done, nothing to do");
  assert.equal(statusLabel("done", JSON.stringify({ outcome: "done", jobs: ["job"] })), "Done");
});
