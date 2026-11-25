"use client";

import { useEffect, useRef, useState } from "react";
import { AccountSettings } from "@/components/settings/AccountSettings";
import { VentureSettings, type SettingsSection } from "@/components/settings/VentureSettings";
import { api, ensureSession, logout, verifiedSessionEpoch, verifiedSessionSubject, type SessionInfo } from "@/lib/client";
import { isDesktopShell } from "@/lib/desktop";
import { serverReason } from "@/lib/serverReason";
import styles from "./CompanySettingsDialog.module.css";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";

const NAV: { id: SettingsSection; label: string }[] = [
  { id: "venture", label: "Venture" },
  { id: "manager", label: "AI manager" },
  { id: "models", label: "Models" },
  { id: "mac", label: "This Mac" },
  { id: "data", label: "Your data" },
  { id: "memory", label: "Memory" },
];

export function CompanySettingsDialog({ ventureId, accountLabel, initialSection, onClose }: { ventureId?: string; accountLabel?: string; initialSection?: SettingsSection | "account"; onClose: () => void }) {
  const local = useLocalMode();
  const dialog = useRef<HTMLDialogElement>(null);
  const [section, setSection] = useState<SettingsSection | "account">(initialSection || (ventureId ? "venture" : "account"));
  const [query, setQuery] = useState("");
  const [account, setAccount] = useState<{ label: string; subject: string; epoch: number } | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const logoutBusy = useRef(false);
  const accountRead = useRef(0);
  const items = NAV.filter(item => item.label.toLowerCase().includes(query.trim().toLowerCase()));
  const current = NAV.find(item => item.id === section) || NAV[0];
  const label = accountLabel || (account?.subject === verifiedSessionSubject() && account?.epoch === verifiedSessionEpoch() ? account.label : "") || "Account";
  useEffect(() => {
    const current = dialog.current;
    current?.showModal();
    return () => current?.close();
  }, []);
  useEffect(() => {
    if (section !== "account" && !items.some(item => item.id === section) && items[0]) setSection(items[0].id);
  }, [query, section]);
  useEffect(() => {
    let live = true;
    const load = async () => {
      const ticket = ++accountRead.current;
      setAccount(null);
      if (logoutBusy.current) return;
      const session = await ensureSession();
      const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
      if (!live || ticket !== accountRead.current || !session.ok || !subject) return;
      const email = (session.data as SessionInfo | null)?.email || "";
      setAccount({ label: email, subject, epoch });
      const greeting = await api("/api/me/greeting");
      if (!live || ticket !== accountRead.current || subject !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch()) return;
      const name = (greeting.data as { first_name?: unknown } | null)?.first_name;
      if (greeting.ok && typeof name === "string" && name.trim()) setAccount({ label: name.trim(), subject, epoch });
    };
    const refresh = () => { void load().catch(() => undefined); };
    refresh(); window.addEventListener("supymem-session-changed", refresh);
    return () => { live = false; window.removeEventListener("supymem-session-changed", refresh); };
  }, []);
  async function doLogout() {
    if (logoutBusy.current) return;
    logoutBusy.current = true; setSigningOut(true); setLogoutError("");
    try {
      const result = await logout();
      if (!result.ok) { setLogoutError(serverReason(result, result.error || "Could not sign out. Try again.")); return; }
      const target = (result.data as { upstream_logout_url?: unknown } | null)?.upstream_logout_url;
      onClose();
      if (!isDesktopShell() && typeof target === "string" && /^https:\/\//.test(target)) window.location.assign(target);
      else window.location.assign("/");
    } catch { setLogoutError("Could not sign out. Try again."); }
    finally { logoutBusy.current = false; setSigningOut(false); }
  }
  return <dialog ref={dialog} className={styles.overlay} onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }} aria-label="Settings">
    <div className={styles.window}>
      <aside className={styles.side}>
        <div className={styles.who}><span aria-hidden="true">{label.slice(0, 1).toUpperCase()}</span><strong>{label}</strong></div>
        <input className={styles.search} value={query} onChange={event => setQuery(event.target.value)} placeholder="Search settings" aria-label="Search settings" />
        {ventureId && <nav className={styles.nav} aria-label="Settings sections">
          {items.map(item => <button type="button" key={item.id} aria-current={item.id === section ? "page" : undefined} onClick={() => setSection(item.id)}>{item.label}</button>)}
          {items.length === 0 && <p className={styles.empty}>No matching settings</p>}
        </nav>}
        <button type="button" aria-current={section === "account" ? "page" : undefined} onClick={() => setSection("account")}>Account settings</button>
        {!local && <div className={styles.foot}>
          {logoutError && <p role="status" className={styles.empty}>{logoutError}</p>}
          <button type="button" disabled={signingOut} onClick={() => void doLogout()}>{signingOut ? "Signing out…" : "Log out"}</button>
        </div>}
      </aside>
      <section className={styles.pane}>
        <header className={styles.paneHead}><h2>{ventureId && section !== "account" ? current.label : "Account"}</h2><button type="button" className={styles.close} onClick={onClose} aria-label="Close settings">×</button></header>
        <div className={styles.body}>{ventureId && section !== "account" ? <VentureSettings ventureId={ventureId} section={section} embedded onClose={onClose} /> : <AccountSettings />}</div>
      </section>
    </div>
  </dialog>;
}
