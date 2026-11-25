"use client";

import { useRef, useState, type ReactNode } from "react";
import type { ChatMessage, CosBriefPayload } from "@/lib/api-types";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import ChatLink from "./ChatLink";
import { chatActionIdentity } from "./chatActionPolicy";
import styles from "./ChatDock.module.css";

type Action = NonNullable<ChatMessage["actions"]>[number];

export function validCosBrief(value: unknown): value is CosBriefPayload {
  if (!value || typeof value !== "object") return false;
  const payload = value as CosBriefPayload;
  const action = (entry: Action | null) => !!entry && typeof entry.label === "string" && typeof entry.action === "string";
  return typeof payload.week_of === "string" && Array.isArray(payload.sections) && payload.sections.every(section => section
    && typeof section.key === "string" && typeof section.title === "string" && Array.isArray(section.items)
    && section.items.every(item => item && [item.id, item.label, item.href].every(text => typeof text === "string")))
    && (payload.next_move === null || action(payload.next_move))
    && Array.isArray(payload.alternatives) && payload.alternatives.every(action);
}

export function CosBrief({ message, ventureId, onAction, disabled = false, children }: {
  message: ChatMessage; ventureId?: string; onAction?: (action: string, payload?: Record<string, unknown>) => boolean | void | Promise<boolean | void>;
  disabled?: boolean; children: ReactNode;
}) {
  const payload = message.citations?.find(citation => citation.cos_brief)?.cos_brief;
  if (message.role !== "assistant" || message.kind !== "cos_brief" || !validCosBrief(payload)) return children;
  return <CosBriefCard key={`${message.id}:${ventureId}:${verifiedSessionSubject()}:${verifiedSessionEpoch()}`} message={message}
    payload={payload} ventureId={ventureId} onAction={onAction} disabled={disabled} />;
}

function CosBriefCard({ message, payload, ventureId, onAction, disabled }: {
  message: ChatMessage; payload: CosBriefPayload; ventureId?: string;
  onAction?: (action: string, payload?: Record<string, unknown>) => boolean | void | Promise<boolean | void>;
  disabled: boolean;
}) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const pending = useRef(false);
  const owner = useRef({ messageId: message.id, ventureId });
  owner.current = { messageId: message.id, ventureId };

  async function act(action: Action) {
    if (disabled || !onAction || pending.current || action.disabled_reason) return;
    const key = chatActionIdentity(action), subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch()
      && owner.current.messageId === message.id && owner.current.ventureId === ventureId;
    pending.current = true; setBusy(key); setError("");
    try {
      if (await onAction(action.action || action.name || "", action.payload) === false) throw new Error();
    } catch {
      if (current()) setError("Your action could not be completed. Try again.");
    } finally { pending.current = false; if (current()) setBusy(""); }
  }

  // Only this venture's own views open from the brief.
  const ownView = (href: string) => !!ventureId && href.startsWith(`/v/${ventureId}/`) && !href.includes("//", 1);
  const button = (action: Action, primary: boolean) => {
    const key = chatActionIdentity(action);
    return <button key={key} type="button" className={`${styles.actionLink} ${primary ? styles.actionPrimary : styles.actionGhost}`}
      data-usage-control="cos_brief_action" disabled={disabled || !onAction || !!busy || !!action.disabled_reason}
      title={action.disabled_reason} aria-busy={busy === key} onClick={() => void act(action)}>{action.label}</button>;
  };
  const empty = payload.sections.every(section => !section.items.length);

  return <section className={styles.msgText} aria-label="Your weekly brief" aria-busy={!!busy} style={{ maxHeight: "min(60vh, 28rem)", overflowY: "auto" }}>
    <h3>Your weekly brief</h3>
    {empty && <p>Nothing new this week.</p>}
    {payload.sections.map(section => <div key={section.key}><h4>{section.title} ({section.items.length})</h4>
      {section.items.length ? <ul>{section.items.map(item => <li key={item.id}>{ownView(item.href)
        ? <ChatLink href={item.href} data-usage-control="cos_brief_open">{item.label}</ChatLink> : item.label}</li>)}</ul> : <p>Nothing new.</p>}
    </div>)}
    <h4>Your next move</h4>
    {payload.next_move ? <div className={styles.actions}>{button(payload.next_move, true)}{payload.alternatives.slice(0, 2).map(action => button(action, false))}</div>
      : <p>Nothing new.</p>}
    <ErrorNotice error={error} source="cos_brief.action" />
  </section>;
}
