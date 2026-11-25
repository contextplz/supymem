"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession } from "@/lib/client";
import { shortTitle } from "@/lib/format";

export default function EvolutionPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [title, setTitle] = useState("Evolution");
  const [summary, setSummary] = useState("Checking verified evolution history…");

  useEffect(() => {
    (async () => {
      await ensureSession();
      const ven = await api(`/api/ventures/${ventureId}`);
      if (ven.ok) {
        const v = ven.data as { name?: string; raw_idea?: string };
        setTitle(shortTitle(v.name, v.raw_idea));
      }
      const r = await api(`/api/ventures/${ventureId}/evolution`);
      if (r.ok) {
        const data = r.data as { summary?: string; cycles?: unknown[] };
        setSummary(data.summary || (data.cycles?.length ? `${data.cycles.length} verified cycles recorded.` : "No verified evolution cycles yet."));
      } else {
        setSummary("Evolution history is temporarily unavailable. Your company state has not been changed.");
      }
    })();
  }, [ventureId]);

  return (
    <AppShell title={title} ventureId={ventureId}>
      <div className="chat-scroll">
        <div className="chat-inner">
          <div className="msg assistant">
            <div className="msg-role">Supymem</div>
            <div className="bubble">
              <p>
                Evolution becomes available after the product is live and Supymem has
                real usage, feedback and a measurable goal. Every proposed change must
                be reviewable and reversible before it can affect the product.
              </p>
            </div>
          </div>
          <div className="panel-card">
            <h3>Evolution readiness</h3>
            <p>{summary}</p>
            <p className="muted" style={{ marginTop: 12 }}>
              Required before the first cycle: a deployed product, observed product
              metrics, founder or customer feedback, a target metric, and an approved
              rollback path. Supymem will show the proposal here before applying it.
            </p>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
