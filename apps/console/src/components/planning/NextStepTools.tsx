"use client";

import Link from "@/components/chat/ChatLink";
import { useEffect, useState, type ComponentType } from "react";
import { CallBriefView, CallNotesForm } from "@/components/gtm/CallNotesForm";
import { CopyDraft, Draft } from "@/components/gtm/OutreachDraft";
import { VentureWaitlist } from "@/components/site/VentureWaitlist";
import { journeyClient, type GtmJourney, type JourneyDraftEdits } from "@/lib/gtmJourneyApi";
import { prefillChat } from "@/lib/chatPrefill";
import { verifiedSessionSubject } from "@/lib/client";
import { evidenceSummary, stepDestination, type ActionCard, type NextStepComponent, type NextStepDecision, type StepResult } from "@/lib/nextStepEngine";
import styles from "@/components/gtm/GtmJourney.module.css";

export type ToolProps = { action: ActionCard; decision: NextStepDecision; disabled: boolean;
  current: () => boolean; onDone: (result: StepResult) => Promise<void> };

function Entry({ action, decision, disabled, onDone }: ToolProps) {
  return <><p>{action.params.context}</p><Link href={stepDestination(action.action_id, decision.venture_id)}>Open the existing tool</Link>
    <button type="button" disabled={disabled} onClick={() => void onDone({ viewed: true })}>Check completion</button></>;
}

const QUESTIONS = ["Tell me about the last time this problem happened.", "What made that situation hard?",
  "What did you do to work around it?", "What did that cost in time or money?", "Who else is involved when you try to solve it?"];

function CallBrief({ action, decision, disabled, onDone }: ToolProps) {
  const text = action.required_evidence_ids.map(identifier => evidenceSummary(decision.evidence[identifier])).join("\n\n")
    + "\n\nHypothesis to test: does this person experience your stated problem?\n\n" + QUESTIONS.join("\n")
    + "\n\nCommitment ask: would a follow-up, introduction or small pilot be appropriate? Ask without pressure.";
  return <><CallBriefView text={text} /><button type="button" disabled={disabled} onClick={() => void onDone({ viewed: true, text: text.slice(0, 8000) })}>Brief ready</button></>;
}

function CallNotes({ action, disabled, onDone }: ToolProps) {
  return <CallNotesForm contactId={action.params.contact_id!} disabled={disabled} structured save={(text, observations) => onDone({ text, observations })} />;
}

function CallSchedule({ disabled, onDone }: ToolProps) {
  const [date, setDate] = useState("");
  return <form onSubmit={event => { event.preventDefault(); if (date && !disabled) void onDone({ scheduled_at: new Date(date).toISOString() }); }}>
    <p>“Call booked” records agreement, not its scheduled time. Record the actual call time to prepare at the right moment.</p>
    <label className={styles.label}>Call time (your local timezone)<input className={styles.input} type="datetime-local" required disabled={disabled} value={date} onChange={event => setDate(event.target.value)} /></label>
    <button className={styles.button} type="submit" disabled={disabled || !date}>Save call time</button>
  </form>;
}

function LearningSummary({ action, decision, disabled, onDone }: ToolProps) {
  const frequencies = new Map<string, { count: number; identifiers: string[] }>();
  const contradictions: { text: string; identifier: string }[] = [];
  for (const identifier of action.required_evidence_ids) {
    const evidence = decision.evidence[identifier];
    if (evidence.kind !== "learnings") continue;
    const learned = evidence.observations;
    if (!learned) continue;
    for (const text of learned.pains_confirmed || []) {
      const entry = frequencies.get(text) || { count: 0, identifiers: [] };
      entry.count++; entry.identifiers.push(identifier); frequencies.set(text, entry);
    }
    for (const text of learned.pains_refuted || []) contradictions.push({ text, identifier });
  }
  return <><table><caption>Pains quoted in saved notes (exact phrases, not inferred themes)</caption><thead><tr><th>Pain</th><th>Calls</th><th>Evidence</th></tr></thead>
    <tbody>{[...frequencies].map(([text, entry]) => <tr key={text}><td>{text}</td><td>{entry.count}</td><td>{entry.identifiers.map((identifier, index) => <a key={identifier} href={`#next-evidence-${encodeURIComponent(identifier)}`}>Source {index + 1} </a>)}</td></tr>)}</tbody></table>
    <h3>Contradictions</h3>{contradictions.length ? <ul>{contradictions.map((item, index) => <li key={`${item.identifier}:${index}`}>{item.text} <a href={`#next-evidence-${encodeURIComponent(item.identifier)}`}>Source {index + 1}</a></li>)}</ul> : <p>No contradictions have been recorded. That does not prove there are none.</p>}
    {action.component === "learning_summary" && <button type="button" disabled={disabled} onClick={() => void onDone({ viewed: true })}>Mark synthesis reviewed</button>}</>;
}

function VentureDecision(props: ToolProps) {
  return <><LearningSummary {...props} /><p>The evidence informs the decision; you make it.</p>{(["persevere", "narrow_segment", "pivot_problem"] as const).map(decision => <button key={decision} type="button" disabled={props.disabled} onClick={() => void props.onDone({ decision })}>{decision.replaceAll("_", " ")}</button>)}</>;
}

function MvpScope({ action, disabled, onDone }: ToolProps) {
  const [text, setText] = useState(`Must-have customer job:\n\nConcierge/manual delivery first:\n\nExplicit cuts (not building):\n\nEvidence:\n${action.params.text || ""}`);
  return <form onSubmit={event => { event.preventDefault(); if (!disabled && text.trim()) void onDone({ text }); }}>
    <label className={styles.label}>One-page MVP scope<textarea className={styles.input} rows={12} maxLength={8000} value={text} disabled={disabled} onChange={event => setText(event.target.value)} /></label>
    <button className={styles.button} type="submit" disabled={disabled || !text.trim()}>Save scope</button>
  </form>;
}

function DraftEditor({ action, decision, disabled, current, onDone }: ToolProps) {
  const [body, setBody] = useState(action.params.body || "");
  const [journey, setJourney] = useState<GtmJourney | null>(null);
  const [notice, setNotice] = useState("");
  const owner = { actorId: verifiedSessionSubject() || "", workspaceId: decision.workspace_id, ventureId: decision.venture_id };
  useEffect(() => { let active = true; void journeyClient.get(owner).then(value => { if (active && current()) setJourney(value); }).catch(() => { if (active && current()) setNotice("Open your existing outreach review to check saved drafts."); }); return () => { active = false; }; }, [decision.decision_id]);
  const draft = journey?.drafts.find(item => item.contact_id === action.params.contact_id && item.state === "draft");
  async function saveDraft(status: "approved" | "skipped" | "draft", edits?: JourneyDraftEdits, revision?: string) {
    if (disabled || !current() || !draft) return false;
    try {
      const saved = await journeyClient.review(owner, { ...draft, revision: revision || draft.revision }, status, edits);
      if (!current()) return false;
      setJourney(saved); return true;
    } catch { if (current()) setNotice("Draft change was not confirmed. Refresh existing review before retrying. Nothing was sent."); return false; }
  }
  return <><label className={styles.label}>Draft message<textarea className={styles.input} value={body} maxLength={4000} disabled={disabled} onChange={event => setBody(event.target.value)} /></label>
    <CopyDraft title={action.params.subject || action.title} text={body} disabled={disabled} current={current} />
    {draft && <Draft key={`${draft.doc_id}:${draft.revision}`} draft={draft} mailboxes={journey?.gmail.multi_gmail_v1 === true ? journey.gmail.mailboxes ?? [] : undefined} disabled={disabled} current={current} save={saveDraft} />}
    <p>Nothing is sent here. Keep the opt-out and address footer; use the existing approval and send flow.</p>
    <Link href={`/gtm?venture=${encodeURIComponent(decision.venture_id)}&step=review`}>Open approved outreach review</Link>
    {!draft && <button type="button" disabled={disabled} onClick={() => { if (current()) prefillChat(`Draft for review only; do not send. ${action.params.subject}\n\n${body}`); }}>Prepare in existing draft workflow</button>}
    {action.component === "draft_editor" && <button type="button" disabled={disabled} onClick={() => void onDone({})}>Check founder-sent completion</button>}
    {action.component === "draft_editor" && <button type="button" disabled={disabled || !body.trim()} onClick={() => void onDone({ sent_by_founder: true, text: body })}>I sent this in my existing conversation</button>}
    {notice && <p role="status">{notice}</p>}</>;
}

function PricingTest(props: ToolProps) {
  const [response, setResponse] = useState<"yes" | "maybe" | "no">("no");
  const [commitment, setCommitment] = useState<"none" | "loi" | "pay">("none");
  return <><DraftEditor {...props} /><form onSubmit={event => { event.preventDefault(); if (!props.disabled) void props.onDone({ price_response: response, commitment }); }}>
    <p>Log an actual price response, not predicted interest.</p><label className={styles.label}>Price response<select value={response} disabled={props.disabled} onChange={event => setResponse(event.target.value as typeof response)}>{["yes", "maybe", "no"].map(value => <option key={value}>{value}</option>)}</select></label>
    <label className={styles.label}>Written or verbal commitment<select value={commitment} disabled={props.disabled} onChange={event => setCommitment(event.target.value as typeof commitment)}>{["none", "loi", "pay"].map(value => <option key={value}>{value}</option>)}</select></label>
    <button type="submit" disabled={props.disabled}>Save price response</button>
  </form></>;
}

function PaymentDraft(props: ToolProps) {
  const [link, setLink] = useState("");
  return <><DraftEditor {...props} /><p>Create the invoice or payment link in your own Stripe or PayPal account. This card cannot charge or move money.</p>
    <label className={styles.label}>Your invoice/payment reference<input className={styles.input} maxLength={1000} value={link} disabled={props.disabled} onChange={event => setLink(event.target.value)} /></label>
    <button type="button" disabled={props.disabled || !link.trim()} onClick={() => void props.onDone({ payment_received: true, text: link })}>I verified payment received</button></>;
}

function LandingPage({ action, decision, disabled, current, onDone }: ToolProps) {
  return <><p>{action.params.context}</p><Link href={`/v/${encodeURIComponent(decision.venture_id)}/site`}>Open existing landing page builder and publication review</Link>
    <button type="button" disabled={disabled} onClick={() => { if (current()) prefillChat(`Prefill a landing page draft from these saved learnings, without publishing: ${action.params.context}`); }}>Prepare a prefilled page draft</button>
    <VentureWaitlist ventureId={decision.venture_id} usageSurface="website" />
    <button type="button" disabled={disabled} onClick={() => void onDone({})}>Check live page</button></>;
}

export const NEXT_STEP_TOOLS: Record<NextStepComponent, ComponentType<ToolProps>> = {
  call_brief: CallBrief, call_notes: CallNotes, draft_editor: DraftEditor, learning_summary: LearningSummary,
  venture_decision: VentureDecision, pricing_test: PricingTest, landing_page: LandingPage, mvp_scope: MvpScope,
  payment_draft: PaymentDraft, journey_entry: Entry, call_schedule: CallSchedule,
};
