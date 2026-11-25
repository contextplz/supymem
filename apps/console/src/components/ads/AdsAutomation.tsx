"use client";

import { plainJob } from "@/lib/jobCopy";
import { opsErrorMessage } from "@/lib/errorCopy";

import { type CSSProperties, type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { api } from "@/lib/client";
import { diagnosisClient, type GtmVenture } from "@/lib/gtmDiagnosisApi";
import {
  createAdProposal,
  createPausedAdCampaign,
  decideAdLaunch,
  getAdCampaigns,
  getAdProposals,
  getAdsReadiness,
  requestAdLaunch,
  type AdCampaign,
  type AdProposal,
} from "@/lib/opsApi";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const field: CSSProperties = { display: "grid", gap: "0.35rem", minWidth: 0 };
const input: CSSProperties = { width: "100%" };
const card: CSSProperties = {
  border: "1px solid var(--line, #d8d8d8)",
  borderRadius: 12,
  padding: "1rem",
  display: "grid",
  gap: "0.75rem",
};

function errorText(error: unknown): string {
  return opsErrorMessage(error, "This ads step did not finish. Your saved work is safe. Try again.");
}

export function AdsAutomation() {
  const operationsScope = useOperationsScope();
  const [platform, setPlatform] = useState<"google" | "meta">("google");
  const [goal, setGoal] = useState<"leads" | "sales" | "traffic">("leads");
  const [idea, setIdea] = useState("");
  const [landingUrl, setLandingUrl] = useState("");
  const [audience, setAudience] = useState("");
  const [geography, setGeography] = useState("US");
  const [budget, setBudget] = useState("10");
  const [proposals, setProposals] = useState<AdProposal[]>([]);
  const [campaigns, setCampaigns] = useState<AdCampaign[]>([]);
  const [readiness, setReadiness] = useState<Record<string, { ok: boolean; missing: string[]; note: string }> | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const companyContextAttempt = useRef("");

  const refresh = useCallback(async () => {
    try {
      const [nextReadiness, nextProposals, nextCampaigns] = await Promise.all([
        getAdsReadiness(),
        getAdProposals(),
        getAdCampaigns(),
      ]);
      setReadiness(nextReadiness.providers);
      setProposals(nextProposals.items);
      setCampaigns(nextCampaigns);
    } catch (caught) {
      setError(errorText(caught));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!operationsScope?.actorId || !operationsScope.workspaceId || !operationsScope.ventureId) return;
    const owner = {
      actorId: operationsScope.actorId,
      workspaceId: operationsScope.workspaceId,
      ventureId: operationsScope.ventureId,
    };
    const attempt = `${owner.actorId}:${owner.workspaceId}:${owner.ventureId}`;
    if (companyContextAttempt.current === attempt) return;
    companyContextAttempt.current = attempt;
    let active = true;
    void (async () => {
      try {
        const result = await api(`/api/ventures/${encodeURIComponent(owner.ventureId)}`);
        if (!result.ok) return;
        const venture = result.data as GtmVenture;
        const context = await diagnosisClient.context(owner, venture);
        if (!active) return;
        setIdea((current) => current || context.idea || context.problem);
        setAudience((current) => current || context.who);
      } catch {
        // Company context is a convenience. The founder can still enter the brief manually.
      }
    })();
    return () => { active = false; };
  }, [operationsScope?.actorId, operationsScope?.workspaceId, operationsScope?.ventureId]);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    setBusy("create");
    try {
      const proposal = await createAdProposal({
        platform,
        idea,
        goal,
        landing_url: landingUrl,
        audience,
        geography,
        daily_budget_usd: Number(budget),
      });
      setNotice("Your campaign blueprint is ready for review.");
      await refresh();
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      setBusy("");
    }
  }

  async function act(proposal: AdProposal, action: "request" | "approve" | "reject" | "create") {
    setBusy(`${action}:${proposal.id}`);
    setError("");
    setNotice("");
    try {
      if (action === "request") await requestAdLaunch(proposal.id);
      if (action === "approve") await decideAdLaunch(proposal.id, "approved");
      if (action === "reject") await decideAdLaunch(proposal.id, "rejected");
      if (action === "create") {
        const result = await createPausedAdCampaign(proposal.id);
        if (!result.ok) throw new Error("Campaign creation did not finish. Review the saved campaign before trying again.");
        setNotice("Your campaign was created paused.");
      }
      await refresh();
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      setBusy("");
    }
  }

  return (
    <div style={{ display: "grid", gap: "1.25rem", maxWidth: 1080 }}>
      <section style={card}>
        <div>
          <h2 style={{ margin: 0 }}>From idea to a paused campaign</h2>
          <p className="muted" style={{ marginBottom: 0 }}>
            We create an approval-backed blueprint first. Creating a campaign never activates spend.
          </p>
        </div>
        <form onSubmit={create} style={{ display: "grid", gap: "0.85rem" }}>
          {(idea || audience) && <p className="muted" style={{ margin: 0 }}>Started from the confirmed company idea. Review every field before building a campaign blueprint.</p>}
          <label style={field}>
            What are you advertising?
            <textarea value={idea} onChange={(e) => setIdea(e.target.value)} rows={4} required placeholder="Describe the product, offer, and customer problem." />
          </label>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "0.75rem" }}>
            <label style={field}>Platform<select value={platform} onChange={(e) => setPlatform(e.target.value as "google" | "meta")}><option value="google">Google Search</option><option value="meta">Meta</option></select></label>
            <label style={field}>Goal<select value={goal} onChange={(e) => setGoal(e.target.value as "leads" | "sales" | "traffic")}><option value="leads">Leads</option><option value="sales">Sales</option><option value="traffic">Traffic</option></select></label>
            <label style={field}>Daily cap (USD)<input style={input} type="number" min="0.01" max="25" step="0.01" value={budget} onChange={(e) => setBudget(e.target.value)} required /></label>
          </div>
          <label style={field}>Landing page<input style={input} type="url" value={landingUrl} onChange={(e) => setLandingUrl(e.target.value)} required placeholder="https://example.com/demo" /></label>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: "0.75rem" }}>
            <label style={field}>Audience<input style={input} value={audience} onChange={(e) => setAudience(e.target.value)} required placeholder="Who should see this?" /></label>
            <label style={field}>Country/region<input style={input} value={geography} onChange={(e) => setGeography(e.target.value)} required placeholder="US" /></label>
          </div>
          <button className="primary" type="submit" disabled={busy === "create"}>{busy === "create" ? "Building…" : "Build campaign blueprint"}</button>
        </form>
      </section>

      {readiness && <section style={card}><h2 style={{ margin: 0 }}>Provider readiness</h2><p className="muted" style={{ margin: 0 }}>Ads are optional. Connect a provider only when you are ready to prepare founder-approved, paused campaigns.</p>{Object.entries(readiness).map(([provider, status]) => <div key={provider}><strong>{provider === "google" ? "Google Ads" : "Meta Ads"}</strong> · {status.ok ? "Connected" : "Not connected"}{!status.ok ? <span className="muted"> · An operator must complete the secure provider setup before this company can connect it.</span> : null}</div>)}</section>}
      {notice && <p role="status" style={{ color: "var(--green, #196c3d)", margin: 0 }}>{notice}</p>}
      <ErrorNotice error={error} source="AdsAutomation.error" className="error" style={{ margin: 0 }} />

      <section style={{ display: "grid", gap: "0.75rem" }}>
        <h2 style={{ margin: 0 }}>Campaign blueprints</h2>
        {proposals.length === 0 && <p className="muted">No campaigns yet. Describe an idea above to prepare the first one.</p>}
        {proposals.map((proposal) => <article key={proposal.id} style={card}>
          <div><strong>{proposal.blueprint.name}</strong> · {proposal.platform} · <span>{plainJob(proposal).title}</span><p className="muted" style={{ marginBottom: 0 }}>{proposal.blueprint.goal} · ${proposal.blueprint.daily_budget_usd}/day · {proposal.blueprint.geography}<br />{proposal.blueprint.audience}<br />{proposal.blueprint.landing_url}</p></div>
          <details><summary>Review campaign blueprint</summary><pre style={{ whiteSpace: "pre-wrap", overflowX: "auto" }}>{JSON.stringify(proposal.blueprint, null, 2)}</pre></details>
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            {proposal.status === "draft" && <button className="primary" onClick={() => void act(proposal, "request")} disabled={Boolean(busy)}>Request launch approval</button>}
            {proposal.status === "pending_approval" && <><button className="primary" onClick={() => void act(proposal, "approve")} disabled={Boolean(busy)}>Approve paused creation</button><button className="danger" onClick={() => void act(proposal, "reject")} disabled={Boolean(busy)}>Reject</button></>}
            {proposal.status === "approved" && <button className="primary" onClick={() => void act(proposal, "create")} disabled={Boolean(busy)}>Create paused campaign</button>}
          </div>
        </article>)}
      </section>

      <section style={{ display: "grid", gap: "0.75rem" }}>
        <h2 style={{ margin: 0 }}>Provider campaigns</h2>
        {campaigns.length === 0 && <p className="muted">Nothing has been created at a provider.</p>}
        {campaigns.map((campaign) => <article key={campaign.id} style={card}><strong>{campaign.name}</strong><span>{campaign.platform} · {campaign.objective} · <span>{plainJob(campaign).title}</span> · ${campaign.daily_budget_usd}/day</span></article>)}
      </section>
    </div>
  );
}
