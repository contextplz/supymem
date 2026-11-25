"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { EmployeePortrait, prettyName, prettyRole } from "@/components/office/avatars";
import { ProvenanceChip } from "@/components/primitives/ProvenanceChip";
import { api, ensureSession } from "@/lib/client";
import { displayName } from "@/lib/format";
import styles from "./team.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const QUESTION = "Who is on my team and what is each of them doing right now?";
const STALE_MS = 90_000;
const ROSTER_MS = 12_000;
const DESK_MS = 5_000;
const CLOCK_MS = 15_000;

type DayLogEntry = {
  at?: string | null;
  kind?: string;
  status?: string;
  text?: string;
  phase?: string;
};

type AgentWork = {
  did?: string;
  doing?: string;
  next?: string;
  next_task_id?: string | null;
  next_provisional?: boolean;
  live_state?: string | null;
  today?: DayLogEntry[];
};

type Agent = {
  id: string;
  name?: string;
  role: string;
  title?: string;
  status: string;
  charge?: string;
  voice?: string;
  source?: string;
  work?: AgentWork;
};

type OrgSummary = {
  hired?: number;
  working?: number;
  watching?: number;
  ready?: number;
  paused?: number;
  active?: number;
  quarantined?: number;
  roles?: string[];
  state?: string;
};

type Rationale = { role: string; why: string; deliverable?: string; retire_when?: string };
type Rejected = { role: string; reason: string };

type Org = {
  agent_count?: number;
  agents?: Agent[];
  rationale?: Rationale[];
  rejected?: Rejected[];
  deferred?: Rejected[];
  source?: string;
  summary?: OrgSummary;
  stage?: string;
};

type LiveItem = {
  agent_id: string;
  state?: string;
  current_task_desc?: string | null;
  last_heartbeat?: string | null;
  blocked_reason?: string | null;
};

type VentureInfo = {
  name?: string;
  raw_idea?: string;
  stage?: string;
  status?: { code?: string; label?: string; detail?: string };
};

function errorMessage(data: unknown, fallback: string): string {
  if (!data || typeof data !== "object") return fallback;
  const root = data as {
    error?: { message?: string } | string;
    detail?: { error?: { message?: string }; message?: string } | string;
    message?: string;
  };
  if (typeof root.error === "object" && root.error?.message) return root.error.message;
  if (typeof root.error === "string" && root.error) return root.error;
  if (typeof root.detail === "object") {
    if (root.detail.error?.message) return root.detail.error.message;
    if (root.detail.message) return root.detail.message;
  }
  if (typeof root.detail === "string" && root.detail) return root.detail;
  if (root.message) return root.message;
  return fallback;
}

function beatAge(iso: string | null | undefined, now: number): string | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function isHeartbeatStale(iso: string | null | undefined, now: number): boolean {
  if (!iso) return true;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return true;
  return now - t > STALE_MS;
}

function stateClass(kind: "working" | "blocked" | "stalled" | "paused" | "watching" | "ready" | "idle"): string {
  if (kind === "working") return styles.dotWorking;
  if (kind === "blocked") return styles.dotBlocked;
  if (kind === "stalled") return styles.dotStalled;
  if (kind === "paused") return styles.dotPaused;
  if (kind === "watching") return styles.dotWatching;
  if (kind === "ready") return styles.dotReady;
  return "";
}

export default function OrgPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [title, setTitle] = useState("Team");
  const [stage, setStage] = useState<string | undefined>();
  const [status, setStatus] = useState<VentureInfo["status"]>(undefined);
  const [org, setOrg] = useState<Org | null>(null);
  const [live, setLive] = useState<Record<string, LiveItem>>({});
  const [liveOk, setLiveOk] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null);
  const [desk, setDesk] = useState<{ agent?: Agent; work?: AgentWork } | null>(null);
  const [deskError, setDeskError] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [activating, setActivating] = useState(false);
  const [activateNote, setActivateNote] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const composedRef = useRef(false);
  const logBaseId = useId();

  const loadRoster = useCallback(
    async (opts?: { composeIfEmpty?: boolean }) => {
      const r = await api(`/api/ventures/${ventureId}/org`);
      if (!r.ok) {
        setError(errorMessage(r.data, "Couldn’t load the team for this venture."));
        return null;
      }
      let data = r.data as Org;
      if (!data?.agents?.length && opts?.composeIfEmpty && !composedRef.current) {
        composedRef.current = true;
        const composed = await api(`/api/ventures/${ventureId}/org/compose`, {
          method: "POST",
          body: "{}",
        });
        if (!composed.ok) {
          setError(
            errorMessage(
              composed.data,
              "Lock a plan first, then I can hire the team.",
            ),
          );
          setOrg(data);
          return data;
        }
        data = composed.data as Org;
      }
      setError("");
      setOrg(data);
      return data;
    },
    [ventureId],
  );

  const loadLive = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/agents/live`);
    if (!r.ok) {
      setLiveOk(false);
      return;
    }
    setLiveOk(true);
    const items = ((r.data as { items?: LiveItem[] })?.items || []).filter(
      (item) => item && item.agent_id,
    );
    const map: Record<string, LiveItem> = {};
    for (const item of items) map[item.agent_id] = item;
    setLive(map);
  }, [ventureId]);

  const loadDesk = useCallback(
    async (agentId: string) => {
      const r = await api(`/api/ventures/${ventureId}/org/agents/${agentId}`);
      if (!r.ok) {
        setDeskError(errorMessage(r.data, "Couldn’t load this teammate’s day log."));
        return;
      }
      setDeskError("");
      setDesk(r.data as { agent?: Agent; work?: AgentWork });
    },
    [ventureId],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setBusy(true);
      await ensureSession();
      const ven = await api(`/api/ventures/${ventureId}`);
      if (cancelled) return;
      if (ven.ok) {
        const v = ven.data as VentureInfo;
        setTitle(displayName(v.name, v.raw_idea));
        setStage(v.stage);
        setStatus(v.status || undefined);
      }
      await Promise.all([loadRoster({ composeIfEmpty: true }), loadLive()]);
      if (!cancelled) setBusy(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [ventureId, loadRoster, loadLive]);

  useEffect(() => {
    const id = window.setInterval(() => {
      void loadRoster();
      void loadLive();
    }, ROSTER_MS);
    return () => window.clearInterval(id);
  }, [loadRoster, loadLive]);

  useEffect(() => {
    if (!openId) {
      setDesk(null);
      setDeskError("");
      return;
    }
    void loadDesk(openId);
    const id = window.setInterval(() => void loadDesk(openId), DESK_MS);
    return () => window.clearInterval(id);
  }, [openId, loadDesk]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), CLOCK_MS);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!openId) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpenId(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openId]);

  async function activateTeam() {
    setActivating(true);
    setActivateNote("");
    const r = await api(`/api/ventures/${ventureId}/org/activate`, {
      method: "POST",
      body: "{}",
    });
    setActivating(false);
    if (!r.ok) {
      setActivateNote(errorMessage(r.data, "Couldn’t activate the team."));
      return;
    }
    setOrg(r.data as Org);
    setActivateNote("The team can take work now.");
    await loadLive();
  }

  const agents = org?.agents || [];
  const summary = org?.summary;
  const watching = summary?.watching ?? 0;
  const ready = summary?.ready ?? 0;
  const canActivate = watching > 0 || ready > 0;

  const headerRight = canActivate ? (
    <button type="button" className="primary" data-usage-control="team_org_activate" disabled={activating} onClick={() => void activateTeam()}>
      {activating
        ? "Activating…"
        : watching > 0
          ? `Activate ${watching} watching`
          : `Activate ${ready} ready`}
    </button>
  ) : null;

  // Only count teammates we can actually see working. summary.working reflects
  // configured status, so it would claim activity even with every beat gone quiet.
  const observedWorking = agents.filter((agent) => {
    const row = live[agent.id];
    const state = (row?.state || agent.work?.live_state || "").toLowerCase();
    return state === "working" && !isHeartbeatStale(row?.last_heartbeat, now);
  }).length;

  const hasRecordedWork = agents.some((agent) => agent.work?.did && !agent.work.did.startsWith("Nothing logged"));

  return (
    <AppShell
      title={title}
      subtitle={QUESTION}
      stage={stage}
      status={status}
      ventureId={ventureId}
      headerRight={headerRight}
    >
      <div className={styles.page}>
        <h1 className={styles.question}>{QUESTION}</h1>
        <p className={styles.lede}>
          Each teammate’s card is what they did, what they are doing, and what is next.
          A stalled heartbeat is shown as blocked, never as a spinner that never ends.
        </p>

        {busy && !org && (
          <div className={styles.loading}>
            <p>Loading the team…</p>
            <div className={styles.skeleton} aria-hidden>
              <div className={styles.skelCard} />
              <div className={styles.skelCard} />
              <div className={styles.skelCard} />
            </div>
          </div>
        )}

        {error && (
          <div className={styles.error}>
            <h2>Couldn’t load the team</h2>
            <p className="error">{error}</p>
            <button
              type="button"
              className="primary"
              onClick={() => {
                setBusy(true);
                void Promise.all([loadRoster({ composeIfEmpty: true }), loadLive()]).finally(() =>
                  setBusy(false),
                );
              }}
            >
              Retry
            </button>
            <p>
              <Link href={`/v/${ventureId}/charter`}>Review the plan</Link>
            </p>
          </div>
        )}

        {!busy && !error && agents.length === 0 && (
          <div className={styles.empty}>
            <h2>No teammates yet</h2>
            <p>
              They’re hired when your plan is approved. That usually takes a few seconds after you
              lock the charter, nothing is spinning in the background until then.
            </p>
            <ProvenanceChip mode="no_data" />
          </div>
        )}

        {agents.length > 0 && (
          <>
            <div className={styles.banner}>
              <div className={styles.bannerCopy}>
                <div className={styles.counts}>
                  <span className={styles.count}>
                    {agents.length} teammate{agents.length === 1 ? "" : "s"}
                  </span>
                  {observedWorking > 0 && (
                    <span className={styles.count}>{observedWorking} working</span>
                  )}
                  {(summary?.watching ?? 0) > 0 && (
                    <span className={styles.count}>{summary?.watching} watching</span>
                  )}
                  {(summary?.ready ?? 0) > 0 && (
                    <span className={styles.count}>{summary?.ready} ready</span>
                  )}
                  {(summary?.paused ?? 0) > 0 && (
                    <span className={styles.count}>{summary?.paused} paused</span>
                  )}
                  <ProvenanceChip mode={observedWorking > 0 || hasRecordedWork ? "live" : "no_data"} />
                </div>
                {!liveOk && (
                  <p className={styles.liveDown}>
                    Live heartbeats are not reaching this page, so activity may be stale.
                  </p>
                )}
                {activateNote && <p className="muted">{activateNote}</p>}
              </div>
            </div>

            <div className={styles.grid}>
              {agents.map((agent) => {
                const name = prettyName(agent.name || agent.role);
                const open = openId === agent.id;
                const liveRow = live[agent.id];
                const work = open && desk?.work ? desk.work : agent.work;
                const heartbeat = liveRow?.last_heartbeat;
                const liveState = (liveRow?.state || work?.live_state || "").toLowerCase();
                const liveWorking = liveState === "working";
                const stale = liveWorking && isHeartbeatStale(heartbeat, now);
                const working = liveWorking && !stale;
                const blocked =
                  liveState === "blocked" ||
                  stale ||
                  Boolean(liveRow?.blocked_reason) ||
                  agent.status === "paused";
                const kind = stale
                  ? "stalled"
                  : blocked && liveState === "blocked"
                    ? "blocked"
                    : working
                      ? "working"
                      : agent.status === "paused"
                        ? "paused"
                        : agent.status === "quarantined" || agent.status === "shadow"
                          ? "watching"
                          : agent.status === "ready" || agent.status === "draft"
                            ? "ready"
                            : "idle";
                const age = beatAge(heartbeat, now);
                const blockedReason =
                  liveRow?.blocked_reason ||
                  (stale
                    ? `No heartbeat for ${age || "long enough that they look stalled"}`
                    : agent.status === "paused"
                      ? "This teammate is paused."
                      : null);
                const wouldDo = work?.next;
                const logId = `${logBaseId}-${agent.id}`;
                return (
                  <article
                    key={agent.id}
                    className={`${styles.card} ${open ? styles.cardOpen : ""}`}
                  >
                    <button
                      type="button"
                      className={styles.cardHit}
                      aria-expanded={open}
                      aria-controls={logId}
                      onClick={() => setOpenId(open ? null : agent.id)}
                    >
                      <EmployeePortrait seed={agent.id || agent.role || name} size={44} />
                      <div className={styles.identity}>
                        <div className={styles.who}>
                          <strong>{name}</strong>
                          <span className={styles.role}>
                            {agent.title || prettyRole(agent.role)}
                          </span>
                        </div>
                        <span className={styles.state}>
                          <span
                            className={`${styles.dot} ${stateClass(kind)}`}
                            aria-hidden
                          />
                          {kind === "stalled"
                            ? `stalled${age ? ` · last beat ${age}` : ""}`
                            : kind === "blocked"
                              ? `blocked${age ? ` · last beat ${age}` : ""}`
                              : kind === "working"
                                ? `working${age ? ` · last beat ${age}` : ""}`
                                : kind === "watching"
                                  ? "watching"
                                  : kind === "ready"
                                    ? "ready"
                                    : kind === "paused"
                                      ? "paused"
                                      : agent.status
                                        ? agent.status.replace(/_/g, " ")
                                        : "idle"}
                        </span>
                        <div className={styles.work}>
                          <div className={styles.line}>
                            <p className={styles.label}>Did</p>
                            <p className={styles.text}>
                              {work?.did || "Nothing logged for them yet today."}
                            </p>
                          </div>
                          <div className={styles.line}>
                            <p className={styles.label}>Doing now</p>
                            <p className={styles.text}>
                              {blocked && blockedReason
                                ? blockedReason
                                : work?.doing ||
                                  "Not doing anything observable right now."}
                            </p>
                          </div>
                          <div className={styles.line}>
                            <p className={styles.label}>Next</p>
                            <p className={styles.text}>
                              {work?.next ||
                                "No queued task yet, the plan may assign one."}
                            </p>
                            {work?.next && work.next_provisional && (
                              <p className={styles.hint}>
                                Provisional, may change when the plan is re-cut.
                              </p>
                            )}
                          </div>
                        </div>
                        {agent.charge && (
                          <p className={styles.charge}>Remit: {agent.charge}</p>
                        )}
                        {blocked && (
                          <div className={styles.blocked}>
                            <p>
                              <strong>
                                {stale ? "Stalled" : "Blocked"}
                                {age ? ` · last beat ${age}` : ""}
                              </strong>
                            </p>
                            <p>{blockedReason}</p>
                            {wouldDo && (
                              <p>
                                What they would do: {wouldDo}
                                {work?.next_provisional ? " (provisional)" : ""}
                              </p>
                            )}
                          </div>
                        )}
                      </div>
                    </button>
                    {open && (
                      <div className={styles.log} id={logId}>
                        <h2>Day log</h2>
                        <ErrorNotice error={deskError} source="org.deskError" className="error" />
                        {!desk && !deskError && <p className="muted">Loading today’s log…</p>}
                        {desk && !(desk.work?.today || []).length && (
                          <p className="muted">
                            Nothing attributed to {name} yet today. New work will show up here
                            with a time, once they do something.
                          </p>
                        )}
                        {(desk?.work?.today || []).length > 0 && (
                          <ol className={styles.logList}>
                            {(desk?.work?.today || []).map((entry, i) => (
                              <li key={`${entry.at || "row"}-${i}`} className={styles.logItem}>
                                <span className={styles.logMeta}>
                                  {beatAge(entry.at, now) || "time not recorded"}
                                  {entry.kind ? ` · ${entry.kind}` : ""}
                                  {entry.status ? ` · ${entry.status}` : ""}
                                  {entry.phase ? ` · ${entry.phase}` : ""}
                                </span>
                                <p className={styles.logText}>
                                  {entry.text || "No description for this entry."}
                                </p>
                              </li>
                            ))}
                          </ol>
                        )}
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          </>
        )}

        {(org?.rationale || []).length > 0 && (
          <section className={styles.section}>
            <h2>Why this team</h2>
            <div className={styles.kv}>
              {(org?.rationale || []).map((row, i) => (
                <div className={styles.kvRow} key={`${row.role}-${i}`}>
                  <div className={styles.label}>{prettyRole(row.role)}</div>
                  <div>{row.why}</div>
                  {row.deliverable && <p className={styles.hint}>Delivers: {row.deliverable}</p>}
                  {row.retire_when && <p className={styles.hint}>Retire when: {row.retire_when}</p>}
                </div>
              ))}
            </div>
          </section>
        )}

        {org && !(org.rationale || []).length && agents.length > 0 && (
          <section className={styles.section}>
            <h2>Why this team</h2>
            <p className="muted">
              No hiring rationale is on record for this roster. That field is written when the
              team is composed from a locked plan.
            </p>
          </section>
        )}

        {((org?.rejected || []).length > 0 || (org?.deferred || []).length > 0) && (
          <section className={styles.section}>
            <h2>Roles deliberately not hired</h2>
            <div className={styles.kv}>
              {(org?.rejected || []).map((row, i) => (
                <div className={styles.kvRow} key={`rej-${row.role}-${i}`}>
                  <div className={styles.label}>{prettyRole(row.role)}</div>
                  <div>{row.reason || "No reason recorded for this rejection."}</div>
                </div>
              ))}
              {(org?.deferred || [])
                .filter(
                  (row) =>
                    !(org?.rejected || []).some(
                      (r) => r.role === row.role && r.reason === row.reason,
                    ),
                )
                .map((row, i) => (
                  <div className={styles.kvRow} key={`def-${row.role}-${i}`}>
                    <div className={styles.label}>{prettyRole(row.role)} · deferred</div>
                    <div>{row.reason || "Deferred, with no further reason recorded."}</div>
                  </div>
                ))}
            </div>
          </section>
        )}
      </div>
    </AppShell>
  );
}
