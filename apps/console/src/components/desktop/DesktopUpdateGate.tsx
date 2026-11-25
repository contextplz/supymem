"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { desktopInvokeOrThrow, desktopListen, isDesktopShell } from "@/lib/desktop";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type UpdateCheckResult = {
  available: boolean;
  current_version: string;
  version?: string | null;
  notes?: string | null;
  mandatory: boolean;
  min_version?: string | null;
  force: boolean;
  can_defer: boolean;
  honesty?: string | null;
  endpoint: string;
  check_id?: string | null;
};

export async function checkDesktopUpdate(): Promise<UpdateCheckResult | null> {
  return desktopInvokeOrThrow<UpdateCheckResult>("check_for_update");
}

export function DesktopUpdateGate() {
  const [desktop, setDesktop] = useState(false);
  const [result, setResult] = useState<UpdateCheckResult | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [userAsked, setUserAsked] = useState(false);
  const [progress, setProgress] = useState("");
  const checking = useRef(false);

  const apply = useCallback((next: UpdateCheckResult | null, asked: boolean) => {
    setResult(next);
    setError("");
    if (!next) {
      setOpen(asked);
      return;
    }
    if (next.available) {
      setOpen(true);
      return;
    }
    setOpen(asked);
  }, []);

  const runCheck = useCallback(
    async (asked: boolean) => {
      if (!isDesktopShell()) return;
      if (checking.current) return;
      checking.current = true;
      setUserAsked(asked);
      try {
        const next = await checkDesktopUpdate();
        apply(next, asked);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setOpen(asked);
      } finally { checking.current = false; }
    },
    [apply],
  );

  useEffect(() => {
    const installed = () => { if (isDesktopShell()) setDesktop(true); };
    installed();
    window.addEventListener("supymem-desktop-ready", installed);
    return () => window.removeEventListener("supymem-desktop-ready", installed);
  }, []);

  useEffect(() => {
    if (!desktop) return;
    void runCheck(false);
    let disposed = false;
    const unsubscribers: (() => void)[] = [];
    void desktopListen("supymem-update-progress", (value) => {
      const next = value as { phase?: string; downloaded?: number };
      if (next.phase === "downloading") setProgress(`Downloading update… ${Math.round((next.downloaded || 0) / 1024 / 1024)} MB received.`);
      if (next.phase === "installing") setProgress("Signature verified. Installing update…");
      if (next.phase === "restarting") setProgress("Update installed. Restarting Supymem…");
    }).then((fn) => { if (disposed) fn(); else unsubscribers.push(fn); });
    return () => { disposed = true; unsubscribers.forEach((fn) => fn()); };
  }, [desktop, runCheck]);

  if (!desktop || !open || (!result && !error)) return null;

  const force = Boolean(result?.available && result.force);
  const optional = Boolean(result?.available && !result.force);
  const title = force
    ? "Update required"
    : optional
      ? "Update available"
      : "Check for updates";

  async function install() {
    setBusy(true);
    setError("");
    try {
      if (!result?.check_id) throw new Error("Check for updates again to confirm this release.");
      await desktopInvokeOrThrow("install_update", { checkId: result.check_id });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  async function later() {
    if (force) return;
    try {
      await desktopInvokeOrThrow("defer_update");
      setOpen(false);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="cos-update-title"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: force ? 90 : 80,
        background: force ? "rgba(28, 25, 23, 0.72)" : "rgba(28, 25, 23, 0.4)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
      onClick={force || busy ? undefined : () => void later()}
    >
      <div
        className="panel-card"
        style={{ maxWidth: 440, width: "100%", pointerEvents: "auto" }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="cos-update-title" style={{ margin: 0 }}>
          {title}
        </h3>
        {result?.available ? (
          <p style={{ marginTop: 8 }}>
            Version {result.version} is ready
            {result.current_version ? ` (you have ${result.current_version})` : ""}.
            {force
              ? " Install this required update to continue using this desktop version."
              : " You can install now or later, we’ll remind you next launch."}
          </p>
        ) : (
          <p className="muted" style={{ marginTop: 8 }}>
            {result?.honesty || "No newer signed release."}
          </p>
        )}
        {result?.notes ? <p className="muted">{result.notes}</p> : null}
        {userAsked && result?.honesty && result.available === false ? (
          <p className="muted" style={{ marginTop: 8 }}>
            Channel: {result.endpoint}
          </p>
        ) : null}
        {busy && progress ? <p role="status">{progress}</p> : null}
        <ErrorNotice error={error} source="DesktopUpdateGate.error" style={{ color: "var(--danger)", marginTop: 8 }} />
        <div style={{ display: "flex", gap: 8, marginTop: 16, justifyContent: "flex-end" }}>
          {error && !busy ? <button type="button" onClick={() => void runCheck(true)}>Check again</button> : null}
          {optional ? (
            <button type="button" className="ghost" disabled={busy} onClick={() => void later()}>
              Later
            </button>
          ) : null}
          {result?.available ? (
            <button type="button" className="primary" disabled={busy} onClick={() => void install()}>
              {busy ? "Updating…" : force ? "Update now" : "Install now"}
            </button>
          ) : (
            <button type="button" onClick={() => setOpen(false)}>
              OK
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
