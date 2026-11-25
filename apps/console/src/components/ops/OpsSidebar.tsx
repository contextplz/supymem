"use client";

import Link from "@/components/chat/ChatLink";
import { usePathname } from "next/navigation";
import { cx } from "@/lib/cx";
import { operationsHref } from "@/lib/operationsScope";
import { useOperationsScope } from "./OpsWorkspaceGate";
import styles from "./ops.module.css";

const NAV_ITEMS: ReadonlyArray<{ href: string; label: string; exact?: boolean; hidden: boolean }> = [
  { href: "/ops", label: "Dashboard · Preview", exact: true, hidden: false },
  { href: "/ops/agents", label: "Agents · Needs review", hidden: true },
  { href: "/ops/tasks", label: "Tasks · Needs review", hidden: true },
  { href: "/ops/social", label: "Social · Needs review", hidden: true },
  { href: "/ops/outreach", label: "Outreach · Needs review", hidden: true },
  { href: "/ops/ads", label: "Ads · Needs review", hidden: true },
  { href: "/ops/finance", label: "Finance · Needs review", hidden: true },
  { href: "/ops/memory", label: "Memory · Needs review", hidden: true },
  { href: "/ops/settings", label: "Settings · Needs review", hidden: true },
];

export function OpsSidebar() {
  const pathname = usePathname();
  const scope = useOperationsScope();

  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <p className={styles.brandTitle}>
          Supymem
        </p>
        <p className={styles.brandSub}>Company operations</p>
      </div>
      <nav className={styles.nav}>
        {NAV_ITEMS.map(({ href, label, exact, hidden }) => {
          const active = exact
            ? pathname === href
            : pathname === href || pathname.startsWith(`${href}/`);
          if (hidden && !active) return null;
          return (
            <Link
              key={href}
              href={operationsHref(href, scope)}
              className={cx(styles.navLink, active && styles.navLinkActive)}
            >
              {label}
            </Link>
          );
        })}
      </nav>
      <Link href="/" className={styles.backLink}>
        ← Ventures
      </Link>
    </aside>
  );
}
