"use client";

import { AdsAutomation } from "@/components/ads/AdsAutomation";
import { AppShell } from "@/components/layout/AppShell";

export default function AdsPage() {
  return (
    <AppShell
      title="Ads automation"
      subtitle="Idea → campaign blueprint → approval → paused provider campaign"
    >
      <AdsAutomation />
    </AppShell>
  );
}
