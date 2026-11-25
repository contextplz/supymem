"use client";

import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { LocalSettings } from "./LocalSettings";

export default function LocalSettingsPage() {
  const local = useLocalMode();

  return local ? <LocalSettings /> : null;
}
