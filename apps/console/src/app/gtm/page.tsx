"use client";

import { AppShell } from "@/components/layout/AppShell";
import { GtmGod } from "@/components/gtm/GtmGod";

export default function GtmGodPage() {
  return (
    <AppShell
      title="Go to market"
      subtitle="Find people who fit, review the message, and learn from their replies."
    >
      <GtmGod />
    </AppShell>
  );
}
