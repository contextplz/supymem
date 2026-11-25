"use client";

import { useInboxBriefing } from "@/components/settings/useInboxBriefing";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "@/components/marketing/MarketingLab.module.css";

export function InboxBriefingConsent({ ventureId, workspaceId, mailboxId, email, disabled = false }: {
  ventureId?: string; workspaceId?: string; mailboxId?: string; email?: string | null; disabled?: boolean;
}) {
  const state = useInboxBriefing(ventureId, workspaceId);
  const mailboxes = state.briefing?.mailboxes;
  if (!state.enabled || !Array.isArray(mailboxes)) return null;
  const selected = mailboxes.filter(mailbox => typeof mailbox?.mailbox_id === "string" && !!mailbox.mailbox_id
    && typeof mailbox.email === "string" && typeof mailbox.status === "string" && typeof mailbox.consent === "boolean"
    && (mailboxId ? mailbox.mailbox_id === mailboxId : !email || mailbox.email.toLowerCase() === email.toLowerCase()));
  if (!selected.length) return null;
  return <div aria-busy={state.busy}>
    {selected.map(mailbox => <label key={mailbox.mailbox_id} style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 8 }}>
      <input type="checkbox" data-usage-control="inbox_briefing_consent" checked={mailbox.consent}
        disabled={disabled || state.busy || !ventureId || mailbox.status !== "active" && !mailbox.consent} aria-label={`Let Charlie read and brief you on ${mailbox.email}`}
        onChange={event => void state.save({ action: "inbox.briefing.consent", payload: { mailbox_id: mailbox.mailbox_id, consent: event.target.checked } })} />
      <span>Let Charlie read and brief you on this inbox{selected.length > 1 ? ` (${mailbox.email})` : ""}</span>
    </label>)}
    <p className={styles.muted}>Read only. Nothing is sent or changed. Turning this off stops reading and deletes saved briefing items for this inbox.</p>
    {!ventureId && <p className={styles.muted}>Open Connections from a venture to change your inbox preferences.</p>}
    <ErrorNotice error={state.error} source="inbox.briefing.consent" />
  </div>;
}
