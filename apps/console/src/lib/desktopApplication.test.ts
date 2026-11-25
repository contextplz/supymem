import test from "node:test";
import assert from "node:assert/strict";
import { selectDesktopApplication } from "./desktopApplication.ts";

const chrome = { name: "Google Chrome", bundle_id: "com.google.Chrome" };
const safari = { name: "Safari", bundle_id: "com.apple.Safari" };
const notes = { name: "Notes", bundle_id: "com.apple.Notes" };
const running = (app: typeof chrome, pid = 42) => ({
  ...app, pid, process_start_identity: `start-${pid}`,
});
const window = (window_id: number, title = "Untitled") => ({
  window_id, title, on_screen: true, width: 1200, height: 800,
});

type Call = { cmd: string; args?: Record<string, unknown> };
function nativeReplies(replies: unknown[]) {
  const calls: Call[] = [];
  const invoke = async <T>(cmd: string, args?: Record<string, unknown>): Promise<T> => {
    calls.push({ cmd, args });
    if (cmd === "permissions_status") return {"screen-recording": "granted", accessibility: "granted"} as T;
    assert.ok(replies.length, `unexpected native call ${cmd}`);
    return replies.shift() as T;
  };
  return { invoke, calls };
}

test("a named installed app launches, pins its process, and chooses a safe window", async () => {
  const native = nativeReplies([
    { items: [], installed: [notes] },
    running(notes),
    { windows: [window(1, "Sign in - Google Accounts"), window(2, "Untitled")] },
  ]);
  const selection = await selectDesktopApplication("Open Notes and inspect my notes", native.invoke);
  assert.deepEqual(selection, {
    target_application: notes.bundle_id, target_pid: 42,
    target_process_start: "start-42", target_window: 2, target_window_title: "Untitled",
  });
  assert.deepEqual(native.calls, [
    { cmd: "permissions_status", args: undefined },
    { cmd: "desktop_app_inventory", args: undefined },
    { cmd: "desktop_launch_installed_app", args: { bundleId: notes.bundle_id } },
    { cmd: "desktop_app_inventory", args: { bundleId: notes.bundle_id } },
  ]);
});

test("an unnamed browser request selects the installed macOS default even when closed", async () => {
  const native = nativeReplies([
    { items: [running(safari)], installed: [safari, chrome], default_browser_bundle: chrome.bundle_id },
    running(chrome, 77),
    { windows: [window(8, "New Tab")] },
  ]);
  const selection = await selectDesktopApplication("Open my browser and look at a page", native.invoke);
  assert.equal(selection.target_application, chrome.bundle_id);
  assert.equal(selection.target_pid, 77);
  assert.equal(native.calls[2]?.cmd, "desktop_launch_installed_app");
});

test("use-my-Mac read-only request resolves the exact existing default browser window", async () => {
  const native = nativeReplies([
    { items: [running(chrome, 77)], installed: [safari, chrome], default_browser_bundle: chrome.bundle_id },
    { windows: [window(8, "Example Domain")] },
  ]);
  const selection = await selectDesktopApplication(
    "Use my Mac to open https://example.com in your browser and report its page title. This is read-only: do not edit, submit forms, or send messages.", native.invoke);
  assert.deepEqual(selection, { target_application: chrome.bundle_id, target_pid: 77,
    target_process_start: "start-77", target_window: 8, target_window_title: "Example Domain" });
  assert.deepEqual(native.calls.map((c) => c.cmd), ["permissions_status", "desktop_app_inventory", "desktop_app_inventory"]);
});

test("an explicitly requested browser is not silently replaced by another browser", async () => {
  const native = nativeReplies([
    { items: [running(safari)], installed: [safari], default_browser_bundle: safari.bundle_id },
  ]);
  await assert.rejects(
    selectDesktopApplication("Open Chrome on this Mac", native.invoke),
    /chrome is not installed/i,
  );
  assert.equal(native.calls.length, 2);
});

test("a running app with no window receives one bounded ensure-window request", async () => {
  const native = nativeReplies([
    { items: [running(notes)], installed: [notes] },
    { windows: [], permissions: { screen_recording: true } },
    running(notes),
    { windows: [window(11, "Notes")] },
  ]);
  const selection = await selectDesktopApplication("Open Notes", native.invoke);
  assert.equal(selection.target_window, 11);
  assert.deepEqual(native.calls[3], {
    cmd: "desktop_launch_installed_app",
    args: { bundleId: notes.bundle_id, ensureWindow: true },
  });
});

test("missing screen recording access stops before any foreground launch", async () => {
  const native = nativeReplies([
    { items: [running(notes)], installed: [notes] },
    { windows: [], permissions: { screen_recording: false } },
  ]);
  await assert.rejects(selectDesktopApplication("Open Notes", native.invoke), /Screen Recording access/);
  assert.equal(native.calls.length, 3);
});

test("an account-only window cannot become a task target", async () => {
  const native = nativeReplies([
    { items: [running(chrome)], installed: [chrome] },
    { windows: [window(20, "Sign in - Google Accounts")], permissions: { screen_recording: true } },
    running(chrome),
    { windows: [window(20, "Sign in - Google Accounts")] },
  ]);
  await assert.rejects(selectDesktopApplication("Open Chrome", native.invoke), /only account or sign-in windows/);
});

test("a browser mentioned as output text does not override the explicitly opened app", async () => {
  const native = nativeReplies([
    { items: [running(notes), running(chrome)], installed: [notes, chrome] },
    { windows: [window(31, "Notes")] },
  ]);
  const selection = await selectDesktopApplication("Open Notes and write a note about Chrome", native.invoke);
  assert.equal(selection.target_application, notes.bundle_id);
  assert.deepEqual(native.calls[2], { cmd: "desktop_app_inventory", args: { bundleId: notes.bundle_id } });
});

test("a conflicting app-scoped request cannot fall back to a mentioned browser", async () => {
  const native = nativeReplies([
    { items: [running(notes), running(chrome)], installed: [notes, chrome], default_browser_bundle: chrome.bundle_id },
  ]);
  await assert.rejects(
    selectDesktopApplication("In Notes, click Cancel in Google Chrome", native.invoke),
    /one installed app from the leading app scope/,
  );
  assert.equal(native.calls.length, 2);
});

test("condition-first request pins only the named installed app and rejects an unavailable scope", async () => {
  const native = nativeReplies([
    { items: [running(notes)], installed: [notes] },
    { windows: [window(31, "Open dialog")] },
  ]);
  const selection = await selectDesktopApplication(
    "If Notes is showing an Open dialog, click Cancel and report which window remains.", native.invoke);
  assert.equal(selection.target_application, notes.bundle_id);
  assert.equal(selection.target_window, 31);
  assert.deepEqual(native.calls[2], { cmd: "desktop_app_inventory", args: { bundleId: notes.bundle_id } });

  const missing = nativeReplies([{ items: [running(notes)], installed: [notes] }]);
  await assert.rejects(selectDesktopApplication(
    "If Finder is showing an Open dialog, click Cancel in Notes.", missing.invoke),
    /one installed app from the leading app scope/);
  assert.equal(missing.calls.length, 2);
});

test("a possessive app location pins one installed app without a fallback", async () => {
  const native = nativeReplies([
    { items: [running(notes)], installed: [notes] },
    { windows: [window(31, "Open dialog")] },
  ]);
  const selection = await selectDesktopApplication(
    "In Notes' current Open dialog on this Mac, click Cancel and report what remains.", native.invoke);
  assert.equal(selection.target_application, notes.bundle_id);
  assert.equal(selection.target_window, 31);
  assert.deepEqual(native.calls[2], { cmd: "desktop_app_inventory", args: { bundleId: notes.bundle_id } });

  const unsafe = nativeReplies([{ items: [running(notes)], installed: [notes] }]);
  await assert.rejects(selectDesktopApplication(
    "In Finder's current dialog, click Cancel in Notes.", unsafe.invoke),
    /one installed app from the leading app scope/);
  assert.equal(unsafe.calls.length, 2);
});

test("an explicitly opened browser wins over another browser named as content", async () => {
  const native = nativeReplies([
    { items: [running(safari), running(chrome)], installed: [safari, chrome] },
    { windows: [window(41, "New Tab")] },
  ]);
  const selection = await selectDesktopApplication("Open Safari and write a note about Chrome", native.invoke);
  assert.equal(selection.target_application, safari.bundle_id);
});

test("a launch reply for a different app is rejected before window inspection", async () => {
  const native = nativeReplies([
    { items: [], installed: [notes, chrome] },
    running(chrome),
  ]);
  await assert.rejects(selectDesktopApplication("Open Notes", native.invoke), /invalid application identity/);
  assert.equal(native.calls.length, 3);
});

test("malformed running identity cannot be pinned in a review", async () => {
  const native = nativeReplies([
    { items: [{ ...running(notes), process_start_identity: "" }], installed: [notes] },
    { ...running(notes), pid: 0 },
  ]);
  await assert.rejects(selectDesktopApplication("Open Notes", native.invoke), /invalid application identity/);
  assert.equal(native.calls.length, 3);
});

test("a failed app launch shows plain copy, never the native code", async () => {
  const native = {
    invoke: async (cmd: string) => {
      if (cmd === "permissions_status") return {"screen-recording": "granted", accessibility: "granted"};
      if (cmd === "desktop_app_inventory") return { items: [], installed: [notes] };
      throw new Error("desktop_app_launch_unconfirmed");
    },
  };
  await assert.rejects(
    selectDesktopApplication("Open Notes", native.invoke as never),
    (error: Error) => !/desktop_/.test(error.message) && /Notes/.test(error.message),
  );
});


test("passive denial stops app inventory before the helper can probe screen access", async () => {
  const calls: string[] = [];
  const invoke = async <T>(command: string): Promise<T> => {
    calls.push(command);
    assert.equal(command, "permissions_status");
    return {"screen-recording": "not_determined", accessibility: "granted"} as T;
  };
  await assert.rejects(selectDesktopApplication("Open Notes", invoke), /Screen Recording/);
  assert.deepEqual(calls, ["permissions_status"]);
});
