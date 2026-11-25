"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession } from "@/lib/client";
import { shortTitle } from "@/lib/format";

export default function SimulatePage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [title, setTitle] = useState("Simulate");
  const [result, setResult] = useState("Run a what-if to see a plain-language prediction.");

  useEffect(() => {
    (async () => {
      await ensureSession();
      const ven = await api(`/api/ventures/${ventureId}`);
      if (ven.ok) {
        const v = ven.data as { name?: string; raw_idea?: string };
        setTitle(shortTitle(v.name, v.raw_idea));
      }
    })();
  }, [ventureId]);

  async function simulate() {
    const r = await api(`/api/ventures/${ventureId}/worldmodel/simulate`, {
      method: "POST",
      body: JSON.stringify({ action: { kind: "increase_posting_rate" } }),
    });
    if (!r.ok) {
      setResult("Simulation unavailable right now.");
      return;
    }
    const data = r.data as { verdict?: string; summary?: string };
    setResult(data.summary || `Verdict: ${data.verdict || "neutral"}`);
  }

  return (
    <AppShell title={title} ventureId={ventureId}>
      <div className="chat-scroll">
        <div className="chat-inner">
          <div className="panel-card">
            <h3>Business what-if</h3>
            <p className="muted">Cheap prediction before the company changes itself.</p>
            <button className="primary" type="button" onClick={() => void simulate()} style={{ marginTop: 12 }}>
              Simulate posting more often
            </button>
            <p style={{ marginTop: 12 }}>{result}</p>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
