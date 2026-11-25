"use client";

import { useId, useState, type FormEvent } from "react";
import { requestPasswordReset } from "@/lib/client";
import { passwordResetError } from "@/lib/passwordPolicy";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export function PasswordResetForm() {
  const emailId = useId();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    const result = await requestPasswordReset(email.trim());
    setBusy(false);
    if (result.ok) {
      setSent(true);
      return;
    }
    const data = result.data as { detail?: { code?: string }; error?: { code?: string } } | null;
    setError(result.error || passwordResetError(data?.detail?.code || data?.error?.code));
  }

  if (!open) {
    return <a href={`#${emailId}-reset`} className="ghost" onClick={(event) => { event.preventDefault(); setOpen(true); }}>Forgot password?</a>;
  }

  return (
    <section id={`${emailId}-reset`} aria-label="Password reset">
      {sent ? <p role="status">If an account exists, we sent a reset link</p> : (
        <form onSubmit={(event) => void onSubmit(event)}>
          <label htmlFor={emailId}>Email</label>
          <input id={emailId} type="email" autoComplete="email" required autoFocus value={email} onChange={(event) => setEmail(event.target.value)} />
          <ErrorNotice error={error} source="auth.password-reset" className="error" />
          <button type="submit" className="primary" disabled={busy}>{busy ? "Sending…" : "Send reset link"}</button>
        </form>
      )}
      <button type="button" className="ghost" disabled={busy} onClick={() => { setOpen(false); setSent(false); setError(null); }}>Back to sign in</button>
    </section>
  );
}
