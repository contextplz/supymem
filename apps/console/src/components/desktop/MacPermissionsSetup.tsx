"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import {
  PERMISSIONS, RESTART_NOTE, continueLabel, markPermissionsSetupDone, rowAction, statusOf,
  type PermissionName, type PermissionSnapshot,
} from "@/lib/macPermissions";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const ICONS: Record<PermissionName, ReactNode> = {
  "screen-recording": <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></>,
  accessibility: <><circle cx="12" cy="5" r="2" /><path d="M5 9l7 1.5L19 9M12 10.5V15l-3 5M12 15l3 5" /></>,
  microphone: <><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0014 0M12 18v3" /></>,
  automation: <><path d="M8 6h13M8 12h13M8 18h13M3 6l1 1 2-2M3 12l1 1 2-2M3 18l1 1 2-2" /></>,
  notifications: <><path d="M6 9a6 6 0 1112 0c0 6 2 7 2 7H4s2-1 2-7M10 20a2 2 0 004 0" /></>,
};

export function MacPermissionsSetup({ onContinue }: { onContinue: () => void }) {
  const [snapshot, setSnapshot] = useState<PermissionSnapshot | null>(null);
  const [requested, setRequested] = useState<Set<PermissionName>>(new Set());
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!isDesktopShell() || document.visibilityState !== "visible") return;
    try {
      setSnapshot(await desktopInvokeOrThrow<PermissionSnapshot>("permissions_status"));
      setError("");
    } catch {
      setError("Supymem could not check your Mac settings. You can continue and set this up later.");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function act(name: PermissionName, button: "Allow" | "Open System Settings") {
    try {
      if (button === "Allow") {
        setRequested((prev) => new Set(prev).add(name));
        setSnapshot(await desktopInvokeOrThrow<PermissionSnapshot>("permissions_request", { name }));
      } else {
        await desktopInvokeOrThrow("permissions_open_settings", { name });
      }
    } catch {
      setError("Supymem could not open that setting. Open System Settings, then Privacy & Security, and turn Supymem on.");
    }
  }

  function done() {
    markPermissionsSetupDone();
    onContinue();
  }

  return (
    <div className="login-gate">
      <main className="login-card" style={{ maxWidth: 560, width: "100%", textAlign: "left" }} aria-labelledby="mac-permissions-title">
        <p className="login-kicker">Supymem</p>
        <h1 id="mac-permissions-title">Set up Supymem on this Mac</h1>
        <p className="muted">Supymem works without any of these. Turn on only what you want. Your Mac asks you to confirm each one, and you can change them later in System Settings.</p>
        <ul style={{ listStyle: "none", margin: "16px 0", padding: 0, display: "grid", gap: 10 }}>
          {PERMISSIONS.map((p) => {
            const action = rowAction(statusOf(snapshot, p.name), requested.has(p.name));
            return (
              <li key={p.name} style={{ display: "flex", gap: 12, alignItems: "center", padding: 12,
                border: "1px solid var(--line)", borderRadius: 12, background: "var(--bg-card)" }}>
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.7"
                  strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "none" }}>{ICONS[p.name]}</svg>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <strong>{p.title}</strong>
                  <p className="muted" style={{ margin: "2px 0 0", fontSize: "0.85rem" }}>
                    {p.reason}{p.restartNote ? ` ${RESTART_NOTE}` : ""}
                  </p>
                </div>
                <div style={{ flex: "none", textAlign: "right", minWidth: 96 }} aria-live="polite">
                  {action.granted
                    ? <span style={{ color: "var(--ok)", fontWeight: 600 }}>Granted ✓</span>
                    : <>
                        <div className="muted" style={{ fontSize: "0.8rem", marginBottom: 4 }}>Off</div>
                        <button type="button" className="primary sm" onClick={() => void act(p.name, action.button!)}>{action.button}</button>
                      </>}
                </div>
              </li>
            );
          })}
        </ul>
        <ErrorNotice error={error} source="MacPermissionsSetup.error" style={{ color: "var(--danger)" }} />
        <button type="button" className="primary" onClick={done}>{continueLabel(snapshot)}</button>
      </main>
    </div>
  );
}
