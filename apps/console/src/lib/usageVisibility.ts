export type UsageEvent = "ui.screen_view" | "ui.navigation" | "session.active" | "feature.used" | "ui.control_activate" | "ui.control_exposure" | "ui.control_outcome";

export function interactionBackedScreenView(hidden: boolean, desktop: boolean, lastTrustedAt: number, now: number): boolean {
  return hidden && desktop && lastTrustedAt > 0 && now >= lastTrustedAt && now - lastTrustedAt <= 10_000;
}

export function shouldCollectUsageEvent(hidden: boolean, event: UsageEvent, interactionBacked = false): boolean {
  return !hidden || event === "feature.used" || event === "ui.control_activate" || event === "ui.control_outcome" || event === "ui.navigation"
    || (event === "ui.control_exposure" && interactionBacked)
    || (event === "ui.screen_view" && interactionBacked);
}
