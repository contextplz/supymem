"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type ApiResult } from "@/lib/client";
import { installedModelsMessage, localProviders, providerModels, settingsFailureMessage, testProvider } from "../../setup/localProviders";
import type { LocalSection, SettingsCatalog } from "./sections";
import styles from "./local.module.css";

type SettingsView = { values?: Record<string, unknown>; secrets_set?: Record<string, unknown>; field_states?: Record<string, string>; data_folder?: unknown; state?: string; missing?: string[]; configured?: boolean; verified?: boolean; sample_selected?: boolean; import_error?: string };

const mailPresets = {
  gmail: { host: "smtp.gmail.com", port: "587", imap_host: "imap.gmail.com", imap_port: "993" },
  outlook: { host: "smtp-mail.outlook.com", port: "587", imap_host: "outlook.office365.com", imap_port: "993" },
};

export function SettingsSection({ section, onSaved }: { section: LocalSection; onSaved?: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [catalogModels, setCatalogModels] = useState<Record<string, string>>({});
  const [keys, setKeys] = useState<Record<string, boolean>>({});
  const [folder, setFolder] = useState("");
  const [state, setState] = useState("loading");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(section.state === "ready");
  const [missing, setMissing] = useState(section.missing ?? []);
  const [dirty, setDirty] = useState(false);
  const [sources, setSources] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState("");
  const [incomingTest, setIncomingTest] = useState(false);
  const [preset, setPreset] = useState("other");
  const [providerPreset, setProviderPreset] = useState("");
  const [providerSaved, setProviderSaved] = useState(false);
  const [providerVerified, setProviderVerified] = useState(false);
  const [testingProvider, setTestingProvider] = useState(false);
  const probe = useRef<AbortController | null>(null);
  const saving = useRef(false);
  const active = useRef(true);
  const selected = section.id === "providers" ? localProviders.find(provider => provider.id === providerPreset) : undefined;
  const fields = section.fields.filter(field => (!field.providers || field.providers.includes(values.provider || "builtin"))
    && !(section.id === "providers" && field.name === "base_url" && selected && !selected.addressRequired));
  const helpUrl = section.provider_urls?.[section.id === "smtp" ? preset : selected?.id || values.provider] || section.url;
  const missingLabels = missing.map(name => section.fields.find(field => field.name === name)?.label || name);

  function accept(result: ApiResult, updated = false) {
    if (result.status === 404 || result.status === 405 || result.status === 501) {
      setState("missing");
      setNote("This setting is not available in your local app yet. Update your local app and try again.");
      return false;
    }
    if (!result.ok || !result.data || typeof result.data !== "object") {
      const detail = (result.data as { detail?: { code?: string; fields?: unknown[]; message?: unknown } } | undefined)?.detail;
      const labels = detail?.code === "invalid_local_settings" && Array.isArray(detail.fields)
        ? section.fields.filter(field => detail.fields?.includes(field.name)).map(field => field.label) : [];
      setNote(section.id === "data" && detail?.code === "invalid_backup_folder" && typeof detail.message === "string"
        ? detail.message : labels.length ? `Check your ${labels.join(", ")} below under ${section.title}, then save again.`
        : settingsFailureMessage(result) + ` Check your fields below under ${section.title}.`);
      setState(current => current === "ready" ? current : "error");
      return false;
    }
    const view = result.data as SettingsView;
    if (!view.values || typeof view.values !== "object" || Array.isArray(view.values)
      || !view.secrets_set || typeof view.secrets_set !== "object" || Array.isArray(view.secrets_set)) {
      setState("error");
      setNote("Your saved settings are not available in this version. Update your local app and try again.");
      return false;
    }
    const next: Record<string, string> = {};
    const configured: Record<string, boolean> = {};
    for (const field of section.fields) {
      const value = view.values?.[field.name];
      next[field.name] = field.secret ? "" : field.name === "auth_configs" && value && typeof value === "object" && !Array.isArray(value)
        ? JSON.stringify(value, null, 2) : typeof value === "string" || typeof value === "number" ? String(value) : "";
      if (field.secret) configured[field.name] = view.secrets_set?.[field.name] === true;
    }
    setValues(next);
    setKeys(configured);
    if (section.id === "providers") {
      const savedPreset = localProviders.find(provider => provider.provider === next.provider && provider.baseUrl === next.base_url)
        ?? localProviders.find(provider => provider.id === next.provider);
      setProviderPreset(savedPreset?.id ?? "");
      setProviderSaved(view.configured === true && view.sample_selected !== true);
      setProviderVerified(view.verified === true);
    }
    setFolder(typeof view.data_folder === "string" ? view.data_folder : "");
    setReady(view.state ? view.state === "ready" : typeof view.configured === "boolean" ? view.configured : section.state === "ready");
    setMissing(Array.isArray(view.missing) ? view.missing : section.missing ?? []);
    if (typeof view.import_error === "string") setNote(view.import_error);
    setDirty(false);
    setSources(view.field_states ?? Object.fromEntries(section.fields.map(field => [field.name, !updated && field.state === "from .env" ? "from .env"
      : (field.secret ? configured[field.name] : next[field.name] && next[field.name] !== "{}") ? "set" : "not set"])));
    setState("ready");
    if (section.id === "smtp") setPreset(Object.entries(mailPresets).find(([, preset]) =>
      Object.entries(preset).every(([name, value]) => next[name] === value))?.[0] ?? "other");
    return true;
  }

  async function load() {
    setNote("");
    try {
      const result = await api(section.path);
      if (active.current) {
        if (accept(result) && section.id === "providers") setCatalogModels(providerModels(result.data));
      }
    } catch {
      if (active.current) {
        setState("error");
        setNote(`Restart your local app, then choose Try again under ${section.title} in Keys and connections.`);
      }
    }
  }

  useEffect(() => {
    active.current = true;
    void load();
    return () => { active.current = false; probe.current?.abort(); };
  }, [section.path]);

  useEffect(() => {
    setReady(section.state === "ready");
    setMissing(section.missing ?? []);
    setSources(Object.fromEntries(section.fields.map(field => [field.name, field.state])));
  }, [section]);

  function providerAddressReady() {
    if (!selected?.addressRequired || values.base_url?.trim()) return true;
    setNote("Enter your service address, then save or test your connection again.");
    return false;
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving.current) return;
    if (!providerAddressReady()) return;
    saving.current = true;
    setBusy(true);
    setSaved(false);
    setNote("");
    setTestResult("");
    const body: Record<string, unknown> = {};
    for (const field of fields) {
      const value = values[field.name] || "";
      if (field.secret && !value) continue;
      if ((field.options || field.type === "number") && !value) {
        if (["monthly_spend_cap", "context_window"].includes(field.name)) body[field.name] = null;
        continue;
      }
      if (field.name === "auth_configs") {
        try {
          const configs = JSON.parse(value || "{}");
          if (!configs || typeof configs !== "object" || Array.isArray(configs)) throw new Error();
          body[field.name] = configs;
        } catch {
          setNote("Enter your App authorization IDs as a JSON object in Keys and connections, then save again.");
          saving.current = false;
          setBusy(false);
          return;
        }
      } else body[field.name] = field.name === "catalog_model" ? value.trim() || null : value;
    }
    if (selected && !selected.addressRequired) body.base_url = values.base_url || selected.baseUrl;
    setValues(current => Object.fromEntries(Object.entries(current).map(([name, value]) => [name, section.fields.some(field => field.name === name && field.secret) ? "" : value])));
    try {
      const result = await api(section.path, { method: "PUT", body: JSON.stringify(body) });
      if (active.current) {
        if (accept(result, true)) {
          setSaved(true);
          if (section.id === "providers" && typeof window !== "undefined") window.dispatchEvent?.(new Event("supymem-local-settings-changed"));
          setNote("Saved. You can update these settings below.");
          onSaved?.();
          if (section.id === "providers" && !onSaved) {
            try {
              const refreshed = await api("/api/local/settings/catalog");
              if (!active.current) return;
              const catalog = refreshed.data as SettingsCatalog | undefined;
              const connection = refreshed.ok && Array.isArray(catalog?.sections) ? catalog.sections.find(item => item.id === section.id) : undefined;
              if (!connection) throw new Error();
              setReady(connection.state === "ready");
              setMissing(connection.missing ?? []);
              setSources(Object.fromEntries(connection.fields.map(field => [field.name, field.state])));
            } catch {
              if (active.current) setNote("Saved. Your connection status could not be refreshed. Save again to check your readiness.");
            }
          }
        }
      }
    } catch {
      if (active.current) setNote("Your settings could not be saved. Check your local app, enter your replacement keys again if needed, then try again.");
    } finally {
      saving.current = false;
      if (active.current) setBusy(false);
    }
  }

  async function testConnection(incoming = false) {
    if (saving.current) return;
    setIncomingTest(incoming);
    setTestResult("");
    if (section.id !== "providers" && dirty) {
      setTestResult("Save your changes below, then choose Test your connection again.");
      return;
    }
    if (!providerAddressReady()) return;
    saving.current = true;
    setBusy(true);
    setNote("");
    const controller = section.id === "providers" ? new AbortController() : null;
    if (controller) {
      probe.current = controller;
      setTestingProvider(true);
      if (!dirty) setProviderVerified(false);
    }
    try {
      if (controller) {
        const result = await testProvider({ provider: values.provider || "", model: values.model || "",
          base_url: values.base_url || "", api_key: values.api_key || undefined,
          ...(values.provider === "azure_openai" && "catalog_model" in values ? { catalog_model: values.catalog_model.trim() || null } : {}),
          ...(values.provider === "openai_compatible" && "context_window" in values ? { context_window: values.context_window ? Number(values.context_window) : null } : {}) }, controller.signal);
        if (active.current && !controller.signal.aborted) {
          if (!dirty) setProviderVerified(result.verified);
          setTestResult((dirty ? "Your connection is working. Save your settings to use it in your workspace."
            : "Your connection is working.") + installedModelsMessage(result.models));
        }
      } else {
        const result = await api(incoming ? "/api/local/settings/imap/test" : `${section.path}/test`, { method: "POST" });
        const data = result.data as { ok?: boolean; next_step?: string } | undefined;
        if (active.current) setTestResult(result.ok && typeof data?.next_step === "string" ? data.next_step
          : `Check your fields below under ${section.title}, save, then test again.`);
      }
    } catch (failure) {
      if (active.current && !controller?.signal.aborted) setTestResult(section.id === "providers" && failure instanceof Error ? failure.message
        : `Check your fields below under ${section.title}, save, then test again.`);
    } finally {
      if (!controller || probe.current === controller) {
        probe.current = null;
        saving.current = false;
        if (active.current) { setBusy(false); setTestingProvider(false); }
      }
    }
  }

  function cancelProviderTest() {
    probe.current?.abort();
    probe.current = null;
    saving.current = false;
    setBusy(false);
    setTestingProvider(false);
    setTestResult("Test canceled. Choose Test your connection to try again.");
  }

  return (
    <section className={styles.section} aria-labelledby={`${section.id}-title`}>
      <h2 id={`${section.id}-title`}>{section.title}</h2>
      <p>{section.description}</p>
      <p role="status">{section.id === "providers" && dirty ? "Unsaved changes. Save your settings, then test your connection."
        : (section.id === "providers" ? providerVerified : ready) ? "Ready"
        : section.id === "providers" && providerSaved ? "You can use your saved provider. To check it, choose Test your connection below."
        : `Needs setup: add ${missingLabels.length ? missingLabels.join(", ") : "your connection details"} below.`}</p>
      {section.unlocks?.length > 0 && <p>Unlocks: {section.unlocks.join(", ")}</p>}
      {helpUrl && <a href={helpUrl} target="_blank" rel="noopener noreferrer">How to get this</a>}
      <p>{section.data_sent}</p>
      {note && <p role="status">{note}</p>}
      {state === "loading" && <p role="status">Loading your saved settings. Please wait.</p>}
      {(state === "error" || state === "missing") && <button type="button" onClick={() => void load()}>Try again</button>}
      {state === "ready" && (
        <form onSubmit={save}>
          {section.id === "search" && <p>{["set", "from .env"].includes(sources.firecrawl_url)
            ? "Your saved Firecrawl reader is on. To use the built-in reader, clear the Firecrawl URL and save."
            : "Your built-in page reader is on. To use your Firecrawl server, enter its URL and save."}</p>}
          {folder && <p>Your data folder: <code>{folder}</code></p>}
          <fieldset disabled={busy} className={styles.fields}>
            {section.id === "smtp" && <div className={styles.field}>
              <label htmlFor="smtp-preset">Your mail service</label>
              <select id="smtp-preset" value={preset} onChange={event => {
                const choice = event.target.value;
                setPreset(choice);
                const preset = mailPresets[choice as keyof typeof mailPresets];
                if (preset) { setValues(current => ({ ...current, ...preset })); setDirty(true); setSaved(false); setTestResult(""); }
              }}>
                <option value="gmail">Gmail</option><option value="outlook">Outlook</option><option value="other">Other</option>
              </select>
              <span>Choose your service to fill SMTP and IMAP hosts and ports. Choose Other to enter your own.</span>
            </div>}
            {fields.map(field => {
              const id = `${section.id}-${field.name}`;
              const fieldHelpUrl = section.id === "images" ? field.url : helpUrl;
              return (
                <div key={field.name} className={styles.field}>
                  <label htmlFor={id}>{field.label}</label>
                  <span id={`${id}-help`}>{field.purpose} {section.id === "smtp" && field.name === "postal_address" && "Cold email laws require your valid postal address in every email. Use your business mailing address or registered PO box. "}{(field.providers ? !["firecrawl_key", "session_token", "catalog_model", "context_window"].includes(field.name) : field.required) ? "Required for this connection." : "Optional."}</span>
                  {section.id === "providers" && field.name === "provider" ? (
                    <select id={id} value={selected?.id || values.provider || ""} onChange={event => {
                      const provider = localProviders.find(item => item.id === event.target.value);
                      setProviderPreset(provider?.id ?? "");
                      setValues(current => ({ ...current, provider: provider?.provider || event.target.value,
                        model: catalogModels[provider?.id || ""] || "", base_url: provider?.baseUrl ?? current.base_url, api_key: "",
                        ...("catalog_model" in current ? { catalog_model: "" } : {}) }));
                      setKeys({ api_key: false });
                      setSources({});
                      setDirty(true);
                      setSaved(false); setTestResult("");
                    }}>
                      <option value="">Choose your AI service</option>
                      {localProviders.map(provider => <option key={provider.id} value={provider.id}>{provider.label}</option>)}
                    </select>
                  ) : field.options ? (
                    <select id={id} aria-describedby={`${id}-help`} value={values[field.name] || ""} onChange={event => { setValues(current => ({ ...current, [field.name]: event.target.value })); setDirty(true); setSaved(false); setTestResult(""); }}>
                      <option value="">Choose your service</option>
                      {field.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  ) : field.type === "textarea" ? (
                    <textarea id={id} value={values[field.name] || ""} rows={3} aria-describedby={`${id}-help`} maxLength={field.name === "auth_configs" ? 200_000 : 2000}
                      onChange={event => { setValues(current => ({ ...current, [field.name]: event.target.value })); setDirty(true); setSaved(false); setTestResult(""); }} />
                  ) : (
                    <input id={id} type={field.secret ? "password" : field.type || "text"} value={values[field.name] || ""}
                      onChange={event => { setValues(current => ({ ...current, [field.name]: event.target.value })); setDirty(true); setSaved(false); setTestResult(""); }}
                      autoComplete={field.secret ? "new-password" : "off"} maxLength={field.secret ? 8192 : 2000}
                      min={field.min} max={field.max} step={field.step} required={field.name === "base_url" && selected?.addressRequired} aria-describedby={`${id}-help${field.secret ? ` ${id}-status` : ""}`} />
                  )}
                  {field.secret && <span id={`${id}-status`}>{keys[field.name] ? sources[field.name] === "from .env" ? "Set from your .env file" : "Saved in the app" : "Not set"}. Enter your replacement to change it. {field.name === "api_key" && selected?.keyHelp}</span>}
                  {!field.secret && <span>{sources[field.name] === "from .env" ? "Set from your .env file" : sources[field.name] === "set" ? "Saved in the app" : "Not set"}</span>}
                  {field.secret && !["secret_access_key", "session_token"].includes(field.name) && fieldHelpUrl && <a href={fieldHelpUrl} target="_blank" rel="noopener noreferrer">{field.help_text || section.help_text}</a>}
                </div>
              );
            })}
          </fieldset>
          <div className={styles.actions}>
            <button type="submit" aria-label={`Save your settings: ${section.title}`} disabled={busy}>{busy ? "Please wait..." : saved ? "Saved" : "Save your settings"}</button>
            {!["spend", "data", "images"].includes(section.id) && <button type="button" disabled={busy} onClick={() => testConnection()}>Test your connection</button>}
            {testingProvider && <button type="button" onClick={cancelProviderTest}>Cancel test</button>}
            {testResult && !incomingTest && <span role="status">{testResult}</span>}
            {section.id === "smtp" && <button type="button" disabled={busy} onClick={() => testConnection(true)}>Test incoming mail</button>}
            {testResult && incomingTest && <span role="status">{testResult}</span>}
          </div>
        </form>
      )}
    </section>
  );
}
