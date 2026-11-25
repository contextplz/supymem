import type { ConnectionsHubCard } from "@/lib/opsApi";

const COPY: Record<string, { purpose: string; setup: string; action: string }> = {
  composio: { purpose: "Links your social and email accounts.", setup: "Account connections are not available on this installation yet.", action: "View details" },
  gmail: { purpose: "Your Gmail, all ventures you own, 15 emails/day total.", setup: "Connecting email is not available on this installation yet.", action: "Set up email" },
  google_calendar: { purpose: "Review your meetings and plan customer interviews.", setup: "Connecting your calendar is not available on this installation yet.", action: "Connect" },
  linkedin: { purpose: "Prepare LinkedIn posts and outreach.", setup: "Connecting LinkedIn is not available on this installation yet. You can still copy drafts and post them yourself.", action: "View setup" },
  discord: { purpose: "Share updates in your Discord community.", setup: "Connecting Discord is not available on this installation yet.", action: "View setup" },
  reddit: { purpose: "Research and join Reddit conversations.", setup: "Connecting Reddit is not available on this installation yet.", action: "View setup" },
  x: { purpose: "Prepare posts for X.", setup: "Connecting X is not available on this installation yet.", action: "View setup" },
  unipile: { purpose: "Read and reply to LinkedIn messages.", setup: "This connection is not available on this installation yet.", action: "View requirements" },
  hunter: { purpose: "Find work emails for the people you research.", setup: "Email enrichment is not available on this installation yet.", action: "View requirements" },
  google_ads: { purpose: "Draft Google ads for you to approve.", setup: "Connect this only when you are ready to run paid acquisition. It is not needed for Supymem's core workflow.", action: "View requirements" },
  meta_ads: { purpose: "Draft Facebook and Instagram ads for you to approve.", setup: "Connect this only when you are ready to run paid acquisition. It is not needed for Supymem's core workflow.", action: "View requirements" },
};

function key(card: ConnectionsHubCard) { return String(card.id || card.name || "").toLowerCase().replace(/[ /-]+/g, "_").replace("twitter", "x"); }
type ToolkitConnectionCard = ConnectionsHubCard & { toolkit_slug?: string; experiment_goals?: string[]; accounts?: { id: string }[] };
type ConnectionPresentation = { purpose: string; setup: string; action: string; toolkit?: ToolkitConnectionCard; visible?: boolean };
export function toolkitConnection(card: ConnectionsHubCard): ToolkitConnectionCard | null {
  const toolkit = card as ToolkitConnectionCard;
  return toolkit.toolkit_slug === card.id && typeof toolkit.toolkit_slug === "string" ? toolkit : null;
}
export function connectionVisible(card: ConnectionsHubCard) { return !toolkitConnection(card) || card.configured === true; }
export function connectionCopy(card: ConnectionsHubCard): ConnectionPresentation {
  if ((card as ConnectionsHubCard & { default?: boolean }).default && card.id === "smtp") {
    return { purpose: "You use SMTP email by default.", setup: card.hint || "Your SMTP settings are configured. You can use your own email server without connecting an app.", action: "View setup" };
  }

  const toolkit = toolkitConnection(card);
  if (toolkit) return { purpose: card.note || "Charlie uses this connection to review your test results.", setup: "This connection is not available on this installation yet.", action: "Connect", toolkit, visible: connectionVisible(card) };
  return COPY[key(card)] || { purpose: "An optional extra for this company.", setup: "This connection is not available on this installation yet.", action: "View setup" };
}
export async function disconnectToolkitAccounts(card: ConnectionsHubCard, revoke: (id: string) => Promise<{ id: string; status: string }>, current: () => boolean) {
  const accounts = toolkitConnection(card)?.accounts;
  if (!Array.isArray(accounts) || !accounts.length || accounts.some(account => !account || typeof account.id !== "string" || !account.id)) throw new Error("Refresh your connection status before disconnecting.");
  for (const account of accounts) {
    if (!current()) return false;
    const revoked = await revoke(account.id);
    if (!current()) return false;
    if (!revoked || revoked.id !== account.id || revoked.status !== "revoked") throw new Error("Disconnection was not confirmed. Refresh your connection status and try again.");
  }
  return true;
}
export function connectedAccountChannels(cards: ConnectionsHubCard[]) {
  const accountChannels = cards.filter(card => typeof card.connected === "boolean");
  return {
    connected: accountChannels.filter(card => card.connected === true && Boolean(card.ready ?? card.ok)).length,
    total: accountChannels.length,
  };
}
export function connectionState(card: ConnectionsHubCard) {
  if (card.needs_reconnect) return "Needs reconnect";
  const ready = Boolean(card.ready ?? card.ok);
  if (typeof card.connected === "boolean") {
    if (card.connected) return ready ? "Connected" : "Needs reconnect";
    return ready ? "Ready to connect" : "Not connected";
  }
  return ready ? "Configured" : "Not configured";
}
export function connectionActionLabel(card: ConnectionsHubCard) { if (card.needs_reconnect) return "Reconnect"; if (card.action === "sync") return card.id === "gmail" || toolkitConnection(card) ? "Refresh status" : "Sync now"; if (card.action === "connect" && !card.paused_reason?.startsWith("composio:")) return "Connect"; return connectionCopy(card).action; }
export function connectionSetupMessage(card: ConnectionsHubCard) {
  if (card.action === "connect" && card.connectable) {
    return "Use Connect to prepare account authorization. The connection is ready only after you complete authorization and refresh its status.";
  }
  const copy = connectionCopy(card);
  return `${copy.setup} ${card.docs ? "You can open the setup guide for the next step." : ""}`.trim();
}
