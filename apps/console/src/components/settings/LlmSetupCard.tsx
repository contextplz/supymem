"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/client";
import type { TokenUsage } from "@/lib/api-types";
import { CredentialField } from "@/components/secrets/CredentialField";
import { providerModelOptions } from "@/app/setup/localProviders";

const LLM_SLOTS: Record<string, { slotId: string; envKey: string }> = {
  openai: { slotId: "openai.api_key", envKey: "OPENAI_API_KEY" },
  anthropic: { slotId: "anthropic.api_key", envKey: "ANTHROPIC_API_KEY" },
  google: { slotId: "google.api_key", envKey: "GOOGLE_API_KEY" },
  deepseek_direct: { slotId: "deepseek.api_key", envKey: "DEEPSEEK_API_KEY" },
  xai: { slotId: "xai.api_key", envKey: "XAI_API_KEY" },
  openrouter: { slotId: "openrouter.api_key", envKey: "OPENROUTER_API_KEY" },
  azure_foundry: { slotId: "azure.api_key", envKey: "AZURE_API_KEY" },
};

const PROVIDERS = [
  { value: "deepseek_direct", label: "DeepSeek" },
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "google", label: "Google" },
  { value: "xai", label: "xAI" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "local", label: "Local (Ollama)" },
  { value: "azure_foundry", label: "Azure AI Foundry" },
  { value: "openai_compatible", label: "OpenAI-compatible" },
];

export type LlmSettingsView = {
  provider: string;
  model: string;
  base_url: string | null;
  source: "founder_override" | "platform_default";
  api_key_set: boolean;
  models?: string[];
  badge?: string;
  offer?: { model?: string; label?: string; disclosure?: string };
  usage?: TokenUsage;
  hosted?: boolean;
};

export function LlmSetupCard({
  workspaceId,
  intent = "ours",
  compact = false,
}: {
  workspaceId?: string | null;
  intent?: "ours" | "byok" | "local";
  compact?: boolean;
}) {
  const [llm, setLlm] = useState<LlmSettingsView | null>(null);
  const [catalog, setCatalog] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [open, setOpen] = useState(intent === "byok" || intent === "local");

  useEffect(() => {
    if (!workspaceId) return;
    (async () => {
      const settings = await api(`/api/workspaces/${workspaceId}/llm-settings`);
      if (settings.ok) setLlm(settings.data as LlmSettingsView);
      const cat = await api(`/api/workspaces/${workspaceId}/llm-catalog`);
      if (cat.ok) setCatalog(providerModelOptions(cat.data));
    })();
  }, [workspaceId]);

  useEffect(() => {
    if (intent !== "local") return;
    setLlm((current) => {
      if (!current || current.provider === "local") return current;
      const models = catalog.local || ["llama3.2"];
      return { ...current, provider: "local", model: models[0] || "llama3.2", models };
    });
  }, [intent, catalog]);

  if (!workspaceId) {
    return (
      <p className="muted" style={{ marginTop: 8 }}>
        Workspace is still spinning up, you can add a key in a moment.
      </p>
    );
  }

  const offer = llm?.offer;
  const models = catalog[llm?.provider || ""] || llm?.models || [];
  const usingOurs = llm?.source !== "founder_override";
  const slot = LLM_SLOTS[llm?.provider || ""];

  async function save() {
    if (!workspaceId || !llm) return;
    setSaving(true);
    const payload: Record<string, string | null> = {
      provider: llm.provider,
      model: llm.model,
      base_url: llm.base_url || null,
    };
    const r = await api(`/api/workspaces/${workspaceId}/llm-settings`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    setSaving(false);
    if (r.ok) {
      setLlm(r.data as LlmSettingsView);
      setMsg("Saved provider and model. Keys go through the secure form only.");
    } else {
      setMsg("Couldn’t save that key. Check the provider and try again.");
    }
  }

  async function useOurs() {
    if (!workspaceId) return;
    const r = await api(`/api/workspaces/${workspaceId}/llm-settings`, { method: "DELETE" });
    if (r.ok) {
      setLlm(r.data as LlmSettingsView);
      setMsg("Back on DeepSeek v4 Flash (ours).");
    }
  }

  const showByokForm = !usingOurs || open || intent === "byok" || intent === "local";

  return (
    <section className="panel-card" aria-label="Model and API key">
      <h3 style={{ margin: 0 }}>{compact ? "How this company thinks" : "Model"}</h3>
      <p className="muted" style={{ marginTop: 8 }}>
        {usingOurs
          ? "Your model connection is hosted. Its key never appears here. Choose a model to continue."
          : offer?.disclosure || "Bring your own key to switch provider."}
      </p>
      <p style={{ marginTop: 8 }}>
        {llm?.badge || (usingOurs ? "Using ours: DeepSeek v4 Flash" : "Using your key")}
      </p>
      {usingOurs && !showByokForm ? (
        <button type="button" className="ghost sm" style={{ marginTop: 8 }} onClick={() => setOpen(true)}>
          Use my own key instead
        </button>
      ) : null}
      {showByokForm ? (
        <>
          <label htmlFor="onboard-llm-provider" style={{ display: "block", marginTop: 12 }}>
            Provider
          </label>
          <select
            id="onboard-llm-provider"
            value={llm?.provider || "deepseek_direct"}
            onChange={(e) => {
              const provider = e.target.value;
              const next = catalog[provider] || [];
              setLlm({
                ...(llm || {
                  provider,
                  model: next[0] || "",
                  base_url: null,
                  source: "platform_default",
                  api_key_set: false,
                }),
                provider,
                model: next[0] || llm?.model || "",
                models: next,
              });
            }}
            style={{ marginTop: 6, width: "100%" }}
          >
            {PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          <label htmlFor="onboard-llm-model" style={{ display: "block", marginTop: 12 }}>
            Model
          </label>
          {models.length > 0 ? (
            <select
              id="onboard-llm-model"
              value={llm?.model || models[0]}
              onChange={(e) => llm && setLlm({ ...llm, model: e.target.value })}
              style={{ marginTop: 6, width: "100%" }}
            >
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          ) : (
            <input
              id="onboard-llm-model"
              value={llm?.model || ""}
              onChange={(e) => llm && setLlm({ ...llm, model: e.target.value })}
              style={{ marginTop: 6, width: "100%" }}
            />
          )}
          {slot ? (
            <CredentialField
              slotId={slot.slotId}
              fields={[
                {
                  env_key: slot.envKey,
                  label: "API key",
                  input: "password",
                },
              ]}
              workspaceId={workspaceId}
            />
          ) : (
            <p className="muted" style={{ marginTop: 12 }}>
              This provider does not need a pasted key here.
            </p>
          )}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
            <button type="button" className="primary" data-usage-control="llm_settings_save" onClick={() => void save()} disabled={saving}>
              {saving ? "Saving…" : "Save key"}
            </button>
            <button type="button" data-usage-control="llm_settings_use_ours" onClick={() => void useOurs()}>
              Back to Supymem hosted
            </button>
          </div>
        </>
      ) : null}
      {msg && <p className="muted" style={{ marginTop: 8 }}>{msg}</p>}
    </section>
  );
}
