"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { StartupReview } from "@/components/chat/StartupReview";

export default function VentureContextPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  return <AppShell title="Your next step" ventureId={ventureId}>
    <StartupReview key={ventureId} ventureId={ventureId} />
  </AppShell>;
}
