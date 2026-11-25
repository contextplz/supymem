"use client";

import { useCallback, useContext, useEffect, useRef, useState, type FormEvent } from "react";
import Link from "@/components/chat/ChatLink";
import { useRouter, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { checkedEntryCommit, clearUnsentEntryDraft, committedChatHref, committedEntryHref, continueSavedEntry, entryConfirmationError, entryConfirmationFailure, entryDraftKey, entryDraftResumeKey, entryError, entryOwnerKey, founderWords, readEntryDraft, resumableEntry, splitEntryHistory, writeEntryDraft, type EntryCommit, type EntryHistoryItem, type EntryPreview, type EntryProposal } from "@/lib/chatEntry";
import { isNearBottom, scrollDecision } from "@/lib/chatScroll";
import { DictateButton } from "@/components/chat/DictateButton";
import { IntakeAttachments, useIntakeAttachments } from "@/components/chat/IntakeAttachments";
import composerStyles from "@/components/chat/ChatDock.module.css";
import { ChatMessageRow, ChatPanel, ChatWorking, useChatSelection } from "@/components/chat/ChatPanel";
import { CharlieMascot } from "@/components/chat/CharlieDock";
import { setCharlieMascot } from "@/lib/charlieMascot";
import { composeIdeaAnswer, savedIdeaChoiceAnswer } from "@/lib/ideaAnswer";
import { VoiceMic } from "@/components/chat/VoiceMic";
import { DesktopNoticeBanner } from "@/components/desktop/DesktopNoticeBanner";
import { ConfirmedConversation } from "./ConfirmedConversation";
import styles from "./page.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { clearChatDraftIfMatches, draftAfterFailedSend, draftStorage, readChatDraft, shouldSendOnKey, writeChatDraft } from "@/lib/chatComposer";
import { apiErrorCode } from "@/lib/apiFailure";
import type { IdeaPageStatus } from "@/lib/runStatusPresentation";
import { FounderUIContext } from "@/lib/founderUI";
import { guidePromptById } from "@/lib/capabilityGuide";
import { forgetAppRoute } from "@/lib/appResume";
import { WorkspaceSwitcher } from "@/components/office/WorkspaceSwitcher";

// Keep in sync with IDEA_MAX_CHARS in services/company_os/app/entry/chat_start.py.
const IDEA_MAX_CHARS = 200000;
const TOO_LONG = `That\u2019s longer than I can read at once (over ${IDEA_MAX_CHARS.toLocaleString()} characters). Trim it to the core idea and send again.`;

type VoiceReply = { body_md?: string; actions?: { label?: string }[]; admitted?: boolean } | null;
type Turn = { id: string; role: "user" | "assistant"; text: string; proposal?: EntryProposal; actions?: Extract<EntryPreview, { status: "reply" }>["actions"]; choices?: string[]; voiceNote?: boolean; voiceNeedsReview?: boolean };
const websiteLabels: Record<string, string> = { offer: "What it does", customer: "Who it's for", problem: "The problem you solve", pricing_hypothesis: "Pricing", positioning: "Positioning" };

// Keep only the latest verified receipt in memory across the URL-driven remount.
let committedHandoff: { receipt: EntryCommit; name: string; actor: string | null; epoch: number } | null = null;
function handoffReceipt(ventureId?: string): EntryCommit | null {
  if (!committedHandoff || committedHandoff.actor !== verifiedSessionSubject() || committedHandoff.epoch !== verifiedSessionEpoch()) return null;
  return committedHandoff.receipt.venture_id === ventureId ? committedHandoff.receipt : null;
}
function rememberReceipt(receipt: EntryCommit, name = "") {
  committedHandoff = { receipt, name, actor: verifiedSessionSubject(), epoch: verifiedSessionEpoch() };
}


function historyTurns(items: EntryHistoryItem[]): Turn[] {
  return items.flatMap((item) => {
    const turns: Turn[] = [
      { id: `${item.proposal.id}-user`, role: "user", text: item.text },
      { id: `${item.proposal.id}-assistant`, role: "assistant", text: ["pending", "clarify"].includes(item.proposal.state || "") && item.proposal.onboarding_question || item.proposal.message, proposal: item.proposal,
        choices: ["pending", "clarify"].includes(item.proposal.state || "") && item.proposal.onboarding_question ? item.proposal.onboarding_choices || [] : item.proposal.state === "clarify" ? item.proposal.choices : undefined },
    ];
    if (item.proposal.receipt?.message) turns.push({ id: `${item.proposal.id}-committed`, role: "assistant", text: item.proposal.receipt.message });
    return turns;
  });
}

function WelcomeIntro() {
  return <>
    <h2>What is your idea?</h2>
    <p className={styles.value}>Clarify your idea, review the open questions, then find customers and book a first call.</p>
    <p>Share your website, attach files, or tell us your idea by voice.</p>
    <p className={styles.hint}>Already have customers or a website? Mention them here, for example how many customers you have and your site's address, and Supymem will start from where you are.</p>
  </>;
}

export default function NewVenturePage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const queryTarget = searchParams.get("venture") || undefined;
  const freshStart = searchParams.get("fresh") || undefined;
  const resume = searchParams.get("resume") || undefined;
  const routeTarget = useRef(queryTarget); routeTarget.current = queryTarget;
  const actor = useRef(verifiedSessionSubject());
  const [retiringTarget, setRetiringTarget] = useState(false);
  const targetVentureId = retiringTarget ? undefined : queryTarget;
  const [sessionEpoch, setSessionEpoch] = useState(verifiedSessionEpoch);
  useEffect(() => { if (!queryTarget) setRetiringTarget(false); }, [queryTarget]);
  useEffect(() => {
    const changed = () => {
      const subject = verifiedSessionSubject();
      if (actor.current && actor.current !== subject) {
        committedHandoff = null;
        setSessionEpoch(verifiedSessionEpoch());
        if (routeTarget.current) { setRetiringTarget(true); router.replace("/new"); }
      }
      actor.current = subject;
    };
    changed();
    window.addEventListener("supymem-session-changed", changed);
    return () => window.removeEventListener("supymem-session-changed", changed);
  }, []);
  // Next keeps this page mounted when only the query changes. A fresh venture
  // must not inherit the previous receipt, composer, clarification or requests.
  return <NewVentureConversation key={`${sessionEpoch}:${targetVentureId || "fresh"}:${freshStart || ""}:${resume || ""}`} targetVentureId={targetVentureId} freshStart={Boolean(freshStart && !targetVentureId)} resume={resume} />;
}

function NewVentureConversation({ targetVentureId, freshStart = false, resume }: { targetVentureId?: string; freshStart?: boolean; resume?: string }) {
  const home = useContext(FounderUIContext) === "home";
  const router = useRouter();
  const storage = draftStorage();
  const savedDraftSearch = resume && !targetVentureId ? readChatDraft(storage, entryDraftResumeKey(verifiedSessionSubject(), resume)) : "";
  const draftSearch = /^\?fresh=[a-zA-Z0-9-]+$/.test(savedDraftSearch) ? savedDraftSearch : resume && !targetVentureId ? "" : typeof window === "undefined" ? "" : window.location.search;
  const guidePrompt = !targetVentureId && !resume ? guidePromptById(new URLSearchParams(draftSearch).get("guide_prompt")) : undefined;
  const draftKey = typeof window === "undefined" ? null : entryDraftKey(verifiedSessionSubject(), {pathname: window.location.pathname, search: draftSearch}, targetVentureId);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const [draft, updateDraft] = useState(() => readEntryDraft(storage, draftKey) || guidePrompt || "");
  const answerRef = useRef<HTMLTextAreaElement>(null);
  const nameInput = useRef<HTMLTextAreaElement>(null);
  const [savedDraftHref, setSavedDraftHref] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [earlier, setEarlier] = useState<Turn[]>([]);
  const [showEarlier, setShowEarlier] = useState(false);
  const [ventureName, setVentureName] = useState(handoffReceipt(targetVentureId) ? committedHandoff?.name || "" : "");
  const nameRequest = useRef<{ proposal: string; id: string } | null>(null);
  const [proposal, setProposal] = useState<EntryProposal | null>(null);
  const [activeReceipt, setActiveReceipt] = useState<EntryCommit | null>(handoffReceipt(targetVentureId));
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [missingSelection, setMissingSelection] = useState(false);
  const [savedDirection, setSavedDirection] = useState<EntryHistoryItem | null>(null);
  const [clarification, setClarification] = useState<{ id?: string; original: string; choices: string[]; question?: string; field?: "offer" | "customer" | "context" | null } | null>(null);
  const [selectedChoices, setSelectedChoices] = useState<string[]>([]);
  const proposalRef = useRef<EntryProposal | null>(null);
  const clarificationRef = useRef<typeof clarification>(null);
  function updateProposal(value: EntryProposal | null, original = "") {
    if (proposalRef.current?.id !== value?.id) setWebsiteEdits({});
    const question = value?.state === "pending" && value.onboarding_question;
    proposalRef.current = question ? null : value; setProposal(proposalRef.current);
    if (question) updateClarification({ id: value.id, original, choices: value.onboarding_choices || [], question, field: value.onboarding_field });
    else if (value) updateClarification(null);
  }
  function updateClarification(value: typeof clarification) { clarificationRef.current = value; setClarification(value); }
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<{ text?: string } | null>(null), historyVersion = useRef(0);
  const fileRead = useRef(0);
  const previewRequest = useRef<{ text: string; id: string } | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const lastScrollSize = useRef<{scrollHeight: number; clientHeight: number} | null>(null);
  const prevTurns = useRef(0);
  const lastMessageId = useRef<string | undefined>(undefined);
  const [newBelow, setNewBelow] = useState(false);
  const [websiteEdits, setWebsiteEdits] = useState<Record<string, string>>({});
  const ownerKey = () => typeof window === "undefined" ? null : entryOwnerKey(
    verifiedSessionSubject(), verifiedSessionEpoch(), window.location, targetVentureId);
  const renderedOwner = ownerKey();
  const attachments = useIntakeAttachments(renderedOwner || "new");
  const draftRef = useRef(draft); draftRef.current = draft;
  const draftOwner = useRef(renderedOwner);
  function setDraft(value: string) {
    draftRef.current = value;
    updateDraft(value);
    writeEntryDraft(storage, draftKey, value, verifiedSessionSubject(), draftSearch, targetVentureId);
  }
  const owned = (owner: string | null) => alive.current && owner !== null && ownerKey() === owner;
  const composerDisabled = busy || !historyLoaded || draftOwner.current !== renderedOwner || !owned(renderedOwner);
  const clarificationCount = new Set(turns.filter(turn => turn.role === "assistant" && (turn.proposal?.state === "clarify" || turn.proposal?.state === "pending" && turn.proposal.onboarding_question)).map(turn => turn.proposal?.onboarding_field || turn.id)).size;
  const questionNumber = 1 + clarificationCount;
  const naming = Boolean(proposal && !proposal.target_venture_id && !ventureName.trim());
  useEffect(() => {
    if (busy || activeReceipt || !historyLoaded || isSelecting()) return;
    if (proposal) nameInput.current?.focus({ preventScroll: true });
    else answerRef.current?.focus({ preventScroll: true });
  }, [questionNumber, clarification?.id, proposal?.id, busy, activeReceipt, historyLoaded]);

  const loadHistory = useCallback(async () => {
    const version = historyVersion.current;
    const knownOwner = renderedOwner;
    if (knownOwner !== null && !owned(knownOwner)) return;
    const deadline = setTimeout(() => {
      if (!owned(knownOwner ?? ownerKey()) || historyVersion.current !== version) return;
      historyVersion.current++;
      setError("The saved conversation is taking longer than expected. Try again.");
      setHistoryLoaded(true);
    }, 15000);
    try {
      await ensureSession();
      // Only the initial unverified render may bootstrap an owner. A known
      // render keeps its original actor/epoch across session refresh.
      const owner = knownOwner ?? ownerKey();
      if (!owned(owner) || historyVersion.current !== version) return;
      const query = targetVentureId ? `?target_venture_id=${encodeURIComponent(targetVentureId)}` : "";
      const res = await api(`/api/entry/chat/history${query}`);
      if (!alive.current || historyVersion.current !== version || typeof window === "undefined" ||
          entryOwnerKey(verifiedSessionSubject(), verifiedSessionEpoch(), window.location, targetVentureId) !== owner) return;
      if (!res.ok) {
        if (res.status === 404 && (targetVentureId || resume)) {
          setMissingSelection(true);
          if (targetVentureId) forgetAppRoute(storage, verifiedSessionSubject(), targetVentureId);
        } else setError("The saved conversation could not be loaded. Try again.");
        return;
      }
      const data = res.data as {items?: EntryHistoryItem[]; voice_notes?: Array<{id: string; body_md: string; partition: string; target_venture_id?: string | null; needs_edit?: boolean}>};
      const items = data?.items;
      if (!Array.isArray(items)) { setError("The saved conversation could not be loaded. Try again."); return; }
      const notes = (Array.isArray(data.voice_notes) ? data.voice_notes : []).filter(note =>
        typeof note.id === "string" && /^entry-voice-note_[a-f0-9]{64}$/.test(note.id) && typeof note.body_md === "string" &&
        (note.partition === "fresh" || note.partition === "earlier") && (note.target_venture_id ?? null) === (targetVentureId ?? null));
      // Without a target venture this is a new venture: start blank, keep older conversations behind a button.
      const resumeIndex = resume ? items.findIndex(item => item.proposal.id === resume) : -1;
      if (resume && resumeIndex < 0) { setMissingSelection(true); return; }
      const history = resumeIndex >= 0 ? items.slice(0, resumeIndex + 1) : items;
      let previousIds: string[] = [];
      if (draftKey && /^\?fresh=/.test(draftSearch)) {
        const saved = readChatDraft(storage, `${draftKey}:history`);
        try { const ids: unknown = JSON.parse(saved); if (Array.isArray(ids) && ids.every(id => typeof id === "string")) previousIds = ids; } catch {}
        if (!saved) { previousIds = [...items.map(item => item.proposal.id), ...notes.map(note => note.id)]; writeChatDraft(storage, `${draftKey}:history`, JSON.stringify(previousIds)); }
      }
      const { earlier: older, fresh } = /^\?fresh=/.test(draftSearch) ? {earlier: history.filter(item => previousIds.includes(item.proposal.id)), fresh: history.filter(item => !previousIds.includes(item.proposal.id))} : targetVentureId ? { earlier: [], fresh: history } : splitEntryHistory(history);
      const savedDraft = splitEntryHistory(older).fresh.findLast(item => resumableEntry(item.proposal) || item.proposal.state === "clarify");
      if (freshStart) setSavedDraftHref(savedDraft ? `/new?resume=${encodeURIComponent(savedDraft.proposal.id)}` :
        readEntryDraft(storage, entryDraftKey(verifiedSessionSubject(), {pathname: "/new", search: ""})) ? "/new" : null);
      const noteTurns = (partition: string): Turn[] => notes.filter(note => note.partition === partition).map(note =>
        ({id: note.id, role: "assistant", text: note.body_md, voiceNote: true, voiceNeedsReview: note.needs_edit === true}));
      const savedNoteIds = new Set(notes.map(note => note.id));
      setEarlier([...historyTurns(older), ...noteTurns("earlier"), ...noteTurns("fresh").filter(note => /^\?fresh=/.test(draftSearch) && previousIds.includes(note.id))]);
      const active = fresh.filter((item) => item.proposal.state !== "cancelled");
      setTurns(current => [...historyTurns(active), ...noteTurns("fresh").filter(note => !/^\?fresh=/.test(draftSearch) || !previousIds.includes(note.id)), ...current.filter(turn => turn.voiceNote && !savedNoteIds.has(turn.id))]);
      const saved = [...fresh].reverse().find((item) => resumableEntry(item.proposal));
      const savedProposal = saved?.proposal || null;
      attachments.restore((savedProposal || active.at(-1)?.proposal)?.files);
      updateProposal(savedProposal, saved?.text);
      const committed = targetVentureId ? [...items].reverse().find((item) => item.proposal.state === "committed" && item.proposal.receipt?.venture_id === targetVentureId)?.proposal : null;
      setVentureName(current => savedProposal ? savedProposal.venture_name || "" : committed?.venture_name || current);
      const latest = fresh.at(-1);
      setSavedDirection(targetVentureId && latest?.proposal.target_venture_id === targetVentureId &&
        (resumableEntry(latest.proposal) || latest.proposal.state === "clarify") ? latest : null);
      if (committed?.receipt) {
        try { setActiveReceipt(checkedEntryCommit(committed, committed.receipt)); }
        catch { setError("The saved conversation could not be verified."); }
      }
      const clarify = active.at(-1)?.proposal.state === "clarify" ? active.at(-1) : undefined;
      if (clarify) updateClarification({ id: clarify.proposal.id, original: clarify.text, choices: clarify.proposal.onboarding_question ? clarify.proposal.onboarding_choices || [] : clarify.proposal.choices || [], question: clarify.proposal.onboarding_question || clarify.proposal.message, field: clarify.proposal.onboarding_field });
    } catch {
      if (owned(knownOwner ?? ownerKey()) && historyVersion.current === version) setError("The saved conversation could not be loaded. Try again.");
    } finally {
      clearTimeout(deadline);
      if (owned(knownOwner ?? ownerKey()) && historyVersion.current === version) setHistoryLoaded(true);
    }
  }, [targetVentureId, renderedOwner, freshStart, resume]);

  useEffect(() => {
    historyVersion.current++;
    pending.current = null; setBusy(false); updateProposal(null); setActiveReceipt(handoffReceipt(targetVentureId)); setSavedDirection(null); setHistoryLoaded(false); setMissingSelection(false);
    setTurns([]); setEarlier([]); updateClarification(null);
    const sameDraftOwner = draftOwner.current === renderedOwner || draftOwner.current === null;
    draftOwner.current = renderedOwner;
    const restored = readEntryDraft(storage, draftKey);
    draftRef.current = sameDraftOwner && draftRef.current ? draftRef.current : restored;
    setDraft(draftRef.current);
    setVentureName(handoffReceipt(targetVentureId) ? committedHandoff?.name || "" : ""); nameRequest.current = null; setError("");
    void loadHistory();
  }, [loadHistory, renderedOwner]);
  const isSelecting = useChatSelection();
  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;
    const last = turns.at(-1);
    const decision = scrollDecision({ newMessage: turns.length > prevTurns.current && last?.id !== lastMessageId.current, following: following.current, selecting: isSelecting(), own: last?.role === "user" });
    lastMessageId.current = last?.id;
    prevTurns.current = turns.length;
    if (decision === "follow") { following.current = true; thread.scrollTop = thread.scrollHeight; setNewBelow(false); }
    else if (decision === "pill") setNewBelow(true);
  }, [turns, isSelecting]);

  async function commitProposal(current: EntryProposal, owner: string | null, flight: object, name: string) {
    const submittedDraft = draftRef.current;
    const currentRequest = () => owned(owner) && pending.current === flight;
    const committed = await continueSavedEntry(current, async (id) => {
      const read = await api(`/api/entry/chat/proposals/${encodeURIComponent(id)}`);
      if (!read.ok) throw entryConfirmationFailure(read.data);
      const saved = (read.data as {proposal?: EntryProposal} | null)?.proposal;
      if (!saved) throw new Error(entryConfirmationError(null));
      return saved;
    }, async (id) => {
      const result = await api("/api/entry/chat/confirm", { method: "POST", body: JSON.stringify({ proposal_id: id, ...(!current.target_venture_id && name.trim() ? { name: name.trim() } : {}),
        ...(current.asset_prefill ? { assets: current.asset_prefill } : {}),
        ...(Object.keys(websiteEdits).length ? { website_answers: websiteEdits } : {}), evidence_ids: attachments.evidenceIds }) });
      if (!result.ok) throw entryConfirmationFailure(result.data);
      return result.data as EntryCommit;
    }, currentRequest, async () => {
      const text = [...turns].reverse().find(turn => turn.role === "user")?.text;
      if (!text) throw new Error("The saved answer could not be found. Reload the conversation.");
      if (previewRequest.current?.text !== text) previewRequest.current = { text, id: crypto.randomUUID() };
      const result = await api("/api/entry/chat/preview", { method: "POST", body: JSON.stringify({
        text, ...(targetVentureId ? { target_venture_id: targetVentureId } : {}), request_id: previewRequest.current.id,
      }) });
      if (!currentRequest()) return;
      if (!result.ok) throw new Error(entryError(result.data, "Could not refresh the saved direction. Try again."));
      previewRequest.current = null;
      await loadHistory();
    });
    if (!committed || !currentRequest()) return;
    const href = committedChatHref(committed);
    setTurns((items) => [...items, { id: `${current.id}-committed`, role: "assistant", text: committed.message }]);
    updateProposal(null);
    setActiveReceipt(committed);
    rememberReceipt(committed, name.trim() || current.venture_name || "");
    if (!current.target_venture_id && current.state !== "committing") {
      clearChatDraftIfMatches(storage, draftKey, submittedDraft);
      clearUnsentEntryDraft(storage, verifiedSessionSubject(), draftSearch);
      if (draftRef.current === submittedDraft) { draftRef.current = ""; updateDraft(""); }
    }
    router.replace(href);
  }

  /** `voice`: the founder spoke it; nothing is committed by voice, the on-screen button does that. */
  async function preview(text: string, voice = false, requestId?: string): Promise<VoiceReply> {
    const sent = text.trim();
    if (!sent || pending.current) return null;
    let owner: string | null = renderedOwner;
    if (owner !== null && !owned(owner)) return null;
    if (proposalRef.current) {
      const message = "Your idea is ready to review. Choose Back to change it, or Continue when it reflects your latest direction.";
      if (!voice) setError(message);
      return voice ? { body_md: message, admitted: false } : null;
    }
    if (sent.length > IDEA_MAX_CHARS) { setError(TOO_LONG); return null; } // draft stays; nothing is dropped
    fileRead.current++;
    const flight = { text: sent }; pending.current = flight; historyVersion.current++;
    const priorClarification = clarification, priorDraft = draft;
    setBusy(true);
    setError("");
    if (!voice) { draftRef.current = ""; updateDraft(""); }
    try {
      if (requestId || previewRequest.current?.text !== sent) previewRequest.current = { text: sent, id: requestId || crypto.randomUUID() };
      const session = await ensureSession();
      owner ??= ownerKey();
      if (!owned(owner) || pending.current !== flight) return null;
      if (!session.ok) throw new Error("Connect your account to continue.");
      const result = await api("/api/entry/chat/preview", {
        method: "POST",
        body: JSON.stringify({ text: sent, source_kind: voice ? "voice" : "typed", evidence_ids: attachments.evidenceIds,
          ...(clarification?.id ? { previous_proposal_id: clarification.id } : {}),
          ...(targetVentureId ? { target_venture_id: targetVentureId } : {}), request_id: previewRequest.current.id }),
      });
      if (!owned(owner) || pending.current !== flight) return null;
      if (!result.ok) throw new Error(entryError(result.data, "Couldn’t understand that request. Please try again."));
      if (!voice) {
        clearChatDraftIfMatches(storage, draftKey, priorDraft);
        clearUnsentEntryDraft(storage, verifiedSessionSubject(), draftSearch);
      }
      const response = result.data as EntryPreview;
      if (response.status === "reply") {
        previewRequest.current = null;
        const id = crypto.randomUUID();
        setTurns(current => [...current, { id: `${id}-user`, role: "user", text: sent },
          { id: `${id}-assistant`, role: "assistant", text: response.body_md, actions: response.actions }]);
        return response;
      }
      const proposalId = response.status === "clarify" ? response.id : response.proposal.id;
      if (proposalId && !targetVentureId && /^\?fresh=/.test(draftSearch)) writeChatDraft(storage, entryDraftResumeKey(verifiedSessionSubject(), proposalId), draftSearch);
      previewRequest.current = null;
      const id = crypto.randomUUID();
      if (response.status === "clarify") {
        setVentureName(current => current || response.venture_name || "");
        const question = response.onboarding_question || response.question;
        if (!question?.trim()) throw new Error("The next question did not arrive. Send your answer again or use Back to continue.");
        const choices = response.onboarding_question ? response.onboarding_choices || [] : response.choices;
        setTurns((current) => [...current, { id: `${id}-user`, role: "user", text: sent }, { id: `${id}-assistant`, role: "assistant", text: question, choices, proposal: response.id ? { id: response.id, route: "clarify", state: "clarify", message: response.question, choices: response.choices, venture_name: response.venture_name ?? undefined, website_answers: response.website_answers, onboarding_question: response.onboarding_question, onboarding_choices: response.onboarding_choices, onboarding_field: response.onboarding_field, next_step: "clarify", skipped_steps: [], expires_at: "" } : undefined }]);
        updateClarification({ id: response.id, original: sent, choices, question, field: response.onboarding_field });
        setSelectedChoices([]);
        return { body_md: question, actions: choices.map((label) => ({ label })) };
      } else {
        const question = response.proposal.state === "pending" && response.proposal.onboarding_question;
        setTurns((current) => [...current, { id: `${id}-user`, role: "user", text: sent }, { id: `${id}-assistant`, role: "assistant", text: question || response.proposal.message, proposal: response.proposal, choices: question ? response.proposal.onboarding_choices || [] : undefined }]);
        setSelectedChoices([]); updateProposal(response.proposal, sent); setVentureName(ventureName || response.proposal.venture_name || ""); nameRequest.current = null;
        return { body_md: question || response.proposal.message, actions: [{ label: "Continue" }] };
      }
    } catch (cause) {
      if (owned(owner) && pending.current === flight) {
        if (!voice) setDraft(draftAfterFailedSend({failedText: priorDraft, currentDraft: draftRef.current}));
        updateClarification(priorClarification);
        setError(cause instanceof Error ? cause.message : "Couldn’t send that message.");
      }
    } finally {
      if (pending.current === flight) { pending.current = null; if (alive.current) setBusy(false); }
    }
    return null;
  }

  async function confirm(name = draftRef.current.trim() || ventureName) {
    if (!proposal || pending.current) return;
    if (!proposal.target_venture_id && proposal.state !== "committing" && !name.trim()) { setError("Give your venture a name, or ask me to suggest one."); return; }
    if (!proposal.target_venture_id && proposal.state !== "committing" && name.trim().length > 48) { setError("Keep your venture name to 48 characters or fewer."); return; }
    const owner = renderedOwner; if (!owned(owner)) return;
    const flight = {}; pending.current = flight; historyVersion.current++;
    setBusy(true);
    setError("");
    try {
      await commitProposal(proposal, owner, flight, name);
    } catch (cause) {
      if (owned(owner) && pending.current === flight) setError(cause instanceof Error ? cause.message : entryConfirmationError(null));
    } finally {
      if (pending.current === flight) { pending.current = null; if (alive.current) setBusy(false); }
    }
  }

  async function suggestName() {
    if (!proposal || pending.current || proposal.target_venture_id) return;
    const owner = renderedOwner; if (!owned(owner)) return;
    const current = proposal;
    const flight = {}; pending.current = flight; setBusy(true); setError("");
    if (nameRequest.current?.proposal !== current.id) nameRequest.current = { proposal: current.id, id: crypto.randomUUID() };
    try {
      const response = await api(`/api/entry/chat/proposals/${encodeURIComponent(current.id)}/name`, {
        method: "POST", body: JSON.stringify({ request_id: nameRequest.current.id }),
      });
      if (!owned(owner) || pending.current !== flight) return;
      const suggestion = response.data as { name?: unknown; source?: string; message?: string } | null;
      const name = suggestion?.name;
      if (response.ok && suggestion?.source === "free_text") {
        nameRequest.current = null;
        nameInput.current?.focus({ preventScroll: true });
        return;
      }
      if (!response.ok && ["name_unavailable", "name_limit", "idea_missing"].includes(apiErrorCode(response) || "") && response.status < 500 && response.status > 0) nameRequest.current = null;
      if (!response.ok || typeof name !== "string" || !name.trim()) throw new Error(entryError(response.data, "Could not suggest a name yet. You can type one or retry."));
      setVentureName(name); nameRequest.current = null;
    } catch (cause) {
      if (owned(owner) && pending.current === flight) setError(cause instanceof Error ? cause.message : "Could not suggest a name yet.");
    } finally { if (pending.current === flight) { pending.current = null; if (alive.current) setBusy(false); } }
  }

  async function back() {
    if (pending.current || activeReceipt) return;
    const current = [...turns].reverse().find((turn) => turn.proposal)?.proposal;
    if (!current || !["pending", "clarify"].includes(current.state || "")) return;
    const owner = renderedOwner; if (!owned(owner)) return;
    const flight = {}; pending.current = flight; historyVersion.current++;
    setBusy(true); setError("");
    try {
      const result = await api(`/api/entry/chat/proposals/${encodeURIComponent(current.id)}/cancel`, { method: "POST" });
      if (!owned(owner) || pending.current !== flight) return;
      if (!result.ok) throw new Error(entryConfirmationError(result.data));
      if ((result.data as { status?: string })?.status !== "cancelled") {
        const saved = (result.data as { proposal?: EntryProposal })?.proposal;
        if (saved?.id === current.id && saved.route === current.route) {
          if (saved.state === "committed" && saved.receipt) {
            const receipt = checkedEntryCommit(saved, saved.receipt);
            updateProposal(null); setActiveReceipt(receipt); rememberReceipt(receipt, ventureName); router.replace(committedChatHref(receipt));
            return;
          }
          updateProposal(saved, [...turns].reverse().find(turn => turn.role === "user")?.text);
          setTurns((items) => items.map((turn) => turn.proposal?.id === saved.id ? { ...turn, proposal: saved } : turn));
        }
        throw new Error(entryConfirmationError(result.data));
      }
      const userIndex = turns.findLastIndex((turn) => turn.role === "user");
      const previous = turns.slice(0, userIndex);
      const prior = [...previous].reverse().find(turn => turn.proposal)?.proposal;
      const original = [...previous].reverse().find((turn) => turn.role === "user")?.text || "";
      setTurns([...previous, ...turns.slice(userIndex).filter(turn => turn.voiceNote)]);
      updateClarification(prior?.state === "clarify" ? { id: prior.id, original, choices: prior.onboarding_question ? prior.onboarding_choices || [] : prior.choices || [], question: prior.onboarding_question || prior.message, field: prior.onboarding_field } : null);
      updateProposal(prior?.state === "pending" ? prior : null, original);
      const restored = savedIdeaChoiceAnswer(prior?.state === "clarify" || prior?.state === "pending" && prior.onboarding_question
        ? turns[userIndex]?.text.split(prior.onboarding_field === "context" ? "\nContext:" : prior.onboarding_field === "customer" || (prior.onboarding_question || prior.message) === "Who is it for?" ? "\nAudience:" : "\nClarification:").at(-1)?.trim() || ""
        : turns[userIndex]?.text || "", prior?.onboarding_question ? prior.onboarding_choices || [] : prior?.choices || []);
      setVentureName((proposal && !proposal.target_venture_id ? draftRef.current.trim() : "") || ventureName || prior?.venture_name || "");
      setDraft(restored.freeText);
      setSelectedChoices(restored.selected);
      previewRequest.current = null; nameRequest.current = null;
    } catch (cause) {
      if (owned(owner)) setError(cause instanceof Error ? cause.message : "Could not go back. Try again.");
    } finally { if (pending.current === flight) { pending.current = null; if (alive.current) setBusy(false); } }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (proposal) void confirm();
    else void answerQuestion(composeIdeaAnswer(selectedChoices, draftRef.current));
  }

  async function answerQuestion(answer: string, voice = false, requestId?: string): Promise<VoiceReply> {
    if (!owned(renderedOwner)) return null;
    if (proposalRef.current) return preview(answer, voice, requestId);
    if (composerDisabled || attachments.uploading || !answer.trim()) return null;
    if (answer.length > IDEA_MAX_CHARS) { setError(TOO_LONG); return null; }
    const text = clarification ? `${clarification.original}\n${clarification.field === "context" ? "Context" : clarification.field === "customer" || clarification.question === "Who is it for?" ? "Audience" : "Clarification"}: ${answer}`
      : answer;
    if (text.length > IDEA_MAX_CHARS) { setError(TOO_LONG); return null; }
    return preview(text, voice, requestId);
  }

  useEffect(() => {
    if (!owned(renderedOwner)) return;
    setCharlieMascot(busy ? "speaking" : draft.trim() ? "listening" : "idle");
  }, [busy, draft, renderedOwner]);
  useEffect(() => () => setCharlieMascot("idle"), [renderedOwner]);

  const voiceControl = <VoiceMic ventureId={null} targetVentureId={targetVentureId} disabled={composerDisabled} compact={home}
    screen={{ page: "/new", step: proposal ? naming ? "Name your venture" : "Review your next step" : clarification?.question || "What is your idea?",
      visible_actions: busy ? ["End voice chat"] : proposal ? ["Continue", ...(!proposal.target_venture_id ? [ventureName.trim() ? "Another name" : "Suggest a name"] : []), "Back"] : clarificationCount > 0 ? ["Continue", "Back"] : ["Continue"],
      expected_next: busy ? "Wait for the reply, or end the call." : proposal ? naming ? "Type a name, then Continue." : "Choose Continue when you are ready." : clarification?.question || "Describe what you want to work on." }}
    onSummary={(text, noteId) => {
      if (!owned(renderedOwner) || !noteId || !/^entry-voice-note_[a-f0-9]{64}$/.test(noteId)) return;
      setTurns(items => items.some(turn => turn.id === noteId) ? items.map(turn => turn.id === noteId ? {...turn, text} : turn) : [...items, {id: noteId, role: "assistant", text, voiceNote: true}]);
    }} onVoiceSend={(text, options) => answerQuestion(text, true, options?.idempotencyKey)} />;

  const [conversationStatus, setConversationStatus] = useState<{ ventureId: string; status: IdeaPageStatus | null } | null>(null);
  const receiveStatus = useCallback((ventureId: string, status: IdeaPageStatus | null) => {
    if (owned(renderedOwner)) setConversationStatus({ ventureId, status });
  }, [renderedOwner]);
  return <AppShell title={activeReceipt ? "Your conversation" : "Start a conversation"} subtitle="Tell Supymem what you want to make or grow." ventureId={activeReceipt?.venture_id || targetVentureId} ideaStatus={conversationStatus?.ventureId === activeReceipt?.venture_id ? conversationStatus?.status || undefined : undefined} fill>
    {missingSelection ? <section className={styles.welcome} aria-label="Choose your saved conversation">
      <h2>Choose your conversation</h2>
      <p>Your saved selection is no longer available. Your unsent text stays saved here. Choose your venture or start a new conversation.</p>
      <WorkspaceSwitcher><span>Choose your venture</span></WorkspaceSwitcher>
      <Link href="/new">Start a new conversation</Link>
      {draft && <><label htmlFor="recovery-draft">Your unsent text</label>
        <textarea id="recovery-draft" value={draft} disabled={!owned(renderedOwner)} onChange={event => setDraft(event.target.value)} /></>}
    </section> : activeReceipt ? historyLoaded && !error
      ? <ConfirmedConversation key={activeReceipt.venture_id} receipt={activeReceipt} initialName={ventureName} initialDirection={savedDirection} onStatusChange={receiveStatus}
          entryVoiceNotes={owned(renderedOwner) && targetVentureId === activeReceipt.venture_id
            ? turns.filter(turn => turn.voiceNote).map(turn => ({id: turn.id, body_md: turn.text})) : []} />
      : <section className={styles.confirmedChat} aria-label="Opening saved conversation" data-webview-pending={!historyLoaded && !error ? "conversation_load" : undefined}>
          {home && <DesktopNoticeBanner ventureId={activeReceipt.venture_id} otherVentureTarget="company-office-notice" />}
          {home && <h2 className={styles.homeTitle}>{ventureName || "Your venture is saved"}</h2>}
          {!home && <header className={styles.context}><div><span className={styles.contextLabel}>Your venture</span>
            <h2>{ventureName || "Your venture is saved"}</h2><p>{activeReceipt.message}</p></div></header>}
          <div className={styles.welcome} role="status"><h2>{error ? "Open your saved conversation" : "Opening your conversation…"}</h2>
            <p>{error || "Reading the latest saved direction."}</p>
            {error && <button type="button" className="primary" onClick={() => { setHistoryLoaded(false); setError(""); void loadHistory(); }}>Try again</button>}
          </div>
        </section>
      : <ChatPanel className={styles.chat} aria-label="Start a venture conversation"
          data-webview-pending={!historyLoaded && !error ? "conversation_load" : undefined}
          data-console-reload-blocked={busy || !!draft || attachments.uploading || attachments.files.length > 0 || selectedChoices.length > 0}>
      {home && <DesktopNoticeBanner ventureId={targetVentureId} messages={turns.map(({ id, role, text }) => ({ id, role, text }))} otherVentureTarget="company-office-notice" />}
      {!targetVentureId && (!clarification || clarification.question?.trim()) && <p className={styles.progress} role="status">{proposal ? naming ? "Name your venture" : "Review your next step" : `Question ${questionNumber}`}</p>}
      <div ref={threadRef} className={composerStyles.messages} role="log" aria-label="Conversation" aria-live="polite"
        onScroll={(event) => {
          // The composer changing size also fires this; only a real scroll means the founder is reading history.
          const {scrollHeight, clientHeight} = event.currentTarget, last = lastScrollSize.current;
          lastScrollSize.current = {scrollHeight, clientHeight};
          if (!last || (last.scrollHeight === scrollHeight && last.clientHeight === clientHeight) || isNearBottom(event.currentTarget)) following.current = isNearBottom(event.currentTarget);
          if (following.current) setNewBelow(false);
        }}>
        {earlier.length > 0 && <button type="button" className={`ghost ${styles.historyToggle}`} onClick={() => setShowEarlier((value) => !value)}>
          {showEarlier ? "Hide earlier conversations" : "Show earlier conversations"}
        </button>}
        {turns.length === 0 && targetVentureId && <section className={styles.welcome} aria-label="Opening saved venture" role="status">
          <h2>{historyLoaded ? "Open your conversation" : "Opening your venture…"}</h2>
          <p>{historyLoaded ? "Your venture is saved. Reload its conversation to continue." : "Reading your conversation."}</p>
          {historyLoaded && <button type="button" className="primary" onClick={() => { setHistoryLoaded(false); setError(""); void loadHistory(); }}>Try again</button>}
        </section>}
        {!proposal && !clarification && !targetVentureId && <div className={styles.welcome}>
          <ChatMessageRow portrait={<CharlieMascot className={composerStyles.imAvatar} active={!turns.some(turn => turn.role === "assistant")} />}>
          <div className={composerStyles.imName}>Charlie</div><div className={composerStyles.bubble}>
          <WelcomeIntro />
          </div></ChatMessageRow>
        </div>}
        {savedDraftHref && <Link href={savedDraftHref}>Continue your draft</Link>}
        {showEarlier && earlier.map((turn, index) => <article key={turn.id} >
          <ChatMessageRow own={turn.role === "user"} portrait={<CharlieMascot className={composerStyles.imAvatar} active={false} />}><div className={composerStyles.imName}>{turn.role === "user" ? "You" : "Charlie"}</div>
          <div className={turn.role === "user" ? composerStyles.bubbleOwn : composerStyles.bubble}>
          <p className={composerStyles.msgText}>{turn.role === "user" ? founderWords(turn.text, earlier.slice(0, index).findLast(item => item.role === "user")?.text) : turn.text}</p>
        </div></ChatMessageRow></article>)}
        {turns.map((turn, index) => turn.role === "assistant" && !turn.text?.trim() && turn.proposal?.state === "clarify" ? null : <article key={turn.id}>
          <ChatMessageRow own={turn.role === "user"} portrait={<CharlieMascot className={composerStyles.imAvatar} active={turn === turns.findLast(message => message.role === "assistant")} />}><div className={composerStyles.imName}>{turn.role === "user" ? "You" : "Charlie"}</div>
          <div className={turn.role === "user" ? composerStyles.bubbleOwn : composerStyles.bubble}>
          {turn.role === "user" && turn.text.length > 1800 ? <details><summary>Your document ({turn.text.length.toLocaleString()} characters)</summary><p className={composerStyles.msgText}>{founderWords(turn.text, turns.slice(0, index).findLast(item => item.role === "user")?.text)}</p></details> : <p className={composerStyles.msgText}>{turn.role === "user" ? founderWords(turn.text, turns.slice(0, index).findLast(item => item.role === "user")?.text) : turn.text}</p>}
          {turn.actions && <div className={composerStyles.actions}>{turn.actions.map(action => <Link key={action.href} href={action.href} className={composerStyles.actionPrimary}>{action.label}</Link>)}</div>}
          {turn.voiceNeedsReview && <p className={styles.muted}>{proposal ? "Your voice notes are saved. Use Back to review changes before continuing with the displayed direction." : "Your voice notes are saved. Review them and type a complete answer below."}</p>}
          {turn.proposal && clarification?.id === turn.proposal.id && Object.keys(turn.proposal.website_answers || {}).length > 0 && <details>
            <summary>Assumptions from your website</summary>
            <p>I'll only ask what's missing. You can correct these before confirming your details.</p>
            {Object.entries(turn.proposal.website_answers || {}).map(([field, answer]) => <p key={field}>{websiteLabels[field] || field}: {answer}</p>)}
          </details>}
          {turn.proposal && resumableEntry(turn.proposal) && proposal?.id === turn.proposal.id && <div className={styles.proposal}>
            {turn.proposal.state === "committing" && <p>The last confirmation is unfinished. Check and continue with this saved direction.</p>}
            {Object.keys(turn.proposal.website_answers || {}).length > 0 && <h2>Here's what I found on your website</h2>}
            {turn.proposal.website_answers?.offer && <p>{turn.proposal.website_answers.offer}</p>}
            {Object.keys(turn.proposal.website_answers || {}).length > 0 && <details>
              <summary>From your website, review or correct</summary>
              <p className={styles.muted}>These are assumptions from your website. You can correct them here. I'll only ask what's missing. Website claims aren't verified customer traction.</p>
              {Object.entries(turn.proposal.website_answers || {}).map(([field, answer]) => <div key={field}>
                <label htmlFor={`website-${field}`}>{websiteLabels[field] || field}, From your website</label>
                <textarea id={`website-${field}`} value={websiteEdits[field] ?? answer} maxLength={2000}
                  disabled={busy || turn.proposal?.state === "committing"} onChange={event => setWebsiteEdits(current => ({ ...current, [field]: event.target.value }))} />
              </div>)}
            </details>}
            {!turn.proposal.target_venture_id && turn.proposal.state !== "committing" && <div className={styles.ventureName}>
              {turn.text.trim() !== "What should we call it?" && <p>{ventureName.trim() ? "You can keep this name or change it." : "What should we call it?"}</p>}
              {ventureName.trim() && <button type="button" className={composerStyles.option} data-usage-control="onboarding_name"
                disabled={composerDisabled || attachments.uploading} onClick={() => void confirm(ventureName)}>{ventureName}</button>}
              <button type="button" className="ghost" disabled={busy} data-usage-control="onboarding_suggest_name" onClick={() => void suggestName()}>{ventureName.trim() ? "Another name" : "Suggest a name"}</button>
            </div>}
            <div className={styles.actions}>
              <button type="button" className="ghost" data-usage-control="onboarding_back" onClick={() => void back()} disabled={busy || turn.proposal.state === "committing"}>Back</button>
            </div>
          </div>}
          {turn.choices && clarification && turn.id === [...turns].reverse().find((item) => item.choices)?.id && <div className={styles.actions}>
            {turn.choices.map((choice) => <button key={choice} type="button" className={composerStyles.option} disabled={busy} aria-label={choice} aria-pressed={selectedChoices.includes(choice)} data-usage-control="onboarding_choice" onClick={() => void answerQuestion(composeIdeaAnswer([choice], draftRef.current))}>{choice}</button>)}
          </div>}
          {turn.choices && !(clarification && turn.id === [...turns].reverse().find((item) => item.choices)?.id) && <p className={styles.muted}>
            Answered
          </p>}
          {turn.proposal?.state === "cancelled" && <p className={styles.muted}>Direction changed</p>}
          {turn.proposal?.state === "committed" && turn.proposal.receipt && <Link href={committedEntryHref(turn.proposal.receipt)}>Open next step →</Link>}
        </div></ChatMessageRow></article>)}
        {busy && pending.current?.text && <article >
          <ChatMessageRow own><div className={composerStyles.imName}>You</div><div className={composerStyles.bubbleOwn}><p>{founderWords(pending.current.text, turns.findLast(turn => turn.role === "user")?.text)}</p>
        </div></ChatMessageRow></article>}
        {busy && <ChatWorking />}
      </div>
      {newBelow && <button type="button" className={styles.newPill} onClick={() => {
        const thread = threadRef.current;
        if (thread) thread.scrollTop = thread.scrollHeight;
        following.current = true; setNewBelow(false);
      }}>New messages ↓</button>}
      <div className={`${composerStyles.composer} ${styles.entryComposer}`}>
        <ErrorNotice error={error} source="new.error" className={styles.error} />
        {!historyLoaded && <p className={styles.muted} role="status">{resume ? "Opening your saved conversation..." : targetVentureId ? "Opening your conversation..." : "Preparing your new conversation..."}</p>}
        {!proposal && <>
        {clarification && !clarification.question?.trim() && <p>The next question did not arrive. <button type="button" disabled={busy} onClick={() => void loadHistory()}>Ask me the questions again</button></p>}
        {draft.length > IDEA_MAX_CHARS * 0.8 && <p className={styles.muted} role="status">{draft.length.toLocaleString()} / {IDEA_MAX_CHARS.toLocaleString()} characters{draft.length > IDEA_MAX_CHARS ? ". Too long to send; trim it to the core idea." : ""}</p>}
        {(clarification || clarificationCount > 0 || turns.some(turn => ["pending", "clarify"].includes(turn.proposal?.state || ""))) && <div className={styles.entryHelpers}>
          {clarification && <p id="entry-answer-hint" role="status">{clarification.choices.length ? "Pick an answer or type your own." : "Type or speak your answer below."}</p>}
          {clarification?.field && <button type="button" className="ghost" data-usage-control="onboarding_skip" disabled={busy} onClick={() => void answerQuestion([composeIdeaAnswer(selectedChoices, draft).trim(), "Skip this question"].filter(Boolean).join("\n"))}>I'm not sure, continue with what I shared</button>}
          {(clarificationCount > 0 || turns.some(turn => ["pending", "clarify"].includes(turn.proposal?.state || ""))) && <button type="button" className="ghost" data-usage-control="onboarding_back" disabled={busy} onClick={() => void back()}>Back</button>}
        </div>}
        </>}
        <form onSubmit={submit} className={composerStyles.composerBox}
          onMouseDown={(event) => {
            if ((event.target as HTMLElement).closest("button, textarea, input, label, details")) return;
            event.preventDefault();
            event.currentTarget.querySelector("textarea")?.focus();
          }}>
          {(!proposal || !proposal.target_venture_id && proposal.state !== "committing") && <>
          <label htmlFor="entry-message" className={styles.srOnly}>{proposal ? "Your venture name" : clarification ? clarification.question || "Answer in your own words" : "What is your idea?"}</label>
          <textarea ref={proposal ? nameInput : answerRef} id="entry-message" data-usage-control={proposal ? "onboarding_name" : "onboarding_answer"} value={draft} maxLength={proposal ? 48 : undefined} onChange={(event) => { fileRead.current++; setDraft(event.target.value); }}
            placeholder={proposal ? "Type your venture name" : clarification ? "Answer in your own words…" : "Example: software that helps hospitals pass compliance audits"}
            rows={3} disabled={composerDisabled} aria-describedby={clarification ? "entry-answer-hint" : undefined}
            onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); }}
            onDrop={(event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); if (!composerDisabled) void attachments.drop(event.dataTransfer); } }}
            onKeyDown={(event) => { if (shouldSendOnKey(event)) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
          </>}
          <div className={composerStyles.composerActions}>
            <div className={composerStyles.composerTools}>
              <details className={composerStyles.attachmentMenu} onKeyDown={event => {
                if (event.key === "Escape") {
                  event.currentTarget.removeAttribute("open");
                  event.currentTarget.querySelector("summary")?.focus();
                }
              }}>
                <summary className={composerStyles.imPlus} aria-label="Attach files or folder" title="Attach files or folder">+</summary>
                <div className={composerStyles.attachmentPopover}>
                  <IntakeAttachments attachments={attachments} disabled={composerDisabled} />
                </div>
              </details>
              {voiceControl}
            </div>
            <div className={composerStyles.composerTools}>
              {(!proposal || !proposal.target_venture_id && proposal.state !== "committing") && <DictateButton scope={`${renderedOwner}:${proposal?.id || clarification?.id || "idea"}`} value={draft} onChange={setDraft} disabled={composerDisabled} />}
              <button type="submit" className="primary" data-usage-control="onboarding_continue" aria-describedby={clarification ? "entry-answer-hint" : undefined} disabled={composerDisabled || attachments.uploading || (proposal ? !proposal.target_venture_id && proposal.state !== "committing" && (!(draft.trim() || ventureName.trim()) || (draft.trim() || ventureName.trim()).length > 48) : !composeIdeaAnswer(selectedChoices, draft).trim() || draft.length > IDEA_MAX_CHARS)}>{busy ? "Working…" : proposal?.state === "committing" ? "Check saved direction and continue" : proposal?.button_label === "Name and find customers" ? proposal.button_label : proposal?.route === "gtm" ? "Continue to find customers" : "Continue"}</button>
            </div>
          </div>
        </form>
      </div>
    </ChatPanel>}
  </AppShell>;
}
