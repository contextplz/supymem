"use client";

import { AppShell } from "@/components/layout/AppShell";
import { AccountSettings } from "@/components/settings/AccountSettings";

export default function AccountPage() {
  return <AppShell title="Account"><AccountSettings /></AppShell>;
}
