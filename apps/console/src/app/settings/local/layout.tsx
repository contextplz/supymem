"use client";

import type { ReactNode } from "react";
import { AppShell } from "@/components/layout/AppShell";

export default function LocalSettingsLayout({ children }: { children: ReactNode }) {
  return <AppShell title="Settings">{children}</AppShell>;
}
