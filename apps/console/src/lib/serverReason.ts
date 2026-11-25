import type { ApiResult } from "./client.ts";
import { usageLimitMessage } from "./credits.ts";
import { apiDiagnosticServerCode } from "./uiDiagnostics.ts";

/** The plain reason a failed call returned ({error|detail.error}.message), else the fallback. */
export function serverReason(result: ApiResult, fallback: string): string {
  if (result.status === 402 || apiDiagnosticServerCode(result.data)?.endsWith("insufficient_credits")) return usageLimitMessage();
  const d = result.data as {
    error?: { message?: string };
    detail?: { error?: { message?: string } } | string;
  } | null;
  const detail = typeof d?.detail === "string" ? undefined : d?.detail?.error?.message;
  const msg = d?.error?.message || detail;
  return typeof msg === "string" && msg.trim() ? msg.trim() : fallback;
}
