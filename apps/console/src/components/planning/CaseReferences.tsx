"use client";

import { useEffect, useRef, useState } from "react";
import { api, ensureSession } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { InlineSpinner } from "@/components/ui/InlineSpinner";
import styles from "./CaseReferences.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type CaseReference = {
  id: string; sheet: string; row: number; original_id?: string; kind: string; title: string;
  company?: string; documented_action?: string; problem?: string; adaptation?: string;
  experiment?: string; metric?: string; stop_condition?: string; boundary?: string; limitations?: string;
  source_confidence?: string; source_urls: string[]; matched_terms?: string[];
  relevance_reason?: string; important_difference?: string; suggested_next_action?: string;
  why_it_worked?: string;
  fields: { label: string; value: unknown; citation: string }[];
};
export type PrecedentApplication = { record_id: string; proposed_application: string; important_difference: string;
  local_test: string; stop_condition: string; context_to_confirm: string };

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object" && "type" in value && "iso" in value
      && value.type === "datetime" && typeof value.iso === "string") {
    // Workbook dates have no timezone; retain the recorded calendar date.
    const calendarDate = /^(\d{4}-\d{2}-\d{2})(?:T|$)/.exec(value.iso);
    if (calendarDate) return calendarDate[1];
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
export function safeUrl(value: string): string | undefined {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}

export function ReferenceCard({ item, application }: { item: CaseReference; application?: PrecedentApplication }) {
  return <article className={styles.card}>
    <h3>{item.title}</h3>
    <p className={styles.meta}>Workbook case · {item.kind.replaceAll("_", " ")}</p>
    {item.relevance_reason && <p><strong>Why this may fit:</strong> {item.relevance_reason}</p>}
    {!application && item.important_difference && <p><strong>Important difference:</strong> {item.important_difference}</p>}
    {item.problem && <p>{item.problem}</p>}
    {item.documented_action && <p><strong>Workbook account:</strong> {item.documented_action}</p>}
    {item.why_it_worked && <p><strong>Why it worked:</strong> {item.why_it_worked}</p>}
    {application ? <>
      <p><strong>Proposed use here:</strong> {application.proposed_application}</p>
      <p><strong>Important difference:</strong> {application.important_difference}</p>
      <p><strong>Our test:</strong> {application.local_test}</p>
      <p><strong>Stop or reconsider:</strong> {application.stop_condition}</p>
      <p><strong>Confirm during plan review:</strong> {application.context_to_confirm}</p>
    </> : <>
      {item.experiment && <p><strong>Suggested experiment in the workbook:</strong> {item.experiment}</p>}
      {item.metric && <p><strong>Measure:</strong> {item.metric}</p>}
      {item.stop_condition && <p><strong>Stop condition:</strong> {item.stop_condition}</p>}
      {item.suggested_next_action && <p><strong>Do:</strong> {item.suggested_next_action}</p>}
    </>}
    {item.limitations && <p><strong>Avoid:</strong> {item.limitations}</p>}
    <p className={styles.meta}>Source confidence in workbook: {item.source_confidence || "not specified"}. Historical claims have not been independently verified by Supymem.</p>
    {item.source_urls.map((url, index) => { const href = safeUrl(url); return href ? <a key={url} href={href} target="_blank" rel="noopener noreferrer">Read source {index + 1}</a> : null; })}
    <details><summary>Full reference and cell citations</summary>
      <dl>{item.fields.filter(field => !/^(case|playbook|problem|edge-case) id$/i.test(field.label)).map((field, index) => <div key={`${field.citation}-${index}`}><dt>{field.label} <span className={styles.meta}>({field.citation})</span></dt><dd>{valueText(field.value)}</dd></div>)}</dl>
    </details>
  </article>;
}

export function CaseReferences({ ventureId }: { ventureId: string }) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("historical_claim");
  const [situation, setSituation] = useState("");
  const [items, setItems] = useState<CaseReference[]>([]);
  const [error, setError] = useState("");
  const [assessment, setAssessment] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadedFor, setLoadedFor] = useState("");
  const generation = useRef(0);
  const pending = useRef(false);

  async function load(text: string, selectedKind: string, review = false) {
    if (pending.current) return;
    pending.current = true;
    const token = ++generation.current;
    setBusy(true); setError(""); setAssessment(""); setItems([]); setLoadedFor("");
    try {
      await ensureSession();
      if (token !== generation.current) return;
      const params = new URLSearchParams({ limit: "3" });
      if (text.trim()) params.set("q", text.trim());
      if (selectedKind) params.set("kind", selectedKind);
      const result = review
        ? await api(`/api/ventures/${encodeURIComponent(ventureId)}/knowledge/review`, {
          method: "POST", body: JSON.stringify({query: text.trim() || null}),
        })
        : await api(`/api/ventures/${encodeURIComponent(ventureId)}/knowledge?${params}`);
      if (token !== generation.current) return;
      const data = result.data as { venture_id?: string; items?: CaseReference[]; advisory?: {assessment?: string; reason?: string} };
      if (!result.ok || data?.venture_id !== ventureId || !Array.isArray(data.items)) {
        setError(apiFailure(result, "loading case references")); return;
      }
      setItems(data.items); setLoadedFor(ventureId);
      if (review) setAssessment(data.advisory?.reason === "input_bounds"
        ? "Describe one specific decision or problem above to assess relevance. The current context exceeds the review limits; keyword matches remain available."
        : {
        selected: "The closest suggested analogy is shown first. Review its source, differences and limitations before using it.",
        no_close_match: "The relevance review found no close analogy. These keyword matches remain available for your own review.",
        uncertain: "The relevance review was uncertain. The original keyword order is retained.",
        unavailable: "The additional relevance review is unavailable. Your keyword matches remain available.",
      }[data.advisory?.assessment || "unavailable"] || "Review complete.");
    } catch { if (token === generation.current) setError("Case references could not be loaded. Try again."); }
    finally { if (token === generation.current) { pending.current = false; setBusy(false); } }
  }
  useEffect(() => {
    pending.current = false;
    setQuery(""); setKind("historical_claim"); setSituation(""); void load("", "historical_claim");
    return () => { generation.current++; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId]);

  return <section className={styles.section} aria-label="Case study references">
    <h2>Learn from comparable decisions</h2>
    <p>These workbook references can help shape an experiment. A similar company's experience does not establish demand for your venture. Check the source and the differences before adopting its approach.</p>
    <div className={styles.search}>
      <label>What decision are you facing?
        <select value={situation} disabled={busy} onChange={event => {
          const selected = event.target.value;
          setSituation(selected); setQuery(selected);
          setKind(selected.startsWith("business ") ? "playbook_guidance" : "historical_claim");
        }}>
          <option value="">Use my venture idea</option>
          <option value="idea stage problem validation customer discovery competitor analysis willingness to pay">Test an early idea</option>
          <option value="business sales lead generation qualified prospects">Sales: finding leads</option>
          <option value="business sales positioning messaging target customer">Sales: positioning or customer fit</option>
          <option value="business sales pricing willingness to pay">Sales: pricing</option>
          <option value="business sales process conversion closing">Sales: closing or process</option>
          <option value="business product market fit retention">Sales: product-market fit</option>
        </select>
      </label>
      {situation && <p>These are diagnostic starting points, not a verdict. Describe the concrete symptom below, then compare a playbook with company examples.</p>}
    </div>
    <form className={styles.search} onSubmit={event => { event.preventDefault(); void load(query, kind); }}>
      <label>Decision or problem<input value={query} maxLength={2000} disabled={busy} placeholder="Uses your venture idea when blank" onChange={event => setQuery(event.target.value)} /></label>
      <label>Reference type<select value={kind} disabled={busy} onChange={event => setKind(event.target.value)}>
        <option value="historical_claim">Company cases</option><option value="playbook_guidance">Playbooks and guidance</option>
        <option value="diagnostic_archetype">Situations and diagnostics</option><option value="adaptation_theory">Theories to test</option>
        <option value="">All workbook content</option>
      </select></label>
      <button type="submit" disabled={busy}>{busy && <InlineSpinner />}{busy ? "Finding references…" : kind === "playbook_guidance" ? "Find playbook" : "Find references"}</button>
    </form>
    {query.trim() && <div className={styles.search}>
      <button type="button" disabled={busy} onClick={() => { setKind("playbook_guidance"); void load(query, "playbook_guidance"); }}>Find a relevant playbook</button>
      <button type="button" disabled={busy} onClick={() => { setKind("historical_claim"); void load(query, "historical_claim"); }}>Compare company examples</button>
    </div>}
    {kind === "historical_claim" && <button className={styles.reviewButton} type="button" disabled={busy}
      onClick={() => void load(query, kind, true)}>Assess case relevance</button>}
    {assessment && <p role="status">{assessment}</p>}
    {busy && <p role="status"><InlineSpinner />Finding relevant examples…</p>}
    <ErrorNotice error={error} source="CaseReferences.error" />
    {loadedFor === ventureId && items.length === 0 && <p>No matching reference found. Try describing the problem differently; Supymem will not invent a precedent.</p>}
    {loadedFor === ventureId && items.map(item => <ReferenceCard key={item.id} item={item} />)}
  </section>;
}
