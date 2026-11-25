import { isVerifiedMacTask } from "./macResult.ts";

export type SavedDeviceOrderState = "pending" | "approved" | "claimed" | "waiting_for_device" | "cancelled" | "stopped" | "failed";

type LocalReceipt = { state: string; tool?: string; failure_code?: string; effect_outcome_unconfirmed?: boolean; credits_reconciled?: boolean; task_verified?: boolean } | null;

/** Prefer the live native stop receipt while the saved order refresh catches up. */
export function deviceOrderStatus(
  state: SavedDeviceOrderState,
  options: { expired: boolean; claimedAt?: string; receipt: LocalReceipt },
): string {
  const { expired, claimedAt, receipt } = options;
  if (state === "failed") return "Your build failed before it started. Open your venture chat and ask me to build again.";
  if (receipt?.state === "not_started" && receipt.failure_code === "native_claim_rejected") {
    return "Not started. This task was refused before it began, so nothing ran.";
  }
  if (receipt?.state === "stopped") {
    if (isVerifiedMacTask(receipt)) {
      return "Your task finished and was independently verified. Review its saved result.";
    }
    if (receipt.tool === "coding") return "Your coding task stopped. Review the saved changes and checks; your product result is not yet verified.";
    return "Your task stopped. Review its saved result; its outcome remains unverified.";
  }
  if (state === "pending") return expired
    ? "This review expired. A new request is required."
    : "Awaiting your permission.";
  if (state === "approved") return "Approved. No execution result has been recorded here.";
  if (state === "claimed") return "Claimed by this Mac. Claiming alone does not prove execution or completion.";
  if (state === "waiting_for_device") return "Waiting for your Mac, it resumes when your Mac is awake";
  if (state === "stopped") return "Your task stopped. Review its saved result; its outcome remains unverified.";
  return claimedAt
    ? "Your cancellation is requested. Check the saved status; the task has not yet been verified as stopped."
    : "Cancelled before execution.";
}
