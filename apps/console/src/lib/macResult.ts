type Review = { verdict?: string; visible_facts?: string[]; next_step?: string } | null | undefined;
type Receipt = { task_verified?: boolean; failure_code?: string; effect_outcome_unconfirmed?: boolean } | null;

/** A verified flag cannot override failure or uncertain native effects. */
export function isVerifiedMacTask(receipt: Receipt): boolean {
  return receipt?.task_verified === true && !receipt.failure_code && !receipt.effect_outcome_unconfirmed;
}

/** Plain founder-facing result line for a finished Mac task; never exposes outcome or driver codes. */
export function macResultHeadline(review: Review, receipt: Receipt = null): string {
  const facts = (review?.visible_facts || []).map((fact) => String(fact).trim().replace(/[.\s]+$/, "")).filter(Boolean).slice(0, 3);
  const verified = isVerifiedMacTask(receipt);
  if (!facts.length) return verified
    ? "Supymem independently verified the reviewed Mac task."
    : "Supymem stopped without confirming the result. Check the Mac app before reviewing another task.";
  const text = `${facts.join(". ")}.`;
  if (receipt?.failure_code || receipt?.effect_outcome_unconfirmed) {
    return `Supymem stopped with an unconfirmed result. What it saw: ${text}`;
  }
  return verified ? `Verified, ${text}` : `Here is what Supymem saw: ${text}`;
}

export function macWrongPrefill(title: string, headline: string): string {
  return `The Mac task “${title}” didn't work as I expected. Supymem reported: ${headline} What went wrong?`;
}
