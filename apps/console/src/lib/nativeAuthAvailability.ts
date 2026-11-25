export const nativeAuthAvailability = {
  secure_store_pending: "Waiting for macOS secure credential access. Respond to the Supymem Keychain prompt; this app will continue automatically. If the prompt is behind another window, bring it forward using macOS. Supymem cannot approve it for you.",
  native_auth_busy: "An account request is finishing. Wait for it to complete before starting another action.",
  restoring: "Restoring your session…",
  keychain_unavailable: "Secure credential storage is unavailable. Unlock your macOS Keychain and reconnect.",
} as const;

export function nativeAuthAvailabilityMessage(code: unknown): string | undefined {
  return typeof code === "string" && Object.hasOwn(nativeAuthAvailability, code)
    ? nativeAuthAvailability[code as keyof typeof nativeAuthAvailability] : undefined;
}

export function nativeAuthActionPending(state: string | undefined): boolean {
  return state === "secure_store_pending" || state === "native_auth_busy" || state === "restoring";
}
