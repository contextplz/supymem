import { BETA_FULL_MESSAGE, inviteErrorMessage } from "./inviteCode.ts";

export { BETA_FULL_MESSAGE };

/** Only known verified-identity admission failures receive invitation recovery guidance. */
export function invitationRecoveryMessage(data: unknown): string | null {
  return inviteErrorMessage(data);
}
