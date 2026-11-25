"use client";

import Link from "@/components/chat/ChatLink";
import { NewVentureLink } from "@/components/chat/NewVentureLink";
import { useState } from "react";
import { cx } from "@/lib/cx";
import { isBetaHiddenPath } from "@/lib/betaRoutes";
import { NAV_ICONS, type NavIconId } from "./NavIcons";
import styles from "./SideNav.module.css";
import { SuggestionsBadge } from "./SuggestionsBadge";
import { GuideNavLink } from "@/components/guide/GuideNavLink";
import { companyOverviewHref, operationsHref, ventureGrowthHrefs, type OperationsScope } from "@/lib/operationsScope";
import { useFeatureVisibility, useLocalMode } from "./LocalModeBoundary";
import { isHostedOnlyPath } from "@/lib/localMode";

type NavItem = {
  href: string;
  label: string;
  description: string;
  icon: NavIconId;
  exact?: boolean;
  beta?: "review" | "hidden";
  badge?: "suggestions";
};

type NavGroup = {
  id: string;
  label: string;
  items: readonly NavItem[];
};

const GLOBAL_NAV: readonly NavItem[] = [
  { href: "/", label: "Ventures", description: "Your companies and products", icon: "ventures", exact: true },
  { href: "/new", label: "New venture", description: "Describe a new idea", icon: "newVenture", exact: true, beta: "review" },
  { href: "/lab", label: "Idea lab", description: "Compare openings before you commit", icon: "lab", beta: "hidden" },
  { href: "/gtm", label: "Find customers", description: "Who to sell to and drafts to reach them", icon: "market", beta: "review" },
  { href: "/content", label: "Content", description: "Create and review your publishing schedule", icon: "plan", beta: "hidden" },
  { href: "/connections", label: "Connections", description: "Services connected to your company", icon: "connections" },
  { href: "/marketing", label: "Marketing", description: "Campaigns and marketing activity", icon: "analytics", beta: "hidden" },
  { href: "/community", label: "Community", description: "Build and operate your customer community", icon: "team", beta: "hidden" },
  { href: "/media", label: "Media", description: "Product and campaign assets", icon: "website", beta: "hidden" },
  { href: "/ops", label: "Operations", description: "Company agents, tasks, and health", icon: "ops", beta: "hidden" },
  { href: "/ops/outreach", label: "Outreach", description: "Review prospects and prepare founder outreach", icon: "market", beta: "hidden" },
  { href: "/ads", label: "Ads", description: "Prepare campaigns for approval", icon: "analytics", beta: "hidden" },
];

const LOCAL_SETTINGS: readonly NavItem[] = [
  { href: "/settings/local", label: "Settings", description: "Your services, keys, and local data", icon: "settings", exact: true },
  { href: "/settings/local/capabilities", label: "Capabilities", description: "Choose which features you want to use", icon: "settings" },
];

function ventureNav(ventureId: string): readonly NavGroup[] {
  const base = `/v/${ventureId}`;
  const growth = ventureGrowthHrefs(ventureId);
  return [
    {
      id: "run",
      label: "Run",
      items: [
        { href: base, label: "Overview", description: "Where this venture stands and what to do next", icon: "cockpit", exact: true },
        { href: `${base}/idea`, label: "Idea", description: "Clarify and confirm the idea", icon: "idea" },
        { href: `${base}/now`, label: "Right now", description: "What Supymem is doing right now", icon: "now" },
        { href: `${base}/suggestions`, label: "Suggestions", description: "Tasks Supymem can take over for you", icon: "now", badge: "suggestions" },
        { href: `${base}/tasks`, label: "Work", description: "What the team is claiming and shipping", icon: "work" },
        { href: `${base}/org`, label: "Team", description: "Who is hired, working, or watching", icon: "team", beta: "hidden" },
        { href: `${base}/site`, label: "Website", description: "The public site for this venture", icon: "website" },
      ],
    },
    {
      id: "evidence",
      label: "Learn",
      items: [
        { href: `${base}/research`, label: "Research", description: "Research your market and review saved evidence", icon: "research", beta: "review" },
        { href: `${base}/experiments`, label: "Experiments", description: "Tests you ran, what happened, what you learned", icon: "experiments" },
        { href: `${base}/market-map`, label: "Market map", description: "Your market beliefs, evidence and experiment timeline", icon: "market" },
        { href: `${base}/market`, label: "Market", description: "Demand, competitors, and signals", icon: "market", beta: "hidden" },
        { href: `${base}/feedback`, label: "Feedback", description: "What users and leads are saying", icon: "feedback", beta: "hidden" },
        { href: `${base}/charter`, label: "Plan", description: "Your audience, offer, and next steps", icon: "plan", beta: "review" },
        { href: growth.execution, label: "Execution", description: "Your milestones, tasks, and progress", icon: "execution", beta: "review" },
        { href: `${base}/builds`, label: "Builds", description: "Saved build history; product build is not accepted", icon: "builds", beta: "review" },
        { href: `${base}/analytics`, label: "Analytics", description: "Reported events; product funnel is unverified", icon: "analytics", beta: "review" },
        { href: `${base}/import`, label: "Import", description: "Bring an existing repo in", icon: "import", beta: "hidden" },
      ],
    },
    {
      id: "grow",
      label: "Growth",
      items: [
        { href: growth.gtm, label: "Find customers", description: "Saved diagnosis and waitlist; acquisition needs review", icon: "market", beta: "review" },
        { href: `${base}/leads`, label: "Leads", description: "All your saved leads and ways to reach them", icon: "people" },
        { href: `${base}/linkedin`, label: "LinkedIn", description: "Public profiles and notes you send yourself", icon: "people" },
        { href: growth.content, label: "Content", description: "Draft posts for your idea and copy them", icon: "plan", beta: "review" },
        { href: growth.community, label: "Community", description: "Build relationships with your users", icon: "team", beta: "hidden" },
        { href: growth.connections, label: "Connections", description: "Services connected to this company", icon: "connections" },
      ],
    },
    {
      id: "manage",
      label: "Manage",
      items: [
        { href: `${base}/evolution`, label: "Evolution", description: "What the venture wants to change next", icon: "evolution", beta: "hidden" },
        { href: `${base}/people`, label: "People", description: "Who can operate this venture", icon: "people", beta: "hidden" },
        { href: `${base}/connections`, label: "Integrations", description: "GitHub, Linear, and the trust receipt", icon: "connections", beta: "hidden" },
        { href: `${base}/settings`, label: "Settings", description: "Keys, local tools, and task receipts", icon: "settings" },
      ],
    },
  ];
}

function isActive(pathname: string, item: NavItem): boolean {
  const path = item.href.split("?", 1)[0];
  if (item.exact) return pathname === path;
  return pathname === path || pathname.startsWith(`${path}/`);
}

function NavLink({ item, pathname, ventureId }: { item: NavItem; pathname: string; ventureId?: string }) {
  const local = useLocalMode();
  const active = isActive(pathname, item);
  const Component = item.href.split("?")[0] === "/new" ? NewVentureLink : Link;
  return (
    <Component
      href={item.href}
      data-chat-route-native={item.href.split("?")[0] === "/connections" ? "true" : undefined}
      className={cx(styles.link, active && styles.linkActive)}
      title={`${item.label}: ${item.description}${!local && item.beta ? " (Beta)" : ""}`}
      aria-label={item.label}
      aria-current={active ? "page" : undefined}
    >
      <span className={styles.glyph} aria-hidden>
        {NAV_ICONS[item.icon]({})}
      </span>
      <span className={styles.label}>{item.label}</span>
      {item.badge === "suggestions" && <SuggestionsBadge ventureId={ventureId} />}
    </Component>
  );
}

function NavSection({ group, pathname, ventureId }: { group: NavGroup; pathname: string; ventureId?: string }) {
  const local = useLocalMode();
  const visible = useFeatureVisibility();
  const active = group.items.some((item) => isActive(pathname, item));
  const [expanded, setExpanded] = useState(true);

  return (
    <nav className={styles.group} aria-label={group.label}>
      <button
        type="button"
        className={cx(styles.groupHead, active && styles.groupHeadActive)}
        aria-expanded={expanded}
        aria-controls={`nav-${group.id}`}
        onClick={() => setExpanded((value) => !value)}
      >
        <span>{group.label}</span>
        <span className={cx(styles.chevron, expanded && styles.chevronExpanded)} aria-hidden="true" />
      </button>
      <div id={`nav-${group.id}`} className={styles.groupItems} hidden={!expanded}>
        {group.items.filter((item) => local ? !isHostedOnlyPath(item.href) && visible("paths", item.href) : item.beta !== "hidden" && !isBetaHiddenPath(item.href)).map((item) => (
          <NavLink key={item.href} item={item} pathname={pathname} ventureId={ventureId} />
        ))}
      </div>
    </nav>
  );
}

export function SideNav({
  pathname,
  ventureId,
  operationsScope,
}: {
  pathname: string;
  ventureId?: string;
  operationsScope?: OperationsScope | null;
}) {
  const local = useLocalMode();
  const visible = useFeatureVisibility();
  if (!ventureId) {
    const items: NavItem[] = [
      ...(operationsScope?.ventureId && pathname !== "/" ? [{
        href: companyOverviewHref(),
        label: "Company overview",
        description: "All ventures and company-wide surfaces",
        icon: "cockpit" as const,
        exact: true,
      }] : []),
      ...GLOBAL_NAV.map((item) => ({ ...item, href: operationsHref(item.href, operationsScope || null) })),
      ...(local ? LOCAL_SETTINGS : []),
    ];
    return (
      <div className={styles.stack}>
        <nav className={styles.groupItems} aria-label="Ventures">
          {items.filter((item) => local ? !isHostedOnlyPath(item.href) && (item.href.startsWith("/settings/local") || visible("paths", item.href)) : item.beta !== "hidden" && !isBetaHiddenPath(item.href)).map((item) => (
            <NavLink key={item.href} item={item} pathname={pathname} />
          ))}
        </nav>
        <GuideNavLink pathname={pathname} />
      </div>
    );
  }

  return (
    <div className={styles.stack}>
      {ventureNav(ventureId).map((group) => (
        <NavSection key={group.id} group={group} pathname={pathname} ventureId={ventureId} />
      ))}
      <GuideNavLink pathname={pathname} ventureId={ventureId} />
      {local && <nav aria-label="Your settings">{LOCAL_SETTINGS.map(item => <NavLink key={item.href} item={item} pathname={pathname} />)}</nav>}
    </div>
  );
}
