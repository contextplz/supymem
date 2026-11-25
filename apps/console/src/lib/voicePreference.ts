const KEY = "supymem.charlie.inputMode.v1";

export function prefersVoice(): boolean {
  try { return window.localStorage.getItem(KEY) !== "typing"; } catch { return true; }
}

export function rememberInputMode(mode: "voice" | "typing"): void {
  try { window.localStorage.setItem(KEY, mode); } catch {}
}
