export type BrowserAIRole = {
  provider: "openai" | "azure"; model: string; service_tier: "default"; reasoning: "none" | "low"; max_output_tokens: number;
  endpoint?: string; deployment?: string;
};
export type BrowserExecutionProfile = {
  version: "browser_cuai_text_v1" | "browser_cuai_text_v2"; backend: "cuai"; policy_version: 1 | 2;
  roles: { plan: BrowserAIRole; actor: BrowserAIRole; verify: BrowserAIRole };
  pricing_digest: string; max_provider_calls: number; max_request_bytes: number;
  completion_policy: "browser_semantic_review_v1";
};

export function browserReadOnlyRequested(goal: string): boolean {
  return /\bread[ -]?only\b|\bno (?:mutation|changes?|edits?)\b/i.test(goal);
}

/** Authority comes from the saved action tuple, independently of the model profile. */
export function browserReadOnlyAuthority(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const manifest = value as {actions?: unknown; general_browser_verification?: unknown};
  const spec = manifest.general_browser_verification as {allow_type?: unknown; fill_values?: unknown; execution_profile?: unknown} | null;
  return Array.isArray(manifest.actions) && manifest.actions.length === 1 && manifest.actions[0] === "browser.read"
    && !!spec && spec.allow_type === false && !!parseBrowserExecutionProfile(spec.execution_profile)
    && !!spec.fill_values && typeof spec.fill_values === "object" && !Array.isArray(spec.fill_values)
    && Object.keys(spec.fill_values).length === 0;
}

function exactObject(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
function boundedInteger(value: unknown, min: number, max: number): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max;
}
function role(value: unknown, actor = false): value is BrowserAIRole {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const azure = (value as Record<string, unknown>).provider === "azure";
  return exactObject(value, ["provider", "model", "service_tier", "reasoning", "max_output_tokens", ...(azure ? ["endpoint", "deployment"] : [])])
    && (value.provider === "openai" || (azure && typeof value.endpoint === "string" && value.endpoint.length <= 300
      && /^https:\/\/[A-Za-z0-9-]+\.openai\.azure\.com(?::443)?\/openai\/v1\/?$/.test(value.endpoint)
      && typeof value.deployment === "string" && /^[A-Za-z0-9._-]{1,100}$/.test(value.deployment)))
    && typeof value.model === "string" && /^[A-Za-z0-9._-]{1,100}$/.test(value.model)
    && value.service_tier === "default"
    && ((value.reasoning === "none" && value.model !== "gpt-6.1-sol")
      || (actor && value.model === "gpt-6.1-sol" && value.reasoning === "low"))
    && boundedInteger(value.max_output_tokens, 1, 2500);
}

/** Unknown profiles cannot inherit the legacy browser grant. */
export function parseBrowserExecutionProfile(value: unknown): BrowserExecutionProfile | null {
  if (!exactObject(value, ["version", "backend", "policy_version", "roles", "pricing_digest", "max_provider_calls", "max_request_bytes", "completion_policy"])
    || !((value.version === "browser_cuai_text_v1" && value.policy_version === 1)
      || (value.version === "browser_cuai_text_v2" && value.policy_version === 2)) || value.backend !== "cuai"
    || value.completion_policy !== "browser_semantic_review_v1"
    || typeof value.pricing_digest !== "string" || !/^[a-f0-9]{64}$/.test(value.pricing_digest)
    || !boundedInteger(value.max_provider_calls, 1, 128) || !boundedInteger(value.max_request_bytes, 1024, 65536)
    || !exactObject(value.roles, ["plan", "actor", "verify"])
    || !role(value.roles.plan) || !role(value.roles.actor, true) || !role(value.roles.verify)) return null;
  return value as BrowserExecutionProfile;
}

function canonical(value: unknown): string {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** All admitted strings are ASCII, matching Core's sorted ensure_ascii JSON. */
export async function browserExecutionProfileDigest(value: unknown): Promise<string | null> {
  const profile = parseBrowserExecutionProfile(value);
  if (!profile) return null;
  try {
    const bytes = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(profile)));
    return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
  } catch { return null; }
}
