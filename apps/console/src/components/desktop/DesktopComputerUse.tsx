"use client";

import { useEffect, useRef } from "react";
import { api } from "@/lib/client";
import { desktopInvoke, isDesktopShell } from "@/lib/desktop";
import { startComputerUse } from "@/lib/computerUseRunner";

type Job = { id: string; prompt?: string; status?: string };

/** Polls for build jobs and starts Computer Use. No founder buttons. */
export function DesktopComputerUse({ ventureId }: { ventureId: string }) {
  const busy = useRef(false);

  useEffect(() => {
    // Native Mac computer use is not in the beta; never auto-claim jobs.
    if (true as boolean) return;
    if (!ventureId || !isDesktopShell()) return;
    let cancelled = false;

    async function tick() {
      if (busy.current || cancelled) return;
      const listed = await api(`/api/ventures/${ventureId}/computer-use/jobs`);
      if (!listed.ok) return;
      const items = ((listed.data as { items?: Job[] })?.items || []).filter(
        (j) => j.status === "pending" && j.prompt,
      );
      const job = items[0];
      if (!job) return;
      const clis = await desktopInvoke<Array<{ name: string }>>("list_local_clis");
      if (!Array.isArray(clis) || !clis.some((c) => c.name === "hermes")) return;
      busy.current = true;
      try {
        const claimed = await api(`/api/ventures/${ventureId}/computer-use/jobs/${job.id}/claim`, {
          method: "POST",
          body: "{}",
        });
        if (!claimed.ok) return;
        const prompt = String((claimed.data as Job).prompt || job.prompt || "");
        try {
          const started = await startComputerUse(ventureId, prompt);
          await api(`/api/ventures/${ventureId}/computer-use/jobs/${job.id}/status`, {
            method: "POST",
            body: JSON.stringify({
              status: started.started ? "running" : "failed",
              detail: started.detail,
            }),
          });
        } catch (error) {
          await api(`/api/ventures/${ventureId}/computer-use/jobs/${job.id}/status`, {
            method: "POST",
            body: JSON.stringify({
              status: "failed",
              detail: error instanceof Error ? error.message : "Computer Use could not start.",
            }),
          });
        }
      } finally {
        busy.current = false;
      }
    }

    void tick();
    const id = window.setInterval(() => void tick(), 8000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [ventureId]);

  return null;
}
