/**
 * Display-side credit units. One credit is one US cent (100 credits = $1).
 * The API transports integer CREDITS; micro-credits stay server-side.
 * Conversion must stay in this module — do not multiply by 100 elsewhere.
 */

import { isLocalConsole } from "./localMode.ts";

export const CREDITS_PER_USD = 100;
const MICRO_PER_CREDIT = 10_000;

/** Precise usage receipts use the ledger's integer micro-credit unit. */
export function formatMicroCreditUsage(micro: number): string {
  if (!Number.isSafeInteger(micro) || micro < 0) return "Usage unavailable";
  const credits = micro / MICRO_PER_CREDIT;
  if (credits > 0 && credits < 0.01) return "Under 0.01 credits";
  return `${credits.toLocaleString("en-US", { maximumFractionDigits: 2 })} credits`;
}

const MICRO_PER_USD = MICRO_PER_CREDIT * CREDITS_PER_USD;

/** Founder spend view: ledger micro-credits shown as dollars ("$0.25", "$3"). */
export function formatMicroDollars(micro: number): string {
  return `$${(micro / MICRO_PER_USD).toFixed(2).replace(/\.00$/, "")}`;
}

export function dollarsToMicro(usd: number): number {
  return Math.round(usd * MICRO_PER_USD);
}

/** Spend line for an experiment: spent vs its founder cap. */
export function experimentSpendLine(spend: { spent_micro: number; cap_micro: number | null }): string {
  return spend.cap_micro == null ? `Spent ${formatMicroDollars(spend.spent_micro)}, no cap`
    : `Spent ${formatMicroDollars(spend.spent_micro)} of ${formatMicroDollars(spend.cap_micro)} cap`;
}

function asInt(n: number): number {
  if (!Number.isFinite(n)) return 0;
  return Math.trunc(n);
}

/** e.g. 1500 → "1,500 Credits" */
export function formatCredits(credits: number): string {
  const n = asInt(credits);
  return `${n.toLocaleString("en-US")} Credits`;
}

/** e.g. 1500 → "1,500 Credits"; 498.79 → "498.79 Credits" */
export function formatCreditBalance(credits: number): string {
  if (!Number.isFinite(credits)) return "0 Credits";
  const rounded = Math.round(credits * 100) / 100;
  const display =
    Math.abs(rounded - Math.round(rounded)) < 0.005
      ? Math.round(rounded).toLocaleString("en-US")
      : rounded.toLocaleString("en-US", {
          minimumFractionDigits: 0,
          maximumFractionDigits: 2,
        });
  return `${display} Credits`;
}

/** e.g. 1500 → "$15.00" */
export function creditsToUsd(credits: number): string {
  const n = asInt(credits);
  const usd = n / CREDITS_PER_USD;
  const sign = usd < 0 ? "-" : "";
  return `${sign}$${Math.abs(usd).toFixed(2)}`;
}
export const USAGE_LIMIT_MESSAGE = "You have reached your usage limit. Buy more in Settings, then retry.";
export const BUY_MORE_HREF = "/account#account-buy";
export const LOCAL_USAGE_LIMIT_MESSAGE = "Your service account has reached its limit. Add funds or raise your limit there, then try again.";
export const AI_SETTINGS_HREF = "/settings/local#providers-title";

export function usageLimitMessage(local = isLocalConsole()): string {
  return local ? LOCAL_USAGE_LIMIT_MESSAGE : USAGE_LIMIT_MESSAGE;
}
