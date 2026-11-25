"use client";

import { useEffect, useRef, useState } from "react";
import { ensureSession, api, type SessionInfo } from "@/lib/client";
import { DesktopAccountConnection } from "@/components/desktop/DesktopAccountConnection";
import { ReportProblem } from "@/components/support/ReportProblem";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { apiErrorCode, apiFailure } from "@/lib/apiFailure";
import { NewVentureLink } from "@/components/chat/NewVentureLink";
import { InboxBriefingSettings } from "./InboxBriefingSettings";
import { RolePresetSettings } from "./RolePresetSettings";
import styles from "@/app/account/account.module.css";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { LocalSettings } from "@/app/settings/local/LocalSettings";

type Venture = { id: string; name?: string; status?: string };
type EmailPrefs = { product_updates: boolean; engagement_reminders: boolean; digests: boolean; suggestions: boolean };
const EMAIL_LABELS: [keyof EmailPrefs, string, string][] = [
  ["product_updates", "Progress updates", "Your plan, research, landing page or build is ready."],
  ["digests", "Daily and weekly digests", "Optional summaries of saved progress. Sent only when something moved."],
  ["suggestions", "Suggestions", "Ideas waiting for your yes or no."],
  ["engagement_reminders", "Reminders to come back", "A few gentle nudges if you have been away."],
];

export function AccountSettings() {
  const local = useLocalMode();
  const [account, setAccount] = useState<SessionInfo | null>(null);
  const [ventures, setVentures] = useState<Venture[]>([]);
  const [ventureId, setVentureId] = useState("");
  const [emailPrefs, setEmailPrefs] = useState<EmailPrefs | null>(null);
  const [emailError, setEmailError] = useState("");
  const [emailBusy, setEmailBusy] = useState(false);
  const savingEmail = useRef(false);
  const sessionEpoch = useRef(0);
  const [firstName, setFirstName] = useState("");
  const [nameNote, setNameNote] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (local) return;
    const retire = () => {
      sessionEpoch.current += 1; savingEmail.current = false;
      setAccount(null); setEmailPrefs(null); setEmailError(""); setEmailBusy(false);
      setVentures([]); setVentureId(""); setFirstName(""); setNameNote("");
      setAttempt(n => n + 1);
    };
    window.addEventListener("supymem-session-changed", retire);
    return () => { sessionEpoch.current += 1; window.removeEventListener("supymem-session-changed", retire); };
  }, [local]);

  useEffect(() => {
    if (local) return;
    let disposed = false;
    const epoch = sessionEpoch.current;
    const current = () => !disposed && epoch === sessionEpoch.current;
    setError("");
    setEmailError("");
    setEmailPrefs(null);
    void (async () => {
      const session = await ensureSession();
      if (!current()) return;
      if (!session.ok || !(session.data as SessionInfo | null)?.authenticated) {
        setError(session.status === 0 ? "Supymem could not load your account. Check your internet connection and press Refresh." : "Sign in to view your account.");
        return;
      }
      setAccount(session.data as SessionInfo);
      void api("/api/me/greeting").then((result) => {
        if (current() && result.ok) setFirstName(((result.data as { first_name?: string | null } | null)?.first_name) || "");
      });
      void api("/api/email/preferences").then((result) => {
        if (!current()) return;
        const preferences = result.ok ? ((result.data as { preferences?: EmailPrefs } | null)?.preferences || null) : null;
        setEmailPrefs(preferences);
        if (!preferences) setEmailError("Email preferences could not load. Press Refresh to try again.");
      });
      const list = await api("/api/ventures");
      if (!current()) return;
      if (!list.ok) { setError("You are signed in, but your ventures did not load. Press Refresh."); return; }
      const payload = list.data as { items?: Venture[]; ventures?: Venture[] } | null;
      const items = Array.isArray(list.data) ? list.data as Venture[] : (payload?.items || payload?.ventures || []);
      setVentures(items);
      setVentureId((current) => current || items[0]?.id || "");
    })();
    return () => { disposed = true; };
  }, [attempt, local]);

  async function saveName(event: React.FormEvent) {
    event.preventDefault();
    const epoch = sessionEpoch.current;
    setNameNote("");
    try {
      const result = await api("/api/me/profile", { method: "POST", body: JSON.stringify({ first_name: firstName.trim() }) });
      if (epoch !== sessionEpoch.current) return;
      setNameNote(result.ok ? "Saved. Charlie will use this name." : apiErrorCode(result) === "invalid_first_name"
        ? "Use letters only, up to 40 characters." : apiFailure(result, "saving your name"));
    } catch { if (epoch === sessionEpoch.current) setNameNote("Couldn’t save your name. Try again when connected."); }
  }

  async function toggleEmail(key: keyof EmailPrefs, value: boolean) {
    if (!emailPrefs || savingEmail.current) return;
    const epoch = sessionEpoch.current;
    savingEmail.current = true; setEmailBusy(true);
    const before = emailPrefs;
    setEmailError("");
    setEmailPrefs({ ...before, [key]: value });
    const result = await api("/api/email/preferences", { method: "PUT", body: JSON.stringify({ [key]: value }) });
    if (epoch !== sessionEpoch.current) return;
    if (result.ok) setEmailPrefs(((result.data as { preferences?: EmailPrefs } | null)?.preferences) || before);
    else { setEmailPrefs(before); setEmailError("Could not save that change. Try again."); }
    savingEmail.current = false; setEmailBusy(false);
  }

  if (local) return <LocalSettings />;

  return <>
    <main className={styles.page}>
      <header className={styles.hero}>
        <div><h1>Your account</h1><p>Manage your account and open your ventures here or in the Supymem Mac app.</p></div>
        <button type="button" className={styles.refresh} disabled={emailBusy} onClick={() => setAttempt((n) => n + 1)}>Refresh</button>
      </header>
      {error ? <p className={styles.notice} role="status">{error}</p> : null}
      {!account && !error ? <p className={styles.notice} role="status">Loading your account…</p> : null}
      {account ? <>
        <section className={styles.identity} aria-label="Account identity">
          <div><span>Signed in as</span><strong>{account.email || "Your Supymem account"}</strong></div>
          <div><span>Account ID (for support)</span><code>{account.subject || "Unavailable"}</code></div>
          <div><span>Desktop connection</span><DesktopAccountConnection expectedSubject={account.subject} onConnected={() => setAttempt((n) => n + 1)} /></div>
        </section>
        <section className={styles.section} aria-label="Your name"><h2>Your name</h2>
          <p className={styles.muted}>What Charlie calls you in the app and in emails.</p>
          <form onSubmit={(event) => void saveName(event)} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <input aria-label="First name" value={firstName} maxLength={40} placeholder="First name" onChange={(event) => setFirstName(event.target.value)} />
            <button type="submit" className={styles.refresh} disabled={!firstName.trim()}>Save</button>
          </form>
          {nameNote ? <p role="status" className={styles.muted}>{nameNote}</p> : null}</section>
        {ventures.length > 1 ? <label>Venture<select value={ventureId} onChange={(event) => setVentureId(event.target.value)}>{ventures.map((venture) => <option key={venture.id} value={venture.id}>{venture.name || venture.id}</option>)}</select></label> : null}
        {!ventureId && <div className={styles.empty}><p>Share your idea to find your first customers.</p><NewVentureLink>What is your idea?</NewVentureLink></div>}
        <section className={styles.section} aria-label="Email preferences" aria-busy={emailBusy}><h2>Email</h2>
          <p className={styles.muted}>Choose what Charlie emails you. Required account and approval messages stay on. Digests start off until you choose them.</p>
          {!emailPrefs && !emailError ? <p className={styles.muted} role="status">Loading email preferences…</p> : null}
          {emailPrefs && EMAIL_LABELS.map(([key, label, help]) => <label key={key} style={{ display: "flex", gap: 10, alignItems: "flex-start", margin: "10px 0" }}>
            <input type="checkbox" disabled={emailBusy} checked={emailPrefs[key]} onChange={(event) => void toggleEmail(key, event.target.checked)} />
            <span><strong>{label}</strong><br /><span className={styles.muted}>{help}</span></span></label>)}
          {emailBusy ? <p role="status" className={styles.muted}>Saving preferences…</p> : null}
          <ErrorNotice error={emailError} source="account.email_preferences" className={styles.notice} /></section>
        <InboxBriefingSettings key={`${account.subject}:${ventureId}`} ventureId={ventureId || undefined} />
        <RolePresetSettings key={`role:${account.subject}:${ventureId}`} ventureId={ventureId || undefined} />
        <section className={styles.section} aria-label="Help"><h2>Something not working?</h2>
          <p className={styles.muted}>Tell us what happened. We attach your app version and recent error codes so we can find it fast.</p>
          <ReportProblem ventureId={ventureId || null} /></section>
      </> : null}
    </main>
  </>;
}
