import test from "node:test";
import assert from "node:assert/strict";
import { forgetWaitlistStatus, lastWaitlistStatus, rememberWaitlistStatus, scopedWaitlistStatus, scopedWaitlistUrl, verifiedPublishedSiteUrl, waitlistReadFailure, waitlistStatusDuringCooldown } from "./waitlistStatusCache.ts";

test("a founder can copy only a genuinely published page after the exact venture probe", () => {
  const ready = {venture_id: "ven_one", enabled: true, submission_verified: true,
    url: "https://gateway.example/public/waitlists/ven_one", total: 0,
    published_site: {source: "deployment" as const, id: "dep_one", url: "https://site.example/ven_one"}};
  assert.equal(verifiedPublishedSiteUrl(ready, true), ready.published_site.url);
  assert.equal(verifiedPublishedSiteUrl(ready, false), undefined);
  assert.equal(verifiedPublishedSiteUrl({...ready, published_site: null}, true), undefined);
  assert.equal(verifiedPublishedSiteUrl({...ready, submission_verified: false}, true), undefined);
  assert.equal(verifiedPublishedSiteUrl({...ready, enabled: false}, true), undefined);
  assert.equal(scopedWaitlistUrl({...ready, url: "https://gateway.example/public/waitlists/ven_other"}), undefined);
  assert.equal(verifiedPublishedSiteUrl({...ready, url: `${ready.url}?token=private`}, true), undefined);
  assert.equal(verifiedPublishedSiteUrl({...ready, published_site: {...ready.published_site, url: "http://site.example/ven_one"}}, true), undefined);
  assert.equal(verifiedPublishedSiteUrl({...ready, published_site: {...ready.published_site, url: "https://user:pass@site.example/ven_one"}}, true), undefined);
});

test("a committed waitlist toggle accepts its scoped status without a follow-up read", () => {
  const response = {
    venture_id: "ven_toggle", enabled: true, url: "https://gateway.example/public/waitlists/ven_toggle",
    submission_verified: false, total: 0, probe_retry_after_seconds: 0,
  };
  assert.equal(scopedWaitlistStatus(response, "ven_toggle"), response);
  assert.equal(scopedWaitlistStatus({...response, enabled: false}, "ven_toggle")?.enabled, false);
  assert.equal(scopedWaitlistStatus(response, "ven_other"), null);
  assert.equal(scopedWaitlistStatus({...response, total: undefined}, "ven_toggle"), null);
});

test("a GTM probe leaves a private summary and cooldown for Website navigation", () => {
  const subject = "founder-gtm-probe";
  const checkedAt = 1_000_000;
  const probeResponse = {
    venture_id: "ven_probe", enabled: true, url: "https://gateway.example/public/waitlists/ven_probe",
    submission_verified: true, total: 3, probe_retry_after_seconds: 60,
    leads: [{email: "private@example.org"}],
  };
  rememberWaitlistStatus(subject, probeResponse, checkedAt);

  const website = lastWaitlistStatus(subject, "ven_probe");
  assert.equal(website?.data.total, 3);
  assert.equal(website?.data.submission_verified, true);
  assert.equal(website?.cooldownUntil, checkedAt + 60_000);
  assert.equal("leads" in (website?.data || {}), false);
  assert.equal(lastWaitlistStatus("another-account", "ven_probe"), undefined);
});

test("a rate-limited status read preserves the last count with retry guidance", () => {
  const saved = rememberWaitlistStatus("founder-status-limit", {
    venture_id: "ven_limit", enabled: true, url: "https://gateway.example/public/waitlists/ven_limit",
    submission_verified: true, total: 4, probe_retry_after_seconds: 40,
  }, 1_000_000);
  assert.ok(saved);
  const failure = waitlistReadFailure(429, saved.data);
  assert.equal(failure?.data?.total, 4);
  assert.match(failure?.notice || "", /Showing the last loaded status/);
  assert.match(waitlistReadFailure(429, null)?.notice || "", /refresh the waitlist status/);
  assert.equal(waitlistReadFailure(403, saved.data), null);
});

test("Website can reuse a private GTM cooldown snapshot after navigation or reload", () => {
  const values = new Map<string, string>();
  const original = globalThis.sessionStorage;
  Object.defineProperty(globalThis, "sessionStorage", {configurable: true, value: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  }});
  try {
    const now = Date.now();
    const subject = "founder-cross-surface";
    const ventureId = "ven_cross_surface";
    rememberWaitlistStatus(subject, {
      venture_id: ventureId, enabled: true,
      url: `https://gateway.example/public/waitlists/${ventureId}`,
      submission_verified: true, total: 2, probe_retry_after_seconds: 60,
      leads: [{email: "private@example.org"}],
    } as Parameters<typeof rememberWaitlistStatus>[1], now);
    const stored = [...values.values()][0];
    assert.ok(stored);
    assert.doesNotMatch(stored, /private@example.org/);
    assert.equal(waitlistStatusDuringCooldown(subject, ventureId, now + 1_000)?.data.submission_verified, true);
    assert.equal(waitlistStatusDuringCooldown(subject, ventureId, now + 60_000), undefined);
    assert.equal(waitlistStatusDuringCooldown("another-founder", ventureId, now + 1_000), undefined);
    const storedKey = [...values.keys()][0];
    const reloadedVenture = "ven_after_reload";
    values.set(storedKey.replace(ventureId, reloadedVenture), stored.replaceAll(ventureId, reloadedVenture));
    assert.equal(waitlistStatusDuringCooldown(subject, reloadedVenture, now + 1_000)?.data.submission_verified, true);
    forgetWaitlistStatus(subject, ventureId);
    forgetWaitlistStatus(subject, reloadedVenture);
    assert.equal(values.size, 0);
  } finally {
    if (original === undefined) Reflect.deleteProperty(globalThis, "sessionStorage");
    else Object.defineProperty(globalThis, "sessionStorage", {configurable: true, value: original});
  }
});
