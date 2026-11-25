"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { apiFailure } from "@/lib/apiFailure";
import { errorCodeCopy } from "@/lib/errorCopy";
import { api, authenticatedFetch, ensureSession, type ApiResult, type SessionInfo } from "@/lib/client";
import { contentRequestFence } from "@/lib/contentIntent";
import { activeImport, importState, importIntentKey, readImportIntent, saveImportIntent, type ImportIntent, type ImportOwner } from "@/lib/importIntent";
import { importAvailabilityMessage } from "@/lib/importAvailability";
import { defaultImportProductId } from "@/lib/importPresentation";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type App = { id: string; name: string };
type Capabilities = { ready: boolean; reasons?: string[]; maximum_archive_bytes?: number };
type Report = { file_count: number; size_bytes: number; stack_candidates: string[]; test_files: string[]; license_files: { path: string }[]; coverage_gaps: string[]; observations: { path: string; parser: string; facts: unknown }[]; manifest_digest: string };
type Run = { deleted?: boolean; source_available?: boolean; import_id: string; app_id: string; state: string; state_version: number; source: ImportIntent["source"]; source_snapshot_id?: string; report?: Report; error_code?: string; bytes: { acquired: number; retained: number }; upload_path?: string };
const errorText = (r: ApiResult) => apiFailure(r, "this import step");
const human = (value: string) => errorCodeCopy(value) ?? value.replace(/^import_/, "").replaceAll("_", " ");

export default function ImportPage() {
  const params = useParams(); const venture = String(params.ventureId || "");
  const [context, setContext] = useState<{ actor: string; workspace: string; name: string } | null>(null);
  const [error, setError] = useState(""); const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let stopped = false;
    const changed = () => { stopped = true; setContext(null); setRefresh(n => n + 1); };
    window.addEventListener("supymem-session-changed", changed);
    void (async () => {
      setContext(null); setError("");
      const auth = await ensureSession(); if (stopped) return;
      const session = auth.data as SessionInfo;
      if (!auth.ok || !session?.authenticated || !session.subject) { setError("Reconnect your account to import a project."); return; }
      const r = await api(`/api/ventures/${encodeURIComponent(venture)}`); if (stopped) return;
      const company = r.data as { workspace_id?: string; name?: string };
      if (!r.ok || !company?.workspace_id) { setError("This company is unavailable to your account."); return; }
      setContext({ actor: session.subject, workspace: company.workspace_id, name: company.name || "Your company" });
    })();
    return () => { stopped = true; window.removeEventListener("supymem-session-changed", changed); };
  }, [venture, refresh]);
  return <AppShell title="Import a project" ventureId={venture}><div className="chat-scroll"><div className="chat-inner">
    {error && <ErrorNotice error={error} source="import.connect"> {error} <button onClick={() => setRefresh(n => n + 1)}>Reconnect</button></ErrorNotice>}
    {!context && !error && <p role="status">Checking your company account…</p>}
    {context && <CompanyImport key={`${context.actor}:${context.workspace}:${venture}`} {...context} venture={venture} />}
  </div></div></AppShell>;
}

function CompanyImport({ actor, workspace, venture, name }: { actor: string; workspace: string; venture: string; name: string }) {
  const [apps, setApps] = useState<App[]>([]); const [selected, setSelected] = useState("");
  const [label, setLabel] = useState(""); const [slug, setSlug] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    void api(`/api/ventures/${encodeURIComponent(venture)}/apps`, { headers: { "X-Workspace-ID": workspace } }).then(r => {
      if (!alive.current) return;
      if (r.ok) {
        const rows = (r.data as { items?: App[] }).items || [];
        setApps(rows);
        setSelected(current => defaultImportProductId(current, rows));
      } else setError(errorText(r));
    });
    return () => { alive.current = false; };
  }, [workspace, venture]);
  async function create() {
    setBusy(true); setError("");
    const r = await api(`/api/ventures/${encodeURIComponent(venture)}/apps`, { method: "POST", headers: { "X-Workspace-ID": workspace, "Idempotency-Key": `import-app:${actor}:${venture}:${slug}` }, body: JSON.stringify({ name: label.trim(), slug: slug.trim() }) });
    if (!alive.current) return; setBusy(false);
    if (!r.ok) { setError(errorText(r)); return; }
    const app = r.data as App; setApps(rows => [...rows.filter(row => row.id !== app.id), app]); setSelected(app.id);
  }
  return <>
    <p>Bring existing source into <strong>{name}</strong>. Supymem checks archive safety and reads project metadata without running its code.</p>
    <div className="panel-card"><label htmlFor="import-app">Product receiving this source</label>
      <select id="import-app" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Choose a product</option>{apps.map(app => <option key={app.id} value={app.id}>{app.name}</option>)}</select>
      <details><summary>Create a product record</summary><label>Name <input value={label} onChange={e => setLabel(e.target.value)} /></label><label>Unique URL name <input value={slug} onChange={e => setSlug(e.target.value)} placeholder="my-product" /></label><button disabled={busy || !label.trim() || !/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(slug)} onClick={() => void create()}>Create product</button></details>
    </div>
    {error && <ErrorNotice error={human(error)} source="import.error" />}
    {selected && <Intake key={selected} owner={{ actor, workspace, venture, app: selected }} />}
  </>;
}

function Intake({ owner }: { owner: ImportOwner }) {
  const [caps, setCaps] = useState<Capabilities | null>(null); const [run, setRun] = useState<Run | null>(null); const [history, setHistory] = useState<Run[]>([]);
  const [mode, setMode] = useState<"github" | "zip">("github"); const [url, setUrl] = useState(""); const [ref, setRef] = useState(""); const [root, setRoot] = useState(""); const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [pending, setPending] = useState<ImportIntent | null>(null);
  const fence = useRef(contentRequestFence()); const polling = useRef(0);
  const base = `/api/ventures/${encodeURIComponent(owner.venture)}/apps/${encodeURIComponent(owner.app)}/imports`;
  const headers = { "X-Workspace-ID": owner.workspace }; const key = importIntentKey(owner);
  useEffect(() => {
    fence.current.activate(key); const current = fence.current.capture();
    const changed = () => fence.current.invalidate(); window.addEventListener("supymem-session-changed", changed);
    try { setPending(readImportIntent(localStorage.getItem(key), owner)); } catch { setError("Local recovery storage is unavailable; keep this window open during intake."); }
    void Promise.all([api(base + "/capabilities", { headers }), api(base, { headers })]).then(([cap, recent]) => {
      if (!current()) return;
      if (cap.ok) setCaps(cap.data as Capabilities); else setError(errorText(cap));
      if (recent.ok) { const rows = (recent.data as { items: Run[] }).items; setHistory(rows); if (rows.length) setRun(rows[0]); } else setError(errorText(recent));
    });
    return () => { fence.current.invalidate(); window.removeEventListener("supymem-session-changed", changed); };
  // All identity components are included in key; never reuse another owner's request.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, base]);
  useEffect(() => { if (run) setHistory(rows => [run, ...rows.filter(row => row.import_id !== run.import_id)]); }, [run]);
  async function refreshRun() {
    if (!run) return; const current = fence.current.capture(); const r = await api(base + "/" + run.import_id, { headers });
    if (!current()) return;
    if (r.ok) { setRun(r.data as Run); setError(""); } else setError(errorText(r));
  }
  useEffect(() => {
    if (!run || !activeImport(run.state) || busy || polling.current >= 12) return;
    const current = fence.current.capture();
    const timer = setTimeout(() => { if (current()) { polling.current += 1; void refreshRun(); } }, 5000);
    return () => clearTimeout(timer);
  // Bounded refresh, without a mutation replay or endless connection retry.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, busy]);
  async function submit(intent?: ImportIntent) {
    setBusy(true); setError(""); const current = fence.current.capture();
    try {
      let value = intent;
      if (!value) {
        if (mode === "zip") {
          if (!file || file.size > 32 * 1024 * 1024 || !file.size) throw new Error("Choose a ZIP of at most 32 MiB.");
          const sha = [...new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()))].map(x => x.toString(16).padStart(2, "0")).join("");
          value = { idempotency_key: crypto.randomUUID(), source: { source: "upload_zip", filename: file.name, size_bytes: file.size, sha256: sha, ...(root.trim() ? { root: root.trim() } : {}) } };
        } else value = { idempotency_key: crypto.randomUUID(), source: { source: "github_public", repo_url: url.trim(), ...(ref.trim() ? { ref: ref.trim() } : {}), ...(root.trim() ? { subdirectory: root.trim() } : {}) } };
      }
      if (!current()) return;
      localStorage.setItem(key, saveImportIntent(owner, value)); setPending(value);
      const r = await api(base, { method: "POST", headers: { ...headers, "Idempotency-Key": value.idempotency_key }, body: JSON.stringify({ source: value.source }) });
      if (!current()) return;
      if (!r.ok) {
        if ([400, 403, 404, 413, 415, 422, 429].includes(r.status)) { localStorage.removeItem(key); setPending(null); }
        throw new Error(errorText(r));
      }
      const saved = r.data as Run; setRun(saved); setHistory(rows => [saved, ...rows.filter(row => row.import_id !== saved.import_id)]);
      localStorage.removeItem(key); setPending(null); polling.current = 0;
    } catch (err) { if (current()) setError(err instanceof Error ? err.message : "The request outcome is unconfirmed. Restore its status before starting another."); }
    finally { if (current()) setBusy(false); }
  }
  async function upload() {
    if (!run?.upload_path || !file) return;
    setBusy(true); setError(""); const current = fence.current.capture();
    try {
      const expected = run.source;
      const sha = [...new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()))].map(x => x.toString(16).padStart(2, "0")).join("");
      if (expected.source !== "upload_zip" || file.size !== expected.size_bytes || sha !== expected.sha256) throw new Error("Select the exact ZIP used to create this intake.");
      if (!current()) return;
      const response = await authenticatedFetch("/api" + run.upload_path, { method: "PUT", headers: { ...headers, "Content-Type": "application/zip" }, body: file });
      const data = await response.json(); if (!current()) return;
      if (!response.ok) throw new Error(errorText({ ok: false, status: response.status, data }));
      setRun(data); polling.current = 0;
    } catch (err) { if (current()) setError((err instanceof Error ? err.message : "Upload outcome unconfirmed.") + " Refresh the intake status before retrying."); }
    finally { if (current()) setBusy(false); }
  }
  async function action(kind: "cancel" | "retry") {
    if (!run) return; setBusy(true); const current = fence.current.capture();
    const r = await api(base + "/" + run.import_id + "/" + kind, { method: "POST", headers, body: JSON.stringify({ state_version: run.state_version }) });
    if (!current()) return; setBusy(false); polling.current = 0;
    if (r.ok) { setRun(r.data as Run); setError(""); } else { setError(errorText(r)); void refreshRun(); }
  }
  return <>
    <div className="panel-card">
      {!caps && <p role="status">Checking import availability…</p>}
      {caps && !caps.ready && <p role="status">{importAvailabilityMessage(caps.reasons)}</p>}
      {caps?.ready && <p>You can import up to 32 MiB and 2,000 files. Choose your source project to continue.</p>}
      {pending ? <p>A previous intake request is retained for this account and product. <button disabled={busy} onClick={() => void submit(pending)}>Recover this request</button></p> : <>
        <label>Source <select value={mode} onChange={e => setMode(e.target.value as "github" | "zip")}><option value="github">Public GitHub repository</option><option value="zip">Local project ZIP</option></select></label>
        {mode === "github" ? <><label>Repository URL <input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://github.com/owner/repository" /></label><label>Branch, tag or commit (optional) <input value={ref} onChange={e => setRef(e.target.value)} /></label></> : <label>Project ZIP <input type="file" accept=".zip,application/zip" onChange={e => setFile(e.target.files?.[0] || null)} /></label>}
        <label>Directory to analyze (optional) <input value={root} onChange={e => setRoot(e.target.value)} placeholder="Leave empty for the repository root" /></label>
        <p className="muted">Remove credentials, .env files and local dependency folders before creating a ZIP. Private repositories and external submodules are not fetched.</p>
        <button disabled={busy || !caps?.ready || (mode === "github" ? !url.trim() : !file)} onClick={() => void submit()}>Create source intake</button>
      </>}
    </div>
    {error && <ErrorNotice error={human(error)} source="import.error" />}
    {history.length > 0 && <label>Saved intakes <select value={run?.import_id || ""} onChange={e => { setRun(history.find(row => row.import_id === e.target.value) || null); polling.current = 0; }}>{history.map(row => <option key={row.import_id} value={row.import_id}>{row.source.source === "github_public" ? row.source.repo_url : row.source.filename} Not recorded {importState(row.state)}</option>)}</select></label>}
    {run && <div className="panel-card"><h3>{importState(run.state)}</h3><p>Received {run.bytes.acquired.toLocaleString()} bytes · stored {run.bytes.retained.toLocaleString()} bytes</p>
      {run.error_code && <ErrorNotice error={human(run.error_code)} source="import.run" />}
      <button disabled={busy} onClick={() => { polling.current = 0; void refreshRun(); }}>Refresh intake status</button>
      {run.upload_path && <div><label>Choose the same ZIP <input type="file" accept=".zip,application/zip" onChange={e => setFile(e.target.files?.[0] || null)} /></label><button disabled={busy || !file || !caps?.ready} onClick={() => void upload()}>Upload verified ZIP</button></div>}
      {(activeImport(run.state) || run.state === "awaiting_upload" || run.state === "needs_reconciliation") && <button disabled={busy} onClick={() => void action("cancel")}>Cancel intake</button>}
      {run.state === "needs_reconciliation" && <button disabled={busy || !caps?.ready} onClick={() => void action("retry")}>Request bounded recovery</button>}
      {run.report && <ReportView report={run.report} />}
      <ImportCleanup key={run.import_id} run={run} base={base} workspace={owner.workspace} onRemoved={setRun} />
      <p className="muted">Import produces a source inventory. Founder review, managed build, tests and deployment are separate steps; this source has not been executed or deployed.</p>
    </div>}
  </>;
}

function ReportView({ report }: { report: Report }) {
  return <div><h3>What is in this project</h3><p>{report.file_count} verified files · {report.size_bytes.toLocaleString()} source bytes · stack candidates: {report.stack_candidates.join(", ")}</p>
    <p>{report.test_files.length} test files identified; no tests executed. Licensing and reuse rights require your review.</p>
    {!!report.coverage_gaps.length && <ul>{report.coverage_gaps.map(gap => <li key={gap}>{human(gap)}</li>)}</ul>}
    <details><summary>File evidence and declared dependencies</summary>{report.observations.map((row, i) => <div key={`${row.path}:${i}`}><strong>{row.path}</strong><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(row.facts, null, 2)}</pre></div>)}</details>
    <p className="muted">Source manifest: <code style={{ overflowWrap: "anywhere" }}>{report.manifest_digest}</code></p>
  </div>;
}


type CleanupItem = { cleanup_id: string; object_kind: string; state: string; state_version: number; due_at: string; completed_at?: string; released_bytes: number; error_code?: string; content_free_fence_retained: boolean };
function ImportCleanup({ run, base, workspace, onRemoved }: { run: Run; base: string; workspace: string; onRemoved: (run: Run) => void }) {
  const [items, setItems] = useState<CleanupItem[] | null>(null); const [error, setError] = useState("");
  const [busy, setBusy] = useState(false); const [confirm, setConfirm] = useState(false);
  const guard = useRef(contentRequestFence());
  const path = `${base}/${run.import_id}`; const headers = { "X-Workspace-ID": workspace };
  useEffect(() => {
    guard.current.activate(path); const current = guard.current.capture();
    const changed = () => guard.current.invalidate(); window.addEventListener("supymem-session-changed", changed);
    void api(path + "/cleanup", { headers }).then(r => {
      if (!current()) return;
      if (r.ok) setItems((r.data as { items: CleanupItem[] }).items); else setError(errorText(r));
    });
    return () => { guard.current.invalidate(); window.removeEventListener("supymem-session-changed", changed); };
  // The parent is keyed by authenticated account and this component by import.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, workspace]);
  async function refresh() {
    const current = guard.current.capture(); setBusy(true);
    const r = await api(path + "/cleanup", { headers });
    if (!current()) return; setBusy(false);
    if (r.ok) { setItems((r.data as { items: CleanupItem[] }).items); setError(""); } else setError(errorText(r));
  }
  async function remove() {
    const current = guard.current.capture(); setBusy(true); setError("");
    const r = await api(path + "/delete", { method: "POST", headers, body: JSON.stringify({ state_version: run.state_version }) });
    if (!current()) return; setBusy(false);
    if (r.ok) { onRemoved(r.data as Run); setConfirm(false); void refresh(); }
    else setError(errorText(r));
  }
  async function recover(item: CleanupItem) {
    const current = guard.current.capture(); setBusy(true);
    const r = await api(`${path}/cleanup/${item.cleanup_id}/retry`, { method: "POST", headers, body: JSON.stringify({ state_version: item.state_version }) });
    if (!current()) return; setBusy(false);
    if (r.ok) { setItems((r.data as { items: CleanupItem[] }).items); setError(""); } else setError(errorText(r));
  }
  return <details><summary>Source storage and removal</summary>
    <p>Raw ZIPs are scheduled for cleanup after six hours. An accepted source remains until you remove it or its product. Byte quota is released after storage erasure is verified.</p>
    {run.deleted && <p role="status">Source access has been retired. Storage erasure status is shown below.</p>}
    {items === null && !error && <p>Reading cleanup status…</p>}
    {items?.map(item => <p key={item.cleanup_id}><strong>{item.object_kind === "raw" ? "Raw ZIP" : "Accepted source"}</strong>: {human(item.state)}.
      {item.state === "complete" ? ` Released ${item.released_bytes.toLocaleString()} reserved/stored bytes.` : ` Scheduled ${new Date(item.due_at).toLocaleString()}.`}
      {item.content_free_fence_retained && " Source-data versions are absent; a content-free write fence remains."}
      {item.error_code && <ErrorNotice as="span" error={human(item.error_code)} source="import.item"> {human(item.error_code)}.</ErrorNotice>}
      {item.state === "needs_attention" && <button disabled={busy} onClick={() => void recover(item)}>Request bounded cleanup recovery</button>}
    </p>)}
    {error && <ErrorNotice error={human(error)} source="import.error" />}
    <button disabled={busy} onClick={() => void refresh()}>Refresh cleanup status</button>
    {!run.deleted && (confirm ? <div><p>Remove this imported source and its report from use? Builds cannot select it after removal. Historical receipt metadata is retained.</p><button disabled={busy} onClick={() => void remove()}>Remove source</button><button disabled={busy} onClick={() => setConfirm(false)}>Keep source</button></div> : <button disabled={busy} onClick={() => setConfirm(true)}>Remove imported source…</button>)}
  </details>;
}
