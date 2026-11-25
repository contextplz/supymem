"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type ResourceAsset = { id?: string; kind: string; name: string; exists: boolean; source?: string;
  accessible?: boolean | null; compatible?: boolean | null;
  detail: { provider?: string | null; plan?: string | null; notes?: string | null; credit_amount?: string | null;
    currency?: string | null; expires_on?: string | null; [key: string]: unknown } };
export type ResourceSnapshot = { inventory: { complete: boolean; assets: ResourceAsset[] };
  capabilities?: { id: string; state: string; reason: string }[];
  execution_budget?: { status?: string; gaps?: string[] } };
const kinds = { coding_agent: "Coding tool", ai_subscription: "AI subscription", aws_account: "AWS account", aws_credits: "AWS credits",
  cloud_account: "Cloud account", cloud_credits: "Cloud credits", api_subscription: "API subscription", llm_key: "AI provider account",
  repo: "Existing project", domain: "Domain", email_provider: "Email service", analytics: "Analytics", brand_asset: "Brand assets",
  saas_subscription: "Other subscription", engineering_time: "Engineering time" };

export function FounderResourceInventory({ ventureId, snapshot, disabled, onSaved, onDirty }: {
  ventureId: string; snapshot: ResourceSnapshot; disabled: boolean; onSaved: () => void; onDirty: () => void;
}) {
  const [assets, setAssets] = useState<ResourceAsset[]>([]);
  const [complete, setComplete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setAssets(snapshot.inventory.assets.filter(asset => asset.source === "founder_inventory"));
    setComplete(snapshot.inventory.complete); setMessage("");
  }, [snapshot]);
  const blocked = busy || disabled;
  function change(index: number, patch: Partial<ResourceAsset>) {
    onDirty(); setSaved(false);
    setAssets(rows => rows.map((row, i) => i === index ? { ...row, ...patch } : row));
  }
  async function save() {
    setSaved(false); setBusy(true); setMessage("");
    const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/resources`, { method: "PUT", body: JSON.stringify({
      complete, assets: assets.map(({ id, kind, name, exists, detail }) => ({ ...(id ? { id } : {}), kind, name, exists, detail })),
    }) });
    setBusy(false);
    if (!result.ok) { setMessage(apiFailure(result, "saving your existing resources")); return; }
    setSaved(true); onSaved();
  }
  return <section className="plan-resource-inventory">
    <h2>What do you already have?</h2>
    <p>List the tools, subscriptions, cloud credits or existing code you want the plan to consider. Access and compatibility still need verification before they can reduce your costs.</p>
    <p>Add subscription names, tiers and credit balances here. Connect API keys securely in Settings.</p>
    {snapshot.inventory.assets.filter(asset => asset.source !== "founder_inventory").map(asset => <p key={asset.id}>
      Existing connection: {asset.name} · {asset.accessible === true ? "Access recorded" : "Access needs verification"}
    </p>)}
    {assets.map((asset, index) => <fieldset key={asset.id || index} disabled={blocked}>
      <legend>Resource {index + 1}</legend>
      <label>Type<select value={asset.kind} onChange={event => change(index, { kind: event.target.value })}>
        {Object.entries(kinds).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
      </select></label>
      <label>Name<input maxLength={120} value={asset.name} placeholder="For example, Cursor Pro or AWS credits" onChange={event => change(index, { name: event.target.value })} /></label>
      <label>Provider<input maxLength={120} value={asset.detail.provider || ""} onChange={event => change(index, { detail: { ...asset.detail, provider: event.target.value || null } })} /></label>
      <label>Plan or tier<input maxLength={120} value={asset.detail.plan || ""} onChange={event => change(index, { detail: { ...asset.detail, plan: event.target.value || null } })} /></label>
      {asset.kind.endsWith("credits") && <>
        <label>Remaining credit amount<input inputMode="decimal" value={asset.detail.credit_amount || ""} onChange={event => change(index, { detail: { ...asset.detail, credit_amount: event.target.value || null } })} /></label>
        <label>Currency<select value={asset.detail.currency || ""} onChange={event => change(index, { detail: { ...asset.detail, currency: event.target.value || null } })}>
          <option value="">Select currency</option>{["USD", "INR", "EUR", "GBP", "CAD", "AUD", "SGD", "JPY"].map(value => <option key={value}>{value}</option>)}
        </select></label>
        <label>Expires on<input type="date" value={asset.detail.expires_on || ""} onChange={event => change(index, { detail: { ...asset.detail, expires_on: event.target.value || null } })} /></label>
      </>}
      <label>Notes<textarea maxLength={500} value={asset.detail.notes || ""} onChange={event => change(index, { detail: { ...asset.detail, notes: event.target.value || null } })} /></label>
      <label><input type="checkbox" checked={asset.exists} onChange={event => change(index, { exists: event.target.checked })} /> I currently have this resource</label>
      <button type="button" onClick={() => { onDirty(); setSaved(false); setAssets(rows => rows.filter((_, i) => i !== index)); }}>Remove resource</button>
    </fieldset>)}
    <button type="button" disabled={blocked || assets.length >= 40} onClick={() => { onDirty(); setSaved(false); setAssets(rows => [...rows, { kind: "coding_agent", name: "", exists: true, detail: {} }]); }}>Add resource</button>
    <label><input type="checkbox" disabled={blocked} checked={complete} onChange={event => { onDirty(); setSaved(false); setMessage(""); setComplete(event.target.checked); }} />
      {assets.length ? " This is my current resource list" : " I have no existing resources to include"}
    </label>
    <p><small>Tick this when the list is complete. Plan estimates stay locked until a complete list is saved.</small></p>
    <button type="button" disabled={blocked || assets.some(asset => !asset.name.trim())} onClick={() => void save()}>{busy ? "Saving…" : "Save resource inventory"}</button>
    <ErrorNotice error={message} source="FounderResourceInventory.message" />
    {saved && !message && <p role="status">{complete
      ? "Saved. Plans will use this resource list."
      : `Saved as a draft. Plans will not use it until you tick “${assets.length ? "This is my current resource list" : "I have no existing resources to include"}” and save again.`}</p>}
  </section>;
}
