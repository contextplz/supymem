"use client";

import { useEffect, useRef, useState } from "react";
import { OneCard } from "@/components/primitives/OneCard";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { FOUNDER_STAGES, checkedFounderStage, founderAssetsClient, type FounderStage, type FounderStageName } from "@/lib/founderAssetsApi";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import type { GtmOwner } from "@/lib/gtmDiagnosisIntent";

export function StageConfirmation({ stage, confirmed, disabled, confirm }: { stage: FounderStageName; confirmed: boolean; disabled: boolean; confirm: (stage: FounderStageName) => void }) {
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState(stage);
  useEffect(() => { setSelected(stage); setEditing(false); }, [stage]);
  return <div aria-label="Founder stage confirmation">
    <p>{confirmed ? "Your stage:" : "We think you’re at"} <strong>{FOUNDER_STAGES[stage]}</strong>{confirmed ? "." : ". Right?"}</p>
    {editing && <label>Current stage <select aria-label="Current founder stage" value={selected} disabled={disabled} data-usage-control="founder_stage_select" onChange={event => setSelected(event.target.value as FounderStageName)}>
      {Object.entries(FOUNDER_STAGES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select></label>}
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
      {(!confirmed || editing) && <button type="button" className="ghost" disabled={disabled} data-usage-control="founder_stage_confirm" onClick={() => { confirm(editing ? selected : stage); setEditing(false); }}>{editing ? "Save stage" : "Yes"}</button>}
      <button type="button" className="ghost" disabled={disabled} data-usage-control="founder_stage_change" onClick={() => setEditing(!editing)}>{editing ? "Cancel change" : "Change"}</button>
    </div>
  </div>;
}

export function FounderStageCard({ owner, stageRecord, onSaved }: { owner: GtmOwner; stageRecord?: FounderStage | null; onSaved?: () => void }) {
  const [record, setRecord] = useState<FounderStage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const key = JSON.stringify([owner.actorId, owner.workspaceId, owner.ventureId]);
  const live = useRef(key); live.current = key;
  const mounted = useRef(true);
  const pending = useRef(false);
  const renderedEpoch = verifiedSessionEpoch();
  useEffect(() => {
    let active = true;
    mounted.current = true;
    const actor = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => active && live.current === key && actor === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    const retire = () => { active = false; mounted.current = false; setRecord(null); setError(""); setBusy(false); };
    window.addEventListener("supymem-session-changed", retire);
    void (stageRecord ? Promise.resolve().then(() => checkedFounderStage(stageRecord)) : founderAssetsClient.stage(owner))
      .then(value => { if (current()) setRecord(value); }).catch(() => { if (current()) setError("Your stage could not load. Reopen this venture to try again."); });
    return () => { active = false; mounted.current = false; window.removeEventListener("supymem-session-changed", retire); };
  }, [owner.actorId, owner.workspaceId, owner.ventureId, stageRecord]);
  async function confirm(stage: FounderStageName) {
    if (pending.current || !mounted.current || key !== live.current || owner.actorId !== verifiedSessionSubject() || renderedEpoch !== verifiedSessionEpoch()) return;
    const current = () => mounted.current && key === live.current && renderedEpoch === verifiedSessionEpoch() && owner.actorId === verifiedSessionSubject();
    pending.current = true; setBusy(true); setError("");
    try { const saved = await founderAssetsClient.confirmStage(owner, stage); if (current()) { setRecord(saved); window.dispatchEvent(new CustomEvent("supymem-venture-changed", { detail: { ventureId: owner.ventureId, actorId: owner.actorId, workspaceId: owner.workspaceId, epoch: renderedEpoch } })); onSaved?.(); } }
    catch { if (current()) setError("Your stage could not be saved. Try again."); }
    finally { pending.current = false; if (current()) setBusy(false); }
  }
  return <OneCard title="Where you are now" body="Your assets and progress guide the next step. Research stays part of every stage.">
    <ErrorNotice error={error} source="founder_stage" />
    {record ? <StageConfirmation stage={record.stage} confirmed={record.confirmed_by_founder} disabled={busy} confirm={stage => void confirm(stage)} /> : !error && <p role="status">Reading your stage…</p>}
    {record?.evidence.some(item => typeof item.detail === "string") && <details><summary data-usage-control="founder_stage_evidence">Why this stage?</summary><ul>{record.evidence.filter(item => typeof item.detail === "string").slice(0, 4).map((item, index) => <li key={index}>{item.detail}</li>)}</ul></details>}
  </OneCard>;
}
