"use client";

import { createContext, useEffect, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "./client";

export type FounderUI = "classic" | "home";
export const FounderUIContext = createContext<FounderUI>("classic");

export function useFounderUI(subject: string | null | undefined, enabled = true) {
  return useFounderUIState(subject, enabled).layout;
}

/** The layout plus whether the server answered: redirects must wait for the real setting, not the "home" default. */
export function useFounderUIState(subject: string | null | undefined, enabled = true): { layout: FounderUI; loaded: boolean } {
  const [config, setConfig] = useState<{ subject: string; epoch: number; layout: FounderUI } | null>(null);
  useEffect(() => {
    if (!enabled || !subject) { setConfig(null); return; }
    let live = true;
    let pending = false;
    const refresh = async () => {
      if (pending || document.hidden) return;
      pending = true;
      const epoch = verifiedSessionEpoch();
      try {
        const result = await api("/api/ui-config");
        const layout = (result.data as { founder_ui?: unknown } | null)?.founder_ui;
        if (live && verifiedSessionSubject() === subject && verifiedSessionEpoch() === epoch && result.ok && (layout === "classic" || layout === "home" || layout === "chat")) {
          setConfig({ subject, epoch, layout: layout === "chat" ? "home" : layout });
        }
      } catch {
      } finally { pending = false; }
    };
    const wake = () => { void refresh(); };
    void refresh();
    const timer = window.setInterval(wake, 5000);
    window.addEventListener("online", wake);
    window.addEventListener("supymem-session-changed", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      live = false;
      window.clearInterval(timer);
      window.removeEventListener("online", wake);
      window.removeEventListener("supymem-session-changed", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [subject, enabled]);
  const loaded = Boolean(enabled && config && config.subject === subject && config.epoch === verifiedSessionEpoch() && verifiedSessionSubject() === subject);
  return { layout: loaded && config ? config.layout : "home", loaded };
}
