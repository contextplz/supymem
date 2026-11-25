"use client";

import { confirmationState, failedSince } from "@/lib/chatAnswered";
import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { useRouter } from "next/navigation";
import type { ChatMessage } from "@/lib/api-types";
import { ideaPollStart, ideaPollState } from "@/lib/ideaChatPoll";
import { parseChatItems } from "@/components/cockpit/parse";
import { parseEmployeeStamp } from "@/components/office/avatars";
import { api, apiLive, ensureSession, verifiedSessionEpoch, verifiedSessionSubject, type ApiResult } from "@/lib/client";
import { useActivityRefresh, watchActivity } from "@/lib/activityRefresh";
import { isLocalConsole } from "@/lib/localMode";
import { apiErrorCode, apiFailure } from "@/lib/apiFailure";
import { conversationChatPath, conversationResponseMatches } from "@/lib/chatConversations";
import { applyChatPrefill, isClientChatAction } from "@/lib/chatPrefill";
import { useChatConversations } from "./useChatConversations";
import { desktopExecutionAvailability, desktopExecutionMessage } from "@/lib/desktopExecutionAvailability";
import { macTaskPrepareFailure, activeMacTaskReviewPath } from "@/lib/macTaskPrepareFailure";
import {
  MAC_TASKS_HINT,
  bareMacTaskConfirmation,
  desktopControlRequest,
  browserTaskRequest,
  reviewableDeviceRequest,
  shouldAppendMacTasksHint,
} from "@/lib/reviewableDeviceRequest";
import { selectDesktopApplication } from "@/lib/desktopApplication";
import { MAC_TASK_PERMISSION_EVENT, macTaskPermissionsGranted, type PermissionSnapshot } from "@/lib/macPermissions";
import { desktopInvoke, invokeErrorMessage, isDesktopShell } from "@/lib/desktop";
import { chatTaskRunPath } from "@/lib/chatTaskRun";
import { ChatActionPendingError, chatActionPending, confirmFailureActions, confirmFailureMessage, gtmConfirmFailureMessage } from "@/lib/chatConfirmError";
import { AI_SETTINGS_HREF, usageLimitMessage } from "@/lib/credits";
import { localMacTaskObservation, macTaskAnswerOrderId } from "@/lib/macTaskAnswerReceipt";
import { mergeChat } from "@/lib/chatMerge";
import { beginChatNavigation } from "@/lib/chatNavigationUsage";
import { chatFlight } from "@/lib/chatStop";
import { chatDraftKey, draftStorage, readChatDraft } from "@/lib/chatComposer";
import { chatActionIdentity } from "./chatActionPolicy";

const VENTURE_SCREENS = new Set([
  "", "idea", "now", "tasks", "org", "site", "research", "market",
  "feedback", "charter", "plan", "builds", "analytics", "import",
  "evolution", "people", "connections", "settings",
]);

type ChatSendOptions = {replyToQuestion?: boolean; questionId?: string; jobId?: string; source?: "voice"; idempotencyKey?: string; onReply?: (message: ChatMessage) => void};
type CheckpointDraft = {text: string; opts?: ChatSendOptions};

async function chatPayloadDigest(body: string, opts?: ChatSendOptions): Promise<string> {
  const canonicalPayload = JSON.stringify({question_id: opts?.questionId || null,
    ...(opts?.jobId ? {job_id: opts.jobId} : {}),
    reply_to_question: !!opts?.replyToQuestion, source: opts?.source || null, text: body.trim().slice(0, 4000)});
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalPayload));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

function sameVentureScreen(ventureId: string, href: string | undefined): href is string {
  if (!href || !href.startsWith("/") || href.startsWith("//")) return false;
  const base = `/v/${encodeURIComponent(ventureId)}`;
  const url = new URL(href, "https://app.supymem.com");
  const suffix = url.pathname === base ? "" : url.pathname.startsWith(`${base}/`)
    ? url.pathname.slice(base.length + 1) : null;
  if (suffix === null || !VENTURE_SCREENS.has(suffix) || url.hash) return false;
  if (suffix !== "builds") return url.search === "";
  const keys = [...url.searchParams.keys()];
  return keys.length === 0 || (keys.length === 2 && keys.includes("app") && keys.includes("run")
    && /^app_[a-z0-9]+$/.test(url.searchParams.get("app") || "")
    && /^bld_[a-z0-9]+$/.test(url.searchParams.get("run") || ""));
}

function appendMacTasksHint(messages: ChatMessage[]): ChatMessage[] {
  return [
    ...messages,
    {
      id: `local-mac-hint-${Date.now()}`,
      role: "system",
      body_md: MAC_TASKS_HINT,
      ts: new Date().toISOString(),
    },
  ];
}

async function withLocalMacTaskObservation(message: ChatMessage, ventureId: string): Promise<ChatMessage> {
  if (!isDesktopShell()) return message;
  const orderId = macTaskAnswerOrderId(message, ventureId);
  if (!orderId) return message;
  // The backend-selected order is the authority for identity and settlement.
  // Its final screenshot assessment is held in the Mac's durable local journal.
  const receipt = await desktopInvoke("device_work_order_action", {
    ventureId, orderId, action: "status",
  });
  const observation = localMacTaskObservation(receipt, orderId);
  return observation ? { ...message, body_md: `${message.body_md || ""}\n\n${observation}` } : message;
}

async function startRequestedToolBuild(message: ChatMessage, ventureId: string, current: () => boolean): Promise<ChatMessage> {
  if (window.__SUPYMEM__?.mode !== "local" || message.kind !== "build_tool_started") return message;
  const action = message.actions?.find(item => typeof item.payload?.order_id === "string");
  const orderId = action?.payload?.order_id;
  if (typeof orderId !== "string" || !/^[A-Za-z0-9_-]+$/.test(orderId)
      || action?.href !== `/v/${ventureId}/device-work-orders/${orderId}`) return message;
  const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const owned = () => current() && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
  try {
    const saved = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(orderId)}`);
    if (!owned()) return message;
    const order = saved.data as {id?: string; venture_id?: string; state?: string; requested_build?: unknown} | undefined;
    if (!saved.ok || order?.id !== orderId || order.venture_id !== ventureId || !order.requested_build) throw new Error("build_not_verified");
    if (order.state === "approved") {
      const receipt = await api(`/native/device-work-orders/${encodeURIComponent(ventureId)}/${encodeURIComponent(orderId)}/start`, {method: "POST"});
      if (!receipt.ok || !receipt.data) throw new Error("build_start_unconfirmed");
    }
    return message;
  } catch {
    return {...message, kind: "text", body_md: "Your build start could not be confirmed. Choose Watch your tool below to check its status before retrying.",
      actions: message.actions?.map(item => item === action ? {...item, label: "Watch your tool"} : item)};
  }
}

function latestManagerName(messages: ChatMessage[]): string {
  return [...messages].reverse().map((m) => parseEmployeeStamp(m.body_md || "").speaker)
    .find((sp) => sp?.title === "Manager")?.name || "Charlie";
}

function sendRefusal(result: ApiResult, reason: string, ventureId: string, managerName: string, text: string): ChatMessage {
  const local = window.__SUPYMEM__?.mode === "local";
  const code = apiErrorCode(result);
  const data = result.data as {detail?: {message?: unknown; error?: {message?: unknown}}; error?: {message?: unknown}} | null;
  const serverReason = data?.detail?.message || data?.detail?.error?.message || data?.error?.message;
  if (result.status === 402 || code?.endsWith("insufficient_credits")) reason = usageLimitMessage(local);
  else if (typeof serverReason === "string" && serverReason.trim()) reason = serverReason.trim();
  if (code === "device_terminal_task_unavailable") {
    reason = "Your coding tool did not start this request. Open Settings > Keys and connections > Coding tools, choose the tool you want to use, then send your request again.";
  } else if (/\bReport a problem\b[\s\S]*\bcode\b/i.test(reason)) {
    reason = "Your request could not start. Choose Edit your request below, then send it again.";
  }
  const showBuildStatus = /\bOpen (?:Builds|Build activity)\b/i.test(reason);
  reason = reason.replace(/\bOpen (?:Builds|Build activity)\b[^.!?\n]*[.!?]?/gi,
    "Choose Show status here below to review your latest build before retrying.");
  const reviewPath = activeMacTaskReviewPath(result);
  let action: NonNullable<ChatMessage["actions"]>[number] = {label: "Edit your request", action: "chat.prefill", payload: {text}, style: "primary"};
  if (reviewPath?.startsWith(`/v/${encodeURIComponent(ventureId)}/device-work-orders/`)) {
    action = {label: "Open your current task", href: reviewPath, style: "primary"};
  } else if (local && (result.status === 402 || code?.endsWith("insufficient_credits"))) {
    action = {label: "Open your AI service settings", href: AI_SETTINGS_HREF, style: "primary"};
  } else if (local && /\bCapabilities\b/i.test(reason)) {
    action = {label: "Open Capabilities", href: "/settings/local/capabilities", style: "primary"};
  } else if (local && /\bSettings\b|Keys and connections|secure connection form/i.test(reason)) {
    action = {label: "Open Keys and connections", href: "/settings/local", style: "primary"};
  } else if (showBuildStatus) {
    action = {label: "Show status here", action: "status", style: "primary"};
  }
  return {id: `local-err-${Date.now()}`, role: "assistant", kind: "needs_you",
    body_md: `<!--employee:${managerName}:Manager:manager-->\n${reason}`, actions: [action]};
}

export function deepAnswerProgress(messages: ChatMessage[], ventureId: string): {spendMicro: number; capMicro: number} | undefined {
  const seen = new Set<string>();
  for (const message of [...messages].reverse()) {
    if (!["deep_answer_pending", "deep_answer"].includes(message.kind || "")) continue;
    for (const action of message.actions || []) {
      const progress = (action.payload as {reasoning?: Record<string, unknown>} | undefined)?.reasoning;
      if (!progress || progress.venture_id !== ventureId || typeof progress.task_id !== "string" || seen.has(progress.task_id)) continue;
      seen.add(progress.task_id);
      if (progress.state !== "thinking") continue;
      const spend = progress.spend_micro, cap = progress.cap_micro;
      if (typeof spend === "number" && Number.isSafeInteger(spend) && spend >= 0 && typeof cap === "number" &&
          Number.isSafeInteger(cap) && cap > 0 && cap <= 10_000_000 && spend <= cap) return {spendMicro: spend, capMicro: cap};
    }
  }
}

// A retired async update cannot relabel another conversation's local state.
function useChatScopeState<T>(scope: string, current: () => boolean, initial: T): [T, (update: SetStateAction<T>) => void] {
  const initialValue = useRef(initial);
  const [saved, setSaved] = useState({scope, value: initial});
  const update = useCallback((next: SetStateAction<T>) => {
    if (!current()) return;
    setSaved(previous => {
      if (!current()) return previous;
      const value = previous.scope === scope ? previous.value : initialValue.current;
      const updated = typeof next === "function" ? (next as (value: T) => T)(value) : next;
      return previous.scope === scope && Object.is(previous.value, updated) ? previous : {scope, value: updated};
    });
  }, [scope, current]);
  return [saved.scope === scope ? saved.value : initialValue.current, update];
}

export function useVentureChat(ventureId: string, { navigateOnResponse = true }: { navigateOnResponse?: boolean } = {}) {
  const activity = useActivityRefresh(ventureId);
  const conversations = useChatConversations(ventureId);
  const conversationId = conversations.conversationId;
  const conversationDraftId = conversations.conversationDraftId;
  const scope = JSON.stringify([verifiedSessionSubject(), verifiedSessionEpoch(), ventureId, conversationId, conversations.conversationVersion]);
  const current = useCallback(() => conversations.conversationReady && conversations.conversationCurrent(conversationId, conversations.conversationVersion),
    [conversations.conversationReady, conversations.conversationCurrent, conversationId, conversations.conversationVersion]);
  const router = useRouter();
  const currentVenture = useRef(ventureId);
  currentVenture.current = ventureId;
  const ideaPollGeneration = useRef(0);
  const flight = useRef(chatFlight());
  const confirmationNonce = useRef<string | null>(null);
  useEffect(() => () => flight.current.stop(), [scope]);
  const chatRequestRef = useRef<{scope: string; attempt: {payload: string; key: string; inFlight: boolean; uncertain: boolean} | null}>({scope, attempt: null});
  if (chatRequestRef.current.scope !== scope) chatRequestRef.current = {scope, attempt: null};
  const [messages, setMessages] = useChatScopeState<ChatMessage[]>(scope, current, []);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const [thinking, setThinking] = useChatScopeState(scope, current, false);
  const deepProgress = deepAnswerProgress(messages, ventureId);
  const reasoningRef = useRef(false);
  reasoningRef.current = !!deepProgress;
  const [nextHistoryCursor, setNextHistoryCursor] = useChatScopeState<string | null>(scope, current, null);
  const [historyLoading, setHistoryLoading] = useChatScopeState(scope, current, false);
  const [historyError, setHistoryError] = useChatScopeState(scope, current, "");
  const [historyReady, setHistoryReady] = useChatScopeState(scope, current, false);
  const checkpointScope = JSON.stringify([verifiedSessionSubject(), ventureId, conversationId]);
  const checkpointKey = `supymem.chatRequest.v1:${encodeURIComponent(checkpointScope)}`;

  const resolveCheckpoint = useCallback(async (loading = false) => {
    let raw = window.localStorage.getItem(checkpointKey);
    if (raw === null) return null;
    const saved = JSON.parse(raw) as {version: number; scope: string; nonce: string; payload_digest: string; sent_at?: number};
    if (!saved || !["nonce,payload_digest,scope,version", "nonce,payload_digest,scope,sent_at,version"].includes(Object.keys(saved).sort().join(","))
        || (saved.sent_at !== undefined && (!Number.isSafeInteger(saved.sent_at) || saved.sent_at <= 0))
        || saved.version !== 1 || saved.scope !== checkpointScope
        || typeof saved.nonce !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(saved.nonce)
        || typeof saved.payload_digest !== "string" || !/^[a-f0-9]{64}$/.test(saved.payload_digest)) {
      throw new Error("chat_request_checkpoint_unavailable");
    }
    let state = "unknown";
    let missing = false;
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat/requests/${encodeURIComponent(saved.nonce)}${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ""}`);
      const reply = result.data as {state?: unknown; missing?: unknown} | null;
      if (result.ok && (reply?.state === "done" || reply?.state === "pending")) state = reply.state;
      missing = result.ok && reply?.state === "unknown" && reply.missing === true && window.__SUPYMEM__?.mode === "local";
    } catch {}
    let retry: CheckpointDraft | undefined;
    if (missing && saved.sent_at !== undefined && Date.now() - saved.sent_at >= 120_000) {
      state = "unsent";
      const offered = messagesRef.current.find(message => message.id === `local-uncertain-${saved.nonce}`)
        ?.actions?.find(action => action.action === "chat.startFresh")?.payload as CheckpointDraft | undefined;
      const draft = readChatDraft(draftStorage(), chatDraftKey(verifiedSessionSubject() ?? undefined, ventureId, conversationDraftId));
      const candidates: (CheckpointDraft | undefined)[] = [offered, {text: draft}];
      for (const candidate of candidates) {
        if (typeof candidate?.text === "string" && candidate.text.trim()
            && await chatPayloadDigest(candidate.text, candidate.opts) === saved.payload_digest) {
          retry = candidate;
          break;
        }
      }
    }
    if (!current() || (loading && chatRequestRef.current.attempt?.inFlight)) return undefined;
    if (window.localStorage.getItem(checkpointKey) !== raw) throw new Error("chat_request_checkpoint_changed");
    if (state === "done" || state === "unsent") window.localStorage.removeItem(checkpointKey);
    else if (missing && saved.sent_at === undefined) {
      saved.sent_at = Date.now();
      raw = JSON.stringify(saved);
      window.localStorage.setItem(checkpointKey, raw);
    }
    return {saved, raw, state, ...(retry ? {retry} : {})};
  }, [checkpointKey, checkpointScope, ventureId, conversationId, conversationDraftId, current]);

  const showCheckpoint = useCallback((checkpoint: NonNullable<Awaited<ReturnType<typeof resolveCheckpoint>>>, retry?: CheckpointDraft) => {
    const id = `local-uncertain-${checkpoint.saved.nonce}`;
    if (checkpoint.state === "unsent") {
      const noticeId = `local-err-unsent-${checkpoint.saved.nonce}`;
      setMessages(prev => [...prev.filter(message => message.id !== id && message.id !== noticeId
        && !(message.id.startsWith("local-out-") && message.client_request_id === checkpoint.saved.nonce)),
        {id: noticeId, role: "system", kind: "needs_you",
          body_md: checkpoint.retry ? "Your earlier message was not sent. Choose Retry to send it again."
            : "Your earlier message was not sent. Choose Retry, then type your message again.",
          actions: [{label: "Retry", action: "chat.startFresh", payload: {...checkpoint.retry, unsentNonce: checkpoint.saved.nonce}}]}]);
      return;
    }
    setMessages(prev => [...prev.filter(message => message.id !== id), {id, role: "system", kind: window.__SUPYMEM__?.mode === "local" ? "needs_you" : "confirmation",
      body_md: checkpoint.state === "pending"
        ? "Your earlier request is still running. Tap Show status for progress."
        : window.__SUPYMEM__?.mode === "local"
          ? "Your earlier request may still finish; starting fresh won't cancel it. Tap Show status or start fresh."
          : "Your earlier request is unconfirmed and may still finish; starting fresh won't cancel it. Tap Show status or start fresh.",
      actions: [{label: "Show status", action: "status", ...(window.__SUPYMEM__?.mode === "local" ? {payload: {nonce: checkpoint.saved.nonce}} : {})},
        ...(checkpoint.state === "unknown" ? [{label: "Start fresh", action: "chat.startFresh",
          payload: {...prev.find(message => message.id === id)?.actions?.find(action => action.action === "chat.startFresh")?.payload,
            checkpoint: checkpoint.raw, ...retry}}] : [])],
    }]);
  }, [ventureId, setMessages]);

  const load = useCallback(async (): Promise<ChatMessage[] | undefined> => {
    if (!ventureId || !current()) return;
    try {
      await ensureSession();
      if (!current()) return;
      if (!chatRequestRef.current.attempt?.inFlight) {
        const checkpoint = await resolveCheckpoint(true);
        if (!current()) return;
        if (checkpoint?.state === "done" || checkpoint?.state === "unsent") {
          if (!chatRequestRef.current.attempt?.inFlight) chatRequestRef.current.attempt = null;
          if (checkpoint.state === "unsent") showCheckpoint(checkpoint);
          else setMessages(prev => prev.filter(message => message.id !== `local-uncertain-${checkpoint.saved.nonce}`));
        } else if (checkpoint) showCheckpoint(checkpoint);
      }
      const r = await apiLive(conversationChatPath(ventureId, conversationId));
      if (!current()) return;
      if (!r.ok) { setHistoryError("You cannot load your saved conversation right now. Try again."); return; }
      if (!conversationResponseMatches(r.data, conversationId)) {setHistoryError("The saved history does not match this chat. Refresh saved chats."); return;}
      const saved = parseChatItems(r.data);
      const cursor = (r.data as { next_cursor?: unknown } | null)?.next_cursor;
      setNextHistoryCursor(typeof cursor === "string" ? cursor : null);
      if (isLocalConsole()) {
        setMessages((current) => mergeChat(current, saved, true));
        setHistoryError("");
        setHistoryReady(true);
      }
      const recentStart = Math.max(0, saved.length - 8);
      const enriched = await Promise.all(saved.map((message, index) => index >= recentStart
        ? withLocalMacTaskObservation(message, ventureId) : Promise.resolve(message)));
      if (!current()) return;
      // Merge, never replace: a reply or error shown locally must survive a page that lacks it.
      setMessages((current) => mergeChat(current, enriched, true));
      setHistoryError("");
      return enriched;
    } catch {
      if (current()) setHistoryError("You cannot load your saved conversation right now. Try again.");
    } finally {
      if (current()) setHistoryReady(true);
    }
  }, [ventureId, conversationId, current, setMessages, setHistoryError, setHistoryReady, setNextHistoryCursor, resolveCheckpoint, showCheckpoint]);
  const reloadHistory = useCallback(async (): Promise<void> => { await load(); }, [load]);

  const loadEarlier = useCallback(async () => {
    if (!ventureId || !current() || !nextHistoryCursor || historyLoading) return;
    setHistoryLoading(true);
    try {
      const r = await apiLive(conversationChatPath(ventureId, conversationId, nextHistoryCursor));
      if (!current()) return;
      if (!r.ok) { setHistoryError("Could not load earlier messages. Retry history."); return; }
      if (!conversationResponseMatches(r.data, conversationId)) {setHistoryError("The earlier messages do not match this chat."); return;}
      const older = parseChatItems(r.data);
      const cursor = (r.data as { next_cursor?: unknown } | null)?.next_cursor;
      setNextHistoryCursor(typeof cursor === "string" ? cursor : null);
      setHistoryError("");
      setMessages(current => {
        const ids = new Set(current.map(message => message.id));
        return mergeChat([...older.filter(message => !ids.has(message.id)), ...current], []);
      });
    } catch {
      if (current()) setHistoryError("Could not load earlier messages. Retry history.");
    } finally {
      setHistoryLoading(false);
    }
  }, [ventureId, conversationId, current, nextHistoryCursor, historyLoading, setHistoryLoading, setHistoryError, setNextHistoryCursor, setMessages]);

  useEffect(() => {
    ideaPollGeneration.current++;
    polledId.current = "";
    setMessages([]);
    setHistoryReady(false);
    setNextHistoryCursor(null);
    setHistoryError("");
    void load();
    return () => { ideaPollGeneration.current++; };
  }, [load]);

  const polledId = useRef("");
  // A paid or background clarification can land after the immediate reply.
  // Reconcile saved history until a newer question arrives; after a proposal, keep
  // polling (~3 min) for the auto-queued validation outcome.
  const startIdeaPoll = useCallback((anchorId: string | undefined, proposalAnchor: boolean) => {
    if (!anchorId || polledId.current === anchorId) return;
    polledId.current = anchorId;
    const generation = ++ideaPollGeneration.current;
    void (async () => {
      let proposal = proposalAnchor;
      for (let attempt = 0, deadline = proposal ? 45 : 70; attempt < deadline; attempt++) {
        await new Promise((resolve) => window.setTimeout(resolve, proposal ? 4000 : 3000));
        if (generation !== ideaPollGeneration.current || !current()) return;
        const saved = await load();
        const at = saved?.findIndex((message) => message.id === anchorId) ?? -1;
        const state = at >= 0 && saved ? ideaPollState(saved.slice(at)) : "waiting";
        if (state === "done") return;
        if (state === "proposal" && !proposal) { proposal = true; attempt = 0; deadline = 45; }
      }
      if (proposal) return;
      if (generation === ideaPollGeneration.current && current()) setMessages((prev) => mergeChat([...prev, {
        id: `local-idea-wait-${Date.now()}`,
        role: "system",
        body_md: "The next question is taking longer to prepare. You can keep chatting or reopen this conversation to check progress.",
      }], []));
    })();
  }, [current, load, setMessages]);

  useEffect(() => {
    const last = messages.at(-1);
    const start = ideaPollStart(last);
    if (start) startIdeaPoll(last?.id, start === "proposal");
  }, [messages, startIdeaPoll]);

  // Charlie posts milestones (e.g. "Your plan is ready") server-side; pick them up while the panel is open and visible.
  const busyRef = useRef(false);
  busyRef.current = thinking;
  useEffect(() => {
    if (!ventureId || !current() || !historyReady) return;
    const local = isLocalConsole();
    let live = true;
    let inFlight = false;
    let delay = reasoningRef.current ? 2000 : 8000;
    let timer: number;
    const controller = new AbortController();
    const poll = async () => {
      if (!current() || !live || inFlight) return;
      window.clearTimeout(timer);
      inFlight = true;
      try {
        if (!busyRef.current) {
          if (window.__SUPYMEM__?.mode === "local" && window.localStorage.getItem(checkpointKey) !== null) { await load(); return; }
          const path = conversationChatPath(ventureId, conversationId);
          const r = await (local ? api(path, { signal: controller.signal }) : apiLive(path, controller.signal));
          if (!current() || !live) return;
          if (!r.ok) {
            delay = Math.min(15000, delay * 2);
            setHistoryError("You cannot get new messages right now. Your conversation is saved. Try again.");
          } else if (!busyRef.current && conversationResponseMatches(r.data, conversationId)) {
            delay = reasoningRef.current ? 2000 : 8000;
            setMessages((current) => mergeChat(current, parseChatItems(r.data), true));
            setHistoryError("");
          }
        }
      } catch {
        if (current() && live) {
          delay = Math.min(15000, delay * 2);
          setHistoryError("You cannot get new messages right now. Your conversation is saved. Try again.");
        }
      } finally {
        inFlight = false;
        if (!local && current() && live) timer = window.setTimeout(() => void poll(), document.hidden ? 15000 : delay);
      }
    };
    if (local) {
      const stop = watchActivity(activity, poll, false);
      return () => { live = false; controller.abort(); stop(); };
    }
    timer = window.setTimeout(() => void poll(), delay);
    const wake = () => { if (!document.hidden) void poll(); };
    window.addEventListener("online", wake);
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => { live = false; controller.abort(); window.clearTimeout(timer); window.removeEventListener("online", wake); window.removeEventListener("focus", wake); document.removeEventListener("visibilitychange", wake); };
  }, [activity, ventureId, conversationId, current, historyReady, setMessages, setHistoryError,
    typeof window !== "undefined" && window.__SUPYMEM__?.mode === "local" ? checkpointKey : null,
    typeof window !== "undefined" && window.__SUPYMEM__?.mode === "local" ? load : null]);

  useEffect(() => {
    if (messages.at(-1)?.kind !== "gtm_diagnosis_pending") return;
    let live = true;
    let inFlight = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (!current() || !live || inFlight) return;
      inFlight = true;
      try {
        const status = await apiLive(`/api/ventures/${encodeURIComponent(ventureId)}/chat/gtm/status${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ""}`, controller.signal);
        if (current() && live && status.ok && (status.data as { message?: { id?: string } } | null)?.message?.id) await load();
      } catch {
        /* The interval checks the saved result again. */
      } finally {
        inFlight = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    window.addEventListener("online", refresh);
    window.addEventListener("focus", refresh);
    return () => { live = false; controller.abort(); window.clearInterval(timer); window.removeEventListener("online", refresh); window.removeEventListener("focus", refresh); };
  }, [messages, ventureId, conversationId, current, load]);

  const sendChat = useCallback(
    async (body: string, opts?: ChatSendOptions): Promise<boolean> => {
      if (!current() || flight.current.busy()) return false;
      const wantHint = shouldAppendMacTasksHint(body);
      const outId = `local-out-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      // A failed chip click must not leave the question looking answered.
      const dropOut = () => { if (opts?.replyToQuestion) setMessages((prev) => prev.filter((m) => m.id !== outId)); };
      setMessages((prev) => [
        ...prev,
        {
          id: outId,
          role: "founder",
          body_md: body,
          ts: new Date().toISOString(),
        },
      ]);
      // Ren typing shows at once; works with #187 clear-on-send (composer awaits this promise).
      setThinking(true);
      const controller = flight.current.begin();
      let ownsThinking = true;
      let requestAttempt: {payload: string; key: string; inFlight: boolean; uncertain: boolean} | null = null;
      let chatDispatched = false;
      try {
        // Main venture chat posts to from-chat only for reviewable Mac phrases.
        // Explicit Mac-task entry points (Settings / Mac tasks buttons) always
        // post free text to from-chat themselves.
        // "Prepare the Mac task for review" confirms the founder's previous Mac
        // request. Charlie must never claim a prepare that did not happen: either
        // an order is created here or we say plainly that nothing was prepared.
        let macBody = body;
        if (!opts?.replyToQuestion && bareMacTaskConfirmation(body)) {
          const earlier = messagesRef.current.filter(
            (m) => m.role === "founder" && reviewableDeviceRequest(m.body_md ?? "", window.__SUPYMEM__?.mode === "local")).at(-1);
          if (!earlier) {
            setMessages((prev) => [...prev, {
              id: `local-err-${Date.now()}`,
              role: "system",
              body_md: "There is no Mac task to prepare yet, so nothing was prepared. Describe a reviewed task, for example: Check git status in the terminal for the venture workspace.",
            }]);
            return true;
          }
          macBody = earlier.body_md ?? "";
        }
        if (!opts?.replyToQuestion && reviewableDeviceRequest(macBody, window.__SUPYMEM__?.mode === "local")) {
          if (browserTaskRequest(macBody) && !/https:\/\/\S+/i.test(macBody)) {
            setMessages(previous => [...previous, { id: `local-site-${Date.now()}`, role: "assistant",
              body_md: `<!--employee:${latestManagerName(previous)}:Manager:manager-->\n` +
                "Which website should I use? Send its HTTPS URL with the task." }]);
            return true;
          }
          if (!isDesktopShell() && window.__SUPYMEM__?.mode !== "local") {
            setMessages(previous => [...previous, { id: `local-err-${Date.now()}`, role: "system",
              body_md: "This task needs Supymem for Mac. Open this venture in the Mac app and ask for the task there. No Mac task was prepared or started." }]);
            return true;
          }
          let desktopSelection;
          try {
            if (desktopControlRequest(macBody) && !browserTaskRequest(macBody)) {
              const scopeEpoch = verifiedSessionEpoch();
              const status = desktopExecutionAvailability(await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`));
              if (!current() || scopeEpoch !== verifiedSessionEpoch()) return false;
              if (status !== "available") {
                setMessages((prev) => [...prev, {
                  id: `local-err-${Date.now()}`, role: "assistant",
                  body_md: `<!--employee:${latestManagerName(prev)}:Manager:manager-->\n` +
                    desktopExecutionMessage(status) + "\n\nI can give you manual steps for this task instead. Nothing was prepared or run.",
                  actions: [{label: "Show manual steps", action: "chat.reply", style: "primary",
                    payload: {text: `Give me manual steps for this task. Answer only; do not prepare a Mac task: ${macBody}`}}],
                }]);
                return true;
              }
              const permissions = await desktopInvoke<PermissionSnapshot>("permissions_status");
              if (!current() || scopeEpoch !== verifiedSessionEpoch()) return false;
              if (!macTaskPermissionsGranted(permissions)) {
                window.dispatchEvent(new CustomEvent(MAC_TASK_PERMISSION_EVENT, { detail: { resume: async () => {
                  if (!current() || scopeEpoch !== verifiedSessionEpoch()) return;
                  setMessages(previous => previous.filter(message => message.id !== outId));
                  await sendChat(macBody, opts);
                } } }));
                return true;
              }
              desktopSelection = await selectDesktopApplication(macBody);
              if (!current() || scopeEpoch !== verifiedSessionEpoch()) return false;
            }
          } catch (error) {
            if (!current()) return false;
            setMessages((prev) => [
              ...prev,
              {
                id: `local-err-${Date.now()}`,
                role: "system",
                body_md: invokeErrorMessage(error, "Supymem could not inspect this Mac. Try again after reconnecting this device."),
              },
            ]);
            // The explanation is posted above; do not also fail the composer.
            return true;
          }
          if (!current()) return false;
          const prepared = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/from-chat`, {
            method: "POST",
            body: JSON.stringify({ instruction: macBody, ...desktopSelection }),
          });
          if (!current()) return false;
          if (!prepared.ok) {
            setMessages((prev) => [
              ...prev,
              sendRefusal(prepared, macTaskPrepareFailure(prepared), ventureId, latestManagerName(prev), body),
            ]);
            dropOut();
            return window.__SUPYMEM__?.mode === "local" && !opts?.replyToQuestion;
          }
          const data = prepared.data as {
            review_path?: unknown;
            id?: unknown;
            title?: unknown;
            manifest?: { title?: unknown };
          };
          const path = data.review_path;
          const prefix = `/v/${ventureId}/device-work-orders/`;
          if (typeof path !== "string" || !path.startsWith(prefix)) {
            setMessages((prev) => [
              ...prev,
              {
                id: `local-err-${Date.now()}`,
                role: "system",
                body_md: "Supymem could not verify the saved Mac review link.",
              },
            ]);
            dropOut();
          return false;
          }
          const title =
            (typeof data.title === "string" && data.title) ||
            (typeof data.manifest?.title === "string" && data.manifest.title) ||
            "Mac task";
          // Stay in the dock: same review_path / authority as the device review page.
          setMessages((prev) => [
            ...prev,
            {
              id: `local-mac-${Date.now()}`,
              role: "assistant",
              kind: "confirmation",
              body_md:
                // Same manager stamp the server saves, so it never renders as "Team".
                `<!--employee:${latestManagerName(prev)}:Manager:manager-->\n` +
                `I prepared a Mac task for review: **${title}**. ` +
                "Nothing runs on your Mac until you open the review and approve it.",
              actions: [
                {
                  label: "Review Mac task",
                  href: path,
                  style: "primary",
                },
              ],
              ts: new Date().toISOString(),
            },
          ]);
          return true;
        }
        if (!current()) return false;
        const requestPayload = JSON.stringify({
          message: body, body,
          ...(conversationId ? {conversation_id: conversationId} : {}),
          ...(opts?.replyToQuestion ? {reply_to_question: true} : {}),
          ...(opts?.questionId ? {question_id: opts.questionId} : {}),
          ...(opts?.jobId ? {job_id: opts.jobId} : {}),
          ...(opts?.source ? {source: opts.source} : {}),
        });
        const previous = chatRequestRef.current.attempt;
        if (previous?.inFlight) {
          ownsThinking = false;
          setMessages(prev => [...prev.filter(m => m.id !== outId), {id: `local-uncertain-${Date.now()}`, role: "system", kind: "confirmation",
            body_md: "Your earlier request is still running. Tap Show status for progress.",
            actions: [{label: "Show status", action: "status"}]}]);
          return false;
        }
        requestAttempt = previous || {payload: requestPayload, key: "", inFlight: false, uncertain: false};
        chatRequestRef.current.attempt = requestAttempt;
        requestAttempt.inFlight = true;
        // A reload must not turn an uncertain request into a fresh provider call.
        // The checkpoint never stores chat text.
        const payloadDigest = await chatPayloadDigest(body, opts);
        if (!current() || !flight.current.owns(controller)) return false;
        const resolved = await resolveCheckpoint();
        if (!current() || !flight.current.owns(controller)) return false;
        if (resolved?.state === "done" || resolved?.state === "unsent") {
          requestAttempt.key = resolved.state === "unsent" && resolved.saved.payload_digest === payloadDigest
            ? resolved.saved.nonce : crypto.randomUUID();
          requestAttempt.uncertain = false;
          requestAttempt.payload = requestPayload;
          if (resolved.state === "unsent") showCheckpoint(resolved);
          else setMessages(prev => prev.filter(message => message.id !== `local-uncertain-${resolved.saved.nonce}`));
        } else if (resolved) {
          if (resolved.state === "pending" || resolved.saved.payload_digest !== payloadDigest) {
            requestAttempt.uncertain = true;
            chatRequestRef.current.attempt = null;
            setMessages(prev => prev.filter(m => m.id !== outId));
            showCheckpoint(resolved, {text: body, opts});
            return false;
          }
          requestAttempt.key = resolved.saved.nonce;
          requestAttempt.uncertain = true;
        }
        if (!requestAttempt.key) requestAttempt.key = opts?.idempotencyKey || crypto.randomUUID();
        const requestKey = requestAttempt.key;
        // Tag the optimistic row with the request nonce so its saved twin reconciles by identity, not content.
        setMessages(prev => prev.filter(m => m.id === outId || !m.id.startsWith("local-out-") || m.client_request_id !== requestKey)
          .map(m => m.id === outId ? { ...m, client_request_id: requestKey } : m));
        const saved = {version: 1, scope: checkpointScope, nonce: requestKey, payload_digest: payloadDigest,
          ...(window.__SUPYMEM__?.mode === "local" ? {sent_at: resolved?.saved.nonce === requestKey ? resolved.saved.sent_at ?? Date.now() : Date.now()} : {})};
        const checkpoint = JSON.stringify(saved);
        window.localStorage.setItem(checkpointKey, checkpoint);
        if (window.localStorage.getItem(checkpointKey) !== checkpoint) throw new Error("chat_request_checkpoint_unavailable");
        const clearCheckpoint = () => {
          if (!current()) return;
          if (window.localStorage.getItem(checkpointKey) !== checkpoint) throw new Error("chat_request_checkpoint_changed");
          window.localStorage.removeItem(checkpointKey);
        };
        chatDispatched = true;
        const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat`, {
          method: "POST",
          signal: controller.signal,
          headers: { "Idempotency-Key": requestKey },
          body: JSON.stringify({...JSON.parse(requestPayload), idempotency_key: requestAttempt.key}),
        });
        if (!current() || !flight.current.owns(controller)) return false;
        if (!r.ok) {
          const code = apiErrorCode(r);
          if (r.status === 0 || r.status >= 500 || code === "account_changed_during_request" || code === "chat_action_failed"
              || code === "chat_request_in_progress" || code === "chat_request_outcome_unknown") {
            requestAttempt.uncertain = true;
            showCheckpoint({saved, raw: checkpoint,
              state: code === "chat_request_in_progress" ? "pending" : "unknown"}, {text: body, opts});
            window.setTimeout(() => void load(), 1000);
            // Keep the composer's exact draft available for reconciliation.
            return false;
          }
          // Admission refusal is known; an already uncertain turn keeps its key.
          if (!requestAttempt.uncertain) {
            clearCheckpoint();
            chatRequestRef.current.attempt = null;
          }
          setMessages((prev) => [
            ...prev.filter(message => message.id !== outId || requestAttempt?.uncertain),
            sendRefusal(r, apiFailure(r, "sending your message"), ventureId, latestManagerName(prev), body),
          ]);
          return false;
        }
        const data = r.data as ChatMessage | null;
        if (data && !conversationResponseMatches(data, conversationId)) {
          requestAttempt.uncertain = true;
          setMessages(prev => [...prev, {id: `local-err-${Date.now()}`, role: "system", body_md: "The reply does not match this chat. Refresh saved history before sending again."}]);
          return false;
        }
        if (data && typeof data === "object" && data.id && parseEmployeeStamp(data.body_md || "").text.trim()
            && (!data.role || data.role === "assistant")) {
          const started = await startRequestedToolBuild(data, ventureId, () => current() && flight.current.owns(controller));
          if (!current() || !flight.current.owns(controller)) return false;
          const grounded = await withLocalMacTaskObservation(started, ventureId);
          if (!current() || !flight.current.owns(controller)) return false;
          opts?.onReply?.(grounded);
          setMessages((prev) => {
            const next = mergeChat(prev.filter(message => message.id !== `local-uncertain-${requestKey}` && message.id !== `local-err-unsent-${requestKey}`),
              [{...grounded, role: grounded.role || "assistant", client_request_id: grounded.client_request_id || requestKey}], true);
            return wantHint ? appendMacTasksHint(next) : next;
          });
          if (grounded.kind === "idea_clarification_pending") startIdeaPoll(grounded.id, false);
          if (grounded.kind === "venture_renamed") {
            void api(`/api/ventures/${encodeURIComponent(ventureId)}`).then(result => {
              const venture = result.data as { id?: string; name?: string } | undefined;
              if (current() && result.ok && venture?.id === ventureId && typeof venture.name === "string" && venture.name.trim()) {
                window.dispatchEvent(new CustomEvent("supymem:venture-renamed", { detail: { ventureId, name: venture.name } }));
              }
            }).catch(() => undefined);
          }
          const href = grounded.kind === "navigation" ? grounded.actions?.[0]?.href : undefined;
          if (navigateOnResponse && sameVentureScreen(ventureId, href)) {
            beginChatNavigation(window.location.pathname, new URL(href, window.location.origin).pathname);
            router.push(href);
          }
          clearCheckpoint();
          chatRequestRef.current.attempt = null;
          if (window.__SUPYMEM__?.mode === "local") void load();
        } else {
          requestAttempt.uncertain = true;
          showCheckpoint({saved, raw: checkpoint,
            state: "unknown"}, {text: body, opts});
          if (wantHint) {
            setMessages((prev) => appendMacTasksHint(prev));
          }
          window.setTimeout(() => void load(), 400);
          return false;
        }
        return true;
      } catch (error) {
        if (controller.signal.aborted) return false;
        if (!current()) return false;
        if (!requestAttempt || (!chatDispatched && !requestAttempt.uncertain)) {
          if (requestAttempt && chatRequestRef.current.attempt === requestAttempt) chatRequestRef.current.attempt = null;
          setMessages(prev => [...prev, {id: `local-err-${Date.now()}`, role: "system",
            body_md: "Your message could not be sent. Choose Retry. If it still doesn't send, reopen your venture and try again.",
            actions: [{label: "Retry", action: "chat.reply", payload: {text: body, question_id: opts?.questionId, job_id: opts?.jobId}}]}]);
          dropOut();
          return false;
        }
        requestAttempt.uncertain = true;
        setMessages(prev => [...prev.filter(m => m.id !== outId), {id: `local-uncertain-${Date.now()}`, role: "system", kind: "confirmation",
          body_md: "The saved request could not be checked. Tap Show status before sending more.",
          actions: [{label: "Show status", action: "status"}]}]);
        return false;
      } finally {
        if (current() && requestAttempt && chatRequestRef.current.attempt === requestAttempt) requestAttempt.inFlight = false;
        if (ownsThinking && flight.current.owns(controller)) setThinking(false);
        flight.current.finish(controller);
      }
    },
    [load, navigateOnResponse, router, ventureId, conversationId, current, setThinking, setMessages, startIdeaPoll, checkpointScope, checkpointKey, resolveCheckpoint, showCheckpoint],
  );

  const runChatAction = useCallback(
    async (action: string, payload?: unknown) => {
      if (action === "referrals.show") return false;
      if (action === "chat.prefill") {
        if (!conversations.contextCurrent()) return false;
        const text = (payload as { text?: unknown } | null)?.text;
        if (typeof text !== "string" || !text.trim()) return false;
        applyChatPrefill(text);
        return true;
      }
      if (!current()) return false;
      if (action === "status" && window.__SUPYMEM__?.mode === "local" && typeof (payload as {nonce?: unknown} | null)?.nonce === "string") {
        return Boolean(await load());
      }
      if (action === "chat.new_conversation") return conversations.newConversation();
      if (action === "chat.stop") {
        const retryNonce = (payload as {nonce?: string} | null)?.nonce;
        const currentNonce = confirmationNonce.current || chatRequestRef.current.attempt?.key;
        const nonce = retryNonce || currentNonce;
        if (!retryNonce || retryNonce === currentNonce) {
          confirmationNonce.current = null;
          flight.current.stop();
          setThinking(false);
          if (!chatRequestRef.current.attempt?.key || chatRequestRef.current.attempt.key === nonce) {
            chatRequestRef.current.attempt = null;
          }
          try {
            const saved = JSON.parse(window.localStorage.getItem(checkpointKey) || "null");
            if (nonce && saved?.nonce === nonce) window.localStorage.removeItem(checkpointKey);
          } catch {}
        }
        const stopped = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat/stop`, {
          method: "POST", body: JSON.stringify({ conversation_id: conversationId,
            job_ids: (payload as {job_ids?: string[]} | null)?.job_ids || [], ...(nonce ? {nonce} : {}) }),
        });
        if (!current()) return false;
        setMessages(prev => [...prev, { id: `local-stop-${Date.now()}`, role: "system",
          body_md: stopped.ok ? "Stopped."
            : "Your message was stopped locally, but stopping saved work could not be confirmed. Tap Show status or press Stop again.",
          actions: stopped.ok ? [] : [{label: "Show status", action: "status"},
            {label: "Stop again", action: "chat.stop", payload: {job_ids: (payload as {job_ids?: string[]} | null)?.job_ids || [], nonce}}] }]);
        return stopped.ok;
      }
      if (action === "chat.startFresh") {
        const retry = payload as {checkpoint?: unknown; unsentNonce?: unknown; text?: unknown; opts?: ChatSendOptions} | null;
        if (typeof retry?.unsentNonce === "string") {
          const saved = messagesRef.current.find(message => message.id === `local-err-unsent-${retry.unsentNonce}`)
            ?.actions?.find(item => item.action === action)?.payload as CheckpointDraft | undefined;
          if (!saved) return false;
          return typeof saved.text === "string" && saved.text.trim()
            ? sendChat(saved.text, {...saved.opts, idempotencyKey: retry.unsentNonce}) : true;
        }
        if (typeof retry?.checkpoint !== "string" || chatRequestRef.current.attempt?.inFlight
            || window.localStorage.getItem(checkpointKey) !== retry.checkpoint) return false;
        if (!current() || window.localStorage.getItem(checkpointKey) !== retry.checkpoint) return false;
        window.localStorage.removeItem(checkpointKey);
        chatRequestRef.current.attempt = null;
        setMessages(prev => prev.filter(message => !message.id.startsWith("local-uncertain-")));
        if (typeof retry.text === "string" && retry.text.trim()) {
          return sendChat(retry.text, {...retry.opts, idempotencyKey: crypto.randomUUID()});
        }
        return true;
      }
      if (action === "chat.reply") {
        const { text, question_id, job_id } = (payload ?? {}) as { text?: unknown; question_id?: unknown; job_id?: unknown };
        if (typeof text === "string" && text.trim()) {
          const questionId = typeof question_id === "string" && question_id.trim() ? question_id : undefined;
          const jobId = typeof job_id === "string" && job_id.trim() ? job_id : undefined;
          return sendChat(text, questionId || jobId ? { ...(questionId ? { replyToQuestion: true, questionId } : {}), ...(jobId ? { jobId } : {}) } : undefined);
        }
        return;
      }
      if (action === "mac.task.prepare") {
        // Prepares the reviewed Mac task only; approval happens in the review.
        const goal = (payload as { text?: unknown } | null)?.text;
        if (typeof goal !== "string" || !goal.trim()) return;
        await sendChat(goal);
        return;
      }
      if (flight.current.busy()) return false;
      setThinking(true);
      const controller = flight.current.begin();
      const nonce = crypto.randomUUID();
      const replyAnchor = (payload as {reply_to_message_id?: string} | undefined)?.reply_to_message_id
        || (payload as {confirmation_message_id?: string} | undefined)?.confirmation_message_id || messagesRef.current.at(-1)?.id;
      setMessages(previous => previous.map(message => message.id === replyAnchor
        ? {...message, reply_request_ids: [...(message.reply_request_ids || []), nonce]} : message));
      confirmationNonce.current = nonce;
      try {
        if (action === "gtm.route.confirm") {
          const proposalId = (payload as { proposal_id?: unknown } | null)?.proposal_id;
          if (typeof proposalId !== "string") {
            setMessages(prev => [...prev, { id: `local-err-${Date.now()}`, role: "system",
              body_md: "The saved go-to-market proposal could not be verified. Refresh this conversation." }]);
            return;
          }
          const approved = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat/gtm/route/confirm`, {
            signal: controller.signal,
            method: "POST", body: JSON.stringify({ proposal_id: proposalId, ...(conversationId ? {conversation_id: conversationId} : {}) }),
          });
          if (!current() || !flight.current.owns(controller)) return false;
          await load();
          if (!approved.ok) setMessages(prev => [...prev, { id: `local-err-${Date.now()}`,
            role: "system", body_md: "Go-to-market confirmation did not complete. Refresh before trying again." }]);
          return;
        }
        if (action === "gtm.diagnosis.start") {
          const quoteMessageId = (payload as { quote_message_id?: unknown } | null)?.quote_message_id;
          if (typeof quoteMessageId !== "string") {
            setMessages(prev => [...prev, { id: `local-err-${Date.now()}`, role: "system",
              body_md: "The saved diagnosis quote could not be verified. Refresh this conversation." }]);
            return;
          }
          const approved = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat/gtm/confirm`, {
            signal: controller.signal,
            method: "POST", body: JSON.stringify({ quote_message_id: quoteMessageId, ...(conversationId ? {conversation_id: conversationId} : {}) }),
          });
          if (!current() || !flight.current.owns(controller)) return false;
          await load();
          if (!approved.ok) setMessages(prev => [...prev, { id: `local-err-${Date.now()}`,
            role: "system", body_md: gtmConfirmFailureMessage(approved.status, approved.data) }]);
          return;
        }
        if (action === "task.run") {
          const path = chatTaskRunPath(ventureId, payload);
          if (path) {
            const task = await api(path, { method: "POST", body: "{}", signal: controller.signal });
            if (!current() || !flight.current.owns(controller)) return false;
            if (task.ok) {
              await load();
              return;
            }
          }
          setMessages((prev) => [
            ...prev,
            {
              id: `local-err-${Date.now()}`,
              role: "system",
              body_md: "Couldn’t run that task. Please try again.",
            },
          ]);
          return;
        }
        if (isClientChatAction(action)) {
          setMessages(previous => [...previous, { id: `local-err-${Date.now()}`, role: "system",
            body_md: "You can continue this review in your original conversation. Nothing ran here.",
            actions: [{ label: "Open original conversation", href: `/new?venture=${encodeURIComponent(ventureId)}`, kind: "recovery_link" }] }]);
          return false;
        }
        const confirmationPayload = Object.fromEntries(Object.entries((payload || {}) as Record<string, unknown>)
          .filter(([key]) => !["confirmation_message_id", "reply_to_message_id"].includes(key)));
        const originalMessages = messagesRef.current;
        const r = await api(`/api/ventures/${ventureId}/chat/confirm`, {
          method: "POST",
          signal: controller.signal,
          headers: { "Idempotency-Key": nonce },
          body: JSON.stringify({ action,
            payload: confirmationPayload,
            message_id: (payload as { confirmation_message_id?: string } | undefined)?.confirmation_message_id,
            idempotency_key: nonce, ...(conversationId ? {conversation_id: conversationId} : {}) }),
        });
        if (!current() || !flight.current.owns(controller)) return false;
        if (r.ok && r.data && typeof r.data === "object") {
          const data = r.data as ChatMessage;
          if (!conversationResponseMatches(data, conversationId)) {
            setMessages(prev => [...prev, {id: `local-err-${Date.now()}`, role: "system", body_md: "The confirmation reply does not match this chat. Refresh its saved status."}]);
            return;
          }
          // A later success settles earlier "nothing ran" notes, which were only ever local.
          const started = await startRequestedToolBuild(data, ventureId, () => current() && flight.current.owns(controller));
          if (!current() || !flight.current.owns(controller)) return false;
          setMessages((prev) => mergeChat(prev.filter(message => !message.id.startsWith("local-err-")), [{...started, role: started.role || "assistant", client_request_id: started.client_request_id || nonce,
            reply_to_message_id: data.reply_to_message_id || replyAnchor}], true));
          window.dispatchEvent(new CustomEvent("supymem-chat-action-done", { detail: { ventureId } }));
          return true;
        }
        if (r.status === 402 && action !== "blocker.explain") {
          if (window.__SUPYMEM__?.mode === "local") {
            setMessages(previous => [...previous, { id: `local-err-${Date.now()}`, role: "system", body_md: usageLimitMessage(true),
              actions: [{ label: "Open your AI service settings", href: AI_SETTINGS_HREF }] }]);
            return false;
          }
          // Out of credits: show the step-by-step guide instead of a generic error.
          flight.current.finish(controller);
          setThinking(false);
          await runChatAction("blocker.explain", { code: "insufficient_credits" });
          return false;
        }
        if (chatActionPending(r.status, r.data)) {
          const identity = chatActionIdentity({label: "", action, payload: confirmationPayload});
          const originalState = originalMessages.find(message => message.id === replyAnchor)?.actions
            ?.find(item => item.confirmation && chatActionIdentity(item) === identity)?.confirmation?.state;
          const noticeId = `local-wait-${nonce}`;
          setMessages(previous => [...previous, {id: noticeId, role: "system", body_md: confirmFailureMessage(r.status, r.data)}]);
          setThinking(false);
          const deadline = Date.now() + 900_000;
          while (current() && flight.current.owns(controller) && Date.now() < deadline) {
            await new Promise(resolve => window.setTimeout(resolve, 5000));
            if (!current() || !flight.current.owns(controller)) break;
            const source = messagesRef.current.find(message => message.id === replyAnchor);
            const card = source?.actions?.find(item => item.confirmation && chatActionIdentity(item) === identity)?.confirmation;
            const state = source && card && confirmationState(messagesRef.current, source.id, action, card);
            if (messagesRef.current.some(message => message.role === "assistant" && message.client_request_id === nonce)
                || state !== originalState && (state === "started" || state === "not_started")) {
              setMessages(previous => previous.filter(message => message.id !== noticeId));
              return;
            }
          }
          throw new ChatActionPendingError();
        }
        setMessages((prev) => [
          ...prev,
          {
            id: `local-err-${Date.now()}`,
            role: "system",
            body_md: confirmFailureMessage(r.status, r.data),
            actions: confirmFailureActions(r.data, prev),
          },
        ]);
        return false;
      } finally {
        if (flight.current.owns(controller)) setThinking(false);
        if (confirmationNonce.current === nonce) confirmationNonce.current = null;
        flight.current.finish(controller);
      }
    },
    [load, sendChat, ventureId, conversationId, current, router, setThinking, setMessages, checkpointKey, conversations.newConversation, conversations.contextCurrent],
  );

  /** Resolves false when the action failed (error shown in chat), so its button can be re-enabled. */
  const confirmChatAction = useCallback(async (action: string, payload?: unknown): Promise<boolean> => {
    if (!current() && action !== "chat.prefill") return false;
    const started = Date.now();
    try {
      const result = await runChatAction(action, payload);
      if (result === false) return false;
      if (result === true && current()) return true;
    } catch (cause) {
      if (cause instanceof ChatActionPendingError) throw cause;
      return false;
    }
    if (action === "chat.prefill") return true;
    await new Promise((resolve) => setTimeout(resolve, 0)); // let the error message commit
    return current() && !failedSince(messagesRef.current, started);
  }, [runChatAction, current]);

  // The manager (default "Charlie") answers unless a specialist is @mentioned.
  const managerName = latestManagerName(messages);

  return {
    ...conversations,
    contextCurrent: current,
    messages,
    loadEarlier,
    hasEarlier: !!nextHistoryCursor,
    historyLoading,
    historyReady,
    historyError,
    reloadHistory,
    sendChat,
    confirmChatAction,
    thinking: thinking || !!deepProgress,
    managerName,
    streaming: thinking || deepProgress
      ? { messageId: "thinking", text: "", speaker: managerName, ...deepProgress }
      : undefined,
  };
}
