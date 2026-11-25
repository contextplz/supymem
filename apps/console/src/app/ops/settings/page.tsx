"use client";

import { useEffect, useRef, useState } from "react";
import { getOpsWorkspace, opsApi, type CompanyConfig } from "@/lib/opsApi";
import { useOpsQuery } from "@/hooks/useOpsQuery";
import { isMissingCompanyConfig, companySettingsPayload } from "@/lib/opsSettings";
import { cx } from "@/lib/cx";
import styles from "@/components/ops/ops.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const EMPTY_CONFIG: CompanyConfig = {
  name: "", industry: "", product_type: "", website_url: "", mission: "", vision: "",
  description: "", target_market: "", value_prop: "", timezone: "UTC", daily_cycle_hour: 6,
};

async function loadConfig() {
  try { return { config: await opsApi.get<CompanyConfig>("/config") }; }
  catch (error) {
    if (isMissingCompanyConfig(error)) return { config: null };
    throw error;
  }
}

export default function OpsSettingsPage() {
  // OpsWorkspaceGate keys this screen by actor and workspace. useOpsQuery
  // cancels stale reads on unmount; the save fence also ignores late writes.
  const result = useOpsQuery(loadConfig);
  const [edited, setConfig] = useState<CompanyConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [created, setCreated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = useRef(false);
  const inFlight = useRef(false);
  const workspace = useRef(getOpsWorkspace());
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const config = edited ?? result.data?.config ?? EMPTY_CONFIG;
  const creating = !result.data?.config && !created;

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (inFlight.current || workspace.current !== getOpsWorkspace()) return;
    inFlight.current = true;
    setSaving(true);
    setSaved(false);
    setError(null);
    const current = () => active.current && workspace.current === getOpsWorkspace();
    try {
      const updated = await opsApi.put<CompanyConfig>("/config", companySettingsPayload(config));
      if (current()) { setConfig(updated); setSaved(true); setCreated(true); }
    } catch (e) {
      if (current()) setError(e instanceof Error ? e.message : "Settings could not be saved. Please try again.");
    } finally {
      inFlight.current = false;
      if (current()) setSaving(false);
    }
  };

  if (result.loading) return <div className={styles.page}><p role="status">Loading settings…</p></div>;
  if (result.error) return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Settings</h1>
      <ErrorNotice error={result.error} as="div" source="ops.settings.load"><p>Company settings could not be loaded. {result.error}</p></ErrorNotice>
      <button type="button" onClick={result.reload}>Retry</button>
    </div>
  );

  const field = (label: string, key: keyof CompanyConfig, type: "text" | "number" = "text") => (
    <div key={key} className={styles.field}>
      <label htmlFor={`ops-${key}`}>{label}</label>
      <input
        id={`ops-${key}`}
        type={type}
        value={(config[key] as string | number) ?? ""}
        required={key === "name" || key === "timezone" || type === "number"}
        min={type === "number" ? 0 : undefined}
        max={type === "number" ? 23 : undefined}
        step={type === "number" ? 1 : undefined}
        onChange={(e) => {
          setSaved(false);
          setConfig({ ...config, [key]: type === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value });
        }}
      />
    </div>
  );

  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Settings</h1>

      {creating && <p>No company profile has been saved for this workspace. Enter your company details to create one.</p>}

      {saved && (
        <div role="status" className={cx(styles.banner, styles.bannerOk)}>Settings saved successfully</div>
      )}
      <ErrorNotice error={error} as="div" source="ops.settings.save" className={cx(styles.banner, styles.bannerDanger)} />

      <form onSubmit={save}>
        <fieldset disabled={saving} style={{ border: 0, padding: 0, margin: 0 }}>
        <div className={styles.formPanel}>
          <h2>Company Profile</h2>
          {field("Company Name", "name")}
          {field("Industry", "industry")}
          {field("Product Type", "product_type")}
          {field("Website URL", "website_url")}
          {field("Mission", "mission")}
          {field("Vision", "vision")}
          {field("Description", "description")}
          {field("Target Market", "target_market")}
          {field("Value Proposition", "value_prop")}
        </div>

        <div className={styles.formPanel}>
          <h2>Scheduler</h2>
          {field("Timezone", "timezone")}
          {field("Morning Cycle Hour (UTC)", "daily_cycle_hour", "number")}
        </div>

        <div>
          <button type="submit" className="primary" disabled={saving}>
            {saving ? "Saving…" : creating ? "Create Company Profile" : "Save Settings"}
          </button>
        </div>
        </fieldset>
      </form>
    </div>
  );
}
