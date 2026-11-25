/** Retain recovery input until the server confirms an authenticated admission. */
export function inviteAfterAuthentication(current: string, result: { ok: boolean; data: unknown }): string {
  const session = result.data as { authenticated?: unknown } | null;
  return result.ok && session?.authenticated === true ? "" : current;
}
