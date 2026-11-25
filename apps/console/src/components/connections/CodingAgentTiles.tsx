"use client";

import { useEffect, useState } from "react";
import { api, verifiedSessionSubject } from "@/lib/client";
import { desktopInvokeQuiet, desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import { isLocalConsole } from "@/lib/localMode";
import { CODING_AGENTS, OPEN_MAC_APP, codingAgentState, macReportedTools } from "@/lib/codingAgents";
import { localToolNextStep, type DetectedLocalTool } from "@/lib/localToolGuidance";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "@/components/marketing/MarketingLab.module.css";

type Consent = Record<string, boolean>;
type CodingTool = DetectedLocalTool & { unsafe_location?: boolean; unsafe_location_message?: string; candidate?: string; candidate_path?: string; build_blocked_message?: string };
const CHOICES_LOAD_ERROR = "Your coding tool choices could not be loaded. Select Try again in Settings > Keys and connections > Coding tools.";
const TOOLS_CHECK_ERROR = "Your coding tools could not be checked. In Settings > Keys and connections > Coding tools, select Re-detect coding tools.";
const UNSAFE_LOCATION = "Reinstall your coding tool in a folder only you or your administrator can change, then select Re-detect coding tools here.";

export function CodingAgentTilesView({ consent, tools, busy, error, loading, detecting, checking, results, onConsent, onConnect, onRedetect, onRetry }: {
  consent: Consent | null; tools: Record<string, CodingTool> | null; busy: boolean; error: string;
  loading: boolean; detecting: boolean; checking: string | null; results: Record<string, string>;
  onConsent: (provider: string, allow: boolean) => void;
  onConnect: (provider: string) => void;
  onRedetect: () => void;
  onRetry: () => void;
}) {
  if (!consent) return loading || error ? <section aria-labelledby="coding-agents-heading"><h2 id="coding-agents-heading">Coding tools</h2>
    {error ? <><ErrorNotice error={error} source="connections.coding_agents" /><button type="button" onClick={onRetry}>Try again</button></>
      : <p className="page-loading" role="status">Loading your coding tools. Please wait.</p>}</section> : null;
  return <section aria-labelledby="coding-agents-heading"><h2 id="coding-agents-heading">Coding tools</h2>
    <p className={styles.muted}>Allowing a tool does not start work. Ask Charlie to build your app when you are ready.</p>
    <div className={styles.connectionsGrid} style={{ alignItems: "start" }}>{CODING_AGENTS.map(agent => {
      const tool = tools?.[agent.name], reported = tool && codingAgentState(tool);
      const login = { claude: "claude auth login", codex: "codex login", cursor: "cursor-agent login", opencode: "opencode auth login" }[agent.name];
      const local = isLocalConsole();
      const state = detecting ? "Checking…" : local && !tool ? "Not checked"
        : tool?.unsafe_location ? "Found in an unsafe location"
        : tool?.build_blocked_message ? "Git needed"
        : tool?.candidate ? "Approval needed" : tool?.installed === false ? "Not installed"
        : reported === "Builds unavailable" ? "Builds unavailable" : reported === "Ready" ? "Ready" : "Sign in needed";
      return <article key={agent.name} className={`${styles.connector} ${styles.connectionTile}`} aria-label={agent.label}>
        <h3>{agent.label}</h3>
        <span role="status" className={`${styles.pill} ${state === "Ready" ? styles.pillLive : styles.pillWarn}`}>{state}</span>
        <p className={styles.muted}>{tool?.unsafe_location ? (tool.unsafe_location_message || UNSAFE_LOCATION)
          : tool?.build_blocked_message || (state === "Builds unavailable" && tool ? localToolNextStep(tool) : `Use your ${agent.plan} subscription for builds you request.`)}</p>
        {tool?.candidate && <p className={styles.muted}>Review this location: <code>{tool.candidate_path}</code>. {consent[agent.name] ? "Select Check connection to approve it." : "Turn on Allow to approve it."}</p>}
        {(state === "Sign in needed" || state === "Approval needed") && <p className={styles.muted}>
          In Terminal, run <code>{login}</code> with your own subscription login before you approve the tool.
        </p>}
        <button type="button" className={styles.btn} disabled={busy || tool?.unsafe_location} onClick={() => onConnect(agent.name)}>{checking === agent.name ? "Checking..." : "Check connection"}</button>
        {(checking === agent.name || results[agent.name]) && <p role="status" aria-live="polite">{checking === agent.name ? "Checking your connection..." : results[agent.name]}</p>}
        <label style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 8 }}>
          <input type="checkbox" role="switch" data-usage-control="connections_action" checked={consent[agent.name] === true} disabled={busy || (consent[agent.name] !== true && state !== "Ready" && !tool?.candidate)}
            aria-label={`Allow Charlie to use ${agent.label}`} onChange={event => onConsent(agent.name, event.target.checked)} />
          <span>Allow Charlie to use {agent.label}</span>
        </label>
        {(!local || tool) && !tool?.unsafe_location && <details><summary>{state === "Builds unavailable" ? "How to build" : "How to sign in"}</summary><p className={styles.muted}>{tool ? localToolNextStep(tool) : OPEN_MAC_APP}</p>
          <p className={styles.muted}>Your sign-in stays in {agent.label}. Your AI provider key does not connect this coding tool. Supymem never copies your sign-in or switches to API-key billing.</p></details>}
      </article>;
    })}</div>
    <button type="button" disabled={busy} onClick={onRedetect}>Re-detect coding tools</button>
    <ErrorNotice error={error} source="connections.coding_agents" />
  </section>;
}

export function CodingAgentTiles() {
  const [saved, setSaved] = useState<{ subject: string; consent: Consent } | null>(null);
  const [detected, setDetected] = useState<{ subject: string; tools: Record<string, CodingTool> | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [choicesAttempt, setChoicesAttempt] = useState(0);
  const [feedback, setFeedback] = useState<{ subject: string; checking: string | null; results: Record<string, string> } | null>(null);
  const subject = verifiedSessionSubject();

  async function reportedTools() {
    if (!isLocalConsole()) return isDesktopShell() ? desktopInvokeQuiet("list_local_tools") : null;
    const result = await api("/api/local-tools/coding-agents?refresh=true", {
      cache: "no-store", signal: AbortSignal.timeout(15_000),
    });
    if (!result.ok) throw new Error(TOOLS_CHECK_ERROR);
    return (result.data as { tools?: unknown } | null)?.tools;
  }

  useEffect(() => {
    let live = true;
    setSaved(null); setDetected(null); setError(""); setFeedback(null);
    if (!subject) return;
    void api("/api/ui-config").then(result => {
      const data = result.data as { coding_agents_v1?: boolean; coding_agents?: { consent?: Consent } } | null;
      if (!live || subject !== verifiedSessionSubject()) return;
      if (!result.ok || (!isLocalConsole() && data?.coding_agents_v1 !== true) || !data?.coding_agents?.consent) {
        if (isLocalConsole()) setError(CHOICES_LOAD_ERROR);
        return;
      }
      setSaved({ subject, consent: data.coding_agents.consent });
      // Older Mac apps lack sign-in fields; macReportedTools then returns null and the tiles ask to open the Mac app.
      if (isDesktopShell() || isLocalConsole()) void reportedTools().then(found => {
        if (!live || subject !== verifiedSessionSubject()) return;
        const tools = macReportedTools(found);
        setDetected({ subject, tools });
        if (isLocalConsole() && !tools) setError(TOOLS_CHECK_ERROR);
      }).catch(() => { if (live && subject === verifiedSessionSubject()) setError(TOOLS_CHECK_ERROR); });
    }).catch(() => { if (live && subject === verifiedSessionSubject() && isLocalConsole()) setError(CHOICES_LOAD_ERROR); });
    return () => { live = false; };
  }, [subject, choicesAttempt]);

  async function redetect(provider?: string) {
    if (!subject || busy) return;
    setBusy(true); setError("");
    setFeedback(previous => ({ subject, checking: provider || null, results: previous?.subject === subject ? previous.results : {} }));
    try {
      if (!isDesktopShell() && !isLocalConsole()) throw new Error(OPEN_MAC_APP);
      const allowed = provider && saved?.subject === subject && saved.consent[provider] === true;
      if (provider && isLocalConsole() && allowed) {
        const candidate = detected?.tools?.[provider]?.candidate;
        const checked = await api("/api/ui-config/coding-agents/check", { method: "POST", body: JSON.stringify({ provider, candidate }) });
        if (subject !== verifiedSessionSubject()) return;
        if (!checked.ok) {
          const tools = macReportedTools(await reportedTools());
          if (subject !== verifiedSessionSubject()) return;
          setDetected({ subject, tools });
          const detail = (checked.data as { detail?: { code?: string; message?: string } } | null)?.detail;
          const message = (detail?.code === "coding_tool_not_connected" || detail?.code === "coding_git_not_installed") && typeof detail.message === "string" ? detail.message
            : "Sign in with your subscription, review the tool location in Settings > Keys and connections > Coding tools, then select Check connection again.";
          setFeedback(previous => ({ subject, checking: null, results: { ...previous?.results, [provider]: message } }));
          return;
        }
      }
      if (provider && isDesktopShell() && !isLocalConsole() && detected?.tools?.[provider]?.installed !== false) await desktopInvokeOrThrow("connect_local_tool", { name: provider });
      const found = await reportedTools();
      if (subject !== verifiedSessionSubject()) return;
      const tools = macReportedTools(found);
      setDetected({ subject, tools });
      if (!tools) throw new Error();
      if (provider) {
        const agent = CODING_AGENTS.find(item => item.name === provider)!;
        const tool: CodingTool = tools[provider];
        const state = codingAgentState(tool);
        const message = tool.unsafe_location ? (tool.unsafe_location_message || UNSAFE_LOCATION)
          : tool.build_blocked_message ? tool.build_blocked_message
          : tool.candidate ? (allowed ? "Review the location shown here, then select Check connection to approve it." : "Review the location shown here, then turn on Allow to approve it.")
          : state === "Builds unavailable" ? localToolNextStep(tool)
          : state === "Ready" ? (allowed ? "Your connection is ready. Ask Charlie to build your app." : "Your connection is ready. Turn on Allow, then ask Charlie to build your app.")
          : !tool.installed ? `Install ${agent.label}, then choose Check connection here.`
          : "Open How to sign in below, follow the steps, then choose Check connection again.";
        setFeedback(previous => ({ subject, checking: null, results: { ...previous?.results, [provider]: message } }));
      }
    } catch {
      if (subject === verifiedSessionSubject()) {
        setDetected({ subject, tools: null });
        const message = "Your coding tool could not be checked. Reopen your desktop app, then open Settings > Keys and connections > Coding tools and choose Check connection.";
        if (provider) setFeedback(previous => ({ subject, checking: null, results: { ...previous?.results, [provider]: message } }));
        else setError(TOOLS_CHECK_ERROR);
      }
    } finally {
      setBusy(false);
    }
  }

  async function onConsent(provider: string, allow: boolean) {
    if (!saved || saved.subject !== verifiedSessionSubject() || busy) return;
    setBusy(true); setError("");
    try {
      const candidate = allow ? detected?.tools?.[provider]?.candidate : undefined;
      const result = await api("/api/ui-config/coding-agents/consent", { method: "POST", body: JSON.stringify({ provider, allow, candidate }) });
      const consent = (result.data as { consent?: Consent } | null)?.consent;
      if (saved.subject !== verifiedSessionSubject()) return;
      const detail = (result.data as { detail?: { code?: string; message?: string } } | null)?.detail;
      if (!result.ok && detail?.code === "coding_git_not_installed" && typeof detail.message === "string") {
        setError(detail.message);
        return;
      }
      if (!result.ok || !consent) throw new Error();
      setSaved({ subject: saved.subject, consent });
      if (isLocalConsole()) {
        const tools = macReportedTools(await reportedTools());
        if (saved.subject === verifiedSessionSubject()) setDetected({ subject: saved.subject, tools });
      }
    } catch {
      if (saved.subject === verifiedSessionSubject()) setError(allow && isLocalConsole()
        ? "Sign in with your subscription, then select Re-detect coding tools in Settings > Keys and connections > Coding tools, review the location, and turn on Allow again."
        : "Your coding tool choice could not be saved. Open Settings > Keys and connections > Coding tools and try again.");
    } finally {
      setBusy(false);
    }
  }

  const consent = saved?.subject === subject ? saved.consent : null;
  const detecting = isLocalConsole() && !!consent && detected?.subject !== subject && !error;
  return <CodingAgentTilesView consent={consent} tools={detected?.subject === subject ? detected.tools : null} busy={busy || detecting} error={error}
    loading={isLocalConsole() && Boolean(subject) && !saved} detecting={detecting} checking={feedback?.subject === subject ? feedback.checking : null} results={feedback?.subject === subject ? feedback.results : {}}
    onRetry={() => { setError(""); setChoicesAttempt(current => current + 1); }}
    onConsent={onConsent} onConnect={provider => redetect(provider)} onRedetect={() => redetect()} />;
}
