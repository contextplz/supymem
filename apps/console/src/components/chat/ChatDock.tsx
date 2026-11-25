"use client";

import { useActivityRefresh, watchActivity } from "@/lib/activityRefresh";

import { ChatAnchor } from "@/components/chat/ChatLink";
import { pageResultKind } from "./PageResultLink";
import { BuildPageCard } from "@/components/site/BuildPageCard";
import { LandingPreviewFrame } from "@/components/cockpit/LandingPreviewFrame";

import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import { ChatMessageRow, ChatMessageText, ChatPanel, ChatWorking, useChatSelection } from "./ChatPanel";
import { readReloadScroll } from "@/components/desktop/ConsoleBuildUpdate";
import { StartupReview } from "./StartupReview";
import { MemoryConflict, memoryConflict } from "./MemoryConflict";
import { MarketBeliefs } from "./MarketBeliefs";
import { ExplorationCard } from "./ExplorationCard";
import { InboxBriefing } from "./InboxBriefing";
import { useInboxBriefing } from "@/components/settings/useInboxBriefing";
import { GoalCard } from "./GoalCard";
import { useFeatureVisibility, useLocalMode } from "@/components/layout/LocalModeBoundary";
import { applyChatPrefill, PREFILL_ACTION, prefillChat } from "@/lib/chatPrefill";
import type { ChatMessage } from "@/lib/api-types";
import { cx } from "@/lib/cx";
import { splitFences } from "@/lib/chatMarkdown";
import { dedupeParagraphs } from "@/lib/chatBrief";
import { humanizeErrorCodes } from "@/lib/errorCopy";
import { founderReviewText } from "@/lib/validationCopy";
import { founderWords } from "@/lib/chatEntry";
import { chatRouteAction, inChatActions, isChatNavigationAction } from "@/lib/chatRouteAction";
import { isMacReviewHref } from "@/lib/usageControl";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import { EmployeePortrait, parseEmployeeStamp } from "@/components/office/avatars";
import { CharlieMascot } from "./CharlieDock";
import { setCharlieMascot } from "@/lib/charlieMascot";
import { useOfficeOptional } from "@/components/office/OfficeProvider";
import {
  DOCK_HEIGHT_DEFAULT,
  DOCK_WIDTH_DEFAULT,
  clampDockSize,
  nudgeDockSize,
  composerPlaceholder,
  readStoredDockSize,
  chatDraftKey,
  writeStoredDockSize,
  type DockSize,
} from "@/lib/chatComposer";
import { MentionField } from "./MentionField";
import { MacTasksEntry } from "./MacTasksEntry";
import { DictateButton } from "./DictateButton";
import { IntakeAttachments, useIntakeAttachments } from "./IntakeAttachments";
import { composeIdeaAnswer, leavesIdeaQuestion, questionSelectionMode, savedIdeaChoiceAnswer, toggleIdeaChoice } from "@/lib/ideaAnswer";
import { usePathname } from "next/navigation";
import type { VoiceSend } from "./useVoiceChat";
import type { VoiceScreen } from "@/lib/voice";
import { VoiceMic } from "./VoiceMic";
import { RecoveryResultForm } from "./RecoveryResultForm";
import { IdeaReferences } from "./IdeaReferences";
import { renderDiagramPart } from "./DiagramCard";
import { useChatComposer } from "./useChatComposer";
import { planSend, takeNext } from "@/lib/chatSendQueue";
import { EmergencyStopButton } from "@/components/desktop/EmergencyStopButton";
import { DesktopNoticeBanner } from "@/components/desktop/DesktopNoticeBanner";
import { chatStopApplies, emergencyStop } from "@/lib/emergencyStop";
import { useComputerUseActive } from "@/lib/useComputerUseActive";
import { answeredState, confirmationState, settleClick } from "@/lib/chatAnswered";
import { isNearBottom, readScrollAnchor, restoreScrollAnchor, scrollDecision } from "@/lib/chatScroll";
import { historyScrollPosition, type HistoryScrollAnchor } from "@/lib/chatHistoryScroll";
import styles from "./ChatDock.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { api } from "@/lib/client";
import { ChatOutreachReview } from "./ChatOutreachReview";
import { ApprovalPolicyControls, type ApprovalPolicy } from "./ApprovalPolicyControls";
import { reportUserError } from "@/lib/userError";
import { ChatActionPendingError } from "@/lib/chatConfirmError";
import { jobFinished, type ResearchProgress as Progress } from "@/lib/durableJobs";
import { ResearchProgress } from "@/components/primitives/ResearchProgress";
import { AgentsTray, browseProgressFor, collapseAgentStatuses, useAgentJobs } from "./AgentsTray";
import { chatActionIdentity, founderAction, isQuestionReply, latestPageMessage, liveActions, planChatActions } from "./chatActionPolicy";

type SearchProgress = Progress & { job_id: string };

function collapseSearchProgress(messages: ChatMessage[]): ChatMessage[] {
  const seen = new Set<string>();
  return [...messages].reverse().filter(message => {
    const reference = message.role === "assistant" && message.kind === "gtm_search_progress" ? message.gtm_search_progress : null;
    const keys = [reference?.job_id && `job:${reference.job_id}`, reference?.run_id && `run:${reference.run_id}`].filter(Boolean) as string[];
    const duplicate = keys.some(key => seen.has(key));
    keys.forEach(key => seen.add(key));
    return !duplicate;
  }).reverse();
}

function searchProgressFor(message: ChatMessage, snapshots: SearchProgress[]): SearchProgress | undefined {
  const reference = message.gtm_search_progress;
  if (message.role !== "assistant" || message.kind !== "gtm_search_progress" || !reference) return;
  return snapshots.find(progress => (reference.job_id ? reference.job_id === progress.job_id : !!reference.run_id && reference.run_id === progress.run_id)
    && (!reference.run_id || reference.run_id === progress.run_id));
}

function speakerName(role: string, stamped?: { name: string } | null): string {
  if (stamped?.name) return stamped.name;
  if (role === "founder" || role === "user") return "You";
  if (role === "system") return "Your update";
  if (role === "assistant") return "Charlie";
  return role;
}

/** Provider authorization links (for example Connect Gmail) open in the real browser, not inside the app view. */
function externalLinkProps(href: string) {
  const artifact = /^\/api\/ventures\/[A-Za-z0-9_-]+\/(?:pagebuilder\/jobs\/[A-Za-z0-9_-]+\/(preview|source)(?:\?variant=[0-2])?|device-work-orders\/[A-Za-z0-9_-]+\/source-archive)$/.exec(href);
  if (artifact) return artifact[1] === "preview" ? {target: "_blank", rel: "noopener noreferrer"} : {download: true};
  if (!/^https?:\/\//i.test(href)) return {};
  return {
    target: "_blank",
    rel: "noopener noreferrer",
    onClick: (e: { preventDefault: () => void }) => {
      try {
        const u = new URL(href);
        if (isDesktopShell() && u.hostname === "connect.composio.dev" && u.pathname.startsWith("/link/")) {
          e.preventDefault();
          void desktopInvokeOrThrow("open_composio_connect_link", { url: href });
        }
      } catch { /* fall through to the normal link */ }
    },
  };
}

function actionKey(message: ChatMessage | undefined, action: NonNullable<ChatMessage["actions"]>[number]) {
  return `${message?.id}:${chatActionIdentity(action)}${action.state_hash ? `:${action.state_hash}` : ""}`;
}

export function ChatDock({
  messages,
  streaming,
  onSend,
  onVoiceSend,
  onAction,
  onRecoverDraft,
  disabled,
  collapsed,
  onToggle,
  variant = "dock",
  liveLines,
  hideComposer = false,
  showMacTasks = false, // Native Mac tasks are not in the beta; opt in per surface.
  managerName,
  ventureId,
  subject,
  conversationId,
  voiceConversationId,
  resizable = false,
  autoFocus = true,
  resizeHeight = true,
  size,
  onSizeChange,
  onLoadEarlier,
  hasEarlier = false,
  historyLoading = false,
  historyReady = true,
  historyError,
  onReloadHistory,
  voiceScreen,
  onQuestionBack,
  draftPrefill,
  visible = true,
}: {
  messages: ChatMessage[];
  streaming?: { messageId: string; text: string; speaker?: string; spendMicro?: number; capMicro?: number };
  onSend: (body: string) => void | Promise<void | boolean | { ok: boolean }>;
  onVoiceSend?: VoiceSend;
  onAction?: (action: string, payload?: unknown) => void | boolean | Promise<void | boolean>;
  onRecoverDraft?: (text?: string) => void;
  disabled?: { reason: string };
  collapsed?: boolean;
  onToggle?: () => void;
  variant?: "dock" | "panel";
  liveLines?: Array<{ id: string; text: string }>;
  hideComposer?: boolean;
  showMacTasks?: boolean;
  managerName?: string;
  ventureId?: string;
  subject?: string;
  conversationId?: string | null;
  voiceConversationId?: string | null;
  resizable?: boolean;
  autoFocus?: boolean;
  resizeHeight?: boolean;
  size?: DockSize;
  onSizeChange?: (size: DockSize) => void;
  onLoadEarlier?: () => void | Promise<void>;
  hasEarlier?: boolean;
  historyLoading?: boolean;
  historyReady?: boolean;
  historyError?: string;
  onReloadHistory?: () => void | Promise<void>;
  voiceScreen?: VoiceScreen;
  onQuestionBack?: () => void | Promise<void>;
  draftPrefill?: {id: string; text: string; questionId?: string};
  visible?: boolean;
}) {
  const local = useLocalMode();
  const featureVisible = useFeatureVisibility();
  const visibleAction = (action: NonNullable<ChatMessage["actions"]>[number]) =>
    featureVisible("actions", action.confirmation?.action || action.action || action.name || "")
    && (!action.href || featureVisible("paths", action.href));
  const [buildStatusScope, setBuildStatusScope] = useState<string | null>(null);
  const buildStatusRef = useRef<HTMLDivElement | null>(null);
  const pathname = usePathname();
  const scope = useRef(chatDraftKey(subject, ventureId, conversationId));
  scope.current = chatDraftKey(subject, ventureId, conversationId);
  const listRef = useRef<HTMLDivElement | null>(null);
  const historyAnchor = useRef<HistoryScrollAnchor | null>(null);
  const viewportAnchor = useRef<{scope: string | null; anchor: ReturnType<typeof readScrollAnchor>} | null>(null);
  const firstMessageId = useRef<string | undefined>(undefined);
  const followLatest = useRef(true);
  const prevCount = useRef(0);
  const lastMessageId = useRef<string | undefined>(undefined);
  const firstVisibleId = useRef<string | undefined>(undefined);
  const isSelecting = useChatSelection();
  const [newBelow, setNewBelow] = useState(false);
  // Chips/confirm buttons clicked in this session: disabled at once, before the server round trip returns.
  // A failed action (false/throw) releases its button so the founder can retry; the error shows in chat.
  const [clicked, setClicked] = useState<Set<string>>(() => new Set());
  const [deniedPolicies, setDeniedPolicies] = useState<Set<string>>(() => new Set());
  const [senderReviews, setSenderReviews] = useState<Record<string, boolean>>({});
  const actionFlights = useRef(new Set<string>());
  const actionQueue = useRef<Promise<unknown> | null>(null);
  const [pendingActions, setPendingActions] = useState<Set<string>>(() => new Set());
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  useEffect(() => {
    const askWithoutVenture = (event: Event) => {
      if (ventureId || !visible) return;
      const detail = (event as CustomEvent<{ action?: string; payload?: { text?: unknown } }>).detail;
      if (detail?.action === PREFILL_ACTION && typeof detail.payload?.text === "string") applyChatPrefill(detail.payload.text);
    };
    window.addEventListener("supymem:ask", askWithoutVenture);
    return () => window.removeEventListener("supymem:ask", askWithoutVenture);
  }, [ventureId, visible]);
  async function trackAction(key: string, action: string, payload: unknown, unavailable?: string) {
    if (action === "status" && local && !unavailable) {
      setBuildStatusScope(workKey);
    }
    if (unavailable || !action || !onAction && action !== "chat.prefill") {
      const reason = unavailable || "You cannot use this action here. Ask me for your next step.";
      reportUserError({message: reason, source: "chat.unhandled_action", intent: "chat_action", serverCode: "unhandled_chat_action"});
      setActionErrors(previous => ({ ...previous, [key]: reason }));
      return;
    }
    if (action === "chat.prefill") {
      const text = (payload as { text?: unknown } | null)?.text;
      if (typeof text !== "string" || !text.trim()) {
        setActionErrors(previous => ({ ...previous, [key]: "Type your request below to get a fresh card." }));
        inputRef.current?.focus();
        return;
      }
      applyChatPrefill(text);
      inputRef.current?.focus();
      return;
    }
    if (!onAction || disabled || actionFlights.current.has(key)) return;
    actionFlights.current.add(key);
    setClicked(previous => new Set(previous).add(key));
    setPendingActions(previous => new Set(previous).add(key));
    setActionErrors(previous => ({ ...previous, [key]: "" }));
    const owner = scope.current;
    const text = (payload as {text?: unknown} | null)?.text;
    const retryText = typeof text === "string" ? text : undefined;
    try {
      // Tapping several buttons quickly runs them in order; chat handles one request at a time.
      const running = actionQueue.current ? actionQueue.current.then(() => onAction(action, payload)) : onAction(action, payload);
      const settled: Promise<unknown> = Promise.resolve(running).catch(() => undefined).finally(() => {
        if (actionQueue.current === settled) actionQueue.current = null;
      });
      actionQueue.current = settled;
      if (await running === false) {
        actionFlights.current.delete(key);
        setClicked(previous => settleClick(previous, key, false));
      } else if (action === "chat.startFresh" && scope.current === owner) {
        if (onRecoverDraft) onRecoverDraft(retryText);
        else {
          if (retryText) composer.acknowledgeDraft(retryText);
          inputRef.current?.focus({preventScroll: true});
        }
      }
    } catch (cause) {
      actionFlights.current.delete(key);
      setClicked(previous => settleClick(previous, key, false));
      if (!(cause instanceof ChatActionPendingError)) setActionErrors(previous => ({ ...previous, [key]: cause instanceof Error ? cause.message : "Could not start this action. Try again." }));
    } finally {
      setPendingActions(previous => settleClick(previous, key, false));
    }
  }
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const attachments = useIntakeAttachments(`${subject}:${ventureId}:${conversationId}`, ventureId);
  const office = useOfficeOptional();
  const mentionPrefix = office?.mentionPrefix || "";
  const setMentionPrefix = office?.setMentionPrefix;
  const narrow =
    typeof window !== "undefined" && window.matchMedia("(max-width: 600px)").matches;
  const composer = useChatComposer(onSend, chatDraftKey(subject, ventureId, conversationId), !historyReady, inputRef, messages);
  const computerUseActive = useComputerUseActive();
  // A message typed while the previous reply is still "Sending…" waits its turn instead of being dropped.
  const waiting = useRef<string[]>([]);
  const [waitingCount, setWaitingCount] = useState(0);
  const { pending: sending, submit: sendNow } = composer;
  useEffect(() => {
    if (!visible) return;
    setCharlieMascot(streaming?.text && /^charlie$/i.test(streaming.speaker || "Charlie") ? "speaking" : composer.draft.trim() ? "listening" : "idle");
  }, [visible, streaming?.text, streaming?.speaker, composer.draft]);
  useEffect(() => () => { if (visible) setCharlieMascot("idle"); }, [visible]);
  const [searchSnapshots, setSearchSnapshots] = useState<{scope: string; items: SearchProgress[]}>({scope: "", items: []});
  const activeJobs = useRef<{scope: string; ids: string[]}>({scope: "", ids: []});
  const stopEpoch = useRef(0);
  const workKey = JSON.stringify([subject, ventureId, voiceConversationId || conversationId]);
  const activity = useActivityRefresh(ventureId);
  const watchWork = visible && (!local || historyReady || !!historyError);
  const stopWork = () => {
    stopEpoch.current++;
    waiting.current = [];
    setWaitingCount(0);
    composer.stop();
    void onAction?.("chat.stop", {job_ids: activeJobs.current.scope === workKey ? activeJobs.current.ids : []});
    inputRef.current?.focus();
  };
  useEffect(() => {
    if (!watchWork || !ventureId || !onAction) return;
    let current = true;
    let timer: number | undefined;
    const conversation = voiceConversationId || conversationId;
    const poll = async () => {
      if (!current) return;
      const epoch = stopEpoch.current;
      let delay = 15000;
      try {
        const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat/work${conversation ? `?conversation_id=${encodeURIComponent(conversation)}` : ""}`);
        if (current && epoch === stopEpoch.current && result.ok) {
          const ids = ((result.data as {items?: {id: string}[]})?.items || []).map(item => item.id);
          activeJobs.current = {scope: workKey, ids};
          delay = ids.length ? 2000 : 15000;
          const progress = (result.data as {research_progress?: SearchProgress | null})?.research_progress;
          if (progress) setSearchSnapshots(previous => ({scope: workKey,
            items: [...(previous.scope === workKey ? previous.items.filter(item => item.job_id !== progress.job_id) : []), progress]}));
        }
      } finally {
        if (current && !local) timer = window.setTimeout(() => void poll(), delay);
      }
    };
    const stop = local ? watchActivity(activity, poll) : undefined;
    if (!local) void poll();
    return () => { current = false; stop?.(); window.clearTimeout(timer); };
  }, [activity, local, watchWork, ventureId, workKey, voiceConversationId, conversationId]);
  const [choiceDrafts, setChoiceDrafts] = useState<Record<string, string[]>>({});
  const [choiceError, setChoiceError] = useState("");
  const choiceFlights = useRef(new Set<string>());
  const typing = useRef({ draft: composer.draft, setDraft: composer.setDraft });
  typing.current = { draft: composer.draft, setDraft: composer.setDraft };
  useEffect(() => {
    if (!draftPrefill) return;
    const question = messages.find(message => message.id === draftPrefill.questionId);
    const restored = savedIdeaChoiceAnswer(draftPrefill.text, question ? questionOptionsFor(question).map(action => action.text || action.label) : []);
    typing.current.setDraft(restored.freeText);
    if (question) {
      setChoiceDrafts(previous => ({...previous, [question.id]: restored.selected}));
      choiceFlights.current.delete(question.id);
      setClicked(previous => settleClick(previous, `${question.id}:answer`, false));
    }
  }, [draftPrefill?.id]);
  useEffect(() => {
    if (sending) return;
    const { next, queue } = takeNext(waiting.current);
    if (next === null) return;
    waiting.current = queue;
    setWaitingCount(queue.length);
    const keep = typing.current.draft; // sending a queued message must not wipe what is being typed now
    void sendNow(next);
    if (keep) typing.current.setDraft(keep);
  }, [sending, sendNow]);
  const [macOpen, setMacOpen] = useState(false);
  const dockHeight = size?.height ?? DOCK_HEIGHT_DEFAULT;
  const [visibleCount, setVisibleCount] = useState(24);
  const inboxBriefing = useInboxBriefing(ventureId);
  const agents = useAgentJobs(ventureId || "", watchWork, onReloadHistory);
  const allMessages = collapseAgentStatuses(collapseSearchProgress(messages.filter(message => message.kind !== "entry_pending")), inboxBriefing.enabled, agents.jobs, ventureId);
  const latestPage = latestPageMessage(messages);
  const firstVisibleIndex = followLatest.current && !isSelecting() ? -1 : allMessages.findIndex(message => message.id === firstVisibleId.current);
  const visibleMessages = allMessages.slice(firstVisibleIndex < 0 ? -visibleCount : Math.min(firstVisibleIndex, Math.max(0, allMessages.length - visibleCount)))
    .map(message => local ? { ...message, actions: message.actions?.filter(visibleAction),
      next_moves: message.next_moves ? { ...message.next_moves, actions: message.next_moves.actions?.filter(visibleAction) } : message.next_moves } : message);
  const announcedMessages = useRef<{ scope: string; ids: Set<string>; ready: boolean }>({ scope: "", ids: new Set(), ready: false });
  const [replyAnnouncement, setReplyAnnouncement] = useState("");
  useEffect(() => {
    const saved = messages.filter(message => message.id !== streaming?.messageId && message.kind !== "entry_pending");
    if (announcedMessages.current.scope !== workKey || !announcedMessages.current.ready || !historyReady || historyLoading || !visible) {
      announcedMessages.current = { scope: workKey, ids: new Set(saved.map(message => message.id)), ready: historyReady && !historyLoading && visible };
      setReplyAnnouncement("");
      return;
    }
    const replies = saved.filter(message => message.role === "assistant" && !announcedMessages.current.ids.has(message.id));
    for (const message of saved) announcedMessages.current.ids.add(message.id);
    if (replies.length) setReplyAnnouncement(replies.map(message => parseEmployeeStamp(message.body_md || "").text).join("\n"));
  }, [messages, historyReady, historyLoading, visible, workKey, streaming?.messageId]);

  useEffect(() => {
    if (!resizable || !autoFocus || hideComposer || !visible) return;
    const frame = requestAnimationFrame(() => { if (!isSelecting()) inputRef.current?.focus({ preventScroll: true }); });
    return () => cancelAnimationFrame(frame);
  }, [resizable, autoFocus, hideComposer, visible, isSelecting]);

  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el || !visible) return;
    firstVisibleId.current = visibleMessages[0]?.id;
    const reloadScroll = readReloadScroll(scope.current);
    if (reloadScroll) {
      if (!historyReady) return;
      if (reloadScroll.count > visibleCount) {
        setVisibleCount(reloadScroll.count);
        return;
      }
      if (reloadScroll.loaded > messages.length && hasEarlier) {
        if (!historyLoading) void onLoadEarlier?.();
        return;
      }
      el.scrollTop = reloadScroll.top;
      followLatest.current = isNearBottom(el);
      lastMessageId.current = messages.at(-1)?.id;
      prevCount.current = messages.length;
      try { window.sessionStorage.removeItem(`console.reload.scroll:${scope.current}`); } catch {}
      return;
    }
    const anchor = historyAnchor.current;
    if (anchor) {
      lastMessageId.current = messages.at(-1)?.id;
      prevCount.current = messages.length;
      const restored = historyScrollPosition(anchor, visibleMessages[0]?.id, el.scrollHeight);
      if (restored !== null) {
        el.scrollTop = restored;
        historyAnchor.current = null;
      }
      return;
    }
    const last = messages.at(-1);
    const decision = scrollDecision({
      // A pending reply replaced by its answer keeps the count; an anchored reply can land above the last row; prepended history is not new.
      newMessage: messages.length >= prevCount.current && (last?.id !== lastMessageId.current
        || (messages.length > prevCount.current && messages[0]?.id === firstMessageId.current)), following: followLatest.current, selecting: isSelecting(),
      own: (last?.role === "founder" || last?.role === "user") && !last.id.startsWith("local-out-"), streaming: Boolean(streaming?.text),
    });
    lastMessageId.current = last?.id;
    firstMessageId.current = messages[0]?.id;
    prevCount.current = messages.length;
    if (decision === "follow") {
      followLatest.current = true;
      el.scrollTop = el.scrollHeight;
      setNewBelow(false);
    } else {
      if (viewportAnchor.current?.scope === scope.current) restoreScrollAnchor(el, viewportAnchor.current.anchor);
      if (decision === "pill") setNewBelow(true);
    }
    viewportAnchor.current = {scope: scope.current, anchor: followLatest.current && !isSelecting() ? null : readScrollAnchor(el)};
  }, [messages, visibleCount, streaming?.text, liveLines?.length, composer.pending, historyReady, hasEarlier, historyLoading, onLoadEarlier, subject, ventureId, conversationId, isSelecting, visible]);

  // Cards load and the tray resizes after a render: keep the bottom in view, or the reader on the same bubble. Idle polls resize nothing.
  useEffect(() => {
    const el = listRef.current;
    if (!el || !visible || typeof ResizeObserver === "undefined") return;
    let size = [el.scrollHeight, el.clientHeight].join();
    const observer = new ResizeObserver(() => {
      const next = [el.scrollHeight, el.clientHeight].join();
      if (next === size) return;
      size = next;
      if (isSelecting()) return;
      if (followLatest.current) el.scrollTop = el.scrollHeight;
      else if (viewportAnchor.current?.scope === scope.current) restoreScrollAnchor(el, viewportAnchor.current.anchor);
    });
    observer.observe(el);
    Array.from(el.children).forEach(child => observer.observe(child));
    return () => observer.disconnect();
  }, [visible, visibleMessages.length, isSelecting]);

  useEffect(() => {
    if (!historyLoading && (historyError || !hasEarlier)) historyAnchor.current = null;
  }, [historyLoading, historyError, hasEarlier]);

  useEffect(() => {
    const talk = () => requestAnimationFrame(() => inputRef.current?.focus());
    window.addEventListener?.("supymem-chat-talk", talk);
    return () => window.removeEventListener?.("supymem-chat-talk", talk);
  }, []);

  useEffect(() => {
    if (!mentionPrefix) return;
    const next = composer.draft.startsWith(mentionPrefix)
      ? composer.draft
      : `${mentionPrefix}${composer.draft}`;
    composer.setDraft(next);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    });
    setMentionPrefix?.("");
    // Only react to a new mention prefix from the office rail.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mentionPrefix, setMentionPrefix]);

  const startResize = useCallback(
    (edge: "width" | "height", event: ReactPointerEvent<HTMLButtonElement>) => {
      if (!resizable || narrow || !size || !onSizeChange) return;
      event.preventDefault();
      const startX = event.clientX;
      const startY = event.clientY;
      const origin = { ...size };
      const applySize = onSizeChange;
      const target = event.currentTarget;
      const panel = target.closest<HTMLElement>(".chat-panel");
      if (panel) panel.dataset.resizing = "true";
      target.setPointerCapture(event.pointerId);
      function onMove(ev: PointerEvent) {
        if (edge === "width") {
          // Left edge: dragging left increases width.
          applySize(clampDockSize({ ...origin, width: origin.width + (startX - ev.clientX) }));
        } else {
          applySize(clampDockSize({ ...origin, height: origin.height + (startY - ev.clientY) }));
        }
      }
      function onUp(ev: PointerEvent) {
        target.releasePointerCapture(ev.pointerId);
        if (panel) delete panel.dataset.resizing;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
      }
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    },
    [narrow, onSizeChange, resizable, size],
  );

  function onHandleKey(edge: "width" | "height", event: KeyboardEvent<HTMLButtonElement>) {
    if (!size || !onSizeChange) return;
    const step = event.shiftKey ? 24 : 12;
    if (edge === "width") {
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        onSizeChange(nudgeDockSize(size, "width", step));
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        onSizeChange(nudgeDockSize(size, "width", -step));
      }
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      onSizeChange(nudgeDockSize(size, "height", step));
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      onSizeChange(nudgeDockSize(size, "height", -step));
    }
  }

  const waitingForReply = sending || !!streaming && !streaming.text;
  const waitKey = JSON.stringify([subject, ventureId, conversationId]);
  const [longWaitKey, setLongWaitKey] = useState<string | null>(null);
  useEffect(() => {
    setLongWaitKey(null);
    if (!waitingForReply) return;
    let active = true;
    const timer = setTimeout(() => { if (active) setLongWaitKey(waitKey); }, 4000);
    return () => { active = false; clearTimeout(timer); };
  }, [waitingForReply, waitKey]);
  const waitingLonger = waitingForReply && longWaitKey === waitKey;
  const home = useContext(FounderUIContext) === "home";

  const isPanel = variant === "panel";
  const homePanel = home && isPanel;
  const blocked = !!disabled || composer.pending;
  const refusal = composer.refusal;

  function questionOptionsFor(message: ChatMessage) {
    if (message.kind === "error" || !parseEmployeeStamp(message.body_md || message.text || "").text.trim()) return [];
    return (message.actions || []).filter(action => !action.href && action.label !== "Skip the questions" && (action.action || action.name) === "chat.reply" &&
      typeof (action.payload as {question_id?: unknown} | null)?.question_id === "string" && Boolean((action.payload as {question_id: string}).question_id.trim()));
  }
  const recommendedChoices = (options: ReturnType<typeof questionOptionsFor>) => options.filter((action) =>
    (action.payload as { recommended?: boolean } | null)?.recommended).map((action) => action.text || action.label);
  const selectedFor = (message: ChatMessage) => choiceDrafts[message.id] ?? [];
  const detailPromptFor = (message: ChatMessage, selected: string[]) => {
    const option = questionOptionsFor(message).find(action => selected.includes(action.text || action.label) &&
      (action.payload as { requires_detail?: boolean } | null)?.requires_detail);
    return option ? (option.payload as { detail_prompt?: string }).detail_prompt || "Describe your example: who, when, what happened?" : "";
  };
  const answerFor = (_message: ChatMessage, selected: string[], text: string, assumed = true) =>
    composeIdeaAnswer(selected, text, assumed || selected.length > 0);
  const lastQuestionIndex = messages.findLastIndex((message) => questionOptionsFor(message).length > 0);
  const activeQuestion = lastQuestionIndex >= 0 && !messages.slice(lastQuestionIndex + 1).some((message) => ["founder", "user"].includes(message.role))
    ? messages[lastQuestionIndex] : null;
  if (collapsed) {
    return (
      <button type="button" className="ghost sm" onClick={onToggle}>
        Chat
      </button>
    );
  }
  const submitLabel = activeQuestion ? "Send" : composer.pending ? (waitingCount ? `Queue (${waitingCount} waiting)` : "Queue") : "Send";
  const preparedMessage = [...visibleMessages].reverse().find((message) => (message.actions || []).some((action) =>
    (action.style === "primary" || action.confirmation) && !clicked.has(actionKey(message, action))));
  const preparedActions = (preparedMessage?.actions || []).filter((action) =>
    (action.style === "primary" || action.confirmation) && !clicked.has(actionKey(preparedMessage, action))).slice(0, 3).map((action) => action.label);
  const voiceWaiting = blocked || Boolean(activeQuestion && clicked.has(`${activeQuestion.id}:answer`));
  async function submitChoices(message: ChatMessage, selected: string[], text: string, assumed = false) {
    const answer = answerFor(message, selected, text, assumed);
    if (!answer || blocked || choiceFlights.current.has(message.id)) return false;
    const option = questionOptionsFor(message)[0];
    if (!option) return false;
    if (answer.length > 4000) { setChoiceError("That answer is too long. Shorten your text or choose fewer options."); return false; }
    setChoiceError("");
    const owner = scope.current;
    const key = `${message.id}:answer`;
    (hideComposer ? listRef.current : inputRef.current)?.focus({ preventScroll: true });
    choiceFlights.current.add(message.id); setClicked((previous) => new Set(previous).add(key));
    // The answer moves into the thread now; the box must not keep showing it while the reply loads.
    if (typing.current.draft === text) typing.current.setDraft("");
    try {
      const ok = onAction ? await onAction("chat.reply", { ...(option.payload as Record<string, unknown>), text: answer }) : await composer.submit(answer);
      if (ok === false) throw new Error("answer failed");
      return true;
    } catch {
      choiceFlights.current.delete(message.id); setClicked((previous) => settleClick(previous, key, false));
      if (scope.current === owner) {
        if (text && !typing.current.draft) typing.current.setDraft(text);
        setChoiceError(`Your answer was not sent yet. It is back in the box; press ${submitLabel.toLowerCase()} to try again.`);
      }
      return false;
    }
  }

  async function submit(body?: string) {
    // "stop" / "emergency stop" while computer use runs: the same one-press stop, never queued behind a reply.
    if (chatStopApplies(body ?? composer.draft, true, computerUseActive)) {
      inputRef.current?.focus({ preventScroll: true });
      composer.setDraft("");
      void emergencyStop("chat").catch(() => undefined);
      return true;
    }
    // A typed command ("skip these, find me people") is a new instruction, sent as normal chat.
    if (activeQuestion && !leavesIdeaQuestion(body ?? composer.draft)) return submitChoices(activeQuestion, selectedFor(activeQuestion), body ?? composer.draft);
    const planned = planSend(composer.pending, waiting.current, body ?? composer.draft);
    if (!planned.sendNow) {
      if (planned.queue.length !== waiting.current.length) {
        inputRef.current?.focus({ preventScroll: true });
        waiting.current = planned.queue;
        setWaitingCount(planned.queue.length);
        composer.setDraft("");
      }
      return true;
    }
    inputRef.current?.focus({ preventScroll: true });
    return composer.submit(body);
  }

  const streamingBody = streaming && (streaming.text ? (
    <div className={cx(styles.msgText, styles.caret)}>{streaming.text}</div>
  ) : waitingLonger ? (
    <div className={styles.msgText} role="status">Thinking a little longer…</div>
  ) : (
    <div className={cx(styles.msgText, styles.thinking)} aria-hidden>
      <span className={styles.dot} />
      <span className={styles.dot} />
      <span className={styles.dot} />
    </div>
  ));

  useEffect(() => {
    if (buildStatusScope !== workKey) return;
    buildStatusRef.current?.focus({preventScroll: true});
    buildStatusRef.current?.scrollIntoView({block: "nearest"});
  }, [buildStatusScope, workKey]);
  return (
    <ChatPanel panel={isPanel} data-webview-pending={visible && !historyReady && !historyError ? "conversation_load" : undefined} data-console-reload-blocked={!!composer.draft || sending || !!streaming || waitingCount > 0 || pendingActions.size > 0 || attachments.uploading || attachments.files.length > 0
      || messages.some(message => !clicked.has(`${message.id}:answer`) && (choiceDrafts[message.id]?.length || 0) > 0)}>
      {resizable && !narrow ? (
        <>
          <button
            type="button"
            className={styles.resizeLeft}
            aria-label="Resize chat width"
            title="Drag to resize. Double-click to reset."
            onPointerDown={(e) => startResize("width", e)}
            onDoubleClick={() => onSizeChange?.(clampDockSize({
              width: DOCK_WIDTH_DEFAULT,
              height: size?.height ?? DOCK_HEIGHT_DEFAULT,
            }))}
            onKeyDown={(e) => onHandleKey("width", e)}
          />
          {resizeHeight && <button
            type="button"
            className={styles.resizeTop}
            aria-label="Resize chat height"
            title="Drag to resize. Double-click to reset."
            onPointerDown={(e) => startResize("height", e)}
            onDoubleClick={() => onSizeChange?.(clampDockSize({
              width: size?.width ?? DOCK_WIDTH_DEFAULT,
              height: DOCK_HEIGHT_DEFAULT,
            }))}
            onKeyDown={(e) => onHandleKey("height", e)}
          />}
        </>
      ) : null}
      {!isPanel && (
        <div className={styles.header}>
          <span className={styles.title}>Chat</span>
          {onToggle && (
            <button type="button" className="ghost sm" onClick={onToggle} aria-label="Collapse chat">
              ✕
            </button>
          )}
        </div>
      )}
      {homePanel && visible && <DesktopNoticeBanner ventureId={ventureId} messages={messages} otherVentureTarget="company-office-notice" />}
      <div className={styles.messageArea}>
      <div ref={buildStatusRef} tabIndex={-1} style={buildStatusScope === workKey ? {maxHeight: "50%", overflowY: "auto"} : undefined}>
        <AgentsTray {...agents} onDismiss={agents.dismiss} onAction={onAction} showInline={buildStatusScope === workKey}
          progress={[...(searchSnapshots.scope === workKey ? searchSnapshots.items : []), ...browseProgressFor(messages)]} />
      </div>
      <div className={styles.replyAnnouncement} role="status" aria-live="polite" aria-atomic="true">{replyAnnouncement}</div>
      <div className={styles.messages} role="log" aria-label="Your conversation" aria-live="off" aria-relevant="additions" tabIndex={0} ref={listRef} data-console-scroll={scope.current}
        data-console-scroll-count={visibleCount} data-console-scroll-loaded={messages.length} onScroll={(event) => {
        if (!visible) return;
        const el = event.currentTarget;
        followLatest.current = isNearBottom(el);
        viewportAnchor.current = {scope: scope.current, anchor: followLatest.current ? null : readScrollAnchor(el)};
        if (followLatest.current) setNewBelow(false);
      }}>
        {historyError && <ErrorNotice error={historyError} as="div" className={styles.msgSystem} source="chat.history" intent="chat_history">
          {historyError} <button type="button" className="ghost sm" onClick={() => void onReloadHistory?.()}>Retry history</button>
        </ErrorNotice>}
        {(messages.length > visibleCount || hasEarlier) && (
          <div className={styles.loadEarlier}>
            <button type="button" className="ghost sm" disabled={historyLoading} onClick={() => {
              const el = listRef.current;
              if (el) historyAnchor.current = {
                firstId: visibleMessages[0]?.id,
                scrollHeight: el.scrollHeight,
                scrollTop: el.scrollTop,
              };
              followLatest.current = false;
              if (messages.length > visibleCount) {
                setVisibleCount((count) => Math.min(messages.length, count + 24));
              } else {
                setVisibleCount((count) => count + 100);
                void onLoadEarlier?.();
              }
            }}>
              {historyLoading ? "Loading earlier messages…" : "Show earlier messages"}
            </button>
          </div>
        )}
        {messages.length === 0 && !streaming && !historyReady && (
          <div className={styles.empty} role="status"><p>Loading conversation…</p></div>
        )}
        {messages.length === 0 && !streaming && !sending && historyReady && (
          <div className={styles.empty}>
            <CharlieMascot active={visible} />
            <p>Your conversation with Charlie starts here. Ask for help with your idea or finding potential customers.</p>
            {ventureId ? <><button type="button" className="primary sm" disabled={blocked} onClick={() => onAction ? void onAction("chat.reply", { text: "Find my first customers" }) : void composer.submit("Find my first customers")}>Find my first customers</button><p><ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/guide`}>How to use Supymem</ChatAnchor></p></> : <ChatAnchor href="/new">Share your idea</ChatAnchor>}
          </div>
        )}
        {visibleMessages.map((m, visibleIndex) => {
          const role = m.role === "user" ? "founder" : m.role;
          const messageText = m.body_md || m.text || "";
          const body = role === "founder" ? messageText : messageText.replace(/\n\s*You can choose Continue clarifying whenever you're ready\.\s*$/, "");
          const questionBody = (m.actions || []).some(action => typeof (action.payload as { question_id?: unknown } | null)?.question_id === "string")
            ? body.replace(/\n\s*(?:Select any that fit, then submit, or type or speak your own answer below\.|Select one or more, then (?:press send|Send)\.?|Pick an answer or type your own\.)\s*$/i, "") : body;
          const suggestedPick = role === "founder" && /^Assumed: /.test(body);
          const raw = role === "founder" ? founderWords(body.replace(/^Assumed: ([^\n]*)/, "Your pick: $1")) : founderReviewText(humanizeErrorCodes(questionBody));
          const parsed = parseEmployeeStamp(raw);
          if (!parsed.text.trim() && (m.kind === "idea_clarification_ready" || (m.actions || []).some(action => typeof (action.payload as {question_id?: unknown} | null)?.question_id === "string"))) return <div key={m.id}>
            <p>The next question did not arrive.</p>
            {onReloadHistory && <button type="button" disabled={historyLoading} onClick={() => void onReloadHistory()}>Ask me the questions again</button>}
            {ventureId && (home ? <button type="button" disabled={blocked} onClick={() => void trackAction("idea-review", "idea.review", undefined)}>Continue with your saved idea</button> : <ChatAnchor href={`/v/${encodeURIComponent(ventureId)}/idea`}>Continue with your saved idea</ChatAnchor>)}
          </div>;
          if (role === "system" && m.kind !== "confirmation") {
            const actions = inChatActions(m.actions || [], home, ventureId || undefined);
            return (
              <div key={m.id} data-message-id={m.id} className={styles.msgSystem}>
                {parsed.text}
                {actions.length > 0 ? (
                  <div className={styles.actions}>
                    {actions.map((a) => a.href ? (
                      <ChatAnchor key={a.label} href={a.href} data-chat-route-native={"kind" in a && Boolean(a.kind) && a.kind !== "recovery_link" || undefined}
                        className={cx(styles.actionLink, styles.actionGhost)}
                        data-usage-control={isMacReviewHref(a.href || "") ? "mac_review" : undefined}>
                        {a.label}
                      </ChatAnchor>
                    ) : <div key={actionKey(m, a)}>
                      <button type="button" className={cx(styles.actionLink, styles.actionGhost)}
                        disabled={!!disabled || !!a.disabled_reason || pendingActions.has(actionKey(m, a))}
                        onClick={() => void trackAction(actionKey(m, a), a.action || a.name || "", a.payload, a.disabled_reason)}>{a.label}</button>
                      <ErrorNotice error={a.disabled_reason || actionErrors[actionKey(m, a)]} source="chat.system_action" />
                    </div>)}
                  </div>
                ) : null}
              </div>
            );
          }
          const own = role === "founder";
          const previewJob = !own && m.kind === "site_preview"
            ? m.actions?.find(action => action.action === "site.preview")?.payload as { job_id?: unknown; workspace_id?: unknown; version?: unknown } | undefined : undefined;
          const showInlinePage = !!ventureId && m.id === latestPage?.id && typeof previewJob?.job_id === "string"
            && !m.actions?.some(action => pageResultKind(action.href) === "preview");
          const name = speakerName(role, parsed.speaker);
          const charlie = role === "assistant" && (!parsed.speaker || /^charlie$/i.test(name));
          const title = parsed.speaker?.title;
          const portraitSeed = parsed.speaker?.id || parsed.speaker?.name || name;
          const ts = m.ts || (m as { created_at?: string }).created_at;
          const deduped = dedupeParagraphs(parsed.text);
          const linkActions = [...(m.actions || []).filter((a) => !a.confirmation),
            ...(isQuestionReply(m) ? [] : m.next_moves?.actions || []),
            ...(ventureId && !isQuestionReply(m) ? chatRouteAction(m, ventureId) : [])].filter(action => action.action !== "idea.review" ||
              (action as { retry?: boolean }).retry === true || (action.payload as { retry?: boolean } | null)?.retry === true ||
              (action.label !== "Continue clarifying" || visibleIndex === visibleMessages.length - 1) &&
              !isQuestionReply(m)).flatMap(action =>
              inChatActions([action], home, ventureId || undefined).map(converted => (action.href || isChatNavigationAction(converted))
                && converted.action !== "gtm.status"
                ? { ...converted, navigation: true } : converted));
          const options = questionOptionsFor(m);
          const prefill = (options.find(action => (action.payload as { prefill?: string } | undefined)?.prefill)?.payload as { prefill?: string } | undefined)?.prefill;
          // ponytail: urgency and context prefills must match an offered answer; use question field metadata for richer matching.
          const showPrefill = !!prefill && (options.some(action => (action.text || action.label).trim().toLowerCase() === prefill.trim().toLowerCase()) ||
            !/\b(?:urgent|urgency|why now)\b/i.test(parsed.text) && !parsed.text.toLowerCase().includes(prefill.trim().toLowerCase()));
          const otherActions = liveActions(m, visibleMessages, visibleIndex, clicked.has(`${m.id}:answer`), linkActions, showInlinePage).filter((action) => !options.includes(action));
          const answered = options.length > 0
            ? answeredState(visibleMessages, visibleIndex, options.map((o) => o.text ?? o.label ?? "")) : null;
          const shownText = deduped;
          const searchProgress = searchProgressFor(m, searchSnapshots.scope === workKey ? searchSnapshots.items : []);
          const selected = selectedFor(m);
          const savedSelection = answered ? savedIdeaChoiceAnswer(answered.chosen || answered.typed || "", options.map((action) => action.text || action.label)).selected : selected;
          const picked = (action: (typeof linkActions)[number]) => savedSelection.includes(action.text || action.label);
          const settled = Boolean(answered) || clicked.has(`${m.id}:answer`) || (activeQuestion?.id !== m.id);
          const questionSubmit = options.length > 0 && !settled && (hideComposer || selected.length > 0);
          const actionPlan = planChatActions([...(m.actions || []).filter(action => action.confirmation), ...otherActions].map(action => founderAction(action, m.id, action.confirmation ? confirmationState(messages, m.id, action.confirmation.action || action.action || "", action.confirmation) : undefined, local)), questionSubmit,
            isQuestionReply(m), local);
          const confirmations = actionPlan.confirmations;
          const questionExtras = options.length > 0 && !settled ? <>
            {recommendedChoices(options).length > 0 && <button type="button" className={cx(styles.actionLink, styles.actionGhost)} data-usage-control="chat_use_recommended" disabled={blocked}
              onClick={() => void submitChoices(m, recommendedChoices(options), composer.draft, true)}>I'm not sure, use your pick</button>}
            <button type="button" className={cx(styles.actionLink, styles.actionGhost)} disabled={blocked} onClick={() => void submitChoices(m, [], "I'm not sure")}>I'm not sure</button>
            {onQuestionBack ? <button type="button" className={cx(styles.actionLink, styles.actionGhost)} data-usage-control="onboarding_back" disabled={blocked} onClick={() => void onQuestionBack()}>Back</button> : ventureId && (home ? <button type="button" className={cx(styles.actionLink, styles.actionGhost)} data-usage-control="chat_previous_answer" disabled={blocked} onClick={() => prefillChat("I want to correct my previous idea answer: ")}>Back to previous answer</button> : <ChatAnchor className={cx(styles.actionLink, styles.actionGhost)} href={`/v/${encodeURIComponent(ventureId)}/idea?edit_previous=1`} data-usage-control="chat_previous_answer">Back to previous answer</ChatAnchor>)}
          </> : null;
          const renderAction = (a: (typeof otherActions)[number]) =>
            (a.action || a.name) === "recovery.submit" && !a.href ? (
              <div key={actionKey(m, a)} className={cx(styles.actionForm, a === actionPlan.primary && styles.actionFormPrimary)}>
                <RecoveryResultForm label={a.label} payload={a.payload as Record<string, unknown> | undefined}
                  onSubmit={(p) => onAction?.("recovery.submit", p)} />
              </div>
            ) : a.href ? (
              <ChatAnchor key={actionKey(m, a)} href={a.href} data-chat-route-native={"kind" in a && Boolean(a.kind) && a.kind !== "recovery_link" || undefined}
                {...externalLinkProps(a.href)}
                className={cx(styles.actionLink, a === actionPlan.primary ? styles.actionPrimary : styles.actionGhost)}
                data-usage-control={isMacReviewHref(a.href) ? "mac_review" : undefined}>
                {a.label}
              </ChatAnchor>
            ) : (
              <div key={actionKey(m, a)}>
                <button
                  type="button"
                  data-usage-control="chat_followup_action"
                  className={cx(styles.actionLink, a === actionPlan.primary ? styles.actionPrimary : styles.actionGhost)}
                  disabled={!!disabled || !!a.disabled_reason || clicked.has(actionKey(m, a))}
                  aria-busy={pendingActions.has(actionKey(m, a))}
                  title={a.disabled_reason || (!(a.action || a.name) || !onAction ? "You cannot use this action here. Ask me for your next step." : undefined)}
                  onClick={() => {
                    const actionName = a.action || a.name;
                    const payload = a.payload
                      ?? (a.task_id
                        ? { task_id: a.task_id }
                        : a.text
                          ? { text: a.text }
                          : undefined);
                    void trackAction(actionKey(m, a), actionName || "", {...payload, reply_to_message_id: m.id}, a.disabled_reason);
                  }}
                >
                  {pendingActions.has(actionKey(m, a)) ? "Starting…" : a.label}
                </button>
                <ErrorNotice error={a.disabled_reason || actionErrors[actionKey(m, a)]} source="chat.followup_action" />
              </div>
            );
          const actionControls = (questionSubmit || questionExtras || actionPlan.visible.length > 0 || actionPlan.overflow.length > 0) ? (
            <div className={styles.actions}>
              {questionSubmit && <button type="button" className={cx(styles.actionLink, confirmations.length ? styles.actionGhost : styles.actionPrimary)} data-usage-control="chat_options_submit" aria-describedby={hideComposer && !answerFor(m, selected, composer.draft).trim() ? `answer-hint-${m.id}` : undefined} disabled={blocked || hideComposer && !answerFor(m, selected, composer.draft).trim()}
                onClick={() => void submitChoices(m, selected, composer.draft)}>{hideComposer ? submitLabel : "Send"}</button>}
              {actionPlan.visible.map(renderAction)}
              {(actionPlan.overflow.length > 0 || questionExtras) && <details className={styles.actionOverflow}
                onBlur={event => {
                  // shortcut: Null blur leaves the menu open for WebKit clicks, add pointer-away dismissal if outside-click closing is needed.
                  if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false;
                }}
                onKeyDown={event => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    event.currentTarget.open = false;
                    event.currentTarget.querySelector("summary")?.focus();
                  }
                }}>
                <summary className={cx(styles.actionLink, styles.actionGhost)}>More actions</summary>
                <div className={styles.actionMenu}>
                  {questionExtras}
                  {actionPlan.overflow.map(renderAction)}
                </div>
              </details>}
              {actionPlan.overflow.map(action => <ErrorNotice key={`error:${actionKey(m, action)}`}
                error={action.disabled_reason || actionErrors[actionKey(m, action)]} source="chat.overflow_action" />)}
              {[...actionPlan.visible, ...actionPlan.overflow, ...confirmations].some(action => actionErrors[actionKey(m, action)])
                && <button type="button" className={cx(styles.actionLink, styles.actionGhost)} disabled={!!disabled}
                  onClick={() => void onSend("What should I do next?")}>Ask for your next step</button>}
            </div>
          ) : null;
          const messageBody = (
              <div className={cx(styles.msgBody, own ? styles.bubbleOwn : styles.bubble)}>
                <div className={styles.msgMeta}>
                  <strong>{name}</strong>
                  {title && <span className={styles.msgTitle}>{title}</span>}
                  {ts && (
                    <time>
                      {new Date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                    </time>
                  )}
                </div>
                {!own && m.kind === "exploration_options" ? (ventureId && <ExplorationCard key={m.id} ventureId={ventureId} message={m} onAction={onAction} disabled={!!disabled} />) : searchProgress ? <div className={styles.msgText} aria-live="polite">
                  <ResearchProgress progress={searchProgress} />
                  <p>{searchProgress.state === "failed" ? "Your customer search stopped. Check progress below to review what happened and retry."
                    : searchProgress.state === "done" ? "Your customer search finished. Check progress below to review your saved results."
                    : searchProgress.state === "queued" ? "You can check progress below while your customer search waits to start."
                    : "Your customer search is running. You can check progress below or keep chatting."}</p>
                </div> : <InboxBriefing key={m.id} message={m} enabled={inboxBriefing.enabled} ventureId={ventureId} onAction={onAction} disabled={!!disabled}>
                {splitFences(shownText).map((seg, i) => seg.code ? (
                  <div key={i} className={styles.codeBlock}>
                    <pre>{seg.text}</pre>
                    <button type="button" className={styles.copyBtn} aria-label="Copy message block"
                      onClick={async (event) => {
                        const button = event.currentTarget;
                        try {
                          if (!navigator.clipboard) throw new Error("Clipboard unavailable");
                          await navigator.clipboard.writeText(seg.text);
                          button.textContent = "Copied";
                          setTimeout(() => { button.textContent = "Copy"; }, 1500);
                        } catch {
                          button.textContent = "Select text to copy";
                        }
                      }}>Copy</button>
                  </div>
                ) : (
                  <ChatMessageText key={i} className={styles.msgText} text={seg.text} />
                ))}</InboxBriefing>}
                {!own && (m.parts || []).filter(part => part.type === "diagram").map((part, i) => <div key={`diagram-${i}`} className={styles.diagram}>{renderDiagramPart(part, text => void submit(text))}</div>)}
                {!own && !isQuestionReply(m) && m.next_moves?.body_md && <ChatMessageText className={styles.msgText} text={m.next_moves.body_md} />}
                {suggestedPick && <small className={styles.optionsHint}>Suggested option, treated as an assumption until you have evidence</small>}
                {!own && <IdeaReferences references={m.founder_references} />}
                {!own && m.kind === "market_beliefs" && ventureId && <MarketBeliefs ventureId={ventureId} />}
                {!own && m.kind === "startup_review" && (ventureId
                  ? <><StartupReview ventureId={ventureId} onContinue={onAction ? () => void trackAction(`${m.id}:build-next`, "whats_next", undefined) : undefined} />
                    <ErrorNotice error={actionErrors[`${m.id}:build-next`]} source="chat.startup_review" /></>
                  : <ErrorNotice error="Your venture context is missing. Reopen this chat to review your saved answers." source="chat.startup_review" />)}
                {m.citations?.some(citation => typeof citation.href === "string" && typeof citation.label === "string") && (
                  <div className={styles.citations}>
                    {m.citations.map(citation => typeof citation.href === "string" && typeof citation.label === "string" ? (
                      <ChatAnchor key={citation.href + citation.label} className={styles.citation} href={citation.href}>
                        {citation.label}
                      </ChatAnchor>
                    ) : null)}
                  </div>
                )}
                {ventureId && m.id === latestPage?.id && typeof previewJob?.workspace_id === "string"
                  && /^[A-Za-z0-9_-]+$/.test(previewJob.workspace_id)
                  && typeof previewJob.version === "number" && Number.isSafeInteger(previewJob.version) && previewJob.version > 0 && (
                    <LandingPreviewFrame src={`/w/${encodeURIComponent(previewJob.workspace_id)}/site/?v=${previewJob.version}`}
                      ventureId={ventureId} title="Your private landing page" sandbox="" />
                )}
                {ventureId && showInlinePage && typeof previewJob?.job_id === "string" && (
                  agents.jobs.some(job => job.id === previewJob.job_id && !jobFinished(job))
                    ? <p className={styles.msgText}>Started page build. You can keep chatting while it runs.</p>
                    : <BuildPageCard key={`${ventureId}:${previewJob.job_id}`} ventureId={ventureId} jobId={previewJob.job_id} />
                )}
                <GoalCard ventureId={ventureId} message={m} messages={messages} disabled={Boolean(disabled)}>
                {confirmations.map((a) => {
                  const conflict = a.confirmation?.resulting_state?.memory_conflict ? memoryConflict(a.confirmation.resulting_state.memory_conflict) : null;
                  const conflictScope = conflict ? scope.current : "";
                  if (conflict) return <MemoryConflict key={actionKey(m, a)} card={conflict} messageId={m.id}
                    disabled={!!disabled || !historyReady || composer.pending || !onAction || conflict.venture_id !== ventureId || clicked.has(actionKey(m, a))}
                    onSubmit={async text => {
                      if (!onAction || scope.current !== conflictScope || conflict.venture_id !== ventureId) throw new Error("Your session changed. Reload to save your choice.");
                      if (await onAction("chat.reply", { text }) === false) throw new Error("Your choice was not saved");
                    }} />;
                  const conf = a.confirmation as (NonNullable<typeof a.confirmation> & {
                    approval_policy?: ApprovalPolicy;
                  }) | undefined;
                  if (!conf) return null;
                  const policy = conf.approval_policy;
                  const actionName = conf.action || a.action || a.name;
                  const confirmationStatus = actionName ? confirmationState(messages, m.id, actionName, conf) : undefined;
                  const started = confirmationStatus === "started";
                  const needsSenderReview = actionName === "gtm.send" && conf.resulting_state?.multi_gmail_v1 === true;
                  const reviewKey = actionKey(m, a), reviewScope = needsSenderReview ? scope.current : "";
                  const policyScope = policy ? scope.current : "";
                  const denied = conf.state === "denied" || Boolean(policy && deniedPolicies.has(reviewKey));
                  const senderBlocked = needsSenderReview && senderReviews[reviewKey] !== true;
                  return (
                    <div key={actionKey(m, a)} className={styles.confirm}>
                      {conf.interpretation && dedupeParagraphs(`${shownText}\n\n${conf.interpretation}`) !== shownText ? (
                        <p className={styles.confirmLead}>{dedupeParagraphs(`${shownText}\n\n${conf.interpretation}`).slice(shownText.length).trim()}</p>
                      ) : null}
                      {conf.consequences && conf.consequences.length > 0 ? (
                        <ul className={styles.consequences}>
                          {conf.consequences.map((line) => (
                            <li key={line}>{line}</li>
                          ))}
                        </ul>
                      ) : null}
                      {needsSenderReview && ventureId && <ChatOutreachReview key={`${reviewScope}:${reviewKey}`} ventureId={ventureId} messageId={m.id} approval={conf.resulting_state || {}} disabled={Boolean(disabled) || started || conf.state === "starting" || clicked.has(reviewKey)} current={() => scope.current === reviewScope} onReady={ready => {
                        if (scope.current === reviewScope) setSenderReviews(previous => ({ ...previous, [reviewKey]: ready }));
                      }} />}
                      <div className={styles.actions}>
                        <button
                          type="button"
                          data-usage-control="chat_confirm_action"
                          className={cx(styles.actionLink, a === actionPlan.primary ? styles.actionPrimary : styles.actionGhost)}
                          disabled={!!disabled || !onAction || !actionName || !!a.disabled_reason || started || senderBlocked || conf.state === "starting" || denied || clicked.has(actionKey(m, a))}
                          title={a.disabled_reason || (!actionName || !onAction ? "You cannot use this action here. Ask me for your next step." : undefined)}
                          aria-busy={pendingActions.has(actionKey(m, a))}
                          onClick={() => {
                            if (denied) return;
                            if (started || senderBlocked || conf.state === "starting") return;
                            void trackAction(actionKey(m, a), actionName || "", actionName === "gtm.diagnosis.start"
                              ? { ...conf.resulting_state, quote_message_id: m.id }
                              : { ...conf.resulting_state, confirmation_message_id: m.id }, a.disabled_reason);
                          }}
                        >
                          {denied ? "Denied" : pendingActions.has(actionKey(m, a)) ? "Starting…" : confirmationStatus === "not_started" ? "Not started" : conf.state === "starting" && !started ? "Check status" : started || clicked.has(actionKey(m, a)) ? "Started" : "approval_policy" in conf ? "Approve once" : a.label}
                        </button>
                        {policy && ventureId && <ApprovalPolicyControls policy={policy} ventureId={ventureId}
                          messageId={m.id} action={actionName || ""} resultingState={conf.resulting_state || {}}
                          disabled={!!disabled || started || denied || Boolean(conf.state) || clicked.has(actionKey(m, a))}
                          allowDisabled={!onAction || !actionName || !!a.disabled_reason || senderBlocked}
                          current={() => scope.current === policyScope && policy.venture_id === ventureId}
                          onApproved={() => { void trackAction(actionKey(m, a), actionName || "", { ...conf.resulting_state, confirmation_message_id: m.id }, a.disabled_reason); }}
                          onDenied={() => { setDeniedPolicies(previous => new Set(previous).add(actionKey(m, a))); }} />}
                        <ErrorNotice error={a.disabled_reason || actionErrors[actionKey(m, a)]} source="chat.confirm_action" />
                      </div>
                    </div>
                  );
                })}
                </GoalCard>
                {options.length > 0 && (
                  <div className={cx(styles.options, settled && styles.optionsDone)} role="group" aria-label="Answer options">
                    <span id={!settled && !detailPromptFor(m, selected) ? `answer-hint-${m.id}` : undefined} className={styles.optionsHint}>{answered?.chosen ? `Your answer: ${answered.chosen}` : answered ? "Answered" : settled ? "Answer saved or sending" : `Select one or more, then ${homePanel && !hideComposer ? "press send" : submitLabel}`}</span>
                    {options.filter(action => !action.label?.startsWith("Use: ") || showPrefill).map((action) => (
                      <button key={action.label} type="button" data-usage-control={(action.payload as { requires_detail?: boolean })?.requires_detail ? "chat_question_detail" : "chat_followup_action"}
                        className={cx(styles.option, picked(action) && styles.optionPicked)} disabled={settled} aria-label={action.label?.startsWith("Use: ") ? action.label : action.text || action.label} aria-pressed={picked(action)}
                        onClick={() => {
                          if (action.label?.startsWith("Use: ") && prefill) { void submitChoices(m, [], prefill); return; }
                          const next = toggleIdeaChoice(selected, action.text || action.label,
                            questionSelectionMode((action.payload as { selection_mode?: "single" | "multiple" })?.selection_mode));
                          setChoiceDrafts(previous => ({ ...previous, [m.id]: next }));
                          setChoiceError("");
                          if (detailPromptFor(m, next)) inputRef.current?.focus();
                        }}>
                        <span>{action.label?.startsWith("Use: ") ? action.label : action.text || action.label}</span>
                        {picked(action) && <span className={styles.optionSelected}><span aria-hidden="true">✓ </span>Selected</span>}
                        {(action.payload as { recommended?: boolean })?.recommended && <span className={styles.optionFull}><span className={styles.recommended}>Recommended</span>{(action.payload as { recommendation_reason?: string }).recommendation_reason ? `: ${(action.payload as { recommendation_reason: string }).recommendation_reason}` : ""}</span>}
                      </button>
                    ))}
                    {!settled && <>
                      {showPrefill && !options.some(action => action.label?.startsWith("Use: ")) && <button type="button" className={styles.option} data-usage-control="chat_followup_action" disabled={blocked}
                        onClick={() => void submitChoices(m, [], prefill || "")}>Use: {prefill}</button>}
                      {detailPromptFor(m, selected) && <span id={`answer-hint-${m.id}`} className={styles.optionsHint} role="status">Optional: {detailPromptFor(m, selected)}</span>}
                      <button type="button" className={styles.optionsHintButton} onClick={() => inputRef.current?.focus()}>Your own answer</button>
                      {hideComposer && <>
                        {choiceError && <p role="status">{choiceError}</p>}
                        <textarea ref={input => { inputRef.current = input; }} aria-label="Add context to your answer" value={composer.draft} onChange={(event) => { setChoiceError(""); composer.setDraft(event.target.value); }} />
                        <DictateButton scope={`${scope.current}:question:${m.id}`} value={composer.draft} onChange={composer.setDraft} disabled={blocked} />
                      </>}
                    </>}
                    {questionExtras && actionControls}
                  </div>
                )}
                {!questionExtras && actionControls}
              </div>
          );
          return (
            homePanel ? <ChatMessageRow key={m.id} own={own} data-message-id={m.id} data-active-question={m.id === activeQuestion?.id || undefined} data-latest-message={m.id === messages.at(-1)?.id || undefined}
              portrait={charlie ? <CharlieMascot className={styles.imAvatar} active={visible && !streaming && m === visibleMessages.findLast(message => message.role === "assistant")} /> : <EmployeePortrait seed={portraitSeed} size={54} className={styles.imAvatar} />}>
              {!own && <div className={styles.imName}>{name}</div>}{messageBody}
            </ChatMessageRow> :
            <div key={m.id} data-message-id={m.id} data-active-question={m.id === activeQuestion?.id || undefined} data-latest-message={m.id === messages.at(-1)?.id || undefined} className={cx(styles.msg, styles.msgRow, own && styles.msgOwn)}>
              {!own && (charlie ? <CharlieMascot small active={visible && !streaming && m === visibleMessages.findLast(message => message.role === "assistant")} /> : <EmployeePortrait seed={portraitSeed} size={36} />)}
              {messageBody}
            </div>
          );
        })}
        {sending && !streaming && <ChatWorking waitingLonger={waitingLonger} />}
        {streaming && (
          <div className={cx(styles.msg, styles.msgRow)} aria-live="off" aria-label={`${streaming.speaker || "Charlie"} is thinking`}>
            {/^charlie$/i.test(streaming.speaker || "Charlie") ? <CharlieMascot small={!homePanel} active={visible} className={homePanel ? styles.imAvatar : undefined} /> : <EmployeePortrait seed={streaming.speaker || "Charlie"} size={homePanel ? 54 : 36} className={homePanel ? styles.imAvatar : undefined} />}
            <div className={cx(styles.msgBody, homePanel && styles.imStack)}>
              {homePanel && <div className={styles.imName}>{streaming.speaker || managerName || "Charlie"}</div>}
              <div className={styles.msgMeta}>
                <strong>{streaming.speaker || "Charlie"}</strong>
              </div>
              {homePanel ? <div className={styles.bubble}>{streamingBody}</div> : streamingBody}
            </div>
          </div>
        )}
      </div>
      {newBelow && (
        <button type="button" className={styles.newPill} onClick={() => {
          const el = listRef.current;
          if (el) el.scrollTop = el.scrollHeight;
          followLatest.current = true;
          setNewBelow(false);
        }}>New messages ↓</button>
      )}
      </div>
      {!hideComposer && (
        <div className={styles.composer}>
          {(disabled || refusal || composer.error || choiceError) && (
            <div className={styles.disabledReason} role={refusal || composer.error || choiceError ? "alert" : undefined}>
              {disabled?.reason || (refusal && parseEmployeeStamp(founderReviewText(humanizeErrorCodes(refusal.body_md || ""))).text) || composer.error || choiceError}
              {onAction && refusal && refusal.actions?.map(action => {
                const name = action.action;
                if ((name !== "status" && name !== "chat.startFresh") || (local && !visibleAction(action))) return null;
                const key = actionKey(refusal, action);
                return <button key={key} type="button" className={cx(styles.actionLink, styles.actionGhost)}
                  disabled={blocked || !!action.disabled_reason || pendingActions.has(key)}
                  onClick={() => void trackAction(key, name, action.payload, action.disabled_reason)}>{action.label}</button>;
              })}
            </div>
          )}
          {homePanel && <details className={styles.attachmentMenu} onKeyDown={event => {
            if (event.key === "Escape" && event.currentTarget.open) {
              event.preventDefault();
              event.stopPropagation();
              event.currentTarget.open = false;
              event.currentTarget.querySelector("summary")?.focus();
            }
          }}>
            <summary className={styles.imPlus} aria-label="Attach files or folder" title="Attach files or folder">+</summary>
            <div className={styles.attachmentPopover}>
              <IntakeAttachments attachments={attachments} disabled={blocked || !ventureId} />
            </div>
          </details>}
          <div
            className={styles.composerBox}
            onMouseDown={(e) => {
              // Whole box focuses the textarea (clicks on padding/gaps, not buttons/field).
              if ((e.target as HTMLElement).closest("button, textarea, input, a")) return;
              e.preventDefault();
              inputRef.current?.focus();
            }}
          >
            <div className={homePanel ? styles.composerInput : undefined} onDragOver={event => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); }}
              onDrop={event => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); if (!blocked) void attachments.drop(event.dataTransfer); } }}>
            <MentionField
              agents={office?.org?.agents || []}
              multiline
              placeholder={homePanel ? "Message" : composerPlaceholder(managerName)}
              linePx={homePanel ? 21 : undefined}
              padPx={homePanel ? 0 : undefined}
              disabled={!!disabled}
              ariaLabel={`Message ${managerName || "Charlie"}`}
              inputRef={inputRef}
              value={composer.draft}
              onChange={(next) => {
                composer.clearError();
                setChoiceError("");
                composer.setDraft(next);
              }}
              dockHeight={dockHeight}
              onSubmit={(body) => {
                void submit(body);
              }}
            />
            </div>
            <div className={styles.composerActions}>
              <div className={styles.composerTools}>
                {!homePanel && <IntakeAttachments attachments={attachments} disabled={blocked || !ventureId} />}
                {featureVisible("settings", "voice") && ventureId && onVoiceSend ? <VoiceMic ventureId={ventureId} onVoiceSend={onVoiceSend} disabled={blocked} compact={homePanel}
                  onTypeInstead={() => inputRef.current?.focus()}
                  active={visible}
                  conversationId={voiceConversationId} onToolsComplete={onReloadHistory}
                  screen={voiceScreen || { page: pathname || "/new", step: activeQuestion ? "Answer the current question" : "Venture conversation",
                    visible_actions: voiceWaiting ? ["End voice chat"] : activeQuestion ? [submitLabel, ...(recommendedChoices(questionOptionsFor(activeQuestion)).length ? ["I'm not sure, use your pick"] : [])] : [...preparedActions, "Send"],
                    expected_next: voiceWaiting ? "Wait for the saved reply, or end the call." : activeQuestion ? `Select options or type an answer, then ${submitLabel}.` : preparedActions.length ? `Review the prepared action on screen: ${preparedActions[0]}.` : "Type or say what you want to do next." }} /> : null}
                <EmergencyStopButton className={homePanel ? styles.emergencyStop : undefined} />
              </div>
              <div className={styles.composerTools}>
                <DictateButton scope={`${scope.current}:question:${activeQuestion?.id || "general"}`} value={composer.draft} onChange={composer.setDraft} disabled={blocked} />
                {onAction && (sending || streaming) && <button type="button" className={homePanel ? styles.stopWork : "ghost sm"} aria-label="Stop Charlie" title="Stop Charlie" onClick={stopWork}>{homePanel ? "■" : "Stop"}</button>}
                <button
                  type="button"
                  className="primary sm"
                  aria-label={submitLabel}
                  title={submitLabel}
                  data-usage-control={activeQuestion ? "chat_options_submit" : "chat_send"}
                  aria-describedby={activeQuestion && (detailPromptFor(activeQuestion, selectedFor(activeQuestion)) || !answerFor(activeQuestion, selectedFor(activeQuestion), composer.draft).trim()) ? `answer-hint-${activeQuestion.id}` : undefined}
                  disabled={!!disabled || (activeQuestion ? blocked || clicked.has(`${activeQuestion.id}:answer`) || !answerFor(activeQuestion, selectedFor(activeQuestion), composer.draft).trim() : !composer.draft.trim())}
                  onClick={() => void submit()}
                >
                  {homePanel ? "↑" : submitLabel}
                </button>
              </div>
            </div>
          </div>
          {ventureId && showMacTasks && featureVisible("settings", "mac") ? (
            <div className={styles.macTools} onKeyDown={event => {
              if (event.key !== "Escape" || !macOpen || event.defaultPrevented) return;
              event.preventDefault();
              event.stopPropagation();
              setMacOpen(false);
              event.currentTarget.querySelector("button")?.focus();
            }}>
              <button type="button" className={styles.macChip} aria-expanded={macOpen} aria-controls="mac-tasks-popover"
                onClick={() => setMacOpen((open) => !open)}>
                Mac tasks {macOpen ? "▾" : "▸"}
              </button>
              {macOpen ? (
                <div id="mac-tasks-popover" className={styles.macPopover}>
                  <MacTasksEntry
                    ventureId={ventureId}
                    compact
                    onSelect={(text) => {
                      if (blocked) return;
                      setMacOpen(false);
                      void submit(text);
                    }}
                  />
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </ChatPanel>
  );
}

export function usePersistedDockSize(): [DockSize, (size: DockSize) => void] {
  const [size, setSize] = useState<DockSize>(() => {
    if (typeof window === "undefined") {
      return { width: DOCK_WIDTH_DEFAULT, height: DOCK_HEIGHT_DEFAULT };
    }
    return readStoredDockSize(window.localStorage) || {
      width: DOCK_WIDTH_DEFAULT,
      height: DOCK_HEIGHT_DEFAULT,
    };
  });
  const update = useCallback((next: DockSize) => {
    const clamped = clampDockSize(next);
    setSize(clamped);
    if (typeof window !== "undefined") writeStoredDockSize(clamped, window.localStorage);
  }, []);
  return [size, update];
}
