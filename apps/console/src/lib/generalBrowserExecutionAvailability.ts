import { browserExecutionProfileDigest, parseBrowserExecutionProfile } from "./browserExecutionProfile.ts";

export type GeneralBrowserExecutionAvailability = "available" | "unavailable" | "unknown";

/** The server's saved-profile capability is required; missing responses grant nothing. */
export function generalBrowserExecutionAvailability(result: { ok?: unknown; data?: unknown }): GeneralBrowserExecutionAvailability {
  if (result.ok !== true || !result.data || typeof result.data !== "object") return "unknown";
  const data = result.data as { available?: unknown; general_browser_execution_v1?: unknown };
  if (data.available === false || data.general_browser_execution_v1 === false) return "unavailable";
  return data.available === true && data.general_browser_execution_v1 === true ? "available" : "unknown";
}

export async function generalBrowserOrderAvailability(result: { ok?: unknown; data?: unknown }, savedProfile?: unknown): Promise<GeneralBrowserExecutionAvailability> {
  const status = generalBrowserExecutionAvailability(result);
  if (savedProfile === undefined || savedProfile === null) return status;
  const digest = await browserExecutionProfileDigest(savedProfile);
  if (!digest) return "unavailable";
  if (status !== "available") return status;
  const version = parseBrowserExecutionProfile(savedProfile)!.version;
  const data = result.data as Record<string, unknown>;
  if (data[version] === false) return "unavailable";
  return data[version] === true && data.execution_profile_digest === digest ? "available" : "unknown";
}

export function generalBrowserExecutionMessage(status: GeneralBrowserExecutionAvailability, savedProfile?: unknown): string {
  if (savedProfile !== undefined && savedProfile !== null) return "Supymem could not confirm this saved browser model and pricing profile. Approval and execution remain unavailable until the server and this app support the exact saved review. No other model is substituted. Saved results and cancellation remain available.";
  return status === "unknown"
    ? "Supymem could not verify whether this general browser task is available. Refresh its saved review before continuing."
    : "General background browser goals are unavailable in this beta while action selection is being verified. Reviewed published-page checks, supported static coding and terminal checks use separate task profiles. Saved results and cancellation remain available.";
}
