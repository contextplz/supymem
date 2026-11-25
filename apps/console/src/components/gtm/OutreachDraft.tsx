"use client";

import { useState } from "react";
import { safeEvidenceUrl, type JourneyDraft, type JourneyDraftEdits } from "@/lib/gtmJourneyApi";
import { mailboxCanSend, type GmailMailbox } from "@/lib/gmailMailboxes";
import { guessedEmailWarning } from "@/lib/reachableLeads";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "./GtmJourney.module.css";

export function CopyDraft({ title, text, disabled, current, buttonLabel = "Copy draft" }: { title: string; text: string; disabled: boolean; current: () => boolean; buttonLabel?: string }) {
  const [status, setStatus] = useState("");
  return <article><h3>{title}</h3><p className={styles.draftBody}>{text}</p><button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled} onClick={async () => {
    if (disabled || !current()) return;
    try { await navigator.clipboard.writeText(text); if (current()) setStatus("Draft copied. Review the rules and permission before using it. Nothing was sent."); }
    catch { if (current()) setStatus("Could not copy. Select the draft text to copy it manually."); }
  }}>{buttonLabel}</button>{status && <p role="status">{status}</p>}</article>;
}


export function Draft({ draft, disabled, current, save, mailboxes }: { draft: JourneyDraft; disabled: boolean; current: () => boolean; save: (decision: "approved" | "skipped" | "draft", edits?: JourneyDraftEdits, expectedRevision?: string) => Promise<boolean | undefined>; mailboxes?: GmailMailbox[] }) {
  const [editing, setEditing] = useState(false);
  const [subject, setSubject] = useState(draft.subject);
  const [body, setBody] = useState(draft.body);
  const [editingRevision, setEditingRevision] = useState(draft.revision);
  const [copyStatus, setCopyStatus] = useState("");
  const [guessReviewed, setGuessReviewed] = useState(false);
  const [senderChanging, setSenderChanging] = useState(false);
  const warning = guessedEmailWarning(draft.email_route ?? undefined);
  const sent = draft.state === "sent";
  const editable = draft.state === "draft";
  const sender = mailboxes?.find(mailbox => mailbox.id === draft.mailbox_id);
  const senderUnavailable = mailboxes !== undefined && draft.channel !== "manual" && (!sender || !mailboxCanSend(sender));
  const senderLocked = draft.sender_locked || draft.kind === "reply" || draft.day === 3 || draft.day === 6;
  return <article className={styles.draft}><div className={styles.row}><strong>{draft.to || "Awaiting a recipient"}</strong><span className={styles.tag}>{sent ? "Sent" : draft.review_state === "approved" ? "Approved" : "Needs review"}</span>{(draft.kind === "reply" || draft.day === 3 || draft.day === 6) && draft.channel !== "manual" && <span className={styles.tag}>{draft.kind === "reply" ? "Suggested reply" : "Follow-up"}</span>}</div>
    {mailboxes && draft.channel !== "manual" && <>
      <p className={styles.muted}>Sender: {draft.sender?.email || sender?.email || "Unavailable"}</p>
      {editable && !senderLocked && <label className={styles.label}>Send from<select className={styles.input} value={draft.mailbox_id || ""} disabled={disabled || senderChanging || editing} onChange={async event => {
        const mailboxId = event.target.value;
        if (disabled || senderChanging || editing || !current() || mailboxId === draft.mailbox_id || !mailboxes.some(mailbox => mailbox.id === mailboxId && mailboxCanSend(mailbox))) return;
        setSenderChanging(true);
        try { await save("draft", { mailbox_id: mailboxId }, draft.revision); }
        finally { if (current()) setSenderChanging(false); }
      }}>
        {!sender && <option value={draft.mailbox_id || ""} disabled>{draft.sender?.email || "Choose an inbox"}</option>}
        {mailboxes.map(mailbox => <option key={mailbox.id} value={mailbox.id} disabled={!mailboxCanSend(mailbox)}>{mailbox.email}{mailbox.needs_reconnect ? " (needs reconnect)" : !mailboxCanSend(mailbox) ? " (unavailable)" : ""}</option>)}
      </select></label>}
      <p className={styles.muted}>{senderLocked || !editable ? "This message keeps the inbox from its original conversation or review." : "Changing your sender clears approval. Review the new sender and exact message before approving again."}</p>
      {senderUnavailable && !sent && <ErrorNotice source="gtm.outreach.sender" error="Your selected inbox is unavailable. Reconnect it in Connections; your other inboxes will not be used instead." />}
    </>}
    {draft.awaiting_recipient && <p className={styles.muted}>Add an evidence-backed prospect before approving an email. Nothing is sent.</p>}
    {warning && <label><input type="checkbox" checked={guessReviewed} onChange={event => setGuessReviewed(event.target.checked)} />{warning} I understand; I will review before sending.</label>}
    {safeEvidenceUrl(draft.source_url ?? undefined) && <p className={styles.muted}>Source: <a href={safeEvidenceUrl(draft.source_url ?? undefined)} target="_blank" rel="noopener noreferrer">{draft.source_url}</a></p>}
    {draft.founder_import && <p className={styles.muted}>Source: Your imported contacts</p>}
    {safeEvidenceUrl(draft.channel_url ?? undefined) && <a href={safeEvidenceUrl(draft.channel_url ?? undefined)} target="_blank" rel="noopener noreferrer">Open saved channel, review rules before outreach</a>}
    {editing ? <><label className={styles.label}>Subject<input className={styles.input} maxLength={120} value={subject} onChange={e => setSubject(e.target.value)} /></label><label className={styles.label}>Message<textarea className={styles.input} maxLength={4000} value={body} onChange={e => setBody(e.target.value)} /></label><p className={styles.muted}>{mailboxes ? "Keep your signature, source disclosure, opt-out and mailing address footer. Saving clears approval; review the new sender and message before approving." : "Keep your signature, opt-out and mailing address footer. The source stays in your review card, not the email. Saving clears approval; review the new message before approving."}</p></> : <><h3>{draft.subject}</h3><p className={styles.draftBody}>{draft.body}</p></>}
    {!sent && !editable && <p className={styles.receipt} role="status">Delivery is held or unconfirmed. Check your Gmail Sent folder before taking further action. This message will not be resent automatically.</p>}
    {draft.channel === "manual" && <><p className={styles.muted}>Continue in your existing conversation. Review this draft before using it for a follow-up, reply or handoff.</p><button type="button" className={`${styles.button} ${styles.secondary}`} data-usage-control="gtm_contacts_copy_continuation" disabled={disabled} onClick={async () => { if (disabled || !current()) return; try { await navigator.clipboard.writeText(`${draft.subject}\n\n${draft.body}`); if (current()) setCopyStatus("Draft copied. Review it in your existing conversation before sending."); } catch { if (current()) setCopyStatus("The draft could not be copied. Select its text to copy it."); } }}>Copy draft</button>{copyStatus && <p role="status">{copyStatus}</p>}</>}
    {editable && draft.channel !== "manual" && <div className={styles.row}>{editing ? <button className={`${styles.button} ${styles.secondary}`} disabled={disabled || !subject.trim() || !body.trim()} onClick={() => void save("draft", { subject, body }, editingRevision).then(ok => { if (ok) setEditing(false); })}>Save edit</button>
      : <button className={`${styles.button} ${styles.secondary}`} disabled={disabled || senderChanging || senderUnavailable || draft.awaiting_recipient || draft.review_state === "approved" || !!warning && !guessReviewed} onClick={() => { if ((!mailboxes || !disabled && !senderChanging && !senderUnavailable && current()) && (!warning || guessReviewed)) void save("approved"); }}>Approve message</button>}
      <button className={`${styles.button} ${styles.secondary}`} disabled={disabled || senderChanging} onClick={() => { setSubject(draft.subject); setBody(draft.body); setEditingRevision(draft.revision); setEditing(!editing); }}>{editing ? "Cancel edit" : "Edit"}</button>
      <button className={`${styles.button} ${styles.secondary}`} disabled={disabled} onClick={() => void save("skipped")}>Skip</button></div>}
  </article>;
}


