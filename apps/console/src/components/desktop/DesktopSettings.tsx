"use client";

import { useEffect, useState, type FormEvent } from "react";
import { desktopInvoke, desktopInvokeOrThrow, invokeErrorMessage, isDesktopShell } from "@/lib/desktop";
import { api } from "@/lib/client";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { DesktopAccountConnection } from "./DesktopAccountConnection";
import { DesktopPermissionSetup } from "./DesktopPermissionSetup";
import {
  macNoticeDiagnosticLine,
  type MacNoticeDiagnostic,
} from "@/lib/macNoticeDiagnostic";

type RoleStatus = {
  role: string;
  port: number;
  healthy: boolean;
  owned: boolean;
};

type DockerStatus = {
  installed?: boolean;
  engine_running?: boolean;
  message?: string;
};

export function DesktopSettings({ ventureId }: { ventureId?: string }) {
  const local = useLocalMode();
  const [desktop, setDesktop] = useState(false);
  const [autostart, setAutostart] = useState(false);
  const [autostartBusy, setAutostartBusy] = useState(false);
  const [cursorOn, setCursorOn] = useState(true);
  const [cursorIdle, setCursorIdle] = useState(8);
  const [dataDir, setDataDir] = useState("");
  const [roles, setRoles] = useState<RoleStatus[]>([]);
  const [note, setNote] = useState("");
  const [version, setVersion] = useState("");
  const [docker, setDocker] = useState<DockerStatus | null>(null);
  const [dockerBusy, setDockerBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [happened, setHappened] = useState("");
  const [contact, setContact] = useState("");
  const [includeAccount, setIncludeAccount] = useState(false);
  const [reportNote, setReportNote] = useState("");
  const [sending, setSending] = useState(false);
  const [noticeDiag, setNoticeDiag] = useState<MacNoticeDiagnostic | null>(null);
  const [noticeBusy, setNoticeBusy] = useState(false);
  const [prefsReady, setPrefsReady] = useState(false);
  const [prefsLoading, setPrefsLoading] = useState(true);
  const [prefsError, setPrefsError] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (!isDesktopShell()) return;
    let live = true;
    setDesktop(true);
    setPrefsReady(false); setPrefsLoading(true); setPrefsError("");
    void Promise.all([
      desktopInvokeOrThrow<boolean>("autostart_enabled"),
      desktopInvokeOrThrow<{ enabled: boolean; idle_hide_secs: number }>("agent_cursor_prefs"),
    ]).then(([enabled, cursor]) => {
      if (!live) return;
      if (typeof enabled !== "boolean" || !cursor || typeof cursor.enabled !== "boolean" || !Number.isFinite(cursor.idle_hide_secs)) throw new Error();
      setAutostart(enabled); setCursorOn(cursor.enabled); setCursorIdle(cursor.idle_hide_secs); setPrefsReady(true);
    }).catch(() => { if (live) setPrefsError("Couldn’t read your saved Mac settings. Try again before changing them."); })
      .finally(() => { if (live) setPrefsLoading(false); });
    return () => { live = false; };
  }, [refresh]);

  useEffect(() => {
    if (!isDesktopShell()) return;
    let live = true;
    (async () => {
      const [dir, status, ver, dock, diag] = await Promise.all([
        desktopInvoke<string>("data_dir_path"),
        desktopInvoke<RoleStatus[]>("backend_status"),
        desktopInvoke<string>("app_version"),
        desktopInvoke<DockerStatus>("docker_status"),
        desktopInvoke<MacNoticeDiagnostic>("mac_notice_diagnostic"),
      ]);
      if (!live) return;
      if (dir) setDataDir(dir);
      if (Array.isArray(status)) setRoles(status);
      if (ver) setVersion(ver);
      if (dock) setDocker(dock);
      if (diag) setNoticeDiag(diag);
    })();
    return () => { live = false; };
  }, []);

  if (!desktop) return null;

  async function toggleAutostart() {
    if (!prefsReady || autostartBusy) return;
    const next = !autostart;
    setAutostartBusy(true);
    try {
      await desktopInvokeOrThrow("set_autostart", { enabled: next });
      setAutostart(next);
      setNote(next ? "Supymem will start when you log in." : "Login start off.");
    } catch (err) {
      setNote(`Couldn’t change login start: ${invokeErrorMessage(err, "The Mac app could not change this setting.")}`);
    } finally {
      setAutostartBusy(false);
    }
  }

  async function saveCursor(enabled: boolean, idle: number) {
    if (!prefsReady) return;
    const before = { enabled: cursorOn, idle: cursorIdle };
    setCursorOn(enabled);
    setCursorIdle(idle);
    try {
      await desktopInvokeOrThrow("set_agent_cursor_prefs", { enabled, idleHideSecs: idle });
    } catch (err) {
      setCursorOn(before.enabled);
      setCursorIdle(before.idle);
      setNote(`Couldn’t change the Supymem cursor: ${invokeErrorMessage(err, "The Mac app could not change this setting.")}`);
    }
  }

  async function reveal(path: string) {
    try {
      await desktopInvokeOrThrow("reveal_in_file_manager", { path });
    } catch (err) {
      setNote(local ? "Your folder could not be opened. Check that your app is running, then try again." : `Couldn’t open that folder: ${invokeErrorMessage(err, "The Mac app could not open it.")}`);
    }
  }

  async function checkUpdates() {
    try {
      await desktopInvokeOrThrow("request_update_check");
    } catch (err) {
      setNote(local ? "Your app could not check for updates. Check your connection, then try again." : `Couldn’t check for updates: ${invokeErrorMessage(err, "The Mac app could not open the updater.")}`);
    }
  }

  async function sendTestNotice() {
    setNoticeBusy(true);
    try {
      const diag = await desktopInvokeOrThrow<MacNoticeDiagnostic>("mac_notice_test");
      setNoticeDiag(diag);
    } catch (err) {
      setNote(local ? "Your test notification could not be sent. Check your notification permissions, then try again." : `Couldn’t send a test notification: ${invokeErrorMessage(err, "The Mac app could not send it.")}`);
    } finally {
      setNoticeBusy(false);
    }
  }

  async function submitBug(event: FormEvent) {
    event.preventDefault();
    if (sending || !title.trim() || !happened.trim()) return;
    setSending(true);
    setReportNote("");
    try {
      const result = await api("/api/desktop/bug-report", {
        method: "POST",
        body: JSON.stringify({
          title: title.trim(), body: happened.trim(), contact: contact.trim() || null,
          app_version: version || undefined, os: "macOS", include_account_id: includeAccount,
        }),
      });
      const report = result.data;
      if (!result.ok || !report || typeof report !== "object" || !("id" in report) ||
          typeof report.id !== "string" || !report.id || !("status" in report) || report.status !== "received") {
        setReportNote(local ? "Your report was not confirmed. Your text is still here. Check your local app and connection, then try again." : "The report was not confirmed. Your text is still here; check your connection and sign-in before trying again.");
        return;
      }
      setTitle("");
      setHappened("");
      setContact("");
      setReportNote(`Report received (${report.id}).`);
    } catch {
      setReportNote("The report was not confirmed. Your text is still here; try again when connected.");
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="panel-card" style={{ marginTop: "1rem" }}>
      <h3 style={{ margin: 0 }}>{local ? "Your desktop app" : "This Mac app"}</h3>
      {!local && <DesktopAccountConnection />}
      <div style={{ marginTop: 16 }}><DesktopPermissionSetup inline /></div>
      <p className="muted" style={{ marginTop: 8 }}>
        {version ? `Version ${version}. ` : ""}
        {local ? "Your work runs on your machine. Closing the window keeps your app running in the menu bar. Quit from the menu bar to stop your app and its work."
          : <>Closing the window hides Supymem to the menu bar. Hosted jobs continue independently.
        Quit from the menu bar to stop this app and its local helper.</>}
      </p>
      {prefsReady ? <><label className="muted" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12 }}>
        <input type="checkbox" data-usage-control="autostart_toggle" checked={autostart} disabled={autostartBusy} onChange={() => void toggleAutostart()} />
        Start Supymem when I log in
      </label>
      <label className="muted" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
        <input type="checkbox" checked={cursorOn} onChange={() => void saveCursor(!cursorOn, cursorIdle)} />
        Show Supymem cursor while it works on this Mac
      </label>
      {cursorOn ? (
        <label className="muted" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4, marginLeft: 24 }}>
          Hide it after
          <select value={cursorIdle} onChange={(e) => void saveCursor(true, Number(e.target.value))}>
            {[5, 8, 15, 30, 60].map((n) => <option key={n} value={n}>{n} seconds</option>)}
          </select>
          idle
        </label>
      ) : null}
      </> : <p role="status">{prefsLoading ? "Reading your saved Mac settings…" : prefsError}</p>}
      {prefsError && !prefsLoading && <button type="button" onClick={() => setRefresh(value => value + 1)}>Refresh saved settings</button>}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
        {dataDir ? (
          <button type="button" onClick={() => void reveal(dataDir)}>
            Reveal data folder
          </button>
        ) : null}
        {ventureId ? (
          <button
            type="button"
            onClick={() => {
              void (async () => {
                try {
                  const path = await desktopInvokeOrThrow<string>("venture_workspace_path", { ventureId });
                  if (path) await reveal(path);
                  else if (dataDir) await reveal(`${dataDir}/ventures/${ventureId}`);
                  else setNote(local ? "Your venture files could not be located. Check that your local app is running, then try again." : "Couldn’t locate your venture files. Try again when the Mac app is connected.");
                } catch { setNote(local ? "Your venture files could not be located. Check that your local app is running, then try again." : "Couldn’t locate your venture files. Try again when the Mac app is connected."); }
              })();
            }}
          >
            Reveal venture files
          </button>
        ) : null}
        <button type="button" data-usage-control="updater_check" onClick={() => void checkUpdates()}>
          Check for updates
        </button>
        <button type="button" data-usage-control="permissions_coach_open" onClick={() => void desktopInvokeOrThrow("open_setup_window").catch((err) => setNote(local ? "Your setup could not be opened. Check that your local app is running, then try again." : `Couldn’t open setup: ${invokeErrorMessage(err, "The Mac app could not open it.")}`))}>
          Set up this Mac
        </button>
      </div>
      <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--line-subtle, #eee)" }}>
        <h4 style={{ margin: 0 }}>Mac notifications</h4>
        <p className="muted" style={{ marginTop: 8 }} role="status">
          {macNoticeDiagnosticLine(noticeDiag)}
        </p>
        <button type="button" data-usage-control="mac_notice_test" disabled={noticeBusy} onClick={() => void sendTestNotice()}>
          {noticeBusy ? "Sending…" : "Send test notification"}
        </button>
      </div>
      {roles.length ? (
        <details><summary>Local helper details for support</summary><ul className="muted" style={{ marginTop: 12, paddingLeft: "1.1rem" }}>
          {roles.map((r) => (
            <li key={r.role}>
              {r.role}, port {r.port}: {r.healthy ? "Responding" : "Not responding"}
              {r.owned ? "" : " (already running)"}
            </li>
          ))}
        </ul></details>
      ) : null}
      {note ? <p className="muted">{note}</p> : null}
      {docker ? (
        <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--line-subtle, #eee)" }}>
          <h4 style={{ margin: 0 }}>Docker</h4>
          <p className="muted" style={{ marginTop: 8 }}>
            {docker.message || (local ? "Start Docker to use your local sandboxes and website preview. Install Docker if you have not already."
              : "Docker is needed for local sandboxes and landing-page preview. Cloud build still works without it.")}
          </p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
            {!docker.installed ? (
              <button
                type="button"
                className="primary"
                data-usage-control="local_tools_docker_install"
                disabled={dockerBusy}
                onClick={() => {
                  void (async () => {
                    setDockerBusy(true);
                    try {
                      const result = await desktopInvokeOrThrow<{ message?: string }>("install_docker");
                      setNote(result?.message || "Docker Desktop is installing.");
                      const next = await desktopInvoke<DockerStatus>("docker_status");
                      if (next) setDocker(next);
                    } catch (err) {
                      setNote(local ? "Docker could not be installed. Check your local app and connection, then try again." : `Couldn’t install Docker Desktop: ${invokeErrorMessage(err, "The Mac app could not install it.")}`);
                    } finally {
                      setDockerBusy(false);
                    }
                  })();
                }}
              >
                {dockerBusy ? "Working…" : "Install Docker Desktop"}
              </button>
            ) : null}
            {docker.installed && !docker.engine_running ? (
              <button
                type="button"
                className="primary"
                data-usage-control="local_tools_docker_install"
                disabled={dockerBusy}
                onClick={() => {
                  void (async () => {
                    setDockerBusy(true);
                    try {
                      const result = await desktopInvokeOrThrow<{ message?: string }>("start_docker");
                      setNote(result?.message || "Docker Desktop is starting.");
                      const next = await desktopInvoke<DockerStatus>("docker_status");
                      if (next) setDocker(next);
                    } catch (err) {
                      setNote(local ? "Docker could not be started. Open Docker on your machine, then try again." : `Couldn’t start Docker Desktop: ${invokeErrorMessage(err, "The Mac app could not start it.")}`);
                    } finally {
                      setDockerBusy(false);
                    }
                  })();
                }}
              >
                {dockerBusy ? "Working…" : "Start Docker"}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
      <details style={{ marginTop: 20 }}>
        <summary style={{ cursor: "pointer" }}>Report a bug</summary>
        <form onSubmit={(event) => void submitBug(event)} style={{ marginTop: 12, display: "grid", gap: 8 }}>
          <p className="muted" style={{ margin: 0 }}>
            {local ? "Your report includes only the information you enter and your app version. Keep private details out of your report, then choose Send report."
              : <>Send a report to Supymem using your signed-in connection. Local crash records remain
            on this Mac. This form sends only the information you enter and the app version.</>}
          </p>
          <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Title" maxLength={200} required />
          <textarea value={happened} onChange={(event) => setHappened(event.target.value)} placeholder="What happened?" rows={4} maxLength={8000} required />
          <input value={contact} onChange={(event) => setContact(event.target.value)} placeholder="Optional contact (email or handle)" maxLength={200} />
          {!local && <label className="muted" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" checked={includeAccount} onChange={(event) => setIncludeAccount(event.target.checked)} />
            Include my account ID with this report
          </label>}
          <button type="submit" className="primary" data-usage-control="feedback_bug_report" disabled={sending}>{sending ? "Sending…" : "Send report"}</button>
          {reportNote ? <p className="muted" role="status">{reportNote}</p> : null}
        </form>
      </details>
    </section>
  );
}
