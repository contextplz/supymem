"use client";

import { useState } from "react";
import { DictateButton } from "@/components/chat/DictateButton";
import type { StepObservations } from "@/lib/nextStepEngine";
import styles from "./GtmJourney.module.css";

export function CallBriefView({ text }: { text: string }) {
  return <pre className={styles.draftBody}>{text}</pre>;
}

export function CallNotesForm({ contactId, initial = "", disabled, structured = false, save }: {
  contactId: string; initial?: string; disabled: boolean; structured?: boolean;
  save: (text: string, observations?: StepObservations) => Promise<boolean | void | undefined>;
}) {
  const [text, setText] = useState(initial);
  const [pain, setPain] = useState("");
  const [refuted, setRefuted] = useState("");
  const [workaround, setWorkaround] = useState("");
  const [spent, setSpent] = useState("");
  const [price, setPrice] = useState<StepObservations["willingness_to_pay"]>("unknown");
  const [commitment, setCommitment] = useState<StepObservations["commitment"]>("none");
  return <form onSubmit={event => { event.preventDefault(); if (!disabled && text.trim()) void save(text, structured ? {
    pains_confirmed: pain.trim() ? [pain] : [], pains_refuted: refuted.trim() ? [refuted] : [], objections: [],
    current_workaround: workaround, money_or_time_spent: spent, willingness_to_pay: price, commitment,
  } : undefined); }}>
    <p className={styles.muted}>Record what they actually said. Saving never sends anything.</p>
    <label className={styles.label}>Call notes<textarea className={styles.input} maxLength={8000} value={text} disabled={disabled} onChange={event => setText(event.target.value)} /></label>
    <DictateButton scope={contactId} value={text} onChange={setText} disabled={disabled} />
    {structured && <>
      <label className={styles.label}>Pain, in their words<textarea className={styles.input} maxLength={1000} value={pain} disabled={disabled} onChange={event => setPain(event.target.value)} /></label>
      <label className={styles.label}>What contradicted your hypothesis?<textarea className={styles.input} maxLength={1000} value={refuted} disabled={disabled} onChange={event => setRefuted(event.target.value)} /></label>
      <label className={styles.label}>Current workaround<input className={styles.input} maxLength={2000} value={workaround} disabled={disabled} onChange={event => setWorkaround(event.target.value)} /></label>
      <label className={styles.label}>Money or time already spent<input className={styles.input} maxLength={2000} value={spent} disabled={disabled} onChange={event => setSpent(event.target.value)} /></label>
      <label className={styles.label}>Willingness to pay<select className={styles.input} value={price} disabled={disabled} onChange={event => setPrice(event.target.value as typeof price)}>{["unknown", "yes", "maybe", "no"].map(value => <option key={value}>{value}</option>)}</select></label>
      <label className={styles.label}>Commitment offered<select className={styles.input} value={commitment} disabled={disabled} onChange={event => setCommitment(event.target.value as typeof commitment)}>{["none", "follow_up", "intro", "loi", "pay"].map(value => <option key={value}>{value}</option>)}</select></label>
    </>}
    <button type="submit" className={styles.button} disabled={disabled || !text.trim()} data-usage-control="gtm_call_notes_save">Save notes and find learnings</button>
  </form>;
}
