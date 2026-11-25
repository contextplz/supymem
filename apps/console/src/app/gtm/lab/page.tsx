"use client";

import type { ReactNode } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { GtmChat } from "@/components/gtm/GtmChat";

function Shell({ children }: { children: ReactNode }) {
  return (
    <AppShell
      title="AI GTM lab"
      subtitle="Older lead-find chat. Harvested rows cannot be marked sent in Outreach."
    >
      {children}
    </AppShell>
  );
}

export default function AiGtmLabPage() {
  return (
    <Shell>
      <GtmChat />
    </Shell>
  );
}
