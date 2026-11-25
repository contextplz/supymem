"use client";

import Link from "@/components/chat/ChatLink";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  connectHubProvider,
  getConnectionsStatus,
  getOpsWorkspace,
  getGmailMailboxes,
  setDefaultGmailMailbox,
  disconnectGmailMailbox,
  getHubGmailVerification,
  syncHubProvider,
  revokeContentAccount,
  verifyHubGmail,
  type ConnectionsHubCard,
  type ConnectionsHubStatus,
  type GmailVerificationReceipt,
  type GmailMailbox,
  type GmailMailboxes,
} from "@/lib/opsApi";
import { mailboxCanSend } from "@/lib/gmailMailboxes";
import styles from "@/components/marketing/MarketingLab.module.css";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import { connectedAccountChannels, connectionActionLabel, connectionCopy, connectionSetupMessage, connectionState, disconnectToolkitAccounts } from "./connectionCopy";
import { founderConnectionMessage, founderSafeConnectionText } from "@/lib/connectionFounderCopy";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { ConnectTools } from "@/components/desktop/ConnectTools";
import { opsErrorMessage } from "@/lib/errorCopy";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { isLocalConsole } from "@/lib/localMode";
import { useFeatureVisibility } from "@/components/layout/LocalModeBoundary";
import type { ChatMessage } from "@/lib/api-types";
import { useIsOperator } from "@/lib/operatorSession";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { InboxBriefingConsent } from "./InboxBriefingConsent";
import { CodingAgentTiles } from "./CodingAgentTiles";
import { NAV_ICONS } from "@/components/layout/NavIcons";

const ORDER_FALLBACK = [
  "composio",
  "gmail",
  "google_calendar",
  "linkedin",
  "discord",
  "reddit",
  "x",
  "unipile",
  "hunter",
  "google_ads",
  "meta_ads",
] as const;

const CONNECTION_LOGOS: Record<string, string> = {
  gmail: "gmail", google_calendar: "googlecalendar", linkedin: "linkedin", reddit: "reddit",
  googlesheets: "googlesheets", cal: "cal", google_analytics: "google_analytics",
  google_search_console: "google_search_console", notion: "notion", stripe: "stripe",
  typeform: "typeform", calendly: "calendly", hubspot: "hubspot", slack: "slack",
  github: "github", googledrive: "googledrive", googledocs: "googledocs",
};

type HubOwner = { key: string; routeKey: string; subject: string; epoch: number; workspaceId: string; ventureId?: string; generation: number };
type Authorization = { name: string; url: string; owner: HubOwner; source: ConnectionsHubStatus };
const toolkitConnection = (card: ConnectionsHubCard) => connectionCopy(card).toolkit || null;
const connectionVisible = (card: ConnectionsHubCard) => connectionCopy(card).visible !== false;

export function ConnectionsHub() {
  const local = isLocalConsole();
  const featureVisible = useFeatureVisibility();
  const operationsScope = useOperationsScope();
  const operator = useIsOperator();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<ConnectionsHubStatus | null>(null);
  const [gmailReceipt, setGmailReceipt] = useState<GmailVerificationReceipt | null>(null);
  const [gmailMailboxes, setGmailMailboxes] = useState<GmailMailboxes | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [gmailError, setGmailError] = useState<string | null>(null);
  const [routeMissing, setRouteMissing] = useState(false);
  const [pendingAuth, setPendingAuth] = useState<Authorization | null>(null);
  const [loadedOwner, setLoadedOwner] = useState<HubOwner | null>(null);
  const [codingAgentsEnabled, setCodingAgentsEnabled] = useState(false);
  const [, setRetirement] = useState(0);
  const generation = useRef(0);
  const selection = useRef<HubOwner | null>(null);
  const listed = useRef<ConnectionsHubStatus | null>(null);
  const readSerial = useRef(0);
  const inFlight = useRef<object | null>(null);
  const subject = verifiedSessionSubject();
  const epoch = verifiedSessionEpoch();
  const routeWorkspace = searchParams.get("workspace") || undefined;
  const routeVenture = searchParams.get("venture") || undefined;
  const routeKey = JSON.stringify([routeWorkspace, routeVenture]);
  const route = useRef({ key: routeKey, workspaceId: routeWorkspace, ventureId: routeVenture });
  route.current = { key: routeKey, workspaceId: routeWorkspace, ventureId: routeVenture };
  const scopeKey = JSON.stringify([operationsScope?.workspaceId, operationsScope?.ventureId, operationsScope?.actorId, subject, epoch, routeKey]);
  if (selection.current?.key !== scopeKey) generation.current += 1;
  const owner: HubOwner | null = subject && operationsScope?.actorId === subject && operationsScope.workspaceId
    ? { key: scopeKey, routeKey, subject, epoch, workspaceId: operationsScope.workspaceId, ventureId: operationsScope.ventureId, generation: generation.current }
    : null;
  selection.current = owner;
  const liveRouteKey = () => {
    const params = new URL(window.location.href).searchParams;
    return JSON.stringify([params.get("workspace") || undefined, params.get("venture") || undefined]);
  };
  const current = (candidate: HubOwner | null) => Boolean(candidate && selection.current?.key === candidate.key &&
    candidate.generation === generation.current && candidate.subject === verifiedSessionSubject() &&
    candidate.epoch === verifiedSessionEpoch() && candidate.workspaceId === getOpsWorkspace() &&
    candidate.routeKey === route.current.key && candidate.routeKey === liveRouteKey() && (!route.current.workspaceId || route.current.workspaceId === candidate.workspaceId) &&
    (!route.current.ventureId || route.current.ventureId === candidate.ventureId));

  const refresh = useCallback(async (candidate: HubOwner | null = owner): Promise<boolean> => {
    if (!current(candidate)) return false;
    const ticket = ++readSerial.current;
    listed.current = null; setLoadedOwner(null); setPendingAuth(null); setError(null);
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const next = local ? await Promise.race([getConnectionsStatus(), new Promise<never>((_resolve, reject) => {
        timeout = setTimeout(() => reject(new Error("Your app connections did not respond. Open Settings > Keys and connections > Connected apps to check your connection key, then choose Reload connections here.")), 15_000);
      })]) : await getConnectionsStatus();
      if (!current(candidate) || ticket !== readSerial.current) return false;
      if (next.workspace_id !== candidate!.workspaceId || !next.connections || !Array.isArray(next.order) ||
          Object.entries(next.connections).some(([id, card]) => !card || card.id !== id)) {
        throw new Error("The connection status did not match this company workspace.");
      }
      listed.current = next; setStatus(next); setLoadedOwner(candidate); setGmailError(null);
      setRouteMissing(false);
      if (local) return true;
      const [saved, inboxes] = await Promise.allSettled([
        getHubGmailVerification(candidate!.ventureId), getGmailMailboxes(candidate!.workspaceId),
      ]);
      if (!current(candidate) || ticket !== readSerial.current) return false;
      setGmailReceipt(saved.status === "fulfilled" ? saved.value.receipt : null);
      setGmailMailboxes(inboxes.status === "fulfilled" ? inboxes.value : null);
      if (saved.status === "rejected" || inboxes.status === "rejected") {
        setGmailError("Couldn’t read your saved Gmail status. Refresh to try again. Your other connections are available.");
      }
      return true;
    } catch (err) {
      if (!current(candidate) || ticket !== readSerial.current) return false;
      setStatus(null); setGmailReceipt(null); setGmailMailboxes(null);
      const msg = opsErrorMessage(err, "Couldn’t load your connections. Refresh to try again.");
      if (msg.includes("404") || msg.includes("Not Found")) {
        setRouteMissing(true);
        setError(local ? "Open Keys and connections to check your app connection settings, then reload connections here."
          : "Connections are temporarily unavailable. Please try again in a few minutes, or contact support if it continues.");
      } else setError(msg);
      return false;
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
    }
  }, [scopeKey, owner?.generation, local]);

  useEffect(() => {
    const retire = () => {
      ++generation.current; ++readSerial.current; selection.current = null; listed.current = null; inFlight.current = null;
      // Effect replay must renew the captured owner even when the cleared state was already empty.
      setRetirement(value => value + 1);
      setLoadedOwner(null); setStatus(null); setGmailReceipt(null); setGmailMailboxes(null); setPendingAuth(null);
      setBusy(null); setBanner(null); setError(null); setGmailError(null); setRouteMissing(false);
    };
    window.addEventListener("supymem-session-changed", retire);
    return () => { retire(); window.removeEventListener("supymem-session-changed", retire); };
  }, []);

  useEffect(() => {
    listed.current = null; inFlight.current = null;
    setLoadedOwner(null); setStatus(null); setGmailReceipt(null); setGmailMailboxes(null); setPendingAuth(null);
    setBusy(null); setBanner(null); setError(null); setRouteMissing(false);
    void refresh();
  }, [refresh]);

  useEffect(() => {
    let live = true;
    const candidate = owner;
    setCodingAgentsEnabled(false);
    if (!current(candidate)) return;
    void api("/api/ui-config").then(result => {
      const data = result.data as { coding_agents_v1?: boolean } | null;
      if (live && current(candidate)) setCodingAgentsEnabled(result.ok && data?.coding_agents_v1 === true);
    }).catch(() => {});
    return () => { live = false; };
  }, [scopeKey, owner?.generation]);

  // A URL return marker is not provider or workspace authority. Only read the
  // currently selected company's saved status; explicit listed-card Sync stays available.
  useEffect(() => {
    if (!current(owner)) return;
    if (searchParams.get("oauth") || searchParams.get("connected") || searchParams.get("gmail")) {
      setBanner("Returned from authorization. Refresh this company’s saved connection status to check the result.");
    }
  }, [searchParams, scopeKey]);

  const ready = current(loadedOwner) && listed.current === status;
  const order = useMemo(() => {
    if (local) return ready ? status?.order ?? [] : [];
    return ready && status?.order?.length ? status.order : [...ORDER_FALLBACK];
  }, [status, ready, local]);

  const cards = useMemo(() => {
    const map = ready ? status?.connections || {} : {};
    return order
      .map((id) => map[id])
      .filter((c): c is ConnectionsHubCard => Boolean(c && (local || connectionVisible(c) && featureVisible("connections", c.id ?? ""))));
  }, [order, status, ready, local, featureVisible]);

  function admitCard(card: ConnectionsHubCard): HubOwner | null {
    if (!current(loadedOwner) || listed.current !== status || !card.id ||
        status?.connections[card.id] !== card || !status.order.includes(card.id)) return null;
    return loadedOwner;
  }

  async function onAction(card: ConnectionsHubCard, gmailIntent?: { intent: "add" | "reconnect"; mailbox_id?: string }) {
    const candidate = admitCard(card), id = card.id || "";
    if (!candidate || !card.action || inFlight.current) return;
    const token = {}; inFlight.current = token;
    const active = () => current(candidate) && inFlight.current === token;
    setBusy(id); setError(null); setBanner(null); setPendingAuth(null);
    try {
      if (!gmailIntent && card.action === "sync" && connectionState(card) !== "Needs reconnect") {
        const synced = await syncHubProvider(id);
        if (!active()) return;
        if (synced.ok === false) throw new Error("Provider sync was not confirmed.");
        if (await refresh(candidate) && active()) setBanner(`${card.name || id} connection status refreshed. Check the connection state below.`);
        return;
      }
      const scopedReturn = new URLSearchParams({ workspace: candidate.workspaceId });
      if (candidate.ventureId) scopedReturn.set("venture", candidate.ventureId);
      const started = gmailIntent
        ? await connectHubProvider(id, { return_to: `/connections?${scopedReturn}`, ...gmailIntent }, candidate.workspaceId)
        : await connectHubProvider(id, id === "gmail" || toolkitConnection(card) ? { return_to: `/connections?${scopedReturn}` } : undefined);
      if (!active()) return;
      const url = started.redirect_url || started.authorize_url;
      if (url) {
        if (listed.current !== status) return;
        const auth: Authorization = { name: card.name || id, url, owner: candidate, source: status! };
        setPendingAuth(auth);
        await openAuthorization(auth);
        return;
      }
      if (started.needs_you) {
        setBanner(founderConnectionMessage({ provider: id, message: started.message, note: started.note }) ||
          [connectionSetupMessage(card), started.docs ? "Open the setup guide for the next step." : null].filter(Boolean).join(" · "));
        await refresh(candidate);
        return;
      }
      if (await refresh(candidate) && active()) setBanner(`Updated ${card.name || id}. Check the saved connection state below.`);
    } catch (err) {
      if (active()) setError(opsErrorMessage(err, "Couldn’t change this connection. Refresh its saved status before trying again."));
    } finally {
      if (inFlight.current === token) { inFlight.current = null; if (current(candidate)) setBusy(null); }
    }
  }

  async function onMailbox(mailbox: GmailMailbox, action: "default" | "verify" | "disconnect") {
    const card = status?.connections.gmail;
    const candidate = card ? admitCard(card) : null;
    if (!candidate || inFlight.current || !gmailMailboxes?.mailboxes.includes(mailbox)) return;
    if (action !== "disconnect" && !mailboxCanSend(mailbox)) return;
    const token = {}; inFlight.current = token;
    const active = () => current(candidate) && inFlight.current === token;
    setBusy(`${action}:${mailbox.id}`); setError(null); setBanner(null); setPendingAuth(null);
    try {
      if (action === "verify") {
        const proof = await verifyHubGmail(candidate.ventureId, mailbox.id, candidate.workspaceId);
        if (!active()) return;
        if (!proof.ok || proof.status !== "verified" || proof.email?.toLowerCase() !== mailbox.email.toLowerCase()) {
          throw new Error(`You could not verify ${mailbox.email}. Refresh or reconnect this inbox and try again. No mail was sent.`);
        }
        setBanner(`You verified ${mailbox.email}. No mail was sent; sending still needs your approval.`);
        return;
      }
      if (action === "default") await setDefaultGmailMailbox(candidate.workspaceId, mailbox.id);
      else await disconnectGmailMailbox(candidate.workspaceId, mailbox.id, mailbox.updated_at);
      if (!active()) return;
      const saved = await getGmailMailboxes(candidate.workspaceId);
      if (!active()) return;
      const selected = saved?.mailboxes.find(item => item.id === mailbox.id);
      if (!saved || (action === "default" ? !selected?.is_default : selected && (mailboxCanSend(selected) || selected.needs_reconnect))) {
        throw new Error("Your inbox change could not be confirmed. Refresh your Gmail inboxes before trying again.");
      }
      if (await refresh(candidate) && active()) setBanner(action === "default"
        ? `You set ${mailbox.email} as your default Gmail inbox. Previously reviewed messages keep their sender.`
        : `You disconnected ${mailbox.email}. Your other Gmail inboxes stay connected.`);
    } catch (err) { if (active()) setError(opsErrorMessage(err, "Couldn’t confirm this inbox change. Refresh saved Gmail status before trying again.")); }
    finally { if (inFlight.current === token) { inFlight.current = null; if (current(candidate)) setBusy(null); } }
  }

  async function onDisconnect(card: ConnectionsHubCard) {
    const candidate = admitCard(card), id = card.id || "";
    const toolkit = toolkitConnection(card);
    if (!candidate || !(card.connected || card.needs_reconnect) || inFlight.current) return;
    if (toolkit && (!toolkit.accounts?.length || !current(candidate))) return;
    if (!toolkit && !candidate.ventureId) { setError("Choose your venture before disconnecting this account."); return; }
    const token = {}; inFlight.current = token;
    const active = () => current(candidate) && inFlight.current === token;
    setBusy(`disconnect:${id}`); setError(null); setBanner(null); setPendingAuth(null);
    try {
      if (toolkit) {
        if (!await disconnectToolkitAccounts(card, revokeContentAccount, active)) return;
      } else {
        const path = `/api/ventures/${encodeURIComponent(candidate.ventureId!)}/chat/confirm`;
        const prepareKey = crypto.randomUUID();
        const prepared = await api(path, { method: "POST", headers: { "Idempotency-Key": prepareKey },
          body: JSON.stringify({ action: "connections.disconnect", payload: { provider: id }, idempotency_key: prepareKey }) });
        if (!active()) return;
        const message = prepared.data as ChatMessage | null;
        const confirmation = Array.isArray(message?.actions) ? message.actions.find(action => action?.confirmation?.action === "connections.disconnect")?.confirmation : undefined;
        if (!prepared.ok || typeof message?.id !== "string" || !message.id || typeof confirmation?.interpretation !== "string" || !confirmation.interpretation ||
            !Array.isArray(confirmation.consequences) || confirmation.consequences.some(value => typeof value !== "string") ||
            !confirmation.resulting_state || confirmation.resulting_state.provider !== id) {
          throw new Error("Your disconnection could not be reviewed. Refresh your connection status and try again.");
        }
        if (!active()) return;
        const confirmKey = crypto.randomUUID();
        const confirmed = await api(path, { method: "POST", headers: { "Idempotency-Key": confirmKey },
          body: JSON.stringify({ action: "connections.disconnect", payload: confirmation.resulting_state, message_id: message.id, idempotency_key: confirmKey }) });
        if (!active()) return;
        const result = confirmed.data as ChatMessage | null;
        if (!confirmed.ok || result?.confirmed_action?.action !== "connections.disconnect" || result.confirmed_action.message_id !== message.id || result.confirmed_action.state !== "started") {
          throw new Error("Disconnection was not confirmed. Refresh your connection status before trying again.");
        }
      }
      if (await refresh(candidate) && active()) {
        const saved = listed.current?.connections[id];
        if (!saved || saved.connected !== false || saved.needs_reconnect) throw new Error("Your connection changed. Review it again before disconnecting.");
        setBanner(`You disconnected ${card.name || id}.`);
      }
    } catch (err) {
      if (active()) setError(opsErrorMessage(err, "Couldn’t confirm this disconnection. Refresh saved status before trying again."));
    } finally {
      if (inFlight.current === token) { inFlight.current = null; if (current(candidate)) setBusy(null); }
    }
  }

  async function onVerifyGmail() {
    const card = status?.connections.gmail;
    const candidate = card ? admitCard(card) : null;
    if (!candidate || !card?.connected || inFlight.current) return;
    const token = {}; inFlight.current = token;
    const active = () => current(candidate) && inFlight.current === token;
    setBusy("gmail"); setError(null); setBanner(null);
    try {
      const proof = await verifyHubGmail(candidate.ventureId);
      if (!active()) return;
      if (proof.receipt_saved) {
        const saved = await getHubGmailVerification(candidate.ventureId).catch(() => ({ receipt: null }));
        if (!active()) return;
        setGmailReceipt(saved.receipt);
      }
      if (proof.ok) setBanner(`Read-only Gmail profile verified${proof.email ? ` (${proof.email})` : ""}. No mail sent.${proof.receipt_saved ? " Receipt saved." : " Receipt could not be saved."}`);
      else if (proof.status === "account_mismatch") setBanner(`Gmail responded from a different account${proof.email ? ` (${proof.email})` : ""}. No mail sent. Reconnect the intended mailbox.`);
      else setBanner(`Read-only Gmail check ${proof.status.replaceAll("_", " ")}. No mail sent.${proof.receipt_saved ? " Receipt saved." : " Receipt could not be saved."}`);
    } catch { if (active()) setBanner("Gmail verification could not complete. No provider result was confirmed."); }
    finally { if (inFlight.current === token) { inFlight.current = null; if (current(candidate)) setBusy(null); } }
  }

  async function openPendingAuthorization() { if (pendingAuth) await openAuthorization(pendingAuth); }

  async function openAuthorization(admitted: Authorization) {
    if (!current(admitted.owner) || listed.current !== admitted.source) return;
    try {
      const parsed = new URL(admitted.url);
      if (isDesktopShell() && parsed.hostname === "connect.composio.dev" && parsed.pathname.startsWith("/link/")) {
        await desktopInvokeOrThrow("open_composio_connect_link", { url: admitted.url });
        if (current(admitted.owner)) setBanner("Opened in your browser. Approve, then come back and Refresh.");
        return;
      }
      const opened = window.open(admitted.url, "_blank", "noopener,noreferrer");
      if (!opened) throw new Error("The browser blocked the authorization tab.");
      if (current(admitted.owner)) setBanner("Opened in your browser. Approve, then come back and Refresh.");
    } catch { if (current(admitted.owner)) setBanner("Didn\u2019t open? Use Copy link below."); }
  }

  async function copyPendingAuthorization() {
    if (!pendingAuth || !current(pendingAuth.owner) || listed.current !== pendingAuth.source) return;
    const admitted = pendingAuth;
    try {
      await navigator.clipboard.writeText(admitted.url);
      if (current(admitted.owner)) setBanner("Link copied. Open it in your browser.");
    } catch { if (current(admitted.owner)) setBanner("Could not copy the link."); }
  }

  const readyCount = status?.ready_count ?? 0;
  const total = status?.total_count ?? cards.length;
  const accounts = connectedAccountChannels(cards);
  if (ready && gmailMailboxes) {
    accounts.total += gmailMailboxes.mailboxes.length - Number(cards.some(card => card.id === "gmail"));
    accounts.connected += gmailMailboxes.mailboxes.filter(mailboxCanSend).length - Number(Boolean(status?.connections.gmail?.connected));
  }
  const scope = new URLSearchParams();
  const workspaceId = loadedOwner?.workspaceId;
  const ventureId = loadedOwner?.ventureId;
  if (workspaceId) scope.set("workspace", workspaceId);
  if (ventureId) scope.set("venture", ventureId);
  const scopedHref = (path: string) => scope.size ? `${path}?${scope}` : path;

  if (!ready && local) return <p className="page-loading" role="status">{error || (owner ? "Loading your app connections… " : "Choose your venture to see its app connections. ")}
    {owner ? <button type="button" onClick={() => void refresh()}>Reload connections</button> : <Link href="/connections">Choose your venture</Link>}
    {error && <Link href="/settings/local">Keys and connections</Link>}
  </p>;
  if (!ready) return <p role="status">{error ? "Connection status could not be verified for this company. " : "Loading this company’s connections… "}<button type="button" onClick={() => void refresh()}>Reload connections</button></p>;

  const visible = cards.filter((card) => (local || operator || Boolean(CONNECTION_LOGOS[card.id || ""])) && !(gmailMailboxes && card.id === "gmail"));
  const connected = visible.filter(card => connectionState(card) === "Connected");
  const available = visible.filter(card => !connected.includes(card));
  const primary = available.find(card => Boolean(card.action));

  function renderMailbox(mailbox: GmailMailbox) {
    const connected = mailboxCanSend(mailbox);
    return <article key={mailbox.id} aria-label={`Gmail ${mailbox.email}`} className={`${styles.connector} ${styles.connectionTile} ${connected ? styles.connectionConnected : ""}`}>
      <div className={styles.connectionHeading}><img src="/connections/gmail.svg" width={36} height={36} alt="" /><h3>{mailbox.email}</h3></div>
      <span role="status" className={`${styles.pill} ${connected ? styles.connectionBadge : styles.pillNeed}`}>{connected ? "Connected" : mailbox.needs_reconnect ? "Needs reconnect" : "Not connected"}</span>
      {mailbox.is_default && <span className={styles.pill}>Default</span>}
      <p className={styles.muted}>{mailbox.sent_today} of {mailbox.daily_send_limit} sends used today. Sending still needs your approval.</p>
      <InboxBriefingConsent key={`${loadedOwner?.key}:${loadedOwner?.generation}:${mailbox.id}`} ventureId={ventureId} workspaceId={workspaceId} mailboxId={mailbox.id} email={mailbox.email} disabled={Boolean(busy)} />
      {connected && !mailbox.is_default && <button type="button" className={`${styles.btn} ${styles.btnGhost}`} disabled={Boolean(busy)} onClick={() => void onMailbox(mailbox, "default")}>Make default</button>}
      {connected && <button type="button" data-usage-control="gmail_verify" className={`${styles.btn} ${styles.btnGhost}`} disabled={Boolean(busy)} onClick={() => void onMailbox(mailbox, "verify")}>{busy === `verify:${mailbox.id}` ? "Checking…" : "Verify (read only)"}</button>}
      {mailbox.needs_reconnect && <button type="button" data-usage-control="connections_action" className={styles.btn} disabled={Boolean(busy)} onClick={() => {
        if (gmailMailboxes?.mailboxes.includes(mailbox) && status?.connections.gmail) void onAction(status.connections.gmail, { intent: "reconnect", mailbox_id: mailbox.id });
      }}>Reconnect</button>}
      {(connected || mailbox.needs_reconnect) && <button type="button" data-usage-control="connections_disconnect" className={`${styles.btn} ${styles.btnGhost}`} disabled={Boolean(busy)} onClick={() => void onMailbox(mailbox, "disconnect")}>Disconnect</button>}
    </article>;
  }

  function renderCard(card: ConnectionsHubCard) {
    const ok = Boolean(card.ready ?? card.ok);
    const id = card.id || card.name || "unknown";
    const localMail = local && id === "smtp";
    const isGmail = id === "gmail";
    const toolkit = toolkitConnection(card);
    const appSetup = local && toolkit && !card.configured;
    const refreshing = card.action === "sync" && connectionState(card) !== "Needs reconnect";
    const state = pendingAuth?.name === (card.name || id) || busy === id && !refreshing ? "Connecting" : connectionState(card);
    const connected = state === "Connected";
    const reconnect = state === "Needs reconnect";
    const label = state === "Ready to connect" ? "Not connected" : state;
    const disconnecting = busy === `disconnect:${id}`;
    return (
      <article key={id} aria-label={card.name || id} aria-busy={busy === id || disconnecting || state === "Connecting"} className={`${styles.connector} ${styles.connectionTile} ${connected ? styles.connectionConnected : ""}`}>
        <div className={styles.connectionHeading}>
          {CONNECTION_LOGOS[id] ? <img src={`/connections/${CONNECTION_LOGOS[id]}.svg`} width={36} height={36} alt="" /> : local ? <NAV_ICONS.connections /> : null}
          <h3>{card.name || id}</h3>
        </div>
        <span role="status" className={`${styles.pill} ${connected ? styles.connectionBadge : reconnect ? styles.pillNeed : styles.pillOff}`}>
          {connected ? <span aria-hidden="true">✓</span> : null}{label}
        </span>
        {card.handle || card.account_email ? <span className={styles.muted}>{card.handle || card.account_email}</span> : null}
        {operator && card.auth_via ? <span className={styles.muted}>via {card.auth_via}</span> : null}
        {operator && card.last_sync ? <span className={styles.muted}>last sync {String(card.last_sync).slice(0, 19)}</span> : null}
        <span className={styles.muted}>{connectionCopy(card).purpose}</span>
        {operator && !ok ? <span className={styles.muted}>{connectionSetupMessage(card)}</span> : null}
        {operator && (() => {
          const note = founderSafeConnectionText(card.note)
            || (!ok && !card.connectable && (card.note || card.paused_reason)
              ? founderConnectionMessage({ provider: id, note: card.note, message: card.paused_reason })
              : null);
          return note ? <span className={styles.muted}>{note}</span> : null;
        })()}
        {operator && card.docs && !ok ? <a href={card.docs} target="_blank" rel="noreferrer" className={styles.muted}>Docs →</a> : null}
        {localMail ? <Link className={styles.btn} href="/settings/local#smtp-title">Open your mail settings</Link>
          : appSetup ? <Link className={styles.btn} href="/settings/local#composio-title">Set up your app connection</Link>
          : card.action && (operator || !card.connected || reconnect) ? (
          <button
            type="button"
            data-usage-control="connections_action"
            className={`${styles.btn} ${card === primary ? "" : styles.btnGhost}`}
            style={{ marginTop: "0.35rem" }}
            disabled={Boolean(busy)}
            onClick={() => void onAction(card)}
          >
            {busy === id ? refreshing ? "Refreshing…" : "Connecting…" : card.connected && !reconnect ? connectionActionLabel(card) : reconnect ? "Reconnect" : "Connect"}
          </button>
        ) : null}
        {!localMail && (toolkit?.accounts?.length || !toolkit) && (card.connected || card.needs_reconnect) ? (
          <button type="button" data-usage-control="connections_disconnect" className={`${styles.btn} ${styles.btnGhost}`} disabled={Boolean(busy)} onClick={() => void onDisconnect(card)}>
            {disconnecting ? "Disconnecting…" : "Disconnect"}
          </button>
        ) : null}
        {pendingAuth && pendingAuth.name === (card.name || id) ? (
          <button type="button" className={styles.muted} style={{ background: "none", border: 0, padding: 0, textAlign: "left", textDecoration: "underline", cursor: "pointer" }} onClick={() => void copyPendingAuthorization()}>
            Didn’t open? Copy link
          </button>
        ) : null}
        {isGmail && card.connected ? (
          <button
            type="button"
            data-usage-control="gmail_verify"
            className={`${styles.btn} ${styles.btnGhost}`}
            style={{ marginTop: "0.35rem" }}
            disabled={Boolean(busy)}
            onClick={() => void onVerifyGmail()}
          >
            {busy === "gmail" ? "Checking…" : "Verify Gmail (read only)"}
          </button>
        ) : null}
        {isGmail ? <InboxBriefingConsent key={`${loadedOwner?.key}:${loadedOwner?.generation}`} ventureId={ventureId} workspaceId={workspaceId} mailboxId={card.mailbox_id} email={card.account_email} disabled={Boolean(busy)} /> : null}
        {operator && isGmail && gmailReceipt ? <span className={styles.muted} role="status">
            Historical read-only profile check{operationsScope?.ventureId ? " for this venture" : " for this workspace"}: {gmailReceipt.status.replaceAll("_", " ")}
            {gmailReceipt.checked_at ? ` · ${new Date(gmailReceipt.checked_at).toLocaleString()}` : ""}.
            {operator && gmailReceipt.log_id ? ` Composio log ID: ${gmailReceipt.log_id}.` : ""} No mail sent; sending requires a separate review.
          </span> : null}
      </article>
    );
  }

  return (
    <div className={styles.wrap}>
      {routeMissing ? (
        <div className={styles.banner}>
          <span className={`${styles.pill} ${styles.pillErr}`}>Unavailable</span>
          <span>Connections are temporarily unavailable. Try again in a few minutes.</span>
        </div>
      ) : null}

      <div className={styles.banner}>
        <span className={`${styles.pill} ${accounts.connected > 0 ? styles.pillLive : styles.pillWarn}`}>
          {accounts.connected} of {accounts.total} connected
        </span>
        {operator && <span className={`${styles.pill} ${readyCount === total && total > 0 ? styles.pillLive : styles.pillWarn}`}>
          {readyCount}/{total} services ready
        </span>}
        <button
          type="button"
          data-usage-control="connections_refresh"
          className={`${styles.btn} ${styles.btnGhost}`}
          disabled={Boolean(busy)}
          onClick={() => void refresh()}
        >
          Refresh
        </button>
        <Link href={scopedHref("/gtm")}>Go to market →</Link>
      </div>

      {banner ? (
        <div className={styles.banner}>
          <span className={`${styles.pill} ${styles.pillNeed}`}>Note</span>
          <span>{banner}</span>
        </div>
      ) : null}
      {error ? (
        <ErrorNotice error={error} source="connections.hub" as="div" className={styles.banner}>
          <span className={`${styles.pill} ${styles.pillErr}`}>Error</span>
          <span>{error}</span>
        </ErrorNotice>
      ) : null}

      {gmailError && current(loadedOwner) && <ErrorNotice error={gmailError} source="connections.gmail" as="div" className={styles.banner} />}
      {gmailMailboxes ? <p className={styles.muted}>Connecting does not approve messages, posts, or payments. You can choose a Gmail inbox for each venture or message. Disconnecting one inbox leaves your other inboxes connected. Sending still needs your approval.</p>
        : <p className={styles.muted}>Connecting does not approve messages, posts, or payments. You connect Gmail once for all ventures you own. Disconnecting Gmail applies to all of them. Sending still needs your approval.</p>}
      <section aria-labelledby="connected-heading"><h2 id="connected-heading">Connected</h2>
        {connected.length || gmailMailboxes?.mailboxes.some(mailboxCanSend) ? <div className={styles.connectionsGrid}>{gmailMailboxes?.mailboxes.filter(mailboxCanSend).map(renderMailbox)}{connected.map(renderCard)}</div> : <p className={styles.muted}>You have not connected any apps yet.</p>}
      </section>
      <section aria-labelledby="available-heading"><h2 id="available-heading">Available</h2>
        {available.length || gmailMailboxes ? <div className={styles.connectionsGrid}>{gmailMailboxes?.mailboxes.filter(mailbox => !mailboxCanSend(mailbox)).map(renderMailbox)}{gmailMailboxes && <article className={`${styles.connector} ${styles.connectionTile}`} aria-label="Add another Gmail">
          <h3>Add another Gmail</h3><p className={styles.muted}>Connect another inbox. You choose the sender before approving each message.</p>
          <button type="button" data-usage-control="connections_action" className={styles.btn} disabled={Boolean(busy) || !status?.connections.gmail?.action} onClick={() => { if (status?.connections.gmail) void onAction(status.connections.gmail, { intent: "add" }); }}>Add another Gmail</button>
        </article>}{available.map(renderCard)}</div> : <p className={styles.muted}>You have connected every available app.</p>}
      </section>

      {!local && ready && codingAgentsEnabled && <CodingAgentTiles key={`${loadedOwner?.key}:${loadedOwner?.generation}`} />}

      {operator && <details>
        <summary>Coding tools · optional</summary>
        <p className={styles.muted}>
          Claude Code and Codex use a signed-in CLI on your Mac
          {operationsScope?.ventureId || ventureId ? (
            <> · model keys in <Link href={`/v/${encodeURIComponent(operationsScope?.ventureId || ventureId || "")}/settings`}>Settings</Link></>
          ) : null}.
        </p>
        <ConnectTools key={`${loadedOwner?.key}:${loadedOwner?.generation}`} />
      </details>}

      {operator && (status?.blockers || []).length ? (
        <section className={styles.card}>
          <div className={styles.cardHead}>
            <h2>Blockers</h2>
            <p>Things that need your attention</p>
          </div>
          <div className={styles.blockList}>
            {(status?.blockers || []).map((b, i) => (
              <div key={`${b.code || "b"}-${i}`} className={styles.blockItem}>
                <span className={styles.pillNeed}>
                  {b.severity || "NeedsYou"}
                </span>{" "}
                {founderConnectionMessage({
                  code: b.code,
                  message: b.message,
                  provider: typeof b.code === "string" ? b.code.replace(/_missing$|_env$/, "") : undefined,
                })}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <p className={styles.muted}>
        {operator ? status?.note || "Deep-linked from Marketing Lab, Community, and GTM readiness cards." : null}
      </p>
    </div>
  );
}
