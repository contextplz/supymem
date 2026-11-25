export type RunningWindow = {
  window_id: number; title: string; on_screen: boolean; width: number; height: number;
};

const BLANK_WINDOW = /^(?:new tab|new window|untitled|blank)$/i;
const ACCOUNT_WINDOW = /\b(?:sign[ -]?in|log[ -]?in|authoriz\w*|authentication|password|two[ -]?factor|2fa|verify(?: your)? account|connect .{0,60} (?:account|for|to))\b/i;

/** An unrelated account window must never become the default task target. */
export function chooseDesktopWindow(windows: RunningWindow[]): RunningWindow | null {
  const safe = windows.filter((window) => !ACCOUNT_WINDOW.test(window.title));
  safe.sort((a, b) => Number(BLANK_WINDOW.test(b.title.trim())) - Number(BLANK_WINDOW.test(a.title.trim()))
    || b.width * b.height - a.width * a.height || a.window_id - b.window_id);
  return safe[0] ?? null;
}
