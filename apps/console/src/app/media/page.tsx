"use client";

import { AppShell } from "@/components/layout/AppShell";
import { MediaStudio } from "@/components/media/MediaStudio";

export default function MediaPage() {
  return (
    <AppShell
      title="Media"
      subtitle="Create and review images and videos for your company"
    >
      <MediaStudio />
    </AppShell>
  );
}
