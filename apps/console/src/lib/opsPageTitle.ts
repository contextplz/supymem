/** Ops layout titles: Outreach is its own surface, not the Operations shell label. */
export function opsPageTitle(pathname: string): { title: string; subtitle: string } {
  if (pathname === "/ops/outreach" || pathname.startsWith("/ops/outreach/")) {
    return {
      title: "Outreach",
      subtitle: "Review prospects and prepare founder outreach",
    };
  }
  return {
    title: "Operations",
    subtitle: "Company tasks, agents and activity",
  };
}
