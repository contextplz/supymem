"use client";

import { useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { contactsImportRequested, founderAssetsClient, type ContactImport, type ContactPreview } from "@/lib/founderAssetsApi";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import type { GtmOwner } from "@/lib/gtmDiagnosisIntent";
import styles from "./GtmJourney.module.css";

export function ImportContacts({ owner, disabled, save }: { owner: GtmOwner; disabled: boolean; save: (preview: ContactPreview, attested: boolean, basis: "consent" | "legitimate_interest", note: string) => Promise<ContactImport | undefined> }) {
  const [text, setText] = useState("");
  const [format, setFormat] = useState<"csv" | "paste">("csv");
  const [preview, setPreview] = useState<ContactPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [attested, setAttested] = useState(false);
  const [basis, setBasis] = useState<"consent" | "legitimate_interest">("consent");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const pending = useRef(false), version = useRef(0), mounted = useRef(true);
  const approval = useRef<{ preview: ContactPreview; version: number; basis: string; note: string } | null>(null);
  const renderVersion = version.current;
  const key = JSON.stringify([owner.actorId, owner.workspaceId, owner.ventureId]);
  const live = useRef(key); live.current = key;
  const epoch = verifiedSessionEpoch();
  const current = () => mounted.current && live.current === key && verifiedSessionEpoch() === epoch && verifiedSessionSubject() === owner.actorId;
  useEffect(() => {
    mounted.current = true;
    const retire = () => { mounted.current = false; version.current++; approval.current = null; setText(""); setPreview(null); setMapping({}); setAttested(false); setReceipt(""); setError(""); };
    window.addEventListener("supymem-session-changed", retire);
    return () => { mounted.current = false; version.current++; window.removeEventListener("supymem-session-changed", retire); };
  }, [key]);
  function edit(value: string, nextFormat = format) {
    version.current++; approval.current = null; setText(value); setFormat(nextFormat); setPreview(null); setMapping({}); setAttested(false); setReceipt(""); setError("");
  }
  async function readFile(file?: File) {
    if (!file || disabled || pending.current || !current()) return;
    const read = ++version.current;
    if (!/\.csv$/i.test(file.name) || file.size > 2 * 1024 * 1024) { setError("Choose a CSV file smaller than 2 MB, with at most 2,000 contacts."); return; }
    try { const value = await file.text(); if (current() && version.current === read) edit(value, "csv"); }
    catch { if (current() && version.current === read) setError("This file could not be read. Attach it again or paste the contacts."); }
  }
  async function check() {
    if (disabled || pending.current || !text.trim() || !current()) return;
    const read = version.current;
    pending.current = true; approval.current = null; setBusy(true); setError(""); setAttested(false); setReceipt("");
    try {
      const value = await founderAssetsClient.previewContacts(owner, text, format, Object.keys(mapping).length ? mapping : undefined);
      if (current() && version.current === read) { setPreview(value); setMapping(value.mapping); }
    } catch (cause) { if (current() && version.current === read) setError(cause instanceof Error ? cause.message : "Your contacts could not be previewed. Try again."); }
    finally { pending.current = false; if (current()) setBusy(false); }
  }
  async function commit() {
    if (disabled || pending.current || !preview || !attested || !current() || !preview.preview_id || Date.parse(preview.expires_at) <= Date.now()
      || approval.current?.preview !== preview || approval.current.version !== version.current || approval.current.basis !== basis || approval.current.note !== note) return;
    const read = version.current;
    pending.current = true; approval.current = null; setAttested(false); setBusy(true); setError("");
    try {
      const result = await save(preview, attested, basis, note.trim());
      if (result && current() && version.current === read) { setReceipt(`${result.imported} contacts imported. ${result.skipped} rows skipped. ${typeof result.message === "string" ? result.message : "Review them in the prospects list."}`); setPreview(null); setAttested(false); }
    } catch { if (current() && version.current === read) setError("The import outcome is unconfirmed. Refresh saved prospects before trying again."); }
    finally { pending.current = false; if (current()) setBusy(false); }
  }
  const expired = preview ? Date.parse(preview.expires_at) <= Date.now() : false;
  const openOnArrival = typeof window !== "undefined" && contactsImportRequested(window.location.search);
  return <details className={styles.details} data-testid="contacts-import" open={openOnArrival || undefined}><summary>Import your existing contacts</summary>
    <p className={styles.muted}>Upload a CSV or paste contacts. Preview first, then confirm your basis for contacting them. Importing does not send messages.</p>
    <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled || busy} data-usage-control="gtm_contacts_choose_file" onClick={() => fileInput.current?.click()}>Choose CSV file</button>
    <input ref={fileInput} type="file" accept=".csv,text/csv" hidden disabled={disabled || busy} data-usage-control="gtm_contacts_file" onChange={event => { void readFile(event.target.files?.[0]); event.target.value = ""; }} />
    <label className={styles.label}>Contact format<select className={styles.input} value={format} disabled={disabled || busy} data-usage-control="gtm_contacts_format" onChange={event => edit(text, event.target.value as "csv" | "paste")}><option value="csv">CSV with column headings</option><option value="paste">Pasted contacts</option></select></label>
    <label className={styles.label}>Your contacts<textarea className={styles.input} value={text} maxLength={2 * 1024 * 1024} disabled={disabled || busy} data-usage-control="gtm_contacts_text" placeholder={format === "csv" ? "name,email,company,status,last_contacted,country" : "Paste names, email addresses or LinkedIn links"} onChange={event => edit(event.target.value)} /></label>
    {preview && format === "csv" && <details><summary>Match CSV columns</summary>
      {([ ["name", "Name"], ["email", "Email"], ["company", "Company"], ["title", "Title"], ["linkedin_url", "LinkedIn"], ["notes", "Notes"], ["status", "Outreach status"], ["last_contacted", "Last contacted"], ["country", "Country code"] ] as const).map(([field, label]) => <label className={styles.label} key={field}>{label}<select className={styles.input} value={mapping[field] || ""} disabled={disabled || busy} data-usage-control="gtm_contacts_mapping" onChange={event => { if (!current() || version.current !== renderVersion) return; version.current++; approval.current = null; setMapping({ ...mapping, [field]: event.target.value }); setAttested(false); setPreview({ ...preview, preview_id: "" }); }}><option value="">Not included</option>{preview.columns.map(column => <option key={column} value={column}>{column}</option>)}</select></label>)}
    </details>}
    <button type="button" className={`${styles.button} ${styles.secondary}`} disabled={disabled || busy || !text.trim()} data-usage-control="gtm_contacts_preview" onClick={() => void check()}>{busy ? "Working…" : "Preview contacts"}</button>
    {preview && <section aria-label="Contact import preview">
      <p>{preview.valid_rows} valid contacts · {preview.duplicates} duplicates · {preview.invalid_rows} rows need correction.</p>
      {!preview.preview_id && <p role="status">Columns changed. Preview again before importing.</p>}
      <div style={{ overflowX: "auto" }}><table><thead><tr><th>Name</th><th>Email</th><th>Company</th><th>Outreach status</th><th>Last contacted</th><th>Check</th></tr></thead><tbody>{preview.rows.slice(0, 10).map(row => <tr key={row.row_number}><td>{row.name}</td><td>{row.email}</td><td>{row.company}</td><td>{row.status || "New"}</td><td>{row.last_contacted || "Not recorded"}</td><td>{row.duplicate ? "Already saved" : row.errors.length ? row.errors.join(" ") : row.email_check === "mx" ? "Email domain checked" : "Review email evidence"}</td></tr>)}</tbody></table></div>
      {preview.rows.length > 10 && <p className={styles.muted}>Showing the first 10 rows. The same checks apply to all {preview.total_rows} rows.</p>}
      <label className={styles.label}>Basis for contacting<select className={styles.input} value={basis} disabled={disabled || busy} data-usage-control="gtm_contacts_basis" onChange={event => { approval.current = null; setBasis(event.target.value as typeof basis); setAttested(false); }}><option value="consent">They agreed to be contacted</option><option value="legitimate_interest">Documented legitimate interest</option></select></label>
      <label className={styles.label}>Evidence of permission<textarea className={styles.input} maxLength={1000} value={note} disabled={disabled || busy} data-usage-control="gtm_contacts_permission_note" onChange={event => { approval.current = null; setNote(event.target.value); setAttested(false); }} /></label>
      <label className={styles.label}><span><input type="checkbox" checked={attested} disabled={disabled || busy || !preview.preview_id || expired} data-usage-control="gtm_contacts_attest" onChange={event => { if (!current() || version.current !== renderVersion || !preview.preview_id || expired) return; approval.current = event.target.checked ? { preview, version: renderVersion, basis, note } : null; setAttested(event.target.checked); }} /> I have a lawful basis to contact these people.</span></label>
      {expired && <p role="status">This preview expired. Preview the contacts again.</p>}
      <button type="button" className={`${styles.button} ${styles.secondary}`} data-usage-control="gtm_contacts_import" disabled={disabled || busy || !attested || !preview.preview_id || expired || !preview.valid_rows} onClick={() => void commit()}>Import {preview.valid_rows} contacts</button>
    </section>}
    <ErrorNotice error={error} source="gtm.contacts_import" />
    {receipt && <p role="status">{receipt}</p>}
  </details>;
}
