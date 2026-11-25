"use client";

import { HOME_NAVIGATION_EVENT } from "@/lib/chatRouteAction";
import { ASK_EVENT } from "@/components/layout/NextStepStrip";

import { useContext, useEffect, useRef, useState } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import Link from "@/components/chat/ChatLink";
import { ChatDock } from "@/components/chat/ChatDock";
import { useVentureChat } from "@/components/chat/useVentureChat";
import { useChatHistory } from "@/components/chat/useChatHistory";
import { api } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { founderProductContext } from "@/lib/gtmDiagnosisApi";
import { gtmSendMode } from "@/lib/gtmConversationSend";
import { ideaConversationActivity } from "@/lib/ideaChatPoll";
import type { IdeaPageStatus } from "@/lib/runStatusPresentation";
import type { ChatMessage } from "@/lib/api-types";
import { committedEntryHref, continueSavedEntry, entryConfirmationFailure, entryReviewLabel, resumableEntry, type EntryCommit, type EntryHistoryItem, type EntryProposal, type EntryPreview } from "@/lib/chatEntry";
import styles from "./page.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export function ConfirmedConversation({ receipt: initialReceipt, initialName = "", initialDirection, entryVoiceNotes = [], onStatusChange }: {
  receipt: EntryCommit; initialName?: string; initialDirection?: EntryHistoryItem | null; entryVoiceNotes?: Array<Pick<ChatMessage, "id" | "body_md">>;
  onStatusChange?: (ventureId: string, status: IdeaPageStatus | null) => void;
}) {
  const home = useContext(FounderUIContext) === "home";
  const saved = initialDirection?.proposal.target_venture_id === initialReceipt.venture_id ? initialDirection : null;
  const [receipt, setReceipt] = useState(initialReceipt);
  const [direction, setDirection] = useState<EntryProposal | null>(saved && resumableEntry(saved.proposal) ? saved.proposal : null);
  const [directionQuestion, setDirectionQuestion] = useState<{ id?: string; text: string; question: string; choices: string[] } | null>(
    saved?.proposal.state === "clarify" ? { id: saved.proposal.id, text: saved.text, question: saved.proposal.message, choices: saved.proposal.choices || [] } : null);
  const directionSteps = useRef<Array<{question: typeof directionQuestion; text: string}>>(saved ? [{question: null, text: saved.text}] : []);
  const [directionDraft, setDirectionDraft] = useState<{id: string; text: string; questionId?: string} | undefined>(undefined);
  const [directionBusy, setDirectionBusy] = useState(false);
  const directionFlight = useRef(false);
  const directionRequest = useRef<{ text: string; id: string } | null>(null);
  const directionText = useRef(saved?.text || "");
  const conversationBody = useRef<HTMLDivElement>(null);
  const chat = useVentureChat(receipt.venture_id, { navigateOnResponse: false });
  const voiceNotes = chat.contextCurrent() ? [...new Map(entryVoiceNotes.map(note => [note.id, note])).values()] : [];
  const [name, setName] = useState(initialName);
  const [idea, setIdea] = useState("");
  const [gtmBusy, setGtmBusy] = useState(false);
  const [gtmError, setGtmError] = useState("");
  const lastGtmStatus = useRef("");
  const quoteAttempt = useRef<{ body: string; id: string } | null>(null);
  const consumedQuotes = new Set<string>();
  let latestQuoteId: string | undefined;
  for (const message of chat.messages) {
    if (message.kind === "gtm_diagnosis_quote") latestQuoteId = message.id;
    if (["gtm_diagnosis_pending", "gtm_diagnosis_failed"].includes(message.kind || "") && latestQuoteId) {
      consumedQuotes.add(latestQuoteId);
    }
  }
  const messages: ChatMessage[] = [...voiceNotes.filter(note => !chat.messages.some(message => message.id === note.id)).map((note): ChatMessage => ({...note, role: "assistant"})), ...chat.messages].map((message) => ({
    ...message,
    actions: (consumedQuotes.has(message.id) ? [] : directionQuestion?.choices.length === 0
      ? message.actions?.filter(action => (action.action || action.name) !== "chat.reply" || typeof (action.payload as {question_id?: unknown} | null)?.question_id !== "string")
      : message.actions)?.map((action) => action.href === receipt.action.href && action.label === "Open next action"
      ? { ...action, label: entryReviewLabel(receipt), style: "ghost" as const }
      : action),
  }));

  const directionQuestionId = directionQuestion?.question?.trim() ? `entry-direction:${directionQuestion.id || "restored"}` : null;
  const directionProposalId = direction ? `entry-direction-proposal:${direction.id}` : null;
  useEffect(() => {
    if (directionBusy) return;
    conversationBody.current?.querySelector<HTMLElement>(directionProposalId ? `[data-message-id="${directionProposalId}"]` : "[data-active-question]")?.scrollIntoView({block: "start"});
  }, [direction?.id, directionQuestionId, directionBusy]);
  if (direction && directionProposalId) messages.push({
    id: directionProposalId, role: "assistant", body_md: direction.message + (direction.state === "committing"
      ? "\n\nThe last confirmation is unfinished. Check and continue with this saved direction." : ""),
    actions: [
      { label: directionBusy ? "Opening…" : direction.state === "committing" ? "Check saved direction and continue" : "Continue", action: "entry.direction.confirm", style: "primary" },
      ...(direction.state === "committing" ? [] : [{ label: "Back", action: "entry.direction.back", style: "ghost" as const }]),
    ],
  });
  if (directionQuestion && directionQuestionId) messages.push({
    id: directionQuestionId, role: "assistant", body_md: directionQuestion.question,
    actions: directionQuestion.choices.map((text) => ({ label: text, text, action: "chat.reply",
      payload: { text, question_id: directionQuestionId } })),
  });

  useEffect(() => {
    let current = true;
    void api(`/api/ventures/${encodeURIComponent(receipt.venture_id)}`).then((result) => {
      const company = result.data as { name?: string; current_idea?: string; raw_idea?: string } | null;
      if (current && result.ok && company?.name) setName(company.name);
      const currentIdea = company?.current_idea || company?.raw_idea;
      if (current && result.ok && currentIdea) setIdea(founderProductContext(currentIdea));
    }).catch(() => {});
    return () => { current = false; };
  }, [receipt.venture_id]);

  useEffect(() => {
    if (receipt.route !== "gtm") return;
    let live = true;
    async function refresh() {
      if (!chat.contextCurrent()) return;
      const status = await api(`/api/ventures/${encodeURIComponent(receipt.venture_id)}/chat/gtm/status${chat.conversationId ? `?conversation_id=${encodeURIComponent(chat.conversationId)}` : ""}`);
      const saved = (status.data as { message?: { id?: string } } | null)?.message;
      if (chat.contextCurrent() && live && status.ok && saved?.id && lastGtmStatus.current !== saved.id) {
        lastGtmStatus.current = saved.id;
        await chat.reloadHistory();
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => { live = false; window.clearInterval(timer); };
  }, [receipt.route, receipt.venture_id, chat.reloadHistory, chat.contextCurrent, chat.conversationId]);

  const [activityNow, setActivityNow] = useState(Date.now());
  const conversationActivity = receipt.route === "startup" ? ideaConversationActivity(chat.messages, Boolean(chat.streaming), activityNow) : null;
  useEffect(() => {
    if (!conversationActivity?.startsWith("Preparing")) return;
    const timer = window.setInterval(() => setActivityNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [conversationActivity]);
  useEffect(() => {
    const ask = (event: Event) => {
      const target = (event as CustomEvent<{ action?: string; payload?: unknown }>).detail;
      if (target?.action) void act(target.action, target.payload);
    };
    window.addEventListener(HOME_NAVIGATION_EVENT, ask);
    window.addEventListener(ASK_EVENT, ask);
    return () => { window.removeEventListener(HOME_NAVIGATION_EVENT, ask); window.removeEventListener(ASK_EVENT, ask); };
  }, [chat.confirmChatAction, chat.contextCurrent, directionQuestionId, gtmBusy]);

  useEffect(() => {
    if (chat.contextCurrent()) onStatusChange?.(receipt.venture_id, conversationActivity ? {
      state: /^(?:Preparing|Saving|Still working)/.test(conversationActivity) ? "working" : "needs_you", activity: conversationActivity,
    } : null);
  }, [receipt.venture_id, conversationActivity, chat.contextCurrent, onStatusChange]);

  function changesDirection(body: string) {
    return Boolean(direction || directionQuestion || /^(?:use (?:the )?(?:whole|full|original) doc(?:ument)?|change (?:the )?(?:path|direction)|switch (?:to|path)|(?:skip|stop) (?:building|research))/i.test(body.trim()));
  }

  async function send(body: string, options?: Parameters<typeof chat.sendChat>[1]): Promise<boolean> {
    if (!chat.contextCurrent() || directionFlight.current) return false;
    // Only an explicit switch leaves this venture's chat; "show me my leads" stays a normal chat message.
    const navigating = /^(?:please\s+)?switch\s+to\s+/i.test(body.trim());
    if (navigating || changesDirection(body)) {
      if (direction && !navigating) { setGtmError("Continue with the saved direction or tell me to change it first."); return false; }
      const text = directionQuestion && !navigating ? `${directionQuestion.text}\nClarification: ${body}` : body;
      directionFlight.current = true; setDirectionBusy(true); setGtmError("");
      if (directionRequest.current?.text !== text) directionRequest.current = { text, id: options?.idempotencyKey || crypto.randomUUID() };
      try {
        const response = await api("/api/entry/chat/preview", { method: "POST", body: JSON.stringify({
          text, target_venture_id: receipt.venture_id, ...(chat.conversationId ? {conversation_id: chat.conversationId} : {}), use_original: true, request_id: directionRequest.current.id,
          ...(options?.source === "voice" ? {source_kind: "voice"} : {}),
        }) });
        if (!chat.contextCurrent()) return false;
        if (!response.ok) { setGtmError("Could not review that direction. Your original input is saved; try again."); return false; }
        const result = response.data as EntryPreview;
        if (result.status === "reply") {
          options?.onReply?.({id: `entry-direction-reply:${directionRequest.current.id}`, role: "assistant", body_md: result.body_md, actions: result.actions});
          directionRequest.current = null;
          await chat.reloadHistory();
          return true;
        }
        directionText.current = text;
        directionSteps.current.push({question: directionQuestion, text: body});
        directionRequest.current = null;
        if (result.status === "clarify") {
          if (!result.question?.trim()) {
            directionSteps.current.pop();
            setDirectionQuestion({ id: result.id, text, question: "", choices: [] });
            setGtmError("The next question did not arrive. Check the saved question or use Back.");
            return false;
          }
          const id = result.id || crypto.randomUUID();
          setDirectionQuestion({ id, text, question: result.question, choices: result.choices });
          const questionId = `entry-direction:${id}`;
          options?.onReply?.({id: questionId, role: "assistant", body_md: result.question,
            actions: result.choices.map(text => ({label: text, action: "chat.reply", text, payload: {text, question_id: questionId}}))});
        }
        else {
          if (result.proposal.onboarding_question) {
            setDirection(null);
            setDirectionQuestion({id: result.proposal.id, text, question: result.proposal.onboarding_question, choices: result.proposal.onboarding_choices || []});
            options?.onReply?.({id: `entry-direction:${result.proposal.id}`, role: "assistant", body_md: result.proposal.onboarding_question,
              actions: (result.proposal.onboarding_choices || []).map(text => ({label: text, action: "chat.reply", text, payload: {text}}))});
            return true;
          }
          setDirection(result.proposal); setDirectionQuestion(null);
          directionFlight.current = false;
          return await confirmDirection(result.proposal, options?.onReply);
        }
        return true;
      } finally { directionFlight.current = false; setDirectionBusy(false); }
    }
    if (receipt.route !== "gtm") return chat.sendChat(body, options);
    if (gtmBusy) return false;
    const mode = gtmSendMode(body, chat.messages);
    if (mode === "general") return chat.sendChat(body, options);
    setGtmBusy(true); setGtmError("");
    if (mode === "quote" && quoteAttempt.current?.body !== body) quoteAttempt.current = { body, id: crypto.randomUUID() };
    try {
      const response = await api(`/api/ventures/${encodeURIComponent(receipt.venture_id)}/chat/gtm/${mode === "quote" ? "quote" : "followup"}`, {
        method: "POST", body: JSON.stringify(mode === "quote"
          ? { request_id: quoteAttempt.current!.id, description: body, idea: idea || body, ...(chat.conversationId ? {conversation_id: chat.conversationId} : {}),
              problem: body, who: "", users_count: 0 }
          : { message: body, ...(chat.conversationId ? {conversation_id: chat.conversationId} : {}) }),
      });
      if (!chat.contextCurrent()) return false;
      if (!response.ok) {
        if (response.status === 402) { setGtmError(apiFailure(response, "your diagnosis")); return false; }
        const detail = (response.data as { detail?: { error?: { code?: string } } })?.detail;
        setGtmError(detail?.error?.code === "gtm_run_unresolved"
          ? "A diagnosis still needs resolution. Review its saved status before requesting another quote."
          : mode === "quote" ? "Could not prepare the diagnosis. Try again."
          : "Could not send that message. Try again.");
        return false;
      }
      if (mode === "quote") quoteAttempt.current = null;
      const reply = response.data as ChatMessage | null;
      if (reply?.body_md) options?.onReply?.(reply);
      await chat.reloadHistory();
      return true;
    } finally { setGtmBusy(false); }
  }

  async function confirmDirection(selected = direction, onReply?: (message: ChatMessage) => void): Promise<boolean> {
    if (!selected || directionFlight.current || !chat.contextCurrent()) return false;
    directionFlight.current = true; setDirectionBusy(true); setGtmError("");
    try {
      const next = await continueSavedEntry(selected, async (id) => {
        const response = await api(`/api/entry/chat/proposals/${encodeURIComponent(id)}`);
        const saved = (response.data as { proposal?: EntryProposal } | null)?.proposal;
        if (!response.ok || !saved) throw entryConfirmationFailure(response.data);
        return saved;
      }, async (id) => {
        const response = await api("/api/entry/chat/confirm", { method: "POST", body: JSON.stringify({ proposal_id: id }) });
        if (!response.ok) throw entryConfirmationFailure(response.data);
        return response.data as EntryCommit;
      }, chat.contextCurrent, async () => {
        if (!directionText.current) throw new Error("The saved answer could not be found. Reload the conversation.");
        if (directionRequest.current?.text !== directionText.current) directionRequest.current = {text: directionText.current, id: crypto.randomUUID()};
        const response = await api("/api/entry/chat/preview", { method: "POST", body: JSON.stringify({
          text: directionText.current, target_venture_id: receipt.venture_id, use_original: true, request_id: directionRequest.current.id,
        }) });
        if (!chat.contextCurrent()) return;
        if (!response.ok) throw new Error("Could not refresh the saved direction. Try again.");
        const result = response.data as EntryPreview;
        directionRequest.current = null;
        if (result.status === "clarify") {
          setDirection(null); setDirectionQuestion({id: result.id, text: directionText.current, question: result.question, choices: result.choices});
        } else if (result.status === "proposal") { setDirection(result.proposal); setDirectionQuestion(null); }
      });
      if (!next || !chat.contextCurrent()) return false;
      if (next.venture_id !== receipt.venture_id || next.workspace_id !== receipt.workspace_id) throw new Error("The saved direction belongs to another venture.");
      setReceipt(next); setDirection(null); setDirectionQuestion(null); directionSteps.current = []; setDirectionDraft(undefined);
      await chat.reloadHistory();
      onReply?.({id: `entry-direction-confirmed:${selected.id}`, role: "assistant", body_md: next.message, actions: []});
      return true;
    } catch (error) {
      if (chat.contextCurrent()) setGtmError(error instanceof Error ? error.message : "Could not confirm that direction.");
      return false;
    } finally { directionFlight.current = false; setDirectionBusy(false); }
  }

  function restoreDirectionStep(fallback: string) {
    const previous = directionSteps.current.pop() || {question: null, text: fallback};
    setDirectionQuestion(previous.question);
    setDirectionDraft({id: crypto.randomUUID(), text: previous.text,
      ...(previous.question?.id ? {questionId: `entry-direction:${previous.question.id}`} : {})});
    directionRequest.current = null;
  }

  async function refreshDirectionQuestion() {
    const result = await api("/api/entry/chat/history");
    if (!chat.contextCurrent()) return;
    const items = (result.data as {items?: EntryHistoryItem[]} | null)?.items || [];
    const saved = items.find(item => item.proposal.id === directionQuestion?.id);
    const question = saved?.proposal.onboarding_question || saved?.proposal.message;
    if (result.ok && saved?.proposal.state === "clarify" && question?.trim()) {
      setDirectionQuestion({id: saved.proposal.id, text: saved.text, question, choices: saved.proposal.choices || []});
      setGtmError("");
    } else setGtmError("The question is not ready. Use Back to revise your direction, or open your saved idea.");
    await chat.reloadHistory();
  }

  async function discardDirectionQuestion() {
    if (!directionQuestion?.id || directionFlight.current || !chat.contextCurrent()) return;
    const current = directionQuestion;
    directionFlight.current = true; setDirectionBusy(true); setGtmError("");
    try {
      const response = await api(`/api/entry/chat/proposals/${encodeURIComponent(current.id!)}/cancel`, {method: "POST"});
      if (!chat.contextCurrent()) return;
      if (response.ok && (response.data as {status?: string})?.status === "cancelled") restoreDirectionStep(current.text);
      else setGtmError("The saved direction could not be cancelled. Refresh to check its outcome.");
    } finally {directionFlight.current = false; setDirectionBusy(false);}
  }

  async function discardDirection(): Promise<boolean> {
    if (!direction || direction.state === "committing" || directionFlight.current || !chat.contextCurrent()) return false;
    directionFlight.current = true; setDirectionBusy(true);
    try {
      const response = await api(`/api/entry/chat/proposals/${encodeURIComponent(direction.id)}/cancel`, { method: "POST" });
      if (!chat.contextCurrent()) return false;
      if (response.ok && (response.data as { status?: string })?.status === "cancelled") {
        setDirection(null); restoreDirectionStep(direction.message);
        return true;
      }
      else setGtmError("The saved direction could not be cancelled. Refresh to check its outcome.");
      return false;
    } finally { directionFlight.current = false; setDirectionBusy(false); }
  }


  async function act(action: string, payload?: unknown): Promise<boolean> {
    if (!chat.contextCurrent()) return false;
    if (action === "entry.direction.confirm" || action === "entry.direction.back") {
      if (!direction || directionFlight.current || action === "entry.direction.back" && direction.state === "committing") return false;
      return action === "entry.direction.confirm" ? confirmDirection() : discardDirection();
    }
    if (action === "chat.reply" && directionQuestionId && (payload as { question_id?: string })?.question_id === directionQuestionId) return send(String((payload as { text?: string }).text || ""));
    if (action !== "gtm.diagnosis.start" || receipt.route !== "gtm") {
      return chat.confirmChatAction(action, payload);
    }
    if (gtmBusy) return false;
    const quoteMessageId = (payload as { quote_message_id?: unknown } | null)?.quote_message_id;
    if (typeof quoteMessageId !== "string") { setGtmError("The saved quote could not be verified. Refresh this conversation."); return false; }
    setGtmBusy(true); setGtmError("");
    try {
      const response = await api(`/api/ventures/${encodeURIComponent(receipt.venture_id)}/chat/gtm/confirm`, {
        method: "POST", body: JSON.stringify({ quote_message_id: quoteMessageId, ...(chat.conversationId ? {conversation_id: chat.conversationId} : {}) }),
      });
      if (!chat.contextCurrent()) return false;
      await chat.reloadHistory();
      if (!response.ok) { setGtmError("The outcome is unconfirmed. Refresh this conversation before approving anything else."); return false; }
      return true;
    } catch { return false; } finally { setGtmBusy(false); }
  }

  useChatHistory(receipt.venture_id, chat);
  return <section className={styles.confirmedChat} aria-label="Venture conversation">
    {home && <h2 className={styles.homeTitle}>{name || "Your venture"}</h2>}
    {!home && <header className={styles.context}>
      <div>
        <span className={styles.contextLabel}>Your venture</span>
        <h2>{name || "Your venture"}</h2>
        <p>{receipt.route === "gtm" ? "Finding customers" : receipt.route === "personal_build" ? "Preparing your build" : receipt.intake_path === "research" ? "Researching your idea" : "Developing your startup idea"}</p>
      </div>
      <Link className={styles.reviewLink} href={committedEntryHref(receipt)}>{entryReviewLabel(receipt)} →</Link>
    </header>}
    <div ref={conversationBody} className={styles.ventureThread}>
      {directionQuestionId && <p className={styles.progress} role="status">Question {Math.max(1, directionSteps.current.length)}</p>}
      {directionQuestion && !directionQuestionId && <div>
        <p>The next question did not arrive.</p>
        <button type="button" disabled={directionBusy} onClick={() => void refreshDirectionQuestion()}>Ask me the questions again</button>
        <Link href={committedEntryHref(receipt)}>Continue with your saved idea</Link>
      </div>}
      {directionQuestion?.choices.length === 0 && <button type="button" className="ghost" disabled={directionBusy} data-usage-control="onboarding_back" onClick={() => void discardDirectionQuestion()}>Back</button>}
      <ErrorNotice error={gtmError} source="new.ConfirmedConversation.gtmError" />
      <div className={styles.chatDock}><ChatDock
        key={chat.conversationKey}
        conversationId={chat.conversationDraftId}
        voiceConversationId={chat.conversationId}
        variant="panel"
        disabled={directionBusy ? {reason: "Your request is being processed. Wait for the result."} : undefined}
        showMacTasks={false}
        ventureId={receipt.venture_id}
        messages={messages}
        onQuestionBack={directionQuestion ? discardDirectionQuestion : undefined}
        draftPrefill={directionDraft}
        voiceScreen={direction ? { page: "/new", step: "Review your new direction",
          visible_actions: directionBusy ? ["End voice"] : ["Continue", ...(direction.state === "committing" ? [] : ["Back"])],
          expected_next: directionBusy ? "Wait for the saved outcome." : "Review the direction, then use its continuation button." } : directionQuestion ? {page: "/new", step: "Describe your new direction",
          visible_actions: directionBusy ? ["End voice"] : [directionQuestion.choices.length ? "Submit" : "Send", "Back"],
          expected_next: directionBusy ? "Wait for the saved outcome." : "Answer the question or use Back to revise the previous step."} : undefined}
        onLoadEarlier={chat.loadEarlier}
        hasEarlier={chat.hasEarlier}
        historyLoading={chat.historyLoading}
        historyReady={chat.historyReady}
        historyError={chat.historyError}
        onReloadHistory={chat.reloadHistory}
        streaming={chat.streaming}
        onSend={send}
        onVoiceSend={async (text, options) => {
          const captured: {reply: ChatMessage | null} = {reply: null};
          const admitted = await send(text, { source: "voice", idempotencyKey: options?.idempotencyKey, onReply: (message) => { captured.reply = message; } });
          return captured.reply ? {...captured.reply, admitted} : {body_md: admitted ? "I've recorded your answer. Review the next step when you're ready." : "I could not record your answer yet. Check its saved status before trying again.", admitted};
        }}
        onAction={(action, payload) => act(action, payload)}
      /></div>
    </div>
  </section>;
}
