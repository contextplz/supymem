"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { ExperimentsView } from "@/components/chat/ExperimentsView";

export default function MarketMapPage() {
  const ventureId = String(useParams().ventureId || "");
  return <AppShell title="Market map" ventureId={ventureId}>
    <div className="chat-scroll"><div className="chat-inner">
      <ExperimentsView key={ventureId} ventureId={ventureId} />
    </div></div>
  </AppShell>;
}
