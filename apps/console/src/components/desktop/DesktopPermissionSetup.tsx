"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import {
  macTaskPermissionsGranted, markPermissionRequested, permissionRequestedBefore,
  screenPermissionAction, statusOf, type PermissionSnapshot,
} from "@/lib/macPermissions";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Props = { inline?: boolean; task?: boolean; onGranted?: () => void | Promise<unknown> };

/** Settings and the first explicit Mac task share the non-prompting native preflight. */
export function DesktopPermissionSetup({ inline = true, task = false, onGranted }: Props) {
  const [snapshot, setSnapshot] = useState<PermissionSnapshot | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [requested, setRequested] = useState({
    "screen-recording": permissionRequestedBefore("screen-recording"),
    accessibility: permissionRequestedBefore("accessibility"),
  });
  const busy = useRef(false);
  const live = useRef(true);
  const completed = useRef(false);

  const refresh = useCallback(async (resume = false) => {
    if (!isDesktopShell() || busy.current) return;
    busy.current = true;
    setChecking(true);
    try {
      const next = await desktopInvokeOrThrow<PermissionSnapshot>("permissions_status", undefined, { quiet: !resume });
      if (!live.current) return;
      setSnapshot(next);
      setError("");
      if (resume && macTaskPermissionsGranted(next) && !completed.current) {
        completed.current = true;
        await onGranted?.();
      }
    } catch {
      if (live.current) {
        setSnapshot(null);
        setError("Supymem could not check your Mac settings. Reopen Supymem, then press Retry.");
      }
    } finally {
      busy.current = false;
      if (live.current) setChecking(false);
    }
  }, [onGranted]);

  useEffect(() => {
    live.current = true;
    void refresh();
    return () => { live.current = false; };
    // Only the initial passive check runs automatically. Grant and Retry are explicit clicks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function request(name: "screen-recording" | "accessibility") {
    if (busy.current || permissionRequestedBefore(name) || screenPermissionAction(statusOf(snapshot, name), requested[name]) !== "grant") return;
    busy.current = true;
    setChecking(true);
    setRequested(previous => ({ ...previous, [name]: true }));
    try {
      markPermissionRequested(name);
      // The helper request also probes Screen Recording. Native request returns only passive preflight.
      const next = await desktopInvokeOrThrow<PermissionSnapshot>("permissions_request", { name });
      if (!live.current) return;
      setSnapshot(next);
      setError("");
      if (macTaskPermissionsGranted(next) && !completed.current) {
        completed.current = true;
        await onGranted?.();
      }
    } catch {
      if (live.current) setError("Open System Settings, then Privacy & Security, and turn Supymem on. Press Retry when you are done.");
    } finally {
      busy.current = false;
      if (live.current) setChecking(false);
    }
  }

  async function openSettings(name: "screen-recording" | "accessibility") {
    try {
      await desktopInvokeOrThrow("permissions_open_settings", { name });
      if (live.current) setError("");
    } catch {
      if (live.current) setError("Open System Settings, then Privacy & Security, and turn Supymem on.");
    }
  }

  if (!isDesktopShell()) return null;
  const screenGranted = statusOf(snapshot, "screen-recording") === "granted";
  const names = task ? [screenGranted ? "accessibility" as const : "screen-recording" as const]
    : ["screen-recording", "accessibility"] as const;
  const granted = macTaskPermissionsGranted(snapshot);
  return <section className="panel-card" style={{width: "100%", padding: 20, marginBottom: inline ? 12 : undefined}} aria-label="Mac permissions">
    <h2 style={{margin: 0, fontSize: 22}}>{task ? "Allow this Mac task" : "Mac permissions"}</h2>
    <p className="muted" style={{marginTop: 8}}>{task
      ? granted ? "Permissions are on. Press Retry to continue your Mac task."
        : screenGranted ? "Supymem needs Accessibility to click and type in the app you chose."
        : "Supymem needs Screen Recording to see the app it's helping you with."
      : "These are macOS permission statuses. Granting a permission does not make a task available or verify background control."}</p>
    {task && !granted && <p className="muted">Used only for a Mac task you review and approve.</p>}
    <div style={{display: "grid", gap: 10}}>{names.map(name => {
      const status = statusOf(snapshot, name);
      const action = screenPermissionAction(status, requested[name]);
      const title = name === "screen-recording" ? "Screen Recording" : "Accessibility";
      return <div key={name}>
        {!task && <strong>{title}, {status === "granted" ? "Granted" : status === "unknown" ? "Not checked" : "Not granted"}</strong>}
        {action === "grant" && <button type="button" className={task ? "primary" : "ghost"} disabled={checking}
          data-usage-control={name === "screen-recording" ? "settings_permission_screen_request" : "settings_accessibility_request"}
          onClick={() => void request(name)}>Grant permission</button>}
        {(action === "settings" || (action === "retry" && requested[name])) && <button type="button" className="ghost" disabled={checking}
          data-usage-control="permissions_coach_open" onClick={() => void openSettings(name)}>Open System Settings</button>}
      </div>;
    })}</div>
    <p className="muted">If your Mac asks, quit and reopen Supymem. Then press Retry.</p>
    <ErrorNotice error={error} source="DesktopPermissionSetup.error" />
    <button type="button" className="ghost" data-usage-control="settings_permissions_refresh" disabled={checking}
      onClick={() => void refresh(true)}>{checking ? "Checking…" : "Retry"}</button>
  </section>;
}

/** Mounted only after an explicit computer-use action needs permissions. */
export function DesktopTaskPermissionCard({ onGranted }: { onGranted: () => void | Promise<unknown> }) {
  return <DesktopPermissionSetup inline task onGranted={onGranted} />;
}
