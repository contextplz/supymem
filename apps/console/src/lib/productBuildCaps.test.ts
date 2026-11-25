import assert from "node:assert/strict";
import test from "node:test";
import { mergeProductOrderDetails, parseProductBuildCaps, parseProductBuildLoop, productBuildActorMatches, productPlanManifest, prepareProductBuildCaps } from "./productBuildCaps.ts";

const caps = { max_iterations: 1, max_wall_seconds: 300, per_call_seconds: 300, max_turns: 20 };
function loop() {
  return { operation: "workspace_loop_v1", provider: "claude", workspace_slug: "synthetic-caps-fixture",
    profile: "web_app_vercel_v1", check_recipe: "web_app_vercel_checks_v1", phase: "plan", network: "none",
    read_scope: "venture_workspace", write_scope: "venture_workspace", plan_order_id: null, plan_digest: null, ...caps };
}
function manifest() {
  return { task_id: "product_build_ven_synthetic", title: "Plan your product with Claude Code",
    instruction: "Write a product specification and at most eight tasks from the confirmed idea. Change only review/spec.md and review/tasks.json.",
    applications: ["com.companyos.app"], origins: [], actions: ["editor.edit", "terminal.execute"],
    credit_ceiling_micro: 0, duration_seconds: 300, coding_loop_verification: loop() };
}
function order() {
  return { id: "dwo_SyntheticOld", venture_id: "ven_synthetic", workspace_id: "ws_synthetic",
    actor_id: "person_synthetic", device_id: "dev_synthetic", review_digest: "a".repeat(64), state: "pending",
    expires_at: "2999-01-01T00:00:00Z", manifest: manifest(),
    task_snapshot: { kind: "product_build", revision_digest: "f".repeat(64), confirmed_idea: { synthetic: true } } };
}
const ok = (data: unknown) => ({ ok: true, status: 200, data });
function preparedPlan() {
  return { ...order(), id: "dwo_SyntheticNew", review_digest: "b".repeat(64), manifest: productPlanManifest(manifest(), caps) };
}

// These are the fields serialized by the 393 native WorkOrder reader.
function nativeProjection(full = order()) {
  const { id, workspace_id, venture_id, actor_id, device_id, state, review_digest, expires_at, manifest } = full;
  return { id, workspace_id, venture_id, actor_id, device_id, state, review_digest, expires_at,
    claimed_at: null, manifest };
}

test("actor custody matches exactly Core's authenticated local aliases", () => {
  for (const subject of ["founder", "local", "per_local000000", " founder ", " local "])
    assert.equal(productBuildActorMatches("per_local000000", subject), true, subject);
  assert.equal(productBuildActorMatches("usr_owned", "usr_owned"), true);
  assert.equal(productBuildActorMatches("usr_owned", " usr_owned "), true);
  for (const subject of [null, undefined, "", " ", false, {}, "usr_other", "Founder", "local-other"])
    assert.equal(productBuildActorMatches("per_local000000", subject), false);
  assert.equal(productBuildActorMatches("usr_other", "usr_owned"), false);
  assert.equal(productBuildActorMatches(undefined, "founder"), false);
});

test("full Core details hydrate the actual native projection without replacing native authority", () => {
  const full = order(), native = nativeProjection(full);
  const merged = mergeProductOrderDetails(native, { ...full, unreviewed_metadata: true })!;
  assert.equal(merged.manifest, native.manifest);
  assert.deepEqual(merged.task_snapshot, full.task_snapshot);
  assert.notEqual(merged.task_snapshot, full.task_snapshot);
  assert.equal(merged.unreviewed_metadata, undefined);
  assert.equal(mergeProductOrderDetails(merged, { ...full,
    task_snapshot: { ...full.task_snapshot, revision_digest: "e".repeat(64) } }), null,
  "the same review cannot acquire a different snapshot on a later refresh");
  for (const field of ["id", "venture_id", "workspace_id", "actor_id", "device_id", "review_digest", "expires_at"]) {
    assert.equal(mergeProductOrderDetails(native, { ...full, [field]: "foreign" }), null, field);
  }
  for (const change of [{ task_snapshot: null }, { task_snapshot: {} }, { state: "future" },
    { manifest: { ...manifest(), instruction: "different reviewed instructions" } },
    { manifest: { ...manifest(), actions: ["browser.write"] } },
    { manifest: { ...manifest(), coding_loop_verification: { ...loop(), max_turns: 21 } } }]) {
    assert.equal(mergeProductOrderDetails(native, { ...full, ...change }), null);
  }
  assert.equal(mergeProductOrderDetails({ ...native, state: "stopped", claimed_at: "earlier" }, full), null);
  assert.equal(mergeProductOrderDetails({ ...native, state: "claimed", claimed_at: "earlier" }, { ...full, state: "claimed" }), null);
});

test("core activity survives each guarded metadata refresh across domains", () => {
  for (const domain of ["Warehouse purchasing", "Family meal planning"]) {
    const full = { ...order(), events: [{ at: 1, text: `${domain}: Building your page` }] };
    const initial = mergeProductOrderDetails(nativeProjection(full), full)!;
    assert.deepEqual(initial.events, full.events);
    assert.notEqual(initial.events, full.events);
    const fresh = { ...full, state: "claimed", claimed_at: "now", events: [...full.events,
      { at: 2, text: `${domain}: Checking your saved page files` }] };
    const refreshed = mergeProductOrderDetails(initial, fresh)!;
    assert.deepEqual(refreshed.events, fresh.events);
    assert.equal(refreshed.manifest, initial.manifest);
    assert.equal(mergeProductOrderDetails(initial, { ...fresh, device_id: "another-device" }), null);
  }
});

test("hydrated native pending and closed-plan reviews prepare through the existing signed API", async () => {
  const full = order(), native = nativeProjection(full);
  assert.equal(native.hasOwnProperty("task_snapshot"), false);
  const source = mergeProductOrderDetails(native, full)!;
  const planCalls: string[] = [];
  const plan = await prepareProductBuildCaps({ order: source, caps, phase: "plan", parserSupported: true,
    current: () => true, api: async path => {
      planCalls.push(path); return ok(planCalls.length === 1 ? { ...full, state: "cancelled" } : preparedPlan());
    } });
  assert.equal(plan.ok, true);
  assert.equal(planCalls.length, 2);
  const selected = { max_iterations: 6, max_wall_seconds: 1200, per_call_seconds: 300, max_turns: 30 };
  const closed = { ...full, state: "stopped", claimed_at: "earlier", closed_at: "now", execution_id: "dex_finished",
    native_stop: { processes_stopped: true, task_verified: true, verification_kind: "workspace_loop_v1" },
    approved_plan: { plan_digest: "c".repeat(64) } };
  const hydrated = mergeProductOrderDetails({ ...native, state: "claimed", claimed_at: "earlier" }, closed)!;
  const next = { ...full, id: "dwo_SyntheticRun", review_digest: "d".repeat(64), manifest: { ...manifest(), duration_seconds: 1200,
    coding_loop_verification: { ...loop(), ...selected, phase: "implement", plan_order_id: full.id, plan_digest: "c".repeat(64) } },
    task_snapshot: { ...full.task_snapshot, approved_plan: closed.approved_plan, plan_order_id: full.id } };
  const runCalls: string[] = [];
  const run = await prepareProductBuildCaps({ order: hydrated, caps: selected, phase: "implement", parserSupported: true,
    current: () => true, api: async (path, init) => {
      runCalls.push(path); assert.deepEqual(JSON.parse(String(init.body)), { caps: selected }); return ok(next);
    } });
  assert.equal(run.ok, true);
  assert.deepEqual(runCalls, [`/api/ventures/ven_synthetic/device-work-orders/${full.id}/prepare-run`]);
  assert.equal([...planCalls, ...runCalls].some(path => /approve|claim|start/.test(path)), false);
});

test("pending cap replacement confirms cancellation before preparing a fresh review", async () => {
  const calls: Array<{ path: string; body: unknown }> = [];
  const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true, current: () => true,
    api: async (path, init) => {
      calls.push({ path, body: JSON.parse(String(init.body)) });
      return ok(calls.length === 1 ? { ...order(), state: "cancelled" } : preparedPlan());
    } });
  assert.deepEqual(calls, [
    { path: "/api/ventures/ven_synthetic/device-work-orders/dwo_SyntheticOld/cancel", body: { review_digest: "a".repeat(64), unstarted_only: true } },
    { path: "/api/ventures/ven_synthetic/device-work-orders", body: productPlanManifest(manifest(), caps) },
  ]);
  assert.deepEqual(result, ok(preparedPlan()));
  assert.equal(calls.some(call => /approve|claim|start/.test(call.path)), false);
});

test("failed, uncertain or foreign cancellation never prepares a successor", async () => {
  const failure = { ok: false, status: 409, data: null, error: "device_order_review_changed" };
  for (const cancellation of [failure, ok(null), ok({ ...order(), state: "pending" }),
    ok({ ...order(), state: "cancelled", claimed_at: "now" }), ok({ ...order(), state: "cancelled", execution_id: "dex_live" }),
    ok({ ...order(), state: "cancelled", actor_id: "foreign" }), ok({ ...order(), state: "cancelled", device_id: "foreign" }),
    ok({ ...order(), state: "cancelled", id: "dwo_Other" }), ok({ ...order(), state: "cancelled", review_digest: "b".repeat(64) })]) {
    let calls = 0, cancelledCallbacks = 0;
    const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true,
      current: () => true, onCancelled: () => { cancelledCallbacks++; }, api: async () => { calls++; return cancellation; } });
    assert.equal(result.ok, false);
    assert.equal(calls, cancellation.ok ? 2 : 1);
    assert.equal(cancelledCallbacks, 0);
    if (!cancellation.ok) assert.deepEqual(result, failure);
  }
  let calls = 0;
  const uncertain = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true,
    current: () => true, api: async () => { calls++; throw new Error("timeout"); } });
  assert.equal(uncertain.error, "device_order_preparation_unconfirmed");
  assert.equal(calls, 2);
});

test("retired page/session ownership and unsupported native parser stop every request boundary", async () => {
  for (const retireAfter of [0, 1, 2]) {
    let calls = 0;
    const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true,
      current: () => calls < retireAfter, api: async () => { calls++; return ok(calls === 1 ? { ...order(), state: "cancelled" } : preparedPlan()); } });
    assert.equal(result.error, "session_scope_changed");
    assert.equal(calls, retireAfter);
  }
  const unsupported = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: false,
    current: () => true, api: async () => { assert.fail("unsupported parser must not cancel"); } });
  assert.equal(unsupported.error, "device_product_build_parser_unavailable");
});

test("stopped verified plans prepare implementation with only explicit caps", async () => {
  const selected = { max_iterations: 6, max_wall_seconds: 1200, per_call_seconds: 300, max_turns: 30 };
  const source = { ...order(), state: "stopped", closed_at: "now", claimed_at: "earlier", execution_id: "dex_finished",
    native_stop: { processes_stopped: true, task_verified: true, verification_kind: "workspace_loop_v1" },
    approved_plan: { plan_digest: "c".repeat(64) } };
  const next = { ...order(), id: "dwo_SyntheticRun", review_digest: "d".repeat(64), manifest: { ...manifest(), duration_seconds: 1200,
    coding_loop_verification: { ...loop(), ...selected, phase: "implement", plan_order_id: source.id, plan_digest: "c".repeat(64) } },
    task_snapshot: { ...source.task_snapshot, approved_plan: source.approved_plan, plan_order_id: source.id } };
  const calls: Array<{ path: string; body: unknown }> = [];
  const result = await prepareProductBuildCaps({ order: source, caps: selected, phase: "implement", parserSupported: true,
    current: () => true, api: async (path, init) => { calls.push({ path, body: JSON.parse(String(init.body)) }); return ok(next); } });
  assert.deepEqual(calls, [{ path: "/api/ventures/ven_synthetic/device-work-orders/dwo_SyntheticOld/prepare-run", body: { caps: selected } }]);
  assert.deepEqual(result, ok(next));
  for (const change of [{ state: "claimed" }, { closed_at: null }, { native_stop: { ...source.native_stop, task_verified: false } },
    { stop_reconciliation_required: true }, { inference_reconciliation_required: true }, { approved_plan: null }]) {
    const invalid = await prepareProductBuildCaps({ order: { ...source, ...change }, caps: selected, phase: "implement", parserSupported: true,
      current: () => true, api: async () => { assert.fail("an unconfirmed plan must not prepare implementation"); } });
    assert.equal(invalid.ok, false);
  }
});

test("preparation failure or a changed returned scope never confirms new caps", async () => {
  const failure = { ok: false, status: 409, data: null, error: "device_order_active" };
  for (const response of [failure, ok({ ...preparedPlan(), actor_id: "foreign" }), ok({ ...preparedPlan(), state: "approved" }),
    ok({ ...preparedPlan(), claimed_at: "now" }), ok({ ...preparedPlan(), id: order().id }),
    ok({ ...preparedPlan(), task_snapshot: { ...order().task_snapshot, revision_digest: "e".repeat(64) } }),
    ok({ ...preparedPlan(), manifest: { ...preparedPlan().manifest!, duration_seconds: 301 } }),
    ok({ ...preparedPlan(), manifest: { ...preparedPlan().manifest!, coding_loop_verification: { ...loop(), max_turns: 21 } } })]) {
    let calls = 0;
    const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true,
      current: () => true, api: async () => { calls++; return calls === 1 ? ok({ ...order(), state: "cancelled" }) : response; } });
    assert.equal(result.ok, false);
    assert.equal(calls, 2);
    if (!response.ok) assert.deepEqual(result, failure);
  }
});

test("caps require explicit bounded integers and provider-specific turns", () => {
  assert.deepEqual(parseProductBuildCaps(caps, "claude", "plan"), caps);
  assert.deepEqual(parseProductBuildCaps({ ...caps, max_turns: null }, "codex", "plan"), { ...caps, max_turns: null });
  assert.ok(parseProductBuildCaps({ max_iterations: 10, max_wall_seconds: 5400, per_call_seconds: 1200, max_turns: 60 }, "claude", "implement"));
  for (const [key, values] of Object.entries({ max_iterations: [0, 11, 1.5, "1", true, NaN, Infinity],
    max_wall_seconds: [0, 5401, "300", 0.5], per_call_seconds: [0, 1201, 301, "300"], max_turns: [null, 0, 61, "20", 1.5] })) {
    for (const value of values) assert.equal(parseProductBuildCaps({ ...caps, [key]: value }, "claude", "implement"), null, `${key}: ${value}`);
  }
  for (const bad of [undefined, [], {}, { ...caps, extra: true }, { ...caps, max_turns: undefined }]) {
    assert.equal(parseProductBuildCaps(bad, "claude", "plan"), null);
  }
  assert.equal(parseProductBuildCaps({ ...caps, max_iterations: 3 }, "claude", "plan"), null);
  assert.equal(parseProductBuildCaps(caps, "codex", "plan"), null);
  assert.equal(parseProductBuildCaps(caps, { toString: () => "claude" }, "plan"), null);
  assert.equal(parseProductBuildCaps(caps, "claude", "future"), null);
});

test("saved loop parser accepts only the supported reviewed profile and complete plan bindings", () => {
  assert.deepEqual(parseProductBuildLoop(loop()), loop());
  const { max_turns: _turns, plan_order_id: _order, plan_digest: _digest, ...codex } = { ...loop(), provider: "codex" };
  assert.deepEqual(parseProductBuildLoop(codex), { ...loop(), provider: "codex", max_turns: null });
  const implementation = { ...loop(), phase: "implement", plan_order_id: "dwo_SyntheticPlan", plan_digest: "a".repeat(64) };
  assert.deepEqual(parseProductBuildLoop(implementation), implementation);
  for (const change of [{ operation: "future" }, { profile: "future" }, { check_recipe: "future" }, { network: "internet" },
    { read_scope: "all_files" }, { write_scope: "all_files" }, { workspace_slug: "../escape" }, { provider: "other" },
    { phase: "future" }, { extra: true }, { plan_order_id: "dwo_Unexpected" }, { plan_digest: "a".repeat(64) }]) {
    assert.equal(parseProductBuildLoop({ ...loop(), ...change }), null);
  }
  for (const change of [{ plan_order_id: null }, { plan_order_id: "../escape" }, { plan_digest: null }, { plan_digest: "A".repeat(64) }]) {
    assert.equal(parseProductBuildLoop({ ...implementation, ...change }), null);
  }
});

test("planning payload preserves instruction and projects saved capabilities without client custody or snapshot", () => {
  const selected = { max_iterations: 2, max_wall_seconds: 360, per_call_seconds: 180, max_turns: 15 };
  const saved = { ...manifest(), actor_id: "must-not-send", device_id: "must-not-send", task_snapshot: { confirmed_idea: "must-not-send" } };
  const payload = productPlanManifest(saved, selected);
  assert.deepEqual(payload, { ...manifest(),
    duration_seconds: 360, coding_loop_verification: { ...loop(), ...selected } });
  assert.equal(saved.instruction.includes("eight"), true);
  assert.equal(saved.duration_seconds, 300);
  const custom = productPlanManifest({ ...manifest(), instruction: "Keep the founder's reviewed static demo scope." }, caps);
  assert.equal(custom?.instruction, "Keep the founder's reviewed static demo scope.");
  for (const change of [{ applications: ["other.app"] }, { origins: ["https://example.com"] }, { actions: ["terminal.execute"] },
    { credit_ceiling_micro: 1 }, { duration_seconds: 299 }, { deploy_verification: {} }, { instruction: "" },
    { coding_loop_verification: { ...loop(), phase: "implement", plan_order_id: "dwo_Plan", plan_digest: "a".repeat(64) } }]) {
    assert.equal(productPlanManifest({ ...manifest(), ...change }, caps), null);
  }
  assert.equal(productPlanManifest(manifest(), { ...caps, max_iterations: 3 }), null);
});

test("unknown cancellation requires a fresh matching closed read before preparation", async () => {
  for (const unknown of ["throw", "malformed", "server_error"]) {
    const calls: string[] = [];
    const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true, current: () => true,
      api: async (path, init) => {
        calls.push(`${init.method} ${path}`);
        if (calls.length === 1) {
          if (unknown === "throw") throw new Error("response lost after cancellation");
          return unknown === "malformed" ? ok(null) : { ok: false, status: 503, data: null };
        }
        return ok(calls.length === 2 ? { ...order(), state: "cancelled" } : preparedPlan());
      } });
    assert.equal(result.ok, true);
    assert.deepEqual(calls, ["POST /api/ventures/ven_synthetic/device-work-orders/dwo_SyntheticOld/cancel",
      "GET /api/ventures/ven_synthetic/device-work-orders/dwo_SyntheticOld", "POST /api/ventures/ven_synthetic/device-work-orders"]);
  }
  for (const fresh of [ok({ ...order(), state: "pending" }), ok({ ...order(), state: "cancelled", execution_id: "dex_live" }),
    ok({ ...order(), state: "cancelled", task_snapshot: { ...order().task_snapshot, confirmed_idea: { synthetic: false } } })]) {
    let calls = 0;
    const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true, current: () => true,
      api: async () => { calls++; return calls === 1 ? ok(null) : fresh; } });
    assert.equal(result.error, "device_order_cancel_unconfirmed");
    assert.equal(calls, 2);
  }
});

test("cancelled or expired unclaimed recovery preserves scope and needs a fresh pending approval", async () => {
  for (const change of [{ state: "cancelled" }, { state: "expired" }, { state: "pending", expires_at: "2000-01-01T00:00:00Z" },
    { state: "approved", expires_at: "2000-01-01T00:00:00Z" }]) {
    const calls: string[] = [];
    const result = await prepareProductBuildCaps({ order: { ...order(), ...change }, caps, phase: "plan", parserSupported: true, current: () => true,
      api: async (path, init) => { calls.push(path); assert.deepEqual(JSON.parse(String(init.body)), productPlanManifest(manifest(), caps)); return ok(preparedPlan()); } });
    assert.equal(result.ok, true);
    assert.deepEqual(calls, ["/api/ventures/ven_synthetic/device-work-orders"]);
  }
  for (const change of [{ state: "cancelled", claimed_at: "now" }, { state: "expired", execution_id: "dex_live" },
    { state: "approved", expires_at: "2999-01-01T00:00:00Z" }]) {
    const result = await prepareProductBuildCaps({ order: { ...order(), ...change }, caps, phase: "plan", parserSupported: true, current: () => true,
      api: async () => { assert.fail("only lapsed unclaimed reviews can recover"); } });
    assert.equal(result.ok, false);
  }
  const selected = { ...caps, max_wall_seconds: 360, max_turns: 21 };
  const replacement = { ...preparedPlan(), manifest: productPlanManifest(manifest(), selected) };
  let calls = 0, cancelledCallbacks = 0;
  const changedCaps = await prepareProductBuildCaps({ order: { ...order(), state: "cancelled" }, caps: selected,
    phase: "plan", parserSupported: true, current: () => true, onCancelled: () => { cancelledCallbacks++; },
    api: async (path, init) => {
      calls++;
      assert.equal(path, "/api/ventures/ven_synthetic/device-work-orders");
      assert.deepEqual(JSON.parse(String(init.body)), productPlanManifest(manifest(), selected));
      return ok(replacement);
    } });
  assert.deepEqual(changedCaps, ok(replacement));
  assert.equal(calls, 1);
  assert.equal(cancelledCallbacks, 0);
});

test("captured snapshot and custody cannot change across cancellation await", async () => {
  const original = order();
  let calls = 0;
  const result = await prepareProductBuildCaps({ order: original, caps, phase: "plan", parserSupported: true, current: () => true,
    api: async () => {
      calls++;
      original.task_snapshot.confirmed_idea.synthetic = false;
      original.actor_id = "changed-after-dispatch";
      return ok(calls === 1 ? { ...order(), state: "cancelled" } : preparedPlan());
    } });
  assert.equal(result.ok, true);
  assert.equal(calls, 2);
});

test("actor returning after an epoch change cannot continue uncertain-cancel reconciliation", async () => {
  const capturedActor = "actor_a", capturedEpoch = 1;
  let actor = capturedActor, epoch = capturedEpoch, calls = 0, cancelledCallbacks = 0;
  const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true,
    current: () => actor === capturedActor && epoch === capturedEpoch,
    onCancelled: () => { cancelledCallbacks++; },
    api: async () => {
      calls++;
      if (calls === 1) return ok(null);
      actor = "actor_b"; epoch++;
      actor = capturedActor; epoch++;
      return ok({ ...order(), state: "cancelled" });
    } });
  assert.equal(result.error, "session_scope_changed");
  assert.equal(calls, 2);
  assert.equal(cancelledCallbacks, 0);
});

test("confirmed cancellation callback runs before preparation and survives successor failure", async () => {
  for (const uncertain of [false, true]) {
    const steps: string[] = [];
    const cancelled = { ...order(), state: "cancelled" };
    const failure = { ok: false, status: 409, data: null, error: "device_order_active" };
    const result = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true, current: () => true,
      onCancelled: saved => { assert.deepEqual(saved, cancelled); steps.push("confirmed cancellation"); },
      api: async (path, init) => {
        if (path.endsWith("/cancel")) { steps.push("cancel"); return uncertain ? ok(null) : ok(cancelled); }
        if (init.method === "GET") { steps.push("fresh read"); return ok(cancelled); }
        steps.push("prepare"); return failure;
      } });
    assert.deepEqual(result, failure);
    assert.deepEqual(steps, uncertain ? ["cancel", "fresh read", "confirmed cancellation", "prepare"]
      : ["cancel", "confirmed cancellation", "prepare"]);
  }
  let current = true, calls = 0;
  const retiredByCallback = await prepareProductBuildCaps({ order: order(), caps, phase: "plan", parserSupported: true,
    current: () => current, onCancelled: () => { current = false; },
    api: async () => { calls++; return ok({ ...order(), state: "cancelled" }); } });
  assert.equal(retiredByCallback.error, "session_scope_changed");
  assert.equal(calls, 1);
});
