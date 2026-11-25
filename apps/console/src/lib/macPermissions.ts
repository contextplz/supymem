/** Permission status and explicit Mac-task permission actions. */

export type PermissionName = "accessibility" | "screen-recording" | "microphone" | "automation" | "notifications";
export type PermissionStatus = "granted" | "denied" | "not_determined" | "unknown";
export type PermissionSnapshot = Partial<Record<PermissionName, PermissionStatus>>;

export const PERMISSIONS: ReadonlyArray<{ name: PermissionName; title: string; reason: string; restartNote?: boolean }> = [
  { name: "screen-recording", title: "Screen Recording", restartNote: true,
    reason: "Needed only for Mac tasks: lets Supymem see the app it is working in, after you approve." },
  { name: "accessibility", title: "Accessibility", restartNote: true,
    reason: "Needed only for Mac tasks: lets Supymem click and type in the app you chose, after you approve." },
  { name: "microphone", title: "Microphone",
    reason: "Optional. Lets you talk to Supymem instead of typing." },
  { name: "automation", title: "Automation",
    reason: "Optional. Allows macOS app automation requests when a supported, reviewed task requires them." },
  { name: "notifications", title: "Notifications",
    reason: "Optional. Tells you when something finishes or needs your OK." },
];

export const RESTART_NOTE = "If your Mac asks, quit and reopen Supymem.";

export function statusOf(snapshot: PermissionSnapshot | null, name: PermissionName): PermissionStatus {
  return snapshot?.[name] ?? "unknown";
}

/**
 * One primary button per row. Accessibility and Screen Recording only ever
 * show a system prompt once, and their status never reads "denied", so after
 * the first Allow they fall through to System Settings.
 */
export function rowAction(status: PermissionStatus, requested: boolean): { granted: boolean; button: "Allow" | "Open System Settings" | null } {
  if (status === "granted") return { granted: true, button: null };
  if (status === "not_determined" && !requested) return { granted: false, button: "Allow" };
  return { granted: false, button: "Open System Settings" };
}

export function continueLabel(snapshot: PermissionSnapshot | null): string {
  const all = PERMISSIONS.every((p) => statusOf(snapshot, p.name) === "granted");
  return all ? "Continue" : "Continue (I can turn the rest on later)";
}

/** Beta: Mac computer use is off, so no first-run screen asks for Screen Recording, Accessibility or Automation. */
export const BETA_COMPUTER_USE = false;

const DONE_KEY = "supymem-mac-permissions-setup-done";

export function permissionsSetupDone(): boolean {
  try { return window.localStorage.getItem(DONE_KEY) === "yes"; } catch { return false; }
}

export function markPermissionsSetupDone(): void {
  try { window.localStorage.setItem(DONE_KEY, "yes"); } catch { /* private mode: screen shows again next launch */ }
}

/** Only the desktop app, only once, never on the setup screen itself. */
export function shouldShowFirstRun(desktop: boolean, done: boolean, pathname: string): boolean {
  return desktop && !done && !pathname.startsWith("/setup/permissions");
}

let supported: Promise<boolean> | null = null;
/** True once per app session when the native shell implements `permissions_status`. */
export function nativePermissionsSupported(): Promise<boolean> {
  supported ??= import("./desktop").then(({ desktopInvoke }) => desktopInvoke("permissions_status")).then((r) => r !== null);
  return supported;
}

/**
 * The old helper (peekaboo) modal auto-opens only after the first-run screen is
 * done and only for a real status with a missing permission. An unavailable
 * status (bridge still warming up) never opens it.
 */
export function shouldAutoOpenHelperSetup(
  p: { screen_recording?: boolean; accessibility?: boolean; event_synthesizing?: boolean; status_unavailable?: boolean } | null,
  firstRunDone: boolean,
  computerUseEnabled = BETA_COMPUTER_USE,
): boolean {
  return computerUseEnabled && firstRunDone && !!p && !p.status_unavailable && !(p.screen_recording && p.accessibility && p.event_synthesizing);
}

export const STATUS_RETRY_MS = 3000;
export const STATUS_RETRY_MAX = 20; // about 60s of quiet retries


/** Missing native preflight is never a reason to probe the capture helper. */
export function screenPermissionAction(status: PermissionStatus, requestedBefore: boolean): "granted" | "grant" | "settings" | "retry" {
  if (status === "unknown") return "retry";
  const action = rowAction(status, requestedBefore);
  return action.granted ? "granted" : action.button === "Allow" ? "grant" : "settings";
}

export function macTaskPermissionsGranted(snapshot: PermissionSnapshot | null): boolean {
  return statusOf(snapshot, "screen-recording") === "granted" && statusOf(snapshot, "accessibility") === "granted";
}

export function permissionRequestedBefore(name: "screen-recording" | "accessibility"): boolean {
  try { return window.localStorage.getItem(`supymem-${name}-requested`) === "yes"; }
  catch { return true; } // Without persistence, Settings is safer than requesting again after relaunch.
}

export function markPermissionRequested(name: "screen-recording" | "accessibility"): void {
  // Persist before opening macOS's prompt. If storage fails, the caller offers Settings.
  window.localStorage.setItem(`supymem-${name}-requested`, "yes");
}

export const MAC_TASK_PERMISSION_EVENT = "supymem-mac-permissions-needed";
export type MacTaskPermissionRequest = { resume: () => void | Promise<unknown> };
