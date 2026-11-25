import { desktopInvokeOrThrow } from "./desktop.ts";
import { macTaskPermissionsGranted, type PermissionSnapshot } from "./macPermissions.ts";
import { chooseDesktopWindow, type RunningWindow } from "./desktopWindowSelection.ts";
import { chooseNamedDesktopApp, leadingDesktopAppScope } from "./desktopAppMention.ts";

type RunningApp = { name: string; bundle_id: string; pid: number; process_start_identity: string };
type InstalledApp = { name: string; bundle_id: string };
export type DesktopSelection = {
  target_application: string; target_pid: number; target_process_start: string;
  target_window: number; target_window_title: string;
};

const BROWSERS = new Set([
  "com.google.Chrome", "com.brave.Browser", "com.apple.Safari", "org.mozilla.firefox",
  "com.microsoft.edgemac", "com.operasoftware.Opera",
]);
const BROWSER_NAMES: Record<string, string> = {
  chrome: "com.google.Chrome", brave: "com.brave.Browser", safari: "com.apple.Safari",
  firefox: "org.mozilla.firefox", edge: "com.microsoft.edgemac", opera: "com.operasoftware.Opera",
};
const BUNDLE_ID = /^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/;
const isRunningApp = (value: unknown): value is RunningApp => {
  if (!value || typeof value !== "object") return false;
  const app = value as Partial<RunningApp>;
  return typeof app.name === "string" && app.name.length > 0
    && typeof app.bundle_id === "string" && BUNDLE_ID.test(app.bundle_id)
    && Number.isSafeInteger(app.pid) && (app.pid ?? 0) > 0
    && typeof app.process_start_identity === "string" && app.process_start_identity.length > 0;
};

/** Founders never see native `desktop_*` codes. */
const plainLaunchError = (error: unknown, name: string): Error => {
  const code = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  if (code.includes("desktop_app_not_installed")) return new Error(`${name} is not installed on this Mac.`);
  if (code.includes("desktop_app_window_unavailable")) {
    return new Error(`Supymem started ${name} but could not open a usable window. Check that this app can open a normal window, then retry.`);
  }
  return new Error(`Supymem could not open ${name} on this Mac. Open it yourself, then send the request again.`);
};

/** Resolve only against the signed app's authenticated local inventory. */
export async function selectDesktopApplication(
  instruction: string,
  invoke: typeof desktopInvokeOrThrow = desktopInvokeOrThrow,
): Promise<DesktopSelection> {
  let permissions: PermissionSnapshot | null;
  try { permissions = await invoke<PermissionSnapshot>("permissions_status"); }
  catch { throw new Error("Supymem could not check your Mac permissions. Reopen Supymem, then retry this Mac task."); }
  if (!macTaskPermissionsGranted(permissions)) {
    throw new Error("Turn on Screen Recording and Accessibility for Supymem in System Settings, then retry this Mac task.");
  }
  const result = await invoke<{ items?: unknown; installed?: unknown; default_browser_bundle?: unknown }>("desktop_app_inventory");
  const apps = (Array.isArray(result?.items) ? result.items : []).filter(isRunningApp);
  const installed = (Array.isArray(result?.installed) ? result.installed : []).filter((value): value is InstalledApp => {
    if (!value || typeof value !== "object") return false;
    const app = value as Partial<InstalledApp>;
    return typeof app.name === "string" && app.name.length > 0
      && typeof app.bundle_id === "string" && BUNDLE_ID.test(app.bundle_id);
  });
  const lower = instruction.toLowerCase();
  const browserRequest = /\b(?:browser|chrome|brave|safari|firefox|edge|opera)\b/.test(lower);
  const explicitApp = chooseNamedDesktopApp(instruction, installed);
  if ((leadingDesktopAppScope(instruction)
      || /^\s*if\s+[a-z][a-z0-9 .'-]{1,50}?\s+(?:is|has|shows|displays)\b/i.test(instruction))
      && !explicitApp) {
    throw new Error("Supymem could not identify one installed app from the leading app scope. Name one installed app and try again.");
  }
  let selected: RunningApp | undefined;
  let installedChoice: InstalledApp | undefined;
  if (browserRequest && (!explicitApp || BROWSERS.has(explicitApp.bundle_id))) {
    if (explicitApp) {
      selected = apps.find((app) => app.bundle_id === explicitApp.bundle_id);
      installedChoice = explicitApp;
    } else {
      for (const [name, bundle] of Object.entries(BROWSER_NAMES)) {
        if (new RegExp(`\\b${name}\\b`).test(lower)) {
          selected = apps.find((app) => app.bundle_id === bundle);
          installedChoice = installed.find((app) => app.bundle_id === bundle);
          if (selected || installedChoice) break;
          throw new Error(`${name} is not installed on this Mac. Ask Supymem to use your browser instead.`);
        }
      }
    }
    if (!selected && !installedChoice) {
      const preferred = typeof result.default_browser_bundle === "string" ? result.default_browser_bundle : "";
      selected = apps.find((app) => app.bundle_id === preferred);
      installedChoice = installed.find((app) => app.bundle_id === preferred);
      if (!selected && !installedChoice) {
        const browsers = installed.filter((app) => BROWSERS.has(app.bundle_id));
        installedChoice = browsers.find((app) => app.bundle_id === "com.apple.Safari")
          ?? (browsers.length === 1 ? browsers[0] : undefined);
      }
    }
    if (!selected && !installedChoice) throw new Error("Supymem could not identify an installed browser. Name an installed browser in the request.");
  } else {
    installedChoice = explicitApp ?? undefined;
    if (!installedChoice) throw new Error("Name one installed Mac app to control in your request.");
    selected = apps.find((app) => app.bundle_id === installedChoice?.bundle_id);
  }
  const expectedBundle = selected?.bundle_id ?? installedChoice?.bundle_id;
  const launchedNow = !selected && !!installedChoice;
  if (!selected && installedChoice) {
    try {
      selected = await invoke<RunningApp>("desktop_launch_installed_app", { bundleId: installedChoice.bundle_id });
    } catch (error) {
      throw plainLaunchError(error, installedChoice.name);
    }
  }
  if (!selected) throw new Error("Supymem could not start the selected Mac app.");
  if (!isRunningApp(selected) || selected.bundle_id !== expectedBundle) {
    throw new Error("The Mac returned an invalid application identity. Reopen Supymem and retry.");
  }
  type WindowInventory = { windows?: unknown; permissions?: { screen_recording?: boolean; accessibility?: boolean } };
  let details: WindowInventory = await invoke<WindowInventory>("desktop_app_inventory", { bundleId: selected.bundle_id });
  const usableWindows = (inventory: WindowInventory) => (Array.isArray(inventory?.windows) ? inventory.windows : []).filter((value): value is RunningWindow => {
    if (!value || typeof value !== "object") return false;
    const window = value as Partial<RunningWindow>;
    return Number.isSafeInteger(window.window_id) && (window.window_id || 0) > 0
      && typeof window.title === "string" && typeof window.on_screen === "boolean"
      && Number.isFinite(window.width) && Number.isFinite(window.height);
  }).filter((window) => window.on_screen && window.width >= 300 && window.height >= 200);
  let windows = usableWindows(details);
  // Launch can return before macOS creates the app's first window. Wait only
  // for a newly launched app, so a missing permission or hidden existing
  // window still produces an actionable error instead of a long delay.
  for (let attempt = 0; launchedNow && !windows.length && attempt < 10 && details.permissions?.screen_recording !== false; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 200));
    details = await invoke<WindowInventory>("desktop_app_inventory", { bundleId: selected.bundle_id });
    windows = usableWindows(details);
  }
  if (!windows.length && details.permissions?.screen_recording !== false) {
    // A running app may have no window. The founder explicitly asked to open
    // it; ask the signed native helper for one bounded foreground launch.
    try {
      selected = await invoke<RunningApp>("desktop_launch_installed_app", {
        bundleId: selected.bundle_id, ensureWindow: true,
      });
    } catch {
      throw new Error(`Supymem started ${selected.name} but could not open a usable window. Check that this app can open a normal window, then retry.`);
    }
    if (!isRunningApp(selected) || selected.bundle_id !== expectedBundle) {
      throw new Error("The Mac returned an invalid application identity. Reopen Supymem and retry.");
    }
    details = await invoke<WindowInventory>("desktop_app_inventory", { bundleId: selected.bundle_id });
    windows = usableWindows(details);
  }
  if (!windows.length) {
    if (details.permissions?.screen_recording === false) {
      throw new Error(`Supymem can see ${selected.name}, but this copy of Supymem needs Screen Recording access in macOS System Settings before it can inspect its windows. Grant access, reopen Supymem, and retry.`);
    }
    throw new Error(`Supymem can see ${selected.name}, but cannot see a usable window. Open a normal window in ${selected.name}, then retry. If it is already open, check Screen Recording access for this copy of Supymem.`);
  }
  const window = chooseDesktopWindow(windows);
  if (!window) throw new Error(`Supymem found only account or sign-in windows in ${selected.name}. Open a blank window in the app and retry; Supymem will not use an unrelated account window for this task.`);
  return { target_application: selected.bundle_id, target_pid: selected.pid,
    target_process_start: selected.process_start_identity, target_window: window.window_id,
    target_window_title: window.title };
}
