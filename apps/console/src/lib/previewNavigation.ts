import { scopedWaitlistStatus, scopedWaitlistUrl } from "./waitlistStatusCache.ts";

/** Only public web URLs may be offered as an escape from the embedded preview. */
export function openablePreviewUrl(src: string): string | undefined {
  try {
    const url = new URL(src, typeof window === "undefined" ? "https://preview.invalid" : window.location.href);
    if ((url.protocol === "https:" || url.protocol === "http:") && !url.username && !url.password)
      return url.href;
  } catch { /* No usable public URL. */ }
  return undefined;
}

/** The API response must match this venture before we offer its managed form. */
export function previewWaitlistUrl(value: unknown, ventureId: string): string | undefined {
  const status = scopedWaitlistStatus(value, ventureId);
  return status?.enabled ? scopedWaitlistUrl(status) : undefined;
}
