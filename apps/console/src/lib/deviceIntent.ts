/** Narrow Mac connect intent. Challenge and state only; never tokens or secrets. */

export const DEVICE_INTENT_KEY = "supymem.device.connect.intent:v1";
// Match the native pending authorization and Gateway challenge binding window.
export const DEVICE_INTENT_TTL_MS = 5 * 60 * 1000;
export const DEVICE_CHALLENGE_RE = /^[A-Za-z0-9_-]{43}$/;
export const DEVICE_STATE_RE = /^[A-Za-z0-9_-]{32,128}$/;

export type DeviceConnectIntent = {
  version: 1;
  code_challenge: string;
  state?: string;
  device_name: string;
  platform: string;
  issued_at?: number;
};

export type DeviceIntentParams = {
  code_challenge?: string | null;
  state?: string | null;
  device_name?: string | null;
  platform?: string | null;
};

type IntentStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/** Browser custody age only; Gateway and the native app still authorize the Mac. */
export function deviceIntentIsFresh(intent: DeviceConnectIntent, now = Date.now()): boolean {
  const issuedAt = intent.issued_at;
  return typeof issuedAt === "number" && Number.isSafeInteger(issuedAt) && Number.isSafeInteger(now)
    && issuedAt >= 0 && now >= issuedAt && now - issuedAt <= DEVICE_INTENT_TTL_MS;
}

function sessionStore(): IntentStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function macProductName(hostname = typeof window === "undefined" ? "" : window.location.hostname): string {
  return hostname.toLowerCase().includes("staging") ? "Supymem Staging" : "Supymem";
}

export function deviceIntentFromParams(params: DeviceIntentParams): DeviceConnectIntent | null {
  const challenge = (params.code_challenge || "").trim();
  const state = (params.state || "").trim();
  const name = (params.device_name || "Supymem for Mac").trim() || "Supymem for Mac";
  const platform = (params.platform || "macos").trim() || "macos";
  if (!DEVICE_CHALLENGE_RE.test(challenge)) return null;
  if (state && !DEVICE_STATE_RE.test(state)) return null;
  if (name.length > 128 || platform.length > 32) return null;
  return {
    version: 1,
    code_challenge: challenge,
    ...(state ? { state } : {}),
    device_name: name,
    platform,
  };
}

export function parseDeviceIntent(raw: string | null): DeviceConnectIntent | null {
  try {
    const value = JSON.parse(raw || "null");
    if (!value || value.version !== 1) return null;
    if (value.token || value.password || value.refresh_token || value.code || value.redirect || value.return_to) {
      return null;
    }
    return deviceIntentFromParams({
      code_challenge: value.code_challenge,
      state: value.state,
      device_name: value.device_name,
      platform: value.platform,
    });
  } catch {
    return null;
  }
}

export function serializeDeviceIntent(intent: DeviceConnectIntent): string | null {
  const clean = deviceIntentFromParams(intent);
  if (!clean) return null;
  return JSON.stringify(clean);
}

export function readDeviceIntent(
  storage: IntentStorage | null = sessionStore(),
  now = Date.now(),
  pathname = typeof window === "undefined" ? null : window.location.pathname,
): DeviceConnectIntent | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(DEVICE_INTENT_KEY);
    const intent = parseDeviceIntent(raw);
    const issuedAt = JSON.parse(raw || "null")?.issued_at;
    if (!intent || !Number.isSafeInteger(issuedAt) || !Number.isSafeInteger(now)
      || issuedAt < 0 || now < issuedAt || now - issuedAt > DEVICE_INTENT_TTL_MS
      || (pathname !== null && !["/auth/device", "/auth/device/approve", "/auth/consume", "/auth/cognito/callback"].includes(pathname))) {
      clearDeviceIntent(storage);
      return null;
    }
    return { ...intent, issued_at: issuedAt };
  } catch {
    clearDeviceIntent(storage);
    return null;
  }
}

export function clearDeviceIntent(storage: IntentStorage | null = sessionStore()): void {
  try { storage?.removeItem(DEVICE_INTENT_KEY); } catch { /* Recovery still returns to normal sign-in. */ }
}

export function persistDeviceIntent(
  intent: DeviceConnectIntent,
  storage: IntentStorage | null = sessionStore(),
  now = Date.now(),
  allowWithoutStorage = false,
): DeviceConnectIntent | null {
  const clean = deviceIntentFromParams(intent);
  if (!clean || !Number.isSafeInteger(now) || now < 0) return null;
  const retained = { ...clean, issued_at: intent.issued_at ?? now };
  if (!deviceIntentIsFresh(retained, now)) return null;
  // A validated current URL can reach Gateway without a browser storage hop.
  if (!storage) return allowWithoutStorage ? retained : null;
  let snapshot = retained;
  try {
    const previousRaw = storage.getItem(DEVICE_INTENT_KEY);
    const previous = parseDeviceIntent(previousRaw);
    // Rendering or retrying this request must not extend the same Mac attempt.
    const previousTime = previous ? JSON.parse(previousRaw || "null")?.issued_at : undefined;
    const issuedAt = previous?.code_challenge === clean.code_challenge && previous.state === clean.state
      && Number.isSafeInteger(previousTime) ? previousTime : retained.issued_at;
    if (issuedAt < 0 || now < issuedAt || now - issuedAt > DEVICE_INTENT_TTL_MS) {
      clearDeviceIntent(storage);
      return null;
    }
    snapshot = { ...clean, issued_at: Math.min(issuedAt, retained.issued_at) };
    if (!deviceIntentIsFresh(snapshot, now)) return null;
    storage.setItem(DEVICE_INTENT_KEY, JSON.stringify(snapshot));
    return snapshot;
  } catch {
    return allowWithoutStorage ? snapshot : null;
  }
}

export function deviceConsolePath(intent: DeviceConnectIntent): string {
  const params = new URLSearchParams({
    code_challenge: intent.code_challenge,
    device_name: intent.device_name,
    platform: intent.platform,
  });
  if (intent.state) params.set("state", intent.state);
  return `/auth/device?${params.toString()}`;
}

export function deviceApproveUrl(intent: DeviceConnectIntent, gateway: string): string {
  const target = new URL("/auth/device/approve", gateway);
  target.searchParams.set("code_challenge", intent.code_challenge);
  target.searchParams.set("device_name", intent.device_name);
  target.searchParams.set("platform", intent.platform);
  if (intent.state) target.searchParams.set("state", intent.state);
  return target.href;
}

export function continueDeviceApproval(
  intent: DeviceConnectIntent,
  gateway: string,
  assign: (url: string) => void = (url) => window.location.assign(url),
  storage: IntentStorage | null = sessionStore(),
  now = Date.now(),
): boolean {
  if (!deviceIntentIsFresh(intent, now)) return false;
  let ownsStored = false;
  try {
    const current = storage ? parseDeviceIntent(storage.getItem(DEVICE_INTENT_KEY)) : null;
    if (current && (current.code_challenge !== intent.code_challenge || current.state !== intent.state)) return false;
    ownsStored = Boolean(current);
  } catch { /* A valid current URL can continue without deleting unreadable custody. */ }
  const target = deviceApproveUrl(intent, gateway);
  // Gateway carries the reviewed PKCE challenge/state from here. Its page
  // cannot clear this origin's storage after approval or abandonment.
  if (ownsStored) clearDeviceIntent(storage);
  assign(target);
  return true;
}

export function afterEmailVerify(intent: DeviceConnectIntent | null, hostname?: string): {
  path: string | null;
  message: string;
  actionLabel: string;
} {
  if (intent) {
    return {
      path: deviceConsolePath(intent),
      message: "Your email is verified. Your Mac connection still needs approval.",
      actionLabel: "Continue Mac connection",
    };
  }
  return {
    path: null,
    message: `Your email is verified. Open ${macProductName(hostname)} on your Mac and click Sign in, then sign in. This website page cannot finish the Mac connection by itself.`,
    actionLabel: "",
  };
}
