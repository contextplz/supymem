"use client";

import { useEffect, useState } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { VentureHub } from "@/components/ventures/VentureHub";
import { WaitlistLanding } from "@/components/marketing/WaitlistLanding";
import { isDesktopShell } from "@/lib/desktop";
import { isMarketingSurface } from "@/lib/hostMode";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";

export default function HomePage() {
  const local = useLocalMode();
  const [marketing, setMarketing] = useState(false);

  useEffect(() => {
    if (local) return;
    setMarketing(
      isMarketingSurface(
        window.location.host,
        window.location.pathname,
        window.location.search,
        isDesktopShell(),
      ),
    );
  }, [local]);

  if (!local && marketing) return <WaitlistLanding />;

  return (
    <AppShell title="Ventures" bare fill>
      <VentureHub local={local} />
    </AppShell>
  );
}
