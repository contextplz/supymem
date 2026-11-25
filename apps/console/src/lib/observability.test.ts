import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { consoleRoute, deadEndCode, ideaObservationStep, OBSERVABILITY_EVENT, observeFounder, startDeadEndObserver } from "./observability.ts";
import { enqueueTelemetry } from "./usageDelivery.ts";

function element(attributes: Record<string, string>, action = false, hidden = false): Element {
  return { getAttribute: (name: string) => attributes[name] ?? null,
    hasAttribute: (name: string) => name in attributes,
    closest: () => hidden ? {} : null, querySelector: () => action ? {} : null,
    querySelectorAll: () => action ? [{ closest: () => null }] : [],
    matches: () => attributes.role === "alert" || "data-error-notice" in attributes } as unknown as Element;
}

test("route labels reject content, queries, IDs and unknown paths", () => {
  assert.equal(consoleRoute("/v/private-founder/idea?token=secret#prompt"), "/v/{id}/idea");
  assert.equal(consoleRoute("/v/private/unknown-product"), "/other");
  assert.equal(consoleRoute("/gtm?email=private@example.com"), "/gtm");
  assert.equal(consoleRoute("/private@example.com"), "/other");
  assert.equal(consoleRoute("/v/private/device-work-orders/dwo_private"), "/other");
  assert.equal(consoleRoute("/auth/private-secret"), "/auth");
  assert.equal(consoleRoute("/invite/private-secret"), "/other");
});

test("errors, reconnect and empty states preserve usable actions and hidden-state exclusions", () => {
  assert.equal(deadEndCode(element({ role: "alert" })), "ui_error_shown");
  assert.equal(deadEndCode(element({ "data-dead-end": "reconnect_shown" }, true)), "reconnect_shown");
  assert.equal(deadEndCode(element({ "data-dead-end": "empty_no_action" })), "empty_no_action");
  assert.equal(deadEndCode(element({ "data-dead-end": "empty_no_action" }, true)), null);
  assert.equal(deadEndCode(element({ role: "alert" }, false, true)), null);
  assert.equal(deadEndCode(element({ class: "panel_error__hash" })), "ui_error_shown");
  assert.equal(deadEndCode(Object.assign(element({ role: "alert" }), { getClientRects: () => [] })), null);
  assert.equal(deadEndCode(Object.assign(element({ "data-dead-end": "empty_no_action" }), {
    querySelectorAll: () => [{ closest: () => null, getClientRects: () => [] }],
  })), "empty_no_action");
  assert.equal(ideaObservationStep("validating"), "validate");
  assert.equal(ideaObservationStep("reviewing"), "review");
  assert.equal(ideaObservationStep("abandoned"), null);
});

test("observer scans initial banners and journey state, not unrelated mutations", async () => {
  const original = { document: globalThis.document, MutationObserver: globalThis.MutationObserver, window: globalThis.window };
  const dispatched: unknown[] = [];
  let mutated: () => void = () => {};
  const banner = Object.assign(element({ role: "alert" }), { textContent: "private content" });
  const journey = Object.assign(element({ "data-journey-step": "clarify", "data-journey-venture": "ven_obs" }), { textContent: "private idea" });
  globalThis.document = { body: {}, querySelectorAll: () => [banner, journey] } as unknown as Document;
  globalThis.window = { dispatchEvent: (event: CustomEvent) => { dispatched.push(event.detail); return true; } } as unknown as Window & typeof globalThis;
  globalThis.MutationObserver = class { constructor(callback: () => void) { mutated = callback; } observe() {} disconnect() {} } as unknown as typeof MutationObserver;
  let stop: (() => void) | undefined;
  try {
    stop = startDeadEndObserver();
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.deepEqual(dispatched, [{ event: "ui.dead_end", code: "ui_error_shown" },
      { event: "journey.step_entered", step: "clarify", ventureId: "ven_obs" }]);
    mutated(); await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(dispatched.length, 2);
    banner.textContent = "another error";
    mutated(); await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(dispatched.length, 3);
    assert.ok(!JSON.stringify(dispatched).includes("private"));
  } finally {
    stop?.(); Object.assign(globalThis, original);
  }
});

test("dead ends use the existing content-free delivery queue and priority", () => {
  const queue = [{ attempts: 0, body: { event: "ui.screen_view" } }];
  enqueueTelemetry(queue, { attempts: 0, body: { event: "ui.dead_end" } }, 1);
  enqueueTelemetry(queue, { attempts: 0, body: { event: "session.active" } }, 1);
  assert.equal(queue[0].body.event, "ui.dead_end");
  assert.equal(OBSERVABILITY_EVENT, "supymem-observability");
  assert.doesNotThrow(() => observeFounder({ event: "ui.dead_end", code: "action_failed" }));
});

test("console wires failed controls, all journey stages and native traces", () => {
  const read = (path: string) => fs.readFileSync(new URL(path, import.meta.url), "utf8");
  const tracker = read("../components/layout/UsageTracker.tsx");
  assert.match(tracker, /CONTROL_OUTCOME_EVENT, failedAction/);
  assert.match(tracker, /code: "action_failed"/);
  assert.match(tracker, /error_text: u.context === "voice" \? "Voice error shown" : "UI error shown"/);
  assert.match(read("../app/v/[ventureId]/idea/page.tsx"), /data-journey-step=/);
  assert.match(read("../components/gtm/GtmJourney.tsx"), /step: "review_leads"/);
  assert.match(read("../components/gtm/GtmJourney.tsx"), /data-journey-step=/);
  // Web requests carry a trace id; the native bridge must not (released Mac apps deny unknown headers).
  assert.doesNotMatch(read("./client.ts"), /headers\["X-Request-ID"\]/);
  assert.doesNotMatch(read("./nativeTransport.ts"), /headers\["X-Request-ID"\]/);
  assert.match(read("./client.ts"), /headers.set\("X-Request-ID", newRequestId\(\)\)/);
  assert.match(read("../components/primitives/EmptyState.tsx"), /empty_no_action/);
});

const adminPage = new URL("../app/admin/analytics/observability/page.tsx", import.meta.url);
test("hosted console admin exposes both observability windows", {skip: !fs.existsSync(adminPage)}, () => {
  const admin = fs.readFileSync(adminPage, "utf8");
  assert.match(admin, /value=\{24\}/); assert.match(admin, /value=\{168\}/);
  assert.match(admin, /data.computer_outcomes/);
});
