"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/client";

export type SecretField = {
  env_key: string;
  label: string;
  input?: string;
};

type Props = {
  slotId: string;
  fields: SecretField[];
  workspaceId?: string;
  onSaved?: () => void;
  onStatusChange?: (armed: boolean) => void;
};

export function CredentialField({ slotId, fields, workspaceId, onSaved, onStatusChange }: Props) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [prints, setPrints] = useState<Record<string, string>>({});
  const [armed, setArmed] = useState(false);
  const hasInput = useMemo(
    () => fields.some((field) => Boolean((values[field.env_key] || "").trim())),
    [fields, values],
  );

  useEffect(() => {
    let active = true;
    void api(`/api/secret-slots/${encodeURIComponent(slotId)}/status`).then((r) => {
      if (!active || !r.ok || !r.data || typeof r.data !== "object") return;
      const ready = Boolean((r.data as { armed?: boolean }).armed);
      setArmed(ready);
      onStatusChange?.(ready);
    });
    return () => {
      active = false;
    };
  }, [onStatusChange, slotId]);

  async function save() {
    setBusy(true);
    setMsg("");
    const body: Record<string, string> = {};
    for (const field of fields) {
      const raw = (values[field.env_key] || "").trim();
      if (raw) body[field.env_key] = raw;
    }
    const r = await api(`/api/secret-slots/${encodeURIComponent(slotId)}/value`, {
      method: "POST",
      body: JSON.stringify({ values: body, workspace_id: workspaceId || undefined }),
    });
    setBusy(false);
    setValues({});
    if (!r.ok) {
      setMsg((r.data as { error?: { message?: string } } | null)?.error?.message || "Could not save.");
      return;
    }
    const fps = (r.data as { fingerprints?: Record<string, string> }).fingerprints || {};
    setPrints(fps);
    setArmed(true);
    onStatusChange?.(true);
    setMsg("Saved securely. Supymem will never display the credential again.");
    onSaved?.();
  }

  return (
    <div>
      {fields.map((field) => (
        <div key={field.env_key} style={{ marginTop: 12 }}>
          <label htmlFor={`secret-${slotId}-${field.env_key}`}>{field.label}</label>
          <input
            id={`secret-${slotId}-${field.env_key}`}
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={values[field.env_key] || ""}
            onChange={(e) => setValues((cur) => ({ ...cur, [field.env_key]: e.target.value }))}
            style={{ width: "100%", marginTop: 6 }}
          />
          <p className="muted" style={{ marginTop: 4 }}>
            {prints[field.env_key]
              ? `Saved securely · ends in ${prints[field.env_key]}`
              : armed
                ? "A credential is already stored. Enter a new value only to replace it."
                : "Stored in Supymem's credential vault and never shown again."}
          </p>
        </div>
      ))}
      <button type="button" className="primary" style={{ marginTop: 16 }} disabled={busy || !hasInput} onClick={() => void save()}>
        {busy ? "Saving…" : armed ? "Replace credential" : "Save securely"}
      </button>
      {msg ? <p className="muted">{msg}</p> : null}
    </div>
  );
}
