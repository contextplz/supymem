"use client";

import { AppShell } from "@/components/layout/AppShell";
import { MarketingWorkspace } from "@/components/marketing/MarketingWorkspace";

export default function MarketingPage() {
  return (
    <AppShell
      title="Marketing"
      subtitle="Choose and run the next evidence-backed path to your first customer."
    >
      <MarketingWorkspace />
    </AppShell>
  );
}
