// Live voice transcript -> chat turns (pure helpers).
import { stripEmDash } from "./copy.ts";
export type VoiceTurn = { id: string; role: "founder" | "assistant"; text: string; final: boolean };

/** Apply a transcript delta/final: updates the open turn of that role in place, else starts a new one. */
export function applyCaption(
  turns: VoiceTurn[], role: VoiceTurn["role"], text: string, final: boolean, nextId: () => string,
): { turns: VoiceTurn[]; turn: VoiceTurn } {
  const at = turns.findLastIndex((t) => t.role === role && !t.final);
  if (at < 0) {
    const turn = { id: nextId(), role, text: stripEmDash(text), final };
    return { turns: [...turns, turn], turn };
  }
  const turn = { ...turns[at], text: stripEmDash(final ? text : turns[at].text + text), final };
  return { turns: turns.map((t, i) => (i === at ? turn : t)), turn };
}
