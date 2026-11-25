/** Plain-language reason and next step for a failed task Run; never the raw transport code. */
export function taskRunFailure(
  status: number,
  code: string,
  serverMessage: string,
): { message: string; unconfirmed: boolean } {
  if (status === 0 || code.endsWith("outcome_unknown")) {
    return {
      message:
        "The connection dropped before this task reported back, so it may still be running or already done. The list below shows its current status; if it is still open, press Run to try again.",
      unconfirmed: true,
    };
  }
  if (status === 409 || code === "venture_paused") {
    return { message: serverMessage || "This venture is paused. Resume it, then press Run.", unconfirmed: false };
  }
  return { message: serverMessage || "Couldn’t run that task. Press Run to try again.", unconfirmed: false };
}
