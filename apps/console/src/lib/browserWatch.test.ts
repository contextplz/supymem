import assert from "node:assert/strict";
import test from "node:test";
import { browserWatchView, needsYouInstruction, nativePollMs, SIGN_IN_PROMPT } from "./browserWatch.ts";

const frame = { seq: 1, step: 3, label: "Clicking", jpeg_b64: "/9j/4AAQSkZJRg==", cursor: { x: 640, y: 450 }, viewport: { w: 1280, h: 900 } };

test("a frame renders as a JPEG data URL with the agent cursor at its viewport position", () => {
  const view = browserWatchView(frame, "acting");
  assert.ok(view);
  assert.equal(view.src, "data:image/jpeg;base64,/9j/4AAQSkZJRg==");
  assert.deepEqual(view.cursor, { left: 50, top: 50 });
  assert.equal(view.step, 3);
  assert.equal(view.label, "Clicking");
});

test("unknown cursor target and off-screen coordinates never break the overlay", () => {
  assert.equal(browserWatchView({ ...frame, cursor: null }, "acting")?.cursor, null);
  assert.equal(browserWatchView({ ...frame, viewport: null }, "acting")?.cursor, null);
  assert.deepEqual(browserWatchView({ ...frame, cursor: { x: 5000, y: -10 } }, "acting")?.cursor, { left: 100, top: 0 });
});

test("no frame is shown while a Needs-you card is open", () => {
  assert.equal(browserWatchView(frame, "needs_you"), null);
  assert.equal(browserWatchView(undefined, "acting"), null);
});

test("malformed or oversized frames are refused instead of reaching an img src", () => {
  assert.equal(browserWatchView({ ...frame, jpeg_b64: '" onerror="x' }, "acting"), null);
  assert.equal(browserWatchView({ ...frame, jpeg_b64: "" }, "acting"), null);
  assert.equal(browserWatchView({ ...frame, jpeg_b64: "A".repeat(200_001) }, "acting"), null);
});

test("polling is fast only while frames stream", () => {
  assert.ok(nativePollMs(true) < nativePollMs(false));
});

test("sign-in card tells the founder to sign in inside the Supymem browser, then Continue", () => {
  const text = needsYouInstruction("sign_in", "message_only");
  assert.ok(text.startsWith(SIGN_IN_PROMPT));
  assert.match(text, /no screenshots/);
  assert.match(needsYouInstruction("captcha", "visible"), /Finish the step there/);
  assert.match(needsYouInstruction(undefined, undefined), /Open Supymem browser/);
});
