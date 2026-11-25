import assert from "node:assert/strict";
import test from "node:test";
import { announceDiagnostic, apiDiagnostic, apiDiagnosticDetail, apiDiagnosticMessage, apiDiagnosticOperation, apiDiagnosticOrderId, apiDiagnosticReference, closedNativeDiagnosticCommand, diagnosticFingerprint, native503Origin, nativeDiagnosticMessage, nativeDiagnosticSeverity, sanitizeDiagnostic } from "./uiDiagnostics.ts";

test("rate limits show only the bounded retry time, never response text", () => {
  assert.equal(apiDiagnosticMessage(429, { error: { code: "rate_limited", retry_after: 17, message: "secret" } }), "You're going a bit fast - try again in 17 seconds");
  assert.equal(apiDiagnosticMessage(429, { detail: { retry_after: 900 } }), "You're going a bit fast - try again in 900 seconds");
  assert.equal(apiDiagnosticMessage(429, { error: { retry_after: "secret" } }), "You're going a bit fast - try again in 60 seconds");
  assert.equal(sanitizeDiagnostic(apiDiagnosticMessage(429, { error: { retry_after: 17 } })), "You're going a bit fast - try again in 17 seconds");
});

test("diagnostics only retain closed technical descriptions", () => {
  const raw = 'Error: GET https://example.test/path?token=private user=a@b.test api_key=secret123 message="private chat" /Users/person/private/file.ts';
  const clean = sanitizeDiagnostic(raw);
  for (const sensitive of ["example.test", "private", "a@b.test", "secret123", "/Users/person"]) {
    assert.ok(!clean.includes(sensitive), sensitive);
  }
  assert.equal(clean, "Runtime failure");
  assert.equal(sanitizeDiagnostic("x".repeat(5000)), "Runtime failure");
  for (const secret of ["vck_1234567890123456", "apikey_1234567890123456", "ak_1234567890123456", "AKIA1234567890123456", "xoxb-123456789012-abcdef", "gho_abc123456789", "AIzaabc123456789", "founder prompt: build my medical intake app"]) {
    assert.ok(!sanitizeDiagnostic(`failure ${secret}`).includes(secret));
  }
  assert.equal(sanitizeDiagnostic("API request_failed (status 500)"), "API request_failed (status 500)");
  assert.equal(sanitizeDiagnostic("Integrity failure"), "Integrity failure");
});

test("setup and permission responses are attention states", () => {
  for (const status of [400, 401, 403, 404, 409, 422, 429]) assert.equal(apiDiagnostic(status), "request_error");
  assert.equal(apiDiagnostic(404, { detail: { error: { code: "no_onboarding" } } }, "venture_onboarding_read"), null);
  assert.equal(apiDiagnostic(404, { detail: "no_charter" }, "venture_charter_read"), null);
  assert.equal(apiDiagnostic(404, { detail: { error: { code: "no_site" } } }, "workspace_site_read"), null);
  assert.equal(apiDiagnostic(404, { detail: { error: { code: "no_site" } } }, "venture_waitlist_read"), "request_error");
  assert.equal(apiDiagnostic(404, { detail: { error: { code: "not_found" } } }, "workspace_site_read"), "request_error");
  assert.equal(apiDiagnostic(404, { detail: "no_charter" }), "request_error");
  assert.equal(apiDiagnostic(500, { detail: { error: { code: "no_onboarding" } } }), "server_error");
  assert.equal(apiDiagnostic(0), "network_error");
  assert.equal(apiDiagnostic(503), "server_error");
  assert.equal(apiDiagnostic(503, { error: { code: "provider_key_required" } }), null);
  assert.equal(apiDiagnostic(400, { error: { code: "composio_api_key_missing" } }), null);
  assert.equal(apiDiagnostic(400, { error: { code: "gmail_not_connected" } }), null);
  assert.equal(apiDiagnostic(401, { error: { code: "invalid_api_key" } }), null);
  assert.equal(apiDiagnostic(503, { detail: { code: "bad_api_key" } }), null);
  assert.equal(apiDiagnostic(401, { error: { code: "reauth_required" } }), null);
  assert.equal(apiDiagnostic(503, { error: { code: "secure_store_pending" } }), null);
  assert.equal(apiDiagnostic(400, { error: { code: "build_worker_failed" } }), "request_error");
  assert.equal(apiDiagnostic(403, { error: { code: "forbidden" } }), "request_error");
});

test("fingerprint is stable for repeated failures", () => {
  assert.equal(diagnosticFingerprint("server_error", "api", "API request_failed (status 503)", "chat"), diagnosticFingerprint("server_error", "api", "API request_failed (status 503)", "chat"));
  assert.notEqual(diagnosticFingerprint("server_error", "api", "API request_failed (status 503)", "chat"), diagnosticFingerprint("render_error", "render", "Render failure", "chat"));
});

test("native offline 503s are transient network errors, not server failures", () => {
  const descriptors = ["window", "CustomEvent"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const);
  const events: { detail: { code: string; severity?: string } }[] = [];
  Object.defineProperty(globalThis, "window", { configurable: true, value: { dispatchEvent: (event: typeof events[number]) => events.push(event) } });
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: class {
    detail: unknown;
    constructor(_type: string, init: { detail: unknown }) { this.detail = init.detail; }
  } });
  try {
    for (const code of ["gateway_unreachable", "session_verification_failed"]) {
      for (const data of [{ error: { code } }, { detail: { error: { code } } }, { detail: { code } }]) {
        assert.equal(apiDiagnostic(503, data), "network_error");
        assert.equal(apiDiagnostic(500, data), "server_error");
        announceDiagnostic(apiDiagnostic(503, data)!, "api", apiDiagnosticMessage(503), null,
          "auth_session_read", null, "native_bridge", null, null, apiDiagnosticDetail("/auth/session", 503, data));
        assert.equal(events.at(-1)?.detail.severity, "transient");
      }
    }
    announceDiagnostic("server_error", "api", apiDiagnosticMessage(503), null,
      null, null, null, null, null, apiDiagnosticDetail("/auth/session", 503));
    assert.equal(events.at(-1)?.detail.severity, undefined);
    for (const status of [0, 403, 503]) {
      announceDiagnostic(apiDiagnostic(status)!, "api", apiDiagnosticMessage(status), null,
        null, null, null, null, null, apiDiagnosticDetail("/api/me/greeting", status));
      assert.equal(events.at(-1)?.detail.severity, "transient");
    }
    announceDiagnostic("request_error", "api", apiDiagnosticMessage(403), null,
      null, null, null, null, null, apiDiagnosticDetail("/api/me/greeting/audio", 403));
    assert.equal(events.at(-1)?.detail.severity, undefined);
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});

test("diagnostic dedupe keeps feature and valid gateway request ref distinct", () => {
  const label = "API request_failed (status 503)";
  const chat = diagnosticFingerprint("server_error", "api", label, "chat", "req_123456abcdef");
  assert.notEqual(chat, diagnosticFingerprint("server_error", "api", label, "planning", "req_123456abcdef"));
  assert.notEqual(chat, diagnosticFingerprint("server_error", "api", label, "chat", "req_abcdef123456"));
  assert.equal(chat, diagnosticFingerprint("server_error", "api", label, "chat", "req_123456abcdef"));
  assert.equal(diagnosticFingerprint("server_error", "api", label, "chat", "invalid-ref"),
    diagnosticFingerprint("server_error", "api", label, "chat"));
});

test("API diagnostics use closed operation names without exposing venture IDs or query content", () => {
  assert.equal(apiDiagnosticOperation("/auth/session"), "auth_session_read");
  assert.equal(apiDiagnosticOperation("/api/ventures"), "venture_list_read");
  assert.equal(apiDiagnosticOperation("/api/ventures/ven_private/waitlist?token=secret"), "venture_waitlist_read");
  assert.equal(apiDiagnosticOperation("/api/ventures/ven_private/waitlist/test", "POST"), "venture_waitlist_test");
  assert.equal(apiDiagnosticOperation("/api/ventures/ven_private/plans/pln_private"), "venture_plan_read");
  assert.equal(apiDiagnosticOperation("/api/apps/app_private/analytics/overview"), "app_analytics_overview_read");
  assert.equal(apiDiagnosticOperation("/api/ventures/ven_private/research/dossiers"), "research_dossiers_read");
  assert.equal(apiDiagnosticOperation("/api/workspaces/wsp_private/llm-settings"), "workspace_llm_settings_read");
  assert.equal(apiDiagnosticOperation("/api/ventures/ven_private/device-work-orders/dwo_exact/approve", "POST"), "mac_order_approve");
  assert.equal(apiDiagnosticOperation("/api/ventures/ven_private/activity/stream"), "venture_activity_stream");
  assert.equal(apiDiagnosticOperation("/events?venture_id=ven_private&topics=job.progress"), "venture_events_stream");
  assert.equal(apiDiagnosticOrderId("/api/ventures/ven_private/device-work-orders/dwo_exact/approve?token=secret"), "dwo_exact");
  assert.equal(apiDiagnosticOrderId("/api/ventures/ven_private/device-work-orders/dwo_exact/unknown"), null);
  assert.equal(apiDiagnosticOrderId("https://private.test/api/ventures/ven_private/device-work-orders/dwo_exact"), null);
  assert.equal(apiDiagnosticOperation("https://private.test/api/ventures/ven_private/waitlist?token=secret"), null);
  assert.equal(apiDiagnosticOperation("/api/ventures/ven_private/waitlist/unknown"), null);
  const label = "API request_failed (status 503)";
  assert.notEqual(diagnosticFingerprint("server_error", "api", label, "website", null, null, "venture_waitlist_read"),
    diagnosticFingerprint("server_error", "api", label, "website", null, null, "workspace_site_read"));
  assert.equal(diagnosticFingerprint("server_error", "api", label, "website", null, null, "token=secret"),
    diagnosticFingerprint("server_error", "api", label, "website"));
});

test("known Mac-generated 503s are distinguishable from an upstream failure", () => {
  for (const code of ["secure_store_pending", "keychain_unavailable", "native_auth_quiescing"]) {
    assert.equal(native503Origin(503, {error: {code}}), code);
  }
  assert.equal(native503Origin(503, { error: { code: "native_auth_busy" } }), "native_auth_busy");
  assert.equal(native503Origin(503, { error: { code: "credential_store_failed" } }), "credential_store_failed");
  assert.equal(native503Origin(503, { error: { code: "private_upstream_code" } }), null);
  assert.equal(native503Origin(500, { error: { code: "native_auth_busy" } }), null);
  assert.notEqual(
    diagnosticFingerprint("server_error", "api", "API request_failed (status 503)", "settings", null, null, "auth_session_read", "native_auth_busy"),
    diagnosticFingerprint("server_error", "api", "API request_failed (status 503)", "settings", null, null, "auth_session_read"),
  );
});

test("transport and exact order separate failures without retaining a private route", () => {
  const label = "API request_failed (status 503)";
  const native = diagnosticFingerprint("server_error", "api", label, "computer_use", null, null, "mac_order_read", null, "native_bridge", "dwo_first");
  assert.notEqual(native, diagnosticFingerprint("server_error", "api", label, "computer_use", null, null, "mac_order_read", null, "native_stream", "dwo_first"));
  assert.notEqual(native, diagnosticFingerprint("server_error", "api", label, "computer_use", null, null, "mac_order_read", null, "native_bridge", "dwo_second"));
  assert.ok(!native.includes("ven_private"));
});

test("distinct failed jobs are not collapsed by the one-minute client filter", () => {
  const one = diagnosticFingerprint("job_error", "job", "Job failure", "planning", null, "job_a");
  assert.equal(one, diagnosticFingerprint("job_error", "job", "Job failure", "planning", null, "job_a"));
  assert.notEqual(one, diagnosticFingerprint("job_error", "job", "Job failure", "planning", null, "job_b"));
  assert.equal(diagnosticFingerprint("job_error", "job", "Job failure", "planning", null, "private text"),
    diagnosticFingerprint("job_error", "job", "Job failure", "planning"));
});

test("API failures retain status but never arbitrary server explanation", () => {
  const message = apiDiagnosticMessage(500, { error: {
    code: "build_worker_failed",
    message: "Worker unavailable for job; see https://private.test/run?token=abc and api_key=secret123",
  } });
  assert.equal(message, "API request_failed (status 500)");
  assert.ok(!message.includes("private.test"));
  assert.ok(!message.includes("secret123"));
});

test("only gateway-generated opaque request references are retained", () => {
  assert.equal(apiDiagnosticReference({ error: { request_id: "req_123456abcdef" } }), "req_123456abcdef");
  // Core's own request IDs use Crockford base-32 rather than gateway hex.
  assert.equal(apiDiagnosticReference(null, "req_7gkmnpqrstvw"), "req_7gkmnpqrstvw");
  assert.equal(apiDiagnosticReference({ error: { request_id: "req_bad" } }, "req_7gkmnpqrstvw"), "req_7gkmnpqrstvw");
  assert.equal(apiDiagnosticReference(null, "req_7gkmnpqrstvi"), null);
  assert.equal(apiDiagnosticReference({ error: { request_id: "req_private-data" } }), null);
  assert.equal(apiDiagnosticReference({ error: { request_id: "xoxb-123456789012-secret" } }), null);
});

test("native failures are captured without command arguments; permission prompts are attention", () => {
  assert.equal(nativeDiagnosticMessage("desktop_start", "permission_required: enable Accessibility"), null);
  // Bridge still starting: callers retry, so it is logged as transient rather than as a defect.
  assert.equal(nativeDiagnosticSeverity("native_desktop_permissions_unavailable"), "transient");
  assert.equal(nativeDiagnosticSeverity("cu_bridge_not_ready"), "transient");
  assert.equal(nativeDiagnosticSeverity("Driver failed"), "error");
  const message = nativeDiagnosticMessage("desktop_start", "Driver failed for https://private.test?key=secret");
  assert.equal(message, "Mac command_failed");
  assert.ok(!message?.includes("private.test"));
  assert.equal(closedNativeDiagnosticCommand("install_update"), "install_update");
  for (const command of ["open_composio_connect_link", "write_hermes_files", "stdio_write", "desktop_events_unsubscribe"]) {
    assert.equal(closedNativeDiagnosticCommand(command), command);
  }
  assert.equal(closedNativeDiagnosticCommand("install_update?token=private"), null);
  assert.notEqual(diagnosticFingerprint("native_error", "native", message!, "settings", null, null, null, null, null, null, "install_update"),
    diagnosticFingerprint("native_error", "native", message!, "settings", null, null, null, null, null, null, "set_autostart"));
});
