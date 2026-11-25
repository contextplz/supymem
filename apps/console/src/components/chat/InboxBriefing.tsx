"use client";

import { useRef, useState, type ReactNode } from "react";
import type { ChatMessage, InboxBriefingItem, InboxBriefingPayload } from "@/lib/api-types";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { validInboxBriefing } from "@/lib/inboxBriefing";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { ChatAnchor } from "./ChatLink";
import { CosBrief } from "./CosBrief";
import { chatActionIdentity, planChatActions } from "./chatActionPolicy";
import styles from "./ChatDock.module.css";

type Action = NonNullable<ChatMessage["actions"]>[number];

function safeAction(action: Action) {
  if (action.href) {
    try {
      const url = new URL(action.href);
      return url.protocol === "https:" && url.hostname === "mail.google.com" && !url.username && !url.password && !url.port;
    } catch { return false; }
  }
  return ["outreach.draft", "contacts.import", "experiment.record"].includes(action.action || action.name || "") && !action.confirmation;
}

export function InboxBriefing({ message, ventureId, onAction, enabled = false, disabled = false, children }: {
  message: ChatMessage; ventureId?: string; onAction?: (action: string, payload?: Record<string, unknown>) => boolean | void | Promise<boolean | void>;
  enabled?: boolean; disabled?: boolean; children: ReactNode;
}) {
  if (message.kind === "cos_brief") return <CosBrief message={message} ventureId={ventureId} onAction={onAction} disabled={disabled}>{children}</CosBrief>;
  const payload = message.citations?.find(citation => citation.inbox_briefing)?.inbox_briefing;
  if (!enabled || message.role !== "assistant" || !["inbox_briefing", "inbox_urgent"].includes(message.kind || "") || !validInboxBriefing(payload)) return children;
  return <InboxBriefingCard key={`${message.id}:${ventureId}:${verifiedSessionSubject()}:${verifiedSessionEpoch()}`} message={message}
    payload={payload} ventureId={ventureId} onAction={onAction} disabled={disabled} />;
}

function InboxBriefingCard({ message, payload, ventureId, onAction, disabled }: {
  message: ChatMessage; payload: InboxBriefingPayload; ventureId?: string;
  onAction?: (action: string, payload?: Record<string, unknown>) => boolean | void | Promise<boolean | void>;
  disabled: boolean;
}) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState<Record<string, string>>({});
  const pending = useRef(false);
  const owner = useRef({ messageId: message.id, ventureId });
  owner.current = { messageId: message.id, ventureId };

  async function act(action: Action, itemId: string) {
    if (disabled || !onAction || pending.current || action.disabled_reason) return;
    const key = chatActionIdentity(action), subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch()
      && owner.current.messageId === message.id && owner.current.ventureId === ventureId;
    pending.current = true; setBusy(key); setError("");
    try {
      if (await onAction(action.action || action.name || "", action.payload) === false) throw new Error();
      if (current() && action.action === "inbox.item.feedback") setFeedback(before => ({ ...before, [itemId]: String(action.payload?.category) }));
    } catch {
      if (current()) setError("Your action could not be completed. Try again.");
    } finally { pending.current = false; if (current()) setBusy(""); }
  }

  function renderItem(item: InboxBriefingItem) {
    const actions: Action[] = [...item.actions.filter(safeAction),
      ...(["important", "ignore"] as const).map(category => ({ label: category === "important" ? "Important" : "Not important",
        action: "inbox.item.feedback", payload: { item_id: item.id, category } }))];
    const plan = planChatActions(actions);
    const renderAction = (action: Action) => {
      const key = chatActionIdentity(action);
      const className = `${styles.actionLink} ${action === plan.primary ? styles.actionPrimary : styles.actionGhost}`;
      return action.href ? <ChatAnchor key={key} href={action.href} target="_blank" rel="noopener noreferrer" className={className} data-usage-control="inbox_briefing_open_gmail">{action.label}</ChatAnchor>
        : <button key={key} type="button" className={className} data-usage-control={action.action === "inbox.item.feedback" ? "inbox_briefing_feedback" : "inbox_briefing_action"}
          disabled={disabled || !onAction || !!busy || !!action.disabled_reason} title={action.disabled_reason} aria-busy={busy === key}
          aria-pressed={action.action === "inbox.item.feedback" ? feedback[item.id] === action.payload?.category : undefined}
          onClick={() => void act(action, item.id)}>{action.label}</button>;
    };
    return <li key={item.id} style={{ marginBottom: 12 }}>
      <strong>{item.subject}</strong><p>{item.from}</p>
      <p>{item.reason}</p>
      <div className={styles.actions}>{plan.visible.map(renderAction)}{plan.overflow.length > 0 && <details className={styles.actionOverflow}>
        <summary className={`${styles.actionLink} ${styles.actionGhost}`}>More actions</summary><div className={styles.actionMenu}>{plan.overflow.map(renderAction)}</div>
      </details>}</div>
    </li>;
  }

  return <section className={styles.msgText} aria-label="Your inbox briefing" aria-busy={!!busy} style={{ maxHeight: "min(60vh, 28rem)", overflowY: "auto" }}>
    <h3>{message.kind === "inbox_urgent" ? "Your inbox needs attention" : "Your inbox briefing"}</h3>
    {(["important", "worth_a_look", "unclassified"] as const).map(category => {
      const items = payload.items.filter(item => item.category === category);
      return <div key={category}><h4>{{ important: "Important", worth_a_look: "Worth a look", unclassified: "Unclassified" }[category]} ({items.length})</h4>
        {items.length ? <ul>{items.map(renderItem)}</ul> : <p>Nothing new.</p>}</div>;
    })}
    <p>Filtered out: {payload.filtered.newsletters} newsletters, {payload.filtered.notifications} notifications.</p>
    <ErrorNotice error={error} source="inbox.briefing.action" />
  </section>;
}
