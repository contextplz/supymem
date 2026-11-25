"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { SlateGrid } from "@/components/idealab/SlateGrid";
import type { Opportunity } from "@/components/idealab/OpportunityCard";
import { api, ensureSession } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Slate = {
  id: string;
  workspace_id: string;
  opportunities: Opportunity[];
  evidence_stamp?: string;
};

function LabInner() {
  const router = useRouter();
  const params = useSearchParams();
  const workspaceId = params.get("workspace") || "";
  const [slate, setSlate] = useState<Slate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (ws: string) => {
    if (!ws) return;
    setBusy(true);
    setError("");
    await ensureSession();
    const res = await api(`/api/workspaces/${ws}/idealab/slate`);
    setBusy(false);
    if (!res.ok) {
      setError("No slate yet, generate one from the home page.");
      return;
    }
    setSlate(res.data as Slate);
  }, []);

  useEffect(() => {
    if (workspaceId) void load(workspaceId);
  }, [workspaceId, load]);

  async function reject(id: string) {
    if (!slate) return;
    setBusy(true);
    await ensureSession();
    await api(`/api/workspaces/${slate.workspace_id}/idealab/reject`, {
      method: "POST",
      body: JSON.stringify({ opportunity_id: id, reason_code: "not_interesting" }),
    });
    setBusy(false);
    void load(slate.workspace_id);
  }

  async function promote(id: string) {
    if (!slate) return;
    setBusy(true);
    await ensureSession();
    const res = await api(`/api/workspaces/${slate.workspace_id}/idealab/promote`, {
      method: "POST",
      body: JSON.stringify({ opportunity_id: id }),
    });
    setBusy(false);
    if (!res.ok) {
      setError("Could not promote that opening.");
      return;
    }
    const data = res.data as { venture_id?: string };
    if (data.venture_id) router.push(`/v/${data.venture_id}`);
  }

  async function reroll() {
    if (!slate) return;
    setBusy(true);
    await ensureSession();
    const res = await api(`/api/workspaces/${slate.workspace_id}/idealab/reroll`, {
      method: "POST",
      body: JSON.stringify({ previous_slate_id: slate.id }),
    });
    setBusy(false);
    if (!res.ok) {
      setError("Reroll failed.");
      return;
    }
    setSlate(res.data as Slate);
  }

  return (
    <AppShell title="Idea Lab">
      <div className="chat-scroll" style={{ padding: "var(--s-5)" }}>
        <div style={{ maxWidth: 960, margin: "0 auto" }}>
          <div className="eyebrow">Desk</div>
          <h2 style={{ marginTop: 4 }}>Openings to compare</h2>
          <p style={{ color: "var(--fg-1)", marginBottom: 20 }}>
            Three at a time. Reject shapes you do not want back.
          </p>
          <ErrorNotice error={error} source="lab.error" as="div" className="error" style={{ marginBottom: 12 }} />
          {slate?.evidence_stamp && (
            <div className="composer-hint" style={{ marginBottom: 12 }}>
              {slate.evidence_stamp}
            </div>
          )}
          <SlateGrid
            opportunities={slate?.opportunities || []}
            busy={busy}
            onReject={reject}
            onPromote={promote}
          />
          {slate && (
            <button
              type="button"
              className="chip"
              style={{ marginTop: 20 }}
              disabled={busy}
              onClick={() => void reroll()}
            >
              {busy ? "Working…" : "Reroll"}
            </button>
          )}
        </div>
      </div>
    </AppShell>
  );
}

export default function LabPage() {
  return (
    <Suspense fallback={<AppShell title="Idea Lab"><div className="chat-scroll" /></AppShell>}>
      <LabInner />
    </Suspense>
  );
}
