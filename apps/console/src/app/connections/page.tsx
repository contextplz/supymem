"use client";

import { Suspense } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { ConnectionsHub } from "@/components/connections/ConnectionsHub";

export default function ConnectionsPage() {
  return (
    <AppShell
      title="Connections"
      subtitle="Connect the apps you use. You stay in control of what happens next."
    >
      <Suspense fallback={<p className="page-loading" role="status">Loading your connections...</p>}>
        <ConnectionsHub />
      </Suspense>
    </AppShell>
  );
}
