"use client";

import { SettingsSection } from "./SettingsSection";
import { DataSettings } from "./DataSettings";
import { CodingAgentTiles } from "@/components/connections/CodingAgentTiles";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { Fragment, useEffect, useState } from "react";
import { api } from "@/lib/client";
import { settingsFailureMessage } from "../../setup/localProviders";
import type { SettingsCatalog } from "./sections";
import { SpendSection } from "@/components/credits/SpendSection";
import { useFeatureVisibility } from "@/components/layout/LocalModeBoundary";
import styles from "./local.module.css";

export function LocalSettings({ optional = false }: { optional?: boolean }) {
  const visible = useFeatureVisibility();
  const [catalog, setCatalog] = useState<SettingsCatalog | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [copyNote, setCopyNote] = useState("");
  useEffect(() => {
    let active = true;
    setError("");
    void api("/api/local/settings/catalog").then(result => {
      if (!active) return;
      const data = result.data as SettingsCatalog | undefined;
      if (!result.ok || !data || typeof data.env_path !== "string" || !Array.isArray(data.sections)) {
        setError(result.ok ? "Your connections could not be loaded. Restart your local app, then choose Try again on Keys and connections."
          : settingsFailureMessage(result) + " Open Keys and connections and choose Try again.");
      } else setCatalog(data);
    }).catch(() => {
      if (active) setError("Your connections could not be loaded. Restart your local app, then choose Try again on Keys and connections.");
    });
    return () => { active = false; };
  }, [attempt]);
  const order = new Map(["providers", "search", "smtp", "publishing", "composio", "images", "spend", "data"].map((id, index) => [id, index]));
  const sections = catalog?.sections.filter(section => visible("settings", section.id) && (!optional || !["providers", "spend", "data"].includes(section.id)))
    .sort((first, second) => (order.get(first.id) ?? order.size) - (order.get(second.id) ?? order.size)) ?? [];
  const Container = optional ? "div" : "main";
  async function copyEnvPath() {
    if (!catalog) return;
    try {
      await navigator.clipboard.writeText(catalog.env_path);
      setCopyNote("Copied");
    } catch {
      setCopyNote("Select the .env path below and copy it with your keyboard.");
    }
  }
  return (
    <Container className={styles.page} tabIndex={optional ? undefined : -1} aria-labelledby={optional ? undefined : "local-settings-title"}>
      <header className={styles.header}><h1 id={optional ? undefined : "local-settings-title"}>Keys and connections</h1>{!optional && <a href="/settings/local/capabilities">Capabilities</a>}</header>
      {optional && <p>Your model is ready. These connections are optional. You can add these later in Keys and connections.</p>}
      <p>Your settings stay on your machine. Saved keys are encrypted and never shown here. Leave a key blank to keep it unchanged. Keep your data folder and full backups private.</p>
      {!optional && <p>Full backups include your encrypted settings, not your recovery key. See <a href="#your-data-title">Your data and recovery</a> for recovery steps.</p>}
      {catalog?.storage_notice && <p role="status">{catalog.storage_notice}</p>}
      {sections.flatMap(section => section.fields.filter(field => field.secret && field.state === "unreadable").map(field =>
        <ErrorNotice key={`${section.id}-${field.name}`} source="settings.local.unreadable_key"
          error={`Your ${field.label} could not be read. Re-enter your ${field.label} below under ${section.title}, then choose Save your settings.`} />))}
      {error && <><ErrorNotice error={error} source="settings.local.connections" intent="connections.load" /><button type="button" onClick={() => setAttempt(attempt + 1)}>Try again</button></>}
      {!catalog && !error && <p className="page-loading" role="status">Loading your connections. Please wait.</p>}
      {catalog?.import_error && <ErrorNotice error={`${catalog.import_error} Your saved settings are unchanged. You can edit your connection here instead.`} source="settings.local.connections" intent="connections.import" />}
      {!optional && <div id="coding-tools"><CodingAgentTiles /></div>}
      {sections.map(section => <Fragment key={section.id}>
        <SettingsSection section={section} onSaved={() => setAttempt(current => current + 1)} />
        {!optional && section.id === "spend" && <SpendSection key={attempt} />}
      </Fragment>)}
      {!optional && !sections.some(section => section.id === "spend") && <SpendSection key={attempt} />}
      {catalog && <section className={styles.section} aria-label="Your .env file">
        <p>Your .env file: <code>{catalog.env_path}</code></p>
        <button type="button" onClick={copyEnvPath} aria-label="Copy your .env file path">Copy</button>
        {copyNote && <span role="status">{copyNote}</span>}
        <details>
          <summary>Use a .env file instead</summary>
          <p>Put these variables in your .env file above and keep it private. Reopen this screen to import new fields. Fields you already saved in the app take priority; edit those here.</p>
          {catalog.sections.map(section => <p key={section.id}>{section.title}: <code>{section.fields.flatMap(field => field.env).join(", ")}</code></p>)}
        </details>
      </section>}
      {!optional && <DataSettings />}
    </Container>
  );
}
