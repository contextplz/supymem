/** Founder-facing build usage line. Hide zero awaiting on terminal runs. */
export function buildUsageSummary(parts: {
  reserved: string;
  consumed: string;
  awaiting: string;
  unresolvedMicro: number | null | undefined;
  terminal: boolean;
}): string {
  const base = `Reserved: ${parts.reserved} · Consumed: ${parts.consumed}`;
  const awaiting = parts.unresolvedMicro == null ? null : Number(parts.unresolvedMicro);
  if (awaiting == null) return base;
  if (parts.terminal && awaiting === 0) return base;
  return `${base} · Awaiting reconciliation: ${parts.awaiting}`;
}
