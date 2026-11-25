"use client";

import type { ReactNode } from "react";
import { useParams } from "next/navigation";
import { DesktopComputerUse } from "@/components/desktop/DesktopComputerUse";
import { PersistentAppShell } from "@/components/layout/AppShell";
import { ActivityProvider } from "@/components/live/ActivityProvider";

export default function VentureLayout({ children }: { children: ReactNode }) {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  return (
    <ActivityProvider ventureId={ventureId}>
      <DesktopComputerUse ventureId={ventureId} />
      <PersistentAppShell ventureId={ventureId}>
        {children}
      </PersistentAppShell>
    </ActivityProvider>
  );
}
