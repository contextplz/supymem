"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export type ApprovalPolicy = {
  eligible: boolean;
  scope_label: string;
  venture_id: string;
  workspace_id: string;
};

export function ApprovalPolicyControls({ policy, ventureId, messageId, action, resultingState, disabled, allowDisabled, current, onApproved, onDenied }: {
  policy: ApprovalPolicy; ventureId: string; messageId: string; action: string; disabled: boolean;
  resultingState: Record<string, unknown>;
  allowDisabled?: boolean;
  current: () => boolean; onApproved: () => void; onDenied: () => void;
}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const owner = `${verifiedSessionSubject()}:${verifiedSessionEpoch()}:${ventureId}:${messageId}:${action}:${JSON.stringify(resultingState)}`;
  const active = useRef(owner); active.current = owner;
  const flight = useRef<object | null>(null), mounted = useRef(true);
  useEffect(() => {
    mounted.current = true; active.current = owner;
    const retire = () => { active.current = ""; flight.current = null; setBusy(false); setError(""); };
    window.addEventListener("supymem-session-changed", retire);
    return () => { mounted.current = false; retire(); window.removeEventListener("supymem-session-changed", retire); };
  }, [owner]);

  async function choose(choice: "always" | "deny") {
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    if (disabled || flight.current || !subject || !current() || active.current !== owner
        || ventureId !== policy.venture_id || choice === "always" && (!policy.eligible || allowDisabled)) return;
    const request = {}; flight.current = request; setBusy(true); setError("");
    const owned = () => mounted.current && active.current === owner && flight.current === request
      && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch && current();
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(policy.workspace_id)}/approvals`, {
        method: "POST", body: JSON.stringify({ kind: "scope_grant", subject_ref: messageId,
          summary: "Your confirmation choice", requested_by: subject,
          detail: { standing_policy_v1: { choice, venture_id: ventureId, message_id: messageId, action, resulting_state: resultingState } } }),
      });
      if (!owned()) return;
      if (!result.ok) { setError("Your approval choice could not be confirmed. Check its saved status before trying again."); return; }
      if (choice === "always") onApproved(); else onDenied();
    } catch {
      if (owned()) setError("Your approval choice could not be confirmed. Check its saved status before trying again.");
    } finally {
      if (owned()) { flight.current = null; setBusy(false); }
    }
  }

  return <span>
    <button type="button" className="secondary sm" disabled={disabled || busy || !policy.eligible || allowDisabled}
      title={!policy.eligible ? "You need to approve this action each time." : undefined}
      data-usage-control="approval_always" onClick={() => void choose("always")}>
      Always allow this for {policy.scope_label}
    </button>
    <button type="button" className="secondary sm" disabled={disabled || busy}
      data-usage-control="approval_deny" onClick={() => void choose("deny")}>Deny</button>
    {policy.eligible && <small>Expires after 30 days. You can revoke it in Settings.</small>}
    <ErrorNotice as="span" error={error} source="chat.approval_policy" />
  </span>;
}
