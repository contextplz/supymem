import assert from "node:assert/strict";
import test from "node:test";

test("prefill fills an open composer and asks a closed panel to open and replay it", async () => {
  const seen: Array<{ type: string; detail: unknown }> = [];
  const target = new EventTarget();
  (globalThis as unknown as { window: EventTarget }).window = target;
  for (const type of ["supymem-chat-prefill", "supymem:ask"]) {
    target.addEventListener(type, (event) => seen.push({ type, detail: (event as CustomEvent).detail }));
  }
  const { prefillChat, PREFILL_ACTION } = await import("./chatPrefill.ts");
  prefillChat("I ran the test: X. What happened: ");
  assert.deepEqual(seen, [
    { type: "supymem-chat-prefill", detail: { text: "I ran the test: X. What happened: " } },
    { type: "supymem:ask", detail: { action: PREFILL_ACTION, payload: { text: "I ran the test: X. What happened: " } } },
  ]);
});
