import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import test from "node:test";
import { apiRouteFeature, apiRouteTemplate } from "./apiRoute.ts";
import { CATALOG } from "./featureCatalog.ts";
import { CONTROL_OUTCOME_EVENT, armControlOutcome, claimControlOutcome, settleControlOutcome } from "./controlOutcome.ts";
import { nativeShellTelemetry } from "./nativeShellEvents.ts";
import { apiDiagnosticDetail, apiDiagnosticServerCode } from "./uiDiagnostics.ts";
import { usageFeature } from "./usageFeature.ts";

const SRC = join(import.meta.dirname, "..");
const walk = (dir: string): string[] => readdirSync(dir).flatMap(name => {
  const path = join(dir, name);
  return statSync(path).isDirectory() ? walk(path) : [path];
});

test("every founder page route maps to a catalog slug", () => {
  const app = join(SRC, "app");
  const pages = walk(app).filter(file => file.endsWith(`${sep}page.tsx`));
  assert.ok(pages.length > 30);
  const unmapped: string[] = [];
  for (const file of pages) {
    const route = "/" + relative(app, file).split(sep).slice(0, -1).map(part => part.replace(/^\[.+\]$/, "x")).join("/");
    if (route === "/admin" || route.startsWith("/admin/") || route === "/lab/diagrams") continue; // operator console, not a founder feature
    const feature = usageFeature(route === "/" ? "/" : route);
    if (!feature || !CATALOG.features.includes(feature)) unmapped.push(`${route} -> ${feature}`);
  }
  assert.deepEqual(unmapped, []);
  // The routes the audit found unlabeled.
  assert.equal(usageFeature("/settings/local"), "settings");
  assert.equal(usageFeature("/settings/local/capabilities"), "settings");
  assert.equal(usageFeature("/settings/local/support"), "settings");
  assert.equal(usageFeature("/settings/local/unknown-private-route"), null);
  assert.equal(usageFeature("/setup/permissions"), "permissions");
  assert.equal(usageFeature("/download"), "account");
  assert.equal(usageFeature("/invite/tok_private"), "team");
  assert.equal(usageFeature("/auth/device"), "account");
  assert.equal(usageFeature("/v/ven_1/build"), "builds");
  assert.equal(usageFeature("/v/ven_1/feedback"), "feedback");
  assert.equal(usageFeature("/v/ven_1/team"), "team");
  assert.equal(usageFeature("/v/ven_1/experiments"), "experiments");
  assert.equal(usageFeature("/v/ven_1/simulate"), "simulate");
  assert.equal(usageFeature("/admin/users"), null);
});

test("every control tagged in the console exists in the catalog", () => {
  const ids = new Set<string>();
  for (const file of walk(SRC).filter(f => /\.(tsx|ts)$/.test(f) && !/\.test\./.test(f))) {
    const text = readFileSync(file, "utf8");
    for (const match of text.matchAll(/data-usage-control=(?:"([a-z0-9_]+)"|\{([^}]*)\})/g)) {
      if (match[1]) ids.add(match[1]);
      else if (!match[2].includes("waitlistUsageControl")) {
        for (const quoted of match[2].replace(/[!=]==? *"[^"]*"/g, "").matchAll(/"([a-z][a-z0-9_]*)"/g)) ids.add(quoted[1]);
      }
    }
    for (const match of text.matchAll(/"data-usage-control": "([a-z0-9_]+)"/g)) ids.add(match[1]);
    for (const match of text.matchAll(/announceUsageControl\(([^)]*)\)/g)) {
      for (const quoted of match[1].matchAll(/"([a-z][a-z0-9_]*)"/g)) ids.add(quoted[1]);
    }
    for (const match of text.matchAll(/usageControl\("([a-z0-9_]+)"\)/g)) ids.add(match[1]);
  }
  assert.ok(ids.size > 100, `found only ${ids.size} tagged controls`);
  const missing = [...ids].filter(id => !(id in CATALOG.controls));
  assert.deepEqual(missing, []);
  // The controls the audit listed as untagged are now tagged somewhere (settings_autonomy_change is hidden in the beta).
  for (const id of ["settings_manager_rename", "settings_emergency_stop",
    "voice_mic_press", "greeting_shown", "mac_result_looks_right", "venture_delete", "venture_pause",
    "llm_settings_save", "team_org_activate", "next_step_action"]) assert.ok(ids.has(id), id);
  if (existsSync(join(SRC, "components/credits/TopUpCard.tsx"))) {
    for (const id of ["credits_request", "billing_topup"]) assert.ok(ids.has(id), id);
  }
});

test("catalog controls carry a catalog slug and a legal id", () => {
  assert.deepEqual(CATALOG.controls.content_draft_download, ["content", "content"]);
  assert.deepEqual(CATALOG.controls.website_export_html, ["website", null]);
  for (const [id, [slug, legacy]] of Object.entries(CATALOG.controls)) {
    assert.match(id, /^[a-z][a-z0-9_]{0,39}$/);
    assert.ok(CATALOG.features.includes(slug), id);
    if (legacy) assert.ok(CATALOG.features.includes(legacy), id);
  }
});

test("failed calls are attributed by route template, never by id", () => {
  assert.equal(apiRouteTemplate("/api/ventures/ven_abc123/plans/pln_9?x=secret@example.com"), "/ventures/{id}/plans/{id}");
  assert.equal(apiRouteTemplate("/api/ventures/ven_1/device-work-orders/dwo_2/approve"), "/ventures/{id}/device-work-orders/{id}/approve");
  assert.equal(apiRouteTemplate("/api/me/greeting"), "/me/greeting");
  assert.equal(apiRouteTemplate("https://evil.test/x"), null);
  assert.equal(apiRouteTemplate(`/api/${"a".repeat(90)}`), null);
  assert.equal(apiRouteFeature("/ventures/{id}/waitlist/test"), "waitlist");
  assert.equal(apiRouteFeature("/ventures/{id}/experiments/start"), "experiments");
  assert.equal(apiRouteFeature("/ventures/{id}/device-work-orders/{id}/approve"), "computer_use");
  assert.equal(apiRouteFeature("/admin/users"), null); // operator routes are not a founder feature
  assert.equal(apiRouteFeature("/totally/unknown"), null);
  const detail = apiDiagnosticDetail("/api/ventures/ven_1/plans/generate", 402, { error: { code: "insufficient_credits", message: "private text" } });
  assert.deepEqual(detail, { route: "/ventures/{id}/plans/generate", status: 402, serverCode: "insufficient_credits", feature: "planning" });
  assert.equal(apiDiagnosticServerCode({ detail: { error: { code: "Has Spaces And Text" } } }), null);
  assert.equal(apiDiagnosticServerCode({ detail: "waitlist_closed" }), "waitlist_closed");
});

test("a control click is answered once, by the first call it starts", () => {
  armControlOutcome("build_start", 1000);
  assert.equal(claimControlOutcome("GET", "/api/ventures/ven_1/dashboard", 1100), null); // a background poll is not the answer
  assert.equal(claimControlOutcome("POST", "/api/telemetry", 1100), null);
  assert.equal(claimControlOutcome("POST", "/api/ventures/ven_1/builds/start", 1200), "build_start");
  assert.equal(claimControlOutcome("POST", "/api/ventures/ven_1/builds/start", 1300), null);
  armControlOutcome("work_refresh", 1000);
  assert.equal(claimControlOutcome("GET", "/api/ventures/ven_1/tasks", 1100), "work_refresh");
  armControlOutcome("build_start", 1000);
  assert.equal(claimControlOutcome("POST", "/api/ventures/ven_1/builds/start", 9000), null); // too late to be this click
});

test("an outcome carries only status and the service's error code", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  const target = new EventTarget();
  const details: unknown[] = [];
  target.addEventListener(CONTROL_OUTCOME_EVENT, event => details.push((event as CustomEvent).detail));
  Object.defineProperty(globalThis, "window", { configurable: true, value: target });
  try {
    settleControlOutcome("build_start", 402, { error: { code: "insufficient_credits", message: "secret" } });
    settleControlOutcome("build_start", 200, { ok: true });
    settleControlOutcome(null, 500, null);
    assert.deepEqual(details, [
      { controlId: "build_start", ok: false, status: 402, serverCode: "insufficient_credits" },
      { controlId: "build_start", ok: true, status: 200, serverCode: null },
    ]);
  } finally {
    if (original) Object.defineProperty(globalThis, "window", original);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("native shell ring entries become closed telemetry and nothing else", () => {
  const events = nativeShellTelemetry([
    { kind: "activate", id: "tray_pause", t: 1 },
    { kind: "outcome", id: "tray_pause", outcome: "failed", t: 2 },
    { kind: "error", id: "device_work_order_action", t: 3 },
    { kind: "activate", id: "/Users/someone/private.txt" },
    { kind: "error", id: "not_a_command" },
    { kind: "outcome", id: "tray_pause", outcome: "maybe" },
    "junk", null,
  ]);
  assert.deepEqual(events.map(e => e.event), ["feature.used", "ui.control_activate", "ui.control_outcome", "ui.error"]);
  assert.deepEqual(events[1].props, { control_id: "tray_pause" });
  assert.equal(events[1].feature, "tray");
  assert.deepEqual(events[2].props, { control_id: "tray_pause", outcome: "failed" });
  assert.equal(events[3].feature, "computer_use");
  assert.equal(events[3].props.error_native_command, "device_work_order_action");
  assert.deepEqual(nativeShellTelemetry("nope"), []);
});
