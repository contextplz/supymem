import assert from "node:assert/strict";
import test from "node:test";
import { isOtherVentureNotice, trustedDesktopNoticeTarget } from "./desktopNoticeTarget.ts";

test("accepts same-app notice targets and rejects open redirects", () => {
  assert.equal(
    trustedDesktopNoticeTarget("/v/ven_1/device-work-orders/dwo_exact-2"),
    "/v/ven_1/device-work-orders/dwo_exact-2",
  );
  assert.equal(trustedDesktopNoticeTarget("/v/ven_1/now"), "/v/ven_1/now");
  assert.equal(trustedDesktopNoticeTarget("/v/ven_1#attention-ht_exact"), "/v/ven_1#attention-ht_exact");
  assert.equal(
    trustedDesktopNoticeTarget("/v/ven_1/device-work-orders/dwo_good?next=https://evil.test"),
    null,
  );
  assert.equal(trustedDesktopNoticeTarget("https://evil.test/v/ven_1/now"), null);
});

test("notice scope follows the exact trusted venture rather than the selected page", () => {
  assert.equal(isOtherVentureNotice("/v/ven_a/now", "/v/ven_b/builds"), true);
  assert.equal(isOtherVentureNotice("/v/ven_a/now", "/v/ven_a/builds"), false);
  assert.equal(isOtherVentureNotice("/v/ven_a/now", "/v/ven_ab/builds"), true);
  assert.equal(isOtherVentureNotice("/v/ven_a#attention-ht_one", "/v/ven_b"), true);
  assert.equal(isOtherVentureNotice("/v/ven_a/device-work-orders/dwo_one", "/v/ven_b"), true);
  assert.equal(isOtherVentureNotice("https://evil.test/v/ven_a/now", "/v/ven_b"), false);
  assert.equal(isOtherVentureNotice("/v/ven_a/now?next=/v/ven_b", "/v/ven_b"), false);
  assert.equal(isOtherVentureNotice(null, "/v/ven_b"), false);
  assert.equal(isOtherVentureNotice("/v/ven_a/now", "/account"), false);
});

test("new venture screens hide unrelated notices, including before a venture is selected", () => {
  assert.equal(isOtherVentureNotice("/v/ven_gyms/now", "/new", undefined), true);
  assert.equal(isOtherVentureNotice("/v/ven_gyms/now", "/new", "ven_coffee"), true);
  assert.equal(isOtherVentureNotice("/v/ven_coffee/now", "/new", "ven_coffee"), false);
  assert.equal(isOtherVentureNotice("/v/ven_gyms#attention-ht_one", "/new"), true);
  assert.equal(isOtherVentureNotice(null, "/new"), false);
  assert.equal(isOtherVentureNotice("/v/ven_gyms/now", "/", undefined), false);
});
