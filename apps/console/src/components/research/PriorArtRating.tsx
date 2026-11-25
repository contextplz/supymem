"use client";

import { useRef, useState, type FormEvent } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type PriorArtRatings = { useful: number; new: number; actionable: number };
const questions = [
  { key: "useful", label: "Was this research useful?", control: "research_prior_rating_useful" },
  { key: "new", label: "Did you learn something new?", control: "research_prior_rating_new" },
  { key: "actionable", label: "Would you act on these findings?", control: "research_prior_rating_actionable" },
] as const;

export function PriorArtRating({ onSubmit, className }: {
  onSubmit: (ratings: PriorArtRatings, note: string) => Promise<boolean>; className?: string;
}) {
  const [ratings, setRatings] = useState({ useful: "", new: "", actionable: "" });
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  const submitted = useRef(false);
  const complete = Object.values(ratings).every(value => /^[1-5]$/.test(value));
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!complete || pending.current || submitted.current) return;
    pending.current = true; setBusy(true); setError("");
    try {
      const scores = { useful: Number(ratings.useful), new: Number(ratings.new), actionable: Number(ratings.actionable) };
      if (await onSubmit(scores, note.trim().slice(0, 500)) === true) { submitted.current = true; setSaved(true); }
      else setError("Your feedback could not be saved. Try again.");
    } catch {
      setError("Your feedback could not be saved. Try again.");
    } finally { pending.current = false; setBusy(false); }
  }
  return <form className={className} aria-label="Research feedback" onSubmit={event => void submit(event)}>
    <h3>How useful was this research?</h3>
    <p>Your feedback is optional. Rate each question from 1 (not at all) to 5 (very much).</p>
    <div style={{ display: "grid", gap: "var(--s-3)" }}>
      {questions.map(question => <label key={question.key} style={{ display: "grid", gap: "var(--s-1)" }}>
        {question.label}
        <select required value={ratings[question.key]} disabled={busy || saved} data-usage-control={question.control}
          onChange={event => { setRatings(current => ({ ...current, [question.key]: event.target.value })); setError(""); }}>
          <option value="" disabled>Choose a rating</option>
          {[1, 2, 3, 4, 5].map(value => <option key={value} value={String(value)}>{value}</option>)}
        </select>
      </label>)}
      <label style={{ display: "grid", gap: "var(--s-1)" }}>What was missing? (optional)
        <textarea value={note} maxLength={500} rows={3} disabled={busy || saved} data-usage-control="research_prior_rating_note"
          onChange={event => { setNote(event.target.value.slice(0, 500)); setError(""); }} />
      </label>
    </div>
    <p>Share feedback about the research without personal or contact details.</p>
    <ErrorNotice error={error} source="research.priorArtRating.error" />
    {saved ? <p role="status">Thanks. Your feedback was saved.</p> : <button type="submit" disabled={!complete || busy}
      data-usage-control="research_prior_rating_submit" style={{ background: "transparent", color: "inherit" }}>
      {busy ? "Saving feedback…" : "Share feedback"}
    </button>}
  </form>;
}
