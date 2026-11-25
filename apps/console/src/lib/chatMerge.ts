import type { ChatMessage } from "./api-types";

const isLocal = (m: ChatMessage) => m.id.startsWith("local-");
// The server stores founder lines as role "user"; the client shows them as "founder".
const side = (m: ChatMessage) => (m.role === "founder" || m.role === "user" ? "f" : m.role);
const key = (m: ChatMessage) => {
  const href = m.actions?.find((a) => a.label === "Review Mac task")?.href;
  return `${side(m)}|${href ?? (m.body_md ?? m.text ?? "").trim()}`;
};

/**
 * Merge a saved-history page into the on-screen list. Never drops a server
 * message; a local optimistic copy (local-*) is replaced in place by its saved
 * twin so each message shows once. `refresh` lets saved copies overwrite same-id rows.
 */
export function mergeChat(current: ChatMessage[], saved: ChatMessage[], refresh = false): ChatMessage[] {
  const have = new Set(current.map((m) => m.id));
  const rid = (m: ChatMessage) => m.client_request_id || (m.role === "assistant" ? m.reply_to_message_id : undefined);
  // Saved rows carrying the nonce of a local optimistic row are that row's twin, even if already on screen.
  const requestKey = (message: ChatMessage) => `${side(message)}|${rid(message)}`;
  const savedByRid = new Map(saved.filter(rid).map((m) => [requestKey(m), m]));
  const fresh = new Map<string, ChatMessage[]>();
  for (const m of saved) if (!have.has(m.id)) fresh.set(key(m), [...(fresh.get(key(m)) ?? []), m]);
  const claimed = new Set<string>();
  const out: ChatMessage[] = [];
  for (const m of current) {
    if (isLocal(m)) {
      const byId = rid(m) ? savedByRid.get(requestKey(m)) : undefined;
      if (byId && !claimed.has(byId.id)) {
        claimed.add(byId.id);
        if (have.has(byId.id)) continue; // saved twin already shown: drop the local copy
        out.push(byId); continue;
      }
      const twin = fresh.get(key(m))?.find((s) => !claimed.has(s.id) && (!rid(m) || !rid(s) || rid(m) === rid(s)));
      if (twin) { claimed.add(twin.id); out.push(twin); continue; }
    }
    const replacement = refresh ? saved.find((s) => s.id === m.id) : undefined;
    out.push(replacement ? {...replacement, ...(m.reply_to_message_id ? {reply_to_message_id: m.reply_to_message_id} : {}),
      ...(m.reply_request_ids ? {reply_request_ids: m.reply_request_ids} : {})} : m);
  }
  const added = saved.filter((m) => !have.has(m.id) && !claimed.has(m.id));
  const all = [...out, ...added];
  // One row per saved id (a repeated id is the same message, never a repeated answer).
  const seen = new Set<string>();
  const unique = all.filter((m) => (seen.has(m.id) ? false : (seen.add(m.id), true)));
  const latestIdea = unique.findLastIndex(message => ["entry_pending", "idea_clarification_pending", "idea_failed", "idea_clarification_ready", "idea_validation_ready", "idea_validation_failed"].includes(message.kind || ""));
  const ready = latestIdea >= 0 && ["idea_failed", "idea_clarification_ready", "idea_validation_ready", "idea_validation_failed"].includes(unique[latestIdea].kind || "");
  const settled = ready ? unique.filter(message => !message.id.startsWith("local-idea-wait-")) : unique;
  const timestamp = (message: ChatMessage) => Date.parse(message.ts || message.created_at || "");
  const chronological = [...settled].sort((left, right) => {
    if (isLocal(left) || isLocal(right)) return 0;
    const difference = timestamp(left) - timestamp(right);
    return Number.isFinite(difference) ? difference : 0;
  });
  const replies = new Map<string, ChatMessage>();
  const pending = (message: ChatMessage) => /(?:_pending|_progress)$/.test(message.kind || "");
  for (const message of chronological) {
    if (message.role !== "assistant" || !rid(message)) continue;
    const previous = replies.get(rid(message)!);
    if (!previous) replies.set(rid(message)!, message);
    else if (pending(previous)) replies.set(rid(message)!, {...message,
      ...(previous.reply_to_message_id && !message.reply_to_message_id ? {reply_to_message_id: previous.reply_to_message_id} : {}),
      ...(previous.reply_request_ids ? {reply_request_ids: [...new Set([...(previous.reply_request_ids || []), ...(message.reply_request_ids || [])])]} : {})});
    else if (!pending(message)) {
      const previousBody = previous.body_md || previous.text || "";
      const messageBody = message.body_md || message.text || "";
      const stamp = /^<!--employee:[^:]+:[^:]*:[^>]*-->\n?/;
      const previousStamp = previousBody.match(stamp)?.[0] || "";
      const messageStamp = messageBody.match(stamp)?.[0] || "";
      const body = previousBody.slice(previousStamp.length);
      const followup = messageBody.slice(messageStamp.length);
      const combined = !followup || body === followup || body.startsWith(`${followup}\n\n`) || body.endsWith(`\n\n${followup}`) ? body
        : !body || followup.startsWith(`${body}\n\n`) || followup.endsWith(`\n\n${body}`) ? followup : `${body}\n\n${followup}`;
      replies.set(rid(message)!, {...previous, ...message, body_md: (messageStamp || previousStamp) + combined,
        ...(previous.reply_request_ids ? {reply_request_ids: [...new Set([...(previous.reply_request_ids || []), ...(message.reply_request_ids || [])])]} : {}),
        parts: [{type: "text", text: combined}, ...[...(previous.parts || []), ...(message.parts || [])].filter((part, index, parts) =>
          part.type !== "text" && parts.findIndex(candidate => JSON.stringify(candidate) === JSON.stringify(part)) === index)],
        citations: [...(previous.citations || []), ...(message.citations || [])].filter((citation, index, citations) =>
          citations.findIndex(candidate => JSON.stringify(candidate) === JSON.stringify(citation)) === index),
        actions: [...(previous.actions || []), ...(message.actions || [])].filter((action, index, actions) =>
          actions.findIndex(candidate => JSON.stringify(candidate) === JSON.stringify(action)) === index)});
    }
  }
  const ordered = chronological.filter(message => message.role !== "assistant" || !rid(message)
    || replies.get(rid(message)!)?.id === message.id).map(message =>
      message.role === "assistant" && rid(message) ? replies.get(rid(message)!)! : message);
  for (const reply of replies.values()) {
    // A reply is only placed after an older row, so a replayed receipt stays before later results.
    const earlier = (id: string) => {
      const target = ordered.find(message => message.id === id);
      const following = chronological[chronological.findIndex(message => message.id === id) + 1];
      const request = target?.client_request_id || (following?.role === "assistant" ? rid(following) : rid(reply));
      return target !== undefined && !(timestamp(target) > timestamp(reply))
        && (side(target) !== "f" || !reply.client_request_id || request === rid(reply));
    };
    const anchor = [ordered.find(message => message.reply_request_ids?.includes(rid(reply)!))?.id,
      reply.confirmed_action?.message_id, reply.reply_to_message_id,
      ordered.find(message => side(message) === "f" && rid(message) === rid(reply))?.id]
      .find(id => id && id !== reply.id && earlier(id));
    if (!anchor) continue;
    const position = ordered.findIndex(message => message.id === reply.id);
    ordered.splice(position, 1);
    ordered.splice(ordered.findIndex(message => message.id === anchor) + 1, 0, reply);
  }
  const previousById = new Map(current.map(message => [message.id, message]));
  const stable = ordered.map(message => {
    const previous = previousById.get(message.id);
    return previous && JSON.stringify(previous) === JSON.stringify(message) ? previous : message;
  });
  return stable.length === current.length && stable.every((message, index) => message === current[index]) ? current : stable;
}
