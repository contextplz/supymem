"use client";

import { useEffect, useState } from "react";
import Link from "@/components/chat/ChatLink";
import { api } from "@/lib/client";
import { useFeatureVisibility } from "@/components/layout/LocalModeBoundary";

type MacTask = { id: string; title: string; state: string; review_path: string };

const STATE_LABEL: Record<string, string> = {
  pending: "Waiting for your review",
  approved: "Approved, ready to start",
  claimed: "Running",
  waiting_for_device: "Waiting for your Mac, it resumes when your Mac is awake",
  stopped: "Stopped, review result",
  cancelled: "Cancelled",
  expired: "Expired",
};

/** This venture's recent Mac tasks, newest first; each opens its own review page. */
export function MacTaskList({ ventureId }: { ventureId: string }) {
  const visible = useFeatureVisibility()("settings", "mac");
  const [tasks, setTasks] = useState<MacTask[] | null>(null);
  useEffect(() => {
    if (!visible) return;
    let live = true;
    void api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders`).then((r) => {
      if (live) setTasks(r.ok ? ((r.data as { orders?: MacTask[] })?.orders ?? []) : []);
    });
    return () => { live = false; };
  }, [ventureId, visible]);
  if (!visible || !tasks?.length) return null;
  return (
    <div className="panel-card" aria-label="Your Mac tasks">
      <h3>Your Mac tasks</h3>
      <ul>
        {tasks.map((t) => (
          <li key={t.id}>
            <Link href={t.review_path}>{t.title}</Link> <span className="muted">{STATE_LABEL[t.state] || t.state}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
