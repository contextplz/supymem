import { browserExecutionProfileDigest, parseBrowserExecutionProfile, type BrowserExecutionProfile } from "./browserExecutionProfile.ts";

export type BrowserProfileChoice = { profile: BrowserExecutionProfile; digest: string };

/** A listed profile is a preparation choice, never an execution grant. */
export async function browserProfileCatalog(result: {ok?: unknown; data?: unknown}): Promise<BrowserProfileChoice | null> {
  if (result.ok !== true || !result.data || typeof result.data !== "object") return null;
  const data = result.data as {available?: unknown; execution_profile?: unknown; execution_profile_digest?: unknown};
  if (data.available !== true || typeof data.execution_profile_digest !== "string") return null;
  const profile = parseBrowserExecutionProfile(data.execution_profile);
  if (!profile) return null;
  const digest = await browserExecutionProfileDigest(profile);
  return digest && digest === data.execution_profile_digest ? {profile, digest} : null;
}
