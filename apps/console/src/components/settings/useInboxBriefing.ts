"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import type { InboxBriefingAction, InboxBriefingSettings, InboxBriefingUiConfig } from "@/lib/api-types";

export function validBriefingSettings(value: unknown): value is InboxBriefingSettings {
  if (!value || typeof value !== "object") return false;
  const settings = value as InboxBriefingSettings;
  const time = (entry: unknown) => typeof entry === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(entry);
  return Array.isArray(settings.windows) && settings.windows.length > 0 && settings.windows.length <= 2 && settings.windows.every(time)
    && new Set(settings.windows).size === settings.windows.length && time(settings.quiet_hours?.start)
    && time(settings.quiet_hours?.end) && settings.email_copy === false;
}

export function useInboxBriefing(ventureId?: string, workspaceId?: string) {
  const [saved, setSaved] = useState<{ subject: string; epoch: number; config: InboxBriefingUiConfig } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const writing = useRef(false);
  const active = useRef({ ventureId, workspaceId });
  active.current = { ventureId, workspaceId };
  const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const config = saved?.subject === subject && saved.epoch === epoch ? saved.config : null;

  useEffect(() => {
    let live = true;
    const refresh = async () => {
      const ticket = ++generation.current;
      const owner = verifiedSessionSubject(), ownerEpoch = verifiedSessionEpoch();
      setSaved(null); setError(""); setBusy(false); writing.current = false;
      if (!owner) return;
      try {
        const result = await api("/api/ui-config");
        if (!live || ticket !== generation.current || owner !== verifiedSessionSubject() || ownerEpoch !== verifiedSessionEpoch()) return;
        const data = result.data as InboxBriefingUiConfig | null;
        if (result.ok && data?.inbox_briefing_v1 === true) setSaved({ subject: owner, epoch: ownerEpoch, config: data });
      } catch {}
    };
    const wake = () => { void refresh(); };
    wake();
    window.addEventListener("supymem-session-changed", wake);
    return () => { live = false; generation.current += 1; window.removeEventListener("supymem-session-changed", wake); };
  }, [subject, epoch, ventureId, workspaceId]);

  async function save(command: InboxBriefingAction) {
    if (!ventureId || !subject || !config || writing.current) return false;
    const ticket = generation.current;
    const current = () => ticket === generation.current && subject === verifiedSessionSubject()
      && epoch === verifiedSessionEpoch() && active.current.ventureId === ventureId && active.current.workspaceId === workspaceId;
    if (!current()) return false;
    if (command.action === "inbox.briefing.settings" && !validBriefingSettings(command.payload)) {
      setError("Choose distinct briefing times and valid quiet hours."); return false;
    }
    if (command.action === "inbox.briefing.consent") {
      const mailboxId = command.payload.mailbox_id;
      if (!Array.isArray(config.inbox_briefing?.mailboxes) || !config.inbox_briefing.mailboxes.some(mailbox => mailbox?.mailbox_id === mailboxId)) return false;
    }
    writing.current = true; setBusy(true); setError("");
    try {
      const nonce = crypto.randomUUID();
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat/confirm`, {
        method: "POST", headers: { "Idempotency-Key": nonce, ...(workspaceId ? { "X-Workspace-Id": workspaceId } : {}) },
        body: JSON.stringify({ ...command, idempotency_key: nonce }),
      });
      if (!current()) return false;
      if (!result.ok) throw new Error("Your inbox preference could not be saved. Try again.");
      setSaved(before => {
        if (!before?.config.inbox_briefing) return before;
        const briefing = before.config.inbox_briefing;
        if (command.action === "inbox.briefing.settings") return { ...before, config: { ...before.config, inbox_briefing: { ...briefing, settings: command.payload } } };
        if (command.action !== "inbox.briefing.consent") return before;
        const { mailbox_id, consent } = command.payload;
        return { ...before, config: { ...before.config, inbox_briefing: { ...briefing, mailboxes: briefing.mailboxes.map(mailbox =>
          mailbox?.mailbox_id === mailbox_id ? { ...mailbox, consent } : mailbox) } } };
      });
      return true;
    } catch {
      if (current()) setError("Your inbox preference could not be saved. Try again.");
      return false;
    } finally {
      if (current()) { writing.current = false; setBusy(false); }
    }
  }

  return { enabled: config?.inbox_briefing_v1 === true, briefing: config?.inbox_briefing, busy, error, save };
}
