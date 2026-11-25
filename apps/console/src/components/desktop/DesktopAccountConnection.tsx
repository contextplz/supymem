"use client";

import { useEffect, useState } from "react";
import { desktopInvokeOrThrow, desktopListen, isDesktopShell } from "@/lib/desktop";
import { waitForChatRequest } from "@/lib/chatStop";
import { nativeAuthAvailability, nativeAuthActionPending } from "@/lib/nativeAuthAvailability";
import { awaitingBrowser, CODE_STEP_AFTER_MS, LINK_EXPIRES_MS, PASTE_CODE_STEPS, signInPhase, signInErrorMessage, supymemSignInLink, type SignInPhase } from "@/lib/signInReturn";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type NativeAuthStatus = {
  state: "signed_out" | "authorizing" | "connecting" | "restoring" | "connected" | "offline" |
    "keychain_unavailable" | "secure_store_pending" | "native_auth_busy" |
    "reauth_required" | "account_mismatch" | "revocation_pending";
  subject?: string | null;
  device_id?: string | null;
  error?: string | null;
  persisted: boolean;
  bridge_connected?: boolean;
  restoring?: boolean;
  login_url?: string;
};

const descriptions: Record<NativeAuthStatus["state"], string> = {
  signed_out: "Sign in with your browser to continue.",
  authorizing: "Finish signing in and approve this Mac in your browser. Opening the browser does not complete the connection.",
  connecting: "Verifying the approved account with Supymem…",
  connected: "Your account is connected. Device credentials stay in the secure credential store on this Mac.",
  offline: "The hosted connection could not be verified. Your saved account and queued work remain on this Mac.",
  ...nativeAuthAvailability,
  reauth_required: "This device needs a new sign-in. Reconnect the account shown below to continue.",
  account_mismatch: "The approved account does not match this connection. Sign out before connecting the intended account.",
  revocation_pending: "Signed out on this Mac. Server revocation is still pending; retry when connected.",
};

function validStatus(value: unknown): value is NativeAuthStatus {
  return !!value && typeof value === "object" && "state" in value &&
    typeof value.state === "string" && Object.hasOwn(descriptions, value.state);
}

async function readNativeAuthStatus(): Promise<NativeAuthStatus> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const value = await waitForChatRequest(desktopInvokeOrThrow<NativeAuthStatus>("desktop_auth_status"), controller.signal);
    if (!validStatus(value)) throw new Error("Invalid account status");
    return value;
  } catch {
    throw new Error("Your account could not be checked. Reopen Supymem or update the Mac app, then press Retry.");
  } finally { window.clearTimeout(timer); }
}

export function DesktopAccountConnection({ expectedSubject, onConnected, compact = false }: {
  expectedSubject?: string;
  onConnected?: () => void;
  compact?: boolean;
}) {
  const [status, setStatus] = useState<NativeAuthStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [desktop, setDesktop] = useState(false);
  const [copyMessage, setCopyMessage] = useState("");
  const [fallbackCode, setFallbackCode] = useState("");
  const [phase, setPhase] = useState<SignInPhase>("waiting");
  const [showCode, setShowCode] = useState(false);
  const waiting = awaitingBrowser(status?.state);
  // The deep link back into the app is not reliable: after a short wait, show the paste-code step by itself.
  useEffect(() => {
    setPhase("waiting");
    setShowCode(false);
    if (!waiting) return;
    const started = Date.now();
    const tick = () => setPhase(signInPhase(Date.now() - started));
    const first = window.setTimeout(tick, CODE_STEP_AFTER_MS);
    const second = window.setTimeout(tick, LINK_EXPIRES_MS);
    return () => { window.clearTimeout(first); window.clearTimeout(second); };
  }, [waiting, status?.login_url]);
  // Coming back from the browser: re-read the native state in case the deep-link event was missed.
  useEffect(() => {
    if (!waiting) return;
    const reread = () => {
      if (document.visibilityState === "hidden") return;
      void readNativeAuthStatus().then((value) => {
        setError(""); setStatus(value); if (value.state === "connected") onConnected?.();
      }).catch((err) => setError(err.message));
    };
    window.addEventListener("focus", reread);
    document.addEventListener("visibilitychange", reread);
    return () => { window.removeEventListener("focus", reread); document.removeEventListener("visibilitychange", reread); };
  }, [waiting, onConnected]);
  useEffect(() => {
    if (!isDesktopShell()) return;
    setDesktop(true);
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void desktopListen("supymem-native-auth", (value) => {
      if (!disposed && validStatus(value)) setStatus(value);
    }).then((stop) => { if (disposed) stop(); else unlisten = stop; });
    void readNativeAuthStatus().then((value) => {
      if (!disposed) setStatus(value);
    }).catch((err) => { if (!disposed) setError(err.message); });
    return () => { disposed = true; unlisten?.(); };
  }, []);

  async function act(command: string) {
    if (busy || nativeAuthActionPending(status?.state)) return;
    setBusy(true); setError(""); setCopyMessage("");
    try {
      const value = command === "desktop_auth_status" ? await readNativeAuthStatus()
        : await desktopInvokeOrThrow<NativeAuthStatus>(command, { expectedSubject });
      if (!validStatus(value)) throw new Error("The app returned an invalid connection status.");
      setStatus(value);
      if (value.state === "connected") onConnected?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The connection could not be updated.");
    } finally { setBusy(false); }
  }

  async function copyLoginLink() {
    if (busy || nativeAuthActionPending(status?.state)) return;
    setBusy(true); setError(""); setCopyMessage("");
    try {
      // Reuse the current challenge: copying must not invalidate the open tab.
      const current = await readNativeAuthStatus();
      const value = current.login_url ? current : await desktopInvokeOrThrow<NativeAuthStatus>("desktop_auth_start", { expectedSubject, openBrowser: false });
      if (!validStatus(value)) throw new Error("The app returned an invalid connection status.");
      setStatus(value);
      if (!value.login_url) throw new Error("The sign-in link is unavailable. Update the Mac app or finish its pending account request.");
      const link = supymemSignInLink(value.login_url);
      if (!link) throw new Error("The Supymem sign-in link is unavailable. Start sign-in again or update the Mac app.");
      await navigator.clipboard.writeText(link);
      setCopyMessage("Supymem sign-in link copied. Open it in your browser within five minutes and approve this Mac.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not copy the sign-in link. Select the link below and copy it manually.");
    } finally { setBusy(false); }
  }

  async function completeFallbackCode() {
    if (busy || nativeAuthActionPending(status?.state) || !fallbackCode.trim()) return;
    setBusy(true); setError(""); setCopyMessage("");
    const code = fallbackCode.trim();
    setFallbackCode("");
    try {
      const value = await desktopInvokeOrThrow<NativeAuthStatus>("desktop_auth_complete_code", { code });
      if (!validStatus(value)) throw new Error("The app returned an invalid connection status.");
      setStatus(value);
      if (value.state === "connected") onConnected?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The approval code could not be verified. Restart sign-in if it expired.");
    } finally { setBusy(false); }
  }

  if (!desktop) return null;
  const connected = status?.state === "connected";
  const needsRetry = status && ["offline", "keychain_unavailable", "revocation_pending"].includes(status.state);
  const needsBrowserReconnect = status && ["reauth_required", "account_mismatch"].includes(status.state);
  const hasAccount = !!status?.subject;
  const actionPending = busy || nativeAuthActionPending(status?.state);
  const signIn = !compact && !hasAccount && (!status || status.state === "signed_out");
  const loginLink = supymemSignInLink(status?.login_url);
  return (
    <section aria-label={signIn ? "Sign in" : "Mac account connection"} data-dead-end={needsBrowserReconnect || needsRetry ? "reconnect_shown" : undefined} className={`panel-card desktop-account-card${compact ? " desktop-account-card-compact" : ""}`}>
      {!signIn && <h3 style={{ margin: 0 }}>Mac account connection</h3>}
      {!signIn && <p role="status" className="muted">{connected && !status.persisted
        ? "This test account is connected for this app session. Credentials stay in native memory and are discarded when the app closes."
        : status ? descriptions[status.state] : "Checking this Mac’s secure connection…"}</p>}
      {status?.error && !/^[a-z0-9_.:-]+$/.test(status.error) && (!nativeAuthActionPending(status.state) || status.error.startsWith("Authorization callback")) ? <p className="muted">{signInErrorMessage(status.error, "Supymem could not complete this connection. Start sign-in again.")}</p> : null}
      {status?.subject ? <details className="desktop-account-identity"><summary>Account details</summary><p className="muted">Account: {status.subject}</p></details> : null}
      {connected && status.bridge_connected === false ? <p className="muted" role="status">Mac tasks are unavailable while the local helper reconnects. Cloud work still works. Quit and reopen Supymem; if this persists, reinstall the signed app in Applications. Your saved sign-in and work are retained.</p> : null}
      {status?.state === "authorizing" && !status.login_url ? <p role="status" className="muted">The sign-in link expired or is unavailable. Start sign-in again to try again.</p> : null}
      <div className="desktop-account-actions">
        {error && <button type="button" disabled={actionPending} onClick={() => void act("desktop_auth_status")}>Retry</button>}
        {!connected && (!hasAccount || status?.state === "authorizing") && <button type="button" className="ghost sm" disabled={actionPending || !status} onClick={() => void copyLoginLink()}>Copy Supymem sign-in link</button>}
        {!connected && (!hasAccount || status?.state === "authorizing") && !needsRetry ? <button type="button" className="primary" data-usage-control="account_sign_in_start" disabled={actionPending || !status} onClick={() => void act("desktop_auth_start")}>
          {status?.state === "authorizing" ? "Start sign-in again" : "Sign in"}
        </button> : null}
        {needsBrowserReconnect ? <button type="button" data-usage-control="settings_account_reconnect" className="primary" disabled={actionPending} onClick={() => void act("desktop_auth_start")}>Reconnect this account</button> : null}
        {needsRetry || connected ? <button type="button" data-usage-control="settings_account_reconnect" disabled={actionPending} onClick={() => void act("desktop_auth_reconnect")}>
          {status?.state === "revocation_pending" ? "Retry device revocation" : "Reconnect"}
        </button> : null}
        {hasAccount || status?.state === "authorizing" ? <button type="button" data-usage-control="account_sign_out" disabled={actionPending} onClick={() => void act("desktop_auth_sign_out")}>Sign out this Mac</button> : null}
      </div>
      {status?.state === "authorizing" && loginLink && <label>Supymem sign-in link (expires after five minutes)<input aria-label="Supymem sign-in link" readOnly value={loginLink} onFocus={(event) => event.target.select()} /></label>}
      {waiting && phase === "waiting" && <p role="status" className="muted">Waiting for your browser. This usually takes under a minute. If Supymem does not come forward on its own, a code step appears here.</p>}
      {waiting && phase === "paste_code" && !showCode && <p role="status" className="muted">Still signing in? Finish in your browser; this window continues by itself. <button type="button" className="ghost sm" onClick={() => setShowCode(true)}>Supymem did not open? Use a code</button></p>}
      {waiting && (phase === "expired" || (phase === "paste_code" && showCode)) && <div className="desktop-account-code" role="status" aria-live="polite">
        <h4 style={{ margin: "0.5rem 0 0.25rem" }}>{phase === "expired" ? "That sign-in link has expired" : "Paste the code from your browser"}</h4>
        {phase === "expired"
          ? <p className="muted">Press Start sign-in again above to get a fresh link, then paste the new code here.</p>
          : <ol className="muted" style={{ margin: "0 0 0.5rem 1.1rem", padding: 0 }}>{PASTE_CODE_STEPS.map((step) => <li key={step}>{step}</li>)}</ol>}
        <label>One-time approval code
          <input aria-label="One-time approval code" type="text" autoComplete="off" spellCheck={false} autoFocus={phase === "paste_code"}
            value={fallbackCode} onChange={(event) => setFallbackCode(event.target.value)} />
        </label>
        <button type="button" className="primary" data-usage-control="account_sign_in_code" disabled={actionPending || !fallbackCode.trim()} onClick={() => void completeFallbackCode()}>Complete connection</button>
      </div>}
      {copyMessage && <p role="status">{copyMessage}</p>}
      <ErrorNotice error={error ? signInErrorMessage(error, "Supymem could not complete this connection. Start sign-in again.") : ""} source="DesktopAccountConnection.error" />
    </section>
  );
}
