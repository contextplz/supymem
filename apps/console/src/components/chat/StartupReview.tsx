"use client";

import { useEffect, useRef, useState } from "react";
import Link from "@/components/chat/ChatLink";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "@/app/v/[ventureId]/idea/idea.module.css";
import { RUN_STATUS_REFRESH } from "@/lib/runStatusPresentation";

type Review = {
  revision: string; complete: boolean; priority_text: string; priorities: string[];
  question: { field: string; text: string; prefill?: string; values: { value: unknown; source: string }[] } | null;
  answers: { field: string; value: unknown; skipped?: boolean }[]; next_href: string | null;
  idea_href: string; attachments_href: string; acknowledgement: string | null;
};

export function StartupReview({ ventureId, onContinue }: { ventureId: string; onContinue?: () => void }) {
  const [review, setReview] = useState<Review | null>(null);
  const [answer, setAnswer] = useState("");
  const [selected, setSelected] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(ventureId); active.current = ventureId;
  const pending = useRef(false);
  const generation = useRef(0);

  async function request(action = "", body: Record<string, unknown> = {}) {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError("");
    const version = ++generation.current;
    try {
      await ensureSession();
      const actor = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/context${action ? `/${action}` : ""}`,
        action ? { method: "POST", body: JSON.stringify({ revision: review?.revision, ...body }) } : {});
      if (active.current !== ventureId || generation.current !== version || actor !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch()) return;
      if (!result.ok) { setError(apiFailure(result, "saving your next step")); return; }
      const next = result.data as Review;
      setReview(next); setAnswer(next.question ? next.question.prefill || "" : next.priority_text); setSelected("");
      if (action) window.dispatchEvent(new Event(RUN_STATUS_REFRESH));
    } catch {
      if (active.current === ventureId && generation.current === version) setError("This step could not load. Your saved answers are kept; try again.");
    } finally {
      if (generation.current === version) { pending.current = false; setBusy(false); }
    }
  }

  useEffect(() => {
    setReview(null); setAnswer(""); setSelected("");
    void request();
    const changed = () => { generation.current++; pending.current = false; setBusy(false); setReview(null); setAnswer(""); setSelected(""); setError("Your session changed. Reload to continue."); };
    window.addEventListener("supymem-session-changed", changed);
    return () => { generation.current++; pending.current = false; window.removeEventListener("supymem-session-changed", changed); };
  }, [ventureId]);

  const question = review?.question;
  const value = selected && selected !== "other" ? selected : answer;
  return <div className={styles.page}><section className={styles.focus} aria-label="Choose your next step">
    <ErrorNotice error={error} source="startup-review" />
    {error && <div className={styles.answerActions}><button className="primary" type="button" disabled={busy} onClick={() => void request()}>Reload this step</button><Link href={`/v/${ventureId}/idea`}>Open saved idea</Link></div>}
    {!review && !error && <p role="status">Loading your saved answers…</p>}
    {review && <>
      {review.complete ? <>
        <h2>Your next step is ready</h2>
        <p>{review.acknowledgement || "Your goal is saved. Nothing is published or sent without you."}</p>
        <div className={styles.answerActions}>
          {onContinue ? <button className="primary" type="button" onClick={onContinue}>Continue in chat</button> : review.next_href && <Link className="primary" href={review.next_href}>{review.priorities[0] === "outreach_leads" ? "Book a first call" : "Review next step"}</Link>}
          <button className="ghost sm" type="button" disabled={busy} onClick={() => void request("back", { field: "priorities" })}>Edit priorities</button>
        </div>
        {!onContinue && <nav className={styles.answerActions} aria-label="Other saved priorities">
          {review.priorities.includes("market_insights") && <Link href={`/v/${ventureId}/research`}>Research your market</Link>}
          {review.priorities.some(category => category === "fundraising" || category === "other") && <Link href={`/v/${ventureId}/tasks`}>Review saved plan items</Link>}
        </nav>}
      </> : <form className={styles.composer} onSubmit={event => { event.preventDefault(); void request(question ? "answer" : "priorities", question ? { field: question.field, value } : { text: answer }); }}>
        <h2>{question?.text || "What matters most to you right now?"}</h2>
        {!question && <p>You can name a couple of things, or say you’re not sure. If you’re unsure, we’ll help you book a first call with a potential customer or an industry person who can give insights.</p>}
        {question && question.values.length > 0 && <fieldset>
          <legend>Choose the value we should use</legend>
          {question.values.map((option, index) => {
            const optionValue = typeof option.value === "string" ? option.value : JSON.stringify(option.value);
            return <label key={`${index}:${option.source}`} style={{ display: "block", marginBottom: 12 }}>
              <input type="radio" name="answer" value={optionValue} checked={selected === optionValue} onChange={() => setSelected(optionValue)} />
              {optionValue} <span className="muted">({option.source})</span>
            </label>;
          })}
          <label><input type="radio" name="answer" value="other" checked={selected === "other"} onChange={() => setSelected("other")} />Something else</label>
        </fieldset>}
        {(!question || !question.values.length || selected === "other") && <label style={{ display: "grid", gap: 9 }}>
          {question ? "Your answer" : "Your priorities"}
          <textarea rows={4} maxLength={2000} value={answer} onChange={event => setAnswer(event.target.value)} />
        </label>}
        <div className={styles.answerActions}>
          <button className="primary" type="submit" disabled={busy || !!question && !value.trim()}>{busy ? "Saving…" : "Continue"}</button>
          <button className="ghost sm" type="button" disabled={busy} onClick={() => void request(question ? "skip" : "priorities", question ? { field: question.field } : { text: "" })}>Skip</button>
        </div>
      </form>}
      <nav className={`${styles.answerActions} ${styles.editLinks}`} aria-label="Go back" style={{ marginTop: 24 }}>
        <Link href={review.idea_href}>Edit idea</Link>
        <Link href={review.attachments_href}>Edit attachments</Link>
        {review.answers.map(saved => <button type="button" key={saved.field} disabled={busy}
          onClick={() => void request("back", { field: saved.field })}>Edit {saved.field === "making" ? "what you’re making" : saved.field.replaceAll("_", " ")}</button>)}
      </nav>
    </>}
  </section></div>;
}
