type Turn = { role: string; kind: string; options?: unknown };

/** A GStack office-hours question is asked while the idea is validating. */
export function isGstackQuestion(turn: Turn | undefined): boolean {
  const options = turn?.options;
  return Boolean(options && !Array.isArray(options) && typeof options === "object" && (options as { phase?: string }).phase === "gstack");
}

// Conversation history is not the current prompt. A founder answer or an
// agent proposal consumes the previous question, even after a reload.
export function pendingJourneyQuestion<T extends Turn>(state: string, turns: T[]): T | undefined {
  const latest = turns.at(-1);
  const open = ["open", "recon", "clarifying"].includes(state) || (state === "validating" && isGstackQuestion(latest));
  if (!open) return undefined;
  return latest?.role === "agent" && latest.kind === "question" ? latest : undefined;
}
