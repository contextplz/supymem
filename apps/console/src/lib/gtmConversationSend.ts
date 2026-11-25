import type { ChatMessage } from "./api-types";

const GTM_KINDS = new Set(["gtm_diagnosis_quote", "gtm_diagnosis_pending", "gtm_diagnosis_completed", "gtm_diagnosis_failed"]);
const QUESTION = /^(?:what|why|how|when|where|who|which|is|are|can|could|should|do|does|did|tell me|show me|explain|status|any update|update me)\b/i;
const NEW_DIAGNOSIS = /\b(?:run|start|prepare|quote|redo|repeat)\b.{0,50}\b(?:gtm\s+)?diagnos(?:is|e)\b|\b(?:another|new|fresh)\s+(?:gtm\s+)?diagnos(?:is|e)\b/i;

export function gtmSendMode(body: string, messages: Pick<ChatMessage, "kind">[]): "quote" | "followup" | "general" {
  const text = body.trim();
  const latest = [...messages].reverse().find(message => GTM_KINDS.has(message.kind || ""));
  const awaitingDiagnosis = latest?.kind === "gtm_diagnosis_quote" || latest?.kind === "gtm_diagnosis_pending";
  if (NEW_DIAGNOSIS.test(text)) return awaitingDiagnosis ? "followup" : "quote";
  if (awaitingDiagnosis && (/\b(?:diagnosis|quote|run)\s+(?:status|progress|update)\b|\b(?:status|progress|update)\s+(?:of|on|for)\s+(?:the\s+)?(?:diagnosis|quote|run)\b/i.test(text)
    || /^(?:what(?:'s| is)(?: the)?|any)\s+(?:status|progress|update)\??$/i.test(text))) return "followup";
  if (!latest && !QUESTION.test(text) && !text.endsWith("?")) return "quote";
  return "general";
}
