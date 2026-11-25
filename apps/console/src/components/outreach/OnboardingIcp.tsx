"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";
import { useState } from "react";
import { KnownAnswer, type KnownCheck } from "@/components/chat/KnownAnswer";

import styles from "./Outreach.module.css";
import { ActionFeedback, useOutreachAction } from "./useOutreachAction";

type Flags = {
  icp: boolean;
  excludes: boolean;
  sender_geo: boolean;
  send_path: boolean;
  voice: boolean;
  evidence: boolean;
  ready: boolean;
  outbound_mode?: string;
  sender_geo_code?: string;
  sender_postal_address?: string;
  segment?: { who?: string };
  voice_config?: { write_like?: string };
  known_checks?: Partial<Record<string, KnownCheck>>;
};

export function OnboardingIcp({
  flags,
  suggestedWho,
  onSave,
}: {
  flags: Flags | null;
  suggestedWho?: string;
  onSave: (body: Record<string, unknown>) => Promise<void>;
}) {
  const { pending, error, run } = useOutreachAction();
  const [editing, setEditing] = useState<string | null>(null);
  if (!flags) return <section className={styles.card}><p role="status">Loading your saved outreach details…</p></section>;
  const email = flags.outbound_mode === "gmail_composio" || flags.outbound_mode === "email";
  const field = editing || (!flags.icp ? "who" : !flags.excludes ? "excludes" : !flags.sender_geo
    ? email && !flags.sender_postal_address ? "sender_postal_address" : "sender_geo"
    : !flags.send_path ? "outbound_mode" : !flags.voice ? "voice" : null);
  const check = field ? flags.known_checks?.[field === "who" ? "audience" : field] : undefined;
  const save = async (body: Record<string, unknown>) => { await onSave(body); setEditing(null); };
  return (
    <section className={styles.card}>
      <h2>Set up your outreach</h2>
      <ActionFeedback pending={pending} error={error} />
      <p className={styles.muted}>
        You only need to answer what is missing. Nothing is sent without your approval.
      </p>
      {(["audience", "voice", "excludes", "sender_geo", "sender_postal_address", "outbound_mode"] as const).map(name => {
        const saved = flags.known_checks?.[name];
        return saved?.status === "known" && editing !== (name === "audience" ? "who" : name)
          ? <KnownAnswer key={name} check={saved} field={name} disabled={pending} onSelect={() => {}}
              onEdit={() => setEditing(name === "audience" ? "who" : name)} /> : null;
      })}
      {field && check && check.status !== "missing" && check.status !== "known" && editing !== field
        ? <KnownAnswer check={check} field={field === "who" ? "audience" : field} disabled={pending}
            onSelect={value => void run(() => save({ [field]: field === "voice" ? { write_like: value }
              : field === "excludes" ? JSON.parse(value) : value }))}
            onEdit={() => setEditing(field)} /> : field ? <form
        key={field}
        className={styles.grid}
        onSubmit={(e) => {
          e.preventDefault();
          const fd = new FormData(e.currentTarget);
          const answer = String(fd.get(field) || "").trim();
          void run(() => save({ [field]: field === "excludes" ? answer.split(",").map(domain => domain.trim()).filter(Boolean)
            : field === "voice" ? { write_like: answer } : answer }));
        }}
      >
        {field === "who" && <label>
          <span className={styles.label}>Who do you want to reach first?</span>
          <textarea
            className={styles.textarea}
            name="who"
            required
            minLength={8}
            defaultValue={flags.segment?.who || suggestedWho}
            placeholder="The people who need your product"
          />
          {suggestedWho ? <span className={styles.muted}>You can edit this saved audience before continuing.</span> : null}
        </label>}
        {field === "excludes" && <label>
          <span className={styles.label}>Which website domains should you avoid? (Optional)</span>
          <input className={styles.input} name="excludes" placeholder="Separate domains with commas, or leave blank" />
        </label>}
        {field === "sender_geo" && <label>
          <span className={styles.label}>Which country are you sending from?</span>
          <input className={styles.input} name="sender_geo" required minLength={2} maxLength={2} pattern="[A-Za-z]{2}" defaultValue={flags.sender_geo_code || ""} placeholder="Two-letter country code" />
        </label>}
        {field === "sender_postal_address" && <label>
          <span className={styles.label}>What sender postal address should your emails include?</span>
          <input className={styles.input} name="sender_postal_address" required />
        </label>}
        {field === "outbound_mode" && <label>
          <span className={styles.label}>How do you want to send your messages?</span>
          <select className={styles.input} name="outbound_mode" defaultValue="human_clipboard"><option value="human_clipboard">Copy and send yourself</option><option value="gmail_composio">Send through your connected Gmail after approval</option></select>
        </label>}
        {field === "voice" && <label>
          <span className={styles.label}>How should your messages sound?</span>
          <textarea className={styles.textarea} name="voice" required defaultValue={flags.voice_config?.write_like} placeholder="Describe your tone or paste a message you wrote" />
        </label>}
        <div className={styles.row}>
          <button className={styles.btn} type="submit" disabled={pending}>
            Continue
          </button>
        </div>
      </form> : <p>{flags.evidence ? "Your outreach details are saved." : <>Add a public source or <ChatAnchor href="/connections">connect an account</ChatAnchor> so you can review real people, not invented names.</>}</p>}
    </section>
  );
}
