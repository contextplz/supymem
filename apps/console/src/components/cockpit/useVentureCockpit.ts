"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useActivity } from "@/components/live/ActivityProvider";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { apiErrorCode, apiFailure } from "@/lib/apiFailure";
import { isLocalConsole } from "@/lib/localMode";
import { routeInChat } from "@/lib/chatRouteAction";
import type {
  AgentLiveItem,
  ChatMessage,
  ClarifyAnswer,
  ClarifyQuestion,
  ClarifyRound,
  DashboardPayload,
  GStackPlan,
  ProductBrief,
  MoneyPayload,
  OrgPayload,
  Venture,
} from "@/lib/api-types";
import type { VerdictBriefData } from "@/components/validation/VerdictBrief";
import { displayName, isReachableLandingUrl } from "@/lib/format";
import { clarificationPayload, selectedIds } from "@/lib/clarificationSelection";
import type { NeedsYouItem } from "./NeedsYou";
import type { BlockerItem } from "./Blockers";
import type { CurrentWorkItem } from "./CurrentWork";
import {
  activityProvenance,
  asString,
  isRecord,
  moneyChipMode,
  parseChatItems,
  parseDashboard,
  parseLiveAgents,
  parseMoney,
  parseOrg,
} from "./parse";
import type { ProvenanceMode } from "@/components/primitives/ProvenanceChip";
import { isHeartbeatStale } from "@/components/primitives/Heartbeat";
import { prettyName } from "@/components/office/avatars";

export type ValidationPayload = {
  validation?: (VerdictBriefData & {
    competitor_count?: number;
    demand_signal_count?: number;
    differentiation_score?: number;
    source_count?: number;
  }) | null;
  review_choice?: string | null;
  decision?: { action?: string } | null;
};

type VentureSnap = {
  venture: Venture | null;
  dashboard: DashboardPayload | null;
  money: MoneyPayload | null;
  org: OrgPayload | null;
  liveAgents: AgentLiveItem[];
  chatMessages: ChatMessage[];
  clarifyRound: ClarifyRound | null;
  answers?: Record<string, ClarifyAnswer>;
  gstackPlan: GStackPlan | null;
  validation: ValidationPayload | null;
  legacyMode: boolean;
};

function ventureCacheKey(id: string) {
  return `supymem:venture:${id}`;
}

function readVentureSnap(id: string): VentureSnap | null {
  if (typeof window === "undefined" || !id) return null;
  try {
    const raw = sessionStorage.getItem(ventureCacheKey(id));
    return raw ? (JSON.parse(raw) as VentureSnap) : null;
  } catch {
    return null;
  }
}

function writeVentureSnap(id: string, snap: VentureSnap) {
  if (typeof window === "undefined" || !id) return;
  try {
    sessionStorage.setItem(ventureCacheKey(id), JSON.stringify(snap));
  } catch {
    /* quota */
  }
}

function isValidationPause(reason?: string | null) {
  return (
    reason === "awaiting_validation_review" ||
    reason === "awaiting_validation_decision" ||
    reason === "parked"
  );
}

function isGstackPlanPause(reason?: string | null) {
  return reason === "awaiting_gstack_plan";
}

function isProductQuestionPause(reason?: string | null) {
  return reason === "awaiting_product_understanding";
}

function isProductBriefPause(reason?: string | null) {
  return reason === "awaiting_product_brief";
}

function isBuilderChoicePause(reason?: string | null) {
  return reason === "awaiting_builder_choice";
}

function isBuilderSessionPause(reason?: string | null) {
  return reason === "awaiting_builder_session";
}

export function useVentureCockpit(ventureId: string) {
  const { lines, run, connection, onboarding, refreshOnboarding, retry } = useActivity();
  const cached = typeof window !== "undefined" ? readVentureSnap(ventureId) : null;

  const [venture, setVenture] = useState<Venture | null>(cached?.venture || null);
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(cached?.dashboard || null);
  const [legacyMode, setLegacyMode] = useState(cached?.legacyMode || false);
  const [clarifyRound, setClarifyRound] = useState<ClarifyRound | null>(cached?.clarifyRound || null);
  const [gstackPlan, setGstackPlan] = useState<GStackPlan | null>(cached?.gstackPlan || null);
  const [productBrief, setProductBrief] = useState<ProductBrief | null>(null);
  const [builder, setBuilder] = useState<{
    adapter?: string | null;
    prompt?: string;
    error?: string | null;
  } | null>(null);
  const [validation, setValidation] = useState<ValidationPayload | null>(cached?.validation || null);
  const [money, setMoney] = useState<MoneyPayload | null>(cached?.money || null);
  const [org, setOrg] = useState<OrgPayload | null>(cached?.org || null);
  const [liveAgents, setLiveAgents] = useState<AgentLiveItem[]>(cached?.liveAgents || []);
  const [answers, setAnswers] = useState<Record<string, ClarifyAnswer>>(cached?.answers || {});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>(cached?.chatMessages || []);
  const [booting, setBooting] = useState(!cached?.venture && !cached?.dashboard);
  const [runNote, setRunNote] = useState("");
  const [replanning, setReplanning] = useState(false);
  const [slateKey, setSlateKey] = useState(0);
  const [pausing, setPausing] = useState(false);
  const [pauseNote, setPauseNote] = useState("");
  const [openTasks, setOpenTasks] = useState<CurrentWorkItem[]>([]);
  const [runningTasks, setRunningTasks] = useState<CurrentWorkItem[]>([]);
  const onboardStarted = useRef(false);
  const finishingRef = useRef(false);
  const builderOwner = useRef(ventureId), builderDispatch = useRef(false);
  const builderOrder = useRef<{id: string; ventureId: string; epoch: number; subject: string} | null>(null);
  if (builderOwner.current !== ventureId) {builderOwner.current = ventureId; builderDispatch.current = false; builderOrder.current = null;}
  useEffect(() => {
    builderOwner.current = ventureId;
    const retire = () => {builderDispatch.current = false; builderOrder.current = null; setSubmitting(false);};
    window.addEventListener("supymem-session-changed", retire);
    return () => {builderOwner.current = ""; window.removeEventListener("supymem-session-changed", retire);};
  }, [ventureId]);
  const hydratedRef = useRef("");
  const queueSig = useRef("");

  const title = useMemo(
    () => displayName(venture?.name, venture?.raw_idea),
    [venture?.name, venture?.raw_idea],
  );

  const renameVenture = useCallback(
    async (name: string) => {
      const { renameVenture: save } = await import("@/lib/ventures");
      const r = await save(ventureId, name);
      if (r.ok) {
        const next = ((r.data as { name?: string })?.name || name).trim();
        setVenture((prev) => (prev ? { ...prev, name: next } : prev));
        setDashboard((prev) =>
          prev?.venture ? { ...prev, venture: { ...prev.venture, name: next } } : prev,
        );
        window.dispatchEvent(new CustomEvent("supymem:venture-renamed", { detail: { ventureId, name: next } }));
        return true;
      }
      return false;
    },
    [ventureId],
  );

  useEffect(() => {
    function onRenamed(e: Event) {
      const detail = (e as CustomEvent<{ ventureId?: string; name?: string }>).detail;
      if (detail?.ventureId !== ventureId) return;
      const name = String(detail?.name || "").trim();
      if (!name) return;
      setVenture((prev) => (prev ? { ...prev, name } : prev));
      setDashboard((prev) =>
        prev?.venture ? { ...prev, venture: { ...prev.venture, name } } : prev,
      );
    }
    window.addEventListener("supymem:venture-renamed", onRenamed);
    return () => window.removeEventListener("supymem:venture-renamed", onRenamed);
  }, [ventureId]);
  const oneLiner = venture?.one_liner || dashboard?.venture?.one_liner || "";

  const loadClarify = useCallback(async () => {
    let r = await api(`/api/ventures/${ventureId}/clarification`);
    if (!r.ok) {
      r = await api(`/api/ventures/${ventureId}/clarify/questions`);
    }
    if (!r.ok) return null;

    const data = r.data as {
      id?: string;
      index?: number;
      process?: string;
      stages?: ClarifyRound["stages"];
      questions?: ClarifyQuestion[];
      items?: ClarifyQuestion[];
      round?: ClarifyRound;
    };
    const questions = data.round?.questions || data.questions || data.items || [];
    if (!questions.length) return null;
    const round: ClarifyRound = {
      id: data.round?.id || data.id || "round-1",
      index: data.round?.index ?? data.index ?? 1,
      process: data.round?.process || data.process,
      stages: data.round?.stages || data.stages,
      questions: questions.map((q, i) => ({
        ...q,
        id: q.id || q.slot_key || `q-${i}`,
        prompt: q.prompt || q.text,
        options: (q.options || []).map((o) => ({
          ...o,
          detail: o.detail || o.implication,
        })),
      })),
    };
    setClarifyRound(round);
    setAnswers((prev) => {
      const next = { ...prev };
      for (const q of round.questions) {
        if (q.id && prev[q.id]) {
          if (q.slot_key) next[q.slot_key] = prev[q.id];
          continue;
        }
        if (q.slot_key && prev[q.slot_key]) {
          next[q.id] = prev[q.slot_key];
        }
      }
      return next;
    });
    return round;
  }, [ventureId]);

  const loadDashboard = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/dashboard`);
    if (!r.ok) return null;
    const data = parseDashboard(r.data);
    if (!data) return null;
    setDashboard(data);
    if (data.venture) setVenture((v) => ({ ...(v || data.venture), ...data.venture }));
    return data;
  }, [ventureId]);

  const loadChat = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/chat`);
    if (!r.ok) return;
    setChatMessages(parseChatItems(r.data));
  }, [ventureId]);

  const loadValidation = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/validation`);
    if (!r.ok) return null;
    if (!isRecord(r.data)) return null;
    const data = r.data as ValidationPayload;
    setValidation(data);
    return data;
  }, [ventureId]);

  const loadProductBrief = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/product/brief`);
    if (!r.ok || !isRecord(r.data)) return null;
    const brief = (r.data.brief && isRecord(r.data.brief) ? r.data.brief : r.data) as ProductBrief;
    if (!brief.what_it_is && !brief.one_liner && !brief.name) return null;
    setProductBrief(brief);
    setClarifyRound(null);
    setGstackPlan(null);
    return brief;
  }, [ventureId]);

  const loadGstackPlan = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/gstack/plan`);
    if (!r.ok || !isRecord(r.data)) return null;
    const plan = (r.data.plan && isRecord(r.data.plan) ? r.data.plan : r.data) as GStackPlan;
    if (!plan.verdict && !plan.title) return null;
    setGstackPlan(plan);
    setClarifyRound(null);
    return plan;
  }, [ventureId]);

  const loadBuilder = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/builder/choice`);
    if (!r.ok || !isRecord(r.data)) return null;
    setBuilder({
      adapter: typeof r.data.adapter === "string" ? r.data.adapter : null,
      prompt: typeof r.data.prompt === "string" ? r.data.prompt : "",
      error: typeof r.data.error === "string" ? r.data.error : null,
    });
    return r.data;
  }, [ventureId]);

  const loadMoney = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/money`);
    if (!r.ok) return null;
    const data = parseMoney(r.data);
    setMoney(data);
    return data;
  }, [ventureId]);

  const loadOrg = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/org`);
    if (!r.ok) {
      setOrg(null);
      return null;
    }
    const data = parseOrg(r.data);
    setOrg(data);
    return data;
  }, [ventureId]);

  const loadLive = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/agents/live`);
    if (!r.ok) {
      setLiveAgents([]);
      return [];
    }
    const items = parseLiveAgents(r.data);
    setLiveAgents(items);
    return items;
  }, [ventureId]);

  const loadQueue = useCallback(async () => {
    const r = await api(`/api/ventures/${ventureId}/tasks`);
    if (!r.ok) {
      setOpenTasks([]);
      setRunningTasks([]);
      return;
    }
    const data = r.data as { items?: CurrentWorkItem[]; running?: CurrentWorkItem[] };
    const open = Array.isArray(data.items) ? data.items : [];
    const running = Array.isArray(data.running) ? data.running : [];
    setOpenTasks(open);
    setRunningTasks(running);
    const sig = [...open, ...running].map((row) => `${row.id}:${row.status || ""}`).join("|");
    if (sig !== queueSig.current) {
      queueSig.current = sig;
      setSlateKey((key) => key + 1);
    }
  }, [ventureId]);

  useEffect(() => {
    writeVentureSnap(ventureId, {
      venture,
      dashboard,
      money,
      org,
      liveAgents,
      chatMessages,
      clarifyRound,
      answers,
      gstackPlan,
      validation,
      legacyMode,
    });
  }, [
    ventureId,
    venture,
    dashboard,
    money,
    org,
    liveAgents,
    chatMessages,
    clarifyRound,
    answers,
    gstackPlan,
    validation,
    legacyMode,
  ]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const hadCache = Boolean(readVentureSnap(ventureId)?.venture || readVentureSnap(ventureId)?.dashboard);
      if (!hadCache) setBooting(true);
      setError("");
      await ensureSession();
      if (cancelled) return;

      let vr = await api(`/api/ventures/${ventureId}`);
      if (!vr.ok && (vr.status === 0 || vr.status >= 500)) {
        await new Promise((resolve) => window.setTimeout(resolve, 300));
        if (cancelled) return;
        vr = await api(`/api/ventures/${ventureId}`);
      }
      if (cancelled) return;
      if (!vr.ok) {
        setError(
          vr.status === 404
            ? "This venture isn’t available to this account."
            : "Couldn’t load this venture. Please try again.",
        );
        setBooting(false);
        return;
      }
      if (!isRecord(vr.data)) {
        setError("Couldn’t load this venture. Please try again.");
        setBooting(false);
        return;
      }
      const ven = vr.data as Venture;
      setVenture(ven);
      setError("");

      const ob = await api(`/api/ventures/${ventureId}/onboarding`);
      if (cancelled) return;

      if (ob.status === 404) {
        setLegacyMode(true);
        const doneStages = [
          "charter_review",
          "org_planning",
          "building",
          "live",
          "fork_choice",
          "operate",
        ];
        if (doneStages.includes(ven.stage)) {
          await loadDashboard();
          await loadMoney();
          await loadOrg();
          await loadLive();
          await loadQueue();
          setBooting(false);
          return;
        }
        setSubmitting(true);
        const intake = await api(`/api/ventures/${ventureId}/intake`, {
          method: "POST",
          body: "{}",
        });
        setSubmitting(false);
        if (intake.ok && isRecord(intake.data)) {
          const data = intake.data as { questions?: ClarifyQuestion[]; stage?: string };
          if (data.questions?.length) {
            setClarifyRound({
              id: "legacy",
              index: 1,
              questions: data.questions.map((q) => ({
                ...q,
                prompt: q.prompt || q.text,
              })),
            });
          }
          if (data.stage) setVenture((v) => (v ? { ...v, stage: data.stage! } : v));
        }
        setBooting(false);
        return;
      }

      if (ob.ok) {
        const state = await refreshOnboarding();
        await loadDashboard();
        await loadMoney();
        await loadOrg();
        await loadLive();
        await loadQueue();
        if (state?.status === "paused" && (state.paused_reason === "awaiting_clarification" || isProductQuestionPause(state.paused_reason))) {
          await loadClarify();
        } else if (state?.status === "paused" && isProductBriefPause(state.paused_reason)) {
          await loadProductBrief();
        } else if (state?.status === "paused" && isGstackPlanPause(state.paused_reason)) {
          await loadGstackPlan();
        } else if (state?.status === "paused" && isValidationPause(state.paused_reason)) {
          await loadValidation();
        } else if (
          state?.status === "paused" &&
          (isBuilderChoicePause(state.paused_reason) || isBuilderSessionPause(state.paused_reason))
        ) {
          await loadBuilder();
        } else if (state?.status === "pending" && !onboardStarted.current) {
          onboardStarted.current = true;
          await api(`/api/ventures/${ventureId}/onboard`, {
            method: "POST",
            body: JSON.stringify({ resume: false }),
          });
          await refreshOnboarding();
        }
      }

      await loadChat();
      setBooting(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [ventureId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!onboarding || legacyMode) return;
    if (onboarding.status === "done") {
      void loadDashboard();
      void loadMoney();
      void loadOrg();
      void loadLive();
      void loadQueue();
      void api(`/api/ventures/${ventureId}`).then((vr) => {
        if (vr.ok && isRecord(vr.data)) setVenture(vr.data as Venture);
      });
      void loadChat();
    }
    if (
      onboarding.status === "paused" &&
      (onboarding.paused_reason === "awaiting_clarification" || isProductQuestionPause(onboarding.paused_reason))
    ) {
      void loadClarify();
    }
    if (onboarding.status === "paused" && isProductBriefPause(onboarding.paused_reason)) {
      setGstackPlan(null);
      void loadProductBrief();
    }
    if (onboarding.status === "paused" && isGstackPlanPause(onboarding.paused_reason)) {
      setClarifyRound(null);
      void loadGstackPlan();
    }
    if (
      onboarding.status === "paused" &&
      (isProductQuestionPause(onboarding.paused_reason) || onboarding.paused_reason === "awaiting_clarification")
    ) {
      setGstackPlan(null);
      setProductBrief(null);
    }
    if (onboarding.status === "paused" && isValidationPause(onboarding.paused_reason)) {
      void loadValidation();
    }
    if (
      onboarding.status === "paused" &&
      (isBuilderChoicePause(onboarding.paused_reason) || isBuilderSessionPause(onboarding.paused_reason))
    ) {
      void loadBuilder();
    }
  }, [
    onboarding?.status,
    onboarding?.paused_reason,
    legacyMode,
    loadDashboard,
    loadClarify,
    loadProductBrief,
    loadGstackPlan,
    loadBuilder,
    loadChat,
    loadQueue,
    loadValidation,
    loadMoney,
    loadOrg,
    loadLive,
    ventureId,
    onboarding,
  ]);

  useEffect(() => {
    if (!ventureId) return;
    const tick = window.setInterval(() => {
      void loadOrg();
      void loadLive();
      void loadQueue();
      void loadDashboard();
    }, 8000);
    const onFocus = () => {
      void loadDashboard();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(tick);
      window.removeEventListener("focus", onFocus);
    };
  }, [ventureId, loadOrg, loadLive, loadQueue, loadDashboard]);

  async function requestHumanAlternative(taskId: string, reason: string): Promise<string> {
    const r = await api(`/api/ventures/${ventureId}/human-tasks/${taskId}/alternative`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
    if (!r.ok) return "Supymem couldn’t save that constraint yet. The original request is unchanged.";
    await loadDashboard();
    return "Supymem has kept the dependency visible and proposed a path that can continue without it.";
  }

  async function retryValidationResearch() {
    setSubmitting(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/validation/retry-research`, { method: "POST" });
    setSubmitting(false);
    if (!r.ok) {
      setError("Live research still could not run. Add a Firecrawl key in Settings, then retry.");
      return;
    }
    await loadValidation();
    await refreshOnboarding();
  }

  async function submitValidationDecision(
    action: "review_full" | "review_summary" | "proceed" | "dont_proceed",
  ) {
    setSubmitting(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/validation/decision`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    setSubmitting(false);
    if (!r.ok) {
      setError("Couldn’t record that decision. Try again.");
      return;
    }
    if (isRecord(r.data)) setValidation(r.data as ValidationPayload);
    await refreshOnboarding();
    window.setTimeout(() => void refreshOnboarding(), 400);
    window.setTimeout(() => void refreshOnboarding(), 1200);
    window.setTimeout(() => void loadValidation(), 800);
  }

  async function submitClarify() {
    if (!clarifyRound || submitting) return;
    setSubmitting(true);
    setError("");

    const summary = clarifyRound.questions
      .map((q) => {
        const a = answers[q.id] || (q.slot_key ? answers[q.slot_key] : undefined);
        if (!a) return null;
        if (a.option_id === "other") return a.other_text;
        const labels = selectedIds(a).map((id) => q.options.find((o) => o.id === id)?.label).filter(Boolean);
        if (a.other_text?.trim()) labels.push(a.other_text.trim());
        return labels.join(", ");
      })
      .filter(Boolean)
      .join(" · ");
    if (summary) {
      setChatMessages((prev) => [
        ...prev,
        {
          id: `local-${Date.now()}`,
          role: "founder",
          body_md: summary,
          ts: new Date().toISOString(),
        },
      ]);
    }

    if (legacyMode) {
      const nextAnswers: Record<string, string> = {};
      const otherMap: Record<string, string> = {};
      for (const q of clarifyRound.questions) {
        const a = answers[q.id];
        if (!a) continue;
        if (q.selection_mode === "multiple") {
          const labels = selectedIds(a).map((id) => q.options.find((o) => o.id === id)?.label || id);
          if (a.other_text?.trim()) labels.push(a.other_text.trim());
          nextAnswers[q.id] = `other:${labels.join(" · ")}`;
          otherMap[q.id] = labels.join(" · ");
          continue;
        }
        if (a.option_id === "other" && a.other_text) {
          nextAnswers[q.id] = `other:${a.other_text}`;
          otherMap[q.id] = a.other_text;
        } else if (a.option_id) {
          nextAnswers[q.id] = a.option_id;
        }
      }
      const r = await api(`/api/ventures/${ventureId}/charter/lock`, {
        method: "POST",
        body: JSON.stringify({ answers: nextAnswers, other_verbatim: otherMap }),
      });
      setSubmitting(false);
      if (!r.ok) {
        setError("Couldn’t lock the plan. Try again.");
        return;
      }
      setClarifyRound(null);
      setVenture((v) => (v ? { ...v, stage: "charter_review" } : v));
      await loadDashboard();
      return;
    }

    const payload = { answers: clarifyRound.questions.map((q) => clarificationPayload(q, answers[q.id] || (q.slot_key ? answers[q.slot_key] : undefined))) };
    let r = await api(`/api/ventures/${ventureId}/clarification/answers`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!r.ok && (r.status === 404 || r.status === 405)) {
      const nextAnswers: Record<string, string> = {};
      const otherMap: Record<string, string> = {};
      for (const q of clarifyRound.questions) {
        const a = answers[q.id];
        if (!a) continue;
        if (q.selection_mode === "multiple") {
          const labels = selectedIds(a).map((id) => q.options.find((o) => o.id === id)?.label || id);
          if (a.other_text?.trim()) labels.push(a.other_text.trim());
          nextAnswers[q.id] = `other:${labels.join(" · ")}`;
          otherMap[q.id] = labels.join(" · ");
          continue;
        }
        if (a.option_id === "other" && a.other_text) {
          nextAnswers[q.id] = `other:${a.other_text}`;
          otherMap[q.id] = a.other_text;
        } else if (a.option_id) {
          nextAnswers[q.id] = a.option_id;
        }
      }
      r = await api(`/api/ventures/${ventureId}/charter/lock`, {
        method: "POST",
        body: JSON.stringify({ answers: nextAnswers, other_verbatim: otherMap }),
      });
    }
    const process = clarifyRound.process;
    setSubmitting(false);
    if (!r.ok) {
      setError("Couldn’t submit answers. Try again.");
      return;
    }
    setClarifyRound(null);
    setAnswers({});
    if (process === "product_understanding") {
      await loadProductBrief();
    } else {
      await loadGstackPlan();
    }
    await refreshOnboarding();
    window.setTimeout(() => void refreshOnboarding(), 400);
    window.setTimeout(() => void refreshOnboarding(), 1200);
  }

  async function acceptProductBrief() {
    setSubmitting(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/product/brief`, {
      method: "POST",
      body: JSON.stringify({ action: "accept" }),
    });
    setSubmitting(false);
    if (!r.ok) {
      setError("Couldn’t lock the product brief. Try again.");
      return;
    }
    setProductBrief(null);
    await refreshOnboarding();
    window.setTimeout(() => void refreshOnboarding(), 400);
    window.setTimeout(() => void refreshOnboarding(), 1200);
  }

  async function chooseBuilder(adapter: "cursor" | "claude" | "codex" | "opencode" | "cloud") {
    if (isLocalConsole()) {
      if (builderOwner.current !== ventureId) return;
      if (builderDispatch.current) {setError("Open Builds in your venture to check or stop your saved build before requesting another one."); return;}
      const epoch = verifiedSessionEpoch(), subject = verifiedSessionSubject();
      if (!subject) {setError("Restart your local app, then open your venture and retry your build."); return;}
      const current = () => builderOwner.current === ventureId && epoch === verifiedSessionEpoch() && subject === verifiedSessionSubject();
      builderDispatch.current = true; setSubmitting(true); setError("");
      let failureMessage = "Your build request could not be confirmed. Open Builds to check its saved status before starting another build.";
      try {
        const result = await api("/api/coding/dispatch", {method: "POST", body: JSON.stringify({
          venture_id: ventureId, prompt: builder?.prompt || "Build a landing page from my saved venture brief.",
          ...(adapter === "cloud" ? {} : {prefs: [adapter]})})});
        if (!current()) return;
        if (!result.ok) {
          if ([400, 401, 402, 403, 409, 422, 423, 429].includes(result.status)) builderDispatch.current = false;
          failureMessage = apiErrorCode(result) === "build_tool_needs_setup"
            ? "Connect your coding tool in Settings > Keys and connections > Coding tools, then retry your build."
            : apiFailure(result, "starting your coding tool");
          throw new Error(failureMessage);
        }
        const order = result.data as {id?: string; venture_id?: string};
        if (!order?.id || !/^[A-Za-z0-9_-]{1,128}$/.test(order.id) || order.venture_id !== ventureId) {
          failureMessage = "Your build receipt could not be verified. Open Builds and refresh your saved work before starting another build.";
          throw new Error(failureMessage);
        }
        builderOrder.current = {id: order.id, ventureId, epoch, subject};
        const path = `/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.id)}`;
        if (!routeInChat(path)) window.location.assign(path);
      } catch {
        if (current()) setError(failureMessage);
      } finally {if (current()) setSubmitting(false);}
      return;
    }
    setSubmitting(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/builder/choice`, {
      method: "POST",
      body: JSON.stringify({ adapter }),
    });
    setSubmitting(false);
    if (!r.ok) {
      const detail = (r.data as { detail?: { message?: string } | string } | null)?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : detail?.message || "Couldn’t start the landing page build. Try again.";
      setError(message);
      return;
    }
    await loadBuilder();
    await refreshOnboarding();
    window.setTimeout(() => void refreshOnboarding(), 400);
    window.setTimeout(() => void refreshOnboarding(), 1200);
  }

  async function finishBuilderSession() {
    if (isLocalConsole()) {
      const order = builderOrder.current;
      if (finishingRef.current) return;
      if (!order || order.ventureId !== ventureId || builderOwner.current !== ventureId
          || order.epoch !== verifiedSessionEpoch() || order.subject !== verifiedSessionSubject()) {
        setError("Open Builds in your venture and choose your build to check its saved result."); return;
      }
      finishingRef.current = true; setSubmitting(true); setError("");
      const current = () => builderOwner.current === ventureId && order.epoch === verifiedSessionEpoch() && order.subject === verifiedSessionSubject();
      try {
        const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.id)}`);
        if (!current()) return;
        const saved = result.data as {id?: string; venture_id?: string; state?: string; stop_reconciliation_required?: boolean;
          inference_reconciliation_required?: boolean; native_stop?: {task_verified?: boolean; processes_stopped?: boolean}};
        if (!result.ok || saved?.id !== order.id || saved.venture_id !== ventureId || saved.state !== "stopped"
            || saved.native_stop?.task_verified !== true || saved.native_stop?.processes_stopped !== true
            || saved.stop_reconciliation_required || saved.inference_reconciliation_required) {
          throw new Error("Open Builds in your venture and choose your build to review its checks before treating the work as complete.");
        }
        setBuilder(previous => previous ? {...previous, error: null} : previous);
        await refreshOnboarding();
      } catch {
        if (current()) setError("Open Builds in your venture and choose your build to review its checks before treating the work as complete.");
      } finally {finishingRef.current = false; if (current()) setSubmitting(false);}
      return;
    }
    if (finishingRef.current) return;
    finishingRef.current = true;
    setSubmitting(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/builder/done`, {
      method: "POST",
      body: JSON.stringify({ ready: true }),
    });
    setSubmitting(false);
    if (!r.ok) {
      finishingRef.current = false;
      const rec = r.data as { detail?: { message?: string } | string; error?: { message?: string } } | null;
      const detail = rec?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : detail?.message || rec?.error?.message || r.error || `Couldn’t save the landing page (${r.status || "blocked"}). Try again.`;
      setError(message);
      return;
    }
    setBuilder((prev) => (prev ? { ...prev, error: null } : prev));
    await refreshOnboarding();
    window.setTimeout(() => void refreshOnboarding(), 400);
    window.setTimeout(() => void refreshOnboarding(), 1200);
    void uploadLocalLanding(ventureId, title);
  }

  useEffect(() => {
    const url = dashboard?.tiles?.website?.url;
    if (!isReachableLandingUrl(url)) return;
    if (onboarding?.status !== "paused") return;
    if (
      !isBuilderChoicePause(onboarding.paused_reason) &&
      !isBuilderSessionPause(onboarding.paused_reason)
    ) {
      return;
    }
    void finishBuilderSession();
  }, [dashboard?.tiles?.website?.url, onboarding?.status, onboarding?.paused_reason]);

  useEffect(() => {
    if (!ventureId || hydratedRef.current === ventureId) return;
    hydratedRef.current = ventureId;
    void uploadLocalLanding(ventureId, title);
  }, [ventureId, title]);

  async function acceptGstackPlan() {
    setSubmitting(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/gstack/plan`, {
      method: "POST",
      body: JSON.stringify({ action: "accept" }),
    });
    setSubmitting(false);
    if (!r.ok) {
      setError("Couldn’t lock the GStack plan. Try again.");
      return;
    }
    setGstackPlan(null);
    await refreshOnboarding();
    window.setTimeout(() => void refreshOnboarding(), 400);
    window.setTimeout(() => void refreshOnboarding(), 1200);
  }

  async function sendChat(body: string) {
    setChatMessages((prev) => [
      ...prev,
      {
        id: `local-out-${Date.now()}`,
        role: "founder",
        body_md: body,
        ts: new Date().toISOString(),
      },
    ]);
    const r = await api(`/api/ventures/${ventureId}/chat`, {
      method: "POST",
      body: JSON.stringify({ message: body, body, idempotency_key: crypto.randomUUID() }),
    });
    if (!r.ok) {
      if (r.status !== 404) {
        setChatMessages((prev) => [
          ...prev,
          {
            id: `local-err-${Date.now()}`,
            role: "system",
            body_md: "Couldn’t send that message right now.",
          },
        ]);
      }
      return;
    }
    const data = r.data as ChatMessage | null;
    if (data && typeof data === "object" && (data.body_md || data.id)) {
      setChatMessages((prev) => [...prev, { ...data, role: data.role || "assistant" }]);
    } else {
      window.setTimeout(() => void loadChat(), 400);
    }
    void loadDashboard();
    void loadQueue();
    void loadLive();
  }

  async function runTaskById(taskId: string) {
    setRunNote("Running task…");
    try {
      const r = await api(`/api/ventures/${ventureId}/tasks/${taskId}/run`, {
        method: "POST",
        body: "{}",
      });
      if (!r.ok) {
        setRunNote("Couldn’t run that task. Try again.");
        return;
      }
      const data = r.data as {
        title?: string;
        owner_name?: string;
        completion?: { mode?: string };
        status?: string;
      };
      if (data.status === "needs_you") {
        setRunNote("Parked until you finish a step only you can do. See Needs you.");
      } else if (data.completion?.mode === "simulated") {
        setRunNote(
          data.title
            ? `Draft only for “${data.title}”, nothing went public.`
            : "Draft only, nothing went public.",
        );
      } else {
        setRunNote(
          data.title
            ? `${data.owner_name ? `${data.owner_name} finished` : "Finished"} “${data.title}”. Notes are on Work.`
            : "Task finished. Notes are on Work.",
        );
      }
      await loadDashboard();
      await loadChat();
      await loadQueue();
      setSlateKey((k) => k + 1);
      const vr = await api(`/api/ventures/${ventureId}`);
      if (vr.ok && isRecord(vr.data)) setVenture(vr.data as Venture);
    } catch {
      setRunNote("Couldn’t run that task. Try again.");
    }
  }

  async function confirmChatAction(action: string, payload?: unknown): Promise<boolean> {
    if (action === "task.run") {
      const taskId = isRecord(payload) ? asString(payload.task_id) : undefined;
      if (taskId) await runTaskById(taskId);
      return true;
    }
    const r = await api(`/api/ventures/${ventureId}/chat/confirm`, {
      method: "POST",
      body: JSON.stringify({ action, payload: payload || {} }),
    });
    if (r.ok && r.data && typeof r.data === "object") {
      const data = r.data as ChatMessage;
      setChatMessages((prev) => [...prev, { ...data, role: data.role || "assistant" }]);
      void loadDashboard();
      return true;
    }
    if (r.status === 402 && action !== "blocker.explain") {
      await confirmChatAction("blocker.explain", { code: "insufficient_credits" });
      return false;
    }
    setChatMessages((prev) => [
      ...prev,
      {
        id: `local-err-${Date.now()}`,
        role: "system",
        body_md: "Couldn’t run that action.",
      },
    ]);
    return false;
  }

  async function handleReplan() {
    setReplanning(true);
    setRunNote("Replanning the next five tasks…");
    try {
      const r = await api(`/api/ventures/${ventureId}/tasks/replan`, {
        method: "POST",
        body: "{}",
      });
      if (!r.ok) {
        const detail = isRecord(r.data) ? r.data.detail : undefined;
        const msg =
          typeof detail === "string"
            ? detail
            : isRecord(detail) && isRecord(detail.error)
              ? asString(detail.error.message) || "Couldn’t replan right now."
              : "Couldn’t replan right now.";
        setRunNote(
          r.status === 409 && msg === "Couldn’t replan right now."
            ? "Finish onboarding first, then Replan will rebuild the work queue."
            : msg,
        );
        return;
      }
      const data = r.data as { headline?: string; tasks?: unknown[] };
      setRunNote(
        data.headline
          ? `Replanned: ${data.headline}`
          : `Replanned ${Array.isArray(data.tasks) ? data.tasks.length : "fresh"} next steps.`,
      );
      setSlateKey((k) => k + 1);
      await loadDashboard();
      await loadChat();
    } catch {
      setRunNote("Couldn’t replan. Check that the API is running, then try again.");
    } finally {
      setReplanning(false);
    }
  }

  async function togglePause() {
    const paused = (venture?.status?.code || dashboard?.venture?.status?.code) === "paused";
    setPausing(true);
    setPauseNote("");
    const r = await api(`/api/ventures/${ventureId}/${paused ? "resume" : "pause"}`, {
      method: "POST",
      body: paused ? "{}" : JSON.stringify({ reason: null }),
    });
    setPausing(false);
    if (r.status === 404) {
      setPauseNote("Pause isn’t available on this build yet.");
      return;
    }
    if (r.status === 409) {
      await loadDashboard();
      const vr = await api(`/api/ventures/${ventureId}`);
      if (vr.ok && isRecord(vr.data)) setVenture(vr.data as Venture);
      return;
    }
    if (!r.ok) {
      setPauseNote("Couldn’t update autopilot. Try again.");
      return;
    }
    if (isRecord(r.data)) {
      const nextPaused = r.data.paused === true;
      setVenture((v) =>
        v
          ? {
              ...v,
              status: {
                ...(v.status || {}),
                code: nextPaused ? "paused" : "live",
                label: nextPaused ? "Paused" : "Live",
              },
            }
          : v,
      );
    }
    await loadDashboard();
  }

  async function resumeOnboarding() {
    await api(`/api/ventures/${ventureId}/onboard`, {
      method: "POST",
      body: JSON.stringify({ resume: true }),
    });
    await refreshOnboarding();
  }

  const status =
    onboarding?.status ||
    (legacyMode ? (clarifyRound ? "paused" : dashboard ? "done" : "running") : run.status);
  const pausedReason = onboarding?.paused_reason;
  const showValidationGate =
    !legacyMode &&
    status === "paused" &&
    isValidationPause(pausedReason) &&
    Boolean(validation?.validation);
  // Dashboard existing is not permission to skip genesis. The quiz, research,
  // and first look happen while onboarding is still running or paused.
  const onboardingActive =
    !legacyMode &&
    (status === "running" ||
      status === "paused" ||
      status === "pending" ||
      status === "failed");
  const showOnboarding = onboardingActive || (legacyMode && (status !== "done" || Boolean(clarifyRound)));
  const showDashboard = !onboardingActive && (status === "done" || Boolean(dashboard));

  const pending = dashboard?.governance?.pending_approvals;
  const needsYou: NeedsYouItem[] = [];
  for (const item of dashboard?.needs_you || []) {
    if (!item?.id || !item.title) continue;
    const provider = item.provider || "";
    needsYou.push({
      id: item.id,
      title: item.title,
      timeEstimate: "3 min",
      unblocks: item.why || undefined,
      href: item.href || (provider ? `/v/${ventureId}/connect/${provider}` : `/v/${ventureId}`),
      actionLabel: item.action_label || "Do it now",
      steps: item.steps || [],
    });
  }
  if (pending != null && pending > 0) {
    needsYou.push({
      id: "pending-approvals",
      title: pending === 1 ? "Approve a waiting action" : `Review ${pending} pending approvals`,
      timeEstimate: "A few minutes",
      unblocks: `unblocks ${pending} item${pending === 1 ? "" : "s"}`,
      href: `/v/${ventureId}/settings`,
      actionLabel: "Review",
    });
  }
  const website = dashboard?.tiles?.website;
  const landingLive = isReachableLandingUrl(website?.url);
  if (website?.note && /credential|account|connect/i.test(website.note)) {
    needsYou.push({
      id: "site-credential",
      title: website.note,
      timeEstimate: "3 min",
      unblocks: "unblocks publishing",
      href: `/v/${ventureId}/site`,
    });
  }

  const statusCode = (venture?.status?.code || dashboard?.venture?.status?.code || "").toLowerCase();
  const autopilotOn = statusCode !== "paused" && statusCode !== "archived";
  const blockers: BlockerItem[] = [];
  if (statusCode === "paused") {
    blockers.push({
      id: "paused",
      title: "This venture is paused",
      detail: "Nothing runs until you resume it.",
    });
  }
  for (const item of liveAgents) {
    if (item.state !== "blocked") continue;
    blockers.push({
      id: `blocked-${item.agent_id}`,
      title: item.blocked_reason || "A teammate is stuck",
      href: `/v/${ventureId}/org`,
      actionLabel: "See the team",
    });
  }
  for (const item of openTasks) {
    if (item.runnable !== false) continue;
    const raw = item.last_error || "";
    const provider = raw.startsWith("needs_you:") ? raw.slice("needs_you:".length) : "";
    blockers.push({
      id: `parked-${item.id}`,
      title: `${item.title} is parked`,
      detail: provider
        ? `Waiting on the ${provider} connect steps before this can run.`
        : "Waiting on you before this can run.",
      href: provider ? `/v/${ventureId}/connect/${provider}` : `/v/${ventureId}`,
      actionLabel: "Unblock this",
    });
  }

  const nowItems = lines.map((line) => ({
    id: String(line.seq),
    text: line.text,
    timestamp: line.ts,
    query: line.query,
    provenance: activityProvenance(line.text, line.kind),
  }));

  const earnedMajor =
    money?.gross_received_minor != null ? money.gross_received_minor / (10 ** (money.minor_unit_exponent ?? 2)) : null;
  const moneyMode = moneyChipMode(money);
  const sparkValues = (money?.charges || []).flatMap((row) => {
    if (!isRecord(row)) return [];
    const n = typeof row.amount_minor === "number" ? row.amount_minor : null;
    return n == null ? [] : [Math.abs(n)];
  });

  const interest =
    website && typeof website.interest_count === "number" ? website.interest_count : null;
  const interestMode: ProvenanceMode =
    !website || website.state === "empty" || interest == null
      ? "no_data"
      : website.interest_mode === "live"
        ? "live"
        : "simulated";

  const agents = org?.agents || [];
  const workingLive = liveAgents.filter(
    (item) => item.state === "working" && !isHeartbeatStale(item.last_heartbeat),
  );
  const rosterCount = agents.length || org?.summary?.hired || null;
  // Observed heartbeats are the only honest "working" count. Otherwise show
  // roster size — PulseStrip labels that "on the roster", not "working".
  const workingCount = workingLive.length > 0 ? workingLive.length : rosterCount;
  const namedAgents = (workingLive.length > 0 ? workingLive : agents)
    .map((row) => {
      if ("name" in row && row.name) return prettyName(String(row.name));
      const id = "agent_id" in row ? String(row.agent_id) : "id" in row ? String(row.id) : "";
      const match = agents.find((agent) => agent.id === id);
      return prettyName(match?.name || match?.role || "");
    })
    .filter(Boolean);
  const fallbackNames = namedAgents.length
    ? namedAgents
    : agents.map((agent) => prettyName(agent.name || agent.role || "")).filter(Boolean);
  const lastHeartbeat = liveAgents
    .map((item) => item.last_heartbeat)
    .filter((iso): iso is string => Boolean(iso))
    .sort()
    .at(-1);

  const researchQueries = lines
    .filter((line) => line.query || line.kind === "search")
    .map((line) => line.query || line.text)
    .filter((text, index, all) => all.indexOf(text) === index);

  return {
    ventureId,
    lines,
    run,
    connection,
    onboarding,
    refreshOnboarding,
    retry,
    venture,
    dashboard,
    legacyMode,
    clarifyRound,
    gstackPlan,
    productBrief,
    builder,
    showBuilderChoice: isBuilderChoicePause(pausedReason) && !landingLive,
    showBuilderSession: isBuilderSessionPause(pausedReason) && !landingLive,
    showClarify:
      Boolean(clarifyRound) &&
      (isProductQuestionPause(pausedReason) || pausedReason === "awaiting_clarification") &&
      !isProductBriefPause(pausedReason) &&
      !isGstackPlanPause(pausedReason),
    showProductBrief: Boolean(productBrief) && isProductBriefPause(pausedReason),
    showGstackPlan: Boolean(gstackPlan) && isGstackPlanPause(pausedReason),
    validation,
    money,
    org,
    liveAgents,
    answers,
    setAnswers,
    submitting,
    error,
    chatMessages,
    booting,
    runNote,
    replanning,
    slateKey,
    pausing,
    pauseNote,
    title,
    renameVenture,
    oneLiner,
    status,
    pausedReason,
    showValidationGate,
    showOnboarding,
    showDashboard,
    needsYou: needsYou.slice(0, 8),
    blockers: blockers.slice(0, 4),
    runningTasks: (() => {
      const fromQueue = runningTasks;
      const seen = new Set(fromQueue.map((row) => row.id));
      const fromLive = liveAgents
        .filter(
          (item) =>
            item.state === "working" &&
            item.current_task_desc &&
            !isHeartbeatStale(item.last_heartbeat),
        )
        .map((item) => {
          const agent = agents.find((row) => row.id === item.agent_id);
          return {
            id: `live-${item.agent_id}`,
            title: item.current_task_desc || "Working",
            owner_name: agent?.name || null,
            owner_role: agent?.role || null,
            status: "working",
          };
        })
        .filter((row) => !seen.has(row.id));
      return [...fromQueue, ...fromLive];
    })(),
    nextOpenTitle: openTasks[0]?.title || null,
    autopilotOn,
    nowItems,
    earnedMajor,
    moneyMode,
    sparkValues,
    interest,
    interestMode,
    workingCount,
    agentNames: fallbackNames.slice(0, 8),
    lastHeartbeat,
    agentsReady: agents.length > 0,
    researchQueries,
    sendChat,
    confirmChatAction,
    runTaskById,
    handleReplan,
    submitClarify,
    acceptProductBrief,
    chooseBuilder,
    markBuilderFailed: (message: string) => {
      setBuilder((prev) => ({ ...(prev || {}), error: message }));
    },
    clearBuilderFailed: () => {
      setBuilder((prev) => (prev ? { ...prev, error: null } : prev));
    },
    finishBuilderSession,
    acceptGstackPlan,
    submitValidationDecision,
    retryValidationResearch,
    requestHumanAlternative,
    togglePause,
    resumeOnboarding,
    loadDashboard,
  };
}

function utf8ToB64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

async function uploadLocalLanding(ventureId: string, folderName?: string) {
  if (isLocalConsole()) return;
  try {
    const { desktopInvoke, isDesktopShell } = await import("@/lib/desktop");
    if (!isDesktopShell()) return;
    const snap = await desktopInvoke<{ html?: string; files?: Record<string, string> }>(
      "read_venture_landing",
      { ventureId, folderName: folderName || undefined },
    );
    const html = snap?.html || "";
    if (html.length < 40) return;
    const files: Record<string, string> = { "index.html": html, ...(snap?.files || {}) };
    const { api } = await import("@/lib/client");
    const chunk = 2400;
    for (const [name, body] of Object.entries(files)) {
      if (!body) continue;
      const total = Math.max(1, Math.ceil(body.length / chunk));
      for (let i = 0; i < total; i += 1) {
        const slice = body.slice(i * chunk, (i + 1) * chunk);
        await api(`/api/ventures/${ventureId}/builder/page`, {
          method: "POST",
          body: JSON.stringify({ name, index: i, total, b64: utf8ToB64(slice) }),
        });
      }
    }
  } catch {
    /* keeping the page hosted is best-effort after the session is already marked done */
  }
}
