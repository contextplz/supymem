import Link from "@/components/chat/ChatLink";
import { cx } from "@/lib/cx";
import { NAV_ICONS } from "@/components/layout/NavIcons";
import styles from "@/components/layout/SideNav.module.css";

/** Kept outside collapsible topic groups so help stays reachable. */
export function GuideNavLink({ pathname, ventureId }: { pathname: string; ventureId?: string }) {
  const href = ventureId ? `/v/${encodeURIComponent(ventureId)}/guide` : "/guide";
  const active = pathname === href;
  return <nav aria-label="Help">
    <Link href={href} className={cx(styles.link, active && styles.linkActive)} title="How to use Supymem" aria-label="How to use Supymem" aria-current={active ? "page" : undefined} data-usage-control="guide_nav">
      <span className={styles.glyph} aria-hidden="true">{NAV_ICONS.research({})}</span>
      <span className={styles.label}>How to use Supymem</span>
    </Link>
  </nav>;
}
