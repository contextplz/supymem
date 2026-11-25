"use client";

import { AppShell } from "@/components/layout/AppShell";
import { CommunityLab } from "@/components/community/CommunityLab";

export default function CommunityPage() {
  return (
    <AppShell
      title="Community"
      subtitle="Decide whether a community fits, connect Discord when useful, and measure member progress."
    >
      <CommunityLab />
    </AppShell>
  );
}
