"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { apiSwr, ensureSession, verifiedSessionEpoch, verifiedSessionSubject, type SessionInfo } from "@/lib/client";
import { getOpsWorkspace, setOpsWorkspace } from "@/lib/opsApi";
import { operationsHref, type OperationsScope } from "@/lib/operationsScope";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const ScopeContext = createContext<OperationsScope | null>(null);
export const useOperationsScope = () => useContext(ScopeContext);

type Workspace = { id: string; name?: string; kind?: string };
type LoadedOwner = { subject: string; receiptEpoch: number; generation: number; routeKey: string };

/** Scope is a UI selection; the gateway and Core still authorize every request. */
export function OpsWorkspaceGate({ children, onScopeChange, hideVentureControls = false }: { children: ReactNode; onScopeChange?: (scope: OperationsScope | null) => void; hideVentureControls?: boolean }) {
  const searchParams = useSearchParams();
  const urlWorkspace = searchParams.get("workspace") || "";
  const urlVenture = searchParams.get("venture") || undefined;
  const routeKey = JSON.stringify([urlWorkspace, urlVenture]);
  const route = useRef(routeKey);
  route.current = routeKey;
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [scope, setScope] = useState<OperationsScope | null>(null);
  const [actorId, setActorId] = useState("");
  const [loadedOwner, setLoadedOwner] = useState<LoadedOwner | null>(null);
  const accountEpoch = useRef(0);
  const generation = useRef(0);
  const receiptSubject = verifiedSessionSubject();
  const receiptEpoch = verifiedSessionEpoch();
  const liveRouteKey = () => {
    const params = new URL(window.location.href).searchParams;
    return JSON.stringify([params.get("workspace") || "", params.get("venture") || undefined]);
  };
  const ownerCurrent = (owner: LoadedOwner | null) => Boolean(owner &&
    owner.generation === generation.current && owner.subject === verifiedSessionSubject() &&
    owner.receiptEpoch === verifiedSessionEpoch() && owner.routeKey === route.current && owner.routeKey === liveRouteKey());

  useEffect(() => {
    const changed = () => {
      // Invalidate pending reads synchronously, before React's next render.
      accountEpoch.current += 1;
      generation.current += 1;
      setLoadedOwner(null);
      setOpsWorkspace(""); setSelected(""); setScope(null); setActorId("");
      setWorkspaces([]); setLoading(true); onScopeChange?.(null);
      setAttempt(value => value + 1);
    };
    window.addEventListener("supymem-session-changed", changed);
    return () => { accountEpoch.current += 1; window.removeEventListener("supymem-session-changed", changed); };
  }, [onScopeChange]);

  useEffect(() => {
    let cancelled = false;
    const epoch = accountEpoch.current;
    const ticket = ++generation.current;
    let owner: LoadedOwner | null = null;
    const current = () => !cancelled && epoch === accountEpoch.current && ticket === generation.current &&
      routeKey === route.current && routeKey === liveRouteKey() && (!owner || ownerCurrent(owner));
    const previous = getOpsWorkspace();
    // URL selectors are hints until this account's membership reads finish.
    setOpsWorkspace(""); setSelected(""); setScope(null); setLoadedOwner(null);
    onScopeChange?.(null);
    setLoading(true);
    setError("");
    void (async () => {
      try {
      const auth = await ensureSession();
      if (!current()) return;
      const session = auth.data as SessionInfo | null;
      if (!auth.ok || !session?.authenticated || !session.subject) {
        setError("Your account could not be verified. Reconnect before choosing a company workspace.");
        setLoading(false);
        return;
      }
      const subject = session.subject;
      if (verifiedSessionSubject() !== subject) return;
      owner = { subject, receiptEpoch: verifiedSessionEpoch(), generation: ticket, routeKey };
      setActorId(subject);
      // Independent reads start together; cached copies answer immediately.
      const ownedPending = urlVenture ? apiSwr(`/api/ventures/${encodeURIComponent(urlVenture)}`)
        .catch(() => ({ ok: false, status: 503, data: null })) : null;
      const result = await apiSwr("/api/workspaces");
      if (!current()) return;
      if (!result.ok) {
        setError("Your company workspaces could not be loaded. Reconnect to try again.");
        setLoading(false);
        return;
      }
      const rows = ((result.data as { items?: Workspace[] })?.items || [])
        .filter((row) => row && typeof row.id === "string");
      setWorkspaces(rows);
      let preferred = urlWorkspace || previous;
      const venture = urlVenture;
      let checkedVenture: string | undefined;
      let invalidSelector = false;
      if (venture) {
        const owned = await ownedPending!;
        if (!current()) return;
        if (!owned.ok) {
          setError("This venture is unavailable to your account. Choose an available workspace.");
          preferred = "";
          invalidSelector = true;
        } else {
          const data = owned.data as { workspace_id?: string; venture?: { workspace_id?: string } };
          preferred = data.workspace_id || data.venture?.workspace_id || "";
          if (urlWorkspace && urlWorkspace !== preferred) {
            setError("The company and workspace selection do not match. Choose the intended workspace.");
            invalidSelector = true;
          } else checkedVenture = venture;
        }
      }
      if (urlWorkspace && !rows.some((row) => row.id === urlWorkspace)) {
        invalidSelector = true;
        setError("This workspace is unavailable to your account. Choose an available workspace.");
      }
      const initial = invalidSelector ? "" : rows.some((row) => row.id === preferred)
        ? preferred : rows.length === 1 ? rows[0].id : "";
      setOpsWorkspace(initial, checkedVenture);
      setSelected(initial);
      const nextScope = initial ? { workspaceId: initial, ventureId: checkedVenture, actorId: subject } : null;
      setScope(nextScope);
      setLoadedOwner(owner);
      onScopeChange?.(nextScope);
      setLoading(false);
      } catch {
        if (!current()) return;
        setLoadedOwner(null); setOpsWorkspace(""); setSelected(""); setScope(null);
        setError("Your company workspaces could not be verified. Reload before choosing a workspace.");
        setLoading(false);
      }
    })();
    return () => { cancelled = true; if (ticket === generation.current) { generation.current += 1; setOpsWorkspace(""); } };
  }, [attempt, onScopeChange, routeKey]);

  function changeWorkspace(value: string) {
    if (loading || !ownerCurrent(loadedOwner) || getOpsWorkspace() !== selected) return;
    if (value && !workspaces.some((row) => row.id === value)) return;
    // Only the already-verified workspace/venture pair may retain a venture.
    // A generic workspace selection must not leave another venture's chat in the shell.
    const keepVenture = value && value === scope?.workspaceId ? scope.ventureId : undefined;
    setOpsWorkspace(value, keepVenture);
    setSelected(value);
    const nextScope = value ? { workspaceId: value, ventureId: keepVenture, actorId } : null;
    setScope(nextScope);
    const nextOwner = { ...loadedOwner!, generation: ++generation.current };
    setLoadedOwner(nextOwner);
    onScopeChange?.(nextScope);
    const next = new URL(window.location.href);
    next.searchParams.delete("venture");
    next.searchParams.delete("workspace");
    const href = operationsHref(next.pathname + next.search, nextScope);
    window.history.replaceState({}, "", href);
    setError("");
  }

  const renderGeneration = generation.current;
  const ready = !loading && ownerCurrent(loadedOwner) && getOpsWorkspace() === selected;
  const retry = () => {
    if (routeKey !== route.current || routeKey !== liveRouteKey() || renderGeneration !== generation.current || receiptSubject !== verifiedSessionSubject() || receiptEpoch !== verifiedSessionEpoch()) return;
    generation.current += 1; setLoadedOwner(null); setLoading(true); setAttempt(value => value + 1);
  };
  if (loading) return <p className="content-wrap" role="status">Loading your company workspaces…</p>;
  if (!ready) return <p className="content-wrap" role="status">{error || "Verify your current account before choosing a company workspace."} <button type="button" onClick={retry}>Reload workspaces</button></p>;

  return (
    <div className="content-wrap">
      {!(hideVentureControls && scope?.ventureId) && <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
        <label htmlFor="operations-workspace">Company</label>
        <select
          id="operations-workspace"
          value={selected}
          disabled={!ready}
          onChange={(event) => changeWorkspace(event.target.value)}
        >
          <option value="">Choose a company</option>
          {workspaces.map((row) => <option key={row.id} value={row.id}>{typeof row.name === "string" && row.name.trim() || "Unnamed company"}</option>)}
        </select>
        <button type="button" className="ghost sm" onClick={retry}>Refresh</button>
      </div>}
      <ErrorNotice error={error} source="OpsWorkspaceGate.error" className="error" />
      {!workspaces.length && !error && <p>Create a venture to start planning your company’s growth.</p>}
      {workspaces.length > 0 && !selected && <p>Select the company these operations belong to.</p>}
      {ready && selected && actorId && <ScopeContext.Provider value={scope}><div key={`${actorId}:${loadedOwner?.receiptEpoch}:${loadedOwner?.generation}:${selected}:${scope?.ventureId || ""}`}>{children}</div></ScopeContext.Provider>}
    </div>
  );
}
