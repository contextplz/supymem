import type { ChatMessage } from "../../lib/api-types";

type ChatAction = NonNullable<ChatMessage["actions"]>[number] & { navigation?: boolean };

export function isQuestionReply(message: ChatMessage) {
  return Boolean(message.citations?.some(item => item?.question === true)
    || message.kind === "question" || message.kind === "idea_clarification_ready"
    || message.kind !== "error" && message.actions?.some(action => {
      const questionId = (action.payload as { question_id?: unknown } | null)?.question_id;
      return action.action === "chat.reply" && typeof questionId === "string" && Boolean(questionId.trim());
    }));
}

function repliesToQuestion(action: ChatAction) {
  const questionId = (action.payload as { question_id?: unknown } | null)?.question_id;
  return (action.action || action.name) === "chat.reply" && typeof questionId === "string" && Boolean(questionId.trim());
}

function pagePreview(action: ChatAction) {
  return (action.action || action.name) === "site.preview"
    || typeof action.href === "string" && /^\/v\/[^/]+\/site(?:[?/#]|$)/.test(action.href);
}

export function latestPageMessage(thread: ChatMessage[]) {
  return thread.findLast(message => message.role !== "founder" && message.role !== "user"
    && (message.kind === "site_preview" || [...message.actions || [], ...message.next_moves?.actions || []].some(pagePreview)));
}

export function liveActions<T extends ChatAction>(message: ChatMessage, thread: ChatMessage[], index: number, answeredHere: boolean, actions: T[], showInlinePage = false): T[] {
  const codingMessage = (item: ChatMessage) => ["build_tool_started", "mac_task_stopped", "mac_task_finished"].includes(item.kind || "")
    || item.actions?.some(action => typeof action.payload?.order_id === "string" || /\/v\/[^/?#]+\/(?:device-work-orders\/[^/?#]+|builds)(?:[/?#]|$)/.test(action.href || ""));
  const codingTask = (item: ChatMessage) => item.actions?.map(action => action.task_id || action.payload?.task_id).find(task => typeof task === "string");
  if (codingMessage(message) && thread.slice(index + 1).some(later => codingMessage(later)
    && (!codingTask(message) || !codingTask(later) || codingTask(message) === codingTask(later)))) actions = actions.filter(action => (action.action || action.name) !== "status");
  if (latestPageMessage(thread.slice(index + 1)) || showInlinePage) {
    actions = actions.filter(action => !pagePreview(action));
  }
  if (!isQuestionReply(message)) return actions;
  const closed = answeredHere || thread.slice(index + 1).some(later => later.role === "founder" || later.role === "user"
    || later.actions?.some(repliesToQuestion));
  return closed ? actions.filter(action => (action.action || action.name) !== "chat.reply") : actions;
}

export function founderAction(action: ChatAction, messageId: string, state = action.confirmation?.state, local = false): ChatAction {
  const { confirmation, ...ordinary } = action;
  if (!confirmation) return action;
  const name = confirmation.action || action.action || action.name || "";
  if (local) {
    const consent = /^(?:outreach\.|autopilot\.|approvals\.|inbox\.briefing\.consent$|gtm\.resume_sending$)/.test(name)
      || /(?:^|[._])(?:spend_cap|send_cap)(?:[._]|$)/.test(name)
      || confirmation.capability === "cap.autopilot.send" || Boolean(confirmation.resulting_state?.memory_conflict);
    if (consent) return { ...action, action: name, confirmation: { ...confirmation, state } };
    if (state === "started" || state === "starting") return { label: "Check status", action: "status", style: action.style };
  }
  if (state === "starting") return { label: "Check status", action: "status", style: action.style };
  return { ...ordinary, name, action: name,
    label: state === "started" ? "Started" : /^(?:confirm|approve|allow|do it)\b/i.test(action.label) ? "Start" : action.label,
    payload: { ...confirmation.resulting_state, [name === "gtm.diagnosis.start" ? "quote_message_id" : "confirmation_message_id"]: messageId },
    disabled_reason: state === "started" ? "You already started this. Ask me how it's going."
      : state === "denied" ? "You chose not to start this. Ask me what to do next." : action.disabled_reason };
}

function canonicalPayload(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalPayload);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right))
      .map(([key, entry]) => [key, canonicalPayload(entry)]));
  }
  return value;
}

export function chatActionIdentity(action: ChatAction) {
  const payload = action.confirmation?.resulting_state ?? action.payload
    ?? (action.task_id ? { task_id: action.task_id } : action.text ? { text: action.text } : {});
  return JSON.stringify([action.confirmation?.action || action.href || action.action || action.name || action.label,
    canonicalPayload(payload)]);
}

export function planChatActions(actions: ChatAction[], questionSubmit = false, question = false, local = false) {
  const isApproval = (action: ChatAction) => Boolean(action.confirmation)
    || /(?:^|[._])(?:approve|confirm)(?:[._]|$)/.test(action.action || action.name || "");
  const priority = (action: ChatAction) => action.confirmation ? 0 : isApproval(action) ? 1
    : (action.action || action.name) === "recovery.submit" ? 2
    : (action.navigation || action.href) && !(local && action.style === "primary") ? 5 : action.style === "primary" ? 3 : 4;
  const seen = new Set<string>();
  const ordered = [...actions].sort((left, right) => question ? 0 : priority(left) - priority(right)).filter(action => {
    const key = chatActionIdentity(action);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const confirmations = ordered.filter(action => action.confirmation);
  const approvals = ordered.filter(action => !action.confirmation && isApproval(action));
  const ordinary = ordered.filter(action => !isApproval(action));
  // Consent stays visible even when it exceeds the three-action budget.
  const recovery = local && ordinary.some(action => (action.action || action.name) === "recovery.alternative"
    || /(?:^|\.)retry$/.test(action.action || action.name || ""));
  const slots = question ? ordinary.length : recovery ? 1 : Math.max(0, 3 - confirmations.length - approvals.length - Number(questionSubmit));
  return {
    confirmations,
    visible: question ? ordered.filter(action => !action.confirmation) : [...approvals, ...ordinary.slice(0, slots)],
    overflow: ordinary.slice(slots),
    primary: question ? undefined : confirmations[0] ?? (questionSubmit ? undefined : approvals[0] ?? ordinary[0]),
  };
}
