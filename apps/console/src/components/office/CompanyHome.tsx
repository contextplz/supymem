"use client";

import { chatNavigation } from "@/lib/chatRouteAction";

import Link from "@/components/chat/ChatLink";
import { useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useId, useRef, useState, type ReactNode, type RefObject, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "@/lib/client";
import { activeAgentCount, archiveDepartment, companyDepartments, departmentFor, useCompanyHomeData, type ArchiveItem } from "@/lib/companyHome";
import { prefillChat } from "@/lib/chatPrefill";
import { connectHubProvider, syncHubProvider, type ConnectionsHubCard } from "@/lib/opsApi";
import { connectionActionLabel, connectionState } from "@/components/connections/connectionCopy";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import styles from "./CompanyOffice.module.css";
import { EmployeePortrait } from "./avatars";
import { useOfficeOptional } from "./OfficeProvider";
import { plainJob } from "@/lib/jobCopy";
import { agentStatusLabel } from "@/lib/format";
import { founderRouteLinks } from "@/lib/founderRoutes";
import { GmailReconnectBanner } from "@/components/connections/GmailReconnectBanner";

export const CompanyHomeContext = createContext<ReturnType<typeof useCompanyHomeData> | null>(null);
export const HOME_TABS = { home: "Home", archives: "Tuesday Archives", toolkit: "Toolkit", departments: "Departments" } as const;
export type HomeTab = keyof typeof HOME_TABS;
export const ARCHIVE_FOLDERS = { research: "Idea & research reports", website: "Website & builds", outreach: "Outreach & leads", tasks: "Tasks", files: "Files you attached", knowledge: "Knowledge" } as const;

export function CompanyHome({ tab, department, children, scrollRef, notices, grabDepartment, dragging }: { tab: HomeTab; department?: string; children: ReactNode; notices?: ReactNode; scrollRef?: RefObject<HTMLDivElement | null>; grabDepartment?: (name: string, event: ReactPointerEvent<HTMLElement>, intent: "add" | "remove") => void; dragging?: RefObject<boolean> }) {
  const state = useContext(CompanyHomeContext);
  const office = useOfficeOptional();
  const router = chatNavigation(useRouter());
  const [status, setStatus] = useState("");
  const [integration, setIntegration] = useState("");
  const [dateRange, setDateRange] = useState("");
  const [tag, setTag] = useState("");
  const [folder, setFolder] = useState<string | null>(null);
  const [opened, setOpened] = useState<{ id: string; title: string; text: string } | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [authorization, setAuthorization] = useState<string | null>(null);
  const [credential, setCredential] = useState<{ provider: string; account: string; token: string } | null>(null);
  const flight = useRef(false);
  const documentRef = useRef<HTMLElement | null>(null);
  const archiveHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const documentInvoker = useRef<HTMLButtonElement | null>(null);
  const documentTitleId = useId();
  useEffect(() => { setOpened(null); setNote(""); setBusy(false); setAuthorization(null); setCredential(null); setFolder(null); setStatus(""); setIntegration(""); setDateRange(""); setTag(""); }, [state?.owner]);
  useEffect(() => { if (opened) documentRef.current?.focus(); }, [opened]);
  if (!state) return <>{children}</>;
  const { data, current, owner, retry, pin, pinNote, pinBusy } = state;
  const active = () => current(owner);
  const groups = new Map<string, NonNullable<typeof data.agents>>();
  for (const agent of data.agents || []) { const name = departmentFor(agent); groups.set(name, [...(groups.get(name) || []), agent]); }
  const pins = data.pins || [];
  const findCustomers = founderRouteLinks(data.ventureId).find(route => route.label === "Find customers");
  function closeFile() {
    setOpened(null);
    if (documentInvoker.current?.isConnected) documentInvoker.current.focus({ preventScroll: true });
    else archiveHeadingRef.current?.focus({ preventScroll: true });
  }
  async function openFile(item: ArchiveItem, invoker: HTMLButtonElement) {
    if (!active() || flight.current) return;
    documentInvoker.current = invoker;
    flight.current = true; setBusy(true); setOpened(null); setNote("");
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(data.ventureId)}/ui-archive/${encodeURIComponent(item.id)}`);
      if (!active()) return;
      if (!result.ok) throw new Error();
      setOpened(result.data as NonNullable<typeof opened>);
    } catch { if (active()) setNote("Document could not be opened. Reload archives or open its original page."); }
    finally { flight.current = false; if (active()) setBusy(false); }
  }
  async function openAuthorization(url: string) {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) throw new Error();
    if (isDesktopShell() && parsed.hostname === "connect.composio.dev" && parsed.pathname.startsWith("/link/")) await desktopInvokeOrThrow("open_composio_connect_link", { url });
    else window.open(url, "_blank", "noopener,noreferrer");
  }
  async function connect(card: ConnectionsHubCard) {
    if (!active() || flight.current || !data.workspaceId || !card.id) return;
    if (["github", "linear"].includes(card.id)) {
      if (card.connected) setNote(`${card.name}: ${connectionState(card)}${card.handle ? ` · ${card.handle}` : ""}. Refresh to check the latest saved status.`);
      else setCredential({ provider: card.id, account: "", token: "" });
      return;
    }
    flight.current = true; setBusy(true); setNote(""); setAuthorization(null);
    try {
      if (card.action === "sync") { await syncHubProvider(card.id, data.workspaceId); if (active()) retry(); return; }
      const result = await connectHubProvider(card.id, { return_to: `/connections?workspace=${encodeURIComponent(data.workspaceId)}&venture=${encodeURIComponent(data.ventureId)}` }, data.workspaceId);
      if (!active()) return;
      const url = result.authorize_url || result.redirect_url;
      if (url) { const parsed = new URL(url); if (parsed.protocol !== "https:" || parsed.username || parsed.password) throw new Error(); setAuthorization(url); await openAuthorization(url); setNote("Approve in your browser, then Refresh. If it did not open, use the authorization link below."); }
      else setNote("This account needs setup before it can connect. Open company connections for the supported setup, or continue without it.");
    } catch { if (active()) setNote("Connection was not confirmed. Refresh status or reopen authorization; nothing was sent."); }
    finally { flight.current = false; if (active()) setBusy(false); }
  }
  async function saveCredential() {
    if (!active() || flight.current || !credential || !data.workspaceId) return;
    flight.current = true; setBusy(true); setNote("");
    const entry = credential;
    setCredential({ ...entry, token: "" });
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(data.workspaceId)}/connections`, { method: "POST", body: JSON.stringify({ provider: entry.provider, token: entry.token, external_account: entry.account, auth_kind: "pat" }) });
      if (!active()) return;
      if (!result.ok) throw new Error();
      setCredential(null); retry();
    } catch { if (active()) setNote("Connection was not confirmed. Refresh saved status before entering the credential again, or cancel and continue."); }
    finally { flight.current = false; if (active()) setBusy(false); }
  }
  const date = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleDateString("en", { day: "numeric", month: "short", year: "numeric" }) : "Date not recorded";
  const departments = companyDepartments(data.agents || []);
  const records: ArchiveItem[] = [...(data.archives || []), ...(data.revenue || []).map(row => ({id: `revenue:${row.day}`, folder: "revenue", title: `Reported revenue · ${row.amount} minor units`, href: `/v/${encodeURIComponent(data.ventureId)}/analytics`, created_at: `${row.day}T00:00:00Z`, kind: "revenue_analytics"}))];
  const departmentOf = (item: ArchiveItem) => archiveDepartment(item, data.agents || []);
  const statuses = [...new Set(records.map(item => item.status).filter((value): value is string => !!value))];
  const integrations = [...new Set(records.map(item => item.integration).filter((value): value is string => !!value))];
  const availableTags = departments.filter(dept => records.some(item => departmentOf(item) === dept.name)).map(dept => dept.name);
  const cycle = (value: string, values: string[]) => values[(values.indexOf(value) + 1) % values.length];
  const filtered = records.filter(item => (!folder || departmentOf(item) === folder) && (!status || item.status === status) && (!integration || item.integration === integration) && (!tag || departmentOf(item) === tag) && (!dateRange || (item.created_at && Date.parse(item.created_at) >= Date.now() - Number(dateRange) * 86_400_000)));
  const line = (name: string) => {
    const people = groups.get(name) || [];
    const working = people.filter(agent => agent.work?.doing && ["working", "blocked"].includes(agent.work.live_state || ""));
    if (working.length) return working.map(agent => agent.work!.doing).join(" · ");
    const saved = records.filter(item => departmentOf(item) === name);
    if (name === "Revenue & Finance" && data.revenue?.length) return `${data.revenue.length} recorded day${data.revenue.length === 1 ? "" : "s"}`;
    if (name === "People & Agent Ops" && data.agents) return `${activeAgentCount(data.agents)} active agents on the floor`;
    if (saved.length) return `${saved.length} saved record${saved.length === 1 ? "" : "s"}`;
    if (people.length) return `${people.length} agent${people.length === 1 ? "" : "s"}`;
    return data.archives === null ? "Saved work not loaded" : "No saved work yet";
  };
  if (tab === "home") return <div className={styles.homeNotices}>
    {data.workspaceId && active() && <GmailReconnectBanner workspaceId={data.workspaceId} ventureId={data.ventureId} />}
    {notices}{children}</div>;
  return <>
    <div className={styles.pageHead}><div><Link className={styles.back} href={`/v/${encodeURIComponent(data.ventureId)}`} data-chat-route-native>Back to chat</Link><h1 ref={archiveHeadingRef} tabIndex={-1}>{HOME_TABS[tab]}</h1></div>
      {tab === "archives" && <div className={styles.filters}>
        <button type="button" disabled={!statuses.length} onClick={() => setStatus(cycle(status, ["", ...statuses]))}>Status: {status ? plainJob({ status }).title : "All"}</button>
        <button type="button" disabled={!integrations.length} title={!integrations.length ? "Saved work does not record an integration." : undefined} onClick={() => setIntegration(cycle(integration, ["", ...integrations]))}>Integrations: {integration || "Any"}</button>
        <button type="button" disabled={!records.some(item => item.created_at)} onClick={() => setDateRange(cycle(dateRange, ["", "7", "30"]))}>Date: {dateRange ? `Last ${dateRange} days` : "Any"}</button>
        <button type="button" disabled={!availableTags.length} onClick={() => setTag(cycle(tag, ["", ...availableTags]))}>Tags: {tag || "All"}</button>
      </div>}
    </div>
    <div className={styles.canvas} data-company-home={tab} ref={scrollRef}>
      {notices}
      {data.errors.length > 0 && <p className={styles.quiet} role="status">Could not load {data.errors.join(", ")}. <button type="button" onClick={retry}>Retry</button></p>}
      {note && <p className={styles.quiet} role="status">{note} <button type="button" disabled={busy} onClick={retry}>Refresh</button> <Link href={`/connections?venture=${encodeURIComponent(data.ventureId)}`}>Company connections</Link></p>}
      {pinNote && <p className={styles.quiet} role="status">{pinNote} <button type="button" onClick={retry}>Reload quick access</button></p>}
      {tab === "departments" && <>
        <p className={styles.quiet}>Drag a department into Quick access. Drag it back out to remove it.</p>
        {department === "Marketing & GTM" && findCustomers && <div className={styles.charlieActions}><button type="button" className={styles.primaryBtn} onClick={() => router.push(findCustomers.href)}>Find customers</button></div>}
        {data.agents === null ? <p className={styles.quiet} role="status">Loading the team. <button type="button" onClick={retry}>Retry</button></p> : department && departments.some(dept => dept.name === department) ? <div className={styles.floor}>
          <Link className={styles.back} href={`/v/${encodeURIComponent(data.ventureId)}?office=departments`}>All departments</Link>
          <div className={styles.floorHead}><div className={styles.cluster} aria-hidden="true">{(groups.get(department) || []).slice(0, 4).map((agent, index) => <span key={agent.id} style={{ zIndex: 4 - index }}><EmployeePortrait seed={agent.id} size={44} className={styles.roundFace} /></span>)}</div><h2>{department}</h2><p>{line(department)}</p></div>
          {(groups.get(department) || []).length ? <div className={styles.agentGrid}>{groups.get(department)!.map(agent => <article key={agent.id} className={styles.agentCard}>
            <button type="button" className={styles.agentOpen} onClick={() => { const seated = office?.org?.agents.find(candidate => candidate.id === agent.id); if (seated) office?.setSelectedAgent(seated); else prefillChat(`Show me ${agent.name || agent.title || agent.role || "this agent"}'s work`); }} aria-label={`Open ${agent.name || agent.title || agent.role || "agent"}`}><EmployeePortrait seed={agent.id} size={56} className={styles.roundFace} /></button>
            <strong>{agent.name || agent.title || agent.role || "Agent"}</strong><span>{agent.title || agent.role}</span><em>{agentStatusLabel(agent.status)}</em>
            {agent.work?.did && !agent.work.did.startsWith("Nothing logged") && <p>{agent.work.did}</p>}{agent.work?.doing && ["working", "blocked"].includes(agent.work.live_state || "") && <p>{agent.work.doing}</p>}
          </article>)}</div> : <p className={styles.quiet}>No one is seated in this department yet. <button type="button" onClick={() => prefillChat(`Help me plan the ${department} team`)}>Ask Charlie</button></p>}
        </div> : <div className={styles.deptGrid}>{departments.map(dept => {
          const people = groups.get(dept.name) || [];
          const pinned = pins.includes(dept.name);
          return <article key={dept.name} className={styles.deptCard} data-tone={dept.tone} data-dept-id={dept.name} onPointerDown={event => grabDepartment?.(dept.name, event, "add")} onClick={() => { if (!dragging?.current) router.push(`/v/${encodeURIComponent(data.ventureId)}?office=departments&department=${encodeURIComponent(dept.name)}`); }}>
            <div><strong><Link href={`/v/${encodeURIComponent(data.ventureId)}?office=departments&department=${encodeURIComponent(dept.name)}`} onClick={event => event.stopPropagation()}>{dept.name}</Link></strong><span>{people.length ? `${people.length} agent${people.length === 1 ? "" : "s"}` : line(dept.name)}</span></div>
            <button type="button" className={pinned ? styles.pinOn : styles.pin} disabled={pinBusy || data.pins === null} onClick={event => { event.stopPropagation(); void pin(dept.name, !pinned); }}>{pinned ? "Pinned" : "Pin"}</button>
          </article>;
        })}</div>}
      </>}
      {tab === "archives" && <>
        {data.archives === null ? !data.errors.includes(HOME_TABS.archives) && <p className={styles.quiet} role="status">Loading saved work. <button type="button" onClick={retry}>Retry</button></p> : <>
          <section><h2 className={styles.sectionLabel}>Folders <em>{departments.length}</em></h2><div className={styles.folders}>{departments.map(dept => {
            const saved = records.filter(item => departmentOf(item) === dept.name);
            const last = saved.map(item => item.created_at).filter((value): value is string => !!value).sort().at(-1);
            return <article key={dept.name} className={styles.pocket} data-tone={dept.tone}>
              <div className={styles.pocketBack} aria-hidden="true" /><div className={styles.pocketSheet} aria-hidden="true" /><button type="button" className={styles.pocketFront} onClick={() => { setFolder(folder === dept.name ? null : dept.name); setOpened(null); }} aria-pressed={folder === dept.name}>
                <header><div><strong>{dept.name}</strong><span>{line(dept.name)}</span></div><span className={styles.pocketClock} aria-hidden="true"><svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.2" /><path d="M8 5.2V8l2 1.4" /></svg></span></header><p>{last ? `Last added ${date(last)}` : "No saved work yet"}</p>
              </button>
            </article>;
          })}</div></section>
          <section><h2 className={styles.sectionLabel}>Files <em>{filtered.length}</em></h2>
            {!filtered.length && <p className={styles.quiet}>No saved records match this view. <button type="button" onClick={() => { setFolder(null); setStatus(""); setIntegration(""); setDateRange(""); setTag(""); }}>Clear filters</button> <Link href={`/v/${encodeURIComponent(data.ventureId)}/context`}>Open company context</Link></p>}
            <div className={styles.files}>{filtered.map((item, index) => {
              const authors = (data.agents || []).filter(agent => item.agent_id ? agent.id === item.agent_id : !!item.agent_role && (agent.role === item.agent_role || agent.id === item.agent_role));
              const author = authors.length === 1 ? authors[0] : null;
              return <article key={item.id} className={styles.file} data-cover={index % 4}>
                <Link className={styles.cover} href={item.href} aria-label={`Open ${item.title}`} />
                <div className={styles.fileBody}><p><time dateTime={item.created_at || undefined}>{date(item.created_at)}</time>{item.status ? ` · ${plainJob({ status: item.status }).title}` : ""}</p><strong><Link href={item.href}>{item.title}</Link></strong>
                  <footer>{author && <span className={styles.faces} aria-hidden="true"><EmployeePortrait seed={author.id} size={24} className={styles.roundFace} /></span>}<span>{departmentOf(item)}</span></footer>
                  {item.document && <button type="button" className={styles.back} aria-label={`Read saved document: ${item.title}`} disabled={busy} onClick={event => void openFile(item, event.currentTarget)}>Read saved document</button>}
                </div>
              </article>;
            })}</div>
          </section>
          {opened && <section ref={documentRef} tabIndex={-1} aria-labelledby={documentTitleId} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); closeFile(); } }}><h2 id={documentTitleId} className={styles.sectionLabel}>{opened.title}</h2><button type="button" onClick={closeFile}>Close document</button><pre className={styles.document}>{opened.text || "This saved record has no text."}</pre></section>}
        </>}
      </>}
      {tab === "toolkit" && <>
        {data.cards === null ? <p className={styles.quiet} role="status">Loading accounts. <button type="button" onClick={retry}>Retry</button></p> : <div className={styles.toolkits}>{["github", "linear", "linkedin", "reddit", "x", "gmail", "discord"].map(id => {
          const card = data.cards!.find(card => card.id === id);
          const name = ({ github: "GitHub", linear: "Linear", linkedin: "LinkedIn", reddit: "Reddit", x: "X", gmail: "Gmail", discord: "Discord" })[id];
          return card ? <article key={id} className={styles.toolkit}><button type="button" className={styles.toolkitTarget} disabled={busy} onClick={() => card.action ? void connect(card) : router.push(`/connections?venture=${encodeURIComponent(data.ventureId)}`)} aria-label={`${card.name}: ${connectionActionLabel(card)}`}><img src={`/toolkit/${id}.svg`} alt="" width={40} height={40} /><strong>{name}</strong><span>{connectionState(card)}</span></button></article> : <article key={id} className={styles.toolkit}><Link className={styles.toolkitTarget} href={`/connections?venture=${encodeURIComponent(data.ventureId)}`}><img src={`/toolkit/${id}.svg`} alt="" width={40} height={40} /><strong>{name}</strong><span>Status unavailable</span></Link></article>;
        })}</div>}
        {authorization && <button type="button" onClick={() => void openAuthorization(authorization).catch(() => setNote("Could not open authorization. Open company connections and try there."))}>Open authorization again</button>}
        {credential && <form onSubmit={event => { event.preventDefault(); void saveCredential(); }}><h2 className={styles.sectionLabel}>Connect {credential.provider === "github" ? "GitHub" : "Linear"}</h2><label>Organization or team ID<input required maxLength={200} value={credential.account} onChange={event => setCredential({ ...credential, account: event.target.value })} /></label><label>Personal access token<input required type="password" autoComplete="off" value={credential.token} onChange={event => setCredential({ ...credential, token: event.target.value })} /></label><p>Your token goes directly to the credential store. Nothing is published or sent.</p><button disabled={busy} type="submit">Connect account</button><button type="button" onClick={() => setCredential(null)}>Cancel</button></form>}
      </>}
    </div>
  </>;
}
