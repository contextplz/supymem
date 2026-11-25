"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { ChatAnchor } from "@/components/chat/ChatLink";
import { api, downloadFile, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { safeEvidenceUrl, type ReachRoute } from "@/lib/gtmJourneyApi";
import { reachAction } from "@/lib/reachableLeads";
import { founderAssetsClient } from "@/lib/founderAssetsApi";
import { serverReason } from "@/lib/serverReason";
import { ImportContacts } from "./ImportContacts";
import { CopyDraft } from "./OutreachDraft";
import { useFeatureVisibility, useLocalMode } from "@/components/layout/LocalModeBoundary";
import styles from "@/components/gtm/GtmJourney.module.css";

type EmailItem = { doc_id?: string; subject?: string; body?: string; truth_state?: string };
export type Lead = {
  id: string; contact_id?: string | null; name: string; role?: string; company?: string; source: "library" | "web" | "imported";
  source_url?: string; website?: string; email?: string; company_inbox: boolean; can_draft: boolean;
  linkedin_url?: string; x_handle?: string; phone?: string; routes?: ReachRoute[]; status: string; created_at: string;
  contact_page?: string; message_draft?: string; outreach_restricted?: boolean;
  why?: string; notes?: string; draft?: EmailItem; outreach?: EmailItem[]; truth_basis?: string; interest_note?: string;
};
type View = { venture_id: string; workspace_id: string; items: Lead[]; total: number;
  receipt?: { kind?: string; body_md?: string }; outreach_next_step?: { reason?: string; body_md?: string } };
const STATUS_LABELS = new Map([
  ["suggested", "Suggested lead"], ["verified", "Address found in saved source"], ["drafted", "Email drafted"],
  ["approved", "Approved to send"], ["queued", "Scheduled to send"], ["sent", "Sent"],
  ["reply received", "Reply received"], ["positive reply", "Positive reply"], ["meeting booked", "Meeting booked"],
  ["delivery unconfirmed", "Delivery not confirmed"], ["opted out", "Opted out"], ["bounced", "Email bounced"],
]);
const CONTACTED = new Set(["sent", "reply received", "positive reply", "meeting booked"]);

export function filterLeads(leads: Lead[], query: string, source: string, status: string): Lead[] {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return leads.filter(lead => (!source || lead.source === source) && (!status || lead.status === status)
    && terms.every(term => [lead.name, lead.role, lead.company, lead.email, lead.why, lead.notes].join(" ").toLocaleLowerCase().includes(term)));
}

export function sourceLabel(lead: Lead): string {
  if (lead.source === "library") return "Lead library";
  if (lead.source === "imported") return "Imported";
  const url = safeEvidenceUrl(lead.source_url);
  return url ? `Found on the web ${new URL(url).host}` : "Found on the web";
}

export function reachLinks(lead: Lead): { label: string; href: string }[] {
  const links = [];
  if (lead.email && lead.can_draft) links.push({ label: lead.company_inbox ? "Company inbox" : "Email", href: `mailto:${encodeURIComponent(lead.email)}` });
  const linkedin = safeEvidenceUrl(lead.linkedin_url);
  if (linkedin) links.push({ label: "LinkedIn", href: linkedin });
  if (lead.x_handle && /^@?[A-Za-z0-9_]{1,15}$/.test(lead.x_handle)) links.push({ label: "X", href: `https://x.com/${lead.x_handle.replace(/^@/, "")}` });
  if (typeof lead.phone === "string" && /^[+\d() .-]{7,30}$/.test(lead.phone)) links.push({ label: "Phone", href: `tel:${lead.phone.replace(/[^+\d]/g, "")}` });
  for (const route of lead.routes || []) {
    if (route.kind === "email") continue;
    const action = reachAction({ name: lead.name, company: lead.company || "", draft: "", connection_note: "" }, route);
    if (action && !links.some(link => link.href === action.url)) links.push({ label: action.label, href: action.url });
  }
  return links;
}

export function LeadsList({ ventureId }: { ventureId: string }) {
  const local = useLocalMode();
  const libraryVisible = useFeatureVisibility()("actions", "gtm.library_claim");
  const [loadedView, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const generation = useRef(0);
  const actionPending = useRef(false);
  const epoch = useRef(verifiedSessionEpoch());
  const session = verifiedSessionEpoch();
  const path = `/api/ventures/${encodeURIComponent(ventureId)}/leads/contacts`;
  const view = loadedView?.venture_id === ventureId && epoch.current === session ? loadedView : null;
  const load = useCallback(async () => {
    const ticket = ++generation.current;
    setBusy(true);
    setError("");
    try {
      await ensureSession();
      if (ticket !== generation.current) return;
      const sessionEpoch = verifiedSessionEpoch();
      const result = await api(path);
      if (ticket !== generation.current || sessionEpoch !== verifiedSessionEpoch()) return;
      const data = result.data as View | null;
      if (result.ok && data?.venture_id === ventureId && Array.isArray(data.items)) { epoch.current = sessionEpoch; setView(data); setError(""); }
      else { if (result.ok || [401, 403, 404, 423].includes(result.status)) setView(null); setError(serverReason(result, "Could not load your leads. Refresh to try again.")); }
    } catch {
      if (ticket === generation.current) { setError("Could not load your leads. Try again."); }
    } finally {
      if (ticket === generation.current) setBusy(false);
    }
  }, [path, ventureId]);

  async function act(lead: Lead, kind: "draft" | "find-contact" | "replied" | "booked" | "remove") {
    if (!view || busy || actionPending.current || epoch.current !== verifiedSessionEpoch()) return;
    if (kind === "remove") {
      window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action: "gtm.eligibility",
        payload: { contact_id: lead.contact_id || lead.id, skip: true, remove: true }, message: `Remove ${lead.name} from my leads` } }));
      return;
    }
    actionPending.current = true;
    setBusy(true); setError("");
    const ticket = ++generation.current;
    const sessionEpoch = verifiedSessionEpoch();
    const history = kind === "replied" || kind === "booked";
    const retry = kind === "find-contact" ? "Open the company website in Leads to look for a public contact page, or select Find a contact to try again."
      : "Refresh Leads, open the contact and try again.";
    try {
      const result = await api(`${path}/${encodeURIComponent(lead.id)}/${history ? "history" : kind}`, {
        method: history ? "PATCH" : "POST",
        ...(history ? { body: JSON.stringify({ status: kind }) } : {}),
      });
      if (ticket !== generation.current || sessionEpoch !== verifiedSessionEpoch()) return;
      const data = result.data as View | null;
      if (result.ok && data?.venture_id === ventureId && Array.isArray(data.items)) setView(data);
      else {
        const detail = (result.data as { detail?: { message?: unknown } } | null)?.detail?.message;
        setError(serverReason(result, typeof detail === "string" && detail.trim() ? detail : retry));
      }
    } catch {
      if (ticket === generation.current && sessionEpoch === verifiedSessionEpoch()) setError(retry);
    } finally {
      actionPending.current = false;
      if (ticket === generation.current && sessionEpoch === verifiedSessionEpoch()) setBusy(false);
    }
  }

  async function exportLeads() {
    if (!view || busy || actionPending.current) return;
    actionPending.current = true;
    setBusy(true); setError("");
    const sessionEpoch = verifiedSessionEpoch();
    try {
      if (!await downloadFile(`${path}/export`, "leads.csv") && sessionEpoch === verifiedSessionEpoch()) {
        setError("Select Refresh leads, then try Export leads CSV again. Keep your exported contacts private.");
      }
    } catch {
      if (sessionEpoch === verifiedSessionEpoch()) setError("Select Refresh leads, then try Export leads CSV again.");
    } finally {
      actionPending.current = false;
      if (sessionEpoch === verifiedSessionEpoch()) setBusy(false);
    }
  }

  useEffect(() => {
    void load();
    const sessionChanged = () => { setView(null); setError(""); setQuery(""); setSource(""); setStatus(""); void load(); };
    const actionDone = (event: Event) => { if ((event as CustomEvent).detail?.ventureId === ventureId) void load(); };
    window.addEventListener("supymem-session-changed", sessionChanged);
    window.addEventListener("supymem-chat-action-done", actionDone);
    return () => { ++generation.current; window.removeEventListener("supymem-session-changed", sessionChanged); window.removeEventListener("supymem-chat-action-done", actionDone); };
  }, [load, ventureId]);

  const shown = view ? filterLeads(view.items, query, source, status) : [];
  const actorId = verifiedSessionSubject();
  const owner = view?.workspace_id && actorId ? { actorId, workspaceId: view.workspace_id, ventureId } : null;
  return <div className={styles.wrap}>
    <p className={styles.muted}>{libraryVisible ? "Your saved leads from the lead library, your web searches and your imports." : "Your saved leads from your web searches and your imports."} Open a person to review their details. Nothing is sent from this list.</p>
    <ErrorNotice error={error} source="venture_leads" />
    {owner && <ImportContacts key={JSON.stringify(owner)} owner={owner} disabled={false} save={async (preview, attested, basis, note) => {
      const sessionEpoch = verifiedSessionEpoch();
      const imported = await founderAssetsClient.importContacts(owner, preview, attested, basis, note);
      if (sessionEpoch === verifiedSessionEpoch()) await load();
      return imported;
    }} />}
    {view?.receipt?.body_md && <p role="status">{view.receipt.body_md}</p>}
    {view?.outreach_next_step?.body_md && <section aria-label="Your next step">
      <h2>Your next step</h2><p>{view.outreach_next_step.body_md}</p>
      {view.outreach_next_step.reason === "cap_reached" && <p>Open chat and ask me to review your daily send limit. Your emails stay saved.</p>}
    </section>}
    <div className={styles.row}>
      <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={busy} aria-busy={busy} onClick={() => void load()}>Refresh leads</button>
      {view && <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={busy || !view.total} onClick={() => void exportLeads()}>Export leads CSV</button>}
      {view && <span role="status">{shown.length} of {view.total} leads</span>}
    </div>
    {busy && <p role="status">{view ? "Refreshing your leads. Please wait." : "Loading your leads..."}</p>}
    {view && view.total === 0 ? <section>
      <p>You have no saved leads yet. Ask Charlie where to find your customers, or import contacts you have permission to contact.</p>
      <button type="button" className={styles.button} disabled={busy} onClick={() => {
        if (epoch.current !== verifiedSessionEpoch()) return;
        window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action: "gtm.find", payload: { prepare: true, run: false } } }));
      }}>Find customers with Charlie</button>
    </section> : view && <>
      <div className={styles.row}>
        <label className={styles.label}>Search leads<input type="search" className={styles.input} value={query} onChange={event => setQuery(event.target.value)} /></label>
        <label className={styles.label}>Source<select className={styles.input} value={source} onChange={event => setSource(event.target.value)}>
          <option value="">All sources</option>{libraryVisible && <option value="library">Lead library</option>}<option value="web">Found on the web</option><option value="imported">Imported</option>
        </select></label>
        <label className={styles.label}>Status<select className={styles.input} value={status} onChange={event => setStatus(event.target.value)}>
          <option value="">All statuses</option>{Array.from(STATUS_LABELS, ([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
      </div>
      {!shown.length && <p>No leads match your filters. <button type="button" className={`${styles.button} ${styles.secondary}`} onClick={() => { setQuery(""); setSource(""); setStatus(""); }}>Clear filters</button></p>}
      <ul className={styles.prospects} aria-label="Your leads">{shown.map(lead => {
        const manual = local && lead.source === "web" && !lead.email && !lead.outreach_restricted && !["opted out", "bounced"].includes(lead.status);
        const contactRoute = lead.routes?.find(route => (route.kind === "contact_form" || route.kind === "website" && route.company_contact) && safeEvidenceUrl(route.url));
        const checkedPage = manual && safeEvidenceUrl(lead.contact_page);
        const contactPage = checkedPage || (manual && safeEvidenceUrl(contactRoute?.url));
        const canFind = manual && !checkedPage && safeEvidenceUrl(lead.website);
        const canDraftMessage = Boolean(checkedPage);
        return <li key={lead.id} className={styles.prospect}>
        <details className={styles.details}>
          <summary><strong>{lead.name}</strong>{lead.role ? `, ${lead.role}` : ""}{lead.company && lead.company.trim().toLocaleLowerCase() !== lead.name.trim().toLocaleLowerCase() ? ` at ${lead.company}` : ""}</summary>
          <section aria-label={`Details for ${lead.name}`}>
            <p>Outreach status: <strong>{STATUS_LABELS.get(lead.status) ?? "Status not recorded"}</strong>{lead.truth_basis ? ` (${lead.truth_basis})` : ""}</p>
            {lead.status === "suggested" && <p>You have a suggested prospect, not a verified contact or permission to send.</p>}
            {lead.status === "verified" && <p>Your saved source contains this contact address. This is not mailbox delivery verification or permission to send.</p>}
            {lead.interest_note && <p>{lead.interest_note}</p>}
            {lead.why && <p><strong>Why this lead matches:</strong> {lead.why}</p>}
            {safeEvidenceUrl(lead.website) && <p><ChatAnchor href={safeEvidenceUrl(lead.website)} aria-label={`Company website for ${lead.name}`} target="_blank" rel="noopener noreferrer">Company website</ChatAnchor></p>}
            {safeEvidenceUrl(lead.source_url) && <p><ChatAnchor href={safeEvidenceUrl(lead.source_url)} aria-label={`Saved source for ${lead.name}`} target="_blank" rel="noopener noreferrer">Saved source</ChatAnchor></p>}
            {lead.email && <p>Email: {lead.email}{lead.company_inbox ? " (company inbox)" : ""}</p>}
            {lead.notes && <p className={styles.draftBody}>Notes: {lead.notes}</p>}
            {(lead.outreach ?? (lead.draft ? [lead.draft] : [])).map((email, index) => <div key={email.doc_id || index} className={styles.draft}>
              <h3>{!email.truth_state || email.truth_state === "drafted" ? "Email draft" : "Email"}</h3>
              <p>Status: {STATUS_LABELS.get(email.truth_state || "drafted") ?? "Status not recorded"}</p><strong>{email.subject}</strong><p className={styles.draftBody}>{email.body}</p>
            </div>)}
            {lead.outreach_restricted && <p>This website asks not to receive outreach. Choose another lead.</p>}
            {canDraftMessage && lead.message_draft && <CopyDraft title="Message draft" text={lead.message_draft} disabled={busy} current={() => session === verifiedSessionEpoch()} />}
            {(contactPage || canFind || canDraftMessage || lead.contact_id !== null) && <div className={styles.row}>
              {contactPage && <ChatAnchor href={contactPage} aria-label={`Open contact page for ${lead.name}`} target="_blank" rel="noopener noreferrer">Open contact page</ChatAnchor>}
              {canFind && <button type="button" className={styles.button} disabled={busy} aria-label={`Find a contact for ${lead.name}`} onClick={() => void act(lead, "find-contact")}>Find a contact</button>}
              {canDraftMessage && !lead.message_draft && <button type="button" className={styles.button} disabled={busy} aria-label={`Draft a message for ${lead.name}`} onClick={() => void act(lead, "draft")}>Draft a message</button>}
              {lead.contact_id !== null && <>
                {lead.can_draft && <button type="button" className={styles.button} disabled={busy} aria-label={`Draft email for ${lead.name}`} onClick={() => void act(lead, "draft")}>Draft email</button>}
                {CONTACTED.has(lead.status) && <>
                  <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={busy || lead.status !== "sent"} aria-label={`Mark replied for ${lead.name}`} onClick={() => void act(lead, "replied")}>Mark replied</button>
                  <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={busy || lead.status === "meeting booked"} aria-label={`Mark booked for ${lead.name}`} onClick={() => void act(lead, "booked")}>Mark booked</button>
                </>}
                <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={busy} aria-label={`Remove ${lead.name} from leads`} onClick={() => void act(lead, "remove")}>Remove from leads</button>
              </>}
            </div>}
          </section>
        </details>
        <p>{sourceLabel(lead)} <span className={styles.tag}>{STATUS_LABELS.get(lead.status) ?? "Status not recorded"}</span></p>
        <p className={styles.muted}>Added {new Date(lead.created_at).toLocaleDateString()}</p>
        {!lead.outreach_restricted && <div className={styles.row}>{reachLinks(lead).filter(link => link.href !== contactPage).map(link => <ChatAnchor key={link.label} href={link.href} target={/^https?:/.test(link.href) ? "_blank" : undefined} rel="noopener noreferrer">{link.label}</ChatAnchor>)}</div>}
      </li>;
      })}</ul>
    </>}
  </div>;
}

