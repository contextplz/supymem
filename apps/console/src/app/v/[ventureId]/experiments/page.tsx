"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { shortTitle } from "@/lib/format";
import { prefillChat } from "@/lib/chatPrefill";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Experiment = {
  id: string; hypothesis: string; state: "proposed" | "running" | "concluded";
  outcome: string | null; result_text: string; learning: string; measure: string; how_to_run: string[];
  runner?: string | null; sample?: boolean; needs_evidence?: boolean;
};

type Suggested = { hypothesis: string; action: string; success_measure: string; source: string; direction: string };

const STATUS: Record<string, string> = { proposed: "Not run yet", running: "Running" };

function prefill(text: string) {
  prefillChat(text);
}

export default function ExperimentsPage() {
  const ventureId = String(useParams().ventureId || "");
  const active = useRef(ventureId), starting = useRef(false);
  active.current = ventureId;
  const [title, setTitle] = useState("Experiments");
  const [items, setItems] = useState<Experiment[]>([]);
  const [suggested, setSuggested] = useState<Suggested[]>([]);
  const [startError, setStartError] = useState("");
  const [reviewReady, setReviewReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function reload(started = false) {
    const current = () => active.current === ventureId;
    const next = started ? "Your test is running. Choose Try again to refresh your experiments." : "Your experiments could not be loaded. Choose Try again.";
    setLoading(true); setError("");
    try {
      await ensureSession();
      if (!current()) return;
      const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/experiments`);
      if (!current()) return;
      if (!r.ok) { setError(started ? next : `${apiFailure(r, "loading experiments")} Choose Try again.`); return; }
      const d = r.data as { items?: Experiment[]; review_ready?: boolean; suggested?: Suggested[] } | null;
      if (!d || (d.items != null && !Array.isArray(d.items)) || (d.suggested != null && !Array.isArray(d.suggested))) {
        setError(next); return;
      }
      setItems(d.items || []);
      setSuggested(d.suggested || []);
      setReviewReady(!!d.review_ready);
    } catch { if (current()) setError(next); }
    finally { if (current()) setLoading(false); }
  }

  async function startTest(test: Suggested) {
    if (starting.current || loading || error) return;
    starting.current = true; setBusy(true); setStartError("");
    const uncertain = "Your test start could not be confirmed. Choose Try again to check your saved experiments.";
    try {
      const { source: _source, ...body } = test;
      const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/experiments/start`, { method: "POST", body: JSON.stringify(body) });
      if (active.current !== ventureId) return;
      if (!r.ok) {
        if (!r.status || r.status >= 500) setError(uncertain);
        else if (r.status === 422) setError("This suggestion may be out of date. Choose Try again to see tests for your current idea.");
        else setStartError(apiFailure(r, "starting this test"));
        return;
      }
      const saved = r.data as Experiment | null;
      if (typeof saved?.id === "string" && saved.hypothesis === test.hypothesis && Array.isArray(saved.how_to_run)) {
        setItems(items => [saved, ...items.filter(item => item.id !== saved.id)]);
      }
      setSuggested(tests => tests.filter(item => item.hypothesis !== test.hypothesis));
      await reload(true);
    } catch {
      if (active.current === ventureId) setError(uncertain);
    } finally {
      if (active.current === ventureId) { starting.current = false; setBusy(false); }
    }
  }

  useEffect(() => {
    active.current = ventureId; starting.current = false;
    setTitle("Experiments"); setItems([]); setSuggested([]); setReviewReady(false); setBusy(false); setStartError("");
    void reload();
    void ensureSession().then(() => api(`/api/ventures/${encodeURIComponent(ventureId)}`)).then(ven => {
      if (ven.ok && active.current === ventureId) {
        const v = ven.data as { name?: string; raw_idea?: string };
        setTitle(shortTitle(v.name, v.raw_idea));
      }
    }).catch(() => {});
    return () => { active.current = ""; };
  }, [ventureId]);

  return (
    <AppShell title={title} ventureId={ventureId}>
      <div className="chat-scroll">
        <div className="chat-inner">
          <div className="panel-card">
            <h3>Experiments</h3>
            <p className="muted">Most ventures change direction. Each test below has a hypothesis, a result and what it taught you.
              A failed test is a useful answer.</p>
            {reviewReady && (
              <button className="primary" type="button" data-usage-control="experiment_review" onClick={() => prefill("What have we learned?")}>Review what we learned</button>
            )}
          </div>
          {loading && <div className="panel-card muted" role="status">Loading experiments…</div>}
          <ErrorNotice error={error} source="experiments.error" as="div" className="panel-card">
            {error} <button type="button" disabled={loading || busy} onClick={() => void reload()}>Try again</button>
          </ErrorNotice>
          {!loading && !error && items.length === 0 && suggested.length === 0 && (
            <div className="panel-card muted"><p>No experiments yet. Experiments are small tests that help you learn whether people need your idea.</p><button className="primary sm" type="button" onClick={() => prefill("Design an experiment to test my idea with the people I want to reach.")}>Design your first experiment</button></div>
          )}
          {!loading && !error && suggested.length > 0 && (
            <div className="panel-card">
              <h3>Suggested tests</h3>
              <p className="muted">Proposed by your {suggested.some((t) => t.source === "research") ? "validation and research reviews" : "validation review"}. Start one when you are ready to run it.</p>
              <ErrorNotice error={startError} source="experiments.startError" />
              {suggested.map((t) => (
                <div key={t.hypothesis} style={{ marginBottom: 12 }}>
                  <p><strong>Hypothesis:</strong> {t.hypothesis}</p>
                  {t.action && <p><strong>Try:</strong> {t.action}</p>}
                  {t.success_measure && <p><strong>Success measure:</strong> {t.success_measure}</p>}
                  <button type="button" data-usage-control="experiment_start_test" disabled={busy} onClick={() => void startTest(t)}>Start this test</button>
                </div>
              ))}
            </div>
          )}
          {items.map((e) => (
            <div className="panel-card" key={e.id}>
              <div className="kv-label">{e.needs_evidence ? "Needs evidence" : e.state === "concluded" ? (e.outcome || "Done") : STATUS[e.state]}</div>
              <div>{e.hypothesis}</div>
              {e.measure && <p className="muted">Measure: {e.measure}</p>}
              {e.result_text && <p>Result: {e.result_text}</p>}
              {e.learning && <p>Learned: {e.learning}</p>}
              {e.runner !== "system" && !e.sample && e.state === "proposed" && e.how_to_run.length > 0 && (
                <button type="button" data-usage-control="experiment_start" onClick={() => prefill(`Start the experiment: ${e.hypothesis.slice(0, 120)}`)}>Start it</button>
              )}
              {e.runner !== "system" && !e.sample && (e.state === "proposed" || e.state === "running" ||
                (e.state === "concluded" && e.needs_evidence)) && (
                <button type="button" data-usage-control="experiment_record" onClick={() => prefill(`Test results: ${e.id}\nWhat happened: `)}
                  style={{ marginLeft: 8 }}>{e.needs_evidence ? "Add evidence" : "Record result"}</button>
              )}
            </div>
          ))}
        </div>
      </div>
    </AppShell>
  );
}
