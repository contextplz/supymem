export type GodModeOutcomeReceipt = { outcome: string; display_outcome?: string; completion_verified?: boolean };
const LABELS: Record<string, string> = {
  attempt: "Guarded attempt (budget reservation, not provider charge)",
  prepared: "Prepared for review", queued: "Work queued", held: "Review needed",
  recorded: "Recorded activity (completion unverified)", done: "Recorded activity (completion unverified)",
  blocked: "Left for you", stopped: "Stopped", failed: "Failed", info: "Note",
};
/** Stored historical activity is never upgraded to independently verified completion. */
export function godModeOutcomeLabel(receipt: GodModeOutcomeReceipt): string {
  if (receipt.outcome === "done") return LABELS.recorded;
  const outcome = receipt.display_outcome || receipt.outcome;
  if (outcome === "completed") return receipt.outcome === "completed" && receipt.completion_verified === true
    ? "Verified effect saved" : LABELS.recorded;
  return LABELS[outcome] || "Recorded activity (status unconfirmed)";
}
