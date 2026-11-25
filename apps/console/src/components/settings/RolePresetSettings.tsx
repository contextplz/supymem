"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "@/app/account/account.module.css";

const ROLES = [
  ["sales", "Sales"],
  ["chief_of_staff", "Chief of Staff"],
  ["product", "Product"],
] as const;
type Role = typeof ROLES[number][0];
type SavedRole = { role_preset: Role; focus: string; suggestions: string };

function checkedRole(value: unknown): SavedRole {
  const role = value as SavedRole | null;
  if (!role || !ROLES.some(([preset]) => preset === role.role_preset)
    || typeof role.focus !== "string" || typeof role.suggestions !== "string") {
    throw new Error("Your saved role could not be confirmed.");
  }
  return role;
}

export function RolePresetSettings({ ventureId }: { ventureId?: string }) {
  const [saved, setSaved] = useState<{ owner: string; value: SavedRole } | null>(null);
  const [draft, setDraft] = useState<Role>("chief_of_staff");
  const [busy, setBusy] = useState(false), [note, setNote] = useState(""), [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const generation = useRef(0), flight = useRef<object | null>(null);
  const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const owner = JSON.stringify([subject, epoch, ventureId]);
  const active = useRef(owner); active.current = owner;
  const path = `/api/ventures/${encodeURIComponent(ventureId || "")}/task-preferences/role-preset`;

  useEffect(() => {
    let mounted = true;
    async function load() {
      const ticket = ++generation.current;
      flight.current = null; setSaved(null); setBusy(false); setNote(""); setError("");
      const current = () => mounted && ticket === generation.current && active.current === owner
        && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
      if (!subject || !ventureId || !current()) return;
      try {
        const result = await api(path);
        if (!current()) return;
        if (!result.ok) throw new Error("Role preferences unavailable");
        const value = checkedRole(result.data);
        setSaved({ owner, value }); setDraft(value.role_preset);
      } catch { if (current()) setError("Your role could not be loaded. Refresh saved settings to try again."); }
    }
    void load(); window.addEventListener("supymem-session-changed", load);
    return () => { mounted = false; ++generation.current; flight.current = null; window.removeEventListener("supymem-session-changed", load); };
  }, [owner, attempt]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const ticket = generation.current;
    const current = () => ticket === generation.current && active.current === owner
      && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    if (!current() || !subject || !ventureId || saved?.owner !== owner || error || flight.current) return;
    const request = {}; flight.current = request; setBusy(true); setNote("");
    try {
      const result = await api(path, { method: "PATCH", body: JSON.stringify({ role_preset: draft }) });
      if (!current() || flight.current !== request) return;
      if (!result.ok) throw new Error("Role change unconfirmed");
      const value = checkedRole(result.data);
      if (value.role_preset !== draft) throw new Error("Role change unconfirmed");
      setSaved({ owner, value });
      setNote("You saved your focus. Open chat to choose your next step; your approvals and limits stay the same.");
    } catch { if (current()) setError("Your role change could not be confirmed. Refresh saved settings before trying again."); }
    finally { if (current() && flight.current === request) { flight.current = null; setBusy(false); } }
  }

  const ready = saved?.owner === owner && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
  return <section className={styles.section} aria-label="Charlie's role" aria-busy={busy || Boolean(subject && ventureId && !ready && !error)}>
    <h2>What Charlie focuses on</h2>
    <p>You choose a focus for this venture. Your permissions, spending limits and sending approvals stay the same.</p>
    {!subject ? <p>Sign in, then choose your focus.</p> : !ventureId ? <p>Choose a venture, then save your focus.</p> : <>
      {!ready && !error && <p role="status">Loading your focus. You can choose a role when it is ready.</p>}
      {ready && <form onSubmit={save}>
        <fieldset disabled={busy || Boolean(error)}>
          <label>Role preset<select value={draft} data-usage-control="charlie_role_preset" onChange={event => {
            if (ROLES.some(([preset]) => preset === event.target.value)) { setDraft(event.target.value as Role); setNote(""); }
          }}>{ROLES.map(([preset, label]) => <option key={preset} value={preset}>{label}</option>)}</select></label>
          <p>{saved.value.focus} {saved.value.suggestions}</p>
          <p>Choose a role, then save your focus.</p>
          <button type="submit" className="primary sm" data-usage-control="charlie_role_preset_save">{busy ? "Saving your focus..." : "Save your focus"}</button>
        </fieldset>
      </form>}
      {note && <p role="status">{note}</p>}
      {error && <><ErrorNotice error={error} source="RolePresetSettings.error" /><button type="button" disabled={busy} onClick={() => setAttempt(value => value + 1)}>Refresh saved settings</button></>}
    </>}
  </section>;
}
