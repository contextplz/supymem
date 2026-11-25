/** Only native auth state can decide whether a failed Mac session check ends the UI session. */
export function nativeSessionNeedsSignIn(state: string | undefined): boolean {
  return ["signed_out", "authorizing", "reauth_required", "account_mismatch", "revocation_pending"].includes(state || "");
}
