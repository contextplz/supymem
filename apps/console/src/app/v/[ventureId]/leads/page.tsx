"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { LeadsList } from "@/components/gtm/Leads";

export default function LeadsPage() {
  const ventureId = String(useParams().ventureId || "");
  return <AppShell title="Leads" ventureId={ventureId}><LeadsList key={ventureId} ventureId={ventureId} /></AppShell>;
}
