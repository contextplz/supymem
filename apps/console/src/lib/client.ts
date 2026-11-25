import { desktopInvoke, desktopInvokeOrThrow, isDesktopShell as isNativeDesktopShell } from "./desktop.ts";
import { nativeFetch } from "./nativeTransport.ts";
import { isTransientNativeIdentityFence } from "./apiFailure.ts";
import { waitForChatRequest } from "./chatStop.ts";
import { claimControlOutcome, settleControlOutcome } from "./controlOutcome.ts";
import { announceDiagnostic, apiDiagnostic, apiDiagnosticDetail, apiDiagnosticMessage, apiDiagnosticOperation, apiDiagnosticOrderId, apiDiagnosticReference, native503Origin } from "./uiDiagnostics.ts";
import { announceAttention } from "./uiAttention.ts";
import { cacheClear, cacheGet, cacheInvalidate, cachePut, isVentureList, mutationInvalidates, ventureOf } from "./apiCache.ts";
import { forgetInvite, recallInvite, rememberInvite } from "./inviteCode.ts";
import { isLocalConsole } from "./localMode.ts";

function isDesktopShell(): boolean {
  return !isLocalConsole() && isNativeDesktopShell();
}

export function resolveGateway(path = ""): string {
  if (typeof window !== "undefined") {
    if (isLocalConsole()) {
      const localPath = path.replace(/^\/api\/api\/v1(?=\/|\?|$)/, "/api/v1");
      return `${window.location.origin}${localPath && !localPath.startsWith("/api/") ? "/api" : ""}${localPath}`;
    }
    try {
      const injected = sessionStorage.getItem("cos_gateway_url");
      if (injected) return `${injected.replace(/\/$/, "")}${path}`;
    } catch { /* Optional override; storage denial must not block authentication. */ }
  }
  return `${(process.env.NEXT_PUBLIC_GATEWAY_URL || "http://127.0.0.1:3200").replace(/\/$/, "")}${path}`;
}

export type SessionInfo = {
  authenticated: boolean;
  csrf?: string | null;
  subject?: string;
  email?: string | null;
  mode?: string;
};

export type ApiResult = {
  ok: boolean;
  status: number;
  data: unknown;
  error?: string;
};

let csrfToken = "";
let sessionSubject: string | null = null;
let sessionInvalidationEpoch = 0;
let sessionReceiptEpoch = 0;
// A successful session check reused for a few seconds so route changes skip the native round trip.
let sessionMemo: { at: number; result: ApiResult } | null = null;
let localSessionFlight: { epoch: number; result: Promise<ApiResult> } | null = null;
const unauthorizedRequests = new Map<string, ApiResult>();
const SESSION_MEMO_MS = 10_000;

/** A process-local hint for preserving the shell across client-side routes.
 * It is never persisted and every new shell still verifies the native session.
 */
export function verifiedSessionSubject(): string | null {
  return sessionSubject;
}

/** Receipt generation changes on every actor transition, including A to B to A. */
export function verifiedSessionEpoch(): number { return sessionReceiptEpoch; }

export function clearVerifiedSession(): void {
  unauthorizedRequests.clear();
  if (sessionSubject === null) sessionInvalidationEpoch++;
  noteSubject(null);
}

function noteSubject(subject: string | null) {
  if (sessionSubject === subject) return;
  sessionReceiptEpoch++;
  if (subject === null) sessionInvalidationEpoch++;
  sessionSubject = subject;
  csrfToken = "";
  sessionMemo = null;
  cacheClear(); // a different (or no) account must never see the previous one's data
  unauthorizedRequests.clear();
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("supymem-session-changed", { detail: { subject } }));
  }
}

export function isInvalidSession(result: ApiResult): boolean {
  const data = result.data as { authenticated?: boolean; error?: { code?: string }; detail?: { code?: string } } | null;
  return (result.status === 200 && data?.authenticated === false)
    || (result.status === 401 && (data?.error?.code || data?.detail?.code) === "session_invalid");
}

function noteReconnecting(status: number): void {
  if ((status === 0 || status === 401 || status === 403 || status === 408 || status === 429 || status >= 500) && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("supymem-session-reconnecting"));
  }
}

async function noteNativeDisconnectAfter401(): Promise<void> {
  // Feature endpoints may reject their own credentials while the Mac device
  // session is still valid. Native auth is the source of truth for this actor.
  const status = await desktopInvoke<{ state?: string }>("desktop_auth_status");
  if (status && ["signed_out", "reauth_required", "account_mismatch", "revocation_pending"].includes(status.state || "")) {
    noteSubject(null);
  }
}

async function nativeRequest(path: string, init: RequestInit = {}, liveRead = false): Promise<ApiResult> {
  const control = claimControlOutcome(init.method || "GET", path);
  let response: ApiResult | null = null;
  try {
    if (init.signal?.aborted) throw new DOMException("Request stopped", "AbortError");
    if (init.body && typeof init.body !== "string") {
      return { ok: false, status: 415, data: null, error: "This upload needs the desktop file transfer service." };
    }
    // Released Mac apps (<= 0.1.99) reject any header outside their allowlist with
    // native_request_denied, so the native bridge never receives X-Request-ID.
    const headers: Record<string, string> = {};
    const supplied = new Headers(init.headers);
    for (const key of ["Idempotency-Key", "If-Match", "X-Workspace-ID"]) {
      const value = supplied.get(key);
      if (value) headers[key] = value;
    }
    const request = desktopInvokeOrThrow<{ status: number; data: unknown; requestId?: string }>("desktop_api_request", {
      method: (init.method || "GET").toUpperCase(), path,
      ...(init.body ? { body: JSON.parse(init.body as string) } : {}), headers,
    }, { quiet: liveRead || path === "/auth/session" || (path === "/api/me/greeting" && (!init.method || init.method.toUpperCase() === "GET")) });
    const reply = await waitForChatRequest(["GET", "HEAD"].includes((init.method || "GET").toUpperCase())
      ? waitForChatRequest(request, AbortSignal.timeout(15_000)) : request, init.signal);
    response = { ok: reply.status >= 200 && reply.status < 300, status: reply.status, data: reply.data,
      ...(reply.status === 429 ? { error: apiDiagnosticMessage(reply.status, reply.data) } : {}) };
    settleControlOutcome(control, reply.status, reply.data);
    if (reply.status === 401) await noteNativeDisconnectAfter401();
    if (path !== "/auth/session" && path !== "/api/telemetry" && sessionSubject) noteReconnecting(reply.status);
    const operation = apiDiagnosticOperation(path, init.method);
    const identityFence = isTransientNativeIdentityFence({ ok: false, status: reply.status, data: reply.data });
    const code = path === "/api/telemetry" || identityFence ? null : apiDiagnostic(reply.status, reply.data, operation);
    if (code) announceDiagnostic(code, "api", apiDiagnosticMessage(reply.status, reply.data), apiDiagnosticReference(reply.data, reply.requestId), operation,
      native503Origin(reply.status, reply.data), "native_bridge", apiDiagnosticOrderId(path), null, apiDiagnosticDetail(path, reply.status, reply.data), (liveRead || path === "/auth/session") && transientReadFailure(reply.status) ? "transient" : "error");
    else if (!reply.status.toString().startsWith("2")) announceAttention(reply.data);
    return response;
  } catch (err) {
    if (response) return response;
    // desktopInvokeOrThrow already records the native failure. Avoid a second
    // network event for the same rejected IPC call.
    if (path !== "/api/telemetry" && err instanceof SyntaxError) announceDiagnostic("runtime_error", "runtime", err);
    if (liveRead) announceDiagnostic("network_error", "api", apiDiagnosticMessage(0), null, apiDiagnosticOperation(path), null, "native_bridge", null, null, apiDiagnosticDetail(path, 0), "transient");
    settleControlOutcome(control, 0, null);
    if (!isAbort(err, init) && path !== "/auth/session" && path !== "/api/telemetry" && sessionSubject) noteReconnecting(0);
    return { ok: false, status: 0, data: null, error: "The Mac app could not connect to Supymem." };
  }
}

function readCsrfCookie(): string {
  if (typeof document === "undefined") return "";
  const match = document.cookie.match(/(?:^|; )cos_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export function getCsrf() {
  if (!csrfToken) csrfToken = readCsrfCookie();
  return csrfToken;
}

function pageOrigin(): string {
  if (typeof window !== "undefined" && window.location?.origin) {
    return window.location.origin;
  }
  return "http://127.0.0.1:3100";
}

export function newRequestId(): string {
  return `req_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
}

/** A cancelled request (unmount, navigation, AbortController) is logged as transient, not a user-facing failure. */
function isAbort(err: unknown, init?: RequestInit): boolean {
  return init?.signal?.aborted === true || (err instanceof DOMException && err.name === "AbortError");
}

async function authPost(path: string, body: Record<string, unknown>): Promise<ApiResult> {
  if (typeof body.invite_code === "string" && body.invite_code) rememberInvite(body.invite_code);
  if (body.invite_code === "") clearOnboardingInvite();
  try {
    const r = await fetch(resolveGateway(path), {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Origin: pageOrigin(), "X-Request-ID": newRequestId() },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => null);
    if (r.ok) {
      const session = data as SessionInfo | null;
      if (session?.authenticated && session.subject) noteSubject(session.subject);
      csrfToken = session?.csrf || readCsrfCookie();
    }
    const operation = apiDiagnosticOperation(path, "POST");
    const code = path === "/api/telemetry" ? null : apiDiagnostic(r.status, data, operation);
    if (code) announceDiagnostic(code, "api", apiDiagnosticMessage(r.status, data), apiDiagnosticReference(data, r.headers.get("x-request-id")), operation, null, "browser_fetch", null, null, apiDiagnosticDetail(path, r.status, data));
    else if (!r.ok) announceAttention(data);
    return { ok: r.ok, status: r.status, data, ...(r.status === 429 ? { error: apiDiagnosticMessage(r.status, data) } : {}) };
  } catch (err) {
    if (path !== "/api/telemetry") announceDiagnostic("network_error", "api", apiDiagnosticMessage(0), null, apiDiagnosticOperation(path, "POST"), null, "browser_fetch", null, null, apiDiagnosticDetail(path, 0), isAbort(err) ? "transient" : "error");
    return {
      ok: false,
      status: 0,
      data: null,
      error: err instanceof Error ? err.message : "Failed to fetch",
    };
  }
}

function clearOnboardingInvite(): void { forgetInvite(); }

function onboardingInvite(): string | undefined { return recallInvite() || undefined; }

/** True when the website download already captured this browser's invitation code (the gateway never returns the code). */
export async function invitePending(): Promise<boolean> {
  try {
    const r = await fetch(resolveGateway("/auth/invite/status"), { credentials: "include" });
    return r.ok && (await r.json())?.pending === true;
  } catch { return false; }
}

/** Operator passphrase login. */
export async function login(passphrase: string): Promise<ApiResult> {
  return authPost("/auth/login", { passphrase });
}

/** Account login. */
export async function loginAccount(email: string, password: string, inviteCode?: string): Promise<ApiResult> {
  return authPost("/auth/login", { email, password, invite_code: inviteCode === undefined ? onboardingInvite() : inviteCode });
}

export async function signup(email: string, password: string, inviteCode?: string): Promise<ApiResult> {
  return authPost("/auth/signup", { email, password, invite_code: inviteCode === undefined ? onboardingInvite() : inviteCode });
}

/** Begin a hosted Cognito authorization flow. The gateway owns state and PKCE. */
export async function startCognitoLogin(provider?: "google", inviteCode?: string): Promise<ApiResult> {
  try {
    const query = provider ? `?provider=${encodeURIComponent(provider)}` : "";
    const r = await fetch(resolveGateway(`/auth/cognito/start${query}`), {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Origin: pageOrigin() },
      body: JSON.stringify({ invite_code: inviteCode === undefined ? onboardingInvite() : inviteCode }),
    });
    const data = await r.json().catch(() => null);
    return { ok: r.ok, status: r.status, data, ...(r.status === 429 ? { error: apiDiagnosticMessage(r.status, data) } : {}) };
  } catch (err) {
    return { ok: false, status: 0, data: null, error: err instanceof Error ? err.message : "Failed to start sign-in" };
  }
}

/** Complete a state-bound Cognito callback and establish the gateway session. */
export async function completeCognitoLogin(code: string, state: string): Promise<ApiResult> {
  return authPost("/auth/cognito/exchange", { code, state, invite_code: onboardingInvite() });
}

/** Request a replacement verification link without exposing account existence. */
export async function resendVerification(email: string): Promise<ApiResult> {
  return authPost("/auth/verification/resend", { email });
}

export async function requestPasswordReset(email: string): Promise<ApiResult> {
  return authPost("/auth/password-reset", { email });
}

/** Consume a one-time verification token supplied only through the email link. */
export async function verifyEmail(token: string): Promise<ApiResult> {
  return authPost("/auth/verify-email", { token });
}

/** A magic-link token creates the hosted cookie session; it is never persisted here. */
export async function consumeMagicLink(token: string): Promise<ApiResult> {
  return authPost("/auth/magic-link/consume", { token, invite_code: onboardingInvite() });
}

/** Reset tokens are accepted only from the email link and never stored locally. */
export async function consumePasswordReset(token: string, password: string): Promise<ApiResult> {
  return authPost("/auth/password-reset/consume", { token, password });
}

export async function logout(): Promise<ApiResult> {
  if (isDesktopShell()) {
    try {
      const data = await desktopInvokeOrThrow<{ state?: string }>("desktop_auth_sign_out");
      if (data.state === "keychain_unavailable") {
        return { ok: false, status: 503, data, error: "Secure credential removal could not be confirmed. Unlock Keychain and retry sign-out." };
      }
      if (data.state !== "signed_out" && data.state !== "revocation_pending") {
        return { ok: false, status: 503, data, error: "The Mac has not confirmed sign-out. Retry from the account connection controls." };
      }
      clearOnboardingInvite();
      noteSubject(null);
      return { ok: true, status: data.state === "revocation_pending" ? 202 : 200, data };
    } catch {
      return { ok: false, status: 0, data: null, error: "Could not disconnect this Mac. Try again." };
    }
  }
  const result = await authPost("/auth/logout", {});
  if (result.ok) { clearOnboardingInvite(); noteSubject(null); }
  return result;
}

export async function ensureSession(passphrase = "changeme", fresh = false): Promise<ApiResult> {
  if (!fresh && sessionMemo && Date.now() - sessionMemo.at < SESSION_MEMO_MS) return sessionMemo.result;
  if (isLocalConsole() && localSessionFlight?.epoch === sessionInvalidationEpoch) return localSessionFlight.result;
  const started = Date.now();
  const pending = checkSession(passphrase);
  if (isLocalConsole()) localSessionFlight = { epoch: sessionInvalidationEpoch, result: pending };
  const result = await pending;
  if (localSessionFlight?.result === pending) localSessionFlight = null;
  // A 401/403 with no current session is a plain signed-out answer, not a restart to wait out.
  if (!result.ok && !isInvalidSession(result) && (sessionSubject || ![401, 403].includes(result.status) || ["core_unavailable", "core_restarting", "upstream_unavailable", "session_unavailable"].includes(String((result.data as { error?: { code?: string } } | null)?.error?.code)))) noteReconnecting(result.status);
  if (fresh && result.ok) unauthorizedRequests.clear();
  console.info("desktop_startup", { phase: "session_checked", ms: Date.now() - started, ok: result.ok });
  sessionMemo = result.ok ? { at: Date.now(), result } : null;
  return result;
}

async function checkSession(passphrase: string, retried = false): Promise<ApiResult> {
  const startedBeforeInvalidation = sessionInvalidationEpoch;
  const startedReceiptEpoch = sessionReceiptEpoch;
  const native = isDesktopShell();
  const current = () => startedBeforeInvalidation === sessionInvalidationEpoch && startedReceiptEpoch === sessionReceiptEpoch && native === isDesktopShell();
  try {
    if (native) {
      // DesktopBoot also discovers this URL, but its asynchronous effect can
      // finish after the first account check. Resolve it before authentication
      // so hosted sign-in never probes an accidental loopback gateway.
      const signal = AbortSignal.timeout(15_000);
      const gateway = await waitForChatRequest(desktopInvoke<string>("gateway_url"), signal);
      if (gateway) sessionStorage.setItem("cos_gateway_url", gateway.replace(/\/$/, ""));
      const result = await nativeRequest("/auth/session", { signal });
      // Sign-out or another verified actor can retire a check in flight.
      // Never resurrect its old actor from that late response.
      if (!current()) {
        return retried ? { ok: false, status: 401, data: { authenticated: false } } : checkSession(passphrase, true);
      }
      const session = result.data as SessionInfo | null;
      if (result.ok && session?.authenticated && session.subject) {
        noteSubject(session.subject);
        return result;
      }
      if (isInvalidSession(result)) noteSubject(null);
      return { ...result, ok: false };
    }

    const sess = await fetch(resolveGateway("/auth/session"), {
      credentials: "include",
      headers: { Origin: pageOrigin(), "X-Request-ID": newRequestId() },
      signal: AbortSignal.timeout(15_000),
    });
    const data = await sess.json().catch(() => null);
    if (!current()) {
      return retried ? { ok: false, status: 401, data: { authenticated: false } } : checkSession(passphrase, true);
    }
    if (sess.ok && (data as SessionInfo | null)?.authenticated && (data as SessionInfo).subject) {
      noteSubject((data as SessionInfo).subject || null);
      csrfToken =
        (data as { csrf?: string }).csrf || readCsrfCookie() || csrfToken;
      return { ok: true, status: sess.status, data };
    }
    if (sess.status === 429) return { ok: false, status: 429, data, error: apiDiagnosticMessage(429, data) };
    const sessionResult = { ok: sess.ok, status: sess.status, data };
    if (!isInvalidSession(sessionResult)) {
      return { ...sessionResult, ok: false };
    }
    noteSubject(null);
    const parsedGateway = new URL(resolveGateway());
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(parsedGateway.hostname)
      && ["http:", "https:"].includes(parsedGateway.protocol);
    if (isLocalConsole() || !loopback) return { ok: false, status: sess.status, data, error: "unauthenticated" };
    return await login(passphrase);
  } catch {
    if (!current()) return retried ? { ok: false, status: 401, data: { authenticated: false } } : checkSession(passphrase, true);
    return { ok: false, status: 0, data: null, error: "Failed to fetch" };
  }
}

function isCsrfFailure(data: unknown): boolean {
  if (!data || typeof data !== "object") return false;
  const root = data as { detail?: { code?: string }; error?: { code?: string } };
  return root.detail?.code === "csrf_failed" || root.error?.code === "csrf_failed";
}

type ApiInit = RequestInit & { _csrfRetry?: boolean; noInvalidate?: boolean };
type RequestScope = { subject: string | null; epoch: number; invalidation: number; local: boolean };
function requestScope(): RequestScope {
  return { subject: sessionSubject, epoch: sessionReceiptEpoch, invalidation: sessionInvalidationEpoch, local: isLocalConsole() };
}
function requestScopeCurrent(scope: RequestScope): boolean {
  return scope.subject === sessionSubject && scope.epoch === sessionReceiptEpoch && scope.invalidation === sessionInvalidationEpoch;
}
function webRequestCurrent(scope: RequestScope): boolean {
  return !isDesktopShell() && requestScopeCurrent(scope);
}
function retiredRequest(): ApiResult {
  return { ok: false, status: 409, data: null, error: "session_scope_changed" };
}
function retiredTransfer(): Response {
  return new Response(JSON.stringify({ detail: { code: "session_scope_changed" } }), {
    status: 409, headers: { "Content-Type": "application/json" },
  });
}

const localReads = new Map<string, Promise<ApiResult>>();
let ventureListRevision = 0;

function readApi(path: string, init: ApiInit, scope: RequestScope, native: boolean, liveRead = false): Promise<ApiResult> {
  if (!scope.local || native || (init.method || "GET").toUpperCase() !== "GET"
      || Object.keys(init).some(option => option !== "method" && option !== "headers" && option !== "signal")) {
    return rawApi(path, init, scope, native, liveRead);
  }
  const stopped: ApiResult = { ok: false, status: 0, data: null, error: "Request stopped" };
  if (init.signal?.aborted) return Promise.resolve(stopped);
  const key = JSON.stringify([scope.subject, scope.epoch, scope.invalidation, resolveGateway(path), [...new Headers(init.headers)]]);
  let pending = localReads.get(key);
  if (!pending) {
    const flight = rawApi(path, {...init, signal: AbortSignal.timeout(15_000)}, scope, native, liveRead)
      .finally(() => { if (localReads.get(key) === flight) localReads.delete(key); });
    localReads.set(key, flight);
    pending = flight;
  }
  return init.signal ? waitForChatRequest(pending, init.signal).catch(() => stopped) : pending;
}

/** Never throws — network blips return `{ ok: false, status: 0 }`. A successful mutation drops the
 * cached GETs for its venture (or the whole scope when the path names none). */
export async function api(path: string, init: ApiInit = {}): Promise<ApiResult> {
  if (isLocalConsole() && path === "/api/telemetry") return { ok: true, status: 200, data: { ok: true } };
  const scope = requestScope(), native = isDesktopShell();
  const result = await readApi(path, init, scope, native);
  if (!native && !webRequestCurrent(scope)) return retiredRequest();
  const method = (init.method || "GET").toUpperCase();
  if (result.ok && method !== "GET" && method !== "HEAD" && !init.noInvalidate && mutationInvalidates(path)) {
    if (scope.local) localReads.clear();
    const ventureLists = /^(?:PATCH|DELETE):\/api\/ventures\/[^/?]+(?:\?|$)|^POST:\/api\/ventures\/[^/?]+\/(?:restore|pause|resume)(?:\?|$)/.test(`${method}:${path}`);
    if (ventureLists) ventureListRevision++;
    cacheInvalidate(sessionSubject, ventureOf(path), ventureLists);
  }
  return result;
}

function transientReadFailure(status: number): boolean {
  return status === 0 || status === 408 || status === 429 || status >= 500;
}

/** Retry only saved-state reads, never a request that could start work again. */
export async function apiLive(path: string, signal?: AbortSignal): Promise<ApiResult> {
  const scope = requestScope(), native = isDesktopShell();
  const current = () => !signal?.aborted && requestScopeCurrent(scope) && native === isDesktopShell();
  for (let attempt = 0; attempt < 4; attempt++) {
    if (!current()) return retiredRequest();
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, 15_000);
    let result: ApiResult;
    try {
      result = await waitForChatRequest(readApi(path, scope.local ? {} : { signal: controller.signal }, scope, native, true), controller.signal);
    } catch {
      result = { ok: false, status: 0, data: null };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
    if (!current()) return retiredRequest();
    if (result.ok || !transientReadFailure(result.status) || attempt === 3) return result;
    const data = result.data as { error?: { retry_after?: unknown }; detail?: { retry_after?: unknown } } | null;
    const retryAfter = data?.error?.retry_after ?? data?.detail?.retry_after;
    const seconds = result.status === 429 ? typeof retryAfter === "number" && Number.isFinite(retryAfter)
      ? Math.max(1, Math.min(86400, Math.ceil(retryAfter))) : 60 : 0;
    await new Promise<void>(resolve => {
      const wake = () => { clearTimeout(wait); signal?.removeEventListener("abort", wake); resolve(); };
      const wait = setTimeout(wake, Math.max(1000 * 2 ** attempt, seconds * 1000));
      signal?.addEventListener("abort", wake, { once: true });
    });
  }
  return retiredRequest();
}

const inflight = new Map<string, Promise<ApiResult>>();

export function openIdeaSession(ventureId: string): Promise<ApiResult> {
  const scope = sessionSubject, epoch = sessionReceiptEpoch;
  const path = `/api/ventures/${encodeURIComponent(ventureId)}/idea/session-open`;
  const key = `${epoch}|${scope}|${path}`;
  const existing = inflight.get(key);
  if (existing) return existing;
  const cached = cacheGet(scope, path);
  const saved = cached?.fresh ? cached.data as { id?: string } : undefined;
  const base = `/api/ventures/${encodeURIComponent(ventureId)}/idea/sessions`;
  const flight = (saved?.id ? api(`${base}/${encodeURIComponent(saved.id)}`) : api(base, { method: "POST", noInvalidate: true }))
    .then(result => {
      if (scope !== sessionSubject || epoch !== sessionReceiptEpoch) return retiredRequest();
      if (result.ok) cachePut(scope, path, result.data);
      return result;
    }).finally(() => inflight.delete(key));
  inflight.set(key, flight);
  return flight;
}

/** Stale-while-revalidate GET. `onData` fires at once with the cached value (if any) and again
 * with the refreshed one, so callers must treat it as idempotent. Uncached until a subject is verified. */
export async function apiSwr(path: string, onData?: (result: ApiResult) => void): Promise<ApiResult> {
  const scope = sessionSubject, epoch = sessionReceiptEpoch;
  const listRevision = isVentureList(path) ? ventureListRevision : null;
  const current = () => sessionSubject === scope && sessionReceiptEpoch === epoch &&
    (listRevision === null || listRevision === ventureListRevision);
  const hit = cacheGet(scope, path);
  const cached: ApiResult | null = hit ? { ok: true, status: 200, data: hit.data } : null;
  if (cached) onData?.(cached);
  if (hit?.fresh) return cached!;
  const flightKey = `${epoch}|${scope}|${path}|${listRevision}`;
  let flight = inflight.get(flightKey);
  if (!flight) {
    flight = readApi(path, {}, requestScope(), isDesktopShell()).then((r) => { if (r.ok && current()) cachePut(scope, path, r.data); return r; })
      .finally(() => inflight.delete(flightKey));
    inflight.set(flightKey, flight);
  }
  // With a cached copy the caller resolves now and `onData` receives the refresh later.
  const refresh = flight.then((r): ApiResult => {
    if (!current()) return { ok: false, status: 409, data: null, error: "session_scope_changed" };
    if (r.ok || !cached) onData?.(r);
    return r;
  });
  return cached ?? refresh;
}

/** Warm the cache for likely next pages when the renderer is idle. */
export function prefetchApi(paths: string[]): void {
  if (typeof window === "undefined") return;
  const run = () => paths.forEach((p) => void apiSwr(p));
  const idle = (window as unknown as { requestIdleCallback?: (cb: () => void) => number }).requestIdleCallback;
  if (idle) idle(run); else window.setTimeout(run, 500);
}

async function rawApi(path: string, init: ApiInit = {}, scope = requestScope(), native = isDesktopShell(), liveRead = false): Promise<ApiResult> {
  const method = (init.method || "GET").toUpperCase();
  const key = `${sessionReceiptEpoch}|${method}|${path}`;
  const blocked = method === "GET" && unauthorizedRequests.get(key);
  if (blocked && path !== "/auth/session") return blocked;
  if (native) {
    const epoch = sessionReceiptEpoch;
    let result = await nativeRequest(path, init, liveRead);
    const retryable = method === "GET" || method === "HEAD" || !!new Headers(init.headers).get("Idempotency-Key")?.trim();
    if (path !== "/auth/session" && retryable && isTransientNativeIdentityFence(result) && requestScopeCurrent(scope) && scope.subject) {
      const session = await ensureSession(undefined, true);
      if (session.ok && requestScopeCurrent(scope) && !init.signal?.aborted) result = await nativeRequest(path, init, liveRead);
    }
    if (result.status === 401 && !isTransientNativeIdentityFence(result) && method === "GET" && path !== "/auth/session" && epoch === sessionReceiptEpoch) unauthorizedRequests.set(key, result);
    return result;
  }
  if (!webRequestCurrent(scope)) return retiredRequest();
  if (!csrfToken) csrfToken = readCsrfCookie();
  const headers = new Headers(init.headers || {});
  headers.set("X-Request-ID", newRequestId());
  headers.set("Origin", pageOrigin());
  if (method !== "GET" && method !== "HEAD") {
    if ((!scope.local && !csrfToken) || scope.subject === null) {
      const session = await ensureSession();
      if (!webRequestCurrent(scope) || scope.subject === null) return retiredRequest();
      if (!session.ok) return session;
    }
    if (!scope.local) headers.set("X-CSRF-Token", csrfToken || readCsrfCookie());
  }
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  let response: ApiResult | null = null;
  try {
    if (!webRequestCurrent(scope)) return retiredRequest();
    const r = await fetch(resolveGateway(path), { ...init, credentials: "include", headers });
    if (!r.ok) response = { ok: false, status: r.status, data: null };
    const text = await r.text();
    // A response cannot become another account's data or a new request intent.
    if (!webRequestCurrent(scope)) return retiredRequest();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    response = { ok: r.ok, status: r.status, data, ...(r.status === 429 ? { error: apiDiagnosticMessage(r.status, data) } : {}) };
    if (r.status === 403 && !init._csrfRetry && isCsrfFailure(data)) {
      csrfToken = "";
      const session = await ensureSession(undefined, true);
      if (!webRequestCurrent(scope) || scope.subject === null) return retiredRequest();
      if (!session.ok) return session.status === 0 ? response : session;
      return rawApi(path, { ...init, _csrfRetry: true }, scope, native, liveRead);
    }
    const operation = apiDiagnosticOperation(path, method);
    if (r.status === 401 && scope.subject) {
      await ensureSession(undefined, true);
      if (!webRequestCurrent(scope)) return retiredRequest();
    }
    if (path !== "/api/telemetry" && sessionSubject) noteReconnecting(r.status);
    const code = path === "/api/telemetry" ? null : apiDiagnostic(r.status, data, operation);
    if (code) announceDiagnostic(code, "api", apiDiagnosticMessage(r.status, data), apiDiagnosticReference(data, r.headers.get("x-request-id")), operation, null, "browser_fetch", apiDiagnosticOrderId(path), null, apiDiagnosticDetail(path, r.status, data), liveRead && transientReadFailure(r.status) ? "transient" : "error");
    else if (!r.ok) announceAttention(data);
    return response;
  } catch (err) {
    if (!webRequestCurrent(scope)) return retiredRequest();
    if (response) return response;
    if (!isAbort(err, init) && path !== "/api/telemetry" && sessionSubject) noteReconnecting(0);
    if (path !== "/api/telemetry") announceDiagnostic("network_error", "api", apiDiagnosticMessage(0), null, apiDiagnosticOperation(path, method), null, "browser_fetch", apiDiagnosticOrderId(path), null, apiDiagnosticDetail(path, 0), liveRead || isAbort(err, init) ? "transient" : "error");
    return {
      ok: false,
      status: 0,
      data: null,
      error: err instanceof Error ? err.message : "Failed to fetch",
    };
  }
}

export const GATEWAY = resolveGateway();

/** Authenticated non-JSON transfer; desktop credentials remain in the native process. */
export async function authenticatedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const scope = requestScope(), native = isDesktopShell();
  try {
    let response: Response;
    if (native) {
      response = await nativeFetch(path, init);
      if (response.status === 401) await noteNativeDisconnectAfter401();
    } else {
      if (!webRequestCurrent(scope)) return retiredTransfer();
      const headers = new Headers(init.headers);
      headers.set("X-Request-ID", newRequestId());
      const method = (init.method || "GET").toUpperCase();
      if (method !== "GET" && method !== "HEAD") {
        if (!getCsrf() || scope.subject === null) {
          const session = await ensureSession();
          if (!webRequestCurrent(scope) || scope.subject === null) return retiredTransfer();
          if (!session.ok) return new Response(null, { status: session.status >= 400 && session.status <= 599 ? session.status : 503 });
        }
        headers.set("X-CSRF-Token", getCsrf());
      }
      if (!webRequestCurrent(scope)) return retiredTransfer();
      response = await fetch(resolveGateway(path), { ...init, method, headers, credentials: "include", cache: "no-store", redirect: "error" });
      if (!webRequestCurrent(scope)) return retiredTransfer();
    }
    if (!response.ok && path !== "/api/telemetry") {
      // Inspect a copy: file/upload callers must receive the original body intact.
      let data: unknown = null;
      if ((response.headers.get("Content-Type") || "").includes("application/json") &&
          Number(response.headers.get("Content-Length") || 0) <= 16384) {
        try { data = await response.clone().json(); } catch { /* Classification falls back to status. */ }
      }
      if (!native && !webRequestCurrent(scope)) return retiredTransfer();
      const operation = apiDiagnosticOperation(path, init.method);
      if (sessionSubject) noteReconnecting(response.status);
      const code = apiDiagnostic(response.status, data, operation);
      if (code) announceDiagnostic(code, "api", apiDiagnosticMessage(response.status, data), apiDiagnosticReference(data, response.headers.get("x-request-id")), operation,
        native ? native503Origin(response.status, data) : null,
        native ? "native_bridge" : "browser_fetch", apiDiagnosticOrderId(path), null, apiDiagnosticDetail(path, response.status, data));
      else announceAttention(data);
    }
    return !native && !webRequestCurrent(scope) ? retiredTransfer() : response;
  } catch (err) {
    if (!native && !webRequestCurrent(scope)) return retiredTransfer();
    if (!isAbort(err, init) && requestScopeCurrent(scope) && native === isDesktopShell() && path !== "/api/telemetry" && sessionSubject) noteReconnecting(0);
    if (!native && path !== "/api/telemetry") {
      announceDiagnostic("network_error", "api", apiDiagnosticMessage(0), null, apiDiagnosticOperation(path, init.method), null, "browser_fetch", apiDiagnosticOrderId(path), null, apiDiagnosticDetail(path, 0), isAbort(err, init) ? "transient" : "error");
    }
    throw err;
  }
}

/** Fetch an authenticated file and hand it to the platform download UI. */
export async function downloadFile(path: string, filename: string): Promise<boolean> {
  const scope = requestScope(), native = isDesktopShell();
  const current = () => scope.subject !== null && requestScopeCurrent(scope) && native === isDesktopShell();
  let preparedURL: string | null = null, handedOff = false;
  try {
    if (!current()) return false;
    const r = await authenticatedFetch(path);
    if (!current() || !r.ok) return false;
    const blob = await r.blob();
    if (!current()) return false;
    const url = URL.createObjectURL(blob);
    preparedURL = url;
    if (!current()) return false;
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    if (!current()) return false;
    a.click();
    handedOff = true;
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return true;
  } catch {
    return false;
  } finally {
    if (preparedURL && !handedOff) {
      try { URL.revokeObjectURL(preparedURL); } catch { /* Cleanup cannot authorize a retired download. */ }
    }
  }
}
