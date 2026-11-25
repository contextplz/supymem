"use client";

import type { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { OpsSidebar } from "@/components/ops/OpsSidebar";
import styles from "@/components/ops/ops.module.css";
import { opsPageTitle } from "@/lib/opsPageTitle";

export function OpsShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() || "/ops";
  const { title, subtitle } = opsPageTitle(pathname);
  return (
    <AppShell title={title} subtitle={subtitle}>
      <div className={styles.shell}>
        <OpsSidebar />
        <main className={styles.main}>{children}</main>
      </div>
    </AppShell>
  );
}
