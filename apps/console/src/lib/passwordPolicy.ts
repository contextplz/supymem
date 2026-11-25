/** Hosted account password rules. Keep this aligned with gateway users.password_policy_code. */

export const PASSWORD_REQUIREMENTS =
  "At least 12 characters, and not a password known to be in public breach lists.";

const SIGNUP_PASSWORD_MESSAGES: Record<string, string> = {
  password_too_short: "Use a password with at least 12 characters.",
  password_breached: "That password appears in a public breach list. Choose a different one.",
  email_taken: "An account with that email already exists. Sign in instead.",
  invalid_email_or_password: "Enter a valid email address.",
  auth_mail_unavailable: "Account email delivery is not enabled yet, so Supymem cannot safely create an account that you cannot verify.",
};

export function accountSignupError(code?: string | null): string | null {
  if (!code) return null;
  return SIGNUP_PASSWORD_MESSAGES[code] || null;
}

export function passwordSetError(code?: string | null): string | null {
  if (code === "password_too_short" || code === "password_breached") {
    return SIGNUP_PASSWORD_MESSAGES[code];
  }
  return null;
}

export function passwordResetError(code?: string | null): string {
  if (code === "auth_mail_unavailable") {
    return "Password reset email isn't available right now, contact support";
  }
  return "Could not request a password reset. Please try again.";
}
