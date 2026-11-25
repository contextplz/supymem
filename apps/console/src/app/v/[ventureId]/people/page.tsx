"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { PresenceStrip } from "@/components/people/PresenceStrip";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { serverReason } from "@/lib/serverReason";

type Scope = { ventureId: string; subject: string; epoch: number; workspaceId: string };

type Member = {
  id: string;
  person_id: string;
  display_name: string;
  email?: string | null;
  role: string;
};

const ROLES = ["admin", "maintainer", "contributor", "viewer"] as const;

export default function PeoplePage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [workspaceId, setWorkspaceId] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<(typeof ROLES)[number]>("contributor");
  const [msg, setMsg] = useState("");
  const [inviteLink, setInviteLink] = useState("");
  const [inviting, setInviting] = useState(false);
  const [plan, setPlan] = useState<{ tier?: string; seats_used?: number; seats_max?: number } | null>(null);

  const [loadedScope, setLoadedScope] = useState<Scope | null>(null);
  const [loadError, setLoadError] = useState("");
  const active = useRef(ventureId); active.current = ventureId;
  const generation = useRef(0);
  const mutation = useRef<object | null>(null);
  const current = useCallback((owner: Scope, ticket: number) => ticket === generation.current
    && owner.ventureId === active.current && owner.subject === verifiedSessionSubject()
    && owner.epoch === verifiedSessionEpoch(), []);

  const load = useCallback(async () => {
    const ticket = ++generation.current;
    try {
      const auth = await ensureSession();
      const subject = verifiedSessionSubject();
      if (ticket !== generation.current || ventureId !== active.current) return;
      if (!auth.ok || !subject) {
        setLoadedScope(null); setLoadError("Your account could not be verified. Reconnect before changing this venture's team."); return;
      }
      const owner: Scope = { ventureId, subject, epoch: verifiedSessionEpoch(), workspaceId: "" };
      const ven = await api(`/api/ventures/${encodeURIComponent(ventureId)}`);
      if (!current(owner, ticket)) return;
      const wid = ven.ok ? String((ven.data as { workspace_id?: string })?.workspace_id || "") : "";
      if (!wid) {
        setLoadedScope(null); setLoadError("This venture's workspace could not be loaded. Refresh before changing its team."); return;
      }
      owner.workspaceId = wid;
      const [mem, ent] = await Promise.all([
        api(`/api/workspaces/${encodeURIComponent(wid)}/members`),
        api(`/api/entitlement?workspace_id=${encodeURIComponent(wid)}`),
      ]);
      if (!current(owner, ticket)) return;
      const items = mem.ok ? (mem.data as { items?: Member[] } | null)?.items : null;
      if (!Array.isArray(items)) {
        setLoadedScope(null); setLoadError("Team membership could not be checked. Refresh before changing it."); return;
      }
      setWorkspaceId(wid); setMembers(items); setLoadedScope(owner); setLoadError("");
      setPlan(ent.ok && ent.data && typeof ent.data === "object" ? ent.data as typeof plan : null);
    } catch {
      if (ticket === generation.current && ventureId === active.current) {
        setLoadedScope(null); setLoadError("The team could not be loaded. Check your connection and refresh.");
      }
    }
  }, [ventureId, current]);

  useEffect(() => {
    const retire = () => {
      ++generation.current; mutation.current = null;
      setWorkspaceId(""); setMembers([]); setLoadedScope(null); setPlan(null);
      setEmail(""); setRole("contributor"); setMsg(""); setInviteLink(""); setInviting(false); setLoadError("");
      void load();
    };
    retire();
    window.addEventListener("supymem-session-changed", retire);
    return () => { ++generation.current; mutation.current = null; window.removeEventListener("supymem-session-changed", retire); };
  }, [load]);

  const ready = Boolean(loadedScope && current(loadedScope, generation.current) && workspaceId === loadedScope.workspaceId);
  const visibleMembers = ready ? members : [];
  const visiblePlan = ready ? plan : null;

  async function submit(path: string, options: RequestInit, completed: (data: unknown) => void) {
    const owner = loadedScope, ticket = generation.current;
    if (!owner || !current(owner, ticket) || !ready || mutation.current) return;
    const request = {}; mutation.current = request; setInviting(true); setMsg(""); setInviteLink("");
    try {
      const result = await api(path, options);
      if (!current(owner, ticket) || mutation.current !== request) return;
      if (!result.ok) { setMsg(serverReason(result, "This team change was not confirmed. Refresh saved membership before retrying.")); return; }
      completed(result.data);
      void load();
    } catch {
      if (current(owner, ticket) && mutation.current === request) setMsg("This team change was not confirmed. Refresh saved membership before retrying.");
    } finally {
      if (mutation.current === request) { mutation.current = null; setInviting(false); }
    }
  }

  async function invite() {
    if (!ready || !loadedScope || !email.trim() || !ROLES.includes(role)) return;
    await submit(`/api/workspaces/${encodeURIComponent(loadedScope.workspaceId)}/invites`, {
      method: "POST", body: JSON.stringify({ email: email.trim(), role }),
    }, data => {
      const result = data as { link?: string; delivery?: { status?: string } } | null;
      setInviteLink(typeof result?.link === "string" ? result.link : ""); setEmail("");
      setMsg(result?.delivery?.status === "sent"
        ? "Invite emailed. The recipient can accept it for the next seven days."
        : "Invite created, but email delivery was not confirmed. Share its saved recovery link if available.");
    });
  }

  async function changeRole(memberId: string, next: string) {
    if (!ready || !visibleMembers.some(member => member.id === memberId && member.role !== "owner")
        || !ROLES.includes(next as typeof role)) return;
    await submit(`/api/members/${encodeURIComponent(memberId)}`, {
      method: "PATCH", body: JSON.stringify({ role: next }),
    }, () => setMsg("Role change saved. Refreshing membership…"));
  }

  async function remove(memberId: string) {
    if (!ready || !visibleMembers.some(member => member.id === memberId && member.role !== "owner")) return;
    await submit(`/api/members/${encodeURIComponent(memberId)}`, { method: "DELETE" },
      () => setMsg("Member removal saved. Refreshing membership…"));
  }

  return (
    <AppShell title="People" ventureId={ventureId}>
      <div className="chat-scroll">
        <div className="chat-inner">
          <section className="panel-card">
            <h3>Who can operate this venture</h3>
            <p className="muted">
              {visiblePlan
                ? `Plan ${visiblePlan.tier || "free"} · ${visiblePlan.seats_used ?? visibleMembers.length} of ${visiblePlan.seats_max ?? 1} seats`
                : "Roles decide who can approve work, invite others, and change autonomy."}
            </p>
            {loadError ? <p role="status">{loadError} <button type="button" disabled={inviting} onClick={() => void load()}>Refresh membership</button></p> : !ready ? <p role="status">Checking this venture’s team…</p> : null}
            <PresenceStrip workspaceId={ready ? workspaceId : ""} surface="people" />
            <ul style={{ marginTop: 16, paddingLeft: 18 }}>
              {visibleMembers.map((m) => (
                <li key={m.id} style={{ marginBottom: 10 }}>
                  <strong>{m.display_name}</strong>
                  {m.email ? ` · ${m.email}` : ""} · {m.role}
                  {m.role !== "owner" ? (
                    <>
                      {" "}
                      <select
                        value={m.role}
                        disabled={inviting || !ready}
                        onChange={(e) => void changeRole(m.id, e.target.value)}
                        aria-label={`Role for ${m.display_name}`}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>{" "}
                      <button type="button" disabled={inviting || !ready} onClick={() => void remove(m.id)}>
                        Remove
                      </button>
                    </>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
          <section className="panel-card">
            <h3>Invite someone</h3>
            <p className="muted">They get a shareable link. 7-day expiry, single use.</p>
            <label htmlFor="invite-email">Email</label>
            <input
              id="invite-email"
              type="email"
              value={ready ? email : ""}
              disabled={!ready || inviting}
              onChange={(e) => setEmail(e.target.value)}
              style={{ width: "100%", marginTop: 6 }}
            />
            <label htmlFor="invite-role" style={{ display: "block", marginTop: 12 }}>
              Role
            </label>
            <select
              id="invite-role"
              value={ready ? role : "contributor"}
              disabled={!ready || inviting}
              onChange={(e) => setRole(e.target.value as (typeof ROLES)[number])}
              style={{ width: "100%", marginTop: 6 }}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            <button type="button" className="primary" data-usage-control="team_invite_send" style={{ marginTop: 16 }} disabled={inviting || !ready || !email.trim()} onClick={() => void invite()}>
              {inviting ? "Creating…" : "Create invite"}
            </button>
            {ready && msg ? <p className="muted">{msg}</p> : null}
            {ready && inviteLink ? (
              <p>
                Link: <code>{inviteLink}</code>
              </p>
            ) : null}
          </section>
        </div>
      </div>
    </AppShell>
  );
}
