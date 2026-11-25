import { CATALOG } from "./featureCatalog.ts";

/** Closed control IDs only, generated from the server feature catalog. Never derive telemetry from labels, hrefs, or text.
 *  The label is the coarse group older builds sent when one exists (history stays in one bucket), else the fine slug. */
export const CONTROL_FEATURE: Record<string, string> = Object.fromEntries(
  Object.entries(CATALOG.controls).map(([id, [slug, legacy]]) => [id, legacy ?? slug]));

export const USAGE_CONTROL_EVENT = "supymem-usage-control-activate";
export const MAC_START_EXPOSURE_EVENT = "supymem-mac-start-exposure";

export function macStartOrderId(value: string | null): string | null {
  return value && /^dwo_[A-Za-z0-9_-]{1,64}$/.test(value) ? value : null;
}

export function announceMacStartExposure(orderId: string): void {
  if (typeof window === "undefined" || !macStartOrderId(orderId)) return;
  window.dispatchEvent(new CustomEvent(MAC_START_EXPOSURE_EVENT, { detail: { orderId } }));
}

export function usageControl(value: string | null): { controlId: string; feature: string } | null {
  if (!value || !Object.hasOwn(CONTROL_FEATURE, value)) return null;
  return { controlId: value, feature: CONTROL_FEATURE[value] };
}

export function contentGenerationConfirmControl(kind: "topics" | "draft"): "content_topics_confirm" | "content_draft_confirm" {
  return kind === "topics" ? "content_topics_confirm" : "content_draft_confirm";
}

const WAITLIST_CONTROLS = {
  website: { enable: "waitlist_enable_website", close: "waitlist_close_website",
    test: "waitlist_test_website", refresh: "waitlist_refresh_website", copy: "waitlist_copy_website" },
  gtm: { enable: "waitlist_enable_gtm", close: "waitlist_close_gtm",
    test: "waitlist_test_gtm", refresh: "waitlist_refresh_gtm", copy: "waitlist_copy_gtm" },
} as const;

export function waitlistUsageControl(surface: "website" | "gtm", action: "enable" | "close" | "test" | "refresh" | "copy") {
  return WAITLIST_CONTROLS[surface][action];
}

/** Report only a closed control ID; never send the chat body or the control's DOM. */
export function announceUsageControl(value: string): void {
  const control = usageControl(value);
  if (!control || typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(USAGE_CONTROL_EVENT, { detail: { controlId: control.controlId } }));
}

export function isMacReviewHref(href: string): boolean {
  return /^\/v\/[^/?#]+\/device-work-orders\/[^/?#]+(?:[?#].*)?$/.test(href);
}
