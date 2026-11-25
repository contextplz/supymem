import { api, type ApiResult } from "@/lib/client";
import { waitForChatRequest } from "@/lib/chatStop";

export type LocalProvider = {
  provider: string;
  api_key?: string;
  model: string;
  base_url?: string;
  catalog_model?: string | null;
  context_window?: number | null;
};

export const localProviders = [
  { id: "openai", provider: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1", keyRequired: true, addressRequired: false, keyHelp: "You can create a key in your OpenAI account under API keys." },
  { id: "azure_openai", provider: "azure_openai", label: "Azure OpenAI", baseUrl: "", keyRequired: true, addressRequired: true, keyHelp: "In Azure, open your resource's Keys and Endpoint for your key and resource endpoint. Put your deployment name in Chat model." },
  { id: "anthropic", provider: "anthropic", label: "Anthropic", baseUrl: "https://api.anthropic.com/v1", keyRequired: true, addressRequired: false, keyHelp: "You can create a key in your Anthropic account under API keys. You can change the model before saving." },
  { id: "openrouter", provider: "openai_compatible", label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1", keyRequired: true, addressRequired: false, keyHelp: "You can create a key in your OpenRouter account under Keys." },
  { id: "ollama", provider: "openai_compatible", label: "Ollama (on your machine)", baseUrl: "http://localhost:11434/v1", keyRequired: false, addressRequired: true, keyHelp: "Open Ollama and download a model, then enter its name below. Leave the key blank only if your server does not require one." },
  { id: "lmstudio", provider: "openai_compatible", label: "LM Studio (on your machine)", baseUrl: "http://localhost:1234/v1", keyRequired: false, addressRequired: true, keyHelp: "Load a model and start your server in LM Studio. Leave the key blank only if your server does not require one." },
  { id: "openai_compatible", provider: "openai_compatible", label: "OpenAI-compatible service", baseUrl: "", keyRequired: false, addressRequired: true, keyHelp: "Enter your service address and model. Add a key below if your service requires one." },
] as const;

export const providerInstructions = "Your app needs an update to save your AI settings. Update your app, then try again.";

export function providerModelOptions(value: unknown): Record<string, string[]> {
  const catalog = value as { providers?: Record<string, string[] | { models?: { chat?: string } }>; default_models?: Record<string, string[]> } | null;
  const names = new Set([...Object.keys(catalog?.default_models ?? {}), ...Object.keys(catalog?.providers ?? {})]);
  return Object.fromEntries([...names].flatMap(name => {
    const entry = catalog?.providers?.[name];
    const defaults = catalog?.default_models?.[name];
    const models = (Array.isArray(entry) ? entry : [entry?.models?.chat])
      .concat(Array.isArray(defaults) ? defaults : [])
      .filter((model): model is string => typeof model === "string" && model.trim() !== "");
    return models.length ? [[name, [...new Set(models)]] as const] : [];
  }));
}

export function providerModels(value: unknown): Record<string, string> {
  const models = providerModelOptions(value);
  return Object.fromEntries(localProviders.map(option => {
    const key = option.id === "ollama" || option.id === "lmstudio" ? "local" : option.provider;
    return [option.id, models[option.id]?.[0] || models[key]?.[0] || ""];
  }));
}

const connectionErrors: Record<string, string> = {
  bad_api_key: "Your service rejected your key. In Keys and connections > Your AI provider, enter a key that matches your service or model server, then test again.",
  rate_limited: "Your service is busy. Wait a moment, then try again.",
  out_of_credits: "Your service account has reached its limit. Add funds or raise your limit there, then try again.",
  provider_down: "Your service could not be reached. Check your service address and that it is running, then try again.",
  provider_error: "In Keys and connections > Your AI provider, check Service URL and Chat model, then test again.",
  service_url_required: "In Keys and connections > Your AI provider, enter your Service URL, then test again.",
  chat_model_required: "In Keys and connections > Your AI provider, enter your Chat model, then test again.",
  provider_key_required: "In Keys and connections > Your AI provider, enter your Model API key for this service, then test again.",
  sample_mode: "You are using sample answers. Save your AI service in Settings, then test your connection.",
  model_not_found: "Load the model in your model server, or choose an installed Chat model in Keys and connections > Your AI provider, then test again.",
  model_json_unsupported: "In Keys and connections > Your AI provider, choose a Chat model that supports JSON replies, then test again.",
  local_spend_limit: "In Keys and connections > Your monthly spend cap, raise or remove Monthly limit (USD), then test again.",
  usage_unconfirmed: "Your service did not report usage. In Keys and connections > Your AI provider, choose a Chat model that reports usage, then test again.",
  provider_timeout: "Start your model server and load your model, then test again in Keys and connections > Your AI provider.",
};

function modelIds(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string" && /^[^\s\p{C}]{1,256}$/u.test(id)).slice(0, 50) : [];
}

export function installedModelsMessage(models: string[] = []): string {
  return models.length ? ` Installed models: ${models.join(", ")}.` : "";
}

export function settingsFailureMessage(response: ApiResult): string {
  const data = response.data as { detail?: string | { code?: string; models?: unknown } } | null;
  const detail = data?.detail;
  if (typeof detail === "object" && detail?.code && Object.hasOwn(connectionErrors, detail.code)) {
    return connectionErrors[detail.code] + installedModelsMessage(modelIds(detail.models));
  }
  if (typeof detail === "string") {
    if (detail.startsWith("In Keys and connections, paste your Azure resource endpoint")) return "In Keys and connections > Your AI provider, paste your Azure resource endpoint into Service URL and put your deployment name in Chat model, then try again.";
    if (response.status === 504) return detail.includes("Start your model server") ? connectionErrors.provider_timeout
      : "In Keys and connections > Your AI provider, check Service URL, then test again.";
    if (detail.startsWith("Enter a context window from ")) return detail;
    if (/keychain/i.test(detail)) return "Your saved keys could not be opened. Unlock your system keychain, then restart your app and try again.";
    if (/permissions|data (?:folder|directory)|backup|saved keys/i.test(detail)) return "Your saved settings need attention. Check your data folder permissions or restore your settings from a backup, then try again.";
    if (/provider URL|service address|HTTPS|loopback/i.test(detail)) return "Your service address needs attention. Enter an HTTPS address, or an HTTP address on this machine, without passwords or extra URL parameters, then try again.";
    if (/model/i.test(detail)) return "Your service details need attention. Check your provider, chat model and key, then try again.";
  }
  return "Your settings could not be saved or checked. Check your entries and that your local app is open, then try again.";
}

async function providerRequest(path = "/api/local/providers", options: RequestInit = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), path === "/api/local/providers/test" ? 620_000 : 15_000);
  let response: ApiResult;
  try {
    const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
    const init = { ...options, cache: "no-store" as const, signal };
    if (typeof window !== "undefined" && !["http:", "https:"].includes(window.location.protocol)) {
      response = await waitForChatRequest(api(path, init), signal);
    } else {
      const reply = await waitForChatRequest(fetch(path, { ...init, credentials: "same-origin", redirect: "error" }), signal);
      response = { ok: reply.ok, status: reply.status, data: await waitForChatRequest(reply.json(), signal).catch(() => null) };
    }
  } catch {
    throw new Error("Your AI settings could not be reached. Check that your app is open, then try again.");
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 404 || response.status === 405) throw new Error(providerInstructions);
  if (!response.ok) {
    const detail = (response.data as { detail?: { code?: string } } | null)?.detail;
    if (path !== "/api/local/providers" || options.method || response.status !== 503 || detail?.code !== "local_credentials_pending") {
      throw new Error(settingsFailureMessage(response));
    }
  }
  return response;
}

export async function providerConfigured(onSampleMode?: (enabled: boolean) => void): Promise<boolean | "pending"> {
  const response = await providerRequest();
  if (!response.ok) return "pending";
  const data = response.data;
  if (!data || typeof data !== "object" || !("configured" in data) || typeof data.configured !== "boolean") {
    throw new Error(providerInstructions);
  }
  onSampleMode?.("sample_selected" in data && data.sample_selected === true);
  return data.configured;
}

export async function saveProvider(provider: LocalProvider): Promise<void> {
  await providerRequest("/api/local/providers", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(provider),
  });
}

export async function testProvider(provider: LocalProvider, signal?: AbortSignal): Promise<{ verified: boolean; models: string[] }> {
  const response = await providerRequest("/api/local/providers/test", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(provider), signal,
  });
  if (!response.data || typeof response.data !== "object" || !("ok" in response.data) || response.data.ok !== true) {
    throw new Error("Your connection test did not finish. Check your service details, then try again.");
  }
  return { verified: "verified" in response.data && response.data.verified === true,
    models: modelIds("models" in response.data ? response.data.models : undefined) };
}

export async function trySampleMode(): Promise<void> {
  await providerRequest("/api/local/providers/sample", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
}
