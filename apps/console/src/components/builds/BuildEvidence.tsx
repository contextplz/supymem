"use client";

import { useEffect, useRef, useState } from "react";
import { api, authenticatedFetch, downloadFile, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { apiFailure, resourceReadFailure } from "@/lib/apiFailure";
import { acceptedReviewDescription, attentionChecks, buildReceiptRoute, checkedReport, currentReviewReady, downloadBuildReceipt, reviewFailureDescription, reviewMatches, reviewReadinessDescription, type ReviewBuild, type QaReceipt, type QaReport } from "@/lib/buildReview";
import { announceDiagnostic } from "@/lib/uiDiagnostics";
import { PrivateCorrection } from "./PrivateCorrection";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export function BuildEvidence({ build, onReviewed }: { build: ReviewBuild; onReviewed: () => void }) {
  const alive = useRef(false), mutation = useRef<object | null>(null);
  const receiptOwner = {actor: verifiedSessionSubject(), epoch: verifiedSessionEpoch(), build: build.id, app: build.app_id,
    route: typeof window === "undefined" ? null : buildReceiptRoute(window.location, build)};
  const receiptOwnerRef = useRef(receiptOwner), receiptFlight = useRef<typeof receiptOwner | null>(null);
  if (JSON.stringify(receiptOwnerRef.current) !== JSON.stringify(receiptOwner)) receiptOwnerRef.current = receiptOwner;
  const renderedReceiptOwner = receiptOwnerRef.current;
  const reviewKey = JSON.stringify([build.id, build.app_id, build.artifact_id, build.qa_run_id,
    build.review, build.review_readiness, build.state, build.completion_status, build.runtime_version,
    build.lifecycle_version, build.mode, build.verified_execution, build.verified_source_import,
    build.source_custody_verified, build.qa_state, build.publication_required, build.landing_page?.qa_report_available]);
  const reviewBuildRef = useRef(reviewKey);
  reviewBuildRef.current = reviewKey;
  const receiptCurrent = () => alive.current && !!renderedReceiptOwner.actor && !!renderedReceiptOwner.route &&
    receiptOwnerRef.current === renderedReceiptOwner && renderedReceiptOwner.actor === verifiedSessionSubject() &&
    renderedReceiptOwner.epoch === verifiedSessionEpoch() && typeof window !== "undefined" &&
    buildReceiptRoute(window.location, build) === renderedReceiptOwner.route;
  const reviewCurrent = () => receiptCurrent() && reviewBuildRef.current === reviewKey;
  const [qa, setQa] = useState<QaReceipt | null>(null);
  const [report, setReport] = useState<QaReport | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [receiptBusy, setReceiptBusy] = useState(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { setQa(null); setReport(null); setError(""); }, [build.qa_run_id, build.review?.report_digest, renderedReceiptOwner]);
  useEffect(() => { mutation.current = null; setBusy(false); setError(""); }, [reviewKey, renderedReceiptOwner]);
  const base = build.qa_run_id ? `/api/builds/${encodeURIComponent(build.id)}/qa/${encodeURIComponent(build.qa_run_id)}` : "";
  const proofMatches = reviewMatches(build, qa, report);
  const ready = proofMatches && currentReviewReady(build);
  // A pending review needs the finalized report; load it as soon as the section opens.
  useEffect(() => {
    if (base && (build.review?.status === "pending" || build.landing_page?.qa_report_available) && build.qa_run_id) void inspect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, build.review?.status, build.review?.report_digest, build.landing_page?.qa_report_available, reviewKey, renderedReceiptOwner]);

  async function inspect() {
    if (!base || !reviewCurrent() || mutation.current) return;
    const flight = {}; mutation.current = flight; setBusy(true); setError(""); setReport(null);
    try {
      const result = await api(base);
      if (!result.ok) throw new Error(apiFailure(result, "loading the QA receipt"));
      const receipt = result.data as QaReceipt;
      if (receipt.id !== build.qa_run_id || receipt.source_artifact_id !== build.artifact_id) {
        announceDiagnostic("integrity_error", "integrity", "qa_receipt_mismatch");
        throw new Error("The QA receipt does not match this build.");
      }
      if (!reviewCurrent()) return;
      setQa(receipt);
      if (!receipt.verified_report) throw new Error("The independent QA report is not ready yet. The saved run will show its progress.");
      const response = await authenticatedFetch(`${base}/report`);
      if (!response.ok) throw new Error(await resourceReadFailure(response, "QA report"));
      let document: QaReport;
      try { document = await checkedReport(await response.arrayBuffer(), build, receipt); }
      catch (err) { announceDiagnostic("integrity_error", "integrity", err); throw err; }
      if (reviewCurrent()) setReport(document);
    } catch (err) { if (reviewCurrent()) setError(err instanceof Error ? err.message : "QA evidence could not be loaded."); }
    finally { if (mutation.current === flight) { mutation.current = null; if (reviewCurrent()) setBusy(false); } }
  }
  async function decide(decision: "accept" | "reject") {
    if (!ready || !reviewCurrent() || !build.review || mutation.current) return;
    const flight = {}; mutation.current = flight; setBusy(true); setError("");
    try {
      const result = await api(`/api/builds/${encodeURIComponent(build.id)}/review`, { method: "POST", body: JSON.stringify({
        source_artifact_id: build.review.source_artifact_id, report_digest: build.review.report_digest,
        decision, feedback: feedback.trim() || undefined,
      }) });
      if (!reviewCurrent()) return;
      if (!result.ok) throw new Error(reviewFailureDescription((result.data as {detail?: unknown} | null)?.detail) || apiFailure(result, "recording the milestone review"));
      onReviewed();
    } catch (err) {
      if (reviewCurrent()) { setError(err instanceof Error ? err.message : "Review outcome is unconfirmed. Refresh saved state before trying again."); onReviewed(); }
    } finally { if (mutation.current === flight) { mutation.current = null; if (reviewCurrent()) setBusy(false); } }
  }
  async function downloadReceipt() {
    if (!receiptCurrent() || receiptFlight.current === renderedReceiptOwner) return;
    receiptFlight.current = renderedReceiptOwner; setReceiptBusy(true); setError("");
    try {
      const ok = await downloadBuildReceipt(build, () => authenticatedFetch(`/api/builds/${encodeURIComponent(build.id)}`), receiptCurrent);
      if (!ok && receiptCurrent()) setError("The build receipt download did not complete. Refresh this saved build before trying again.");
    } finally {
      if (receiptFlight.current === renderedReceiptOwner) receiptFlight.current = null;
      if (receiptCurrent()) setReceiptBusy(false);
    }
  }
  if (build.runtime_version !== 1) return null;
  return <section aria-label="Build evidence and milestone review">
    <h3>Product evidence</h3>
    <p>{build.artifact_id ? build.landing_page?.qa_report_available === false
      ? "The captured source is saved. Final checks have not produced a verified report." : "The captured source is saved. Review the independent QA results and screenshots below." : "No verified source artifact is available yet."}</p>
    <button type="button" disabled={!renderedReceiptOwner.actor || !renderedReceiptOwner.route || receiptBusy && receiptFlight.current === renderedReceiptOwner}
      onClick={() => void downloadReceipt()}>Download build receipt</button>
    <p>The receipt includes saved plan, source, review and publication status. It may include your private instructions and feedback; keep it private.</p>
    {base && build.landing_page?.qa_report_available !== false ? <button type="button" disabled={busy} onClick={() => void inspect()}>{error && !report ? "Retry QA results" : report ? "Reload QA results" : "Review QA results"}</button> : <p>No verified final-check report is available.</p>}
    {busy && <p role="status">Loading or saving the reviewed result…</p>}
    <ErrorNotice error={error} source="BuildEvidence.error" />
    {qa?.verified_report && <p>Independent checks: {qa.passed ?? "Unknown"} passed · {qa.failed ?? "Unknown"} failed · {qa.unsupported ?? "Unknown"} unsupported.
      {!qa.verified_execution && !qa.verified_source_import && " The complete build and usage reconciliation are not verified."}
      {qa.verified_source_import && " Saved source and QA usage are verified; this was not a managed-builder execution."}</p>}
    {report && build.landing_page?.qa_report_available !== false && <>
      {report.cases.some(item => item.status !== "passed") && <><h4>Checks that need attention</h4><ul>{attentionChecks(report.cases).map((item, index) => <li key={index}>
        <strong>{item.label}</strong>: {item.message}{item.count > 1 && ` (${item.count} checks)`}
      </li>)}</ul></>}
      {report.exclusions.length > 0 && <><h4>Limits of this test</h4><ul>{report.exclusions.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
      {report.evidence.map(item => <Screenshot key={item.id} base={base} item={item} />)}
      <details><summary>Technical details · All {report.cases.length} recorded checks</summary><ul>{report.cases.map(item => <li key={item.id}>
        <strong>{item.status}: {item.id} · {item.target || item.suite}</strong>, {item.suite === "runtime" ? item.status === "passed" ? "The check runner's runtime check passed." : "The check runner stopped before it could finish." : item.observed}<br />Expected: {item.expected}
      </li>)}</ul></details>
      <button type="button" disabled={busy} onClick={() => void downloadFile(`${base}/report`, `qa-${build.id}.json`).then(ok => {
        if (!ok && alive.current) setError("The report download did not complete.");
      })}>Download QA report</button>
    </>}
    {build.review?.status === "pending" && <>
      <h3>{build.publication_required ? "Review this source" : "Review this milestone"}</h3>
      <p>{build.publication_required ? "Compare the source and independent QA with the agreed requirements. Accepting the source enables a separate publication review; this plan task stays incomplete until publication and public waitlist submission are verified." : "Compare the result with the agreed product requirements. Acceptance completes this plan task; publishing is a separate action."}</p>
      <label>Feedback<textarea maxLength={2000} value={feedback} disabled={busy} onChange={event => setFeedback(event.target.value)} /></label>
      <div><button type="button" disabled={busy || !ready} onClick={() => void decide("accept")}>{build.publication_required ? "Accept source" : "Accept milestone"}</button>{" "}
        <button type="button" disabled={busy || !ready} onClick={() => void decide("reject")}>Request changes</button></div>
      <p role="status">{reviewReadinessDescription(build)}</p>
      <button type="button" disabled={busy} onClick={() => { if (reviewCurrent()) onReviewed(); }}>Refresh review status</button>
      {!proofMatches && <p>Load the matching, finalized QA report before recording your review.</p>}
    </>}
    {build.review?.status === "accepted" && <p role="status">{acceptedReviewDescription(build.review, build)}</p>}
    {build.review?.status === "accepted" && build.artifact_profile === "static_landing_v1" && <PrivateCorrection build={build} onQueued={onReviewed} />}
    {build.review?.status === "rejected" && <section aria-label="Requested changes">
      <h3>Requested changes</h3>
      {build.review.feedback?.trim() ? <p style={{ whiteSpace: "pre-wrap" }}>{build.review.feedback}</p>
        : <p>No written feedback was saved with this result.</p>}
      <p>Review this feedback with the source and QA evidence before planning a correction. No repair estimate is available for this run.</p>
    </section>}
  </section>;
}

function Screenshot({ base, item }: { base: string; item: QaReport["evidence"][number] }) {
  const alive = useRef(false), savedUrl = useRef("");
  const [url, setUrl] = useState(""), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; if (savedUrl.current) URL.revokeObjectURL(savedUrl.current); }; }, []);
  async function show() {
    if (busy || url) return;
    setBusy(true); setError("");
    try {
      if (!/^[a-z0-9_-]+\.png$/.test(item.id) || item.media_type !== "image/png") {
        announceDiagnostic("integrity_error", "integrity", "unsupported_evidence_format");
        throw new Error("Unsupported evidence format.");
      }
      const response = await authenticatedFetch(`${base}/evidence/${encodeURIComponent(item.id)}`);
      if (!response.ok) throw new Error(await resourceReadFailure(response, "Screenshot"));
      const bytes = await response.arrayBuffer();
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
      if (bytes.byteLength !== item.size_bytes || bytes.byteLength > 10 * 1024 * 1024 || digest !== item.sha256) {
        announceDiagnostic("integrity_error", "integrity", "qa_screenshot_mismatch");
        throw new Error("Screenshot does not match its QA receipt.");
      }
      if (alive.current) { savedUrl.current = URL.createObjectURL(new Blob([bytes], { type: "image/png" })); setUrl(savedUrl.current); }
    } catch (err) { if (alive.current) setError(err instanceof Error ? err.message : "Screenshot could not be loaded."); }
    finally { if (alive.current) setBusy(false); }
  }
  return <figure>{url ? <img src={url} alt={`Independent product QA screenshot: ${item.id}`} style={{ maxWidth: "100%", height: "auto" }} />
    : <button type="button" disabled={busy} onClick={() => void show()}>{busy ? "Loading screenshot…" : `${error ? "Retry" : "Show"} screenshot: ${item.id}`}</button>}
    <ErrorNotice error={error} source="BuildEvidence.error" as="figcaption" /></figure>;
}
