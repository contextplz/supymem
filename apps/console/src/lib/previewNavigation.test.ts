import assert from "node:assert/strict";
import test from "node:test";
import { openablePreviewUrl, previewWaitlistUrl } from "./previewNavigation.ts";

test("embedded preview offers only web URLs for browser review", () => {
  assert.equal(openablePreviewUrl("https://site.example/landing"), "https://site.example/landing");
  assert.equal(openablePreviewUrl("javascript:alert(1)"), undefined);
  assert.equal(openablePreviewUrl("file:///tmp/index.html"), undefined);
  assert.equal(openablePreviewUrl("https://name:secret@site.example/"), undefined);
});

test("managed waitlist link requires an enabled response for the same venture", () => {
  const result = {venture_id: "ven_one", enabled: true, url: "https://gateway.example/public/waitlists/ven_one",
    submission_verified: true, total: 0};
  assert.equal(previewWaitlistUrl(result, "ven_one"), result.url);
  assert.equal(previewWaitlistUrl(result, "ven_other"), undefined);
  assert.equal(previewWaitlistUrl({...result, enabled: false}, "ven_one"), undefined);
  assert.equal(previewWaitlistUrl({...result, url: "javascript:alert(1)"}, "ven_one"), undefined);
});
