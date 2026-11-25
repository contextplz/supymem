"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { CharlieMascot } from "@/components/chat/CharlieDock";
import { CharlieIntroWelcome } from "@/components/desktop/CharlieIntro";
import { installedModelsMessage, localProviders, providerConfigured, providerInstructions, providerModels, saveProvider, testProvider, trySampleMode, type LocalProvider } from "./localProviders";
import { LocalSettings } from "../settings/local/LocalSettings";
import { waitForChatRequest } from "@/lib/chatStop";
import { api, apiLive } from "@/lib/client";
import type { LocalSection, SettingsCatalog } from "../settings/local/sections";

const VENTURES_READ_ERROR = "Your saved ventures could not be checked yet. Wait a moment, then choose Retry.";

export function LocalProviderGate({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const setupPage = pathname.startsWith("/setup");
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [recovery, setRecovery] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [sample, setSample] = useState(false);
  const finishedSetup = useRef(false);
  const startingSample = useRef(false);
  const [introduced, setIntroduced] = useState(false);
  const workspaceRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const {signal} = controller;
    setConfigured(null);
    setError("");
    setRecovery(false);
    let settingsFailure: unknown;
    const readProvider = () => waitForChatRequest(providerConfigured(enabled => { if (!signal.aborted) setSample(enabled); }), signal);
    const checkProvider = async () => {
      const initial = await readProvider().catch(failure => {
        settingsFailure = failure;
        return false;
      });
      if (initial !== "pending") return initial;
      for (const delay of [250, 500, 1000, 2000, 4000]) {
        await waitForChatRequest(new Promise(resolve => setTimeout(resolve, delay)), signal);
        const current = await readProvider();
        if (current !== "pending") return current;
      }
      throw new Error("Your saved keys are still opening. Unlock your system keychain, then choose Retry.");
    };
    void checkProvider().then(async value => {
      if (signal.aborted) return;
      if (!value) {
        const path = "/api/ventures?summary=true";
        let result = await api(path, {signal});
        if (signal.aborted) return;
        const warming = [0, 502, 503, 504].includes(result.status);
        if (warming) result = await apiLive(path, signal);
        if (signal.aborted) return;
        if (!result.ok) throw new Error(VENTURES_READ_ERROR);
        if (warming && settingsFailure) {
          settingsFailure = undefined;
          value = await checkProvider();
          if (signal.aborted) return;
        }
        const data = result.data as { items?: unknown[] } | unknown[] | null;
        const ventures = Array.isArray(data) ? data : data?.items;
        if (!value && Array.isArray(ventures) && ventures.length) {
          setRecovery(true);
          value = true;
        } else if (settingsFailure) throw settingsFailure;
      }
      setConfigured(value);
    }).catch(failure => {
      if (!signal.aborted) setError(failure.message);
    });
    return () => controller.abort();
  }, [attempt, setupPage]);
  useEffect(() => {
    if (!configured || !finishedSetup.current) return;
    finishedSetup.current = false;
    const target = workspaceRef.current?.querySelector<HTMLElement>("h1, main, [role='main']");
    if (target) { target.tabIndex = -1; target.focus(); }
  }, [configured]);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const refresh = () => setAttempt(current => current + 1);
    window.addEventListener?.("supymem-local-settings-changed", refresh);
    return () => window.removeEventListener?.("supymem-local-settings-changed", refresh);
  }, []);
  if (pathname === "/settings/local" || pathname.startsWith("/settings/local/")) return children;
  if (error) return <main className="page-loading"><ErrorNotice error={error} source="local_setup" />{error !== VENTURES_READ_ERROR && <p><a href="/settings/local">Open Settings &gt; Keys and connections &gt; Your AI provider</a> to check your connection.</p>}<button onClick={() => setAttempt(attempt + 1)}>Retry</button></main>;
  if (configured === null) return <main className="page-loading" role="status" data-webview-pending="local_provider">Starting your app…</main>;
  if (configured) return <div ref={workspaceRef} className={sample || recovery ? "local-sample-workspace" : undefined} style={{ display: sample || recovery ? "grid" : "contents" }}>{sample && <p role="status">You are viewing sample data and answers, not market evidence. No outside services are contacted. Open Settings &gt; Keys and connections &gt; Your AI provider to start a separate real venture.</p>}{recovery && <p role="status">Your saved ventures are still here. Open <a href="/settings/local">Settings &gt; Keys and connections</a> &gt; Your AI provider, check your saved model and service address, and re-enter your API key if it is missing.</p>}{children}</div>;
  if (!introduced) return <CharlieIntroWelcome onConnect={() => setIntroduced(true)} sampleEntry={<button type="button" onClick={async () => {
    if (startingSample.current) return;
    startingSample.current = true;
    try { await trySampleMode(); finishedSetup.current = true; setSample(true); setConfigured(true); }
    catch (failure) { setError(failure instanceof Error ? failure.message : providerInstructions); }
    finally { startingSample.current = false; }
  }}>Try without a key (sample mode)</button>} />;
  return <LocalSetup error={error} onConfigured={enabled => { finishedSetup.current = true; setSample(enabled); setConfigured(true); }} />;
}

export function SampleVentures({ refresh }: { refresh: string | null }) {
  const router = useRouter();
  const [ventures, setVentures] = useState<{ id: string; name: string }[]>([]);
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState("");
  useEffect(() => {
    let active = true;
    void api("/api/ventures?summary=true").then(result => {
      if (!result.ok) throw new Error();
      const data = result.data as { items?: { id: string; name: string; sample?: boolean }[] } | { id: string; name: string; sample?: boolean }[];
      const items = Array.isArray(data) ? data : data.items || [];
      if (active) { setVentures(items.filter(venture => venture.sample === true)); setError(""); }
    // A failed read must not put a banner over every screen of a real workspace: the list simply stays hidden.
    }).catch(() => { if (active) setVentures([]); });
    return () => { active = false; };
  }, [refresh]);
  async function remove(id: string) {
    setDeleting(id);
    setError("");
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(id)}/delete`, { method: "POST", body: JSON.stringify({ confirmation: "DELETE" }) });
      if (!result.ok) throw new Error();
      setVentures(current => current.filter(venture => venture.id !== id));
      if (refresh === `/v/${id}` || refresh?.startsWith(`/v/${id}/`)) router.push("/");
    } catch { setError("Your sample venture could not be deleted. Open your workspace, reload the app, then choose Delete sample venture again."); }
    finally { setDeleting(""); }
  }
  if (!ventures.length && !error) return null;
  return <section aria-label="Your sample ventures" style={{ padding: "12px 16px", background: "var(--bg-card)", borderBottom: "1px solid var(--line)" }}><p className="muted">Your sample ventures stay separate when you add a key. Their results are not real evidence.</p>
    <a href="/settings/local">Keys and connections</a>{" "}<a href="/settings/local/capabilities">See what your keys unlock</a>
    <ul style={{ listStyle: "none", padding: 0 }}>{ventures.map(venture => <li key={venture.id}><a href={`/v/${encodeURIComponent(venture.id)}`}>{venture.name}</a>{" "}<button className="ghost sm" type="button" disabled={!!deleting} onClick={() => remove(venture.id)} aria-label={`Delete sample venture ${venture.name}`}>{deleting === venture.id ? "Deleting your sample..." : "Delete sample venture"}</button></li>)}</ul>
    <ErrorNotice error={error} source="local_sample" /></section>;
}

export function LocalSetup({ error: initialError = "", onConfigured }: { error?: string; onConfigured: (sample: boolean) => void }) {
  const [provider, setProvider] = useState<string>(localProviders[0].id);
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [catalogModel, setCatalogModel] = useState("");
  const [catalogModels, setCatalogModels] = useState<Record<string, string>>({});
  const [savedKey, setSavedKey] = useState(false);
  const [baseUrl, setBaseUrl] = useState<string>(localProviders[0].baseUrl);
  const [error, setError] = useState(initialError);
  const [invalidFields, setInvalidFields] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [verified, setVerified] = useState(false);
  const [installedModels, setInstalledModels] = useState<string[]>([]);
  const probe = useRef<AbortController | null>(null);
  const [connected, setConnected] = useState(false);
  const [guide, setGuide] = useState<LocalSection | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.all([api("/api/local/settings/catalog"), api("/api/local/providers"), api("/api/workspaces/local/llm-catalog")]).then(([result, saved, models]) => {
      const catalog = result.data as SettingsCatalog | undefined;
      if (!active) return;
      if (!result.ok || !saved.ok || !models.ok || !Array.isArray(catalog?.sections)) {
        setError("Your connection guide could not be loaded. Restart your local app, then open Keys and connections and try setup again.");
        return;
      }
      setGuide(catalog.sections.find(section => section.id === "providers") ?? null);
      const settings = saved.data as { values?: Partial<LocalProvider>; secrets_set?: { api_key?: boolean } };
      const choices = providerModels(models.data);
      setCatalogModels(choices);
      const values = settings.values || {};
      const selected = localProviders.find(option => option.provider === values.provider && option.baseUrl === values.base_url)
        || localProviders.find(option => option.id === values.provider) || localProviders[0];
      setProvider(selected.id);
      setModel(values.model || choices[selected.id]);
      setCatalogModel(values.catalog_model || "");
      setBaseUrl(values.base_url || selected.baseUrl);
      setSavedKey(settings.secrets_set?.api_key === true);
    }).catch(() => {
      if (active) setError("Your connection guide could not be loaded. Restart your local app, then open Keys and connections and try setup again.");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; probe.current?.abort(); probe.current = null; };
  }, []);
  const selected = localProviders.find(option => option.id === provider) ?? localProviders[0];
  const busy = loading || saving || testing;

  function details() {
    if (!model.trim() || selected.keyRequired && !savedKey && !apiKey.trim() || !baseUrl.trim()) {
      setInvalidFields([!model.trim() && "model", selected.keyRequired && !savedKey && !apiKey.trim() && "api_key", !baseUrl.trim() && "base_url"].filter((field): field is string => !!field));
      throw new Error("Enter your model and the required service details, then try again.");
    }
    try {
      const address = new URL(baseUrl.trim());
      if (!["https:", "http:"].includes(address.protocol) || address.username || address.password || address.search || address.hash) throw new Error();
    } catch {
      setInvalidFields(["base_url"]);
      throw new Error("Enter an HTTP or HTTPS service address without a password or extra URL parameters, then try again.");
    }
    return { provider: selected.provider, ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}), model: model.trim(), base_url: baseUrl.trim(),
      ...(selected.id === "azure_openai" ? { catalog_model: catalogModel.trim() || null } : {}) };
  }

  function cancelTest() {
    probe.current?.abort();
    probe.current = null;
    setTesting(false);
  }

  async function verify(draft: LocalProvider) {
    const controller = new AbortController();
    probe.current = controller;
    setTesting(true);
    setVerified(false);
    try {
      const result = await testProvider(draft, controller.signal);
      if (!controller.signal.aborted) setInstalledModels(result?.models ?? []);
      return !controller.signal.aborted;
    } catch (failure) {
      if (controller.signal.aborted) return false;
      const message = failure instanceof Error ? failure.message : providerInstructions;
      setError(selected.provider === "openai_compatible" && selected.addressRequired
        ? `${message} If your model runs on this machine, start its server and load your selected model before retrying.` : message);
      return false;
    } finally {
      if (probe.current === controller) {
        probe.current = null;
        setTesting(false);
      }
    }
  }

  async function connect(test = false, sample = false) {
    if (busy || probe.current) return;
    setError("");
    setInvalidFields([]);
    let draft: LocalProvider | null;
    try {
      draft = sample ? null : details();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : providerInstructions);
      return;
    }
    if (draft && (test || !verified)) {
      if (!(await verify(draft))) return;
      setVerified(true);
    }
    if (test) return;
    setSaving(true);
    try {
      if (draft) await saveProvider(draft);
      else await trySampleMode();
      if (await providerConfigured() === true) {
        setApiKey("");
        if (sample) onConfigured(true);
        else setConnected(true);
      }
      else setError("Your AI service is not ready yet. Check your service details, then try again.");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : providerInstructions);
    } finally {
      setSaving(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    return connect();
  }

  if (connected) return <main style={{ colorScheme: "light", background: "#fff", color: "#111827" }}>
    <LocalSettings optional />
    <button type="button" onClick={() => onConfigured(false)}>Continue to your workspace</button>
  </main>;

  return (
    <main style={{ maxWidth: 560, margin: "48px auto", padding: 24, colorScheme: "light", background: "#fff", color: "#111827", borderRadius: 16 }}>
      <CharlieMascot />
      {guide && <><p role="status">{verified ? "Connection test passed" : "Needs a connection test"}</p><p>{guide.description}</p><p>Unlocks: {guide.unlocks.join(", ")}</p><p>{guide.data_sent}</p>
        {selected.keyRequired && <a href={guide.provider_urls?.[selected.id] || guide.url} target="_blank" rel="noopener noreferrer">{guide.help_text}</a>}</>}
      <p>Charlie, your AI co-founder</p>
      <h1>Connect your AI service</h1>
      <p>Choose the service you want Charlie to use. Your key stays on your machine.</p>
      <p>You can explore sample data: research, customers, an experiment, outreach drafts and an exportable landing page with no key and no outside connections. Samples are labelled and cannot send email. Open Settings &gt; Keys and connections &gt; Your AI provider later to start a separate real venture.</p>
      <button type="button" disabled={busy} onClick={() => connect(false, true)}>Try without a key (sample mode)</button>
      <form onSubmit={submit} onInvalid={event => {
        const field = (event.target as HTMLInputElement).name;
        setInvalidFields(current => current.includes(field) ? current : [...current, field]);
        setError("Check your required service details, then try again.");
      }} aria-busy={busy} style={{ display: "grid", gap: 12 }}>
        <label htmlFor="provider">AI service</label>
        <select id="provider" name="provider" autoFocus value={selected.id} disabled={busy} onChange={event => {
          const option = localProviders.find(value => value.id === event.target.value);
          if (!option) return;
          setProvider(option.id); setModel(catalogModels[option.id] || ""); setCatalogModel(""); setBaseUrl(option.baseUrl); setApiKey(""); setSavedKey(false); setError(""); setVerified(false); setInvalidFields([]);
        }}>{localProviders.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</select>
        <label htmlFor="api_key">Your API key{savedKey ? " (saved, leave blank to keep it)" : selected.keyRequired ? "" : " (optional)"}</label>
        <input id="api_key" name="api_key" type="password" required={selected.keyRequired && !savedKey} value={apiKey} onChange={event => { setApiKey(event.target.value); setVerified(false); }} autoComplete="new-password" spellCheck={false} disabled={busy} aria-invalid={invalidFields.includes("api_key")} aria-describedby={invalidFields.includes("api_key") ? "key-help local-setup-error" : "key-help"} />
        <p id="key-help">{selected.keyHelp}</p>
        <label htmlFor="model">{selected.id === "azure_openai" ? "Chat model (deployment name)" : "Your model"}</label>
        <input id="model" name="model" required value={model} onChange={event => { setModel(event.target.value); setVerified(false); }} autoComplete="off" disabled={busy} aria-invalid={invalidFields.includes("model")} aria-describedby={invalidFields.includes("model") ? "local-setup-error" : undefined} />
        {selected.id === "azure_openai" && <><label htmlFor="catalog_model">Azure model ID (if different from your deployment name)</label>
          <input id="catalog_model" name="catalog_model" value={catalogModel} onChange={event => { setCatalogModel(event.target.value); setVerified(false); }} autoComplete="off" disabled={busy} /></>}
        {selected.addressRequired && <label htmlFor="base_url">{selected.id === "azure_openai" ? "Service URL" : "Your service address"}</label>}
        {selected.addressRequired && <input id="base_url" name="base_url" type="url" required value={baseUrl} onChange={event => { setBaseUrl(event.target.value); setVerified(false); }} autoComplete="off" disabled={busy} aria-invalid={invalidFields.includes("base_url")} aria-describedby={invalidFields.includes("base_url") ? "local-setup-error" : undefined} />}
        <ErrorNotice id="local-setup-error" error={error} source="local_setup" />
        {verified && <p role="status">{"Your connection works. Choose Save and continue to get started." + installedModelsMessage(installedModels)}</p>}
        <button type="button" onClick={() => connect(true)} disabled={busy}>{testing ? "Testing your connection..." : "Test connection"}</button>
        {testing && <button type="button" onClick={cancelTest}>Cancel test</button>}
        <button type="submit" disabled={busy}>{saving ? "Saving your choice..." : "Save and continue"}</button>
      </form>
    </main>
  );
}
