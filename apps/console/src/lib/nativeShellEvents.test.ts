import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { nativeShellTelemetry } from "./nativeShellEvents.ts";

// Every id src-tauri/src/error_capture.rs can queue must become a ui.error with its source and intent.
test("every native shell error id maps to a ui.error with source and intent", () => {
  const rust = readFileSync(new URL("../../../../src-tauri/src/error_capture.rs", import.meta.url), "utf8");
  const events = [...rust.matchAll(/\("(shell_[a-z_]+)", "([a-z_]+)", "([a-z_]+)"\)/g)];
  assert.ok(events.length >= 7);
  for (const [, id, source, intent] of events) {
    const [out] = nativeShellTelemetry([{ kind: "error", id }]);
    assert.equal(out?.event, "ui.error", id);
    assert.equal(out.props.error_source, source, id);
    assert.equal(out.props.error_intent, intent, id);
  }
  assert.deepEqual(nativeShellTelemetry([{ kind: "error", id: "/Users/x/secret" }]), []);
});

test("native reasons stay closed and legacy events keep their prior shape", () => {
  for (const [id, reason] of [["shell_prev_crash", "unclean_exit"], ["shell_prev_crash", "panic"],
    ["shell_start_failed", "console_navigation_failed"], ["shell_sidecar_spawn", "sidecar_binary_missing"]]) {
    assert.equal(nativeShellTelemetry([{kind: "error", id, reason}])[0].props.reason, reason);
    assert.ok(!("reason" in nativeShellTelemetry([{kind: "error", id}])[0].props));
    assert.ok(!("reason" in nativeShellTelemetry([{kind: "error", id, reason: "/Users/private"}])[0].props));
  }
  assert.ok(!("reason" in nativeShellTelemetry([{kind: "error", id: "shell_prev_crash", reason: "sidecar_binary_missing"}])[0].props));
});

test("offline startup preserves its retry page and restart clears the session marker", () => {
  const rust = readFileSync(new URL("../../../../src-tauri/src/lib.rs", import.meta.url), "utf8");
  assert.match(rust, /if connection\.connected && !finish_main_navigation/);
  assert.equal([...rust.matchAll(/error_capture::end_session\(&conventional_data_dir\(\)\);\s+app\.restart\(\)/g)].length, 2);
  assert.ok(rust.indexOf("error_capture::begin_session") > rust.indexOf(".setup(|app|"));
  const supervisor = readFileSync(new URL("../../../../src-tauri/src/supervisor.rs", import.meta.url), "utf8");
  assert.match(supervisor, /let budget = timeout;/);
  assert.doesNotMatch(supervisor, /!running && timeout\.is_zero\(\).*Duration::from_secs\(20\)/);
});
