/** One-press emergency Stop for computer use (Mac app only). The native shell owns the stop; every
 *  button and the chat command in the console call the same `emergency_stop_computer_use` command. */
import { desktopInvokeOrThrow, desktopInvokeQuiet, desktopListen, isDesktopShell } from "./desktop.ts";

export type StopSource = "app" | "chat";
export type StopReceipt = { status: "stopped_by_user"; source: string; bridge: string; order_id: string | null };
export const SHORTCUT_LABEL = "⌃⌥⌘.";
export const ACTIVE_EVENT = "computer-use-active";

/** "stop", "emergency stop", "stop computer use", "stop the browser task" ... and nothing longer,
 *  so a sentence that merely contains the word still goes to chat. */
const STOP_COMMAND = /^\s*(?:please\s+)?(?:emergency[\s-]+stop|stop(?:\s+(?:it|now|everything|computer[\s-]+use|the\s+(?:mac|browser|computer)(?:\s+task)?|(?:the\s+)?(?:mac|browser|computer)\s+task))?)\s*[.!]?\s*$/i;

export function isStopCommand(text: string): boolean {
  return STOP_COMMAND.test(text || "");
}

/** The red Stop button shows only in the Mac app while a device task is running. */
export function stopButtonVisible(desktop: boolean, active: boolean): boolean {
  return desktop && active;
}

/** The chat word "stop" is a stop command only when there is something to stop; otherwise chat answers it. */
export function chatStopApplies(text: string, desktop: boolean, active: boolean): boolean {
  return stopButtonVisible(desktop, active) && isStopCommand(text);
}

type Invoke = typeof desktopInvokeOrThrow;

export async function emergencyStop(source: StopSource, invoke: Invoke = desktopInvokeOrThrow): Promise<StopReceipt> {
  return invoke<StopReceipt>("emergency_stop_computer_use", { source });
}

export function stopNotice(receipt: StopReceipt | null): string {
  if (!receipt) return "Could not reach the Mac app to stop. Press ⌃⌥⌘. or use the menu bar item.";
  return receipt.bridge === "idle"
    ? "Nothing was running. Computer use is stopped."
    : `Stopped. Nothing further will run. Anything uncertain stays held for your review.`;
}

/** Subscribe to running/idle. Returns an unsubscribe. Safe outside the Mac app (never fires). */
export async function watchComputerUse(onChange: (active: boolean) => void): Promise<() => void> {
  if (!isDesktopShell()) return () => undefined;
  const status = await desktopInvokeQuiet<{ active?: boolean }>("computer_use_status"); // quiet: older Mac apps lack it
  if (status) onChange(status.active === true);
  return desktopListen(ACTIVE_EVENT, (payload) => onChange((payload as { active?: boolean } | null)?.active === true));
}
