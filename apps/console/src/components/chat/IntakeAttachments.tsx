"use client";

import { useEffect, useRef, useState } from "react";
import { api, authenticatedFetch, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { droppedFiles, fileLimit, type IntakeFile, type NamedFile } from "@/lib/intakeFiles";
import { AttachFileButton } from "./AttachFileButton";

export function useIntakeAttachments(scope: string, ventureId?: string | null) {
  const [files, setFiles] = useState<IntakeFile[]>([]), [note, setNote] = useState("");
  const [uploading, setUploading] = useState(false);
  const active = useRef({ scope, epoch: verifiedSessionEpoch(), subject: verifiedSessionSubject() });
  const currentFiles = useRef(files); currentFiles.current = files;
  const flight = useRef(false), alive = useRef(true);
  const storageKey = `intake-files:${scope}`;
  useEffect(() => {
    alive.current = true;
    active.current = { scope, epoch: verifiedSessionEpoch(), subject: verifiedSessionSubject() };
    try { const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]"); setFiles(Array.isArray(saved) ? saved.filter(file => file.status === "read" && typeof file.evidence_id === "string") : []); } catch { setFiles([]); }
    setNote(""); setUploading(false);
    return () => { alive.current = false; };
  }, [storageKey, scope]);
  function current(owner: typeof active.current) { return alive.current && owner === active.current && owner.epoch === verifiedSessionEpoch() && owner.subject === verifiedSessionSubject(); }
  function update(change: (items: IntakeFile[]) => IntakeFile[]) {
    const next = change(currentFiles.current); currentFiles.current = next;
    try { sessionStorage.setItem(storageKey, JSON.stringify(next.filter(file => file.status === "read"))); } catch { }
    setFiles(next);
  }
  async function add(selected: NamedFile[]) {
    if (flight.current) { setNote("Wait for these files to finish, then add more. You can still remove or skip them."); return; }
    const owner = active.current;
    let count = currentFiles.current.filter(file => file.status !== "skipped").length;
    let total = currentFiles.current.filter(file => file.status !== "skipped").reduce((sum, file) => sum + file.size, 0);
    const accepted: Array<NamedFile & { id: string }> = [], rows: IntakeFile[] = [];
    for (const selection of selected.slice(0, 100)) {
      const id = crypto.randomUUID(), reason = fileLimit(selection.file, count, total);
      rows.push({ id, source_label: selection.name, size: selection.file.size, status: reason ? "skipped" : "reading", ...(reason ? { note: reason } : {}) });
      if (!reason) { count++; total += selection.file.size; accepted.push({ ...selection, id }); }
    }
    if (selected.length > 100) setNote("Showing the first 100 files; up to 50 can be read. Select smaller batches or remove files to continue.");
    update(items => [...items, ...rows]);
    flight.current = true; setUploading(true);
    try {
      for (const selection of accepted) {
        if (!current(owner)) break;
        if (!currentFiles.current.some(file => file.id === selection.id)) continue;
        const form = new FormData(); form.append("files", selection.file, selection.name);
        form.append("source_labels", selection.name);
        if (ventureId) form.append("venture_id", ventureId);
        try {
          const response = await authenticatedFetch("/api/entry/evidence/files", { method: "POST", body: form });
          if (!response.ok) throw new Error("Could not upload this file. Remove it and attach again, or paste its text.");
          const result = (await response.json()).files?.[0];
          if (!result || !["read", "skipped"].includes(result.status)) throw new Error("No saved result. Attach again or paste the text.");
          if (current(owner)) {
            if (result.evidence_id && !currentFiles.current.some(file => file.id === selection.id)) {
              const removed = await api(`/api/entry/evidence/files/${encodeURIComponent(result.evidence_id)}${ventureId ? `?venture_id=${encodeURIComponent(ventureId)}` : ""}`, { method: "DELETE" });
              if (current(owner) && !removed.ok) {
                update(items => [...items, { ...rows.find(file => file.id === selection.id)!, ...result }]);
                setNote("That file finished saving before removal. Try Remove again.");
              }
            } else update(items => items.map(file => file.id === selection.id ? { ...file, ...result } : file));
          }
        } catch (error) {
          if (current(owner)) update(items => items.map(file => file.id === selection.id ? { ...file, status: "skipped", note: error instanceof Error ? error.message : "Attach again or paste the text." } : file));
        }
      }
    } finally { flight.current = false; if (current(owner)) setUploading(false); }
  }
  async function drop(transfer: DataTransfer) {
    const owner = active.current, result = await droppedFiles(transfer);
    if (!current(owner)) return;
    if (result.note) setNote(result.note);
    await add(result.files);
  }
  function restore(saved: Array<{ evidence_id: string; source_label: string; size: number; status: "read" }> | undefined) {
    if (saved) update(items => [...items, ...saved.filter(file => !items.some(item => item.evidence_id === file.evidence_id)).map(file => ({ ...file, id: file.evidence_id }))]);
  }
  async function remove(id: string) {
    const owner = active.current, file = currentFiles.current.find(item => item.id === id);
    if (file?.evidence_id) {
      const result = await api(`/api/entry/evidence/files/${encodeURIComponent(file.evidence_id)}${ventureId ? `?venture_id=${encodeURIComponent(ventureId)}` : ""}`, { method: "DELETE" });
      if (!current(owner)) return;
      if (!result.ok) { setNote("Could not remove this saved file. Try Remove again."); return; }
    }
    update(items => items.filter(file => file.id !== id));
  }
  return { files, note, uploading, add, drop, restore, remove,
    evidenceIds: files.filter(file => file.status === "read" && file.evidence_id).map(file => file.evidence_id!) };
}

export function IntakeAttachments({ attachments, disabled }: { attachments: ReturnType<typeof useIntakeAttachments>; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null), folder = useRef<HTMLInputElement>(null);
  return <div>
    <AttachFileButton disabled={disabled || attachments.uploading} onClick={() => input.current?.click()} />
    <button type="button" className="ghost sm" disabled={disabled || attachments.uploading} onClick={() => folder.current?.click()}>Attach folder</button>
    <input ref={input} type="file" multiple hidden disabled={disabled} onChange={event => { void attachments.add(Array.from(event.target.files || []).map(file => ({ file, name: file.webkitRelativePath || file.name }))); event.target.value = ""; }} />
    <input ref={folder} type="file" multiple {...{ webkitdirectory: "" }} hidden disabled={disabled} onChange={event => { void attachments.add(Array.from(event.target.files || []).map(file => ({ file, name: file.webkitRelativePath || file.name }))); event.target.value = ""; }} />
    <small>PDF, DOCX, PPTX, XLSX/CSV, Markdown, text and code. Up to 50 files, 25 MB each, 100 MB total.</small>
    {attachments.note && <p role="status">{attachments.note}</p>}
    <ul aria-label="Attached files" aria-live="polite" style={{ maxHeight: "12rem", overflow: "auto", overflowWrap: "anywhere" }}>
      {attachments.files.map(file => <li key={file.id}>
        <span>{file.source_label}, {file.status}{file.note ? `: ${file.note}` : ""}</span>{" "}
        <button type="button" className="ghost sm" disabled={disabled} aria-label={`Remove ${file.source_label}`} onClick={() => void attachments.remove(file.id)}>Remove</button>
      </li>)}
    </ul>
  </div>;
}
