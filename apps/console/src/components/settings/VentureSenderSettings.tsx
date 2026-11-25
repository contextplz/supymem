"use client";

import { useEffect, useRef, useState } from "react";
import { getGmailMailboxes, setDefaultGmailMailbox, type GmailMailboxes } from "@/lib/opsApi";
import { mailboxCanSend } from "@/lib/gmailMailboxes";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ChatAnchor } from "@/components/chat/ChatLink";

export function VentureSenderSettings({ workspaceId, ventureId, current }: { workspaceId: string; ventureId: string; current: () => boolean }) {
  const [inboxes, setInboxes] = useState<GmailMailboxes | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [failed, setFailed] = useState(false);
  const live = useRef(current); live.current = current;
  const serial = useRef(0);
  const flight = useRef(false);
  const mounted = useRef(true);
  const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const key = JSON.stringify([workspaceId, ventureId, subject, epoch]);
  const selection = useRef(key); selection.current = key;
  const owned = () => mounted.current && live.current() && selection.current === key && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();

  async function load() {
    if (!owned()) return;
    const ticket = ++serial.current;
    try {
      const saved = await getGmailMailboxes(workspaceId, ventureId);
      if (!owned() || ticket !== serial.current) return;
      setInboxes(saved); setFailed(false); setNotice("");
    } catch {
      if (owned() && ticket === serial.current) { setFailed(true); setNotice("Your Gmail sender settings could not be loaded. Refresh or open Connections to check your inboxes."); }
    }
  }

  useEffect(() => {
    mounted.current = true;
    setInboxes(null); setNotice(""); setFailed(false); setBusy(false); flight.current = false;
    void load();
    return () => { mounted.current = false; ++serial.current; };
  }, [key]);

  if (!owned() || !inboxes && !failed) return null;
  const workspaceDefault = inboxes?.mailboxes.find(mailbox => mailbox.id === inboxes.workspace_default_mailbox_id);
  const selectedId = inboxes?.venture_default_mailbox_id || "";
  const selected = inboxes?.mailboxes.find(mailbox => mailbox.id === selectedId);
  return <div className="panel-card">
    <h3>Default Gmail sender</h3>
    <p className="muted">You choose the default inbox for new messages in this venture. Previously reviewed messages and existing conversations keep their sender.</p>
    {inboxes && <label>Send new messages from<select aria-label="Default Gmail sender" value={selectedId} disabled={busy} onChange={async event => {
      const mailboxId = event.target.value;
      if (!owned() || flight.current || mailboxId === selectedId || mailboxId && !inboxes.mailboxes.some(mailbox => mailbox.id === mailboxId && mailboxCanSend(mailbox))) return;
      flight.current = true; ++serial.current; setBusy(true); setNotice("");
      try {
        await setDefaultGmailMailbox(workspaceId, mailboxId || null, ventureId);
        if (!owned()) return;
        const saved = await getGmailMailboxes(workspaceId, ventureId);
        if (!owned()) return;
        if (!saved || (saved.venture_default_mailbox_id || "") !== mailboxId) throw new Error("Sender change was not confirmed.");
        setInboxes(saved); setFailed(false); setNotice("You saved your default sender. Sending still needs your approval.");
      } catch { if (owned()) { setFailed(true); setNotice("Your sender change could not be confirmed. Refresh saved settings before trying again; no messages were sent."); } }
      finally { if (owned()) { flight.current = false; setBusy(false); } }
    }}>
      <option value="">Use workspace default{workspaceDefault ? ` (${workspaceDefault.email}${mailboxCanSend(workspaceDefault) ? "" : ", unavailable"})` : " (unavailable)"}</option>
      {selectedId && !selected && <option value={selectedId} disabled>Selected inbox unavailable</option>}
      {inboxes.mailboxes.map(mailbox => <option key={mailbox.id} value={mailbox.id} disabled={!mailboxCanSend(mailbox)}>{mailbox.email}{mailboxCanSend(mailbox) ? "" : " (unavailable)"}</option>)}
    </select></label>}
    {notice && <p role="status">{notice}</p>}
    {failed && <button type="button" disabled={busy} onClick={() => void load()}>Refresh saved settings</button>}
    <ChatAnchor href={`/connections?workspace=${encodeURIComponent(workspaceId)}&venture=${encodeURIComponent(ventureId)}`}>Manage your Gmail inboxes</ChatAnchor>
  </div>;
}
