"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";
import { VENTURE_NAME_MAX_CHARS } from "@/lib/ventures";

import { useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { AppShell, type AppShellProps } from "@/components/layout/AppShell";
import { TaskPreferences } from "@/components/tasks/TaskPreferences";
import { SuggestionsSettings } from "@/components/settings/SuggestionsSettings";
import { GodModeRequestCard } from "@/components/settings/GodModeRequestCard";
import { BookingLinkCard } from "@/components/settings/BookingLinkCard";
import { VentureSenderSettings } from "@/components/settings/VentureSenderSettings";
import { VentureMemoryPanel } from "@/components/settings/VentureMemoryPanel";
import { StandingApprovalsSettings } from "@/components/settings/StandingApprovalsSettings";
import { RoutinesSettings } from "@/components/settings/RoutinesSettings";
import { BrowserProfilePreparation } from "@/components/desktop/BrowserProfilePreparation";
import { DesktopSettings } from "@/components/desktop/DesktopSettings";
import { MacTaskList } from "@/components/settings/MacTaskList";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { providerModelOptions } from "@/app/setup/localProviders";
import type { TokenUsage } from "@/lib/api-types";
import { shortTitle } from "@/lib/format";
import { serverReason } from "@/lib/serverReason";
import { RUN_STATUS_REFRESH } from "@/lib/runStatusPresentation";
import { FounderUIContext } from "@/lib/founderUI";
import { inChatActions } from "@/lib/chatRouteAction";
import { ASK_EVENT } from "@/components/layout/NextStepStrip";
import styles from "./VentureSettings.module.css";
import { useFeatureVisibility, useLocalMode } from "@/components/layout/LocalModeBoundary";

export type SettingsSection = "venture" | "manager" | "models" | "mac" | "data" | "memory";

type LLMSettings = {
  provider: string;
  model: string;
  base_url: string | null;
  source: "founder_override" | "platform_default";
  api_key_set: boolean;
  hosted?: boolean;
  models?: string[];
  badge?: string;
  offer?: { model?: string; label?: string; disclosure?: string };
  usage?: TokenUsage;
};

type SettingsScope = { ventureId: string; subject: string; epoch: number; workspaceId: string; ticket: number };
type SettingsRequest = { owner: SettingsScope };

const PROVIDERS = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "google", label: "Google" },
  { value: "deepseek_direct", label: "DeepSeek" },
  { value: "xai", label: "xAI" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "local", label: "Local (Ollama)" },
  { value: "azure_foundry", label: "Azure AI Foundry" },
  { value: "openai_compatible", label: "OpenAI-compatible" },
];

function SettingsFrame({ embedded, ...props }: AppShellProps & { embedded: boolean }) {
  return embedded ? <>{props.children}</> : <AppShell {...props} />;
}

function SettingsLayout({ classic, children }: { classic: ReactNode; children: ReactNode }) {
  return useContext(FounderUIContext) === "classic" ? classic : children;
}

export function VentureSettings({ ventureId, embedded = false, section, onClose }: { ventureId: string; embedded?: boolean; section?: SettingsSection; onClose?: () => void }) {
  const local = useLocalMode();
  const visible = useFeatureVisibility();
  const router = useRouter();
  const chatFirst = useContext(FounderUIContext) === "home";
  const [title, setTitle] = useState("Settings");
  const [level, setLevel] = useState("L1");
  const [msg, setMsg] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [llm, setLlm] = useState<LLMSettings | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [savingLlm, setSavingLlm] = useState(false);
  const [testing, setTesting] = useState(false);
  const [catalog, setCatalog] = useState<Record<string, string[]>>({});
  const [deletePhrase, setDeletePhrase] = useState("DELETE");
  const [deleteTyped, setDeleteTyped] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [bundleNote, setBundleNote] = useState("");
  const [ventureName, setVentureName] = useState("");
  const [managerName, setManagerName] = useState("");
  const [managerSaved, setManagerSaved] = useState(false);
  const [paused, setPaused] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);
  const [levelSaved, setLevelSaved] = useState(false);
  const [pauseNote, setPauseNote] = useState("");
  const [loadedScope, setLoadedScope] = useState<SettingsScope | null>(null);
  const [loadError, setLoadError] = useState("");
  const [llmLoading, setLlmLoading] = useState(true);
  const [mutationBusy, setMutationBusy] = useState(false);
  const active = useRef(ventureId); active.current = ventureId;
  const generation = useRef(0);
  const llmRead = useRef(0);
  const llmDirty = useRef(false);
  const mutation = useRef<SettingsRequest | null>(null);
  const current = useCallback((owner: SettingsScope) => owner.ticket === generation.current
    && owner.ventureId === active.current && owner.subject === verifiedSessionSubject()
    && owner.epoch === verifiedSessionEpoch(), []);
  const ready = Boolean(loadedScope && current(loadedScope));
  const editable = () => Boolean(loadedScope && current(loadedScope) && !mutationBusy && !mutation.current);

  const load = useCallback(async () => {
    if (ventureId !== active.current) return;
    const ticket = ++generation.current;
    setLoadedScope(null); setLoadError(""); setLlmLoading(true);
    try {
      const auth = await ensureSession();
      const subject = verifiedSessionSubject();
      if (ticket !== generation.current || ventureId !== active.current) return;
      if (!auth.ok || !subject) {
        setLoadError("Your account could not be verified. Reconnect before changing these settings."); return;
      }
      const owner: SettingsScope = { ventureId, subject, epoch: verifiedSessionEpoch(), workspaceId: "", ticket };
      const [phrase, ven] = await Promise.all([
        api("/api/ventures/delete-phrase").catch(() => null),
        api(`/api/ventures/${encodeURIComponent(ventureId)}`),
      ]);
      if (!current(owner)) return;
      if (phrase?.ok && typeof (phrase.data as { phrase?: unknown } | null)?.phrase === "string") {
        setDeletePhrase((phrase.data as { phrase: string }).phrase);
      }
      if (!ven.ok || !ven.data || typeof ven.data !== "object") {
        setLoadError("This venture's settings could not be loaded. Refresh saved settings before changing them."); return;
      }
      const v = ven.data as { name?: string; raw_idea?: string; autonomy_level?: string;
        workspace_id?: string; manager_name?: string; paused?: boolean };
      owner.workspaceId = typeof v.workspace_id === "string" ? v.workspace_id : "";
      setManagerName(v.manager_name || ""); setPaused(Boolean(v.paused));
      setTitle(shortTitle(v.name, v.raw_idea)); setVentureName(v.name || shortTitle(v.name, v.raw_idea));
      setLevel(v.autonomy_level || "L1"); setWorkspaceId(owner.workspaceId); setLoadedScope(owner);
      if (owner.workspaceId) {
        const read = ++llmRead.current;
        const [settings, cat] = await Promise.all([
          api(`/api/workspaces/${encodeURIComponent(owner.workspaceId)}/llm-settings`).catch(() => null),
          api(`/api/workspaces/${encodeURIComponent(owner.workspaceId)}/llm-catalog`).catch(() => null),
        ]);
        if (!current(owner)) return;
        if (read === llmRead.current && !llmDirty.current) setLlm(settings?.ok ? settings.data as LLMSettings : null);
        if (cat?.ok) setCatalog(providerModelOptions(cat.data));
      }
      setLlmLoading(false);
    } catch {
      if (ticket === generation.current && ventureId === active.current) {
        setLoadedScope(null); setLoadError("Settings could not be checked. Refresh before changing them.");
      }
    }
  }, [ventureId, current]);

  useEffect(() => {
    const retire = () => {
      ++generation.current; ++llmRead.current; llmDirty.current = false; mutation.current = null; setMutationBusy(false);
      setLoadedScope(null); setLoadError(""); setTitle("Settings"); setLevel("L1"); setMsg("");
      setWorkspaceId(""); setLlm(null); setApiKey(""); setCatalog({}); setDeletePhrase("DELETE"); setDeleteTyped("");
      setVentureName(""); setManagerName(""); setManagerSaved(false); setPaused(false); setPauseNote("");
      setSavingLlm(false); setTesting(false); setDeleting(false); setExporting(false); setStopBusy(false);
      setLevelSaved(false); setBundleNote(""); void load();
    };
    retire(); window.addEventListener("supymem-session-changed", retire);
    return () => { ++generation.current; ++llmRead.current; mutation.current = null;
      window.removeEventListener("supymem-session-changed", retire); };
  }, [load]);

  // "Open Mac tasks" links end in #computer-use; the cards above it load after first paint and push it down.
  const llmReady = !!llm;
  useEffect(() => {
    const target = window.location.hash.slice(1);
    if (!ready || !["computer-use", "settings-computer-use"].includes(target) || !workspaceId) return;
    const owner = loadedScope!;
    const go = () => { if (current(owner)) document.getElementById(target)?.scrollIntoView({ block: "start" }); };
    go(); requestAnimationFrame(go);
  }, [workspaceId, llmReady, ventureName, ready, loadedScope, current]);

  useEffect(() => {
    const owner = loadedScope;
    if (!owner || !current(owner) || !owner.workspaceId) return;
    const refresh = async () => {
      if (!current(owner) || mutation.current || llmDirty.current) return;
      const read = ++llmRead.current;
      const settings = await api(`/api/workspaces/${encodeURIComponent(owner.workspaceId)}/llm-settings`);
      if (current(owner) && read === llmRead.current && !llmDirty.current && settings.ok) setLlm(settings.data as LLMSettings);
    };
    const tick = window.setInterval(() => void refresh(), 8000);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(tick);
      window.removeEventListener("focus", onFocus);
    };
  }, [loadedScope, current]);

  function begin(): SettingsRequest | null {
    if (!loadedScope || !current(loadedScope) || mutation.current) return null;
    const request = { owner: loadedScope }; mutation.current = request; setMutationBusy(true); ++llmRead.current; return request;
  }
  const admitted = (request: SettingsRequest) => mutation.current === request && current(request.owner);
  function finish(request: SettingsRequest) { if (mutation.current === request) { mutation.current = null; setMutationBusy(false); } }

  async function saveLevel(next: string) {
    const request = begin(); if (!request) return;
    const prev = level;
    setLevel(next);
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(request.owner.ventureId)}/autonomy`, { method: "POST", body: JSON.stringify({ level: next }) });
      if (!admitted(request)) return;
      if (r.ok) { setMsg("Autonomy updated for this venture."); setLevelSaved(true);
        window.setTimeout(() => { if (current(request.owner)) setLevelSaved(false); }, 4000);
      } else { setLevel(prev); setMsg(serverReason(r, "Couldn’t confirm the autonomy change. Refresh saved settings before retrying.")); }
    } finally { finish(request); }
  }

  async function downloadJson(kind: "export" | "support-bundle") {
    const request = begin(); if (!request) return;
    setExporting(true);
    setBundleNote("");
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(request.owner.ventureId)}/${kind}`,
        kind === "export" ? { method: "POST" } : undefined);
      if (!admitted(request)) return;
      if (!r.ok) { setBundleNote("Could not build that file. Retry, or copy the venture id from the URL."); return; }
      const blob = new Blob([JSON.stringify(r.data, null, 2)], { type: "application/json" });
      const href = URL.createObjectURL(blob);
      try {
        if (!admitted(request)) return;
        const a = document.createElement("a"); a.href = href;
        a.download = `${ventureName || "venture"}-${kind === "export" ? "export" : "stuck-report"}.json`; a.click();
      } finally { URL.revokeObjectURL(href); }
    } finally { if (admitted(request)) setExporting(false); finish(request); }
  }

  async function pauseVenture(pause: boolean) {
    const request = begin(); if (!request) return;
    setStopBusy(true);
    setPauseNote("");
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(request.owner.ventureId)}/${pause ? "pause" : "resume"}`, {
        method: "POST", body: JSON.stringify(pause ? { reason: "Stopped from Settings" } : {}),
      });
      if (!admitted(request)) return;
      if (r.ok) {
        setPaused(pause); window.dispatchEvent(new Event(RUN_STATUS_REFRESH)); setPauseNote(pause ? "Paused" : "Resumed");
        setMsg(pause ? "Paused. New work is blocked for this venture. Work already running may finish; review its results and credit settlement." : "Resumed. This venture’s work can continue.");
      } else setMsg(serverReason(r, pause ? "Couldn’t pause this venture. Check saved status before retrying." : "Couldn’t resume this venture. Check saved status before retrying."));
    } finally { if (admitted(request)) setStopBusy(false); finish(request); }
  }

  async function saveManagerName() {
    const request = begin(); if (!request) return;
    setManagerSaved(false);
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(request.owner.ventureId)}/settings`, {
        method: "PATCH", body: JSON.stringify({ manager_name: managerName }),
      });
      if (!admitted(request)) return;
      if (r.ok) {
        const next = (r.data as { manager_name?: string })?.manager_name || "";
        setManagerName(next); setManagerSaved(true);
        window.dispatchEvent(new CustomEvent("supymem-manager-renamed", { detail: next || "Charlie" }));
        setMsg(`Your manager is now called ${next || "Charlie"}.`);
      } else setMsg(serverReason(r, "Couldn’t save the manager name. Check saved settings before retrying."));
    } finally { finish(request); }
  }

  async function saveVentureName() {
    const request = begin(); if (!request) return;
    try {
      const { renameVenture } = await import("@/lib/ventures");
      if (!admitted(request)) return;
      const r = await renameVenture(request.owner.ventureId, ventureName);
      if (!admitted(request)) return;
      if (r.ok) {
        const next = ((r.data as { name?: string })?.name || ventureName).trim();
        setVentureName(next); setTitle(next); setMsg("Name saved.");
      } else setMsg("The name change was not confirmed. Refresh saved settings before retrying.");
    } finally { finish(request); }
  }

  async function saveLlm() {
    if (!workspaceId || !llm) return;
    const request = begin(); if (!request) return;
    if (request.owner.workspaceId !== workspaceId) { finish(request); return; }
    setSavingLlm(true);
    const payload: Record<string, string | null> = {
      provider: llm.provider,
      model: llm.model,
      base_url: llm.base_url || null,
    };
    if (apiKey.trim()) payload.api_key = apiKey.trim();
    try {
      const r = await api(`/api/workspaces/${encodeURIComponent(request.owner.workspaceId)}/llm-settings`, {
        method: "PUT", body: JSON.stringify(payload),
      });
      if (!admitted(request)) return;
      if (r.ok) { llmDirty.current = false; setLlm(r.data as LLMSettings); setApiKey(""); setMsg("LLM settings saved, live calls will use this key."); }
      else setMsg(serverReason(r, "Couldn’t confirm LLM settings. Refresh saved settings before retrying."));
    } finally { if (admitted(request)) setSavingLlm(false); finish(request); }
  }

  async function clearLlm() {
    if (local || !workspaceId) return;
    const request = begin(); if (!request) return;
    if (request.owner.workspaceId !== workspaceId) { finish(request); return; }
    try {
      const r = await api(`/api/workspaces/${encodeURIComponent(request.owner.workspaceId)}/llm-settings`, { method: "DELETE" });
      if (!admitted(request)) return;
      if (r.ok) { llmDirty.current = false; setLlm(r.data as LLMSettings); setApiKey(""); setMsg("Using platform keys."); }
      else setMsg(serverReason(r, "Couldn’t confirm the provider reset. Refresh saved settings before retrying."));
    } finally { finish(request); }
  }

  async function testConnection() {
    if (!workspaceId) return;
    const request = begin(); if (!request) return;
    if (request.owner.workspaceId !== workspaceId) { finish(request); return; }
    setTesting(true);
    try {
      const r = await api(`/api/workspaces/${encodeURIComponent(request.owner.workspaceId)}/llm-settings/test`, { method: "POST" });
      if (!admitted(request)) return;
      if (r.ok) {
        setMsg("Your connection works. You can continue with your request.");
        const settings = await api(`/api/workspaces/${encodeURIComponent(request.owner.workspaceId)}/llm-settings`);
        if (admitted(request) && !llmDirty.current && settings.ok) setLlm(settings.data as LLMSettings);
      } else {
        const err = (r.data as { detail?: { error?: { message?: string; hint?: string; code?: string } } })
          ?.detail?.error;
        const parts = [err?.message || "Connection test failed."];
        if (err?.hint) parts.push(err.hint);
        else if (err?.code === "bad_api_key") parts.push("Check the key and provider, then try again.");
        setMsg(parts.join(" "));
      }
    } finally { if (admitted(request)) setTesting(false); finish(request); }
  }

  async function deleteVenture() {
    if (deleteTyped.trim().toUpperCase() !== deletePhrase.trim().toUpperCase()) return;
    const request = begin(); if (!request) return;
    setDeleting(true);
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(request.owner.ventureId)}/delete`, {
        method: "POST", body: JSON.stringify({ confirmation: deleteTyped.trim() }),
      });
      if (!admitted(request)) return;
      if (r.ok) { setMsg(`${ventureName || "Venture"} moved to trash, recoverable for 7 days.`); router.push("/"); }
      else setMsg(serverReason(r, "Deletion was not confirmed. Check saved venture status before retrying."));
    } finally { if (admitted(request)) setDeleting(false); finish(request); }
  }

  const modelOptions =
    (llm && (catalog[llm.provider] || llm.models)) || (llm ? [llm.model] : []);
  const renderedSubject = verifiedSessionSubject(), renderedEpoch = verifiedSessionEpoch();
  const refreshSaved = () => {
    if (!mutation.current && ventureId === active.current && renderedSubject === verifiedSessionSubject()
        && renderedEpoch === verifiedSessionEpoch()) void load();
  };

  if (!ready) return <SettingsFrame embedded={embedded} title="Settings" ventureId={ventureId}>
    <div className="panel-card"><p role="status">{loadError || "Checking this venture's saved settings…"}</p>
      {loadError && <button type="button" disabled={mutationBusy} onClick={refreshSaved}>Refresh saved settings</button>}
    </div>
  </SettingsFrame>;

  const classic = (
      <div className="chat-scroll">
        <div className="chat-inner" key={`${loadedScope!.subject}:${loadedScope!.epoch}:${loadedScope!.ticket}:${ventureId}`}>
          <TaskPreferences ventureId={ventureId} />
          <div className="panel-card">
            <h3>Tasks</h3>
            <p className="muted">Review progress, pause a standing task, or hand off something new.</p>
            <ChatAnchor data-usage-control="settings_tasks_open" href={`/v/${encodeURIComponent(ventureId)}/tasks`}>Open tasks</ChatAnchor>
          </div>
          <div className="panel-card">
            <h3 id="settings-venture">Venture name</h3>
            <p className="muted">Shown in the sidebar and in downloads. Saved right away.</p>
            <form
              style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}
              onSubmit={(e) => {
                e.preventDefault();
                void saveVentureName();
              }}
            >
              <input
                value={ventureName}
                disabled={mutationBusy}
                onChange={(e) => { if (editable()) setVentureName(e.target.value); }}
                maxLength={VENTURE_NAME_MAX_CHARS}
                style={{ flex: "1 1 16rem" }}
                aria-label="Venture name"
              />
              <button className="primary" type="submit" data-usage-control="venture_rename" disabled={mutationBusy}>
                Save name
              </button>
            </form>
          </div>
          <div className="panel-card">
            <h3 id="settings-manager">Manager name</h3>
            <p className="muted">What your manager is called in chat. It applies to every venture in this workspace. Leave empty for the default, Charlie.</p>
            <form
              style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}
              onSubmit={(e) => {
                e.preventDefault();
                void saveManagerName();
              }}
            >
              <input
                value={managerName}
                disabled={mutationBusy}
                onChange={(e) => { if (editable()) { setManagerName(e.target.value); setManagerSaved(false); } }}
                maxLength={40}
                placeholder="Charlie"
                style={{ flex: "1 1 16rem" }}
                aria-label="Manager name"
              />
              <button className="primary" type="submit" data-usage-control="settings_manager_rename" disabled={mutationBusy}>Save manager name</button>
              {managerSaved && <span role="status" className="muted" style={{ alignSelf: "center" }}>Saved</span>}
            </form>
          </div>
          <div className="panel-card">
            <h3>Pause this venture</h3>
            <p className="muted">
              {paused
                ? "This venture is paused: new work is blocked. Review results and credit settlement for any work already running."
                : "Blocks new work for this venture. Work already running may finish, and unconfirmed usage remains unresolved. Other ventures are not affected."}
            </p>
            <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center" }}>
              {paused ? (
                <button type="button" data-usage-control="venture_resume" disabled={mutationBusy || stopBusy} onClick={() => void pauseVenture(false)}>Resume</button>
              ) : (
                <button className="danger" type="button" data-usage-control="settings_emergency_stop" disabled={mutationBusy || stopBusy} onClick={() => void pauseVenture(true)}>Pause venture</button>
              )}
              {pauseNote && <span role="status" className="muted">{pauseNote}</span>}
            </div>
          </div>
          <GodModeRequestCard ventureId={ventureId} />
          <BookingLinkCard ventureId={ventureId} workspaceId={workspaceId} />
          <VentureSenderSettings key={`${loadedScope!.subject}:${loadedScope!.epoch}:${loadedScope!.ticket}`} ventureId={ventureId} workspaceId={workspaceId} current={() => current(loadedScope!)} />
          <SuggestionsSettings />
          {local ? <div className="panel-card">
            <h3 id="settings-models">Your AI service</h3>
            <p className="muted">You choose the AI service and model your app uses. Your key stays on your machine. Open your AI service settings to check your connection or change your service.</p>
            <ChatAnchor href="/settings/local#providers-title">Open your AI service settings</ChatAnchor>
          </div> : llm && (
            <div className="panel-card">
              <h3 id="settings-models">How this company thinks</h3>
              <p className="muted">
                This model powers Supymem&apos;s research and chat. Claude Code and Codex coding sessions
                use local tools on your Mac below. Signing in to Claude or ChatGPT in a browser does
                not add an API key here or verify a local coding session.
              </p>
              {llm.source !== "founder_override" ? (
                <>
                  <p className="muted">
                    Supymem hosts the model on our cloud proxy. Everyone who downloads the app uses
                    the same service. The platform key never
                    appears in this app, you do not paste it, and we do not write it to this Mac.
                  </p>
                  <p>{llm.badge || "Using Supymem: DeepSeek-V4-Pro"}</p>
                </>
              ) : (
                <>
              <p className="muted">
                Optional. Paste a provider API key you own; API billing is separate from a Claude or
                ChatGPT subscription. Supymem&apos;s platform key is never shown here.
              </p>
              <p className="muted">{llm.badge || "Using your workspace provider."}</p>
              <label htmlFor="llm-provider">Provider</label>
              <select
                id="llm-provider"
                disabled={mutationBusy}
                value={llm.provider}
                onChange={(e) => {
                  if (!editable()) return;
                  llmDirty.current = true;
                  const provider = e.target.value;
                  const models = catalog[provider] || [];
                  setLlm({
                    ...llm,
                    provider,
                    model: models[0] || llm.model,
                    models,
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
              <label htmlFor="llm-model" style={{ display: "block", marginTop: 12 }}>
                Model
              </label>
              {modelOptions.length > 0 ? (
                <select
                  id="llm-model"
                  disabled={mutationBusy}
                  value={llm.model}
                  onChange={(e) => { if (editable()) { llmDirty.current = true; setLlm({ ...llm, model: e.target.value }); } }}
                  style={{ marginTop: 6, width: "100%" }}
                >
                  {modelOptions.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id="llm-model"
                  disabled={mutationBusy}
                  value={llm.model}
                  onChange={(e) => { if (editable()) { llmDirty.current = true; setLlm({ ...llm, model: e.target.value }); } }}
                  style={{ marginTop: 6, width: "100%" }}
                />
              )}
              <label htmlFor="llm-base-url" style={{ display: "block", marginTop: 12 }}>
                Base URL
              </label>
              <input
                id="llm-base-url"
                disabled={mutationBusy}
                value={llm.base_url || ""}
                placeholder="Provider default"
                onChange={(e) => { if (editable()) { llmDirty.current = true; setLlm({ ...llm, base_url: e.target.value || null }); } }}
                style={{ marginTop: 6, width: "100%" }}
              />
              <label htmlFor="llm-api-key" style={{ display: "block", marginTop: 12 }}>
                API key
              </label>
              <input
                id="llm-api-key"
                disabled={mutationBusy}
                type="password"
                value={apiKey}
                placeholder={
                  llm.api_key_set
                    ? "Key saved (leave blank to keep)"
                    : "Optional, platform default if empty"
                }
                onChange={(e) => { if (editable()) { llmDirty.current = true; setApiKey(e.target.value); } }}
                style={{ marginTop: 6, width: "100%" }}
              />
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
                <button
                  type="button"
                  className="primary"
                  data-usage-control="llm_settings_save"
                  onClick={() => void saveLlm()}
                  disabled={mutationBusy || savingLlm}
                >
                  {savingLlm ? "Saving…" : "Save"}
                </button>
                <button type="button" data-usage-control="llm_settings_test" onClick={() => void testConnection()} disabled={mutationBusy || testing}>
                  {testing ? "Testing…" : "Test connection"}
                </button>
                {!local && <button type="button" data-usage-control="llm_settings_use_ours" onClick={() => void clearLlm()} disabled={mutationBusy}>
                    Back to Supymem hosted
                  </button>}
              </div>
                </>
              )}
            </div>
          )}
          {msg && <div className="muted">{msg}</div>}
          {visible("settings", "mac") && <div id="computer-use">
            <div className="panel-card" role="status">
              <h3>Computer use</h3>
              <p className="muted">{local ? "You can ask for a browser task in chat. Your task uses only the websites you permit. Open a saved task below to check its progress or change its website permissions." : "Supymem can do short browser tasks for you in the Mac app: you review the exact websites and goal, then approve it. Controlling other apps on your Mac is coming soon in beta."}</p>
            </div>
            <div id="settings-computer-use"><BrowserProfilePreparation ventureId={ventureId} /></div>
            <p className="muted">You approve websites for each task, not all future tasks. Prepare a task above, or open a saved task below to review its websites, continue a waiting task, or cancel it. Cancelling withdraws approval for the whole task, including its other websites, and keeps your saved results. Work already running may finish. Review its saved status before trying again after an interruption. Sign in only in the separate task browser when asked. Your existing browser tabs are not connected.</p>
            <MacTaskList key={`${loadedScope!.subject}:${loadedScope!.epoch}:${ventureId}`} ventureId={ventureId} />
          </div>}
          {visible("settings", "mac") && <DesktopSettings ventureId={ventureId} />}

          <div className="panel-card" style={{ marginTop: 24 }}>
            <h3 id="settings-your-data">Your data, and if you&apos;re stuck</h3>
            <p className="muted">
              Download a copy of your plan, tasks, and website details any time. It never includes your passwords or keys.
            </p>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
              <button
                type="button"
                className="primary"
                disabled={mutationBusy || exporting}
                onClick={() =>
                  void downloadJson("export")
                }
              >
                {exporting ? "Preparing…" : "Download my venture"}
              </button>
              <button
                type="button"
                disabled={mutationBusy || exporting}
                onClick={() =>
                  void downloadJson("support-bundle")
                }
              >
                I&apos;m stuck, download report
              </button>
            </div>
            {bundleNote ? <p className="muted" style={{ marginTop: 8 }}>{bundleNote}</p> : null}
          </div>

          <div id="settings-memory"><VentureMemoryPanel ventureId={ventureId} /></div>
          {workspaceId && <StandingApprovalsSettings workspaceId={workspaceId} ventureId={ventureId} />}
          {workspaceId && <RoutinesSettings workspaceId={workspaceId} ventureId={ventureId} />}

          <div className="panel-card" style={{ marginTop: 24, borderColor: "var(--danger)" }}>
            <h3>Delete this venture</h3>
            <p className="muted">
              Your venture leaves your list right away. To undo, choose Restore under Recently deleted in All ventures
              within <strong>7 days</strong>.
              {local && " To see what stays on this computer, open Manage Ventures and choose Delete."}
            </p>
            <p style={{ marginTop: 12, color: "var(--fg-0)" }}>
              Type <strong>{deletePhrase}</strong> to confirm. Capitalization does not matter.
            </p>
            <input
              aria-label="Type DELETE to confirm"
              disabled={mutationBusy}
              value={deleteTyped}
              onChange={(e) => { if (editable()) setDeleteTyped(e.target.value); }}
              placeholder="DELETE"
              style={{ marginTop: 12, width: "100%" }}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              type="button"
              className="danger"
              data-usage-control="venture_delete"
              style={{ marginTop: 12 }}
              disabled={
                mutationBusy || deleting ||
                deleteTyped.trim().toUpperCase() !== deletePhrase.trim().toUpperCase()
              }
              onClick={() => void deleteVenture()}
            >
              {deleting ? "Deleting…" : "Delete venture"}
            </button>
          </div>
        </div>
      </div>
  );
  const show = (id: SettingsSection) => !section || section === id;
  const home = <div className={styles.settings}>
    <div className="chat-inner" key={`${loadedScope!.subject}:${loadedScope!.epoch}:${loadedScope!.ticket}:${ventureId}`}>
      {show("venture") && <section className="settings-group" id="settings-venture">
        <h2>Venture</h2>
        <div className="settings-row">
          <strong>Venture name</strong>
          <form onSubmit={event => { event.preventDefault(); void saveVentureName(); }}>
            <input value={ventureName} disabled={mutationBusy} onChange={event => { if (editable()) setVentureName(event.target.value); }} maxLength={VENTURE_NAME_MAX_CHARS} aria-label="Venture name" />
            <button className="primary" type="submit" data-usage-control="venture_rename" disabled={mutationBusy}>Save</button>
          </form>
        </div>
        <div className="settings-row">
          <strong>Manager name</strong>
          <form onSubmit={event => { event.preventDefault(); void saveManagerName(); }}>
            <input value={managerName} disabled={mutationBusy} onChange={event => { if (editable()) { setManagerName(event.target.value); setManagerSaved(false); } }} maxLength={40} placeholder="Charlie" aria-label="Manager name" />
            <button className="primary" type="submit" data-usage-control="settings_manager_rename" disabled={mutationBusy}>Save</button>
            {managerSaved && <span role="status">Saved</span>}
          </form>
        </div>
        <div className="settings-row">
          <strong>Open</strong>
          <span className="settings-actions">
            {[
              { label: "Landing page", href: `/v/${encodeURIComponent(ventureId)}/site` },
              { label: "Tasks", href: `/v/${encodeURIComponent(ventureId)}/tasks`, control: "settings_tasks_open" },
              { label: "Connections", href: "/connections" },
              { label: "Next step", href: `/v/${encodeURIComponent(ventureId)}/${chatFirst ? "suggestions" : "now"}` },
            ].map(link => chatFirst
              ? <button key={link.label} type="button" className="settings-link" data-usage-control={link.control} onClick={() => {
                if (!loadedScope || !current(loadedScope)) return;
                const action = inChatActions([link], true, ventureId)[0];
                onClose?.();
                window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: { action: action.action, payload: action.payload } }));
              }}>{link.label}</button>
              : <ChatAnchor key={link.label} className="settings-link" href={link.href} data-usage-control={link.control}>{link.label}</ChatAnchor>)}
          </span>
        </div>
        <BookingLinkCard ventureId={ventureId} workspaceId={workspaceId} />
        <VentureSenderSettings key={`${loadedScope!.subject}:${loadedScope!.epoch}:${loadedScope!.ticket}`} ventureId={ventureId} workspaceId={workspaceId} current={() => current(loadedScope!)} />
        {!workspaceId && <button type="button" disabled={mutationBusy} onClick={refreshSaved}>Refresh saved settings</button>}
      </section>}
      {show("manager") && <section className="settings-group" id="settings-manager">
        <h2>AI manager</h2>
        <div className="settings-row">
          <strong>{paused ? "Paused" : "Running"}</strong>
          <span className="settings-actions">
            {paused ? <button type="button" data-usage-control="venture_resume" disabled={mutationBusy || stopBusy} onClick={() => void pauseVenture(false)}>Resume</button>
              : <button className="danger" type="button" data-usage-control="settings_emergency_stop" disabled={mutationBusy || stopBusy} onClick={() => void pauseVenture(true)}>Pause</button>}
            {pauseNote && <span role="status">{pauseNote}</span>}
          </span>
        </div>
        <details className={styles.explanation}><summary>About pausing</summary><p className="muted">Blocks new work for this venture. Work already running may finish, and unconfirmed usage remains unresolved. Other ventures are not affected.</p></details>
        <GodModeRequestCard ventureId={ventureId} compact />
        <TaskPreferences ventureId={ventureId} />
        <SuggestionsSettings />
      </section>}
      {show("models") && <section className="settings-group" id="settings-models">
        <h2>Models and computer use</h2>
        {local ? <div className="panel-card"><h3>Your AI service</h3><p>You choose your service and model. Open your AI service settings to check your connection or change your service.</p><ChatAnchor href="/settings/local#providers-title">Open your AI service settings</ChatAnchor></div> : llm ? <div className="panel-card">
          <h3>Model</h3>
          <p>{llm.badge || llm.model}</p>
          <details className={styles.explanation}><summary>About this model</summary><p className="muted">This model powers your research and chat. Claude Code and Codex coding sessions use local tools on your Mac. Signing in to Claude or ChatGPT in a browser does not add an API key here or verify a local coding session.</p><p className="muted">Your hosted connection key never appears in this app, you do not paste it, and we do not write it to this Mac. Choose your model here, or connect your local coding tool separately.</p></details>
          {llm.source === "founder_override" && <>
            <label htmlFor="llm-provider">Provider</label>
            <select id="llm-provider" value={llm.provider} disabled={mutationBusy} onChange={event => {
              if (!editable()) return;
              const provider = event.target.value, models = catalog[provider] || [];
              llmDirty.current = true; setLlm({ ...llm, provider, model: models[0] || llm.model, models });
            }}>{PROVIDERS.map(provider => <option key={provider.value} value={provider.value}>{provider.label}</option>)}</select>
            <label htmlFor="llm-model">Model</label>
            {modelOptions.length > 0 ? <select id="llm-model" value={llm.model} disabled={mutationBusy} onChange={event => { if (editable()) { llmDirty.current = true; setLlm({ ...llm, model: event.target.value }); } }}>{modelOptions.map(model => <option key={model} value={model}>{model}</option>)}</select>
              : <input id="llm-model" value={llm.model} disabled={mutationBusy} onChange={event => { if (editable()) { llmDirty.current = true; setLlm({ ...llm, model: event.target.value }); } }} />}
            <label htmlFor="llm-base-url">Base URL</label>
            <input id="llm-base-url" value={llm.base_url || ""} disabled={mutationBusy} placeholder="Provider default" onChange={event => { if (editable()) { llmDirty.current = true; setLlm({ ...llm, base_url: event.target.value || null }); } }} />
            <label htmlFor="llm-api-key">API key</label>
            <input id="llm-api-key" type="password" value={apiKey} disabled={mutationBusy} placeholder={llm.api_key_set ? "Key saved (leave blank to keep)" : "Optional, platform default if empty"} onChange={event => { if (editable()) { llmDirty.current = true; setApiKey(event.target.value); } }} />
            <div className="settings-actions">
              <button type="button" className="primary" data-usage-control="llm_settings_save" onClick={() => void saveLlm()} disabled={mutationBusy || savingLlm}>{savingLlm ? "Saving…" : "Save"}</button>
              <button type="button" data-usage-control="llm_settings_test" onClick={() => void testConnection()} disabled={mutationBusy || testing}>{testing ? "Testing…" : "Test connection"}</button>
              {!local && <button type="button" data-usage-control="llm_settings_use_ours" onClick={() => void clearLlm()} disabled={mutationBusy}>Back to Supymem hosted</button>}
            </div>
          </>}
        </div> : <div className="panel-card"><p role="status">{llmLoading ? "Loading model settings…" : "Model settings could not be loaded."}</p>{!llmLoading && <button type="button" disabled={mutationBusy} onClick={refreshSaved}>Refresh saved settings</button>}</div>}
        {visible("settings", "mac") && <div id="computer-use" className="panel-card" role="status">
          <h3>Computer use</h3>
          <p className="muted">{local ? "You can ask for a browser task in chat. Your task uses only the websites you permit. Open your saved task to check its progress or change its website permissions." : "Supymem can do short browser tasks for you in the Mac app: you review the exact websites and goal, then approve it. Controlling other apps on your Mac is coming soon in beta."}</p>
          <ChatAnchor className="settings-link" href={`/v/${encodeURIComponent(ventureId)}/settings#settings-computer-use`}>Prepare a browser task</ChatAnchor>
        </div>}
      </section>}
      {show("mac") && visible("settings", "mac") && <section className="settings-group" id="settings-mac">
        <h2>This Mac</h2>
        <DesktopSettings ventureId={ventureId} />
        <div id="settings-computer-use"><BrowserProfilePreparation ventureId={ventureId} /></div>
        <p className="muted">You approve websites for each task, not all future tasks. Prepare a task above, or open a saved task below to review its websites, continue a waiting task, or cancel it. Cancelling withdraws approval for the whole task, including its other websites, and keeps your saved results. Work already running may finish. Review its saved status before trying again after an interruption. Sign in only in the separate task browser when asked. Your existing browser tabs are not connected.</p>
        <MacTaskList key={`${loadedScope!.subject}:${loadedScope!.epoch}:${ventureId}`} ventureId={ventureId} />
      </section>}
      {show("data") && <section className="settings-group" id="settings-your-data">
        <h2>Your data</h2>
        <div className="panel-card">
          <h3>Export</h3>
          <div className="settings-actions">
            <button type="button" className="primary" disabled={mutationBusy || exporting} onClick={() => void downloadJson("export")}>{exporting ? "Preparing…" : "Download my venture"}</button>
            <button type="button" disabled={mutationBusy || exporting} onClick={() => void downloadJson("support-bundle")}>I&apos;m stuck, download report</button>
          </div>
          <details className={styles.explanation}><summary>About your download</summary><p className="muted">Download a copy of your plan, tasks, and website details any time. It never includes your passwords or keys.</p></details>
          {bundleNote && <p className="muted" role="status">{bundleNote}</p>}
        </div>
      </section>}
      {show("memory") && <section className="settings-group" id="settings-memory">
        <h2>Memory</h2>
        <VentureMemoryPanel ventureId={ventureId} compact />
        {workspaceId && <StandingApprovalsSettings workspaceId={workspaceId} ventureId={ventureId} />}
        {workspaceId && <RoutinesSettings workspaceId={workspaceId} ventureId={ventureId} />}
        <details className="settings-danger">
          <summary>Delete venture</summary>
          <div className="panel-card">
            <h3>Delete venture</h3>
            <p>Type {deletePhrase}. Recoverable for 7 days.</p>
            <input aria-label={`Type ${deletePhrase} to confirm`} value={deleteTyped} disabled={mutationBusy} onChange={event => { if (editable()) setDeleteTyped(event.target.value); }} placeholder={deletePhrase} autoComplete="off" spellCheck={false} />
            <button type="button" className="danger" data-usage-control="venture_delete" disabled={mutationBusy || deleting || deleteTyped.trim().toUpperCase() !== deletePhrase.trim().toUpperCase()} onClick={() => void deleteVenture()}>{deleting ? "Deleting…" : "Delete venture"}</button>
          </div>
        </details>
      </section>}
      {msg && <p className="muted" role="status">{msg}</p>}
    </div>
  </div>;
  return <SettingsFrame embedded={embedded} title={title} ventureId={ventureId}>
    {mutationBusy && <p role="status">Updating or checking your settings…</p>}
    {embedded ? home : <SettingsLayout classic={classic}>{home}</SettingsLayout>}
  </SettingsFrame>;
}
