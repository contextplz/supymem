/** Tauri 2 invoke/listen without adding @tauri-apps/api to the Next bundle. */
import { announceDiagnostic, nativeDiagnosticMessage, nativeDiagnosticSeverity } from "./uiDiagnostics.ts";

export type DesktopAPI = {
  invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
  listen: (event: string, handler: (payload: unknown) => void) => Promise<() => void>;
};

function raw(): DesktopAPI | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    __TAURI__?: {
      core?: { invoke?: DesktopAPI["invoke"] };
      event?: {
        listen?: (e: string, cb: (ev: { payload: unknown }) => void) => Promise<() => void>;
      };
    };
    __TAURI_INTERNALS__?: { invoke?: DesktopAPI["invoke"] };
  };
  const invoke = w.__TAURI__?.core?.invoke || w.__TAURI_INTERNALS__?.invoke;
  if (!invoke) return null;
  const listenRaw = w.__TAURI__?.event?.listen;
  return {
    invoke,
    listen: async (event, handler) => {
      if (!listenRaw) throw new Error("The Mac app event bridge is unavailable. Reopen Supymem before starting local tools.");
      return listenRaw(event, (ev) => handler(ev.payload));
    },
  };
}

export function isDesktopShell(): boolean {
  return raw() !== null;
}

/** The webview may finish installing its bridge after React hydrates or resumes. */
export function waitForDesktopShell(
  timeoutMs = 1500,
  intervalMs = 100,
  signal?: AbortSignal,
): Promise<boolean> {
  if (isDesktopShell()) return Promise.resolve(true);
  if (signal?.aborted || timeoutMs <= 0) return Promise.resolve(false);
  return new Promise((resolve) => {
    const started = Date.now();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (desktop: boolean) => {
      if (timer) clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      resolve(desktop);
    };
    const abort = () => finish(false);
    const check = () => {
      if (isDesktopShell()) return finish(true);
      const remaining = timeoutMs - (Date.now() - started);
      if (remaining <= 0) return finish(false);
      timer = setTimeout(check, Math.min(intervalMs, remaining));
    };
    signal?.addEventListener("abort", abort, { once: true });
    check();
  });
}

export async function desktopInvoke<T = unknown>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T | null> {
  const api = raw();
  if (!api) return null;
  try {
    return (await api.invoke(cmd, args)) as T;
  } catch (err) {
    reportNativeFailure(cmd, err);
    return null;
  }
}

/** Invoke without reporting a failure: for telemetry plumbing, which must never produce errors of its own
 *  (an older Mac app does not know newer commands). Returns null when the command is missing or fails. */
export async function desktopInvokeQuiet<T = unknown>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  try { return ((await raw()?.invoke(cmd, args)) ?? null) as T | null; } catch { return null; }
}

/** Same as desktopInvoke but keeps the native error so the UI can say why. */
export function invokeErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message.trim()) return err.message;
  if (typeof err === "string" && err.trim()) return err;
  if (err && typeof err === "object") {
    const rec = err as Record<string, unknown>;
    for (const key of ["message", "error", "msg"]) {
      const value = rec[key];
      if (typeof value === "string" && value.trim()) return value;
    }
  }
  return fallback;
}

/** True only when the IPC layer does not know the command — not a runtime failure. */
export function isUnknownDesktopCommand(err: string, command: string): boolean {
  const raw = err.toLowerCase();
  const cmd = command.toLowerCase();
  return (
    raw.includes(`unknown command ${cmd}`) ||
    raw.includes(`command ${cmd} not found`) ||
    raw.includes(`command ${cmd} not allowed`) ||
    raw.includes(`${cmd} not allowed`)
  );
}

function reportNativeFailure(command: string, err: unknown, quiet = false): string {
  const message = invokeErrorMessage(err, "The Mac app could not run that command.");
  const diagnostic = nativeDiagnosticMessage(command, message);
  // Background polls/cleanup and bridge-starting states are still logged, but as transient.
  const severity = quiet ? "transient" : nativeDiagnosticSeverity(message);
  if (diagnostic) announceDiagnostic("native_error", "native", diagnostic, null, null, null, null, null, command, null, severity);
  return message;
}

/** `quiet` is for background polls/cleanup the caller retries or ignores: still thrown and logged, but as a transient event. */
export async function desktopInvokeOrThrow<T = unknown>(
  cmd: string,
  args?: Record<string, unknown>,
  opts?: { quiet?: boolean },
): Promise<T> {
  const api = raw();
  if (!api) {
    throw new Error("This action needs the Supymem Mac app.");
  }
  try {
    return (await api.invoke(cmd, args)) as T;
  } catch (err) {
    throw new Error(reportNativeFailure(cmd, err, opts?.quiet));
  }
}

export async function desktopListen(
  event: string,
  handler: (payload: unknown) => void,
): Promise<() => void> {
  const api = raw();
  if (!api) return () => undefined;
  try {
    return await api.listen(event, handler);
  } catch (err) {
    reportNativeFailure("listen_event", err);
    return () => undefined;
  }
}

/** Control sessions require a real listener; a no-op would lose permission requests. */
export async function desktopListenOrThrow(
  event: string,
  handler: (payload: unknown) => void,
): Promise<() => void> {
  const api = raw();
  if (!api) throw new Error("This action needs the Supymem Mac app.");
  try {
    return await api.listen(event, handler);
  } catch (err) {
    throw new Error(reportNativeFailure("listen_event", err));
  }
}
