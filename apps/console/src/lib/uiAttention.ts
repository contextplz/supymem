/** Closed attention categories. No API response body or secret enters this event. */
export const UI_ATTENTION_EVENT = "supymem-ui-attention";
export type AttentionKind = "provider_connection" | "mac_permission";

const shownAt = new Map<AttentionKind, number>();

export function attentionKind(data: unknown): AttentionKind | null {
  if (!data || typeof data !== "object") return null;
  const root = data as { error?: { code?: unknown }; detail?: { error?: { code?: unknown }; code?: unknown } };
  const code = root.error?.code ?? root.detail?.error?.code ?? root.detail?.code;
  if (typeof code !== "string") return null;
  if (/(?:provider|api|integration|composio|connector|account|credential|gmail|linkedin).*(?:missing|not_ready|not_connected|key_required|setup_required|reconnect_required)|^(?:gmail|google_ads|meta_ads)_env$/i.test(code)) return "provider_connection";
  if (/(?:screen_recording|accessibility|mac_permission|permission_required)/i.test(code)) return "mac_permission";
  return null;
}

export function announceAttention(data: unknown): void {
  if (typeof window === "undefined") return;
  const kind = attentionKind(data);
  if (!kind) return;
  const now = Date.now();
  if (now - (shownAt.get(kind) || 0) < 15 * 60_000) return;
  shownAt.set(kind, now);
  window.dispatchEvent(new CustomEvent(UI_ATTENTION_EVENT, { detail: { kind } }));
}
