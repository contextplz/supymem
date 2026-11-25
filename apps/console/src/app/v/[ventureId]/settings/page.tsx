"use client";

import { useParams } from "next/navigation";
import { VentureSettings } from "@/components/settings/VentureSettings";

export default function SettingsPage() {
  const params = useParams();
  return <VentureSettings ventureId={String(params.ventureId || "")} />;
}
