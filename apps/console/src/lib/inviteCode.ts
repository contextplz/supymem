/**
 * Beta invitation code: normalise what founders paste, keep it across the
 * Google/Cognito redirect (and a different browser tab), and explain refusals.
 */
export const INVITE_KEY = "supymem.invite";
const KEEP_MS = 24 * 60 * 60 * 1000;

export const BETA_FULL_MESSAGE = "The Supymem beta is full. Join the waitlist at supymem.com and we will let you know when a seat opens.";

/** Codes are 8 letters/digits; pasted text often carries spaces, dashes or lower case. */
export function normalizeInviteCode(raw: string): string {
  return raw.replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(0, 8);
}

type Store = Pick<Storage, "getItem" | "setItem" | "removeItem">;
function stores(): Store[] {
  if (typeof window === "undefined") return [];
  const out: Store[] = [];
  for (const pick of [() => window.sessionStorage, () => window.localStorage]) {
    try { out.push(pick()); } catch { /* blocked storage: sign-in still works without the memory */ }
  }
  return out;
}

/** sessionStorage covers the same tab; localStorage covers the invite link opened in one tab and the app's browser sign-in in another. */
export function rememberInvite(code: string, all: Store[] = stores(), now = Date.now()): void {
  const value = normalizeInviteCode(code);
  for (const s of all) {
    try { if (value) s.setItem(INVITE_KEY, JSON.stringify({ code: value, at: now })); else s.removeItem(INVITE_KEY); } catch { /* ignore */ }
  }
}

export function recallInvite(all: Store[] = stores(), now = Date.now()): string {
  for (const s of all) {
    try {
      const raw = s.getItem(INVITE_KEY);
      if (!raw) continue;
      let parsed: { code?: unknown; at?: unknown } | null = null;
      try { parsed = JSON.parse(raw); } catch { parsed = { code: raw, at: now }; } // legacy plain string
      const code = typeof parsed?.code === "string" ? normalizeInviteCode(parsed.code) : "";
      if (code && typeof parsed?.at === "number" && now - parsed.at < KEEP_MS) return code;
      s.removeItem(INVITE_KEY);
    } catch { /* ignore */ }
  }
  return "";
}

export function forgetInvite(all: Store[] = stores()): void {
  for (const s of all) { try { s.removeItem(INVITE_KEY); } catch { /* ignore */ } }
}

const INVITE_COPY: Record<string, string> = {
  invite_code_required: "Supymem is invite-only for now. Enter the 8-character invitation code you were sent, then sign in again.",
  invalid_invite_code: "That invitation code was not recognised. Check it for typos (letters and numbers only) or ask whoever sent it to confirm it.",
  invite_code_exhausted: "That invitation code has already been used up. Ask for a new one, then sign in again.",
  self_invitation: "You cannot use your own invitation code. Ask someone else for theirs.",
  already_onboarded: "This account has already joined, so it does not need a code. Clear the invitation field and sign in again.",
  beta_full: BETA_FULL_MESSAGE,
};

export function inviteErrorCode(data: unknown): string | null {
  const payload = data as { error?: { code?: unknown }; detail?: { code?: unknown } } | null;
  const code = payload?.error?.code || payload?.detail?.code;
  return typeof code === "string" ? code : null;
}

/** Specific copy for each known admission refusal, or null when the failure is not about the invitation. */
export function inviteErrorMessage(data: unknown): string | null {
  const code = inviteErrorCode(data);
  return code && Object.hasOwn(INVITE_COPY, code) ? INVITE_COPY[code] : null;
}
