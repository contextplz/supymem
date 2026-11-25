import { useEffect, useRef, useState } from "react";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "./client";
import { getConnectionsStatus, type ConnectionsHubCard } from "./opsApi";

export type HomeAgent = { id: string; name?: string; role?: string; kind?: string; title?: string; department?: string; status?: string; charge?: string; work?: { did?: string; doing?: string; live_state?: string | null } };
export type ArchiveItem = { id: string; folder: string; title: string; href: string; status?: string | null; created_at?: string | null; updated_at?: string | null; document?: boolean; kind?: string; agent_role?: string | null; agent_id?: string | null; integration?: string | null };
export type HomeData = { ventureId: string; subject?: string | null; epoch?: number; workspaceId?: string; name?: string; cardType?: string | null; cardColor?: string | null; cardArt?: string | null; email?: string; profileName?: string; revenue?: {day: string; amount: number}[]; createdAt?: string; appCount?: number; ideaConfirmed?: boolean; firstBuild?: boolean; agents: HomeAgent[] | null; cards: ConnectionsHubCard[] | null; archives: ArchiveItem[] | null; pins: string[] | null; openWork: number | null; waitlist: number | null; views: number | null; signups: number | null; days: { day: string; visits: number }[]; funnel?: { steps: string[]; counts: Record<string, number> }; errors: string[] };
const empty = (ventureId: string): HomeData => ({ ventureId, agents: null, cards: null, archives: null, pins: null, openWork: null, waitlist: null, views: null, signups: null, days: [], errors: [] });

export function departmentFor(agent: HomeAgent): string {
  if (agent.department?.trim()) return agent.department.trim();
  const role = `${agent.role || agent.kind || ""} ${agent.title || ""}`.toLowerCase();
  if (/research|analyst/.test(role)) return "Research";
  if (/pay|pric|financ|stripe|revenue/.test(role)) return "Revenue & Finance";
  if (/support|customer/.test(role)) return "Customers & Support";
  if (/growth|channel|copy|market|gtm/.test(role)) return "Marketing & GTM";
  if (/architect|build|review|qa|design|integrat|product|engineer/.test(role)) return "Product & Engineering";
  if (/maintain|ops/.test(role)) return "Operations";
  if (/orchestr|concie|compli|strateg|lead/.test(role)) return "Strategy";
  return "People & Agent Ops";
}

export function foundedDays(createdAt?: string, now = Date.now()): number | null {
  // Some API rows append "Z" to an already offset timestamp ("...+00:00Z").
  createdAt = createdAt?.replace(/([+-]\d{2}:\d{2})Z$/, "$1");
  const created = createdAt ? Date.parse(createdAt.includes("T") && !/(Z|[+-]\d{2}:\d{2})$/.test(createdAt) ? `${createdAt}Z` : createdAt) : NaN;
  return Number.isFinite(created) && created <= now ? Math.floor((now - created) / 86_400_000) : null;
}

export function liveToolkitCount(cards: ConnectionsHubCard[]): number {
  return cards.filter(card => card.connected === true && (card.ready ?? card.ok) === true).length;
}

export function activeAgentCount(agents: HomeAgent[]): number {
  return agents.filter(agent => agent.work?.live_state === "working").length;
}

export const COMPANY_DEPARTMENTS = [
  { name: "Product & Engineering", tone: "product" },
  { name: "Marketing & GTM", tone: "marketing" },
  { name: "Research", tone: "research" },
  { name: "Customers & Support", tone: "customers" },
  { name: "Strategy", tone: "strategy" },
  { name: "Operations", tone: "ops" },
  { name: "Revenue & Finance", tone: "revenue" },
  { name: "People & Agent Ops", tone: "people" },
] as const;

export function archiveDepartment(item: ArchiveItem, agents: HomeAgent[]): string {
  const candidates = agents.filter(agent => item.agent_id ? agent.id === item.agent_id : !!item.agent_role && (agent.role === item.agent_role || agent.id === item.agent_role));
  const agent = candidates.length === 1 ? candidates[0] : null;
  return agent ? departmentFor(agent) : item.kind === "gtm_contacts" ? "Customers & Support" : ({ research: "Research", website: "Product & Engineering", outreach: "Marketing & GTM", tasks: "Operations", files: "Strategy", revenue: "Revenue & Finance", knowledge: "Strategy" }[item.folder] || "Operations");
}

export function companyDepartments(agents: HomeAgent[]) {
  const extra = [...new Set(agents.map(departmentFor))].filter(name => !COMPANY_DEPARTMENTS.some(dept => dept.name === name));
  return [...COMPANY_DEPARTMENTS, ...extra.map(name => ({ name, tone: "people" }))];
}

export function useCompanyHomeData(ventureId?: string, loadDetails = true) {
  const [data, setData] = useState<HomeData>(empty(ventureId || ""));
  const [attempt, setAttempt] = useState(0);
  const live = useRef(false);
  const pinFlight = useRef(false);
  const pinsReady = useRef(false);
  const [pinBusy, setPinBusy] = useState(false);
  const [pinNote, setPinNote] = useState("");
  const owner = useRef({ ventureId, epoch: verifiedSessionEpoch(), subject: verifiedSessionSubject() });
  if (owner.current.ventureId !== ventureId || owner.current.epoch !== verifiedSessionEpoch() || owner.current.subject !== verifiedSessionSubject()) owner.current = { ventureId, epoch: verifiedSessionEpoch(), subject: verifiedSessionSubject() };
  const current = (candidate: typeof owner.current) => live.current && candidate === owner.current && candidate.epoch === verifiedSessionEpoch() && candidate.subject === verifiedSessionSubject();
  const retry = () => { owner.current = { ...owner.current }; setAttempt(value => value + 1); };
  async function pin(department: string, pinned: boolean) {
    const candidate = owner.current;
    if (!ventureId || !current(candidate) || pinFlight.current || !pinsReady.current || data.pins === null) return;
    pinFlight.current = true; setPinBusy(true); setPinNote("");
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/ui-pins`, { method: "POST", body: JSON.stringify({ department, pinned }) });
      if (!current(candidate)) return;
      const saved = result.data as { venture_id?: string; pins?: unknown } | null;
      if (!result.ok || saved?.venture_id !== ventureId || !Array.isArray(saved.pins) || !saved.pins.every(item => typeof item === "string")) throw new Error();
      setData(value => current(candidate) ? ({ ...value, pins: saved.pins as string[] }) : value);
      try { window.localStorage.setItem(`supymem.sidebar-pins.${candidate.subject}.${ventureId}`, JSON.stringify(saved.pins)); } catch {}
    } catch { if (current(candidate)) setPinNote("Pin was not confirmed. Reload quick access and try again, or use the page list."); }
    finally { pinFlight.current = false; if (current(candidate)) setPinBusy(false); }
  }
  useEffect(() => {
    live.current = true;
    pinsReady.current = false;
    let cancelled = false;
    const candidate = owner.current;
    setData(empty(ventureId || ""));
    setPinBusy(false); setPinNote("");
    const active = () => !cancelled && current(candidate);
    const publish = (patch: Partial<HomeData>) => { if (active()) setData(value => active() ? ({ ...value, ...patch, subject: candidate.subject, epoch: candidate.epoch }) : value); };
    const fail = (section: string) => { if (active()) setData(value => active() ? ({ ...value, subject: candidate.subject, epoch: candidate.epoch, errors: [...value.errors, section] }) : value); };
    const read = async (path: string) => { const result = await api(path); if (!result.ok) throw new Error("unavailable"); return result.data as Record<string, any>; };
    if (ventureId) {
      const base = `/api/ventures/${encodeURIComponent(ventureId)}`;
      void (async () => {
        try {
          const session = await ensureSession();
          if (cancelled || candidate !== owner.current || !session.ok || !(session.data as { authenticated?: boolean })?.authenticated) return;
          if (candidate.subject === null) { candidate.subject = verifiedSessionSubject(); candidate.epoch = verifiedSessionEpoch(); }
          if (!active()) return;
          publish({ email: (session.data as { email?: string })?.email });
          void read("/api/me/greeting").then(result => { if (typeof result.first_name === "string" && result.first_name.trim()) publish({profileName: result.first_name.trim()}); }).catch(() => undefined);
          const venture = await read(base);
          if (!active()) return;
          const pinsKey = `supymem.sidebar-pins.${candidate.subject}.${ventureId}`;
          try {
            const cached = JSON.parse(window.localStorage.getItem(pinsKey) || "null");
            if (Array.isArray(cached) && cached.length <= 32 && cached.every(pin => typeof pin === "string" && pin.length > 0 && pin.length <= 120 && !/[\x00-\x1f\x7f]/.test(pin))) publish({pins: cached});
          } catch {}
          publish({ workspaceId: venture.workspace_id, name: venture.name, createdAt: venture.created_at, cardType: venture.card_type, cardColor: venture.card_color, cardArt: venture.card_art });
          void read(`${base}/ui-pins`).then(result => {
            if (result.venture_id !== ventureId || !Array.isArray(result.pins) || !result.pins.every(pin => typeof pin === "string")) throw new Error("pins");
            if (!active()) return;
            pinsReady.current = true;
            publish({ pins: result.pins });
            try { window.localStorage.setItem(pinsKey, JSON.stringify(result.pins)); } catch {}
          }).catch(() => fail("Quick access"));
          if (!loadDetails) return;
          void read(`${base}/idea/sessions`).then(result => {
            if (!Array.isArray(result.items)) throw new Error("ideas");
            publish({ ideaConfirmed: result.items[0]?.state === "confirmed" });
          }).catch(() => fail("Milestones"));
          if (typeof venture.workspace_id === "string" && venture.workspace_id) {
            void Promise.all([getConnectionsStatus(venture.workspace_id), read(`/api/workspaces/${encodeURIComponent(venture.workspace_id)}/connections`)]).then(([hub, saved]) => {
              if (hub.workspace_id !== venture.workspace_id) throw new Error("scope");
              const cards = hub.order.filter(id => ["gmail", "x", "linkedin", "discord", "reddit"].includes(id)).map(id => ({ ...hub.connections[id], id })).filter(card => card.name);
              for (const provider of ["github", "linear"]) {
                const records = (saved.items || []).filter((row: any) => row.provider === provider && row.workspace_id === venture.workspace_id && !row.simulated && row.status !== "revoked");
                cards.push({ id: provider, name: provider === "github" ? "GitHub" : "Linear", connected: records.length > 0, handle: records[0]?.external_account,
                  ready: records.some((row: any) => row.status === "active" && row.health === "ok"), action: "connect", connectable: true });
              }
              publish({ cards });
            }).catch(() => fail("Toolkit"));
          }
          void Promise.all([read(`${base}/org`), read(`${base}/agents`)]).then(([org, runtime]) => {
            const states = new Map((runtime.items || []).map((agent: any) => [agent.id, agent.status]));
            publish({ agents: (org.agents || []).map((agent: HomeAgent) => ({ ...agent, status: states.get(agent.id) || agent.status })) });
          }).catch(() => fail("Departments"));
          void read(`${base}/ui-archive`).then(result => publish({ archives: result.items, openWork: result.open_work })).catch(() => fail("Tuesday Archives"));
          void read(`${base}/waitlist`).then(result => { if (typeof result.total !== "number") throw new Error("count"); publish({ waitlist: result.total }); }).catch(() => fail("Waitlist"));
          void read(`${base}/apps`).then(async result => {
            if (!Array.isArray(result.items)) throw new Error("apps");
            publish({ appCount: result.items.length });
            void Promise.all(result.items.map((app: { id: string }) => read(`/api/apps/${encodeURIComponent(app.id)}/builds`))).then(builds => {
              if (builds.some(result => !Array.isArray(result.items))) throw new Error("builds");
              publish({ firstBuild: builds.some(result => result.items.some((build: any) => build.state === "succeeded" && build.completion_status === "closed" && build.qa_state === "passed" && build.review?.status === "accepted")) });
            }).catch(() => fail("Milestones"));
            const measurements = await Promise.all((result.items || []).map((app: { id: string }) => Promise.all([read(`/api/apps/${encodeURIComponent(app.id)}/analytics/overview`), read(`/api/apps/${encodeURIComponent(app.id)}/analytics/funnel`)])));
            const overviews = measurements.map(([overview]) => overview);
            const counts: Record<string, number> = {};
            for (const [, funnel] of measurements) for (const step of funnel.steps || []) counts[step] = (counts[step] || 0) + (funnel.counts[step] || 0);
            const days = new Map<string, number>();
            const revenue = new Map<string, number>();
            for (const overview of overviews) for (const day of overview.days || []) if (day.day && typeof day.revenue_minor === "number") revenue.set(day.day, (revenue.get(day.day) || 0) + day.revenue_minor);
            for (const overview of overviews) for (const day of overview.days || []) days.set(day.day, (days.get(day.day) || 0) + day.visits);
            publish({ revenue: [...revenue].sort().map(([day, amount]) => ({day, amount})), views: overviews.reduce((total, overview) => total + (overview.today?.visits ?? 0), 0), signups: overviews.reduce((total, overview) => total + (overview.today?.signups ?? 0), 0), days: [...days].sort().map(([day, visits]) => ({ day, visits })), funnel: { steps: Object.keys(counts), counts } });
          }).catch(() => fail("Visitors"));
        } catch { fail("Company"); }
      })();
    }
    const changed = () => { owner.current = { ventureId, epoch: verifiedSessionEpoch(), subject: verifiedSessionSubject() }; setData(empty(ventureId || "")); setAttempt(value => value + 1); };
    const renamed = (event: Event) => {
      if (active() && (event as CustomEvent<{ ventureId?: string }>).detail?.ventureId === ventureId) retry();
    };
    window.addEventListener("supymem:venture-renamed", renamed);
    window.addEventListener("supymem-session-changed", changed);
    return () => { cancelled = true; live.current = false; window.removeEventListener("supymem-session-changed", changed); window.removeEventListener("supymem:venture-renamed", renamed); };
  }, [ventureId, attempt, loadDetails]);
  return { data: data.ventureId === ventureId && data.subject === owner.current.subject && data.epoch === owner.current.epoch ? data : empty(ventureId || ""), retry, pin, pinBusy: pinBusy || !pinsReady.current, pinNote, current, owner: owner.current };
}
