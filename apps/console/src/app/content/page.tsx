"use client";

import { Suspense } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { ContentStudio } from "@/components/content/ContentStudio";
import { LocalCompanyPage } from "@/components/connections/LocalCompanyPage";
import { isLocalConsole } from "@/lib/localMode";

export default function ContentPage() {
  if (isLocalConsole()) return <Suspense fallback={<p>Loading your content studio…</p>}>
    <LocalCompanyPage title="Content" subtitle="Prepare topics and drafts for your company with your model key.">
      <ContentStudio />
    </LocalCompanyPage>
  </Suspense>;
  return (
    <AppShell
      title="Content"
      subtitle="Prepare company content, review drafts and manage approved publishing"
    >
      <Suspense fallback={null}>
        <ContentStudio />
      </Suspense>
    </AppShell>
  );
}
