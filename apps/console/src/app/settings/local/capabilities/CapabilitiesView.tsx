"use client";

import { useEffect, useRef, useState } from "react";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { api } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { capabilityView, SECTION_TITLES, SETTINGS, STATE_TITLES, type Capability } from "./capabilities";
import styles from "../local.module.css";

const PATH = "/api/local/capabilities";
const LOAD_FAILURE = "Your features could not be loaded. Open Settings > Keys and connections > Your data folder and backup, check your folder permissions and restore your settings and encryption key from your backup if needed. Restart your local app, then choose Try again.";
const SAVE_FAILURE = "Your feature switch was not saved. Open Settings > Keys and connections > Your data folder and backup, check your folder permissions and restore your settings and encryption key from your backup if needed, then try your switch again.";
const UPDATE_REQUIRED = "Update your local app, then open Settings > Capabilities and choose Try again.";

export function Capabilities() {
  const [items, setItems] = useState<Capability[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const active = useRef(true);
  const saving = useRef(false);

  async function load() {
    setLoading(true);
    setNote("");
    try {
      const result = await api(PATH);
      if (!active.current) return;
      if ([404, 405, 501].includes(result.status || 0)) {
        setNote(UPDATE_REQUIRED);
        return;
      }
      const data = result.data as { capabilities?: unknown[] } | undefined;
      const views = Array.isArray(data?.capabilities) ? data.capabilities.map(capabilityView) : [];
      if (!result.ok || views.length < Object.keys(SETTINGS).length || views.some(item => !item)
        || Object.keys(SETTINGS).some(name => !views.some(item => item?.name === name))
        || new Set(views.map(item => item?.name)).size !== views.length) {
        setNote(LOAD_FAILURE);
        return;
      }
      setItems((views as Capability[]).filter(item => item.state !== "not_in_this_version"));
    } catch {
      if (active.current) setNote(LOAD_FAILURE);
    } finally {
      if (active.current) setLoading(false);
    }
  }

  useEffect(() => {
    active.current = true;
    void load();
    return () => { active.current = false; };
  }, []);

  async function toggle(item: Capability, on: boolean) {
    if (saving.current) return;
    saving.current = true;
    setBusy(item.name);
    setNote("");
    try {
      const result = await api(`${PATH}/${item.name}`, { method: "PUT", body: JSON.stringify({ on }) });
      if (!active.current) return;
      const next = result.ok ? capabilityView(result.data) : null;
      if (!next || next.name !== item.name) {
        setNote([404, 405, 501].includes(result.status || 0) ? UPDATE_REQUIRED : SAVE_FAILURE);
        return;
      }
      setItems(current => current.map(value => value.name === next.name ? next : value));
    } catch {
      if (active.current) setNote(SAVE_FAILURE);
    } finally {
      saving.current = false;
      if (active.current) setBusy("");
    }
  }

  return (
    <main className={styles.page} tabIndex={-1} aria-labelledby="local-capabilities-title">
      <a href="/settings/local">Back to your keys and connections</a>
      <h1 id="local-capabilities-title">Capabilities</h1>
      <p>Choose which features you want to use. Turning on background work lets your requested work continue on its saved schedule. Other work still waits until you ask, and your email safety limits stay in place.</p>
      <p>Recovery and cleanup of expired inbox notices keep running when these switches are off. Your project data is not deleted.</p>
      {loading && <p role="status">Loading your features...</p>}
      {note && <ErrorNotice error={note} source="settings.local.capabilities" />}
      {!loading && note && <button type="button" disabled={Boolean(busy)} onClick={load}>Try again</button>}
      {items.map(item => {
        const status = STATE_TITLES[item.state];
        return (
        <section key={item.name} className={styles.section} aria-labelledby={`${item.name}-title`}>
          <h2 id={`${item.name}-title`}>{item.title}</h2>
          <p><span aria-label={`Status: ${status}`} style={{ display: "inline-block", border: "1px solid currentColor", borderRadius: 4, padding: "2px 8px" }}>{status}</span></p>
          <label className={styles.switch}>
            <input className={styles.toggle} type="checkbox" role="switch" checked={item.state !== "off" && item.state !== "not_in_this_version"}
              disabled={Boolean(busy) || loading || item.state === "not_in_this_version"}
              aria-label={`Enable ${item.title}`} aria-describedby={`${item.name}-next-step`} onChange={event => void toggle(item, event.target.checked)} />
            {item.state === "ready" || item.state === "needs_setup" ? "On" : "Off"}
          </label>
          {busy === item.name && <p role="status">Saving your switch...</p>}
          {item.missing.length > 0 && <p>What you need: {item.missing.join(", ")}</p>}
          <p id={`${item.name}-next-step`}>{item.next_step}</p>
          {Boolean(item.background_jobs?.length) && <ul aria-label="Your scheduled work">
            {item.background_jobs?.map(job => <li key={job.description}>{job.schedule}: {job.description}</li>)}
          </ul>}
          {SETTINGS[item.name].map(section => <p key={section}><a href={`/settings/local#${section}-title`}>{SECTION_TITLES[section]}</a></p>)}
        </section>
        );
      })}
    </main>
  );
}

export function LocalCapabilities() {
  return useLocalMode() ? <Capabilities /> : null;
}
