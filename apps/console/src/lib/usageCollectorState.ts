export type UsageCollectorState =
  | "collecting"
  | "waiting_for_subject"
  | "unmapped_route"
  | "subject_mismatch"
  | "hidden";

const HEALTH_KEY = "supymem.usage.health.v1";
const HEALTH_STATES: readonly UsageCollectorState[] = ["collecting", "waiting_for_subject", "unmapped_route", "subject_mismatch", "hidden"];

/** A bounded support diagnostic retained across WebView restarts, without identity or content. */
export function reportUsageCollectorState(state: UsageCollectorState, storage?: Pick<Storage, "getItem" | "setItem">): void {
  try {
    if (!storage) return;
    const parsed: unknown = JSON.parse(storage.getItem(HEALTH_KEY) || "null");
    const history = Array.isArray(parsed) ? parsed.filter((item): item is { state: UsageCollectorState; at: string } =>
      item && typeof item === "object" && HEALTH_STATES.includes(item.state) &&
      typeof item.at === "string" && /^\d{4}-\d\d-\d\dT/.test(item.at)).slice(-15) : [];
    const at = new Date().toISOString();
    if (history.at(-1)?.state === state && Date.parse(at) - Date.parse(history.at(-1)!.at) < 60000) return;
    storage.setItem(HEALTH_KEY, JSON.stringify([...history, { state, at }]));
  } catch {
    // Storage denial cannot break the collector.
  }
}

export function listenForUsageSubject(
  target: Pick<Window, "addEventListener" | "removeEventListener">,
  onChange: (next: { subject?: string; recheck: boolean }) => void,
  local = false,
): () => void {
  const changed = (event: Event) => {
    const value = (event as CustomEvent<{ subject?: unknown }>).detail?.subject;
    const subject = typeof value === "string" && (value.startsWith("usr_") || value === "founder" || (local && value.startsWith("per_"))) ? value : undefined;
    onChange({ subject, recheck: !subject });
  };
  target.addEventListener("supymem-session-changed", changed);
  return () => target.removeEventListener("supymem-session-changed", changed);
}
