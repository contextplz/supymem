"use client";

import { useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import styles from "./local.module.css";

const STAGED_RESTORE = "Your restore is already ready. Quit and reopen your app to finish it.";
const MAINTENANCE_MESSAGES = new Set([
  "Choose your ZIP data export, then retry the import.",
  "Your import needs more free disk space. Free some space, then retry.",
  "Your restore needs more free disk space. Free some space, then retry in Keys and connections, under Your data and recovery.",
  "Your archive could not be verified. Open Keys and connections, under Your data and recovery, choose an intact data export, then retry.",
  "Your archive could not be verified. Open Keys and connections, under Your data and recovery, choose an intact backup for your configured database, then retry.",
  "Your archive uses a different backup version. Open it with the app version that created it, update that app, then export again in Keys and connections, under Your data and recovery.",
  "Your archive needs a newer app. Update your app, then retry in Keys and connections, under Your data and recovery.",
  "Your export uses an older data version. Open your original data, update that app, then export again in Keys and connections, under Your data and recovery.",
  "Choose one of your listed backups, then retry your restore.",
  STAGED_RESTORE,
  "Your backup folder belongs to another account. Choose a folder you own in Keys and connections, under Your data and recovery, then retry.",
  "Your backup settings could not be opened. Restore your saved settings or choose your backup folder again in Keys and connections, under Your data and recovery, then retry.",
  "Choose a backup folder outside your data directory in Keys and connections, under Your data and recovery, then retry.",
  "Your data directory contains a link or special file. Replace it with a regular file, then retry.",
  "Your data changed while preparing your backup. Retry your backup in Keys and connections, under Your data and recovery.",
  "Copying your saved data took too long. Close your other app window, check free disk space, then retry.",
  "Update your app before opening this data folder. Your saved work has not been changed.",
  "Your export does not identify the app version that saved it. Update the app with your original data, then export again in Keys and connections, under Your data and recovery.",
  "Your data's app version could not be verified. Keep your files and choose an intact copy in Keys and connections, under Your data and recovery.",
  "Your recovery app's version could not be verified. Use the recovery command from the same release that saved this data, or open that release and use Keys and connections, under Your data and recovery.",
  "This backup contains an old recovery key. Keep it private, open it with the app that created it, then create a new backup in Keys and connections, under Your data and recovery.",
  "Your restore contains a link or special file. Choose an intact copy in Keys and connections, under Your data and recovery, then retry.",
]);

async function maintenanceMessage(response: Response): Promise<string> {
  const data = await response.json().catch(() => null) as { detail?: unknown } | null;
  const detail = data?.detail;
  return isMaintenanceMessage(detail) ? detail : "";
}

function isMaintenanceMessage(detail: unknown): detail is string {
  return typeof detail === "string" && (MAINTENANCE_MESSAGES.has(detail)
    || /^This file name cannot be restored on every computer: .+\. Rename it in your data folder, then retry in Keys and connections, under Your data and recovery\.$/.test(detail)
    || /^(Choose|Enter|Your backup settings could not be opened\.).*Full backup folder path in Keys and connections, under Your data folder and backup, then retry\.$/.test(detail));
}

function omissionNotice(response: Response, kind: "backup" | "data export"): string {
  const count = Number(response.headers.get("X-Supymem-Skipped-Files"));
  return Number.isSafeInteger(count) && count > 0
    ? `Your ${kind} is partial. It skipped ${count} linked or special ${count === 1 ? "file" : "files"}. Copy any files you need into your data folder as regular files, then try again in Your data and recovery.`
    : "";
}

export function DataSettings() {
  const [backups, setBackups] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [note, setNote] = useState("");
  const [transferError, setTransferError] = useState("");
  const [busy, setBusy] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const running = useRef(false);
  const active = useRef(true);
  const loadingBackups = useRef(false);

  async function load() {
    if (loadingBackups.current) return;
    loadingBackups.current = true;
    setLoading(true);
    try {
      const response = await fetch("/api/local/settings/backups", { cache: "no-store", credentials: "same-origin" });
      if (!response.ok) throw new Error(await maintenanceMessage(response));
      const data = await response.json();
      if (!Array.isArray(data.backups) || !data.backups.every((name: unknown) => typeof name === "string")) throw new Error();
      if (active.current) { setBackups(data.backups); setLoadError(""); }
    } catch (error) {
      if (active.current) setLoadError(error instanceof Error && isMaintenanceMessage(error.message) ? error.message
        : "Your backups could not be loaded. Open Settings > Keys and connections > Your data folder and backup, check your backup folder permissions, then choose Refresh your backups.");
    } finally {
      loadingBackups.current = false;
      if (active.current) setLoading(false);
    }
  }

  useEffect(() => {
    active.current = true;
    void load();
    return () => { active.current = false; };
  }, []);

  async function transfer(action: "export" | "import" | "restore" | "backup", body?: File | string) {
    if (running.current) return;
    running.current = true;
    const desktop = isDesktopShell();
    const restart = desktop ? "Quit and reopen your app to finish." : "Press Ctrl+C in the terminal where your app is running. To finish, run ./start --web on macOS or Linux, or .\\start.ps1 --web in Windows PowerShell.";
    setBusy(true);
    setTransferError("");
    setNote(action === "export" ? "Exporting your data. Please wait." : action === "import" ? "Importing your data. Please wait." : action === "backup" ? "Creating your backup. Please wait." : "Restoring your backup. Please wait.");
    try {
      const response = await fetch(`/api/local/settings/${action}`, {
        method: action === "export" ? "GET" : "POST",
        cache: "no-store", credentials: "same-origin",
        ...(action === "export" ? {} : { headers: { "Content-Type": action === "import" ? "application/zip" : "application/json" },
          body: action === "import" ? body : JSON.stringify({ body }) }),
      });
      if (!response.ok) throw new Error(await maintenanceMessage(response));
      if (action === "export") {
        const archive = await response.blob();
        if (!active.current) return;
        const url = URL.createObjectURL(archive);
        try {
          const link = document.createElement("a");
          link.href = url;
          link.download = "my-data.zip";
          link.click();
          setNote(omissionNotice(response, "data export") || "Your data export is ready. Keep it private. To bring it back, choose the ZIP under Your data and recovery, then choose Replace workspace with this ZIP.");
        } finally {
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
      } else if (action === "backup") {
        if (!active.current) return;
        setNote(omissionNotice(response, "backup") || "Your backup is ready. Keep it private. Choose Restore your backup to recover your data and encrypted settings. Keep your recovery key separate.");
        await load();
      } else {
        if (!active.current) return;
        setNote(`Your replacement is ready. Your current data is backed up first. ${desktop ? "If your app does not restart, quit and reopen it to finish." : restart}`);
        if (desktop) {
          try { await desktopInvokeOrThrow("relaunch_app"); }
          catch {
            if (active.current) setNote(`Your replacement is ready. ${restart}`);
          }
        }
      }
    } catch (error) {
      if (active.current) {
        setNote("");
        setTransferError(error instanceof Error && error.message === STAGED_RESTORE ? `Your replacement is already ready. ${restart}`
          : error instanceof Error && isMaintenanceMessage(error.message) ? error.message : action === "backup"
          ? "Your backup could not be created. Open Settings > Keys and connections > Your data folder and backup, check your backup folder permissions and free disk space, then choose Create backup again."
          : action === "export"
          ? "Your export could not finish. Check your free disk space and local app, then try again."
          : `Your data could not be imported or restored. Choose an intact archive, check your free disk space, then retry. If you already started the replacement: ${restart}`);
      }
    } finally {
      running.current = false;
      if (active.current) setBusy(false);
    }
  }

  return (
    <section className={styles.section} aria-labelledby="your-data-title">
      <h2 id="your-data-title">Your data and recovery</h2>
      <p>Your app backs up your database and files before each update. Your export includes your database and files, not your saved keys. Pause your work before exporting so your files do not change while they are copied.</p>
      <p>Full backups include encrypted settings, not your recovery key. To recover your saved connections, unlock your original system keychain or restore your matching private recovery key file. If neither is available, reconnect your accounts on Keys and connections.</p>
      <button type="button" disabled={busy} onClick={() => void transfer("export")}>Export my data</button>
      <button type="button" disabled={busy} onClick={() => void transfer("backup")}>Create backup</button>
      <div className={styles.field}>
        <label htmlFor="your-data-import">Choose a ZIP data export</label>
        <p id="your-data-import-warning">Importing a ZIP replaces your current workspace. It does not merge workspaces. Your current data is backed up first.</p>
        <input id="your-data-import" type="file" accept=".zip,application/zip" disabled={busy} aria-describedby="your-data-import-warning"
          onChange={event => setImportFile(event.target.files?.[0] ?? null)} />
        <button type="button" disabled={busy || !importFile} aria-describedby="your-data-import-warning"
          onClick={() => { if (importFile) void transfer("import", importFile); }}>Replace workspace with this ZIP</button>
      </div>
      {loading && <p role="status">Loading your backups. Please wait.</p>}
      {!loading && <ErrorNotice error={loadError} source="settings.local.data" intent="data.backups" />}
      {!loading && !loadError && backups?.length === 0 && <p role="status">No backups yet. Choose Create backup to keep a private copy of your data and encrypted settings.</p>}
      {backups?.map(name => <p key={name}><button type="button" disabled={busy} onClick={() => void transfer("restore", name)}>Restore your backup: {name}</button></p>)}
      <button type="button" disabled={busy || loading} onClick={() => void load()}>Refresh your backups</button>
      <p role="status" aria-live="polite">{note}</p>
      <ErrorNotice error={transferError} source="settings.local.data" intent="data.transfer" />
    </section>
  );
}
