"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { SiteAddress } from "./SiteAddress";
import styles from "./SiteAddress.module.css";

type Blocker = { code: string; message: string };
type Draft = { site_version_id: string; version: number; sha256: string; headline?: string; subhead?: string };
type Publication = {
  state: "live" | "publishable" | "blocked";
  live_url?: string | null;
  draft?: Draft | null;
  blockers: Blocker[];
  deployment?: { state: string; error?: string | null } | null;
  preview_html?: string;
};
type Owner = { ventureId: string; actor: string | null; epoch: number };
type View = { owner: Owner; publication: Publication | null; preview: boolean; confirm: boolean; busy: boolean; note: string; error: string };
const BUSY = new Set(["pending", "packaging", "pushing", "creating_infra", "health_checking"]);
const UNKNOWN = "Publishing status could not be confirmed. Refresh the saved status before trying again; the request may have reached hosting.";
const emptyView = (owner: Owner): View => ({ owner, publication: null, preview: false, confirm: false, busy: false, note: "", error: "" });

function onVentureRoute(ventureId: string): boolean {
  try {
    const match = window.location.pathname.match(/^\/v\/([^/]+)(?:\/|$)/);
    return Boolean(match && decodeURIComponent(match[1]) === ventureId);
  } catch { return false; }
}

/** Preview and confirm one current concept page, then wait for the verified public readback. */
export function PublishConceptPage({ ventureId, onDraft }: { ventureId: string; onDraft?: (hasDraft: boolean) => void }) {
  const [, rerender] = useState(0);
  const [view, setView] = useState<View | null>(null);
  const ownerRef = useRef<Owner | null>(null);
  const active = useRef<Owner | null>(null);
  const readSerial = useRef(0);
  const source = useRef<{ owner: Owner; publication: Publication } | null>(null);
  const flight = useRef<Owner | null>(null);
  const [address, setAddress] = useState<{ slug: string | null; current: string | null }>({ slug: null, current: null });
  const [changing, setChanging] = useState(false);
  const onAddress = useCallback((slug: string | null, current: string | null) => setAddress({ slug, current }), []);
  const actor = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  if (!ownerRef.current || ownerRef.current.ventureId !== ventureId || ownerRef.current.actor !== actor || ownerRef.current.epoch !== epoch) {
    ownerRef.current = { ventureId, actor, epoch };
    source.current = null;
    flight.current = null;
    readSerial.current++;
  }
  const owner = ownerRef.current;
  const base = `/api/ventures/${encodeURIComponent(ventureId)}/site/publication`;
  const current = useCallback((expected: Owner) => active.current === expected && ownerRef.current === expected && Boolean(expected.actor)
    && verifiedSessionSubject() === expected.actor && verifiedSessionEpoch() === expected.epoch && onVentureRoute(expected.ventureId), []);
  const update = useCallback((expected: Owner, patch: Partial<View>) => {
    if (current(expected)) setView(previous => current(expected)
      ? { ...(previous?.owner === expected ? previous : emptyView(expected)), ...patch } : previous);
  }, [current]);

  const refresh = useCallback(async (withPreview = false) => {
    if (!current(owner)) return;
    const serial = ++readSerial.current;
    try {
      const result = await api(withPreview ? `${base}?preview=true` : base);
      if (!current(owner) || serial !== readSerial.current) return;
      if (!result.ok) throw new Error("status_unavailable");
      const publication = result.data as Publication;
      source.current = { owner, publication };
      update(owner, { publication, error: "", confirm: false });
    } catch {
      if (!current(owner) || serial !== readSerial.current) return;
      source.current = null;
      update(owner, { publication: null, confirm: false, error: "The landing page's publishing status could not be loaded. Refresh its saved status before publishing." });
    }
  }, [base, current, owner, update]);

  useEffect(() => {
    active.current = owner;
    setView(emptyView(owner));
    void refresh();
    return () => { if (active.current === owner) active.current = null; readSerial.current++; };
  }, [owner, refresh]);
  useEffect(() => {
    const changed = () => rerender(n => n + 1);
    window.addEventListener("supymem-session-changed", changed);
    return () => window.removeEventListener("supymem-session-changed", changed);
  }, []);

  // A waitlist test/enable here or in chat, or coming back to this tab, changes readiness: re-read it from the server.
  useEffect(() => {
    const reread = (event?: Event) => {
      const id = (event as CustomEvent<{ ventureId?: string }> | undefined)?.detail?.ventureId;
      if (typeof document !== "undefined" && document.hidden || (id && id !== ventureId)) return;
      void refresh();
    };
    const doc = typeof document === "undefined" ? null : document;
    window.addEventListener("supymem-waitlist-changed", reread);
    window.addEventListener("supymem-chat-action-done", reread);
    doc?.addEventListener("visibilitychange", reread);
    return () => {
      window.removeEventListener("supymem-waitlist-changed", reread);
      window.removeEventListener("supymem-chat-action-done", reread);
      doc?.removeEventListener("visibilitychange", reread);
    };
  }, [ventureId, refresh]);

  const visible = view?.owner === owner && current(owner) ? view : null;
  const state = visible?.publication;
  const inFlight = Boolean(state?.deployment && BUSY.has(state.deployment.state));
  const hasDraft = Boolean(state?.draft || state?.live_url);
  useEffect(() => { onDraft?.(hasDraft); }, [hasDraft, onDraft]);
  useEffect(() => {
    if (!inFlight) return;
    const timer = window.setInterval(() => { void refresh(); }, 4000);
    return () => window.clearInterval(timer);
  }, [inFlight, refresh]);

  function currentPage() {
    return current(owner) && source.current?.owner === owner && source.current.publication === state;
  }
  /** Save the chosen address; returns a plain-words problem, or "" when it is saved. */
  async function saveAddress(slug: string): Promise<string> {
    const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/site-address`, { method: "PUT", body: JSON.stringify({ slug }) });
    if (result.ok) return "";
    const detail = (result.data as { detail?: { message?: string; suggestion?: string } } | undefined)?.detail;
    const hint = detail?.suggestion ? ` Try ${detail.suggestion}.` : "";
    return typeof detail?.message === "string" ? detail.message + hint : "The landing page address could not be saved. Try again.";
  }
  async function changeAddress() {
    const draft = state?.draft;
    if (!currentPage() || !draft || !address.slug || flight.current === owner) return;
    flight.current = owner;
    update(owner, { busy: true, error: "", note: "" });
    readSerial.current++;
    try {
      const problem = await saveAddress(address.slug);
      if (!current(owner)) return;
      if (problem) { update(owner, { error: problem }); return; }
      const result = await api(base, { method: "POST", body: JSON.stringify({ site_version_id: draft.site_version_id, public_sha256: draft.sha256 }) });
      if (!current(owner)) return;
      const detail = (result.data as { detail?: { message?: string } } | undefined)?.detail;
      if (!result.ok) { update(owner, { error: typeof detail?.message === "string" ? detail.message : UNKNOWN }); return; }
      setChanging(false);
      update(owner, { note: "Moving your landing page to the new address. Your old link keeps working." });
      await refresh();
    } catch {
      if (current(owner)) update(owner, { error: UNKNOWN });
    } finally {
      if (current(owner) && flight.current === owner) { flight.current = null; update(owner, { busy: false }); }
    }
  }
  async function publish() {
    const draft = state?.draft;
    if (!currentPage() || !draft || state?.state !== "publishable" || !visible?.confirm || flight.current === owner) return;
    flight.current = owner;
    update(owner, { busy: true, error: "", note: "" });
    // A pre-publication read must not overwrite the saved response or revive its old confirmation.
    readSerial.current++;
    try {
      const problem = address.slug ? await saveAddress(address.slug) : "";
      if (!current(owner)) return;
      if (problem) { update(owner, { confirm: false, error: problem }); return; }
      const result = await api(base, { method: "POST", body: JSON.stringify({ site_version_id: draft.site_version_id, public_sha256: draft.sha256 }) });
      if (!current(owner)) return;
      if (!result.ok) {
        const detail = (result.data as { detail?: { message?: string } } | undefined)?.detail;
        source.current = null;
        update(owner, { publication: null, confirm: false, error: typeof detail?.message === "string" ? detail.message : UNKNOWN });
        return;
      }
      update(owner, { confirm: false, note: "Publishing is queued. The landing page is not live until the public address serves exactly this landing page." });
      await refresh();
    } catch {
      if (current(owner)) {
        source.current = null;
        update(owner, { publication: null, confirm: false, error: UNKNOWN });
      }
    } finally {
      if (current(owner) && flight.current === owner) { flight.current = null; update(owner, { busy: false }); }
    }
  }

  if (!visible || (!state && !visible.error)) return null;
  if (state?.state === "blocked" && !state.draft && !state.live_url && !visible.error
      && !state.blockers.some(b => b.code === "hosting_public_state_uncertain")) return null;
  return <section aria-label="Publish concept landing page">
    <h2>Publish your concept landing page</h2>
    {state?.live_url && state.state === "live" && <p role="status">Live at <a href={state.live_url} target="_blank" rel="noopener noreferrer">{state.live_url}</a></p>}
    {state?.live_url && state.state === "live" && state.draft && <div>
      {!changing && <button type="button" data-usage-control="website_address_change" disabled={visible.busy} onClick={() => setChanging(true)}>Change address</button>}
      {changing && <div role="group" aria-label="Change landing page address">
        <SiteAddress ventureId={ventureId} onReady={onAddress} disabled={visible.busy} />
        <p className={styles.note}>Your old link keeps working.</p>
        <button type="button" data-usage-control="website_address_save" className="primary"
          disabled={visible.busy || !address.slug || address.slug === address.current} onClick={() => void changeAddress()}>Use this address</button>{" "}
        <button type="button" disabled={visible.busy} onClick={() => setChanging(false)}>Cancel</button>
      </div>}
    </div>}
    {state?.draft && state.state !== "live" && <>
      <p><strong>{state.draft.headline}</strong>{state.draft.subhead ? ` Not recorded ${state.draft.subhead}` : ""}</p>
      <ErrorNotice error={state.deployment?.error ? state.deployment.error === "hosting_public_state_uncertain"
        ? "The last attempt's public state could not be confirmed. Refresh the saved status before publishing again."
        : "The last attempt did not finish. Review the saved publishing status before trying again." : ""} source="PublishConceptPage.deployment" />
      {inFlight && <p role="status">Publishing is in progress. It is live only after the public address serves this exact landing page.</p>}
      <button type="button" onClick={() => { if (!currentPage()) return; const next = !visible.preview; update(owner, { preview: next }); if (next) void refresh(true); }}>
        {visible.preview ? "Hide the public landing page preview" : "Preview the public landing page"}
      </button>
      {visible.preview && state.preview_html && <iframe title="Public landing page preview" sandbox="" srcDoc={state.preview_html} style={{ width: "100%", height: 480, border: "1px solid #ccd8eb" }} />}
      {state.state === "publishable" && <SiteAddress ventureId={ventureId} onReady={onAddress} disabled={visible.busy || visible.confirm} />}
      {state.state === "publishable" && !visible.confirm && <button type="button" className="primary" disabled={visible.busy || !address.slug} onClick={() => { if (currentPage()) update(owner, { confirm: true }); }}>Publish landing page</button>}
      {visible.confirm && <div role="group" aria-label="Confirm publishing">
        <p>This makes the landing page public on a supymem.com address. It is a concept landing page: it says what you are building and has one button that opens your waitlist form. The exact landing page above is uploaded and read back before I say it is live. You can take it offline again.</p>
        <button type="button" className="primary" disabled={visible.busy || !address.slug} onClick={() => void publish()}>Yes, publish this landing page</button>{" "}
        <button type="button" disabled={visible.busy} onClick={() => { if (currentPage()) update(owner, { confirm: false }); }}>Cancel</button>
      </div>}
    </>}
    {state?.blockers.filter(b => b.code !== "hosting_publication_in_progress").map(b => <ErrorNotice key={b.code} error={b.message} source="PublishConceptPage.blocker" />)}
    {visible.note && <p role="status">{visible.note}</p>}
    <ErrorNotice error={visible.error} source="PublishConceptPage.request" />
    {visible.error && <button type="button" disabled={visible.busy} onClick={() => void refresh()}>Refresh publishing status</button>}
  </section>;
}
