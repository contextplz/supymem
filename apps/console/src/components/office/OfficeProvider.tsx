"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, apiSwr } from "@/lib/client";

export type AgentWork = {
  did?: string;
  doing?: string;
  next?: string;
  next_task_id?: string | null;
  next_provisional?: boolean;
  live_state?: string | null;
  today?: Array<{
    at?: string | null;
    kind?: string;
    status?: string;
    text?: string;
    phase?: string;
  }>;
};

export type OfficeAgent = {
  id: string;
  role: string;
  status: string;
  name?: string;
  title?: string;
  charge?: string;
  voice?: string;
  work?: AgentWork;
};

export type OfficeOrg = {
  agents: OfficeAgent[];
  summary: {
    hired: number;
    working: number;
    watching: number;
  };
  stage?: string;
};

type OfficeContextValue = {
  ventureId?: string;
  org: OfficeOrg | null;
  loading: boolean;
  activating: boolean;
  activateNote: string;
  selectedAgent: OfficeAgent | null;
  mentionPrefix: string;
  setSelectedAgent: (agent: OfficeAgent | null) => void;
  setMentionPrefix: (prefix: string) => void;
  reloadOrg: () => Promise<void>;
  activateTeam: () => Promise<boolean>;
};

const OfficeContext = createContext<OfficeContextValue | null>(null);

function normalizeOrg(data: {
  agents?: OfficeAgent[];
  summary?: { hired?: number; working?: number; watching?: number };
  stage?: string;
} | null): OfficeOrg | null {
  if (!data) return null;
  const agents = data.agents || [];
  const summary = data.summary || {};
  return {
    agents,
    stage: data.stage,
    summary: {
      hired: summary.hired ?? agents.length,
      working: summary.working ?? 0,
      watching: summary.watching ?? 0,
    },
  };
}

export function OfficeProvider({
  ventureId,
  children,
}: {
  ventureId?: string;
  children: ReactNode;
}) {
  const [org, setOrg] = useState<OfficeOrg | null>(null);
  const [loading, setLoading] = useState(false);
  const [activating, setActivating] = useState(false);
  const [activateNote, setActivateNote] = useState("");
  const [selectedAgent, setSelectedAgent] = useState<OfficeAgent | null>(null);
  const [mentionPrefix, setMentionPrefix] = useState("");

  const reloadOrg = useCallback(async () => {
    if (!ventureId) {
      setOrg(null);
      return;
    }
    setLoading(true);
    // A cached org paints at once; the refresh replaces it. A failed refresh keeps the cached copy.
    await apiSwr(`/api/ventures/${ventureId}/org`, (r) => {
      setLoading(false);
      if (!r.ok) {
        setOrg(null);
        return;
      }
      setOrg(normalizeOrg(r.data as OfficeOrg));
    });
    setLoading(false);
  }, [ventureId]);

  useEffect(() => {
    void reloadOrg();
    if (!ventureId) return;
    const timer = window.setInterval(() => void reloadOrg(), 10000);
    return () => window.clearInterval(timer);
  }, [reloadOrg, ventureId]);

  useEffect(() => {
    setSelectedAgent(null);
    setMentionPrefix("");
    setActivateNote("");
  }, [ventureId]);

  useEffect(() => {
    setSelectedAgent((prev) => {
      if (!prev) return prev;
      const fresh = org?.agents.find((agent) => agent.id === prev.id);
      return fresh ? { ...prev, ...fresh } : prev;
    });
  }, [org]);

  const activateTeam = useCallback(async () => {
    if (!ventureId) return false;
    setActivating(true);
    setActivateNote("Activating watching agents…");
    const r = await api(`/api/ventures/${ventureId}/org/activate`, {
      method: "POST",
      body: "{}",
    });
    setActivating(false);
    if (!r.ok) {
      setActivateNote("Couldn’t activate the team. Lock a plan first, then try again.");
      return false;
    }
    setOrg(normalizeOrg(r.data as OfficeOrg));
    setActivateNote("Team is live, they can take work from the board.");
    return true;
  }, [ventureId]);

  const value = useMemo(
    () => ({
      ventureId,
      org,
      loading,
      activating,
      activateNote,
      selectedAgent,
      mentionPrefix,
      setSelectedAgent,
      setMentionPrefix,
      reloadOrg,
      activateTeam,
    }),
    [
      ventureId,
      org,
      loading,
      activating,
      activateNote,
      selectedAgent,
      mentionPrefix,
      reloadOrg,
      activateTeam,
    ],
  );

  return <OfficeContext.Provider value={value}>{children}</OfficeContext.Provider>;
}

export function useOffice(): OfficeContextValue {
  const ctx = useContext(OfficeContext);
  if (!ctx) {
    throw new Error("useOffice must be used inside OfficeProvider");
  }
  return ctx;
}

export function useOfficeOptional(): OfficeContextValue | null {
  return useContext(OfficeContext);
}
