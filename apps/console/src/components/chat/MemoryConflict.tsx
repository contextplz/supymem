"use client";

import { useRef, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Choice = { value: string; source: string; ref: string; timestamp: string; untrusted: boolean };
type Conflict = { id: string; venture_id: string; subject: string; question: string; choices: [Choice, Choice] };

export function memoryConflict(value: unknown): Conflict | null {
  if (!value || typeof value !== "object") return null;
  const card = value as Record<string, unknown>;
  if (typeof card.id !== "string" || !/^[a-f0-9]{64}$/.test(card.id)
    || typeof card.venture_id !== "string" || !card.venture_id.trim()
    || typeof card.subject !== "string" || !/^idea\.[a-z_]+$/.test(card.subject)
    || typeof card.question !== "string" || !Array.isArray(card.choices) || card.choices.length !== 2) return null;
  if (!card.choices.every(option => option && typeof option.value === "string" && option.value.trim()
    && option.value.length <= 16000 && typeof option.source === "string" && typeof option.ref === "string"
    && typeof option.timestamp === "string" && Number.isFinite(Date.parse(option.timestamp))
    && typeof option.untrusted === "boolean")) return null;
  return card as unknown as Conflict;
}

export function MemoryConflict({ card, messageId, disabled, onSubmit }: {
  card: Conflict; messageId: string; disabled?: boolean; onSubmit: (text: string) => Promise<unknown>;
}) {
  const [selected, setSelected] = useState<number | null>(null);
  const [state, setState] = useState<"ready" | "saving" | "saved">("ready");
  const [error, setError] = useState("");
  const pending = useRef(false);
  const blocked = disabled || state !== "ready";
  return <form aria-label="Resolve conflicting saved values" onSubmit={async event => {
    event.preventDefault();
    if (blocked || pending.current || selected === null) return;
    pending.current = true; setState("saving"); setError("");
    const value = selected === 2 ? `Both are true:\n${card.choices.map(option => option.value).join("\n")}` : card.choices[selected].value;
    try {
      await onSubmit(`Memory correction ${messageId}: ${selected}\n${value}`);
      setState("saved");
    } catch {
      setState("ready"); setError("Your choice was not saved. Try again.");
    } finally { pending.current = false; }
  }}>
    <fieldset disabled={blocked}>
      <legend>Choose the value we should use</legend>
      {card.choices.map((option, index) => <label key={`${option.ref}:${index}`} style={{ display: "block", marginBottom: 12 }}>
        <input type="radio" name={`memory-conflict-${messageId}`} checked={selected === index} onChange={() => setSelected(index)} />
        {option.value} <span className="muted">({option.source}, <time dateTime={option.timestamp}>{new Date(option.timestamp).toLocaleDateString("en-US", { timeZone: "UTC" })}</time>)</span>
      </label>)}
      <label><input type="radio" name={`memory-conflict-${messageId}`} checked={selected === 2} onChange={() => setSelected(2)} />Both are true</label>
    </fieldset>
    <button className="primary sm" type="submit" data-usage-control="chat_confirm_action" disabled={blocked || selected === null}>
      {state === "saving" ? "Saving…" : state === "saved" ? "Choice sent" : "Save your choice"}
    </button>
    <ErrorNotice error={error} source="memory-conflict" />
  </form>;
}
