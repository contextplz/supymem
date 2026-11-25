"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/client";

type PresenceItem = {
  person_id: string;
  display_name: string;
  surface?: string | null;
  last_seen_at: string;
};

export function PresenceStrip({
  workspaceId,
  surface = "console",
}: {
  workspaceId: string;
  surface?: string;
}) {
  const [items, setItems] = useState<PresenceItem[]>([]);

  const beat = useCallback(async () => {
    if (!workspaceId) return;
    const r = await api(`/api/workspaces/${workspaceId}/presence`, {
      method: "POST",
      body: JSON.stringify({ surface }),
    });
    if (r.ok && r.data && typeof r.data === "object") {
      const next = (r.data as { items?: PresenceItem[] }).items;
      if (Array.isArray(next)) setItems(next);
    }
  }, [workspaceId, surface]);

  useEffect(() => {
    void beat();
    const id = window.setInterval(() => void beat(), 30_000);
    return () => window.clearInterval(id);
  }, [beat]);

  if (!workspaceId) return null;
  if (!items.length) {
    return <p className="muted">No one else is looking at this venture right now.</p>;
  }
  return (
    <p className="muted" aria-live="polite">
      Live now: {items.map((p) => p.display_name).join(", ")}
    </p>
  );
}
