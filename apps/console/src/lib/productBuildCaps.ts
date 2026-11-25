import type { ApiResult } from "./client";

export type ProductBuildCaps = {
  max_iterations: number; max_wall_seconds: number; per_call_seconds: number; max_turns: number | null;
};
export type ProductBuildPhase = "plan" | "implement";
export type ProductBuildProvider = "claude" | "codex";
export type ProductBuildLoop = ProductBuildCaps & {
  operation: "workspace_loop_v1"; provider: ProductBuildProvider; workspace_slug: string;
  profile: "web_app_vercel_v1" | "static_landing_v1"; check_recipe: "web_app_vercel_checks_v1" | "static_landing_checks_v1"; phase: ProductBuildPhase;
  network: "none"; read_scope: "venture_workspace"; write_scope: "venture_workspace";
  plan_order_id: string | null; plan_digest: string | null;
};

const capKeys = ["max_iterations", "max_wall_seconds", "per_call_seconds", "max_turns"] as const;
const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const integer = (value: unknown, max: number): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 1 && value <= max;
const digest = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);

/** Match Core's strict caps contract; never fill omitted fields with wider defaults. */
export function parseProductBuildCaps(value: unknown, provider: unknown, phase: unknown): ProductBuildCaps | null {
  const caps = record(value);
  if (!caps || Object.keys(caps).length !== capKeys.length || !capKeys.every(key => Object.hasOwn(caps, key))
    || (provider !== "claude" && provider !== "codex") || (phase !== "plan" && phase !== "implement")
    || !integer(caps.max_iterations, phase === "plan" ? 2 : 10) || !integer(caps.max_wall_seconds, 5400)
    || !integer(caps.per_call_seconds, Math.min(1200, caps.max_wall_seconds))
    || (provider === "claude" ? !integer(caps.max_turns, 60) : caps.max_turns !== null)) return null;
  return { max_iterations: caps.max_iterations, max_wall_seconds: caps.max_wall_seconds,
    per_call_seconds: caps.per_call_seconds, max_turns: caps.max_turns as number | null };
}

export function parseProductBuildLoop(value: unknown): ProductBuildLoop | null {
  const loop = record(value);
  if (!loop) return null;
  const savedCaps = Object.fromEntries(capKeys.map(key => [key,
    key === "max_turns" && loop.provider === "codex" && !Object.hasOwn(loop, key) ? null : loop[key]]));
  const caps = parseProductBuildCaps(savedCaps, loop.provider, loop.phase);
  const landing = loop.profile === "static_landing_v1";
  if (landing && (loop.check_recipe !== "static_landing_checks_v1" || loop.phase !== "implement"
    || !caps || caps.max_iterations > 2 || loop.plan_order_id != null || loop.plan_digest != null)) return null;
  const keys = ["operation", "provider", "workspace_slug", "profile", "check_recipe", "phase", "network",
    "read_scope", "write_scope", ...capKeys, "plan_order_id", "plan_digest"];
  if (!caps || Object.keys(loop).some(key => !keys.includes(key)) || loop.operation !== "workspace_loop_v1"
    || (!landing && (loop.profile !== "web_app_vercel_v1" || loop.check_recipe !== "web_app_vercel_checks_v1"))
    || loop.network !== "none" || loop.read_scope !== "venture_workspace" || loop.write_scope !== "venture_workspace"
    || typeof loop.workspace_slug !== "string" || loop.workspace_slug.length > 64
    || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(loop.workspace_slug)
    || (!landing && (loop.phase === "plan" ? loop.plan_order_id != null || loop.plan_digest != null
      : typeof loop.plan_order_id !== "string" || !/^dwo_[A-Za-z0-9]{1,124}$/.test(loop.plan_order_id) || !digest(loop.plan_digest)))) return null;
  return { ...caps, operation: loop.operation, provider: loop.provider as ProductBuildProvider,
    workspace_slug: loop.workspace_slug, profile: loop.profile as ProductBuildLoop["profile"], check_recipe: loop.check_recipe as ProductBuildLoop["check_recipe"],
    phase: loop.phase as ProductBuildPhase, network: loop.network, read_scope: loop.read_scope,
    write_scope: loop.write_scope, plan_order_id: typeof loop.plan_order_id === "string" ? loop.plan_order_id : null,
    plan_digest: typeof loop.plan_digest === "string" ? loop.plan_digest : null };
}

export type ProductPlanManifest = {
  task_id: string; title: string; instruction: string; applications: string[]; origins: string[];
  actions: string[]; credit_ceiling_micro: number; duration_seconds: number; coding_loop_verification: ProductBuildLoop;
};

function productManifest(value: unknown): ProductPlanManifest | null {
  const manifest = record(value), loop = parseProductBuildLoop(manifest?.coding_loop_verification);
  if (!manifest || !loop || typeof manifest.task_id !== "string" || !manifest.task_id || manifest.task_id.length > 128
    || typeof manifest.title !== "string" || !manifest.title || manifest.title.length > 160
    || typeof manifest.instruction !== "string" || !manifest.instruction || manifest.instruction.length > 12000
    || JSON.stringify(manifest.applications) !== '["com.companyos.app"]' || JSON.stringify(manifest.origins) !== "[]"
    || JSON.stringify(manifest.actions) !== '["editor.edit","terminal.execute"]'
    || manifest.credit_ceiling_micro !== 0 || manifest.duration_seconds !== loop.max_wall_seconds
    || Object.keys(manifest).some(key => key.endsWith("_verification") && key !== "coding_loop_verification" && manifest[key] != null)) return null;
  return { task_id: manifest.task_id, title: manifest.title, instruction: manifest.instruction,
    applications: [...manifest.applications as string[]], origins: [], actions: [...manifest.actions as string[]],
    credit_ceiling_micro: 0, duration_seconds: loop.max_wall_seconds, coding_loop_verification: loop };
}

/** Project the saved scope only. Core derives the snapshot and actor/device custody. */
export function productPlanManifest(value: unknown, selectedCaps: unknown): ProductPlanManifest | null {
  const manifest = productManifest(value);
  if (!manifest || manifest.coding_loop_verification.phase !== "plan") return null;
  const caps = parseProductBuildCaps(selectedCaps, manifest.coding_loop_verification.provider, "plan");
  if (!caps) return null;
  return { ...manifest, duration_seconds: caps.max_wall_seconds,
    coding_loop_verification: { ...manifest.coding_loop_verification, ...caps } };
}

const custodyKeys = ["venture_id", "workspace_id", "actor_id", "device_id"] as const;
function savedOrder(value: unknown): Record<string, unknown> | null {
  const order = record(value);
  return order && typeof order.id === "string" && /^dwo_[A-Za-z0-9]{1,124}$/.test(order.id)
    && digest(order.review_digest) && custodyKeys.every(key => typeof order[key] === "string" && order[key]) ? order : null;
}
const unstarted = (order: Record<string, unknown>) => !order.claimed_at && !order.execution_id;
const sameCustody = (left: Record<string, unknown>, right: Record<string, unknown>) =>
  custodyKeys.every(key => left[key] === right[key]);
const canonical = (value: unknown) => JSON.stringify(value, (_key, item: unknown) =>
  record(item) ? Object.fromEntries(Object.entries(item as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b))) : item);
const sameValue = (left: unknown, right: unknown) => canonical(left) === canonical(right);
const failed = (error: string): ApiResult => ({ ok: false, status: 409, data: null, error });

/** Match Core's subject_person_id aliases, but require an authenticated subject. */
export function productBuildActorMatches(actor: unknown, subject: unknown): boolean {
  if (typeof subject !== "string" || !subject.trim()) return false;
  const sub = subject.trim();
  return actor === (["founder", "local", "per_local000000"].includes(sub) ? "per_local000000" : sub);
}

/** Hydrate Core receipts without replacing the manifest parsed by the native reader. */
export function mergeProductOrderDetails(reviewed: unknown, details: unknown): Record<string, unknown> | null {
  const native = savedOrder(reviewed), core = savedOrder(details);
  const manifest = productManifest(native?.manifest), fullManifest = productManifest(core?.manifest);
  const snapshot = record(core?.task_snapshot);
  if (!native || !core || !manifest || !fullManifest || !snapshot || !Object.keys(snapshot).length
    || native.id !== core.id || native.review_digest !== core.review_digest || !sameCustody(native, core)
    || native.expires_at !== core.expires_at || !sameValue(manifest, fullManifest)
    || (native.task_snapshot != null && !sameValue(native.task_snapshot, snapshot))
    || !["pending", "approved", "claimed", "waiting_for_device", "cancelled", "stopped"].includes(String(core.state))
    || (native.claimed_at && !core.claimed_at) || (native.state === "stopped" && core.state !== "stopped")
    || (native.state !== "pending" && core.state === "pending")) return null;
  // Copy only metadata. Native manifest and custody stay authoritative.
  const keys = ["state", "claimed_at", "execution_id", "closed_at", "reviewed_at", "task_snapshot", "native_stop",
    "approved_plan", "stop_reconciliation_required", "inference_reconciliation_required", "events"];
  try {
    const metadata = structuredClone(Object.fromEntries(keys.map(key => [key, core[key]])));
    return { ...native, ...metadata, manifest: native.manifest };
  } catch { return null; }
}

/** Preparation only: every await retains the captured page/session ownership. */
export async function prepareProductBuildCaps(input: {
  order: unknown; caps: unknown; phase: ProductBuildPhase; parserSupported: boolean;
  api: (path: string, init: RequestInit) => Promise<ApiResult>; current: () => boolean;
  onCancelled?: (savedOrder: Record<string, unknown>) => void;
}): Promise<ApiResult> {
  if (!input.current()) return failed("session_scope_changed");
  if (!input.parserSupported) return failed("device_product_build_parser_unavailable");
  let captured: unknown;
  try { captured = structuredClone(input.order); } catch { return failed("device_order_scope_invalid"); }
  const order = savedOrder(captured), source = productManifest(order?.manifest), snapshot = record(order?.task_snapshot);
  if (!order || !source || source.coding_loop_verification.phase !== "plan"
    || !snapshot || !Object.keys(snapshot).length || source.task_id !== `product_build_${order.venture_id}`) return failed("device_order_scope_invalid");
  const caps = parseProductBuildCaps(input.caps, source.coding_loop_verification.provider, input.phase);
  if (!caps) return failed("device_coding_loop_scope_invalid");
  const path = `/api/ventures/${encodeURIComponent(String(order.venture_id))}/device-work-orders`;
  const selected = input.phase === "plan" ? productPlanManifest(source, caps) : null;
  const approved = record(order.approved_plan), stopped = record(order.native_stop);
  const lapsed = order.state === "cancelled" || order.state === "expired"
    || ((order.state === "pending" || order.state === "approved") && typeof order.expires_at === "string" && Date.parse(order.expires_at) <= Date.now());
  if (input.phase === "plan" ? !selected || !unstarted(order) || (order.state !== "pending" && !lapsed)
    : order.state !== "stopped" || !order.closed_at || stopped?.processes_stopped !== true
      || stopped.task_verified !== true || stopped.verification_kind !== "workspace_loop_v1"
      || order.stop_reconciliation_required || order.inference_reconciliation_required || !digest(approved?.plan_digest)) {
    return failed("device_order_scope_invalid");
  }
  const confirmsCancellation = (value: unknown) => {
    const closed = savedOrder(value);
    return !!closed && closed.id === order.id && closed.review_digest === order.review_digest && sameCustody(closed, order)
      && closed.state === "cancelled" && unstarted(closed) && sameValue(closed.task_snapshot, snapshot)
      && sameValue(productManifest(closed.manifest), source);
  };
  try {
    if (selected && !lapsed) {
      // An older strict server rejects this guard; never fall back to cancelling live work.
      let cancelled: ApiResult;
      try {
        cancelled = await input.api(`${path}/${encodeURIComponent(String(order.id))}/cancel`, {
          method: "POST", body: JSON.stringify({ review_digest: order.review_digest, unstarted_only: true }),
        });
      } catch { cancelled = { ok: false, status: 0, data: null, error: "device_order_cancel_unconfirmed" }; }
      if (!input.current()) return failed("session_scope_changed");
      if (!cancelled.ok && cancelled.status >= 400 && cancelled.status < 500) return cancelled;
      let confirmed = cancelled.data;
      if (!cancelled.ok || !confirmsCancellation(cancelled.data)) {
        const fresh = await input.api(`${path}/${encodeURIComponent(String(order.id))}`, { method: "GET" });
        if (!input.current()) return failed("session_scope_changed");
        if (!fresh.ok || !confirmsCancellation(fresh.data)) return failed("device_order_cancel_unconfirmed");
        confirmed = fresh.data;
      }
      if (!input.current()) return failed("session_scope_changed");
      input.onCancelled?.(confirmed as Record<string, unknown>);
    }
    if (!input.current()) return failed("session_scope_changed");
    const result = await input.api(selected ? path : `${path}/${encodeURIComponent(String(order.id))}/prepare-run`, {
      method: "POST", body: JSON.stringify(selected ?? { caps }),
    });
    if (!input.current()) return failed("session_scope_changed");
    if (!result.ok) return result;
    const next = savedOrder(result.data), manifest = productManifest(next?.manifest), loop = manifest?.coding_loop_verification;
    if (!next || !manifest || !loop || next.id === order.id || next.review_digest === order.review_digest
      || !sameCustody(next, order) || next.state !== "pending" || !unstarted(next)
      || manifest.task_id !== source.task_id || loop.phase !== input.phase
      || loop.provider !== source.coding_loop_verification.provider || loop.workspace_slug !== source.coding_loop_verification.workspace_slug
      || !capKeys.every(key => loop[key] === caps[key])
      || !sameValue(next.task_snapshot, selected ? snapshot : { ...snapshot, approved_plan: approved, plan_order_id: order.id })
      || (selected ? !sameValue(manifest, selected) : loop.plan_order_id !== order.id || loop.plan_digest !== approved?.plan_digest)) {
      return failed("device_order_response_invalid");
    }
    return result;
  } catch {
    return failed(input.current() ? "device_order_preparation_unconfirmed" : "session_scope_changed");
  }
}
