"use client";

import { useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const TEXT_CAP = 6000;

/** "Submit my result": what the founder did themselves (text, optional https link) goes back to Charlie. */
export function RecoveryResultForm({ label, payload, onSubmit }: {
  label: string;
  payload?: Record<string, unknown>;
  onSubmit: (payload: Record<string, unknown>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  if (!open) {
    return <button type="button" className="primary sm" data-usage-control="chat_followup_action" onClick={() => setOpen(true)}>{label}</button>;
  }
  const link = url.trim();
  const badLink = link !== "" && !/^https?:\/\/\S+$/i.test(link);
  const empty = text.trim() === "" && link === "";
  return (
    <form style={{ display: "grid", gap: 6, width: "100%" }} onSubmit={(e) => {
      e.preventDefault();
      if (empty || badLink) return;
      onSubmit({ ...payload, text: text.trim().slice(0, TEXT_CAP), ...(link ? { url: link } : {}) });
      setOpen(false); setText(""); setUrl("");
    }}>
      <textarea aria-label="Your result" rows={4} maxLength={TEXT_CAP} value={text} placeholder="Paste what you did or made"
        onChange={(e) => setText(e.target.value)} />
      <input aria-label="Link (optional)" type="url" value={url} placeholder="Link, https:// (optional)" onChange={(e) => setUrl(e.target.value)} />
      {badLink && <ErrorNotice as="span" error="Links must start with http:// or https://" source="recovery.result_form" intent="recovery_submit" />}
      <span style={{ display: "flex", gap: 6 }}>
        <button type="submit" className="primary sm" disabled={empty || badLink}>Save my result</button>
        <button type="button" className="ghost sm" onClick={() => setOpen(false)}>Cancel</button>
      </span>
    </form>
  );
}
