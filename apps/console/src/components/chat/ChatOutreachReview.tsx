"use client";

import { useEffect, useRef, useState } from "react";
import { journeyClient, type GtmJourney, type JourneyDraft, type JourneyDraftEdits } from "@/lib/gtmJourneyApi";
import { getOpsWorkspace } from "@/lib/opsApi";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { mailboxCanSend } from "@/lib/gmailMailboxes";
import { Draft } from "@/components/gtm/OutreachDraft";
import { ChatAnchor } from "./ChatLink";

export function ChatOutreachReview({ ventureId, messageId, approval, disabled, current, onReady }: {
  ventureId: string; messageId: string; approval: Record<string, unknown>; disabled: boolean;
  current: () => boolean; onReady: (ready: boolean) => void;
}) {
  const [journey, setJourney] = useState<GtmJourney | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const callbacks = useRef({ current, onReady }); callbacks.current = { current, onReady };
  const flight = useRef(false), changed = useRef(false), serial = useRef(0);
  const mounted = useRef(true);
  const owner = { actorId: verifiedSessionSubject() || "", workspaceId: getOpsWorkspace(), ventureId };
  const epoch = verifiedSessionEpoch();
  const key = JSON.stringify([owner.actorId, owner.workspaceId, ventureId, messageId, epoch]);
  const selected = useRef(key); selected.current = key;
  const owned = () => mounted.current && callbacks.current.current() && selected.current === key && owner.actorId === verifiedSessionSubject() &&
    epoch === verifiedSessionEpoch() && owner.workspaceId === getOpsWorkspace();
  const ids = Array.isArray(approval.draft_ids) && approval.draft_ids.every(id => typeof id === "string") ? approval.draft_ids as string[] : [];
  const drafts = journey?.drafts.filter(draft => ids.includes(draft.doc_id)) || [];

  useEffect(() => {
    mounted.current = true;
    const ticket = ++serial.current;
    callbacks.current.onReady(false); setNotice(""); setJourney(null);
    void journeyClient.get(owner).then(saved => {
      if (!owned() || ticket !== serial.current) return;
      const batch = saved.batches.find(item => item.batch_id === approval.batch_id);
      const reviewed = saved.drafts.filter(draft => ids.includes(draft.doc_id));
      if (saved.gmail.multi_gmail_v1 !== true || !ids.length || reviewed.length !== ids.length ||
          batch?.approval_digest !== approval.approval_digest || reviewed.some(draft => draft.state !== "draft" || draft.review_state !== "approved" ||
            !saved.gmail.mailboxes?.some(mailbox => mailbox.id === draft.mailbox_id && mailboxCanSend(mailbox)))) {
        throw new Error("The sender or approved messages changed. Open your saved outreach review before sending.");
      }
      setJourney(saved); callbacks.current.onReady(!changed.current);
    }).catch(() => {
      if (owned() && ticket === serial.current) setNotice("Your reviewed sender could not be verified. Refresh or open your saved outreach review; nothing was sent.");
    });
    return () => { mounted.current = false; ++serial.current; };
  }, [key, retry]);

  async function save(draft: JourneyDraft, decision: "approved" | "skipped" | "draft", edits?: JourneyDraftEdits, revision?: string) {
    if (!owned() || disabled || flight.current) return false;
    flight.current = true; changed.current = true; callbacks.current.onReady(false); setBusy(true); setNotice("");
    try {
      const saved = await journeyClient.review(owner, { ...draft, revision: revision || draft.revision }, decision, edits);
      if (!owned()) return false;
      setJourney(saved); setNotice("Your message changed. Review its sender and exact text, then open the updated sending review. This old approval cannot send it.");
      return true;
    } catch { if (owned()) setNotice("Your change could not be confirmed. Open the saved review before trying again. This old approval will not send anything."); return false; }
    finally { if (owned()) { flight.current = false; setBusy(false); } }
  }

  if (!owned()) return null;
  return <div>
    {journey ? <><p>Sender: {[...new Set(drafts.map(draft => draft.sender?.email || journey.gmail.mailboxes?.find(mailbox => mailbox.id === draft.mailbox_id)?.email || "Unavailable"))].join(", ")}</p>
      <details><summary>Review or change your sender</summary>{drafts.map(draft => <Draft key={`${draft.doc_id}:${draft.revision}`} draft={draft} mailboxes={journey.gmail.mailboxes || []} disabled={disabled || busy} current={owned} save={(decision, edits, revision) => save(draft, decision, edits, revision)} />)}</details></>
      : <p role="status">{notice || "Checking your reviewed sender…"}</p>}
    {journey && notice && <p role="status">{notice}</p>}
    {!journey && notice && <button type="button" disabled={disabled || busy} onClick={() => { if (owned()) setRetry(value => value + 1); }}>Refresh sender review</button>}
    <ChatAnchor href={`/gtm?workspace=${encodeURIComponent(owner.workspaceId)}&venture=${encodeURIComponent(ventureId)}&step=review`}>Open updated outreach review</ChatAnchor>
  </div>;
}
