import { isLocalConsole } from "./localMode.ts";

const ENV_KEY = /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/;

const BY_CODE: Record<string, string> = {
  unipile_missing: "LinkedIn outreach is not set up on Supymem yet.",
  hunter_missing: "Email enrichment is not set up on Supymem yet.",
  google_ads_env: "Google Ads is not set up on Supymem yet.",
  meta_ads_env: "Meta Ads is not set up on Supymem yet.",
  composio_missing: "Supymem’s shared connection service is not set up yet.",
};

const BY_PROVIDER: Record<string, string> = {
  unipile: "LinkedIn outreach is not set up on Supymem yet.",
  hunter: "Email enrichment is not set up on Supymem yet.",
  google_ads: "Google Ads is not set up on Supymem yet.",
  meta_ads: "Meta Ads is not set up on Supymem yet.",
  composio: "Supymem’s shared connection service is not set up yet.",
};

/** Map API blocker/card copy to founder-safe language; never surface server env key names. */
export function founderConnectionMessage(input: {
  code?: string | null;
  message?: string | null;
  note?: string | null;
  provider?: string | null;
}): string {
  if (isLocalConsole()) {
    const message = founderSafeConnectionText(input.message) || founderSafeConnectionText(input.note);
    return message
      ? message.replace(/\s*\u2014\s*/g, ", ")
      : "Open Settings > Keys and connections > Your app connections, check your Composio key and the app's authorization configuration ID, then reconnect your account in Connections.";
  }

  const code = (input.code || "").trim();
  if (code && BY_CODE[code]) return BY_CODE[code];
  const provider = String(input.provider || "").toLowerCase();
  if (provider && BY_PROVIDER[provider]) {
    const raw = `${input.message || ""} ${input.note || ""}`;
    if (!raw.trim() || ENV_KEY.test(raw)) return BY_PROVIDER[provider];
  }
  for (const text of [input.message, input.note]) {
    const value = (text || "").trim();
    if (!value) continue;
    if (ENV_KEY.test(value)) {
      return provider && BY_PROVIDER[provider]
        ? BY_PROVIDER[provider]
        : "This connection is not set up on Supymem yet.";
    }
    return value;
  }
  return provider && BY_PROVIDER[provider]
    ? BY_PROVIDER[provider]
    : "This connection is not set up on Supymem yet.";
}

export function founderSafeConnectionText(text: string | null | undefined): string | null {
  const value = (text || "").trim();
  if (!value) return null;
  if (ENV_KEY.test(value)) return null;
  return value;
}
