import { desktopInvokeQuiet, isDesktopShell } from "./desktop.ts";

export type CharlieState = "idle" | "listening" | "speaking";

let lastState: CharlieState | undefined;
let lastUpdate = -Infinity;
let lastLevel: number | null = null;
let lastDesktop = false;

export function getCharlieMascotState(): { state: CharlieState; level: number | null } {
  return { state: lastState ?? "idle", level: lastLevel };
}

export function setCharlieMascot(state: CharlieState, level: number | null = null): void {
  if (typeof window === "undefined") return;
  const desktop = isDesktopShell();
  const now = performance.now();
  if (state === lastState && desktop === lastDesktop && (state !== "speaking" || now - lastUpdate < 50)) return;
  lastState = state;
  lastDesktop = desktop;
  lastUpdate = now;
  const detail = { state, level: level !== null && Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : null };
  lastLevel = detail.level;
  window.dispatchEvent(new CustomEvent("supymem-charlie-mascot", { detail }));
  if (desktop) void desktopInvokeQuiet("charlie_mascot_state", detail);
}
